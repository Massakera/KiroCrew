"""The ride-along: a published outcome reaches the assistant row, and only once.

The strip has to arrive through BOTH doors a client reads -- the live
``chat_message`` frame the segment flush broadcasts, and the transcript line the
history writer persists -- from ONE write. So these drive the real
``_flush_segment`` against a real slot and then run the real persistence entry
builder over the row it produced, rather than asserting on the helper alone.

There are two finalizers, and both are covered from production dispatch: the normal
end-of-segment flush, and the recovery finalizer a turn that dies mid-reply goes
through. A cancelled turn made its decision too, so a strip that only survives the
happy path is missing exactly where the user most wants to see what happened.

The absence case is the one that has to hold on every ordinary turn: with nothing
published, the row must be exactly what it is today. And the pop matters as much as
the attach: a strip left in the registry would ride the NEXT reply too and read as
a decision made on that turn.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from chat_test_helpers import _make_ready_kiro_prerequisite

from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_TEXT_CHUNK
from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard.chat_persistence import _build_message_entry_uncached
from kiro_crew.dashboard.chat_utils import effective_session_key
from kiro_crew.dashboard.state import DashboardState, _ChatSlot
from kiro_crew.decisions import outcomes
from kiro_crew.history import ConversationLog
from kiro_crew.providers.base import LLMEvent

STRIP = {
    "turn_id": "turn-7",
    "ts": "2026-09-19T07:00:00+00:00",
    "point": "skills.select",
    "baseline": ["brazil"],
    "jev": ["crux-code-reviews"],
    "agree": False,
    "p": 0.91,
    "tokens_saved": 3100,
    "candidates": 42,
    "batches": 1,
    "history_chars": 9000,
    "truncated": 0,
    "error": None,
}


@pytest.fixture(autouse=True)
def clean_registry():
    outcomes.reset()
    yield
    outcomes.reset()


def _slot(key: str = "chat-strip-1") -> _ChatSlot:
    slot = _ChatSlot(key)
    slot._titled = True
    return slot


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


def _assistant_rows(slot: _ChatSlot) -> list[dict]:
    return [m for m in slot.messages if m.get("role") == "assistant"]


def _assistant(slot: _ChatSlot) -> dict:
    rows = _assistant_rows(slot)
    assert len(rows) == 1, f"expected one assistant row, got {slot.messages}"
    return rows[0]


def _strip_of(msg: dict) -> object:
    return (msg.get("meta") or {}).get("decisions_strip")


# ── the real turn harness, for the recovery finalizer ──────────────────────


def _runner_state(tmp_path):
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


def _cancelled_after_text(client, text: str) -> None:
    """Script ``client.stream`` so the first turn streams *text* then is cancelled.

    That is the real route into the recovery finalizer: the turn produced visible
    text and then died, which is precisely the case whose partial reply has to be
    persisted -- strip included.
    """
    calls = {"n": 0}

    async def _first():
        yield LLMEvent(kind=EVENT_TEXT_CHUNK, text=text)
        raise asyncio.CancelledError()

    async def _later():
        yield LLMEvent(kind=EVENT_COMPLETE, stop_reason="end_turn")

    def _stream(*_args, **_kwargs):
        calls["n"] += 1
        return _first() if calls["n"] == 1 else _later()

    client.stream = MagicMock(side_effect=_stream)


class TestRideAlong:
    def test_a_published_outcome_lands_on_the_finalized_assistant_row(self, tmp_path):
        state, slot = _state(tmp_path), _slot()
        outcomes.publish(effective_session_key(slot), STRIP)

        chat_runner._flush_segment(state, slot, "here is the answer")

        assert _strip_of(_assistant(slot)) == STRIP

    def test_the_strip_survives_into_the_persisted_transcript_line(self, tmp_path):
        state, slot = _state(tmp_path), _slot()
        outcomes.publish(effective_session_key(slot), STRIP)

        chat_runner._flush_segment(state, slot, "here is the answer")
        entry = _build_message_entry_uncached(_assistant(slot))

        assert entry is not None
        assert entry["meta"]["decisions_strip"] == STRIP
        # Serializable as written: the transcript is JSONL, so a value the writer
        # cannot dump would cost the whole flush, not just this field.
        assert json.loads(json.dumps(entry))["meta"]["decisions_strip"] == STRIP

    def test_the_live_frame_carries_it_because_it_is_set_before_the_broadcast(self, tmp_path):
        """``slot.append`` broadcasts from inside the call, so a later write misses it."""
        state, slot = _state(tmp_path), _slot()
        broadcast: list[dict] = []
        slot._on_message = lambda _key, msg: broadcast.append(json.loads(json.dumps(msg)))
        outcomes.publish(effective_session_key(slot), STRIP)

        chat_runner._flush_segment(state, slot, "here is the answer")

        assistants = [m for m in broadcast if m.get("role") == "assistant"]
        assert len(assistants) == 1
        assert assistants[0]["meta"]["decisions_strip"] == STRIP

    def test_the_row_keeps_its_delivery_identity_alongside_the_strip(self, tmp_path):
        """``append`` merges its minted ``mid`` into meta; it must not replace it."""
        state, slot = _state(tmp_path), _slot()
        outcomes.publish(effective_session_key(slot), STRIP)

        chat_runner._flush_segment(state, slot, "here is the answer")

        meta = _assistant(slot)["meta"]
        assert meta["decisions_strip"] == STRIP
        assert meta.get("mid")

    @pytest.mark.asyncio
    async def test_a_turn_that_dies_mid_reply_still_carries_its_strip(self, tmp_path):
        """The recovery finalizer is the other funnel, driven here through a real turn."""
        state, client = _runner_state(tmp_path)
        slot = _slot()
        slot._empty_response_retries = 2
        _cancelled_after_text(client, "half an answer")
        outcomes.publish(effective_session_key(slot), STRIP)

        with _quiet_sel():
            await chat_runner._run_chat(state, slot, "hello")
        await _settle(slot)

        rows = _assistant_rows(slot)
        assert rows, f"expected a persisted partial reply, got {slot.messages}"
        assert "half an answer" in rows[0]["content"]
        assert _strip_of(rows[0]) == STRIP


class TestNothingPublished:
    def test_the_row_carries_no_strip_key_at_all(self, tmp_path):
        """Absent, not null: a consumer must not have to special-case an empty value."""
        state, slot = _state(tmp_path), _slot()

        chat_runner._flush_segment(state, slot, "here is the answer")

        assert "decisions_strip" not in (_assistant(slot).get("meta") or {})

    def test_the_persisted_line_is_unchanged_by_the_seam(self, tmp_path):
        state, slot = _state(tmp_path), _slot()

        chat_runner._flush_segment(state, slot, "here is the answer")
        entry = _build_message_entry_uncached(_assistant(slot))

        assert entry is not None
        assert "decisions_strip" not in (entry.get("meta") or {})

    def test_another_sessions_outcome_is_not_borrowed(self, tmp_path):
        state, slot = _state(tmp_path), _slot("chat-strip-1")
        outcomes.publish("chat-somebody-else", STRIP)

        chat_runner._flush_segment(state, slot, "here is the answer")

        assert "decisions_strip" not in (_assistant(slot).get("meta") or {})
        assert outcomes.pending_count() == 1


class TestConsumedOnce:
    def test_the_next_reply_in_the_same_session_carries_nothing(self, tmp_path):
        state, slot = _state(tmp_path), _slot()
        outcomes.publish(effective_session_key(slot), STRIP)

        chat_runner._flush_segment(state, slot, "first reply")
        chat_runner._flush_segment(state, slot, "second reply")

        rows = _assistant_rows(slot)
        assert len(rows) == 2
        assert _strip_of(rows[0]) == STRIP
        assert _strip_of(rows[1]) is None
