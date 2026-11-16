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

Bundle format: every file in the bundle is listed with its sha256 in
`RELEASE-FILES.json`, and the installed `luna` checks that list in both
directions (a changed, missing or unlisted file, a duplicate member or a
link is refused) before it runs anything from the bundle, including the
bundled `scripts/luna.py`. Older bundles without the list are refused unless
you pass `--allow-legacy-bundle`. With `--latest` the bundle is also checked
against its `.sha256` sidecar; `--bundle`/`--url` without one are checked only
against the bundle's own list (self-consistency). This is integrity, not
authenticity: someone who can publish a bundle can write a matching list. A
signature would be needed for that and is not implemented.

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

## Several machines over SSH

`luna fleet` manages Luna on your other machines from one place. Register them
once (`luna fleet add laptop` - any name `ssh` understands, key-based login
required; connections run with `BatchMode` and a connect timeout):

```
luna fleet status [--json]           version, luna.py hash, add-on match/drift,
                                     Steam pin, packages, update block, Steam running
luna fleet push-cache laptop         rsync the verified Steam client cache if the build differs
luna fleet install [laptop] [--only NAME] [--restart-steam] [--dry-run] [--force]
```

`install` builds the release bundle from this checkout and installs it with the
remote `luna update --bundle`. It refuses a host where Steam or Lumen is running
unless `--restart-steam` (graceful `steam -shutdown`, install, relaunch), and
refuses drifted payloads (or an older Luna that cannot report them) unless
`--force`. The config lives in `~/.config/luna/fleet.json`.

### Games across machines

```
luna games list [--json]                 installed games here: appid, name, size, library,
                                         Proton prefix, unlocked via stplug-in lua
luna games diff [laptop] [--json]        installed where; for games missing on the target:
                                         does it have the stplug-in lua, does it fit
luna games install laptop 1245620 ...    ask the target's Steam to install (steam://install/ID)
                [--dry-run] [--start-steam]
```

Independent of Proton and save sync. `diff` reads the target through `luna probe --json`
(the `games` section) and falls back to scanning its `appmanifest_*.acf` files over ssh when
the remote Luna is older. A game unlocked through SLSsteam/Lumen can only be installed on a
machine that has `config/stplug-in/<appid>.lua` (normally Syncthing-synced), so `install`
refuses it otherwise; it also checks free space in the target's default library and refuses
when Steam is not running there unless `--start-steam`. Game files are never copied between
machines. A future opt-in `--copy` (rsync of the install directory) is a possible addition.

## GE-Proton

**Rule: one GE-Proton version per machine.** Steam probes every registered
compatibility tool against one shared prefix (`compatdata/0/pfx`), and a second GE
version can overwrite the first one's Windows DLLs through that prefix's symlinks
(details: [docs/PROTON_SINGLE_VERSION.md](docs/PROTON_SINGLE_VERSION.md)).

GE-Proton lives in `~/.local/share/Steam/compatibilitytools.d`, which is not
synced, so each machine installs its own.

- `luna proton status [--json]` lists installed tools, the latest GitHub release
  (`unknown` when offline), games mapped to a tool this machine lacks, and cheap
  (hash-free) warnings: several GE versions installed, DLLs bulk-newer than the tool,
  and prefix links that dangle or point into another tool.
- `luna proton install [VERSION|latest] [--dry-run]` verifies the release's sha512,
  extracts beside the target, records an integrity manifest and renames atomically.
  It never removes the old version: it warns and points at `prune`. Restart Steam after.
- `luna proton verify [VERSION]` hashes the install against its manifest
  (`~/.local/share/Lumen/sls-luna/proton-manifests/NAME.json`: relative path ->
  size + sha256, plus symlink targets) and reports changed / missing / extra files;
  exit 1 on any mismatch. Installs from before manifests existed are never blessed from
  disk: `verify VERSION --rebuild` re-downloads the release, checks its sha512, builds the
  manifest from that tree and compares the install to it. `--against FILE` compares with a
  `sha256sum`-format reference, read-only.
- `luna proton remove VERSION [--force]` refuses while a game is mapped to it
  (`--force` overrides that only).
- `luna proton prune [--keep N] [--dry-run]` keeps the newest N (**default 1**) and never
  removes a version a game is mapped to; if one stays, it tells you to remap and prune again.
