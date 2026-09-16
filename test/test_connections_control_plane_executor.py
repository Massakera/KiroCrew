"""W01 · L09 executor tests.

These pin the properties the whole control-plane rests on: the four judgments run
BEFORE any transport call, a denied gate emits ZERO calls, routing reads the
TRUSTED handle view (never the mutable handle), a 412 is preserved as a
structured signal instead of flattened, and pagination advances for real.
"""

from __future__ import annotations

import math

import kiro_crew.connections.control_plane as cp
from kiro_crew.connections.control_plane.auth_modes import declare_permitted_modes
from kiro_crew.connections.control_plane.binding import (
    Binding,
    VerifiedIdentity,
    create_binding,
)
from kiro_crew.connections.control_plane.executor import (
    EXECUTOR_SCHEMA_VERSION,
    PageWalk,
    PreconditionFailure,
    TransportResponse,
    advance_page,
    classify_error,
    execute,
)
from kiro_crew.connections.control_plane.handle import (
    DerivedHandle,
    derive_handle,
)
from kiro_crew.connections.control_plane.operation import OperationDescriptor
from kiro_crew.connections.control_plane.policy import LayerCeilings
from kiro_crew.connections.control_plane.result import OperationResult
from kiro_crew.connections.control_plane.writes import (
    ATTEMPT_FAILED_NOT_APPLIED,
    ATTEMPT_UNKNOWN,
    args_fingerprint,
    record_attempt,
)

_T0 = 1_000_000.0
_GRANTED = ("mail.read", "mail.send")


# --- a counting, in-memory fake transport (NOT a real business write) ---------
class FakeTransport:
    """Records every call so a test can assert the gate emitted zero of them.

    In-memory only -- it performs no network and no business write. It hands
    back whatever ``TransportResponse`` the test queued, and remembers the
    trusted axes it was called with so routing can be checked.
    """

    def __init__(self, response: TransportResponse | list[TransportResponse]):
        self._responses = response if isinstance(response, list) else [response]
        self._i = 0
        self.calls: list[dict] = []

    def __call__(self, **kwargs) -> TransportResponse:
        self.calls.append(kwargs)
        resp = self._responses[min(self._i, len(self._responses) - 1)]
        self._i += 1
        return resp


def _verifier(*, claimed_subject, claimed_tenant, service_id) -> VerifiedIdentity:
    return {
        "subject_ref": f"subject://verified/{claimed_subject}",
        "tenant_ref": f"tenant://verified/{claimed_tenant}",
    }


def _binding(service_id="outlook") -> Binding:
    return create_binding(
        service_id=service_id,
        claimed_subject="alice",
        claimed_tenant="acme",
        credential_mode="oauth_user",
        verifier=_verifier,
        slug="outlook",
    )


def _handle(
    *, binding: Binding | None = None, requested=("mail.read",), ttl=300.0
) -> DerivedHandle:
    return derive_handle(
        binding or _binding(),
        granted_scopes=_GRANTED,
        requested_scopes=requested,
        now=_T0,
        ttl_seconds=ttl,
    )


def _descriptor(effect="read", modes=("oauth_user",)) -> OperationDescriptor:
    return {
        "operation_id": "outlook.messages.list",
        "service_id": "outlook",
        "operation_kind": "list",
        "effect": effect,
        "credential_modes": tuple(modes),
    }


def _ok_response(next_cursor=None) -> TransportResponse:
    result: OperationResult = {"status": "ok", "next_cursor": next_cursor}
    return TransportResponse(http_status=200, result=result)


def _kw(**over):
    base = dict(
        now=_T0,
        offered_mode="oauth_user",
        permitted=declare_permitted_modes(("oauth_user",)),
        layers=LayerCeilings(),  # all None == ungoverned == permit
        governance_scope="tools",
        governance_item="messages.list",
    )
    base.update(over)
    return base


