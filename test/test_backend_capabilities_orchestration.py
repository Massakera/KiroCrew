"""What codex and droid can do for an orchestrator, held to the recorded frames.

Three orchestration paths are covered, each against the evidence its membership
rests on rather than a hand-written shape:

* a droid subagent's conversation survives its process: a restarted adapter
  restores it by id (``droid/session-load-live.jsonl``), and the resume path
  adopts that modes-less load instead of discarding it;
* droid's model and reasoning-effort selects are the ones its ``session/new``
  advertises (``droid/handshake-live.jsonl``);
* ``spawn_steer`` never reports an injection the model did not get: codex rides
  ``_session/steering`` and counts only ``injected``, and a harness with no
  mid-turn channel (droid answers ``-32601``, ``droid/steer-refused-live.jsonl``)
  has the message queued as a follow-up, with the detail saying so.
"""

from __future__ import annotations

import json
import pathlib
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.runtime import AcpRuntime
from kiro_crew.acp.session_handle import AcpSessionHandle, WatchdogSettings
from kiro_crew.agent_sdk import backend_cards
from kiro_crew.agent_sdk.backends import (
    ACP_BACKEND_CODEX,
    ACP_BACKEND_DROID,
    ACP_BACKENDS_ADVERTISED_MODEL_SELECTION,
    ACP_BACKENDS_COMPACT,
    ACP_BACKENDS_DEDICATED_SESSION_RESTORE,
    ACP_BACKENDS_EFFORT_FROM_ADVERTISED_OPTION,
    ACP_BACKENDS_EFFORT_VIA_CONFIG_OPTION,
    ACP_BACKENDS_HARNESS_OWNED_SESSIONS,
    ACP_BACKENDS_LOAD_WITHOUT_MODES,
    ACP_BACKENDS_MODEL_VIA_CONFIG_OPTION,
    ACP_BACKENDS_STEER,
    ACP_BACKENDS_STEERING_EXTENSION,
    effort_config_option_id,
)
from kiro_crew.subagent import SubagentInfo, SubagentManager

FRAMES = pathlib.Path(__file__).parent / "fixtures" / "acp_frames"


def _frames(rel: str) -> list[dict]:
    lines = (FRAMES / rel).read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines[1:] if line.strip()]


def _result_with(frames: list[dict], key: str) -> dict:
    return next(
        f["result"] for f in frames if isinstance(f.get("result"), dict) and key in f["result"]
    )


# ── droid: what the recorded frames establish ──


def test_droid_model_and_effort_selects_are_the_advertised_ones() -> None:
    new = _result_with(_frames("droid/handshake-live.jsonl"), "sessionId")
    options = {o["id"]: o for o in new["configOptions"]}
    assert options["model"]["type"] == "select"
    effort = options[effort_config_option_id(ACP_BACKEND_DROID)]
    assert effort["category"] == "thought_level"
    assert {"low", "medium", "high", "xhigh", "max"} <= {o["value"] for o in effort["options"]}
    for members in (
        ACP_BACKENDS_MODEL_VIA_CONFIG_OPTION,
        ACP_BACKENDS_EFFORT_VIA_CONFIG_OPTION,
        ACP_BACKENDS_EFFORT_FROM_ADVERTISED_OPTION,
        ACP_BACKENDS_ADVERTISED_MODEL_SELECTION,
    ):
        assert ACP_BACKEND_DROID in members


def test_droid_restores_a_session_from_a_second_process() -> None:
    frames = _frames("droid/session-load-live.jsonl")
    init = _result_with(frames, "agentCapabilities")
    assert init["agentCapabilities"]["loadSession"] is True
    listed = _result_with(frames, "sessions")["sessions"]
    replay = [
        f["params"]["update"]
        for f in frames
        if f.get("method") == "session/update"
        and f["params"]["update"]["sessionUpdate"] == "user_message_chunk"
    ]
    load = _result_with(frames, "configOptions")
    refused = next(f for f in frames if "error" in f)

    assert listed and replay, "the second process must list and replay the first's session"
    assert "modes" not in load
    assert "autonomy_level" in {o["id"] for o in load["configOptions"]}
    assert refused["error"]["code"] == -32602
    assert ACP_BACKEND_DROID in ACP_BACKENDS_HARNESS_OWNED_SESSIONS
    assert ACP_BACKEND_DROID in ACP_BACKENDS_LOAD_WITHOUT_MODES
    assert ACP_BACKEND_DROID in ACP_BACKENDS_DEDICATED_SESSION_RESTORE


