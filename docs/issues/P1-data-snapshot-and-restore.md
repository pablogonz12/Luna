# P1: snapshot and restore add-on data as a set

Uninstall deliberately keeps `~/.local/share/slssteam-integrations` (playtime
ledgers and the presence mapping), and the Decky migration showed how much value
sits in that directory. There is no Lua-side way to take or restore a consistent
snapshot of it.

## Acceptance criteria

- `luna backup [--out DIR]` writes one archive containing the playtime ledgers
  (including `.bak`), `presence.json` and the account mapping, plus a manifest of
  file hashes and the pinned add-on versions.
- `luna restore ARCHIVE` validates that manifest and refuses on mismatch, unless
  `--force`, and never overwrites a newer ledger without saying so.
- Both commands refuse while Steam and the sidecar are running, matching install.
- Round-trip test: back up, mutate, restore, byte-compare.
