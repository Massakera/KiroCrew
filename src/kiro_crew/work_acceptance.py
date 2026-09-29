"""The work ledger's acceptance evaluator, run by the GATEWAY.

The conductor used to run ``goal-conductor/scripts/accept_eval.py`` in its own
shell and copy the answer into the ledger with ``action=verdict``. Nothing tied that
copy to an evaluation that really happened, and ``pr_checks`` read the pull request
as it stood rather than a revision, so a green run on an earlier head was
indistinguishable from one on the head being accepted. This module is the
replacement: the gateway observes the criterion itself, and what it returns is the
only thing the store accepts as evidence (``work_ledger.apply_evaluation``) and the
only fresh observation it accepts at ``close(state="accepted")``.

Three properties are the whole contract, and each names what it rules out:

* **No caller-supplied command, verdict or revision.** :func:`observe` reads the
  PERSISTED acceptance object and nothing else the model wrote. The ``cmd`` kind
  stays refused: a generic executor behind a gateway route would remove the denied-
  command floor exactly as it did behind the approved wrapper
  (``accept_eval.py``'s module docstring records the three rounds that settled it).
* **Bound to a revision.** ``pr_checks`` reads the head SHA ``H`` and then every
  check run and commit status OF ``H``; each row must itself name ``H``. The
  revision it returns is ``H`` plus a digest of the exact current check set (ids
  and results), so a close that sees a check vanish, appear or re-run is not the
  revision that was evaluated. ``file`` hashes bytes read under a project root
  pinned by inode when the item was bound.
* **Nothing unproven becomes ``pass``.** An unreadable, uncounted, partial or empty
  board, a row for another commit, a cancelled or stale attempt with no proven
  replacement, a check in a state this build does not know, a path outside the
  admitted root, a read that failed: each is ``fail``, ``pending``, ``refused`` or
  ``error``.

Scope limits, deliberately not lifted here: no local test execution, no host file
read outside the root admitted at bind, no human-approval channel.

This module performs I/O (``gh``, file reads) and blocks; the routes call it off
the event loop. It holds no state.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import stat as _stat
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

#: Identity of THIS implementation. Recorded on every evidence record; the store
#: accepts at close only evidence whose evaluator AND policy are admitted below, so
#: a change to either retires every earlier ``pass`` instead of silently honouring it.
EVALUATOR_VERSION = "work_acceptance/2"

POLICY_PR_CHECKS = "pr_checks/v2"
POLICY_FILE = "file/v2"
POLICY_HUMAN_APPROVAL = "human_approval/none"
POLICY_NONE = "none"

#: The (evaluator, policy) pairs whose ``pass`` can authorise ``accepted``.
#: ``human_approval`` is absent on purpose: there is no authenticated approval
#: channel, so nothing this module returns for it may close an item.
ADMITTED_POLICIES: frozenset[tuple[str, str]] = frozenset(
    {(EVALUATOR_VERSION, POLICY_PR_CHECKS), (EVALUATOR_VERSION, POLICY_FILE)}
)

#: Bound on the bytes the ``file`` kind will hash. Existence is the contract, not
#: content; the digest pins WHICH bytes were observed, and a larger artifact is
#: refused rather than read without bound on the gateway.
MAX_FILE_BYTES = 16 * 1024 * 1024

#: Bounds on what an evidence record KEEPS for a reader. The digest in the revision
#: always covers the whole check set; only the human-readable receipt is clipped.
MAX_RECEIPT_ROWS = 40
MAX_CHECK_NAME_CHARS = 120
MAX_DIAGNOSTIC_CHARS = 500

#: Pagination for the two check endpoints: the page size GitHub caps at, and a
#: bound on pages so a pathological board is an ``error``, never an unbounded read.
PAGE_SIZE = 100
MAX_PAGES = 12

_REPO_RE = re.compile(r"^[\w.-]+/[\w.-]+$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

#: Check-run conclusions. ``cancelled`` and ``stale`` are NOT passing and NOT noise:
#: a cancelled attempt verified nothing, and it only stops mattering when a later
#: attempt of the same check in the same suite is proven to exist.
_RUN_PASSING = frozenset({"success", "neutral", "skipped"})
_RUN_FAILING = frozenset(
    {"failure", "timed_out", "action_required", "startup_failure", "cancelled", "stale"}
)
_RUN_PENDING_STATUS = frozenset({"queued", "in_progress", "waiting", "requested", "pending"})
#: Combined-status states.
_STATUS_PASSING = frozenset({"success"})
_STATUS_FAILING = frozenset({"failure", "error"})
_STATUS_PENDING = frozenset({"pending"})


class GitHub(Protocol):
    """The two calls this evaluator makes: a ``gh`` invocation and one API page."""

    def call(self, args: list[str]) -> Any: ...

    def api(self, path: str, *, paginated_page: int = 0) -> Any: ...


@dataclass(frozen=True)
class Observation:
    """What one evaluation observed, before the store binds it to an item.

    ``revision`` is the immutable thing observed (a head SHA with its check-set
    digest, a byte digest, or the recorded absence of a file) and ``sources`` the
    references that back the verdict. Both are plain JSON so they can be persisted
    and compared at close.
    """

    verdict: str
    policy: str
    target: dict[str, Any] = field(default_factory=dict)
    revision: dict[str, Any] = field(default_factory=dict)
    sources: dict[str, Any] = field(default_factory=dict)
    diagnostic: str = ""
    observed_at: str = ""
    evaluator: str = EVALUATOR_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "evaluator": self.evaluator,
            "policy": self.policy,
            "verdict": self.verdict,
            "observed_at": self.observed_at,
            "target": self.target,
            "revision": self.revision,
            "sources": self.sources,
            "diagnostic": self.diagnostic,
        }


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _obs(verdict: str, policy: str, diagnostic: str, **fields: Any) -> Observation:
    return Observation(
        verdict=verdict,
        policy=policy,
        diagnostic=_clip(diagnostic, MAX_DIAGNOSTIC_CHARS),
        observed_at=_now_iso(),
        **fields,
    )


def _clip(text: Any, limit: int) -> str:
    text = str(text or "")
    return text if len(text) <= limit else text[: max(1, limit - 1)] + "\u2026"


def canonical_digest(value: Any) -> str:
    """sha256 of *value*'s canonical JSON. Identifies content; authenticates nothing."""
    body = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def observe(
    acceptance: Any,
    *,
    file_root: dict[str, Any] | None = None,
    gh: GitHub | None = None,
) -> Observation:
    """Evaluate one PERSISTED acceptance object. Never raises for a bad criterion.

    *file_root* is the project root admitted for the worker when its item was bound
    (``{"path", "dev", "ino"}``) -- the only tree the ``file`` kind may read. *gh* is
    the GitHub transport, injectable for tests; the default is the gateway's own
    (:class:`kiro_crew.probes.gh_pr._Transport`: bounded retry and rate limits).
    """
    if not isinstance(acceptance, dict):
        return _obs("error", POLICY_NONE, "acceptance is not an object")
    kind = acceptance.get("kind")
    if kind == "pr_checks":
        return _observe_pr_checks(acceptance, gh if gh is not None else _default_gh())
    if kind == "file":
        return _observe_file(acceptance, file_root)
    if kind == "human_approval":
        return _obs(
            "pending",
            POLICY_HUMAN_APPROVAL,
            "awaiting human approval; no authenticated approval channel exists, so "
            "this kind cannot be accepted through the ledger",
        )
    if kind == "cmd":
        return _obs(
            "refused",
            POLICY_NONE,
            "the 'cmd' kind was removed: a criterion may not name a command to run. "
            "Use 'pr_checks' for CI-backed acceptance",
        )
    return _obs("error", POLICY_NONE, f"unknown accept kind {kind!r}")


