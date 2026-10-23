# Changelog

Notable changes to the Luna bundle. Versions follow the `luna` add-on payload
(the bundle version); the other add-ons keep their own versions.

## 0.14.14 - 2026-10-23

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
- HLTB home shelves invalidate only while sorted by Luna and only on key
  changes, without remounting shelves or losing scroll state.

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
