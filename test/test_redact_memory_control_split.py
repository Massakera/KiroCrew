"""Control characters must not carry credential material past the memory scrubber.

Both redactors behind ``_redact_memory_field`` decide by matching a pattern against
text. A control character embedded mid-token splits the token, so the pattern cannot
match and the field leaves on an egress path still carrying the credential -- broken
for a consumer that renders it verbatim, whole again for any consumer that drops
control bytes. Separators are spelled as explicit ``bytes`` here so the case under
test is the byte sequence rather than an editor's rendering of it.
"""

from __future__ import annotations

import json
import unicodedata

import pytest

from kiro_crew.dashboard.handlers._shared import _redact_memory_field

#: A documentation-only AWS key id, matched by the plaintext credential pass.
CREDENTIAL = "AKIAIOSFODNN7EXAMPLE"

#: Every character the scrubber removes: the C0 and C1 controls other than the three
#: kept as content, plus the Unicode format characters. The output is checked against
#: this set, because an output holding none of them cannot be reinterpreted or rejoined
#: by any consumer.
INVISIBLE_CHARACTERS = (
    frozenset(
        chr(code)
        for code in list(range(0x00, 0x20)) + list(range(0x7F, 0xA0))
        if chr(code) not in "\t\n\r"
    )
    | frozenset(chr(code) for code in range(0x110000) if unicodedata.category(chr(code)) == "Cf")
    | frozenset("\ufe0f\u034f\u3164\u180b\u17b4\U000e0100")
)

#: Separators made only of control characters. Removing them rejoins the token, so the
#: credential pattern matches and the field is redacted.
CONTROL_ONLY_SEPARATORS = [
    pytest.param(b"\x1b", id="escape"),
    pytest.param(b"\x00", id="c0-nul"),
    pytest.param(b"\x08", id="c0-backspace"),
    pytest.param(b"\x0b", id="c0-vertical-tab"),
    pytest.param(b"\x0c", id="c0-form-feed"),
    pytest.param(b"\x1f", id="c0-unit-separator"),
    pytest.param(b"\x7f", id="delete"),
    pytest.param(b"\x80", id="c1-low"),
    pytest.param(b"\x9b", id="c1-csi"),
    pytest.param(b"\x9c", id="c1-string-terminator"),
    pytest.param(b"\x9d", id="c1-osc"),
    pytest.param(b"\x90", id="c1-dcs"),
    pytest.param(b"\x9e", id="c1-pm"),
    pytest.param(b"\x9f", id="c1-apc"),
    pytest.param(b"\x1b\x1b", id="two-escapes"),
    pytest.param(b"\x00\x9b\x7f", id="mixed-controls"),
]

#: Invisible Unicode code points, whether or not ``unicodedata`` calls them ``Cf``.
#: They render as nothing, so a consumer that drops default-ignorable code points
#: rejoins whatever one of them split.
INVISIBLE_SEPARATORS = [
    pytest.param("\u200b", id="zero-width-space"),
    pytest.param("\u200c", id="zero-width-non-joiner"),
    pytest.param("\u200d", id="zero-width-joiner"),
    pytest.param("\u2060", id="word-joiner"),
    pytest.param("\u00ad", id="soft-hyphen"),
    pytest.param("\u202e", id="right-to-left-override"),
    pytest.param("\ufeff", id="zero-width-no-break-space"),
    pytest.param("\U000e0041", id="tag-latin-a"),
    pytest.param("\ufe0f", id="variation-selector-16"),
    pytest.param("\u034f", id="combining-grapheme-joiner"),
    pytest.param("\u3164", id="hangul-filler"),
    pytest.param("\u180b", id="mongolian-free-variation-selector"),
    pytest.param("\U000e0100", id="variation-selector-17"),
    pytest.param("\u17b4", id="khmer-inherent-vowel"),
]

#: Complete sequences, whose printable payload is content and survives. The token stays
#: split around that payload, which is safe because the output carries no invisible
#: character for a consumer to act on.
PAYLOAD_SEPARATORS = [
    pytest.param(b"\x1b[0m", id="csi-sgr-reset"),
    pytest.param(b"\x9b0m", id="csi-8bit-with-parameters"),
    pytest.param(b"\x1b]0;t\x07", id="osc-through-bel"),
    pytest.param(b"\x9d0;t\x9c", id="osc-8bit-through-st"),
    pytest.param(b"\x1bPq\x1b\\", id="dcs-through-st"),
    pytest.param(b"\x9fx\x9c", id="apc-8bit-through-st"),
]

#: Kept as content, so a token split by one stays split.
CONTENT_SEPARATORS = [
    pytest.param(b"\n", id="newline"),
    pytest.param(b"\t", id="tab"),
    pytest.param(b"\r", id="carriage-return"),
    pytest.param(b"\r\n", id="crlf"),
]