# --------------------------------------------------------------------------- #
# pr_checks
# --------------------------------------------------------------------------- #


def _default_gh() -> GitHub:
    # The probe's transport, reused rather than re-implemented: it owns gh
    # resolution, bounded retry with backoff and rate-limit handling. Imported here
    # because it pulls in the monitoring stack, which a gateway that never
    # evaluates a pull request has no reason to load.
    from kiro_crew.probes.gh_pr import _Transport

    return _Transport("", budget_secs=60.0)


def _json(response: Any) -> Any:
    if response is None or not getattr(response, "ok", False):
        return None
    try:
        return json.loads(getattr(response, "stdout", "") or "")
    except (json.JSONDecodeError, RecursionError, TypeError):
        return None


def _strict_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _counted(
    gh: GitHub, path: str, rows_key: str, head: str, *, sha_key: str | None = None
) -> tuple[list[dict[str, Any]], str]:
    """Every row of one self-counting list endpoint, or ``([], why)``.

    STRICTER than the monitor's reader, on purpose: a page that carries no integer
    ``total_count`` cannot bound the read, so it is an error rather than "complete
    at whatever arrived"; the read must end with exactly ``total_count`` rows; and,
    for the combined-status endpoint, every page must name ``head`` as its ``sha``.
    """
    rows: list[dict[str, Any]] = []
    declared: int | None = None
    for page in range(1, MAX_PAGES + 1):
        payload = _json(gh.api(path, paginated_page=page))
        if not isinstance(payload, dict):
            return [], f"{path} page {page} unreadable"
        total = _strict_int(payload.get("total_count"))
        if total is None:
            return [], f"{path} page {page} carries no integer total_count"
        if declared is not None and total != declared:
            return [], f"{path} total_count changed during the read ({declared} -> {total})"
        declared = total
        if sha_key is not None and str(payload.get(sha_key) or "").lower() != head:
            return [], f"{path} page {page} does not name commit {head[:12]}"
        batch = payload.get(rows_key)
        if not isinstance(batch, list) or not all(isinstance(r, dict) for r in batch):
            return [], f"{path} page {page} carries no rows"
        rows.extend(batch)
        if len(rows) >= declared or len(batch) < PAGE_SIZE:
            break
    else:
        return [], f"{path} exceeds {MAX_PAGES} pages"
    if declared is None or len(rows) != declared:
        return [], f"{path} read {len(rows)} of {declared} rows"
    return rows, ""


