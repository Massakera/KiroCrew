"""A host with only codex and/or droid must not need kiro-cli.

The kiro readiness gate, the credit-usage scrape and the model list all used to
reach kiro-cli whatever ``agent.acp_backend`` said. They now ask the same
backend-selection gate the provider factory uses, and only a session that runs
on kiro-cli (kiro, KAS) is held to the kiro prerequisite.
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew import cli_setup
from kiro_crew.agent_sdk import backends as sdk_backends
from kiro_crew.agent_sdk.backend_install import INSTALLED, MISSING, BackendInstallState
from kiro_crew.config import KiroCrewConfig
from kiro_crew.config.loader import config_path, update_config_locked
from kiro_crew.dashboard import kiro_readiness
from kiro_crew.dashboard.handlers import agents, sessions
from kiro_crew.env import sanitize_spec_env

_RESOLVE_TARGET = "kiro_crew.acp.client._resolve_kiro_bin_for_spawn"


@pytest.fixture(autouse=True)
def _reset_refusal_warning():
    kiro_readiness._clear_refusal_warning()
    yield
    kiro_readiness._clear_refusal_warning()


@pytest.fixture
def restore_registry():
    baseline, selectable = set(sdk_backends._baseline), set(sdk_backends._selectable)
    yield
    sdk_backends._baseline.clear()
    sdk_backends._baseline.update(baseline)
    sdk_backends._selectable.clear()
    sdk_backends._selectable.update(selectable)


def _configure(**agent: str) -> None:
    def _apply(data: dict) -> dict:
        data.setdefault("agent", {}).update(agent)
        return data

    update_config_locked(config_path(), mutate=_apply, stamp_meta=False)


def _unready_request() -> MagicMock:
    """A request whose prerequisite service is absent, which fails closed."""
    request = MagicMock()
    request.app = {
        "kiro_prerequisite_service": None,
        "state": SimpleNamespace(kiro_prerequisite_service=None, _background_tasks=set()),
    }
    request.path = "/api/test"
    return request


# ── backend_needs_kiro_cli ──


def test_the_shipped_default_needs_kiro_cli():
    assert kiro_readiness.backend_needs_kiro_cli() is True


def test_a_codex_default_does_not_need_kiro_cli():
    _configure(acp_backend="codex", member_acp_backend="codex")
    assert kiro_readiness.backend_needs_kiro_cli() is False
    assert kiro_readiness.backend_needs_kiro_cli("member-alex") is False


def test_a_member_slot_on_kas_still_needs_kiro_cli():
    _configure(acp_backend="codex", member_acp_backend="kas")
    assert kiro_readiness.backend_needs_kiro_cli("chat-1") is False
    assert kiro_readiness.backend_needs_kiro_cli("member-alex") is True


def test_an_unreadable_config_keeps_the_gate_on(monkeypatch):
    def _boom(*_a, **_k):
        raise RuntimeError("corrupt")

    monkeypatch.setattr(KiroCrewConfig, "load", staticmethod(_boom))
    assert kiro_readiness.backend_needs_kiro_cli() is True


# ── the gate itself ──


@pytest.mark.asyncio
async def test_the_gate_lets_a_codex_host_through_without_kiro():
    _configure(acp_backend="codex", member_acp_backend="codex")
    assert await kiro_readiness.reject_if_kiro_unverified(_unready_request()) is None


@pytest.mark.asyncio
async def test_the_gate_still_refuses_a_kiro_host():
    resp = await kiro_readiness.reject_if_kiro_unverified(_unready_request())
    assert resp is not None and resp.status == 503


@pytest.mark.asyncio
async def test_a_rerun_on_a_kas_member_slot_is_still_gated():
    _configure(acp_backend="codex", member_acp_backend="kas")
    request = _unready_request()
    assert await kiro_readiness.reject_if_kiro_unverified(request, session_key="chat-1") is None
    resp = await kiro_readiness.reject_if_kiro_unverified(request, session_key="member-alex")
    assert resp is not None and resp.status == 503


# ── usage and models ──


@pytest.mark.asyncio
async def test_usage_reports_unavailable_without_scraping_on_codex(monkeypatch):
    _configure(acp_backend="codex", member_acp_backend="codex")
    monkeypatch.setattr(sessions, "_usage_cache_ts", 0.0)
    with patch.object(sessions, "_fetch_usage_bg", AsyncMock()) as fetch:
        resp = await sessions.api_sessions_usage(_unready_request())
    fetch.assert_not_called()
    assert resp.status == 200
    assert json.loads(resp.body) == {
        "usage": {"available": False, "reason": "non_kiro_backend"}
    }


@pytest.mark.asyncio
async def test_droid_models_never_resolve_kiro_cli(restore_registry):
    sdk_backends.opt_in_experimental_backends("droid")
    _configure(acp_backend="droid")
    with patch(_RESOLVE_TARGET, AsyncMock(return_value="/usr/bin/kiro-cli")) as resolve:
        resp = await agents.api_models(_unready_request())
    resolve.assert_not_called()
    assert resp.status == 200
    assert json.loads(resp.body)[0]["model_name"] == "auto"


# ── first-run gate status ──


async def _status_body(tmp_path) -> dict:
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from kiro_crew.dashboard.handlers.kiro_prerequisite import api_kiro_prerequisite_status
    from kiro_crew.kiro_prerequisite import KiroPrerequisiteService

    service = KiroPrerequisiteService(
        platform_name="linux",
        environ={"HOME": str(tmp_path), "PATH": ""},
        home=tmp_path,
        audit_writer=lambda *_a, **_k: None,
    )

    async def not_ready(*, force: bool = False, coalesce: bool = False) -> dict:
        del force, coalesce
        return {"installed": False, "authenticated": False, "ready": False}

    service.snapshot = not_ready  # type: ignore[method-assign]

    @web.middleware
    async def identity(request, handler):
        request["user"] = "owner"
        request["app"] = ""
        return await handler(request)

    app = web.Application(middlewares=[identity])
    app["state"] = SimpleNamespace(owner_id="owner")
    app["kiro_prerequisite_service"] = service
    app.router.add_get("/api/kiro-prerequisite", api_kiro_prerequisite_status)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/api/kiro-prerequisite")
        assert resp.status == 200
        return await resp.json()


@pytest.mark.asyncio
async def test_a_codex_default_is_not_held_at_the_kiro_setup_screen(tmp_path):
    _configure(acp_backend="codex")
    body = await _status_body(tmp_path)
    assert body["ready"] is True
    assert body["kiro_cli_required"] is False


@pytest.mark.asyncio
async def test_a_kiro_default_still_reports_the_probe(tmp_path):
    body = await _status_body(tmp_path)
    assert body["ready"] is False
    assert "kiro_cli_required" not in body


# ── KIROCREW_HOME pin ──


def test_the_gateways_own_home_pin_drops_quietly(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    with caplog.at_level(logging.WARNING, logger="kiro_crew.env"):
        assert sanitize_spec_env([("KIROCREW_HOME", str(tmp_path))]) == {}
    assert "KIROCREW_HOME" not in caplog.text


def test_a_foreign_home_value_still_warns(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    with caplog.at_level(logging.WARNING, logger="kiro_crew.env"):
        assert sanitize_spec_env([("KIROCREW_HOME", str(tmp_path / "other"))]) == {}
    assert "namespace is reserved" in caplog.text


# ── setup offers an installed harness ──


def _probe(installed: dict[str, str]):
    return lambda backend: BackendInstallState(backend, backend, installed.get(backend, MISSING))


def test_setup_prints_the_switch_when_not_interactive(monkeypatch, capsys):
    monkeypatch.setattr(
        "kiro_crew.agent_sdk.backend_install.probe_backend", _probe({"codex": INSTALLED})
    )
    monkeypatch.setattr(cli_setup, "_stdio_is_interactive", lambda: False)
    cli_setup._offer_non_kiro_default_backend()
    out = capsys.readouterr().out
    assert "kirocrew config set agent.acp_backend codex" in out
    assert "kirocrew config set agent.member_acp_backend codex" in out
    assert KiroCrewConfig.load().agent.acp_backend == ""


def test_setup_switches_both_backends_on_yes(monkeypatch):
    monkeypatch.setattr(
        "kiro_crew.agent_sdk.backend_install.probe_backend", _probe({"codex": INSTALLED})
    )
    monkeypatch.setattr(cli_setup, "_stdio_is_interactive", lambda: True)
    monkeypatch.setattr(cli_setup, "_input_or_skip", lambda _prompt: None)
    cli_setup._offer_non_kiro_default_backend()
    agent_cfg = KiroCrewConfig.load().agent
    assert (agent_cfg.acp_backend, agent_cfg.member_acp_backend) == ("codex", "codex")


def test_setup_offers_nothing_without_an_installed_harness(monkeypatch, capsys):
    monkeypatch.setattr("kiro_crew.agent_sdk.backend_install.probe_backend", _probe({}))
    monkeypatch.setattr(cli_setup, "_stdio_is_interactive", lambda: True)
    cli_setup._offer_non_kiro_default_backend()
    assert capsys.readouterr().out == ""


def test_setup_skips_droid_without_the_opt_in(monkeypatch, capsys, restore_registry):
    if "droid" in sdk_backends.selectable_backends():
        pytest.skip("droid opted in by the environment")
    monkeypatch.setattr(
        "kiro_crew.agent_sdk.backend_install.probe_backend", _probe({"droid": INSTALLED})
    )
    monkeypatch.setattr(cli_setup, "_stdio_is_interactive", lambda: False)
    cli_setup._offer_non_kiro_default_backend()
    assert capsys.readouterr().out == ""
