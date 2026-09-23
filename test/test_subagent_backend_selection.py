"""Per-spawn ACP backend selection for sub-agents.

An orchestrating session places each sub-agent on a named harness
(``spawn_run(backend=... | backends=[...])``). These tests pin the whole path
without any real harness: the wire vocabulary, the one selection gate, the
provider factory, the ``/api/spawn`` refusals, the MCP tool forwarding, the
dedicated-process rule, persistence, continuation, and a mixed-backend wave on a
real ``SubagentManager`` whose only double is the native provider.
"""

from __future__ import annotations

import asyncio
import json
import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from test_subagent_continuable import continuation_runtime  # noqa: F401 - fixture

from kiro_crew import subagent_backend as sb
from kiro_crew.agent_sdk.backend_install import INSTALLED, MISSING, UNKNOWN, BackendInstallState
from kiro_crew.execution_context import execution_for_store


def _probe(state: str, command: str = "npm i -g x"):
    def probe(backend: str) -> BackendInstallState:
        missing = ("codex-acp",) if state == MISSING else ()
        return BackendInstallState(backend, backend, state, missing, command)

    return probe


# ── vocabulary ──


class TestVocabulary:
    def test_wire_names_round_trip(self):
        assert sb.backend_from_name("kiro") == ""
        assert sb.backend_from_name("codex") == "codex"
        assert sb.backend_from_name(" Codex ") == "codex"
        assert sb.backend_name("") == "kiro"
        assert sb.backend_name("codex") == "codex"

    def test_unknown_name_has_no_backend(self):
        assert sb.backend_from_name("nope") is None
        assert sb.backend_from_name("") is None

    def test_selectable_names_include_the_baseline(self):
        names = sb.selectable_backend_names()
        assert {"kiro", "codex", "claude"} <= set(names)

    def test_selectable_backend_passes(self):
        assert sb.check_spawn_backend("codex") is None
        assert sb.check_spawn_backend("kiro") is None

    @pytest.mark.parametrize("name", ["nope", "deepseek"])
    def test_unknown_or_dormant_backend_is_refused_with_the_roster(self, name):
        refusal = sb.check_spawn_backend(name)
        assert refusal is not None
        assert refusal.code == sb.UNKNOWN_BACKEND_CODE
        assert "codex" in refusal.error

    def test_missing_harness_is_refused_with_install_command(self, monkeypatch):
        monkeypatch.setattr(
            "kiro_crew.agent_sdk.backend_install.probe_backend", _probe(MISSING, "npm i -g codex")
        )
        refusal = sb.check_backend_installed("codex")
        assert refusal is not None
        assert refusal.code == sb.BACKEND_UNAVAILABLE_CODE
        assert "npm i -g codex" in refusal.error
        assert "codex-acp" in refusal.error

    @pytest.mark.parametrize("state", [INSTALLED, UNKNOWN])
    def test_installed_or_undecided_harness_is_not_refused(self, monkeypatch, state):
        monkeypatch.setattr("kiro_crew.agent_sdk.backend_install.probe_backend", _probe(state))
        assert sb.check_backend_installed("codex") is None

    def test_parent_backend_name_reads_the_live_provider(self):
        provider = SimpleNamespace(_client=SimpleNamespace(backend="codex"))
        state = SimpleNamespace(sessions=SimpleNamespace(get_provider=lambda key: provider))
        assert sb.parent_backend_name(state, "dashboard:1") == "codex"
        kiro = SimpleNamespace(_client=SimpleNamespace(backend=""))
        state.sessions.get_provider = lambda key: kiro
        assert sb.parent_backend_name(state, "dashboard:1") == "kiro"
        state.sessions.get_provider = lambda key: None
        assert sb.parent_backend_name(state, "dashboard:1") == ""
        assert sb.parent_backend_name(state, "") == ""


# ── the selection gate and the factory ──


