#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
    printf '%s\n' \
        'Usage: ./scripts/install.sh [--user | --system]' \
        '' \
        '  --user    Install for the current user (default, no sudo).' \
        '  --system  Install for every user (run with sudo).'
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

if [[ "$(uname -s)" != "Linux" ]]; then
    printf 'Codex Kostyl currently supports Linux only.\n' >&2
    exit 1
fi

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd -- "$script_dir/.." && pwd)"
python_bin="${PYTHON:-python3}"

if ! command -v "$python_bin" >/dev/null 2>&1; then
    printf 'Python was not found: %s\n' "$python_bin" >&2
    exit 1
fi
if ! "$python_bin" -c 'import sys; raise SystemExit(sys.version_info < (3, 11))'; then
    printf 'Python 3.11 or newer is required.\n' >&2
    exit 1
fi

if [[ "$mode" == "system" ]]; then
    if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
        printf 'System installation requires root. Run: sudo ./scripts/install.sh --system\n' >&2
        exit 1
    fi
    app_dir="/opt/codex-kostyl"
    bin_dir="/usr/local/bin"
    data_dir="/usr/local/share"
else
    if [[ ${EUID:-$(id -u)} -eq 0 ]]; then
        printf 'Do not run a user installation with sudo. Use --system or run as your normal user.\n' >&2
        exit 1
    fi
    data_dir="${XDG_DATA_HOME:-$HOME/.local/share}"
    app_dir="$data_dir/codex-kostyl"
    bin_dir="$HOME/.local/bin"
fi

venv_dir="$app_dir/venv"
app_exec="$venv_dir/bin/codex-kostyl"
applications_dir="$data_dir/applications"
icon_theme_dir="$data_dir/icons/hicolor"
icon_dir="$icon_theme_dir/scalable/apps"

printf 'Installing Codex Kostyl (%s mode)...\n' "$mode"
install -d -m 755 "$app_dir" "$bin_dir" "$applications_dir" "$icon_dir"

if [[ ! -x "$venv_dir/bin/python" ]]; then
    "$python_bin" -m venv "$venv_dir"
fi
"$venv_dir/bin/python" -m pip install --upgrade "$project_root"

tmp_launcher="$(mktemp)"
tmp_desktop="$(mktemp)"
cleanup() {
    rm -f -- "$tmp_launcher" "$tmp_desktop"
}
trap cleanup EXIT

escape_sed_replacement() {
    local value="$1"
    value="${value//\\/\\\\}"
    value="${value//&/\\&}"
    value="${value//|/\\|}"
    printf '%s' "$value"
}

escaped_app_exec="$(escape_sed_replacement "$app_exec")"
escaped_launcher="$(escape_sed_replacement "$bin_dir/codex-kostyl")"
sed "s|@APP_EXEC@|$escaped_app_exec|g" "$project_root/packaging/codex-kostyl-launcher" >"$tmp_launcher"
sed "s|^Exec=.*$|Exec=\"$escaped_launcher\"|" "$project_root/packaging/codex-kostyl.desktop" >"$tmp_desktop"

install -m 755 "$tmp_launcher" "$bin_dir/codex-kostyl"
install -m 644 "$tmp_desktop" "$applications_dir/codex-kostyl.desktop"
install -m 644 "$project_root/src/codex_gui/assets/codex-kostyl.svg" "$icon_dir/codex-kostyl.svg"

if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$applications_dir" >/dev/null 2>&1 || true
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache -q -t "$icon_theme_dir" >/dev/null 2>&1 || true
fi

printf '\nInstallation complete. Open “Codex Kostyl” from the application menu.\n'
printf 'Command-line launcher: %s/codex-kostyl\n' "$bin_dir"
