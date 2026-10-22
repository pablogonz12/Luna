# Test plan and release gates

## Automated

| Suite | Command | Covers |
| --- | --- | --- |
| Installer | `python3 -m unittest discover -s tests -p 'test_*.py'` | install all or one add-on, idempotence, dry run, refusals (Steam, Lumen sidecar, drifted `boot.lua`, divergent payload), uninstall one / last, backup-missing refusal, user data untouched, status reporting, manifest verification |
| Steam downgrade | Same Python suite (`test_steam_client.py`) | synthetic ZIP/VZip packages; automatic executable replacement, cache/pin/block, stale-log detection, offline/fresh downloads, rollback, CRC/SHA checks, path/symlink safety, account/game data preservation, running-client refusals and write-free dry runs |
| Bootstrap installer | `bash -n install.sh`; `./install.sh --help`; `./install.sh --dry-run` on a ready host | syntax/help; existing stack is reused; dry run performs no writes; upstream bootstrap is checksum-verified and uses `--nolaunch` |
| Composition | `bash scripts/test-backend.sh` | the shipped bridge and add-on backends register in one registry, expected assets are injected in order, and a deliberately broken add-on is skipped instead of stopping healthy add-ons |
| Payload pinning | `python3 scripts/luna.py verify` | every payload and shared file matches `vendor/MANIFEST.json`, and no file exists that the manifest does not list |
| Drift | `python3 scripts/luna.py verify --sources` | the pinned bytes equal the local add-on checkouts (skipped where they are absent) |
| Syntax | CI | `python3 -m py_compile`, `luac -p`, `bash -n` |

The installer suite runs against a disposable `HOME` with a minimal Lumen fixture
and a stubbed `pgrep`, so it never depends on the developer's own Steam being
open or closed.

## On-device gates

1. `preflight` reports the Steam root, manifest count, installed payload versions,
   `boot.lua` state and the data directory, and refuses while Steam or a Lumen
   sidecar is running.
2. `install` with Steam closed and no sidecar: all selected add-ons load after a restart,
   `boot.lua` shows exactly one marker, and `status` reports every file `same`.
3. Live: the Luna shell loads; Playtime and Presence report healthy agents; the
   Compressor page loads its status and settings through backend RPCs.
4. `uninstall --only presence` leaves the other add-ons working after a restart;
   `uninstall` restores `boot.lua` byte-for-byte.
5. User data: playtime, presence and compressor state are preserved across
   install, upgrade and uninstall.
6. A second add-on version (re-run `vendor-update.sh`, then install) replaces the
    payload and `status` returns to `same` everywhere.
7. On a disposable installation, the default installer produces build
   `1791918634` executables (not just metadata), retains a rollback backup, and
   leaves accounts/games/saves untouched. On launch, **new** runtime log lines
   must show `Client version: 1791918634` and `Suppressing Steam update`.
   Do not claim a runtime validation from an old log or a package manifest.

## Evidence requirements

Record the environment, the commands, the observed output and any screenshot.
State which gates were **not** run and why. Never commit Steam credentials, tokens
or account identifiers.
# HLTB view-local sorting regression checks (0.14.15)

- Home: select HowLongToBeat on All Games and leave another shelf alphabetical
  or Hours Played. Check ascending numeric times, unknown times last, then
  navigate away/back and leave the view untouched for more than five minutes.
- Collections: repeat with installed games, a user collection, filtering and
  both short/long preferences. No other dropdown may reset this view's order.
- Start Steam with HLTB selected before cache loading completes. Both home and
  collection layouts must refresh automatically when the cache arrives/changes.
- Alphabetical and Hours Played views must keep Steam's own sort and label;
  stale `AppGridDisplaySettings`/`slsLunaSortLive` must not relabel them HLTB.
- Shared app objects and collection arrays must remain unmodified. Mount/unmount
  a collection repeatedly; only mounted HLTB views subscribe to refreshes.
