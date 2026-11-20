#!/usr/bin/env bash
# Default: install Luna's standalone luna-host (no Lumen). With --lumen, install
# the slsteam-moon/Lumen/LuaTools base first, then layer SLS Luna on top.
set -euo pipefail

readonly LUNA_REPO="pgm1207/Luna"
# The pinned LuaTools-Moon installer commit/sha256 live in scripts/luna.py
# (`luna bootstrap`), the single source of truth for the base installation.

usage() {
  cat <<'EOF'
SLS Luna installer

Usage:
  bash install.sh [options]

One-liner (fresh PC):
  curl -fsSL https://raw.githubusercontent.com/pgm1207/Luna/main/install.sh | bash

Or run `bash install.sh` from a checkout.

By default this installs Luna's own luna-host in ~/.local/share/Luna: no
Lumen, no LuaTools and no Steam wrapper patch. It installs the system packages,
the add-ons, the launchers, the user unit, ~/.local/bin/luna and the timer, and
leaves the Steam client exactly as it is.

With --lumen it keeps an existing LuaTools-Moon stack intact (or installs the
pinned one), pins and hard-locks the Steam client first, runs the pinned
LuaTools-Moon installer with --nolaunch, then installs the sha256-verified SLS
Luna add-ons on top. An existing install whose host.json says lumen is never
downgraded: a plain run refreshes its Luna add-ons only.

On a fresh PC the package step installs native Steam (32-bit support enabled),
32-bit libcrypto, mangohud, gamescope, gamemode (+32-bit parts), curl jq tar
unzip git python3 zenity (pacman/apt/dnf; sudo is asked for only on an
interactive terminal, otherwise the exact command is printed). Re-running
changes nothing that is already in place.

--lumen rolls Steam back to the ubuntu12 client and hard-locks it (Steam.cfg +
steam.cfg, read-only package/); use --lock-steam-client to do that on the
standalone host. The historical build 1791918634 is opt-in with
--steam-build 1791918634. Steam/Lumen must be fully closed. The same flow is
available after install as `luna bootstrap` and `luna update-all`;
`luna doctor` prints a health report.
SteamOS Mode application shortcuts are generated automatically, including SDL
mouse compatibility and a free-pointer action. Gamescope is a system dependency.

Options:
  --lumen                 Opt in to the full Lumen/LuaTools/SLSsteam base pipeline
  --lock-steam-client     Standalone host: pin and hard-lock the Steam client
  --only NAME             Install one Luna add-on (repeatable)
  --force                 Replace installed add-on files that differ
  --dry-run               Show Luna changes without writing them
  --keep-steam-client     Leave the Steam client exactly as it is (no lock)
  --steam-build BUILD     Install the historical build 1791918634 instead of the hard lock
  --no-system-packages    Skip the distro package step (Steam, 32-bit libs, tools)
  --no-mangohud           Do not install mangohud (+32-bit)
  --no-gamescope          Do not install gamescope
  --no-gamemode           Do not install gamemode (+32-bit)
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
    --noplugin) die "--noplugin is not supported: install the standalone host without --lumen instead." ;;
    --force|--dry-run|--keep-steam-client|--lumen|--lock-steam-client|--no-system-packages|--no-mangohud|--no-gamescope|--no-gamemode|--no-steam-machine|--migrate-steam-machine) luna_args+=("$1"); shift ;;
    --steam-build)
      (($# >= 2)) || die "--steam-build requires a build number."
      luna_args+=("$1" "$2"); shift 2 ;;
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

: "${HOME:?HOME must be set}"

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

printf '\nSetting up SLS Luna (standalone host, or the Lumen base with --lumen).\n'
python3 "$script_dir/scripts/luna.py" bootstrap "${luna_args[@]}" "${base_args[@]}"
