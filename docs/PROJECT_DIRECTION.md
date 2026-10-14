# Project direction: a custom SLS Luna menu

## Goal

Use the existing Luna menu as the one SLS Luna entry point. Add original Luna
pages for the useful capabilities currently exposed by Lumen's menu: slsteam-moon
configuration, Game Updates, Cloud Saves, and Fixes. Keep Luna's Playtime, Presence,
and Compressor features in that same navigation.

SLS Luna is an independent project, not a fork of LuaTools-Moon, slsteam-moon, or
Lumen. The intention is to reuse supported backend APIs where possible, not copy
upstream UI or native-core code.

The current installer layers Luna on the established LuaTools-Moon stack. This
does **not** yet mean the LuaTools UI has been replaced or its button removed;
those changes depend on completing and validating Luna's page integrations and
resolving the Lumen extension/licensing questions below.

## Component responsibilities

| Component | Responsibility | SLS Luna relationship |
| --- | --- | --- |
| [SLS Luna](https://github.com/pgm1207/Luna) | Custom menu, feature pages, and add-on installer | Owns Luna-authored UI and features |
| [Lumen](https://github.com/swwayps/lumen) | Runtime, Steam UI injection, existing menu and backend RPCs | Runtime dependency and potential API upstream; no code copied without license clarification |
| [slsteam-moon](https://github.com/swwayps/slsteam-moon) | Native Steam integration, based on SLSsteam | Pinned external dependency; not copied into Luna |
| [SLSsteam](https://github.com/AceSLS/SLSsteam) | Upstream project for slsteam-moon | Credit through the Moon lineage |
| [LuaTools-Moon](https://github.com/swwayps/luatools-moon) | Plugin and base installer for slsteam-moon | Current backend prerequisite; Luna calls its pinned installer when needed, but is not based on its plugin source |
| [CloudRedirect](https://github.com/Selectively11/CloudRedirect) | Optional cloud-save component | Separate component; review license before bundling |
| [btrfs-game-compressor](https://github.com/pgm1207/btrfs-game-compressor) | External Btrfs game-storage tool | Invoked by Luna's Compressor feature, not installed by this repository |

## Integration principles

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
5. **Pin compatible dependencies.** Record tested component versions and checksums;
   do not silently follow moving branches or install whichever binary is latest.

## Work sequence

1. Map each current Lumen menu page to its backend RPC contract, arguments, data
   format, and side effects. Identify which calls are safe and supported for an
   add-on to use.
2. Ask Lumen's maintainers to clarify the source license and whether they support
   an add-on/menu-extension API. The repository did not show a `LICENSE` file when
   checked; public visibility alone is not permission to redistribute its code.
3. Add one Luna page at a time, starting with a read-only or low-risk page. Keep
   the existing Lumen menu available until feature parity and rollback are tested.
4. Request a supported way to disable or hide Lumen's built-in menu entry. Avoid
   fragile CSS/DOM hiding as a permanent solution; if no supported option exists,
   document that limitation and keep the Lumen entry until a safe path is agreed.
5. Add backend-contract and UI tests, then validate with the exact pinned Lumen
   and slsteam-moon versions before changing the default installation.

## Credits and releases

Credit Lumen, slsteam-moon, SLSsteam, LuaTools-Moon, and optional integrations in
documentation according to what is actually used. Preserve each component's
license and notices. Do not describe SLS Luna as an official upstream release or
claim ownership of upstream functionality. See [`../THIRD_PARTY_NOTICES.md`](../THIRD_PARTY_NOTICES.md).