class TestSelectionGate:
    def test_override_beats_member_route_and_default(self):
        from kiro_crew.members import select_provider_backend

        assert select_provider_backend("subagent:a1", "claude", "", "codex") == "codex"
        assert select_provider_backend("subagent:a1", "claude", "codex", "") == ""

    def test_no_override_keeps_the_existing_precedence(self):
        from kiro_crew.members import select_provider_backend

        assert select_provider_backend("subagent:a1", "claude", "codex") == "codex"
        assert select_provider_backend("subagent:a1", "claude", "codex", None) == "codex"

    def test_unselectable_override_degrades_through_the_gate_and_says_so(self, caplog):
        from kiro_crew.members import select_provider_backend

        with caplog.at_level(logging.WARNING, logger="kiro_crew.members"):
            assert select_provider_backend("subagent:a1", "", "codex", "deepseek") == ""
        assert any("no longer selectable" in r.getMessage() for r in caplog.records)

    def test_factory_builds_the_provider_on_the_override(self, tmp_path):
        from kiro_crew.config.loader import KiroCrewConfig

        cfg = KiroCrewConfig()
        with (
            patch.object(KiroCrewConfig, "_resolve_agent_model", staticmethod(lambda: "")),
            patch("kiro_crew.providers.acp.AcpProvider") as provider_cls,
        ):
            factory = cfg.create_provider_factory()
            factory("subagent:a1", cwd=str(tmp_path), acp_backend_override="codex")
            factory("subagent:a2", cwd=str(tmp_path))
        backends = [c.kwargs["acp_backend"] for c in provider_cls.call_args_list]
        assert backends == ["codex", cfg.agent.acp_backend]


# ── /api/spawn ──


class _Req:
    def __init__(self, state: Any, body: Any) -> None:
        self.app: dict[str, Any] = {"state": state}
        self._body = body
        self.match_info: dict[str, str] = {}
        self.query: dict[str, str] = {}
        self.headers: dict[str, str] = {}
        self.remote = "127.0.0.1"
        self._extra = {"app": ""}

    def __contains__(self, key: str) -> bool:
        return key in self._extra

    async def json(self) -> Any:
        return self._body

    def get(self, key: str, default: Any = None) -> Any:
        return self._extra.get(key, default)


def _spawn_state() -> tuple[Any, MagicMock]:
    mgr = MagicMock()
    mgr.max_concurrent = 4
    mgr.spawn.return_value = SimpleNamespace(id="a9", done=False, error="", error_code="")
    state = MagicMock()
    state.subagents = mgr
    state.slack_client = None
    return state, mgr


def _api_spawn(body: dict) -> tuple[int, dict, MagicMock]:
    from kiro_crew.dashboard.handlers import messaging

    state, mgr = _spawn_state()
    resp = asyncio.run(messaging.api_spawn(_Req(state, body)))
    return resp.status, json.loads(resp.body), mgr


class TestApiSpawn:
    def test_unknown_backend_is_refused_before_spawning(self):
        status, body, mgr = _api_spawn({"task": "x", "backend": "nope"})
        assert status == 400
        assert body["code"] == sb.UNKNOWN_BACKEND_CODE
        mgr.spawn.assert_not_called()

    def test_malformed_backend_fails_the_schema(self):
        status, _body, mgr = _api_spawn({"task": "x", "backend": "Not A Name"})
        assert status == 400
        mgr.spawn.assert_not_called()

    def test_uninstalled_backend_is_refused_with_the_install_hint(self, monkeypatch):
        monkeypatch.setattr(
            "kiro_crew.agent_sdk.backend_install.probe_backend", _probe(MISSING, "npm i -g codex")
        )
        status, body, mgr = _api_spawn({"task": "x", "backend": "codex"})
        assert status == 400
        assert body["code"] == sb.BACKEND_UNAVAILABLE_CODE
        assert "npm i -g codex" in body["error"]
        mgr.spawn.assert_not_called()

    def test_accepted_backend_reaches_the_manager_and_the_receipt(self, monkeypatch):
        monkeypatch.setattr("kiro_crew.agent_sdk.backend_install.probe_backend", _probe(INSTALLED))
        status, body, mgr = _api_spawn({"task": "x", "backend": "codex"})
        assert status == 200, body
        assert mgr.spawn.call_args.kwargs["acp_backend"] == "codex"
        assert body["backend"] == "codex"

    def test_absent_backend_changes_nothing(self):
        status, body, mgr = _api_spawn({"task": "x"})
        assert status == 200, body
        assert mgr.spawn.call_args.kwargs["acp_backend"] == ""
        assert "backend" not in body


