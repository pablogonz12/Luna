# SLS Luna

Repository: `pgm1207/slsteam-luna` (private). Product name: SLS Luna.

One installer, verification and rollback for the SLSsteam-Moon / Lumen add-ons.

Lumen injects code into the Steam client and exposes a single dispatch registry.
Every add-on it loads lives under `~/.local/share/Lumen/sls-addons/<name>` and is
reached through one shared bridge that is patched into `lua/boot.lua` **once**.
Each add-on project ships its own installer for people who want only that add-on;
**Luna owns the combination**:

| Add-on | Version | What it does |
| --- | --- | --- |
| `playtime` | from the pinned payload | counts non-Steam games while they run; every manifest and shortcut stays editable |
| `presence` | from the pinned payload | publishes a hidden placeholder shortcut so friends see the game being played |

## Install

```bash
bash scripts/preflight.sh          # read-only: environment, state, and refusals
bash scripts/install-local.sh      # Steam must be closed; installs both add-ons
bash scripts/status.sh             # what is installed and whether it matches
```

`install-local.sh` accepts the add-on projects' flags plus its own:

```bash
bash scripts/install-local.sh --only playtime     # one add-on instead of both
bash scripts/install-local.sh --dry-run           # print the plan, write nothing
bash scripts/install-local.sh --force             # replace files that differ
```

Uninstall removes one add-on and leaves the other working; removing the **last**
one restores `boot.lua` from its backup before deleting the payload it points at:

```bash
bash scripts/uninstall-local.sh --only presence
bash scripts/uninstall-local.sh                   # everything
```

Your playtime ledger and presence mapping are never touched: they live in
`~/.local/share/slssteam-integrations`, not in the add-on directories.

## What it refuses to do

Fail-closed by design, because every one of these has bitten this stack:

* **Steam is running** - modifying Lumen under a live client. Close it via
  *Steam → Exit*.
* **A Lumen sidecar is still alive** from an earlier session. It keeps the old
  `boot.lua` in memory, so a successful install looks like it did nothing. Luna
  never kills it for you:
  `pkill -u $(id -u) -x lumen`
* **`boot.lua` has drifted** from the layout the patcher knows. Nothing is
  written.
* **An installed file differs from the pinned payload** - for example a local
  edit or a newer build from an add-on's own installer. Use `--dry-run` to see
  the diff, then `--force` if you mean it.
* **The backup is missing** on uninstall. Nothing is deleted while Lumen is
  patched, so a failed restore cannot leave a live bridge pointing at removed
  files.

Every write is transactional: if anything fails partway, the previous files are
restored and the `boot.lua` patch is reverted.

## Pinned payloads

The add-on code in this repository is not a fork. `addons/` and the shared
`bridge/bridge.lua` + `scripts/patch-lumen.py` are extracted from the add-on
projects at exact commits and recorded with their sha256 in
`vendor/MANIFEST.json`:

```bash
python3 scripts/luna.py verify                # payloads match the manifest
python3 scripts/luna.py verify --sources      # ...and the local source checkouts
bash scripts/vendor-update.sh                 # re-pin from the source checkouts
```

That is why installing Luna needs no network and no second checkout, and why what
gets installed is exactly what was reviewed.

## Requirements

Native Linux Steam plus the [LuaTools-Moon
stack](https://github.com/swwayps/luatools-moon) (Lumen + SLSsteam-Moon). Steam's
manifest directory, SLSsteam configuration and your games are never modified.

## Tests

```bash
python3 -m unittest discover -s tests -p 'test_*.py'   # installer, refusals, rollback, data safety
bash scripts/test-backend.sh                           # real bridge + both real backends
python3 scripts/luna.py verify                         # pinned payload integrity
```

## Towards upstream Lumen

The `boot.lua` patch exists because upstream Lumen has no add-on hook yet. Luna is
built so that hook can replace the patch later without changing any add-on:
`docs/UPSTREAM.md` describes the contract to propose and the migration, and that
work is deliberately **not** started yet.
