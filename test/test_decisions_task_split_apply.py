"""``task.split`` in the runner: who gets asked, and where the receipt lands.

Three halves.

WHO gets asked. The point is for a message the dashboard owner typed, so an
app-token send, a cron, a sub-agent, a gateway stage, a channel relay and a
nudge-loop wake must ask nothing at all -- checked against the real
``_run_chat``, not against the predicate alone, because the predicate is only
worth what its four arguments are wired to.

WHAT the agent did. The baseline arm is counted from the TRUSTED ``_meta.kiro``
identity of each tool call, so a scripted turn that calls two spawns reads as
``split`` while the same turn calling a shell tool named ``spawn_run`` reads as
``single``. That mutation is the whole claim of the comparison: nothing the model
writes may move the arm its own suggestion is scored against.

WHERE the receipt lands. On the turn's FINAL assistant row, through both doors a
client reads, and BESIDE a ``skills.select`` strip on the same reply rather than
displacing it -- the collision that kept this point off ``decisions.outcomes``.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from chat_test_helpers import _make_ready_kiro_prerequisite

from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    EVENT_TEXT_CHUNK,
    EVENT_TOOL_CALL,
    EVENT_TOOL_RESULT,
)
from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard.chat_persistence import _build_message_entry_uncached
from kiro_crew.dashboard.state import DashboardState, _ChatSlot
from kiro_crew.decisions.points import task_split as ts
from kiro_crew.history import ConversationLog
from kiro_crew.providers.base import LLMEvent

#: What the point returns for a decided turn -- patched in, so the wiring under
#: test is the call site rather than the oracle.
DECIDED = {"turn_id": "ts-7", "choice": ts.CHOICE_SPLIT, "p": 0.84, "latency_ms": 190}

#: The row the point writes for it, as the caller stamps it.
RECORD = {
    "ts": "2026-09-21T07:00:00+00:00",
    "point": "task.split",
    "session": "0123456789ab",
    "latency_ms": 190,
    "scrubbed": False,
    "answers": None,
    "error": None,
    "turn_id": "ts-7",
    "jev_choice": "split",
    "agent_choice": "split",
    "spawn_calls": 1,
    "spawn_helpers": 3,
    "spawn_tools": ["spawn_sub_agents"],
    "agree": True,
    "p": 0.84,
}


def _state(tmp_path) -> DashboardState:
    sessions = MagicMock(count=0)
    sessions.get_pid = MagicMock(return_value=None)
    sessions.get_slack_link = MagicMock(return_value=(None, None))
    sessions.get_mirror_link = MagicMock(return_value=None)
    sessions.get_provider = MagicMock(return_value=None)
    sessions.resumable_sid = MagicMock(return_value=None)
    sessions.check_context_usage = MagicMock()
    sessions.reset = AsyncMock()
    sessions.remove = AsyncMock()
    sessions.record_failure = AsyncMock()
    state = DashboardState(
        sessions=sessions,
        crons=MagicMock(list_jobs=MagicMock(return_value=[]), status=MagicMock(return_value={})),
        lessons=MagicMock(load_all=MagicMock(return_value=[])),
        start_time=0.0,
        conversation_log=ConversationLog(base_dir=tmp_path),
    )
    state.kiro_prerequisite_service = _make_ready_kiro_prerequisite()
    state.broadcast_ws = MagicMock()
    state.push_slots_update = MagicMock()
    state.push_refresh = MagicMock()
    state.refresh_slot_source_status = MagicMock()
    state.broadcast_context_usage = MagicMock()
    return state


def _runner(tmp_path):
    """``(state, client)`` wired for a scripted ``_run_chat`` turn."""
    state = _state(tmp_path)
    client = AsyncMock()
    client.context_usage_pct = MagicMock(return_value=0.0)
    client.context_window_tokens = MagicMock(return_value=0)
    client.context_used_tokens = MagicMock(return_value=0)
    client.last_prompt_stats = None
    client._client = client
    client.exit_code = None
    state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
    state.sessions.consume_replay_suppression = MagicMock(return_value=False)
    state.sessions.consume_needs_reinjection = MagicMock(return_value=False)
    state._hook_store = MagicMock(fire=AsyncMock(return_value=[]))
    return state, client


@pytest.fixture(autouse=True)
def clean_registry():
    """No published receipt survives into another test: the registry is process
    memory, so a row no reply claimed would be consumed by the next test's flush."""
    from kiro_crew.decisions import outcomes

    outcomes.reset()
    yield
    outcomes.reset()


