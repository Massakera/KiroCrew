"""Evidence for the ``nudge.wake`` judge, and the one call an auto-nudge tick makes.

The point module (``decisions.points.nudge_wake``) owns the questions, the bounded
state and the mapping. This module owns WHERE the evidence comes from: a watched
session's new transcript rows, a watched pull request's observation, and (later)
work-ledger events. It sits beside ``autonudge`` rather than under ``decisions``
because collecting is a gateway concern -- it reads live slot state -- while the
point stays a pure adapter that a test can drive with literals.

Why the reads arrive as CALLABLES
---------------------------------
``AutoNudgeService`` holds no ``DashboardState``: it is constructed with a data
directory and two callbacks, and the gateway injects closures that reach the rest
(``on_fire``, ``on_monitor_tick``, ``owner_session_id``). The session read is the
same shape, and it has to be, because authorizing it needs state the service
cannot see. So :func:`collect_evidence` takes readers and this module imports no
dashboard module at all -- which is also what lets every function here be tested
without a gateway.

Creator-only, enforced by reuse
-------------------------------
A ``session`` target is read through ``session_control.read_messages``, which
authorizes with ``authorize_target`` before returning a row: deny-by-default, and
SEL-audited on refusal. The judge therefore cannot read a session its owning loop
could not read by hand, and a target that refuses is DROPPED and counted rather
than fetched. Nothing here re-implements that check, because a second copy of an
authorization rule is a second place for it to be wrong.

Assistant rows only
-------------------
``decisions.points.HISTORY_ROLES`` excludes tool output from every other point on
the grounds that it is the largest and least selective text in a transcript and
routinely quotes files nobody mentioned. The same reasoning holds here, and the
evidence a watcher actually needs -- a worker's ``RULING:`` or ``BLOCKED:`` line,
a reviewer's verdict -- is an assistant row. Tool rows are skipped.
"""

from __future__ import annotations

import logging
import math
import re
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Mapping, Sequence

from kiro_crew import validation as _validation
from kiro_crew.decisions.points import nudge_wake as point

logger = logging.getLogger(__name__)

#: A dashboard chat-slot key, the spelling a `judge.targets` entry uses for a
#: session. Anchored and bounded: the value is owner-supplied and becomes a
#: `read_messages` target, so it is matched rather than trusted.
SESSION_TARGET_RE = re.compile(r"\Achat-[A-Za-z0-9][A-Za-z0-9-]{0,127}\Z")

#: The same shape, found INSIDE prose, so a loop whose instruction names the
#: sessions it watches needs no second spelling of them in ``judge.targets``. The
#: anchored pattern above still screens every hit, so this only decides where to
#: look, never what counts.
SESSION_TARGET_IN_TEXT_RE = re.compile(r"\bchat-[A-Za-z0-9][A-Za-z0-9-]{0,127}\b")

#: How many transcript rows one target contributes to a single tick. The char
#: budget in the point is what bounds egress; this bounds the READ, so a session
#: that produced a hundred rows between ticks cannot turn one decision into a
#: whole-transcript scan.
MAX_ROWS_PER_TARGET = 12

#: How many targets one loop may name. Bounds the number of authorizations and
#: reads a single tick performs. Spelled once, in ``validation``, where the arming
#: surface refuses an oversized brief: a copy here would be a second number to keep
#: in step, and a disagreement would silently drop targets the arming call accepted.
MAX_TARGETS = _validation.MAX_JUDGE_TARGETS

#: Transcript roles whose text is evidence. Assistant only -- see the module
#: docstring for why tool rows are excluded.
EVIDENCE_ROLES = frozenset({"assistant"})


