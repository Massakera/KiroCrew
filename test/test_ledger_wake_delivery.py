"""The wake hook's decision and its one-turn injection.

RECONSTRUCTED REFERENCE IMPLEMENTATION -- see this branch's commit message.

Two things are proved here that the pure-rule tests cannot: that an actionable
record resolves to the RIGHT conductor (and to none at all when the child has no
creator), and that several wakes arrive as ONE turn rather than one turn each --
which is the whole cost argument for the design.

The gateway is a double. What is real is the decision path and the control files
it reads, so a regression in either shows up here rather than only in a live
gateway.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from kiro_crew import ledger_wake as lw
from kiro_crew import session_ledger as sl
from kiro_crew.dashboard import ledger_wake_delivery as lwd

CONDUCTOR = "chat-conductor"
CHILD = "chat-worker"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Own data home per test, so no delivered map outlives its own test."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    sl._fold_cache.clear()
    # The snapshot render reaches the ledger store; the decision under test does
    # not depend on its text, and pinning it keeps these tests off that path.
    monkeypatch.setattr(lwd, "_snapshot", lambda key: f"SNAPSHOT({key})")
    monkeypatch.setattr(lwd, "wake_enabled", lambda: True)


class _Slot:
    """A slot double that records what would have been run as a turn."""

    def __init__(self, created_by: str = "") -> None:
        self._created_by = created_by
        self.prompts: list[str] = []
        self.started = True

    def enqueue_or_run_prompt(self, prompt: str, run_chat_coro: Any, state: Any) -> bool:
        self.prompts.append(prompt)
        return self.started


def _state(slots: dict[str, Any]) -> Any:
    state = MagicMock()
    state.get_slot = MagicMock(side_effect=lambda key: slots.get(key))
    return state


def _record(phase: str = "blocked", events: int = 1) -> dict[str, Any]:
    return {"phase": phase, "events": [{"kind": "phase", "text": f"e{i}"} for i in range(events)]}


class TestPlan:
    """Which records resolve to a wake, and to whose conductor."""

    def test_a_quiet_record_owes_nothing(self):
        state = _state({CHILD: _Slot(CONDUCTOR)})
        assert (
            lwd.plan_record_wake(
                state,
                child_key=CHILD,
                previous_phase="implementing",
                state_record=_record("awaiting-ci"),
                event_kind="progress",
            )
            is None
        )

    def test_an_actionable_record_names_the_creator_and_the_child(self):
        state = _state({CHILD: _Slot(CONDUCTOR)})
        planned = lwd.plan_record_wake(
            state,
            child_key=CHILD,
            previous_phase="implementing",
            state_record=_record("blocked"),
            event_kind="phase",
        )
        assert planned is not None
        conductor, envelope = planned
        assert conductor == CONDUCTOR
        assert f"child={CHILD}" in envelope
        assert "phase=blocked" in envelope
        assert f"SNAPSHOT({CHILD})" in envelope

    def test_a_child_with_no_creator_wakes_nobody(self):
        """A person's own tab is unattributed, and must not be treated as a child."""
        state = _state({CHILD: _Slot("")})
        assert (
            lwd.plan_record_wake(
                state,
                child_key=CHILD,
                previous_phase="implementing",
                state_record=_record("done"),
                event_kind="phase",
            )
            is None
        )

    def test_a_slot_naming_itself_wakes_nobody(self):
        """Guards a self-referential creator stamp from waking its own session."""
        state = _state({CHILD: _Slot(CHILD)})
        assert (
            lwd.plan_record_wake(
                state,
                child_key=CHILD,
                previous_phase="implementing",
                state_record=_record("done"),
                event_kind="phase",
            )
            is None
        )

    def test_a_ruling_event_wakes_without_a_phase_move(self):
        state = _state({CHILD: _Slot(CONDUCTOR)})
        planned = lwd.plan_record_wake(
            state,
            child_key=CHILD,
            previous_phase="implementing",
            state_record=_record("implementing"),
            event_kind="ruling",
        )
        assert planned is not None

    def test_the_same_record_is_planned_only_once(self):
        """Idempotence at the seam, not just in the map: replay must plan nothing."""
        state = _state({CHILD: _Slot(CONDUCTOR)})
        first = lwd.plan_record_wake(
            state,
            child_key=CHILD,
            previous_phase="implementing",
            state_record=_record("blocked", events=3),
            event_kind="phase",
        )
        assert first is not None
        again = lwd.plan_record_wake(
            state,
            child_key=CHILD,
            previous_phase="implementing",
            state_record=_record("blocked", events=3),
            event_kind="phase",
        )
        assert again is None

    def test_a_record_with_no_events_owes_nothing(self):
        state = _state({CHILD: _Slot(CONDUCTOR)})
        assert (
            lwd.plan_record_wake(
                state,
                child_key=CHILD,
                previous_phase="implementing",
                state_record={"phase": "blocked", "events": []},
                event_kind="phase",
            )
            is None
        )

    def test_the_kill_switch_suppresses_the_wake_and_still_advances_the_map(self, monkeypatch):
        """Off must not accumulate: turning it back on releases no stale burst."""
        monkeypatch.setattr(lwd, "wake_enabled", lambda: False)
        state = _state({CHILD: _Slot(CONDUCTOR)})
        assert (
            lwd.plan_record_wake(
                state,
                child_key=CHILD,
                previous_phase="implementing",
                state_record=_record("blocked", events=4),
                event_kind="phase",
            )
            is None
        )
        assert lw.already_delivered(CONDUCTOR, CHILD, 3)

    def test_a_child_over_its_hourly_budget_is_folded_forward(self):
        state = _state({CHILD: _Slot(CONDUCTOR)})
        for _ in range(lw.MAX_WAKES_PER_CHILD_PER_HOUR):
            lw.note_wake(CONDUCTOR, CHILD)
        assert (
            lwd.plan_record_wake(
                state,
                child_key=CHILD,
                previous_phase="implementing",
                state_record=_record("blocked", events=9),
                event_kind="phase",
            )
            is None
        )


