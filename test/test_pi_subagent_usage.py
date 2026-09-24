"""Pi-subagents child usage, from fixtures. No live credentials."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew.acp._dispatch import parse_session_update
from kiro_crew.acp.pi_subagent_usage import parse_pi_subagent_children
from kiro_crew.agent_sdk.backends import ACP_BACKEND_DROID, ACP_BACKEND_PI
from kiro_crew.dashboard.handlers import usage as usage_mod
from kiro_crew.dashboard.handlers.usage import slot_turn_usage
from kiro_crew.dashboard.pi_child_usage import (
    close_pi_children_async,
    live_task_rows,
    observe_pi_children_async,
    provider_for_completed_turn,
    reset_pi_child_usage_for_tests,
)

_FIXTURES = Path(__file__).parent / "fixtures" / "pi_subagents"


def _load(name: str) -> dict:
    return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(usage_mod, "_TOKEN_USAGE_DIR", tmp_path)
    reset_pi_child_usage_for_tests()
    yield
    reset_pi_child_usage_for_tests()


class _Bus:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def broadcast_ws(self, kind: str, payload: dict) -> None:
        self.events.append((kind, payload))


def test_foreground_children_keep_their_own_usage_and_the_transcript_text():
    update = _load("foreground.json")
    events = parse_session_update(update)
    result = next(event for event in events if event.tool_output)
    assert "child finished" in (result.tool_output or "")
    assert "totalChildUsage" not in (result.tool_output or "")
    children = result.pi_children or []
    assert [child["agent"] for child in children] == [
        "review-architecture",
        "review-simplicity",
    ]
    assert children[0]["input"] == 120
    assert children[0]["cost"] == 0.02
    assert children[0]["input"] != 9999
    factory = children[1]
    assert factory["model"] == "factory/claude-opus-5-5"
    assert factory["input"] == 80
    assert factory["cost"] == 0.0
    assert factory["terminal"] is True
    assert "/tmp/not-a-secret" not in factory["id"]


def test_async_status_stays_live_and_does_not_invent_an_input_split():
    children = parse_pi_subagent_children(_load("async-status.json"))
    assert len(children) == 1
    child = children[0]
    assert child["id"] == "pi:run-77"
    assert child["terminal"] is False
    assert child["total_tokens"] == 4321
    assert child["input"] is None
    assert child["output"] is None
    assert child["cost"] == 0.0
    assert child["requested_model"] == "factory/claude-opus-5-5"
    assert child["model"].startswith("factory/")


def test_missing_numbers_stay_missing():
    (child,) = parse_pi_subagent_children(_load("missing-numbers.json"))
    assert child["output"] == 15
    assert child["input"] is None
    assert child["cache_read"] is None
    assert child["cache_write"] is None
    assert child["cost"] is None
    assert child["total_tokens"] is None


def test_an_unrelated_tool_is_not_a_child():
    assert parse_pi_subagent_children(_load("unrelated-tool.json")) == []


def _shard_rows(tmp_path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in next(tmp_path.glob("*.jsonl")).read_text(encoding="utf-8").splitlines()
    ]


@pytest.mark.asyncio
async def test_a_factory_or_pi_child_is_not_labeled_kiro(tmp_path):
    bus = _Bus()
    await observe_pi_children_async(
        bus,
        slot_key="chat-1",
        session_key="dashboard:chat-1",
        children=parse_pi_subagent_children(_load("labeled-kiro.json")),
    )
    (row,) = _shard_rows(tmp_path)
    assert row["agent"] == "factory/claude-opus-5-5"
    assert row["provider"] == ACP_BACKEND_DROID
    assert row["cost"] == 0.0
    assert "input" in row
    assert bus.events[0][1]["agent"] == "factory/claude-opus-5-5"
    assert "kiro" not in row["agent"]
    assert row["provider"] != "kiro"


@pytest.mark.asyncio
async def test_ledger_rows_hang_under_the_parent_and_omit_missing_numbers(tmp_path):
    bus = _Bus()
    await observe_pi_children_async(
        bus,
        slot_key="chat-1",
        session_key="dashboard:chat-1",
        children=parse_pi_subagent_children(_load("foreground.json")),
    )
    turns = slot_turn_usage("chat-1")
    by_agent = {turn["model"]: turn for turn in turns}
    pi = next(turn for turn in turns if turn["model"] == "opencode-go/mimo-v2.6-pro")
    factory = by_agent["factory/claude-opus-5-5"]
    assert pi["input"] == 120
    assert pi["output"] == 30
    assert pi["cost"] == 0.02
    assert factory["input"] == 80
    assert factory["cost"] == 0.0
    assert "total_tokens" not in factory
    raw = _shard_rows(tmp_path)
    for row in raw:
        assert row["provider"] in {ACP_BACKEND_PI, ACP_BACKEND_DROID}
        assert row["provider"] != "kiro"
        assert row["agent"] not in {"kiro", "kirocrew"}
        assert row["slot"] == "chat-1"
        assert row["surface"] == "subagent"
        assert "sessionFile" not in row
        assert "/tmp/not-a-secret" not in json.dumps(row)
    assert next(row for row in raw if row["model"].startswith("factory/"))["provider"] == (
        ACP_BACKEND_DROID
    )
    kinds = [kind for kind, _payload in bus.events]
    assert kinds[0] == "subagent_spawn"
    assert "subagent_done" in kinds
    assert all(payload["agent"] not in {"", "kiro", "kirocrew"} for _kind, payload in bus.events)
    assert live_task_rows() == []

    await observe_pi_children_async(
        bus,
        slot_key="chat-1",
        session_key="dashboard:chat-1",
        children=parse_pi_subagent_children(_load("foreground.json")),
    )
    assert len(slot_turn_usage("chat-1")) == 2


@pytest.mark.asyncio
async def test_missing_input_is_omitted_from_the_parent_drilldown():
    await observe_pi_children_async(
        _Bus(),
        slot_key="chat-1",
        session_key="dashboard:chat-1",
        children=parse_pi_subagent_children(_load("missing-numbers.json")),
    )
    (turn,) = slot_turn_usage("chat-1")
    assert turn["output"] == 15
    assert "input" not in turn
    assert "cost" not in turn
    assert "credits" not in turn
    assert turn["model"] == "opencode-go/mimo-v2.6-pro"


@pytest.mark.asyncio
async def test_system_shows_a_live_child_only_while_it_is_spending(tmp_path):
    child = parse_pi_subagent_children(_load("async-status.json"))
    bus = _Bus()
    await observe_pi_children_async(
        bus,
        slot_key="chat-1",
        session_key="dashboard:chat-1",
        children=child,
    )
    (row,) = live_task_rows()
    assert row["parent"] == "dashboard:chat-1"
    assert row["agent"] == "review-api-security"
    assert row["sampled"] is False
    assert row["procs"] is None
    assert row["pid"] is None
    assert slot_turn_usage("chat-1") == []
    assert bus.events[0][0] == "subagent_spawn"
    assert bus.events[0][1]["model"] == "factory/claude-opus-5-5"
    assert bus.events[0][1]["total_tokens"] == 4321
    assert bus.events[0][1]["cost_usd"] == 0.0
    assert "input_tokens" not in bus.events[0][1]

    await close_pi_children_async(bus, slot_key="chat-1", session_key="dashboard:chat-1")
    assert live_task_rows() == []
    (turn,) = slot_turn_usage("chat-1")
    assert turn["total_tokens"] == 4321
    assert "input" not in turn
    assert turn["cost"] == 0.0
    (row,) = _shard_rows(tmp_path)
    assert row["provider"] == ACP_BACKEND_DROID
    assert row["agent"] == "review-api-security"


def test_droid_turn_label_does_not_relabel_the_pi_coordinator():
    assert provider_for_completed_turn("acp", ACP_BACKEND_DROID) == ACP_BACKEND_DROID
    assert provider_for_completed_turn("acp", ACP_BACKEND_PI) == "acp"
