"""``task.split``: the three shapes, the hint, the baseline count, and the row.

The load-bearing groups are :class:`TestEveryRefusalPrependsNothing` -- the reason
this point is safe on the path that assembles every owner turn -- and
:class:`TestTheBaselineArmIsTheAgentsOwnBehaviour`, which pins that the arm the
suggestion is scored against comes from the TRUSTED tool identity, so nothing the
model writes can move it.

The runner wiring -- which turns get asked, and where the record lands -- is
``test_decisions_task_split_apply.py``.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from kiro_crew import credential_patterns as _cred
from kiro_crew.decisions import log as log_mod
from kiro_crew.decisions import points as _points
from kiro_crew.decisions.points import skills_select as ss
from kiro_crew.decisions.points import task_split as ts
from kiro_crew.decisions.types import Answer
from kiro_crew.session_directive import CORE_MCP_SERVER

#: An AWS key id assembled from the prefix list rather than written out, for the
#: reason ``test_decisions_gate`` gives: a contiguous key-shaped literal is refused
#: by the repo's own secret scanners, correctly, since neither they nor Semgrep can
#: tell a test vector from a leak.
_AWS_KEY = _cred.AWS_KEY_ID_PREFIXES.split("|")[0] + "A2B3C4D5E6F7G8H9"


@pytest.fixture
def answer(monkeypatch):
    """Install one ``decide`` answer and record what was asked. Returns a setter."""
    asked: list[dict] = []

    def _install(value, *, p=0.84):
        async def _decide(point, state, questions, **kwargs):
            asked.append({"point": point, "state": state, "questions": questions, "kwargs": kwargs})
            if value is None:
                return None
            return {ts.QUESTION_ID: Answer(id=ts.QUESTION_ID, value=value, p=p)}

        monkeypatch.setattr("kiro_crew.decisions.decide", _decide)
        return asked

    return _install


@pytest.fixture(autouse=True)
def no_history_budget(monkeypatch):
    """The shipped ceiling: 0, so a test that wants prior turns raises it itself."""
    monkeypatch.setattr("kiro_crew.decisions.history_budget_chars", lambda: 0)


@pytest.fixture
def written_rows(monkeypatch):
    """Capture every row the point hands the writer; report it as written."""
    rows: list[dict] = []

    def _append(row):
        rows.append(row)
        return True

    monkeypatch.setattr(log_mod, "append", _append)
    return rows


@pytest.fixture(autouse=True)
def clean_registry():
    """Every test starts with an empty outcome registry, the way every turn does.

    A published outcome stays claimable until a reply takes it, and in the product
    the next turn clears any leftover on the way in (``_discard_stale_decision``).
    Nothing plays that part in a test process, so without this a row one test
    published and no reply claimed is what the NEXT test's ``consume`` reads.
    """
    from kiro_crew.decisions import outcomes

    outcomes.reset()
    yield
    outcomes.reset()


def _SPAWNED(count: int) -> str:
    """A spawn result announcing *count* started helpers, as the tool renders it."""
    return f"Spawned {count} subagent(s). Results will arrive as completion events:\n" + "\n".join(
        f"  a{i} (kirocrew): part {i}" for i in range(count)
    )


def _BATCH(completed: int = 0, running: int = 0, failed: int = 0) -> str:
    """A ``spawn_sub_agents`` result, in the shape that tool really returns.

    ``json.dumps`` rows joined by a blank line, NOT prose: one per-agent row per
    helper that reached a terminal state, one aggregate row naming the ids still
    running, and a ``spawn_errors`` row for entries that never started.
    """
    rows = [
        json.dumps({"agent": f"c{i} (kirocrew)", "status": "completed", "text": "ok"})
        for i in range(completed)
    ]
    if running:
        ids = [f"r{i}" for i in range(running)]
        rows.append(
            json.dumps(
                {
                    "status": "still_running",
                    "task_ids": ids,
                    "states": {i: "running" for i in ids},
                    "waited_secs": 60,
                }
            )
        )
    if failed:
        rows.append(
            json.dumps(
                {
                    "status": "spawn_errors",
                    "errors": [f"entry {i}: spawn returned no agent id" for i in range(failed)],
                }
            )
        )
    return "\n\n".join(rows)


def ts_solo_refusal() -> str:
    """The tool's own refusal text, which starts nothing.

    Taken from the producer rather than written out, so the "nothing ran" case is the
    real string a denied spawn returns.
    """
    from kiro_crew.solo_spawn import solo_spawn_question

    return solo_spawn_question(tool="spawn_sub_agents")


def _turns(*pairs):
    """Prior transcript rows in the shape ``ConversationLog.recent`` returns."""
    return [{"role": role, "content": content} for role, content in pairs]


class TestTheAnswerIsTheSuggestion:
    @pytest.mark.asyncio
    async def test_split_comes_back_as_the_split_shape(self, answer):
        asked = answer(ts.CHOICE_SPLIT)
        decided = await ts.suggest("audit these four modules independently")
        assert decided is not None
        assert decided["choice"] == ts.CHOICE_SPLIT
        assert decided["p"] == pytest.approx(0.84)
        assert isinstance(decided["turn_id"], str) and decided["turn_id"]
        assert decided["latency_ms"] >= 0
        assert asked[0]["point"] == ts.POINT

    @pytest.mark.asyncio
    async def test_each_shape_round_trips(self, answer):
        for shape in ts.CHOICES:
            answer(shape)
            decided = await ts.suggest("do the thing")
            assert decided is not None and decided["choice"] == shape

    @pytest.mark.asyncio
    async def test_the_three_shapes_are_the_whole_declared_domain(self, answer):
        answer(ts.CHOICE_SINGLE)
        asked = answer(ts.CHOICE_SINGLE)
        await ts.suggest("rename one function")
        questions = asked[0]["questions"]
        assert len(questions) == 1, "one Choice: the answer is consumed as one line"
        assert questions[0].id == ts.QUESTION_ID
        assert questions[0].options == list(ts.CHOICES)
        for shape in ts.CHOICES:
            assert shape in questions[0].prompt, "each option is defined in the rubric"


class TestEveryRefusalPrependsNothing:
    @pytest.mark.asyncio
    async def test_no_answer_is_none_not_a_shape(self, answer):
        answer(None)
        assert await ts.suggest("do the thing") is None

    @pytest.mark.asyncio
    async def test_an_answer_outside_the_domain_is_none(self, answer):
        answer("fan-out")
        assert await ts.suggest("do the thing") is None

    @pytest.mark.asyncio
    async def test_a_raising_provider_is_none(self, monkeypatch):
        async def _decide(*args, **kwargs):
            raise RuntimeError("transport")

        monkeypatch.setattr("kiro_crew.decisions.decide", _decide)
        assert await ts.suggest("do the thing") is None

    @pytest.mark.asyncio
    async def test_a_call_past_the_callers_budget_is_none(self, monkeypatch):
        monkeypatch.setattr(ts, "wait_budget", lambda: 0.01)

        async def _decide(*args, **kwargs):
            await asyncio.sleep(5)

        monkeypatch.setattr("kiro_crew.decisions.decide", _decide)
        assert await ts.suggest("do the thing") is None

    @pytest.mark.asyncio
    async def test_cancellation_propagates(self, monkeypatch):
        async def _decide(*args, **kwargs):
            raise asyncio.CancelledError

        monkeypatch.setattr("kiro_crew.decisions.decide", _decide)
        with pytest.raises(asyncio.CancelledError):
            await ts.suggest("do the thing")

    @pytest.mark.asyncio
    async def test_a_refusal_writes_no_outcome_row(self, answer, written_rows):
        answer(None)
        assert await ts.suggest("do the thing") is None
        assert written_rows == [], "the gate writes its own row; the point writes none"


class TestTheHintIsAdviceAndNamesJev:
    def test_each_shape_has_its_own_sentence(self):
        seen = set()
        for shape in ts.CHOICES:
            line = ts.hint_line({"choice": shape, "p": 0.84})
            assert line.startswith("Jev suggests:"), "attributed, never an instruction"
            assert "You decide." in line, "the agent still chooses"
            assert "0.84" in line, "the score is readable so a weak hint can be discounted"
            seen.add(line)
        assert len(seen) == len(ts.CHOICES), "three distinct sentences"

    def test_the_split_sentence_names_parallel_sub_tasks(self):
        line = ts.hint_line({"choice": ts.CHOICE_SPLIT, "p": 0.84})
        assert "independent sub-tasks" in line and "parallel" in line

    def test_a_missing_probability_still_reads_as_a_sentence(self):
        line = ts.hint_line({"choice": ts.CHOICE_DELEGATE, "p": None})
        assert line.startswith("Jev suggests:") and line.endswith("You decide.")
        assert "(" not in line, "no empty parenthetical"

    def test_an_unreadable_choice_prepends_nothing(self):
        for decided in ({}, {"choice": ""}, {"choice": "fan-out"}, {"choice": None}):
            assert ts.hint_line(decided) == ""

    def test_the_hint_is_not_localised(self):
        """Prompt input for a model, not interface copy for a person.

        A translated hint would make the advice a function of the reader's UI
        language, so the text lives in this module rather than in a catalog.
        """
        assert set(ts._HINT_TEXT) == set(ts.CHOICES)
        for text in ts._HINT_TEXT.values():
            assert text.isascii()


class TestTheBaselineArmIsTheAgentsOwnBehaviour:
    @pytest.mark.parametrize(
        "helpers,expected",
        [
            (0, ts.CHOICE_SINGLE),
            (1, ts.CHOICE_DELEGATE),
            (2, ts.CHOICE_SPLIT),
            (7, ts.CHOICE_SPLIT),
        ],
    )
    def test_the_helper_count_maps_onto_a_shape(self, helpers, expected):
        assert ts.agent_choice_for(helpers) == expected

    def test_neither_a_call_count_nor_a_tool_name_could_get_this_right(self):
        """Why the shape is read from what RAN.

        BOTH spawn tools can start several helpers at once -- ``spawn_sub_agents``
        takes an ``agents`` batch and ``spawn_run`` takes a ``tasks`` list -- so one
        call can be a split by either route. A call count reads three parallel helpers
        as one delegation; a tool NAME reads ``spawn_run(tasks=[a, b])`` as one too.
        Only the helper count gets all of them right, and `agree` is the field this has
        to be trustworthy about.
        """
        # One batch call that started three: a split, not a delegation.
        assert ts.agent_choice_for(ts.helpers_started(ts.TOOL_SPLIT, _SPAWNED(3))) == (
            ts.CHOICE_SPLIT
        )
        # One `spawn_run` carrying two tasks: also a split, which the tool name alone
        # would have called a delegation.
        assert ts.agent_choice_for(ts.helpers_started(ts.TOOL_DELEGATE, _SPAWNED(2))) == (
            ts.CHOICE_SPLIT
        )
        # And one helper by either route is still a delegation.
        assert ts.agent_choice_for(ts.helpers_started(ts.TOOL_DELEGATE, _SPAWNED(1))) == (
            ts.CHOICE_DELEGATE
        )
        assert ts.agent_choice_for(ts.helpers_started(ts.TOOL_SPLIT, _SPAWNED(1))) == (
            ts.CHOICE_DELEGATE
        )

    @pytest.mark.parametrize("bad", [None, "2", b"2", 1.5, True, [2], {}])
    def test_an_unusable_value_reads_as_no_spawns(self, bad):
        """Never raises: this runs while a reply is being persisted.

        ``True`` is excluded deliberately -- a bool is not a count, for the reason
        ``gate._probability`` excludes one from being a probability.
        """
        assert ts.agent_choice_for(bad) == ts.CHOICE_SINGLE

    @pytest.mark.parametrize("tool", sorted(ts.SPAWN_TOOLS))
    def test_a_core_spawn_call_is_named(self, tool):
        assert ts.spawn_tool_named(CORE_MCP_SERVER, tool) == tool

    @pytest.mark.parametrize(
        "tool,expected",
        [
            ("kirocrew-core___spawn_run", ts.TOOL_DELEGATE),
            ("mcp__kirocrew-core__spawn_sub_agents", ts.TOOL_SPLIT),
        ],
    )
    def test_a_server_qualified_name_resolves(self, tool, expected):
        """Transports disagree on the separator; both shipped spellings resolve."""
        assert ts.spawn_tool_named(CORE_MCP_SERVER, tool) == expected

    @pytest.mark.parametrize(
        "server,tool",
        [
            # A shell call: no MCP server, a canonical name of its own.
            ("", "execute_bash"),
            # A third-party server exposing a same-named tool.
            ("other-server", "spawn_run"),
            ("", "spawn_run"),
            # A crafted tail that is not a >= 2 underscore separator.
            (CORE_MCP_SERVER, "do_spawn_run"),
            (CORE_MCP_SERVER, "a/b/spawn_run"),
            # A core tool that is not a spawn.
            (CORE_MCP_SERVER, "send_message"),
            (CORE_MCP_SERVER, ""),
        ],
    )
    def test_nothing_else_can_move_the_arm(self, server, tool):
        """The arm the suggestion is scored against.

        So it is read from the trusted ``_meta.kiro`` identity alone, and absent
        identity fails closed -- which here means not credited.
        """
        assert ts.spawn_tool_named(server, tool) == ""

    @pytest.mark.parametrize(
        "tool,result,expected",
        [
            # A BATCH of three: one call, three sub-agents.
            (ts.TOOL_SPLIT, _SPAWNED(3), 3),
            (ts.TOOL_SPLIT, _SPAWNED(1), 1),
            (ts.TOOL_DELEGATE, _SPAWNED(1), 1),
            # DENIED, or failed to start: the phrase is absent, so nothing ran.
            (ts.TOOL_SPLIT, ts_solo_refusal(), 0),
            (ts.TOOL_SPLIT, "Error: subagents not available", 0),
            (ts.TOOL_DELEGATE, "", 0),
            (ts.TOOL_DELEGATE, None, 0),
            # Not a spawn at all, whatever its text says.
            ("execute_bash", _SPAWNED(3), 0),
            ("", _SPAWNED(3), 0),
        ],
    )
    def test_the_count_is_what_the_call_actually_started(self, tool, result, expected):
        """The RESULT, not the call: only it says what ran.

        A call count reads a batch of three as one, and reads a spawn the person
        DENIED as one too -- its call frame is indistinguishable from a successful one.
        """
        assert ts.helpers_started(tool, result) == expected

    @pytest.mark.parametrize(
        "result,expected",
        [
            # Every helper finished: one per-agent row each.
            (_BATCH(completed=3), 3),
            (_BATCH(completed=1), 1),
            # Some still running: they are named together in the aggregate row, and a
            # helper is in exactly one of the two places, so the two ADD.
            (_BATCH(completed=1, running=2), 3),
            (_BATCH(running=4), 4),
            # Nothing started: a `spawn_errors` row parses, and names no helper.
            (_BATCH(failed=2), 0),
            # Started some, failed others: only the started ones count.
            (_BATCH(completed=2, failed=1), 2),
            # The tool's hard refusals are not rows at all.
            ("Error: 'agents' array is required", 0),
            ("Error spawning sub-agents:\n  - entry: no agent id", 0),
        ],
    )
    def test_the_batch_tool_is_counted_from_its_STRUCTURED_rows(self, result, expected):
        """``spawn_sub_agents`` returns json rows, not prose.

        Its result carries no "Spawned N" line at all, so a regex over it found nothing
        and read a whole batch as ZERO helpers -- the arm reported a split turn as
        `single`. Counted from the rows instead.
        """
        assert ts.helpers_started(ts.TOOL_SPLIT, result) == expected

    def test_the_two_tools_answer_in_different_shapes_and_both_are_read(self):
        """Neither tool depends on the other's format.

        ``spawn_run`` announces its count in prose and ``spawn_sub_agents`` in rows, so
        structure is tried first and the prose is the fallback. A reading that handled
        only one of them silently zeroed the other.
        """
        assert ts.helpers_started(ts.TOOL_SPLIT, _BATCH(completed=3)) == 3
        assert ts.helpers_started(ts.TOOL_DELEGATE, _SPAWNED(2)) == 2
        # And the fallback is only reached when nothing parsed as a row: a result that
        # IS rows but names no helper is a real 0, not a hand-off to the regex.
        assert ts.helpers_started(ts.TOOL_SPLIT, _BATCH(failed=1) + "\n\nSpawned 9 subagents") == 0

    def test_a_denied_spawn_moves_neither_the_count_nor_the_shape(self):
        """Nothing ran, so the arm says nothing ran.

        A refused batch call reads as `single`, not as the `split` it asked for.
        """
        started = ts.helpers_started(ts.TOOL_SPLIT, ts_solo_refusal())
        assert started == 0
        assert ts.agent_choice_for(started) == ts.CHOICE_SINGLE

    def test_a_huge_batch_is_bounded_but_still_reads_as_a_split(self):
        """The cap bounds a number that reaches a row and a line, not the tool."""
        capped = ts.helpers_started(ts.TOOL_SPLIT, _SPAWNED(ts.MAX_HELPERS_PER_CALL + 50))
        assert capped == ts.MAX_HELPERS_PER_CALL
        assert ts.agent_choice_for(capped) == ts.CHOICE_SPLIT

    def test_the_parsed_phrase_is_the_one_the_spawn_tool_emits(self):
        """Pins the contract this count is read through.

        The count comes out of prose, so the wording is a dependency. Held against the
        PRODUCER's own f-string rather than a copy of it, so a change in
        ``mcp_tools/spawn.py`` turns this red instead of silently zeroing every count.
        """
        source = (
            Path(__file__).resolve().parents[1] / "src/kiro_crew/mcp_tools/spawn.py"
        ).read_text(encoding="utf-8")
        assert 'f"Spawned {len(agent_ids)} subagent(s).' in source, (
            "mcp_tools/spawn.py no longer announces the count in the shape "
            "task_split._SPAWNED_RE parses"
        )
        # And the phrase is emitted ONLY with ids in hand, which is what makes its
        # absence mean "nothing started".
        assert "if agent_ids:" in source
        for count in (1, 3, 17):
            rendered = f"Spawned {count} subagent(s). Results will arrive as completion events:"
            assert ts.helpers_started(ts.TOOL_SPLIT, rendered) == count

    def test_the_identity_checks_are_the_forgery_gate_s_own(self):
        """Not a private copy of the server + canonical-name pair.

        ``session_directive.core_tool_named`` is where those two checks are written,
        and its docstring requires callers to use it rather than inline them, so this
        boundary cannot diverge from the directive gate's.
        """
        from kiro_crew import session_directive as sd

        assert sd.core_tool_named(sd.CORE_MCP_SERVER, "spawn_run", ts.SPAWN_TOOLS) == "spawn_run"
        # And the directive gate still answers through the same generalised pair.
        assert sd.directive_tool_for(sd.CORE_MCP_SERVER, "monitor_start") == "monitor_start"
        assert sd.directive_tool_for("other-server", "monitor_start") == ""


class TestTheRequestIsBoundedAndTheHistoryIsCeilinged:
    @pytest.mark.asyncio
    async def test_the_message_is_clipped_to_the_shared_bound(self, answer):
        asked = answer(ts.CHOICE_SINGLE)
        await ts.suggest("x" * (ts.MAX_MESSAGE_CHARS + 500))
        assert len(asked[0]["state"]["message"]) == ts.MAX_MESSAGE_CHARS
        assert ts.message_chars("x" * 5000) == ts.MAX_MESSAGE_CHARS

    @pytest.mark.asyncio
    async def test_the_shipped_ceiling_sends_the_request_alone(self, answer):
        asked = answer(ts.CHOICE_SINGLE, p=0.5)
        await ts.suggest("do the thing", history=_turns(("user", "earlier"), ("assistant", "ok")))
        assert "history" not in asked[0]["state"], "omitted, never an empty list"
        assert asked[0]["kwargs"]["extra"]["history_chars"] == 0

    @pytest.mark.asyncio
    async def test_a_raised_ceiling_buys_prior_turns(self, answer, monkeypatch):
        monkeypatch.setattr("kiro_crew.decisions.history_budget_chars", lambda: 500)
        asked = answer(ts.CHOICE_SPLIT)
        await ts.suggest("do the thing", history=_turns(("user", "earlier"), ("assistant", "ok")))
        state = asked[0]["state"]
        assert [row["text"] for row in state["history"]] == ["ok", "earlier"], "newest first"
        assert asked[0]["kwargs"]["extra"]["history_chars"] == len("ok") + len("earlier")

    @pytest.mark.asyncio
    async def test_the_ceiling_is_read_before_the_history_is_used(self, answer):
        """At 0 the caller's rows contribute nothing, whatever it passed."""
        asked = answer(ts.CHOICE_SINGLE)
        await ts.suggest("do the thing", history=_turns(("user", "y" * 4000)))
        assert "history" not in asked[0]["state"]

    def test_the_history_walk_is_the_one_the_package_owns(self):
        """One spender of the consented ceiling, not two nearly-equal walks."""
        assert ts.build_history is _points.build_history
        assert ts.history_budget is _points.history_budget
        assert ts.MAX_MESSAGE_CHARS == ss.MAX_MESSAGE_CHARS

    @pytest.mark.asyncio
    async def test_a_secret_in_a_prior_turn_refuses_the_whole_request(self, monkeypatch):
        """The state is what the gate renders, so history is inside the scrub."""
        monkeypatch.setattr("kiro_crew.decisions.history_budget_chars", lambda: 500)
        from kiro_crew.decisions import gate

        state = ts.build_state(
            "do the thing", _turns(("user", f"key {_AWS_KEY}")), history_budget_chars=500
        )
        assert gate.scrub_reason(state, ts.questions()) == gate.ERROR_SCRUBBED_CREDENTIAL


