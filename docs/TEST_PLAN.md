# Test plan and release gates

## Automated

| Suite | Command | Covers |
| --- | --- | --- |
| Installer | `python3 -m unittest discover -s tests -p 'test_*.py'` | install all or one add-on, idempotence, dry run, refusals (Steam, Lumen sidecar, drifted `boot.lua`, divergent payload), uninstall one / last, backup-missing refusal, user data untouched, status reporting, manifest verification |
| Steam downgrade | Same Python suite (`test_steam_client.py`) | synthetic ZIP/VZip packages; automatic executable replacement, cache/pin/block, stale-log detection, offline/fresh downloads, rollback, CRC/SHA checks, path/symlink safety, account/game data preservation, running-client refusals and write-free dry runs |
| Steam refresh | Same Python suites (`test_steam_client.py`, `test_luna.py`) | explicit cache builds with manifest validation, post-update shutdown requests, backup pruning, full refresh flow (update→pin→cache→install→verify), refresh refusals/dry runs, timer consumption of queued refreshes |
| Bridge repair | Same Python suite (`test_luna.py`) + Lua smoke + UI suite | base-update bridge wipe is repaired by reinstalling (no false "nothing to do"), fail-closed Repair RPC, Health repair button wiring |
| SteamOS Mode | Same Python suite (`test_steam_machine.py`) | automatic shortcut generation (`SteamOS (RES REF)` per screen), display profiles, packaged/local helper, desktop quoting, SDL/relative and free mouse modes, session takeover via pkill with stuck-session refusal, environment isolation, adopt-and-rename migration with backups, custom edits/collisions, dry runs, duplicate-session lock |
| Bootstrap installer | `bash -n install.sh`; `./install.sh --help`; `./install.sh --dry-run` on a ready host | syntax/help; existing stack is reused; dry run performs no writes; upstream bootstrap is checksum-verified and uses `--nolaunch` |
| Composition | `bash scripts/test-backend.sh` | the shipped bridge and add-on backends register in one registry, expected assets are injected in order, and a deliberately broken add-on is skipped instead of stopping healthy add-ons |
| Payload pinning | `python3 scripts/luna.py verify` | every payload and shared file matches `vendor/MANIFEST.json`, and no file exists that the manifest does not list |
| Drift | `python3 scripts/luna.py verify --sources` | the pinned bytes equal the pinned commits in local add-on checkouts (skipped where they are absent) |
| Upstream drift | `python3 scripts/luna.py verify --upstream` | opt-in network check for stale pins; unreachable sources are reported separately |
| Health i18n | UI suite (`luna-ui.test.cjs`) + Lua smoke | Spanish attention list, headers and actions behind the Settings language switch; English output byte-identical; preference validation rejects unknown languages |
| Syntax | local gate (`test-all.sh`; hosted CI blocked, see below) | `python3 -m py_compile`, `luac -p`, `bash -n` |

The installer suite runs against a disposable `HOME` with a minimal Lumen fixture
and a stubbed `pgrep`, so it never depends on the developer's own Steam being
open or closed.
Standalone installer tests also cover enabling the managed host and creating
Steam's CEF debugging flag, preserving pre-existing settings on uninstall,
and reporting host liveness without relying on Lumen's bridge heartbeat.

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
7. On a disposable installation, the default installer produces the ubuntu12 hard lock;
   with `--steam-build 1791918634` it produces build `1791918634` executables (not just metadata), retains a rollback backup, and
   leaves accounts/games/saves untouched. On launch, **new** runtime log lines
   must show the expected `Client version` (ubuntu12 default, or 1791918634 when opted in) and `Suppressing Steam update`.
   Do not claim a runtime validation from an old log or a package manifest.
8. Open a generated SteamOS Mode shortcut on Niri. Confirm the live Gamescope
   process uses the SDL backend and intended mouse mode, Steam menus remain
   usable, and the game responds to mouse movement/camera input. Confirm the
   free-pointer desktop action omits forced capture. A good startup/log alone
   does not prove physical mouse behaviour; obtain on-device confirmation.

## Evidence requirements

Record the environment, the commands, the observed output and any screenshot.
State which gates were **not** run and why. Never commit Steam credentials, tokens
or account identifiers.

Hosted GitHub Actions is currently blocked by account billing limits, so the
local `bash scripts/test-all.sh` run is the release gate; record its result on
the release PR instead of claiming a hosted run.

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

### SteamOS Mode launcher work (0.14.16–0.14.17)

- Full local gate passed (see the release PR for exact counts): Python suites,
  UI tests, all Lua/backend suites, payload/helper verification, Python
  compilation, and shell/JavaScript syntax.
- On the desktop, generated shortcuts and migrated 2K/3K/4K entries passed
  `desktop-file-validate`. Three exact original launcher backups were retained.
  `--print-command` confirmed SDL plus Niri relative capture and a separate
  free-pointer action; no monitor configuration or game/account data was changed.
- The user reported the mouse working in the existing session. A read-only
  process check showed that session was still the old Wayland-backend launcher.
  It was not restarted or interrupted. Physical in-game mouse behaviour with
  the new SDL launcher therefore remains unvalidated until the next launch.

Note: the LuaTools installer and its Auto-fix remove `steam.cfg`; `luna steam heal`
(timer, preflight, install) must restore it and the read-only `package/`.

### Pinned rollback / libcrypto

- Local-server pinned rollback, cache-first, sha mismatch refused before kill,
  zero manifest hits fail with `package/` writable, offline fallback WARN.
- libcrypto: system ELF32 copy, pinned download with sha/ELF checks, no overwrite
  without `--force`, remove, heal. History formatting and rate-limit handling.
- Flatpak and SteamOS Gaming Mode warnings. All offline, fake HOME.
