# SLS Luna

Repository: `pgm1207/Luna`. Project name: SLS Luna.

SLS Luna (the **Luna** repository) is an independent Steam integration and add-on project. It combines existing upstream components with Luna-maintained features and is intended to provide one useful menu for them. It is not a fork or replacement for Moon or Lumen.

> **Project status:** SLS Luna is not a fork of LuaTools-Moon, slsteam-moon, or Lumen. Its UI and add-ons are maintained here. Integration with Lumen's existing backend capabilities is planned; this repository does not include Lumen's menu or backend source.

## Upstream projects and credits

- **slsteam-moon** ([source](https://github.com/swwayps/slsteam-moon)) is the native Steam integration this stack targets. It is itself a fork of **SLSsteam** ([source](https://github.com/AceSLS/SLSsteam)); SLS Luna does not claim authorship of either project's core code.
- **Lumen** ([source](https://github.com/swwayps/lumen)) is the runtime and UI/backend host. Luna is an add-on to Lumen; this project does not claim authorship of Lumen. Its repository currently has no visible license, so we do not copy or redistribute its code or binaries without clarification.
- **LuaTools-Moon** ([source](https://github.com/swwayps/luatools-moon)) currently provides the base installer and a backend prerequisite for Luna. The installer calls its upstream installer when the base stack is absent; Luna does not fork or copy its plugin source. Its README credits the Linux port lineage and other projects it builds on.
- **CloudRedirect** ([source](https://github.com/Selectively11/CloudRedirect)) is a separate optional project for cloud-save functionality; its code is not owned by SLS Luna.

See [Third-party notices](THIRD_PARTY_NOTICES.md) for the current provenance and licensing notes, and [Contributing](CONTRIBUTING.md) for the planned upstream workflow.

## How the stack fits together

```text
Steam
└── slsteam-moon             native Steam integration (based on SLSsteam)
    └── Lumen                 runtime/bridge into Steam's UI
        ├── LuaTools-Moon     current base/plugin dependency
        └── Luna             installer + Luna-owned add-ons and menu shell
```

The goal is one coherent Luna experience—not to copy upstream projects into one undifferentiated codebase. Moon and Lumen remain separate dependencies; Luna owns its installer/orchestration, add-ons, and UI code. Some of the planned pages are not implemented yet, and the existing upstream UI is not yet replaced.

## What Luna is (and is not)

Think of Luna as an **integrator/distribution with its own features**:

- **Not a fork:** Luna has not copied/forked LuaTools-Moon, slsteam-moon, or Lumen.
- **Installer/orchestrator:** it can set up the upstream base and install Luna's
  verified add-ons on top. A complete existing base is reused, not reinstalled.
- **Luna-owned features:** Playtime, Presence, Compressor, and the menu shell are
  maintained in this project or its explicitly pinned companion repositories.
- **Long-term UI goal:** move useful controls into the Luna shell and offer one
  Luna entry point. Right now the shell exists, but Lumen/LuaTools pages have not
  all been ported; do not expect the upstream menu/button to be gone yet.

## Current implementation

Lumen injects code into the Steam client and exposes a shared backend dispatch registry. The current Luna installer registers one shared bridge in Lumen's `lua/boot.lua` and installs add-ons under `~/.local/share/Lumen/sls-addons/<name>`. Each add-on project can be installed independently; **Luna owns the combination**:

| Add-on | Version | What it does |
| --- | --- | --- |
| `luna` | from the pinned payload | shared shell, navigation and feature composition |
| `playtime` | from the pinned payload | counts non-Steam games while they run; every manifest and shortcut stays editable |
| `presence` | from the pinned payload | publishes a hidden placeholder shortcut so friends see the game being played |
| `compressor` | from the pinned payload | scan, settings, queue, progress and compaction UI for the external btrfs-game-compressor tool |

The compressor add-on drives the separate
[btrfs-game-compressor](https://github.com/pgm1207/btrfs-game-compressor) CLI.
It is discovered at `~/.local/bin/btrfs-game-compressor` (or from
`SLS_COMPRESSOR_BIN`); Luna never installs or updates the tool itself.

## Install

For a fresh installation, the top-level installer sets up the upstream
slsteam-moon/Lumen/LuaTools base first, then installs the pinned Luna add-ons.
The repository is currently private, so the public one-liner will only work after
we explicitly make the repository public. For now, use an authenticated clone:

```bash
git clone https://github.com/pgm1207/Luna.git
cd Luna
bash install.sh
```

It calls a checksum-verified, commit-pinned LuaTools-Moon installer rather than
copying or maintaining that installer. When a complete base is already present,
it skips the upstream installer and only layers SLS Luna on top. It does not
silently upgrade an existing Moon/Lumen installation. The base installer is run
with `--nolaunch` so Luna is installed before Steam is started again.

For a checkout, or to manage an existing installation directly:

```bash
bash scripts/preflight.sh          # read-only: environment, state, and refusals
bash scripts/install-local.sh      # Steam must be closed; installs every add-on
bash scripts/status.sh             # what is installed and whether it matches
```

`install-local.sh` accepts the add-on projects' flags plus its own:

```bash
bash scripts/install-local.sh --only playtime     # one add-on instead of all
bash scripts/install-local.sh --dry-run           # print the plan, write nothing
bash scripts/install-local.sh --force             # replace files that differ
```

Uninstall removes one add-on and leaves the others working; removing the **last**
one restores `boot.lua` from its backup before deleting the payload it points at:

```bash
bash scripts/uninstall-local.sh --only presence
bash scripts/uninstall-local.sh                   # everything
```

Your playtime ledger, presence mapping and compressor state are never touched:
they live in `~/.local/share/slssteam-integrations`, not in the add-on
directories.

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

Native Linux Steam plus the [LuaTools-Moon stack](https://github.com/swwayps/luatools-moon)
(Lumen + slsteam-moon + LuaTools plugin). The LuaTools backend is currently
required by Luna's integration contract; do not use upstream `--noplugin` yet.
Steam's manifest directory, SLSsteam configuration and your games are never
modified.

## Tests

```bash
python3 -m unittest discover -s tests -p 'test_*.py'   # installer, refusals, rollback, data safety
bash scripts/test-backend.sh                           # bridge and shipped add-on backends
python3 scripts/luna.py verify                         # pinned payload integrity
```

## Upstream collaboration

The current installer patches `boot.lua` because the installed Lumen version has
no stable add-on hook. `docs/UPSTREAM.md` describes a possible upstream hook proposal.
The custom Lumen integration is described in `docs/PROJECT_DIRECTION.md`; it is
planned work and should not be presented as already supported by upstream Lumen.