ALL_SEPARATORS = (
    CONTROL_ONLY_SEPARATORS + INVISIBLE_SEPARATORS + PAYLOAD_SEPARATORS + CONTENT_SEPARATORS
)

#: Every separator whose removal rejoins the token, so the credential is matched.
REJOINING_SEPARATORS = CONTROL_ONLY_SEPARATORS + INVISIBLE_SEPARATORS


def _split_credential(separator: bytes | str) -> str:
    """Return ``CREDENTIAL`` with ``separator`` spliced in after its ``AKIA`` prefix."""
    text = separator.decode("latin-1") if isinstance(separator, bytes) else separator
    prefix, suffix = CREDENTIAL[:4], CREDENTIAL[4:]
    return f"{prefix}{text}{suffix}"


#: A value shaped like a Discord bot token, built from repeated characters so the file
#: holds no credential-shaped literal. Its pattern is guarded by a negative lookbehind
#: for a non-word character, which makes it the case where normalising alone is not
#: enough. The runs are sized to the pattern: an ``[MNO]`` anchor, 22 to 30 characters,
#: a six-character middle, then 25 or more.
BOUNDARY_GUARDED_TOKEN = "M" + "A" * 25 + "." + "B" * 6 + "." + "C" * 27


def test_a_word_char_before_an_invisible_char_does_not_hide_the_token() -> None:
    """The scan of the text as stored is what catches a boundary-guarded credential.

    The token's pattern requires a non-word character before it, and the invisible
    character supplies one. Removing it glues the preceding word character onto the
    token, so a scan of the normalised text alone fails to match and the whole token
    would egress. Scanning the original first keeps that verdict.
    """
    stored = f"X\x1b{BOUNDARY_GUARDED_TOKEN}"

    redacted = _redact_memory_field(stored)

    assert BOUNDARY_GUARDED_TOKEN not in redacted
    assert "[REDACTED" in redacted


def test_a_split_boundary_guarded_token_is_still_caught_when_rejoining_helps() -> None:
    """With a clean left boundary, the second pass catches the split token."""
    stored = f"see \x1b{BOUNDARY_GUARDED_TOKEN}"

    redacted = _redact_memory_field(stored)

    assert BOUNDARY_GUARDED_TOKEN not in redacted
    assert "[REDACTED" in redacted


def test_contiguous_credential_is_redacted() -> None:
    """The unsplit case is the control: the pattern matches and the field is scrubbed."""
    redacted = _redact_memory_field(f"never commit {CREDENTIAL}")

    assert CREDENTIAL not in redacted
    assert "[REDACTED" in redacted


@pytest.mark.parametrize("separator", REJOINING_SEPARATORS)
def test_control_split_credential_is_redacted(separator: bytes | str) -> None:
    """A credential split by invisible characters alone is scrubbed, not passed through."""
    redacted = _redact_memory_field(f"never commit {_split_credential(separator)}")

    assert "[REDACTED" in redacted
    assert "IOSFODNN7EXAMPLE" not in redacted


@pytest.mark.parametrize("separator", REJOINING_SEPARATORS)
def test_stripping_never_reassembles_the_credential(separator: bytes | str) -> None:
    """Output carries no whole credential, which is what pins the order of the two steps.

    Redacting first and normalising second leaves this assertion failing while the one
    above still passes: normalisation rejoins the halves the credential pattern just
    failed to match, so the field egresses the complete secret in plaintext.
    """
    redacted = _redact_memory_field(f"never commit {_split_credential(separator)}")

    assert CREDENTIAL not in redacted


@pytest.mark.parametrize("separator", ALL_SEPARATORS)
def test_no_invisible_character_reaches_the_caller(separator: bytes | str) -> None:
    """The output holds no invisible character, whatever split went in.

    This is the guarantee the whole approach rests on. With none left,
    no consumer can reinterpret the text or drop bytes out of it, so a token that still
    looks split to the scanner looks split to every consumer as well.
    """
    redacted = _redact_memory_field(f"never commit {_split_credential(separator)}")

    assert [ch for ch in redacted if ch in INVISIBLE_CHARACTERS] == []


@pytest.mark.parametrize("separator", ALL_SEPARATORS)
def test_the_credential_is_never_recoverable_from_the_output(separator: bytes | str) -> None:
    """No split leaves the credential contiguous in what the caller receives."""
    redacted = _redact_memory_field(f"never commit {_split_credential(separator)}")

    assert CREDENTIAL not in redacted


@pytest.mark.parametrize("separator", PAYLOAD_SEPARATORS)
def test_a_sequence_payload_survives_as_text(separator: bytes) -> None:
    """A sequence's printable bytes are content, so they are kept rather than deleted.

    Consuming them would mean deleting however much visible text sits between an
    introducer and the next terminator, which loses a user's stored note instead of
    sanitising it. The token stays split around the payload, and safely so: the bytes
    that would have told a terminal to hide it are gone.
    """
    redacted = _redact_memory_field(f"never commit {_split_credential(separator)}")
    printable = [ch for ch in separator.decode("latin-1") if ch not in INVISIBLE_CHARACTERS]

    for character in printable:
        assert character in redacted


