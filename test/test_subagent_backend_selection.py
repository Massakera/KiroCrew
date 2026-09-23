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

pytestmark = pytest.mark.usefixtures("healthy_host_memory")


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

    def test_a_default_routed_run_freezes_the_backend_the_provider_started(
        self, tmp_path, monkeypatch
    ):
        from kiro_crew import subagent_persistence as sp
        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.providers.base import EVENT_COMPLETE, LLMEvent
        from kiro_crew.subagent import SubagentInfo, SubagentManager

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
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
        client.backend = "codex"

        async def fake_get_or_create(key, agent=None, approval_policy="", **kwargs):
            captured.update(kwargs)
            return client, True, False

        async def fake_stream(msg):
            yield LLMEvent(kind=EVENT_COMPLETE)

        sessions.get_or_create = fake_get_or_create
        client.stream = fake_stream
        runner = SubagentManager(sessions=sessions, ctx_builder=ctx_builder)
        shared = AsyncMock(return_value=client)
        info = SubagentInfo(
            execution_context=execution_for_store(""),
            id="sub-default",
            task="test",
            parent_session_key="parent-key",
            acp_backend="",
        )
        runner._log_spawned(info)
        cfg = KiroCrewConfig()
        with (
            patch.object(runner, "_create_shared_session", shared),
            patch.object(runner, "_should_use_session_sharing", return_value=True),
            patch("kiro_crew.config.loader.KiroCrewConfig.load", classmethod(lambda c: cfg)),
        ):
            asyncio.run(runner._run_inner(info, "subagent:sub-default"))
        # The first start still took the default path (shared runtime, no override).
        shared.assert_awaited()
        assert "acp_backend_override" not in captured
        # What that provider actually served is now frozen for retry and resume.
        assert info.acp_backend == "codex"
        assert sp.read_state("sub-default")["acp_backend"] == "codex"


# ── persistence, meta ──


class TestEffectiveBackend:
    def test_the_wire_name_comes_from_the_live_provider(self):
        assert sb.provider_wire_backend(SimpleNamespace(backend="codex")) == "codex"
        assert sb.provider_wire_backend(SimpleNamespace(backend="")) == "kiro"
        assert (
            sb.provider_wire_backend(SimpleNamespace(client=SimpleNamespace(backend="droid")))
            == "droid"
        )
        assert sb.provider_wire_backend(SimpleNamespace(backend="not-a-harness")) == ""
        assert sb.provider_wire_backend(MagicMock()) == ""

    def test_an_empty_live_pin_resumes_the_recorded_backend(self):
        assert sb.resume_backend_name("", "codex") == "codex"
        assert sb.resume_backend_name("kiro", "codex") == "kiro"
        assert sb.resume_backend_name("", "") == ""


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


async def _all_done(runs, manager=None) -> None:
    # A finished run leaves ``manager._tasks``, so wait on the run itself -- and on
    # its LIVE record when a manager is given: a queued spawn returns a placeholder
    # and the drain registers a fresh record under the same id.
    def live(run):
        return manager._agents.get(run.id, run) if manager is not None else run

    while not all(live(r).done for r in runs):
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
                # One after another, as the other tests on this fixture do: the
                # manager sizes its cap from host memory, so a parallel wave can queue
                # behind a busy CI host and this test is about backends, not capacity.
                runs = []
                for backend in ("codex", "kiro", ""):
                    run = manager.spawn(
                        f"task on {backend or 'default'}",
                        agent="worker",
                        cwd=world.project,
                        keep=True,
                        acp_backend=backend,
                    )
                    assert run is not None and not run.error, run and run.error
                    await asyncio.wait_for(_all_done([run]), timeout=20)
                    assert not run.error, run.error
                    runs.append(run)
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


# ── rate limits: classification, cooldown, fallback pick ──