- `install`, `remove` and `prune` refuse while Steam, or a process running from the tool
  directory, is up. `luna doctor` reports the same findings. Luna only reads `config.vdf`.

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

`install --only NAME` leaves the client alone when the pinned build is intact;
add `--steam-client` to force it, or `--no-steam-client` to skip it entirely.
Downloads retry on HTTP 429/5xx (honouring `Retry-After`), print the size up
front (`--dry-run` shows it too) and show progress. If Valve rate-limits you,
copy the `package` files from another machine's
`~/.local/share/Lumen/sls-luna/steam-client-cache/package` and pass
`--client-cache DIR` (or set `LUNA_STEAM_CACHE=DIR`); files are sha256-verified.

```bash
python3 scripts/luna.py steam status    # running build, pin, cache, drift
python3 scripts/luna.py steam pin       # pin the currently working build
python3 scripts/luna.py steam cache     # copy its packages aside (Steam closed)
python3 scripts/luna.py steam recover   # put the pinned build back (Steam closed)
python3 scripts/luna.py steam lock      # roll back to the ubuntu12 client + hard-lock updates (kills Steam; --force)
python3 scripts/luna.py steam unlock    # undo the lock and pin (--now launches Steam to self-update)
python3 scripts/luna.py refresh         # update Steam, pin, cache, reinstall Luna (Steam closed)
```

**Refresh Steam + Luna** is the whole flow in one command (and one Health
button): with Steam closed, it updates Steam to Valve's latest, pins and
caches the new build, reinstalls the Luna add-ons keeping that client, and
verifies. Every step fails closed with the exact next action; nothing is
killed, no account/game/save data is touched, and `--dry-run` previews it.
A queued refresh is applied by the user timer while Steam is closed, even
with unattended updates off. The post-update shutdown is requested
gracefully because Valve's bootstrapper relaunches Steam after updating;
if it stays up, Luna reports it instead of proceeding.

`pin` records the client build plus the SLSsteam hashes; `cache` copies the
`package/` tree beside it with a hash manifest (an explicit `--build N` must
match the installed manifest, so a stale log can never mislabel the cache);
`recover` verifies the cache,
extracts and verifies the actual client files, and re-applies the update block.
A package manifest or `build.txt` alone is never proof of a downgrade. Old or
incomplete caches without verifiable Valve packages are refused. The install
receipt identifies the on-disk build using core executable hashes; the next
Steam launch is still the runtime check. Build detection ignores
`Client version: 0` reports (newer clients log that at startup) and prefers
the installed manifest over stale log lines. Before replacing files, Luna keeps
machine-local backups and a `restore.json` index under
`~/.local/share/Lumen/sls-luna/steam-client-backups/`; only the newest five are
kept, the rest are pruned automatically. A failed file transaction
restores the prior client files.
`steam.cfg` settings unrelated to update blocking are preserved. This controls
Steam's bootstrapper, not Fedora/DNF or other system package managers.
`preflight`, the Health page and
every unattended run report drift; with the update mode on Install, the timer
recovers a drifted client from the cache by itself while Steam is closed.

## SteamOS Mode shortcuts and mouse compatibility

