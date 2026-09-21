#!/usr/bin/env python3
"""push_guard.py - pre-push stale-base guard for the prepare-pr skill.

Verifies that the current HEAD is safe to force-push by checking:
1. The fetch of origin/<base> succeeds (fail closed on network error).
2. origin/<base> is an ancestor of HEAD (i.e. HEAD sits on the freshly
   fetched base tip, not on a stale fork point that would cause the squash
   to bake in reversions of newer base changes).
3. The number of commits HEAD is ahead of origin/<base> is plausibly small
   (default threshold: 5 commits for a single-commit PR workflow; configurable
   via --max-ahead).
4. None of the ahead-commits are patch-equivalent to commits in a bounded
   window (REPLAY_HISTORY_WINDOW) of origin/<base> history.  Comparison uses
   `git patch-id --stable` so renames, whitespace, and commit metadata are
   ignored — only the semantic diff matters.  The window is bounded because
   full history is unbounded cost, and replayed commits from a recent stale
   fork are by construction recent.

This prevents the catastrophic failure mode where a worktree branched from a
local integration trunk (kiki-trunk) carries 100+ unshipped commits that get
force-pushed to the remote feature branch, clobbering upstream work.

On a PASS it also records a RECEIPT naming this worktree and the commit it judged,
so the publish floor can tell a gate that ran from one that was skipped -- a verdict
printed and discarded leaves the two indistinguishable.  The receipt is written through
``kiro_crew.security.push_receipt``, the ONE place that decides where a receipt
lives and what it contains, so a reader and this writer cannot drift into two
stores.  That import is the single non-stdlib dependency here and it is optional:
when it fails the guard still reports its own verdict and warns that no receipt was
recorded, which in an enrolled repository surfaces as a refused publish naming this
gate rather than as a silent pass.

Portable: stdlib only (plus the optional receipt import above); shells out to git
via argument lists.

Usage:  python3 push_guard.py [--base <branch>] [--max-ahead <N>]
Exit:   0 SAFE | 40 REFUSED (stale base detected) | 2 environment error
"""

import argparse
import importlib.util
import os
import re
import shutil
import subprocess
import sys

# Single source of truth for the default max-ahead threshold.  Shared by
# preflight.py (which imports this constant) so the two scripts cannot drift.
DEFAULT_MAX_AHEAD = 5

# Maximum number of base-history commits to scan for patch-id equivalence in
# the replay-detection check.  Bounded because full history is unbounded cost,
# and replayed commits from a recent stale fork are by construction recent
# (the operator branched from a stale tip that was N commits behind; the
# replayed patches are in that window).  500 is generous — a typical PR
# workflow replays at most a few dozen commits from a stale integration trunk.
REPLAY_HISTORY_WINDOW = 500

# Test-only injection point: when set to a non-None list, run() uses it
# instead of resolving "git" from PATH.  Allows tests to monkeypatch a
# Python-based fake git directly (e.g. push_guard._GIT_CMD = [sys.executable,
# str(fake_script)]) without platform-specific PATH/shell wrappers or
# environment-variable indirection.
_GIT_CMD: list[str] | None = None


def run(args):
    """Run a command; return (returncode, stdout, stderr) as stripped text.

    When the module-level _GIT_CMD is set (test monkeypatch), "git" is
    replaced with the specified command list — no PATH/shell wrappers or
    environment-variable indirection needed.  Otherwise git is resolved via
    shutil.which so PATH-injected wrappers (including .bat/.cmd on Windows)
    are found without shell=True.
    """
    try:
        if args and args[0] == "git":
            if _GIT_CMD:
                args = _GIT_CMD + list(args[1:])
            else:
                resolved = shutil.which("git")
                if resolved:
                    args = [resolved] + list(args[1:])
        p = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace")
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except OSError as exc:
        return 127, "", "{}: {}".format(args[0], exc)


def err(msg):
    sys.stderr.write(msg + "\n")