def test_visible_text_between_an_introducer_and_a_terminator_is_kept() -> None:
    """A stored note bracketed by control characters keeps every visible word."""
    note = "note: \x9d my deploy runbook lives here \x9c see also"

    assert _redact_memory_field(note) == "note:  my deploy runbook lives here  see also"


@pytest.mark.parametrize("separator", CONTENT_SEPARATORS)
def test_whitespace_is_preserved_as_content(separator: bytes) -> None:
    """Tab, newline and carriage return survive, so stored documents are unchanged.

    Dropping the carriage return alone would rewrite every CRLF document into LF, which
    is why it sits with the other two rather than with the controls.
    """
    text = separator.decode("latin-1")

    assert text in _redact_memory_field(f"first{text}second")


def test_crlf_line_endings_round_trip() -> None:
    """A CRLF document comes back byte for byte."""
    document = "line one\r\nline two\r\nline three"

    assert _redact_memory_field(document) == document


def test_control_split_credential_inside_a_container() -> None:
    """The recursion reaches list and dict members, and shape is preserved."""
    by_escape = _split_credential(b"\x1b")
    by_nul = _split_credential(b"\x00")
    payload = {
        "superseded": [f"never commit {by_escape}"],
        "nested": {"rule": f"never commit {by_nul}"},
    }

    redacted = _redact_memory_field(payload)

    assert isinstance(redacted, dict)
    assert isinstance(redacted["superseded"], list)
    assert CREDENTIAL not in redacted["superseded"][0]
    assert CREDENTIAL not in redacted["nested"]["rule"]


def test_ordinary_text_is_unchanged() -> None:
    """Text with no credential and no invisible character passes through byte for byte."""
    value = "commit early, commit often, one\ttab and one\nnewline"

    assert _redact_memory_field(value) == value


def test_binary_is_still_dropped() -> None:
    """Bytes remain unscannable and so remain dropped rather than returned unread."""
    assert _redact_memory_field(b"\x1b[0m") is None
    assert _redact_memory_field(memoryview(b"\x1b[0m")) is None


def test_a_format_char_between_controls_does_not_survive() -> None:
    """Controls cannot shelter a format character inside an ASCII credential.

    Removing the controls first is what makes the flank judgement read the credential's
    own characters rather than the controls that were about to be removed.
    """
    redacted = _redact_memory_field("never commit AKIA\x9b\u200d\x9cIOSFODNN7EXAMPLE")

    assert "[REDACTED" in redacted
    assert CREDENTIAL not in redacted
    assert "\u200d" not in redacted


def test_a_json_encoded_field_does_not_carry_the_credential_out() -> None:
    """Encoding hides the control character from the scan, so the payload is scanned too.

    A memory row carries the same value as text and as a JSON document. JSON writes a
    control character as the six printable characters of an escape, so the control pattern
    finds nothing to remove and the credential it splits matches neither redactor pass.
    Whatever decodes the document gets the control character back, where it renders as
    nothing and the credential reads as whole.
    """
    stored = f"never commit {CREDENTIAL[:8]}\x01{CREDENTIAL[8:]}"
    row = {"text": stored, "value_json": json.dumps(stored)}

    out = _redact_memory_field(row)

    assert CREDENTIAL not in out["text"]
    assert "[REDACTED" in out["value_json"]
    decoded = json.loads(out["value_json"])
    assert CREDENTIAL not in decoded
    assert "\x01" not in decoded


def test_a_json_encoded_object_field_is_scanned_through_its_values() -> None:
    """A document decoding to an object is scanned value by value, shape preserved."""
    stored = f"never commit {CREDENTIAL[:8]}\x01{CREDENTIAL[8:]}"

    out = _redact_memory_field({"value_json": json.dumps({"rule": stored, "tier": "always"})})

    decoded = json.loads(out["value_json"])
    assert CREDENTIAL not in decoded["rule"]
    assert decoded["tier"] == "always"


def test_a_json_field_holding_nothing_sensitive_is_passed_through_unchanged() -> None:
    """Scanning a payload must not re-spell a document it had no reason to touch.

    The document here is spelled with spacing no serialiser would choose, so passing it
    through and re-serialising it are distinguishable.
    """
    document = '{"count":3,  "note":  "commit early, commit often"}'

    assert _redact_memory_field({"value_json": document})["value_json"] == document
    assert _redact_memory_field("123") == "123"
    assert _redact_memory_field('{"broken"') == '{"broken"'
    assert _redact_memory_field("") == ""
