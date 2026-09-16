"""W01 · L09: the executor that wires the four control-plane judgments together.

L01..L08 delivered four decision primitives, but nothing yet CALLS them in the
one order a real dispatch must. This module is that caller. It is the seam every
provider stream (W02 GitHub, W05 Graph, ...) routes an operation through, and it
enforces -- BEFORE any transport call is emitted -- the full gate chain:

    1. trusted handle view   :func:`~...control_plane.handle.ensure_usable`
    2. credential-mode permit :func:`~...control_plane.auth_modes.permit_operation`
    3. governance intersection :func:`~...control_plane.policy.decide`
    4. write-replay gate       :func:`~...control_plane.writes.replay_decision`

Any one of these rejecting returns a typed :class:`OperationError` and the
transport is NEVER touched -- "emit first, decide after" would make all four
upstream slices dead code, so the order is load-bearing and tested by asserting
the fake transport's call count stays at zero on every deny path.

**Routing trusts the view, never the handle.** ``service_id`` and
``credential_mode`` are read ONLY off the :class:`TrustedHandleView` that
:func:`ensure_usable` returns from its issuance record -- never off the mutable
handle dict. Reading them off the handle would be the "validate then use the
unvalidated value" hole L08 exists to close; a test proves that mutating the
handle's ``service_id`` does not change where the executor routes.

**Server clock, not the caller's word.** Expiry and precondition judgments never
trust a non-finite ``now``: :func:`ensure_usable` already refuses a ``NaN`` /
``inf`` clock (L08 turned a car over on exactly that), and this module refuses a
non-finite ``now`` up front too, so a bad clock cannot slip an expired handle or
a stale precondition through here.

**HTTP 412 is preserved, not flattened.** A precondition-failed response
(``If-Match`` / ``If-Unmodified-Since`` ETag mismatch) is NOT collapsed into a
generic ``conflict``: the executor returns a structured
:class:`PreconditionFailure` carrying the failed preconditions and the server's
current ETag, so the CALLER can decide to re-read and re-derive rather than blind
retry. It maps the write's recorded outcome to L07's ``failed_not_applied`` (the
write provably did not land), which is the branch ``replay_decision`` *allows* --
but "allowed to replay" is NOT "safe to replay as-is": a 412 tells you only that
the precondition you asserted is false, never the server's current state. The
correct shape is 412 -> preserve the structured signal -> readback + re-derive
the precondition -> only then retry. The executor deliberately does NOT
auto-reissue; it hands the structured signal back and stops.

**Boundaries.** Pure decision + dispatch glue, no real network. The transport is
an injected callable (an in-memory fake in tests). MS's own 412 / readback /
baseline / fresh / locator revalidation semantics live under
``vendors/microsoft/**`` and are that owner's -- this module only preserves the
shared structured signal MS consumes; it changes nothing there. It also does not
claim to cover document- / provider-level query ACL: L06's five-layer
intersection and the handle scope narrowing do NOT substitute for that
(a separately-owned gap), and this module makes no such claim.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional, Tuple

from kiro_crew.connections.control_plane.auth_modes import (
    PermittedModes,
    permit_operation,
)
from kiro_crew.connections.control_plane.errors import (
    ErrorClass,
    OperationError,
    operation_error,
)
from kiro_crew.connections.control_plane.handle import (
    DerivedHandle,
    HandleExpiredError,
    HandleNotIssuedError,
    HandleScopeError,
    HandleTamperedError,
    TrustedHandleView,
    ensure_usable,
)
from kiro_crew.connections.control_plane.operation import (
    CredentialMode,
    OperationDescriptor,
)
from kiro_crew.connections.control_plane.policy import LayerCeilings, decide
from kiro_crew.connections.control_plane.result import OperationResult
from kiro_crew.connections.control_plane.writes import (
    ATTEMPT_FAILED_NOT_APPLIED,
    REPLAY_REFUSE,
    REPLAY_REUSE,
    AttemptRecord,
    ReplayDecision,
    replay_decision,
)

#: Bumped when this module's OUTER interface shape changes -- the executor
#: request/response envelope, the transport-callable contract, or the paging /
#: error-classification surface. Two downstreams (W02 GitHub, W05 Graph) encode
#: against these shapes, so they pin this number; a shape change that would make
#: an old pin decode wrong MUST bump it.
EXECUTOR_SCHEMA_VERSION = 1

# --- effects that are non-idempotent by default (the write-replay gate runs) --
#: Effects whose ``unknown``-outcome replay must be gated by L07. Read from the
#: descriptor's ``effect`` -- never inferred from an operation's name. ``read``
#: and ``delete`` are naturally idempotent and are not gated here (L07 itself
#: still refuses to widen). Mirrors L07's own non-idempotent set.
_NON_IDEMPOTENT_EFFECTS = frozenset({"write", "share", "external_send", "admin", "billable"})


# --- the structured transport outcome the injected transport returns ----------
@dataclass(frozen=True)
class TransportResponse:
    """What the injected transport hands back for ONE emitted call.

    The transport is the only thing that touches a network (a real HTTP client
    in production, an in-memory fake in tests). It returns this structured
    envelope rather than raising, so the executor classifies uniformly.

    ``http_status`` -- the HTTP status the provider returned (e.g. 200, 404,
    412, 429, 503). ``result`` -- the success envelope on a 2xx (with
    ``next_cursor`` for paging), else ``None``. ``preconditions`` -- the
    precondition names that failed on a 412 (``("If-Match",)`` etc.), empty
    otherwise. ``etag`` -- the server's CURRENT ETag on a 412 (what a readback
    would re-derive against), else ``None``. ``retry_after_seconds`` -- the
    server's advisory backoff on a 429/503, else ``None``. ``detail`` -- a short
    provider message; it is redacted by :func:`operation_error` before it ever
    leaves the executor.
    """

    http_status: int
    result: Optional[OperationResult] = None
    preconditions: Tuple[str, ...] = ()
    etag: Optional[str] = None
    retry_after_seconds: Optional[float] = None
    detail: str = ""


# --- the structured 412 signal the caller consumes (NOT flattened) ------------
@dataclass(frozen=True)
class PreconditionFailure:
    """A structured HTTP 412 signal -- preserved, never collapsed to ``conflict``.

    A 412 means the precondition the caller ASSERTED (an ``If-Match`` ETag, an
    ``If-Unmodified-Since``) does not hold, so the write did NOT apply. The
    caller must be able to tell this apart from a generic error and get enough
    context to decide "re-read and re-derive the precondition, then retry"
    rather than blind-retry.

    IMPORTANT: a 412 does NOT tell you the server's current state -- only that
    your asserted precondition is false. ``failed_not_applied`` here records that
    the write did not land (the branch L07 *allows* replaying); it is NOT a
    licence to reissue the identical request. Re-read (readback), re-derive the
    precondition against ``server_etag``, and only THEN retry. MS owns its own
    baseline / fresh / locator revalidation on top of this signal.

    ``preconditions`` -- the failed precondition names. ``server_etag`` -- the
    provider's current ETag to re-derive against (``None`` if the provider sent
    none). ``recorded_outcome`` -- always ``failed_not_applied``. ``error`` -- the
    typed L01 ``conflict`` :class:`OperationError` (redacted detail) for a caller
    that only wants the flat class; the structured fields above are what makes the
    signal discriminable.
    """

    preconditions: Tuple[str, ...]
    server_etag: Optional[str]
    error: OperationError
    recorded_outcome: str = ATTEMPT_FAILED_NOT_APPLIED


# --- the executor's own result envelope ---------------------------------------
@dataclass(frozen=True)
class ExecutionOutcome:
    """The executor's uniform return for one ``execute`` / ``advance_page`` call.

    Exactly one of ``result`` / ``error`` / ``precondition`` is set.

    ``result`` -- the success envelope (with ``next_cursor`` for paging) when the
    call was authorized, emitted, and returned 2xx. ``error`` -- a typed
    :class:`OperationError` when a gate denied (transport NOT called) or the
    transport returned a non-precondition failure. ``precondition`` -- the
    structured :class:`PreconditionFailure` on a 412. ``view`` -- the trusted
    :class:`TrustedHandleView` the routing decision used, present whenever the
    gate chain got far enough to resolve it (so a caller/audit can see the
    trusted axes); ``None`` when the handle itself was rejected.
    """

    result: Optional[OperationResult] = None
    error: Optional[OperationError] = None
    precondition: Optional[PreconditionFailure] = None
    view: Optional[TrustedHandleView] = None

    @property
    def ok(self) -> bool:
        return self.result is not None and self.error is None and self.precondition is None


# --- the injected transport contract ------------------------------------------
#: A transport takes the trusted routing axes plus the request and returns a
#: :class:`TransportResponse`. It is called ONLY after every gate has passed. The
#: executor passes the TRUSTED ``service_id`` / ``credential_mode`` from the
#: handle view -- never the handle's own -- so the thing that reaches the network
#: cannot be pointed elsewhere by a mutated handle.
Transport = Callable[..., TransportResponse]


def _finite(now: float) -> bool:
    return isinstance(now, (int, float)) and math.isfinite(now)


def _classify_status(status: int, detail: str) -> Optional[ErrorClass]:
    """Map an HTTP status to an L01 :class:`ErrorClass`, or ``None`` for 2xx.

    Pure, table-driven. 412 is deliberately NOT in this table: a precondition
    failure is handled as a structured :class:`PreconditionFailure`, not a flat
    error class, so it is never collapsed to ``conflict`` here.
    """

    if 200 <= status < 300:
        return None
    return {
        400: "input",
        401: "auth",
        403: "forbidden",
        404: "not_found",
        409: "conflict",
        429: "throttle",
    }.get(status, "temporary" if status >= 500 else "input")


def classify_error(response: TransportResponse) -> Optional[OperationError]:
    """Public error-classification surface for downstreams (GitHub/MS).

    Returns ``None`` for a 2xx, a structured signal is NOT its job (412 is
    surfaced by :func:`execute`); for every other non-2xx it returns a typed
    :class:`OperationError` whose ``detail`` is redacted by
    :func:`operation_error`. Downstream error handling pins
    :data:`EXECUTOR_SCHEMA_VERSION` and switches on the returned ``class_``.
    """

    error_class = _classify_status(response.http_status, response.detail)
    if error_class is None:
        return None
    suffix = ""
    if response.retry_after_seconds is not None:
        suffix = f" (retry_after={response.retry_after_seconds}s)"
    return operation_error(
        error_class,
        f"transport returned HTTP {response.http_status}{suffix}",
    )


def _authorize(
    descriptor: OperationDescriptor,
    handle: DerivedHandle,
    *,
    now: float,
    offered_mode: CredentialMode,
    permitted: PermittedModes,
    layers: LayerCeilings,
    governance_scope: str,
    governance_item: str,
    attempt_record: Optional[AttemptRecord],
    request_args: Mapping[str, Any],
    request_idempotency_key: str,
) -> Tuple[Optional[TrustedHandleView], Optional[OperationError], Optional[ReplayDecision]]:
    """Run the four judgments IN ORDER. Returns as soon as one denies.

    On allow: ``(view, None, replay_or_None)``. On deny: ``(view_or_None,
    error, None)`` -- ``view`` is set once step 1 resolved it, so an audit sees
    the trusted axes even on a later deny. The transport is the caller's job and
    is only reached when this returns no error.
    """

    # (0) The clock must be finite before any time-sensitive judgment. L08's
    # ensure_usable re-checks this, but refusing here keeps a bad clock from
    # ever reaching a gate.
    if not _finite(now):
        return (
            None,
            operation_error("input", f"now must be a finite POSIX timestamp, got {now!r}"),
            None,
        )

    # (1) Trusted handle view -- the ONLY source of service_id / credential_mode.
    try:
        view = ensure_usable(handle, now=now)
    except (HandleExpiredError, HandleNotIssuedError, HandleTamperedError, HandleScopeError) as exc:
        return None, exc.error, None

    # (2) Credential-mode permit (deny-by-default, unstated == denied).
    auth_error = permit_operation(descriptor, offered_mode, permitted)
    if auth_error is not None:
        return view, auth_error, None

    # (3) Five-layer governance intersection (unknown scope == deny).
    permitted_by_policy, policy_error = decide(layers, governance_scope, governance_item)
    if not permitted_by_policy:
        return view, policy_error, None

    # (4) Write-replay gate -- only for a non-idempotent write that carries a
    # prior attempt record. A first attempt (no record) is not a replay.
    replay: Optional[ReplayDecision] = None
    if descriptor["effect"] in _NON_IDEMPOTENT_EFFECTS and attempt_record is not None:
        replay = replay_decision(
            descriptor,
            attempt_record,
            request_args=dict(request_args),
            request_idempotency_key=request_idempotency_key,
        )
        if replay["verdict"] == REPLAY_REFUSE:
            return view, replay["error"], None
        if replay["verdict"] == REPLAY_REUSE:
            # A prior success recorded: hand back the recorded result, do NOT
            # reissue (that would duplicate the effect). Signalled to execute()
            # via the replay decision.
            return view, None, replay

    return view, None, replay


def execute(
    descriptor: OperationDescriptor,
    handle: DerivedHandle,
    transport: Transport,
    *,
    now: float,
    offered_mode: CredentialMode,
    permitted: PermittedModes,
    layers: LayerCeilings,
    governance_scope: str,
    governance_item: str,
    request_args: Optional[Mapping[str, Any]] = None,
    request_idempotency_key: str = "",
    attempt_record: Optional[AttemptRecord] = None,
) -> ExecutionOutcome:
    """Authorize, then (only if authorized) emit ONE call through ``transport``.

    The gate chain (handle view -> credential mode -> governance -> write replay)
    runs FIRST. If any gate denies, this returns an :class:`ExecutionOutcome`
    carrying the typed error and the transport is NEVER called. Routing uses the
    TRUSTED view's ``service_id`` / ``credential_mode``, not the handle's.

    A ``reuse`` replay verdict hands back the recorded result without calling the
    transport. On a real emit, an HTTP 412 is returned as a structured
    :class:`PreconditionFailure` (never flattened); every other non-2xx becomes a
    typed error; a 2xx returns the success envelope.
    """

    args: Mapping[str, Any] = request_args or {}
    view, error, replay = _authorize(
        descriptor,
        handle,
        now=now,
        offered_mode=offered_mode,
        permitted=permitted,
        layers=layers,
        governance_scope=governance_scope,
        governance_item=governance_item,
        attempt_record=attempt_record,
        request_args=args,
        request_idempotency_key=request_idempotency_key,
    )
    if error is not None:
        return ExecutionOutcome(error=error, view=view)

    # Authorized. A recorded success is reused WITHOUT emitting (no duplicate).
    if replay is not None and replay["verdict"] == REPLAY_REUSE:
        return ExecutionOutcome(result=replay["reuse_result"], view=view)

    # Emit exactly one call, routed on the TRUSTED axes.
    assert view is not None  # invariant: no error means the view resolved
    response = transport(
        service_id=view.service_id,
        credential_mode=view.credential_mode,
        descriptor=descriptor,
        request_args=dict(args),
        request_idempotency_key=request_idempotency_key,
    )

    # HTTP 412: preserve the structured precondition signal, do not flatten.
    if response.http_status == 412:
        return ExecutionOutcome(
            precondition=_precondition_failure(response),
            view=view,
        )

    error = classify_error(response)
    if error is not None:
        return ExecutionOutcome(error=error, view=view)

    return ExecutionOutcome(result=response.result, view=view)


def _precondition_failure(response: TransportResponse) -> PreconditionFailure:
    """Build the structured 412 signal from a transport response."""

    preconditions = tuple(response.preconditions) or ("If-Match",)
    error = operation_error(
        "conflict",
        "precondition failed: the asserted precondition(s) "
        + ", ".join(preconditions)
        + " do not hold; the write did not apply -- re-read and re-derive the "
        "precondition before any retry",
    )
    return PreconditionFailure(
        preconditions=preconditions,
        server_etag=response.etag,
        error=error,
    )


# --- pagination: real cursor advance, never a placeholder ---------------------
@dataclass
class PageWalk:
    """A real, terminating pagination walk over ``execute``.

    Each :meth:`next` call runs the FULL gate chain again (a cursor does not
    exempt a page from authorization) and emits one call carrying the current
    cursor. It advances on the response's ``next_cursor``, stops when that is
    ``None``, and refuses to loop forever on a repeated cursor -- so it neither
    drops nor duplicates a page and always terminates.

    ``done`` is True once the last page returned no ``next_cursor`` (or a gate
    denied / a transport error stopped the walk). ``pages`` counts the pages
    successfully fetched.
    """

    descriptor: OperationDescriptor
    handle: DerivedHandle
    transport: Transport
    now: float
    offered_mode: CredentialMode
    permitted: PermittedModes
    layers: LayerCeilings
    governance_scope: str
    governance_item: str
    _cursor: Optional[str] = None
    done: bool = False
    pages: int = 0
    _seen_cursors: set = field(default_factory=set)

    def next(self) -> ExecutionOutcome:
        """Fetch the next page. Returns the outcome; sets ``done`` at the end."""

        if self.done:
            raise StopIteration("pagination walk is already complete")

        # A repeated cursor would loop forever: terminate defensively.
        if self._cursor is not None and self._cursor in self._seen_cursors:
            self.done = True
            return ExecutionOutcome(
                error=operation_error(
                    "temporary",
                    "pagination refused to advance: the provider returned a "
                    "cursor already seen this walk (would loop)",
                )
            )
        if self._cursor is not None:
            self._seen_cursors.add(self._cursor)

        outcome = execute(
            self.descriptor,
            self.handle,
            self.transport,
            now=self.now,
            offered_mode=self.offered_mode,
            permitted=self.permitted,
            layers=self.layers,
            governance_scope=self.governance_scope,
            governance_item=self.governance_item,
            request_args={"cursor": self._cursor},
            request_idempotency_key="",
        )

        if not outcome.ok:
            # A denied gate, a transport error, or a 412 stops the walk.
            self.done = True
            return outcome

        self.pages += 1
        next_cursor = outcome.result["next_cursor"] if outcome.result else None
        if next_cursor is None:
            self.done = True
        else:
            self._cursor = next_cursor
        return outcome


def advance_page(walk: PageWalk) -> ExecutionOutcome:
    """Advance one page of a :class:`PageWalk` (the public paging surface)."""

    return walk.next()


__all__ = [
    "EXECUTOR_SCHEMA_VERSION",
    "ExecutionOutcome",
    "PageWalk",
    "PreconditionFailure",
    "Transport",
    "TransportResponse",
    "advance_page",
    "classify_error",
    "execute",
]
