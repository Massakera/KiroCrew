"""End-to-end: real Codex and Factory Droid sessions, built the way a sub-agent is.

Spends real model tokens, so nothing here runs unless ``KIROCREW_LIVE_BACKEND_E2E=1``.
Past that opt-in every precondition -- the harness binary, a credential -- is a
FAILURE rather than a skip: the opt-in declares that this host can run them, and a
skip would report success having measured nothing. Deliberately not marked
``real_adapter``, for the reason ``test_codex_session_mcp`` states: the contract
lane holds no model credential.

Credentials are read by the harnesses themselves and never by this module: a
subscription login (``~/.codex/auth.json``, ``~/.factory/auth.v2.file``) or an API
key in the environment (``OPENAI_API_KEY`` / ``CODEX_API_KEY``, ``FACTORY_API_KEY``).
Nothing here prints, logs or asserts on a credential value.

Run on a signed-in host::

    KIROCREW_LIVE_BACKEND_E2E=1 .venv/bin/python -m pytest -q -n 0 \\
        test/test_e2e_subagent_backends.py
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from kiro_crew.agent_sdk import backends as sdk_backends
from kiro_crew.agent_sdk.backend_install import INSTALLED, clear_probe_cache, probe_backend
from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK

OPT_IN_ENV = "KIROCREW_LIVE_BACKEND_E2E"
PROMPT = "Reply with exactly one word, pong, and nothing else. Do not use any tool."

pytestmark = pytest.mark.skipif(
    os.environ.get(OPT_IN_ENV) != "1",
    reason=f"spends real model tokens; set {OPT_IN_ENV}=1 on a signed-in host to opt in",
)


def _has_credential(env_vars: tuple[str, ...], files: tuple[Path, ...]) -> bool:
    return any(os.environ.get(v) for v in env_vars) or any(f.is_file() for f in files)


def _require(backend: str, env_vars: tuple[str, ...], files: tuple[Path, ...]) -> None:
    clear_probe_cache()
    state = probe_backend(backend)
    if state.installed != INSTALLED:
        pytest.fail(
            f"{backend} is not installed ({', '.join(state.missing_components)}); "
            f"install it with: {state.install_command}"
        )
    if not _has_credential(env_vars, files):
        pytest.fail(
            f"no {backend} credential: sign in with its own CLI, or set one of "
            f"{', '.join(env_vars)} in this environment"
        )


@pytest.fixture
def droid_opted_in():
    baseline, selectable = set(sdk_backends._baseline), set(sdk_backends._selectable)
    sdk_backends.opt_in_experimental_backends("droid")
    yield
    sdk_backends._baseline.clear()
    sdk_backends._baseline.update(baseline)
    sdk_backends._selectable.clear()
    sdk_backends._selectable.update(selectable)


async def _one_turn(backend: str, work_dir: Path) -> str:
    """Build the provider exactly as a sub-agent on *backend* gets it, and prompt once."""
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.subagent_backend import backend_from_name

    factory = KiroCrewConfig.load().create_provider_factory()
    provider = factory(
        f"subagent:e2e-{backend}",
        cwd=str(work_dir),
        acp_backend_override=backend_from_name(backend),
    )
    text: list[str] = []
    try:
        await asyncio.wait_for(provider.start(), timeout=120)
        async for event in provider.stream(PROMPT):
            if event.kind == EVENT_TEXT_CHUNK:
                text.append(event.text or "")
            elif event.kind == EVENT_COMPLETE:
                break
    except Exception as exc:  # noqa: BLE001 - reported as the test's failure
        pytest.fail(f"{backend} session failed: {type(exc).__name__}: {exc}")
    finally:
        await provider.shutdown()
    return "".join(text)


CODEX_CREDENTIALS = (
    ("CODEX_API_KEY", "OPENAI_API_KEY"),
    (Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "auth.json",),
)
DROID_CREDENTIALS = (
    ("FACTORY_API_KEY",),
    (Path(os.environ.get("FACTORY_HOME_OVERRIDE") or Path.home()) / ".factory" / "auth.v2.file",),
)


def test_a_codex_subagent_session_answers(tmp_path):
    _require("codex", *CODEX_CREDENTIALS)
    reply = asyncio.run(_one_turn("codex", tmp_path))
    assert "pong" in reply.lower(), reply[:200]


def test_a_droid_subagent_session_answers(tmp_path, droid_opted_in):
    _require("droid", *DROID_CREDENTIALS)
    reply = asyncio.run(_one_turn("droid", tmp_path))
    assert "pong" in reply.lower(), reply[:200]


def test_codex_and_droid_answer_in_parallel(tmp_path, droid_opted_in):
    _require("codex", *CODEX_CREDENTIALS)
    _require("droid", *DROID_CREDENTIALS)
    (tmp_path / "codex").mkdir()
    (tmp_path / "droid").mkdir()

    async def both() -> list[str]:
        return list(
            await asyncio.gather(
                _one_turn("codex", tmp_path / "codex"), _one_turn("droid", tmp_path / "droid")
            )
        )

    for backend, reply in zip(("codex", "droid"), asyncio.run(both())):
        assert "pong" in reply.lower(), (backend, reply[:200])