def _slot(key: str = "chat-split-1") -> _ChatSlot:
    slot = _ChatSlot(key)
    slot._titled = True
    return slot


@contextmanager
def _quiet_sel():
    with patch.object(chat_runner, "sel") as mock_sel:
        mock_sel.return_value = MagicMock()
        yield mock_sel


async def _settle(slot) -> None:
    task = slot.task
    if task is None or not hasattr(task, "cancel"):
        return
    if not task.done():
        task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:  # pragma: no cover - draining, never the assertion
        pass


def _spawn_call(tool_call_id: str, *, server: str = "kirocrew-core", tool: str = "spawn_run"):
    """The CALL frame for one spawn. What it started is in its RESULT."""
    return LLMEvent(
        kind=EVENT_TOOL_CALL,
        title=tool,
        tool_name=tool,
        mcp_server_name=server,
        tool_call_id=tool_call_id,
        tool_kind="other",
        tool_input="{}",
    )


def _spawned_text(count: int) -> str:
    """A ``spawn_run`` result announcing *count* started helpers, in its own prose."""
    return f"Spawned {count} subagent(s). Results will arrive as completion events:\n" + "\n".join(
        f"  a{i} (kirocrew): part {i}" for i in range(count)
    )


def _batch_text(count: int) -> str:
    """A ``spawn_sub_agents`` result, in the json-row shape that tool returns.

    Deliberately NOT the prose form: that tool announces no count, which is the whole
    reason the arm reads structured rows first.
    """
    import json as _json

    return "\n\n".join(
        _json.dumps({"agent": f"c{i} (kirocrew)", "status": "completed", "text": "ok"})
        for i in range(count)
    )


def _spawn_result(
    tool_call_id: str, *, started: int = 1, batch: bool = False, text: str | None = None
):
    """The terminal RESULT frame for a spawn call.

    *started* renders what the tool actually returns: ``spawn_run``'s prose
    announcement, or -- with *batch* -- ``spawn_sub_agents``'s json rows, which carry no
    announcement at all. *text* overrides both, which is how a denial is scripted: the
    producers emit neither shape when nothing started.
    """
    if text is None:
        text = _batch_text(started) if batch else _spawned_text(started)
    return LLMEvent(
        kind=EVENT_TOOL_RESULT,
        tool_call_id=tool_call_id,
        tool_output=text,
        tool_final=True,
        tool_status="completed",
    )


def _scripts(client, events) -> None:
    """Script ``client.stream`` so one turn yields *events* then completes."""

    async def _once():
        for event in events:
            yield event
        yield LLMEvent(kind=EVENT_COMPLETE, stop_reason="end_turn")

    client.stream = MagicMock(side_effect=lambda *_a, **_k: _once())


@contextmanager
def _answering(decided: dict | None, *, record: dict | None = RECORD):
    """Patch the POINT's two halves and report what each was handed.

    ``recorded`` holds one entry per ``record_outcome`` call, so a test can assert
    both WHAT the runner reported as the agent's arm and that it reported it exactly
    once -- the row is a once-per-turn write with two possible callers.

    The stand-in PUBLISHES the row it "wrote", because that is what the real
    :func:`record_outcome` does with one: a fake that only returned it would test a
    delivery path the point does not have. A refused row publishes nothing.
    """
    asked: list[dict] = []
    recorded: list[dict] = []

    async def _suggest(text, **kwargs):
        asked.append({"text": text, **kwargs})
        return decided

    async def _record(**kwargs):
        from kiro_crew.decisions import outcomes

        recorded.append(kwargs)
        if record is not None:
            outcomes.publish(kwargs.get("session_key"), record)
        return record

    with patch.object(ts, "suggest", _suggest), patch.object(ts, "record_outcome", _record):
        yield asked, recorded


def _assistant_rows(slot: _ChatSlot) -> list[dict]:
    return [m for m in slot.messages if m.get("role") == "assistant"]


