# Luna feature roadmap

This document records product goals and a plausible implementation path. **The
features below are planned, not currently available**, unless the status explicitly
says otherwise. “Integrated in Luna” means Luna is the Steam-facing entry point
and coordinates a feature; it does not imply that Luna owns, bundles, or replaces
the upstream runtime or tool.

## Goals

| Goal | Intended Luna experience | Status |
| --- | --- | --- |
| Game asset picker | Find artwork for a Steam game and choose/apply a selected asset from a right-click action in the library | Planned; no picker or game-art editing is implemented |
| Frame generation | Discover and manage supported `lsfg-vk` setup and per-game enablement from Luna | Research/design; installer and compatibility work required |
| OptiScaler / OptiClient | Manage supported OptiScaler installation and per-game configuration through a Luna-owned page and game context actions | Planned; not an all-games or universal frame-generation promise |
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

- Start with a Luna-owned **Graphics** page that detects installed versions and
  shows upstream documentation/status. A Luna right-click action can open the
  selected game's configuration view once identity mapping is reliable.
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

The experience goal is to match a Steam game to HLTB estimates (for example,
main-story and completionist times) and display the result in Luna. Do not build
against an assumed official public API: this research did not confirm a supported
public HLTB API or permission for automated scraping.

**Implemented so far:** add-on `luna` 0.2.0 publishes the shared game right-click
registry and a **HowLongToBeat** action that opens the game's search page in the
user's browser through Steam's own URL handlers. The search happens in the
browser's session; Luna sends no library data and stores no results. In-panel
estimates remain unimplemented.

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
3. **HLTB research:** determine whether a permitted source exists. The no-network
   link fallback is implemented; data integration waits on that research.
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