def _resolve_base(base_arg):
    """Resolve the base branch name from arg, symbolic ref, or default."""
    base = base_arg
    if not base:
        sym = run(["git", "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"])[1]
        if sym.startswith("origin/"):
            base = sym[len("origin/") :]
        if not base:
            base = "main"
    return base


# Allowlist of known git fetch failure classes.  Each entry is a tuple of
# (compiled regex applied case-insensitively to stderr, user-facing label).
# When none match, the diagnostic withholds the raw text entirely — free-text
# stderr cannot be closed by shape enumeration alone (round-13 lesson).
_FETCH_ERROR_CLASSES: list[tuple["re.Pattern[str]", str]] = [
    (re.compile(r"could not resolve host", re.IGNORECASE), "could not resolve host"),
    (re.compile(r"name or service not known", re.IGNORECASE), "DNS resolution failed"),
    (re.compile(r"permission denied", re.IGNORECASE), "permission denied"),
    (re.compile(r"repository not found", re.IGNORECASE), "repository not found"),
    (re.compile(r"does not appear to be a git repo", re.IGNORECASE), "not a git repository"),
    (re.compile(r"not a git repository", re.IGNORECASE), "not a git repository"),
    (re.compile(r"timed? ?out", re.IGNORECASE), "connection timed out"),
    (re.compile(r"connection refused", re.IGNORECASE), "connection refused"),
    (re.compile(r"connection reset", re.IGNORECASE), "connection reset"),
    (re.compile(r"couldn't connect to server", re.IGNORECASE), "could not connect"),
    (re.compile(r"ssl|tls|certificate", re.IGNORECASE), "TLS/certificate error"),
    (re.compile(r"authentication failed", re.IGNORECASE), "authentication failed"),
    (re.compile(r"invalid credentials", re.IGNORECASE), "authentication failed"),
    (re.compile(r"remote:.*not found", re.IGNORECASE), "remote ref not found"),
    (re.compile(r"couldn't find remote ref", re.IGNORECASE), "remote ref not found"),
    (re.compile(r"no matching remote head", re.IGNORECASE), "remote ref not found"),
]


def _classify_fetch_error(stderr: str) -> str:
    """Derive a safe diagnostic from git fetch stderr.

    Returns one of:
    - A matched error class label (e.g. "could not resolve host") when stderr
      contains a recognized git/ssh/curl failure pattern.
    - A generic withholding message when no pattern matches — free-text stderr
      can carry bare tokens (e.g. from ext:: remote helpers, pre-push hook
      output, or credential-helper error messages) that no URL-shape scrubber
      can redact.  The operator can run ``git fetch`` manually to see the full
      error in their own terminal.

    Contract: the return value NEVER contains raw stderr content that did not
    match the allowlist.  Only the matched class label (a hardcoded literal)
    is surfaced.  This closes the free-text credential egress class entirely
    rather than chasing individual shapes (round-13 lesson: 13 consecutive
    rounds of shape enumeration proved the approach cannot converge).
    """
    for pattern, label in _FETCH_ERROR_CLASSES:
        if pattern.search(stderr):
            return label
    return "fetch failed (details withheld — run git fetch manually to see the error)"


def _fetch_base(base):
    """Fetch origin/<base>; return 0 on success or 40 on failure.

    Uses an explicit refspec (+refs/heads/<base>:refs/remotes/origin/<base>)
    so the remote-tracking ref is always updated regardless of the clone's
    configured remote.origin.fetch (e.g. single-branch clones, narrow CI
    checkouts).  The leading '+' ensures non-fast-forward updates are accepted
    (required after an upstream force-push of the base branch).

    On failure, prints a REFUSED diagnostic with the classified error (from
    ``_classify_fetch_error``) — never raw stderr, which may contain bare
    tokens from remote helpers or credential error messages.
    """
    print("Fetching origin/{} ...".format(base))
    refspec = "+refs/heads/{}:refs/remotes/origin/{}".format(base, base)
    fetch_rc, _, fetch_err = run(["git", "fetch", "--quiet", "origin", refspec])
    if fetch_rc != 0:
        diagnostic = _classify_fetch_error(fetch_err)
        err(
            "REFUSED: git fetch origin {} failed. Cannot verify merge-base "
            "freshness — refusing to push on a potentially stale ref.\n"
            "  error class: {}".format(base, diagnostic)
        )
        return 40
    return 0


