# Test plan and release gates

## Rules for the suite

- Tests never write inside the checkout. Anything that must modify add-on files, `vendor/` or `scripts/` (tamper, dirty-tree and vendor tests) runs against a temporary copy. `scripts/test-all.sh` snapshots `git status` and the hashes of tracked files under `addons/`, `vendor/` and `scripts/` before and after the run and fails, listing the paths, if they changed.
- One suite run per checkout at a time. `scripts/test-all.sh` takes a `flock` lock (under `$XDG_RUNTIME_DIR` or `/tmp`, keyed by the checkout path) and a second concurrent run exits 75 immediately. The lock is released when the script exits or is killed.

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
| Drift | `python3 scripts/luna.py verify --sources` | the pinned bytes equal the local add-on checkouts (skipped where they are absent) |
| Health i18n | UI suite (`luna-ui.test.cjs`) + Lua smoke | Spanish attention list, headers and actions behind the Settings language switch; English output byte-identical; preference validation rejects unknown languages |
| Syntax | local gate (`test-all.sh`; hosted CI blocked, see below) | `python3 -m py_compile`, `luac -p`, `bash -n` |

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
8. Open a generated SteamOS Mode shortcut on Niri. Confirm the live Gamescope
   process uses the SDL backend and intended mouse mode, Steam menus remain
   usable, and the game responds to mouse movement/camera input. Confirm the
   free-pointer desktop action omits forced capture. A good startup/log alone
   does not prove physical mouse behaviour; obtain on-device confirmation.

### GUI checks

`luna gui verify` (needs `agent-desktop`, a signed-in Steam started with
`luna gui launch`) can now cover the panel-loading part of gates 2 and 3: after
`install` and a restart it checks that the Luna button and the Playtime, Presence
and Compressor panels are present and stable in Steam's accessibility tree, and
treats "Sign in to Steam" as the startup window until it persists past `--login-grace` (default 120 s) with no log-on in `connection_log.txt`; it writes a markdown report plus a screenshot of the Steam window only as evidence
(niri `screenshot-window`; no full-screen capture unless `--full-screen` is passed). It does **not** cover
gates 7 and 8 (client build, Gamescope/mouse behaviour) or backend health. The
selectors in `GUI_PANEL_SELECTORS` were confirmed on a live signed-in client (Luna entry
`button 'Luna'`, overlay pages `heading 'Playtime'/'Presence'/'Compressor'`); the
HowLongToBeat, SteamGridDB, Graphics, Health, Settings and About pages are confirmed
too (`gui watch --addon KEY`). A real modal dialog was never observed (menu items
refuse clicks by policy and physical input is unavailable on Wayland), so the
`alert`/`dialog` shape is **unverified** (a secondary window is a plain `webarea`).
Confirmed live (2026-11): the `starting` -> `main` screen transition during auto-logon
(the login form itself never appeared, so its tree is still unverified); Steam's popup
menu webareas (`Help Root Menu`, `Supernav`, `Account Menu`, `Notifications Menu`)
stay in the AT-SPI tree while idle and are NOT windows (cross-checked against niri
titles); `gui launch` never restarts a running Steam; `restart-check --final normal`
ends in normal mode (`gui status` accessible: no).
`luna gui restart-check` now covers the restart part of gates 2-4 with before/after
evidence (Library order and sort, add-on panels, the HowLongToBeat-sorted Home grid
read over CDP when that sort is active (otherwise skipped, never a failure; the sort
is never toggled), sidecar survival, shutdown and relaunch times); `library --save/--compare --common` remains for manual use. Not
covered: other compositors than niri. Fleet hosts: `luna fleet gui [--verify]` is built and
hermetically tested (`tests/test_fleet.py`, mocked ssh), but NOT yet confirmed against a real
remote host with a running accessible Steam (and `gui` session discovery over non-interactive SSH is unproven).

Review-fix coverage (all hermetic: temp HOME, fake `/proc`, fake niri/wl-clipboard, local loopback HTTP):
`tests/test_proton.py` (symlinked tool dirs, unreadable `config.vdf`, fail-closed process scan, Steam re-check before
rename/delete, symlink-chain tar escapes vs legitimate internal links, malformed manifest, non-UTF-8 files),
`tests/test_gui_check.py` (empty/unreadable/CDP-lost restart compare, stable-order settle, clipboard PNG and SIGTERM
restore, 0600/0700 permissions, `watch` unreadable streak, empty-baseline refusal) and `tests/test_live_probe.py`
(redirect and non-loopback refusal, zero-row library, `--save` refusal/permissions). Not run against a real Steam.

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
