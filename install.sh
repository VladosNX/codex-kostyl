#!/usr/bin/env bash
set -Eeuo pipefail

repository="https://github.com/VladosNX/codex-kostyl"
mode="user"
requested_version="${CODEX_KOSTYL_VERSION:-latest}"

usage() {
    printf '%s\n' \
        'Remote installer for Codex Kostyl' \
        '' \
        'Usage: install.sh [--user | --system] [--version VERSION]' \
        '' \
        '  --user             Install for the current user (default).' \
        '  --system           Install for every user (requires root).' \
        '  --version VERSION  Install a release such as 0.2.0.'
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --user)
            mode="user"
            shift
            ;;
        --system)
            mode="system"
            shift
            ;;
        --version)
            if [[ $# -lt 2 || -z "$2" ]]; then
                printf '%s\n' 'The --version option requires a value.' >&2
                exit 2
            fi
            requested_version="$2"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            usage >&2
            exit 2
            ;;
    esac
done

if [[ "$(uname -s)" != "Linux" ]]; then
    printf 'The remote installer currently supports Linux only.\n' >&2
    exit 1
fi
for command_name in curl tar; do
    if ! command -v "$command_name" >/dev/null 2>&1; then
        printf '%s is required for remote installation.\n' "$command_name" >&2
        exit 1
    fi
done

archive_url=""
source_label=""
if [[ "$requested_version" == "latest" ]]; then
    latest_url="$(
        curl -fsSL -o /dev/null -w '%{url_effective}' \
            "$repository/releases/latest" 2>/dev/null || true
    )"
    if [[ "$latest_url" == *'/tag/'* ]]; then
        release_tag="${latest_url##*/tag/}"
        if [[ ! "$release_tag" =~ ^v[0-9]+\.[0-9]+\.[0-9]+([+-][0-9A-Za-z.-]+)?$ ]]; then
            printf 'The latest release has an unsupported tag: %s\n' "$release_tag" >&2
            exit 1
        fi
        archive_url="$repository/archive/refs/tags/$release_tag.tar.gz"
        source_label="release ${release_tag#v}"
    else
        archive_url="$repository/archive/refs/heads/main.tar.gz"
        source_label="the main branch (no published release found)"
    fi
else
    release_tag="$requested_version"
    [[ "$release_tag" == v* ]] || release_tag="v$release_tag"
    if [[ ! "$release_tag" =~ ^v[0-9]+\.[0-9]+\.[0-9]+([+-][0-9A-Za-z.-]+)?$ ]]; then
        printf 'Invalid version: %s\n' "$requested_version" >&2
        exit 2
    fi
    archive_url="$repository/archive/refs/tags/$release_tag.tar.gz"
    source_label="release ${release_tag#v}"
fi

bootstrap_dir="$(mktemp -d)"
cleanup() {
    rm -rf -- "$bootstrap_dir"
}
trap cleanup EXIT

printf 'Downloading Codex Kostyl from %s...\n' "$source_label"
curl -fsSL --retry 3 "$archive_url" -o "$bootstrap_dir/source.tar.gz"
tar -xzf "$bootstrap_dir/source.tar.gz" -C "$bootstrap_dir"

shopt -s nullglob
extracted_roots=("$bootstrap_dir"/*/)
shopt -u nullglob
if [[ ${#extracted_roots[@]} -ne 1 ]]; then
    printf 'The downloaded source archive has an unexpected layout.\n' >&2
    exit 1
fi

project_root="${extracted_roots[0]%/}"
printf 'Running the local installer from the downloaded source tree...\n'
bash "$project_root/scripts/install.sh" "--$mode"
