#!/usr/bin/env bash
# Re-sign an INSTALLED KiroCrew.app with a local codesigning identity.
#
# Why this exists at all: macOS silently DROPS every notification posted by an
# ad-hoc signed bundle -- no banner, nothing in Notification Center, no Dock
# badge -- while Chromium still reports `Notification.permission === "granted"`
# and `new Notification(...)` still returns an object. There is no error
# anywhere, and System Settings > Notifications shows the app present and
# allowed, so the only symptom is "notifications do not work" with nothing to
# diagnose. TCC consent (microphone, accessibility) is keyed to the signing
# identity too, so an ad-hoc build also loses those grants on every rebuild.
# `make desktop` produces an ad-hoc bundle by design (see build-desktop.sh), so
# a developer who needs either feature needs this script.
#
# Why NOT signing inside the build: electron-builder signs every file it walks
# in the bundle individually and with `--timestamp`, which is one network round
# trip to Apple's timestamp authority PER FILE. The bundled CPython's
# site-packages is thousands of files (it reaches `__pycache__/*.pyc`, which are
# not even Mach-O), and a universal build pays all of it twice. Measured at 20+
# minutes still going. A single `codesign --deep` over the finished bundle walks
# nested BUNDLES rather than every file and carries no timestamp, which is the
# difference between seconds and abandoning the build.
#
# A timestamp is required for DISTRIBUTION (a signature without one stops
# validating when the certificate expires) and that lane is untouched: releases
# are signed and notarized by packaging/signing/, not by this script.
set -euo pipefail

APP="${1:-/Applications/KiroCrew.app}"
IDENTITY="${KIROCREW_SIGN_IDENTITY:-}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENTITLEMENTS="${KIROCREW_SIGN_ENTITLEMENTS:-$HERE/../website/electron/build/entitlements.mac.plist}"
# The identity to expect, read from build.appId in website/electron/package.json
# rather than copied: that file is what stamps the bundle, so deriving it here
# cannot drift from what electron-builder actually wrote.
APP_PKG="$HERE/../website/electron/package.json"
EXPECTED_BUNDLE_ID="${KIROCREW_SIGN_BUNDLE_ID:-$(
  grep -o '"appId"[[:space:]]*:[[:space:]]*"[^"]*"' "$APP_PKG" 2>/dev/null |
    head -1 | sed 's/.*"\([^"]*\)"[[:space:]]*$/\1/'
)}"
if [ -z "$EXPECTED_BUNDLE_ID" ]; then
  echo "resign-desktop: could not read build.appId from $APP_PKG." >&2
  echo "  Set KIROCREW_SIGN_BUNDLE_ID to the bundle id this app is signed as." >&2
  exit 2
fi

if [ "$(uname -s)" != "Darwin" ]; then
  echo "resign-desktop: macOS only (codesign does not exist here)." >&2
  exit 1
fi

if [ -z "$IDENTITY" ]; then
  cat >&2 <<'EOF'
resign-desktop: set KIROCREW_SIGN_IDENTITY to a codesigning identity first.

  security find-identity -v -p codesigning
  export KIROCREW_SIGN_IDENTITY="Apple Development: you@example.com (XXXXXXXXXX)"

An "Apple Development" identity is enough; notarization is not needed for an app
you install yourself.
EOF
  exit 2
fi

[ -d "$APP" ] || { echo "resign-desktop: no app bundle at $APP" >&2; exit 3; }

# Everything below this line MUTATES the target irreversibly: it deletes a file
# and clears extended attributes recursively, neither of which is restorable. A
# directory-exists check alone is not enough to earn that, because the argument
# is an ordinary path a person can fat-finger or tab-complete -- `APP=/Applications`
# is one keystroke from `APP=/Applications/KiroCrew.app` and would strip the
# attributes off every app on the machine. So prove the target IS this app's
# bundle first: the `.app` suffix, the bundle layout, and the identity
# electron-builder stamps in (`appId` in website/electron/package.json). Anything
# else fails closed with the path it was given.
case "$APP" in
  *.app) ;;
  *) echo "resign-desktop: $APP is not a .app bundle -- refusing to touch it." >&2; exit 7 ;;