class TestDeliver:
    """One turn, whatever the number of children reporting into it."""

    def test_several_wakes_arrive_as_one_turn(self):
        """The cost argument: N reporting children must not cost N turns."""
        slot = _Slot()
        state = _state({CONDUCTOR: slot})
        assert lwd.deliver(state, CONDUCTOR, ["ENV-A", "ENV-B", "ENV-C"])
        assert len(slot.prompts) == 1
        for marker in ("ENV-A", "ENV-B", "ENV-C"):
            assert marker in slot.prompts[0]

    def test_the_turn_marks_the_envelopes_as_reference_data(self):
        """A worker writes those lines, so the conductor must not read them as orders."""
        slot = _Slot()
        assert lwd.deliver(_state({CONDUCTOR: slot}), CONDUCTOR, ["ENV"])
        assert "REFERENCE DATA" in slot.prompts[0]

    def test_an_unreachable_conductor_delivers_nothing(self):
        assert not lwd.deliver(_state({}), CONDUCTOR, ["ENV"])

    def test_no_envelopes_starts_no_turn(self):
        slot = _Slot()
        assert not lwd.deliver(_state({CONDUCTOR: slot}), CONDUCTOR, [])
        assert slot.prompts == []

    def test_the_coalesced_count_is_bounded(self):
        slot = _Slot()
        many = [f"ENV-{i}" for i in range(lwd._MAX_COALESCED + 5)]
        assert lwd.deliver(_state({CONDUCTOR: slot}), CONDUCTOR, many)
        assert len(slot.prompts) == 1
        assert f"ENV-{lwd._MAX_COALESCED + 4}" not in slot.prompts[0]

    def test_a_dashboard_prefixed_conductor_slot_is_found(self):
        slot = _Slot()
        state = _state({f"dashboard_{CONDUCTOR}": slot})
        assert lwd.deliver(state, CONDUCTOR, ["ENV"])

    def test_a_raising_slot_does_not_propagate(self):
        """A delivery failure must never reach the worker's write path."""
        slot = _Slot()
        slot.enqueue_or_run_prompt = MagicMock(side_effect=RuntimeError("boom"))
        assert not lwd.deliver(_state({CONDUCTOR: slot}), CONDUCTOR, ["ENV"])