def _record_of(msg: dict) -> object:
    """This row's ``task.split`` receipt out of the strip LIST, or ``None``: several
    points can decide one turn, so this picks its own rather than assuming it alone."""
    strips = (msg.get("meta") or {}).get("decisions_strip") or []
    rows = [row for row in strips if isinstance(row, dict) and row.get("point") == ts.POINT]
    return rows[0] if rows else None


def _text(body: str) -> LLMEvent:
    return LLMEvent(kind=EVENT_TEXT_CHUNK, text=body)


# ── who gets asked ────────────────────────────────────────────────────────────


class TestOnlyAnOwnerTurnIsAsked:
    @pytest.mark.asyncio
    async def test_an_owner_send_is_asked_and_the_hint_is_prepended(self, tmp_path):
        state, client = _runner(tmp_path)
        slot = _slot()
        _scripts(client, [_text("on it")])

        with _quiet_sel(), _answering(DECIDED) as (asked, _recorded):
            await chat_runner._run_chat(
                state, slot, "audit these four modules", _directive_user_origin=True
            )
        await _settle(slot)

        assert len(asked) == 1, "one question per owner turn"
        assert asked[0]["text"] == "audit these four modules"
        prompt = client.stream.call_args[0][0]
        assert "Jev suggests:" in prompt, "one advisory line, prepended"
        assert "You decide." in prompt

    @pytest.mark.asyncio
    async def test_an_expansion_sends_the_typed_text_and_not_the_file(self, tmp_path):
        """An `@name` turn asks about the mention, never the prompt file's contents.

        The expansion REPLACES the message in place at this depth, so a question
        built from the assembled text would forward a local file to the provider on a
        turn whose person typed one word.
        """
        state, client = _runner(tmp_path)
        slot = _slot()
        _scripts(client, [_text("on it")])

        async def _expand(_message, _state, _slot):
            return "SECRET LOCAL PROMPT BODY: step 1, step 2", "ok"

        with (
            _quiet_sel(),
            _answering(DECIDED) as (asked, _recorded),
            patch.object(chat_runner, "_expand_prompt_mention_off_loop", _expand),
        ):
            await chat_runner._run_chat(
                state, slot, "@audit the four modules", _directive_user_origin=True
            )
        await _settle(slot)

        assert len(asked) == 1
        assert asked[0]["text"] == "@audit the four modules"
        assert "SECRET LOCAL PROMPT BODY" not in asked[0]["text"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "kwargs",
        [
            # An app token: the middleware stamps the actor and clears user origin.
            {"_directive_user_origin": False, "_turn_actor": "app"},
            {"_directive_user_origin": True, "_turn_actor": "cron"},
            {"_directive_user_origin": True, "_turn_actor": "subagent"},
            {"_directive_user_origin": True, "_turn_actor": "gateway"},
            # A nudge/monitor loop waking the slot.
            {"_directive_user_origin": True, "_directive_self_wake": True},
            # A Slack or Discord relay.
            {"_directive_user_origin": True, "_directive_channel_origin": True},
            # No dispatch claimed a person at all.
            {},
        ],
    )
    async def test_nothing_else_is_asked(self, tmp_path, kwargs):
        state, client = _runner(tmp_path)
        slot = _slot()
        _scripts(client, [_text("done")])

        with _quiet_sel(), _answering(DECIDED) as (asked, recorded):
            await chat_runner._run_chat(state, slot, "do the thing", **kwargs)
        await _settle(slot)

        assert asked == [], "asked on a turn nobody typed"
        assert recorded == []
        assert "Jev suggests:" not in client.stream.call_args[0][0]
        assert _record_of(_assistant_rows(slot)[-1]) is None

    @pytest.mark.asyncio
    async def test_a_recursive_prompt_expansion_is_never_asked_about(self, tmp_path):
        """An EGRESS guard, not a tidiness one.

        A `@prompt` or `$skill` mention re-enters `_run_chat` at depth 1 carrying the
        same `_directive_user_origin`, so without the depth check the nested turn looks
        like a fresh owner message -- and the excerpt sent to Jev would be the EXPANDED
        body, the local prompt or skill FILE, rather than anything the person typed.
        """
        state, client = _runner(tmp_path)
        slot = _slot()
        _scripts(client, [_text("done")])

        with _quiet_sel(), _answering(DECIDED) as (asked, recorded):
            await chat_runner._run_chat(
                state,
                slot,
                "<the expanded body of a local prompt file>",
                _directive_user_origin=True,
                _prompt_depth=1,
            )
        await _settle(slot)

        assert asked == [], "the expansion's own body was sent to the oracle"
        assert recorded == []
        assert "Jev suggests:" not in client.stream.call_args[0][0]
        assert _record_of(_assistant_rows(slot)[-1]) is None

    @pytest.mark.asyncio
    async def test_the_same_owner_turn_at_depth_zero_IS_asked(self, tmp_path):
        """The other half, so the guard is not vacuous: depth is what differs."""
        state, client = _runner(tmp_path)
        slot = _slot()
        _scripts(client, [_text("done")])

        with _quiet_sel(), _answering(DECIDED) as (asked, _recorded):
            await chat_runner._run_chat(
                state,
                slot,
                "<the expanded body of a local prompt file>",
                _directive_user_origin=True,
                _prompt_depth=0,
            )
        await _settle(slot)

        assert len(asked) == 1

    @pytest.mark.asyncio
    async def test_a_refusal_prepends_nothing_and_records_nothing(self, tmp_path):
        state, client = _runner(tmp_path)
        slot = _slot()
        _scripts(client, [_text("done")])

        with _quiet_sel(), _answering(None) as (asked, recorded):
            await chat_runner._run_chat(state, slot, "do the thing", _directive_user_origin=True)
        await _settle(slot)

        assert len(asked) == 1, "the point was called; it declined"
        assert recorded == [], "no suggestion, so no arm to compare and no row"
        assert "Jev suggests:" not in client.stream.call_args[0][0]
        assert _record_of(_assistant_rows(slot)[-1]) is None


