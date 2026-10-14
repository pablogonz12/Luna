# Luna feature roadmap

This document records product goals and a plausible implementation path. **The
features below are planned, not currently available**, unless the status explicitly
says otherwise. “Integrated in Luna” means Luna is the Steam-facing entry point
and coordinates a feature; it does not imply that Luna owns, bundles, or replaces
the upstream runtime or tool.

## Goals

| Goal | Intended Luna experience | Status |
| --- | --- | --- |
| Game asset picker | Find artwork for a Steam game and choose/apply a selected asset from a right-click action in the library | Available behind the beta switch (add-on `sgdb` 0.2.0) |
| Frame generation | Discover and manage supported `lsfg-vk` setup and per-game enablement from Luna | Read-only detection shipped (Graphics page); installer and per-game work still gated on upstream and platform compatibility |
| OptiScaler / OptiClient | Manage supported OptiScaler installation and per-game configuration through a Luna-owned page and game context actions | **Port started**: read-only detection and the client's managed-game list shipped; deployment and configuration planned (see `docs/optiscaler-client-port.md`) |
| Store quality of life | Keep the store focused: hide games you already follow from its browsing surfaces, adjustable in Settings | Available (Settings switch); more store/library quality-of-life planned |
| Compressor savings view | Show how much the Btrfs compressor has actually saved, per game, with charts | Available: Compressor → Stats reads the tool's own ledgers, plus a Measure now action |
| HowLongToBeat (HLTB) | Show completion-time estimates for matched games in Luna and, where feasible, the Steam game context | Search-link action shipped in add-on `luna` 0.2.0; matched in-panel estimates still planned until a permitted data source is confirmed |
| Rust-based runtime alternative | Explore whether Luna should eventually own a Rust runtime that can host Luna features without requiring Lumen | Feasibility study only; no replacement runtime exists |
| One integrated menu | Bring these and existing Luna features into the Luna navigation, with game-specific actions available by right-click | Long-term goal; upstream Luna dependencies and menu contracts still constrain this |

## Product and safety principles

- **Luna owns the experience, not every engine.** Build Luna's pages and actions;
  delegate to upstream tools or documented interfaces where appropriate. Do not
  copy an upstream UI or claim upstream projects as Luna code.
- **Right-click is contextual.** Add actions only when a game can be identified
  reliably. Show the resolved title/app ID and a preview before changing files or
  downloading an asset. Keep Luna's main settings and library management in its
  existing navigation rather than adding another top-level button.
- **Preview, backup, rollback.** Game-file operations must be opt-in, scoped to the
  selected game's discovered install/compatdata paths, backed up before mutation,
  recorded in a Luna operation log, and reversible. Never silently overwrite a
  user's custom artwork, game files, launch options, or existing OptiScaler setup.
- **No universal compatibility claims.** `lsfg-vk` and OptiScaler behavior depends
  on GPU, driver, graphics API, Proton/runtime, game, and upstream version. Detect
  and explain compatibility limits; do not promise frame generation will work for
  every game or hardware configuration.
- **Least privilege and opt-in installs.** Prefer user-local installs and
  per-game deployment. If an upstream installer needs elevation or changes system
  state, explain that before handing off; do not hide privileged steps inside Luna.
- **Privacy and attribution.** Explain network requests, API keys, cache contents,
  and upstream ownership. Do not upload library data unless the user has explicitly
  chosen a service that requires it.

## 1. Game artwork picker (Steam ROM Manager-like outcome)

The desired feature is an **asset picker for already-known Steam games**, not a
reimplementation of Steam ROM Manager's bulk ROM parsing/import workflow. Steam
ROM Manager is a separate bulk game importer/artwork manager; Luna should only
integrate or delegate to it if a concrete supported interface is established.

**Implemented (add-on `sgdb` 0.2.0, behind the beta switch):** the library
right-click menu carries **Change artwork** (the agent writes a request to
localStorage; the panel opens the picker on that game) and the Luna menu has a
**SteamGridDB** page - both visible only when **Settings → Show beta features**
is on. The user
adds their own free SteamGridDB API key (stored locally in
`~/.local/share/slssteam-integrations/sgdb.json`), the page searches
SteamGridDB, matches the game, and lists **grid / wide / hero / logo / icon**
images (PNG or JPEG only - anything else cannot be displayed by Steam).
**Apply best to every slot** fills all five with SteamGridDB's top-ranked usable
image in one pass. Applying downloads the image into
`userdata/<account>/config/grid/` with Steam's own file naming, **backs up**
whatever file was there, and **Restore** puts it back or removes the custom
file. Nothing is written before an image is picked.

