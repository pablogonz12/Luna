# Architecture

## The layers

```
SLSsteam-Moon + LuaTools-Moon     the installed stack: store hook + manifest manager
        │
        └── Lumen                 sidecar: CDP injector, Lua RPC registry, Steam path,
        │                         shared JSON/fs shims. Ships lua/boot.lua as its entry point.
        │
        └── SLS Luna              THIS PROJECT: distribution and boot hook
        │                         - one reversible boot.lua patch
        │                         - one shared bridge at sls-addons/bridge.lua
        │                         - pinned payloads under sls-addons/<name>/
        │
        └── add-ons               features, one directory each and nothing else:
            playtime              local, editable playtime for non-Steam games
            presence              hidden placeholder shortcut for friend visibility
```

Luna deliberately owns no features. It answers three questions and nothing else:
*what* gets installed, *how* it is wired into Lumen, and *how* it is removed.

## The contract Lumen already offers

`lua/boot.lua` builds an asset list per injection channel and runs `loop.run`. A
channel can ask for the `callServerMethod` polyfill, and the "SharedJSContext"
channel is the `control = true` channel - the only context that has
`SteamClient`. Upstream LuaTools registers its RPC handlers in the same `registry`
table the frontend dispatches against.

That is enough to host add-ons without touching Lumen's own logic:

1. a `dofile` of `sls-addons/bridge.lua` in file scope, so the locals it defines
   are visible to the builders below it;
2. the bridge registers each add-on's backend into `registry`;
3. the bridge returns `ui_js` (shell and store views) and `control_js`
   (SharedJSContext), which the patch appends to the existing asset lists;
4. the control channel's `polyfill` flag is enabled only when the bridge loaded,
   which is what lets an add-on talk to its backend from SharedJSContext.

## Why one patch and not four

The patcher edits four places in `boot.lua` and refuses to run unless each anchor
appears exactly once. Every add-on that installed itself would need the same four
edits, in a different order, and the second installer would see an already-patched
file. One patch, owned by Luna, keeps the invariant checkable:
`SLS-ADDONS-INTEGRATION` appears exactly once, and the original is kept as
`boot.lua.sls-addons.original`.

Ordering also matters for correctness. `build_menu_assets()` and
`build_webview_assets()` are *called* by `loop.run(...)` after the bridge has run,
so the add-on scripts are appended before injection. The control channel's script
list is likewise built inline in the channel table.

## Files on disk

```
~/.local/share/Lumen/
  lua/boot.lua                         patched once, backup beside it
  sls-addons/
    bridge.lua                         shared loader (pinned)
    presence/{backend,public}/...      payload (pinned)
    playtime/{backend,public}/...      payload (pinned)
~/.local/share/slssteam-integrations/  USER DATA, never touched by install/uninstall
    presence.json
    playtime/<steamid64>.json(.bak)
```

An add-on directory contains exactly `backend/main.lua`, `public/panel.js`,
`public/agent.js` and `plugin.json`. The bridge treats a missing or unreadable
file as "this add-on is not installed" and keeps loading the others, which is
covered by `tests/bridge-smoke.lua`.

## Payload pinning, and why not submodules

`vendor/MANIFEST.json` records, per file, the source repository, the source
commit, the source path and the sha256 of the bytes that were extracted. The
payload is produced with `git archive <commit>` from each add-on checkout, so the
bytes are the repository's own.

Alternatives were considered:

* **Git submodules** keep provenance but need network and credentials at install
  time, and a private submodule can silently fail to initialise.
* **Cloning at install time** makes the install non-reproducible and adds a
  failure mode to a step that must be boring.
* **Fetching release archives** needs a release to exist for every change.

Vendoring with a manifest keeps the install offline and auditable, and costs one
command to refresh. The trade-off is drift: the payload can grow stale relative
to the add-on projects, which is why `verify --sources` compares against the local
checkouts and the drift issue in `docs/issues/` asks for an automated check.

## What Luna will not do

* It never edits SLSsteam configuration, manifests, games or Steam userdata.
* It never touches user data on install, uninstall or upgrade.
* It never kills Steam or a Lumen sidecar; it refuses and prints the command.
* It does not merge add-ons into one process or one registry entry - each keeps
  its own RPC namespace (`SlsPlaytime*`, `SlsPresence*`) so upstreaming one does
  not require upstreaming the other.
