"""W01 · L09: the PRODUCTION transport composition for the executor.

:mod:`kiro_crew.connections.control_plane.executor` decides; it does not reach a
network. That is the right shape for the decision chain -- every gate is testable
against an in-memory fake, and the unit tests keep using that fake -- but a
dispatch seam that ONLY ever had a fake behind it is not an executor, it is a
decision table with an executor-shaped hole. This module fills the hole: it
composes a real :data:`~kiro_crew.connections.control_plane.executor.Transport`
out of the two things a real call needs and a fake supplies neither of:

1. **Real secret custody.** The credential comes from the EXISTING vault --
   :class:`kiro_crew.secrets.SecretVault` (``secrets/vault.py``), the same
   AES-256-GCM store the Secrets panel and ``oauth_clients`` already use -- read
   by NAME through the :class:`~kiro_crew.connections.control_plane.binding.SecretRef`
   L02 records on a binding (``binding_secret_ref`` builds the
   ``CONNECTIONS_<SLUG>_BINDING_SECRET`` name). No new vault, no new naming
   scheme, and no plaintext anywhere on the control plane's own types: the
   secret is resolved PER CALL, revealed once into an ``Authorization`` header,
   and never returned, stored on a closure, or placed in an error detail.
2. **A real HTTP client.** :func:`urllib_http_send` is the standard library's
   ``urllib.request`` -- the convention this repo already uses for outbound HTTP
   in non-async code (``apps/backend.py``, ``apps/official_catalog.py``,
   ``ops_mission_control/backend/providers/http.py``) -- so this adds no
   third-party dependency.

What this module deliberately does NOT do
-----------------------------------------
**No vendor semantics.** Turning an operation + its arguments into a concrete
method/URL/headers/body is the VENDOR owner's job (``vendors/microsoft/**``,
``vendors/github/**``): locator shape, ``@odata.nextLink`` vs ``page``/``perPage``
paging, readback and precondition derivation all differ per provider. So a
:data:`RequestLocator` is INJECTED, and the 2xx -> ``OperationResult`` decode is
injected too; the neutral default reports no continuation cursor rather than
guessing one provider's spelling. This module owns custody + wire mechanics and
nothing above them.

**No network at import time.** Nothing here opens a socket, constructs a client,
or reads the vault while the module is being imported: every side effect happens
inside the transport closure, on a call. Importing this module is free.

**Not exercised by the executor's unit tests.** Those stay on the in-memory fake
(dependency injection is the correct design, not a workaround). The one test this
module owns proves the custody wiring -- that the transport asks the vault for the
binding's ``secret_ref`` name -- with an injected vault and an injected sender, so
it too performs no network.

**https only.** A ``Bearer`` credential over plain ``http`` is a credential sent
in clear, so :func:`urllib_http_send` refuses any scheme but ``https`` rather than
leaving it to the caller to remember.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional, Protocol, Tuple

from kiro_crew.connections.control_plane.binding import (
    SECRET_BACKEND_VAULT,
    SecretRef,
)
from kiro_crew.connections.control_plane.executor import (
    Transport,
    TransportResponse,
)
from kiro_crew.connections.control_plane.operation import (
    CredentialMode,
    OperationDescriptor,
)
from kiro_crew.connections.control_plane.result import OperationResult
from kiro_crew.secrets import SecretValue

#: Bumped when this module's composition shape changes, mirroring the schema
#: version every sibling control-plane module carries.
PRODUCTION_SCHEMA_VERSION = 1

#: Default per-call HTTP timeout. A transport with no timeout can hang a dispatch
#: forever, which is a worse failure than a typed ``temporary`` error.
DEFAULT_TIMEOUT_SECONDS = 30.0

#: The request headers that ASSERT a precondition. On a 412 the provider often
#: does not say which one failed, so the transport reports the ones this request
#: actually sent -- never a name it did not send (the executor's
#: ``condition_unknown`` covers the "nothing is known" case).
_PRECONDITION_HEADERS: Tuple[str, ...] = (
    "If-Match",
    "If-None-Match",
    "If-Unmodified-Since",
    "If-Modified-Since",
)


class SecretStore(Protocol):
    """The READ side of the existing vault, as this module needs it.

    :class:`kiro_crew.secrets.SecretVault` satisfies this structurally -- this is
    NOT a second vault, it is the injection seam that lets a test hand in a stub
    instead of building an encrypted store on disk. Nothing here writes, deletes,
    or re-keys a secret; the transport only ever reads one by name.
    """

    def get(self, name: str) -> Optional[SecretValue]:  # pragma: no cover - Protocol
        ...


class SecretResolutionError(Exception):
    """Raised when a binding's :class:`SecretRef` cannot be resolved to a value.

    Carries no secret material and no vault contents -- only the entry NAME that
    was looked up, which is a public, deterministic string (``binding_secret_ref``
    derives it from the provider slug). The transport catches this and returns a
    typed ``auth`` failure rather than letting it escape, because the
    :data:`~kiro_crew.connections.control_plane.executor.Transport` contract is
    "return a structured envelope, do not raise".
    """


@dataclass(frozen=True)
class HttpRequest:
    """One concrete outbound HTTP request, as a VENDOR locator shaped it.

    ``method`` -- the HTTP verb. ``url`` -- the absolute ``https`` URL.
    ``headers`` -- vendor headers WITHOUT the credential; the transport adds
    ``Authorization`` itself so a locator never touches a secret. ``body`` --
    the encoded request body, or ``None``.
    """

    method: str
    url: str
    headers: Mapping[str, str] = field(default_factory=dict)
    body: Optional[bytes] = None


@dataclass(frozen=True)
class HttpReply:
    """One raw HTTP reply, before it is mapped to a :class:`TransportResponse`.

    ``status`` -- the HTTP status. ``headers`` -- the response headers.
    ``body`` -- the raw response body.
    """

    status: int
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes = b""


#: Sends one :class:`HttpRequest` and returns its :class:`HttpReply`.
#: :func:`urllib_http_send` is the production implementation; a test injects its
#: own so no socket is opened.
HttpSend = Callable[..., HttpReply]

#: Shapes an operation + its arguments into one concrete :class:`HttpRequest`.
#: VENDOR-OWNED: it is injected, never implemented here (see the module
#: docstring).
RequestLocator = Callable[..., HttpRequest]

#: Maps a 2xx :class:`HttpReply` to the L01 success envelope. VENDOR-OWNED for
#: anything cursor-shaped; :func:`neutral_decode` is the default.
ResultDecode = Callable[..., OperationResult]


def _header(headers: Mapping[str, str], name: str) -> Optional[str]:
    """Case-insensitive header lookup (HTTP header names are case-insensitive)."""

    lowered = name.lower()
    for key, value in headers.items():
        if key.lower() == lowered:
            return value
    return None


def neutral_decode(reply: HttpReply) -> OperationResult:
    """The default 2xx -> :class:`OperationResult` mapping: no continuation.

    Reports ``next_cursor=None`` -- "this is all there is" -- because the cursor
    lives at a different place in every provider's body (``@odata.nextLink``, a
    ``page`` pair, ``queryMore``) and picking one here would silently give every
    other provider a wrong answer. A vendor owner injects its own ``decode`` to
    surface its paging shape. ``status`` is ``ok``; a provider that distinguishes
    a partial page says so through its own decode.
    """

    return {"status": "ok", "next_cursor": None}


def resolve_binding_secret(secret_ref: SecretRef, *, vault: SecretStore) -> SecretValue:
    """Resolve L02's :class:`SecretRef` to a live value through the EXISTING vault.

    The one custody path. It reads the recorded entry NAME out of the ref and asks
    the vault for it -- :meth:`kiro_crew.secrets.SecretVault.get`, the same store
    the rest of the product uses -- and it refuses anything else:

    - a ref naming a backend other than :data:`SECRET_BACKEND_VAULT` is refused
      rather than assumed to mean the vault (the field exists precisely so a
      resolver does not have to guess);
    - a name the vault does not hold is refused, never substituted with an empty
      credential that would reach the provider as an anonymous call.

    Returns the opaque :class:`kiro_crew.secrets.SecretValue` (whose ``repr`` /
    ``str`` are ``****``), so the plaintext is only ever produced by an explicit
    ``.reveal()`` at the point of use. Raises :class:`SecretResolutionError`.
    """

    if secret_ref["backend"] != SECRET_BACKEND_VAULT:
        raise SecretResolutionError(
            f"secret ref names backend {secret_ref['backend']!r}, and this "
            f"composition resolves only {SECRET_BACKEND_VAULT!r}"
        )
    value = vault.get(secret_ref["name"])
    if value is None:
        raise SecretResolutionError(
            f"vault holds no entry named {secret_ref['name']!r}; the binding's "
            "secret must be stored before an operation can be dispatched"
        )
    return value


def urllib_http_send(
    request: HttpRequest,
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> HttpReply:
    """Send ``request`` with the standard library's ``urllib.request``.

    The real HTTP client on the production path -- stdlib, so no new dependency.
    A non-2xx is a REPLY, not an exception: ``urllib`` raises
    :class:`urllib.error.HTTPError` for it, and that object carries the status,
    headers and body, so it is unwrapped back into an :class:`HttpReply` for the
    caller to classify. A genuine connectivity failure still raises
    :class:`urllib.error.URLError`, which the transport maps to a typed
    ``temporary``.

    Refuses any scheme but ``https``: the caller is about to attach a ``Bearer``
    credential, and sending that over ``http`` would put it on the wire in clear.
    """

    if not request.url.lower().startswith("https://"):
        raise SecretResolutionError("refusing to send a credentialed request over a non-https URL")
    req = urllib.request.Request(  # noqa: S310 - scheme is pinned to https above
        request.url,
        data=request.body,
        headers=dict(request.headers),
        method=request.method,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_seconds) as response:
            return HttpReply(
                status=int(response.status),
                headers={k: v for k, v in response.headers.items()},
                body=response.read(),
            )
    except urllib.error.HTTPError as exc:
        # A status the provider chose to return, not a transport failure.
        return HttpReply(
            status=int(exc.code),
            headers={k: v for k, v in (exc.headers or {}).items()},
            body=exc.read(),
        )


def _retry_after(headers: Mapping[str, str]) -> Optional[float]:
    """The ``Retry-After`` advisory in seconds, or ``None``.

    Only the delta-seconds form is read. The HTTP-date form would need the
    server's clock to be trusted and subtracted from ours -- exactly the
    caller-clock mistake the executor's own clock source exists to avoid -- so it
    is reported as "no advisory" instead of being converted on a guess.
    """

    raw = _header(headers, "Retry-After")
    if raw is None:
        return None
    try:
        return float(raw.strip())
    except ValueError:
        return None


def _asserted_preconditions(headers: Mapping[str, str]) -> Tuple[str, ...]:
    """The precondition headers THIS request actually sent, in canonical order."""

    return tuple(name for name in _PRECONDITION_HEADERS if _header(headers, name) is not None)


def build_production_transport(
    *,
    secret_ref: SecretRef,
    vault: SecretStore,
    locator: RequestLocator,
    http_send: HttpSend = urllib_http_send,
    decode: ResultDecode = neutral_decode,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> Transport:
    """Compose the real transport the executor dispatches through in production.

    Returns a :data:`~kiro_crew.connections.control_plane.executor.Transport`: it
    accepts the TRUSTED routing axes the executor passes (``service_id`` /
    ``credential_mode`` off the handle view, never off the handle) and returns a
    :class:`TransportResponse`, so it drops straight into
    :func:`~kiro_crew.connections.control_plane.executor.execute` in place of the
    unit tests' fake.

    Per call, in order: ``locator`` shapes the vendor request (no credential in
    it); :func:`resolve_binding_secret` reads the credential FRESH from the vault;
    it is revealed once into an ``Authorization: Bearer`` header; ``http_send``
    emits it; the reply is mapped to a :class:`TransportResponse`. A 412 carries
    back the preconditions this request asserted plus the server's ``ETag`` --
    and NOTHING when it asserted none, which is what lets the executor set
    ``condition_unknown`` instead of inventing an ``If-Match``.

    Nothing is cached: the secret is re-resolved every call, so a rotated or
    deleted secret takes effect immediately and no plaintext outlives the call.
    Failures are returned, never raised -- an unresolvable secret becomes a 401
    (the executor classifies it ``auth``) and a connectivity failure a 503
    (``temporary``) -- because the transport contract is a structured envelope.
    Details name only the status, the service and the operation: no response body,
    no header values, no credential.
    """

    def _transport(
        *,
        service_id: str,
        credential_mode: CredentialMode,
        descriptor: OperationDescriptor,
        request_args: Mapping[str, Any],
        request_idempotency_key: str = "",
        **_ignored: Any,
    ) -> TransportResponse:
        operation_id = descriptor["operation_id"]
        request = locator(
            service_id=service_id,
            credential_mode=credential_mode,
            descriptor=descriptor,
            request_args=dict(request_args),
            request_idempotency_key=request_idempotency_key,
        )
        try:
            secret = resolve_binding_secret(secret_ref, vault=vault)
        except SecretResolutionError:
            # Typed as an auth failure. The exception's own text is NOT forwarded:
            # it names a vault entry, which the caller does not need in order to
            # know the credential is unavailable.
            return TransportResponse(
                http_status=401,
                detail=f"credential for operation {operation_id} is not available",
            )

        headers = dict(request.headers)
        # The ONE place the plaintext exists, on a local that dies with the call.
        # All three L01 credential modes present as a bearer credential here; a
        # provider needing another header shape is a vendor-owned locator concern.
        headers["Authorization"] = f"Bearer {secret.reveal()}"

        try:
            reply = http_send(
                HttpRequest(
                    method=request.method,
                    url=request.url,
                    headers=headers,
                    body=request.body,
                ),
                timeout_seconds=timeout_seconds,
            )
        except urllib.error.URLError:
            return TransportResponse(
                http_status=503,
                detail=f"could not reach {service_id} for operation {operation_id}",
            )
        except SecretResolutionError:
            # urllib_http_send's https refusal. Nothing was sent.
            return TransportResponse(
                http_status=400,
                detail=f"operation {operation_id} was not dispatched over https",
            )

        if 200 <= reply.status < 300:
            return TransportResponse(http_status=reply.status, result=decode(reply))
        if reply.status == 412:
            return TransportResponse(
                http_status=412,
                preconditions=_asserted_preconditions(headers),
                etag=_header(reply.headers, "ETag"),
                detail=f"precondition failed for operation {operation_id}",
            )
        return TransportResponse(
            http_status=reply.status,
            retry_after_seconds=_retry_after(reply.headers),
            detail=f"{service_id} returned HTTP {reply.status} for operation {operation_id}",
        )

    return _transport


def decode_json_body(reply: HttpReply) -> Mapping[str, Any]:
    """Parse a JSON reply body, or ``{}`` when it is empty or not JSON.

    A helper for a vendor-owned ``decode``: it does the byte/JSON mechanics (this
    module's business) without deciding where a cursor lives (the vendor's).
    """

    if not reply.body:
        return {}
    try:
        parsed = json.loads(reply.body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "PRODUCTION_SCHEMA_VERSION",
    "HttpReply",
    "HttpRequest",
    "HttpSend",
    "RequestLocator",
    "ResultDecode",
    "SecretResolutionError",
    "SecretStore",
    "build_production_transport",
    "decode_json_body",
    "neutral_decode",
    "resolve_binding_secret",
    "urllib_http_send",
]