class TestRateLimitPolicy:
    @pytest.fixture(autouse=True)
    def _clean(self):
        sb.clear_cooldowns()
        sb.reset_limits_cache()
        yield
        sb.clear_cooldowns()
        sb.reset_limits_cache()

    @pytest.mark.parametrize(
        "text",
        [
            "You've hit your usage limit. Upgrade to Pro or try again in 3 hours.",
            "Internal error: Agent error: 429 status code (no body)",
            "rate_limit_error: This request would exceed your rate limit",
            "Overloaded",
            "agent_turn_completed reason=model_rate_limited",
        ],
    )
    def test_provider_limits_are_recognised(self, text):
        assert sb.is_rate_limited_error(text)

    @pytest.mark.parametrize(
        "text", ["401 status code (no body)", "Authentication required", "tool failed", ""]
    )
    def test_other_failures_are_not(self, text):
        assert not sb.is_rate_limited_error(text)

    def test_a_spent_window_cools_down_longer_than_a_burst(self):
        assert sb.cool_down("codex", "usage limit reached", now=0.0) == sb.EXHAUSTED_COOLDOWN_SECS
        assert sb.cool_down("droid", "429", now=0.0) == sb.RATE_LIMITED_COOLDOWN_SECS
        assert sb.in_cooldown("droid", now=sb.RATE_LIMITED_COOLDOWN_SECS - 1)
        assert not sb.in_cooldown("droid", now=sb.RATE_LIMITED_COOLDOWN_SECS + 1)

    def test_the_pick_walks_the_chain_and_skips_what_cannot_take_the_task(self, monkeypatch):
        unavailable = {"claude"}
        monkeypatch.setattr(sb, "check_spawn_backend", lambda n: "x" if n in unavailable else None)
        monkeypatch.setattr(sb, "check_backend_installed", lambda n: None)
        chain = ["codex", "claude", "droid", "kiro"]
        assert sb.pick_fallback("codex", chain) == "droid"
        sb.cool_down("droid", "429")
        assert sb.pick_fallback("codex", chain) == "kiro"
        assert sb.pick_fallback("kiro", chain) == "codex"
        assert sb.pick_fallback("gone", ["gone"]) is None

    def test_the_cap_counts_running_runs_on_the_same_effective_backend(self, monkeypatch):
        monkeypatch.setattr(sb, "_limits_and_default", lambda: ({"codex": 2, "kiro": 1}, "kiro"))

        def run(backend, **kw):
            return SimpleNamespace(**{"done": False, "queued": False, "acp_backend": backend, **kw})

        agents = [run("codex"), run("codex", done=True), run(""), run("claude")]
        assert not sb.backend_at_cap(agents, "codex")
        assert sb.backend_at_cap(agents + [run("codex")], "codex")
        assert sb.backend_at_cap(agents, "")  # the default, kiro, already has one
        assert not sb.backend_at_cap(agents, "claude")  # no entry: global cap only

    def test_no_limits_means_no_cap(self, monkeypatch):
        monkeypatch.setattr(sb, "_limits_and_default", lambda: ({}, "kiro"))
        assert not sb.backend_at_cap([SimpleNamespace(done=False, acp_backend="codex")], "codex")


