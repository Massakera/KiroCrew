"""Push receipts: evidence that the prepare-pr push gate ran on the tree being published.

WHAT THIS IS
------------
The prepare-pr skill's pre-push stale-base guard (``push_guard.py``) prints a verdict.
A verdict alone leaves no trace, so a publish whose gate was skipped is byte-identical
to one whose gate passed, and the difference surfaces only when a reviewer, a conflict
or a CI red exposes it.

On a PASS the guard writes a RECEIPT naming the worktree it judged and the commit
it judged, and the publish allow path in the shell-command floor refuses a publish in
an ENROLLED worktree whose current ``HEAD`` carries no such receipt.

THE BLAST RADIUS THAT SHAPES EVERY DECISION BELOW
-------------------------------------------------
The publish floor is global -- the sole enforcement every publish in every repository
passes through -- so a defect here does not degrade one workflow, it wedges all of
them. Three properties bound that risk, and each is a deliberate limit:

* **Opt-in per worktree.** Nothing is checked until an operator lists a worktree in
  ``push_receipts.json`` under the data home. With no such file a publish pays one
  failed ``open`` and any other command pays nothing at all: the check is reached
  only from inside the git-publish branch of ``is_denied``.
* **Fail OPEN on an internal error, fail CLOSED only on a definite answer.** An
  absent receipt and a receipt naming another commit are definite answers, and both
  deny. A worktree whose ``HEAD`` cannot be resolved, a policy file that cannot be
  read, a receipt that cannot be opened -- none of those is an answer about the
  receipt, so each allows the publish and logs at WARNING.
* **An explicit, audited override.** A non-empty ``KIROCREW_PUSH_RECEIPT_OVERRIDE``
  in the gateway's environment allows the publish and emits one SEL
  ``push_receipt_override`` event carrying its value, which is why the value is the
  operator's REASON rather than a bare flag: it exists for the case where the receipt
  mechanism is itself the broken part, and that case belongs in the audit trail.

WHAT A RECEIPT DOES AND DOES NOT PROVE
--------------------------------------
It proves the guard passed on THIS worktree at THIS commit. The receipt is stored
under a name derived from the worktree's own git directory, repeats that directory
inside itself, and is compared against the worktree's CURRENT ``HEAD`` -- so amending
after the gate, copying a sibling worktree's receipt into this name, and publishing a
worktree that never ran the gate all deny.

It proves nothing against an agent that sets out to forge one. The store is a plain
directory the agent's own shell can write, and signing would not change that: the SEL
HMAC key is itself agent-readable (``AGENTS.md``, keystone note), so a signature would
be exactly as forgeable as the file it signs. The defect this closes is the SILENT
one -- a gate skipped by omission, indistinguishable from a gate that passed -- and
the sanctioned bypass is the audited override. Making a receipt unforgeable needs it
written by a process the agent cannot reach, which is a different change.

A git ``pre-push`` hook is deliberately NOT the mechanism: the gateway pins
``core.hooksPath=/dev/null`` so a repository-planted hook cannot execute host-side,
and that decision stands.

WHY THE READER NEVER GUESSES WHICH REPOSITORY IS BEING PUSHED
-------------------------------------------------------------
``is_denied`` receives a command line and nothing else -- no working directory (the
gateway's own cwd is not the cwd the tool runs in) -- so there is no trustworthy way
to read the pushed repository out of the call. The check therefore asks its question
of the ENROLLED set instead: every enrolled worktree must present a receipt for its
current ``HEAD``.

The consequence is stated rather than bounded away: while ANY enrolled worktree is
unreceipted -- the ordinary state between guard runs, since a commit or a fetch
invalidates -- EVERY publish from this gateway is refused, including one in an
unrelated repository. That is why enrollment is per-worktree opt-in, why over-refusal
is the chosen direction, and why the refusal text says so (``CROSS_REPO_NOTE``) instead
of leaving an operator to work it out from a message naming a worktree they are not
standing in. Scoping the question to the repository being pushed needs the publish
floor to learn the tool's working directory, which is a change to the floor's own
interface and belongs on its own.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import stat
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

#: Receipt payload version. A reader refuses a version it does not know rather than
#: guessing at fields, because a guess on this file is an allow.
RECEIPT_VERSION = 1

#: Operator-owned enrollment file, directly under the data home.
POLICY_FILE_NAME = "push_receipts.json"

#: Receipt store, a sibling of the enrollment file.
RECEIPT_DIR_NAME = "push_receipt_store"

#: Environment variable carrying the operator's override REASON (any non-empty value
#: overrides; the value is audited, so a reason is what belongs there).
OVERRIDE_ENV = "KIROCREW_PUSH_RECEIPT_OVERRIDE"

#: SEL event type for a use of the override.
OVERRIDE_EVENT_TYPE = "push_receipt_override"

#: Both git object-format hash widths. Anchored: a prefix match would accept a sha
#: with trailing junk, and this value is compared for equality against ``HEAD``.
_SHA_RE = re.compile(r"\A[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")

#: A symbolic ``HEAD`` must name a ref under ``refs/``. The character class excludes
#: the separators a traversal would need, and ``..`` is rejected outright below, so a
#: planted ``ref: ../../somewhere`` cannot turn a ref read into an arbitrary read.
_REF_RE = re.compile(r"\Arefs/[A-Za-z0-9._/@+-]{1,240}\Z")

#: Read budgets. Every file read here is attacker-shaped in the sense that its size is
#: not ours to assume, and this code runs inside the permission gate, so each read is
#: capped rather than slurped.
_MAX_POINTER_BYTES = 4096
_MAX_RECEIPT_BYTES = 8192
_MAX_POLICY_BYTES = 65536
_MAX_PACKED_REFS_BYTES = 4 * 1024 * 1024

#: Enrollment is an operator's short list of worktrees. A longer one is not evaluated
#: partially (that would silently allow the entries past the cap); it is reported as
#: unjudgeable, which allows and logs.
_MAX_ENROLLED = 64

#: Per-worktree verdicts. DENY and UNKNOWN are kept apart all the way to the caller
#: because they have opposite fail directions.
VERDICT_OK = "ok"
VERDICT_DENY = "deny"
VERDICT_UNKNOWN = "unknown"

#: Label the refusal reports. Not a catalog rule pattern: this gate has no
#: ``BUILTIN_DENIED_RULES`` row to toggle, because its enable IS the enrollment file.
DENY_LABEL = "prepare-pr push receipt"

#: Refusal text for the gate to run, kept in one place so the message and the spec
#: cannot drift apart. Named as the script the skill actually runs -- not as a
#: ready-to-paste command line, because the interpreter and the skill's location on
#: disk differ per install and a command that does not run is worse than a name.
GATE_COMMAND = "prepare-pr's scripts/push_guard.py"

#: Appended to every refusal, because the reader is not necessarily standing in the
#: worktree the refusal names. ``is_denied`` gets a command line and no working
#: directory, so it asks its question of the whole enrolled set: while ANY enrolled
#: worktree is unreceipted, every publish from this gateway is refused, including one in
#: an unrelated repository. Saying so in the refusal is the difference between an
#: operator who knows what to fix and one who reads the message as nonsense.
CROSS_REPO_NOTE = (
    " This refusal covers every publish from this gateway, not only one in the worktree "
    "named above: the check cannot see which repository a push is for, so any enrolled "
    "worktree without a current receipt refuses all of them."
)


def _config_dir() -> Path:
    """Data home, through the ONE resolver the rest of the product uses.

    Deliberately no fallback spelling of ``~/.kiro/crew``: a second resolver is a
    second store, and a writer and a reader that disagree about where a receipt lives
    fail as "no receipt" while the file sits on disk. Imported lazily so this module
    stays importable from the skill script without paying for the package graph at
    ``kiro_crew.security`` import time.
    """
    from kiro_crew.config.paths import config_dir

    return config_dir()


def policy_path() -> Path:
    """Path of the operator's enrollment file."""
    return _config_dir() / POLICY_FILE_NAME


