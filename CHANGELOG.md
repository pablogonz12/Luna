# Changelog

Notable changes to the Luna bundle. Versions follow the `luna` add-on payload
(the bundle version); the other add-ons keep their own versions.

## 0.14.19 — 2026-11-21

- **Playtime across two machines**: each installation writes only its own
  contribution file and the reply is the merge of all of them, so a desktop and
  a laptop sharing the data directory through Syncthing cannot overwrite each
  other. A per-writer high-water floor keeps a stale ledger from lowering
  recorded totals, recovered pulses are revalidated against the baseline,
  conflict copies are merged once, imports carry their export time, and every
  store has a `.bak` with atomic writes. 30 smoke tests, from three independent
  reviews.
- **Page fixes**: Presence says when Discord cannot publish and why; Playtime
  has one time format, a busy Save and a two-step import confirm; Artwork shows
  change times, refuses Undo at zero depth and asks for a restart only when the
  kind needs one; Graphics marks the GPU in use and "checked N s ago"; a run of
  rate-limit and error states are rendered inline.
- **Faster tabs**: a tab switch issues only that page's reads, and issues them
  together — 13 sequential round trips became 5 plus parallel page calls.
  HowLongToBeat data is read with its `rev` and only when something needs it.
- **Backends**: HowLongToBeat no longer matches a sequel to the base game and
  re-checks stored matches instead of trusting them; every store is written
  atomically with a `.bak` and reports recovery; Presence running entries
  expire and the reply states whether Discord can publish; SteamGridDB keeps
  the key server-side, reports change times and rate limits; Graphics reports
  the GPU in use; page-load handlers are cached (Health 199 → 8 ms, HLTB first
  reply 223 → 3 ms) behind a latency budget test.
- **Menu**: artwork undo and history per slot, a 30-day Playtime chart with a
  week-over-week line, and arrow-key navigation for the sidebar.
- **Installer and health**: `--dry-run` prints the plan and never stops for a
  running Steam; a stopped one-liner tells you to re-run the one-liner rather
  than a deleted temporary path; the bridge records when it attached, so
  `luna doctor` distinguishes "host started, Steam closed" from "stale".
- **`luna selftest`** now settles the backend before timing calls, checks the
  embedded Lumen page by its position rather than DOM containment, and works
  when Steam's window is hidden.

- **Luna menu on Store and Community**: the menu opens clickable there now. On
  those routes Steam composites a separate browser view above the shell
  document, so the overlay was visible but received no clicks (it worked on the
  Library only). Steam's view is now hidden while the menu is open and put back
  on close, through the add-on's SharedJSContext agent (the manager lives in
  that context, not in the panel's document). The menu no longer navigates the
  user to the Library; that remains only as the fallback when the view API is
  unavailable, with one short line explaining it.
- **Luna host**: `luna install` now deploys Luna's own plugin host (`luna-host`)
  into `~/.local/share/Luna` next to Lumen, with a `luna-host.service` user unit
  that is not enabled until `luna host cutover`; `luna host status|cutover|rollback`
  switch hosts safely (Steam closed, Lumen never deleted). Release bundles carry
  the prebuilt binary and Lua tree. The backend resolves its state dir from
  `$LUNA_HOME` under luna-host (`SlsLunaHost` RPC, Health/Settings "Host" line).
  Luna now drives Lumen updates itself while Lumen's own updater stays blocked
  (the `lua/about.lua` block is unchanged): `luna lumen status|update|cancel|restore`,
  the `SlsLunaLumen` RPC and a new **Updates** page (after Health) with an
  "Update Lumen" button and "Update everything". A Lumen update is queued while Steam
  runs, backs Lumen up, runs the upstream installer, then re-attaches Luna on top.
  Update controls moved from Health to Updates. The automatic recovery hook and its
  systemd units are removed (`luna hook` is a stub).
- **Steam gradient**: Steam's background gets a gradient built from the Noctalia
  palette (`matugen.css`), updating live with the desktop theme. Settings →
  Steam gradient turns it off.
