# Luna product direction: a Steam feature bundle

## Goal

Build **Luna** as a curated installer and feature bundle for Linux Steam: make a
good set of compatible projects easy to install together, add Luna-maintained
features, and provide a coherent Steam-facing menu for those features.

The menu is one part of the product, not the whole product. Luna should orchestrate
the existing Moon/Lumen stack and offer explicit optional components such as
CloudRedirect and, if supported on the target device, Decky Loader. It should not
silently turn every dependency into bundled Luna-owned code.

SLS Luna is an independent project, not a fork of LuaTools-Moon, slsteam-moon, or
Lumen. The intention is to reuse supported backend APIs where possible, not copy
upstream UI or native-core code.

The current installer layers Luna on the established LuaTools-Moon stack. This
does **not** yet mean the LuaTools or Lumen UI has been replaced or its buttons
removed. Moon settings, updates, cloud saves, and fixes are target integrations;
they are not all available inside Luna yet.

## Component responsibilities

| Component | Responsibility | SLS Luna relationship |
| --- | --- | --- |
| [Luna](https://github.com/pgm1207/Luna) | Bundle installer, Luna menu, and Luna-authored features | Owns orchestration, original UI, and its add-ons |
| [Lumen](https://github.com/swwayps/lumen) | Runtime, Steam UI injection, existing menu and backend RPCs | Runtime dependency and potential API upstream; no code copied without license clarification |
| [slsteam-moon](https://github.com/swwayps/slsteam-moon) | Native Steam integration, based on SLSsteam | Pinned external dependency; not copied into Luna |
| [SLSsteam](https://github.com/AceSLS/SLSsteam) | Upstream project for slsteam-moon | Credit through the Moon lineage |
| [LuaTools-Moon](https://github.com/swwayps/luatools-moon) | Plugin and base installer for slsteam-moon | Current backend prerequisite; Luna calls its pinned installer when needed, but is not based on its plugin source |
| [CloudRedirect](https://github.com/Selectively11/CloudRedirect) | Optional cloud-save component | Separate component; review license before bundling |
| [Decky Loader](https://github.com/SteamDeckHomebrew/decky-loader) | Optional homebrew plugin loader | Separate Steam Deck-focused project; compatibility and safe unattended installation are unproven for Luna's broader Linux target |
| [btrfs-game-compressor](https://github.com/pgm1207/btrfs-game-compressor) | External Btrfs game-storage tool | Invoked by Luna's Compressor feature, not installed by this repository |

## Product principles

1. **One Luna entry point.** Extend the existing Luna navigation rather than add a
   second Luna button or another competing overlay.
2. **Original presentation.** Implement Luna's pages using Luna-owned UI code.
   Reuse documented/public Lumen RPC methods where appropriate; do not copy
   Lumen's menu implementation without a license that permits it.
3. **Keep backend ownership clear.** Operations that belong to Lumen or
   slsteam-moon should continue to use their supported backend contract. Luna is
   the UI and integration layer, not a second implementation of Moon's native
   Steam hooks.
4. **Preserve safety and user data.** Keep current confirmations, fail-closed
   checks, settings/data formats, and rollback behavior. Any migration must be
   tested and reversible.
5. **Optional means opt-in.** CloudRedirect, Decky, and other system-changing
   components must be clearly described, separately selected, and independently
   removable. Never install an optional service just because the user chose Luna.
6. **Pin and report dependencies.** Record tested component versions and
   checksums; show when an upstream installer selects its own release channel.
   Do not imply that an upstream component is authored or maintained by Luna.
7. **Use least privilege.** Detect the host platform before offering installs,
   ask before privilege escalation, refuse unsupported configurations, and preserve
   the user's existing setup and data.

## Work sequence

See [`INSTALL_PLAN.md`](INSTALL_PLAN.md) for the staged bundle-installer design,
optional-component safety gates, and rollout sequence.
See [`FEATURE_ROADMAP.md`](FEATURE_ROADMAP.md) for planned game artwork, graphics
management (`lsfg-vk`/OptiScaler), HLTB, right-click integrations, and the
Rust-runtime feasibility study. These are goals, not current features.

1. Stabilize the bundle installer: detect/reuse existing base components, present
   a clear component-selection summary, support dry-run/status/rollback, and report
   the exact versions installed.
2. Define platform support. Keep native Linux Steam as the base target; treat
   Steam Deck/SteamOS and generic desktop Linux as separate compatibility targets
   until tested.
3. Review optional components independently. CloudRedirect requires license,
   version, config/data, and uninstall review. Decky Loader's official project is
   Steam Deck-focused; test its supported install/uninstall flows before offering
   any automatic option on other Linux systems.
4. Map each current Lumen menu page to its backend RPC contract, arguments, data
   format, and side effects. Identify which calls are safe and supported for an
   add-on to use.
5. Ask Lumen's maintainers to clarify the source license and whether they support
   an add-on/menu-extension API. The repository did not show a `LICENSE` file when
   checked; public visibility alone is not permission to redistribute its code.
6. Add one Luna page at a time, starting with a read-only or low-risk page. Keep
   the existing Lumen menu available until feature parity and rollback are tested.
7. Request a supported way to disable or hide Lumen's built-in menu entry. Avoid
   fragile CSS/DOM hiding as a permanent solution; if no supported option exists,
   document that limitation and keep the Lumen entry until a safe path is agreed.
8. Add backend-contract and UI tests, then validate with the exact pinned Lumen
   and slsteam-moon versions before changing the default installation.
9. Establish a reliable selected-game identity and safe context-menu API before
   building artwork, HLTB, or graphics actions. Keep game-file mutations backed
   up, confirmed, scoped, and reversible.

## Credits and releases

Credit Lumen, slsteam-moon, SLSsteam, LuaTools-Moon, and optional integrations in
documentation according to what is actually used. Preserve each component's
license and notices. Do not describe SLS Luna as an official upstream release or
claim ownership of upstream functionality. See [`../THIRD_PARTY_NOTICES.md`](../THIRD_PARTY_NOTICES.md).
