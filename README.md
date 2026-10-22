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
| **Compressor** | Available | Manage Btrfs game compression jobs and progress from Steam; the Stats sub-tab charts the savings its own ledgers measured |
| **Store filter** | Available | Settings → Hide followed games in the store: store home and discovery lists stop showing games you already follow (search and game pages stay untouched; the list is read from the store session on this machine) |
| **Settings that follow you** | Available | The switches (beta features, HLTB order, store filter) live in Luna's backend file, so a CEF cache reset cannot lose them and both machines follow the same choice; the shell mirrors them into localStorage for the agent |
| **Health page** | Available | A local diagnostics tab: add-on versions versus the pinned bundle, boot.lua marker/backup state, Lumen and Steam paths, settings and data-file sizes, plus Copy diagnostics for bug reports |
| **Bundle releases** | Available | `luna release` builds a verified bundle (payloads + pins + sha256), `luna update --bundle/--url/--latest` installs it on another machine - no repo checkout or file sync needed |
| **One-step setup** | Available, private preview | Install the upstream base when needed, then add Luna's integrity-checked payloads |
| **Cloud saves / CloudRedirect** | Optional upstream component; Luna page planned | CloudRedirect is separate software; a unified Luna page is not implemented yet |
| **Decky Loader** | Planned / compatibility review | Optional install is being evaluated; Decky is a separate project and is not installed by Luna today |
| **Moon settings, updates, fixes** | Planned | Bring useful controls into Luna's navigation without copying upstream UI code |
| **Artwork picker / right-click actions** | Available (beta switch) | Under Settings → Show beta features: right-click a game → Change artwork opens the Luna SteamGridDB picker (grid, wide, hero, logo, icon, auto-apply, live refresh, backup/restore) |
| **Beta features switch** | Available | Settings tab hides in-development pages; today it gates SteamGridDB artwork and the read-only Graphics page, and will gate frame generation (`lsfg-vk`) and OptiScaler management as those land |
| **Graphics detection (beta)** | Available (read-only) | Reports GPU, driver, kernel/distro, `lsfg-vk` Vulkan layer + config, overlapping Decky plugins, OptiScaler installs (including the OptiScaler Client's managed games) and the Steam runtime; installers are planned, nothing is installed yet |
| **`lsfg-vk` / frame generation** | Research/design | Optional setup and per-game controls only after upstream, hardware, and platform compatibility gates |
| **OptiScaler / OptiClient** | Port started (read-only) | The Graphics page already lists the client's managed games and their upscaler state; deployment and configuration are planned (see `docs/optiscaler-client-port.md`). OptiClient is a separate third-party project, and per-game support must be validated |
| **HowLongToBeat (HLTB)** | Luna page + library sort and capsule hours available | The HowLongToBeat tab manages locally cached times (background fetch pass, skips unmatched games, pauses on rate limits). The library's SORT BY gains a HowLongToBeat option that labels each capsule with its hours. The game-page cell was attempted and removed; tracked as an issue |
| **Rust runtime alternative** | Feasibility study | Explore a Luna-owned runtime path; no Lumen replacement exists, and Steam UI injection still needs a web/CEF bridge |

The Compressor page requires the separate
[btrfs-game-compressor](https://github.com/pgm1207/btrfs-game-compressor) tool;
the current installer does not install or update that executable.

## Updating

The **Health** page in the Luna menu checks the release channel and shows the
installed version against the newest release, with **Check now**, **Install
update** and a three-way switch:

- **Off** - never checks.
- **Notify me** - a user timer checks every 30 minutes and records what it found;
  the Health page shows it (and the Health tab gains a dot). Nothing is installed.
- **Install automatically** - the same timer installs a newer release by itself.

Applying an update needs Steam closed, so one queued from the menu lands on the
next Steam exit - or, with the timer, whenever Steam is closed. The check uses
your own `gh` authentication (no token is stored by Luna) and is cached for six
hours. The timer is a plain systemd user unit (`luna-autoupdate.timer`) running
the standalone updater copy at `~/.local/share/Lumen/sls-luna/luna.py`.

```bash
python3 scripts/luna.py check --force        # JSON: installed, latest, available
python3 scripts/luna.py update --latest      # apply now (Steam must be closed)
python3 scripts/luna.py timer on|off|status  # the unattended timer
```

On the source machine, `luna release --publish` builds the bundle, writes its
`.sha256` and can create the GitHub release; on any other machine
`luna update --bundle FILE`, `--url URL` or `--latest` verifies it and installs
with the bundled installer, so no checkout is needed. A tampered bundle is
refused and nothing is installed.

## Two machines, one library

Playtime hours, preferences, artwork and Steam's own `localconfig.vdf`
(recents, collections) sync between machines, so both feel the same even with
different games installed. Two Steam instances writing that file at once
produce `localconfig.sync-conflict-*` copies and last-writer-wins amnesia
("gaming mode forgot what desktop mode played"). The fix is merge, not picking
a side:

```bash
python3 scripts/luna.py reconcile --dry-run   # what would merge
python3 scripts/luna.py reconcile             # merge (Steam must be closed)
```

Reconcile keeps every non-recency key from the newest copy and takes the
newest timestamp per game for `LastPlayed`, the recent-played list and the
Deck seen-state; consumed conflicts are archived next to the file. Workflow:
fully exit Steam (check no `lumen`/`steamwebhelper` lingers), let the sync
settle, reconcile, then start Steam on the other machine. The Health page
reports waiting conflicts so the menu tells you when this is needed.

## Steam client versions

A Steam client update can move the interfaces used by the native integration.
**New installations and explicit reinstalls default to client build `1791918634`.**
Luna verifies a SHA256-pinned historical Valve manifest, obtains its hash-named
packages from the local installation/cache or Valve's CDN (archive fallback),
extracts the actual executables, and verifies every installed client file. It
then caches and pins that build and blocks the Steam bootstrapper's self-updates.
It never copies another machine's accounts, games, saves or settings.

An older client does not include newer security/compatibility fixes. To retain
your chosen Steam client instead, use `bash install.sh --keep-steam-client` or
`python3 scripts/luna.py install --keep-steam-client`. Ordinary Luna bundle
updates preserve the client choice; they do not silently undo a Health update.

```bash
python3 scripts/luna.py steam status    # running build, pin, cache, drift
python3 scripts/luna.py steam pin       # pin the currently working build
python3 scripts/luna.py steam cache     # copy its packages aside (Steam closed)
python3 scripts/luna.py steam recover   # put the pinned build back (Steam closed)
```

`pin` records the client build plus the SLSsteam hashes; `cache` copies the
`package/` tree beside it with a hash manifest; `recover` verifies the cache,
extracts and verifies the actual client files, and re-applies the update block.
A package manifest or `build.txt` alone is never proof of a downgrade. Old or
incomplete caches without verifiable Valve packages are refused. The install
receipt identifies the on-disk build using core executable hashes; the next
Steam launch is still the runtime check. Before replacing files, Luna keeps
machine-local backups and a `restore.json` index under
`~/.local/share/Lumen/sls-luna/steam-client-backups/`; a failed file transaction
restores the prior client files. These backups are retained, not auto-deleted.
`steam.cfg` settings unrelated to update blocking are preserved. This controls
Steam's bootstrapper, not Fedora/DNF or other system package managers.
`preflight`, the Health page and
every unattended run report drift; with the update mode on Install, the timer
recovers a drifted client from the cache by itself while Steam is closed.

## Health

The **Health** page in the Luna menu reports what a bug report needs: installed
add-on versions against the pinned bundle, `boot.lua` patch markers and backup,
Lumen/Steam paths, the current settings, and the data files with sizes. *Copy
diagnostics* puts the whole report on the clipboard.

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

The repository is currently **private**, so you install it from a checkout (the
`curl | bash` one-liner in `install.sh` starts working once it is public).

**What you need first**

- A Steam install for this user (`~/.local/share/Steam` or `~/.steam/steam`).
- The supported base: **slsteam-moon + Lumen (+ its LuaTools backend)**, either
  already working or absent. A complete base is reused untouched; if it is
  absent, `install.sh` fetches the pinned upstream installer, verifies its
  SHA256 and runs it with auto-launch disabled; a **partial** base is a stop
  condition, because Luna never removes or replaces base components itself.
- `python3` and `bash` (`curl` only for the bootstrap path).
- Network access to the pinned historical manifest and client packages, unless
  the complete verified build is already available locally; free space for
  staging, a package cache, and a rollback copy of replaced client files.
- **Steam fully closed** - the installer patches Lumen's `boot.lua` and the
  add-ons live inside its runtime.
- Do not run as root.

**Install**

```bash
git clone https://github.com/pgm1207/Luna.git
cd Luna

bash scripts/preflight.sh                 # read-only: what is here, what is missing
bash scripts/install-local.sh --dry-run   # the exact plan; writes nothing
bash scripts/install-local.sh             # verified Steam downgrade + Luna add-ons
```

`install.sh` is the one-shot version: the same checks, plus it bootstraps the
base when it is missing (pinned commit, SHA256-verified, `--nolaunch`).
`bash install.sh --help` lists its options (`--only NAME`, `--force`,
`--dry-run`, `--keep-steam-client`, and the upstream release-channel flags).
Both installation entry points include the compatibility downgrade by default,
even when the add-on payloads are already identical. Steam/Lumen must be fully
closed. Downloads or checks that fail stop installation without claiming success;
`--dry-run` neither downloads nor changes the client. Queued Steam tasks must be
cancelled explicitly before installing, so they cannot immediately undo the pin.

Then **restart Steam** (Steam → Exit, then launch it again): the Luna button
appears next to Lumen's in the menu bar and holds every page.

**Verify**

```bash
bash scripts/status.sh               # installed files vs. the pinned payloads
python3 scripts/luna.py verify       # manifest, payload and drift checks
```

In the client, the Luna menu's **Health** page shows the installed versions
against the pinned bundle, the `boot.lua` marker/backup state, the Lumen/Steam
paths, the current settings and the data files, with *Copy diagnostics*.

**If it refuses**

The installer is fail-closed on purpose: Steam running, a Lumen sidecar still
alive, a `boot.lua` that drifted, a partial base, or an installed payload that
differs from the pin. Each message names the file or process to look at; use
`--force` only after reading the diff it prints.

**Other machines and updates**

`luna release` builds a verified bundle and `luna update --bundle/--url/--latest`
installs it without a checkout - see [Updating](#updating).

See the [installation roadmap](docs/INSTALL_PLAN.md) for the planned
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
bash scripts/test-all.sh                   # everything CI runs: UI, backends, installer
python3 scripts/luna.py verify --sources   # also compares against local checkouts
```

Luna is a private preview. Versions, supported distributions, optional installs,
and feature availability will be documented here as they are implemented and
tested.
