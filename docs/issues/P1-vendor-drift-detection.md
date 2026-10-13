# P1: detect stale pinned payloads automatically

`vendor/MANIFEST.json` pins the add-on payloads to exact commits, which keeps
installation offline and reproducible. Nothing currently notices when the add-on
projects move ahead: `verify --sources` compares bytes, but only when a developer
runs it on a machine that has both checkouts.

## Acceptance criteria

- Compare each pinned `source_commit` against the branch head of the source
  repository, and report how far behind the payload is (commits and files).
- Fail a scheduled check when the payload is behind, and say which files changed.
- `verify --sources` should compare against the *checkout at that commit* rather
  than whatever is currently on disk, so a newer local checkout does not look like
  corruption.
- Document the refresh procedure next to the failure message:
  `bash scripts/vendor-update.sh && python3 scripts/luna.py verify && install`.

## Why it matters

The whole point of pinning is that the installed bytes are the reviewed bytes. A
pin that silently ages out erodes that guarantee without ever failing loudly.
