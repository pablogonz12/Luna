#!/usr/bin/env bash
# Install the supported slsteam-moon/Lumen/LuaTools base, then layer SLS Luna on top.
# This intentionally calls (rather than copies) the LuaTools-Moon installer.
set -euo pipefail

readonly LUNA_REPO="pgm1207/Luna"
readonly UPSTREAM_REPO="swwayps/luatools-moon"
readonly UPSTREAM_INSTALLER_COMMIT="2c739b5349a972bb1932ac9457b0acf91b09e044"
readonly UPSTREAM_INSTALLER_SHA256="1c6896832d32d6d17c5635daf95b539763955f7ea1d9bd4446e24c2e8a5806c2"

usage() {
  cat <<'EOF'
SLS Luna installer

Usage:
  bash install.sh [options]

The curl one-liner is available once pgm1207/Luna is public. For now, clone the
private repository and run bash install.sh from its checkout.

The installer keeps an existing LuaTools-Moon stack intact. If no complete base
is found, it runs the pinned LuaTools-Moon installer with --nolaunch, then installs
the sha256-verified SLS Luna add-ons. LuaTools is currently required by Luna's
backend contract; --noplugin is therefore intentionally unsupported.

By default Luna installs and verifies Steam client build 1791918634, caches and
pins it, and blocks Steam self-updates. Steam/Lumen must be fully closed.
SteamOS Mode application shortcuts are generated automatically, including SDL
mouse compatibility and a free-pointer action. Gamescope is a system dependency.

Options:
  --only NAME             Install one Luna add-on (repeatable)
  --force                 Replace installed add-on files that differ
  --dry-run               Show Luna changes without writing them
  --keep-steam-client     Skip the default Steam compatibility downgrade
  --no-steam-machine      Do not generate SteamOS Mode shortcuts
  --migrate-steam-machine Back up, rename and upgrade recognized old launchers to SteamOS Mode
  --slsteam-channel NAME  Pass stable or beta to the base installer
  --plugin-channel NAME   Pass stable or beta to the base installer
  --lumen-channel NAME   Pass stable or beta to the base installer
  --keep-millennium       Pass through to the base installer
  -h, --help              Show this help

The base installer selects the requested upstream release channels (stable by
default). Luna's own add-on payloads are pinned in vendor/MANIFEST.json.
EOF
}

die() { printf 'SLS Luna installer: %s\n' "$*" >&2; exit 1; }

[[ "${EUID:-$(id -u)}" -ne 0 ]] || die "Do not run as root."

luna_args=()
base_args=()
while (($#)); do
  case "$1" in
    -h|--help) usage; exit 0 ;;
    --noplugin) die "--noplugin is not supported: current Luna features require the LuaTools backend." ;;
    --force|--dry-run|--keep-steam-client|--no-steam-machine|--migrate-steam-machine) luna_args+=("$1"); shift ;;
    --only)
      (($# >= 2)) || die "--only requires an add-on name."
      luna_args+=("$1" "$2"); shift 2 ;;
    --slsteam-channel|--plugin-channel|--lumen-channel)
      (($# >= 2)) || die "$1 requires stable or beta."
      base_args+=("$1" "$2"); shift 2 ;;
    --keep-millennium) base_args+=("$1"); shift ;;
    *) die "Unknown option: $1 (see --help)." ;;
  esac
done

home="${HOME:?HOME must be set}"
lumen_dir="${LUMEN_DIR:-$home/.local/share/Lumen}"
steam_wrapper="$home/.local/share/SLSsteam/path/steam"

base_ready=1
base_any=0
for path in "$lumen_dir/lua/boot.lua" "$lumen_dir/luatools/backend/main.lua" "$steam_wrapper"; do
  if [[ -e "$path" ]]; then base_any=1; else base_ready=0; fi
done

tmp_dir=""
cleanup() { [[ -z "$tmp_dir" ]] || rm -rf -- "$tmp_dir"; }
trap cleanup EXIT

# A curl|bash invocation has no checkout beside the script. Fetch one immutable
# repository commit, then install from its verified manifest just like a clone.
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" 2>/dev/null && pwd || true)"
if [[ -z "$script_dir" || ! -f "$script_dir/scripts/luna.py" ]]; then
  command -v curl >/dev/null || die "curl is required to fetch the SLS Luna payload."
  command -v python3 >/dev/null || die "python3 is required to fetch the SLS Luna payload."
  command -v tar >/dev/null || die "tar is required to unpack the SLS Luna payload."
  tmp_dir="$(mktemp -d "${TMPDIR:-/tmp}/sls-luna-install.XXXXXX")"
  commit="$(curl -fsSL -H 'Accept: application/vnd.github+json' \
    "https://api.github.com/repos/${LUNA_REPO}/commits/main" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["sha"])')" \
    || die "Could not resolve the SLS Luna main commit."
  [[ "$commit" =~ ^[0-9a-f]{40}$ ]] || die "GitHub returned an invalid SLS Luna commit."
  curl -fsSL "https://codeload.github.com/${LUNA_REPO}/tar.gz/${commit}" -o "$tmp_dir/luna.tar.gz" \
    || die "Could not download SLS Luna at ${commit}."
  tar -xzf "$tmp_dir/luna.tar.gz" -C "$tmp_dir"
  script_dir="$(find "$tmp_dir" -mindepth 1 -maxdepth 1 -type d -print -quit)"
  [[ -f "$script_dir/scripts/luna.py" && -f "$script_dir/vendor/MANIFEST.json" ]] \
    || die "Downloaded SLS Luna archive is incomplete."
fi

if ((base_ready == 0)); then
  if ((base_any)); then
    die "A partial Moon/Lumen installation was found. Repair the base with its upstream installer first; refusing to clean or replace it automatically."
  fi
  if [[ " ${luna_args[*]} " == *" --dry-run "* ]]; then
    die "The Moon/Lumen/LuaTools base is missing; --dry-run will not bootstrap or modify it."
  fi
  command -v curl >/dev/null || die "curl is required to install the Moon/Lumen base."
  command -v sha256sum >/dev/null || die "sha256sum is required to verify the upstream installer."
  tmp_dir="${tmp_dir:-$(mktemp -d "${TMPDIR:-/tmp}/sls-luna-install.XXXXXX")}"
  upstream_script="$tmp_dir/luatools-moon-install.sh"
  curl -fsSL "https://raw.githubusercontent.com/${UPSTREAM_REPO}/${UPSTREAM_INSTALLER_COMMIT}/install.sh" \
    -o "$upstream_script" || die "Could not download the pinned LuaTools-Moon installer."
  actual_sha="$(sha256sum "$upstream_script" | awk '{print $1}')"
  [[ "$actual_sha" == "$UPSTREAM_INSTALLER_SHA256" ]] \
    || die "LuaTools-Moon installer checksum mismatch; aborting before installing the base."
  printf 'Installing Moon/Lumen/LuaTools base from %s @ %s\n' "$UPSTREAM_REPO" "$UPSTREAM_INSTALLER_COMMIT"
  bash "$upstream_script" --nolaunch "${base_args[@]}"
fi

printf '\nInstalling SLS Luna add-ons on top of the existing base.\n'
python3 "$script_dir/scripts/luna.py" verify
python3 "$script_dir/scripts/luna.py" install "${luna_args[@]}"
