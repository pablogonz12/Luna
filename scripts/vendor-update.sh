#!/usr/bin/env bash
# Re-pin the add-on payloads from their source checkouts and rewrite the manifest.
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec python3 "$root/scripts/luna.py" vendor "$@"