class TestTheWaitIsTheShapeEveryPointUses:
    def test_the_three_values_are_held_equal_across_the_package(self):
        """No point invents its own ceiling on the turn's critical path."""
        assert ts.WAIT_MARGIN_SECS == ss.WAIT_MARGIN_SECS
        assert ts.MIN_WAIT_SECS == ss.MIN_WAIT_SECS
        assert ts.MAX_WAIT_SECS == ss.MAX_WAIT_SECS

    def test_a_hand_edited_timeout_cannot_hold_the_reply(self, monkeypatch):
        monkeypatch.setattr("kiro_crew.decisions.timeout_secs", lambda: 3600.0)
        assert ts.wait_budget() == ts.MAX_WAIT_SECS

    def test_an_unreadable_budget_is_the_floor(self, monkeypatch):
        def _boom():
            raise RuntimeError("no config")

        monkeypatch.setattr("kiro_crew.decisions.timeout_secs", _boom)
        assert ts.wait_budget() == ts.MIN_WAIT_SECS

    def test_a_non_finite_budget_is_the_floor(self, monkeypatch):
        monkeypatch.setattr("kiro_crew.decisions.timeout_secs", lambda: float("inf"))
        assert ts.wait_budget() == ts.MIN_WAIT_SECS

    def test_an_unreadable_ceiling_sends_no_prior_turns(self, monkeypatch):
        def _boom():
            raise RuntimeError("no keystone")

        monkeypatch.setattr("kiro_crew.decisions.history_budget_chars", _boom)
        assert ts.history_budget() == 0