Luna's **SteamOS Mode** is one Big-Picture shortcut per screen: `SteamOS
(1080p 60)`, `SteamOS (4K 60)` and so on, generated from the detected
displays (plus a portable 1080p60 target). They are created automatically on
install/update, and any time from **Settings → SteamOS Mode → Create
shortcuts**. No machine's display settings, accounts or games are imported.
Gamescope must be installed separately using the system package manager;
entries remain hidden via `TryExec` until it is available.

- The launcher uses the **SDL backend**. On Niri it also enables relative mouse
  capture (`--force-grab-cursor`), following the
  [Niri application-issues guidance](https://github.com/niri-wm/niri/wiki/Application-Issues#fullscreen-games).
  This avoids the default nested Wayland backend's mouse-lock failures in games.
- Right-click the shortcut and select **Launch with free mouse pointer** to keep
  SDL but disable forced capture. The command-line equivalent is
  `~/.local/bin/luna-steam-machine --mouse-mode free`.
- Steam and games run on Gamescope's Xwayland display instead of inheriting
  desktop-wide Wayland-only SDL/GTK/Qt settings. An existing executable
  `~/.local/bin/ccd-launch` CPU-policy helper is retained when available.
- Launching while a Steam session is already running ends it first (`pkill -x`
  steam, plus its web helper and Lumen sidecar) and then starts the new one,
  so the shortcut never lands in a half-dead session. Save and close games
  first: only the Steam stack is signaled, never game processes, but killing
  Steam out from under a running game still orphans it. A lock prevents
  duplicate console sessions.
- Profiles specify the nested rendering resolution and frame-rate target.
  They do **not** enable/disable monitors, change the desktop refresh rate or
  claim to route a nested window using Gamescope's embedded-only `-O` option.

Recognized old launchers are adopted and renamed to the SteamOS scheme:
originals are backed up under
`~/.local/share/Lumen/sls-luna/steam-machine-backups/`, and uninstalling
restores them. To upgrade recognized old `steam-machine*.desktop` entries
(including the old forced-kill wrappers) explicitly, use:

```bash
python3 scripts/luna.py launchers --dry-run --migrate-legacy
python3 scripts/luna.py launchers --migrate-legacy
```

Unknown custom launchers and hand-edited entries are never silently replaced.
`bash install.sh --no-steam-machine` skips shortcut generation for that install.
`--dry-run` writes nothing. Launcher errors are logged locally in
`~/.local/state/Luna/steam-machine.log`; the desktop version, games and input
behaviour still require an on-device check after changing the backend.

## Health

The **Health** page in the Luna menu reports what a bug report needs: installed
add-on versions against the pinned bundle, `boot.lua` patch markers and backup,
Lumen/Steam paths, the current settings, and the data files with sizes. *Copy
diagnostics* puts the whole report on the clipboard.

If a base update wipes the bridge, Health names it and offers **Repair
bridge** (Steam must be closed); the shell equivalent is
`python3 scripts/luna.py install --keep-steam-client`, which reinstalls the
bridge even when every payload is already current. The attention list, headers
Development builds also arm automatic recovery after **Lumen → Update All**:
the updater waits for the base installer, reattaches the **installed** Luna and
re-arms the next-update hook before Steam is restarted. An independent user
service watches the completion stamp and checks for missed hooks once a minute.
No full reinstall, Steam downgrade, update-policy change or add-on replacement
is involved. Unknown upstream layouts fail closed; running Steam/Lumen is deferred,
never killed by the repair worker. Details: [`docs/LUMEN_UPDATE_HOOK.md`](docs/LUMEN_UPDATE_HOOK.md).

Bridge-only manual commands (close Steam **and** Lumen for a repair):

```sh
python3 scripts/luna.py hook on      # arm/enrol the installed Luna
python3 scripts/luna.py hook status
python3 scripts/luna.py hook repair  # bridge + next-update hook only
python3 scripts/luna.py hook off     # disable recovery, retain add-ons/data/backups
```

Older Health pages offer **Repair bridge** through a full installer invocation;
prefer the bridge-only command above to preserve local payloads and Steam policy.
The attention list, headers
and action buttons switch to Spanish when Settings → Language is Español;
paths and bug-report dumps stay in English in both languages.

## Game saves without Steam Cloud

`luna saves scan` lists installed games whose saves (Proton prefix `AppData`/`Documents`/`Saved Games`,
`~/.config/unity3d`) are not covered by Steam Cloud. `luna saves link <appid|path>` moves the save
directory into `~/.local/share/game-saves/<appid>/` (which Syncthing syncs) and leaves a symlink at the
original path. On the other machine run `luna saves link --all` (launch each game once first so its
Proton prefix exists). If both sides already have saves Luna refuses; choose with `--prefer local|store`
- the losing side is kept as a `.luna-backup-<timestamp>` copy. Nothing is ever deleted.
`luna saves unlink <appid|path>` copies the data back; `luna saves status` shows link health and sync
conflicts (also surfaced by `luna doctor`). Close the game first; `--dry-run` previews.

## Doctor

`luna doctor` finds drift and problems Luna did not cause, one line per
finding with its severity and the exact fix command: a Steam update lock Luna
has no record of (a manual or community-script lock), installed add-ons that
differ from the pins (older/newer release or a `-dev` build), an outdated
installed `luna.py`, SteamOS Mode launchers edited outside Luna, Steam not in
pacman's `IgnorePkg`, updater errors, a drifted `boot.lua` bridge or stale
Lumen sidecar, and Syncthing `*.sync-conflict-*` files. Exit code 0 = ok,
1 = warnings, 2 = errors; `--json` is for scripts. `--fix` applies only safe,
reversible fixes (restoring Luna's own `steam.cfg` block, with Steam closed);
it never uses sudo or overwrites payloads. A foreign read-only Steam lock whose
packages match the pin is *adopted* (`luna steam lock --adopt`: records Luna's
hard lock, no rollback, nothing killed; undo with `luna steam unlock`).
`luna fleet status` shows each host's doctor warn/error counts in a DOCTOR column.

```bash
python3 scripts/luna.py doctor [--json] [--fix]
```

## GUI checks (optional)

`luna gui` looks at what the Steam client actually shows, through the
`agent-desktop` accessibility CLI (AT-SPI). It is optional:
without the tool, a graphical session or an accessible Steam every command prints
`GUI checks skipped: <reason>`. Exit codes of `verify`, `library`, `launch`: 0 = pass,
1 = failure, **5 = skipped** (it could not run; not a pass and not a failure), so a
script can tell "nothing was checked" from "checked and fine". `gui` / `gui status`
only reports and always exits 0.

```bash
python3 scripts/luna.py gui                    # tool, session, Steam, accessible mode, screen, window titles
python3 scripts/luna.py gui launch             # start Steam with -cef-force-accessibility (never the normal launch)
python3 scripts/luna.py gui verify [--launch] [--no-navigate] [--full-screen] [--report FILE] [--screenshots DIR] [--wait SECONDS] [--only NAME]
python3 scripts/luna.py gui library [--open] [--json] [--save FILE] [--compare FILE [--common]]
python3 scripts/luna.py gui watch (--role ROLE [--name NAME [--contains]] | --addon KEY) [--interval S] [--duration S] [--fail-if-vanished]
python3 scripts/luna.py gui restart-check [--force] [--final accessible|normal|stopped] [--keep-steam] [--settle S]
```

`verify` waits for a Steam window, waits out the startup window and refuses a persistent sign-in screen ("sign in first"),
then checks that each installed add-on's panel is found in the accessibility tree
(sampled several times, so an element that appears and vanishes is reported
`unstable`), takes a screenshot of the Steam window only (`niri msg action screenshot-window`, the
main window picked by title, checked to exist and sized against niri's window size;
without niri the screenshot is skipped with a note, never a full-screen fallback; the
explicit `--full-screen` flag captures everything visible with `grim`) and writes a markdown report under
`~/.local/share/Lumen/sls-luna/gui-reports/`. Exit 1 on any failure. Set
`LUNA_GUI_CHECKS=0` to disable the layer. The panel selectors were confirmed on a
live signed-in client; the Luna entry point is `button 'Luna'` and the add-on pages
are reached by one navigation click (the Luna overlay, closed again afterwards;
`--no-navigate` checks only the entry point). All six overlay pages
(Playtime, Presence, Compressor, HowLongToBeat, SteamGridDB, Graphics) plus Health,
Settings and About were confirmed the same way. Steam's secondary windows (Settings,
Friends List) turned out to be ordinary windows (one `webarea` named after the
window, no dialog role, no Close button), and blocker messages say so; a real
modal dialog was never observed, so the `alert`/`dialog` handling is still
unverified. `verify --launch` closes the Steam it started again (through the
normal graceful shutdown, never a kill) unless `--keep-steam`; a Steam that was
already running is never closed. The screenshot step saves and restores the
clipboard (niri's `screenshot-window` overwrites it; needs `wl-clipboard`, else the
report notes that the clipboard was not restored). Window listing and capture use
the `niri` backend (tested) or a `generic` one (agent-desktop window list, no
capture; other compositors are not guessed at, `luna gui` shows which is active). `library` reads the Library list in display order
(a virtualized grid: only the rendered rows exist, ~54 of the whole library, so the
result depends on the scroll position); `--save` writes it, `--compare` exits 1 on
a difference, `--common` compares only the relative order of shared names. Steam
exposes many AT-SPI windows, so Luna lists them and snapshots the main one. When Steam will not shut down, the
messages also list the open Steam windows and any dialog buttons, and a Lumen
sidecar that outlives Steam is reported with its pid and start time.

`watch` samples the tree every `--interval` seconds (default 2) and prints a
timestamped `APPEARED` / `VANISHED` line when the element changes, then a summary;
it only reads, so open the page you want to watch yourself. Ctrl-C and `--duration`
end it with exit 0; `--fail-if-vanished` stops at the first vanish after the element
was seen and exits 1; a bad selector exits 2. A snapshot that cannot be read is
never counted as a vanish.

`restart-check` is the one command that restarts Steam, once (twice with
`--final normal` from accessible mode). It refuses (exit 3) while a game runs or a
download is active unless `--force`; records the screen, the add-on panels and the
Library order (complete list and sort state over DevTools when it answers, else the
visible rows); shuts Steam down through the normal graceful path, reports a Lumen
sidecar that survived; relaunches in accessible mode, waits up to `--settle`
seconds for the Library order, compares, and writes
`gui-reports/gui-restart-<stamp>.md` with a window screenshot. Exit 0 = identical,
1 = any difference or failure, 2 = bad `--final`, 3 = refused, 5 = skipped. It
returns Steam to the mode it started in (`--final` overrides, `--keep-steam` leaves
it accessible). "Sign in to Steam" is also Steam's startup window, so Luna tells three
states apart: `starting` (window up, or a log-on in `connection_log.txt`), `login` (the
window outlasted `--login-grace` seconds, default 120, with no log-on attempt) and `main`.
Only `login` stops the check: nothing is typed and Steam is not restarted again. The report
says which signal led to each conclusion. A Lumen sidecar that survives the shutdown is a
warning (exit stays 0), never killed. `luna gui` prints `starting` while it cannot yet tell.

After `install`/`update` on a terminal, one line points at `luna gui verify`; it is
printed only when the GUI layer could run and never starts Steam.

**Not done, and why:** running the GUI checks across the fleet. `luna fleet` only
sends fixed scripts over non-interactive SSH, which has no graphical session
(no `WAYLAND_DISPLAY`, D-Bus or AT-SPI bus), so `luna gui` there would only print
"skipped". No remote GUI mechanism was built, and none of this was tested on a
second machine.

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
python3 scripts/luna.py status --live   # also ask the RUNNING client (read-only)
```

