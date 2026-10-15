#!/usr/bin/env bash
# One-command installer for dymo-linux-service (Debian/Ubuntu).
#
#   ./install.sh [--port N]
#
# Or without a checkout (set DYMO_REPO to the git URL):
#   curl -fsSL <raw install.sh URL> | DYMO_REPO=<git url> bash
set -euo pipefail

DEST="${DYMO_LINUX_HOME:-$HOME/.local/share/dymo-linux-service}"
PKGS=(cups printer-driver-dymo python3 python3-pil python3-qrcode libnss3-tools openssl fonts-liberation)

say() { printf '\n==> %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -ne 0 ] || die "run as your normal user, not root (it uses sudo only for apt)"
command -v apt-get >/dev/null || die "apt-get not found; this installer supports Debian/Ubuntu only (see README for other distros)"

say "Installing packages (sudo): ${PKGS[*]}"
sudo apt-get install -y "${PKGS[@]}"

# Locate the service script: next to this file, else clone DYMO_REPO.
SRC=""
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "$(dirname "${BASH_SOURCE[0]}")/dymo-linux-service.py" ]; then
  SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
else
  [ -n "${DYMO_REPO:-}" ] || die "dymo-linux-service.py not found next to the installer; set DYMO_REPO to the git URL"
  command -v git >/dev/null || sudo apt-get install -y git
  SRC="$(mktemp -d)"
  trap 'rm -rf "$SRC"' EXIT
  git clone --depth 1 "$DYMO_REPO" "$SRC"
fi

say "Copying service to $DEST"
mkdir -p "$DEST"
install -m 0755 "$SRC/dymo-linux-service.py" "$DEST/dymo-linux-service.py"
[ -d "$SRC/examples" ] && cp -r "$SRC/examples" "$DEST/"

mkdir -p "$HOME/.local/bin"
ln -sf "$DEST/dymo-linux-service.py" "$HOME/.local/bin/dymo-linux-service"

say "Creating certificate, trusting it in Chrome, starting the systemd user service"
"$DEST/dymo-linux-service.py" install "$@"

say "Printers the service sees"
"$DEST/dymo-linux-service.py" printers || true

cat <<MSG

Done. Next:
  1. Restart your browser (so it picks up the trusted certificate).
  2. Open https://127.0.0.1:41951/DYMO/DLS/Printing/StatusConnected  (should show: true)
  3. If no printer is listed above, add the LabelWriter at http://localhost:631
MSG
