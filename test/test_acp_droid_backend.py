"""Factory Droid as a dormant ACP harness, and the fork's explicit opt-in for it.

Everything here runs without the harness or a credential: the vocabulary, the
routing precondition read off the captured ``session/new``, the opt-in's effect on
the registry, the auth declaration, the install probe, and the spawn arm's
preflight.
"""

from __future__ import annotations

import inspect
import json
import os
import stat
from pathlib import Path

import pytest

from kiro_crew.acp.client import AcpClient
from kiro_crew.agent_sdk import backends as sdk_backends
from kiro_crew.agent_sdk import host_auth, tool_gate
from kiro_crew.agent_sdk.backends import (
    ACP_BACKEND_DROID,
    ACP_BACKENDS_KNOWN,
    ACP_BACKENDS_SESSION_MCP_ARRAY,
    BASELINE_SELECTABLE_BACKENDS,
    EXPERIMENTAL_OPT_IN_BACKENDS,
    Routing,
    launch_for,
    opt_in_experimental_backends,
    permission_config_for,
    routing_for,
    selectable_backends,
)

CORPUS = Path(__file__).parent / "fixtures" / "acp_frames" / "droid"


@pytest.fixture
def restore_registry():
    baseline, selectable = set(sdk_backends._baseline), set(sdk_backends._selectable)
    yield
    sdk_backends._baseline.clear()
    sdk_backends._baseline.update(baseline)
    sdk_backends._selectable.clear()
    sdk_backends._selectable.update(selectable)


def _frames(name: str) -> list[dict]:
    lines = (CORPUS / name).read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines[1:] if line.strip()]


# ── vocabulary ──


def test_droid_is_known_and_dormant():
    assert ACP_BACKEND_DROID in ACP_BACKENDS_KNOWN
    assert ACP_BACKEND_DROID not in BASELINE_SELECTABLE_BACKENDS
    assert ACP_BACKEND_DROID in EXPERIMENTAL_OPT_IN_BACKENDS


def test_it_is_not_selectable_without_the_opt_in():
    if os.environ.get(sdk_backends.EXPERIMENTAL_OPT_IN_ENV):
        pytest.skip("this run opted in on purpose")
    assert ACP_BACKEND_DROID not in selectable_backends()


def test_the_launch_never_raises_its_own_autonomy():
    launch = launch_for(ACP_BACKEND_DROID)
    assert launch.binary == "droid"
    assert launch.acp_args == ("exec", "--output-format", "acp")
    assert "--auto" not in launch.acp_args
    assert "--skip-permissions-unsafe" not in launch.acp_args
    assert launch.bin_env_var == "DROID_BIN"
    assert sdk_backends.ACP_BACKEND_PROCESS_NAMES[ACP_BACKEND_DROID] == "droid"


def test_its_session_array_is_the_mcp_channel():
    assert ACP_BACKEND_DROID in ACP_BACKENDS_SESSION_MCP_ARRAY


# ── routing, read off the captured wire ──


def test_routing_is_the_enforced_session_config_option():
    assert routing_for(ACP_BACKEND_DROID) is Routing.SESSION_CONFIG
    assert permission_config_for(ACP_BACKEND_DROID) == ("autonomy_level", "normal")
    assert tool_gate.is_enforced(ACP_BACKEND_DROID)
    verdict, _reason = tool_gate.routing_verdict(ACP_BACKEND_DROID)
    assert verdict is tool_gate.Verdict.ROUTED


def _captured_session_new() -> dict:
    return next(
        f["result"]
        for f in _frames("handshake-live.jsonl")
        if "sessionId" in (f.get("result") or {})
    )


def test_the_captured_session_advertises_the_option_crew_arms():
    options = _captured_session_new()["configOptions"]
    assert tool_gate.session_config_issue(ACP_BACKEND_DROID, options) in ("", None)
    autonomy = next(o for o in options if o["id"] == "autonomy_level")
    assert "normal" in {o["value"] for o in autonomy["options"]}


def test_a_session_without_the_option_is_not_armable():
    assert tool_gate.session_config_issue(ACP_BACKEND_DROID, [])


