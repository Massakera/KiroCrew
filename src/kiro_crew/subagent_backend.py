"""Per-spawn ACP backend choice for sub-agents.

An orchestrating session may run each sub-agent on a different harness
(``spawn_run(backends=[...])``). The choice travels as a WIRE name -- the same
identifier a governance rule uses (``"kiro"``, ``"codex"``, ``"claude"``...) --
because the kiro backend's internal id is the empty string, which cannot be told
apart from "not given" once it is stored.

What lives here is the vocabulary, the refusals, and the rate-limit policy on
top of them: a per-backend concurrency cap (``agent.subagent_backend_limits``) and
failover of a rate-limited run to the next backend in
``agent.subagent_backend_fallback``. The backend a session actually gets is still
decided by the one selection gate, :func:`kiro_crew.members.select_provider_backend`,
which receives the override as its highest-precedence input (harness-parity
H3/H13). Backoff WITHIN a backend is upstream's: the in-turn transient ladder and
the dependency coordinator's schedule for a typed provider throttle run first, and
failover only sees a run they gave up on.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from kiro_crew.agent_sdk.backends import (
    ACP_BACKENDS_KNOWN,
    POLICY_ID_BY_BACKEND,
    selectable_backends,
)

logger = logging.getLogger(__name__)

UNKNOWN_BACKEND_CODE = "unknown_backend"
BACKEND_UNAVAILABLE_CODE = "backend_unavailable"

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


# ── Rate limits: cooldown, failover, per-backend cap ──

#: What a harness says when a provider refused the turn for quota or rate. Codex
#: reports "usage limit" / 429, Droid a ``429 status code`` or a
#: ``model_rate_limited`` turn outcome, Claude "rate limit" / "overloaded".
_RATE_LIMIT_RE = re.compile(
    r"\b429\b|rate[\s_-]?limit|too many requests|usage[\s_-]?limit|quota|"
    r"resource[\s_-]?exhausted|model_rate_limited|model_usage_exhausted|overloaded",
    re.IGNORECASE,
)
#: The subset that will not clear within a turn's backoff: a spent plan window.
_EXHAUSTED_RE = re.compile(
    r"usage[\s_-]?limit|quota|model_usage_exhausted|resource[\s_-]?exhausted", re.IGNORECASE
)

#: How long a backend that refused a sub-agent for rate is skipped by failover.
#: A spent usage window is hours; a 429 burst is seconds -- long enough not to pick
#: the backend straight back, short enough that it rejoins the chain soon.
EXHAUSTED_COOLDOWN_SECS = 15 * 60.0
RATE_LIMITED_COOLDOWN_SECS = 2 * 60.0

_cooldown_until: dict[str, float] = {}
_limits_cache: tuple[float, dict[str, int], str] = (0.0, {}, "")
_LIMITS_TTL_SECS = 2.0


def is_rate_limited_error(text: str) -> bool:
    """Whether *text* reports a provider rate or usage limit."""
    return bool(text) and bool(_RATE_LIMIT_RE.search(text))


def cool_down(name: str, error_text: str, *, now: float | None = None) -> float:
    """Keep failover off *name* for a while. Returns the cooldown applied."""
    seconds = (
        EXHAUSTED_COOLDOWN_SECS if _EXHAUSTED_RE.search(error_text) else RATE_LIMITED_COOLDOWN_SECS
    )
    start = time.monotonic() if now is None else now
    _cooldown_until[name] = max(_cooldown_until.get(name, 0.0), start + seconds)
    return seconds


def in_cooldown(name: str, *, now: float | None = None) -> bool:
    return _cooldown_until.get(name, 0.0) > (time.monotonic() if now is None else now)


def clear_cooldowns() -> None:
    _cooldown_until.clear()


def pick_fallback(current: str, chain: list[str]) -> str | None:
    """The first backend in *chain* after *current* that can take the task now.

    Walks the chain from just past *current* (from the start when *current* is not
    in it) and wraps once, skipping *current*, anything cooling down, anything this
    deployment cannot select, and anything its install probe reports missing.
    Blocking (the probe); event-loop callers offload it.
    """
    start = chain.index(current) + 1 if current in chain else 0
    for name in chain[start:] + chain[:start]:
        if name == current or in_cooldown(name):
            continue
        if check_spawn_backend(name) is not None or check_backend_installed(name) is not None:
            continue
        return name
    return None


def _limits_and_default() -> tuple[dict[str, int], str]:
    global _limits_cache
    stamp, limits, default = _limits_cache
    now = time.monotonic()
    if now - stamp < _LIMITS_TTL_SECS:
        return limits, default
    try:
        from kiro_crew.config.loader import KiroCrewConfig

        agent = KiroCrewConfig.load().agent
        limits, default = dict(agent.subagent_backend_limits), backend_name(agent.acp_backend)
    except Exception:
        limits, default = {}, "kiro"
    _limits_cache = (now, limits, default)
    return limits, default


def reset_limits_cache() -> None:
    global _limits_cache
    _limits_cache = (0.0, {}, "")


def backend_at_cap(agents: Iterable[Any], requested: str) -> bool:
    """Whether a sub-agent asking for *requested* (``""`` = default) must wait.

    Counts the runs that have started and not finished on the same effective
    backend against ``agent.subagent_backend_limits``; a backend with no entry is
    never at cap here, so the global cap alone applies to it.
    """
    limits, default = _limits_and_default()
    if not limits:
        return False
    name = requested or default
    limit = limits.get(name)
    if limit is None:
        return False
    running = sum(
        1
        for info in agents
        if not getattr(info, "done", True)
        and not getattr(info, "queued", False)
        and ((getattr(info, "acp_backend", "") or default) == name)
    )
    return running >= limit


async def fail_over(manager: Any, info: Any) -> str | None:
    """Re-dispatch a rate-limited sub-agent to the next fallback backend.

    Only a run that failed on a rate or usage limit BEFORE it ran any tool is
    eligible -- replaying one that already wrote a file or ran a command on another
    harness would do it twice. Continuations (the conversation belongs to its
    harness) and wave members (the wave's accounting counts this run) are left to
    report their failure as they are. The replacement is an ordinary spawn of the
    same task with the same scope; the failed run's own completion names it, so the
    parent waits for one more completion instead of retrying by hand.

    Returns the replacement's id, or None when nothing was dispatched.
    """
    error = str(getattr(info, "error", "") or "")
    if (
        not error
        or getattr(info, "user_stopped", False)
        or getattr(info, "reaped", False)
        or getattr(info, "conversation_key", "")
        or getattr(info, "keep", False)
        or getattr(info, "batch_id", "")
        or getattr(info, "tool_count", 0)
        or getattr(info, "_failover_checked", False)
        or not is_rate_limited_error(error)
    ):
        return None
    info._failover_checked = True
    from kiro_crew.config.loader import KiroCrewConfig

    try:
        agent_cfg = (await asyncio.to_thread(KiroCrewConfig.load)).agent
    except Exception:
        logger.debug("failover: config unavailable for %s", info.id, exc_info=True)
        return None
    chain = list(agent_cfg.subagent_backend_fallback)
    if not chain:
        return None
    current = info.acp_backend or backend_name(agent_cfg.acp_backend)
    cool_down(current, error)
    target = await asyncio.to_thread(pick_fallback, current, chain)
    if target is None:
        logger.warning(
            "subagent %s: backend %r hit a rate/usage limit and no fallback in %s is available",
            info.id,
            current,
            chain,
        )
        return None
    execution = getattr(info, "execution_context", None)
    replacement = manager.spawn(
        getattr(info, "_raw_task", "") or info.task,
        parent_session_key=info.parent_session_key,
        agent=info.agent,
        max_turns=info.max_turns,
        cwd=info.cwd,
        model=info.model or None,
        reasoning_effort=info.reasoning_effort,
        approval_mode=info.approval_mode or None,
        silent=info.silent,
        delegation=dict(info.delegation or {}),
        include_memory=info.include_memory,
        include_lessons=info.include_lessons,
        include_project=info.include_project,
        memory_store=info.memory_store,
        crew=info.crew,
        app=info.app,
        _memory_mode=info.memory_mode,
        _execution_context=execution.to_record() if execution is not None else None,
        acp_backend=target,
    )
    refused = "capacity" if replacement is None else str(getattr(replacement, "error", None) or "")
    if refused:
        logger.warning(
            "subagent %s: failover to %r was not accepted (%s)", info.id, target, refused
        )
        return None
    info.error = (
        f"{error} [failed over: backend '{current}' hit a rate/usage limit, so the task "
        f"was re-dispatched to backend '{target}' as subagent `{replacement.id}`; its "
        "result arrives as its own completion event]"
    )
    logger.info(
        "subagent %s: failed over from %r to %r as %s", info.id, current, target, replacement.id
    )
    return str(replacement.id)
