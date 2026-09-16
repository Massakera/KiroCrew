"""W01 · L07: the replay gate for a non-idempotent connector write.

A non-idempotent write (POST-creating a resource, sending a message) that is
issued and then leaves its caller with an UNCERTAIN outcome -- a timeout, a
dropped connection, a 5xx with no usable body -- is the dangerous case this
module exists for. Blindly retrying such a write can DO IT TWICE: two issues
created, two messages sent. The core invariant is:

    When a non-idempotent write's outcome is ``unknown``, a replay MUST be
    refused unless there is evidence the prior attempt did NOT take effect (or
    its recorded result can be reused).

This is pure decision logic over a recorded attempt, zero IO. It does not issue
the write, hold a client, or resolve a credential -- it decides, given what is
KNOWN about a prior attempt, whether replaying it now is allowed. The three
outcome states are deliberately distinct from both the success envelope
(:mod:`kiro_crew.connections.control_plane.result`) and the RUN-01 error
taxonomy (:mod:`kiro_crew.connections.control_plane.errors`): those classify
what a call RETURNED, this records what is known about whether a call's effect
LANDED.

The three attempt outcomes
--------------------------
- ``succeeded`` -- the write is known to have taken effect. A replay would
  duplicate it, so the gate refuses to reissue and instead REUSES the recorded
  result (``reuse``). This is why an attempt record carries an optional
  ``recorded_result``: a known-succeeded write has a result to hand back rather
  than redo.
- ``failed_not_applied`` -- the write is known NOT to have taken effect (a
  provider rejection that never mutated state, a pre-flight failure). A replay
  is safe and ALLOWED: reissuing cannot duplicate an effect that never landed.
- ``unknown`` -- the write was issued and the outcome is uncertain. This is the
  case blind retry gets wrong. For a NON-idempotent operation the gate REFUSES
  the replay with a typed rejection, because reissuing might do the effect a
  second time and nothing here proves it didn't.

The idempotent-write exception
------------------------------
Whether ``unknown`` is safe to replay is a property of the OPERATION, read from
its descriptor's ``effect``. An idempotent write -- one whose repetition is
indistinguishable from a single application -- can be replayed after ``unknown``
without risking a double effect, so the gate ALLOWS it. Idempotency is NOT
inferred from the operation's name or from the presence of an idempotency key;
it is declared by the descriptor. The manifest's ``effect`` closed set
(``read`` / ``write`` / ``delete`` / ``share`` / ``external_send`` / ``admin``
/ ``billable``) is the vocabulary this decision reads:

- ``read`` is not a mutation at all: replay is always safe.
- ``delete`` is naturally idempotent -- deleting an already-deleted resource
  leaves the same end state -- so an ``unknown`` delete is safe to replay.
- ``write`` / ``share`` / ``external_send`` / ``admin`` / ``billable`` are
  treated as NON-idempotent: a second ``write`` can create a second resource, a
  second ``external_send`` sends a second message, a second ``billable`` charges
  twice. These are exactly the effects an ``unknown`` outcome must gate.

An operation that genuinely IS idempotent despite a non-idempotent effect (a
PUT-to-a-fixed-key upsert, a create carrying a provider-honored idempotency
token) declares that with the explicit ``idempotent`` flag on its attempt
record; the flag is an OVERRIDE the caller asserts, never a default. When set,
an ``unknown`` replay is allowed regardless of effect. This keeps the safe
default (refuse) and makes the exception something a caller must state, not
something the gate guesses.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal, TypedDict

from kiro_crew.connections.control_plane.errors import OperationError, operation_error
from kiro_crew.connections.control_plane.operation import Effect, OperationDescriptor
from kiro_crew.connections.control_plane.result import OperationResult

#: Bumped when this module's shapes change, mirroring the sibling modules'
#: module-level schema-version constant.
WRITES_SCHEMA_VERSION = 1

# --- attempt outcome: what is KNOWN about whether the write's effect landed --
#: The write is known to have taken effect; its result is recorded for reuse.
ATTEMPT_SUCCEEDED = "succeeded"
#: The write is known NOT to have taken effect; a replay is safe.
ATTEMPT_FAILED_NOT_APPLIED = "failed_not_applied"
#: The outcome is uncertain (timeout / dropped connection / bodyless 5xx). This
#: is the case blind retry gets wrong.
ATTEMPT_UNKNOWN = "unknown"

#: The three-value closed set of what is known about a prior attempt's effect.
#: Distinct from the success envelope's ``ResultStatus`` and the RUN-01
#: ``ErrorClass``: those classify what a call returned; this records whether the
#: effect landed.
AttemptOutcome = Literal["succeeded", "failed_not_applied", "unknown"]

#: Tuple form of :data:`AttemptOutcome`'s closed set.
ATTEMPT_OUTCOMES: tuple[AttemptOutcome, ...] = (
    "succeeded",
    "failed_not_applied",
    "unknown",
)

# --- replay decision: the gate's verdict -----------------------------------
#: Reissue the write: the prior attempt provably did not apply.
REPLAY_ALLOW: Literal["allow"] = "allow"
#: Do NOT reissue; hand back the prior attempt's recorded result instead.
REPLAY_REUSE: Literal["reuse"] = "reuse"
#: Refuse the replay: the outcome is uncertain and the operation is not
#: idempotent, so reissuing risks a duplicate effect.
REPLAY_REFUSE: Literal["refuse"] = "refuse"

#: The three-value closed set of the gate's verdict.
ReplayVerdict = Literal["allow", "reuse", "refuse"]

#: Tuple form of :data:`ReplayVerdict`'s closed set.
REPLAY_VERDICTS: tuple[ReplayVerdict, ...] = ("allow", "reuse", "refuse")

#: The effects treated as naturally safe to replay after an ``unknown`` outcome
#: WITHOUT an explicit idempotency assertion. ``read`` is not a mutation;
#: ``delete`` is naturally idempotent (deleting an already-deleted resource
#: leaves the same end state). Every other effect is non-idempotent by default
#: and an ``unknown`` replay of it must be gated. This is the DEFAULT, widened
#: per-attempt by the explicit ``idempotent`` override flag.
_REPLAY_SAFE_EFFECTS: frozenset[Effect] = frozenset({"read", "delete"})


class AttemptRecord(TypedDict):
    """What is known about ONE non-idempotent-write attempt.

    Every field present, matching the sibling seam ``TypedDict``s' shape.

    Identity is the triple ``(operation_id, args_fingerprint, idempotency_key)``
    -- the operation issued, a fingerprint of the arguments it was issued with,
    and the caller-supplied idempotency key that ties a retry to its original.
    Two attempts with the same triple are the SAME logical write; that is what
    lets the gate recognize a retry as a replay of a known prior attempt.

    ``outcome`` is one of the :data:`AttemptOutcome` closed set -- what is known
    about whether the effect landed.

    ``recorded_result`` is the :class:`OperationResult` a ``succeeded`` attempt
    returned, so the gate can REUSE it rather than reissue; it is ``None`` for a
    ``failed_not_applied`` or ``unknown`` attempt, which have no result to hand
    back.

    ``idempotent`` is the caller's explicit assertion that this operation is
    safe to replay even after an ``unknown`` outcome (a fixed-key upsert, a
    provider-honored idempotency token). It is an OVERRIDE, defaulting to the
    non-idempotent-safe behavior; the gate never infers it.
    """

    operation_id: str
    args_fingerprint: str
    idempotency_key: str
    outcome: AttemptOutcome
    recorded_result: OperationResult | None
    idempotent: bool


class ReplayDecision(TypedDict):
    """The gate's verdict on whether a replay of a recorded attempt is allowed.

    Every field present, matching the sibling seam ``TypedDict``s' shape.

    ``verdict`` is one of the :data:`ReplayVerdict` closed set. ``reuse_result``
    carries the recorded result to hand back when ``verdict == "reuse"`` and is
    ``None`` otherwise. ``error`` carries the typed :class:`OperationError` when
    ``verdict == "refuse"`` and is ``None`` otherwise -- a refusal is always a
    typed rejection built through :func:`operation_error`, never a bare
    boolean.
    """

    verdict: ReplayVerdict
    reuse_result: OperationResult | None
    error: OperationError | None


def args_fingerprint(args: dict[str, Any]) -> str:
    """A stable content fingerprint of a write's arguments.

    Two calls issued with the same arguments produce the same fingerprint
    regardless of key order, so a retry fingerprints identically to its
    original. This is a plain content hash for identity/equality, NOT a
    security primitive and NOT a place credentials belong -- arguments are the
    resource shape being written, and a credential value never travels here (the
    seam's context carries a ``binding_ref``, never a token). The value is
    serialized with sorted keys so ordering does not change the digest.
    """

    encoded = json.dumps(args, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def record_attempt(
    *,
    operation_id: str,
    args_fingerprint: str,
    idempotency_key: str,
    outcome: AttemptOutcome,
    recorded_result: OperationResult | None = None,
    idempotent: bool = False,
) -> AttemptRecord:
    """Build an :class:`AttemptRecord` with every field present.

    A convenience constructor mirroring :func:`operation_error`'s shape, so a
    caller cannot accidentally omit a field. ``recorded_result`` defaults to
    ``None`` (only a ``succeeded`` attempt carries one) and ``idempotent``
    defaults to ``False`` (the safe, non-idempotent default -- the exception is
    something a caller must state).
    """

    return {
        "operation_id": operation_id,
        "args_fingerprint": args_fingerprint,
        "idempotency_key": idempotency_key,
        "outcome": outcome,
        "recorded_result": recorded_result,
        "idempotent": idempotent,
    }


def _is_replay_safe_after_unknown(descriptor: OperationDescriptor, record: AttemptRecord) -> bool:
    """Whether an ``unknown`` outcome is safe to replay for this operation.

    Safe when the operation's declared ``effect`` is naturally replay-safe
    (``read`` / ``delete``) OR the caller explicitly asserted idempotency on the
    attempt record. The effect is READ from the descriptor -- never inferred
    from the operation's name or from the mere presence of an idempotency key.
    """

    if record["idempotent"]:
        return True
    return descriptor["effect"] in _REPLAY_SAFE_EFFECTS


def replay_decision(descriptor: OperationDescriptor, record: AttemptRecord) -> ReplayDecision:
    """Decide whether replaying the recorded attempt is allowed.

    The gate over the core invariant. Given the operation's descriptor (which
    declares its ``effect``, hence whether it is naturally idempotent) and a
    record of what is known about the prior attempt:

    - ``failed_not_applied`` -> ``allow``: the prior write provably did not
      land, so a reissue cannot duplicate an effect.
    - ``succeeded`` -> ``reuse``: the write took effect; hand back the recorded
      result rather than reissue and duplicate it.
    - ``unknown`` -> ``refuse`` for a non-idempotent operation: the outcome is
      uncertain and nothing proves the effect did not land, so a blind reissue
      risks doing it twice. The refusal is a typed
      :class:`OperationError` (``conflict``) whose detail says the outcome is
      uncertain.
    - ``unknown`` for an idempotent operation (declared by ``effect`` or the
      explicit ``idempotent`` override) -> ``allow``: replay is safe because a
      second application is indistinguishable from one.
    """

    outcome = record["outcome"]

    if outcome == ATTEMPT_FAILED_NOT_APPLIED:
        return {"verdict": REPLAY_ALLOW, "reuse_result": None, "error": None}

    if outcome == ATTEMPT_SUCCEEDED:
        return {
            "verdict": REPLAY_REUSE,
            "reuse_result": record["recorded_result"],
            "error": None,
        }

    # outcome == ATTEMPT_UNKNOWN -- the case blind retry gets wrong.
    if _is_replay_safe_after_unknown(descriptor, record):
        return {"verdict": REPLAY_ALLOW, "reuse_result": None, "error": None}

    return {
        "verdict": REPLAY_REFUSE,
        "reuse_result": None,
        "error": operation_error(
            "conflict",
            (
                "replay refused: the prior attempt for operation "
                f"{descriptor['operation_id']} left an uncertain (unknown) "
                "outcome and this operation is not idempotent, so reissuing it "
                "could apply the effect a second time; no evidence proves the "
                "prior attempt did not take effect"
            ),
        ),
    }
