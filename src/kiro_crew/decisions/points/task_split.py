"""``task.split`` -- should this request be done inline, delegated, or split up?

Today the main agent alone decides how much of a request it keeps. It can answer
inline, hand the whole thing to one sub-agent (``spawn_run``), or cut it into
independent pieces and run several at once (``spawn_sub_agents``); the dashboard
draws a card per sub-agent either way (``chat_runner._native_subagent_sync``,
``handlers/messaging.api_spawn``). Nothing asks the question before the turn
starts, so a request that wanted three parallel workers is often answered by one
long serial reply, and a one-line question is sometimes delegated for no reason.

An ADVISORY, and only that
--------------------------
The answer becomes ONE system-side line prepended to the turn's context -- the
same pure-prepend channel a regenerate hint and hook output travel -- and the
agent still decides. The question carries what the PERSON typed, never the
turn's assembled text: an expanded prompt or skill file is not a request anybody
made, and a shape suggestion about one would leave local file contents with the
provider. Nothing here spawns anything, cancels anything, or changes a
tool's arguments: :func:`hint_line` returns text, and the caller's only use of it
is a string concatenation. That is the property that makes the point admissible
at all, and the reason the record calls its own arm a SUGGESTION and the agent's
arm the outcome.

Both arms, one line
-------------------
The oracle's arm is the answer. The BASELINE arm is what the agent actually did,
which is why it is recorded when the turn FINALIZES rather than when the question
is asked: it is read from how many sub-agents the turn actually STARTED
(:func:`agent_choice_for` over :func:`helpers_started`), so the row says whether the
suggestion matched behaviour without anybody reporting on themselves. Helpers, not
calls and not the tool name: BOTH spawn tools can start several at once
(``spawn_sub_agents`` takes an ``agents`` batch, ``spawn_run`` a ``tasks`` list), so
either of those readings would record three parallel helpers as one delegation. That comparison is the whole
point of the row -- an advisory nobody can score is an advisory nobody can
withdraw.

Everything is a refusal back to today's turn
--------------------------------------------
:func:`suggest` returns ``None`` for: the seam off, the session unsampled, the
answer outside the three options, the transport failed, the budget expired. ``None``
means "prepend nothing", which is byte-identical to the turn this build runs today,
and :func:`record_outcome` is then never called, so a refusal leaves no row on the
reply either. A caller needs no try/except and no feature check of its own.

The record rides ``decisions.outcomes``
--------------------------------------
:func:`record_outcome` hands the row it wrote to that registry, the way both
sibling points do. It holds one outcome per POINT and ``consume`` returns the
LIST, so this receipt sits BESIDE whatever else decided the turn and reaches the
reply through the one consumer the seam has (``meta.decisions_strip``).

Published when the turn FINALIZES rather than during prompt assembly, because the
baseline arm is not settled until the turn stops calling spawn tools. A turn that
produced no assistant text publishes a row no reply claims, and the next turn's
``outcomes.discard`` drops it.

Runs on the event loop
----------------------
Both halves are awaited by ``_run_chat`` itself, so there is no cross-thread
hand-off: ``gate.decide`` bounds the provider call by ``timeout_secs`` and this
module bounds the whole call by :func:`wait_budget`. The row's append is pushed to
a thread, because a synchronous write here would put a lock wait and a filesystem
write on the loop that serves every other session.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
import uuid
from typing import Any, Mapping, Sequence

from kiro_crew import decisions as core
from kiro_crew.decisions import log as _log
from kiro_crew.decisions.points import build_history, history_budget
from kiro_crew.decisions.types import Answer, Choice, Question

logger = logging.getLogger(__name__)

POINT = "task.split"

#: The question's id, and therefore the key the answer arrives under.
QUESTION_ID = "shape"

#: The three shapes a turn can take, in increasing fan-out. The ORDER is part of
#: the contract: a reader comparing two rows needs to know which way "more
#: parallel" runs without consulting prose.
CHOICE_SINGLE = "single"
CHOICE_DELEGATE = "delegate"
CHOICE_SPLIT = "split"
CHOICES: tuple[str, ...] = (CHOICE_SINGLE, CHOICE_DELEGATE, CHOICE_SPLIT)

#: The rubric, sent as the question's prompt. One sentence per option, because the
#: options are the answer domain and a domain nobody defined is a domain every
#: provider reads differently. It names the EFFECT on the work rather than the
#: tools, so the answer does not turn on whether a provider has heard of
#: ``spawn_run``.
PROMPT = (
    "How should the assistant take on this request? "
    f"Answer {CHOICE_SINGLE} when one worker should do it from start to finish, "
    "because the steps depend on each other or the whole job is small. "
    f"Answer {CHOICE_DELEGATE} when it is one self-contained job worth handing to "
    "a single helper working on its own. "
    f"Answer {CHOICE_SPLIT} when it holds two or more parts that do not need each "
    "other's results, so several helpers could work at the same time."
)

#: Characters of the request sent with the question -- the SAME bound the other
#: points apply, because it is the same kind of text answering a question about
#: the same turn, and two different excerpt sizes would mean the consent text
#: describes one of them.
MAX_MESSAGE_CHARS = 2000

#: How many rows a point may read (``points.MAX_HISTORY_MESSAGES``) and how many
#: characters of them may leave the machine (``points.history_budget``) are the
#: package's own bounds, spent through its ``build_history``: one bound for the
#: whole seam, because a second copy would be a second thing to keep equal.

#: Scheduling slack on top of the provider budget, and the floor and ceiling the
#: wait is clamped into. The ceiling is the real protection: this budget is spent
#: before the turn's first token, so a hand-edited ``timeout_ms`` must not hold a
#: reply there. Held equal to the values every other point in this package waits
#: by (``test_decisions_task_split.py``) so no point invents its own ceiling.
WAIT_MARGIN_SECS = 0.5
MIN_WAIT_SECS = 0.25
MAX_WAIT_SECS = 10.0

#: How long the outcome row's write may hold the caller, on top of the provider
#: budget. One ``O_APPEND`` of a few hundred bytes, so this exists only so a
#: stalled filesystem cannot make an observation cost the reply. Named and valued
#: as ``gate._LOG_BUDGET_SECS`` is, because it is the same write on the same loop.
LOG_BUDGET_SECS = 0.05

#: The two sub-agent spawn tools, named APART because each one means a different
#: shape: ``spawn_run`` starts one helper, and ``spawn_sub_agents`` is the batch
#: tool whose ``agents`` list IS the parallel path. So the arm is read from the tool
#: NAME rather than from a call count -- a single batch call carrying three agents is
#: the canonical way to split work, and counting calls would record it as a
#: delegation.
TOOL_DELEGATE = "spawn_run"
TOOL_SPLIT = "spawn_sub_agents"
SPAWN_TOOLS = frozenset({TOOL_DELEGATE, TOOL_SPLIT})


def questions() -> list[Question]:
    """The one question, with the three shapes as its whole domain.

    ONE ``Choice``, because the answer is consumed as one line of advice: a second
    question would be a second thing to reconcile inside a single hint.
    """
    return [Choice(QUESTION_ID, PROMPT, options=list(CHOICES))]


def spawn_tool_named(mcp_server_name: str, tool_name: str) -> str:
    """Which spawn tool a recorded CALL is, or ``""`` for anything else.

    Both arguments MUST come from the out-of-band ``_meta.kiro`` channel
    (``mcpServerName`` / ``toolName``), never the model-authored title. A shell tool
    has no MCP server name and a canonical tool name like ``execute_bash``, so it is
    not a spawn; neither is a third-party server that merely exposes a tool named
    ``spawn_run``. Absent identity fails CLOSED, which here means "not credited":
    this is the arm the suggestion is scored against, so a value the model can write
    must not be able to move it.

    The two checks are ``session_directive.core_tool_named`` -- the one place the
    server check and the qualified-name normalization are written -- rather than a
    private copy, so this boundary cannot silently diverge from the forgery gate's.
    """
    from kiro_crew.session_directive import core_tool_named

    return core_tool_named(mcp_server_name, tool_name, SPAWN_TOOLS)


def _spawn_names(spawn_tools: object) -> list[str]:
    """*spawn_tools* as the :data:`SPAWN_TOOLS` names it holds, in order. Never raises.

    Anything that is not a sequence of strings yields no names, and a name outside
    the set is dropped: the caller reports what the turn called, and a value that is
    not one of the two tools cannot describe a shape.

    A ``str`` is refused as a whole rather than iterated: a bare ``"spawn_run"``
    would otherwise decompose into single characters and read as no spawn, which is
    a silent wrong arm rather than a visible caller error.
    """
    if isinstance(spawn_tools, (str, bytes)) or not isinstance(spawn_tools, Sequence):
        return []
    return [name for name in spawn_tools if isinstance(name, str) and name in SPAWN_TOOLS]


#: The most helpers one call is credited with. A bound on a number that reaches a
#: row and a rendered line, not a limit on the tool: a call that started more still
#: reads as a split, which is the only thing the arm turns on.
MAX_HELPERS_PER_CALL = 64

#: How ``spawn_run`` announces what it started: ``mcp_tools/spawn.py`` emits
#: ``f"Spawned {len(agent_ids)} subagent(s). ..."`` and emits it ONLY when
#: ``agent_ids`` is non-empty, so its absence is how a denied or wholly failed call
#: reads. The FALLBACK reading -- ``spawn_sub_agents`` returns structured rows
#: instead and is counted from those. Pinned against the producer's own wording by
#: ``test_decisions_task_split.py``, so a change there turns a test red rather than
#: silently zeroing every count.
_SPAWNED_RE = re.compile(r"\bSpawned\s+(\d{1,4})\s+subagent", re.IGNORECASE)

#: Keys on a ``spawn_sub_agents`` result row. That tool returns ``"\n\n".join`` of
#: ``json.dumps`` objects, NOT prose: one PER-AGENT row carrying ``agent`` for each
#: helper that reached a terminal state, plus at most one aggregate row listing the
#: ids still running under ``task_ids``. A helper appears in exactly one of the two,
#: so adding them is a count of what started -- and an aggregate row that is neither
#: (a resume note, a ``spawn_errors`` list of entries that never started) adds
#: nothing, which is the behaviour a denial needs.
_ROW_AGENT_KEY = "agent"
_ROW_RUNNING_IDS_KEY = "task_ids"


def helpers_started(tool: str, result: object) -> int:
    """How many sub-agents one spawn call actually STARTED, from its own result.

    The RESULT rather than the call, because the call says what was asked for and only
    the result says what ran. Two cases the call cannot distinguish: both spawn tools
    fan out (``spawn_sub_agents`` takes an ``agents`` batch, ``spawn_run`` a ``tasks``
    list), so a call count reads three helpers as one; and a spawn the person DENIED,
    or that failed to start, started none at all while its call frame looks identical
    to a successful one.

    The two tools answer in different SHAPES, so both are read. ``spawn_sub_agents``
    returns structured rows (:func:`_helpers_from_rows`) and is counted from them;
    ``spawn_run`` returns prose and is counted from its own announcement. Structure is
    tried first and the prose is the fallback, so neither tool depends on the other's
    format: a batch carries no announcement to find, and reading only prose would score
    every one of them zero.

    ``0`` for a tool that is not a spawn, and 0 when neither reading finds a helper --
    which is exactly what a denial produces. Failing to 0 is the right direction here:
    this number says what RAN, and crediting a helper to a refused call would report
    work that never happened.

    Bounded by :data:`MAX_HELPERS_PER_CALL`. Never raises: this runs on the turn path.
    """
    if tool not in SPAWN_TOOLS:
        return 0
    try:
        text = result if isinstance(result, str) else ""
        if not text:
            return 0
        rows = _helpers_from_rows(text)
        if rows is not None:
            return max(0, min(MAX_HELPERS_PER_CALL, rows))
        match = _SPAWNED_RE.search(text)
        if match is None:
            return 0
        started = int(match.group(1))
    except Exception:
        return 0
    return max(0, min(MAX_HELPERS_PER_CALL, started))


def _helpers_from_rows(text: str) -> int | None:
    """Helpers named by a STRUCTURED spawn result, or ``None`` when it is not one.

    ``spawn_sub_agents`` answers with ``json.dumps`` objects joined by a blank line,
    so there is no count to read in prose -- a regex over its result finds nothing and
    would record a whole batch as zero. Each helper that reached a terminal state has
    its own row carrying :data:`_ROW_AGENT_KEY`; the ones still running are named
    together in an aggregate row's :data:`_ROW_RUNNING_IDS_KEY`. A helper is in exactly
    one of those, so the two add.

    ``None`` -- not 0 -- when nothing here parses as a row, which is what hands
    ``spawn_run``'s prose to the fallback instead of scoring it zero. A result that
    DOES parse but names no helper returns 0, because that is a real answer: a
    ``spawn_errors`` row lists entries that never started.

    Never raises.
    """
    started = 0
    parsed_any = False
    for chunk in text.split("\n\n"):
        chunk = chunk.strip()
        if not chunk.startswith("{"):
            continue
        try:
            row = json.loads(chunk)
        except (TypeError, ValueError):
            continue
        if not isinstance(row, dict):
            continue
        parsed_any = True
        if row.get(_ROW_AGENT_KEY):
            started += 1
            continue
        running = row.get(_ROW_RUNNING_IDS_KEY)
        if isinstance(running, Sequence) and not isinstance(running, (str, bytes)):
            started += sum(1 for entry in running if entry)
    return started if parsed_any else None


def agent_choice_for(spawn_helpers: object) -> str:
    """Which of :data:`CHOICES` the agent's own behaviour amounts to.

    The number of sub-agents that actually RAN, because that is what the question
    asked about: none is :data:`CHOICE_SINGLE`, one is :data:`CHOICE_DELEGATE`, and
    two or more working at once is :data:`CHOICE_SPLIT`.

    Helpers rather than CALLS, and helpers rather than the tool NAME, because neither
    of those can count what ran. Every spawn tool can start several at once -- both
    ``spawn_sub_agents`` (an ``agents`` batch) and ``spawn_run`` (a ``tasks`` list) --
    so a call count reads three parallel helpers as one delegation, and a tool name
    reads ``spawn_run(tasks=[a, b])`` as one too. Counting what started is the only
    reading that gets all of those right, and ``agree`` is the field this has to be
    trustworthy about.

    A call that started NOTHING -- denied by the person, or failed -- contributes no
    helpers, so it moves neither the shape nor the count. The agent asked; the arm
    records what the turn DID.

    An unusable value reads as none rather than raising (:func:`_helper_total` applies
    the same rule): this runs while a reply is being persisted.
    """
    started = _helper_total(spawn_helpers, 0)
    if started <= 0:
        return CHOICE_SINGLE
    return CHOICE_DELEGATE if started == 1 else CHOICE_SPLIT


def wait_budget() -> float:
    """How long one call may take, clamped into a sane window. Never raises."""
    try:
        budget = float(core.timeout_secs()) + WAIT_MARGIN_SECS
    except Exception:
        logger.debug("task.split: provider budget unreadable", exc_info=True)
        return MIN_WAIT_SECS
    if not math.isfinite(budget):
        return MIN_WAIT_SECS
    return min(max(budget, MIN_WAIT_SECS), MAX_WAIT_SECS)


def message_excerpt(text: str) -> str:
    """The part of the request that actually leaves the machine, after the cap.

    One function so the count on the row and the string in the request cannot
    disagree: :func:`build_state` sends this and :func:`message_chars` measures the
    same call.
    """
    return (text or "")[:MAX_MESSAGE_CHARS]


def message_chars(text: str) -> int:
    """Characters of *text* that were sent, which is the excerpt's own length."""
    return len(message_excerpt(text))


