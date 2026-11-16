# Changelog

Notable changes to the Luna bundle. Versions follow the `luna` add-on payload
(the bundle version); the other add-ons keep their own versions.

## Unreleased

- **`luna fleet gui [HOST...] [--json] [--verify]`**: one table of every machine's Steam client state
  (agent-desktop, running, accessible mode, screen, blocker windows, backend) from the remote `luna gui status --json`.
  Read-only; never starts/stops Steam. `--verify` (opt-in) also runs the new `luna gui verify --json`
  (exit codes 0/1/5 unchanged) where Steam is already accessible. Old remote Luna -> "luna too old" row.
  Exit 1 for unreachable/no or old Luna/failed verify; skips exit 0.
- **Signed releases.** `luna release` signs the bundle (OpenSSH ed25519, `<bundle>.sig`) when a key exists;
  `luna update` verifies it against a local trust file before unpacking. New `luna signing status|init|trust|untrust`,
  `luna update --allow-unsigned`, `luna fleet install --trust-signing-key`; fleet copies the `.sig`. Once a key is
  trusted, unsigned bundles are refused and invalid/untrusted signatures are always refused. See README, "Release signing".

- **Update/signing hardening (security review).** Signature enforcement is now remembered in
  `signing-policy.json` (two locations) so a missing trust file or another environment refuses instead of
  silently accepting unsigned bundles; `luna signing disable` is the deliberate off switch and untrusting the last
  key keeps enforcing. `luna update --dry-run` executes nothing from the bundle. `--url`: https only (loopback http
  allowed), https-only redirects, streamed download with size caps. Empty/malformed `.sha256`, non-object manifest or
  file list now refuse cleanly; `cmd_autoupdate` records any exception; fleet remote-script values are all validated
  (`fullmatch` host, remote temp dir, bundle/add-on names, int timeout, key blob); autoupdate requires a `vX.Y.Z` tag
  and a bundle of that version; the manifest check walks the bundle's own add-ons; the update lock is `flock`.
  **Rollback protection:** `update-state.json` `min_version`; `luna update --allow-downgrade` and
  `luna fleet install --allow-downgrade`. See README, "Release signing".

- `luna update` verifies every bundle file (incl. `scripts/luna.py`) against `RELEASE-FILES.json` before running it; legacy bundles need `--allow-legacy-bundle`.

## Unreleased

- `luna gui restart-check` also compares the HowLongToBeat-sorted grid (read-only, over CDP,
  Home shelf order) before and after the restart; skipped when that sort is not active.
- `luna gui` no longer reports Steam's idle popup-menu webareas as open windows.

## Unreleased

- **GE-Proton safety.** `luna proton verify [VERSION] [--rebuild|--against FILE]` and an
  integrity manifest written by `install`; hash-free corruption warnings in `proton status` and
  `luna doctor` (several GE versions, DLLs bulk-newer than the tool, dangling/foreign prefix
  links); `install`/`remove`/`prune` refuse while Steam or a wine process from the tool runs.
  **`proton prune` now keeps 1 by default (was 2)**; `install` warns when it leaves a second
  version. Background: docs/PROTON_SINGLE_VERSION.md.
- **`luna games list|diff|install`.** See which Steam games are installed on which
  machine (`luna probe --json` gains an additive `games` section; older remotes are
  scanned over ssh) and queue `steam://install` on another host after checking free space
  and the stplug-in lua for unlock-only games. No game files are copied.


- **`luna saves`: share saves that have no Steam Cloud.** `scan` finds installed games with
  Proton-prefix or `~/.config/unity3d` saves and no `remotecache.vdf`; `link APPID|PATH` moves the
  save directory into `~/.local/share/game-saves/` (synced by Syncthing) and leaves a symlink;
  `link --all` re-creates the links on the other machine (backs up local data, refuses with
  `--prefer local|store` when both sides have saves, never deletes); `unlink` copies it back;
  `status` and a `luna doctor` check report broken links and sync conflicts.

- **`luna proton`.** Manage GE-Proton per machine (`~/.local/share/Steam` is not synced):
  `status [--json]` (installed tools, latest release, games mapped to a missing tool; Steam
  config is only read), `install [VERSION|latest] [--dry-run]` (sha512-verified, extracted
  then atomically renamed, retry/backoff), `remove VERSION [--force]`, `prune [--keep N]`.
  `luna probe --json` lists the installed tools; `luna doctor` warns about a game mapped to a
  missing tool. Restart Steam after installing.
- **CLI source split into fragments.** The 4600-line `scripts/luna.py` is now generated
  from `scripts/luna_cli/*.py` by `scripts/build-luna.py`; the deployed artifact is still the
  single `luna.py`, with identical behaviour. A test fails if it is stale.
- **Add-on installs no longer drag the Steam client along.** `luna install
  --only NAME` skips the Steam client unless it is actually needed (no pin, pin
  differs from the compatibility build, or packages/executables drifted); a full
  install still verifies it. New `--steam-client` forces it, `--no-steam-client`
  skips it (same as `--keep-steam-client`).
- **Robust, visible client download.** Retries with exponential backoff and
  jitter on HTTP 429/5xx and timeouts (honours `Retry-After`), prints
  "need N files, X MB to download, Y reused from cache" first, shows per-file
  progress (carriage-return on a TTY, sparse plain lines otherwise), and
  `--dry-run` reports the same summary (offline; "size unknown" until the
  manifest is cached). `--client-cache DIR` / `LUNA_STEAM_CACHE` names an extra
  local package directory checked (sha256) before any download.
- **`luna fleet`: see and manage Luna across machines over SSH.** `fleet
  add|remove|list HOST` keeps hosts in `~/.config/luna/fleet.json` (names are
  validated and only ever passed to ssh as argv); `fleet status [--json]` shows
  per machine the Luna version, `luna.py` hash, add-on payload match/drift, the
  Steam pin, package match, update block / hard lock and whether Steam runs
  (unreachable hosts and older remote Luna builds are reported, not fatal);
  `fleet push-cache HOST` rsyncs the verified Steam client cache only when the
  remote build differs; `fleet install [HOST...] [--only NAME]
  [--restart-steam] [--dry-run] [--force]` ships the release bundle through the
  remote `luna update --bundle`, refuses while Steam or Lumen runs there (with
  `--restart-steam`: `steam -shutdown`, wait, install, relaunch), and refuses
  drifted payloads without `--force`. New `luna probe --json` is the
  machine-readable state it reads (works from the installed copy).

- **`luna doctor`.** Aggregates the existing checks into one report (foreign
  Steam locks, payload/`luna.py`/launcher drift, `IgnorePkg`, updater errors,
  bridge and sidecar state, sync-conflict files) with a fix command per line,
  `--json` output and exit codes 0/1/2. `--fix` applies only safe, reversible
  fixes and never overwrites payloads, uses sudo or touches a foreign lock.
- **`luna steam lock --adopt` and doctor adoption.** Records a read-only
  `package/` + pin + update block left by a manual lock (or preserved by
  `luna install`) as Luna's hard lock, only when the packages match the pin;
  nothing is rolled back or killed (undo: `luna steam unlock`). `luna doctor`
  suggests it for `steam.foreign-lock` and `--fix` applies it (Steam closed).
  `luna fleet status` gains a DOCTOR column (warn/error counts, `?` for hosts
  whose Luna lacks `doctor --json`) and a note per host with findings.
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