esac
[ -d "$APP/Contents/MacOS" ] && [ -f "$APP/Contents/Info.plist" ] || {
  echo "resign-desktop: $APP has no Contents/MacOS + Contents/Info.plist -- not an app bundle." >&2
  exit 7
}
FOUND_ID="$(defaults read "$APP/Contents/Info" CFBundleIdentifier 2>/dev/null || true)"
if [ "$FOUND_ID" != "$EXPECTED_BUNDLE_ID" ]; then
  echo "resign-desktop: $APP is '${FOUND_ID:-unreadable}', not $EXPECTED_BUNDLE_ID." >&2
  echo "  Point APP at this app's own bundle, or set KIROCREW_SIGN_BUNDLE_ID to re-sign another." >&2
  exit 7
fi

[ -f "$ENTITLEMENTS" ] || { echo "resign-desktop: no entitlements at $ENTITLEMENTS" >&2; exit 4; }

# The identity must RESOLVE, not merely be non-empty. `codesign` is the last step,
# so a mistyped or expired identity would otherwise be discovered only after the
# cleanup below has already deleted the icon file and cleared the bundle's
# extended attributes -- leaving the bundle still ad-hoc, having mutated it for
# nothing. Checking the keychain here is what makes the mutations conditional on
# the run being able to finish. Substring match because a person legitimately
# passes either the full "Apple Development: name (TEAMID)" string or just the
# SHA-1 hash, and `find-identity` prints both on one line per identity.
if ! security find-identity -v -p codesigning 2>/dev/null | grep -qF "$IDENTITY"; then
  echo "resign-desktop: '$IDENTITY' does not resolve to a codesigning identity in the keychain." >&2
  echo "  Pick one from: security find-identity -v -p codesigning" >&2
  exit 8
fi

# A running bundle gets a half-written signature, and the failure surfaces later
# as a launch error rather than here.
if pgrep -f "$APP/Contents/MacOS/" >/dev/null 2>&1; then
  echo "resign-desktop: Kiro Crew is running -- quit it first." >&2
  exit 5
fi

# A top-level `Icon\r` is Finder's custom-icon marker -- pure Finder metadata
# carrying a resource fork, which is precisely the "resource fork, Finder
# information, or similar detritus" codesign refuses. The app's real icon lives
# in Contents/Resources/*.icns, so this file is cruft: delete it rather than try
# to strip attributes off it. It frequently is not the installing user's to
# touch (a DMG copy can land it root-owned), hence the escalation hint.
ICON_FILE="$(printf '%s/Icon\r' "$APP")"
if [ -e "$ICON_FILE" ] && ! rm -f "$ICON_FILE" 2>/dev/null; then
  cat >&2 <<EOF
resign-desktop: cannot delete the Finder custom-icon file in the bundle:
  $APP/Icon\$'\r'
It is Finder cruft (the real icon is in Contents/Resources), it carries a
resource fork codesign refuses, and it is not yours to remove. Run this once,
then re-run:
  sudo rm -f "$APP"/Icon\$'\r'
EOF
  exit 6
fi

# Extended attributes (quarantine flags, Finder metadata) make codesign refuse
# outright with "resource fork, Finder information, or similar detritus not
# allowed", and the signature is then NOT written -- the bundle silently stays
# ad-hoc. Clearing them recursively is harmless on an app bundle.
#
# `xattr -cr` exits nonzero when ANY single path refuses, even though every other
# path in the tree was cleared -- so treating its status as fatal aborted the
# whole re-sign over one unreadable file. Report the stragglers and continue:
# codesign below is the authority on whether any of them actually matter, and it
# fails loudly rather than silently.
xattr_errors="$(xattr -cr "$APP" 2>&1 >/dev/null || true)"
if [ -n "$xattr_errors" ]; then
  echo "resign-desktop: these paths kept their extended attributes:" >&2
  printf '%s\n' "$xattr_errors" | tr -d '\r' | sed 's/^/  /' >&2
  echo "Continuing -- codesign refuses below if any of them matter." >&2
fi

# --deep is discouraged for shipping (it applies the app's entitlements to the
# helper binaries too) and is the right trade here: this is a local development
# bundle, and the alternative is hand-signing every nested binary inside-out.
codesign --force --deep --options runtime \
  --entitlements "$ENTITLEMENTS" \
  --sign "$IDENTITY" \
  "$APP"

echo
codesign -dv --verbose=4 "$APP" 2>&1 | grep -E 'Signature|Authority|TeamIdentifier' || true
echo
echo "resign-desktop: done. Expect macOS to re-ask for microphone and"
echo "accessibility on the next launch -- those grants belonged to the old"
echo "signature."