def parse_targets(spec: Mapping[str, Any] | None, message: str = "") -> list[str]:
    """The targets to collect from: the spec's own list, else what *message* names.

    An explicit ``targets`` list adds or narrows; without one the loop's
    instruction is read the way the PR probe already reads it, so arming a judge
    on a loop that already names a pull request needs no second spelling of the
    subject.

    Only recognised shapes survive: a ``chat-*`` key matching
    :data:`SESSION_TARGET_RE`, or a string a pull-request target can be inferred
    from. Anything else is dropped rather than passed to a reader, because these
    strings come from the owner's tool call.
    """
    raw: list[str] = []
    if isinstance(spec, Mapping):
        listed = spec.get("targets")
        if isinstance(listed, (list, tuple)):
            raw = [str(item) for item in listed if isinstance(item, str)]
    out: list[str] = []
    for item in raw:
        value = item.strip()
        if not value or value in out:
            continue
        if SESSION_TARGET_RE.fullmatch(value) or _pr_subject(value):
            out.append(value)
        if len(out) >= MAX_TARGETS:
            break
    if out:
        return out
    # Nothing explicit, so read the instruction the way the design says: the targets
    # default to what the MESSAGE names. Both shapes, not just pull requests -- a
    # conductor's instruction names the sessions it patrols, and requiring those to be
    # repeated in ``targets`` would mean a brief that looks armed and watches nothing.
    for match in SESSION_TARGET_IN_TEXT_RE.findall(message or ""):
        if match not in out and SESSION_TARGET_RE.fullmatch(match):
            out.append(match)
        if len(out) >= MAX_TARGETS:
            break
    if _pr_subject(message):
        # The whole message, because pull-request inference reads the original
        # spelling (it carries the host a URL-armed watch is entitled to) rather
        # than a token lifted out of it.
        stripped = message.strip()
        if stripped not in out:
            out.append(stripped)
    return out


def is_session_target(value: str) -> bool:
    """Whether *value* names a dashboard chat slot rather than a pull request."""
    return SESSION_TARGET_RE.fullmatch(value or "") is not None


def session_evidence(
    rows: Sequence[Mapping[str, Any]],
    target: str,
    *,
    now_ts: float | None = None,
) -> list[dict[str, Any]]:
    """New assistant rows from one watched session, newest first, as evidence items.

    Each row's ``ts`` becomes the item's age, so the point can order and drop by
    recency. A row without a usable timestamp reads as age 0 -- treating it as the
    newest thing available, which keeps it in the request under a tight budget
    rather than silently dropping evidence because a row lacked a field.
    """
    clock = point.now() if now_ts is None else now_ts
    items: list[dict[str, Any]] = []
    for row in list(rows)[-MAX_ROWS_PER_TARGET:]:
        if not isinstance(row, Mapping):
            continue
        if str(row.get("role", "") or "") not in EVIDENCE_ROLES:
            continue
        text = row.get("content", "")
        if not isinstance(text, str) or not text.strip():
            continue
        items.append(
            {
                "source": f"session:{target}",
                "kind": point.KIND_TRANSCRIPT_TAIL,
                "age_s": _age_from_ts(row.get("ts"), clock),
                "text": text,
            }
        )
    return items


#: How many check identities one rendered bucket names before it reports a
#: remainder. The canonical object carries up to
#: ``MAX_MONITOR_CHECK_IDENTITIES_PER_BUCKET`` of them, which would spend the whole
#: item budget on lane names; a criterion asks WHETHER a lane is red and usually
#: which one, so a handful plus a count answers it and leaves room for the rest of
#: the reading.
MAX_RENDERED_CHECK_IDENTITIES = 8


def _render_check_bucket(checks: Mapping[str, Any], state: str) -> str:
    """``failed 3 (lint, tests-2, e2e)`` for one bucket, or ``""`` when empty."""
    values = checks.get(state)
    if not isinstance(values, (list, tuple)) or not values:
        return ""
    names = [str(value) for value in values if isinstance(value, str) and value.strip()]
    if not names:
        return f"{state} {len(values)}"
    shown = names[:MAX_RENDERED_CHECK_IDENTITIES]
    remainder = len(names) - len(shown)
    listed = ", ".join(shown)
    if remainder > 0:
        listed = f"{listed}, +{remainder} more"
    return f"{state} {len(names)} ({listed})"


