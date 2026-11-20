# Architecture

## Current stack

The default install is standalone: Steam loads `luna-host` from
`~/.local/share/Luna`, with no Lumen dependency. The Lumen variant below is
opt-in (`luna install --lumen`) and kept for existing installs; see
[`LUMEN.md`](LUMEN.md).

```text
Steam client
└── slsteam-moon                 native Steam integration; fork of SLSsteam
    └── Lumen                    optional third-party sidecar host (--lumen)
        ├── LuaTools-Moon        plugin UI and Moon-oriented features
        └── SLS Luna             current add-on distribution and feature set
            ├── playtime         local playtime support for non-Steam games
            ├── presence         friend-visibility integration
            ├── compressor       UI/backend for the external Btrfs compressor
            └── luna             Luna shell and shared integration
```

For the standalone path, `luna-host` replaces Lumen at the same layer and loads
the add-ons directly from `~/.local/share/Luna/addons`.

The boundaries matter: slsteam-moon is the low-level Steam integration; Lumen
hosts and injects plugin/add-on code; LuaTools-Moon supplies its own plugin UI and
features; SLS Luna currently installs additional feature add-ons through a shared
Lumen bridge. Cloud-save support may also use the separate CloudRedirect project.

## Intended direction

Keep the existing Luna menu as the single SLS Luna entry point and add original
Luna UI pages for useful slsteam-moon capabilities: configuration, game updates,
cloud saves, and fixes. Where possible, these pages should call Lumen's existing
backend RPCs instead of reimplementing operations or copying Lumen's UI. This is
planned, not current functionality. See [`PROJECT_DIRECTION.md`](PROJECT_DIRECTION.md).

SLS Luna is an add-on, not a replacement for the native slsteam-moon core or the
Lumen runtime. Pin compatible runtime versions and keep upstream code and release
artifacts separate. If an extension point is needed, propose it to Lumen instead
of silently maintaining a fragile permanent patch.

## The contract Lumen already offers

`lua/boot.lua` builds an asset list per injection channel and runs `loop.run`. A
channel can ask for the `callServerMethod` polyfill, and the "SharedJSContext"
channel is the `control = true` channel - the only context that has
`SteamClient`. Upstream LuaTools registers its RPC handlers in the same `registry`
table the frontend dispatches against.

That is enough to host the current add-ons without changing Lumen's own logic:

1. a `dofile` of `sls-addons/bridge.lua` in file scope, so the locals it defines
   are visible to the builders below it;
2. the bridge registers each add-on's backend into `registry`;
3. the bridge returns `ui_js` (shell and store views) and `control_js`
   (SharedJSContext), which the patch appends to the existing asset lists;
4. the control channel's `polyfill` flag is enabled only when the bridge loaded,
   which is what lets an add-on talk to its backend from SharedJSContext.

The `luna` add-on uses that same control channel to publish
`window.__SlsLunaMenu`, a shared registry of per-game right-click actions. The
first action (HowLongToBeat) opens the game's search page through Steam's URL
handlers; other add-ons register their own actions instead of each hooking the
library's jsx layer separately. Playtime keeps its own menu hook because it
predates the registry and must stay independent.

One rule keeps two menu-hooking add-ons from fighting: after the first wrap, an
add-on may only re-wrap when Steam rebuilt the module (a new jsx module object).
Wrapping another add-on's wrapper is not detachment - the inner wrapper still
runs - and treating it as detachment makes both agents re-wrap each other on
every re-arm beat. Each wrapper also falls back to the leaf function on
re-entry, so a wrapper cycle can never recurse without bound.

## Why the current installer uses one patch and not four

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

## Files on disk (current installation)

```
~/.local/share/Lumen/
  lua/boot.lua                         patched once, backup beside it
  sls-addons/
    bridge.lua                         shared loader (pinned)
    luna/{backend,public}/...          shared shell (pinned)
    presence/{backend,public}/...      payload (pinned)
    playtime/{backend,public}/...      payload (pinned)
    compressor/{backend,public}/...    payload (pinned)
~/.local/share/slssteam-integrations/  USER DATA, never touched by install/uninstall
    presence.json
    playtime/<steamid64>.json(.bak)
    compressor/                         settings and queue state
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

## Current safety boundaries

* It does not edit SLSsteam configuration, manifests, games or Steam userdata.
* It does not touch user data on install, uninstall or upgrade.
* It never kills Steam or a Lumen sidecar; it refuses and prints the command.
* Current add-ons use separate, namespaced RPC methods so they can be tested and
  migrated independently. The planned unified UI does not require collapsing
  those backend boundaries.