class TestTheOwnerPredicateIsStructural:
    """The four arguments, held one at a time.

    Read directly as well as through the runner: the runner proves the wiring, and
    this proves the rule, so a future dispatch that stamps a new actor is refused
    by the same line rather than by a branch somebody remembered.
    """

    def test_an_owner_turn_is_all_four(self):
        assert chat_runner._is_owner_turn(
            user_origin=True, turn_actor="", self_wake=False, channel_origin=False
        )

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"user_origin": False},
            {"turn_actor": "app"},
            {"turn_actor": "cron"},
            {"self_wake": True},
            {"channel_origin": True},
        ],
    )
    def test_any_one_of_them_refuses(self, kwargs):
        base = {
            "user_origin": True,
            "turn_actor": "",
            "self_wake": False,
            "channel_origin": False,
        }
        assert chat_runner._is_owner_turn(**{**base, **kwargs}) is False


# ── what the agent did ────────────────────────────────────────────────────────


class TestTheBaselineArmIsCountedFromTheTurn:
    @pytest.mark.asyncio
    async def test_two_core_spawn_calls_are_named(self, tmp_path):
        state, client = _runner(tmp_path)
        slot = _slot()
        _scripts(
            client,
            [
                _spawn_call("tc-1"),
                _spawn_result("tc-1"),
                _spawn_call("tc-2"),
                _spawn_result("tc-2"),
                _text("both running"),
            ],
        )

        with _quiet_sel(), _answering(DECIDED) as (_asked, recorded):
            await chat_runner._run_chat(
                state, slot, "audit these four modules", _directive_user_origin=True
            )
        await _settle(slot)

        assert len(recorded) == 1
        assert recorded[0]["spawn_tools"] == [ts.TOOL_DELEGATE, ts.TOOL_DELEGATE]
        assert ts.agent_choice_for(recorded[0]["spawn_helpers"]) == ts.CHOICE_SPLIT

    @pytest.mark.asyncio
    async def test_a_batch_spawn_and_a_single_one_are_both_counted(self, tmp_path):
        state, client = _runner(tmp_path)
        slot = _slot()
        _scripts(
            client,
            [
                _spawn_call("tc-1", tool="spawn_sub_agents"),
                _spawn_result("tc-1", started=2, batch=True),
                _spawn_call("tc-2", tool="mcp__kirocrew-core__spawn_run"),
                _spawn_result("tc-2"),
                _text("ok"),
            ],
        )

        with _quiet_sel(), _answering(DECIDED) as (_asked, recorded):
            await chat_runner._run_chat(state, slot, "split this up", _directive_user_origin=True)
        await _settle(slot)

        assert recorded[0]["spawn_tools"] == [ts.TOOL_SPLIT, ts.TOOL_DELEGATE]
        assert ts.agent_choice_for(recorded[0]["spawn_helpers"]) == ts.CHOICE_SPLIT

    @pytest.mark.asyncio
    async def test_the_batch_size_is_the_reported_helper_count(self, tmp_path):
        """What the receipt prints is SUB-AGENTS, not calls.

        One `spawn_sub_agents` call carrying three entries started three helpers, and
        "1 spawned" over three working sub-agents would misreport the turn.
        """
        state, client = _runner(tmp_path)
        slot = _slot()
        _scripts(
            client,
            [
                _spawn_call("tc-1", tool=ts.TOOL_SPLIT),
                _spawn_result("tc-1", started=3, batch=True),
                _text("all three in"),
            ],
        )

        with _quiet_sel(), _answering(DECIDED) as (_asked, recorded):
            await chat_runner._run_chat(
                state, slot, "audit these four modules", _directive_user_origin=True
            )
        await _settle(slot)

        assert recorded[0]["spawn_tools"] == [ts.TOOL_SPLIT], "one call"
        assert recorded[0]["spawn_helpers"] == 3, "three sub-agents"

    @pytest.mark.asyncio
    async def test_one_spawn_run_carrying_two_tasks_is_a_split(self, tmp_path):
        """``spawn_run`` takes a ``tasks`` LIST, so one call can be a split too.

        The shape a tool NAME got wrong: `spawn_run` alone read as `delegate` however
        many helpers its `tasks` list started. Reading what RAN fixes it without the
        arm having to know which field each tool fans out on.
        """
        state, client = _runner(tmp_path)
        slot = _slot()
        _scripts(
            client,
            [
                _spawn_call("tc-1", tool=ts.TOOL_DELEGATE),
                _spawn_result("tc-1", started=2),
                _text("both parts done"),
            ],
        )

        with _quiet_sel(), _answering(DECIDED) as (_asked, recorded):
            await chat_runner._run_chat(
                state, slot, "do these two independent things", _directive_user_origin=True
            )
        await _settle(slot)

        assert recorded[0]["spawn_tools"] == [ts.TOOL_DELEGATE], "one spawn_run call"
        assert recorded[0]["spawn_helpers"] == 2, "which started two helpers"
        assert ts.agent_choice_for(recorded[0]["spawn_helpers"]) == ts.CHOICE_SPLIT

    @pytest.mark.asyncio
    async def test_one_spawn_run_carrying_one_task_is_still_a_delegation(self, tmp_path):
        """The other half of the same rule, so the fix is not a blanket `split`."""
        state, client = _runner(tmp_path)
        slot = _slot()
        _scripts(
            client,
            [
                _spawn_call("tc-1", tool=ts.TOOL_DELEGATE),
                _spawn_result("tc-1", started=1),
                _text("done"),
            ],
        )

        with _quiet_sel(), _answering(DECIDED) as (_asked, recorded):
            await chat_runner._run_chat(
                state, slot, "do this one thing", _directive_user_origin=True
            )
        await _settle(slot)

        assert recorded[0]["spawn_helpers"] == 1
        assert ts.agent_choice_for(recorded[0]["spawn_helpers"]) == ts.CHOICE_DELEGATE

    @pytest.mark.asyncio
    async def test_a_denied_spawn_counts_nothing_and_moves_no_shape(self, tmp_path):
        """A spawn the person refused started nothing, so the arm says so.

        The call frame of a denied spawn is indistinguishable from a successful one --
        which is why the arm is read at the RESULT. Its result carries no "Spawned N"
        announcement, so the call is dropped rather than credited, and the turn reads
        as `single` instead of as a `split` that never happened.
        """
        state, client = _runner(tmp_path)
        slot = _slot()
        _scripts(
            client,
            [
                _spawn_call("tc-1", tool=ts.TOOL_SPLIT),
                # The tool refused: no ids, so no "Spawned N" announcement.
                _spawn_result("tc-1", text="Error: solo spawn refused. Nothing was spawned."),
                _text("done"),
            ],
        )

        with _quiet_sel(), _answering(DECIDED) as (_asked, recorded):
            await chat_runner._run_chat(state, slot, "do the thing", _directive_user_origin=True)
        await _settle(slot)

        assert recorded[0]["spawn_helpers"] == 0, "nothing started"
        assert recorded[0]["spawn_tools"] == [], "the refused call is dropped, not credited"
        assert ts.agent_choice_for(recorded[0]["spawn_helpers"]) == ts.CHOICE_SINGLE

    @pytest.mark.asyncio
    async def test_one_batch_spawn_alone_reads_as_a_split(self, tmp_path):
        """The canonical parallel path is ONE call carrying an `agents` list.

        Read through the runner as well as in the point's own unit, because this is
        the case a call count got wrong: it recorded the clearest possible agreement
        with a `split` suggestion as a disagreement.
        """
        state, client = _runner(tmp_path)
        slot = _slot()
        _scripts(
            client,
            [
                _spawn_call("tc-1", tool=ts.TOOL_SPLIT),
                _spawn_result("tc-1", started=3, batch=True),
                _text("three audits in"),
            ],
        )

        with _quiet_sel(), _answering(DECIDED) as (_asked, recorded):
            await chat_runner._run_chat(
                state, slot, "audit these four modules", _directive_user_origin=True
            )
        await _settle(slot)

        assert recorded[0]["spawn_tools"] == [ts.TOOL_SPLIT]
        assert ts.agent_choice_for(recorded[0]["spawn_helpers"]) == ts.CHOICE_SPLIT

    @pytest.mark.asyncio
    async def test_no_spawn_call_reads_as_a_single_worker(self, tmp_path):
        state, client = _runner(tmp_path)
        slot = _slot()
        _scripts(client, [_text("here is the answer")])

        with _quiet_sel(), _answering(DECIDED) as (_asked, recorded):
            await chat_runner._run_chat(
                state, slot, "audit these four modules", _directive_user_origin=True
            )
        await _settle(slot)

        assert recorded[0]["spawn_tools"] == []
        assert ts.agent_choice_for(recorded[0]["spawn_helpers"]) == ts.CHOICE_SINGLE

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "server,tool",
        [
            # A shell tool the MODEL named `spawn_run`: no MCP server identity.
            ("", "spawn_run"),
            # A third-party MCP server exposing a same-named tool.
            ("other-server", "spawn_run"),
            # A core tool that is not a spawn.
            ("kirocrew-core", "send_message"),
        ],
    )
    async def test_nothing_the_model_can_write_moves_the_arm(self, tmp_path, server, tool):
        """The mutation the comparison rests on.

        Two calls that LOOK like spawns still read as ``single``, because the count
        is taken from the trusted ``_meta.kiro`` identity rather than from a title.
        """
        state, client = _runner(tmp_path)
        slot = _slot()
        _scripts(
            client,
            [
                _spawn_call("tc-1", server=server, tool=tool),
                _spawn_result("tc-1"),
                _spawn_call("tc-2", server=server, tool=tool),
                _spawn_result("tc-2"),
                _text("ok"),
            ],
        )

        with _quiet_sel(), _answering(DECIDED) as (_asked, recorded):
            await chat_runner._run_chat(state, slot, "do the thing", _directive_user_origin=True)
        await _settle(slot)

        assert recorded[0]["spawn_tools"] == []

    @pytest.mark.asyncio
    async def test_an_unasked_turn_records_nothing(self, tmp_path):
        """The count is only kept while a suggestion is outstanding."""
        state, client = _runner(tmp_path)
        slot = _slot()
        _scripts(client, [_spawn_call("tc-1"), _spawn_result("tc-1"), _text("ok")])

        with _quiet_sel(), _answering(None) as (_asked, recorded):
            await chat_runner._run_chat(state, slot, "do the thing", _directive_user_origin=True)
        await _settle(slot)

        assert recorded == []