# --- schema version -----------------------------------------------------------
def test_executor_has_a_schema_version() -> None:
    assert isinstance(EXECUTOR_SCHEMA_VERSION, int)
    assert EXECUTOR_SCHEMA_VERSION >= 1


# --- the happy path emits exactly one call ------------------------------------
def test_authorized_call_emits_exactly_one_transport_call() -> None:
    transport = FakeTransport(_ok_response())
    outcome = execute(_descriptor(), _handle(), transport, **_kw())
    assert outcome.ok
    assert len(transport.calls) == 1


# --- THE judgment point: a denied gate emits ZERO transport calls -------------
def test_denied_credential_mode_emits_zero_calls() -> None:
    transport = FakeTransport(_ok_response())
    # offered mode not permitted (permitted set is empty == deny-by-default)
    outcome = execute(
        _descriptor(),
        _handle(),
        transport,
        **_kw(permitted=declare_permitted_modes(())),
    )
    assert outcome.error is not None
    assert outcome.error["error_class"] == "auth"
    assert len(transport.calls) == 0  # THE assertion: transport never touched


def test_undeclared_mode_emits_zero_calls() -> None:
    transport = FakeTransport(_ok_response())
    # descriptor declares only service_to_service; caller offers oauth_user
    outcome = execute(
        _descriptor(modes=("service_to_service",)),
        _handle(),
        transport,
        **_kw(offered_mode="oauth_user", permitted=declare_permitted_modes(("oauth_user",))),
    )
    assert outcome.error is not None
    assert outcome.error["error_class"] == "auth"
    assert len(transport.calls) == 0


def test_unknown_governance_scope_emits_zero_calls() -> None:
    transport = FakeTransport(_ok_response())
    outcome = execute(
        _descriptor(),
        _handle(),
        transport,
        **_kw(governance_scope="definitely.not.a.catalog.scope", governance_item="x"),
    )
    assert outcome.error is not None
    assert len(transport.calls) == 0


def test_tampered_handle_scope_emits_zero_calls() -> None:
    transport = FakeTransport(_ok_response())
    handle = _handle(requested=("mail.read",))
    tampered = dict(handle)
    tampered["scopes"] = ("mail.read", "mail.send")  # widened beyond issued
    outcome = execute(_descriptor(), tampered, transport, **_kw())
    assert outcome.error is not None
    assert outcome.error["error_class"] == "auth"
    assert outcome.view is None  # handle rejected before a view resolved
    assert len(transport.calls) == 0


def test_expired_handle_emits_zero_calls() -> None:
    transport = FakeTransport(_ok_response())
    handle = _handle(ttl=100.0)
    outcome = execute(_descriptor(), handle, transport, **_kw(now=_T0 + 200.0))
    assert outcome.error is not None
    assert len(transport.calls) == 0


def test_non_finite_now_emits_zero_calls() -> None:
    for bad in (math.nan, math.inf, -math.inf):
        t = FakeTransport(_ok_response())
        outcome = execute(_descriptor(), _handle(), t, **_kw(now=bad))
        assert outcome.error is not None
        assert outcome.error["error_class"] == "input"
        assert len(t.calls) == 0


def test_write_replay_refuse_on_unknown_emits_zero_calls() -> None:
    transport = FakeTransport(_ok_response())
    desc = _descriptor(effect="external_send")
    args = {"to": "x", "body": "y"}
    record = record_attempt(
        operation_id=desc["operation_id"],
        args_fingerprint=args_fingerprint(args),
        idempotency_key="k1",
        outcome=ATTEMPT_UNKNOWN,
    )
    outcome = execute(
        desc,
        _handle(),
        transport,
        **_kw(),
        request_args=args,
        request_idempotency_key="k1",
        attempt_record=record,
    )
    assert outcome.error is not None
    assert outcome.error["error_class"] == "conflict"
    assert len(transport.calls) == 0  # uncertain write is NOT blindly reissued