`status --live` reads Steam's DevTools port (found from Lumen's `cef_port`, else the
`--remote-debugging-port` of `steamwebhelper`) and reports, per add-on, whether its
backend answers and what it says about its own health, whether the `boot.lua` marker
is reflected in the running process, and whether the Lumen sidecar started before the
installed files changed (restart Steam if so). It only calls an allow-listed set of
read-only RPCs, never clicks, and prints one clear line instead of failing when Steam
is closed or the port is unavailable. The *rendered* column comes from the GUI layer
(`n/a (<reason>)` when it is unavailable). `LUNA_LIVE_CHECKS=0` switches it off.
`python3 scripts/luna.py library-order [--save FILE|--compare FILE|--json]` prints
the whole Library list order as the client displays it (all rows, not just the visible ones).

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

**Where the CLI code lives.** `scripts/luna.py` is a *generated* single file (it is
what the `luna` shim, `luna update`, `luna fleet install` and the in-Steam add-on
run, so it must stay one file). Edit the fragments in `scripts/luna_cli/` instead
(`10_reconcile`, `20_common`, `30_plan_apply`, `40_status_release_update`,
`50_timer_backup`, `60_steam_client`, `70_steam_cmd_refresh`,
`80_install_verify_vendor`, `90_fleet`, `95_doctor`, `99_cli`). They are
consecutive slices of one module, concatenated in filename order, not
separately importable packages. After editing, rebuild and commit both:

```bash
python3 scripts/build-luna.py           # rewrite scripts/luna.py
python3 scripts/build-luna.py --check   # exit 1 if it is stale (also a unit test)
```

```bash
bash scripts/test-all.sh                   # everything CI runs: UI, backends, installer
python3 scripts/luna.py verify --sources   # also compares against local checkouts
```

Hosted GitHub Actions is currently blocked by account billing limits, so no
hosted run is claimed as a release gate: the local `test-all.sh` run above is
the gate, and its result is recorded on the release PR.

Luna is a private preview. Versions, supported distributions, optional installs,
and feature availability will be documented here as they are implemented and
tested.
