#!/usr/bin/env bash
# Installs OffGridFinder for the current user (no root needed).
#   ./install.sh             install or upgrade
#   ./install.sh --uninstall remove the program (your downloaded regions are kept)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="${HOME}/.local/bin"
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
APP_DIR="${DATA_HOME}/applications"
ICON_DIR="${DATA_HOME}/icons/hicolor/256x256/apps"

if [[ "${1:-}" == "--uninstall" ]]; then
    rm -f "${BIN_DIR}/OffGridFinder" "${APP_DIR}/offgridfinder.desktop" "${ICON_DIR}/offgridfinder.png"
    command -v update-desktop-database >/dev/null && update-desktop-database "${APP_DIR}" || true
    echo "OffGridFinder removed. Region data is still in ${DATA_HOME}/OffGridFinder (delete it by hand if unwanted)."
    exit 0
fi

mkdir -p "${BIN_DIR}" "${APP_DIR}" "${ICON_DIR}"
install -m 755 "${HERE}/OffGridFinder" "${BIN_DIR}/OffGridFinder"
install -m 644 "${HERE}/offgridfinder.png" "${ICON_DIR}/offgridfinder.png"
sed "s|^Exec=.*|Exec=${BIN_DIR}/OffGridFinder|" "${HERE}/offgridfinder.desktop" > "${APP_DIR}/offgridfinder.desktop"
chmod 644 "${APP_DIR}/offgridfinder.desktop"
command -v update-desktop-database >/dev/null && update-desktop-database "${APP_DIR}" || true
command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -q "${DATA_HOME}/icons/hicolor" 2>/dev/null || true

echo "Installed to ${BIN_DIR}/OffGridFinder with a menu entry."
case ":${PATH}:" in
    *":${BIN_DIR}:"*) ;;
    *) echo "Note: ${BIN_DIR} is not on your PATH. The menu entry works regardless." ;;
esac