# ── where the receipt lands ───────────────────────────────────────────────────


class TestTheRecordRidesTheFinalReply:
    @pytest.mark.asyncio
    async def test_it_lands_on_the_last_assistant_row(self, tmp_path):
        state, client = _runner(tmp_path)
        slot = _slot()
        # Text, then a spawn, then the final answer: the FIRST segment flushes
        # before the spawn, so the receipt is published only after it -- a claim on
        # that earlier row would compare the suggestion against a count that had not
        # finished.
        _scripts(
            client,
            [_text("planning"), _spawn_call("tc-1"), _spawn_result("tc-1"), _text("the answer")],
        )

        with _quiet_sel(), _answering(DECIDED):
            await chat_runner._run_chat(
                state, slot, "audit these modules", _directive_user_origin=True
            )
        await _settle(slot)

        rows = _assistant_rows(slot)
        assert len(rows) >= 2, f"expected a pre-tool segment and a final one, got {rows}"
        assert _record_of(rows[-1]) == RECORD
        assert all(_record_of(row) is None for row in rows[:-1]), "one receipt per turn"

    @pytest.mark.asyncio
    async def test_it_reaches_both_doors_from_one_write(self, tmp_path):
        """The live frame ``slot.append`` broadcasts, and the persisted line."""
        state, client = _runner(tmp_path)
        slot = _slot()
        _scripts(client, [_text("the answer")])

        with _quiet_sel(), _answering(DECIDED):
            await chat_runner._run_chat(state, slot, "do the thing", _directive_user_origin=True)
        await _settle(slot)

        row = _assistant_rows(slot)[-1]
        assert _record_of(row) == RECORD
        assert _record_of(_build_message_entry_uncached(row)) == RECORD

    @pytest.mark.asyncio
    async def test_a_row_the_writer_refused_stamps_nothing(self, tmp_path):
        state, client = _runner(tmp_path)
        slot = _slot()
        _scripts(client, [_text("the answer")])

        with _quiet_sel(), _answering(DECIDED, record=None):
            await chat_runner._run_chat(state, slot, "do the thing", _directive_user_origin=True)
        await _settle(slot)

        assert _record_of(_assistant_rows(slot)[-1]) is None

    @pytest.mark.asyncio
    async def test_it_sits_beside_a_skills_select_strip_rather_than_replacing_it(self, tmp_path):
        """Two points decide one turn, and the reply carries both.

        ``decisions.outcomes`` is keyed by POINT and ``consume`` returns the list, so
        publishing this receipt leaves a ``skills.select`` strip on the same turn
        exactly where it was.
        """
        from kiro_crew.decisions import outcomes

        strip = {"turn_id": "sk-1", "point": "skills.select", "baseline": [], "jev": ["a"]}
        state, client = _runner(tmp_path)
        slot = _slot()
        _scripts(client, [_text("the answer")])

        with _quiet_sel(), _answering(DECIDED):
            # Published from inside the turn, the way prompt assembly does it:
            # after `_discard_stale_decision` has run on the way in.
            original = chat_runner._task_split_suggestion

            async def _publishing(*args, **kwargs):
                outcomes.publish(chat_runner.effective_session_key(slot), strip)
                return await original(*args, **kwargs)

            with patch.object(chat_runner, "_task_split_suggestion", _publishing):
                await chat_runner._run_chat(
                    state, slot, "do the thing", _directive_user_origin=True
                )
        await _settle(slot)

        # A LIST of records, which is the shape the assistant row carries: the
        # claim here is that this receipt sat beside the strip rather than over it,
        # and in publish order.
        assert _assistant_rows(slot)[-1]["meta"]["decisions_strip"] == [strip, RECORD]


