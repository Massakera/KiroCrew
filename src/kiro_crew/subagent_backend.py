"""Per-spawn ACP backend choice for sub-agents.

An orchestrating session may run each sub-agent on a different harness
(``spawn_run(backends=[...])``). The choice travels as a WIRE name -- the same
identifier a governance rule uses (``"kiro"``, ``"codex"``, ``"claude"``...) --
because the kiro backend's internal id is the empty string, which cannot be told
apart from "not given" once it is stored.

What lives here is the vocabulary and the refusals. The backend a session
actually gets is still decided by the one selection gate,
:func:`kiro_crew.members.select_provider_backend`, which receives the override
as its highest-precedence input (harness-parity H3/H13).
"""

from __future__ import annotations

from dataclasses import dataclass

from kiro_crew.agent_sdk.backends import (
    ACP_BACKENDS_KNOWN,
    POLICY_ID_BY_BACKEND,
    selectable_backends,
)

UNKNOWN_BACKEND_CODE = "unknown_backend"
BACKEND_UNAVAILABLE_CODE = "backend_unavailable"
BACKEND_MISMATCH_CODE = "backend_mismatch"

_BACKEND_BY_NAME: dict[str, str] = {name: backend for backend, name in POLICY_ID_BY_BACKEND.items()}


@dataclass(frozen=True)
class SpawnBackendRefusal:
    code: str
    error: str


def backend_name(backend: str) -> str:
    """The wire name for an internal backend id (``""`` -> ``"kiro"``)."""
    return str(POLICY_ID_BY_BACKEND.get(backend, backend))


def backend_from_name(name: str) -> str | None:
    """The internal backend id for a wire name, or None when no build spells it."""
    backend = _BACKEND_BY_NAME.get(name.strip().lower())
    if backend is None or backend not in ACP_BACKENDS_KNOWN:
        return None
    return backend


def selectable_backend_names() -> list[str]:
    """Wire names of every backend this deployment may select right now."""
    return sorted(backend_name(b) for b in selectable_backends())


def parent_backend_name(state: object, parent_session: str) -> str:
    """Wire name of the harness the parent's live session runs on, ``""`` if unknown."""
    if not parent_session:
        return ""
    try:
        provider = state.sessions.get_provider(parent_session)  # type: ignore[attr-defined]
        backend = getattr(getattr(provider, "_client", None), "backend", None)
    except Exception:  # noqa: BLE001 - identity check is best-effort
        return ""
    return backend_name(backend) if isinstance(backend, str) else ""


def check_spawn_backend(name: str) -> SpawnBackendRefusal | None:
    """Refuse a requested backend this deployment cannot select.

    An explicit request is REFUSED rather than degraded to the default the way a
    persisted ``agent.acp_backend`` is: a caller that asked for codex and silently
    got kiro would compare two runs that were never on different harnesses.
    """
    backend = backend_from_name(name)
    if backend is None or backend not in selectable_backends():
        return SpawnBackendRefusal(
            UNKNOWN_BACKEND_CODE,
            f"Unknown or unselectable backend {name!r}. "
            f"Selectable backends: {', '.join(selectable_backend_names())}.",
        )
    return None


def check_backend_installed(name: str) -> SpawnBackendRefusal | None:
    """Refuse a backend whose harness is definitely not installed on this host.

    Blocking (the probe may shell out); event-loop callers must offload it. An
    UNKNOWN verdict is not a refusal: the probe failing to decide is not evidence
    the harness is absent, and the session start reports the real failure.
    """
    from kiro_crew.agent_sdk.backend_install import MISSING, probe_backend

    backend = backend_from_name(name)
    if backend is None:
        return None
    state = probe_backend(backend)
    if state.installed != MISSING:
        return None
    missing = ", ".join(state.missing_components) or backend_name(backend)
    hint = f" Install it with: {state.install_command}" if state.install_command else ""
    return SpawnBackendRefusal(
        BACKEND_UNAVAILABLE_CODE,
        f"Backend {backend_name(backend)!r} is not installed on this host "
        f"(missing: {missing}).{hint}",
    )