def receipt_dir() -> Path:
    """Directory holding one receipt per enrolled worktree."""
    return _config_dir() / RECEIPT_DIR_NAME


def _read_text_capped(path: Path, cap: int) -> str:
    """Read *path* as UTF-8, refusing anything over *cap* bytes.

    Raises ``OSError`` when the file cannot be read and ``ValueError`` when it is too
    large or not UTF-8. The two are distinct on purpose: the caller allows on the
    former and refuses on the latter.
    """
    with open(path, "rb") as handle:
        data = handle.read(cap + 1)
    if len(data) > cap:
        raise ValueError(f"{path.name} is larger than {cap} bytes")
    return data.decode("utf-8")


def enrolled_worktrees() -> tuple[str, ...]:
    """Worktree paths the operator enrolled, verbatim.

    Returns an empty tuple when the enrollment file does not exist -- the not-opted-in
    case, and the only one that is free. Raises ``OSError`` / ``ValueError`` when the
    file exists but cannot be read as a policy; the caller treats that as an internal
    error and allows, because a policy it cannot read names no worktree it can judge.
    """
    try:
        raw = _read_text_capped(policy_path(), _MAX_POLICY_BYTES)
    except FileNotFoundError:
        return ()
    document = json.loads(raw)
    if not isinstance(document, dict):
        raise ValueError("push receipt policy is not a JSON object")
    entries = document.get("enrolled", [])
    if not isinstance(entries, list):
        raise ValueError("push receipt policy 'enrolled' is not a list")
    if len(entries) > _MAX_ENROLLED:
        raise ValueError(f"push receipt policy enrolls more than {_MAX_ENROLLED} worktrees")
    return tuple(entry for entry in entries if isinstance(entry, str) and entry.strip())


