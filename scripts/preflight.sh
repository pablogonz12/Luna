#!/usr/bin/env bash
# SLS Luna - thin wrapper so this project keeps the add-on projects' entry points.
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec python3 "$root/scripts/luna.py" preflight "$@"
