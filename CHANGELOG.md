# Changelog

Notable changes to the Luna bundle. Versions follow the `luna` add-on payload
(the bundle version); the other add-ons keep their own versions.

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
