"""W01 · L01: the result envelope a connector operation returns.

A pure-type envelope, in the ``TypedDict`` + module-level schema-version shape
the connections subsystem already uses (``l0_probe.ProbeResult`` and
``l1_smoke.SmokeResult`` are the in-repo precedent). It does no IO: it describes
the OUTCOME of a call plus the DATA that call returned, so a downstream stream
and a runtime dispatch read one vocabulary for "did it fully succeed, partly
succeed, or is there more to fetch" -- and one vocabulary for "here is what came
back" -- instead of each inventing its own.

The envelope used to describe the outcome and NOTHING ELSE, which made the
success path functionally empty: a caller that got ``{"status": "ok",
"next_cursor": None}`` held no items, no object and no bytes, so every operation
in the plane succeeded at returning nothing. :data:`OperationPayload` closes
that. It is ONE neutral channel with exactly three preserved shapes -- a
COLLECTION (:class:`CollectionPayload`), a SINGLE OBJECT
(:class:`ObjectPayload`), and RAW BYTES (:class:`BytesPayload`, which is how an
Office document travels) -- because those are the three things a connector
operation actually returns and collapsing any of them into another loses data:
a collection flattened to an object loses every item but one, and bytes coerced
to text corrupt an ``xlsx`` irreversibly.

The ``status`` axis is two-valued and deliberately distinct from the
error taxonomy in :mod:`kiro_crew.connections.control_plane.errors`:

- ``ok`` -- the operation completed and returned everything it was asked for.
- ``partial`` -- the operation returned a usable-but-incomplete result. This is
  the SUCCESS-side ``partial``: some data came back and the caller may act on
  it. It is NOT the same concept as the RUN-01 error class ``partial`` in
  ``errors.py``, which classifies a FAILURE that partially applied; the two
  live on opposite sides of the success/failure line on purpose and a consumer
  must not fold them together.

Pagination is carried by ``next_cursor``: an OPAQUE continuation token when
more results remain, or ``None`` when the result is complete. It is deliberately
a single opaque string and never the vendor's raw locator shape -- one operation
uses ``@odata.nextLink``, another a ``page``/``perPage`` pair, another
``queryMore``; the manifest declares each operation's own ``pagination``
contract, and this envelope only needs to say "here is where you resume, or you
are done". A ``next_cursor`` is meaningful for both ``ok`` and ``partial``: a
fully-successful page can still have a successor, and the terminal page carries
``None``.

``next_cursor`` stays the machinery a paging walk advances on -- but a COLLECTION
payload carries the SAME cursor a second time, on
:attr:`CollectionPayload.next_cursor`. That is deliberate duplication, not an
oversight: a consumer that receives a collection and hands it on must be able to
hand the cursor on WITH it, and a cursor that lives only on the outer envelope
gets dropped the moment the items are passed to something else -- which is
silent truncation, indistinguishable from a complete result. To keep the two from
ever disagreeing, both are written by ONE constructor,
:func:`result_with_payload`, which DERIVES the envelope's cursor from the
payload rather than accepting it separately.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar, Literal, Mapping, Optional, Tuple, TypedDict, Union

#: Bumped when this envelope's shape changes, mirroring the sibling modules.
#:
#: ``2`` added the ``payload`` field to :class:`OperationResult`. It is a
#: REQUIRED key, so this is a breaking change for a producer and an old pin would
#: decode a v2 envelope wrong (it would not know a payload could be there at all,
#: and would keep reading a success as data-free). Making it optional was the
#: alternative and was rejected: a silently-absent payload is the exact defect
#: being fixed, and a required key forces every producer to say ``None`` on
#: purpose instead of omitting it by accident.
RESULT_SCHEMA_VERSION = 2

#: The operation completed and returned everything asked for.
RESULT_STATUS_OK = "ok"
#: The operation returned a usable-but-incomplete result (success side; NOT the
#: RUN-01 error class ``partial`` -- see the module docstring and ``errors.py``).
RESULT_STATUS_PARTIAL = "partial"

#: The result envelope's own two-value success axis.
ResultStatus = Literal["ok", "partial"]

#: Tuple form of :data:`ResultStatus`'s closed set.
RESULT_STATUSES: tuple[ResultStatus, ...] = ("ok", "partial")

#: A list of records plus the cursor that continues it.
PAYLOAD_KIND_COLLECTION = "collection"
#: Exactly one record.
PAYLOAD_KIND_OBJECT = "object"
#: Raw bytes with a declared media type (an Office document, a PDF, an image).
PAYLOAD_KIND_BYTES = "bytes"

#: The payload channel's own closed discriminant.
PayloadKind = Literal["collection", "object", "bytes"]

#: Tuple form of :data:`PayloadKind`'s closed set.
PAYLOAD_KINDS: tuple[PayloadKind, ...] = ("collection", "object", "bytes")

#: What a :class:`BytesPayload` reports when the provider declared no media type.
#: The generic binary type, never a guess derived from the bytes themselves: this
#: envelope reports what it was TOLD, and sniffing would make the plane assert a
#: format it did not read.
DEFAULT_MEDIA_TYPE = "application/octet-stream"


@dataclass(frozen=True)
class CollectionPayload:
    """A page of records AND the cursor that continues it.

    ``items`` -- the records this page returned, in the provider's order. A tuple
    because the envelope is handed across a seam and a caller must not be able to
    mutate another caller's page.

    ``next_cursor`` -- the opaque continuation token for the page AFTER this one,
    or ``None`` on the terminal page. It is here, and not only on the outer
    :class:`OperationResult`, because the items and their continuation are one
    fact: a consumer handed just ``items`` cannot tell a complete collection from
    the first page of a hundred, and would report a truncation as a result. Both
    copies are written by :func:`result_with_payload` from this one field, so they
    cannot drift.

    ``kind`` -- the closed discriminant, so a consumer can switch on it instead of
    ``isinstance`` when it is routing rather than unpacking.
    """

    items: Tuple[Mapping[str, Any], ...] = ()
    next_cursor: Optional[str] = None
    kind: ClassVar[PayloadKind] = "collection"


@dataclass(frozen=True)
class ObjectPayload:
    """Exactly ONE record -- a single fetch, or the thing a write created.

    ``object`` -- the record itself. Kept as a :class:`Mapping` rather than
    flattened into the envelope so a field named ``status`` or ``next_cursor`` in
    a provider's object cannot collide with the envelope's own machinery.

    It is deliberately NOT a one-element :class:`CollectionPayload`: an operation
    whose contract is "one object" must not present as a collection a caller then
    pages, and a caller that asked for one object must not have to unwrap a list
    and guess what more than one element would have meant.
    """

    object: Mapping[str, Any]
    kind: ClassVar[PayloadKind] = "object"


@dataclass(frozen=True)
class BytesPayload:
    """RAW BYTES, never text-coerced and never dropped.

    This is how an Office document (``xlsx`` / ``docx`` / ``pptx``), a PDF, or any
    other binary body travels. The two properties that matter are both negative:

    * **Never coerced.** Nothing on this path calls ``.decode()``. An ``xlsx`` is
      a ZIP container: it is not valid UTF-8, so a decode either raises or (with
      ``errors="replace"``) silently substitutes replacement characters and
      produces a file that no longer opens. ``data`` is the provider's bytes,
      byte for byte.
    * **Never dropped.** The body used to be discarded on the way to the envelope,
      which turned a downloaded workbook into a bare ``status``.

    ``data`` -- the exact bytes. ``media_type`` -- what the provider DECLARED the
    bytes are (the ``Content-Type`` value, parameters included, verbatim), or
    ``application/octet-stream`` when it declared nothing; it is reported, never
    sniffed from the bytes. ``filename`` -- the provider-supplied name when there
    was one, so a consumer writing the bytes out has something to call them.
    """

    data: bytes
    media_type: str = DEFAULT_MEDIA_TYPE
    filename: Optional[str] = None
    kind: ClassVar[PayloadKind] = "bytes"


#: The ONE neutral data channel: a success carries exactly one of these shapes,
#: or ``None`` when the operation genuinely returned no data (a 204, an empty
#: acknowledgement). A union rather than one struct with three optional members,
#: so "a collection AND some bytes" is not representable and a consumer that
#: handled the three cases has handled all of them.
OperationPayload = Union[CollectionPayload, ObjectPayload, BytesPayload]


class OperationResult(TypedDict):
    """The outcome envelope for one connector operation invocation.

    Every field present, matching the sibling descriptors' shape.

    ``status`` -- ``ok`` or ``partial`` (success axis; a failure is carried by
    :mod:`kiro_crew.connections.control_plane.errors`, not by this envelope).
    ``next_cursor`` -- an opaque continuation token when more results remain, or
    ``None`` when the result is complete.
    ``payload`` -- the DATA the operation returned, as exactly one of the three
    :data:`OperationPayload` shapes, or ``None`` when it returned none. Build it
    with :func:`result_with_payload` so a collection's cursor cannot disagree with
    ``next_cursor``.
    """

    status: ResultStatus
    next_cursor: str | None
    payload: OperationPayload | None


def result_with_payload(
    payload: Optional[OperationPayload],
    *,
    status: ResultStatus = "ok",
) -> OperationResult:
    """Build an envelope whose ``next_cursor`` is DERIVED from ``payload``.

    The one constructor for a result carrying data, and the reason
    :attr:`CollectionPayload.next_cursor` and
    :attr:`OperationResult.next_cursor` cannot drift: the cursor is passed ONCE,
    on the collection, and copied out to the envelope here. There is deliberately
    no ``next_cursor`` parameter -- accepting one would re-create the two
    independently-settable copies this exists to prevent, and the disagreement
    would be silent (a paging walk would stop while the collection still claimed
    a successor, or walk on while the collection claimed to be terminal).

    An :class:`ObjectPayload`, a :class:`BytesPayload` and ``None`` have no
    continuation, so the envelope's cursor is ``None`` for all three: a single
    object and a byte stream are not paged shapes, and inventing a cursor for them
    would tell a caller to fetch a page that does not exist.
    """

    next_cursor = payload.next_cursor if isinstance(payload, CollectionPayload) else None
    return {"status": status, "next_cursor": next_cursor, "payload": payload}
