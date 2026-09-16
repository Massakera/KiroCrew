"""Contract, gate, and negative tests for the W01 · L07 write-replay gate.

The gate is pure decision logic over a recorded attempt, zero IO, so these
tests use in-memory fake attempt records only -- no real network write, no real
business write. They pin the core invariant (an ``unknown`` non-idempotent
write is NOT blindly replayed), its positive counterparts (a
``failed_not_applied`` replay is allowed, a ``succeeded`` one reuses its result,
an idempotent ``unknown`` replay is allowed), and the seam invariants
(additive-only export face, no top-level alias, typed refusals only).
"""

from __future__ import annotations

from kiro_crew import connections
from kiro_crew.connections import control_plane as cp
from kiro_crew.connections.control_plane import (
    ATTEMPT_OUTCOMES,
    REPLAY_VERDICTS,
    AttemptRecord,
    OperationDescriptor,
    OperationResult,
    args_fingerprint,
    record_attempt,
    replay_decision,
)

# --- fakes: in-memory descriptors and attempt records ----------------------


def _non_idempotent_descriptor() -> OperationDescriptor:
    # A GitHub create-issue-shaped write: effect `write`, non-idempotent, so an
    # `unknown` outcome must gate a replay.
    return {
        "operation_id": "github.create_issue",
        "service_id": "github",
        "operation_kind": "mutation",
        "effect": "write",
        "credential_modes": ("oauth_user", "fine_grained_pat"),
    }


def _external_send_descriptor() -> OperationDescriptor:
    # A send-message-shaped write: effect `external_send`, non-idempotent.
    return {
        "operation_id": "slack.post_message",
        "service_id": "slack",
        "operation_kind": "mutation",
        "effect": "external_send",
        "credential_modes": ("oauth_user",),
    }


def _idempotent_delete_descriptor() -> OperationDescriptor:
    # A delete: naturally idempotent -- deleting an already-deleted resource
    # leaves the same end state -- so an `unknown` replay is safe.
    return {
        "operation_id": "github.delete_label",
        "service_id": "github",
        "operation_kind": "mutation",
        "effect": "delete",
        "credential_modes": ("fine_grained_pat",),
    }


def _record(outcome: str, *, idempotent: bool = False, result=None) -> AttemptRecord:
    return record_attempt(
        operation_id="github.create_issue",
        args_fingerprint=args_fingerprint({"title": "bug", "body": "x"}),
        idempotency_key="idem-key-1",
        outcome=outcome,  # type: ignore[arg-type]
        recorded_result=result,
        idempotent=idempotent,
    )


# --- the counterexample that decides this slice ----------------------------


def test_a_write_with_unknown_outcome_is_NOT_blindly_replayed() -> None:
    # A non-idempotent write left an `unknown` record (issued, outcome
    # uncertain). A replay request must be REFUSED with a typed operation_error
    # whose detail says the outcome is uncertain -- reissuing could create a
    # second issue and nothing proves the first did not land.
    descriptor = _non_idempotent_descriptor()
    record = _record("unknown")

    decision = replay_decision(descriptor, record)

    assert decision["verdict"] == "refuse"
    assert decision["reuse_result"] is None
    # Typed rejection, not a bare boolean -- built through operation_error.
    error = decision["error"]
    assert error is not None
    assert error["error_class"] == "conflict"
    detail = error["detail"].lower()
    assert "unknown" in detail or "uncertain" in detail


# --- the positive counterparts: the gate is not always-refuse --------------


def test_failed_not_applied_is_allowed_to_replay() -> None:
    # Known NOT to have applied -> a reissue cannot duplicate an effect.
    descriptor = _non_idempotent_descriptor()
    record = _record("failed_not_applied")

    decision = replay_decision(descriptor, record)

    assert decision["verdict"] == "allow"
    assert decision["reuse_result"] is None
    assert decision["error"] is None


def test_succeeded_reuses_the_recorded_result_instead_of_replaying() -> None:
    # Known to have applied -> hand back the recorded result, do not reissue.
    descriptor = _non_idempotent_descriptor()
    recorded: OperationResult = {"status": "ok", "next_cursor": None}
    record = _record("succeeded", result=recorded)

    decision = replay_decision(descriptor, record)

    assert decision["verdict"] == "reuse"
    assert decision["reuse_result"] == recorded
    assert decision["error"] is None


