#!/bin/sh
# Install the jack coding CLI. Usage:
#   curl -fsSL https://raw.githubusercontent.com/mdjakkariya/jacky/main/install.sh | sh
# Honors JACK_VERSION (pin) and JACK_INSTALL_DIR (default: ~/.local/bin).
set -eu

REPO="mdjakkariya/jacky"
INSTALL_DIR="${JACK_INSTALL_DIR:-$HOME/.local/bin}"

uname_s="$(uname -s)"
uname_m="$(uname -m)"
case "$uname_s" in
  Darwin) OS="macos" ;;
  Linux)  OS="linux" ;;
  *) echo "unsupported OS: $uname_s (use install.ps1 on Windows)" >&2; exit 1 ;;
esac
case "$uname_m" in
  arm64|aarch64) ARCH="arm64" ;;
  x86_64|amd64)  ARCH="x64" ;;
  *) echo "unsupported arch: $uname_m" >&2; exit 1 ;;
esac
EXT="tar.gz"

if [ -n "${JACK_VERSION:-}" ]; then
  VERSION="$JACK_VERSION"
else
  VERSION="$(curl -fsSL "https://api.github.com/repos/${REPO}/releases/latest" \
    | grep '"tag_name"' | head -n1 | sed -E 's/.*"v?([^"]+)".*/\1/')"
fi
[ -n "$VERSION" ] || { echo "could not determine the latest version" >&2; exit 1; }

NAME="jack-${VERSION}-${OS}-${ARCH}.${EXT}"
URL="https://github.com/${REPO}/releases/download/v${VERSION}/${NAME}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "downloading $NAME ..."
curl -fsSL "$URL" -o "$TMP/$NAME"
curl -fsSL "$URL.sha256" -o "$TMP/$NAME.sha256"
WANT="$(cut -d' ' -f1 < "$TMP/$NAME.sha256")"
if command -v shasum >/dev/null 2>&1; then GOT="$(shasum -a 256 "$TMP/$NAME" | cut -d' ' -f1)";
else GOT="$(sha256sum "$TMP/$NAME" | cut -d' ' -f1)"; fi
[ "$WANT" = "$GOT" ] || { echo "checksum mismatch — aborting" >&2; exit 1; }

tar -xzf "$TMP/$NAME" -C "$TMP"
mkdir -p "$INSTALL_DIR"
mv "$TMP/jack" "$INSTALL_DIR/jack"
chmod +x "$INSTALL_DIR/jack"
echo "installed jack $VERSION -> $INSTALL_DIR/jack"

case ":$PATH:" in
  *":$INSTALL_DIR:"*) : ;;
  *) echo "add to PATH:  export PATH=\"$INSTALL_DIR:\$PATH\"" ;;
esac
echo "run 'jack' in a project to start (set a cloud key or Ollama first — see the README)."
