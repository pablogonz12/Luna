#!/usr/bin/env bash
# Default: install Luna's standalone luna-host (no Lumen). With --lumen, install
# the slsteam-moon/Lumen/LuaTools base first, then layer SLS Luna on top.
set -euo pipefail

readonly LUNA_REPO="pgm1207/Luna"
# The pinned LuaTools-Moon installer commit/sha256 live in scripts/luna.py
# (`luna bootstrap`), the single source of truth for the base installation.
#
# Which Luna to install: by default the newest published GitHub release (the
# tree CI tested and tagged), never the moving branch tip. --main or
# LUNA_REF=main takes the tip; LUNA_REF=<tag|sha> pins one. LUNA_API overrides
# the API base (offline hosts, tests).
readonly API_BASE="${LUNA_API:-https://api.github.com}"

api_get() {
  # Short, bounded: a slow or blocked API must not stall the one-liner.
  curl -fsSL --connect-timeout 3 --max-time 8 \
    -H 'Accept: application/vnd.github+json' "$@"
}

# The newest published release tag, or nothing when the API is unreachable or
# the repository has no release yet.
latest_release_tag() {
  local body tag
  command -v python3 >/dev/null 2>&1 || return 1
  body="$(api_get "${API_BASE}/repos/${LUNA_REPO}/releases/latest" 2>/dev/null)" || return 1
  [[ -n "$body" ]] || return 1
  tag="$(printf '%s' "$body" | python3 -c 'import json,sys
try:
    print(json.load(sys.stdin).get("tag_name") or "")
except Exception:
    pass' 2>/dev/null)" || true
  [[ "$tag" =~ ^[A-Za-z0-9._-]{1,64}$ ]] || return 1
  printf '%s' "$tag"
}

usage() {
  cat <<'EOF'
SLS Luna installer

Usage:
  bash install.sh [options]

One-liner (fresh PC):
  curl -fsSL https://raw.githubusercontent.com/pgm1207/Luna/main/install.sh | bash

Or run `bash install.sh` from a checkout (the checkout is installed as-is
unless a ref is asked for explicitly).

The one-liner installs the newest *published release* tag: the same tree CI ran
the full suite on, not whatever happens to be on main. If GitHub has no release
or the API is unreachable, it says so in one line and installs main instead.
  --main                  Install the main branch tip instead of the latest release
  LUNA_REF=<tag|sha>      Pin one release tag or commit (env; same as --main for "main")
  LUNA_API=<url>          Use another GitHub API base (offline mirrors, tests)

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
  --main                  Install the main branch tip, not the latest release
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
    --main) LUNA_REF=main; shift ;;
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

# Piped through bash there is no BASH_SOURCE; stay quiet under set -u.
# Which ref to install. An explicit --main/LUNA_REF always wins, even next to a
# checkout; otherwise the newest release, then main as the last resort.
requested_ref="${LUNA_REF:-}"
ref=""
case "$requested_ref" in
  "") ref="release" ;;
  main|master) ref="main" ;;
  *) ref="$requested_ref" ;;
esac

script_dir=""
if [[ -n "${BASH_SOURCE[0]:-}" ]]; then
  script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" 2>/dev/null && pwd || true)"
fi
# A checkout with no ref requested installs that checkout: the developer's own
# tree, unchanged, exactly as before.
if [[ -n "$script_dir" && -f "$script_dir/scripts/luna.py" && -z "$requested_ref" ]]; then
  printf 'SLS Luna: installing from the local checkout at %s.\n' "$script_dir"
  ref="checkout"
fi
if [[ "$ref" != "checkout" ]]; then
  command -v curl >/dev/null || die "curl is required to fetch the SLS Luna payload."
  command -v python3 >/dev/null || die "python3 is required to fetch the SLS Luna payload."
  command -v tar >/dev/null || die "tar is required to unpack the SLS Luna payload."
  if [[ "$ref" == "release" ]]; then
    release_tag="$(latest_release_tag || true)"
    if [[ -n "$release_tag" ]]; then
      ref="$release_tag"
      printf 'SLS Luna: installing release %s (the latest published release).\n' "$ref"
    else
      ref="main"
      printf 'SLS Luna: no published release found (or the GitHub API is unreachable); installing main instead.\n'
    fi
  fi
  [[ "$ref" =~ ^[A-Za-z0-9._-]{1,64}$ ]] || die "Invalid ref: ${ref}"
  tmp_dir="$(mktemp -d "${TMPDIR:-/tmp}/sls-luna-install.XXXXXX")"

  fetch_ref() { # $1 = ref, $2 = output tarball
    local url
    case "$1" in
      main) url="https://codeload.github.com/${LUNA_REPO}/tar.gz/main" ;;
      *)
        if [[ "$1" =~ ^[0-9a-f]{40}$ ]]; then
          url="https://codeload.github.com/${LUNA_REPO}/tar.gz/${1}"
        else
          url="https://codeload.github.com/${LUNA_REPO}/tar.gz/refs/tags/${1}"
        fi ;;
    esac
    curl -fsSL --connect-timeout 5 --max-time 120 "$url" -o "$2"
  }

  unpack_ref() { # $1 = tarball; prints the tree root
    local root
    rm -rf -- "$tmp_dir/tree"
    mkdir -p -- "$tmp_dir/tree"
    tar -xzf "$1" -C "$tmp_dir/tree"
    root="$(find "$tmp_dir/tree" -mindepth 1 -maxdepth 1 -type d -print -quit)"
    [[ -f "$root/scripts/luna.py" && -f "$root/vendor/MANIFEST.json" ]] || return 1
    printf '%s' "$root"
  }

  fetch_ref "$ref" "$tmp_dir/luna.tar.gz" \
    || die "Could not download SLS Luna at ${ref}."
  script_dir="$(unpack_ref "$tmp_dir/luna.tar.gz")" \
    || die "Downloaded SLS Luna archive (${ref}) is incomplete."

  # A release cut before the standalone host has no `bootstrap`; this script's
  # flags belong to the newer tree, so that release cannot be installed here.
  if [[ "$ref" != "main" ]] && ! python3 "$script_dir/scripts/luna.py" bootstrap --help >/dev/null 2>&1; then
    printf 'SLS Luna: release %s predates the standalone installer; installing main instead.\n' "$ref"
    ref="main"
    fetch_ref main "$tmp_dir/luna.tar.gz" || die "Could not download SLS Luna at main."
    script_dir="$(unpack_ref "$tmp_dir/luna.tar.gz")" || die "Downloaded SLS Luna main archive is incomplete."
  fi
fi

printf '\nSetting up SLS Luna (standalone host, or the Lumen base with --lumen).\n'
# This tree came from a tarball in a temporary directory that the EXIT trap
# deletes, and a fresh machine has no `luna` command yet: tell the pipeline to
# quote the one-liner itself, with these exact flags, when it has to tell the
# user how to resume.
if [[ "$ref" != "checkout" ]]; then
  export LUNA_INSTALL_ONELINER=1
  install_quoted=()
  for arg in "${luna_args[@]}" "${base_args[@]}"; do
    install_quoted+=("$(printf '%q' "$arg")")
  done
  export LUNA_INSTALL_FLAGS="${install_quoted[*]}"
fi
python3 "$script_dir/scripts/luna.py" bootstrap "${luna_args[@]}" "${base_args[@]}"