def test_droid_has_no_steer_and_no_compact_on_its_acp_surface() -> None:
    errors = [f["error"] for f in _frames("droid/steer-refused-live.jsonl") if "error" in f]
    assert [e["code"] for e in errors] == [-32601, -32601]
    assert ACP_BACKEND_DROID not in ACP_BACKENDS_STEER
    assert ACP_BACKEND_DROID not in ACP_BACKENDS_STEERING_EXTENSION

    compact = _frames("droid/compact-as-prompt-live.jsonl")
    assert any(
        f.get("error", {}).get("code") == -32603 for f in compact
    ), "/compact must reach the model as a turn, not be answered as a command"
    assert ACP_BACKEND_DROID not in ACP_BACKENDS_COMPACT


def test_codex_advertises_the_steering_extension() -> None:
    init = _result_with(_frames("codex/session-live.jsonl"), "agentCapabilities")
    assert init["_meta"]["steering"]["supported"] is True
    assert ACP_BACKEND_CODEX in ACP_BACKENDS_STEERING_EXTENSION
    assert ACP_BACKEND_CODEX not in ACP_BACKENDS_STEER


@pytest.mark.parametrize(
    ("backend", "available"),
    [
        (
            ACP_BACKEND_DROID,
            {"crew_tools", "subagent_continuation", "reasoning_effort", "model_switch"},
        ),
        (
            ACP_BACKEND_CODEX,
            {
                "crew_tools",
                "member_thread_tools",
                "subagent_continuation",
                "manual_compact",
                "reasoning_effort",
                "model_switch",
            },
        ),
    ],
)
def test_the_card_reports_what_the_sets_now_hold(backend: str, available: set[str]) -> None:
    card = backend_cards.card_for(backend)
    assert {line.id for line in card.capabilities if line.available} == available


# ── droid: the resume path adopts the restored conversation ──


def _droid_client(tmp_path: pathlib.Path) -> AcpClient:
    client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_DROID)
    proc = MagicMock()
    proc.returncode = None
    proc.stdin = MagicMock()
    proc.stdin.write = MagicMock()
    proc.stdin.drain = AsyncMock()
    client._process = proc
    return client


@pytest.mark.asyncio
async def test_a_droid_resume_is_adopted_from_the_recorded_load(tmp_path: pathlib.Path) -> None:
    frames = _frames("droid/session-load-live.jsonl")
    scripted = {
        "initialize": _result_with(frames, "agentCapabilities"),
        "session/load": _result_with(frames, "configOptions"),
        "session/set_config_option": {},
    }
    sent: list[tuple[str, dict]] = []

    async def fake_send(method: str, params: dict) -> int:
        sent.append((method, params))
        return len(sent)

    async def fake_wait(req_id: int, timeout: float = 50.0, *, method="", expected_mcp=None):
        return scripted.get(sent[req_id - 1][0], {"sessionId": "fresh"})

    client = _droid_client(tmp_path)
    client._send_request = AsyncMock(side_effect=fake_send)
    client._wait_for_response = AsyncMock(side_effect=fake_wait)
    client._drain_notifications = AsyncMock()
    client._resume_session_id = "sess-droid-live-b"

    await client._initialize_session()

    methods = [m for m, _ in sent]
    assert "session/load" in methods
    assert "session/new" not in methods, "a restored droid conversation must not be replaced"
    load_params = dict(sent)["session/load"]
    assert load_params["sessionId"] == "sess-droid-live-b"
    assert "_meta" not in load_params, "droid reads no kiro session_file"
    assert client._session_id == "sess-droid-live-b"
    assert client._resumed is True
    assert (
        "session/set_config_option",
        {"sessionId": "sess-droid-live-b", "configId": "autonomy_level", "value": "normal"},
    ) in sent


# ── codex: _session/steering on the runtime-served handle ──


def _codex_handle(tmp_path: pathlib.Path, answer: dict) -> tuple[AcpSessionHandle, AsyncMock]:
    runtime = AcpRuntime(work_dir=tmp_path, acp_backend=ACP_BACKEND_CODEX)
    runtime._send_and_await = AsyncMock(return_value=answer)
    handle = AcpSessionHandle("sub", MagicMock(), runtime, watchdog=WatchdogSettings())
    handle._session_id = "codex-sess"
    return handle, runtime._send_and_await


@pytest.mark.asyncio
async def test_codex_steering_sends_the_extension_and_returns_the_outcome(
    tmp_path: pathlib.Path,
) -> None:
    handle, wire = _codex_handle(tmp_path, {"outcome": "injected"})
    assert handle.supports_steer is False
    assert handle.supports_steering_extension is True

    assert await handle.inject_steering("use pytest, not unittest") == "injected"

    method, params = wire.await_args.args
    assert method == "_session/steering"
    assert params["sessionId"] == "codex-sess"
    assert params["prompt"][0]["type"] == "text"
    assert "use pytest, not unittest" in params["prompt"][0]["text"]
    assert handle.last_steer_monotonic > 0