class TestTheOutcomeRowIsNeverLost:
    """A turn that produced no assistant text still records both arms.

    The row is the MEASUREMENT the point exists for, and a turn that only called
    tools is exactly the kind most likely to have spawned -- so losing it would make
    the log over-represent turns that ended in prose.
    """

    @pytest.mark.asyncio
    async def test_a_tool_only_turn_still_writes_the_row(self, tmp_path):
        state, client = _runner(tmp_path)
        slot = _slot()
        # A spawn and nothing else: no assistant text, so no row to stamp.
        _scripts(
            client,
            [_spawn_call("tc-1", tool=ts.TOOL_SPLIT), _spawn_result("tc-1", started=3, batch=True)],
        )

        with _quiet_sel(), _answering(DECIDED) as (_asked, recorded):
            await chat_runner._run_chat(
                state, slot, "audit these four modules", _directive_user_origin=True
            )
        await _settle(slot)

        assert len(recorded) == 1, "written by the turn's own finally"
        assert recorded[0]["spawn_tools"] == [ts.TOOL_SPLIT]
        assert all(
            _record_of(row) is None for row in _assistant_rows(slot)
        ), "nothing to stamp, so nothing is stamped"

    @pytest.mark.asyncio
    async def test_a_turn_with_a_reply_writes_it_exactly_once(self, tmp_path):
        """Two callers, one row: the reply's flush and the finally fallback."""
        state, client = _runner(tmp_path)
        slot = _slot()
        _scripts(client, [_spawn_call("tc-1"), _spawn_result("tc-1"), _text("the answer")])

        with _quiet_sel(), _answering(DECIDED) as (_asked, recorded):
            await chat_runner._run_chat(state, slot, "do the thing", _directive_user_origin=True)
        await _settle(slot)

        assert len(recorded) == 1, f"the row was written {len(recorded)} times"
        assert _record_of(_assistant_rows(slot)[-1]) == RECORD

    @pytest.mark.asyncio
    async def test_a_refused_row_is_not_retried_by_the_fallback(self, tmp_path):
        """A write the log declined is not a write that never happened.

        The flush marks the turn resolved whether or not a row came back, so the
        fallback cannot turn one refused append into two.
        """
        state, client = _runner(tmp_path)
        slot = _slot()
        _scripts(client, [_text("the answer")])

        with _quiet_sel(), _answering(DECIDED, record=None) as (_asked, recorded):
            await chat_runner._run_chat(state, slot, "do the thing", _directive_user_origin=True)
        await _settle(slot)

        assert len(recorded) == 1
        assert _record_of(_assistant_rows(slot)[-1]) is None

    @pytest.mark.asyncio
    async def test_a_stop_during_the_write_still_writes_it_once(self, tmp_path):
        """A Stop landing on the write leaves one row, not two.

        The append runs in a worker thread a cancellation cannot stop, so the row
        lands on disk AND the ``CancelledError`` reaches the turn's ``finally``. The
        flush claims the turn before it writes, which is what keeps the fallback off
        a row that is already written.
        """
        state, client = _runner(tmp_path)
        slot = _slot()
        _scripts(client, [_spawn_call("tc-1"), _spawn_result("tc-1"), _text("the answer")])
        recorded: list[dict] = []

        async def _suggest(text, **kwargs):
            return DECIDED

        async def _record(**kwargs):
            recorded.append(kwargs)
            raise asyncio.CancelledError

        with (
            _quiet_sel(),
            patch.object(ts, "suggest", _suggest),
            patch.object(ts, "record_outcome", _record),
        ):
            await chat_runner._run_chat(state, slot, "do the thing", _directive_user_origin=True)
        await _settle(slot)

        assert len(recorded) == 1, f"the row was written {len(recorded)} times"

    @pytest.mark.asyncio
    async def test_an_unasked_turn_writes_nothing_from_either_caller(self, tmp_path):
        state, client = _runner(tmp_path)
        slot = _slot()
        _scripts(client, [_spawn_call("tc-1"), _spawn_result("tc-1")])

        with _quiet_sel(), _answering(None) as (_asked, recorded):
            await chat_runner._run_chat(state, slot, "do the thing", _directive_user_origin=True)
        await _settle(slot)

        assert recorded == []


class TestAnOrdinaryRowIsUnchanged:
    @pytest.mark.asyncio
    async def test_an_undecided_turn_writes_no_meta_key(self, tmp_path):
        """``slot.append`` writes no ``meta`` for ``None``, which is what keeps a
        turn with no decision byte-identical to one this build appends today."""
        state, client = _runner(tmp_path)
        slot = _slot()
        _scripts(client, [_text("the answer")])

        with _quiet_sel(), _answering(None):
            await chat_runner._run_chat(state, slot, "do the thing", _directive_user_origin=True)
        await _settle(slot)

        row = _assistant_rows(slot)[-1]
        assert "decisions_strip" not in (row.get("meta") or {})