- **Pinned-manifest Steam rollback** (ideas from
  [SteamRollbackrLite](https://github.com/foodiemonster/SteamRollbackrLite)):
  `luna steam lock` serves the sha256-pinned ubuntu12 manifest from a local
  server (Valve deleted it upstream on 2026-11-12), falling back with a WARN;
  `luna steam history`; `luna steam libcrypto [--install|--remove]` wired into
  lock/bootstrap/heal/status/doctor; Flatpak and SteamOS Gaming Mode warnings.
- **`luna bootstrap` installs everything** on a fresh PC: per-distro system
  packages from one table (native Steam, 32-bit libcrypto, mangohud, gamescope,
  gamemode incl. 32-bit parts, curl/jq/tar/unzip/git/python3/zenity), sudo only
  interactively, `--dry-run` prints exact commands, opt-outs `--no-system-packages`
  / `--no-mangohud` / `--no-gamescope` / `--no-gamemode`; then known-good
  ubuntu12 build 1792618285 (Oct 22 2026) is verified by bootstrap, update-all
  and doctor (WARN, never a failure, if it differs); IgnorePkg and a final doctor run.
- **Hard lock is the new default.** `luna install` / `install.sh` roll Steam back
  to ubuntu12 and hard-lock it; build `1791918634` is opt-in via `--steam-build`.
  `--keep-steam-client` is unchanged. `refresh` refuses under the lock unless
  `--unlock-first`; the timer defers a pending refresh while locked.
- **`luna steam heal`** restores `steam.cfg`/`Steam.cfg` and read-only `package/`
  after third-party installers (LuaTools installer/Auto-fix) remove them; run by
  the 30-minute timer, preflight and install. Logs to `steam-heal.log`; never re-pins.
- **`luna steam launchopts [--fix]`** finds launch options pointing at missing
  paths (also WARNed in status/preflight); `--fix` backs up `localconfig.vdf`.
- **`luna steam ignore-pkg [--apply]`** prints/applies pacman `IgnorePkg = steam`.
- **Lumen sidecar** is stopped once Steam is closed (lock, unlock, recover, refresh).
- **`luna bootstrap`, `luna update-all`, `luna doctor`**; `install.sh` now calls
  `bootstrap` (pinned base installer constants live only in `luna.py`).
- Known gaps: no Health-page launch-option warning; no systemd guard hook.
- **`luna steam lock` / `luna steam unlock`.** `lock` rolls Steam back to the
  old ubuntu12 client (kills Steam: needs `--force` or a y/N answer), pins the
  result, writes the hard update block to both `Steam.cfg` and `steam.cfg`,
  makes `package/` read-only and resets Moon's `boot_fail_count`. Recover,
  cache and install keep working under the lock (they unlock `package/` only
  while writing); `update` and `unpin` refuse until `unlock`. `unlock [--now]`
  undoes it all and can launch Steam so it self-updates. `status` shows the lock.
- **Steam's UI no longer stalls on Luna's network calls.** Lumen runs every
  add-on RPC on its single injector thread. HowLongToBeat lookups now use
  Lumen's non-blocking HTTP (the panel polls until the answer is in, with the
  blocking path kept for older Lumen builds), and the Health page's update
  check runs detached and is followed until its result arrives. Other updater
  calls from the menu are capped with a timeout.
- **The library HLTB sort stopped re-rendering in a loop.** Shelves sorted by
  HowLongToBeat re-render only when their times or direction change; before,
  each forced re-render triggered the next pass about every 800 ms.
- **Hardening.** Game names, paths and messages are escaped before they reach
  the menu's markup; `gh` calls have timeouts (a hung one no longer wedges the
  update timer); state files are written atomically; a failed update check
  keeps the last known release.
- **Tooling.** `luna vendor --only NAME` now re-pins just that add-on (it used
  to rewrite every pin) and refuses first-party bytes that are not committed;
  `verify` and `release` name pins that point at no commit.

## 0.14.18 - 2026-10-23

- **SteamOS Mode shortcuts take over the session.** Launching one while Steam
  runs ends the stale session first (`pkill -x steam`, web helper and Lumen
  sidecar) instead of refusing, then starts the new one. A session that will
  not exit still refuses rather than launching broken. Save and close games
  first: game processes are never targeted, but killing Steam can orphan them.

## 0.14.17 - 2026-10-23

- **SteamOS Mode.** The console launchers are now `SteamOS (1080p 60)`,
  `SteamOS (4K 60)` and so on: one shortcut per detected screen, created
  automatically and any time from **Settings → SteamOS Mode → Create
  shortcuts**. Recognized legacy launchers are adopted and renamed (backed
  up first, restored on uninstall); hand-edited entries are never touched.

