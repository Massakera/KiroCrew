"""Pi sessions list Pi's models and Pi's agent profiles, not Kiro's.

``/api/models`` used to fall through to ``kiro-cli --list-models`` for the pi
backend. On a machine without that catalog the dashboard substituted an auto
row and labelled it Default. ``/api/agents/catalog`` listed KiroCrew members
and ``~/.kiro/agents`` templates. A Pi profile is not one of those templates,
so picking it with ``agent_kind=template`` answered 409.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew import model_registry
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp_backends import ACP_BACKEND_KIRO, ACP_BACKEND_PI, model_registry_namespace
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.config.sections import KiroCrewAgentConfig, ResolvedBindings
from kiro_crew.dashboard import chat_handlers
from kiro_crew.dashboard.chat import api_chat_slot_agent
from kiro_crew.dashboard.handlers import agent_catalog, agents
from kiro_crew.dashboard.state import DashboardState, _ChatSlot
from kiro_crew.pi_agents import is_pi_user_profile, list_pi_agent_profiles


@pytest.fixture(autouse=True)
def _cold_advertised_cache(monkeypatch):
    monkeypatch.setattr(model_registry, "_ADVERTISED_MODELS", {})
    monkeypatch.setattr(model_registry, "persist_advertised_models", lambda: None)


def _request() -> MagicMock:
    request = MagicMock()
    request.app = {"state": SimpleNamespace(sessions=SimpleNamespace(active_providers=lambda: []))}
    return request


def _names(rows: list[dict]) -> list[str]:
    return [row["model_name"] for row in rows]


def _pi_config() -> KiroCrewConfig:
    config = KiroCrewConfig()
    config.agent.acp_backend = ACP_BACKEND_PI
    return config


def _write_profile(root: Path, relative: str, body: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


# ── models ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_api_models_lists_advertised_pi_ids_and_does_not_spawn_kiro(monkeypatch):
    monkeypatch.setattr(agents.KiroCrewConfig, "load", staticmethod(_pi_config))

    async def _never_spawn(*_a, **_k):
        raise AssertionError("pi must not spawn kiro-cli --list-models")

    monkeypatch.setattr(agents, "reject_if_kiro_unverified", _never_spawn)
    namespace = model_registry_namespace(ACP_BACKEND_PI)
    model_registry.refresh_advertised_models(
        namespace,
        ["factory/claude-opus-5-5", "openai-codex/gpt-6-astra", "opencode-go/mimo-v2"],
    )

    resp = await agents.api_models(_request())

    assert resp.status == 200
    assert _names(json.loads(resp.body)) == [
        "auto",
        "factory/claude-opus-5-5",
        "openai-codex/gpt-6-astra",
        "opencode-go/mimo-v2",
    ]


@pytest.mark.asyncio
async def test_api_models_503_when_pi_has_advertised_nothing(monkeypatch):
    monkeypatch.setattr(agents.KiroCrewConfig, "load", staticmethod(_pi_config))
    monkeypatch.setattr(
        agents,
        "reject_if_kiro_unverified",
        AsyncMock(side_effect=AssertionError("empty pi catalog must not reach kiro")),
    )

    resp = await agents.api_models(_request())

    assert resp.status == 503
    body = json.loads(resp.body)
    assert body["code"] == "model_catalog_unavailable"
    assert "auto" not in json.dumps(body)


@pytest.mark.asyncio
async def test_api_models_kiro_backend_does_not_use_the_pi_503(monkeypatch):
    """An empty kiro backend still takes the kiro gate, not the pi catalog error."""
    config = KiroCrewConfig()
    config.agent.acp_backend = ACP_BACKEND_KIRO
    monkeypatch.setattr(agents.KiroCrewConfig, "load", staticmethod(lambda: config))
    blocked = web.json_response({"error": "signed out"}, status=503)
    monkeypatch.setattr(agents, "reject_if_kiro_unverified", AsyncMock(return_value=blocked))

    resp = await agents.api_models(_request())

    assert resp.status == 503
    assert json.loads(resp.body) == {"error": "signed out"}


# ── agent profiles ────────────────────────────────────────────────────────


def test_list_pi_profiles_reads_frontmatter_and_skips_chains(tmp_path: Path):
    _write_profile(
        tmp_path,
        "explorer.md",
        "---\nname: explorer\ndescription: reads the tree\nmodel: openai-codex/gpt-6-sol\nthinking: low\n---\n# prompt\n",
    )
    _write_profile(
        tmp_path,
        "nested/review-flex.md",
        "---\nname: review-flex\ndescription: flex review\nmodel: factory/claude-opus-5-5\nthinking: high\n---\n",
    )
    _write_profile(tmp_path, "skip.chain.md", "---\nname: skip-chain\n---\n")
    outside = tmp_path.parent / "escaped-profile.md"
    outside.write_text("---\nname: escaped\n---\n", encoding="utf-8")
    link = tmp_path / "linked.md"
    link.symlink_to(outside)

    profiles = list_pi_agent_profiles(tmp_path)

    assert [row["name"] for row in profiles] == ["explorer", "review-flex"]
    by_name = {row["name"]: row for row in profiles}
    assert by_name["explorer"]["model"] == "openai-codex/gpt-6-sol"
    assert by_name["review-flex"]["reasoning_effort"] == "high"
    assert is_pi_user_profile("review-flex", tmp_path)
    assert not is_pi_user_profile("escaped", tmp_path)
    assert not is_pi_user_profile("kirocrew", tmp_path)


def test_missing_pi_agents_dir_is_not_an_empty_catalog(tmp_path: Path):
    missing = tmp_path / "nope"
    with pytest.raises(FileNotFoundError):
        list_pi_agent_profiles(missing)


@pytest.mark.asyncio
async def test_catalog_on_pi_lists_profiles_and_not_kiro_agents(monkeypatch, tmp_path: Path):
    _write_profile(
        tmp_path,
        "review-architecture.md",
        "---\nname: review-architecture\ndescription: architecture\n---\n",
    )
    config = _pi_config()
    config.default_agent = "kirocrew"
    config.agents["reviewer"] = KiroCrewAgentConfig(kiro_agent="reviewer")
    monkeypatch.setattr(KiroCrewConfig, "load", lambda: config)
    monkeypatch.setattr(
        agent_catalog, "list_agents", MagicMock(side_effect=AssertionError("kiro discovery"))
    )
    monkeypatch.setenv("KIROCREW_PI_AGENTS_DIR", str(tmp_path))

    app = web.Application()
    app["state"] = SimpleNamespace(owner_id="owner", _slots={})
    app.router.add_get("/api/agents/catalog", agent_catalog.api_agent_catalog)
    async with TestClient(TestServer(app)) as client:
        response = await client.get("/api/agents/catalog")
        assert response.status == 200
        body = await response.json()
    assert [row["name"] for row in body["agents"]] == ["review-architecture"]
    assert body["agents"][0]["selection_kind"] == "template"
    assert body["agents"][0]["scope"] == "pi"
    assert body["default_agent"] == ""
    agent_catalog.list_agents.assert_not_called()


@pytest.mark.asyncio
async def test_catalog_on_pi_503_when_the_profiles_dir_is_missing(monkeypatch, tmp_path: Path):
    config = _pi_config()
    monkeypatch.setattr(KiroCrewConfig, "load", lambda: config)
    monkeypatch.setattr(
        agent_catalog, "list_agents", MagicMock(side_effect=AssertionError("kiro discovery"))
    )
    monkeypatch.setenv("KIROCREW_PI_AGENTS_DIR", str(tmp_path / "missing"))

    app = web.Application()
    app["state"] = SimpleNamespace(owner_id="owner", _slots={})
    app.router.add_get("/api/agents/catalog", agent_catalog.api_agent_catalog)
    async with TestClient(TestServer(app)) as client:
        response = await client.get("/api/agents/catalog")
        assert response.status == 503
        body = await response.json()
    assert body["code"] == "agent_catalog_unavailable"
    assert "agents" not in body


# ── slot agent pick ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pi_profile_pick_sticks_without_retargeting_the_workspace(
    monkeypatch, tmp_path: Path
):
    _write_profile(tmp_path, "review-flex.md", "---\nname: review-flex\ndescription: flex\n---\n")
    monkeypatch.setenv("KIROCREW_PI_AGENTS_DIR", str(tmp_path))
    config = _pi_config()
    monkeypatch.setattr(chat_handlers.KiroCrewConfig, "load", staticmethod(lambda: config))
    bindings = ResolvedBindings(
        workspace_dir=Path("/tmp/kiro-default-workspace"),
        memory_store_name="kiro-store",
        effective_memory_config={},
        kiro_agent="kirocrew",
        requested_resolved=False,
        selection_kind="",
    )
    monkeypatch.setattr(chat_handlers, "resolve_agent_bindings", lambda *_a, **_k: bindings)
    monkeypatch.setattr(chat_handlers, "warm_project_agent_names", AsyncMock())
    monkeypatch.setattr(chat_handlers, "session_agent_selection_name", lambda *_a, **_k: None)
    monkeypatch.setattr(chat_handlers, "_workspace_name_for_dir", lambda *_a, **_k: "retargeted")
    monkeypatch.setattr(chat_handlers, "default_project_dir", lambda *_a, **_k: "/retargeted")
    monkeypatch.setattr(chat_handlers, "cached_project_agent_names", lambda *_a, **_k: frozenset())
    monkeypatch.setattr(chat_handlers, "_subagents_attached_response", AsyncMock(return_value=None))
    monkeypatch.setattr(chat_handlers, "_reset_slot_session_or_warn", AsyncMock(return_value=True))
    recorded = AsyncMock(return_value=None)
    monkeypatch.setattr(chat_handlers, "_record_explicit_agent_selection", recorded)
    monkeypatch.setattr(chat_handlers, "schedule_eager_spawn", MagicMock())
    monkeypatch.setattr(chat_handlers, "sel", lambda: MagicMock())
    import kiro_crew.execution_context as execution_context

    monkeypatch.setattr(execution_context, "read_session_execution", lambda *_a, **_k: None)

    slot = _ChatSlot("s1")
    slot.agent = "old-agent"
    slot.workspace = "kept-ws"
    slot.project = "/kept"
    slot.memory_store = "kept-store"
    state = MagicMock(spec=DashboardState)
    state.owner_id = "owner"
    state._slots = {slot.key: slot}
    state.sessions = MagicMock()
    state.sessions.get_provider = MagicMock(return_value=None)
    state.sessions.reset = AsyncMock(return_value=True)
    state.conversation_log = None
    state.push_slots_update = MagicMock()

    @web.middleware
    async def owner(request, handler):
        request["app"] = ""
        request["user"] = "owner"
        return await handler(request)

    app = web.Application(middlewares=[owner])
    app["state"] = state
    app.router.add_post("/api/chat/slots/{slot}/agent", api_chat_slot_agent)
    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/api/chat/slots/s1/agent",
            json={"agent": "review-flex", "agent_kind": "template"},
        )
        assert response.status == 200, await response.text()
        body = await response.json()
    assert body["agent"] == "review-flex"
    assert body["agent_kind"] == "template"
    assert body["workspace"] == "kept-ws"
    assert slot.agent == "review-flex"
    assert slot.workspace == "kept-ws"
    assert slot.project == "/kept"
    assert slot.memory_store == "kept-store"
    assert bindings.requested_resolved is True
    recorded.assert_awaited()


# ── review client effort ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pi_startup_effort_writes_thought_level(tmp_path: Path):
    client = AcpClient(
        work_dir=tmp_path,
        acp_backend=ACP_BACKEND_PI,
        model="factory/claude-opus-5-5",
        reasoning_effort="high",
    )
    seen: dict[str, str] = {}

    async def _set(config_id: str, value: str) -> None:
        seen["id"] = config_id
        seen["value"] = value

    client.set_config_option = _set  # type: ignore[method-assign]
    await client._apply_startup_effort()
    assert seen == {"id": "thought_level", "value": "high"}


@pytest.mark.asyncio
async def test_pi_startup_effort_folds_max_to_xhigh(tmp_path: Path):
    client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_PI, reasoning_effort="max")
    seen: dict[str, str] = {}

    async def _set(config_id: str, value: str) -> None:
        seen["id"] = config_id
        seen["value"] = value

    client.set_config_option = _set  # type: ignore[method-assign]
    await client._apply_startup_effort()
    assert seen == {"id": "thought_level", "value": "xhigh"}
