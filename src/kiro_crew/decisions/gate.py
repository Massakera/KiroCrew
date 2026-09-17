"""``decide`` -- the one function a point calls, and every refusal in front of it.

Five gates in a fixed order, cheapest first, each of which returns ``None``:

1. ``decisions.preview`` off -> refuse with ZERO awaits and zero IO.
2. the point is not configured, or its ``arm`` is ``off``.
3. the session hashes outside the point's ``bucket``.
4. the state carries something that looks like a credential.
5. the implementation raised, or outran ``provider.timeout_ms``.

Gates 1-3 write nothing at all. Gates 4-5 write a log row, because both are
findings an operator needs to see (see log.py's own note on why a silent scrub
is the worst available outcome).

The order is the contract, not an optimisation
---------------------------------------------
``preview`` is first so a disabled seam costs one attribute read: not "fast",
but *provably inert*, which is what lets this land in three hot paths at once.
Concretely, ``decide`` is an ``async def`` whose body performs no ``await``
before that check, so awaiting a refused call never yields to the loop and never
reaches an implementation -- ``test_decisions_gate`` pins that with an
implementation whose ``ask`` fails the test if it is ever entered.

Scrub is last of the cheap gates and BEFORE the network, which is the whole
point of it: a credential in the state must never reach a provider, so no
transport may be constructed above it.

Why an unresolvable config means off
-----------------------------------
The config is read from the live watcher's snapshot, never from disk -- a disk
read here would be IO on the path that promises none. Before the watcher is
primed the snapshot is ``None``, and that resolves to OFF rather than to a
``load()`` fallback. Fail-closed is the only safe direction for a gate whose
open state sends conversation text to a third party, and it costs nothing real:
the snapshot is primed at boot, long before any point fires.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from hashlib import sha256
from typing import Any

from kiro_crew import credential_patterns as _cred
from kiro_crew.decisions import log as _log
from kiro_crew.decisions.oracle import DecisionOracle, OracleResult
from kiro_crew.decisions.types import Answers, Question

logger = logging.getLogger(__name__)

ARM_OFF = "off"
ARM_SHADOW = "shadow"
ARM_LIVE = "live"

IMPL_JEV = "jev"
IMPL_LLM = "llm"

#: The bucket modulus. A session's first 4 digest bytes are reduced mod this, so
#: ``bucket`` reads directly as a percentage of sessions.
_BUCKET_MOD = 100

#: Credential spellings the state is refused for. Compiled from
#: ``credential_patterns``, which exports pattern SOURCE strings rather than
#: compiled objects (it is an import leaf that does not even import ``re``), so
#: the compile has to happen at a consumer -- here, once at import.
#:
#: This is the scrubber-side AWS spelling (``AKIA``/``ASIA``), not the wider
#: redaction one. That is deliberate and is documented at the source: widening
#: it here would change what a request-blocking gate refuses, which is a
#: security behaviour change rather than a stricter-is-better tweak.
_CREDENTIAL_RE = re.compile(
    "|".join(
        [_cred.AWS_KEY_ID, _cred.JWT_MULTI_SEGMENT]
        + [frag for _label, frag in _cred.VENDOR_TOKEN_PATTERNS]
    )
)


def _snapshot() -> Any:
    """The live config snapshot, or ``None``.

    Imported inside the function, not at module scope: ``config.live`` pulls the
    whole loader in, and ``decisions`` is imported lazily from hot paths
    precisely so that it adds nothing to their import cost.
    """
    from kiro_crew.config import live

    return live.snapshot()


def _decisions_config(config: Any | None) -> Any | None:
    """The ``decisions`` section of *config*, or of the live snapshot.

    *config* is an injection seam for tests and for a caller that already holds
    a config; ``None`` means "read the snapshot". Returns ``None`` when there is
    no config at all or it predates the section, which gate 1 reads as off.
    """
    cfg = config if config is not None else _snapshot()
    if cfg is None:
        return None
    return getattr(cfg, "decisions", None)


def in_bucket(session_key: str | None, bucket: int) -> bool:
    """Whether *session_key* falls inside a *bucket*-percent sample.

    The digest is the SAME one the log's ``session`` field carries, so a row is
    enough to re-derive why it was sampled without keeping the key.

    Both ends are closed forms, not approximations: ``bucket=0`` admits nothing
    (every residue is ``>= 0``) and ``bucket=100`` admits everything (no residue
    reaches 100). A value outside 0..100 is clamped rather than rejected --
    ``arm`` is the switch, and a typo'd bucket must not become a third,
    undocumented way to disable a point.
    """
    bucket = max(0, min(_BUCKET_MOD, int(bucket)))
    digest = sha256((session_key or "").encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") % _BUCKET_MOD < bucket


def state_text(state: dict | str) -> str:
    """*state* as one string, for the credential scan.

    A ``dict`` is rendered with ``json.dumps`` rather than ``str()``: ``str()``
    on a nested structure can elide content behind a ``__repr__``, and a
    credential hidden inside an object whose repr is ``<Foo object at 0x...>``
    would pass the scan and then be serialised onto the wire by the
    implementation. Rendering the way the wire will renders what the wire sees.
    """
    if isinstance(state, str):
        return state
    import json

    try:
        return json.dumps(state, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        # Unserialisable state cannot be scanned honestly, and gate 4's job is to
        # refuse what it cannot clear -- ``repr`` here only feeds the scan, and a
        # scan that finds nothing in a repr it could not read is why the
        # implementation will hit the same failure and raise.
        return repr(state)


def has_credential(state: dict | str) -> bool:
    """Whether *state* contains anything matching a credential spelling."""
    return _CREDENTIAL_RE.search(state_text(state)) is not None


def _resolve_impl(name: str, provider: Any) -> DecisionOracle:
    """The implementation named *name*. Raises ``ValueError`` on an unknown name.

    Both imports are function-local so that arming a point on ``llm`` does not
    drag ``aiohttp`` and the vault in, and arming it on ``jev`` does not drag
    the session/dashboard side in. They are also the reason an unknown name
    raises rather than falling back: a silent fallback would send state to a
    provider the operator did not name.
    """
    if name == IMPL_JEV:
        from kiro_crew.decisions.impl_jev import JevOracle

        return JevOracle(provider)
    if name == IMPL_LLM:
        from kiro_crew.decisions.impl_llm import LlmOracle

        return LlmOracle(provider)
    raise ValueError(f"unknown decisions impl {name!r}")


async def decide(
    point: str,
    state: dict | str,
    questions: list[Question],
    *,
    session_key: str | None = None,
    baseline: dict | None = None,
    config: Any | None = None,
) -> Answers | None:
    """Ask *questions* about *state* at *point*, or return ``None``.

    ``None`` is the ONLY failure signal and it is never exceptional: every
    refusal above, every provider error, every timeout, and the whole ``shadow``
    arm all return it. A caller therefore needs no try/except and no arm check
    of its own -- ``answers = await decide(...)`` followed by ``if answers is
    None: <existing behaviour>`` is the complete integration, and it is the same
    line whether the seam is off, shadowing, or live.

    *baseline* is what the EXISTING logic concluded, passed in so the row can
    record whether the two agreed. It never influences the answer.

    *config* injects a config instead of reading the live snapshot; production
    callers leave it unset.
    """
    # ---- Gate 1: preview. No await, no IO, no import of an implementation. ----
    decisions = _decisions_config(config)
    if decisions is None or not getattr(decisions, "preview", False):
        return None

    # ---- Gate 2: point configured and armed. ----
    points = getattr(decisions, "points", None) or {}
    entry = points.get(point) if isinstance(points, dict) else None
    if entry is None:
        # Not a warning: an unconfigured point is the normal state of a point
        # that shipped after the operator's config was written, and the loader
        # already warns about names it does not recognise.
        return None
    arm = str(getattr(entry, "arm", ARM_OFF) or ARM_OFF)
    if arm == ARM_OFF:
        return None

    # ---- Gate 3: sampling. ----
    if not in_bucket(session_key, getattr(entry, "bucket", _BUCKET_MOD)):
        return None

    impl_name = str(getattr(entry, "impl", IMPL_JEV) or IMPL_JEV)
    provider = getattr(decisions, "provider", None)

    def _write(
        *,
        latency_ms: int,
        answers: Answers | None = None,
        in_tokens: int = 0,
        cost_usd: float = 0.0,
        scrubbed: bool = False,
        error: str | None = None,
    ) -> None:
        # Guarded here as well as inside ``log.append``. ``append`` protects the
        # WRITE; this protects BUILDING the row, which ``append`` never sees.
        # ``build_row`` compares the caller's ``baseline`` against the answers,
        # and a baseline is an arbitrary object supplied by a point file -- one
        # whose ``__eq__`` raises would otherwise escape ``decide`` and break the
        # very turn this seam promises never to affect.
        try:
            _log.append(
                _log.build_row(
                    point=point,
                    arm=arm,
                    impl=impl_name,
                    session_key=session_key,
                    latency_ms=latency_ms,
                    answers=answers,
                    baseline=baseline,
                    in_tokens=in_tokens,
                    cost_usd=cost_usd,
                    scrubbed=scrubbed,
                    error=error,
                )
            )
        except Exception as exc:
            logger.warning("decisions: could not record %s row: %s", point, exc)

    # ---- Gate 4: credentials. Refuse BEFORE any transport exists. ----
    if has_credential(state):
        _write(latency_ms=0, scrubbed=True, error="scrubbed")
        return None

    # ---- Gate 5: the call itself. ----
    started = time.monotonic()
    try:
        impl = _resolve_impl(impl_name, provider)
        timeout = _timeout_secs(provider)
        result = await asyncio.wait_for(impl.ask(state, questions), timeout=timeout)
    except asyncio.TimeoutError:
        # Named explicitly rather than folded into the generic branch: "timeout"
        # is the one error a report can act on mechanically (raise timeout_ms, or
        # accept the loss rate), so it must not arrive as a provider-specific
        # message that differs per implementation.
        _write(latency_ms=_elapsed_ms(started), error="timeout")
        return None
    except asyncio.CancelledError:
        # Cancellation is the CALLER going away, not a decision failure. Writing
        # a row would attribute the caller's shutdown to the provider, and
        # swallowing it would break structured concurrency, so: no row, re-raise.
        raise
    except Exception as exc:
        _write(latency_ms=_elapsed_ms(started), error=f"{type(exc).__name__}: {exc}"[:300])
        return None

    latency_ms = _elapsed_ms(started)
    answers = result.answers if isinstance(result, OracleResult) else None
    if not answers:
        # An implementation that returns an empty result broke its own contract
        # (oracle.py: raise, never return empty). Record it as an error rather
        # than as a successful decision with nothing in it, so the row cannot be
        # read as agreement.
        _write(latency_ms=latency_ms, error="empty result")
        return None

    _write(
        latency_ms=latency_ms,
        answers=answers,
        in_tokens=result.in_tokens,
        cost_usd=result.cost_usd,
    )

    if arm == ARM_LIVE:
        return answers
    # ``shadow`` -- and any arm value the loader let through that is neither
    # ``off`` nor ``live``. Returning None for an unrecognised arm keeps the
    # unknown case on the observe-only side of the switch.
    return None


def _timeout_secs(provider: Any) -> float:
    """``provider.timeout_ms`` in seconds, floored so it is always a real budget.

    A zero or negative ``timeout_ms`` would make ``wait_for`` cancel immediately
    and turn every row into ``error: timeout`` -- a config typo that looks like a
    broken provider. The floor makes it look like a fast timeout instead.
    """
    raw = getattr(provider, "timeout_ms", 1000) if provider is not None else 1000
    try:
        ms = float(raw)
    except (TypeError, ValueError):
        ms = 1000.0
    return max(0.001, ms / 1000.0)


def _elapsed_ms(started: float) -> int:
    """Whole milliseconds since *started* (a ``time.monotonic()`` reading)."""
    return int((time.monotonic() - started) * 1000)
