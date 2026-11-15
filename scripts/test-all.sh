#!/usr/bin/env bash
# One entry point for everything CI runs, so a local run matches the pipeline:
# the shell/UI tests, the Lua backend suites, the installer tests and the
# static checks. Lua is required; node is required for the UI tests.
set -euo pipefail
# Hermetic: no test may probe the real desktop (tests/test_gui_check.py manages this itself).
export LUNA_GUI_CHECKS=0
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

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

echo
echo 'all suites passed.'
