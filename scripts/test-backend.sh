#!/usr/bin/env bash
# Run the Lua composition test in a disposable HOME with the real payloads.
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if command -v lua5.4 >/dev/null 2>&1; then lua=lua5.4
elif command -v lua >/dev/null 2>&1; then lua=lua
else echo 'Lua runtime unavailable; install lua' >&2; exit 1
fi
tmp="$(mktemp -d)"
trap 'rm -rf -- "$tmp"' EXIT
mkdir -p "$tmp/home"
LUNA_TEST_HOME="$tmp/home" HOME="$tmp/home" LUNA_REPO="$root" "$lua" "$root/tests/bridge-smoke.lua"
LUNA_TEST_HOME="$tmp/home" HOME="$tmp/home" LUNA_REPO="$root" "$lua" "$root/tests/compressor-smoke.lua"
LUNA_TEST_HOME="$tmp/home" HOME="$tmp/home" LUNA_REPO="$root" "$lua" "$root/tests/hltb-smoke.lua"
LUNA_TEST_HOME="$tmp/home" HOME="$tmp/home" LUNA_REPO="$root" "$lua" "$root/tests/sgdb-smoke.lua"
LUNA_TEST_HOME="$tmp/home" HOME="$tmp/home" LUNA_REPO="$root" \
  SLS_GRAPHICS_SYSFS="$tmp/home/sys" SLS_GRAPHICS_OS_RELEASE="$tmp/home/os-release" \
  SLS_GRAPHICS_SYSTEM_LAYER_DIRS="$tmp/home/no-system-layers" \
  "$lua" "$root/tests/graphics-smoke.lua"