## 0.14.16 - 2026-10-23

- **One-command Steam+Luna refresh.** `luna refresh` (and a Health button)
  updates Steam to Valve's latest, pins and caches the new build, reinstalls
  the Luna add-ons keeping that client, and verifies - failing closed at each
  step with the exact next action. A queued refresh is applied by the user
  timer while Steam is closed, even with unattended updates off. Nothing is
  ever killed; games, accounts and saves are untouched. The updater's
  post-update Steam relaunch is shut back down gracefully, and a stuck
  relaunch is reported instead of silently continuing.
- **Bridge repair after base updates.** A Lumen/Moon update replaces
  `boot.lua` and wipes the bridge while payloads stay current; installs used
  to report "nothing to do" and leave Luna missing from the menu. Installing
  now reinstalls the bridge in that case, and Health offers one-click
  **Repair bridge** (Steam must be closed) with the exact shell equivalent.
- **Honest Steam build detection.** `Client version: 0` reports (what newer
  clients log at startup) no longer masquerade as a build; the installed
  package manifest wins over stale log lines, with the verified executable
  receipt still first. `steam cache` accepts an explicit `--build` and
  refuses to label a cache its manifest does not describe.
- **Bounded rollback backups.** Client install/recovery keeps only the newest
  five `steam-client-backups` entries; anything else in that directory,
  including operator files, is left alone.
- **Health in Spanish.** Settings → Language switches the attention list,
  section headers and action buttons to Spanish; paths and bug-report dumps
  stay in English in both languages.
- Automatically generate Steam Machine application shortcuts during installation
  and updates: portable 1080p60 plus detected local display profiles, with a
  dedicated verified launcher included in bundles and local tooling.
- Use Gamescope's SDL backend and Niri relative-mouse capture for in-game mouse
  compatibility, with a separate free-pointer action. Keep Steam/game clients
  inside Gamescope's Xwayland instead of leaking desktop Wayland-only settings.
- Replace recognized legacy launchers only on explicit migration, preserving
  names/render profiles and backing up originals. Preserve custom edits; restore
  migrated originals on uninstall. Remove forced `killall -9` startup behaviour
  in favour of safe running-client refusals and a single-session lock.

## 0.14.15 - 2026-10-23

- **HLTB sorting is view-local, not a shared app-store rewrite.** Sort home
  shelves and collection grids directly by numeric completion times. Different
  shelves/dropdowns cannot undo one another; no global sort healing, five-minute
  activity timeout or writable `sort_as` field is needed. Cache and direction
  changes refresh both layouts without remounting or mutating Steam app objects.
- Remove the global DOM label override that could display "HowLongToBeat" on
  an actually alphabetical view. Each dropdown now gets its label from its own
  selection and options, not another shelf's last reported state.
- Regression tests cover simultaneous independent views, late/changed/missing
  times, both directions, immutable shared objects, functional-grid lifecycle,
  sorter restoration on render failure and misleading stale global preferences.

## 0.14.14 - 2026-10-22

- **HowLongToBeat sorting refreshes automatically.** Home-library shelves now
  redraw when cached completion times arrive after the initial render, when
  times change, or when the sort direction changes. Shortest-first remains the
  default, longest-first is respected, and games without times stay last.
  No developer-console refresh or manual live fix is needed. Only HLTB shelves
  are refreshed; scroll state and upstream component cleanup are preserved.

## 0.14.13 - 2026-10-22

- **Presence keeps the real game first in Steam Machine mode.** After starting
  its windowless activity helper, Presence restores Steam's selection to the
  actual game. Both remain running: the game is first, the helper second.
  Restoration is bounded and does not close Steam/QAM menus or override another
  selected game. Unsupported Gamescope clients without selection controls pause
  only the helper. The user's Presence preference and playtime are preserved.

- **Automatic real Steam downgrade on installation.** The one-shot installer
  and `luna install` default to build `1791918634`: SHA256-pinned historical
  manifest, verified Valve packages, safe ZIP/VZip extraction, executable and
  payload verification, retained rollback backups, a verified local cache, pin,
  and Steam self-update block. No account, game or save files are imported.
- **Recovery now restores executables**, not merely `package/` metadata. Core
  executable receipts distinguish the installed build from stale logs/manifests;
  a missing/incomplete archive cache can no longer claim a successful recovery.