def git_dir_for(worktree: str | Path) -> Path:
    """Resolve the git directory that holds ``HEAD`` for *worktree*.

    A LINKED worktree has its own git directory under ``.git/worktrees/<name>`` with
    its own ``HEAD``, so this is what distinguishes two worktrees of one repository --
    which matters, because they sit on different commits and a receipt for one must
    never authorize a publish from the other.

    Raises ``OSError`` / ``ValueError`` when it cannot be resolved.
    """
    root = Path(worktree).expanduser().resolve(strict=True)
    dot_git = root / ".git"
    if stat.S_ISDIR(dot_git.stat().st_mode):
        return dot_git.resolve(strict=True)
    pointer = _read_text_capped(dot_git, _MAX_POINTER_BYTES).strip()
    prefix = "gitdir:"
    if not pointer.startswith(prefix):
        raise ValueError(".git is a file that names no gitdir")
    target = Path(pointer[len(prefix) :].strip())
    if not target.is_absolute():
        target = root / target
    resolved = target.resolve(strict=True)
    if not (resolved / "HEAD").is_file():
        raise ValueError("gitdir pointer names a directory with no HEAD")
    return resolved


def _common_dir(git_dir: Path) -> Path:
    """Shared git directory for *git_dir*, which is *git_dir* itself for a main tree.

    A linked worktree keeps ``HEAD`` locally and shares ``refs/`` and ``packed-refs``
    with the repository, naming the shared directory in its ``commondir`` file.
    """
    try:
        pointer = _read_text_capped(git_dir / "commondir", _MAX_POINTER_BYTES).strip()
    except (OSError, ValueError):
        return git_dir
    if not pointer:
        return git_dir
    target = Path(pointer)
    if not target.is_absolute():
        target = git_dir / target
    try:
        return target.resolve(strict=True)
    except OSError:
        return git_dir


def head_sha(git_dir: Path) -> str:
    """Current commit of the working tree owning *git_dir*.

    Reads files rather than running ``git``: this is called from the permission gate,
    where a subprocess is both a stall on the event loop and a dependency on a git
    binary the gate has no business needing.

    Raises ``OSError`` / ``ValueError`` when ``HEAD`` cannot be resolved to a commit.
    """
    head = _read_text_capped(git_dir / "HEAD", _MAX_POINTER_BYTES).strip()
    if _SHA_RE.match(head):
        return head
    marker = "ref:"
    if not head.startswith(marker):
        raise ValueError("HEAD is neither a commit nor a symbolic ref")
    ref = head[len(marker) :].strip()
    if not _REF_RE.match(ref) or ".." in ref:
        raise ValueError("HEAD names an unusable ref")
    common = _common_dir(git_dir)
    return _resolve_ref(git_dir, common, ref)


