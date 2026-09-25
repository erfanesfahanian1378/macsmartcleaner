#!/bin/sh
# macsmartcleaner installer
#   install / update:  curl -fsSL https://raw.githubusercontent.com/erfanesfahanian1378/macsmartcleaner/main/install.sh | sh
#   uninstall:         curl -fsSL https://raw.githubusercontent.com/erfanesfahanian1378/macsmartcleaner/main/install.sh | sh -s -- --uninstall
set -eu

REPO="erfanesfahanian1378/macsmartcleaner"
REF="${MSC_REF:-main}"
DEST="${MSC_HOME:-$HOME/.macsmartcleaner}"
BIN_DIR="${MSC_BIN_DIR:-$HOME/.local/bin}"

say()  { printf '\033[1m==>\033[0m %s\n' "$*"; }
fail() { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

if [ "${1:-}" = "--uninstall" ]; then
  if [ -x "$DEST/msc" ]; then "$DEST/msc" schedule remove >/dev/null 2>&1 || true; fi
  rm -f "$BIN_DIR/msc"
  rm -rf "$DEST"
  say "macsmartcleaner removed. (Cleanup history in ~/.local/state/macsmartcleaner was kept.)"
  exit 0
fi

[ "$(uname -s)" = "Darwin" ] || fail "macsmartcleaner is for macOS."

# On a fresh Mac, /usr/bin/python3 is only a stub until the Command Line Tools are installed.
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1; then
  say "Python 3.9+ is needed. macOS includes it with the free Command Line Tools."
  xcode-select --install >/dev/null 2>&1 || true
  fail "Finish the Command Line Tools install window that just opened, then run this installer again."
fi

say "Downloading macsmartcleaner ($REF)..."
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
curl -fsSL "https://github.com/$REPO/archive/$REF.tar.gz" | tar -xz -C "$TMP" --strip-components 1 \
  || fail "download failed"

rm -rf "$DEST"
mkdir -p "$DEST" "$BIN_DIR"
cp -R "$TMP/msc" "$TMP/macsmartcleaner" "$TMP/README.md" "$TMP/LICENSE" "$DEST/"
chmod +x "$DEST/msc"
ln -sf "$DEST/msc" "$BIN_DIR/msc"

say "Installed: $BIN_DIR/msc  ($("$DEST/msc" --version))"
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *)
    say "Add it to your PATH (then open a new Terminal window):"
    echo "    echo 'export PATH=\"$BIN_DIR:\$PATH\"' >> ~/.zshrc" ;;
esac
cat <<MSG

Next steps:
  1. System Settings > Privacy & Security > Full Disk Access > enable your Terminal app, restart it
  2. msc doctor        check everything is ready
  3. msc scan          see what is using your disk (changes nothing)
  4. msc clean         remove safe junk (asks first)
MSG
