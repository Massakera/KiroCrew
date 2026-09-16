"""Shared connector control plane (W01 · L01).

The one typed seam every provider stream (W02..W14) dispatches an operation
through: an :class:`~kiro_crew.connections.control_plane.operation.OperationDescriptor`
(what the operation is), an
:class:`~kiro_crew.connections.control_plane.context.OperationContext` (the
references one call is made under -- never a credential value), an
:class:`~kiro_crew.connections.control_plane.result.OperationResult` (the
success/partial envelope with an opaque pagination cursor), and the RUN-01
:class:`~kiro_crew.connections.control_plane.errors.OperationError` taxonomy.

W01 · L02 adds :class:`~kiro_crew.connections.control_plane.binding.Binding` --
AUTH-01's authorized-account record that a ``binding_ref`` points at: an
unguessable random id, a VERIFIED subject/tenant, a monotonic ``generation``
counter (for L04 revoke fencing; field + increment only here), and a
``secret_ref`` that carries the secret's location and metadata but never its
value.

L05 adds the per-operation permitted-credential-mode declaration and its
deny-by-default check
(:mod:`~kiro_crew.connections.control_plane.auth_modes`): an operation declares
which of the L01 credential modes it permits, and a caller-offered mode is
allowed or denied against that declaration (unstated == denied).

W01 · L08 adds :class:`~kiro_crew.connections.control_plane.handle.DerivedHandle`
-- a restricted, short-lived capability DERIVED from a trusted binding: its
scope set is a proven SUBSET of the binding's granted scopes, it carries an
absolute ``not_after`` TTL that :func:`~kiro_crew.connections.control_plane.handle.ensure_usable`
enforces, and it carries NOTHING that reconstructs the binding (no
``binding_id`` / subject / tenant / secret) -- only a one-way keyed
``binding_fingerprint`` and the ``generation``, both for L04 revoke fencing.

Pure types, zero IO. This module is the control plane's own export face, and it
is the CANONICAL one: the wider ``kiro_crew.connections`` package does NOT
re-export these symbols, so consumers import them from
``kiro_crew.connections.control_plane`` (or its submodules), never as
``kiro_crew.connections.<name>`` aliases.
"""

from kiro_crew.connections.control_plane.auth_modes import (
    AUTH_MODES_SCHEMA_VERSION,
    PermittedModeRegistry,
    PermittedModes,
    declare_permitted_modes,
    effective_permitted_modes,
    permit_operation,
    permit_registered_operation,
)
from kiro_crew.connections.control_plane.binding import (
    BINDING_SCHEMA_VERSION,
    INITIAL_GENERATION,
    SECRET_BACKEND_VAULT,
    Binding,
    BindingVerificationError,
    SecretRef,
    SubjectTenantVerifier,
    VerifiedIdentity,
    binding_secret_ref,
    create_binding,
    next_generation,
)
from kiro_crew.connections.control_plane.context import (
    CONTEXT_SCHEMA_VERSION,
    OperationContext,
)
from kiro_crew.connections.control_plane.errors import (
    ERROR_CLASSES,
    ERRORS_SCHEMA_VERSION,
    MAX_ERROR_CHARS,
    ErrorClass,
    OperationError,
    operation_error,
    redacted_detail,
)
from kiro_crew.connections.control_plane.handle import (
    HANDLE_SCHEMA_VERSION,
    DerivedHandle,
    HandleExpiredError,
    HandleNotIssuedError,
    HandleScopeError,
    HandleTamperedError,
    TrustedHandleView,
    derive_handle,
    ensure_usable,
    is_expired,
)
from kiro_crew.connections.control_plane.operation import (
    CREDENTIAL_MODES,
    EFFECTS,
    OPERATION_KINDS,
    OPERATION_SCHEMA_VERSION,
    SERVICE_IDS,
    CredentialMode,
    Effect,
    OperationDescriptor,
    OperationKind,
    ServiceId,
)
from kiro_crew.connections.control_plane.policy import (
    LAYERS,
    POLICY_SCHEMA_VERSION,
    Approval,
    LayerCeilings,
    LayerName,
    approval_applies,
    decide,
    resolve_layers,
)
from kiro_crew.connections.control_plane.result import (
    RESULT_SCHEMA_VERSION,
    RESULT_STATUSES,
    OperationResult,
    ResultStatus,
)
from kiro_crew.connections.control_plane.writes import (
    ATTEMPT_OUTCOMES,
    REPLAY_VERDICTS,
    WRITES_SCHEMA_VERSION,
    AttemptOutcome,
    AttemptRecord,
    ReplayDecision,
    ReplayVerdict,
    args_fingerprint,
    record_attempt,
    replay_decision,
)

__all__ = [
    "ATTEMPT_OUTCOMES",
    "AUTH_MODES_SCHEMA_VERSION",
    "BINDING_SCHEMA_VERSION",
    "CONTEXT_SCHEMA_VERSION",
    "CREDENTIAL_MODES",
    "EFFECTS",
    "ERRORS_SCHEMA_VERSION",
    "ERROR_CLASSES",
    "HANDLE_SCHEMA_VERSION",
    "INITIAL_GENERATION",
    "LAYERS",
    "MAX_ERROR_CHARS",
    "OPERATION_KINDS",
    "OPERATION_SCHEMA_VERSION",
    "POLICY_SCHEMA_VERSION",
    "REPLAY_VERDICTS",
    "RESULT_SCHEMA_VERSION",
    "RESULT_STATUSES",
    "SECRET_BACKEND_VAULT",
    "SERVICE_IDS",
    "WRITES_SCHEMA_VERSION",
    "Approval",
    "AttemptOutcome",
    "AttemptRecord",
    "Binding",
    "BindingVerificationError",
    "CredentialMode",
    "DerivedHandle",
    "Effect",
    "ErrorClass",
    "HandleExpiredError",
    "HandleNotIssuedError",
    "HandleScopeError",
    "HandleTamperedError",
    "LayerCeilings",
    "LayerName",
    "OperationContext",
    "OperationDescriptor",
    "OperationError",
    "OperationKind",
    "OperationResult",
    "PermittedModeRegistry",
    "PermittedModes",
    "ReplayDecision",
    "ReplayVerdict",
    "ResultStatus",
    "SecretRef",
    "ServiceId",
    "SubjectTenantVerifier",
    "TrustedHandleView",
    "VerifiedIdentity",
    "approval_applies",
    "args_fingerprint",
    "binding_secret_ref",
    "create_binding",
    "decide",
    "declare_permitted_modes",
    "derive_handle",
    "effective_permitted_modes",
    "ensure_usable",
    "is_expired",
    "next_generation",
    "operation_error",
    "permit_operation",
    "permit_registered_operation",
    "record_attempt",
    "redacted_detail",
    "replay_decision",
    "resolve_layers",
]