- Preserve custom `steam.cfg` settings, refuse running Steam/Lumen even on
  internal recovery/update paths, and keep dry runs download/write-free.
  `--keep-steam-client` opts out; add-on bundle updates preserve explicit Steam
  version choices instead of silently repeating the compatibility downgrade.

## 0.14.12 - 2026-10-22

- Detect the Steam client build from package metadata when runtime logs are
  unavailable. Version 0.14.13 adds executable verification so metadata alone
  cannot be mistaken for a completed downgrade or recovery.

## 0.14.11 - 2026-10-22

- **Steam updates only from the Health menu.** The bootstrapper stays blocked
  everywhere else; the Health page is the sanctioned path: Pin current build
  (immediate), Update Steam client or Cache client (queued, applied by the
  timer with Steam closed, block re-applied after). `luna steam` offers the
  same actions plus `--now` from the shell. Unattended runs report Steam drift
  and apply queued Steam work before checking Luna updates.

## 0.14.10 - 2026-10-22

- **Steam update lockdown.** Pinning now also writes the bootstrapper block, so
  the pinned build cannot self-update; `unpin` removes exactly that block.
  `luna install` re-applies the block when a pin is active (base installers
  delete `steam.cfg`), and pacman-managed Steam without `IgnorePkg = steam`
  is reported by `preflight`, the Health page and `luna steam status` with the
  exact conf line to add.
- **Health you can read.** The Health page leads with a Needs attention list:
  every item says what is wrong and the exact command to run; a Steam client
  card shows build, pin and cache state next to the rest.

## 0.14.9 - 2026-10-22

- **Steam client version management.** A Steam update can move the interfaces
  SLSsteam hooks, breaking installs and launches until the integration catches
  up. Luna now pins the working build (`luna steam pin`), caches its packages
  locally (`luna steam cache`, Steam closed), and puts it back after an
  unwanted update (`luna steam recover`). Drift is reported by `preflight`,
  the Health page and every unattended run; Install mode recovers
  automatically from the cache while Steam is closed. `preflight` also warns
  when SLSsteam itself changed since the pin.

## 0.14.8 - 2026-10-21

- **Update reliability.** Stale update locks expire instead of wedging every
  future update; releases refuse dirty trees and verify their own manifest.
- **Backup and restore.** `luna backup` snapshots playtime, presence, HLTB,
  prefs, the artwork key and compressor settings with a hash manifest;
  `luna restore` validates, refuses while Steam runs, and never overwrites a
  newer file without `--force`.
- **Artwork undo history.** Every replaced file is stashed (keep 5, op-logged);
  failed writes roll back; Undo steps back losslessly and Steam artwork resets
  the slot to Steam's own art. Legacy single-level backups are imported.
- **Secrets.** The SteamGridDB key is never echoed to the panel (masked tail
  only); the field is write-only. Key file stays owner-only.
- **Two-machine awareness.** Synced heartbeats show the other machine in
  `preflight`; it warns against Steam on both at once and points at
  `luna reconcile` while localconfig conflicts wait.

## 0.14.7 - 2026-10-21

- **Two machines, one library.** Steam's synced `localconfig.vdf` no longer
  loses recents to last-writer-wins: `luna reconcile` merges Syncthing
  conflict copies (newest `LastPlayed` per game, union of the recent-played
  list and Deck seen-state) and archives what it consumed. The Health page
  reports waiting conflicts so the menu tells you when to run it. This keeps
  full-file sync: same hours, recents, collections and saves on both machines
  even with different games installed.

## 0.14.6 - 2026-10-21

- **Fix: Steam no longer reopens the artwork picker on launch.** Right-click
  requests are now one-shot (claimed on first read) and expire after five
  minutes, so a stale Fortnite request cannot replay after a restart.
- **Updater integrity.** `release` refuses a dirty tree, `update` verifies the
  bundle against its own manifest before running its installer, remote
  downloads require the checksum sidecar, and concurrent updates are locked.
- **Update modes corrected.** Notify reports while Steam runs, queued updates
  ensure the timer worker exists, the menu never auto-checks while Off, and a
  full uninstall disables the timer and clears the queue.
- **Health and status.** `status` degrades on a missing installed file, and the
  Health page flags half-installed add-ons by expected file, not just version.