class TestTheRowHoldsBothArms:
    @pytest.mark.asyncio
    async def test_the_outcome_row_names_both_choices_and_the_route(self, written_rows):
        decided = {"turn_id": "ts-1", "choice": ts.CHOICE_SPLIT, "p": 0.84, "latency_ms": 190}
        row = await ts.record_outcome(
            session_key="s", decided=decided, spawn_tools=[ts.TOOL_SPLIT], spawn_helpers=3
        )
        assert row is not None
        assert row["point"] == ts.POINT
        assert row["jev_choice"] == ts.CHOICE_SPLIT
        assert row["agent_choice"] == ts.CHOICE_SPLIT
        assert row["spawn_calls"] == 1, "one call"
        assert row["spawn_helpers"] == 3, "three sub-agents -- what the line prints"
        assert row["spawn_tools"] == [ts.TOOL_SPLIT], "WHICH route, not only how many"
        assert row["agree"] is True
        assert row["p"] == pytest.approx(0.84)
        assert row["latency_ms"] == 190, "a core field, carried at top level"
        assert written_rows == [row], "the row returned IS the row written"

    @pytest.mark.asyncio
    async def test_a_suggestion_the_agent_ignored_reads_as_a_difference(self, written_rows):
        decided = {"turn_id": "ts-2", "choice": ts.CHOICE_SPLIT, "p": 0.6, "latency_ms": 12}
        row = await ts.record_outcome(session_key="s", decided=decided, spawn_tools=[])
        assert row["agent_choice"] == ts.CHOICE_SINGLE
        assert row["spawn_calls"] == 0
        assert row["spawn_helpers"] == 0
        assert row["spawn_tools"] == []
        assert row["agree"] is False

    def test_agree_is_derived_from_the_two_choices(self):
        """Never asserted by a caller: a flag that disagreed with the words beside
        it would hide a real divergence behind one of them."""
        built = ts.build_outcome(
            {"turn_id": "t", "choice": ts.CHOICE_DELEGATE}, [ts.TOOL_DELEGATE], 1
        )
        assert built["agree"] is True
        built = ts.build_outcome(
            {"turn_id": "t", "choice": ts.CHOICE_DELEGATE}, [ts.TOOL_DELEGATE], 4
        )
        assert built["agree"] is False, "four helpers is a split, whichever tool started them"

    def test_a_name_outside_the_two_tools_is_dropped(self):
        """A value that is not one of the two tools cannot describe a shape."""
        built = ts.build_outcome({"choice": ts.CHOICE_SINGLE}, [ts.TOOL_DELEGATE, "execute_bash"])
        assert built["spawn_tools"] == [ts.TOOL_DELEGATE]
        assert built["spawn_calls"] == 1

    @pytest.mark.parametrize("bad", [None, True, "3", 1.5, -4])
    def test_an_unmeasured_helper_count_falls_back_to_one_per_started_call(self, bad):
        """Conservative: never larger than the truth.

        ``True`` is excluded deliberately -- a bool is not a count, for the reason
        ``gate._probability`` excludes one from being a probability.
        """
        built = ts.build_outcome(
            {"choice": ts.CHOICE_SPLIT}, [ts.TOOL_DELEGATE, ts.TOOL_DELEGATE], bad
        )
        assert built["spawn_helpers"] == 2

    def test_latency_is_not_an_extra(self):
        """It is a core row field, so an ``extra`` naming it would be dropped."""
        assert "latency_ms" not in ts.build_outcome({"choice": ts.CHOICE_SINGLE}, [])

    @pytest.mark.asyncio
    async def test_a_row_the_writer_refused_stamps_nothing(self, monkeypatch):
        from kiro_crew.decisions import outcomes

        monkeypatch.setattr(log_mod, "append", lambda row: False)
        decided = {"turn_id": "ts-3", "choice": ts.CHOICE_SINGLE, "p": 0.5, "latency_ms": 1}
        assert await ts.record_outcome(session_key="s", decided=decided, spawn_tools=[]) is None
        assert outcomes.consume("s") == [], "a refused row names a turn nobody can rate"

    @pytest.mark.asyncio
    async def test_a_raising_writer_stamps_nothing(self, monkeypatch):
        def _boom(row):
            raise OSError("sealed")

        monkeypatch.setattr(log_mod, "append", _boom)
        decided = {"turn_id": "ts-4", "choice": ts.CHOICE_SINGLE, "p": 0.5, "latency_ms": 1}
        assert await ts.record_outcome(session_key="s", decided=decided, spawn_tools=[]) is None

    @pytest.mark.asyncio
    async def test_a_write_past_its_budget_stamps_nothing(self, monkeypatch):
        monkeypatch.setattr(ts, "LOG_BUDGET_SECS", 0.01)

        def _slow(row):
            import time as _t

            _t.sleep(0.5)
            return True

        monkeypatch.setattr(log_mod, "append", _slow)
        decided = {"turn_id": "ts-5", "choice": ts.CHOICE_SINGLE, "p": 0.5, "latency_ms": 1}
        assert await ts.record_outcome(session_key="s", decided=decided, spawn_tools=[]) is None


