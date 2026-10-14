# OptiScaler Client port (Luna)

Status: **step 1 shipped, read-only.** This document scopes what Luna will build
for OptiScaler management, what it deliberately will not build, and the gates
between the steps. It is the plan the roadmap's section 3 refers to.

Keep the terms distinct, as the roadmap does: **OptiScaler** is the graphics
middleware; **OptiScaler Client** is a separate third-party management
application, not the official OptiScaler project. Luna never quietly
redistributes either project's binaries, and nothing here says the client is
part of OptiScaler.

## What exists on a real machine

Verified against the install this project is developed on:

- Application: `/opt/optiscaler-client/OptiscalerClient` plus its assets and
  native libraries; the desktop entry carries
  `X-OptiscalerClient-Generated=true`. On another machine the same app may live
  in `~/.local/share/OptiScalerClient`, `~/Applications/OptiScalerClient`, or a
  Flatpak data dir under `~/.var/app/<id>`; all of those are detection
  candidates.
- State: `~/.config/OptiscalerClient/`
  - `games.json` - one record per managed game. Useful fields: `Name`,
    `InstallPath`, `AppId`, `Platform`, `IsOptiscalerInstalled`, `FsrVersion`,
    `FsrPath`, `FsrIsNative`, `IsFsr4DllSwapped`, `DlssIsNative`,
    `DlssViaOptiscaler`, `XessIsNative`, `XessViaOptiscaler`, and the
    `DlssNr*` flags. Records often carry the placeholder name `appid_<id>`
    until the client resolves a real one.
  - `config.json` - application settings (default channels, language, update
    bookkeeping).
  - `Profiles/builtin` and `Profiles/custom` - JSON profiles; a builtin profile
    is just a name, description and an `IniSettings` object.
  - `Cache/` - downloaded payloads per project (`OptiScaler`, `DlssNr`,
    `DlssNrLinuxWrapper`, `Fakenvapi`, `OptiPatcher`, `Extras`).
  - `Backups/`, `Covers/`, `Icons/`, release caches, and a crash log.

## Step 1 - read-only integration (shipped)

Add-on `graphics` 0.2.0:

- Detects every install flavour above, without running the client.
- Parses `games.json` and returns a compact per-game record: resolved name
  (`appid_<id>` names are resolved through the adjoining
  `steamapps/appmanifest_<id>.acf`), install path, whether the path still
  exists, the client's `IsOptiscalerInstalled` flag, the FSR/DLSS/XeSS state,
  and the OptiScaler files Luna can actually see deployed (top level and one
  subdirectory, capped, names containing `optiscaler`/`nvngx`).
- Reports the config directory, the application directory and how many
  OptiScaler versions are cached.
- Surfaces all of it in the Graphics beta page. Luna never writes to the
  client's files.

Deliberate limits: the client database is treated as a **claim** that Luna
re-checks against the filesystem; a stale record is shown as `path missing`
rather than as a live install.

## Step 2 - deployment engine (planned)

Goal: install, update and remove OptiScaler for a single game from Luna, with
the guarantees the roadmap already promises everywhere else (preview, backup,
rollback, operation log).

Preconditions before any code:

1. Choose the payload source. Options: the official OptiScaler releases (with
   integrity verification where published), or the client's already-downloaded
   cache when the user has it. If the cache is used, Luna reads it, never
   mutates it; the licence and redistribution terms of both projects must be
   reviewed first.
2. Define the deployment inventory: which files are added or replaced next to
   the executable (`OptiScaler.dll`/`.ini` and the loader name the game's
   graphics API needs), how a pre-existing file with the same name is treated
   (always back up, never merge silently), and how the choice interacts with
   the game's Proton runtime.
3. An identity check: the selected game must resolve to an app id and an
   install path Luna can prove (the same mapping the artwork picker uses).

Acceptance for step 2: deploying to a chosen game shows the exact file diff
before writing, makes backups, verifies the game still launches, can restore
byte-for-byte, and records everything in the operation log. No global installs,
no silent updates, no anti-cheat circumvention of any kind.

## Step 3 - per-game configuration (planned)

Goal: edit the settings that actually exist for that game and build, from Luna.

- Read the deployed `OptiScaler.ini`, present only the settings the detected
  OptiScaler build supports, and write through the same backup/rollback path.
- Represent what cannot be controlled from outside OptiScaler (some options are
  in-game only) as report-only text, never as a fake toggle.
- Profiles: importing/exporting the client's profile format is attractive, but
  its schema is undocumented; only adopt it once its behaviour is understood,
  and never write into `Profiles/` without a documented contract.

## Step 4 - companion components (separate gates)

FSR4 DLL swaps, DLSS enabler mirrors, Fakenvapi and OptiPatcher are their own
projects with their own file-level effects and update channels. Each one needs
its own preview/backup/restore story and licence review before Luna touches it.
They are not implied by steps 2-3.

## Non-goals

- No redistribution of OptiScaler, OptiScaler Client or companion DLLs without
  a licence review.
- No UI scraping of the client, no editing its undocumented state files.
- No universal frame-generation or upscaling promises; compatibility depends on
  GPU, driver, graphics API, Proton runtime, OptiScaler build and the game.
- No anti-cheat bypass, no multiplayer integrity interference.

## Open questions

- Does OptiScaler Client expose any stable CLI or local API? Today only its
  files are a usable interface, and they are undocumented; every step above is
  planned around that constraint.
- Which Flatpak application id ships it (for the `~/.var/app` detection path)?
  The current machine uses the native `/opt` layout.
- Should Luna keep its own deployed-file manifest per game, or rely on backups
  alone? A manifest is needed for reliable updates/removals; the backup alone
  cannot tell "ours" from "the user's".