def _run_entry(row: dict[str, Any]) -> dict[str, Any]:
    app = row.get("app") if isinstance(row.get("app"), dict) else {}
    suite = row.get("check_suite") if isinstance(row.get("check_suite"), dict) else {}
    return {
        "kind": "check_run",
        "source": str(app.get("slug") or app.get("id") or ""),
        "suite": _strict_int(suite.get("id")),
        "name": str(row.get("name") or ""),
        "id": _strict_int(row.get("id")),
        "status": str(row.get("status") or "").lower(),
        "result": str(row.get("conclusion") or "").lower(),
    }


def _status_entry(row: dict[str, Any]) -> dict[str, Any]:
    creator = row.get("creator") if isinstance(row.get("creator"), dict) else {}
    return {
        "kind": "status",
        "source": str(creator.get("login") or ""),
        "suite": None,
        "name": str(row.get("context") or ""),
        "id": _strict_int(row.get("id")),
        "status": "",
        "result": str(row.get("state") or "").lower(),
    }


def _classify(entry: dict[str, Any]) -> str:
    """``passing`` / ``failing`` / ``pending`` / ``unknown`` for one CURRENT row."""
    if entry["kind"] == "check_run":
        if entry["status"] != "completed":
            return "pending" if entry["status"] in _RUN_PENDING_STATUS else "unknown"
        if entry["result"] in _RUN_PASSING:
            return "passing"
        if entry["result"] in _RUN_FAILING:
            return "failing"
        return "unknown"
    if entry["result"] in _STATUS_PASSING:
        return "passing"
    if entry["result"] in _STATUS_FAILING:
        return "failing"
    if entry["result"] in _STATUS_PENDING:
        return "pending"
    return "unknown"


