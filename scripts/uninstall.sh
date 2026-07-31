#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
    printf '%s\n' \
        'Usage: ./scripts/uninstall.sh [--user | --system]' \
        '' \
        '  --user    Remove the current-user installation (default).' \
        '  --system  Remove the system-wide installation (run with sudo).'
}

mode="user"
if [[ $# -gt 1 ]]; then
    usage >&2
    exit 2
fi
if [[ $# -eq 1 ]]; then
    case "$1" in
        --user) mode="user" ;;
        --system) mode="system" ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; exit 2 ;;
    esac
fi

if [[ "$mode" == "system" ]]; then
    if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
        printf 'System removal requires root. Run: sudo ./scripts/uninstall.sh --system\n' >&2
        exit 1
    fi
    app_dir="/opt/codex-kostyl"
    bin_dir="/usr/local/bin"
    data_dir="/usr/local/share"
else
    if [[ ${EUID:-$(id -u)} -eq 0 ]]; then
        printf 'Do not run a user removal with sudo. Use --system or run as your normal user.\n' >&2
        exit 1
    fi
    data_dir="${XDG_DATA_HOME:-$HOME/.local/share}"
    app_dir="$data_dir/codex-kostyl"
    bin_dir="$HOME/.local/bin"
fi

case "$app_dir" in
    */codex-kostyl) ;;
    *) printf 'Refusing to remove an unexpected path: %s\n' "$app_dir" >&2; exit 1 ;;
esac

rm -rf -- "$app_dir"
rm -f -- \
    "$bin_dir/codex-kostyl" \
    "$data_dir/applications/codex-kostyl.desktop" \
    "$data_dir/icons/hicolor/scalable/apps/codex-kostyl.svg"

if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$data_dir/applications" >/dev/null 2>&1 || true
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache -q -t "$data_dir/icons/hicolor" >/dev/null 2>&1 || true
fi

printf 'Codex Kostyl (%s mode) has been removed.\n' "$mode"
