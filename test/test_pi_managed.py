"""Opt-in Crew-owned pi resources and read-only profile, with no model calls."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import fields
from pathlib import Path

import pytest

from kiro_crew.acp import client as acp_client
from kiro_crew.acp.client import (
    AcpClient,
    AcpError,
    pi_gate_extension_path,
    pi_managed_extension_path,
)
from kiro_crew.acp.types import EVENT_PERMISSION_REQUEST, AcpEvent
from kiro_crew.agent_sdk.backends import ACP_BACKEND_KIRO, ACP_BACKEND_PI
from kiro_crew.agent_sdk.pi_profile import (
    managed_pi_grants,
    managed_pi_tools,
    managed_profile_issue,
)
from kiro_crew.config.loader import KiroCrewConfig, _build_agent_config
from kiro_crew.config.sections import AgentConfig
from kiro_crew.hooks import (
    TOOL_ALLOW,
    TOOL_AUTO_APPROVE,
    TOOL_DENY,
    ToolHookResult,
    managed_pi_allowed_grant,
)
from kiro_crew.providers.acp import AcpProvider
from kiro_crew.subprocess_utf8 import UTF8_TEXT

BRIDGE = {"kirocrew-core": ("spawn_run", "spawn_list", "spawn_status")}
READS = ("find", "grep", "ls", "read")


class TestManagedProjection:
    def test_reader_gets_no_shell_mutation_or_delegation(self):
        assert managed_pi_tools({"tools": ["fs_read"]}, BRIDGE) == READS

    def test_empty_and_unknown_tools_do_not_mean_all(self):
        assert managed_pi_tools({"tools": []}, BRIDGE) == ()
        assert managed_pi_tools({"tools": ["subagent", "custom_unknown"]}, BRIDGE) == ()

    @pytest.mark.parametrize("spec", [None, {}, {"tools": None}, {"tools": "*"}, {"tools": [4]}])
    def test_malformed_spec_refuses(self, spec):
        with pytest.raises(ValueError):
            managed_pi_tools(spec, BRIDGE)

    def test_exact_mcp_grant_never_mounts_sibling_tools(self):
        assert managed_pi_tools({"tools": ["@kirocrew-core/spawn_list"]}, BRIDGE) == (
            "mcp__kirocrew-core__spawn_list",
        )

    def test_disabled_tools_narrow_even_a_wildcard(self):
        spec = {
            "tools": ["*"],
            "mcpServers": {"kirocrew-core": {"disabledTools": ["spawn_r*", "spawn_status"]}},
        }
        tools = managed_pi_tools(spec, BRIDGE)
        assert "bash" in tools and "write" in tools
        assert [t for t in tools if t.startswith("mcp__")] == ["mcp__kirocrew-core__spawn_list"]

    def test_disabled_server_is_not_readded(self):
        spec = {"tools": ["@kirocrew-core"], "mcpServers": {"kirocrew-core": {"disabled": True}}}
        assert managed_pi_tools(spec, BRIDGE) == ()

    @pytest.mark.parametrize("disabled", [None, "spawn_run", [1]])
    def test_malformed_deny_never_becomes_an_allow(self, disabled):
        spec = {"tools": ["*"], "mcpServers": {"kirocrew-core": {"disabledTools": disabled}}}
        with pytest.raises(ValueError):
            managed_pi_tools(spec, BRIDGE)

    def test_builtin_namespace_never_grants_mcp(self):
        tools = managed_pi_tools({"tools": ["@builtin"]}, BRIDGE)
        assert set(READS) <= set(tools)
        assert not any(t.startswith("mcp__") for t in tools)


class TestManagedConfiguration:
    def test_opt_in_and_restart_metadata(self):
        assert AgentConfig().pi_managed is False
        assert _build_agent_config({}).pi_managed is False
        assert _build_agent_config({"pi_managed": True}).pi_managed is True
        assert _build_agent_config({"pi_managed": "false"}).pi_managed is False
        entry = next(f for f in fields(AgentConfig) if f.name == "pi_managed")
        assert entry.metadata["restart"] is True

    @pytest.mark.parametrize("backend", [ACP_BACKEND_PI, ACP_BACKEND_KIRO])
    def test_provider_preserves_opt_in_only_for_pi(self, tmp_path, backend):
        provider = AcpProvider(work_dir=tmp_path, acp_backend=backend, pi_managed=True)
        assert provider._client._pi_managed is (backend == ACP_BACKEND_PI)

    def test_config_factory_carries_managed_mode(self, tmp_path):
        cfg = KiroCrewConfig()
        cfg.agent.acp_backend = ACP_BACKEND_PI
        cfg.agent.pi_managed = True
        provider = cfg.create_provider_factory()(cwd=str(tmp_path))
        assert provider._client._pi_managed is True

    def test_ambient_session_does_not_read_a_profile_or_inherit_one(self, tmp_path, monkeypatch):
        client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_PI)
        monkeypatch.setattr(acp_client, "_pi_installed_version", lambda _p: pytest.fail("read"))
        client._prepare_pi_managed_profile("unused")
        env = {acp_client._ENV_PI_MANAGED_TOOLS: '["write"]'}
        client._apply_pi_managed_env(env)
        assert acp_client._ENV_PI_MANAGED_TOOLS not in env

    @pytest.mark.parametrize("version", [None, ((0, 85, 1), "pi"), ((0, 87, 0), "pi")])
    def test_managed_requires_verified_supported_version(self, tmp_path, monkeypatch, version):
        client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_PI, pi_managed=True)
        monkeypatch.setattr(acp_client, "_pi_installed_version", lambda _p: version)
        with pytest.raises(AcpError, match="0.87.1"):
            client._prepare_pi_managed_profile("pi")

    def test_projection_and_respawn_take_this_sessions_snapshot(self, tmp_path, monkeypatch):
        client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_PI, pi_managed=True)
        monkeypatch.setattr(acp_client, "_pi_installed_version", lambda _p: ((0, 87, 1), "pi"))
        client._mcp_ref_spec = {"tools": ["fs_read"]}
        client._prepare_pi_managed_profile("pi")
        env = {acp_client._ENV_PI_MANAGED_TOOLS: '["write"]'}
        client._apply_pi_managed_env(env)
        assert json.loads(env[acp_client._ENV_PI_MANAGED_TOOLS]) == list(READS)
        client._mcp_ref_spec = None
        with pytest.raises(AcpError, match="explicit tools list"):
            client._prepare_pi_managed_profile("pi")
        with pytest.raises(AcpError, match="not been prepared"):
            client._apply_pi_managed_env(env)

    def test_bridge_is_narrowed_and_reader_starts_no_mcp_child(self, tmp_path, monkeypatch):
        client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_PI, pi_managed=True)
        monkeypatch.setattr(
            client,
            "_pooled_broker_stubs",
            lambda: [
                {
                    "name": "kirocrew-core",
                    "command": "python",
                    "args": [],
                    "env": [],
                }
            ],
        )
        monkeypatch.setattr(
            acp_client, "_seal_pi_bridge_extension", lambda: str(tmp_path / "bridge.ts")
        )
        client._pi_managed_tools = READS
        assert client._prepare_pi_tool_bridge() is None
        client._pi_managed_tools = ("mcp__kirocrew-core__spawn_list",)
        bridge = client._prepare_pi_tool_bridge()
        assert bridge is not None
        assert json.loads(bridge[1])["servers"][0]["tools"] == ["spawn_list"]


LEDGER_SERVERS = ("kirocrew-core", "kirocrew-work", "kirocrew-dashboard")
CONDUCTOR_SPEC = {"tools": ["fs_read", "@kirocrew-core", "@kirocrew-dashboard", "@kirocrew-work"]}
WORKER_SPEC = {"tools": ["fs_read", "fs_write", "execute_bash", "@kirocrew-core", "@kirocrew-work"]}


def _stubbed_client(tmp_path, monkeypatch, *, managed, stubs=LEDGER_SERVERS, spec=None):
    client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_PI, pi_managed=managed)
    monkeypatch.setattr(
        client,
        "_pooled_broker_stubs",
        lambda: [{"name": n, "command": "python", "args": [], "env": []} for n in stubs],
    )
    monkeypatch.setattr(
        acp_client, "_seal_pi_bridge_extension", lambda: str(tmp_path / "bridge.ts")
    )
    if managed:
        monkeypatch.setattr(acp_client, "_pi_installed_version", lambda _p: ((0, 87, 1), "pi"))
        client._mcp_ref_spec = spec
        client._prepare_pi_managed_profile("pi")
    return client


def _bridged(client) -> dict[str, list[str]]:
    prepared = client._prepare_pi_tool_bridge()
    assert prepared is not None
    return {s["name"]: s["tools"] for s in json.loads(prepared[1])["servers"]}


class TestManagedLedgerSurface:
    def test_ambient_session_keeps_the_spawn_only_bridge(self, tmp_path, monkeypatch):
        client = _stubbed_client(tmp_path, monkeypatch, managed=False)
        assert _bridged(client) == {
            "kirocrew-core": list(acp_client._PI_BRIDGE_TOOLS["kirocrew-core"])
        }

    def test_conductor_spec_gets_ledger_session_control_and_patrol(self, tmp_path, monkeypatch):
        client = _stubbed_client(tmp_path, monkeypatch, managed=True, spec=CONDUCTOR_SPEC)
        bridged = _bridged(client)
        assert {k: set(v) for k, v in bridged.items()} == {
            k: set(v) for k, v in acp_client._PI_MANAGED_BRIDGE_TOOLS.items()
        }
        prepared = client._prepare_pi_tool_bridge()
        assert prepared is not None and prepared[2].servers == frozenset(LEDGER_SERVERS)

    def test_worker_spec_never_gets_an_unmounted_servers_tools(self, tmp_path, monkeypatch):
        client = _stubbed_client(tmp_path, monkeypatch, managed=True, spec=WORKER_SPEC)
        bridged = _bridged(client)
        assert "kirocrew-dashboard" not in bridged
        assert set(bridged["kirocrew-work"]) >= {"work_brief", "work_report"}
        assert {"write", "edit", "bash"} <= set(client._pi_managed_tools or ())

    def test_an_exact_grant_narrows_a_ledger_server(self, tmp_path, monkeypatch):
        spec = {"tools": ["@kirocrew-work/work_brief", "@kirocrew-work/work_report"]}
        client = _stubbed_client(tmp_path, monkeypatch, managed=True, spec=spec)
        assert _bridged(client) == {"kirocrew-work": ["work_brief", "work_report"]}

    def test_a_mounted_but_unstubbed_server_is_named_once(self, tmp_path, monkeypatch, caplog):
        monkeypatch.setattr(acp_client, "_pi_bridge_off_noted", set())
        client = _stubbed_client(
            tmp_path, monkeypatch, managed=True, stubs=("kirocrew-core",), spec=CONDUCTOR_SPEC
        )
        with caplog.at_level("WARNING", logger=acp_client.logger.name):
            assert set(_bridged(client)) == {"kirocrew-core"}
            _bridged(client)
        notes = [r.getMessage() for r in caplog.records if "stub_servers" in r.getMessage()]
        assert len(notes) == 1
        assert "kirocrew-dashboard, kirocrew-work" in notes[0]

    def test_every_managed_bridge_tool_exists_on_its_server(self):
        from kiro_crew import mcp_core, mcp_dashboard, mcp_work

        served = {
            "kirocrew-core": mcp_core._list_tools(),
            "kirocrew-work": mcp_work._list_tools(),
            "kirocrew-dashboard": mcp_dashboard._list_tools(),
        }
        assert set(acp_client._PI_MANAGED_BRIDGE_TOOLS) == set(served)
        for server, tools in acp_client._PI_MANAGED_BRIDGE_TOOLS.items():
            assert set(tools) <= {t["name"] for t in served[server]}, server
        assert set(acp_client._PI_BRIDGE_TOOLS["kirocrew-core"]) <= set(
            acp_client._PI_MANAGED_BRIDGE_TOOLS["kirocrew-core"]
        )

    @pytest.mark.parametrize("windows", [False, True])
    def test_managed_launcher_keeps_gate_and_bridge_but_not_ambient_resources(
        self, monkeypatch, windows
    ):
        monkeypatch.setattr(acp_client.platform_compat, "IS_WINDOWS", windows)
        body = acp_client._pi_gate_launcher_body("pi", "gate.ts", ("bridge.ts",), managed=True)
        assert "--no-extensions" in body and "--no-skills" in body
        assert "--no-prompt-templates" in body
        assert "gate.ts" in body and "bridge.ts" in body
        assert "--no-context-files" not in body, "repository instructions must survive"
        plain = acp_client._pi_gate_launcher_body("pi", "gate.ts", ("bridge.ts",))
        assert "--no-extensions" not in plain

    def test_managed_extension_is_sealed_and_tampering_is_refused(self, tmp_path, monkeypatch):
        monkeypatch.setattr(acp_client, "_pi_gate_artifact_dir", lambda: str(tmp_path))
        sealed = Path(acp_client._seal_pi_managed_extension())
        assert sealed.read_bytes() == Path(pi_managed_extension_path()).read_bytes().replace(
            b"\r\n", b"\n"
        )
        bad = tmp_path / "bad.ts"
        bad.write_text("not the shipped profile", encoding="utf-8")
        monkeypatch.setattr(acp_client, "pi_managed_extension_path", lambda: str(bad))
        with pytest.raises(acp_client.PiGateExtensionTampered):
            acp_client._seal_pi_managed_extension()

    @pytest.mark.parametrize("source", [None, "foreign", "expected"])
    def test_readback_requires_both_the_gate_and_managed_profile(
        self, tmp_path, monkeypatch, source
    ):
        gate, profile = str(tmp_path / "gate.ts"), str(tmp_path / "profile.ts")
        commands = [{"name": "kiro-crew-gate", "sourceInfo": {"path": gate}}]
        if source:
            commands.append(
                {
                    "name": "kiro-crew-managed",
                    "sourceInfo": {
                        "path": profile if source == "expected" else str(tmp_path / "foreign.ts"),
                    },
                }
            )
        assert bool(managed_profile_issue(commands, profile)) is (source != "expected")
        completed = subprocess.CompletedProcess(
            [],
            0,
            stdout=json.dumps(
                {
                    "id": "kiro-crew-gate-readback",
                    "type": "response",
                    "command": "get_commands",
                    "success": True,
                    "data": {"commands": commands},
                }
            ),
            stderr="",
        )
        monkeypatch.setattr(acp_client.subprocess_mod, "run", lambda *_a, **_kw: completed)
        client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_PI, pi_managed=True)
        client._pi_managed_tools = READS
        client._pi_managed_extension = profile
        issue, _ = client._verify_pi_gate(["not-executed"], gate)
        assert bool(issue) is (source != "expected")

    def test_launcher_cache_separates_ambient_and_managed(self, tmp_path, monkeypatch):
        monkeypatch.setattr(acp_client, "_pi_gate_artifact_dir", lambda: str(tmp_path))
        monkeypatch.setattr(acp_client, "_pi_gate_launcher_cache", {})
        ambient = acp_client._ensure_pi_gate_launcher("pi", "gate.ts")
        managed = acp_client._ensure_pi_gate_launcher("pi", "gate.ts", managed=True)
        assert ambient != managed
        assert "--no-extensions" not in Path(ambient).read_text(encoding="utf-8")
        assert "--no-extensions" in Path(managed).read_text(encoding="utf-8")


MOUNTED = (
    "bash",
    "mcp__kirocrew-core__spawn_run",
    "mcp__kirocrew-work__work_brief",
    "mcp__kirocrew-work__work_report",
    "write",
)


def _bridged_event(server="kirocrew-work", tool="work_brief", *, verified=True):
    return AcpEvent(
        kind=EVENT_PERMISSION_REQUEST,
        title=f"mcp__{server}__{tool}",
        mcp_server_name=server,
        tool_name=tool,
        mcp_identity_trusted=True,
        bridge_verified=verified,
    )


class TestManagedAllowedTools:
    def test_only_mcp_refs_grant_and_never_beyond_what_is_mounted(self):
        spec = {"allowedTools": ["fs_write", "execute_bash", "*", "@kirocrew-work/work_brief"]}
        assert managed_pi_grants(spec, MOUNTED) == {"@kirocrew-work/work_brief"}
        spec = {"allowedTools": ["@kirocrew-dashboard", "@kirocrew-core/spawn_list"]}
        assert managed_pi_grants(spec, MOUNTED) == frozenset()

    @pytest.mark.parametrize("entry", ["@kirocrew-work", "@kirocrew-work/", "@kirocrew-work/*"])
    def test_whole_server_spellings_grant_the_mounted_tools(self, entry):
        assert managed_pi_grants({"allowedTools": [entry]}, MOUNTED) == {
            "@kirocrew-work/work_brief",
            "@kirocrew-work/work_report",
        }

    def test_a_glob_narrows(self):
        spec = {"allowedTools": ["@kirocrew-work/*_report"]}
        assert managed_pi_grants(spec, MOUNTED) == {"@kirocrew-work/work_report"}

    @pytest.mark.parametrize("spec", [None, {}, {"allowedTools": "@kirocrew-work"}, {"a": 1}])
    def test_malformed_or_absent_grants_nothing(self, spec):
        assert managed_pi_grants(spec, MOUNTED) == frozenset()

    def _client(self, tmp_path, monkeypatch, *, managed=True, denied=()):
        from kiro_crew.platform import governance

        monkeypatch.setattr(governance, "may_skip_gate_now", lambda ref: ref not in denied)
        spec = dict(WORKER_SPEC, allowedTools=["@kirocrew-work", "@kirocrew-core/spawn_run"])
        return _stubbed_client(tmp_path, monkeypatch, managed=managed, spec=spec)

    def test_a_verified_bridged_call_the_spec_allows_is_granted(self, tmp_path, monkeypatch):
        client = self._client(tmp_path, monkeypatch)
        assert client.managed_pi_grant(_bridged_event()) is True
        assert client.managed_pi_grant(_bridged_event("kirocrew-core", "spawn_run")) is True
        assert client.managed_pi_grant(_bridged_event("kirocrew-core", "spawn_list")) is False

    def test_an_identity_from_any_other_channel_is_not(self, tmp_path, monkeypatch):
        client = self._client(tmp_path, monkeypatch)
        assert client.managed_pi_grant(_bridged_event(verified=False)) is False

    def test_native_tools_still_ask(self, tmp_path, monkeypatch):
        client = self._client(tmp_path, monkeypatch)
        native = AcpEvent(kind=EVENT_PERMISSION_REQUEST, title="write", tool_name="write")
        assert client.managed_pi_grant(native) is False

    def test_the_ceiling_strips_a_grant(self, tmp_path, monkeypatch):
        client = self._client(tmp_path, monkeypatch, denied=("@kirocrew-work/work_brief",))
        assert client.managed_pi_grant(_bridged_event()) is False
        assert client.managed_pi_grant(_bridged_event(tool="work_report")) is True

    def test_an_ambient_session_grants_nothing(self, tmp_path, monkeypatch):
        client = self._client(tmp_path, monkeypatch, managed=False)
        assert client.managed_pi_grant(_bridged_event()) is False

    def test_the_gate_verdict_is_only_upgraded_from_undecided(self, tmp_path, monkeypatch):
        client = self._client(tmp_path, monkeypatch)
        event = _bridged_event()
        upgraded = managed_pi_allowed_grant(ToolHookResult(action=TOOL_ALLOW), event, client)
        assert upgraded.action == TOOL_AUTO_APPROVE and upgraded.identity_grant is True
        deny = ToolHookResult(action=TOOL_DENY, reason="policy")
        assert managed_pi_allowed_grant(deny, event, client) is deny
        undecided = ToolHookResult(action=TOOL_ALLOW)
        assert managed_pi_allowed_grant(undecided, event, client, classifier_only=True) is (
            undecided
        )
        assert managed_pi_allowed_grant(undecided, event, object()) is undecided
        unverified = _bridged_event(verified=False)
        assert managed_pi_allowed_grant(undecided, unverified, client) is undecided

    def test_the_provider_forwards_to_its_client(self, tmp_path, monkeypatch):
        provider = AcpProvider(work_dir=tmp_path, acp_backend=ACP_BACKEND_PI, pi_managed=True)
        provider._client = self._client(tmp_path, monkeypatch)
        assert provider.managed_pi_grant(_bridged_event()) is True
        assert provider.managed_pi_grant(_bridged_event(verified=False)) is False


def _node(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    version = subprocess.run(
        [node, "--version"], cwd=tmp_path, capture_output=True, timeout=10, **UTF8_TEXT
    )
    numbers = tuple(int(n) for n in version.stdout.strip().lstrip("v").split("."))
    if numbers < (22, 15, 0) or (numbers[0] == 23 and numbers < (23, 5, 0)):
        pytest.skip("node cannot load the isolated SDK import hook")
    return node


@pytest.mark.timeout(90)
@pytest.mark.parametrize("spelling", ["absolute", "tilde", "file-url"])
def test_shipped_gate_enforces_profile_and_keeps_project_instructions(tmp_path, spelling):
    """Execute the actual TS factory/handlers; never start a model or touch host config."""
    script = r"""
      import { pathToFileURL } from "node:url";
      import { join } from "node:path";
      import { registerHooks } from "node:module";
      // Unit-test boundary: pi supplies an already normalized agent directory.
      // A separate integration test exercises the real SDK normalization/loader.
      registerHooks({ resolve(specifier, context, nextResolve) {
        if (specifier === "@earendil-works/pi-coding-agent") {
          return { url: "data:text/javascript," + encodeURIComponent(
            "export const getAgentDir = () => process.env.RESOLVED_PI_AGENT_DIR;"
          ), shortCircuit: true };
        }
        return nextResolve(specifier, context);
      }});
      const mod = await import(pathToFileURL(process.env.PROFILE_PATH));
      const gate = await import(pathToFileURL(process.env.GATE_PATH));
      const handlers = new Map(); let active = null; const questions = [];
      const pi = {
        registerCommand() {}, on(name, fn) {
          handlers.set(name, [...(handlers.get(name) ?? []), fn]);
        },
        setActiveTools(tools) { active = tools; }, getAllTools() { return []; },
      };
      mod.default(pi);
      gate.default(pi);
      for (const handler of handlers.get("session_start")) await handler();
      const options = {
        contextFiles: [
          { path: join(process.cwd(), "agent", "AGENTS.md"), content: "use subagent" },
          { path: join(process.cwd(), "project", "AGENTS.md"), content: "project rules" },
        ], customPrompt: "ambient", forceSystemPrompt: "ambient", appendSystemPrompt: "ambient",
        sections: {},
      };
      for (const handler of handlers.get("before_agent_start")) {
        await handler({ systemPromptOptions: options });
      }
      const ctx = { hasUI: true, ui: { confirm: async (name) => { questions.push(name); return true; } } };
      const results = {};
      for (const toolName of ["write", "edit", "bash", "subagent", "mcp__kirocrew-core__spawn_run", "read"]) {
        results[toolName] = null;
        for (const handler of handlers.get("tool_call")) {
          const result = await handler({ toolName, toolCallId: "x", input: {} }, ctx);
          if (result?.block) { results[toolName] = result; break; }
        }
      }
      let invalid = false;
      try { mod.managedTools('null'); } catch { invalid = true; }
      delete process.env.KIROCREW_PI_MANAGED_TOOLS;
      const ambient = new Map();
      gate.default({ ...pi, on(name, fn) { ambient.set(name, fn); } });
      console.log(JSON.stringify({ active, options, results, questions, invalid, ambient: [...ambient.keys()] }));
    """
    result = subprocess.run(
        [_node(tmp_path), "--experimental-strip-types", "--input-type=module", "-e", script],
        cwd=tmp_path,
        env={
            **os.environ,
            "GATE_PATH": pi_gate_extension_path(),
            "PROFILE_PATH": pi_managed_extension_path(),
            "RESOLVED_PI_AGENT_DIR": str(tmp_path / "agent"),
            "HOME": str(tmp_path),
            "USERPROFILE": str(tmp_path),
            "PI_CODING_AGENT_DIR": {
                "absolute": str(tmp_path / "agent"),
                "tilde": "~/agent",
                "file-url": (tmp_path / "agent").as_uri(),
            }[spelling],
            "KIROCREW_PI_MANAGED_TOOLS": json.dumps(READS),
        },
        capture_output=True,
        timeout=60,
        **UTF8_TEXT,
    )
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert out["active"] == list(READS)
    assert [f["content"] for f in out["options"]["contextFiles"]] == ["project rules"]
    assert out["options"]["appendSystemPrompt"] == ""
    assert "customPrompt" not in out["options"] and "forceSystemPrompt" not in out["options"]
    assert out["results"]["read"] is None
    assert all(result["block"] for name, result in out["results"].items() if name != "read")
    assert out["questions"] == ["read"], "allowed calls still pass through Crew's gate"
    assert out["invalid"] is True
    assert out["ambient"] == ["tool_call"]


@pytest.mark.timeout(90)
def test_real_pi_managed_startup_exposes_only_reader_tools(tmp_path, monkeypatch):
    """Real pi, no provider request: prove resource suppression and active tools."""
    pi_bin = shutil.which("pi")
    if not pi_bin:
        pytest.skip("pi is not installed")
    installed = acp_client._pi_installed_version(pi_bin)
    if installed is None or installed[0] < (0, 87, 1):
        pytest.skip("managed pi requires a verifiable pi 0.87.1 installation")
    agent_dir = tmp_path / "agent"
    extensions = agent_dir / "extensions"
    extensions.mkdir(parents=True)
    witness = tmp_path / "ambient-loaded"
    (extensions / "ambient.ts").write_text(
        'import { writeFileSync } from "node:fs";\n'
        f'export default function () {{ writeFileSync({json.dumps(str(witness))}, "loaded"); }}\n',
        encoding="utf-8",
    )
    observer = tmp_path / "observer.ts"
    observer.write_text(
        'export default function (pi) { pi.on("session_start", () => {\n'
        "console.log(JSON.stringify({managedTestTools: pi.getActiveTools()}));\n"
        "}); }\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(acp_client, "_pi_gate_artifact_dir", lambda: str(tmp_path))
    monkeypatch.setattr(acp_client, "_pi_gate_launcher_cache", {})
    launcher = acp_client._ensure_pi_gate_launcher(
        pi_bin,
        pi_gate_extension_path(),
        (str(observer),),
        managed=True,
        managed_extension=pi_managed_extension_path(),
    )
    env = {
        **os.environ,
        "PI_CODING_AGENT_DIR": str(agent_dir),
        "PI_OFFLINE": "1",
        "KIROCREW_PI_MANAGED_TOOLS": json.dumps(READS),
        "TMPDIR": str(tmp_path),
        "TMP": str(tmp_path),
        "TEMP": str(tmp_path),
    }
    env.pop("KIROCREW_PI_BRIDGE_SERVERS", None)
    result = subprocess.run(
        [launcher, *acp_client._PI_RPC_ARGS, "--no-session"],
        cwd=tmp_path,
        env=env,
        input=json.dumps(acp_client._PI_READBACK_REQUEST) + "\n",
        capture_output=True,
        timeout=60,
        **UTF8_TEXT,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    records = []
    # Pi routes extension console output to stderr in RPC mode.
    for line in result.stderr.splitlines():
        try:
            records.append(json.loads(line))
        except ValueError:
            pass
    observations = [r["managedTestTools"] for r in records if "managedTestTools" in r]
    assert observations, (result.stdout, result.stderr)
    active = observations[-1]
    assert sorted(active) == list(READS)
    assert not witness.exists(), "ambient extensions must not even execute their factories"
    commands = acp_client._pi_commands_from_readback(result.stdout)
    assert commands is not None
    assert managed_profile_issue(commands, pi_managed_extension_path()) == ""
    assert (
        acp_client.acp_tool_gate.gate_extension_issue(
            ACP_BACKEND_PI, commands, pi_gate_extension_path()
        )
        == ""
    )


@pytest.mark.timeout(90)
@pytest.mark.parametrize("spelling", ["absolute", "tilde", "file-url"])
def test_real_pi_context_loader_excludes_global_but_keeps_project(tmp_path, spelling):
    pi_bin = shutil.which("pi")
    if not pi_bin:
        pytest.skip("pi SDK is not installed")
    sdk = next(
        (
            p
            for p in Path(pi_bin).resolve().parents
            if (p / "dist/config.js").is_file() and (p / "dist/core/extensions/loader.js").is_file()
        ),
        None,
    )
    if sdk is None:
        pytest.skip("this pi installation does not expose the SDK modules")
    agent, project = tmp_path / "agent", tmp_path / "project"
    agent.mkdir()
    project.mkdir()
    (agent / "AGENTS.md").write_text("GLOBAL-MUST-NOT-SURVIVE", encoding="utf-8")
    (project / "AGENTS.md").write_text("PROJECT-MUST-SURVIVE", encoding="utf-8")
    script = r"""
      import { pathToFileURL } from "node:url";
      import { join } from "node:path";
      const sdk = process.env.SDK;
      const { getAgentDir } = await import(pathToFileURL(join(sdk, "dist/config.js")));
      const { loadProjectContextFiles } = await import(pathToFileURL(join(sdk, "dist/core/resource-loader.js")));
      const { loadExtensions } = await import(pathToFileURL(join(sdk, "dist/core/extensions/loader.js")));
      const project = process.env.PROJECT;
      const options = { contextFiles: loadProjectContextFiles({cwd: project, agentDir: getAgentDir()}), sections: {} };
      const before = options.contextFiles.map(f => f.content);
      const result = await loadExtensions([process.env.PROFILE_PATH], project);
      if (result.errors.length) throw new Error(JSON.stringify(result.errors));
      for (const handler of result.extensions[0].handlers.get("before_agent_start")) {
        await handler({systemPromptOptions: options}, {});
      }
      console.log(JSON.stringify({before, after: options.contextFiles.map(f => f.content)}));
    """
    result = subprocess.run(
        [_node(tmp_path), "--input-type=module", "-e", script],
        cwd=tmp_path,
        env={
            **os.environ,
            "SDK": str(sdk),
            "PROJECT": str(project),
            "PROFILE_PATH": pi_managed_extension_path(),
            "PI_OFFLINE": "1",
            "KIROCREW_PI_MANAGED_TOOLS": json.dumps(READS),
            "HOME": str(tmp_path),
            "USERPROFILE": str(tmp_path),
            "PI_CODING_AGENT_DIR": {
                "absolute": str(agent),
                "tilde": "~/agent",
                "file-url": agent.as_uri(),
            }[spelling],
            "TMPDIR": str(tmp_path),
            "TMP": str(tmp_path),
            "TEMP": str(tmp_path),
        },
        capture_output=True,
        timeout=60,
        **UTF8_TEXT,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    data = json.loads(result.stdout.strip().splitlines()[-1])
    assert "GLOBAL-MUST-NOT-SURVIVE" in data["before"]
    assert "GLOBAL-MUST-NOT-SURVIVE" not in data["after"]
    assert "PROJECT-MUST-SURVIVE" in data["after"]