def _resolve_ref(git_dir: Path, common: Path, ref: str) -> str:
    """Commit *ref* names in the repository owning *git_dir*.

    Reads files rather than running ``git`` for the same reason ``head_sha`` does: the
    caller is the permission gate. Per-worktree refs live in the worktree's own git
    directory and shared refs in the common one, and a loose ref wins over
    ``packed-refs`` -- exactly as git resolves it.

    Raises ``ValueError`` when the ref resolves to no commit.
    """
    for root in (git_dir, common):
        try:
            loose = _read_text_capped(root / ref, _MAX_POINTER_BYTES).strip()
        except (OSError, ValueError):
            continue
        if _SHA_RE.match(loose):
            return loose
    try:
        packed = _read_text_capped(common / "packed-refs", _MAX_PACKED_REFS_BYTES)
    except (OSError, ValueError) as exc:
        raise ValueError(f"{ref} resolves to no commit") from exc
    for line in packed.splitlines():
        if not line or line[0] in "#^":
            continue
        parts = line.split(" ", 1)
        if len(parts) == 2 and parts[1].strip() == ref and _SHA_RE.match(parts[0]):
            return parts[0]
    raise ValueError(f"{ref} resolves to no commit")


def repo_identity(git_dir: Path) -> str:
    """Stable file-name-safe identity for the working tree owning *git_dir*.

    A digest of the resolved git directory: stable across runs, distinct per worktree,
    and free of the path separators and case-folding a raw path would bring into a
    file name.
    """
    return hashlib.sha256(os.fsencode(str(git_dir))).hexdigest()[:32]


def receipt_path(git_dir: Path) -> Path:
    """Path of the receipt for the working tree owning *git_dir*."""
    return receipt_dir() / f"{repo_identity(git_dir)}.json"


def base_ref_for(base: str) -> str:
    """Remote-tracking ref the guard's freshness verdict is about.

    The guard judges ``HEAD`` against the base it just fetched, so the ref that carries
    that base locally is what a later reader must re-read to know the verdict still
    holds. One spelling, here, so the writer and the reader cannot disagree.
    """
    return f"refs/remotes/origin/{base}"


def clear_receipt(worktree: str | Path) -> None:
    """Remove *worktree*'s receipt, if it has one.

    The guard calls this BEFORE it runs its checks, so a run that goes on to refuse
    leaves no receipt behind. Without it a pass at one base survives a later refusal
    and keeps authorizing the publish the refusal was about.

    Absence is success -- there is nothing to invalidate. Any other failure raises, so
    the caller can refuse rather than proceed on a receipt it could not clear.
    """
    with contextlib.suppress(FileNotFoundError):
        os.unlink(receipt_path(git_dir_for(worktree)))


