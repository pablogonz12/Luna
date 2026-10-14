# Luna

### One curated bundle for a better Linux Steam setup.

Luna brings useful Steam integrations, tools, and Luna-made features together in
one installation—with a focused menu to manage the Luna features. Instead of
rebuilding every underlying component, Luna installs and coordinates proven
projects, then adds features designed to work well together.

> **Private preview · Linux Steam · In active development**
>
> Luna is an integrator and feature bundle, **not a fork or replacement** for
> slsteam-moon, Lumen, LuaTools-Moon, or Decky Loader. The one-menu goal is still
> being built: upstream settings, updates, fixes, and cloud-save pages are not all
> in Luna yet, and Luna does not currently hide or remove upstream menus.

## What you get today

| Feature | Status | What it does |
| --- | --- | --- |
| **Luna menu shell** | Available | One place for Luna's installed add-on pages |
| **Playtime** | Available | Track and edit local playtime for Steam shortcuts and non-Steam games |
| **Presence** | Available | Show selected non-Steam activity to friends through the supported integration |
| **Compressor** | Available | Manage Btrfs game compression jobs and progress from Steam |
| **One-step setup** | Available, private preview | Install the upstream base when needed, then add Luna's integrity-checked payloads |
| **Cloud saves / CloudRedirect** | Optional upstream component; Luna page planned | CloudRedirect is separate software; a unified Luna page is not implemented yet |
| **Decky Loader** | Planned / compatibility review | Optional install is being evaluated; Decky is a separate project and is not installed by Luna today |
| **Moon settings, updates, fixes** | Planned | Bring useful controls into Luna's navigation without copying upstream UI code |
| **Artwork picker / right-click actions** | Planned; the action menu is wired | A game right-click action can now open Luna features; artwork search/apply is still to come |
| **`lsfg-vk` / frame generation** | Research/design | Optional setup and per-game controls only after upstream, hardware, and platform compatibility gates |
| **OptiScaler / OptiClient** | Planned | Unified Luna management is a goal; OptiClient is a separate third-party project, and per-game support must be validated |
| **HowLongToBeat (HLTB)** | Luna page + library sort available; game-page display designed | The HowLongToBeat tab manages locally cached times: an on-demand **background pass** that skips unmatched games, pauses on rate limits, keeps running with the menu closed and resumes after a Steam restart until stopped. The library's SORT BY gains a HowLongToBeat option that also labels each game with its hours |
| **Rust runtime alternative** | Feasibility study | Explore a Luna-owned runtime path; no Lumen replacement exists, and Steam UI injection still needs a web/CEF bridge |

The Compressor page requires the separate
[btrfs-game-compressor](https://github.com/pgm1207/btrfs-game-compressor) tool;
the current installer does not install or update that executable.

## How Luna fits into the stack

```text
Steam client
└── slsteam-moon       native Steam integration
    └── Lumen           Steam UI injection, runtime, and RPC host
        ├── LuaTools-Moon   current base/plugin dependency
        └── Luna           installer + Luna-owned menu and add-ons
```

Luna's job is to make the overall setup easier to install and use—not to claim
ownership of the upstream runtime or native Steam integration. Luna's installer
keeps dependencies identifiable and installs Luna's own verified add-ons on top.
Today, the LuaTools backend is still required by Luna's integration, so the base
plugin is retained. Removing that dependency and consolidating the overlapping
menus requires completing and validating the Luna page integrations first.

## Install

The repository is currently **private**, so installation requires GitHub access.
Clone it, then run the installer:

```bash
git clone https://github.com/pgm1207/Luna.git
cd Luna
bash install.sh
```

The installer reuses a complete existing LuaTools-Moon stack. If the base is
missing, it invokes the upstream installer at a pinned commit and verifies its
SHA256 before running it; Steam is not auto-launched until Luna's add-ons are
installed. Luna add-on payloads are separately pinned and integrity-checked.
Existing Moon/Lumen installations are not silently upgraded. A partial base is a
stop condition, not an invitation for Luna to remove or replace it.

For an existing installation, inspect before changing anything:

```bash
bash scripts/preflight.sh          # read-only environment and safety checks
bash scripts/status.sh              # installed files and drift
bash scripts/install-local.sh --dry-run
bash scripts/install-local.sh       # Steam must be fully closed
```

Use `--only NAME` to install one add-on, or `--force` only after reviewing the
differences. See the [installation roadmap](docs/INSTALL_PLAN.md) for the planned
optional-component flow, including Decky Loader and CloudRedirect.

## What Luna protects

- Installation refuses while Steam or the Lumen sidecar is running.
- Payloads are pinned by source commit and SHA256; unexpected drift fails closed.
- Lumen's boot integration is checked before it is changed and restored on rollback.
- Playtime, presence, and compressor user data live outside add-on directories and
  are preserved during installation and removal.
- Current Luna installers/add-ons do not edit Steam game manifests or game files.
  Future artwork and graphics features may need scoped game/library changes; they
  must be previewed, backed up, and reversible before release.

## Project identity and credits

Luna is an independent integration/distribution project. It is **not** a fork of
[LuaTools-Moon](https://github.com/swwayps/luatools-moon),
[slsteam-moon](https://github.com/swwayps/slsteam-moon), or
[Lumen](https://github.com/swwayps/lumen). Those projects provide components that
Luna currently uses; Luna's own menu and features are maintained here and in its
credited companion repositories. Decky Loader and CloudRedirect are also separate
projects, not Luna-owned code.

See [third-party notices](THIRD_PARTY_NOTICES.md) for provenance and license
status, [project direction](docs/PROJECT_DIRECTION.md) for scope, the
[feature roadmap](docs/FEATURE_ROADMAP.md) for planned artwork, graphics,
right-click, and HLTB integrations, and [contributing](CONTRIBUTING.md) for the
collaboration model. Lumen's repository
had no visible license when last checked; Luna does not copy or redistribute its
source.

## Development checks

```bash
node --test tests/*.test.cjs
python3 -m unittest discover -s tests -p 'test_*.py'
bash scripts/test-backend.sh
python3 scripts/luna.py verify --sources
```

Luna is a private preview. Versions, supported distributions, optional installs,
and feature availability will be documented here as they are implemented and
tested.