def build_state(
    text: str,
    history: Sequence[Mapping[str, Any]] | None = None,
    *,
    history_budget_chars: int | None = None,
    trace: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The state sent to the oracle: this request, and the prior turns.

    History is built by the package's own ``build_history`` rather than by a second
    walk here: it is the same prior conversation spent out of the same consented
    ceiling, with the same two roles and the same newest-first order, and two
    nearly-identical walks would be two things to keep equal. ``history`` is
    OMITTED when there is none, so the request at the shipped default of 0 carries
    the request alone.

    *trace* receives ``history_chars`` and ``truncated`` from that builder, so the
    row can state the egress it actually paid for.
    """
    state: dict[str, Any] = {"message": message_excerpt(text)}
    rows = build_history(history, text, history_budget_chars=history_budget_chars, trace=trace)
    if rows:
        state["history"] = [dict(entry) for entry in rows]
    return state


def read_choice(answers: Any) -> str:
    """The shape the answer names, or ``""``. Identity is exact.

    The gate has already held the value against the declared options, so this is
    the second check rather than the only one -- and it is here because the caller
    renders it into prose the agent reads: a near-miss spelling would become a
    hint naming a shape nobody offered.
    """
    if not isinstance(answers, dict):
        return ""
    answer = answers.get(QUESTION_ID)
    if not isinstance(answer, Answer):
        return ""
    value = answer.value
    return value if isinstance(value, str) and value in CHOICES else ""


def probability_of(answers: Any) -> float | None:
    """The answer's probability, or ``None``. Only read after :func:`read_choice`."""
    if not isinstance(answers, dict):
        return None
    answer = answers.get(QUESTION_ID)
    return answer.p if isinstance(answer, Answer) else None


#: How the hint names each shape to the agent. One clause each, in the agent's own
#: vocabulary, so the line is actionable without the agent having to map a bare
#: word back onto a tool. English only and NOT localised: this text is prompt
#: input for a model, not interface copy for a person, and a translated hint would
#: make the advice a function of the reader's UI language.
_HINT_TEXT = {
    CHOICE_SINGLE: "handle this request yourself in this turn, without sub-agents",
    CHOICE_DELEGATE: "hand this request to a single sub-agent",
    CHOICE_SPLIT: "split this request into independent sub-tasks and run them in parallel",
}


def hint_line(decided: Mapping[str, Any]) -> str:
    """The one advisory line the caller prepends, or ``""``.

    Deliberately phrased as a SUGGESTION and marked as Jev's, because the agent
    still decides and a line that read as an instruction would make an advisory a
    silent gate. The probability is included for the same reason the strip prints
    it: an agent told how sure the suggestion is can discount a weak one, and a
    number nobody can see is a number nobody can weigh.

    ``""`` for an unreadable choice, so a caller that skipped :func:`read_choice`
    still prepends nothing rather than a half-sentence.
    """
    choice = str(decided.get("choice") or "")
    text = _HINT_TEXT.get(choice)
    if not text:
        return ""
    p = decided.get("p")
    if isinstance(p, (int, float)) and not isinstance(p, bool) and math.isfinite(p):
        return f"Jev suggests: {text} ({float(p):.2f}). You decide."
    return f"Jev suggests: {text}. You decide."


async def suggest(
    text: str,
    *,
    session_key: str | None = None,
    history: Sequence[Mapping[str, Any]] | None = None,
    config: Any | None = None,
) -> dict[str, Any] | None:
    """Jev's suggested shape for this request, or ``None`` to prepend nothing.

    Returns ``{turn_id, choice, p, latency_ms}``. ``choice`` is one of
    :data:`CHOICES`; no answer at all is ``None`` rather than a choice of
    :data:`CHOICE_SINGLE`, so a caller can tell "Jev said do it inline" from
    "nothing was asked or nothing came back" -- the first is advice with a receipt,
    the second is the turn this build runs today.

    *history* is prior transcript rows (``{role, content}``, newest LAST), already
    bounded by the caller's read. Passed as a value rather than a callable because
    this runs on the loop and the caller's read is its own to schedule; at the
    shipped ceiling of 0 nothing from it is sent.

    Never raises except :class:`asyncio.CancelledError`, which ``decide``
    propagates: cancellation is the turn going away, not a decision failure.
    """
    turn_id = uuid.uuid4().hex[:16]
    # Bound before the try so the latency is always measurable, including when the
    # ceiling read and the history build were what took the time.
    started = time.monotonic()
    try:
        # The ceiling FIRST, off the loop: it reads the keystone as well as the
        # config, and at the shipped default of 0 there is nothing for the history
        # build to contribute.
        budget = await asyncio.to_thread(history_budget)
        trace: dict[str, Any] = {}
        state = build_state(
            text,
            history if budget > 0 else (),
            history_budget_chars=budget,
            trace=trace,
        )
        extra: dict[str, Any] = {
            "turn_id": turn_id,
            "message_chars": message_chars(text),
            "history_chars": trace.get("history_chars", 0),
        }
        answers = await asyncio.wait_for(
            core.decide(
                POINT, state, questions(), session_key=session_key, config=config, extra=extra
            ),
            timeout=wait_budget(),
        )
    except asyncio.CancelledError:
        raise
    except asyncio.TimeoutError:
        # The gate writes its own row for a provider timeout; this arm is the
        # caller-side budget expiring around it, which means the gate is still
        # inside its own and will record whatever it finds.
        logger.debug("task.split: the call outlived the caller's budget")
        return None
    except Exception:
        # This sits before the turn's first token, so the seam may cost an
        # observation and must never cost the reply.
        logger.debug("task.split: prepending no hint", exc_info=True)
        return None
    choice = read_choice(answers)
    if not choice:
        return None
    return {
        "turn_id": turn_id,
        "choice": choice,
        "p": probability_of(answers),
        "latency_ms": int((time.monotonic() - started) * 1000),
    }


def build_outcome(
    decided: Mapping[str, Any],
    spawn_tools: "Sequence[str] | None",
    spawn_helpers: object = None,
) -> dict[str, Any]:
    """The fields the outcome row and the strip line share.

    ``agent_choice`` is derived from the spawn tool NAMES rather than reported, and
    ``agree`` is derived from the two words rather than asserted, so no field here
    can disagree with the one beside it.

    Three numbers, because they answer three questions a reader actually has.
    ``spawn_helpers`` is how many sub-agents ran: it is what the line prints and what
    ``agent_choice`` is derived from, so one call starting three helpers is "3
    spawned" and a split, never "1" and a delegation. ``spawn_calls`` is how many
    times the turn reached for a spawn tool. ``spawn_tools`` is WHICH route it took,
    which an auditor of ``agree`` needs in order to check the derivation. Absent
    *spawn_helpers* falls back to one per started call, so an older caller records a
    number that is at least never larger than the truth.

    ``latency_ms`` is deliberately NOT here: it is a core row field
    (:func:`~kiro_crew.decisions.log.build_row`), so the row carries it at top level
    and an ``extra`` naming it would be dropped.
    """
    names = _spawn_names(spawn_tools)
    jev_choice = str(decided.get("choice") or "")
    helpers = _helper_total(spawn_helpers, len(names))
    agent_choice = agent_choice_for(helpers)
    return {
        "turn_id": decided.get("turn_id"),
        "jev_choice": jev_choice,
        "agent_choice": agent_choice,
        "spawn_calls": len(names),
        "spawn_helpers": helpers,
        "spawn_tools": names,
        "agree": jev_choice == agent_choice,
        "p": decided.get("p"),
    }


def _helper_total(spawn_helpers: object, calls: int) -> int:
    """Sub-agents the turn started, or one per call. Never raises.

    A ``bool`` is not a count, for the reason ``gate._probability`` excludes one from
    being a probability, and neither is a negative -- both read as UNMEASURED and take
    the per-call fallback rather than 0, because a turn that called a spawn tool
    started at least one helper and 0 would read as "no spawn" on a turn that plainly
    spawned. The fallback is what keeps this number conservative when nothing measured
    it: one per call is never larger than the truth.
    """
    if isinstance(spawn_helpers, bool) or not isinstance(spawn_helpers, int):
        return max(0, calls)
    if spawn_helpers < 0:
        return max(0, calls)
    return spawn_helpers


async def record_outcome(
    *,
    session_key: str | None,
    decided: Mapping[str, Any],
    spawn_tools: "Sequence[str] | None",
    spawn_helpers: object = None,
) -> dict[str, Any] | None:
    """Write one outcome row for the finished turn and RETURN it. Never raises.

    Called when the turn FINALIZES, which is the earliest moment the baseline arm
    exists: the agent's own shape is which spawn tools it called, and those are only
    all in once the turn stops calling them.

    ``None`` means no row was written -- the build raised, ``append`` refused it
    (the day-file ceiling, a sealed directory), or the write outlived
    :data:`LOG_BUDGET_SECS`. Nothing is published in that case, the rule the other
    points already follow: a line whose durable row was refused names a
    ``turn_id`` no verdict could be filed against, and the thumbs on it POST that
    id.

    A written row is PUBLISHED exactly as written, so what the transcript shows and
    what the log holds cannot drift into two descriptions of one turn.

    OFF THE LOOP: this is awaited from ``_run_chat``, so a synchronous append would
    put a lock wait and a filesystem write on the gateway's event loop.
    """
    try:
        row = _log.build_row(
            point=POINT,
            session_key=session_key,
            latency_ms=int(decided.get("latency_ms") or 0),
            extra=build_outcome(decided, spawn_tools, spawn_helpers),
        )
        written = await asyncio.wait_for(asyncio.to_thread(_log.append, row), LOG_BUDGET_SECS)
    except asyncio.CancelledError:
        raise
    except asyncio.TimeoutError:
        # The worker thread is NOT cancellable, so the append may still land
        # afterwards -- acceptable for a write that cannot corrupt a line. What
        # this arm refuses is the LINE, because the caller stopped waiting for the
        # row a verdict would be filed against.
        logger.debug("task.split: outcome row outlived its write budget; stamping nothing")
        return None
    except Exception:
        logger.debug("task.split: could not record the outcome row", exc_info=True)
        return None
    if not written:
        logger.debug("task.split: outcome row was not written; publishing nothing")
        return None
    try:
        # Imported INSIDE the call, the reason both sibling points resolve it late:
        # the registry is optional, so a build without it publishes nothing instead
        # of making this point unimportable.
        from kiro_crew.decisions.outcomes import publish

        publish(session_key, row)
    except Exception:
        logger.debug("task.split: could not publish the outcome", exc_info=True)
    return row
