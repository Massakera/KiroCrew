"""Focused contracts for the opt-in investigations extension; no live services."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiohttp import web

from kiro_crew.investigation_policy import diagnostic_read
from kiro_crew.investigation_routes import register_routes
from kiro_crew.investigations import Engine, InvestigationError, service_config


class State:
    def __init__(self):
        self._slots = {}
        self.sessions = object()

    def get_slot(self, key):
        return self._slots.get(key)


@pytest.fixture
def engine(tmp_path):
    return Engine(State(), tmp_path)


@pytest.fixture(autouse=True)
def isolate_disable_hooks(monkeypatch):
    monkeypatch.setattr("kiro_crew.apps.teardown._APP_DISABLE_HOOKS", {})


def test_service_requires_identity_pins():
    with pytest.raises(InvestigationError, match="12-digit"):
        service_config({"name": "API", "aws_profile": "prod"})
    with pytest.raises(InvestigationError, match="HTTPS"):
        service_config({"name": "API", "kube_context": "prod"})
    assert service_config({"name": "API", "database": "local pg_service entry"})["name"] == "API"


@pytest.mark.asyncio
async def test_start_snapshot_survives_restart_and_service_edit(engine, monkeypatch):
    monkeypatch.setattr(engine, "launch", Mock())
    service = engine.save_service({"name": "API"})
    run = await engine.start(service["id"], "Investigate latency", "chat-origin")
    row = engine.runs[run["id"]]
    engine.update(row, "running", "Reading logs")
    engine.save_service({**service, "name": "Renamed"})
    restored = Engine(State(), engine.directory)
    assert restored.runs[row["id"]]["status"] == "interrupted"
    assert restored.runs[row["id"]]["service"]["name"] == "API"
    assert restored.runs[row["id"]]["question"] == "Investigate latency"
    assert restored.runs[row["id"]]["origin"] == "chat-origin"


@pytest.mark.asyncio
async def test_identity_mismatch_stops_before_slot(engine, monkeypatch):
    monkeypatch.setattr("kiro_crew.platform_compat.trusted_aws_bin", lambda: "/usr/bin/aws")
    engine.command = AsyncMock(return_value=json.dumps({"Account": "222222222222"}))
    service = service_config({"name": "API", "aws_profile": "prod", "aws_account": "111111111111"})
    with pytest.raises(InvestigationError, match="mismatch"):
        await engine.preflight(service)
    assert engine.command.call_args.args[0][-4:] == ["--profile", "prod", "--output", "json"]
    assert not engine.state._slots


@pytest.mark.asyncio
async def test_kube_checks_explicit_context(engine):
    engine.command = AsyncMock(
        return_value=json.dumps({"clusters": [{"cluster": {"server": "https://wrong"}}]})
    )
    with pytest.raises(InvestigationError, match="mismatch"):
        await engine.preflight(
            service_config(
                {"name": "API", "kube_context": "prod", "kube_server": "https://expected"}
            )
        )
    assert engine.command.call_args.args[0][:3] == ["kubectl", "--context", "prod"]


@pytest.mark.asyncio
async def test_cancel_revokes_binding_before_stop(engine, monkeypatch):
    monkeypatch.setattr(engine, "launch", Mock())
    service = engine.save_service({"name": "API"})
    result = await engine.start(service["id"], "Check")
    row = engine.runs[result["id"]]
    slot = SimpleNamespace(
        key=row["slot_key"], _app="service-investigations", running=True, task=None
    )
    engine.state._slots[slot.key] = slot
    engine.slots[row["id"]] = slot

    async def stop(*args, **kwargs):
        assert row["status"] == "cancelled"

    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.stop_slot_turn", stop)
    await engine.cancel(row)
    assert Engine(State(), engine.directory).runs[row["id"]]["status"] == "cancelled"


@pytest.mark.asyncio
async def test_launch_deduplicates_inflight_preflight(engine, monkeypatch):
    started = asyncio.Event()
    release = asyncio.Event()

    async def run(*args):
        started.set()
        await release.wait()

    monkeypatch.setattr(engine, "run", run)
    service = engine.save_service({"name": "API"})
    result = await engine.start(service["id"], "Check")
    try:
        await asyncio.wait_for(started.wait(), 1)
        with pytest.raises(InvestigationError, match="already running"):
            engine.launch(engine.runs[result["id"]])
    finally:
        release.set()
        await engine.tasks[result["id"]]


@pytest.fixture
def policy_run(engine, monkeypatch):
    row = {"id": "test", "status": "running", "service": {}, "scratch": "/tmp/test"}
    slot = SimpleNamespace(key="investigation-test")
    engine.runs["test"] = row
    engine.slots["test"] = slot
    engine.state._slots[slot.key] = slot
    monkeypatch.setattr("kiro_crew.apps.manager.is_app_enabled", lambda name: True)
    event = SimpleNamespace(
        child_low_fidelity=False,
        tool_input_redacted=False,
        tool_name="bash",
        mcp_server_name="",
        is_shell=True,
        shell_command="kubectl get pods",
        tool_input=json.dumps({"command": "kubectl get pods"}),
    )
    return engine, row, slot, event


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "verdict,allowed",
    [
        ('{"decision":"read"}', True),
        ('{"decision":"change"}', False),
        ('{"decision":"unknown"}', False),
        ("read", False),
        ("{}", False),
    ],
)
async def test_semantic_decisions_fail_closed(policy_run, monkeypatch, verdict, allowed):
    engine, row, slot, event = policy_run
    review = AsyncMock(return_value=verdict)
    monkeypatch.setattr("kiro_crew.llm_helpers.run_bg_oneliner", review)
    assert await diagnostic_read(engine.state, slot, event) is allowed
    assert "kubectl get pods" in review.call_args.args[1]


@pytest.mark.asyncio
async def test_cancel_during_review_cannot_grant(policy_run, monkeypatch):
    engine, row, slot, event = policy_run

    async def review(*args, **kwargs):
        row["status"] = "cancelled"
        return '{"decision":"read"}'

    monkeypatch.setattr("kiro_crew.llm_helpers.run_bg_oneliner", review)
    assert not await diagnostic_read(engine.state, slot, event)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [TimeoutError(), RuntimeError("unavailable")])
async def test_review_failure_uses_approval(policy_run, monkeypatch, failure):
    engine, row, slot, event = policy_run
    monkeypatch.setattr("kiro_crew.llm_helpers.run_bg_oneliner", AsyncMock(side_effect=failure))
    assert not await diagnostic_read(engine.state, slot, event)


@pytest.mark.asyncio
async def test_restored_metadata_cannot_mint_grant(policy_run, monkeypatch):
    engine, row, slot, event = policy_run
    review = AsyncMock(return_value='{"decision":"read"}')
    monkeypatch.setattr("kiro_crew.llm_helpers.run_bg_oneliner", review)
    clone = SimpleNamespace(key=slot.key, _app="service-investigations")
    assert not await diagnostic_read(engine.state, clone, event)
    review.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["child_low_fidelity", "tool_input_redacted"])
async def test_missing_actual_arguments_require_approval(policy_run, monkeypatch, field):
    engine, row, slot, event = policy_run
    setattr(event, field, True)
    review = AsyncMock()
    monkeypatch.setattr("kiro_crew.llm_helpers.run_bg_oneliner", review)
    assert not await diagnostic_read(engine.state, slot, event)
    review.assert_not_called()


class Request(dict):
    def __init__(self, state, body, **claims):
        super().__init__(claims)
        self.app = {"state": state}
        self.headers = {}
        self.method = "POST"
        self.body = body

    async def json(self):
        return self.body


@pytest.mark.asyncio
async def test_routes_refuse_foreign_app_and_unknown_internal_session(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "kiro_crew.investigation_routes.is_owner_dashboard_request", lambda r: False
    )
    monkeypatch.setattr(
        "kiro_crew.investigation_routes.private_owner_surface_refusal", AsyncMock(return_value=None)
    )
    ctx = SimpleNamespace(data_dir=tmp_path)
    handler = register_routes(ctx)[1].handler
    with pytest.raises(web.HTTPForbidden):
        await handler(Request(State(), {"action": "list"}, app="other-app"), ctx)
    with pytest.raises(web.HTTPForbidden):
        await handler(Request(State(), {"action": "start"}, internal_auth=True), ctx)
    assert not (tmp_path / "investigations.sqlite3").exists()


@pytest.mark.asyncio
async def test_service_config_is_owner_action_and_roundtrips(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.investigation_routes.is_owner_dashboard_request", lambda r: True)
    ctx = SimpleNamespace(data_dir=tmp_path)
    handler = register_routes(ctx)[1].handler
    state = State()
    response = await handler(
        Request(state, {"action": "save_service", "service": {"name": "API"}}), ctx
    )
    assert response.status == 200
    response = await handler(Request(state, {"action": "list"}), ctx)
    assert json.loads(response.text)["services"][0]["name"] == "API"


@pytest.mark.asyncio
async def test_cancel_and_disable_stop_turn_waiting_for_capacity(engine, monkeypatch):
    from kiro_crew.apps.teardown import _APP_DISABLE_HOOKS

    monkeypatch.setattr(engine, "prepare_turn", AsyncMock())
    monkeypatch.setattr(engine, "preflight", AsyncMock(return_value={}))
    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.stop_slot_turn", AsyncMock())
    executed = AsyncMock()
    monkeypatch.setattr("kiro_crew.dashboard.chat_runner._run_chat", executed)
    waiting = asyncio.Event()
    release = asyncio.Event()

    async def background(slot, coro):
        waiting.set()
        try:
            await release.wait()
        except BaseException:
            coro.close()
            raise
        return await coro

    engine.state.run_background_turn = background

    class Slot:
        _app = "service-investigations"
        _approval_futures = {}
        task = None

        @property
        def running(self):
            return self.task is not None and not self.task.done()

        def enqueue_or_run_prompt(self, prompt, turn, state):
            self.task = asyncio.create_task(turn(state, self, prompt))

    async def ensure(row):
        slot = Slot()
        slot.key = row["slot_key"]
        engine.state._slots[slot.key] = slot
        engine.slots[row["id"]] = slot
        return slot

    monkeypatch.setattr(engine, "ensure_slot", ensure)
    service = engine.save_service({"name": "API"})
    result = await engine.start(service["id"], "Check")
    await engine.tasks[result["id"]]
    await asyncio.wait_for(waiting.wait(), 1)
    await engine.cancel(engine.runs[result["id"]])
    assert engine.slots[result["id"]].task.cancelled()
    release.set()
    executed.assert_not_called()
    waiting.clear()
    release.clear()
    result = await engine.start(service["id"], "Check again")
    await engine.tasks[result["id"]]
    await asyncio.wait_for(waiting.wait(), 1)
    await _APP_DISABLE_HOOKS["service-investigations"]()
    assert engine.closed
    assert engine.slots[result["id"]].task.cancelled()
    assert engine.runs[result["id"]]["status"] == "interrupted"
    with pytest.raises(InvestigationError, match="disabled"):
        engine.launch(engine.runs[result["id"]])
    executed.assert_not_called()


def test_investigations_never_propagate_yolo_or_trust():
    from kiro_crew.dashboard.chat_runner import _persistable_session_policy

    slot = SimpleNamespace(_app="service-investigations", _trust=True)
    assert _persistable_session_policy(slot, True) == ""
    assert _persistable_session_policy(slot, False) == ""


@pytest.mark.asyncio
async def test_prepare_refreshes_spec_and_retires_stale_runtime(policy_run, monkeypatch):
    from kiro_crew.agent_sdk.backends import ACP_BACKEND_KIRO
    from kiro_crew.dashboard.side_readonly_spec import PublishedSpec

    engine, row, slot, event = policy_run
    slot.project = ""
    slot.agent = "unsafe-agent"
    engine.state.sessions = SimpleNamespace(reset=AsyncMock())
    published = Mock(return_value=PublishedSpec(name="investigator--readonly", digest="v1"))
    monkeypatch.setattr("kiro_crew.dashboard.side_readonly_spec.publish_readonly_spec", published)
    monkeypatch.setattr("kiro_crew.dashboard.chat_utils.slot_history_key", lambda s: s.key)
    monkeypatch.setattr("kiro_crew.execution_context.read_session_execution", lambda key: None)
    monkeypatch.setattr(
        "kiro_crew.config.loader.KiroCrewConfig.load",
        lambda: SimpleNamespace(agent=SimpleNamespace(acp_backend=ACP_BACKEND_KIRO)),
    )

    # Avoid the Python 3.14/old pytest-asyncio executor teardown incompatibility.
    async def inline(function, *args):
        return function(*args)

    monkeypatch.setattr(asyncio, "to_thread", inline)
    await engine.prepare_turn(slot)
    engine.state.sessions.reset.assert_awaited_once_with(slot.key)
    assert slot.agent == "investigator--readonly"
    await engine.prepare_turn(slot)
    assert engine.state.sessions.reset.await_count == 1
    published.return_value = PublishedSpec(name="investigator--readonly", digest="v2")
    await engine.prepare_turn(slot)
    assert engine.state.sessions.reset.await_count == 2


@pytest.mark.asyncio
async def test_prepare_refuses_other_harness_without_switching(policy_run, monkeypatch):
    engine, row, slot, event = policy_run
    cfg = SimpleNamespace(agent=SimpleNamespace(acp_backend="codex"))
    monkeypatch.setattr("kiro_crew.config.loader.KiroCrewConfig.load", lambda: cfg)

    async def inline(function, *args):
        return function(*args)

    monkeypatch.setattr(asyncio, "to_thread", inline)
    with pytest.raises(InvestigationError, match="has not been changed"):
        await engine.prepare_turn(slot)
    assert cfg.agent.acp_backend == "codex"


def test_installed_agent_derives_with_mcp_mounted_and_no_grants(tmp_path, monkeypatch):
    from kiro_crew import agent
    from kiro_crew.apps import bridges
    from kiro_crew.apps.manifest import AppManifest
    from kiro_crew.dashboard import side_readonly_spec

    root = Path(__file__).resolve().parents[1] / "packages/service-investigations"
    manifest = AppManifest.from_json_file(root / "app.json")
    registry = tmp_path / "agents"
    registry.mkdir()
    monkeypatch.setattr(bridges, "KIRO_AGENTS_DIR", registry)
    monkeypatch.setattr(agent, "KIRO_AGENTS_DIR", registry)
    monkeypatch.setattr(bridges, "_mcp_json_path", lambda: tmp_path / "mcp.json")
    monkeypatch.setattr(bridges, "_agent_mcp_policy", lambda name: {})
    monkeypatch.setattr(
        bridges,
        "_own_mcp_servers",
        lambda name: {f"{name}:investigations": manifest.mcpServers["investigations"]},
    )
    monkeypatch.setattr(bridges, "schedule_materialized_agents_refresh", lambda: None)
    monkeypatch.setattr(side_readonly_spec, "_refresh_materialized_snapshot", lambda: None)
    assert manifest.validate(root) == []
    assert bridges._register_agents("service-investigations", manifest, root)
    published = side_readonly_spec.publish_readonly_spec("service-investigator")
    spec = json.loads((registry / f"{published.name}.json").read_text())
    assert spec["allowedTools"] == []
    assert spec["includeMcpJson"] is False
    assert "@service-investigations:investigations" in spec["tools"]
    assert "service-investigations:investigations" in spec["mcpServers"]
    assert all("autoApprove" not in server for server in spec["mcpServers"].values())


def test_effective_agent_cannot_override_investigation_binding(policy_run):
    from kiro_crew.investigation_policy import require_agent

    engine, row, slot, event = policy_run
    engine.specs[row["id"]] = ("service-investigator--readonly", "digest")
    slot.agent = "service-investigator--readonly"
    require_agent(engine.state, slot, slot.agent)
    for effective in ("kirocrew", "", "another-template"):
        with pytest.raises(RuntimeError, match="effective agent changed"):
            require_agent(engine.state, slot, effective)


@pytest.mark.asyncio
async def test_resume_waits_for_old_wrapper_even_after_native_slot_cleared(engine, monkeypatch):
    with monkeypatch.context() as local:
        local.setattr(engine, "launch", Mock())
        service = engine.save_service({"name": "API"})
        result = await engine.start(service["id"], "Check")
    row = engine.runs[result["id"]]
    release = asyncio.Event()
    task = asyncio.create_task(release.wait())
    engine.turn_tasks[row["id"]] = task
    try:
        with pytest.raises(InvestigationError, match="already running"):
            await engine.resume(row)
    finally:
        release.set()
        await task


@pytest.mark.asyncio
async def test_resume_cannot_enter_during_cancel(engine, monkeypatch):
    service = engine.save_service({"name": "API"})
    launcher = Mock()
    monkeypatch.setattr(engine, "launch", launcher)
    result = await engine.start(service["id"], "Check")
    row = engine.runs[result["id"]]
    entered = asyncio.Event()
    finish = asyncio.Event()

    async def cancel(target):
        entered.set()
        await finish.wait()

    monkeypatch.setattr(engine, "_cancel", cancel)
    cancelled = asyncio.create_task(engine.cancel(row))
    await asyncio.wait_for(entered.wait(), 1)
    resumed = asyncio.create_task(engine.resume(row))
    try:
        await asyncio.sleep(0)
        assert launcher.call_count == 1
    finally:
        finish.set()
        await asyncio.gather(cancelled, resumed)
    assert launcher.call_count == 2


@pytest.mark.asyncio
async def test_cancel_stops_native_followup_after_retained_turn_finished(engine, monkeypatch):
    monkeypatch.setattr(engine, "launch", Mock())
    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.stop_slot_turn", AsyncMock())
    service = engine.save_service({"name": "API"})
    result = await engine.start(service["id"], "Check")
    row = engine.runs[result["id"]]
    previous = asyncio.create_task(asyncio.sleep(0))
    await previous
    release = asyncio.Event()
    native = asyncio.create_task(release.wait())
    slot = SimpleNamespace(task=native)
    engine.slots[row["id"]] = slot
    engine.turn_tasks[row["id"]] = previous
    try:
        await engine.cancel(row)
        assert native.cancelled()
    finally:
        release.set()
        await asyncio.gather(native, return_exceptions=True)


@pytest.mark.asyncio
async def test_native_followup_waits_for_identity_verification(policy_run):
    engine, row, slot, event = policy_run
    row["status"] = "checking"
    with pytest.raises(InvestigationError, match="identity verification"):
        await engine.prepare_turn(slot)
    assert not await diagnostic_read(engine.state, slot, event)