def write_receipt(worktree: str | Path, *, base: str, mode: str, head: str, base_sha: str) -> Path:
    """Record that the push guard passed on *worktree* at *head* against *base_sha*.

    Written atomically (``mkstemp`` in the destination directory, then ``os.replace``)
    so a reader never sees a half-written receipt, and with the strict permissions
    ``mkstemp`` gives every temporary file (0600) rather than a ``chmod`` after the
    fact, which would leave a window. The directory is 0700 on POSIX.

    The revisions are ARGUMENTS, never re-read here. The guard judged one specific pair
    -- this commit, against that base -- and re-reading the refs at write time would
    record whatever they point at a moment later, so a commit or fetch landing between
    the check and the write would produce a receipt for a state nothing checked. The
    caller owns that pair and passes it; a value that is not a commit is refused rather
    than stored.

    Raises on any failure. The caller reports it rather than leaving the operator to
    discover a missing receipt at publish time.
    """
    if not _SHA_RE.match(head):
        raise ValueError("head is not a commit")
    if not _SHA_RE.match(base_sha):
        raise ValueError("base_sha is not a commit")
    git_dir = git_dir_for(worktree)
    payload = {
        "version": RECEIPT_VERSION,
        "gitdir": str(git_dir),
        "sha": head,
        "base_ref": base_ref_for(base),
        "base_sha": base_sha,
        "mode": str(mode),
        "ts": int(time.time()),
    }
    target = receipt_path(git_dir)
    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        os.chmod(parent, 0o700)  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions -- 0o700 intentionally keeps the receipt store owner-only while retaining directory traversal; Semgrep's suggested 0o644 would grant world-read and remove traversal.  # noqa: E501  # fmt: skip
    handle_fd, tmp_name = tempfile.mkstemp(dir=str(parent), prefix=".receipt-", suffix=".tmp")
    try:
        with os.fdopen(handle_fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise
    return target


def _absent_detail(worktree: str, sha: str) -> str:
    return (
        f"no prepare-pr push receipt for the current HEAD ({sha[:12]}) of {worktree}. "
        f"Run the push guard ({GATE_COMMAND}) and publish again, or set "
        f"{OVERRIDE_ENV}=<reason> when the receipt mechanism is itself broken."
    )


def _stale_detail(worktree: str, sha: str, recorded: str, context: str) -> str:
    return (
        f"the prepare-pr push receipt for {worktree} names {recorded[:12]}, not the "
        f"current HEAD {sha[:12]} -- the tree changed after the gate passed{context}. "
        f"Re-run the push guard ({GATE_COMMAND}) and publish again, or set "
        f"{OVERRIDE_ENV}=<reason> when the receipt mechanism is itself broken."
    )


def _recorded_context(document: dict) -> str:
    """Trailing clause naming WHEN the receipt was written and by WHICH check.

    An operator reading a refusal has two questions the commit shas do not answer: how
    old the superseded pass is, and which of the guard's two modes made it -- they assert
    different things, so "the post-squash check passed an hour ago" and "the pre-squash
    check passed a minute ago" call for different next steps.

    Renders nothing when either field is absent or the wrong type: a receipt is a file,
    its shape is not ours to assume, and a refusal must not fail while explaining itself.
    """
    parts = []
    mode = document.get("mode")
    if isinstance(mode, str) and mode:
        parts.append(f"by the {mode} check")
    ts = document.get("ts")
    if isinstance(ts, int) and not isinstance(ts, bool) and ts > 0:
        minutes = max(0, int(time.time()) - ts) // 60
        parts.append(f"{minutes} minute(s) ago")
    return f" (recorded {', '.join(parts)})" if parts else ""


def _moved_base_detail(worktree: str, base_ref: str, current: str, context: str) -> str:
    return (
        f"the prepare-pr push receipt for {worktree} was judged against {base_ref} at a "
        f"commit that is no longer there -- it now names {current[:12]}, so the "
        f"freshness verdict{context} no longer holds. Re-run the push guard "
        f"({GATE_COMMAND}) and publish again, or set {OVERRIDE_ENV}=<reason> when the "
        "receipt mechanism is itself broken."
    )


def _invalid_detail(worktree: str, why: str) -> str:
    return (
        f"the prepare-pr push receipt for {worktree} is not usable ({why}). Re-run the "
        f"push guard ({GATE_COMMAND}) and publish again, or set {OVERRIDE_ENV}=<reason> "
        "when the receipt mechanism is itself broken."
    )


def worktree_verdict(worktree: str) -> tuple[str, str]:
    """Judge one enrolled worktree; return ``(verdict, detail)``.

    ``VERDICT_DENY`` is reserved for definite answers: the receipt is absent, names
    another commit, or is readable but not a receipt this reader trusts (wrong
    version, wrong shape, or recorded against a different git directory -- which is
    what a receipt copied from a sibling worktree into this name looks like).

    ``VERDICT_UNKNOWN`` is every case that is not an answer: the worktree's own
    ``HEAD`` could not be resolved, or the receipt could not be opened at all. The
    caller allows on those.
    """
    try:
        git_dir = git_dir_for(worktree)
        sha = head_sha(git_dir)
    except (OSError, ValueError) as exc:
        return VERDICT_UNKNOWN, f"{worktree}: HEAD unresolvable ({type(exc).__name__})"
    path = receipt_path(git_dir)
    try:
        raw = _read_text_capped(path, _MAX_RECEIPT_BYTES)
    except FileNotFoundError:
        return VERDICT_DENY, _absent_detail(worktree, sha)
    except ValueError as exc:
        return VERDICT_DENY, _invalid_detail(worktree, str(exc))
    except OSError as exc:
        return VERDICT_UNKNOWN, f"{worktree}: receipt unreadable ({type(exc).__name__})"
    try:
        document = json.loads(raw)
    except ValueError:
        return VERDICT_DENY, _invalid_detail(worktree, "not JSON")
    if not isinstance(document, dict):
        return VERDICT_DENY, _invalid_detail(worktree, "not a JSON object")
    if document.get("version") != RECEIPT_VERSION:
        return VERDICT_DENY, _invalid_detail(worktree, "unknown receipt version")
    recorded_dir = document.get("gitdir")
    if not isinstance(recorded_dir, str) or recorded_dir != str(git_dir):
        return VERDICT_DENY, _invalid_detail(worktree, "recorded against another git directory")
    recorded_sha = document.get("sha")
    if not isinstance(recorded_sha, str) or not _SHA_RE.match(recorded_sha):
        return VERDICT_DENY, _invalid_detail(worktree, "records no commit")
    if recorded_sha != sha:
        return VERDICT_DENY, _stale_detail(worktree, sha, recorded_sha, _recorded_context(document))
    recorded_base_ref = document.get("base_ref")
    if (
        not isinstance(recorded_base_ref, str)
        or not _REF_RE.match(recorded_base_ref)
        or ".." in recorded_base_ref
    ):
        return VERDICT_DENY, _invalid_detail(worktree, "records no base ref")
    recorded_base_sha = document.get("base_sha")
    if not isinstance(recorded_base_sha, str) or not _SHA_RE.match(recorded_base_sha):
        return VERDICT_DENY, _invalid_detail(worktree, "records no base commit")
    try:
        current_base = _resolve_ref(git_dir, _common_dir(git_dir), recorded_base_ref)
    except (OSError, ValueError) as exc:
        return (
            VERDICT_UNKNOWN,
            f"{worktree}: base ref unresolvable ({type(exc).__name__})",
        )
    if current_base != recorded_base_sha:
        return VERDICT_DENY, _moved_base_detail(
            worktree, recorded_base_ref, current_base, _recorded_context(document)
        )
    return VERDICT_OK, ""


def _audit_override(reason: str) -> None:
    """Record one override use in the SEL.

    Read through the ``kiro_crew.security`` facade rather than imported at module
    scope, so the audit lands wherever the facade's ``SecurityEventLog`` currently
    points -- which is what a test that captures events patches, and what keeps this
    module out of an import cycle with the facade.
    """
    import kiro_crew.security as facade

    facade.SecurityEventLog().log(
        facade.SecurityEvent(
            event_id=uuid.uuid4().hex[:16],
            timestamp=datetime.now(tz=timezone.utc).isoformat(),
            event_type=OVERRIDE_EVENT_TYPE,
            caller_identity="",
            agent="kirocrew",
            source="security",
            operation="git_push",
            outcome="allowed",
            resources="feature_branch_push",
            metadata={
                "reason": facade.redact_and_truncate(reason, 200),
                "mechanism": "PUSH_RECEIPT_OVERRIDE",
            },
        )
    )


def denial_reason() -> str | None:
    """Refusal detail when an enrolled worktree has no receipt for its ``HEAD``.

    Returns ``None`` when the publish may proceed: nothing enrolled, every enrolled
    worktree receipted, the override set, or anything this check could not judge.

    NEVER raises. It is called from inside the PreToolUse gate, which must return a
    decision, and an exception there is a crash rather than a security answer -- so an
    unexpected failure allows the publish and logs, exactly like the per-worktree
    UNKNOWN verdict it cannot distinguish itself from.
    """
    try:
        override = os.environ.get(OVERRIDE_ENV, "").strip()
        if override:
            try:
                _audit_override(override)
            except Exception:
                logger.warning(
                    "SEL audit failed for %s (override stands)", OVERRIDE_EVENT_TYPE, exc_info=True
                )
            return None
        try:
            worktrees = enrolled_worktrees()
        except (OSError, ValueError):
            logger.warning(
                "push receipt policy could not be read; allowing the publish", exc_info=True
            )
            return None
        if not worktrees:
            return None
        for worktree in worktrees:
            verdict, detail = worktree_verdict(worktree)
            if verdict == VERDICT_DENY:
                return detail + CROSS_REPO_NOTE
            if verdict == VERDICT_UNKNOWN:
                logger.warning("push receipt check could not judge a worktree: %s", detail)
        return None
    except Exception:
        logger.warning("push receipt check failed; allowing the publish", exc_info=True)
        return None
