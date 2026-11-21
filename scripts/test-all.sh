#!/usr/bin/env bash
# One entry point for everything CI runs, so a local run matches the pipeline:
# the shell/UI tests, the Lua backend suites, the installer tests and the
# static checks. Lua is required; node is required for the UI tests.
# Every suite runs even when an earlier one fails, so one push reports every
# failure instead of only the first; the script exits non-zero at the end.
set -uo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

# Tests run real install/update code paths; never pop desktop notifications.
export LUNA_NO_NOTIFY=1

node_step() {
  local file
  for file in addons/*/plugin/public/*.js; do
    node --check "$file" || return 1
  done
  node --test tests/*.test.cjs tests/presence/*.test.cjs
}

lua_step() {
  local lua luac
  if command -v lua5.4 >/dev/null 2>&1; then lua=lua5.4
  elif command -v lua >/dev/null 2>&1; then lua=lua
  else echo 'Lua runtime unavailable; install lua' >&2; return 1
  fi
  if command -v luac5.4 >/dev/null 2>&1; then luac=luac5.4
  elif command -v luac >/dev/null 2>&1; then luac=luac
  else echo 'luac unavailable; install lua5.4 (it ships luac5.4)' >&2; return 1
  fi
  "$luac" -p tests/bridge-smoke.lua tests/presence/backend-smoke.lua tests/compressor-smoke.lua \
    tests/hltb-smoke.lua tests/sgdb-smoke.lua tests/graphics-smoke.lua bridge/bridge.lua \
    addons/*/plugin/backend/main.lua || return 1
  bash scripts/test-backend.sh
}

host_step() {
  if [[ ! -x host/bin/luna-host ]]; then
    echo 'SKIP: host/bin/luna-host missing; build it with make -C host all'
    return 0
  fi
  make -C host test
}

python_step() {
  python3 -m py_compile scripts/luna.py tests/*.py || return 1
  python3 -m unittest discover -s tests -p 'test_*.py' || return 1
  python3 scripts/luna.py verify
}

shell_step() {
  local file
  for file in scripts/*.sh; do
    bash -n "$file" || return 1
  done
}

failures=()
step() {
  local label="$1"; shift
  echo "== $label =="
  if ! "$@"; then
    failures+=("$label")
    echo "FAIL: $label" >&2
  fi
}

step 'node: check + tests' node_step
step 'lua: syntax + backend suites' lua_step
step 'host: native luna-host tests (rt_* + cdp_*)' host_step
step 'python: installer tests + compile + verify' python_step
step 'shell syntax' shell_step

if ((${#failures[@]})); then
  echo
  printf 'FAILED: %s\n' "${failures[*]}" >&2
  exit 1
fi
echo
echo 'all suites passed.'