class TestHook:
    """The async hook: it must never raise into the route that calls it."""

    @pytest.mark.asyncio
    async def test_it_delivers_an_actionable_record(self):
        slot = _Slot()
        state = _state({CHILD: _Slot(CONDUCTOR), CONDUCTOR: slot})
        await lwd.on_record(
            state,
            child_key=CHILD,
            previous_phase="implementing",
            state_record=_record("blocked"),
            event_kind="phase",
        )
        assert len(slot.prompts) == 1
        assert f"child={CHILD}" in slot.prompts[0]

    @pytest.mark.asyncio
    async def test_it_stays_silent_on_a_quiet_record(self):
        slot = _Slot()
        state = _state({CHILD: _Slot(CONDUCTOR), CONDUCTOR: slot})
        await lwd.on_record(
            state,
            child_key=CHILD,
            previous_phase="implementing",
            state_record=_record("awaiting-ci"),
            event_kind="progress",
        )
        assert slot.prompts == []

    @pytest.mark.asyncio
    async def test_a_broken_gateway_does_not_raise_into_the_write_path(self):
        """The worker's record already landed; the hook owes it no exception."""
        state = MagicMock()
        state.get_slot = MagicMock(side_effect=RuntimeError("registry down"))
        await lwd.on_record(
            state,
            child_key=CHILD,
            previous_phase="implementing",
            state_record=_record("blocked"),
            event_kind="phase",
        )

    @pytest.mark.asyncio
    async def test_a_delivered_wake_spends_the_budget(self):
        state = _state({CHILD: _Slot(CONDUCTOR), CONDUCTOR: _Slot()})
        await lwd.on_record(
            state,
            child_key=CHILD,
            previous_phase="implementing",
            state_record=_record("blocked"),
            event_kind="phase",
        )
        rate = lw._read_map(CONDUCTOR, lw._RATE_FILE).get(CHILD)
        assert isinstance(rate, dict) and rate["count"] == 1


class TestDeadlineHelpers:
    """What the scheduler asks before it arms anything."""

    def test_only_armed_deadlines_are_reported(self):
        artifacts = {
            lw.deadline_artifact_key("a"): "500",
            lw.deadline_artifact_key("b"): "0",
            lw.deadline_artifact_key("c"): "not-a-number",
            "branch": "feat/x",
        }
        assert lw.deadlines_from_artifacts(artifacts) == {"a": 500.0}

    def test_the_earliest_deadline_is_what_the_scheduler_needs(self):
        assert lw.earliest_deadline({"a": 900.0, "b": 300.0, "c": 600.0}) == 300.0

    def test_nothing_armed_is_a_distinct_answer(self):
        """None, never a sentinel: a sentinel would pull the next fire time in."""
        assert lw.earliest_deadline({}) is None

    def test_a_silent_child_is_due_and_a_writing_one_is_not(self):
        deadlines = {"quiet": 500.0, "busy": 500.0}
        last = {"quiet": 100.0, "busy": 600.0}
        assert lw.due_children(deadlines, last, now=700.0) == ["quiet"]

    def test_a_child_that_never_wrote_is_due(self):
        assert lw.due_children({"new": 500.0}, {}, now=700.0) == ["new"]

    def test_due_children_are_ordered_longest_overdue_first(self):
        deadlines = {"late": 100.0, "later": 400.0, "latest": 200.0}
        assert lw.due_children(deadlines, {}, now=900.0) == ["late", "latest", "later"]

    def test_clearing_is_a_patch_not_a_deletion(self):
        """Artifacts are merge-only, so a disarm writes zero rather than removing."""
        assert lw.cleared_deadline_artifacts("a") == {lw.deadline_artifact_key("a"): "0"}
        assert lw.deadlines_from_artifacts(lw.cleared_deadline_artifacts("a")) == {}

    def test_a_stall_envelope_is_distinguishable_from_a_record_wake(self):
        stall = lw.stall_envelope(child_key=CHILD, phase="implementing", due_ts=500, snapshot="S")
        assert "reason=stall" in stall
        assert "event=" not in stall


class TestStateDoubleSanity:
    def test_the_slot_double_matches_the_real_admission_signature(self):
        """Guards the double from drifting from ``_ChatSlot.enqueue_or_run_prompt``.

        This caught a real mismatch when it was written: the parameter is
        ``run_chat_coro``, and the double had named it ``run_chat``. Positional
        calls hid it.
        """
        import inspect

        from kiro_crew.dashboard.state import _ChatSlot

        real = inspect.signature(_ChatSlot.enqueue_or_run_prompt)
        mine = inspect.signature(_Slot.enqueue_or_run_prompt)
        assert list(real.parameters) == list(mine.parameters)