# --- routing trusts the VIEW, not the handle ----------------------------------
def test_routing_ignores_a_mutated_handle_service_id() -> None:
    binding = _binding(service_id="outlook")
    handle = _handle(binding=binding)
    # Mutate the handle's self-reported service_id to a DIFFERENT service.
    lying = dict(handle)
    lying["service_id"] = "github"
    transport = FakeTransport(_ok_response())
    outcome = execute(_descriptor(), lying, transport, **_kw())
    # ensure_usable refuses a tampered service_id outright -> zero calls, so the
    # mutation cannot even reach routing. Either way, routing never uses "github".
    if outcome.ok:
        assert transport.calls[0]["service_id"] == "outlook"
    else:
        assert len(transport.calls) == 0
    assert all(c["service_id"] != "github" for c in transport.calls)


def test_routing_uses_the_trusted_view_service_id() -> None:
    handle = _handle()
    transport = FakeTransport(_ok_response())
    outcome = execute(_descriptor(), handle, transport, **_kw())
    assert outcome.ok
    assert transport.calls[0]["service_id"] == "outlook"
    assert transport.calls[0]["credential_mode"] == "oauth_user"
    assert outcome.view is not None
    assert outcome.view.service_id == "outlook"


# --- HTTP 412: structured, not flattened --------------------------------------
def test_http_412_is_preserved_as_a_structured_signal() -> None:
    transport = FakeTransport(
        TransportResponse(
            http_status=412,
            preconditions=("If-Match",),
            etag='W/"v7"',
            detail="etag mismatch",
        )
    )
    outcome = execute(
        _descriptor(effect="write"), _handle(requested=("mail.send",)), transport, **_kw()
    )
    # Not flattened into a generic error: the structured signal is present.
    assert outcome.precondition is not None
    assert isinstance(outcome.precondition, PreconditionFailure)
    assert outcome.precondition.preconditions == ("If-Match",)
    assert outcome.precondition.server_etag == 'W/"v7"'
    # Maps to L07's failed_not_applied (write did not land).
    assert outcome.precondition.recorded_outcome == ATTEMPT_FAILED_NOT_APPLIED
    # A caller that wants the flat class can still read it, but it is a typed
    # conflict, not a swallowed error.
    assert outcome.precondition.error["error_class"] == "conflict"
    assert outcome.error is None  # NOT surfaced as a generic error


def test_412_recorded_outcome_is_the_replayable_branch() -> None:
    # failed_not_applied is exactly the branch replay_decision ALLOWS -- but the
    # executor still does not auto-reissue; it hands back the structured signal.
    assert ATTEMPT_FAILED_NOT_APPLIED in cp.ATTEMPT_OUTCOMES
    transport = FakeTransport(
        TransportResponse(http_status=412, preconditions=("If-Unmodified-Since",), etag=None)
    )
    outcome = execute(
        _descriptor(effect="write"), _handle(requested=("mail.send",)), transport, **_kw()
    )
    assert outcome.precondition.recorded_outcome == ATTEMPT_FAILED_NOT_APPLIED
    assert outcome.precondition.server_etag is None  # provider sent none; preserved as None
    # Exactly one call was emitted (the write was attempted, got 412).
    assert len(transport.calls) == 1


# --- error classification -----------------------------------------------------
def test_classify_error_maps_statuses() -> None:
    assert classify_error(_ok_response()) is None
    assert classify_error(TransportResponse(http_status=404))["error_class"] == "not_found"
    assert classify_error(TransportResponse(http_status=401))["error_class"] == "auth"
    assert classify_error(TransportResponse(http_status=403))["error_class"] == "forbidden"
    assert (
        classify_error(TransportResponse(http_status=429, retry_after_seconds=30))["error_class"]
        == "throttle"
    )
    assert classify_error(TransportResponse(http_status=503))["error_class"] == "temporary"
    # 412 is NOT classified as a flat error here -- it is a structured signal.
    # classify_error would call it conflict, but execute() never routes a 412
    # through classify_error (covered by the 412 tests above).


