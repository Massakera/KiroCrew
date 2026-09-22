"""``packaging/resign-desktop.sh`` -- the local codesigning on-ramp.

Why this is pinned rather than left to review: the script's whole reason to exist
is a failure with NO error surface. macOS silently drops every notification
posted by an ad-hoc signed bundle while Chromium still reports
``Notification.permission === "granted"``, so a regression here reproduces a bug
whose only symptom is "notifications do not work". Two properties carry that:

* it clears extended attributes BEFORE signing -- ``codesign`` refuses outright
  on "resource fork, Finder information, or similar detritus" and does not write
  the signature, leaving the bundle silently ad-hoc;
* it signs with ``--deep`` and NO ``--timestamp`` -- a timestamp is one network
  round trip per file, and the bundled CPython's site-packages makes that tens of
  minutes (the reason signing is not done inside electron-builder at all).

``codesign``, ``xattr`` and ``pgrep`` are stubbed on PATH, so this runs on any
POSIX host: the assertions are about the argv the script composes, not about
whether the platform can sign.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name == "nt", reason="resign-desktop.sh is a POSIX bash script")

_REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = _REPO_ROOT / "packaging" / "resign-desktop.sh"

IDENTITY = "Apple Development: dev@example.com (ABCDE12345)"

# Read from the same place the script reads it, so neither copy can drift and
# no test line restates a value that belongs to the packaging config.
BUNDLE_ID = json.loads(
    (_REPO_ROOT / "website" / "electron" / "package.json").read_text(encoding="utf-8")
)["build"]["appId"]


def _make_bundle(path: Path) -> Path:
    """A directory with the layout the script requires before it will mutate."""
    (path / "Contents" / "MacOS").mkdir(parents=True)
    (path / "Contents" / "Info.plist").write_text("<plist/>\n", encoding="utf-8")
    return path


def _stub(bindir: Path, name: str, body: str) -> None:
    path = bindir / name
    path.write_text(f"#!/usr/bin/env bash\n{body}\n", encoding="utf-8")
    path.chmod(0o755)


def _run(
    tmp_path: Path,
    *,
    identity: str | None = IDENTITY,
    app: Path | None = None,
    darwin: bool = True,
    running: bool = False,
    args: list[str] | None = None,
    xattr_body: str | None = None,
    bundle_id: str | None = BUNDLE_ID,
    keychain_identity: str | None = IDENTITY,
) -> subprocess.CompletedProcess[str]:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "calls.log"
    # `uname` decides the platform gate, so the stub is what lets a Linux CI host
    # exercise the macOS path (and the refusal, with darwin=False).
    _stub(bindir, "uname", f'printf "%s\\n" "{"Darwin" if darwin else "Linux"}"')
    _stub(bindir, "xattr", xattr_body or f'printf "xattr %s\\n" "$*" >> "{log}"')
    _stub(bindir, "codesign", f'printf "codesign %s\\n" "$*" >> "{log}"')
    # pgrep's exit status is the "is it running" answer; 1 means no match.
    _stub(bindir, "pgrep", f"exit {0 if running else 1}")
    # `defaults` reads the bundle id the script validates against. Stubbed to the
    # id the real bundle carries, so the identity gate is exercised rather than
    # skipped -- and overridable so a test can present a FOREIGN bundle.
    _stub(bindir, "defaults", f'printf "%s\\n" "{bundle_id}"' if bundle_id else "exit 1")
    # `security find-identity` decides whether the identity resolves. Stubbed to
    # the identity the keychain would hold, so the resolution gate is exercised
    # rather than skipped -- and overridable so a test can present a keychain
    # that does NOT hold it.
    _stub(
        bindir,
        "security",
        (
            f'printf "  1) DEADBEEF \\"%s\\"\\n" "{keychain_identity}"'
            if keychain_identity
            else "exit 1"
        ),
    )

    if app is None:
        app = _make_bundle(tmp_path / "KiroCrew.app")
    ents = tmp_path / "entitlements.plist"
    ents.write_text("<plist/>\n", encoding="utf-8")

    env = dict(os.environ)
    env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
    env["KIROCREW_SIGN_ENTITLEMENTS"] = str(ents)
    env.pop("KIROCREW_SIGN_IDENTITY", None)
    if identity is not None:
        env["KIROCREW_SIGN_IDENTITY"] = identity

    proc = subprocess.run(
        ["bash", str(SCRIPT), *(args if args is not None else [str(app)])],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=30,
    )
    proc.stdout = (log.read_text(encoding="utf-8") if log.exists() else "") + proc.stdout
    return proc


def test_it_clears_xattrs_before_signing(tmp_path: Path) -> None:
    proc = _run(tmp_path)
    assert proc.returncode == 0, proc.stderr
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith(("xattr", "codesign"))]
    assert lines[0].startswith("xattr -cr "), (
        "xattr must run FIRST: a quarantine flag or Finder metadata anywhere in "
        "the tree makes codesign refuse and NOT write the signature, which leaves "
        "the bundle silently ad-hoc -- the exact failure this script exists to fix"
    )
    assert any(ln.startswith("codesign ") for ln in lines)


def test_it_signs_deep_with_the_runtime_and_no_timestamp(tmp_path: Path) -> None:
    proc = _run(tmp_path)
    sign = next(ln for ln in proc.stdout.splitlines() if ln.startswith("codesign -"))
    assert "--force" in sign and "--deep" in sign
    # hardenedRuntime is on in the electron-builder config; re-signing without
    # --options runtime would strip it from the bundle.
    assert "--options runtime" in sign
    assert IDENTITY in sign
    assert "--timestamp" not in sign, (
        "a timestamp is one network round trip PER FILE; with the bundled CPython "
        "in the bundle that is the 20-minute path this script exists to avoid. "
        "Distribution builds get their timestamp from packaging/signing/, not here"
    )


def test_it_refuses_without_an_identity(tmp_path: Path) -> None:
    proc = _run(tmp_path, identity=None)
    assert proc.returncode == 2
    assert "KIROCREW_SIGN_IDENTITY" in proc.stderr
    assert "find-identity" in proc.stderr, "the refusal must name how to get one"
    assert "codesign" not in proc.stdout


def test_it_refuses_a_missing_bundle(tmp_path: Path) -> None:
    proc = _run(tmp_path, app=tmp_path / "nope.app")
    assert proc.returncode == 3
    assert "codesign" not in proc.stdout


def test_it_refuses_a_running_bundle(tmp_path: Path) -> None:
    proc = _run(tmp_path, running=True)
    assert proc.returncode == 5
    assert "quit it first" in proc.stderr
    # Signing a running bundle writes a half-signature whose failure only shows
    # up at the next launch.
    assert "codesign" not in proc.stdout


def test_it_refuses_a_non_macos_host(tmp_path: Path) -> None:
    proc = _run(tmp_path, darwin=False)
    assert proc.returncode == 1
    assert "macOS only" in proc.stderr


def test_it_defaults_to_the_installed_application(tmp_path: Path) -> None:
    # No path argument: the default target is the installed app, which does not
    # exist on this host, so the run must refuse by PATH rather than sign
    # something else.
    proc = _run(tmp_path, args=[])
    assert proc.returncode == 3
    assert "/Applications/KiroCrew.app" in proc.stderr


def test_a_refusing_path_does_not_abort_the_resign(tmp_path: Path) -> None:
    # `xattr -cr` exits nonzero when ANY ONE path refuses (a root-owned file in
    # the bundle) even though the rest of the tree was cleared. Treating that as
    # fatal aborted the whole re-sign and left the bundle ad-hoc -- i.e. it
    # reproduced the exact bug this script exists to fix.
    proc = _run(tmp_path, xattr_body='echo "xattr: [Errno 13] Permission denied" >&2; exit 1')
    assert proc.returncode == 0, proc.stderr
    assert "Permission denied" in proc.stderr, "the straggler must still be reported"
    assert any(
        ln.startswith("codesign -") for ln in proc.stdout.splitlines()
    ), "signing must still happen: codesign is the authority on whether it mattered"


def test_it_deletes_the_finder_custom_icon_file(tmp_path: Path) -> None:
    app = tmp_path / "KiroCrew.app"
    _make_bundle(app)
    # Finder's custom-icon marker. It carries a resource fork, which codesign
    # refuses outright, and it is not the app's icon (that is in
    # Contents/Resources/*.icns) -- so it is cruft to remove, not metadata to
    # strip.
    icon = app / "Icon\r"
    icon.write_text("finder cruft", encoding="utf-8")

    proc = _run(tmp_path, app=app)
    assert proc.returncode == 0, proc.stderr
    assert not icon.exists()
    assert any(ln.startswith("codesign -") for ln in proc.stdout.splitlines())


def test_it_names_the_sudo_escalation_for_an_undeletable_icon_file(tmp_path: Path) -> None:
    app = tmp_path / "KiroCrew.app"
    _make_bundle(app)
    (app / "Icon\r").write_text("finder cruft", encoding="utf-8")
    app.chmod(0o555)  # unlink needs write on the DIRECTORY, not the file
    try:
        proc = _run(tmp_path, app=app)
    finally:
        app.chmod(0o755)

    if proc.returncode == 0:  # running as root: the chmod does not bind
        pytest.skip("root can unlink regardless of directory permissions")
    assert proc.returncode == 6
    assert "sudo rm -f" in proc.stderr, "the refusal must carry the one command that fixes it"
    assert "codesign" not in proc.stdout, (
        "signing must not be attempted: codesign would refuse on the resource "
        "fork and leave the bundle ad-hoc with a confusing error"
    )


def test_a_spaced_app_path_survives_as_one_argument(tmp_path: Path) -> None:
    # The script side of the pair below: `$1` must carry the whole path. If a
    # spaced path arrived word-split, `APP` would be its first word only, the
    # `[ -d "$APP" ]` guard would still pass whenever that prefix is a real
    # directory, and `xattr -cr` would then recurse over THAT unrelated
    # directory -- clearing extended attributes with no way back.
    parent = tmp_path / "Applications with space"
    app = parent / "KiroCrew.app"
    _make_bundle(app)

    proc = _run(tmp_path, app=app)

    assert proc.returncode == 0, proc.stderr
    log = (tmp_path / "calls.log").read_text(encoding="utf-8")
    assert f"xattr -cr {app}" in log, "xattr must receive the full spaced path"
    assert (
        f"xattr -cr {parent}\n" not in log
    ), "a truncated path would strip attributes off the containing directory"


def test_the_make_target_quotes_the_app_path(tmp_path: Path) -> None:
    # The Makefile half. `make -n` prints the recipe without running it, so this
    # asserts the composed command line rather than the signing. An unquoted
    # $(APP) in the recipe is what turned a spaced path into two arguments.
    make = shutil.which("make")
    if make is None:
        pytest.skip("make is not installed")
    root = Path(__file__).parent.parent
    spaced = "/tmp/dir with space/KiroCrew.app"

    proc = subprocess.run(
        [make, "-n", "resign-desktop", f"APP={spaced}"],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    assert (
        f'"{spaced}"' in proc.stdout
    ), f"the recipe must pass APP as one quoted argument; got: {proc.stdout!r}"


def test_it_refuses_a_directory_that_is_not_an_app_bundle(tmp_path: Path) -> None:
    # The reaching condition is ordinary: `APP=/Applications` is one keystroke
    # from `APP=/Applications/KiroCrew.app`, and both are existing directories.
    # Everything past the gate is irreversible -- `rm -f` plus a recursive
    # `xattr -cr` -- so "is a directory" must not be enough to earn it.
    plain = tmp_path / "Applications"
    plain.mkdir()

    proc = _run(tmp_path, app=plain)

    assert proc.returncode == 7
    assert "not a .app bundle" in proc.stderr
    assert not (tmp_path / "calls.log").exists(), (
        "nothing may run before the target is validated: xattr and codesign are "
        "both mutations with no recovery path"
    )


def test_it_refuses_an_app_suffixed_directory_with_no_bundle_layout(tmp_path: Path) -> None:
    # The suffix alone is a naming convention, not proof: any directory can be
    # called `something.app`.
    hollow = tmp_path / "Decoy.app"
    hollow.mkdir()

    proc = _run(tmp_path, app=hollow)

    assert proc.returncode == 7
    assert "Contents/MacOS" in proc.stderr
    assert not (tmp_path / "calls.log").exists()


def test_it_refuses_a_bundle_belonging_to_another_app(tmp_path: Path) -> None:
    # Right shape, wrong app: re-signing someone else's bundle with this
    # identity is never what the caller meant, and the mutations still apply.
    other = _make_bundle(tmp_path / "Other.app")

    proc = _run(tmp_path, app=other, bundle_id="com.example.other")

    assert proc.returncode == 7
    assert BUNDLE_ID in proc.stderr, "the refusal must name what it expected"
    assert "KIROCREW_SIGN_BUNDLE_ID" in proc.stderr, "and the escape hatch for a deliberate re-sign"
    assert not (tmp_path / "calls.log").exists()


def test_an_unreadable_bundle_id_is_refused_not_assumed(tmp_path: Path) -> None:
    # `defaults` failing must not read as a match -- failing open here would put
    # the mutations back on any directory shaped like a bundle.
    shaped = _make_bundle(tmp_path / "KiroCrew.app")

    proc = _run(tmp_path, app=shaped, bundle_id=None)

    assert proc.returncode == 7
    assert "unreadable" in proc.stderr
    assert not (tmp_path / "calls.log").exists()


def test_an_identity_the_keychain_does_not_hold_is_refused(tmp_path: Path) -> None:
    # A mistyped identity is only discovered by `codesign`, which runs LAST. Were
    # the resolution check absent, the icon would already be deleted and the
    # bundle's extended attributes already cleared by then -- mutating the target
    # for a run that cannot finish, and leaving it ad-hoc.
    proc = _run(tmp_path, keychain_identity="Apple Development: someone.else (ZZZZZZZZZZ)")

    assert proc.returncode == 8
    assert "does not resolve" in proc.stderr
    assert "find-identity" in proc.stderr, "the refusal must name how to list the real ones"
    assert not (tmp_path / "calls.log").exists(), "nothing may run before the identity resolves"


def test_an_empty_keychain_is_refused_not_assumed(tmp_path: Path) -> None:
    # `security` exiting nonzero (no identities, locked keychain) must not read as
    # a match, for the same fail-closed reason as the bundle-id check.
    proc = _run(tmp_path, keychain_identity=None)

    assert proc.returncode == 8
    assert not (tmp_path / "calls.log").exists()


def test_the_icon_survives_an_unresolvable_identity(tmp_path: Path) -> None:
    # The sharpest form of the ordering: the icon deletion is irreversible, so it
    # must not happen for a run the identity gate was always going to stop.
    app = _make_bundle(tmp_path / "KiroCrew.app")
    icon = app / "Icon\r"
    icon.write_text("finder marker\n", encoding="utf-8")

    proc = _run(tmp_path, app=app, keychain_identity="Apple Development: nobody (YYYYYYYYYY)")

    assert proc.returncode == 8
    assert icon.exists(), "a refused run must leave the bundle exactly as it found it"