def test_the_captured_write_of_the_option_was_accepted():
    frames = _frames("handshake-live.jsonl")
    ids = {f.get("id"): f for f in frames if "id" in f}
    assert any("result" in f and f["result"] == {} for f in ids.values())
    modes = [
        f["params"]["update"]
        for f in frames
        if f.get("method") == "session/update"
        and f["params"]["update"].get("sessionUpdate") == "current_mode_update"
    ]
    assert modes and modes[-1]["currentModeId"] == "normal"


def test_the_corpus_carries_no_credential():
    for path in CORPUS.glob("*.jsonl"):
        text = path.read_text(encoding="utf-8")
        assert "fk-" not in text, path.name
        assert "FACTORY_API_KEY=" not in text, path.name


# ── the opt-in ──


def test_opting_in_makes_it_selectable(restore_registry):
    assert opt_in_experimental_backends("droid") == frozenset({ACP_BACKEND_DROID})
    assert ACP_BACKEND_DROID in selectable_backends()
    assert sdk_backends.resolve_selected_backend("droid") == ACP_BACKEND_DROID


def test_the_opt_in_accepts_only_its_allowlist(restore_registry, caplog):
    before = selectable_backends()
    assert opt_in_experimental_backends(" deepseek , nope ") == frozenset()
    assert selectable_backends() == before
    assert "not an opt-in backend" in caplog.text


def test_an_empty_opt_in_changes_nothing(restore_registry):
    before = selectable_backends()
    assert opt_in_experimental_backends("") == frozenset()
    assert selectable_backends() == before


def test_governance_still_narrows_an_opted_in_backend(restore_registry):
    opt_in_experimental_backends("droid")
    removed = sdk_backends.apply_selectable_denials({ACP_BACKEND_DROID})
    assert ACP_BACKEND_DROID in removed
    assert ACP_BACKEND_DROID not in selectable_backends()
    sdk_backends.apply_selectable_denials(set())


def test_a_spawn_on_droid_needs_the_opt_in(restore_registry):
    from kiro_crew import subagent_backend as sb

    if ACP_BACKEND_DROID not in selectable_backends():
        refusal = sb.check_spawn_backend("droid")
        assert refusal is not None and refusal.code == sb.UNKNOWN_BACKEND_CODE
    opt_in_experimental_backends("droid")
    assert sb.check_spawn_backend("droid") is None


# ── auth, install probe, spawn arm ──


def test_the_auth_declaration_spares_only_its_own_store():
    declaration = host_auth.declaration_for(ACP_BACKEND_DROID)
    assert ".factory/auth.v2.file" in declaration.credential_leaves
    assert set(declaration.adapter_own_leaves) <= set(declaration.credential_leaves)
    assert declaration.home_override_env_vars == ("FACTORY_HOME_OVERRIDE",)
    assert "FACTORY_API_KEY" in declaration.sign_in_remedy
    assert declaration.host_logout_retires_children is False


def test_the_install_probe_reads_the_override(tmp_path, monkeypatch):
    from kiro_crew.acp import client as client_mod
    from kiro_crew.agent_sdk import backend_install

    binary = tmp_path / "droid"
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setattr(client_mod, "_self_served_bin_caches", {})
    backend_install.clear_probe_cache()
    monkeypatch.setenv("DROID_BIN", str(binary))
    assert backend_install.probe_backend(ACP_BACKEND_DROID).installed == backend_install.INSTALLED
    backend_install.clear_probe_cache()
    monkeypatch.setenv("DROID_BIN", str(tmp_path / "absent"))
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setattr(client_mod, "_mise_which", lambda name: None, raising=False)
    # ``augmented_path`` prepends well-known bins like ~/.local/bin, so a host with
    # the real harness installed would answer INSTALLED regardless of the empty PATH.
    monkeypatch.setattr(client_mod, "augmented_path", lambda base="", **_: base)
    missing = backend_install.probe_backend(ACP_BACKEND_DROID)
    assert missing.installed == backend_install.MISSING
    assert "app.factory.ai/cli" in missing.install_command
    backend_install.clear_probe_cache()


def test_the_spawn_arm_runs_the_enforced_preflight():
    body = inspect.getsource(AcpClient._spawn)
    arm = body[body.index("elif self._is_droid:") :]
    arm = arm[: arm.index("\n        elif ")]
    assert "_resolve_self_served_launch" in arm
    assert "_sandbox_preflight" in arm
    assert "adapter_expose_files" in arm