def test_transport_error_is_surfaced_as_typed_error() -> None:
    transport = FakeTransport(TransportResponse(http_status=404, detail="no such message"))
    outcome = execute(_descriptor(), _handle(), transport, **_kw())
    assert outcome.error is not None
    assert outcome.error["error_class"] == "not_found"
    assert len(transport.calls) == 1  # the call WAS emitted; provider said 404


# --- pagination: real advance, terminates, no drop/dup ------------------------
def test_pagination_walks_all_pages_and_terminates() -> None:
    transport = FakeTransport(
        [
            _ok_response(next_cursor="c1"),
            _ok_response(next_cursor="c2"),
            _ok_response(next_cursor=None),
        ]
    )
    walk = PageWalk(
        descriptor=_descriptor(),
        handle=_handle(),
        transport=transport,
        now=_T0,
        offered_mode="oauth_user",
        permitted=declare_permitted_modes(("oauth_user",)),
        layers=LayerCeilings(),
        governance_scope="tools",
        governance_item="messages.list",
    )
    pages = []
    while not walk.done:
        pages.append(advance_page(walk))
    assert walk.pages == 3
    assert len(pages) == 3
    assert walk.done
    # Cursors advanced in order: no page dropped, none duplicated.
    seen_cursors = [c["request_args"]["cursor"] for c in transport.calls]
    assert seen_cursors == [None, "c1", "c2"]


def test_pagination_refuses_to_loop_on_a_repeated_cursor() -> None:
    # A provider that keeps returning the SAME cursor must not spin forever.
    transport = FakeTransport(_ok_response(next_cursor="stuck"))
    walk = PageWalk(
        descriptor=_descriptor(),
        handle=_handle(),
        transport=transport,
        now=_T0,
        offered_mode="oauth_user",
        permitted=declare_permitted_modes(("oauth_user",)),
        layers=LayerCeilings(),
        governance_scope="tools",
        governance_item="messages.list",
    )
    outcomes = []
    for _ in range(10):  # bounded: the walk must terminate well before this
        if walk.done:
            break
        outcomes.append(advance_page(walk))
    assert walk.done
    assert outcomes[-1].error is not None
    assert outcomes[-1].error["error_class"] == "temporary"


def test_pagination_stops_on_a_denied_gate_without_emitting() -> None:
    transport = FakeTransport(_ok_response(next_cursor="c1"))
    walk = PageWalk(
        descriptor=_descriptor(),
        handle=_handle(),
        transport=transport,
        now=_T0,
        offered_mode="oauth_user",
        permitted=declare_permitted_modes(()),  # deny-by-default
        layers=LayerCeilings(),
        governance_scope="tools",
        governance_item="messages.list",
    )
    outcome = advance_page(walk)
    assert outcome.error is not None
    assert walk.done
    assert walk.pages == 0
    assert len(transport.calls) == 0


# --- guard: exports live only on control_plane, NOT top-level connections -----
def test_executor_symbols_are_reachable_on_control_plane() -> None:
    for name in (
        "EXECUTOR_SCHEMA_VERSION",
        "execute",
        "advance_page",
        "classify_error",
        "ExecutionOutcome",
        "PageWalk",
        "PreconditionFailure",
        "Transport",
        "TransportResponse",
    ):
        assert hasattr(cp, name), name
        assert name in cp.__all__, name


def test_executor_symbols_are_not_top_level_connections_reexports() -> None:
    import kiro_crew.connections as c

    for name in (
        "EXECUTOR_SCHEMA_VERSION",
        "execute",
        "advance_page",
        "classify_error",
        "ExecutionOutcome",
        "PageWalk",
        "PreconditionFailure",
        "TransportResponse",
    ):
        assert not hasattr(c, name), f"{name} leaked to top-level connections"