def _check_single_on_base(base):
    """Post-squash structural guard: assert HEAD~1 == origin/<base>.

    After a squash, the single commit should sit directly on the freshly
    fetched origin/<base>.  If HEAD~1 != origin/<base>, the squash landed
    on a stale ref or the branch carries unexpected history.

    Returns: 0 safe, 40 refused.
    """
    rc, head_parent, _ = run(["git", "rev-parse", "HEAD~1"])
    if rc != 0:
        err(
            "REFUSED: cannot resolve HEAD~1. The branch may have no parent "
            "commit (single root commit with no base)."
        )
        return 40

    rc, origin_base_sha, _ = run(["git", "rev-parse", "origin/{}".format(base)])
    if rc != 0:
        err("REFUSED: cannot resolve origin/{}.".format(base))
        return 40

    head_sha = run(["git", "rev-parse", "HEAD"])[1][:12]

    print("HEAD~1:          " + head_parent[:12])
    print("origin/{}:     {}".format(base, origin_base_sha[:12]))
    print("HEAD:            " + head_sha)

    if head_parent != origin_base_sha:
        err(
            "REFUSED: HEAD~1 ({}) != origin/{} ({}). "
            "The squashed commit does not sit directly on the freshly fetched "
            "remote base — either the squash landed on a stale ref or the "
            "branch carries unexpected history.\n"
            "  To fix: rebase onto the fresh origin/{} first "
            "(git rebase origin/{}), then re-squash "
            "(git reset --soft origin/{} && git commit).".format(
                head_parent[:12], base, origin_base_sha[:12], base, base, base
            )
        )
        return 40

    print("STATUS: SAFE TO PUSH (single commit on base)")
    return 0


