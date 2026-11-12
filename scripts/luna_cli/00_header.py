#!/usr/bin/env python3
"""SLS Luna - install the SLSsteam-Moon / Lumen add-ons as one unit.

Lumen loads every add-on it finds under `~/.local/share/Lumen/sls-addons/<name>`
through one shared bridge, which is patched into `lua/boot.lua` once. Each add-on
project ships its own installer for people who want only that add-on; Luna owns
the combination:

  * one preflight, one install, one rollback for the whole set;
  * payloads pinned by source commit and verified by sha256, so what is installed
    is exactly what was reviewed (see vendor/MANIFEST.json);
  * fail-closed refusals: Steam running, a Lumen sidecar still alive from a
    previous session, a drifted boot.lua, or an on-disk payload that differs from
    the pinned one without --force;
  * user data is never touched - it lives outside the add-on directories.

Commands:
    luna preflight [--only NAME]        read-only environment and state report
    luna status [--only NAME]           what is installed, and whether it matches
    luna install [--only NAME] [--force] [--dry-run]
                 [--steam-client | --no-steam-client] [--client-cache DIR]
    luna hook on|off|status|repair       automatic bridge recovery after Lumen updates
    luna uninstall [--only NAME]
    luna verify [--sources]             manifest, payload and drift checks
    luna doctor [--json] [--fix]        find drift Luna did not cause (foreign Steam
                                        locks, edited payloads/launchers, sync
                                        conflicts, ...); one line each with the fix;
                                        exit 0 ok / 1 warnings / 2 errors; --fix
                                        applies only safe, reversible fixes
                                        (a matching foreign lock is adopted: steam lock --adopt)
    luna vendor [--source-dir DIR]      re-pin payloads from the add-on projects
    luna release [--out DIR] [--publish]  build a bundle tarball (+ sha256) for other machines
    luna update --bundle FILE | --url URL | --latest [--only NAME] [--dry-run]
                                        verify and install a released bundle
    luna reconcile [--dry-run]          merge Syncthing sync-conflict copies of
                                        Steam's localconfig.vdf (newest timestamp wins)
    luna backup [--out DIR]             snapshot Luna user data (playtime ledgers,
                                        presence, HLTB, prefs, artwork key, settings)
    luna restore ARCHIVE [--force]      restore a backup (never overwrites newer
                                        files without --force)
    luna steam status|pin|cache|recover manage the Steam client build: pin the
                                        working build, cache its packages, recover
                                        it after an unwanted update
    luna steam lock [--force] [--relaunch]
                                        roll back to the ubuntu12 client, pin it and
                                        hard-lock updates (kills Steam; package/
                                        read-only, Steam.cfg + steam.cfg)
    luna steam lock --adopt             record an existing read-only lock (pin + cfg block +
                                        read-only package/, packages matching the pin) as
                                        Luna's hard lock; no rollback, nothing killed
    luna steam unlock [--now|--relaunch] undo the hard lock and the pin; --now
                                        launches Steam so it self-updates;
                                        --relaunch closes a running Steam first
                                        and reopens it (used by the in-Steam UI)
    luna fleet add|remove|list [HOST]   hosts managed over SSH (~/.config/luna/fleet.json)
    luna fleet status [--json]          Luna/Steam state and doctor warn/error counts (DOCTOR column)
                                        of this machine and every host
    luna fleet push-cache HOST...       rsync the verified Steam client cache when HOST's differs
    luna fleet install [HOST...] [--only NAME] [--restart-steam] [--force] [--dry-run]
                                        push the release bundle; refuses while Steam runs
    luna refresh [--dry-run]            update Steam to Valve's latest, pin and
                                        cache it, then reinstall Luna on top
                                        (Steam closed; never kills anything)
"""
import argparse
import contextlib
import hashlib
import io
import json
import lzma
import os
import re
import shutil
import stat
import struct
import subprocess
import sys
import tarfile
import random
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
import zlib
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / 'vendor' / 'MANIFEST.json'
BRIDGE_SOURCE = ROOT / 'bridge' / 'bridge.lua'
PATCHER = ROOT / 'scripts' / 'patch-lumen.py'
MARKER = 'SLS-ADDONS-INTEGRATION'

# Installed as one set. The bridge discovers them from the directory and sorts
# them, so this order is only for reporting; "luna" and "compressor" are
# first-party (they live in this repository) while the other two are vendored
# from their own projects.
ADDONS = ('luna', 'sgdb', 'graphics', 'playtime', 'presence', 'compressor')
SOURCED = ('playtime', 'presence')
FIRST_PARTY = ('luna', 'sgdb', 'graphics', 'compressor')

# Files that make up one add-on, as (path in this repo, path under sls-addons/<name>).
ADDON_FILES = {
    'luna': (
        ('plugin/backend/main.lua', 'backend/main.lua'),
        ('plugin/public/agent.js', 'public/agent.js'),
        ('plugin/public/panel.js', 'public/panel.js'),
        ('plugin/plugin.json', 'plugin.json'),
    ),
    'sgdb': (
        ('plugin/backend/main.lua', 'backend/main.lua'),
        ('plugin/plugin.json', 'plugin.json'),
    ),
    'graphics': (
        ('plugin/backend/main.lua', 'backend/main.lua'),
        ('plugin/plugin.json', 'plugin.json'),
    ),
    'playtime': (
        ('plugin/backend/main.lua', 'backend/main.lua'),
        ('plugin/public/agent.js', 'public/agent.js'),
        ('plugin/public/panel.js', 'public/panel.js'),
        ('plugin/plugin.json', 'plugin.json'),
    ),
    'presence': (
        ('plugin/backend/main.lua', 'backend/main.lua'),
        ('plugin/public/agent.js', 'public/agent.js'),
        ('plugin/public/panel.js', 'public/panel.js'),
        ('plugin/plugin.json', 'plugin.json'),
    ),
    'compressor': (
        ('plugin/backend/main.lua', 'backend/main.lua'),
        ('plugin/public/panel.js', 'public/panel.js'),
        ('plugin/plugin.json', 'plugin.json'),
    ),
}

EXIT_OK = 0
EXIT_REFUSED = 2
EXIT_NEEDS_FORCE = 3
EXIT_PATCH = 4