**Live refresh:** after applying, the panel asks the agent to push the same
bytes through Steam's own `SetCustomArtworkForApp`, so the library shows the
new art without a restart. The asset types were mapped empirically against a
live client (0=grid, 1=hero, 2=logo, 3=wide); **icons have no live type** and
appear after a restart. The file itself is always written, so nothing depends
on the live call.

Still open: per-image style filters, an NSFW toggle, and portrait-grid
thumbnails in the picker.

### Proposed flow

1. Add a game right-click action such as **Luna → Change artwork**.
2. Resolve the selected Steam app ID or non-Steam shortcut identity. For ambiguous
   shortcuts, ask the user to choose/search rather than guessing.
3. Search a permitted artwork provider (SteamGridDB is a candidate), showing the
   asset type, dimensions, attribution/source, and a preview.
4. Apply only the selected artwork type. Preserve the current file or Steam state
   and offer **Restore previous artwork**. Do not change game manifests or launch
   options as a side effect.

### Design checks before implementation

- Confirm the provider API's current terms, rate limits, attribution requirements,
  and API-key handling. Make a user key optional only if the provider supports
  that model; never commit a key or send it to Luna-operated services.
- Map app IDs and non-Steam shortcut IDs to Steam's artwork storage carefully.
  Steam client updates and multiple library roots make hard-coded paths fragile.
- Prefer Steam-supported artwork/customization mechanisms where documented. If
  direct file writes are unavoidable, use atomic writes, backups, and a recovery
  test; do not overwrite artwork changed outside Luna without confirmation.
- Test Steam restart/reload behavior and artwork types (grid, hero, logo, and
  library/header art) on supported Steam clients.

## 2. `lsfg-vk` frame-generation setup