def render_pr_summary(observation: Mapping[str, Any]) -> str:
    """The canonical pull-request facts as one line of prose, or ``""``.

    The rendering lives HERE, in the judge's own collector, because this is its only
    consumer. The monitor's canonical fact object is pinned by full-dict equality
    tests and hashed into the wake fingerprint, so a field added there to serve one
    reader changes what every provider must emit; a reading assembled from the keys
    the probe already published costs nothing and stays local to the reader.

    Only keys the object declares are read, and a key whose value has the wrong type
    is skipped rather than coerced: the judge is better served by a shorter true
    reading than by a field it cannot trust.
    """
    parts: list[str] = []
    for key in ("state", "mergeability", "review_decision", "blocking_review"):
        value = observation.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(f"{key}={value.strip()}")
    draft = observation.get("draft")
    if isinstance(draft, bool):
        parts.append(f"draft={'yes' if draft else 'no'}")
    threads = observation.get("unresolved_review_threads")
    if isinstance(threads, int) and not isinstance(threads, bool):
        parts.append(f"unresolved_review_threads={threads}")
    checks = observation.get("checks")
    if isinstance(checks, Mapping):
        # Failed and pending first, and named: those are the buckets an owner's
        # criterion asks about. Passed and unknown are counted only.
        for state in ("failed", "pending"):
            rendered = _render_check_bucket(checks, state)
            if rendered:
                parts.append(f"checks {rendered}")
        for state in ("passed", "unknown"):
            values = checks.get(state)
            if isinstance(values, (list, tuple)) and values:
                parts.append(f"checks {state} {len(values)}")
    # A partial reading must say so: a criterion about red checks means something
    # different when the check list itself is incomplete, and the judge cannot see
    # that from the tallies.
    for key in ("checks_complete", "review_threads_complete"):
        value = observation.get(key)
        if value is False:
            parts.append(f"{key}=no")
    head = observation.get("head_revision")
    if isinstance(head, str) and head.strip():
        parts.append(f"head={head.strip()[:12]}")
    digest = observation.get("pr_comment_body_digest")
    if isinstance(digest, str) and digest.strip():
        # Opportunistic, and a FINGERPRINT rather than text: the reader that sees
        # PR-level comment bodies reduces each to a fixed width and retains no body,
        # so this says discussion exists and carries none of it.
        parts.append(f"pr_comments_fingerprint={digest.strip()[:12]}")
    if not parts:
        return ""
    return "; ".join(parts)


def _pr_identity(value: str) -> tuple[str, str, str] | None:
    """*value*'s subject as ``(kind, subject, host)``, or ``None``. Never raises.

    The host is part of the identity: the same repository slug on two servers is
    two different pull requests, which is the case a slug comparison would miss.
    """
    if not value or not value.strip():
        return None
    try:
        from kiro_crew.probes import targets as _targets

        inferred = _targets.infer(value)
    except Exception:
        logger.debug("nudge.wake: target inference unavailable", exc_info=True)
        return None
    if inferred is None:
        return None
    return (inferred.kind, inferred.subject, inferred.host_key)


def pr_observation_is_about(
    target: str,
    *,
    monitor_kind: str,
    monitor_target: str,
    observation: Mapping[str, Any] | None = None,
) -> bool:
    """Whether *target* names the same pull request the observation is a reading of.

    A loop holds ONE monitor, so its reader answers with that monitor's single
    observation whatever subject it is asked for. The brief's ``targets`` are the
    owner's own strings, so a brief may name a SECOND pull request -- and the row
    would then be labelled with the name it asked for while carrying the watched
    subject's state. A judge could rule quiet on facts about a different pull
    request, which is the one way this path can suppress a turn that was owed.

    Two ways to agree, because the two sides are spelled by different writers. An
    identical string is unambiguous. Otherwise both are put through the same
    inference :func:`parse_targets` already admits a target by, and their subject
    identities must match -- host included, since one slug on two servers is two
    pull requests. The observation's own ``target`` and ``kind`` labels are checked
    too when it carries them: those are what the reading says about itself, and a
    reading disagreeing with the record it came from is not a case to guess at.

    ``False`` on every doubtful case, which drops the target: the collector counts a
    drop, and a tick with no evidence answers FALLBACK, which fires.
    """
    kind = (monitor_kind or "").strip()
    watched = (monitor_target or "").strip()
    requested = (target or "").strip()
    if not kind or not watched or not requested:
        return False
    if isinstance(observation, Mapping):
        labelled = observation.get("target")
        if isinstance(labelled, str) and labelled.strip() and labelled.strip() != watched:
            return False
        observed_kind = observation.get("kind")
        if (
            isinstance(observed_kind, str)
            and observed_kind.strip()
            and observed_kind.strip() != kind
        ):
            return False
    if requested == watched:
        return True
    identity = _pr_identity(requested)
    return identity is not None and identity == _pr_identity(watched)