def _check_pre_squash(base, max_ahead):
    """Pre-squash guard: merge-base ancestry, commit count, replayed commits.

    Returns: 0 safe, 40 refused.

    Fail-closed contract: every git subprocess failure (nonzero exit code or
    OSError) refuses the push (exit 40) with a diagnostic naming the failed
    operation.  The ONLY paths that return 0 ("SAFE TO PUSH") are those where
    the git command SUCCEEDED and its output is genuinely empty (no ahead
    commits, or an empty diff for a single commit which is skipped).
    """
    # Compute merge-base of HEAD and freshly-fetched origin/<base>.
    rc, merge_base, _ = run(["git", "merge-base", "HEAD", "origin/{}".format(base)])
    if rc != 0 or not merge_base:
        err(
            "REFUSED: cannot compute merge-base between HEAD and origin/{}. "
            "The branch may have no common history with the remote base.".format(base)
        )
        return 40

    # Verify origin/<base> is an ancestor of HEAD — i.e. HEAD sits on the
    # freshly fetched base tip.  After a correct rebase (Phase 1 step 2),
    # this is always true.  If it fails, the branch forks from a stale base
    # and the squash would bake in reversions of newer base changes.
    rc, _, _ = run(["git", "merge-base", "--is-ancestor", "origin/{}".format(base), "HEAD"])
    if rc != 0:
        err(
            "REFUSED: HEAD is not based on the fresh origin/{} tip — the "
            "branch forks from a stale base and squashing would bake in "
            "reversions of newer base changes. Rebase onto origin/{} "
            "first.".format(base, base)
        )
        return 40

    # Count commits HEAD is ahead of origin/<base>.
    rc, count_str, _ = run(["git", "rev-list", "--count", "origin/{}..HEAD".format(base)])
    if rc != 0:
        err("REFUSED: cannot count commits ahead of origin/{}.".format(base))
        return 40

    try:
        ahead = int(count_str)
    except ValueError:
        err("REFUSED: unexpected rev-list output: {}".format(count_str))
        return 40

    origin_base_sha = run(["git", "rev-parse", "origin/{}".format(base)])[1][:12]
    head_sha = run(["git", "rev-parse", "HEAD"])[1][:12]

    print("merge-base:      " + merge_base[:12])
    print("origin/{}:     {}".format(base, origin_base_sha))
    print("HEAD:            " + head_sha)
    print("commits ahead:   {}".format(ahead))
    print("max allowed:     {}".format(max_ahead))

    if ahead > max_ahead:
        err(
            "REFUSED: HEAD is {} commits ahead of origin/{} (max allowed: {}). "
            "This is far too many for a squashed single-commit PR — the branch "
            "likely carries unshipped local integration commits that would "
            "clobber upstream work if force-pushed.\n"
            "  To fix (if you authored all {} commits): squash them down "
            "(git reset --soft origin/{} && git commit) so the branch carries "
            "a single deliverable commit.\n"
            "  To fix (if any ahead-commit is unfamiliar): STOP and diagnose — "
            "do not squash foreign history. The branch may have picked up "
            "commits from a local integration trunk.\n"
            "  To fix (stale fork): rebase onto origin/{} "
            "(git rebase origin/{}) so HEAD sits on the fresh remote "
            "base tip.".format(ahead, base, max_ahead, ahead, base, base, base)
        )
        return 40

    # Detect replayed commits via patch-id comparison.
    # Compare each ahead-commit's patch-id against a BOUNDED window of
    # origin/<base> history.  If any ahead-commit is patch-equivalent to a
    # base-history commit, the branch replays upstream patches (e.g. from a
    # stale fork that cherry-picked base commits back).  The window is bounded
    # (REPLAY_HISTORY_WINDOW) because full history is unbounded cost, and
    # replayed commits from a recent stale fork are by construction recent.
    #
    # Step 1: get patch-ids for ahead-commits (origin/<base>..HEAD).
    rc, ahead_revs, _ = run(["git", "rev-list", "origin/{}..HEAD".format(base)])
    if rc != 0:
        err(
            "REFUSED: git rev-list origin/{}..HEAD failed (exit {})."
            " Cannot verify replay safety — failing closed.".format(base, rc)
        )
        return 40
    if not ahead_revs:
        # rev-list SUCCEEDED with empty output — genuinely 0 ahead commits.
        print("STATUS: SAFE TO PUSH")
        return 0

    ahead_patch_ids: dict[str, str] = {}  # patch-id → commit-sha
    for commit_sha in ahead_revs.splitlines():
        # Get the patch content and pipe to patch-id.
        diff_rc, diff_out, _ = run(["git", "diff-tree", "-p", commit_sha])
        if diff_rc != 0:
            err(
                "REFUSED: git diff-tree -p {} failed (exit {})."
                " Cannot verify replay safety — failing closed.".format(commit_sha[:12], diff_rc)
            )
            return 40
        if not diff_out:
            # diff-tree SUCCEEDED with empty output — empty commit, skip.
            continue
        # Feed the diff to git patch-id --stable via stdin.
        try:
            pid_proc = subprocess.run(
                ["git", "patch-id", "--stable"],
                input=diff_out,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            if pid_proc.returncode != 0:
                err(
                    "REFUSED: git patch-id --stable failed (exit {}) for"
                    " commit {}. Cannot verify replay safety —"
                    " failing closed.".format(pid_proc.returncode, commit_sha[:12])
                )
                return 40
            if pid_proc.stdout.strip():
                patch_id = pid_proc.stdout.strip().split()[0]
                ahead_patch_ids[patch_id] = commit_sha
        except OSError as exc:
            err(
                "REFUSED: git patch-id --stable raised OSError for commit"
                " {}: {}. Cannot verify replay safety —"
                " failing closed.".format(commit_sha[:12], exc)
            )
            return 40

    if not ahead_patch_ids:
        print("STATUS: SAFE TO PUSH")
        return 0

    # Step 2: get patch-ids for bounded base history.
    rc, base_revs, _ = run(
        [
            "git",
            "rev-list",
            "--max-count={}".format(REPLAY_HISTORY_WINDOW),
            "origin/{}".format(base),
        ]
    )
    if rc != 0:
        err(
            "REFUSED: git rev-list --max-count={} origin/{} failed (exit {})."
            " Cannot verify replay safety — failing closed.".format(REPLAY_HISTORY_WINDOW, base, rc)
        )
        return 40
    if not base_revs:
        # rev-list SUCCEEDED with empty output — no base history to compare.
        print("STATUS: SAFE TO PUSH")
        return 0

    base_patch_ids: dict[str, str] = {}  # patch-id → commit-sha
    for commit_sha in base_revs.splitlines():
        diff_rc, diff_out, _ = run(["git", "diff-tree", "-p", commit_sha])
        if diff_rc != 0:
            err(
                "REFUSED: git diff-tree -p {} (base history) failed (exit {})."
                " Cannot verify replay safety — failing closed.".format(commit_sha[:12], diff_rc)
            )
            return 40
        if not diff_out:
            # diff-tree SUCCEEDED with empty output — empty commit, skip.
            continue
        try:
            pid_proc = subprocess.run(
                ["git", "patch-id", "--stable"],
                input=diff_out,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            if pid_proc.returncode != 0:
                err(
                    "REFUSED: git patch-id --stable failed (exit {}) for"
                    " base commit {}. Cannot verify replay safety —"
                    " failing closed.".format(pid_proc.returncode, commit_sha[:12])
                )
                return 40
            if pid_proc.stdout.strip():
                patch_id = pid_proc.stdout.strip().split()[0]
                base_patch_ids[patch_id] = commit_sha
        except OSError as exc:
            err(
                "REFUSED: git patch-id --stable raised OSError for base"
                " commit {}: {}. Cannot verify replay safety —"
                " failing closed.".format(commit_sha[:12], exc)
            )
            return 40

    # Step 3: find matches.
    replayed_pairs: list[tuple[str, str]] = []  # (ahead-sha, base-sha)
    for patch_id, ahead_sha in ahead_patch_ids.items():
        if patch_id in base_patch_ids:
            replayed_pairs.append((ahead_sha, base_patch_ids[patch_id]))

    if replayed_pairs:
        pair_strs = ["{} ↔ {}".format(a[:12], b[:12]) for a, b in replayed_pairs]
        err(
            "REFUSED: {} ahead-commit(s) are patch-equivalent to commits "
            "already on origin/{} — the branch replays upstream history.\n"
            "  replayed (ahead ↔ base): {}\n"
            "  To fix: STOP and diagnose. Do not squash — one or more "
            "ahead-commits duplicate patches already on the base branch. "
            "Rebase onto a fresh origin/{} so only novel changes remain, "
            "or cherry-pick your original commits onto "
            "origin/{}.".format(len(replayed_pairs), base, ", ".join(pair_strs), base, base)
        )
        return 40

    print("STATUS: SAFE TO PUSH")
    return 0


#: Depth from this file to the package root that holds ``kiro_crew/`` --
#: scripts/ -> prepare-pr/ -> kirocrew-dev/ -> builtin_skills/ -> kiro_crew/ -> root.
_PACKAGE_ROOT_DEPTH = 5


def _package_root():
    """Directory holding the ``kiro_crew`` package this script ships inside."""
    root = os.path.abspath(__file__)
    for _ in range(_PACKAGE_ROOT_DEPTH + 1):
        root = os.path.dirname(root)
    return root


def _receipt_module():
    """Load the receipt writer that ships BESIDE this script, by file location.

    The pairing is the point.  The agent runs this with a bare ``python3`` from inside
    whatever repository it is working in, so ``kiro_crew`` may be absent from that
    interpreter's path -- or, worse, present from a DIFFERENT checkout, in which case a
    plain ``from kiro_crew.security import push_receipt`` resolves to a package that
    does not carry this module (or carries an older one) and the receipt goes unwritten
    for a reason that reads like a missing dependency.  An editable install registers a
    meta-path finder that outranks ``sys.path``, so ordering the path alone does not fix
    that; loading by location does.

    The ``sys.path`` insert is still needed for the other half: the loaded module reaches
    the data home through ``kiro_crew.config.paths``, the one resolver that decides where
    a receipt lives, and that import needs SOME importable ``kiro_crew`` -- this tree's,
    when the interpreter has none of its own.
    """
    root = _package_root()
    if root not in sys.path:
        sys.path.insert(0, root)
    location = os.path.join(root, "kiro_crew", "security", "push_receipt.py")
    spec = importlib.util.spec_from_file_location("kirocrew_push_receipt", location)
    if spec is None or spec.loader is None:
        raise ImportError("no receipt writer beside this script: {}".format(location))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _worktree_root():
    """Root of the worktree git resolves from here, or "" when there is none.

    Every other check in this script runs git, which finds the repository from any
    depth, so the guard passes when invoked from a subdirectory.  The receipt writer
    needs the ROOT: handed a bare cwd it would look for a ``.git`` that a subdirectory
    does not have and record nothing.
    """
    rc, toplevel, _ = run(["git", "rev-parse", "--show-toplevel"])
    return toplevel if rc == 0 else ""


def _clear_receipt():
    """Drop any receipt this worktree already has; return a process exit code.

    Returns non-zero only when the store is reachable and the receipt could not be
    removed, because then a stale pass may still be sitting there and this run cannot
    promise otherwise.  An unimportable receipt module is NOT that case: the store is
    out of reach, so this script neither wrote nor can clear anything, and refusing
    would turn an optional dependency into a required one.
    """
    root = _worktree_root()
    if not root:
        err("ERROR: not inside a git worktree.")
        return 2
    try:
        module = _receipt_module()
    except Exception as exc:  # noqa: BLE001 - optional dependency, see above
        err(
            "WARNING: push receipt store unreachable ({}: {}); no receipt was cleared "
            "or will be recorded.".format(type(exc).__name__, exc)
        )
        return 0
    try:
        module.clear_receipt(root)
    except Exception as exc:  # noqa: BLE001 - reported as a refusal, see above
        err(
            "ERROR: could not clear this worktree's previous push receipt ({}: {}). "
            "Refusing, because a receipt from an earlier pass would otherwise keep "
            "authorizing a publish this run has not judged.".format(type(exc).__name__, exc)
        )
        return 2
    return 0


def _revision_pair(base):
    """The ``(HEAD, origin/<base>)`` commits this run is judging, or ``("", "")``.

    Read from git rather than re-read later inside the receipt writer: the guard's
    verdict is about ONE pair, and a receipt that names whatever the refs point at a
    moment afterwards describes a state nothing checked.
    """
    rc_head, head, _ = run(["git", "rev-parse", "HEAD"])
    rc_base, base_sha, _ = run(["git", "rev-parse", "refs/remotes/origin/" + base])
    if rc_head != 0 or rc_base != 0 or not head or not base_sha:
        return "", ""
    return head, base_sha


def _record_receipt(base, mode, head, base_sha):
    """Record this PASS as a receipt for the current worktree's HEAD.

    Called from exactly one place -- ``main()``, once the mode's own check has returned
    0 -- so "a receipt exists" means "this guard passed", which is the whole property
    the publish floor reads.  Writing it at each individual ``return 0`` inside the
    checks would put the same claim behind five sites that can drift apart.

    A failure here does NOT change the guard's verdict: this script answers whether the
    base is fresh, and it either is or is not regardless of whether a receipt could be
    stored.  It warns loudly instead, because in an enrolled repository the missing
    receipt is what the publish floor will refuse on, and an operator reading that
    refusal needs to know the write failed rather than that the gate was skipped.

    The worktree is git's own ``--show-toplevel`` rather than the process cwd, resolved
    by ``_worktree_root``.
    """
    root = _worktree_root()
    if not root:
        err(
            "WARNING: push receipt not recorded (this directory is not inside a git "
            "worktree). The verdict above still stands, but a repository enrolled in "
            "the push-receipt check will refuse the publish until a receipt is stored."
        )
        return
    try:
        module = _receipt_module()
        path = module.write_receipt(root, base=base, mode=mode, head=head, base_sha=base_sha)
    except Exception as exc:  # noqa: BLE001 - a receipt is best-effort, see above
        err(
            "WARNING: push receipt not recorded ({}: {}). The verdict above still "
            "stands, but a repository enrolled in the push-receipt check will refuse "
            "the publish until a receipt is stored.".format(type(exc).__name__, exc)
        )
        return
    print("receipt:         " + str(path))
    # Read it back through the SAME function the publish floor calls.  Writing a receipt
    # the reader cannot accept is silent otherwise: the publish is refused later with no
    # trace of why the write did not help.  It catches a ref storage format the reader
    # does not parse, a writer and reader resolving different data homes, and any drift
    # between the two halves -- at gate time, where an operator is watching.
    try:
        verdict, detail = module.worktree_verdict(root)
    except Exception as exc:  # noqa: BLE001 - a read-back is best-effort, see above
        err(
            "WARNING: the push receipt was written but could not be read back ({}: {}). "
            "An enrolled repository may still refuse the publish.".format(type(exc).__name__, exc)
        )
        return
    if verdict != "ok":
        err(
            "WARNING: the push receipt was written but the publish floor does not accept "
            "it ({}: {}). An enrolled repository will refuse the publish.".format(verdict, detail)
        )


def main():
    parser = argparse.ArgumentParser(description="Pre-push stale-base guard")
    parser.add_argument(
        "--base",
        default="",
        help="Base branch name (without origin/ prefix). "
        "Auto-detected from PR or origin/HEAD if omitted.",
    )
    parser.add_argument(
        "--max-ahead",
        type=int,
        default=DEFAULT_MAX_AHEAD,
        help="Maximum commits HEAD may be ahead of origin/<base> (default: {}).".format(
            DEFAULT_MAX_AHEAD
        ),
    )
    parser.add_argument(
        "--require-single-on-base",
        action="store_true",
        default=False,
        help="Post-squash mode: assert HEAD~1 == origin/<base> after a fresh "
        "fetch (the single squashed commit sits directly on the remote base).",
    )
    args = parser.parse_args()

    # Must be in a git repo.
    if run(["git", "rev-parse", "--is-inside-work-tree"])[0] != 0:
        err("ERROR: not inside a git repository.")
        return 2

    base = _resolve_base(args.base)

    # Invalidate first, judge second.  A receipt is evidence about the tree as it is
    # NOW; leaving a previous pass in place while this run decides means a run that
    # goes on to REFUSE still leaves the publish authorized by the verdict it just
    # superseded.  Failing to clear is fatal rather than a warning, because the whole
    # point is that no stale receipt survives this line.
    clear_result = _clear_receipt()
    if clear_result != 0:
        return clear_result

    # Fetch origin/<base> — MUST succeed (fail closed) for both modes.
    fetch_result = _fetch_base(base)
    if fetch_result != 0:
        return fetch_result

    # Capture the pair the check is about to judge, so the receipt can name exactly
    # that and not whatever the refs point at once the check returns.
    before = _revision_pair(base)

    if args.require_single_on_base:
        mode = "single-on-base"
        result = _check_single_on_base(base)
    else:
        mode = "pre-squash"
        result = _check_pre_squash(base, args.max_ahead)
    if result == 0:
        after = _revision_pair(base)
        if not all(after) or after != before:
            err(
                "ERROR: HEAD or the base moved while this guard was checking, so its "
                "verdict is about neither state. Nothing was recorded; re-run the guard."
            )
            return 2
        _record_receipt(base, mode, after[0], after[1])
    return result


if __name__ == "__main__":
    sys.exit(main())
