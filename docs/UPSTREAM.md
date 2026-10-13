# Upstreaming the add-on hook into Lumen (plan, not started)

Luna exists because Lumen has nowhere to put an add-on. This document records the
shape we would propose upstream, what must not change, and how to migrate without
breaking an installed machine. **Nothing here is proposed yet** - the patch stays
so that today's installs keep working.

## What the patch does today

Four edits to `lua/boot.lua`, each verified to match an upstream anchor exactly
once (see `scripts/patch-lumen.py`):

| # | Where | What it does |
| --- | --- | --- |
| 1 | file scope, after `local lua_dir = ...` | `dofile`s `sls-addons/bridge.lua` and calls `register(registry)` |
| 2 | `build_menu_assets()` | wraps the returned table so `ui_js` is appended |
| 3 | `build_webview_assets()` | same for the store/community bundle |
| 4 | the `SharedJSContext` channel | appends `control_js` and enables `polyfill` when the bridge loaded |

The patch is textually fragile by nature: it depends on four exact strings and on
the insertion point being in file scope so the helper locals are visible to the
builders. It fails closed (the patcher refuses and writes nothing), but a Lumen
release that renames those functions breaks installation until the anchors are
re-derived.

## What to propose instead

A documented capability, owned by Lumen, that behaves the same way:

```
-- boot.lua (upstream, proposed)
local addons = require("addons")
addons.load("sls-addons")        -- or: addons.discover(LUMEN_ADDONS_DIR)
-- ...
build_menu_assets():   return addons.decorate_assets({ polyfill = true, css = {}, js = js })
build_webview_assets():return addons.decorate_assets(base)
SharedJSContext channel: assets = addons.control_assets({ polyfill = ..., css = {}, js = {...} })
```

The contract each add-on directory satisfies - already implemented by the bridge,
so the add-ons themselves would not change:

* `backend/main.lua` returns a table with `register(registry)`;
* `register` may add functions to the dispatch registry (namespaced per add-on);
* `public/panel.js` is injected into the shell and the store/community views;
* `public/agent.js` is injected into the `control = true` channel, i.e. the only
  context that has `SteamClient`;
* the `callServerMethod` polyfill is enabled on the control channel **only** when
  at least one add-on loaded, so a machine with no add-ons behaves exactly as
  upstream does today;
* a missing, empty or malformed directory is skipped, never fatal - already the
  bridge's behaviour and covered by `tests/bridge-smoke.lua`.

## Why Lumen would want it

* The four anchors are re-derived on every Lumen release; an upstream hook ends
  that maintenance and the associated "install silently does nothing" failure.
* Add-ons are isolated from Lumen's internals: they see the registry and the
  injection channels, not `boot.lua`'s structure.
* It keeps the failure mode honest: a broken add-on degrades to "that feature is
  missing", not to "Lumen did not start".

## Migration, if it lands

1. Add-on payloads are unchanged; no code in `playtime` or `presence` needs to
   move.
2. Luna detects the hook (`require("addons")` succeeding, or a version check) and
   stops patching. Both paths stay supported for one release.
3. `uninstall` restores `boot.lua` from backup only if the file is still patched,
   so an upgraded Lumen that already ships the hook is not rewritten.
4. The patcher stays in the repository as the compatibility path for older Lumen
   builds, with its anchor check intact.

## What must not change

* **No manifest execution.** Add-ons are Lua files under a directory Lumen owns;
  the store's `stplug-in/*.lua` files are never read or executed by this stack.
* **RPC names stay namespaced** (`SlsPlaytime*`, `SlsPresence*`). Upstreaming a
  hook must not force add-on authors to squat on generic names in the shared
  registry.
* **The polyfill stays per-channel and conditional.** Enabling
  `callServerMethod` in a context that has no backend, or in every context by
  default, widens what injected content can reach.
* **User data stays outside the installation.** Anything under `sls-addons/` must
  remain replaceable by a reinstall.