class TestSoloGate:
    def _state(self, parent_backend: str | None):
        provider = (
            None
            if parent_backend is None
            else SimpleNamespace(_client=SimpleNamespace(backend=parent_backend))
        )
        sessions = SimpleNamespace(
            get_provider=lambda key: provider,
            get_agent_selection=lambda key: ("template", "kirocrew"),
            get_agent=lambda key: "kirocrew",
        )
        return SimpleNamespace(sessions=sessions, _slots={})

    def test_another_backend_is_a_difference(self):
        from kiro_crew.solo_spawn import solo_spawn_difference

        assert solo_spawn_difference(self._state(""), "p", backend="codex") == "backend"

    def test_the_parents_own_backend_is_not(self):
        from kiro_crew.solo_spawn import solo_spawn_difference

        assert solo_spawn_difference(self._state("codex"), "p", backend="codex") == ""

    def test_unknown_parent_fails_open_and_says_so(self):
        from kiro_crew.solo_spawn import solo_spawn_difference

        assert (
            solo_spawn_difference(self._state(None), "p", backend="codex")
            == "backend (parent unknown)"
        )

    def test_tool_side_lets_a_named_backend_through(self):
        from kiro_crew.solo_spawn import solo_spawn_refusal

        assert solo_spawn_refusal(1, "", backend="codex") is None
        assert solo_spawn_refusal(1, "") is not None


# ── MCP tools ──


def _run_tool(tool: str, args: dict, replies: dict[str, dict] | None = None):
    from kiro_crew import mcp_core

    bodies: list[dict] = []

    def _fake_post(path: str, body: dict) -> dict:
        if path == "/api/spawn":
            bodies.append(body)
            reply = (replies or {}).get(body.get("backend", ""))
            if reply is not None:
                return reply
            return {"id": f"a{len(bodies)}"}
        return {}

    def _fake_get(path: str) -> dict:
        return {"done": True, "result": "ok", "agent": ""}

    with (
        patch.object(mcp_core, "_post", side_effect=_fake_post),
        patch.object(mcp_core, "_get", side_effect=_fake_get),
        patch.object(mcp_core, "_resolve_session_key", return_value="dashboard:chat-1"),
        patch.object(mcp_core, "sel", MagicMock()),
    ):
        result = mcp_core._call_tool_inner(tool, args)
    return bodies, result