class TestTheReceiptTravelsTheRegistry:
    """``decisions.outcomes`` is keyed by POINT, so this receipt displaces nothing."""

    @pytest.mark.asyncio
    async def test_a_written_row_is_published_exactly_as_written(self, written_rows):
        from kiro_crew.decisions import outcomes

        decided = {"turn_id": "ts-8", "choice": ts.CHOICE_SPLIT, "p": 0.84, "latency_ms": 190}
        row = await ts.record_outcome(
            session_key="s", decided=decided, spawn_tools=[ts.TOOL_SPLIT], spawn_helpers=3
        )

        assert outcomes.consume("s") == [row], "the row published is the row written"

    @pytest.mark.asyncio
    async def test_it_leaves_another_point_s_outcome_where_it_is(self, written_rows):
        """The claim the parallel channel was built for, held against the registry."""
        from kiro_crew.decisions import outcomes

        strip = {"turn_id": "sk-1", "point": "skills.select", "baseline": [], "jev": ["a"]}
        outcomes.publish("s", strip)
        decided = {"turn_id": "ts-10", "choice": ts.CHOICE_SINGLE, "p": 0.5, "latency_ms": 1}
        row = await ts.record_outcome(session_key="s", decided=decided, spawn_tools=[])

        assert outcomes.consume("s") == [strip, row], "in publish order, neither displaced"


class TestThePointIsRegistered:
    def test_the_gate_knows_the_name(self):
        from kiro_crew.decisions import DECISION_POINT_NAMES

        assert ts.POINT in DECISION_POINT_NAMES

    def test_it_needs_no_tool_argument_scope(self):
        """It sends a message excerpt and prior turns -- the category the main
        consent already names -- so it is not a second consent."""
        from kiro_crew.decisions.gate import POINTS_NEEDING_TOOL_ARGS

        assert ts.POINT not in POINTS_NEEDING_TOOL_ARGS