def _current_and_superseded(
    runs: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split check runs into the attempt that counts per check, and proven leftovers.

    An attempt is superseded only by a PROVEN replacement: a run of the same check
    name, from the same app, in the same check suite, with a larger id (GitHub
    assigns ids in creation order). Two lanes that merely share a name in different
    suites are two checks, and both count. A run whose identity cannot be
    established (no id or no suite) is never superseded and never supersedes.
    """
    groups: dict[tuple[str, int, str], list[dict[str, Any]]] = {}
    current: list[dict[str, Any]] = []
    for entry in runs:
        if entry["id"] is None or entry["suite"] is None:
            current.append(entry)
            continue
        groups.setdefault((entry["source"], entry["suite"], entry["name"]), []).append(entry)
    superseded: list[dict[str, Any]] = []
    for members in groups.values():
        members.sort(key=lambda e: e["id"])
        current.append(members[-1])
        superseded.extend(members[:-1])
    return current, superseded


def _receipt(entry: dict[str, Any], state: str | None = None) -> dict[str, Any]:
    out = {
        "kind": entry["kind"],
        "source": _clip(entry["source"], MAX_CHECK_NAME_CHARS),
        "suite": entry["suite"],
        "name": _clip(entry["name"], MAX_CHECK_NAME_CHARS),
        "id": entry["id"],
        "result": entry["result"] or entry["status"],
    }
    if state is not None:
        out["state"] = state
    return out


def _bounded(rows: list[dict[str, Any]]) -> dict[str, Any]:
    kept = rows[:MAX_RECEIPT_ROWS]
    return {"rows": kept, "omitted": max(0, len(rows) - len(kept))}


def _identity(entry: dict[str, Any]) -> list[Any]:
    """The part of a row a revision is made of: which attempt, and how it ended."""
    return [
        entry["kind"],
        entry["source"],
        entry["suite"],
        entry["name"],
        entry["id"],
        entry["status"],
        entry["result"],
    ]


def _observe_pr_checks(acceptance: dict[str, Any], gh: GitHub) -> Observation:
    """``pr_checks/v2``: every current check run and commit status of head ``H``.

    ``pass`` needs ALL of: the pull request and both boards read completely, with
    counts; every row naming ``H``; at least one current check; none failing (a
    cancelled or stale attempt without a proven replacement is failing), none
    pending, none unrecognised; and at least one that concluded ``success`` --
    skipped and neutral checks alone prove nothing. A merged or closed pull request
    is ``refused``; a draft whose checks are pending is ``refused`` (the author's
    own "not ready").
    """
    pr = acceptance.get("pr")
    repo = acceptance.get("repo")
    if isinstance(pr, bool) or not isinstance(pr, int) or pr < 1:
        return _obs("error", POLICY_PR_CHECKS, "pr_checks spec needs an integer pr")
    if not isinstance(repo, str) or not _REPO_RE.fullmatch(repo):
        return _obs(
            "error",
            POLICY_PR_CHECKS,
            'pr_checks needs "repo": "owner/name" -- the gateway has no working '
            "directory to infer the repository from",
        )
    target = {"kind": "pr_checks", "repo": repo, "pr": pr}
    try:
        core = _json(
            gh.call(
                [
                    "pr",
                    "view",
                    str(pr),
                    "--repo",
                    repo,
                    "--json",
                    "state,mergedAt,isDraft,headRefOid",
                ]
            )
        )
    except Exception as exc:  # noqa: BLE001 - any reader failure is an error verdict
        return _obs(
            "error", POLICY_PR_CHECKS, f"pull request could not be read: {exc}", target=target
        )
    if not isinstance(core, dict):
        return _obs("error", POLICY_PR_CHECKS, "pull request unreadable", target=target)
    if core.get("mergedAt") or str(core.get("state") or "").upper() in ("MERGED", "CLOSED"):
        return _obs(
            "refused",
            POLICY_PR_CHECKS,
            "the pull request has ended (merged or closed); its checks are not judged",
            target=target,
        )
    head = str(core.get("headRefOid") or "").lower()
    if not _SHA_RE.fullmatch(head):
        return _obs("error", POLICY_PR_CHECKS, "head revision unknown", target=target)

    try:
        raw_runs, run_note = _counted(
            gh, f"repos/{repo}/commits/{head}/check-runs?filter=all", "check_runs", head
        )
        raw_statuses, status_note = _counted(
            gh, f"repos/{repo}/commits/{head}/status", "statuses", head, sha_key="sha"
        )
    except Exception as exc:  # noqa: BLE001
        return _obs(
            "error", POLICY_PR_CHECKS, f"check board could not be read: {exc}", target=target
        )
    base_revision = {"kind": "git_sha", "sha": head}
    if run_note or status_note:
        return _obs(
            "error",
            POLICY_PR_CHECKS,
            f"check board incomplete: {run_note or status_note}",
            target=target,
            revision=base_revision,
        )
    foreign = [r for r in raw_runs if str(r.get("head_sha") or "").lower() != head]
    if foreign:
        return _obs(
            "error",
            POLICY_PR_CHECKS,
            f"{len(foreign)} check run(s) on the board name another commit than {head[:12]}",
            target=target,
            revision=base_revision,
        )

    current_runs, superseded = _current_and_superseded([_run_entry(r) for r in raw_runs])
    current = current_runs + [_status_entry(r) for r in raw_statuses]
    states = [(entry, _classify(entry)) for entry in current]
    ordered = sorted(states, key=lambda pair: _identity(pair[0]), reverse=False)
    revision = {
        "kind": "git_sha",
        "sha": head,
        "checks": len(current),
        "checks_digest": canonical_digest([_identity(e) for e, _ in ordered]),
    }
    tally = {
        name: sum(1 for _, s in states if s == name)
        for name in ("passing", "failing", "pending", "unknown")
    }
    sources = {
        "endpoints": [
            f"repos/{repo}/commits/{head}/check-runs?filter=all",
            f"repos/{repo}/commits/{head}/status",
        ],
        "check_runs_total": len(raw_runs),
        "statuses_total": len(raw_statuses),
        "tally": tally,
        "current": _bounded([_receipt(e, s) for e, s in ordered]),
        "superseded": _bounded([_receipt(e) for e in sorted(superseded, key=_identity)]),
    }
    fields = {"target": target, "revision": revision, "sources": sources}
    if tally["failing"]:
        cancelled = sum(
            1 for e, s in states if s == "failing" and e["result"] in ("cancelled", "stale")
        )
        extra = f" ({cancelled} cancelled or stale with no proven replacement)" if cancelled else ""
        return _obs(
            "fail",
            POLICY_PR_CHECKS,
            f"{tally['failing']} check(s) not green on {head[:12]}{extra}",
            **fields,
        )
    if tally["pending"]:
        if core.get("isDraft") is True:
            return _obs(
                "refused",
                POLICY_PR_CHECKS,
                f"PR #{pr} is a draft and its checks have not finished; mark it ready for "
                "review or change the acceptance kind",
                **fields,
            )
        return _obs(
            "pending",
            POLICY_PR_CHECKS,
            f"{tally['pending']} check(s) pending on {head[:12]}",
            **fields,
        )
    if tally["unknown"]:
        return _obs(
            "error",
            POLICY_PR_CHECKS,
            f"{tally['unknown']} check(s) report a state this evaluator does not recognise",
            **fields,
        )
    if not current:
        return _obs(
            "pending", POLICY_PR_CHECKS, f"no checks reported for {head[:12]} yet", **fields
        )
    if not any(e["result"] == "success" for e, _ in states):
        return _obs(
            "refused",
            POLICY_PR_CHECKS,
            f"no check on {head[:12]} concluded success; skipped or neutral checks prove "
            "nothing -- re-express the condition",
            **fields,
        )
    return _obs(
        "pass", POLICY_PR_CHECKS, f"{tally['passing']} check(s) passing on {head[:12]}", **fields
    )


# --------------------------------------------------------------------------- #
# file
# --------------------------------------------------------------------------- #


class _Refused(Exception):
    pass


def _sensitive(path: str) -> bool:
    from kiro_crew.security.paths import is_sensitive_resolved_path

    try:
        return bool(is_sensitive_resolved_path(path))
    except Exception:  # noqa: BLE001 - an unanswerable fence refuses
        return True


def admitted_root_for(project: str | None) -> dict[str, Any] | None:
    """The identity of *project* to pin at bind: canonical path, device and inode.

    ``None`` when there is no project or it is not a directory: the item then has no
    admitted root, and a ``file`` condition on it is refused rather than guessed.
    Taken by the route when the conductor binds the worker -- before the worker's
    first turn -- so a root the worker later swaps is detected, not adopted.
    """
    if not isinstance(project, str) or not project:
        return None
    try:
        real = os.path.realpath(project)
        st = os.stat(real)
    except OSError:
        return None
    if not _stat.S_ISDIR(st.st_mode):
        return None
    return {"path": real, "dev": int(st.st_dev), "ino": int(st.st_ino)}


def _relative_parts(path: str, root: str) -> list[str] | None:
    """*path*'s components under *root*, lexically, or ``None`` when it escapes."""
    if os.path.isabs(path):
        normal = os.path.normpath(path)
        try:
            if os.path.commonpath([root, normal]) != root:
                return None
        except ValueError:
            return None
        rel = os.path.relpath(normal, root)
    else:
        rel = os.path.normpath(path)
    parts = [p for p in rel.split(os.sep) if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        return None
    return parts


def _observe_file(acceptance: dict[str, Any], file_root: dict[str, Any] | None) -> Observation:
    """``file/v2``: presence or absence of one regular file under the ADMITTED root.

    The root is the project directory pinned at bind (path + device + inode). It is
    opened with its ancestor chain pinned and its inode re-checked, so a worker that
    renames it or swaps it for a link reaches nothing. Below the root, every
    component is opened relative to its parent's descriptor with ``O_NOFOLLOW``: a
    link anywhere in the path is refused, never followed, and ``..`` cannot appear.
    A platform without descriptor-relative opens refuses the kind outright.
    """
    path = acceptance.get("path")
    exists = acceptance.get("exists", True)
    if not isinstance(path, str) or not path:
        return _obs("error", POLICY_FILE, "file spec needs a path")
    if not isinstance(exists, bool):
        return _obs("error", POLICY_FILE, "file spec needs a boolean exists")
    target: dict[str, Any] = {"kind": "file", "path": path, "exists": exists}
    root = file_root if isinstance(file_root, dict) else None
    root_path = root.get("path") if root else None
    if (
        not isinstance(root_path, str)
        or _strict_int(root.get("dev")) is None
        or _strict_int(root.get("ino")) is None
    ):
        return _obs(
            "refused",
            POLICY_FILE,
            "no project directory was admitted for this worker when the item was bound",
            target=target,
        )
    from kiro_crew.pinned_fs import supports_pinned_walk

    if not supports_pinned_walk():
        return _obs(
            "refused",
            POLICY_FILE,
            "this platform cannot confine a read to the admitted directory",
            target=target,
        )
    parts = _relative_parts(path, root_path)
    if parts is None:
        return _obs(
            "refused", POLICY_FILE, "path is outside the admitted project directory", target=target
        )
    lexical = os.path.join(root_path, *parts)
    target.update({"root": root_path, "resolved": lexical})
    if _sensitive(root_path) or _sensitive(lexical):
        return _obs("refused", POLICY_FILE, "path is a sensitive location", target=target)
    try:
        found = _read_under_root(root, parts)
    except _Refused as exc:
        return _obs("refused", POLICY_FILE, str(exc), target=target)
    except OSError as exc:
        # An unreadable file is not an absent one.
        return _obs("error", POLICY_FILE, f"{path} could not be read: {exc}", target=target)
    if found is None:
        absent: dict[str, Any] = {"kind": "file_absent", "path": lexical}
        verdict = "fail" if exists else "pass"
        return _obs(verdict, POLICY_FILE, f"{path} does not exist", target=target, revision=absent)
    digest, size = found
    revision: dict[str, Any] = {
        "kind": "file_sha256",
        "path": lexical,
        "sha256": digest,
        "size": size,
    }
    verdict = "pass" if exists else "fail"
    return _obs(
        verdict, POLICY_FILE, f"{path} exists ({size} bytes)", target=target, revision=revision
    )


def _read_under_root(root: dict[str, Any], parts: list[str]) -> tuple[str, int] | None:
    """``(sha256, size)`` of the file at *parts* under the pinned *root*, or ``None``.

    ``None`` means provably absent: a component or the file itself does not exist,
    or a component is a regular file. A link, a directory at the leaf, a non-regular
    file, a hardlinked file, a root whose inode moved: each raises :class:`_Refused`.
    """
    from kiro_crew.pinned_fs import dir_flags, open_dir_pinned

    fds: list[int] = []
    try:
        root_fd = open_dir_pinned(root["path"], what="admitted project directory", refusal=_Refused)
        fds.append(root_fd)
        st = os.fstat(root_fd)
        if (int(st.st_dev), int(st.st_ino)) != (int(root["dev"]), int(root["ino"])):
            raise _Refused("the project directory is not the one admitted when the item was bound")
        parent = root_fd
        for name in parts[:-1]:
            try:
                child = os.open(name, dir_flags(), dir_fd=parent)
            except OSError as exc:
                if exc.errno == errno.ENOENT:
                    return None
                if exc.errno == errno.ENOTDIR:
                    lst = os.stat(name, dir_fd=parent, follow_symlinks=False)
                    if _stat.S_ISLNK(lst.st_mode):
                        raise _Refused(f"refusing to follow a link at {name!r}") from exc
                    return None
                if exc.errno == errno.ELOOP:
                    raise _Refused(f"refusing to follow a link at {name!r}") from exc
                raise
            fds.append(child)
            parent = child
        leaf = parts[-1]
        try:
            lst = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            return None
        if _stat.S_ISLNK(lst.st_mode):
            raise _Refused(f"refusing to follow a link at {leaf!r}")
        if _stat.S_ISDIR(lst.st_mode):
            raise _Refused("path is a directory; file/v2 observes one regular file")
        if not _stat.S_ISREG(lst.st_mode):
            raise _Refused("path is not a regular file")
        fd = os.open(
            leaf,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
            dir_fd=parent,
        )
        fds.append(fd)
        opened = os.fstat(fd)
        if not _stat.S_ISREG(opened.st_mode):
            raise _Refused("path is not a regular file")
        if opened.st_nlink != 1:
            raise _Refused("refusing to read a hardlinked file")
        if opened.st_size > MAX_FILE_BYTES:
            raise _Refused(
                f"file is {opened.st_size} bytes; file/v2 hashes at most {MAX_FILE_BYTES}"
            )
        hasher = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_FILE_BYTES:
                raise _Refused("file grew past the hashing bound while being read")
            hasher.update(chunk)
        return hasher.hexdigest(), total
    finally:
        for fd in reversed(fds):
            try:
                os.close(fd)
            except OSError:
                pass
