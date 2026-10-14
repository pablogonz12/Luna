# Luna installer roadmap

## Product promise

Luna should make a compatible Linux Steam setup easier to assemble and maintain.
It is an **orchestrator plus Luna features**, not a monolithic replacement for
every upstream project. Required base components and optional integrations must
be visible as separate choices, with ownership and update behavior explained.

## Today

- The installer reuses a complete LuaTools-Moon stack; if it is absent, it calls a
  pinned, checksum-verified upstream installer with auto-launch disabled.
- Luna installs its pinned Lumen add-ons and refuses partial or unsafe states.
- LuaTools' backend remains required by the current integration.
- CloudRedirect may be installed by the upstream base installer on a fresh setup,
  but Luna does not yet offer a consistent optional-component manager for existing
  installations.
- The Compressor add-on expects the separate `btrfs-game-compressor` executable;
  Luna does not install it.
- Decky Loader is not installed by Luna.

## Desired install experience

1. **Preflight first.** Identify distro, Steam installation type, Steam Deck vs
   desktop, permissions, current Moon/Lumen/LuaTools/Decky state, and running
   processes. Show a plan before any changes.
2. **Reuse, don't reinstall.** If required components are present, keep them and
   report versions. Offer explicit updates separately; never rerun a destructive
   upstream cleanup just to install Luna add-ons.
3. **Required base.** Install the supported slsteam-moon/Lumen/LuaTools base only
   when missing, with a pinned installer revision, checksum validation, and
   auto-launch disabled until Luna is installed.
4. **Luna features.** Install the Luna shell and selected Luna add-ons with pinned
   payload hashes. Explain required external executables before enabling pages that
   depend on them.
5. **Optional components.** Offer CloudRedirect, Decky Loader, and future tools as
   separate opt-in selections. Each needs a compatibility check, source/license
   review, version/checksum policy, explicit privilege consent, status reporting,
   and a tested removal path.
6. **Verify and report.** Validate installed files, show versions and skipped
   options, then tell the user how to launch Steam and how to undo Luna changes.

## Optional component gates

| Component | Potential value | Gate before Luna automates it |
| --- | --- | --- |
| CloudRedirect | Cloud-save support for games/configurations that need it | Confirm licensing and supported install/update/uninstall contract; make opt-in; avoid overwriting existing provider config or save data |
| Decky Loader | Decky plugin ecosystem, especially on Steam Deck | Official project describes itself as a Steam Deck plugin loader. Validate the official installer on SteamOS first; generic desktop Linux is not assumed supported. Require explicit consent for its privileged/system service changes and preserve its own uninstall/update flow |
| btrfs-game-compressor | Compress game data on Btrfs | Detect Btrfs and supported filesystem state; install only from its own release/source with a hash; keep data operations user-confirmed |
| `lsfg-vk` | Linux frame generation for compatible games/hardware | Confirm its current canonical source, license, verifiable release/install contract, platform matrix, rollback and uninstall before offering an installer; otherwise provide upstream documentation only |
| OptiScaler | Per-game graphics upscaling/frame-generation middleware | Review license and official release integrity; detect the target game/runtime and preserve existing mods; require explicit per-game install/restore consent |
| OptiScaler Client | Third-party management UI for OptiScaler | Treat as a separate project, not official OptiScaler; integrate only through a documented stable CLI/API/config contract and review its separate license |
| Steam artwork provider | Artwork search and selection for Steam games | Verify provider API terms, keys, attribution, and Steam artwork storage; make changes previewable and reversible |
| HLTB data source | Completion-time estimates | No supported public API was confirmed; do not scrape undocumented endpoints absent permission. A user-opened HLTB link is the fallback |

Luna must not silently execute an unpinned `curl | sh` from an optional component.
Prefer release artifacts with verified checksums or a documented upstream installer
revision. If upstream only offers a mutable installer, record that risk and require
an explicit opt-in rather than presenting it as a fully reproducible install.

### Decky-specific installer risk

The official [`decky-installer` release script](https://github.com/SteamDeckHomebrew/decky-installer/blob/main/cli/install_release.sh)
was inspected while writing this plan. At that revision it self-elevates with
`sudo`, removes and recreates `~/homebrew/services`, fetches the latest stable
loader without verifying an artifact hash, writes a system-wide
`plugin_loader.service`, and enables/starts it. Those are meaningful system and
existing-data changes. Luna must not run that script automatically as a hidden
substep; any future Decky option needs explicit consent, backup/restore behavior,
an immutable installer revision, and a tested Decky-owned uninstall path.

## Safety and maintenance requirements

- Default optional selections to **off**; show what each option changes and which
  upstream owns it.
- Never request root for Luna add-ons. If an upstream component requires
  elevation, hand off to its supported installer only after explicit consent.
- Keep Steam closed for edits to Lumen/Steam files, but do not kill Steam or a
  sidecar automatically. Fail closed and explain how to proceed.
- Persist a component inventory with origin, version, install path, and checksum
  where available. Do not treat a component as Luna-owned just because Luna
  installed it.
- Separate update and uninstall actions by component; don't remove a shared
  dependency while another installed feature needs it.
- Never overwrite cloud-provider credentials, save data, Decky plugins, or user
  settings without an explicit migration and backup plan.
- Test from clean installs and existing installations on each supported target;
  document unsupported configurations honestly.

## Rollout

1. **Now:** maintain the existing fail-closed installer and finish the README and
   component inventory. No automatic Decky installation yet.
2. **Next:** unify optional-component detection and selection for CloudRedirect and
   the Compressor executable; add tests for reuse, refusal, rollback, and removal.
3. **Then:** validate Decky Loader on SteamOS/Deck, document side effects and
   rollback, and only then decide whether to expose an opt-in installer checkbox.
4. **In parallel:** port Lumen menu capabilities into Luna page-by-page using
   supported APIs, after license/extension questions are resolved.
5. **Feature foundation:** define selected-game identity and a safe Steam context
   action contract; see [`FEATURE_ROADMAP.md`](FEATURE_ROADMAP.md) before starting
   artwork, graphics, or HLTB implementation.
6. **Release gate:** keep the repo private until component licenses, the project
   license, platform support, and public installer behavior are reviewed.