class TestSpawnRunTool:
    def test_backends_are_forwarded_per_task(self):
        bodies, text = _run_tool(
            "spawn_run", {"tasks": ["a", "b", "c"], "backends": ["codex", "kiro", "codex"]}
        )
        assert [b.get("backend") for b in bodies] == ["codex", "kiro", "codex"]
        assert "backend=codex" in text and "backend=kiro" in text

    def test_one_backend_applies_to_every_task(self):
        bodies, _ = _run_tool("spawn_run", {"tasks": ["a", "b"], "backend": "claude"})
        assert [b.get("backend") for b in bodies] == ["claude", "claude"]

    def test_no_backend_sends_no_field(self):
        bodies, _ = _run_tool("spawn_run", {"tasks": ["a", "b"]})
        assert all("backend" not in b for b in bodies)

    def test_length_mismatch_is_refused_before_any_post(self):
        bodies, text = _run_tool("spawn_run", {"tasks": ["a", "b"], "backends": ["codex"]})
        assert bodies == []
        assert text.startswith("Error: backends length")

    def test_a_refused_backend_is_not_re_posted_for_the_rest_of_the_wave(self):
        refused = {"error": "Unknown backend", "code": sb.UNKNOWN_BACKEND_CODE}
        bodies, text = _run_tool(
            "spawn_run",
            {"tasks": ["a", "b", "c"], "backends": ["gone", "kiro", "gone"]},
            replies={"gone": refused},
        )
        assert [b.get("backend") for b in bodies] == ["gone", "kiro"]
        assert "backend 'gone' refused above" in text

    def test_a_solo_task_on_another_backend_passes_the_tool_gate(self):
        bodies, _ = _run_tool("spawn_run", {"task": "second opinion", "backend": "codex"})
        assert len(bodies) == 1 and bodies[0]["backend"] == "codex"
        assert bodies[0]["solo"] is True

    def test_schema_advertises_the_selectable_backends(self):
        from kiro_crew.mcp_tools.spawn import schemas

        spawn_run = next(s for s in schemas() if s["name"] == "spawn_run")
        props = spawn_run["inputSchema"]["properties"]
        assert "backend" in props and "backends" in props
        assert "codex" in props["backend"]["description"]


class TestSpawnSubAgentsTool:
    def test_backend_is_forwarded_per_entry(self):
        bodies, _ = _run_tool(
            "spawn_sub_agents",
            {"agents": [{"prompt": "a", "backend": "codex"}, {"prompt": "b"}]},
        )
        assert [b.get("backend") for b in bodies] == ["codex", None]

    def test_malformed_backend_is_refused(self):
        bodies, text = _run_tool(
            "spawn_sub_agents", {"agents": [{"prompt": "a", "backend": "Bad Name"}]}
        )
        assert bodies == []
        assert text.startswith("Error: invalid backend")


# ── run path ──


class TestRunPath:
    def _run(self, acp_backend: str):
        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.providers.base import EVENT_COMPLETE, LLMEvent
        from kiro_crew.subagent import SubagentInfo, SubagentManager

        sessions = MagicMock()
        sessions.get_pid = MagicMock(return_value=None)
        sessions.get_approval_policy = MagicMock(return_value="")
        sessions.get_agent = MagicMock(return_value="")
        sessions.get_agent_selection = MagicMock(return_value=("template", ""))
        ctx_builder = MagicMock()
        ctx_builder.build_message = MagicMock(return_value=("msg", None))
        ctx_builder.hooks.auto_approve_subagent_tools = False
        captured: dict = {}
        client = MagicMock()

        async def fake_get_or_create(key, agent=None, approval_policy="", **kwargs):
            captured.update(kwargs)
            return client, True, False

        async def fake_stream(msg):
            yield LLMEvent(kind=EVENT_COMPLETE)

        sessions.get_or_create = fake_get_or_create
        client.stream = fake_stream
        runner = SubagentManager(sessions=sessions, ctx_builder=ctx_builder)
        shared = AsyncMock(side_effect=AssertionError("shared runtime used across harnesses"))
        info = SubagentInfo(
            execution_context=execution_for_store(""),
            id="sub1",
            task="test",
            parent_session_key="parent-key",
            acp_backend=acp_backend,
        )
        runner._log_spawned(info)
        cfg = KiroCrewConfig()
        with (
            patch.object(runner, "_create_shared_session", shared),
            patch.object(runner, "_should_use_session_sharing", return_value=True),
            patch("kiro_crew.config.loader.KiroCrewConfig.load", classmethod(lambda c: cfg)),
        ):
            asyncio.run(runner._run_inner(info, "subagent:sub1"))
        return captured, shared, info

    def test_a_named_backend_forces_a_dedicated_process_on_that_harness(self):
        captured, shared, _ = self._run("codex")
        shared.assert_not_called()
        assert captured["acp_backend_override"] == "codex"

    def test_kiro_is_a_real_override_spelled_as_the_empty_id(self):
        captured, shared, _ = self._run("kiro")
        shared.assert_not_called()
        assert captured["acp_backend_override"] == ""

    def test_no_backend_leaves_the_session_on_the_default_path(self):
        captured, shared, _ = self._run("")
        shared.assert_awaited()
        assert "acp_backend_override" not in captured


