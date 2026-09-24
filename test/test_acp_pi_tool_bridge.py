"""The pi tool bridge: Crew's subagent tools reach a pi session through an extension.

pi-acp never hands the ``session/new`` MCP array to pi, so Crew's own tools reach a
pi session only through an extension Crew loads, the same way its gate does. Four
things are pinned here:

* **The seal.** The bridge is verified against a pinned digest and loaded from a
  sealed copy, like the gate. Unlike the gate it is optional: any failure means
  "no bridge", never a refused session.
* **The server list.** Only this session's broker stubs for Crew's control-plane
  server go to the child, with the tools the bridge may register.
* **The identity.** A bridged call is governed as the MCP tool it names only when
  the gate reports it came from the sealed bridge copy.
* **The extension itself.** Driven under Node against a fake MCP server, and the
  gate's envelope carried from Node into the Python parser.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import logging
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from kiro_crew.acp import client as acp_client
from kiro_crew.acp._dispatch import (
    GATE_ENVELOPE_MARKER,
    GateBridgeIdentity,
    build_permission_event,
    gate_bridged_mcp_call,
    gate_envelope,
)
from kiro_crew.acp.client import (
    PI_BRIDGE_EXTENSION_SHA256,
    AcpClient,
    PiGateExtensionTampered,
    _ensure_pi_gate_launcher,
    _pi_gate_launcher_body,
    _seal_pi_bridge_extension,
    pi_bridge_extension_path,
    pi_gate_extension_path,
)
from kiro_crew.acp.types import JsonRpcMessage
from kiro_crew.acp_backends import (
    ACP_BACKEND_PI,
    ACP_BACKENDS_EXTENSION_TOOL_BRIDGE,
    ACP_BACKENDS_KNOWN,
)
from kiro_crew.subprocess_utf8 import UTF8_TEXT

NONCE = "c0ffee00" * 4
SEALED = "/crew/pi-gate/kirocrew_pi_gate_1_bridge.ts"
BRIDGE = GateBridgeIdentity(sources=frozenset({SEALED}), servers=frozenset({"kirocrew-core"}))


# ── The seal ────────────────────────────────────────────────────────────────


class TestBridgeSeal:
    def test_the_shipped_bytes_match_the_pinned_digest(self):
        payload = acp_client._pi_gate_extension_bytes(Path(pi_bridge_extension_path()).read_bytes())
        assert hashlib.sha256(payload).hexdigest() == PI_BRIDGE_EXTENSION_SHA256

    def test_the_bridge_ships_beside_the_gate(self):
        assert Path(pi_bridge_extension_path()).parent == Path(pi_gate_extension_path()).parent
        assert Path(pi_bridge_extension_path()).is_file()

    def test_the_sealed_copy_is_read_only_in_the_artifact_dir(self, monkeypatch, tmp_path):
        run_dir = tmp_path / "pi-gate"
        run_dir.mkdir()
        monkeypatch.setattr(acp_client, "_pi_gate_artifact_dir", lambda: str(run_dir))
        sealed = Path(_seal_pi_bridge_extension())
        assert sealed.parent == run_dir
        assert sealed.name == f"kirocrew_pi_gate_{os.getpid()}_bridge.ts"
        assert sealed.read_bytes() == Path(pi_bridge_extension_path()).read_bytes()
        gate = Path(acp_client._seal_pi_gate_extension())
        assert gate != sealed, "the bridge must not overwrite the gate's copy"

    def test_a_rewritten_bridge_is_refused(self, monkeypatch, tmp_path):
        tampered = tmp_path / "bridge.ts"
        tampered.write_text("// not the bridge\n", encoding="utf-8")
        monkeypatch.setattr(acp_client, "pi_bridge_extension_path", lambda: str(tampered))
        with pytest.raises(PiGateExtensionTampered) as excinfo:
            _seal_pi_bridge_extension()
        assert "bridge extension" in str(excinfo.value)


class TestLauncherCarriesBothExtensions:
    def test_the_gate_comes_first_then_the_bridge(self, monkeypatch):
        monkeypatch.setattr(acp_client.platform_compat, "IS_WINDOWS", False)
        body = _pi_gate_launcher_body("/usr/bin/pi", "/g/gate.ts", ("/g/bridge's.ts",))
        line = body.splitlines()[1]
        assert line.count("--extension") == 2
        assert line.index("/g/gate.ts") < line.index("bridge")
        assert line.endswith("'/g/bridge'\"'\"'s.ts'"), line

    def test_the_windows_body_carries_both(self, monkeypatch):
        monkeypatch.setattr(acp_client.platform_compat, "IS_WINDOWS", True)
        body = _pi_gate_launcher_body("C:\\pi.cmd", "C:\\g\\gate.ts", ("C:\\g\\bridge.ts",))
        assert body.count("--extension") == 2
        assert '"C:\\g\\bridge.ts"' in body

    def test_no_extra_extension_is_the_old_launcher(self, monkeypatch):
        monkeypatch.setattr(acp_client.platform_compat, "IS_WINDOWS", False)
        assert _pi_gate_launcher_body("/p", "/g.ts") == _pi_gate_launcher_body("/p", "/g.ts", ())
        assert _pi_gate_launcher_body("/p", "/g.ts").count("--extension") == 1

    def test_a_different_bridge_is_a_different_launcher(self, monkeypatch, tmp_path):
        monkeypatch.setattr(acp_client, "_pi_gate_artifact_dir", lambda: str(tmp_path))
        monkeypatch.setattr(acp_client, "_pi_gate_launcher_cache", {})
        plain = _ensure_pi_gate_launcher("/usr/bin/pi", "/g.ts")
        bridged = _ensure_pi_gate_launcher("/usr/bin/pi", "/g.ts", ("/b.ts",))
        assert plain != bridged
        assert "/b.ts" in Path(bridged).read_text(encoding="utf-8")
        assert "/b.ts" not in Path(plain).read_text(encoding="utf-8")


# ── The server list ─────────────────────────────────────────────────────────


def _stub(name: str) -> dict:
    return {
        "name": name,
        "command": "/venv/bin/python",
        "args": ["-m", "kiro_crew.mcp_gateway.stub", "--stub-flags-b64=abc"],
        "env": [{"name": "KIROCREW_STUB_SESSION_TOKEN", "value": "tok"}],
    }


def _client_with_stubs(monkeypatch, stubs: list[dict]) -> AcpClient:
    client = AcpClient.__new__(AcpClient)
    client._agent = "kirocrew"
    monkeypatch.setattr(AcpClient, "_pooled_broker_stubs", lambda self: stubs)
    return client


class TestPrepareBridge:
    def test_only_crews_server_goes_to_the_child(self, monkeypatch, tmp_path):
        monkeypatch.setattr(acp_client, "_seal_pi_bridge_extension", lambda: SEALED)
        client = _client_with_stubs(monkeypatch, [_stub("github"), _stub("kirocrew-core")])
        prepared = client._prepare_pi_tool_bridge()
        assert prepared is not None
        sealed, servers_env, identity = prepared
        assert sealed == SEALED
        servers = json.loads(servers_env)["servers"]
        assert [s["name"] for s in servers] == ["kirocrew-core"]
        assert servers[0]["command"] == "/venv/bin/python"
        assert servers[0]["env"] == {"KIROCREW_STUB_SESSION_TOKEN": "tok"}
        assert set(servers[0]["tools"]) == set(acp_client._PI_BRIDGE_TOOLS["kirocrew-core"])
        assert identity.servers == frozenset({"kirocrew-core"})
        assert any(s.endswith("kirocrew_pi_gate_1_bridge.ts") for s in identity.sources)

    def test_no_stub_means_no_bridge_and_says_why_once(self, monkeypatch, caplog):
        monkeypatch.setattr(acp_client, "_pi_bridge_off_noted", set())
        sealed = []
        monkeypatch.setattr(
            acp_client, "_seal_pi_bridge_extension", lambda: sealed.append(1) or SEALED
        )
        client = _client_with_stubs(monkeypatch, [_stub("github")])
        with caplog.at_level(logging.WARNING, logger=acp_client.logger.name):
            assert client._prepare_pi_tool_bridge() is None
            assert client._prepare_pi_tool_bridge() is None
        notes = [r for r in caplog.records if "stub_servers" in r.getMessage()]
        assert len(notes) == 1
        assert sealed == [], "nothing is sealed for a session that cannot use it"

    def test_a_bad_seal_is_no_bridge_not_a_refusal(self, monkeypatch):
        def tampered():
            raise PiGateExtensionTampered("digest mismatch")

        monkeypatch.setattr(acp_client, "_seal_pi_bridge_extension", tampered)
        client = _client_with_stubs(monkeypatch, [_stub("kirocrew-core")])
        assert client._prepare_pi_tool_bridge() is None

    def test_an_unreadable_overlay_is_no_bridge(self, monkeypatch):
        client = AcpClient.__new__(AcpClient)
        client._agent = "kirocrew"

        def boom(self):
            raise OSError("overlay gone")

        monkeypatch.setattr(AcpClient, "_pooled_broker_stubs", boom)
        assert client._prepare_pi_tool_bridge() is None

    def test_the_bridged_tools_exist_on_crews_server(self):
        from kiro_crew import mcp_core

        served = {t["name"] for t in mcp_core._list_tools()}
        assert set(acp_client._PI_BRIDGE_TOOLS) == {"kirocrew-core"}
        assert set(acp_client._PI_BRIDGE_TOOLS["kirocrew-core"]) <= served


class TestSpawnWiring:
    def test_the_set_names_pi_alone(self):
        assert ACP_BACKENDS_EXTENSION_TOOL_BRIDGE == frozenset({ACP_BACKEND_PI})
        assert ACP_BACKENDS_EXTENSION_TOOL_BRIDGE <= ACP_BACKENDS_KNOWN

    def test_the_arm_prepares_the_bridge_off_the_loop_and_by_membership(self):
        body = inspect.getsource(AcpClient._spawn).split("elif self._is_pi:", 1)[1]
        body = body.split("        elif self._is_droid:", 1)[0]
        assert "self.backend in ACP_BACKENDS_EXTENSION_TOOL_BRIDGE" in body
        assert "asyncio.to_thread(self._prepare_pi_tool_bridge)" in body
        assert body.index("_prepare_pi_tool_bridge") < body.index("_ensure_pi_gate_launcher")

    def test_the_child_gets_the_list_only_from_this_session(self):
        source = inspect.getsource(AcpClient._spawn)
        assert "env[_ENV_PI_BRIDGE_SERVERS] = self._pi_bridge_servers_env" in source
        assert "env.pop(_ENV_PI_BRIDGE_SERVERS, None)" in source

    def test_the_read_back_never_starts_a_bridge_server(self):
        source = inspect.getsource(AcpClient._verify_pi_gate)
        assert "env.pop(_ENV_PI_BRIDGE_SERVERS, None)" in source

    def test_the_parser_gets_the_identity_only_with_a_nonce(self):
        client = AcpClient.__new__(AcpClient)
        client._tool_call_inputs = {}
        client._tool_call_is_shell = {}
        client._tool_call_params = {}
        client._tool_call_mcp_server = {}
        client._tool_call_tool_name = {}
        client._permission_options = {}
        client._pi_gate_asked_ids = set()
        client._pi_gate_request_tool = {}
        client._pi_bridge_identity = BRIDGE
        frame = _frame(_bridge_envelope())
        client._pi_gate_nonce = ""
        assert client._build_permission_event(frame).mcp_server_name == ""
        client._pi_gate_nonce = NONCE
        event = client._build_permission_event(frame)
        assert (event.mcp_server_name, event.tool_name) == ("kirocrew-core", "spawn_run")


# ── The identity ────────────────────────────────────────────────────────────


def _bridge_envelope(**overrides) -> str:
    body = {
        GATE_ENVELOPE_MARKER: 1,
        "nonce": NONCE,
        "toolCallId": "call_1",
        "tool": "mcp__kirocrew-core__spawn_run",
        "kind": "other",
        "source": SEALED,
        "input": {"task": "review the diff", "backend": "codex"},
        "truncated": False,
    }
    body.update(overrides)
    return json.dumps(body)


def _frame(message: str) -> JsonRpcMessage:
    title = json.loads(message)["tool"]
    return JsonRpcMessage(
        id=7,
        method="session/request_permission",
        params={
            "sessionId": "s",
            "toolCall": {
                "toolCallId": "pi-ui-1",
                "title": title,
                "kind": "other",
                "rawInput": {"method": "confirm", "title": title, "message": message},
            },
            "options": [
                {"optionId": "yes", "name": "Yes", "kind": "allow_once"},
                {"optionId": "no", "name": "No", "kind": "reject_once"},
            ],
        },
    )


def _envelope_of(message: str) -> dict | None:
    return gate_envelope(_frame(message).params["toolCall"], NONCE)


class TestBridgedIdentity:
    def test_a_call_from_the_sealed_bridge_names_its_mcp_tool(self):
        env = _envelope_of(_bridge_envelope())
        assert gate_bridged_mcp_call(env, BRIDGE) == ("kirocrew-core", "spawn_run")

    def test_a_tool_of_the_same_name_from_another_file_is_not_identified(self):
        env = _envelope_of(_bridge_envelope(source="/home/me/.pi/agent/extensions/evil.ts"))
        assert gate_bridged_mcp_call(env, BRIDGE) is None

    def test_an_envelope_without_a_source_is_not_identified(self):
        body = json.loads(_bridge_envelope())
        del body["source"]
        assert gate_bridged_mcp_call(_envelope_of(json.dumps(body)), BRIDGE) is None

    def test_a_server_the_bridge_was_not_handed_is_not_identified(self):
        env = _envelope_of(_bridge_envelope(tool="mcp__github__delete_repo"))
        assert gate_bridged_mcp_call(env, BRIDGE) is None

    def test_a_session_without_the_bridge_identifies_nothing(self):
        assert gate_bridged_mcp_call(_envelope_of(_bridge_envelope()), None) is None

    def test_the_server_is_a_known_prefix_not_a_split(self):
        env = _envelope_of(_bridge_envelope(tool="mcp__kirocrew-core__a__b"))
        assert gate_bridged_mcp_call(env, BRIDGE) == ("kirocrew-core", "a__b")

    def test_the_permission_event_carries_the_trusted_identity(self):
        event, _ = build_permission_event(
            _frame(_bridge_envelope()), gate_envelope_nonce=NONCE, gate_bridge=BRIDGE
        )
        assert event.title == "mcp__kirocrew-core__spawn_run"
        assert event.mcp_server_name == "kirocrew-core"
        assert event.tool_name == "spawn_run"
        assert event.mcp_identity_trusted is True
        assert event.is_shell is False
        assert event.raw_tool_params == {"task": "review the diff", "backend": "codex"}

    def test_a_foreign_source_keeps_the_title_but_earns_no_identity(self):
        event, _ = build_permission_event(
            _frame(_bridge_envelope(source="/elsewhere.ts")),
            gate_envelope_nonce=NONCE,
            gate_bridge=BRIDGE,
        )
        assert event.title == "mcp__kirocrew-core__spawn_run"
        assert event.mcp_server_name == ""
        assert event.mcp_identity_trusted is False

    def test_without_the_nonce_nothing_is_read_at_all(self):
        event, _ = build_permission_event(
            _frame(_bridge_envelope()), gate_envelope_nonce=None, gate_bridge=BRIDGE
        )
        assert event.mcp_server_name == ""
        assert event.mcp_identity_trusted is False


# ── The extensions under Node ───────────────────────────────────────────────

_FAKE_MCP = textwrap.dedent("""
    import json, sys
    for line in sys.stdin:
        msg = json.loads(line)
        mid, method = msg.get("id"), msg.get("method")
        if mid is None:
            continue
        if method == "initialize":
            res = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}}
        elif method == "tools/list":
            res = {"tools": [
                {"name": "spawn_list", "description": "List runs",
                 "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer"}}}},
                {"name": "memory_save", "inputSchema": {"type": "object"}},
            ]}
        elif method == "tools/call":
            args = msg["params"].get("arguments") or {}
            if args.get("crash"):
                sys.exit(3)
            if args.get("limit") == 0:
                res = {"content": [{"type": "text", "text": "limit must be positive"}],
                       "isError": True}
            else:
                res = {"content": [{"type": "text", "text": "runs " + json.dumps(args)}]}
        else:
            print(json.dumps({"jsonrpc": "2.0", "id": mid,
                              "error": {"code": -32601, "message": "no"}}), flush=True)
            continue
        print(json.dumps({"jsonrpc": "2.0", "id": mid, "result": res}), flush=True)
    """)

_BRIDGE_DRIVER = textwrap.dedent("""
    const mod = await import(process.env.BRIDGE_PATH);
    const tools = [];
    const handlers = {};
    const pi = {
      registerCommand() {},
      registerTool(t) { tools.push(t); },
      on(name, fn) { handlers[name] = fn; },
    };
    await mod.default(pi);
    const out = { names: tools.map((t) => t.name) };
    const tool = tools[0];
    out.ok = (await tool.execute("c1", { limit: 2 })).content;
    try { await tool.execute("c2", { limit: 0 }); out.error = null; }
    catch (e) { out.error = e.message; }
    try { await tool.execute("c3", { crash: true }); out.crash = null; }
    catch (e) { out.crash = e.message; }
    out.after = (await tool.execute("c4", { limit: 1 })).content;
    await handlers.session_shutdown?.();
    console.log(JSON.stringify(out));
    """)

_GATE_DRIVER = textwrap.dedent("""
    const mod = await import(process.env.GATE_PATH);
    let handler;
    const pi = {
      registerCommand() {},
      on(name, fn) { if (name === "tool_call") handler = fn; },
      getAllTools() {
        return [
          { name: "mcp__kirocrew-core__spawn_run",
            sourceInfo: { path: process.env.SEALED, source: "cli" } },
          { name: "read", sourceInfo: { path: "<builtin:read>", source: "builtin" } },
        ];
      },
    };
    mod.default(pi);
    const messages = [];
    const ctx = { hasUI: true, ui: { confirm: async (_t, m) => { messages.push(m); return true; } } };
    await handler({ toolCallId: "call_1", toolName: "mcp__kirocrew-core__spawn_run",
                    input: { task: "t" } }, ctx);
    await handler({ toolCallId: "call_2", toolName: "read", input: { path: "a" } }, ctx);
    console.log(JSON.stringify(messages));
    """)


def _node_or_skip() -> str:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    probe = subprocess.run(
        [
            node,
            "--input-type=module",
            "-e",
            f"await import({json.dumps(pi_bridge_extension_path())})",
        ],
        capture_output=True,
        timeout=60,
        **UTF8_TEXT,
    )
    if probe.returncode != 0:
        pytest.skip(f"this node cannot load TypeScript directly: {probe.stderr[-200:]}")
    return node


def _run_node(node: str, script: str, env: dict[str, str]) -> str:
    completed = subprocess.run(
        [node, "--input-type=module", "-e", script],
        capture_output=True,
        timeout=60,
        env={"PATH": "/usr/bin:/bin", **env},
        **UTF8_TEXT,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.strip().splitlines()[-1]


@pytest.mark.timeout(120)
class TestExtensionsUnderNode:
    def test_the_bridge_registers_only_named_tools_and_forwards_calls(self, tmp_path):
        node = _node_or_skip()
        server = tmp_path / "fake_mcp.py"
        server.write_text(_FAKE_MCP, encoding="utf-8")
        servers = {
            "servers": [
                {
                    "name": "kirocrew-core",
                    "command": sys.executable,
                    "args": [str(server)],
                    "env": {},
                    "tools": ["spawn_list", "spawn_run"],
                }
            ]
        }
        out = json.loads(
            _run_node(
                node,
                _BRIDGE_DRIVER,
                {
                    "BRIDGE_PATH": pi_bridge_extension_path(),
                    "KIROCREW_PI_BRIDGE_SERVERS": json.dumps(servers),
                },
            )
        )
        assert out["names"] == ["mcp__kirocrew-core__spawn_list"]
        assert out["ok"] == [{"type": "text", "text": 'runs {"limit": 2}'}]
        assert out["error"] == "limit must be positive"
        assert "exited" in out["crash"]
        assert out["after"] == [{"type": "text", "text": 'runs {"limit": 1}'}], "restarted"

    def test_without_a_server_list_the_bridge_registers_nothing(self):
        node = _node_or_skip()
        out = json.loads(
            _run_node(
                node,
                _BRIDGE_DRIVER.split("const out")[0] + "console.log(JSON.stringify(tools.length));",
                {"BRIDGE_PATH": pi_bridge_extension_path()},
            )
        )
        assert out == 0

    def test_the_gate_envelope_names_the_source_and_python_reads_it(self):
        node = _node_or_skip()
        messages = json.loads(
            _run_node(
                node,
                _GATE_DRIVER,
                {
                    "GATE_PATH": pi_gate_extension_path(),
                    "SEALED": SEALED,
                    "KIROCREW_PI_GATE_SESSION": NONCE,
                },
            )
        )
        bridged, builtin = (json.loads(m) for m in messages)
        assert bridged["source"] == SEALED
        assert builtin["source"] == ""
        event, _ = build_permission_event(
            _frame(messages[0]), gate_envelope_nonce=NONCE, gate_bridge=BRIDGE
        )
        assert (event.mcp_server_name, event.tool_name) == ("kirocrew-core", "spawn_run")
        assert event.mcp_identity_trusted is True