def _failed(**kw):
    base = dict(
        id="f1",
        task="t",
        _raw_task="raw t",
        error="You've hit your usage limit",
        user_stopped=False,
        reaped=False,
        conversation_key="",
        keep=False,
        batch_id="",
        tool_count=0,
        acp_backend="codex",
        parent_session_key="dashboard:1",
        agent="worker",
        max_turns=0,
        cwd="/w",
        model="",
        reasoning_effort="",
        approval_mode="",
        silent=False,
        delegation={},
        include_memory=True,
        include_lessons=False,
        include_project=True,
        memory_store="",
        crew="",
        app="",
        memory_mode="persistent",
        execution_context=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


class TestFailOver:
    @pytest.fixture(autouse=True)
    def _chain(self, monkeypatch):
        from kiro_crew.config.loader import AgentConfig, KiroCrewConfig

        sb.clear_cooldowns()
        cfg = KiroCrewConfig(agent=AgentConfig(subagent_backend_fallback=["codex", "kiro"]))
        monkeypatch.setattr(
            "kiro_crew.config.loader.KiroCrewConfig.load", classmethod(lambda c: cfg)
        )
        monkeypatch.setattr(sb, "check_backend_installed", lambda n: None)
        yield cfg
        sb.clear_cooldowns()

    def _manager(self):
        manager = MagicMock()
        manager.spawn_async = AsyncMock(return_value=SimpleNamespace(id="r9", error=""))
        return manager

    def test_a_rate_limited_run_is_re_dispatched_on_the_next_backend(self):
        manager, info = self._manager(), _failed()
        assert asyncio.run(sb.fail_over(manager, info)) == "r9"
        args, kwargs = manager.spawn_async.call_args
        assert args == ("raw t",)
        assert kwargs["acp_backend"] == "kiro"
        assert kwargs["include_lessons"] is False
        assert "batch_id" not in kwargs and "keep" not in kwargs
        assert "subagent `r9`" in info.error and "backend 'kiro'" in info.error
        assert sb.in_cooldown("codex")
        manager.spawn.assert_not_called()

    def test_it_is_decided_once(self):
        manager, info = self._manager(), _failed()
        asyncio.run(sb.fail_over(manager, info))
        assert asyncio.run(sb.fail_over(manager, info)) is None
        assert manager.spawn_async.call_count == 1

    @pytest.mark.parametrize(
        "field,value",
        [
            ("tool_count", 1),
            ("batch_id", "wave1"),
            ("conversation_key", "subagent:c1"),
            ("keep", True),
            ("user_stopped", True),
            ("error", "401 status code"),
            ("error", ""),
        ],
    )
    def test_ineligible_runs_report_as_they_are(self, field, value):
        manager, info = self._manager(), _failed(**{field: value})
        before = info.error
        assert asyncio.run(sb.fail_over(manager, info)) is None
        manager.spawn_async.assert_not_called()
        assert info.error == before

    def test_no_chain_no_failover(self, _chain):
        _chain.agent.subagent_backend_fallback = []
        manager = self._manager()
        assert asyncio.run(sb.fail_over(manager, _failed())) is None
        manager.spawn_async.assert_not_called()

    def test_every_fallback_cooling_down_reports_the_failure(self):
        sb.cool_down("kiro", "usage limit")
        manager, info = self._manager(), _failed()
        assert asyncio.run(sb.fail_over(manager, info)) is None
        assert "failed over" not in info.error

    def test_a_refused_replacement_leaves_the_error_alone(self):
        manager, info = self._manager(), _failed()
        manager.spawn_async = AsyncMock(return_value=SimpleNamespace(id="r9", error="capacity"))
        assert asyncio.run(sb.fail_over(manager, info)) is None
        assert "failed over" not in info.error

    def test_a_frozen_backend_cools_down_even_after_the_default_changes(self, _chain):
        _chain.agent.acp_backend = ""
        manager, info = self._manager(), _failed(acp_backend="codex")
        assert asyncio.run(sb.fail_over(manager, info)) == "r9"
        assert sb.in_cooldown("codex")
        assert not sb.in_cooldown("kiro")


# ── a real manager: failover and the per-backend cap ──


class TestFailOverOnARealManager:
    @pytest.mark.asyncio
    async def test_a_usage_limited_codex_run_is_finished_by_kiro(
        self, continuation_runtime, monkeypatch  # noqa: F811 - fixture
    ):
        from kiro_crew.config.loader import KiroCrewConfig

        world = continuation_runtime
        cfg = KiroCrewConfig.load()
        cfg.agent.subagent_backend_fallback = ["codex", "kiro"]
        cfg.save()
        sb.clear_cooldowns()
        monkeypatch.setattr(sb, "check_backend_installed", lambda n: None)
        sessions, manager = world.new_manager()
        seen = _record_backends(sessions)
        factory = sessions._provider_factory

        def limited(key, *args, **kwargs):
            provider = factory(key, *args, **kwargs)
            if kwargs.get("acp_backend_override") == "codex":

                async def refuse(message):
                    raise RuntimeError("You've hit your usage limit. Try again in 3 hours.")
                    yield  # pragma: no cover - makes this an async generator

                provider.stream = refuse
            return provider

        sessions._provider_factory = limited
        try:
            with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
                run = manager.spawn(
                    "write the report", agent="worker", cwd=world.project, acp_backend="codex"
                )
                assert run is not None and not run.error, run and run.error
                await asyncio.wait_for(_all_done([run]), timeout=20)
                assert "usage limit" in run.error, run.error
                assert "failed over" in run.error, run.error
                replacement_id = run.error.split("subagent `")[1].split("`")[0]
                replacement = manager._agents[replacement_id]
                await asyncio.wait_for(_all_done([replacement]), timeout=20)
                assert not replacement.error, replacement.error
                assert replacement.acp_backend == "kiro"
                assert seen[f"subagent:{replacement_id}"] == ""
                assert sb.in_cooldown("codex")
        finally:
            sb.clear_cooldowns()
            await asyncio.wait_for(sessions.close_all(drain_timeout=0), timeout=10)


class TestPerBackendCapOnARealManager:
    @pytest.mark.asyncio
    async def test_a_second_codex_run_waits_for_the_first(
        self, continuation_runtime  # noqa: F811 - fixture
    ):
        from kiro_crew.config.loader import KiroCrewConfig

        world = continuation_runtime
        cfg = KiroCrewConfig.load()
        cfg.agent.subagent_backend_limits = {"codex": 1}
        cfg.save()
        sb.reset_limits_cache()
        sessions, manager = world.new_manager()
        seen = _record_backends(sessions)
        factory = sessions._provider_factory
        release = asyncio.Event()

        def gated(key, *args, **kwargs):
            provider = factory(key, *args, **kwargs)
            original = provider.stream

            async def held(message):
                await release.wait()
                async for event in original(message):
                    yield event

            provider.stream = held
            return provider

        sessions._provider_factory = gated
        try:
            with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
                first = manager.spawn("one", agent="worker", cwd=world.project, acp_backend="codex")
                assert first is not None and not first.error
                for _ in range(200):
                    if f"subagent:{first.id}" in seen:
                        break
                    await asyncio.sleep(0.01)
                second = manager.spawn(
                    "two", agent="worker", cwd=world.project, acp_backend="codex"
                )
                assert second is not None and not second.error
                await asyncio.sleep(0.3)
                assert f"subagent:{second.id}" not in seen, "the cap let a second codex run start"
                release.set()
                await asyncio.wait_for(_all_done([first, second], manager), timeout=20)
                second = manager._agents[second.id]
                assert not first.error and not second.error, (first.error, second.error)
                assert seen[f"subagent:{second.id}"] == "codex"
        finally:
            sb.reset_limits_cache()
            await asyncio.wait_for(sessions.close_all(drain_timeout=0), timeout=10)