# ── persistence, meta ──


class TestPersistence:
    def test_state_json_records_the_backend_only_when_set(self, tmp_path, monkeypatch):
        from kiro_crew import subagent_persistence as sp

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        sp.create_agent_folder("r1", task="t", acp_backend="codex")
        sp.create_agent_folder("r2", task="t")
        assert sp.read_state("r1")["acp_backend"] == "codex"
        assert "acp_backend" not in sp.read_state("r2")

    def test_recreating_a_folder_keeps_the_recorded_backend(self, tmp_path, monkeypatch):
        from kiro_crew import subagent_persistence as sp

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        sp.create_agent_folder("r1", task="t", acp_backend="codex")
        sp.create_agent_folder("r1", task="t")
        assert sp.read_state("r1")["acp_backend"] == "codex"

    def test_completion_meta_names_the_backend_only_when_set(self):
        from kiro_crew.subagent_completion_meta import single_completion_meta

        assert single_completion_meta(agent_id="a", outcome="ok", backend="codex")["backend"] == (
            "codex"
        )
        assert "backend" not in single_completion_meta(agent_id="a", outcome="ok")


# ── a real manager: a mixed wave, then a continuation ──


async def _all_done(runs) -> None:
    # A finished run leaves ``manager._tasks``, so wait on the run itself.
    while not all(r.done for r in runs):
        await asyncio.sleep(0.01)


def _record_backends(sessions) -> dict[str, Any]:
    seen: dict[str, Any] = {}
    factory = sessions._provider_factory

    def recording(key, *args, **kwargs):
        seen[key] = kwargs.get("acp_backend_override", "<none>")
        return factory(key, *args, **kwargs)

    sessions._provider_factory = recording
    return seen


class TestMixedBackendWave:
    @pytest.mark.asyncio
    async def test_each_child_runs_on_its_own_backend_and_continues_there(
        self, continuation_runtime  # noqa: F811 - fixture
    ):
        from kiro_crew import subagent_persistence as sp

        world = continuation_runtime
        sessions, manager = world.new_manager()
        seen = _record_backends(sessions)
        try:
            with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
                runs = [
                    manager.spawn(
                        f"task on {backend or 'default'}",
                        agent="worker",
                        cwd=world.project,
                        keep=True,
                        acp_backend=backend,
                    )
                    for backend in ("codex", "kiro", "")
                ]
                for run in runs:
                    assert run is not None and not run.error, run and run.error
                await asyncio.wait_for(_all_done(runs), timeout=20)
                for run in runs:
                    assert not run.error, run.error
                assert [seen[f"subagent:{r.id}"] for r in runs] == ["codex", "", "<none>"]
                states = [await asyncio.to_thread(sp.read_state, r.id) for r in runs]
                assert [s.get("acp_backend", "") for s in states] == ["codex", "kiro", ""]

                seen.clear()
                followup = manager.continue_conversation(
                    runs[0].id, "keep going", cwd=world.project
                )
                assert followup is not None and not followup.error, followup and followup.error
                await asyncio.wait_for(_all_done([followup]), timeout=10)
                assert not followup.error, followup.error
                assert followup.acp_backend == "codex"
                assert seen[f"subagent:{runs[0].id}"] == "codex"
        finally:
            await asyncio.wait_for(sessions.close_all(drain_timeout=0), timeout=10)
