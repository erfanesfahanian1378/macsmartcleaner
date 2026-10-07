#!/bin/sh
# macsmartcleaner installer
#   install / update:  curl -fsSL https://raw.githubusercontent.com/erfanesfahanian1378/macsmartcleaner/HEAD/install.sh | sh
#   uninstall:         curl -fsSL https://raw.githubusercontent.com/erfanesfahanian1378/macsmartcleaner/HEAD/install.sh | sh -s -- --uninstall
set -eu

REPO="erfanesfahanian1378/macsmartcleaner"
REF="${MSC_REF:-HEAD}"
DEST="${MSC_HOME:-$HOME/.macsmartcleaner}"
BIN_DIR="${MSC_BIN_DIR:-$HOME/.local/bin}"

MARK="# added by macsmartcleaner"

say()  { printf '\033[1m==>\033[0m %s\n' "$*"; }
fail() { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

if [ "${1:-}" = "--uninstall" ]; then
  if [ -x "$DEST/msc" ]; then "$DEST/msc" schedule remove >/dev/null 2>&1 || true; fi
  rm -f "$BIN_DIR/msc"
  rm -rf "$DEST"
  for rc in "$HOME/.zshrc" "$HOME/.bash_profile"; do
    if [ -f "$rc" ] && grep -q "$MARK" "$rc"; then
      grep -v "$MARK" "$rc" > "$rc.msc-tmp" && cat "$rc.msc-tmp" > "$rc" && rm -f "$rc.msc-tmp"
    fi
  done
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
  *":$BIN_DIR:"*) NEED_PATH="" ;;
  *)
    # put msc on the PATH for new Terminal windows (macOS doesn't include ~/.local/bin)
    case "${SHELL:-/bin/zsh}" in
      */bash) RC="$HOME/.bash_profile" ;;
      */zsh)  RC="$HOME/.zshrc" ;;
      *)      RC="" ;;
    esac
    if [ -n "$RC" ]; then
      if ! grep -q "$MARK" "$RC" 2>/dev/null; then
        printf '\nexport PATH="%s:$PATH"  %s\n' "$BIN_DIR" "$MARK" >> "$RC"
      fi
      say "Added $BIN_DIR to your PATH in $RC"
    else
      say "Add $BIN_DIR to your PATH to run msc from anywhere."
    fi
    NEED_PATH=1 ;;
esac
cat <<MSG

${NEED_PATH:+In THIS window, first run:   export PATH=\"$BIN_DIR:\$PATH\"
(new Terminal windows already have it)

}Next steps:
  1. System Settings > Privacy & Security > Full Disk Access > enable your Terminal app, restart it
  2. msc doctor        check everything is ready
  3. msc scan          see what is using your disk (changes nothing)
  4. msc               open the menu (Smart Clean, Space Lens, Auto-Protect...)
MSG
