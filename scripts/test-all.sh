#!/usr/bin/env bash
# One entry point for everything CI runs, so a local run matches the pipeline:
# the shell/UI tests, the Lua backend suites, the installer tests and the
# static checks. Lua is required; node is required for the UI tests.
set -euo pipefail
# Hermetic: no test may probe the real desktop (tests/test_gui_check.py manages this itself).
export LUNA_GUI_CHECKS=0
# Likewise no test may reach a real Steam through its DevTools port (the live probe tests use a fake one).
export LUNA_LIVE_CHECKS=0
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

# One suite run per checkout: a second concurrent run refuses at once. The lock
# lives outside the tree, is keyed by the checkout path, and the kernel drops it
# when this script exits or is killed.
lock_dir="${XDG_RUNTIME_DIR:-/tmp}"
lock_file="$lock_dir/luna-test-all-$(printf '%s' "$root" | sha256sum | cut -c1-16).lock"
exec 9>"$lock_file"
if ! flock -n 9; then
  echo "test-all.sh: another suite run is already using $root (lock $lock_file); refusing to start a second one." >&2
  exit 75
fi

# Tests must never write inside the checkout: snapshot `git status` and the
# content hashes of addons/, vendor/ and scripts/, and compare after the run.
tree_snapshot() {
  git status --porcelain
  git ls-files -z -- addons vendor scripts | xargs -0 -r sha256sum 2>&1
}
guard_dir="$(mktemp -d)"
trap 'rm -rf "$guard_dir"' EXIT
if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  tree_snapshot >"$guard_dir/before" || true
  guard=1
else
  echo 'note: not a git checkout; skipping the tree-unchanged guard.'
  guard=0
fi

echo '== personal-data leak check =='
bash scripts/check-personal-data.sh

echo '== node: check + tests =='
for file in addons/*/plugin/public/*.js; do node --check "$file"; done
node --test tests/*.test.cjs tests/presence/*.test.cjs

echo '== lua: syntax + backend suites =='
if command -v lua5.4 >/dev/null 2>&1; then lua=lua5.4
elif command -v lua >/dev/null 2>&1; then lua=lua
else echo 'Lua runtime unavailable; install lua' >&2; exit 1
fi
luac -p tests/bridge-smoke.lua tests/presence/backend-smoke.lua tests/compressor-smoke.lua tests/hltb-smoke.lua \
  tests/sgdb-smoke.lua tests/graphics-smoke.lua bridge/bridge.lua \
  addons/*/plugin/backend/main.lua
bash scripts/test-backend.sh

echo '== python: installer tests + compile + verify =='
python3 scripts/build-luna.py --check
python3 -m py_compile scripts/luna.py scripts/lumen-hook.py tests/*.py
python3 -m unittest discover -s tests -p 'test_*.py'
python3 scripts/luna.py verify

echo '== shell syntax =='
bash -n scripts/*.sh

if [ "$guard" = 1 ]; then
  tree_snapshot >"$guard_dir/after" || true
  if ! diff -u "$guard_dir/before" "$guard_dir/after" >"$guard_dir/diff"; then
    echo 'FAIL: the test run changed files inside the checkout:' >&2
    grep -E '^[+-][^+-]' "$guard_dir/diff" >&2 || true
    exit 1
  fi
fi

echo
echo 'all suites passed.'