def pr_evidence(
    observation: Mapping[str, Any] | None,
    target: str,
    *,
    now_ts: float | None = None,
) -> list[dict[str, Any]]:
    """One watched pull request's probe reading, as a single evidence row.

    The probe's own observation is carried rather than re-derived: it is the typed
    half of this decision and it has already run this tick, so asking the forge a
    second question would cost a subprocess to learn what the caller already holds.
    :func:`render_pr_summary` turns its typed keys into the prose a judge reads.

    What the judge adds over the probe is not a second reading of these facts. It is
    the OWNER'S criterion -- up to 500 characters of their own prose -- evaluated
    against them, which is the one thing a typed probe has no way to do.

    Comment body TEXT is not part of this evidence. The reader that sees PR-level
    comments reduces each body to a fixed-width fingerprint and retains none of
    them, so the fingerprint's presence is reported and nothing more.
    """
    if not isinstance(observation, Mapping):
        return []
    clock = point.now() if now_ts is None else now_ts
    summary = render_pr_summary(observation)
    if not summary:
        return []
    return [
        {
            "source": f"pr:{target}",
            "kind": point.KIND_PR_CHECKS,
            "age_s": _age_from_ts(observation.get("observed_at"), clock),
            "text": summary,
        }
    ]


async def collect_evidence(
    targets: Sequence[str],
    *,
    read_session: (
        Callable[[str, int], Awaitable[tuple[Sequence[Mapping[str, Any]], int]]] | None
    ) = None,
    read_pr: Callable[[str], Awaitable[Mapping[str, Any] | None]] | None = None,
    cursors: dict[str, int] | None = None,
    now_ts: float | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Evidence for one tick, and how many targets were dropped. Never raises.

    *read_session* is given a target and its cursor and returns ``(rows,
    next_cursor)``; *read_pr* is given a target and returns the probe observation.
    Either may be ``None``, which simply means that collector is unavailable on
    this build or this loop -- not an error, because a judge watching only
    sessions needs no pull-request reader.

    A reader that RAISES counts as a dropped target rather than a failed tick. The
    refusal a creator-only check produces arrives exactly that way, which is what
    makes "a target the owner may not read is dropped and noted" true by
    construction instead of by a second check here.

    *cursors* is updated in place for the targets that were read, so the next tick
    sees only what arrived in between. It is only advanced on a SUCCESSFUL read: a
    target that refused or raised keeps its old cursor, so a transient failure
    cannot silently skip the rows it would have returned.
    """
    clock = point.now() if now_ts is None else now_ts
    evidence: list[dict[str, Any]] = []
    dropped = 0
    for target in list(targets)[:MAX_TARGETS]:
        try:
            if is_session_target(target):
                if read_session is None:
                    dropped += 1
                    continue
                rows, next_cursor = await read_session(target, int((cursors or {}).get(target, 0)))
                evidence.extend(session_evidence(rows, target, now_ts=clock))
                if cursors is not None and isinstance(next_cursor, int) and next_cursor >= 0:
                    cursors[target] = next_cursor
            else:
                if read_pr is None:
                    dropped += 1
                    continue
                observation = await read_pr(target)
                if observation is None:
                    # The reader answers ``None`` for a loop carrying no monitor --
                    # which is every loop armed through ``POST /api/autonudge`` -- for
                    # a brief naming a pull request this loop does not watch, and
                    # before the monitor's first observation. The subject was not read
                    # in any of those, so this is a DROPPED target: without the count
                    # the tick would hold no evidence and no drop, which is the one
                    # shape the point reads as "every target was calm".
                    dropped += 1
                    continue
                evidence.extend(pr_evidence(observation, target, now_ts=clock))
        except Exception as exc:
            # Includes the creator-only refusal. The class is not logged at
            # warning: a loop naming a session it may not read is an owner
            # mistake that would otherwise repeat every interval.
            dropped += 1
            if cursors is not None and getattr(exc, "code", "") == "cursor_unavailable":
                # The stored cursor does not address this session's transcript: it
                # was rewound, regenerated or trimmed under the loop. Counting that
                # as an ordinary drop keeps the same unusable cursor, so every later
                # tick re-sends it, is refused again, and the judge never reads this
                # target while the loop stays armed. Clearing the entry is what makes
                # the next tick a tail read, which is the recovery the reader
                # documents. Matched on the refusal's code rather than its class so
                # the collector stays independent of which reader was injected.
                cursors.pop(target, None)
                logger.debug(
                    "nudge.wake: clearing an unusable read cursor for one target",
                    exc_info=True,
                )
                continue
            logger.debug("nudge.wake: dropping a target this loop could not read", exc_info=True)
    return evidence, dropped


def spec_of(loop: Any) -> dict[str, Any]:
    """One loop's stored judge spec as a mapping, or ``{}``. Never raises.

    ``{}`` is "no judge on this loop", which is what a record written before the
    field existed decodes to and what an unreadable value resolves to -- the tick
    then behaves exactly as it does today.
    """
    try:
        raw = getattr(loop, "judge", None)
    except Exception:
        return {}
    return dict(raw) if isinstance(raw, Mapping) else {}


def criteria_of(spec: Mapping[str, Any] | None) -> tuple[str, str]:
    """The owner's ``(wake_when, quiet_when)``, each clipped, each possibly empty."""
    if not isinstance(spec, Mapping):
        return "", ""
    wake = spec.get("wake_when", "")
    quiet = spec.get("quiet_when", "")
    return (
        wake[: point.MAX_CRITERION_CHARS] if isinstance(wake, str) else "",
        quiet[: point.MAX_CRITERION_CHARS] if isinstance(quiet, str) else "",
    )


def verdict_record(verdict: Any, evidence_items: int) -> dict[str, Any]:
    """The previous-verdict summary carried into the NEXT tick's state.

    Deliberately small and text-free: the outcome, how much it was based on, and
    when. A judge seeing this knows it already passed on comparable evidence
    without being handed that evidence a second time.
    """
    try:
        outcome = verdict.outcome.value
    except Exception:
        outcome = "unknown"
    return {"outcome": outcome, "evidence_items": int(evidence_items), "at": time.time()}


def _pr_subject(value: str) -> bool:
    """Whether a pull-request target can be inferred from *value*. Never raises."""
    if not value or not value.strip():
        return False
    try:
        from kiro_crew.probes import targets as _targets

        return _targets.infer(value) is not None
    except Exception:
        logger.debug("nudge.wake: target inference unavailable", exc_info=True)
        return False


def _age_from_ts(raw: object, clock: float) -> float:
    """Seconds between *raw* and *clock*, or 0.0 when *raw* is not a usable time.

    Both shapes are parsed because the two callers genuinely differ: a probe
    observation carries an epoch float, while a transcript row carries an ISO 8601
    STRING (``'2026-09-22T07:25:47.670891+00:00'``). Reading only the float made every
    session row age 0.0, which quietly broke the drop-oldest-first bound -- with all
    ages equal, what got discarded when the cap was reached was arbitrary, so a newer
    actionable row could lose its place to an older one.

    A naive string -- no offset -- is read as UTC, matching how the rest of the
    gateway stores times. Anything unparseable is 0.0, which keeps the item rather
    than dropping it: an unreadable clock is a reason to let the judge see the
    evidence, not to hide it.
    """
    if isinstance(raw, bool):
        return 0.0
    stamp: float | None = None
    if isinstance(raw, (int, float)):
        stamp = float(raw)
    elif isinstance(raw, str) and raw.strip():
        text = raw.strip()
        # ``fromisoformat`` handles the trailing 'Z' only from 3.11, and the gateway
        # supports older readers of the same rows, so normalise it here.
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return 0.0
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        stamp = parsed.timestamp()
    if stamp is None or not math.isfinite(stamp):
        return 0.0
    age = clock - stamp
    return age if age > 0 else 0.0