@pytest.mark.asyncio
async def test_a_non_member_handle_sends_nothing(tmp_path: pathlib.Path) -> None:
    runtime = AcpRuntime(work_dir=tmp_path, acp_backend="")
    runtime._send_and_await = AsyncMock()
    handle = AcpSessionHandle("sub", MagicMock(), runtime, watchdog=WatchdogSettings())
    handle._session_id = "kiro-sess"
    assert await handle.inject_steering("x") == ""
    runtime._send_and_await.assert_not_awaited()


# ── spawn_steer on the manager ──


def _manager(provider: object) -> SubagentManager:
    sessions = MagicMock()
    sessions.get_pid = MagicMock(return_value=None)
    sessions.get_provider = MagicMock(return_value=provider)
    sessions.set_continuable_fallback = MagicMock()
    ctx = MagicMock()
    ctx.build_message = MagicMock(return_value=("built", None))
    mgr = SubagentManager(sessions=sessions, ctx_builder=ctx)
    mgr._arm_followup_watcher = MagicMock()
    return mgr


def _provider(*, steer: bool, extension: bool, outcome: str = "") -> MagicMock:
    provider = MagicMock()
    provider.supports_steer = steer
    provider.supports_steering_extension = extension
    provider.steer = AsyncMock(return_value=True)
    provider.inject_steering = AsyncMock(return_value=outcome)
    return provider


@pytest.mark.asyncio
async def test_codex_steer_counts_only_an_injected_outcome() -> None:
    provider = _provider(steer=False, extension=True, outcome="injected")
    mgr = _manager(provider)
    info = SubagentInfo(id="run1", task="t", acp_backend=ACP_BACKEND_CODEX)
    mgr._agents["run1"] = info

    ok, detail = await mgr.steer_run("run1", "narrow it to src/")

    assert (ok, detail) == (True, "ok")
    provider.inject_steering.assert_awaited_once_with("narrow it to src/")
    provider.steer.assert_not_awaited()
    assert info.pending_followups == []


@pytest.mark.asyncio
async def test_an_idle_codex_turn_falls_back_to_a_follow_up() -> None:
    provider = _provider(steer=False, extension=True, outcome="promptRequired")
    mgr = _manager(provider)
    info = SubagentInfo(id="run1", task="t", acp_backend=ACP_BACKEND_CODEX)
    mgr._agents["run1"] = info

    ok, detail = await mgr.steer_run("run1", "also add tests")

    assert ok is True
    assert detail.startswith("queued_follow_up: _session/steering answered promptRequired")
    assert info.pending_followups == ["also add tests"]
    mgr._arm_followup_watcher.assert_called_once_with(info)


@pytest.mark.asyncio
async def test_a_droid_steer_is_queued_rather_than_reported_injected() -> None:
    provider = _provider(steer=False, extension=False)
    mgr = _manager(provider)
    info = SubagentInfo(id="run1", task="t", acp_backend=ACP_BACKEND_DROID)
    mgr._agents["run1"] = info

    ok, detail = await mgr.steer_run("run1", "stop editing README")

    assert ok is True
    assert detail.startswith("queued_follow_up: backend droid has no mid-turn steer channel")
    provider.steer.assert_not_awaited()
    provider.inject_steering.assert_not_awaited()
    assert info.pending_followups == ["stop editing README"]


@pytest.mark.asyncio
async def test_a_steer_member_keeps_the_native_path() -> None:
    provider = _provider(steer=True, extension=False)
    mgr = _manager(provider)
    mgr._agents["run1"] = SubagentInfo(id="run1", task="t")

    assert await mgr.steer_run("run1", "go") == (True, "ok")
    provider.steer.assert_awaited_once_with("go")


def test_spawn_steer_tells_the_coordinator_when_it_was_queued(monkeypatch) -> None:
    from kiro_crew.mcp_tools import spawn as spawn_tools

    monkeypatch.setattr(
        spawn_tools.mcp_core,
        "_post",
        lambda path, body: {
            "id": "run1",
            "status": "follow_up_queued",
            "detail": "queued_follow_up: backend droid has no mid-turn steer channel",
        },
    )
    out = spawn_tools.spawn_steer("spawn_steer", {"agent_id": "run1", "message": "x"})
    assert "could not be steered mid-turn" in out
    assert "backend droid" in out
    assert "injected" not in out
