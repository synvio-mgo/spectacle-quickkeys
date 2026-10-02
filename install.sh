#!/usr/bin/env bash
# Install (or remove) spectacle-quickkeys as a systemd user service.
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="${XDG_BIN_HOME:-$HOME/.local/bin}"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
CONF_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/spectacle-quickkeys"
UNIT=spectacle-quickkeys.service

if [[ "${1:-}" == "--uninstall" ]]; then
    systemctl --user disable --now "$UNIT" 2>/dev/null || true
    rm -f "$UNIT_DIR/$UNIT" "$BIN_DIR/spectacle-quickkeys"
    systemctl --user daemon-reload
    echo "Removed. Your config in $CONF_DIR was left alone."
    exit 0
fi

command -v spectacle >/dev/null || { echo "spectacle is not installed"; exit 1; }
python3 -c 'import gi; gi.require_version("Atspi", "2.0"); from gi.repository import Atspi' 2>/dev/null \
    || { echo "missing dependency: install python3-gi and gir1.2-atspi-2.0"; exit 1; }

mkdir -p "$BIN_DIR" "$UNIT_DIR" "$CONF_DIR"
install -m 755 "$SRC_DIR/spectacle_quickkeys.py" "$BIN_DIR/spectacle-quickkeys"
install -m 644 "$SRC_DIR/systemd/$UNIT" "$UNIT_DIR/$UNIT"
[[ -f "$CONF_DIR/config.toml" ]] || install -m 644 "$SRC_DIR/config.example.toml" "$CONF_DIR/config.toml"

systemctl --user daemon-reload
systemctl --user enable --now "$UNIT"

echo
echo "Installed. Config: $CONF_DIR/config.toml"
systemctl --user --no-pager --lines=5 status "$UNIT" || true
echo
echo "Check the setup with:  $BIN_DIR/spectacle-quickkeys --check"