**Implemented (add-on `graphics` 0.1.0, behind the beta switch):** a read-only
**Graphics** page reports the distro and kernel, every DRM card with its vendor,
device, driver and product name, the `lsfg-vk` Vulkan layers found (with the
manifest's implementation version) and its user config, the overlapping Decky
plugins (`decky-lsfg-vk`, `Decky-Framegen`) without touching them, OptiScaler /
OptiScaler Client installs in the usual user locations, and the Steam root with
its Linux runtimes plus gamescope. It installs, downloads and changes nothing.

The goal is to make supported setup and per-game selection manageable in Luna,
including an installer flow when upstream permits it. It is **not** to bundle a
mutable installer or claim a universal frame-generation toggle. The project has
moved its canonical distribution/documentation away from its archived GitHub
repository; Luna must use the current upstream source and terms rather than pin
old GitHub instructions by assumption.

### Proposed stages

1. **Read-only discovery:** identify OS, Steam type, GPU/vendor, driver, Vulkan
   availability, existing layer installation, and current `lsfg-vk` version. Do
   not infer that the feature is compatible solely because a GPU is present.
2. **Upstream and licensing review:** confirm the canonical release channel,
   current license, supported installation method, checksums/signatures, update
   policy, and uninstall instructions. If no verifiable artifact/install contract
   exists, Luna can link to upstream instructions but must not auto-install it.
3. **Explicit install/update:** preview file and system changes, require consent,
   prefer user-local deployment, retain rollback data, and never run an upstream
   script fetched from a moving branch without pinning and verification.
4. **Per-game controls:** once a supported API/config contract exists, expose
   enable/disable and relevant settings on the game page/context menu. Preserve
   user launch options and existing environment variables; merge only through a
   tested, reversible mechanism.
5. **Compatibility validation:** maintain a tested matrix by GPU, driver, runtime,
   SteamOS/desktop distro, graphics API, and game. Mark unknown combinations as
   unverified, not supported.

## 3. OptiScaler and OptiClient

Keep the terms distinct: **OptiScaler** is the graphics middleware; **OptiScaler
Client** is a separate third-party management application and is explicitly not
the official OptiScaler project. Luna's goal is to offer a coherent management
experience, not silently redistribute either project's binaries or imply that
OptiClient is part of OptiScaler.

### Proposed integration

- **Implemented (read-only):** the Luna-owned **Graphics** beta page (add-on
  `graphics` 0.2.0) reports GPU/driver/kernel/distro, `lsfg-vk` layers and
  config, overlapping Decky plugins, OptiScaler/Client installs and the Steam
  runtime; it installs or changes nothing. Per-game configuration views come
  once identity mapping and upstream contracts allow it.
- **Port, step 1 (read-only):** the same page reads the OptiScaler Client's own
  database (`~/.config/OptiscalerClient/games.json`) and shows every game the
  client manages with the upscaler state it recorded, re-checking the install
  paths and looking for the deployed OptiScaler files, and resolving
  `appid_<id>` names through the Steam appmanifest. Luna never writes to the
  client's files. The deployment/configuration steps and their gates are scoped
  in `docs/optiscaler-client-port.md`.
- Evaluate whether OptiClient exposes a stable CLI, local API, or documented
  configuration format. If it does, Luna may launch/delegate through that public
  contract. If it does not, use a clearly separate launch/handoff rather than
  scraping its UI or editing undocumented state.
- For any direct OptiScaler deployment, inventory the target files and game
  graphics API/runtime, show exactly what will be added or replaced, make backups,
  and offer a verified removal/restore flow. Preserve existing mods and overlays.
- Represent settings such as upscaler and frame-generation mode only where the
  selected game, OptiScaler build, GPU, driver, and runtime support them. Some
  settings may only be changeable in-game or by OptiScaler itself; Luna should
  report that rather than pretend a global toggle exists.
- Keep OptiScaler releases and OptiClient releases separately versioned and
  attributed. Download only from their official release channels with integrity
  verification where available. Do not package components until license and
  redistribution terms are reviewed.

## 4. HowLongToBeat integration

> **FROZEN at luna 0.10.2 / sgdb 0.2.0 (2026-10-14).** The user confirmed the
> library sort, the capsule hour labels, the Luna page, the background pass and
> the artwork picker all work. From here on, do **not** change any HLTB
> behaviour unless a regression is reported. Any future change to the luna
> agent, panel or backend must keep these behaviours exactly as they are today:
> the SORT BY option (value 12) and the `sort_as` keys (including the forced
> re-sort after a key change), the live-sort healing and the truthful SORT BY
> label, the capsule hour labels and their diagnostics, the background pass and
> its skip/pause rules, and the SteamGridDB picker with auto-apply and live
> refresh.

The experience goal is to match a Steam game to HLTB estimates (for example,
main-story and completionist times) and display the result in Luna. Do not build
against an assumed official public API: this research did not confirm a supported
public HLTB API or permission for automated scraping.

**Implemented so far:** add-on `luna` owns HLTB inside the Luna menu and the
library. The **HowLongToBeat** page lists the owned library and non-Steam
shortcuts (reported by the SharedJSContext agent, locally only), shows cached
times, and fetches on demand - per game or as a **background pass** that
spaces requests, **skips games the site cannot match** (remembering them so
they stop counting as missing), pauses a minute to retry once when the site
rate-limits, **keeps running with the Luna window closed**, and resumes after a
Steam restart until it finishes or the user stops it (the intent is remembered
in localStorage; only Stop clears it). The right-click menu keeps only the
search link. The library's **SORT BY** dropdown gains a **HowLongToBeat** option: the agent
recognizes the dropdown by its option-value fingerprint (locale-independent)
and appends value 12. Steam's comparator factory exports are non-configurable
webpack getters, so instead of replacing it Luna relies on Steam's own fallback
for unknown values - alphabetical order - and rewrites each game's `sort_as`
field (a plain writable property) to a fixed-width time key while the option is
selected, restoring every original when another sort is chosen. The grid
memoizes its sorted order and `sort_as` is not observable, so the agent hands
the grid a fresh `appOverviews` array on every render: its memo has to
recompute, and the order follows the current keys (an earlier attempt to poke
Steam's own sort setter was removed - that dropdown exposes no usable
callback). The direction is a Luna page switch (shortest main story first by
default, longest first when off), stored in localStorage; the Storage event
makes the agent re-key immediately. Games without times always sort last, and
with no HLTB data or any failure the library behaves exactly as Steam's
alphabetical sort. Everything is stored in
`~/.local/share/slssteam-integrations/hltb.json`. Nothing is fetched
automatically.

### In-Steam surfacing (design, verified against the installed client bundle)

The **sort option and the capsule hours are implemented**; the game-page stat
cell was attempted and removed - a foreign DOM node inside a React-owned row
does not survive re-renders, so it is tracked as
[`issues/P2-hltb-game-page-stat.md`](issues/P2-hltb-game-page-stat.md) with
what was learned (the app-id bridge is reusable; the cell needs a React-level
hook).

The goal is that HLTB data appears where Steam shows its own stats, not only in a
Luna panel. Two integration points were located in the client's `steamui/sp.js`
(re-check identifiers after every Steam update; if discovery fails, Luna must do
nothing rather than guess):

1. **Game page stat cell - attempted, removed.** The app details page renders a
   row of stat cells (Last Played, Play Time, Achievements) using one cell
   component and a shared class group (`GameStat` / `GameStatRight` /
   `PlayBarLabel` / `PlaytimeRight`). A DOM insertion built from those classes
   looked right but does not survive React re-renders, so it was removed; see
   [`issues/P2-hltb-game-page-stat.md`](issues/P2-hltb-game-page-stat.md) for
   what was learned and the acceptance criteria for a React-level attempt.
2. **Library sort option.** The "SORT BY" dropdown is built from an enum
   (`eL=1 … og=9, wD=10, LU=11`) with labels from `#Library_SortBy*` tokens, and
   the real comparator comes from one exported factory (`gQ(sortValue) -> sort
   function`) in a library module; the choice is persisted as
   `AppGridDisplaySettings` in `localStorage`. Luna can append a
   `{data: 12, label: 'HowLongToBeat'}` option to the dropdown's `rgOptions` and
   wrap the exported `gQ` so value `12` sorts by cached HLTB hours (unknown
   games after known ones, stable order). Steam treats unknown values with its
   alphabetical fallback, so a failed/partial injection degrades to a normal
   library instead of breaking it. Boot-order and persistence behavior
   (restoring a stored `12` before Luna's patch lands) must be tested against a
   live client before release.

Both points consume the same local dataset described below; neither can work
without a permitted data source. Detection must be locale-independent (match the
numeric enum fingerprint, not translated labels), and all injected UI must be
clearly attributable to Luna.

Before implementation, choose a compliant data source and verify its current
terms, attribution requirements, rate limits, and redistribution rules. If the
only available route depends on undocumented endpoints or scraping that violates
service terms, do not ship it; consider a user-opened HLTB link instead.

If an allowed source is established:

- Match using Steam app ID/title and show the selected match; never silently attach
  a similarly named game's results.
- Cache results locally with an expiry and a way to refresh/clear them. Send the
  minimum query data required and disclose network access.
- Treat estimates as third-party data with source attribution, not as Luna's own
  measurements. Gracefully handle missing games, rate limits, and offline use.
- Add the read-only page first; only add a right-click shortcut after its game
  identity contract is shared with the artwork and graphics integrations.

## 5. Rust-based runtime alternative

It is technically possible to build a Rust runtime for Luna, but “rewrite Lumen in
Rust” would mean reimplementing and maintaining a substantial integration stack,
not translating Lua files line-for-line. Lumen currently supplies the Steam UI
injection/runtime and a Lua backend/RPC environment. Steam's library UI itself
runs in the client's web/CEF environment, so a Rust process alone cannot replace
the JavaScript that must execute there. A Rust design would still need a supported
way to connect that UI layer to a native service (or a carefully limited
JavaScript bootstrap).

### Main work and compatibility risks

- Recreate or replace Steam UI injection and lifecycle handling across Steam
  updates without destabilizing the client.
- Define a versioned RPC boundary between the Steam web UI and a Rust service,
  including process startup, authentication/permissions, reconnects, and error
  handling.
- Either preserve the existing Lua add-on contract with an embedded Lua runtime
  and compatibility tests, or explicitly port add-ons to a new API. A Rust
  backend does not automatically make existing Lua add-ons compatible.
- Rebuild the parts of Luna's current integration that depend on Lumen's
  `boot.lua`, dispatch registry, plugin loading, and data/lifecycle conventions.
- Test across Steam client updates, native and Flatpak Steam where applicable,
  and each supported distribution; provide safe upgrade, rollback, and coexistence
  with an existing Lumen install.

### Recommended decision path

1. **Do not copy Lumen code yet.** Its repository had no visible license when
   checked. Ask its maintainers about licensing, a supported extension/runtime
   API, and whether a Rust backend or upstream collaboration is welcome. Until
   rights are clear, use only independently designed behavior and documented,
   permitted interfaces.
2. **Specify the minimum contract first.** Inventory the calls Luna actually
   needs, define a language-neutral RPC schema and UI lifecycle requirements, and
   test the schema against the current integration. Avoid rewriting unrelated
   Lumen features merely for language uniformity.
3. **Prototype out of process.** Build a small Rust service that handles a
   non-invasive Luna feature (for example, read-only status) behind a mock/test UI
   client. Measure startup, crash recovery, and protocol behavior before touching
   Steam injection.
4. **Choose compatibility intentionally.** Prefer a narrow adapter for existing
   Lua add-ons during a transition if it is safe and maintainable; otherwise port
   Luna-owned add-ons one at a time and keep the old route available for rollback.
5. **Gate any runtime replacement.** Require a stable Steam UI bridge, security
   review, backend/add-on tests, Steam-update resilience, documented install and
   uninstall, and hands-on platform validation. Do not make it the default or
   remove the Lumen dependency until these gates pass.

The likely near-term value is a **Luna-owned Rust backend/service**, not a full
replacement for Lumen's Steam injection and UI runtime. Treat even that narrower
service as exploratory until the integration contract and licensing questions are
resolved.

## Delivery sequence and gates

1. **Foundation — landed for the first action:** the shared game right-click
   registry and the first consumer (HowLongToBeat search) ship in add-on `luna`
   0.2.0, with tests. The identity contract is Steam's own menu/overview data
   (Steam app ID vs non-Steam shortcut) plus the jsx hook proven by Playtime.
   Remaining foundation work: the UI/backend permission boundary for features
   that mutate game files.
2. **Low-risk first feature:** prototype artwork search/preview with a mock provider
   and tests; resolve provider terms and reversible Steam artwork updates before
   enabling writes.
3. **HLTB research:** determine whether a permitted source exists. The
   user-triggered fetch, local store and no-network link fallback are
   implemented; nothing is fetched automatically.
4. **Graphics discovery:** implement read-only detection for `lsfg-vk`, OptiScaler,
   GPU/driver/runtime facts, and unknown/unsupported states.
5. **Optional installers:** separately validate install, update, rollback, and
   uninstall for each upstream tool on each supported platform. No default-on
   installs; do not bundle without licensing approval.
6. **Per-game controls:** only after stable upstream contracts and compatibility
   tests; add explicit confirmation and restore paths for every mutation.
7. **Runtime feasibility:** investigate licensing and define the RPC/UI contract;
   keep any Rust prototype separate from the supported installer until it passes
   the runtime replacement gates above.
8. **Release gate:** document supported combinations and known failures, pass
   automated tests and hands-on Steam tests, then update the README's “available”
   table. Until then, these remain goals.

## Research references

Project details and terms can change; re-check official sources before shipping:

- [Steam ROM Manager](https://github.com/SteamGridDB/steam-rom-manager) — bulk
  game importer/artwork manager; not assumed to expose a Luna asset-picker API.
- [SteamGridDB](https://www.steamgriddb.com/) — candidate artwork source; API
  terms and key requirements need review before use.
- [`lsfg-vk` archived GitHub repository](https://github.com/PancakeTAS/lsfg-vk) —
  points to its current canonical site/repository; do not use archived install
  instructions without revalidation.
- [OptiScaler](https://github.com/optiscaler/OptiScaler) — graphics middleware.
- [OptiScaler Client](https://github.com/Optiscaler-Client/Optiscaler-Client) —
  independent third-party management client, not an official OptiScaler project.
- [HowLongToBeat](https://howlongtobeat.com/) — no supported public API was
  confirmed in the initial research for this roadmap.