- **Compressor cancel portability.** Cancel uses `/bin/kill` and verifies
  delivery (the old dash builtin form failed on Ubuntu CI).
- **Artwork safety.** The SteamGridDB key file is owner-only (0600) and the
  picker escapes game names instead of injecting them as HTML.

## 0.14.5 - 2026-10-15

- **Fix: the unattended-update timer could not set itself up** when it ran from
  the standalone updater copy (it tried to copy `luna.py` from a repository that
  is not there). It now copies *itself* when needed and skips a self-copy, with
  a regression test that drives the copy directly.

## 0.14.4 - 2026-10-15

- **Updates inside the menu.** The Health page shows the installed version
  against the newest release (checked through your own `gh` auth, cached six
  hours) with *Check now*, *Install update* (queued, because applying needs
  Steam closed) and an Off / Notify / Install switch. The Health tab carries a
  dot while an update is available or queued.
- **Unattended updates (opt-in).** Choosing Notify or Install enables a systemd
  user timer (`luna-autoupdate.timer`, every 30 minutes) that runs a standalone
  updater copy at `~/.local/share/Lumen/sls-luna/luna.py`: it checks, and in
  Install mode applies a newer release whenever Steam is closed. New commands:
  `luna check`, `luna autoupdate`, `luna timer on|off|status`. Every install
  refreshes the updater copy and the install snapshot.

## 0.14.3 - 2026-10-15

- **Fix: the cached-times order now actually holds.** 0.14.2 stopped the blind
  restore but only trusted the sort dropdown, and a section's dropdown is only
  rendered while its header is hovered. The sync now also trusts the grid
  itself (a grid rendering as cached-times-sorted is the reliable signal) and
  records the last decision in `slsLunaSortDiag` for the Health page.

## 0.14.2 - 2026-10-15

- **Fix: the HowLongToBeat order no longer held.** The same client update that
  removed `SP_REACT` also stopped persisting `AppGridDisplaySettings` in
  localStorage, and the periodic sort sync read "nothing stored" as "not Luna"
  and restored Steam's own sort keys - so a view could show the cached hours
  while sorting alphabetically. With no stored value the sync now follows the
  live sort dropdown (and otherwise leaves the keys untouched); an explicit
  change still restores them.

## 0.14.1 - 2026-10-15

- **Fix: the HowLongToBeat library sort disappeared.** A Steam client update
  stopped exposing `window.SP_REACT` in SharedJSContext, and the agent required
  it before installing its jsx hooks, so the sort option (and the right-click
  Luna actions, and Playtime's editors) silently stopped appearing. React is now
  resolved from the same webpack module graph when the global is absent, and
  both agents have a regression test for the missing global. Vendored Playtime
  **0.6.1** carries the same fix.

## 0.14.0 - 2026-10-15

- **Settings moved to the backend.** The beta switch, the HLTB order and the
  store filter live in `~/.local/share/slssteam-integrations/luna.json`; the
  shell mirrors them into localStorage for the agent. A CEF cache reset can no
  longer lose them, and both machines follow the same choice. Existing local
  choices migrate themselves on the next menu open.
- **Health page.** A new Luna tab with local diagnostics: add-on versions
  against the pinned bundle (including drift), `boot.lua` marker/backup state,
  Lumen and Steam paths, the current settings and data-file sizes, plus a
  *Copy diagnostics* button.
- **Bundle releases and updates.** `luna release` builds a verified bundle
  tarball with a sha256 sidecar (and can publish it as a GitHub release);
  `luna update --bundle/--url/--latest` verifies and installs it with the
  bundled installer. Other machines no longer need a repo checkout.
- **Install snapshot.** Every install writes `sls-addons/luna-manifest.json`
  (versions, pinned commits, timestamp) so the Health page can compare what is
  installed with what was pinned.
- **One test entry point.** `scripts/test-all.sh` runs every suite the CI
  workflow runs; the workflow now calls it.

## Playtime 0.6.0 (vendored) - 2026-10-15

- For games Steam itself tracks, the lifetime total shown in the panel and in
  the game-details page is Steam's own `localconfig.vdf` playtime plus any
  manual correction, so Luna and Steam agree (Cronos: 9.2 h, not 3.5 h).
  Shortcuts keep their locally tracked totals. Pre-model ledgers retire their
  old adjustments once, keeping the bytes as `<ledger>.pre-official`.