def test_idempotent_operation_may_replay_after_unknown() -> None:
    # A naturally-idempotent operation (effect `delete`) after `unknown` is
    # SAFE to replay -- the exception the descriptor's effect declares.
    descriptor = _idempotent_delete_descriptor()
    record = record_attempt(
        operation_id="github.delete_label",
        args_fingerprint=args_fingerprint({"name": "wontfix"}),
        idempotency_key="idem-del-1",
        outcome="unknown",
        recorded_result=None,
        idempotent=False,
    )

    decision = replay_decision(descriptor, record)

    assert decision["verdict"] == "allow"
    assert decision["error"] is None


def test_explicit_idempotent_override_allows_unknown_replay() -> None:
    # A write whose caller ASSERTS idempotency (a provider-honored idempotency
    # token) may replay after `unknown`, even with a non-idempotent effect.
    descriptor = _non_idempotent_descriptor()
    record = _record("unknown", idempotent=True)

    decision = replay_decision(descriptor, record)

    assert decision["verdict"] == "allow"
    assert decision["error"] is None


def test_external_send_unknown_is_refused_without_override() -> None:
    # Distinguishes the idempotent exception from the default: a send-message
    # write with an `unknown` outcome and no override must be refused.
    descriptor = _external_send_descriptor()
    record = record_attempt(
        operation_id="slack.post_message",
        args_fingerprint=args_fingerprint({"channel": "C1", "text": "hi"}),
        idempotency_key="idem-send-1",
        outcome="unknown",
        recorded_result=None,
        idempotent=False,
    )

    decision = replay_decision(descriptor, record)

    assert decision["verdict"] == "refuse"
    assert decision["error"] is not None
    assert decision["error"]["error_class"] == "conflict"


# --- contract --------------------------------------------------------------


def test_attempt_outcomes_are_the_three_value_closed_set() -> None:
    assert ATTEMPT_OUTCOMES == ("succeeded", "failed_not_applied", "unknown")


def test_replay_verdicts_are_the_three_value_closed_set() -> None:
    assert REPLAY_VERDICTS == ("allow", "reuse", "refuse")


def test_writes_carries_a_schema_version_constant() -> None:
    assert cp.WRITES_SCHEMA_VERSION >= 1


def test_attempt_record_has_every_declared_field() -> None:
    record = _record("unknown")
    assert set(record) == set(AttemptRecord.__annotations__)


def test_args_fingerprint_is_key_order_independent() -> None:
    # A retry fingerprints identically to its original regardless of key order.
    a = args_fingerprint({"title": "bug", "body": "x"})
    b = args_fingerprint({"body": "x", "title": "bug"})
    assert a == b
    # Different arguments fingerprint differently.
    assert a != args_fingerprint({"title": "bug", "body": "y"})


def test_refusal_detail_is_redacted_and_typed() -> None:
    # A refusal detail goes through operation_error's redaction discipline like
    # every other typed rejection on the seam.
    descriptor = _non_idempotent_descriptor()
    decision = replay_decision(descriptor, _record("unknown"))
    error = decision["error"]
    assert error is not None
    # detail is a plain str, capped like every OperationError detail.
    assert isinstance(error["detail"], str)
    assert len(error["detail"]) <= cp.MAX_ERROR_CHARS


# --- negative: seam invariants ---------------------------------------------


def test_writes_symbols_live_on_the_canonical_subpackage_only() -> None:
    # The L07 symbols are consumed via the canonical
    # `kiro_crew.connections.control_plane` path, NOT re-exported as top-level
    # `kiro_crew.connections` aliases: a second spelling with zero consumers is
    # a rename hazard, not a convenience. (Mirrors the L06 policy guard.)
    from kiro_crew.connections import control_plane

    new_symbols = (
        "ATTEMPT_OUTCOMES",
        "REPLAY_VERDICTS",
        "WRITES_SCHEMA_VERSION",
        "AttemptOutcome",
        "AttemptRecord",
        "ReplayDecision",
        "ReplayVerdict",
        "args_fingerprint",
        "record_attempt",
        "replay_decision",
    )
    for name in new_symbols:
        assert hasattr(control_plane, name), f"{name} missing from the canonical subpackage"
        assert name not in connections.__all__, f"{name} must not be a top-level alias"
        assert not hasattr(
            connections, name
        ), f"{name} must not be attribute-reachable at top level"
