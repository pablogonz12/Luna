#!/usr/bin/env python3
"""SLS Luna - install the Luna add-ons as one unit on Luna's own plugin host.

By default `luna install` sets up the standalone `luna-host` in
`~/.local/share/Luna`: no Lumen, no LuaTools, no Steam wrapper patch. The
optional Lumen stack stays available behind `--lumen` (Steam pin, then the
pinned Lumen/LuaTools/SLSsteam installer, then Luna on top). Existing Lumen
installs keep their host until an explicit `luna host cutover`.

Each add-on project ships its own installer for people who want only that
add-on; Luna owns the combination:

  * one preflight, one install, one rollback for the whole set;
  * payloads pinned by source commit and verified by sha256, so what is installed
    is exactly what was reviewed (see vendor/MANIFEST.json);
  * fail-closed refusals: Steam running, a Lumen sidecar still alive from a
    previous session, a drifted boot.lua, or an on-disk payload that differs from
    the pinned one without --force;
  * user data is never touched - it lives outside the add-on directories.

Commands:
    luna preflight [--only NAME]        read-only environment and state report
    luna status [--only NAME] [--live] [--json]
                                        installed state and optional live backend health
    luna install [--only NAME] [--force] [--dry-run] [--lumen] [--lock-steam-client]
                                        default: luna-host (standalone); --lumen opts in to the
                                        full Steam-pin + Lumen/LuaTools/SLSsteam pipeline
    luna bootstrap [--lumen] [--lock-steam-client] [--dry-run]
                                        fresh machine; the default installs luna-host only
    luna host status|cutover|rollback    show / switch the plugin host (Luna's luna-host vs. Lumen)
    luna lumen status|update|cancel|restore [--dry-run] [--yes] [--queue]
                                        update the Lumen base (Lumen host only): back up, run the
                                        base installer, re-attach Luna; `restore` undoes it
    luna lumen channels [set stable|beta] [--only NAME]
                                        show or change the per-component update channels
                                        (update-channels.json), written atomically
    luna lumen update --terminal        plan, confirm, close Steam (steam -shutdown) and run the
                                        same update in a terminal window, teed to
                                        ~/.local/share/Luna/state/update-<timestamp>.log
    luna uninstall [--only NAME]
    luna verify [--sources] [--upstream] manifest, payload and optional drift checks
    luna selftest [--json] [--keep-open]
                                        drive the real menu in the running Steam over CDP:
                                        every sidebar page, the read-only status RPCs, the
                                        Store route (report only); never writes, never navigates
    luna doctor [--report [--out FILE]]
                                        print a health check, or write one redacted report file
                                        for sharing (versions, units, journals, last selftest)
    luna vendor [--source-dir DIR]      re-pin payloads from the add-on projects
    luna release [--out DIR] [--publish]  build a bundle tarball (+ sha256) for other machines
    luna update --bundle FILE | --url URL | --latest [--only NAME] [--dry-run]
                                        verify and install a released bundle
    luna update-all [--base-latest] [--yes] [--terminal] [--dry-run]
                                        update Luna's add-ons and re-verify the Lumen base;
                                        never touches the Steam client build
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
    luna steam unlock [--now|--relaunch] undo the hard lock and the pin; --now
                                        launches Steam so it self-updates;
                                        --relaunch closes a running Steam first
                                        and reopens it (used by the in-Steam UI)
    luna refresh [--dry-run]            update Steam to Valve's latest, pin and
                                        cache it, then reinstall Luna on top
                                        (Steam closed; never kills anything)

Exit codes (scripts can rely on these):
    0  success / healthy
    1  corrupt or incomplete state (also: a failed run)
    2  refused by an environment guard (Steam or a Lumen sidecar is running)
    3  installed payloads differ; re-run with --force
    4  the boot.lua patcher refused or failed
    5  another luna command holds the single-writer lock; nothing was changed
    6  `luna status`: payloads differ from the pinned copy (a local edit or
       drift); corruption always reports 1 instead
"""
import argparse
import contextlib
import hashlib
import http.server
import io
import json
import lzma
import os
import re
import shlex
import shutil
import signal
import stat
import struct
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
import zlib
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
# install_bundle runs luna.py from an unpacked bundle, and write_tooling copies
# it to <luna_tools_dir()>/luna.py. Neither place is a checkout, so the support
# tree (patch-lumen.py, bridge, add-on sources) is deployed beside the copy as
# <tools>/runtime and becomes ROOT. Without this an installed copy found no
# patcher and a Lumen update could not re-attach Luna.
_INSTALLED_ROOT = Path(__file__).resolve().parent / 'runtime'
if (not (ROOT / 'scripts' / 'patch-lumen.py').is_file()
        and (_INSTALLED_ROOT / 'scripts' / 'patch-lumen.py').is_file()):
    ROOT = _INSTALLED_ROOT
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
EXIT_CORRUPT = 1
EXIT_REFUSED = 2
EXIT_NEEDS_FORCE = 3
EXIT_PATCH = 4
EXIT_BUSY = 5
EXIT_MODIFIED = 6
EXIT_EPILOG = ('exit codes: 0 ok, 1 corrupt or failed, 2 refused, 3 needs --force, '
               '4 patcher failed, 5 another luna command is running, '
               '6 status: payload differs from the pinned copy (local edit or drift; '
               'pass --strict to report it as 1)')
# Set for a child `install` spawned by an owner that already holds the lock
# (install_bundle, the timer): the lock is ours, a second acquire would refuse.
LOCK_HELD_ENV = 'LUNA_LOCK_HELD'


# ---------------------------------------------------------------------------
# Multi-machine reconcile: Steam's localconfig.vdf is Syncthing-synced so both
# machines share recents, collections and per-game state, but two Steam
# instances writing it concurrently produce sync-conflict copies and
# last-writer-wins clobbering ("gaming mode forgot what desktop mode played").
# Reconcile merges the conflict copies into the live file (max LastPlayed per
# app, union of recent-played entries) instead of picking one file blindly.
# Idempotent: re-running over archived conflicts changes nothing.

_VDF_TOKEN = re.compile(r'"((?:[^"\\]|\\.)*)"|([{}])')
_NONSTEAM_MARKER = 0x02000000


def _vdf_unescape(text):
    return text.replace('\\\\', '\x00').replace('\\"', '"').replace('\x00', '\\')


def _vdf_escape(text):
    return text.replace('\\', '\\\\').replace('"', '\\"')


def parse_vdf(text):
    """Minimal Valve KeyValues parser: quoted strings, braces, last-wins."""
    root = {}
    stack = [root]
    key = None
    for match in _VDF_TOKEN.finditer(text):
        brace = match.group(2)
        if brace == '{':
            child = {}
            if key is not None:
                stack[-1][key] = child
                key = None
            stack.append(child)
        elif brace == '}':
            if len(stack) > 1:
                stack.pop()
            key = None
        else:
            value = _vdf_unescape(match.group(1))
            if key is None:
                key = value
            else:
                stack[-1][key] = value
                key = None
    return root


def render_vdf(node, level=0):
    """Render parsed KeyValues back; Steam rewrites the file itself, so only
    structure and values (not original whitespace) are preserved."""
    lines = []
    pad = '\t' * level
    for key, value in node.items():
        escaped = _vdf_escape(str(key))
        if isinstance(value, dict):
            lines.append('%s"%s"\n%s{\n%s%s}\n'
                         % (pad, escaped, pad, render_vdf(value, level + 1), pad))
        else:
            lines.append('%s"%s"\t\t"%s"\n' % (pad, escaped, _vdf_escape(str(value))))
    return ''.join(lines)


def _steam_section(config):
    """The Software/Valve/Steam mapping inside a parsed localconfig, or {}."""
    try:
        return config['UserLocalConfigStore']['Software']['Valve']['Steam']
    except (KeyError, TypeError):
        return {}


def _decode_recent(blob):
    """RecentLocalPlayedGameIDs hex -> [(kind, id, ts)] or None.

    Entries are (appid, 0, lastplayed) for Steam games and
    (0x02000000, shortcutid, lastplayed) for non-Steam shortcuts.
    """
    try:
        raw = bytes.fromhex(blob)
    except (ValueError, TypeError):
        return None
    if not raw or len(raw) % 12:
        return None
    entries = []
    for offset in range(0, len(raw), 12):
        first, second, third = struct.unpack('<III', raw[offset:offset + 12])
        if first == _NONSTEAM_MARKER:
            entries.append(('shortcut', second, third))
        elif second == 0 and third > 0:
            entries.append(('app', first, third))
        else:
            return None
    return entries


def _encode_recent(entries):
    """Entries back to hex, most recent first (Steam's own order)."""
    ordered = sorted(entries, key=lambda entry: entry[2], reverse=True)
    raw = bytearray()
    for kind, ident, stamp in ordered:
        if kind == 'shortcut':
            raw += struct.pack('<III', _NONSTEAM_MARKER, ident, stamp)
        else:
            raw += struct.pack('<III', ident, 0, stamp)
    return raw.hex()


def _merge_recent(blobs):
    """Union of recent-played entries, newest timestamp wins per game."""
    merged = {}
    for blob in blobs:
        entries = _decode_recent(blob or '')
        if entries is None:
            return None
        for kind, ident, stamp in entries:
            key = (kind, ident)
            if stamp > merged.get(key, 0):
                merged[key] = stamp
    return _encode_recent([(kind, ident, stamp) for (kind, ident), stamp in merged.items()])


def _merge_seen(values):
    """Union of GamepadHomeWhatsNewSeen vecSeenIDs ("appid:timestamp")."""
    merged = {}
    sections = {}
    for value in values:
        try:
            parsed = json.loads(value or '')
            regions = parsed.get('mapSections') or {}
        except (ValueError, AttributeError):
            return None
        if not isinstance(regions, dict):
            return None
        for name, region in regions.items():
            if not isinstance(region, dict):
                continue
            seen = sections.setdefault(name, {})
            for item in region.get('vecSeenIDs') or []:
                try:
                    ident, stamp = str(item).split(':')
                    stamp = int(stamp)
                except ValueError:
                    continue
                if stamp > seen.get(ident, 0):
                    seen[ident] = stamp
    for name, seen in sections.items():
        merged[name] = {'vecSeenIDs': ['%s:%d' % (ident, stamp)
                                       for ident, stamp in sorted(seen.items())]}
    return json.dumps({'version': 1, 'mapSections': merged}, sort_keys=True)


def _steam_user_configs():
    root = home() / '.local/share/Steam/userdata'
    if not root.is_dir():
        return []
    return sorted(path for path in root.iterdir()
                  if (path / 'config' / 'localconfig.vdf').is_file())


def _merge_localconfig(documents):
    """Merge parsed localconfig documents (newest last); returns (doc, notes).

    Recency keys merge by max; every other key comes from the newest document
    that carries it, so collections and settings keep syncing via the file.
    """
    merged = {}
    notes = []
    for key in set().union(*(doc.keys() for doc in documents)):
        if key == 'UserLocalConfigStore':
            continue
        candidates = [doc[key] for doc in documents if key in doc]
        merged[key] = candidates[-1]
    stores = [doc.get('UserLocalConfigStore') for doc in documents]
    merged_store = {}
    for key in set().union(*(s.keys() for s in stores if isinstance(s, dict))):
        if key == 'Software':
            continue
        candidates = [s[key] for s in stores if isinstance(s, dict) and key in s]
        merged_store[key] = candidates[-1]
    softwares = [s.get('Software') for s in stores if isinstance(s, dict)]
    merged_software = {}
    for key in set().union(*(s.keys() for s in softwares if isinstance(s, dict))):
        if key == 'Valve':
            continue
        candidates = [s[key] for s in softwares if isinstance(s, dict) and key in s]
        merged_software[key] = candidates[-1]
    valves = [s.get('Valve') for s in softwares if isinstance(s, dict)]
    merged_valve = {}
    for key in set().union(*(s.keys() for s in valves if isinstance(s, dict))):
        if key == 'Steam':
            continue
        candidates = [s[key] for s in valves if isinstance(s, dict) and key in s]
        merged_valve[key] = candidates[-1]
    steams = [s.get('Steam') for s in valves if isinstance(s, dict)]
    merged_steam = {}
    for key in set().union(*(s.keys() for s in steams if isinstance(s, dict))):
        if key in ('Apps', 'RecentLocalPlayedGameIDs') or key.startswith('GamepadHomeWhatsNewSeen'):
            continue
        candidates = [s[key] for s in steams if isinstance(s, dict) and key in s]
        merged_steam[key] = candidates[-1]
    # Per-app LastPlayed merges by max; other per-app keys from the newest file.
    apps_list = [s.get('Apps') for s in steams if isinstance(s, dict) and isinstance(s.get('Apps'), dict)]
    merged_apps = {}
    for appid in set().union(*(a.keys() for a in apps_list)):
        records = [a[appid] for a in apps_list if appid in a and isinstance(a[appid], dict)]
        if not records:
            continue
        record = dict(records[-1])
        best = 0
        for older in records:
            try:
                stamp = int(older.get('LastPlayed') or 0)
            except (ValueError, TypeError):
                continue
            best = max(best, stamp)
        if best:
            try:
                current = int(record.get('LastPlayed') or 0)
            except (ValueError, TypeError):
                current = 0
            if best != current:
                record['LastPlayed'] = str(best)
                notes.append('app %s LastPlayed -> %d' % (appid, best))
        merged_apps[appid] = record
    if merged_apps:
        merged_steam['Apps'] = merged_apps
    # Recent-played blob: union, newest timestamp wins per game.
    recent_blobs = [s.get('RecentLocalPlayedGameIDs') for s in steams
                    if isinstance(s, dict) and s.get('RecentLocalPlayedGameIDs')]
    if recent_blobs:
        merged_blob = _merge_recent(recent_blobs)
        if merged_blob is not None:
            if not steams or merged_blob != (steams[-1].get('RecentLocalPlayedGameIDs') or ''):
                notes.append('recent-played list merged from %d file(s)' % len(recent_blobs))
            merged_steam['RecentLocalPlayedGameIDs'] = merged_blob
        else:
            merged_steam['RecentLocalPlayedGameIDs'] = steams[-1].get('RecentLocalPlayedGameIDs')
    # Gamepad/Deck seen-state: union per section.
    seen_keys = {key for s in steams if isinstance(s, dict) for key in s
                 if key.startswith('GamepadHomeWhatsNewSeen')}
    for key in sorted(seen_keys):
        values = [s[key] for s in steams if isinstance(s, dict) and s.get(key)]
        merged_seen = _merge_seen(values) if len(values) > 1 else values[0]
        if merged_seen is None:
            merged_seen = values[-1]
        elif values and merged_seen != values[-1]:
            notes.append('%s seen-state merged' % key)
        merged_steam[key] = merged_seen
    merged_valve['Steam'] = merged_steam
    merged_software['Valve'] = merged_valve
    merged_store['Software'] = merged_software
    merged['UserLocalConfigStore'] = merged_store
    return merged, notes


def cmd_reconcile(options):
    """Merge Syncthing sync-conflict copies of localconfig.vdf into the live file.

    Keeps full-file sync (same hours/recents/collections on both machines) but
    replaces last-writer-wins with max-wins for recency. Refuses while Steam or
    the Lumen sidecar runs; consumed conflicts are archived next to the file.
    """
    if steam_running() or lumen_running():
        print('REFUSING to reconcile: Steam or the Lumen sidecar is running. '
              'Close it completely (Steam -> Exit), let Syncthing settle, then retry.')
        return EXIT_REFUSED
    users = _steam_user_configs()
    if not users:
        print('no Steam userdata with localconfig.vdf found')
        return EXIT_OK
    changed = False
    for user in users:
        config = user / 'config'
        live = config / 'localconfig.vdf'
        conflicts = sorted(config.glob('localconfig.sync-conflict-*.vdf'))
        if not conflicts:
            print('%s: no conflicts' % user.name)
            continue
        documents = []
        order = []
        for path in [live] + conflicts:
            try:
                documents.append(parse_vdf(path.read_text(encoding='utf-8', errors='replace')))
                order.append(path)
            except OSError as error:
                print('  WARN: cannot read %s: %s' % (path.name, error))
        if len(documents) < 2:
            print('%s: only one readable copy, nothing to merge' % user.name)
            continue
        # Oldest first so the newest file wins every non-recency key.
        ranked = sorted(zip(order, documents), key=lambda pair: pair[0].stat().st_mtime)
        merged, notes = _merge_localconfig([doc for _, doc in ranked])
        if options.dry_run:
            print('%s: %d conflict(s) would merge:' % (user.name, len(conflicts)))
            for note in notes or ['no recency differences']:
                print('  - %s' % note)
            continue
        stamp = time.strftime('%Y%m%d-%H%M%S')
        archive = config / ('localconfig.reconciled-' + stamp)
        archive.mkdir(parents=True, exist_ok=True)
        tmp = live.with_name('localconfig.vdf.luna-tmp')
        tmp.write_text(render_vdf(merged))
        os.replace(tmp, live)
        for path in conflicts:
            path.replace(archive / path.name)
        changed = True
        print('%s: merged %d conflict(s) into localconfig.vdf (archived in %s)'
              % (user.name, len(conflicts), archive.name))
        for note in notes:
            print('  - %s' % note)
    return EXIT_OK


def home():
    return Path(os.environ.get('HOME') or Path.home())


def lumen_root():
    return Path(os.environ.get('LUMEN_DIR') or (home() / '.local/share/Lumen'))


# Set while an install runs: the host that run targets. Without it addons_dir()
# and active_host() answer for the host that is actually serving Steam.
_TARGET_HOST = None


def serving_host():
    """The host this command serves: an explicit install target, else the active host."""
    return _TARGET_HOST or active_host()


def prefer_luna():
    try:
        data = json.loads(host_state_path().read_text())
    except (OSError, ValueError):
        data = None
    return isinstance(data, dict) and data.get('active') == 'luna'


def lumen_install_present():
    """True when a Moon/Lumen stack exists here, even without a host.json."""
    root = lumen_root()
    return ((root / 'lua' / 'boot.lua').is_file() or (root / 'sls-addons').is_dir()
            or (root / 'luatools').is_dir() or (root / 'sls-luna').is_dir())


def addons_dir():
    return (luna_home() / 'addons') if serving_host() == 'luna' else lumen_root() / 'sls-addons'


def boot_file():
    return ((luna_home() / 'lua' / 'boot.lua') if serving_host() == 'luna'
            else lumen_root() / 'lua' / 'boot.lua')


def data_dir():
    return home() / '.local/share/slssteam-integrations'


def luna_home():
    """Luna's own home: the plugin host, its Lua tree, add-ons and host state."""
    return Path(os.environ.get('LUNA_HOME') or (home() / '.local/share/Luna'))


def host_state_path():
    return luna_home() / 'host.json'


def active_host():
    """'luna' for Luna's own host, 'lumen' for the third-party host.

    host.json is authoritative. Without it, an existing Lumen/Moon installation
    is left exactly where it is (never downgraded silently); a machine with no
    such tree is a fresh standalone install and defaults to luna-host.
    """
    try:
        data = json.loads(host_state_path().read_text())
    except (OSError, ValueError):
        return 'lumen' if lumen_install_present() else 'luna'
    return 'luna' if isinstance(data, dict) and data.get('active') == 'luna' else 'lumen'


def host_label(host=None):
    host = host or active_host()
    return 'luna (standalone)' if host == 'luna' else 'lumen'


def lumen_tools_dir():
    return lumen_root() / 'sls-luna'


def luna_tools_dir():
    """Machine-local Luna tooling: the standalone updater copy and its units.

    While Lumen is the active host it sits under the Lumen directory on purpose:
    that tree is excluded from the user's Syncthing folders, so two machines
    never fight over the copies. After `luna host cutover` it is
    `$LUNA_HOME/state` (also machine-local), matching the backend.
    """
    if serving_host() == 'luna':
        return luna_home() / 'state'
    return lumen_tools_dir()


def machine_id():
    """Stable per-machine id. Machine-local on purpose (never synced): it is
    how one machine tells its own heartbeat apart from the other's."""
    path = luna_tools_dir() / 'machine-id'
    try:
        existing = path.read_text().strip()
        if existing:
            return existing
    except OSError:
        pass
    import uuid
    ident = uuid.uuid4().hex[:12]
    try:
        luna_tools_dir().mkdir(parents=True, exist_ok=True)
        path.write_text(ident + '\n')
    except OSError:
        pass
    return ident


HEARTBEAT_TTL = 2 * 3600
HEARTBEAT_PRUNE = 7 * 24 * 3600


def heartbeat_dir():
    """Synced heartbeats: data_dir *is* shared between machines, so each
    machine's periodic check-in is visible to the other."""
    return data_dir() / 'heartbeats'


def write_heartbeat():
    """Record this machine's presence for the other machine to see."""
    try:
        directory = heartbeat_dir()
        directory.mkdir(parents=True, exist_ok=True)
        entry = {'machine': machine_id(),
                 'at': int(time.time()),
                 'steamRunning': bool(steam_running()),
                 'luna': installed_version()}
        (directory / (entry['machine'] + '.json')).write_text(json.dumps(entry) + '\n')
        now = time.time()
        for child in directory.glob('*.json'):
            try:
                if now - child.stat().st_mtime > HEARTBEAT_PRUNE:
                    child.unlink()
            except OSError:
                pass
    except OSError:
        pass


def read_peers(max_age=HEARTBEAT_TTL):
    """Heartbeats from *other* machines fresh enough to matter."""
    me = machine_id()
    peers = []
    try:
        files = sorted(heartbeat_dir().glob('*.json'))
    except OSError:
        return peers
    now = time.time()
    for path in files:
        if path.stem == me:
            continue
        try:
            entry = json.loads(path.read_text())
            at = int(entry.get('at') or 0)
        except (OSError, ValueError, TypeError, AttributeError):
            continue
        if now - at > max_age:
            continue
        peers.append(entry)
    return peers


def read_json_file(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def write_json_file(path, data):
    """Atomic: the Lua backend reads these files while Python writes them."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.' + path.name + '.', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w') as handle:
            handle.write(json.dumps(data, indent=2, sort_keys=True) + '\n')
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return path


# A hung gh must not hang its caller: the Health page waits on `check`, and a
# oneshot timer unit that never exits is never started again.
GH_TIMEOUT = 30
GH_DOWNLOAD_TIMEOUT = 600


def find_gh():
    """The gh CLI, from PATH or the usual user locations."""
    found = shutil.which('gh')
    if found:
        return found
    for candidate in (home() / '.local/bin/gh', Path('/usr/local/bin/gh'), Path('/usr/bin/gh')):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def installed_version():
    """The luna add-on version actually on disk.

    Read from the installed payload first, so the standalone updater copy (which
    has no repository around it) reports the truth too.
    """
    for candidate in (addons_dir() / 'luna' / 'plugin.json',
                      payload_path('luna', 'plugin/plugin.json')):
        try:
            return str(json.loads(candidate.read_text()).get('version', '?'))
        except (OSError, ValueError):
            continue
    return '?'


def version_key(text):
    """Sortable tuple for tags like v0.14.3 or 0.14.3-rc1 (best effort)."""
    numbers = []
    for part in str(text or '').strip().lstrip('v').replace('-', '.').split('.'):
        digits = ''.join(ch for ch in part if ch.isdigit())
        numbers.append(int(digits) if digits else 0)
    while len(numbers) < 3:
        numbers.append(0)
    return tuple(numbers[:3])


def payload_path(addon, source_rel):
    """Where a payload file lives in this repo (mirrors the add-on project)."""
    return ROOT / 'addons' / addon / source_rel


def sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def process_running(name):
    """True when a process with this exact name belongs to this user."""
    try:
        result = subprocess.run(['pgrep', '-u', str(os.getuid()), '-x', name],
                                capture_output=True, text=True)
    except FileNotFoundError:
        return False
    # pgrep's contract is the exit status; it prints PIDs but the status is what
    # says whether anything matched.
    return result.returncode == 0


def load_manifest():
    if not MANIFEST_PATH.is_file():
        raise SystemExit('FAIL: %s is missing; run scripts/vendor-update.sh' % MANIFEST_PATH)
    return json.loads(MANIFEST_PATH.read_text())


def addon_version(name):
    payload = payload_path(name, 'plugin/plugin.json')
    if not payload.is_file():
        return '?'
    try:
        return json.loads(payload.read_text()).get('version', '?')
    except ValueError:
        return '?'


# --------------------------------------------------------------------------- refusals

def lumen_running():
    """The single answer to "is Lumen up?".

    Every refusal, doctor line and install decision goes through this helper so
    a pgrep result and a unit state can never tell two different stories.
    """
    return bool(process_running('lumen'))


def steam_running():
    """The single answer to "is Steam up?" (client or its webhelper)."""
    return bool(process_running('steam') or process_running('steamwebhelper'))


def luna_host_running():
    """True while a luna-host helper serves the menu (host cutover active)."""
    return bool(process_running('luna-host') or process_running('luna_host'))


def stop_lumen_sidecar(wait=10):
    """Stop this user's lingering `lumen` sidecar (pkill -u uid -x lumen).

    Only ever called once Steam itself is closed: the sidecar outlives Steam and
    would keep the old boot.lua / client in memory. Returns True when none is
    left running.
    """
    if not lumen_running():
        return True
    try:
        subprocess.run(['pkill', '-u', str(os.getuid()), '-x', 'lumen'], capture_output=True)
    except FileNotFoundError:
        return not lumen_running()
    for _ in range(max(1, int(wait * 4))):
        if not lumen_running():
            return True
        time.sleep(0.25)
    return not lumen_running()


def refuse_reasons(for_install, stop_sidecar=False):
    """Environment problems that must stop an install or uninstall.

    With stop_sidecar a lingering Lumen sidecar is stopped (Steam is closed),
    instead of being reported.
    """
    reasons = []
    root = lumen_root()
    host = serving_host()
    if host == 'lumen':
        if not (root / 'lua' / 'boot.lua').is_file():
            reasons.append('Lumen lua/boot.lua not found under %s (is the LuaTools-Moon stack installed?)' % root)
        if not (root / 'luatools' / 'backend' / 'main.lua').is_file():
            reasons.append('LuaTools-Moon backend not found under %s' % root)
    if steam_running():
        reasons.append('Steam is running. Quit Steam (Steam → Exit) and run the command again; '
                       'the %s host is not changed while Steam is up.' % host_label(host))
    # With the luna-host cutover the menu is served from Luna's tree, so an
    # add-on write aimed at Lumen is invisible until the two are mirrored again.
    if active_host() == 'luna' and luna_host_running() and not steam_running():
        print('host      : luna-host is serving; payloads are mirrored after this run')
    # A sidecar from an earlier session keeps the old boot.lua in memory, which
    # makes a successful install look ineffective. Plain install reports it;
    # lock/unlock/recover/refresh stop it once Steam is closed.
    if (stop_sidecar and not (steam_running())
            and lumen_running()):
        if stop_lumen_sidecar():
            print('lumen     : stopped a lingering sidecar from an earlier session')
    if host == 'lumen' and lumen_running():
        reasons.append('A Lumen sidecar is still running (pid owner: %s). Stop it first:\n'
                       '    pkill -u $(id -u) -x lumen' % os.getuid())
    return reasons


def patcher(mode, *extra):
    args = [sys.executable, str(PATCHER), mode, '--lumen', str(lumen_root())]
    return subprocess.run(args + list(extra), capture_output=True, text=True)


# --------------------------------------------------------------------------- planning

def plan(targets):
    """What would change on disk: (addon, label, source, dest, state)."""
    rows = []
    bridge = addons_dir() / 'bridge.lua'
    rows.append(('shared', 'bridge.lua', BRIDGE_SOURCE, bridge, file_state(BRIDGE_SOURCE, bridge)))
    for name in targets:
        for source_rel, dest_rel in ADDON_FILES[name]:
            source = payload_path(name, source_rel)
            dest = addons_dir() / name / dest_rel
            rows.append((name, dest_rel, source, dest, file_state(source, dest)))
    return rows


def file_state(source, dest):
    if not source.is_file():
        return 'missing-source'
    if not dest.is_file():
        return 'new'
    if sha256(source) == sha256(dest):
        return 'same'
    return 'differs'


def selected_addons(options):
    if options.only:
        for name in options.only:
            if name not in ADDONS:
                raise SystemExit('FAIL: unknown add-on %r (known: %s)' % (name, ', '.join(ADDONS)))
        return tuple(name for name in ADDONS if name in options.only)
    return tuple(ADDONS)


def installed_addons():
    return tuple(name for name in ADDONS if (addons_dir() / name).is_dir())


def component_inventory():
    """Read-only inventory of the base stack and optional external tools."""
    home_dir = home()
    root = lumen_root()
    moon_wrapper = home_dir / '.local/share/SLSsteam/path/steam'
    lumen_boot = root / 'lua' / 'boot.lua'
    luatools_backend = root / 'luatools' / 'backend' / 'main.lua'

    cloud_dir = home_dir / '.local/share/CloudRedirect'
    cloud_files = (cloud_dir / 'cloud_redirect.so', cloud_dir / 'cloud_redirect_cli')
    cloud_present = tuple(path for path in cloud_files if path.is_file())
    if len(cloud_present) == len(cloud_files):
        cloud_state = 'present'
    elif cloud_present:
        cloud_state = 'partial'
    else:
        cloud_state = 'not detected'
    cloud_detail = ', '.join(str(path) for path in cloud_present) or str(cloud_dir)

    decky_services = home_dir / 'homebrew' / 'services'
    decky_loader = decky_services / 'PluginLoader'
    decky_version = decky_services / '.loader.version'
    if decky_loader.is_file() and os.access(decky_loader, os.X_OK):
        decky_state = 'present'
        try:
            version = decky_version.read_text().strip() if decky_version.is_file() else ''
        except OSError:
            version = ''
        decky_detail = '%s%s (service state not checked)' % (
            decky_loader, (' (version ' + version + ')') if version else '')
    elif decky_services.exists():
        decky_state = 'partial'
        decky_detail = str(decky_services)
    else:
        decky_state = 'not detected'
        decky_detail = str(decky_services)

    compressor_candidates = []
    configured = os.environ.get('SLS_COMPRESSOR_BIN')
    if configured:
        compressor_candidates.append(Path(configured).expanduser())
    compressor_candidates.extend((
        home_dir / '.local/bin/btrfs-game-compressor',
        Path('/usr/local/bin/btrfs-game-compressor'),
        Path('/usr/bin/btrfs-game-compressor'),
        home_dir / 'Documents/Scripts/btrfs-game-compressor/btrfs-game-compressor',
    ))
    compressor = next((path for path in compressor_candidates
                       if path.is_file() and os.access(path, os.X_OK)), None)

    return (
        ('slsteam-moon wrapper', 'present' if moon_wrapper.is_file() else 'not detected', str(moon_wrapper)),
        ('Lumen runtime', 'present' if lumen_boot.is_file()
         else ('not installed' if serving_host() == 'luna' else 'not detected'), str(lumen_boot)),
        ('LuaTools backend', 'present' if luatools_backend.is_file()
         else ('not installed' if serving_host() == 'luna' else 'not detected'), str(luatools_backend)),
        ('CloudRedirect', cloud_state, cloud_detail),
        ('Decky Loader', decky_state, decky_detail),
        ('Compressor CLI', 'present' if compressor else 'not detected', str(compressor or compressor_candidates[0])),
    )


def print_component_inventory():
    print('components:')
    for name, state, detail in component_inventory():
        print('  %-20s %-12s %s' % (name, state, detail))


# --------------------------------------------------------------------------- apply

def install_marker_path():
    return luna_tools_dir() / 'install-in-progress.json'


def write_install_marker(targets, force=False):
    """Record that payloads are being written, before the first byte changes.

    A kill -9 (or a power cut) leaves this file behind, so `luna status` and
    `luna doctor` can say "the last install was interrupted" instead of
    pretending a half-written tree is installed.
    """
    path = install_marker_path()
    try:
        write_json_file(path, {'pid': os.getpid(), 'at': int(time.time()),
                               'addons': list(targets), 'force': bool(force)})
    except OSError as error:
        print('  WARN: could not write the install marker: %s' % error, file=sys.stderr)
    return path


def clear_install_marker():
    try:
        install_marker_path().unlink()
    except OSError:
        pass


def archive_previous(differing, reason='undo'):
    """Copy every payload --force is about to overwrite; returns the root dir.

    The bytes stay under <tools>/undo/<timestamp>/ with the same relative
    layout, so a local hotfix can always be recovered by hand.
    """
    if not differing:
        return None
    stamp = time.strftime('%Y%m%d-%H%M%S')
    root = luna_tools_dir() / reason / stamp
    suffix = 0
    while root.exists():
        suffix += 1
        root = luna_tools_dir() / reason / ('%s-%d' % (stamp, suffix))
    for addon, label, source, dest, state in differing:
        target = root / addon / label
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(dest, target)
    return root


class Apply:
    """Write files, remembering how to put the previous state back."""

    def __init__(self):
        self.undo = []
        self.did_patch = False

    def write(self, source, dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.is_file():
            self.undo.append(('restore', dest, dest.read_bytes()))
        else:
            self.undo.append(('remove', dest, None))
        data = source.read_bytes()
        # Per-run unique name: two writers can never share one temp file.
        fd, temporary = tempfile.mkstemp(prefix='.%s.' % dest.name, suffix='.luna-tmp',
                                         dir=dest.parent)
        try:
            with os.fdopen(fd, 'wb') as out:
                out.write(data)
            os.chmod(temporary, 0o644)
            os.replace(temporary, dest)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def mkdir(self, path):
        if not path.is_dir():
            path.mkdir(parents=True, exist_ok=True)
            self.undo.append(('rmdir', path, None))

    def rollback(self):
        """Undo every recorded step; returns how many could not be undone."""
        failures = 0
        for action, target, payload in reversed(self.undo):
            try:
                if action == 'restore':
                    target.write_bytes(payload)
                elif action == 'remove':
                    if target.exists():
                        target.unlink()
                elif action == 'rmdir':
                    shutil.rmtree(target, ignore_errors=True)
            except OSError as error:
                failures += 1
                print('  WARN: rollback could not restore %s: %s' % (target, error), file=sys.stderr)
        if self.did_patch:
            result = patcher('uninstall')
            if result.returncode != 0:
                failures += 1
                print('  WARN: could not revert the boot.lua patch: %s'
                      % (result.stderr.strip() or result.stdout.strip()), file=sys.stderr)
        return failures


# --------------------------------------------------------------------------- commands

def cmd_preflight(options):
    host = active_host()
    print('SLS Luna %s' % (load_manifest().get('generated', '?')[:10]))
    print('host       : %s' % host_label(host))
    print('host home  : %s' % (luna_home() if host == 'luna' else lumen_root()))
    if host == 'lumen':
        print('lumen root : %s' % lumen_root())
    print('add-ons    : %s' % ', '.join('%s %s' % (name, addon_version(name)) for name in ADDONS))
    steam_root = home() / '.local/share/Steam'
    resolved = Path(os.path.realpath(home() / '.steam/steam')) if (home() / '.steam/steam').exists() else None
    print('steam root : %s' % (resolved if resolved else steam_root))
    manifests = 0
    plugdir = steam_root / 'config' / 'stplug-in'
    if plugdir.is_dir():
        manifests = sum(1 for entry in plugdir.glob('*.lua') if entry.stem.isdigit())
    print('manifests  : %d' % manifests)
    print('installed  : %s' % (', '.join(installed_addons()) or 'none'))
    print_component_inventory()
    boot = boot_file()
    if host == 'luna':
        print('host run   : %s (%s)'
              % ('installed' if (luna_home() / 'bin/luna-host').is_file() else 'not installed',
                 luna_home() / 'bin/luna-host'))
        print('boot.lua   : luna-host runtime (no Lumen patch)')
    elif boot.is_file():
        content = boot.read_text(errors='replace')
        print('boot.lua   : %s, %d marker(s), backup %s'
              % ('patched' if MARKER in content else 'unpatched', content.count(MARKER),
                 'present' if boot.with_name('boot.lua.sls-addons.original').is_file() else 'absent'))
    if host == 'lumen':
        check = patcher('check')
        print('anchors    : %s' % (check.stdout.strip() or check.stderr.strip() or 'no output'))
    else:
        print('anchors    : not applicable (Lumen is not installed)')
    data = data_dir()
    if data.is_dir():
        ledgers = sorted(p.name for p in (data / 'playtime').glob('*.json')) if (data / 'playtime').is_dir() else []
        print('data       : %s (playtime ledgers: %s; presence: %s)'
              % (data, ', '.join(ledgers) or 'none',
                 'present' if (data / 'presence.json').is_file() else 'absent'))
    else:
        print('data       : %s (absent - no playtime recorded yet)' % data)
    conflicts = [p for user in _steam_user_configs()
                 for p in (user / 'config').glob('localconfig.sync-conflict-*.vdf')]
    if conflicts:
        print('sync       : %d localconfig conflict(s) waiting - close Steam on both machines, '
              'let Syncthing settle, then run: luna reconcile' % len(conflicts))
    else:
        print('sync       : no localconfig conflicts')
    for peer in read_peers():
        ago = int(time.time() - int(peer.get('at') or 0))
        print('peer       : %s seen %dm ago, Steam %s, Luna %s%s'
              % (peer.get('machine', '?'), ago // 60,
                 'running - avoid Steam on both machines at once'
                 if peer.get('steamRunning') else 'closed',
                 peer.get('luna', '?'),
                 '' if peer.get('steamRunning') else ''))
    if not getattr(options, 'no_heal', False):
        # Third-party installers (LuaTools, Auto-fix) delete steam.cfg: put the block back.
        try:
            print_heal(heal_steam_lock(), 'heal       ')
        except OSError as error:
            print('heal       : WARN: could not heal: %s' % error)
    drift = steam_drift_note()
    warning = launchopts_warning()
    if warning:
        print('launchopts : WARN: %s' % warning)
    if drift:
        print('steam      : %s' % drift)
    else:
        state = steam_state()
        print('steam      : client %s%s, updates %s'
              % (state['build'] or '?',
                 (', pinned %s' % state['pinned']) if state['pinned'] else ', not pinned',
                 'blocked' if state['updatesBlocked'] else 'allowed'))
    reasons = refuse_reasons(for_install=True)
    if reasons:
        print('\nREFUSALS (install/uninstall would stop):')
        for reason in reasons:
            print('  - %s' % reason)
        return EXIT_REFUSED
    print('\nREADY: nothing here was modified.')
    return EXIT_OK


def bridge_heartbeat_path():
    return luna_state_dir() / 'bridge-heartbeat.json'


BRIDGE_ATTACH_GRACE = 180  # seconds Steam gets to load the bridge after the host started


def _bridge_stamp(when):
    return time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(when)) if when else 'unknown time'


def _bridge_age(seconds):
    seconds = max(0, int(seconds))
    if seconds < 60:
        return '%ds' % seconds
    if seconds < 3600:
        return '%dm' % (seconds // 60)
    return '%dh' % (seconds // 3600)


def bridge_status():
    """(level, lines): what the bridge's heartbeat says, against whether the host runs now.

    The bridge stamps `time`/`pid` when the host loads it (at boot, before Steam
    necessarily exists) and `attachedAt` - plus a refreshed `lastSeen` - the first
    time Steam calls one of its add-ons, which is the only moment the bridge can
    observe that it is really serving a live session. So three states:
      - host started, not attached yet (Steam closed or just starting): not a problem
      - attached, with the age of that attach
      - stale: Steam and the host both run and nothing attached after the grace
        period, which is a problem and comes with the repair command
    A heartbeat written by an older bridge has no `attachedAt`; it is read exactly
    as before (an alive pid means attached), so the two versions never break each
    other. Level is 'ok', 'warn' or 'info'.
    """
    if active_host() == 'luna':
        if luna_host_running():
            return 'ok', ['Luna host is running; it loads add-ons directly (no Lumen bridge heartbeat).',
                          'Run luna selftest to check Steam attachment and add-on RPCs.']
        return 'info', ['Luna host is not running; it loads add-ons directly (no Lumen bridge heartbeat).']
    data = read_json_file(bridge_heartbeat_path())
    data = data if isinstance(data, dict) else {}
    now = int(time.time())
    when = int(data.get('time') or 0)
    stamp = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(when)) if when else 'unknown time'
    pid = int(data.get('pid') or 0)
    host_alive = bool(pid) and pid_alive(pid)
    attached = int(data.get('attachedAt') or 0)
    new_format = int(data.get('format') or 0) >= 2
    if data and host_alive and not new_format:
        attached = when  # an older bridge only knows when it loaded
    last_seen = int(data.get('lastSeen') or 0)
    host = active_host()
    running = luna_host_running() if host == 'luna' else lumen_running()
    steam = steam_running()
    host_name = 'Luna host' if host == 'luna' else 'Lumen'
    failed = [item for item in data.get('failed') or [] if isinstance(item, dict)]
    lines, level = [], 'ok'
    if attached and host_alive:
        lines.append('attached: %d add-ons, loaded at %s (%s ago)'
                     % (len(data.get('addons') or []), _bridge_stamp(attached), _bridge_age(now - attached)))
        if last_seen > attached:
            lines.append('          last RPC %s ago' % _bridge_age(now - last_seen))
    elif data and host_alive and steam and when and (now - when) > BRIDGE_ATTACH_GRACE:
        level = 'warn'
        lines.append('WARN: %s started at %s and Steam is running, but the bridge has not attached '
                     'after %s: Steam is not serving Luna'
                     % (host_name, stamp, _bridge_age(now - when)))
        lines.append('      repair: luna hook repair, then restart Steam')
    elif data and host_alive:
        lines.append('host started at %s, not attached yet (%s): that is not a problem'
                     % (stamp, 'Steam is starting' if steam else 'Steam is closed'))
    elif running:
        level = 'warn'
        lines.append('WARN: %s is running but the bridge has not loaded since Steam started%s'
                     % (host_name, ' (last heartbeat %s is from a process that is gone)' % stamp if data
                        else ' (no heartbeat file yet)'))
        lines.append('      repair: luna hook repair, then restart Steam')
    elif data:
        level = 'info'
        lines.append('not attached now (%s is not running); last load: %d add-ons at %s'
                     % (host_name, len(data.get('addons') or []), stamp))
    else:
        level = 'info'
        lines.append('no bridge heartbeat yet (it appears the first time Steam loads Luna)')
    for item in failed:
        level = 'warn'
        lines.append('WARN: add-on %s did not load: %s' % (item.get('name') or '?', item.get('error') or 'unknown error'))
    return level, lines


def pid_alive(pid):
    try:
        os.kill(int(pid), 0)
    except OSError:
        return False
    return True


def process_pids(name):
    """PIDs belonging to this user, using the same exact-name lookup as process_running."""
    try:
        result = subprocess.run(['pgrep', '-u', str(os.getuid()), '-x', name],
                                capture_output=True, text=True)
        return [int(pid) for pid in result.stdout.split() if pid.isdigit()]
    except OSError:
        return []


def sidecar_freshness(host, installed):
    names = ('luna-host', 'luna_host') if host == 'luna' else ('lumen',)
    pids = sorted({pid for name in names for pid in process_pids(name)})
    result = {'pids': pids, 'stale': None, 'message': 'host process start time unavailable'}
    try:
        boot_time = next(int(line.split()[1]) for line in Path('/proc/stat').read_text().splitlines()
                         if line.startswith('btime '))
        ticks = os.sysconf('SC_CLK_TCK')
        starts = [_proc_start(pid) for pid in pids]
        if not starts or not all(starts):
            return result
        paths = [boot_file()] + [row[3] for row in plan(installed)]
        mtimes = [path.stat().st_mtime for path in paths if path.is_file()]
        if not mtimes:
            result['message'] = 'installed payload timestamps unavailable'
            return result
        result.update(started_at=min(boot_time + start / ticks for start in starts),
                      newest_payload_at=max(mtimes))
        result['stale'] = result['started_at'] < result['newest_payload_at']
        result['message'] = ('host predates installed files; restart needed to load them'
                             if result['stale'] else 'host started after installed files')
    except (OSError, ValueError, StopIteration):
        pass
    return result


def live_health(reply):
    """Keep health indicators, not account data, game lists or logs."""
    fields = ('success', 'ok', 'healthy', 'error', 'reason', 'enabled', 'running',
              'publishing', 'accountMismatch', 'identityAmbiguous', 'helpersPaused',
              'helperPauseReason', 'recovered', 'recoveredReason', 'found', 'failed')
    health = {key: reply[key] for key in fields
              if key in reply and isinstance(reply[key], (bool, int, float, str))}
    for section in ('health', 'runtime', 'discord', 'session', 'tool'):
        value = reply.get(section)
        if isinstance(value, dict):
            health[section] = {key: value[key] for key in fields
                               if key in value and isinstance(value[key], (bool, int, float, str))}
    return health


def live_status(targets):
    """Observe the host and call only the selftest's allowlisted status RPCs."""
    host = active_host()
    installed = installed_addons()
    heartbeat = read_json_file(bridge_heartbeat_path()) or {}
    result = {'host': host, 'steam_running': steam_running(),
              'sidecar': sidecar_freshness(host, installed),
              'bridge': {}, 'addons': {}, 'message': ''}
    rpc_names = {'luna': 'SlsLunaInfo', 'sgdb': 'SlsSgdbGet',
                 'graphics': 'SlsGraphicsGet', 'playtime': 'SlsPlaytimeGet',
                 'presence': 'SlsPresenceGet', 'compressor': 'SlsCompressorGet'}
    for name in targets:
        result['addons'][name] = {'installed': name in installed, 'answered': None,
                                  'health': None}
    cdp = None
    if result['steam_running']:
        selftest = _selftest_module()
        if selftest is None:
            result['message'] = 'live status unavailable: selftest support is missing'
            result['bridge'] = {'marker_present': MARKER in (boot_file().read_text(errors='replace')
                                        if boot_file().is_file() else ''),
                                'attached': None, 'addon_count': None}
            return result
        try:
            port = selftest.cef_port(prefer=(host,))
            target = selftest.pick_target(selftest.list_targets(port))
            if not target or not selftest._is_websocket(target):
                raise selftest.Unreachable('Steam Library CDP target unavailable')
            cdp = selftest.CDP(target['webSocketDebuggerUrl']).connect()
            allowed_rpcs = dict(selftest.READONLY_RPCS)
            for name, state in result['addons'].items():
                if not state['installed']:
                    continue
                rpc = rpc_names[name]
                args = allowed_rpcs[rpc]
                allowed, reason = selftest._is_readonly_call(rpc, args)
                if not allowed:
                    state.update(answered=False, error='refused: ' + reason)
                    continue
                try:
                    answer = selftest._rpc_call(cdp, rpc, args)
                    reply = answer.get('reply')
                    state.update(answered=bool(answer.get('ok') and isinstance(reply, dict)),
                                 health=live_health(reply) if isinstance(reply, dict) else None)
                    if not state['answered']:
                        state['error'] = str(answer.get('error') or 'invalid or missing reply')
                except (selftest.CDPError, selftest.Unreachable, OSError) as error:
                    state.update(answered=False, error=str(error))
            result['message'] = 'live backend probes complete'
        except (selftest.CDPError, selftest.Unreachable, OSError) as error:
            result['message'] = 'live status unavailable: %s' % error
        finally:
            if cdp:
                cdp.close()
    else:
        result['message'] = 'Steam is closed; live backend health is unavailable'
    # Re-read after probing: the first RPC can establish attachment.
    heartbeat = read_json_file(bridge_heartbeat_path()) or heartbeat
    alive = bool(heartbeat.get('pid') and pid_alive(heartbeat['pid']))
    attached = (bool(heartbeat.get('attachedAt')) if int(heartbeat.get('format') or 0) >= 2
                else bool(heartbeat.get('time')))
    level, lines = bridge_status()
    result['bridge'] = {'marker_present': MARKER in (boot_file().read_text(errors='replace')
                                                        if boot_file().is_file() else ''),
                        'attached': bool(alive and attached) if host == 'lumen' else None,
                        'addon_count': len(heartbeat.get('addons') or []) if alive and host == 'lumen' else None,
                        'level': level, 'details': lines}
    return result


def live_addon_summary(state):
    """Return a severity and plain-language summary of known health fields."""
    if not state.get('installed'):
        return 'warn', 'not installed'
    if state.get('answered') is None:
        return 'warn', 'backend health unavailable'
    if not state['answered']:
        error = ' '.join(str(state.get('error') or 'unknown error').split())
        return 'PROBLEM', 'backend did not answer (%s)' % error
    health = state.get('health') or {}
    runtime = health.get('runtime') or {}
    discord = health.get('discord') or {}
    tool = health.get('tool') or {}
    facts = ['backend answered']
    severity = 'ok'
    if health.get('success') is False or health.get('ok') is False:
        severity = 'PROBLEM'
        facts.append('backend reports failure%s' %
                     (': ' + ' '.join(str(health['error']).split()) if health.get('error') else ''))
    elif health.get('error'):
        severity = 'warn'
        facts.append('backend error: ' + ' '.join(str(health['error']).split()))
    if runtime.get('healthy') is True:
        facts.append('runtime healthy')
    elif runtime.get('healthy') is False:
        severity = 'PROBLEM'
        facts.append('runtime unhealthy')
    if runtime.get('error'):
        severity = 'PROBLEM'
        facts.append('runtime error: ' + ' '.join(str(runtime['error']).split()))
    if runtime.get('accountMismatch'):
        severity = 'PROBLEM'
        facts.append('account mismatch')
    if runtime.get('helpersPaused') or runtime.get('helperPauseReason'):
        if severity == 'ok':
            severity = 'warn'
        reason = runtime.get('helperPauseReason')
        facts.append('helpers paused%s' % (' (' + ' '.join(str(reason).split()) + ')' if reason else ''))
    if health.get('recovered') or health.get('recoveredReason'):
        reason = health.get('recoveredReason')
        facts.append('recovered%s' % (' (' + ' '.join(str(reason).split()) + ')' if reason else ''))
    if discord.get('publishing') is True:
        facts.append('Discord publishing')
    elif discord.get('publishing') is False:
        if severity == 'ok':
            severity = 'warn'
        reason = str(discord.get('reason') or '').replace('-', ' ')
        facts.append('Discord not publishing%s' % (' (' + ' '.join(reason.split()) + ')' if reason else ''))
    if tool.get('found') is True:
        facts.append('compressor tool found')
    elif tool.get('found') is False:
        severity = 'PROBLEM'
        facts.append('compressor tool not found')
    return severity, '; '.join(facts)


def live_verdict(live):
    summaries = {name: live_addon_summary(state) for name, state in live['addons'].items()}
    problems = [name for name, (severity, _) in summaries.items() if severity == 'PROBLEM']
    warnings = [name for name, (severity, _) in summaries.items() if severity == 'warn']
    states = live['addons'].values()
    answering = sum(state.get('answered') is True for state in states)
    installed = sum(bool(state.get('installed')) for state in live['addons'].values())
    if problems:
        headline = 'look at ' + ', '.join(problems)
    elif warnings:
        headline = '%d of %d installed add-ons answering; check %s' % (answering, installed, ', '.join(warnings))
    else:
        headline = 'all %d add-ons answering' % answering
    stale = live['sidecar'].get('stale')
    restart = ('restart Steam to load the installed files' if stale is True else
               'restart not needed' if stale is False else 'restart need unknown')
    return summaries, '%s; %s' % (headline, restart), bool(problems or stale is True)


def cmd_status(options):
    if getattr(options, 'live', False) or getattr(options, 'json', False):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = cmd_status(argparse.Namespace(only=options.only,
                                                  strict=getattr(options, 'strict', False)))
        live = live_status(selected_addons(options)) if getattr(options, 'live', False) else None
        if live is not None:
            summaries, verdict, failed = live_verdict(live)
            if failed:
                code = EXIT_CORRUPT
        if getattr(options, 'json', False):
            print(json.dumps({'status': output.getvalue().splitlines(), 'live': live,
                              'exit_code': code}, sort_keys=True))
        else:
            print(output.getvalue(), end='')
            print('live       : %s' % live['message'])
            print('sidecar    : %s' % live['sidecar']['message'])
            print('live bridge: marker=%s attached=%s add-ons=%s'
                  % (live['bridge']['marker_present'], live['bridge']['attached'],
                     live['bridge']['addon_count']))
            for name, state in live['addons'].items():
                severity, detail = summaries[name]
                print('live %-10s: %s - %s' % (name, severity, detail))
            print('live verdict: %s' % verdict)
        return code
    targets = selected_addons(options)
    manifest = load_manifest()
    installed = installed_addons()
    host = active_host()
    boot = boot_file()
    content = boot.read_text(errors='replace') if boot.is_file() else ''
    patched = MARKER in content
    print('host       : %s' % host_label(host))
    if host == 'luna':
        print('boot.lua   : luna-host runtime (%s)'
              % ('present' if (luna_home() / 'bin/luna-host').is_file() else 'binary missing'))
    else:
        print('boot.lua   : %s (marker x%d, backup %s)'
              % ('patched' if patched else 'unpatched', content.count(MARKER),
                 'present' if boot.with_name('boot.lua.sls-addons.original').is_file() else 'absent'))
    print('processes  : steam=%s host=%s lumen=%s'
          % (steam_running(), luna_host_running(), lumen_running()))
    for line in bridge_status()[1]:
        print('bridge     : %s' % line)
    print_component_inventory()
    interrupted = read_json_file(install_marker_path()) or {}
    if interrupted:
        print('note       : last install was interrupted (pid %s, started %s); run: luna install --force'
              % (interrupted.get('pid') or '?',
                 time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(int(interrupted.get('at') or 0)))
                 if interrupted.get('at') else 'unknown time'))
    rows = plan(targets)
    print('%-9s %-22s %s' % ('add-on', 'file', 'state'))
    degraded = bool(interrupted)
    modified = False
    for addon, label, source, dest, state in rows:
        print('%-9s %-22s %s' % (addon, label, state))
        if state == 'differs':
            # Somebody (or another tool) changed an installed payload. That is
            # a local edit or drift, not corruption: its own exit code, so a
            # script can tell "review this" from "this tree is broken".
            modified = True
        if state == 'missing-source':
            degraded = True
        if state == 'new' and addon in installed:
            # The directory claims the add-on is present but an expected
            # file is missing: that is corruption, not a fresh install.
            degraded = True
    for name in targets:
        if name not in installed:
            print('note       : %s is not installed' % name)
    if installed and not patched and host == 'lumen':
        print('note       : add-ons are installed but boot.lua is not patched; '
              'run scripts/install-local.sh')
        degraded = True
    print('data       : %s' % ('present' if data_dir().is_dir() else 'absent'))
    warning = launchopts_warning()
    if warning:
        print('launchopts : WARN: %s' % warning)
    print('manifest   : %d add-on(s) pinned from %s'
          % (len(manifest.get('addons', {})), ', '.join(
              sorted({entry.get('source_repo', '?') for entry in manifest.get('addons', {}).values()}))))
    if degraded:
        return EXIT_CORRUPT
    if modified:
        if getattr(options, 'strict', False):
            return EXIT_CORRUPT
        return EXIT_MODIFIED
    return EXIT_OK


def write_install_snapshot():
    """Record what this installer put on disk, for the Health page.

    Versions and sources identify the release; file hashes let the Health
    page report drift without a checkout. The full pins stay in
    vendor/MANIFEST.json (and in `luna verify`).
    """
    manifest = load_manifest()
    files = {}
    for name in ADDONS:
        entry = (manifest.get('addons', {}).get(name) or {}).get('files', {})
        hashes = {}
        for rel, meta in entry.items():
            path = payload_path(name, meta.get('source_path', ''))
            try:
                hashes[rel] = sha256(path) if path.is_file() else ''
            except OSError:
                hashes[rel] = ''
        files[name] = hashes
    snapshot = {
        'generated': manifest.get('generated', ''),
        'installedAt': int(time.time()),
        'source': repo_revision(),
        'versions': {name: addon_version(name) for name in ADDONS},
        'sources': {name: str((manifest.get('addons', {}).get(name) or {})
                              .get('source_commit', ''))[:12] for name in ADDONS},
        'files': files,
    }
    path = addons_dir() / 'luna-manifest.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(snapshot, indent=2, sort_keys=True) + '\n')
    return path


def repo_revision():
    try:
        result = subprocess.run(['git', '-C', str(ROOT), 'rev-parse', '--short', 'HEAD'],
                                capture_output=True, text=True)
        if result.returncode == 0:
            return result.stdout.strip()
    except OSError:
        pass
    return ''


def _manifest_problems(manifest, root):
    """Payload bytes vs manifest pins, for a checkout or an extracted bundle."""
    problems = []
    for name in ADDONS:
        entry = manifest.get('addons', {}).get(name)
        if not entry:
            problems.append('manifest has no entry for %s' % name)
            continue
        for rel, meta in entry.get('files', {}).items():
            path = root / 'addons' / name / meta['source_path']
            if not path.is_file():
                problems.append('missing payload %s/%s' % (name, rel))
                continue
            if sha256(path) != meta['sha256']:
                problems.append('payload %s/%s does not match the manifest' % (name, rel))
    for rel, meta in manifest.get('shared', {}).items():
        path = root / rel
        if not path.is_file():
            problems.append('missing shared file %s' % rel)
        elif sha256(path) != meta['sha256']:
            problems.append('shared file %s does not match the manifest' % rel)
    return problems


# Every regular file a bundle carries, with its sha256. The add-on manifest only
# pins payloads; this also covers luna.py, the host binary and the bridge, so a
# bundle whose sidecar was recomputed after corruption still cannot install.
BUNDLE_FILES = 'BUNDLE-FILES.json'


def _bundle_file_hashes(entries):
    files = {}
    for arcname, path in entries:
        if path.is_symlink() or not path.exists():
            continue
        if path.is_file():
            files[arcname] = sha256(path)
            continue
        for parent, dirs, names in os.walk(path):
            dirs.sort()
            for entry in sorted(names):
                full = Path(parent) / entry
                if full.is_file() and not full.is_symlink():
                    rel = PurePosixPath(arcname, full.relative_to(path).as_posix())
                    files[str(rel)] = sha256(full)
    return files


def _bundle_file_problems(root):
    """None when the bundle predates the file list; else a list of problems."""
    listing = root / BUNDLE_FILES
    if not listing.is_file():
        return None
    try:
        files = json.loads(listing.read_text()).get('files')
    except (ValueError, AttributeError):
        return ['%s is not valid JSON' % BUNDLE_FILES]
    if not isinstance(files, dict):
        return ['%s has no file list' % BUNDLE_FILES]
    problems = []
    seen = set()
    for parent, _dirs, names in os.walk(root):
        for entry in names:
            full = Path(parent) / entry
            rel = full.relative_to(root).as_posix()
            if rel == BUNDLE_FILES or full.is_symlink() or not full.is_file():
                continue
            seen.add(rel)
            if rel not in files:
                problems.append('unlisted file %s' % rel)
            elif sha256(full) != files[rel]:
                problems.append('%s does not match the bundle file list' % rel)
    for rel in sorted(set(files) - seen):
        problems.append('missing file %s' % rel)
    return sorted(problems)


def _check_gzip_stream(bundle):
    """Read the whole compressed stream so its CRC is checked; tar alone stops
    at the end-of-archive marker and can miss a corrupt tail."""
    import gzip
    with gzip.open(bundle, 'rb') as handle:
        while handle.read(1 << 20):
            pass


def cmd_release(options):
    """Build a bundle another machine can install with `luna update`."""
    manifest = load_manifest()  # fail early when the pins are missing
    problems = _manifest_problems(manifest, ROOT)
    if problems:
        print('REFUSING to release: the working tree does not match the manifest:')
        for problem in problems:
            print('  - %s' % problem)
        print('Run scripts/vendor-update.sh, then verify, before releasing.', file=sys.stderr)
        return 1
    warn_untraceable(manifest, sys.stderr)
    version = addon_version('luna')
    out_dir = Path(options.out).expanduser() if options.out else ROOT / 'dist'
    out_dir.mkdir(parents=True, exist_ok=True)
    name = 'luna-%s' % version
    bundle = out_dir / (name + '.tar.gz')
    entries = [
        ('addons', ROOT / 'addons'),
        ('vendor/MANIFEST.json', MANIFEST_PATH),
         ('scripts/luna.py', ROOT / 'scripts' / 'luna.py'),
        ('scripts/luna_selftest.py', ROOT / 'scripts' / 'luna_selftest.py'),
        ('scripts/steam-machine.py', ROOT / 'scripts' / 'steam-machine.py'),
        ('scripts/patch-lumen.py', PATCHER),
        ('bridge/bridge.lua', BRIDGE_SOURCE),
    ]
    # Luna's own plugin host: prebuilt binary + Lua tree, so a bundle install
    # needs no compiler. Not manifest-pinned (built locally); verified by bundle sha256.
    host_bin = host_source() / 'bin' / 'luna-host'
    if not host_bin.is_file() and (host_source() / 'Makefile').is_file():
        ensure_host_built()
    if host_bin.is_file() and (host_source() / 'lua').is_dir():
        entries.append(('host/bin/luna-host', host_bin))
        entries.append(('host/lua', host_source() / 'lua'))
    else:
        print('warning: no luna-host binary; the bundle will not carry the host', file=sys.stderr)
    readme = ROOT / 'README.md'
    if readme.is_file():
        entries.append(('README.md', readme))
    changelog = ROOT / 'CHANGELOG.md'
    if changelog.is_file():
        entries.append(('CHANGELOG.md', changelog))
    files = _bundle_file_hashes(entries)
    listing = (json.dumps({'version': 1, 'files': files}, indent=2, sort_keys=True) + '\n').encode()
    with tarfile.open(bundle, 'w:gz') as tar:
        for arcname, path in entries:
            tar.add(path, arcname='%s/%s' % (name, arcname), recursive=path.is_dir())
        info = tarfile.TarInfo('%s/%s' % (name, BUNDLE_FILES))
        info.size, info.mtime, info.mode = len(listing), int(time.time()), 0o644
        tar.addfile(info, io.BytesIO(listing))
    digest = sha256(bundle)
    checksum = out_dir / (bundle.name + '.sha256')
    checksum.write_text('%s  %s\n' % (digest, bundle.name))
    print('bundle : %s' % bundle)
    print('sha256 : %s' % digest)
    print('version: %s (%s)' % (version, ', '.join('%s %s' % (n, addon_version(n)) for n in ADDONS)))
    if options.publish:
        gh = shutil.which('gh')
        if not gh:
            print('gh CLI not found; publish manually: %s' % bundle, file=sys.stderr)
            return 1
        tag = 'v%s' % version
        view = subprocess.run([gh, 'release', 'view', tag], capture_output=True, text=True)
        if view.returncode == 0:
            args = [gh, 'release', 'upload', tag, str(bundle), str(checksum), '--clobber']
        else:
            args = [gh, 'release', 'create', tag, str(bundle), str(checksum),
                    '--title', 'Luna %s' % version,
                    '--notes', ('Luna %s: pinned payloads plus scripts/luna.py.\n\n'
                                'Install on another machine: `luna update --latest` (needs gh).'
                                % version)]
        result = subprocess.run(args, capture_output=True, text=True)
        if result.returncode != 0:
            print('gh failed: %s' % (result.stderr.strip() or result.stdout.strip()), file=sys.stderr)
            return 1
        print('published: %s' % tag)
    return EXIT_OK


def cmd_update(options):
    """Verify a released bundle and install it with the bundled script."""
    lock_path = _update_lock()
    acquired, reason = _acquire_lock(lock_path)
    if not acquired:
        print('REFUSING: %s' % reason, file=sys.stderr)
        return EXIT_BUSY
    temp_dir = Path(tempfile.mkdtemp(prefix='luna-update-'))
    try:
        bundle = None
        if options.bundle:
            bundle = Path(options.bundle).expanduser()
        elif options.latest:
            tag, tag_error = latest_release()
            if not tag:
                print('gh release lookup failed: %s' % tag_error, file=sys.stderr)
                return 1
            bundle, error = download_release(tag, temp_dir)
            if bundle is None:
                print('gh release download failed: %s' % error, file=sys.stderr)
                return 1
        elif options.url:
            bundle = temp_dir / 'luna-bundle.tar.gz'
            try:
                urllib.request.urlretrieve(options.url, bundle)
            except Exception as error:  # noqa: BLE001 - report the URL failure
                print('download failed: %s' % error, file=sys.stderr)
                return 1
            try:
                urllib.request.urlretrieve(options.url + '.sha256', str(bundle) + '.sha256')
            except Exception:  # noqa: BLE001 - the sidecar is optional for --url
                pass
            if not Path(str(bundle) + '.sha256').is_file():
                print('warning: no .sha256 beside the URL; the bundled manifest is still verified, '
                      'but prefer --latest or a checksum-signed source', file=sys.stderr)
        else:
            print('Point at a bundle: --bundle FILE, --url URL or --latest', file=sys.stderr)
            return 2
        return install_bundle(bundle, only=options.only, dry_run=options.dry_run)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
        _release_lock(lock_path)


LUNA_REPO = 'pgm1207/Luna'
UPDATE_TTL = 6 * 3600


def gh_json(args):
    gh = find_gh()
    if not gh:
        return None, 'the gh CLI is not installed (update checks use your own GitHub auth)'
    try:
        result = subprocess.run([gh] + args, capture_output=True, text=True, timeout=GH_TIMEOUT)
    except subprocess.TimeoutExpired:
        return None, 'gh did not answer within %d seconds' % GH_TIMEOUT
    if result.returncode != 0:
        return None, (result.stderr.strip() or result.stdout.strip() or 'gh failed')
    try:
        return json.loads(result.stdout), ''
    except ValueError:
        return None, 'gh returned something that is not JSON'


def latest_release():
    data, error = gh_json(['release', 'view', '--json', 'tagName,assets', '-R', LUNA_REPO])
    if data is None:
        return '', error
    return str(data.get('tagName') or ''), ''


def update_source():
    """Where the latest version comes from (shown beside the version)."""
    return 'GitHub releases of %s (via gh)' % LUNA_REPO


def _compare_fields(installed, latest):
    """available / newerThanLatest / note for an installed version against the latest release."""
    if not latest:
        return {'available': False, 'newerThanLatest': False, 'note': ''}
    newer = version_key(latest) > version_key(installed)
    ahead = version_key(installed) > version_key(latest)
    return {'available': newer, 'newerThanLatest': ahead,
            'note': 'installed %s is newer than the latest release %s; nothing to update' % (installed, latest)
            if ahead else ''}


def check_updates(force=False):
    """Compare the installed version with the newest release; cached for 6h."""
    cache_path = luna_tools_dir() / 'update-check.json'
    cached = read_json_file(cache_path) or {}
    now = int(time.time())
    installed = installed_version()
    if (not force and cached.get('latest') and cached.get('checkedAt') and not cached.get('checkError')
            and now - int(cached['checkedAt']) < UPDATE_TTL):
        cached['installed'] = installed
        cached.update(_compare_fields(installed, str(cached.get('latest') or '')))
        cached['source'] = update_source()
        return cached
    latest, error = latest_release()
    if not latest and error:
        # A failed lookup keeps the last known release, so an offline check
        # does not hide an update that was already found.
        latest = str(cached.get('latest') or '')
    report = {
        'installed': installed,
        'latest': latest,
        'checkedAt': now,
        'checkError': error,
        'source': update_source(),
    }
    report.update(_compare_fields(installed, latest))
    write_json_file(cache_path, report)
    return report


def _safe_extract(bundle, extract_dir):
    """Unpack without allowing absolute paths or parent-directory escapes."""
    with tarfile.open(bundle) as tar:
        members = tar.getmembers()
        for member in members:
            name = member.name
            if not name or name.startswith('/') or '..' in Path(name).parts:
                raise ValueError('REFUSING: unsafe path in bundle: %r' % name)
        try:
            tar.extractall(extract_dir, filter='data')
        except TypeError:  # Python < 3.12 has no extraction filter
            tar.extractall(extract_dir)


def install_bundle(bundle, only=None, dry_run=False):
    """Verify a bundle and run the installer inside it."""
    bundle = Path(bundle)
    if not bundle.is_file():
        print('bundle not found: %s' % bundle, file=sys.stderr)
        return 2
    checksum_file = Path(str(bundle) + '.sha256')
    if checksum_file.is_file():
        expected = checksum_file.read_text().split()[0].strip().lower()
        actual = sha256(bundle)
        if expected != actual:
            print('REFUSING: sha256 mismatch (bundle %s, sidecar %s)'
                  % (actual[:12], expected[:12]), file=sys.stderr)
            return 1
        print('sha256 : verified (%s)' % actual[:12])
    else:
        print('note   : no .sha256 beside the bundle; the payload hashes in the '
              'bundled manifest are still enforced')
    with tempfile.TemporaryDirectory(prefix='luna-bundle-') as work:
        extract_dir = Path(work) / 'unpacked'
        extract_dir.mkdir()
        try:
            with bundle.open('rb') as stream:
                is_gzip = stream.read(2) == b'\x1f\x8b'
            if is_gzip:
                _check_gzip_stream(bundle)
            _safe_extract(bundle, extract_dir)
        except (tarfile.TarError, ValueError, OSError, EOFError, zlib.error) as error:
            print('REFUSING: cannot safely unpack the bundle: %s' % error, file=sys.stderr)
            return 1
        roots = [entry for entry in extract_dir.iterdir() if entry.is_dir()]
        root = roots[0] if len(roots) == 1 else extract_dir
        script = root / 'scripts' / 'luna.py'
        if not script.is_file():
            print('REFUSING: the bundle does not contain scripts/luna.py', file=sys.stderr)
            return 1
        version = '?'
        bundled_manifest = root / 'vendor' / 'MANIFEST.json'
        if not bundled_manifest.is_file():
            print('REFUSING: the bundle does not contain vendor/MANIFEST.json', file=sys.stderr)
            return 1
        try:
            manifest = json.loads(bundled_manifest.read_text())
            version = str((manifest.get('addons', {}).get('luna') or {})
                          .get('plugin_version', '?'))
        except ValueError:
            print('REFUSING: the bundled manifest is not valid JSON', file=sys.stderr)
            return 1
        listed = _bundle_file_problems(root)
        if listed:
            print('REFUSING: the bundle contents do not match its file list:')
            for problem in listed[:20]:
                print('  - %s' % problem, file=sys.stderr)
            return 1
        if listed is None:
            print('note   : bundle predates %s; only the pinned payloads are verified'
                  % BUNDLE_FILES)
        problems = _manifest_problems(manifest, root)
        if problems:
            print('REFUSING: the bundle contents do not match its own manifest:')
            for problem in problems:
                print('  - %s' % problem, file=sys.stderr)
            return 1
        print('bundle : %s (Luna %s, manifest verified)' % (bundle.name, version))
        # Add-on updates must not undo a deliberate client update from Health.
        command = [sys.executable, str(script), 'install', '--force']
        for name in only or []:
            command += ['--only', name]
        if dry_run:
            command.append('--dry-run')
        result = subprocess.run(command, env=dict(os.environ, LUNA_KEEP_STEAM_CLIENT='1',
                                                  **{LOCK_HELD_ENV: '1'}))
        return result.returncode


def download_release(tag, work):
    """Fetch a release's bundle + checksum with the user's gh auth."""
    gh = find_gh()
    if not gh:
        return None, 'the gh CLI is not installed'
    if not tag:
        return None, 'no release tag to download'
    try:
        result = subprocess.run([gh, 'release', 'download', tag,
                                 '--pattern', 'luna-*.tar.gz',
                                 '--pattern', 'luna-*.tar.gz.sha256',
                                 '--dir', str(work), '--clobber', '-R', LUNA_REPO],
                                capture_output=True, text=True, timeout=GH_DOWNLOAD_TIMEOUT)
    except subprocess.TimeoutExpired:
        return None, 'the release download did not finish within %d seconds' % GH_DOWNLOAD_TIMEOUT
    if result.returncode != 0:
        return None, (result.stderr.strip() or result.stdout.strip() or 'download failed')
    found = sorted(work.glob('luna-*.tar.gz'))
    if not found:
        return None, 'the release has no bundle asset'
    bundle = found[-1]
    if not Path(str(bundle) + '.sha256').is_file():
        return None, 'the release has no checksum sidecar; refusing an unauthenticated bundle'
    return bundle, ''


def _update_lock():
    """Single writer lock for *every* mutating command.

    Timer, menu, `luna install`, uninstall, launchers, host, backup and restore
    all take this one file, so two writers can never interleave payload bytes
    or fight over boot.lua. The name is historical (tests and old units know it).
    """
    tools = luna_tools_dir()
    try:
        tools.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return tools / 'update.lock'


LOCK_TTL = 3600
CHECK_LOCK_TTL = 600


def _lock_owner(lock_path):
    """(pid, started) recorded by the lock owner, or (0, 0) when unreadable."""
    try:
        info = json.loads(lock_path.read_text())
        return int(info.get('pid') or 0), int(info.get('at') or 0)
    except (OSError, ValueError, TypeError, AttributeError):
        return 0, 0


def _pid_cmdline(pid):
    """/proc/<pid>/cmdline as one string; '' when unreadable."""
    try:
        return Path('/proc/%d/cmdline' % int(pid)).read_bytes().replace(b'\0', b' ').decode('utf-8', 'replace')
    except (OSError, ValueError):
        return ''


def _proc_start(pid):
    """Start time (clock ticks since boot) of a process; 0 when unknown."""
    try:
        text = Path('/proc/%d/stat' % int(pid)).read_text()
        return int(text[text.rindex(')') + 2:].split()[19])
    except (OSError, ValueError, IndexError):
        return 0


def pid_is_live_luna(pid, start=0):
    """True only for a live, non-zombie luna process with the recorded start time.

    A bare kill(pid, 0) is fooled by pid reuse after a reboot or a long
    outage; the command line and the start time are checked as well.
    """
    try:
        pid = int(pid or 0)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:
        stat_text = Path('/proc/%d/stat' % pid).read_text()
        if stat_text[stat_text.rindex(')') + 2:].split()[0] == 'Z':
            return False
    except (OSError, ValueError, IndexError):
        pass
    # An empty command line (exiting process, kernel thread, unreadable) is NOT proof of a
    # luna process: only a positive match counts, or a recycled pid reads as alive.
    if 'luna' not in _pid_cmdline(pid).lower():
        return False
    if start and _proc_start(pid) and _proc_start(pid) != int(start):
        return False
    return True


def _busy_message(lock_path):
    """One wording for every refusal, so callers and the panel agree."""
    pid, started = _lock_owner(lock_path)
    when = (time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(started))
            if started else 'unknown time')
    return ('another luna command is already running (pid %s, started %s); '
            'nothing was changed, try again when it finishes' % (pid or '?', when))


def _acquire_lock(lock_path, ttl=LOCK_TTL):
    """Claim the lock; clear it when stale (dead owner or too old).

    Returns (True, '') on success or (False, reason). A kill -9 between
    acquire and release must never wedge updates forever.
    """
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        pass
    except OSError:
        return True, ''
    else:
        try:
            os.write(fd, json.dumps(
                {'pid': os.getpid(), 'at': int(time.time())}).encode())
        finally:
            os.close(fd)
        return True, ''
    owner, started = _lock_owner(lock_path)
    alive = False
    if owner > 0:
        try:
            os.kill(owner, 0)
            alive = True
        except (OSError, ProcessLookupError, PermissionError):
            alive = False
    if alive and time.time() - started >= ttl:
        # Never take the lock from a live luna process: a stalled installer must
        # be killed by its own timeout, not raced by a second writer. Only an
        # unrelated process that inherited a reused pid is treated as stale.
        cmdline = _pid_cmdline(owner)
        if cmdline and 'luna' not in cmdline.lower():
            alive = False
    if alive:
        return False, _busy_message(lock_path)
    try:
        lock_path.unlink()
    except OSError:
        pass
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except (FileExistsError, OSError):
        return False, _busy_message(lock_path)
    try:
        os.write(fd, json.dumps({'pid': os.getpid(), 'at': int(time.time())}).encode())
    finally:
        os.close(fd)
    return True, ''


def _release_lock(lock_path):
    try:
        lock_path.unlink()
    except OSError:
        pass


def lock_inherited():
    """True when an owner that already holds the lock spawned this process."""
    return os.environ.get(LOCK_HELD_ENV) == '1'


def run_locked(func, options):
    """Run a mutating handler under the single writer lock.

    Non-blocking: a second invocation reports who holds the lock and exits
    EXIT_BUSY (distinct from success and from every refusal code).
    """
    if lock_inherited():
        return func(options)
    lock_path = _update_lock()
    acquired, reason = _acquire_lock(lock_path)
    if not acquired:
        print('REFUSING: %s' % reason, file=sys.stderr)
        return EXIT_BUSY
    try:
        return func(options)
    finally:
        _release_lock(lock_path)


def locked(func):
    """Decorator form used by the dispatch table."""
    def wrapper(options):
        return run_locked(func, options)
    wrapper.__name__ = getattr(func, '__name__', 'locked')
    wrapper.__doc__ = func.__doc__
    return wrapper


CLI_LAUNCHER = '''#!/bin/sh
# Managed by SLS Luna CLI v4
# Runs the copy of luna.py kept by the active plugin host (see: luna host status); when a
# base install replaced the Lumen tree, the rescue copy in Luna's own state dir is used.
# A copy is only run when its support tree sits beside it: a pre-runtime copy takes ROOT
# from its own directory and then looks for vendor/MANIFEST.json in the Lumen tree, which
# is the "is missing; run scripts/vendor-update.sh" dead end. Skip it, never consult it.
LUNA_HOME="${LUNA_HOME:-$HOME/.local/share/Luna}"
if grep -q '"active": *"luna"' "$LUNA_HOME/host.json" 2>/dev/null \\
   && [ -f "$LUNA_HOME/state/runtime/RUNTIME.json" ] \\
   && [ -f "$LUNA_HOME/state/runtime/vendor/MANIFEST.json" ]; then
  exec python3 "$LUNA_HOME/state/luna.py" "$@"
fi
for copy in "$HOME/.local/share/Lumen/sls-luna/luna.py" "$LUNA_HOME/state/rescue/luna.py"; do
  dir=$(dirname "$copy")
  if [ -f "$copy" ] && [ -f "$dir/runtime/RUNTIME.json" ] \\
     && [ -f "$dir/runtime/vendor/MANIFEST.json" ]; then
    exec python3 "$copy" "$@"
  fi
done
echo "luna: no Luna tooling copy with its runtime tree found (a base install may have replaced ~/.local/share/Lumen/sls-luna)." >&2
echo "Get Luna back: unpack a Luna release (or use the checkout) and run: python3 scripts/luna.py install --force" >&2
exit 1
'''

# The pre-runtime launcher Luna wrote itself (v3): still ours, upgraded in place on install.
LEGACY_CLI_LAUNCHERS = ('''#!/bin/sh
# Managed by SLS Luna CLI v1
# Recovery remains available even if a base update replaces Lumen's tooling.
if [ "${1:-}" = hook ] && [ -f "$HOME/.local/share/slssteam-luna/lumen-hook.py" ]; then
    shift
    action="${1:-status}"
    case "$action" in on) action=enable;; off) action=disable;; esac
    [ "$#" -eq 0 ] || shift
    exec python3 "$HOME/.local/share/slssteam-luna/lumen-hook.py" "$action" "$@"
fi
exec python3 "$HOME/.local/share/Lumen/sls-luna/luna.py" "$@"
''', '''#!/bin/sh
# Managed by SLS Luna CLI v2
# Runs the copy of luna.py kept by the active plugin host (see: luna host status).
LUNA_HOME="${LUNA_HOME:-$HOME/.local/share/Luna}"
if grep -q '"active": *"luna"' "$LUNA_HOME/host.json" 2>/dev/null && [ -f "$LUNA_HOME/state/luna.py" ]; then
  exec python3 "$LUNA_HOME/state/luna.py" "$@"
fi
exec python3 "$HOME/.local/share/Lumen/sls-luna/luna.py" "$@"
''', '''#!/bin/sh
# Managed by SLS Luna CLI v1
exec python3 "$HOME/.local/share/Lumen/sls-luna/luna.py" "$@"
''', '''#!/bin/sh
# Managed by SLS Luna CLI v3
# Runs the copy of luna.py kept by the active plugin host (see: luna host status); when a
# base install replaced the Lumen tree, the rescue copy in Luna's own state dir is used.
LUNA_HOME="${LUNA_HOME:-$HOME/.local/share/Luna}"
if grep -q '"active": *"luna"' "$LUNA_HOME/host.json" 2>/dev/null && [ -f "$LUNA_HOME/state/luna.py" ]; then
  exec python3 "$LUNA_HOME/state/luna.py" "$@"
fi
for copy in "$HOME/.local/share/Lumen/sls-luna/luna.py" "$LUNA_HOME/state/rescue/luna.py"; do
  [ -f "$copy" ] && exec python3 "$copy" "$@"
done
echo "luna: no Luna tooling copy found (a base install may have replaced ~/.local/share/Lumen/sls-luna)." >&2
echo "Get Luna back: unpack a Luna release (or use the checkout) and run: python3 scripts/luna.py install --force" >&2
exit 1
''')


def ours_cli_launcher(launcher):
    try:
        data = launcher.read_bytes()
    except OSError:
        return False
    return data in [CLI_LAUNCHER.encode()] + [text.encode() for text in LEGACY_CLI_LAUNCHERS]


def runtime_sources():
    """(relative path, source) for the support tree an installed copy needs."""
    rels = ['scripts/patch-lumen.py', 'scripts/steam-machine.py',
            'scripts/luna_selftest.py',
            'bridge/bridge.lua', 'vendor/MANIFEST.json']
    for name in ADDONS:
        for source_rel, _dest in ADDON_FILES[name]:
            rels.append('addons/%s/%s' % (name, source_rel))
    return [(rel, ROOT / rel) for rel in rels]


RUNTIME_MANIFEST = 'RUNTIME.json'


def _file_sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


def deploy_runtime(tools=None, luna_source=None):
    """Copy patcher, bridge, add-on sources and manifest to <tools>/runtime.

    Staged in runtime.new, RUNTIME.json (sha256 of the luna.py it belongs to and of
    every file) written last, then swapped in with renames, so an interrupted
    install never leaves a half-new tree that looks complete. Returns the
    relative paths whose source was missing.
    """
    tools = Path(tools or luna_tools_dir())
    dest = tools / 'runtime'
    luna_source = Path(luna_source or __file__).resolve()
    missing = []
    try:
        if dest.resolve() == ROOT.resolve():  # running from the installed copy: nothing to stage
            missing = [rel for rel, source in runtime_sources() if not source.is_file()]
            if not (dest / RUNTIME_MANIFEST).is_file() and not missing:
                _write_runtime_manifest(dest, [rel for rel, _s in runtime_sources()], luna_source)
            return missing
    except OSError:
        pass
    stage, old = tools / 'runtime.new', tools / 'runtime.old'
    shutil.rmtree(stage, ignore_errors=True)
    copied = []
    for rel, source in runtime_sources():
        if not source.is_file():
            missing.append(rel)
            continue
        target = stage / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        copied.append(rel)
    _write_runtime_manifest(stage, copied, luna_source)
    shutil.rmtree(old, ignore_errors=True)
    if dest.exists():
        os.replace(dest, old)
    os.replace(stage, dest)
    shutil.rmtree(old, ignore_errors=True)
    return missing


def _write_runtime_manifest(directory, rels, luna_source):
    manifest = {'lunaSha256': _file_sha256(luna_source),
                'files': {rel: _file_sha256(Path(directory) / rel) for rel in rels
                          if (Path(directory) / rel).is_file()}}
    tmp = Path(directory) / (RUNTIME_MANIFEST + '.tmp')
    tmp.write_text(json.dumps(manifest, sort_keys=True))
    os.replace(tmp, Path(directory) / RUNTIME_MANIFEST)


RUNTIME_REPAIR = ('repair: run `python3 <checkout or unpacked release>/scripts/luna.py install --force` '
                  '(it redeploys the runtime tree)')


def runtime_problems(full=False):
    """Does <tools>/runtime belong to the luna.py that is running? Only the installed
    copy is checked (a checkout has no runtime tree). Cheap: manifest, luna.py hash and
    file presence; full also hashes every file and refuses an interrupted install."""
    if ROOT != _INSTALLED_ROOT:
        return []
    tools = _INSTALLED_ROOT.parent
    if not _INSTALLED_ROOT.is_dir() and (tools / 'runtime.old').is_dir():
        with contextlib.suppress(OSError):  # interrupted swap: put the old tree back
            os.replace(tools / 'runtime.old', _INSTALLED_ROOT)
    manifest = read_json_file(_INSTALLED_ROOT / RUNTIME_MANIFEST)
    bad = 'runtime tree does not match this luna.py'
    if not isinstance(manifest, dict):
        return ['%s (no %s); %s' % (bad, RUNTIME_MANIFEST, RUNTIME_REPAIR)]
    problems = []
    try:
        if manifest.get('lunaSha256') != _file_sha256(Path(__file__).resolve()):
            problems.append('%s (luna.py changed since the tree was deployed)' % bad)
    except OSError:
        problems.append('%s (luna.py unreadable)' % bad)
    for rel, digest in sorted((manifest.get('files') or {}).items()):
        path = _INSTALLED_ROOT / rel
        try:
            if not path.is_file() or (full and _file_sha256(path) != digest):
                problems.append('%s (%s %s)' % (bad, rel, 'missing' if not path.is_file() else 'differs'))
                if len(problems) > 3:
                    break
        except OSError:
            problems.append('%s (%s unreadable)' % (bad, rel))
    if full and install_marker_path().is_file():
        problems.append('a previous Luna install was interrupted (%s present)' % install_marker_path())
    return ['%s; %s' % (problems[0], RUNTIME_REPAIR)] + problems[1:] if problems else []


def installed_copy_problems(tools=None):
    """Problems with the copy at <tools>/luna.py + <tools>/runtime, checked before re-exec'ing it."""
    tools = Path(tools or luna_tools_dir())
    script = tools / 'luna.py'
    runtime = tools / 'runtime'
    if not script.is_file():
        return ['%s is missing' % script]
    manifest = read_json_file(runtime / RUNTIME_MANIFEST)
    if not isinstance(manifest, dict):
        return ['%s has no %s, so it would run without its support tree' % (runtime, RUNTIME_MANIFEST)]
    problems = []
    try:
        if manifest.get('lunaSha256') != _file_sha256(script):
            problems.append('%s was replaced after its runtime tree was deployed' % script)
    except OSError:
        problems.append('%s is unreadable' % script)
    for rel in sorted((manifest.get('files') or {})):
        if not (runtime / rel).is_file():
            problems.append('%s/%s is missing' % (runtime, rel))
            break
    return problems


def write_tooling():
    """Keep a standalone updater copy the timer can run without a checkout.

    Called both by the installer (from a checkout) and by the copy itself, so
    the source is *this* file, never a fixed path relative to it. The support
    tree beside it is what makes a Lumen update work from the installed copy.
    """
    tools = luna_tools_dir()
    tools.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):  # the bridge only writes its heartbeat here, it cannot mkdir
        luna_state_dir().mkdir(parents=True, exist_ok=True)
    target = tools / 'luna.py'
    source = Path(__file__).resolve()
    try:
        same = source.samefile(target)
    except OSError:
        same = False
    missing = deploy_runtime(tools, source)  # runtime first, luna.py last: RUNTIME.json names its hash
    if not same:
        shutil.copy2(source, target)
        target.chmod(0o755)
    launcher = home() / '.local/bin/luna'
    if launcher.is_symlink() or (launcher.exists() and
                                (not launcher.is_file() or not ours_cli_launcher(launcher))):
        print('WARNING: custom luna command preserved; use the installed Python tooling directly.', file=sys.stderr)
    elif launcher.is_file() and launcher.read_bytes() != CLI_LAUNCHER.encode():
        tmp = launcher.with_name('luna.luna-tmp')
        tmp.write_bytes(CLI_LAUNCHER.encode())
        tmp.chmod(0o755)
        os.replace(tmp, launcher)
    elif not launcher.exists():
        launcher.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(launcher, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o755)
        except FileExistsError:
            print('WARNING: luna command appeared concurrently; it was left untouched.', file=sys.stderr)
        else:
            with os.fdopen(fd, 'wb') as out:
                out.write(CLI_LAUNCHER.encode())
    if luna_tools_dir() != luna_state_dir():
        # A copy outside the Lumen tree: `luna lumen restore` and `luna install` still work
        # after a base install replaced ~/.local/share/Lumen/sls-luna.
        rescue = luna_state_dir() / 'rescue'
        try:
            deploy_runtime(rescue, source)
            if not (rescue / 'luna.py').exists() or not source.samefile(rescue / 'luna.py'):
                shutil.copy2(source, rescue / 'luna.py')
                (rescue / 'luna.py').chmod(0o755)
        except OSError as error:
            print('WARNING: could not keep a rescue copy in %s: %s' % (rescue, error), file=sys.stderr)
    if missing:
        print('WARNING: installed tooling is incomplete; missing %s' % ', '.join(missing),
              file=sys.stderr)
    return target


def unit_paths():
    unit_dir = home() / '.config/systemd/user'
    return unit_dir, unit_dir / 'luna-autoupdate.service', unit_dir / 'luna-autoupdate.timer'


APPLY_UNIT = 'luna-apply.service'


def apply_unit_paths():
    unit_dir = home() / '.config/systemd/user'
    return unit_dir, unit_dir / APPLY_UNIT


def systemd_available():
    """True when a user manager can start the waiter unit."""
    try:
        result = systemctl_user('is-system-running', timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 or result.stdout.strip() in ('running', 'degraded', 'starting')


def write_apply_unit():
    """The prompt-apply unit: waits for Steam to stay closed, then updates.

    A separate oneshot unit on purpose: it must NOT share lumen.service's
    cgroup, because the update stops Lumen. Cancel stops this unit.
    """
    unit_dir, service = apply_unit_paths()
    unit_dir.mkdir(parents=True, exist_ok=True)
    service.write_text(
        '[Unit]\n'
        'Description=Luna: apply a queued update once Steam has stayed closed\n'
        'After=graphical-session.target\n'
        '\n'
        '[Service]\n'
        'Type=oneshot\n'
        'Environment=PATH=%%h/.local/bin:/usr/local/bin:/usr/bin\n'
        'TimeoutStartSec=infinity\n'
        # SIGTERM goes to luna.py only, so its `finally` blocks (steam lock,
        # rollback) run; the installer is killed by luna itself, then by systemd.
        'KillMode=mixed\n'
        'TimeoutStopSec=300\n'
        'ExecStart=%s %s autoupdate --wait-steam\n' % (sys.executable, luna_tools_dir() / 'luna.py'))
    systemctl_user('daemon-reload')
    return service


def start_apply_service():
    """Start the waiter now; returns 'service' or 'timer' (degraded)."""
    if not systemd_available():
        return 'timer'
    unit_dir, service = apply_unit_paths()
    if not service.is_file():
        write_apply_unit()
    result = systemctl_user('start', '--no-block', APPLY_UNIT)
    return 'service' if result.returncode == 0 else 'timer'


def stop_apply_service():
    unit_dir, service = apply_unit_paths()
    if service.is_file():
        systemctl_user('stop', APPLY_UNIT)


def cmd_check(options):
    """Print the update check as JSON (the Luna menu reads it).

    Single-flight: the menu can spawn `check` on every page load, but only one
    may hold the check lock (pid liveness, not a timestamp window). A second
    invocation answers "busy" instead of racing for the same gh release.
    """
    lock = luna_tools_dir() / 'check.lock'
    acquired, reason = _acquire_lock(lock, ttl=CHECK_LOCK_TTL)
    if not acquired:
        print(json.dumps({'result': 'busy', 'reason': reason}, sort_keys=True))
        return EXIT_OK
    try:
        write_heartbeat()
        print(json.dumps(check_updates(force=getattr(options, 'force', False)), sort_keys=True))
        return EXIT_OK
    finally:
        _release_lock(lock)


def cmd_timer(options):
    """Enable, disable or report the unattended-update timer."""
    unit_dir, service, timer = unit_paths()
    if options.action == 'status':
        enabled = systemctl_user('is-enabled', 'luna-autoupdate.timer')
        active = systemctl_user('is-active', 'luna-autoupdate.timer')
        print(json.dumps({'enabled': enabled.stdout.strip() or 'unknown',
                          'active': active.stdout.strip() or 'unknown',
                          'units': str(unit_dir)}, sort_keys=True))
        return EXIT_OK
    if options.action == 'on':
        write_tooling()
        write_apply_unit()
        unit_dir.mkdir(parents=True, exist_ok=True)
        service.write_text(
            '[Unit]\n'
            'Description=SLS Luna updates (applies a newer release when Steam is closed)\n'
            'After=graphical-session.target\n\n'
            '[Service]\n'
            'Type=oneshot\n'
            'Environment=PATH=%%h/.local/bin:/usr/local/bin:/usr/bin\n'
            'ExecStart=%s %s autoupdate\n' % (sys.executable, luna_tools_dir() / 'luna.py'))
        timer.write_text(
            '[Unit]\n'
            'Description=Check the SLS Luna release channel every 30 minutes\n\n'
            '[Timer]\n'
            'OnCalendar=*:0/30\n'
            'Persistent=true\n'
            'RandomizedDelaySec=120\n\n'
            '[Install]\n'
            'WantedBy=timers.target\n')
        systemctl_user('daemon-reload')
        result = systemctl_user('enable', '--now', 'luna-autoupdate.timer')
        if result.returncode != 0:
            print('FAILED: %s' % (result.stderr.strip() or result.stdout.strip()), file=sys.stderr)
            return 1
        print('unattended updates: on (%s)' % timer)
        return EXIT_OK
    if options.action == 'off':
        systemctl_user('disable', '--now', 'luna-autoupdate.timer')
        for path in (timer, service):
            if path.exists():
                path.unlink()
        systemctl_user('daemon-reload')
        print('unattended updates: off')
        return EXIT_OK
    print('unknown timer action: %s' % options.action, file=sys.stderr)
    return 2


def retire_lumen_hook():
    """Remove the old automatic Lumen-update recovery (units + stored worker).

    Nothing watches for a stock Lumen update any more: Luna's own host
    (`luna host cutover`) replaces the third-party one.
    Only units carrying Luna's own marker are touched; custom units stay.
    """
    mark = '# Managed by Luna: Lumen update recovery v1'
    unit_dir = home() / '.config/systemd/user'
    names = ['luna-lumen-hook.' + kind for kind in ('path', 'timer', 'service')]
    found = [unit_dir / name for name in names if (unit_dir / name).is_file()]
    if found:
        systemctl_user('disable', '--now', names[0], names[1])
        for path in found:
            try:
                if mark in path.read_text():
                    path.unlink()
                else:
                    print('note: %s is not Luna-managed; left in place' % path, file=sys.stderr)
            except OSError:
                pass
        systemctl_user('daemon-reload')
        print('hook      : retired the automatic Lumen recovery units')
    store_dir = home() / '.local/share/slssteam-luna'
    for stale in ('lumen-hook.py',):
        try:
            (store_dir / stale).unlink()
        except OSError:
            pass


# --------------------------------------------------------------------------- Luna host
#
# luna-host is Luna's own Steam plugin host. It lives side by side with the
# third-party Lumen host: install writes it (never enables it), `luna host
# cutover` switches over, `luna host rollback` switches back. ~/.local/share/Lumen
# is never deleted.

HOST_UNIT_MARK = '# Managed by Luna: host v1'
HOST_UNIT = 'luna-host.service'
LUMEN_UNIT = 'lumen.service'


def host_source():
    """Where the host sources/binary come from: a checkout or an extracted bundle."""
    return Path(os.environ.get('LUNA_HOST_SRC') or (ROOT / 'host'))


def host_binary():
    return luna_home() / 'bin' / 'luna-host'


def host_unit_path():
    return home() / '.config/systemd/user' / HOST_UNIT


def systemctl_user(*args, timeout=None):
    """The one way Luna talks to the user's systemd manager (tests replace it)."""
    try:
        return subprocess.run(['systemctl', '--user'] + list(args), capture_output=True, text=True,
                              timeout=timeout)
    except FileNotFoundError:
        return subprocess.CompletedProcess(args, 127, '', 'systemctl not found')
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(args, 124, '', 'systemctl timed out')


def unit_state(unit):
    enabled = systemctl_user('is-enabled', unit).stdout.strip()
    active = systemctl_user('is-active', unit).stdout.strip()
    return {'enabled': enabled or 'unknown', 'active': active or 'unknown'}


def ensure_host_built():
    """Make sure the host binary exists (release bundles ship it prebuilt)."""
    source = host_source()
    binary = source / 'bin' / 'luna-host'
    if binary.is_file():
        return binary
    if not (source / 'Makefile').is_file():
        print('host      : no luna-host binary and no sources at %s; skipped' % source, file=sys.stderr)
        return None
    print('host      : building luna-host (make -C %s)' % source)
    try:
        result = subprocess.run(['make', '-C', str(source)], capture_output=True, text=True)
    except FileNotFoundError:
        print('host      : WARN: make is not installed; luna-host not built', file=sys.stderr)
        return None
    if result.returncode != 0 or not binary.is_file():
        tail = (result.stderr or result.stdout).strip().splitlines()[-5:]
        print('host      : WARN: the luna-host build failed:\n    %s' % '\n    '.join(tail), file=sys.stderr)
        return None
    return binary


def _replace_tree(source, dest):
    staging = dest.with_name(dest.name + '.luna-new')
    shutil.rmtree(staging, ignore_errors=True)
    shutil.copytree(source, staging, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    shutil.rmtree(dest, ignore_errors=True)
    os.replace(staging, dest)


def host_unit_text():
    return (
        HOST_UNIT_MARK + '\n'
        '[Unit]\n'
        "Description=Luna plugin host (injects Luna's add-ons into Steam)\n"
        'After=graphical-session.target\n\n'
        '[Service]\n'
        'Type=simple\n'
        'Environment=LUNA_HOME=%s\n'
        'Environment=LUNA_LUA_DIR=%s\n'
        'ExecStart=%s\n'
        'Restart=always\n'
        'RestartSec=5\n\n'
        '[Install]\n'
        'WantedBy=default.target\n' % (luna_home(), luna_home() / 'lua', host_binary()))


def mirror_addons_to_host():
    """Copy the installed add-ons (same layout as sls-addons) into $LUNA_HOME/addons."""
    dest_root = luna_home() / 'addons'
    dest_root.mkdir(parents=True, exist_ok=True)
    if dest_root.resolve() == addons_dir().resolve():
        return  # already installing straight into the standalone host
    for name in ADDONS:
        source = addons_dir() / name
        if source.is_dir():
            _replace_tree(source, dest_root / name)
    snapshot = addons_dir() / 'luna-manifest.json'
    if snapshot.is_file():
        shutil.copy2(snapshot, dest_root / 'luna-manifest.json')


def write_legacy_plugins():
    """Third-party plugins luna-host may also load: only LuaTools, only if present."""
    target = luna_home() / 'legacy-plugins.json'
    luatools = lumen_root() / 'luatools'
    if target.exists() or not (luatools / 'plugin.json').is_file():
        return
    target.write_text(json.dumps([str(luatools)], indent=2) + '\n')


def standalone_setup():
    """Enable the managed host and remember only settings this install created."""
    unit = host_unit_path()
    if unit.is_symlink() or not unit.is_file() or HOST_UNIT_MARK not in unit.read_text(errors='replace'):
        print('host      : WARNING: no managed host unit; not enabling it', file=sys.stderr)
        return
    ownership = luna_home() / 'state/standalone-setup.json'
    state = read_json_file(ownership) or {}
    root = steam_home()
    try:
        if not root.is_dir():
            print('host      : WARNING: Steam root not found; skipping debugging flag', file=sys.stderr)
        else:
            flag = root / '.cef-enable-remote-debugging'
            try:
                fd = os.open(flag, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
            except FileExistsError:
                pass
            else:
                st = os.fstat(fd)
                os.close(fd)
                flags = _owned_flags(state)
                flags[str(flag)] = [st.st_dev, st.st_ino]
                state.pop('flag', None)
                state.pop('flag_path', None)
                state['flags'] = flags
                write_json_file(ownership, state)
        enabled = unit_state(HOST_UNIT)['enabled'] in ('enabled', 'enabled-runtime', 'linked', 'linked-runtime')
        if not enabled:
            result = systemctl_user('enable', HOST_UNIT)
            if result.returncode:
                print('host      : WARNING: could not enable %s: %s' % (HOST_UNIT, result.stderr.strip()),
                      file=sys.stderr)
                return
            state['enabled'] = True
            write_json_file(ownership, state)
        result = systemctl_user('start', HOST_UNIT)
        if result.returncode:
            print('host      : WARNING: could not start %s: %s' % (HOST_UNIT, result.stderr.strip()),
                  file=sys.stderr)
    except OSError as error:
        print('host      : WARNING: standalone setup failed: %s' % error, file=sys.stderr)


def _owned_flags(state):
    """{flag path: [dev, ino]} for every debugging flag this install created.
    Reads the older single-flag record too, so an upgrade keeps its ownership."""
    flags = dict(state.get('flags') or {}) if isinstance(state.get('flags'), dict) else {}
    if isinstance(state.get('flag'), list):
        legacy = state.get('flag_path') or str(steam_home() / '.cef-enable-remote-debugging')
        flags.setdefault(legacy, state['flag'])
    return flags


def undo_standalone_setup():
    """Undo only what standalone_setup created. Anything that fails to undo stays
    recorded, so a later rollback or uninstall can retry it."""
    ownership = luna_home() / 'state/standalone-setup.json'
    state = read_json_file(ownership) or {}
    remaining = {}
    if state.get('enabled'):
        result = systemctl_user('disable', '--now', HOST_UNIT)
        if result.returncode:
            print('host      : WARNING: could not disable %s (kept for a retry): %s'
                  % (HOST_UNIT, result.stderr.strip()), file=sys.stderr)
            remaining['enabled'] = True
    kept = {}
    for path, identity in _owned_flags(state).items():
        flag = Path(path)
        try:
            st = flag.lstat()
        except FileNotFoundError:
            continue
        except OSError as error:
            print('host      : WARNING: cannot check %s (kept for a retry): %s' % (flag, error),
                  file=sys.stderr)
            kept[path] = identity
            continue
        if identity != [st.st_dev, st.st_ino] or flag.is_symlink() or not flag.is_file():
            continue  # replaced by someone else since: not ours any more
        try:
            flag.unlink()
        except OSError as error:
            print('host      : WARNING: cannot remove %s (kept for a retry): %s' % (flag, error),
                  file=sys.stderr)
            kept[path] = identity
    if kept:
        remaining['flags'] = kept
    if remaining:
        write_json_file(ownership, remaining)
    elif ownership.is_file():
        ownership.unlink()


def install_luna_host(dry_run=False):
    """Deploy luna-host into LUNA_HOME next to Lumen. Never enables the unit
    unless the cutover already happened. Failures warn; add-on install goes on."""
    if dry_run:
        print('host      : would install luna-host, its Lua tree, add-ons and %s into %s'
              % (HOST_UNIT, luna_home()))
        return
    binary = ensure_host_built()
    if binary is None:
        return
    source_lua = host_source() / 'lua'
    if not source_lua.is_dir():
        print('host      : WARN: %s is missing; luna-host not installed' % source_lua, file=sys.stderr)
        return
    try:
        target_bin = host_binary()
        target_bin.parent.mkdir(parents=True, exist_ok=True)
        tmp = target_bin.with_name('luna-host.luna-tmp')
        shutil.copy2(binary, tmp)
        tmp.chmod(0o755)
        os.replace(tmp, target_bin)
        _replace_tree(source_lua, luna_home() / 'lua')
        mirror_addons_to_host()
        write_legacy_plugins()
        (luna_home() / 'state').mkdir(parents=True, exist_ok=True)
        unit = host_unit_path()
        text = host_unit_text()
        changed = False
        if unit.is_file() and HOST_UNIT_MARK not in unit.read_text(errors='replace'):
            print('host      : %s is not Luna-managed; left in place' % unit, file=sys.stderr)
        elif not unit.is_file() or unit.read_text() != text:
            unit.parent.mkdir(parents=True, exist_ok=True)
            unit.write_text(text)
            changed = True
        if changed:
            systemctl_user('daemon-reload')
    except OSError as error:
        print('host      : WARN: could not install luna-host: %s' % error, file=sys.stderr)
        return
    print('host      : luna-host installed in %s (active host: %s)' % (luna_home(), active_host()))
    if active_host() == 'luna':
        patch_wrapper()
        standalone_setup()
        print('host      : restart %s to load this version' % HOST_UNIT)


WRAPPER_BEGIN = '# >>> luna-patch: skip the Lumen sidecar when luna-host is active (luna host unpatch-wrapper)'
WRAPPER_END = '# <<< luna-patch'
WRAPPER_ANCHOR = 'if [ -x "$LUMEN_DIR/lumen" ] && ! pgrep -f "$LUMEN_DIR/lumen"'


def wrapper_path():
    return Path.home() / '.local/share/SLSsteam/path/steam'


def wrapper_patch_block():
    return '\n'.join([
        WRAPPER_BEGIN,
        '# luna-host needs Steam\'s CEF remote debugging; keep enabling it.',
        'touch "$HOME/.steam/steam/.cef-enable-remote-debugging" 2>/dev/null || true',
        'touch "$HOME/.steam/debian-installation/.cef-enable-remote-debugging" 2>/dev/null || true',
        'if grep -qsE \'"active": *"luna"\' "${LUNA_HOME:-$HOME/.local/share/Luna}/host.json" 2>/dev/null; then',
        '\tLUMEN_DIR=/nonexistent/luna-host-active',
        'fi',
        WRAPPER_END, ''])


def wrapper_patched():
    try:
        return WRAPPER_BEGIN in wrapper_path().read_text(errors='replace')
    except OSError:
        return False


def patch_wrapper():
    """Idempotent. Returns True when the wrapper is patched afterwards."""
    path = wrapper_path()
    if not path.is_file():
        return False
    text = path.read_text(errors='replace')
    if WRAPPER_BEGIN in text:
        return True
    lines = text.splitlines(keepends=True)
    index = next((i for i, line in enumerate(lines) if line.startswith(WRAPPER_ANCHOR)), None)
    if index is None:
        if 'lumen' in text.lower():
            print('host      : WARNING: %s changed shape (Lumen launch anchor not found); '
                  'it may still start Lumen. Not patched.' % path, file=sys.stderr)
        return False
    backup = path.with_name(path.name + '.luna-orig')
    try:
        if not backup.exists():
            shutil.copy2(path, backup)
        lines.insert(index, wrapper_patch_block())
        mode = path.stat().st_mode
        tmp = path.with_name(path.name + '.luna-tmp')
        tmp.write_text(''.join(lines))
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except OSError as error:
        print('host      : WARNING: could not patch %s: %s' % (path, error), file=sys.stderr)
        return False
    return True


def unpatch_wrapper():
    path = wrapper_path()
    if not wrapper_patched():
        return False
    out, skipping = [], False
    for line in path.read_text(errors='replace').splitlines(keepends=True):
        if line.startswith(WRAPPER_BEGIN):
            skipping = True
        if not skipping:
            out.append(line)
        if skipping and line.startswith(WRAPPER_END):
            skipping = False
    try:
        mode = path.stat().st_mode
        tmp = path.with_name(path.name + '.luna-tmp')
        tmp.write_text(''.join(out))
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except OSError as error:
        print('host      : WARNING: could not unpatch %s: %s' % (path, error), file=sys.stderr)
        return True
    return False


def stop_lumen_process():
    try:
        subprocess.run(['pkill', '-u', str(os.getuid()), '-x', 'lumen'], capture_output=True, text=True)
    except FileNotFoundError:
        pass


def host_status():
    binary = host_binary()
    version = ''
    if binary.is_file():
        try:
            version = sha256(binary)[:12]
        except OSError:
            version = ''
    try:
        legacy = json.loads((luna_home() / 'legacy-plugins.json').read_text())
    except (OSError, ValueError):
        legacy = []
    return {
        'active': active_host(),
        'display': host_label(),
        'standalone': active_host() == 'luna',
        'home': str(luna_home()),
        'addons': str(luna_home() / 'addons' if active_host() == 'luna' else addons_dir()),  # the ACTIVE host's real path
        'state': str(luna_tools_dir()),
        'lunaHostAddons': str(luna_home() / 'addons'),
        'lunaState': str(luna_state_dir()),
        'binary': {'path': str(binary), 'present': binary.is_file(), 'version': version},
        'unit': {'path': str(host_unit_path()), 'present': host_unit_path().is_file()},
        'units': {'luna': unit_state(HOST_UNIT), 'lumen': unit_state(LUMEN_UNIT)},
        'legacyPlugins': legacy if isinstance(legacy, list) else [],
        'lumenDir': str(lumen_root()),
        'wrapperPatched': wrapper_patched(),
        'lumenRunning': lumen_running(),
    }


def _write_host_state(active, previous):
    path = host_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json_file(path, {'active': active, 'previous': previous, 'at': int(time.time())})


def _carry_state(source, dest, link_big=False):
    """Copy the small machine-local state (json files, machine-id) between tools
    dirs without overwriting. Large Steam client backups/cache are linked."""
    if not source.is_dir():
        return
    dest.mkdir(parents=True, exist_ok=True)
    for path in sorted(source.iterdir()):
        target = dest / path.name
        if path.is_file() and not path.is_symlink() and (path.suffix == '.json' or path.name == 'machine-id'):
            if not target.exists():
                shutil.copy2(path, target)
        elif link_big and path.is_dir() and path.name in ('steam-client-backups', 'steam-client-cache'):
            if not target.exists() and not target.is_symlink():
                target.symlink_to(path)


def _refresh_tooling_after_switch():
    write_tooling()
    _, service, _timer = unit_paths()
    if service.is_file():
        cmd_timer(argparse.Namespace(action='on'))


def cmd_host(options):
    action = getattr(options, 'host_action', 'status')
    if action == 'status':
        print(json.dumps(host_status(), sort_keys=True))
        return EXIT_OK
    if action == 'cutover':
        reasons = [reason for reason in refuse_reasons(for_install=True) if 'Steam is running' in reason]
        if not host_binary().is_file() or not (luna_home() / 'lua').is_dir() or not host_unit_path().is_file():
            reasons.append('luna-host is not installed in %s; run: luna install' % luna_home())
        elif HOST_UNIT_MARK not in host_unit_path().read_text(errors='replace'):
            reasons.append('%s is not Luna-managed; refusing to enable it' % host_unit_path())
        if reasons:
            print('REFUSING cutover:')
            for reason in reasons:
                print('  - %s' % reason)
            return EXIT_REFUSED
        previous_tools = luna_tools_dir()
        _carry_state(lumen_tools_dir(), luna_home() / 'state', link_big=True)
        if previous_tools != lumen_tools_dir():
            _carry_state(previous_tools, luna_home() / 'state')
        result = systemctl_user('disable', '--now', LUMEN_UNIT)
        if result.returncode != 0:
            print('host      : note: could not disable %s (%s)'
                  % (LUMEN_UNIT, (result.stderr or result.stdout).strip() or 'no output'))
        systemctl_user('daemon-reload')
        stop_lumen_process()
        result = systemctl_user('enable', '--now', HOST_UNIT)
        if result.returncode != 0:
            print('FAILED to start %s: %s' % (HOST_UNIT, (result.stderr or result.stdout).strip()),
                  file=sys.stderr)
            systemctl_user('enable', '--now', LUMEN_UNIT)
            print('Lumen was re-enabled; nothing changed.', file=sys.stderr)
            return 1
        _write_host_state('luna', 'lumen')
        patch_wrapper()
        _refresh_tooling_after_switch()
        print('host      : cutover done; luna-host is the active plugin host (restart Steam to attach)')
        print('rollback  : luna host rollback')
        return EXIT_OK
    if action == 'rollback':
        systemctl_user('disable', '--now', HOST_UNIT)
        result = systemctl_user('enable', '--now', LUMEN_UNIT)
        if result.returncode != 0:
            print('FAILED to start %s: %s' % (LUMEN_UNIT, (result.stderr or result.stdout).strip()),
                  file=sys.stderr)
            systemctl_user('enable', '--now', HOST_UNIT)
            return 1
        _carry_state(luna_home() / 'state', lumen_tools_dir())
        _write_host_state('lumen', 'luna')
        undo_standalone_setup()
        unpatch_wrapper()
        _refresh_tooling_after_switch()
        print('host      : rolled back; Lumen is the active plugin host (restart Steam to attach)')
        return EXIT_OK
    if action == 'patch-wrapper':
        ok = patch_wrapper()
        print('wrapper   : %s' % ('patched' if ok else 'NOT patched'))
        return EXIT_OK if ok else 1
    if action == 'unpatch-wrapper':
        unpatch_wrapper()
        print('wrapper   : unpatched')
        return EXIT_OK
    print('unknown host action: %s' % action, file=sys.stderr)
    return 2


def cmd_hook(options):
    """Retired: kept so old muscle memory gets a pointer instead of a traceback."""
    if options.action == 'repair':
        return cmd_install(argparse.Namespace(only=[], force=True, dry_run=False, keep_steam_client=True,
                                              no_steam_machine=False, migrate_steam_machine=False))
    retire_lumen_hook()
    print('The automatic Lumen recovery hook is retired. To stop depending on Lumen, '
          'see: luna host status')
    return EXIT_OK


def _update_lock_held_by_other():
    """A live luna command (a Steam lock/unlock/refresh, another update) owns the writer lock."""
    owner, _started = _lock_owner(_update_lock())
    return bool(owner and owner != os.getpid() and pid_is_live_luna(owner))


def cmd_autoupdate(options):
    """Timer entry point: check quietly and apply when Steam is closed.

    Modes come from the Health page switch (data/luna.json -> auto_update):
    "off" does nothing unless a manual update was queued, "notify" only
    records what it found, "install" applies a newer release. A queued update
    (pending.json) is honoured in any mode but still needs Steam closed.
    """
    tools = luna_tools_dir()
    pending_path = tools / 'pending.json'
    result_path = tools / 'autoupdate.json'
    if getattr(options, 'wait_steam', False):
        # luna-apply.service entry point: a queued job starts seconds after
        # Steam exits, not on the next 30-minute tick. Steam stays untouched.
        wait = wait_for_steam_closed(probe=lambda: bool(steam_running() or _update_lock_held_by_other()))
        if wait == 'timeout':
            message = 'waited %d hours for Steam to stay closed; the update is still queued' % (
                APPLY_WAIT_LIMIT // 3600)
            if read_lumen_pending():
                record_lumen_result('failed', message)
            _notify('Luna update not applied', message)
            print(message, file=sys.stderr)
            return 1
        if wait != 'ready':
            return EXIT_OK
        _notify('Luna update', 'Steam is closed; applying the queued update')
    lock_path = _update_lock()
    prefs = read_json_file(data_dir() / 'luna.json') or {}
    mode = str(prefs.get('auto_update') or 'off')
    pending = read_json_file(pending_path) or {}
    pending_refresh = read_refresh_pending()
    verbose = bool(getattr(options, 'verbose', False))
    write_heartbeat()
    # Self-heal the update block first: third-party installers delete steam.cfg,
    # and the timer is what notices it while Steam is closed or running.
    try:
        heal_steam_lock()
    except Exception as error:  # noqa: BLE001 - healing must never break the timer
        print('heal      : failed: %s' % error, file=sys.stderr)
    extra_result = {}
    holding_lock = False

    def record(payload):
        # Order for "Update everything": the Luna add-ons first (above), then the
        # queued Lumen update, whose re-attach installs that fresh Luna. A failed
        # Luna step skips it; Steam running just leaves it queued.
        if payload.get('result') != 'failed' and holding_lock:
            apply_queued_lumen(extra_result)
        payload.update(extra_result)
        payload['mode'] = mode
        payload['at'] = int(time.time())
        snapshot = steam_state()
        payload['steamBuild'] = snapshot['build']
        payload['steamPinned'] = snapshot['pinned']
        payload['steamDrift'] = steam_drift_note()
        queued = read_steam_pending()
        payload['steamPending'] = str(queued.get('action') or '') if queued else ''
        write_json_file(result_path, payload)
        if payload.get('result') in ('applied', 'failed'):
            _notify('Luna update %s' % payload['result'],
                    payload.get('reason') or payload.get('version') or '')
        if verbose:
            print(json.dumps(payload, sort_keys=True))
        return EXIT_OK

    if (mode not in ('notify', 'install') and not pending and not pending_refresh and not read_lumen_pending()
            and not lumen_running_record()):
        return EXIT_OK
    acquired, reason = _acquire_lock(lock_path)
    if not acquired:
        return record({'result': 'deferred', 'reason': reason})
    refresh_message = ''
    holding_lock = True
    try:
        recovery = recover_interrupted_lumen()
        if recovery:
            extra_result['lumenRecovery'] = '%s: %s' % recovery
            if recovery[0] == 'busy':
                return record({'result': 'deferred', 'reason': recovery[1]})
        # An explicit refresh request (Health page or `luna refresh --dry-run`
        # planned it, the timer applies it): Steam to Valve's latest, then pin
        # and cache, then hand the Luna add-ons to the normal bundle path
        # below so the bridge and payloads land verified. Needs Steam closed
        # like every other mutation; otherwise it waits for the next run.
        if read_refresh_pending():
            if steam_hard_lock_active():
                return record({'result': 'deferred', 'refreshPending': True,
                               'reason': 'the ubuntu12 hard lock is active; run: luna refresh --unlock-first'})
            reasons = refuse_reasons(for_install=True)
            if reasons:
                return record({'result': 'deferred', 'reason': reasons[0],
                               'refreshPending': True})
            ok, message, build = _refresh_steam_side()
            if not ok:
                return record({'result': 'error', 'reason': message,
                               'refreshPending': True})
            refresh_message = message
            extra_result['refresh'] = message
            try:
                refresh_pending_path().unlink()
            except OSError:
                pass
            pending = {'tag': '', 'requestedAt': int(time.time())}
            write_json_file(pending_path, pending)
        # The timer only runs while Steam is closed, which is exactly when a
        # pinned client can be restored safely. In install mode (explicit
        # consent via pin + mode) a drifted client is recovered from the local
        # cache before anything else; every mode reports the drift.
        snapshot = steam_state()
        if (snapshot['pinned'] and snapshot['cacheReady']
                and (snapshot['packagesMatch'] is False or snapshot.get('binariesMatch') is False)
                and (mode == 'install' or pending)):
            ok, message = _recover_steam_client(snapshot['pinned'])
            if ok:
                snapshot = steam_state()
            else:
                return record({'result': 'error', 'reason': message})
        # Queued Steam work (Health menu or `luna steam update|cache`) runs here:
        # the timer only fires while Steam is closed, which both need.
        pending_steam = read_steam_pending()
        if pending_steam:
            steam_job = pending_steam.get('action')
            if steam_job == 'update':
                ok, message = _apply_steam_update()
            elif steam_job == 'cache':
                build = steam_client_build()
                if not build:
                    return record({'result': 'error',
                                    'reason': 'cannot determine the client build for caching'})
                try:
                    _cache_client_packages(build)
                    ok, message = True, 'cached Steam client %s' % build
                except OSError as error:
                    ok, message = False, 'could not cache the client: %s' % error
                if ok:
                    try:
                        steam_pending_path().unlink()
                    except OSError:
                        pass
            else:
                try:
                    steam_pending_path().unlink()
                except OSError:
                    pass
                ok, message = True, 'dropped unknown queued Steam task'
            if not ok:
                return record({'result': 'error', 'reason': message})
            # Fall through: a Steam change may come with owed Luna updates.
        # A check is read-only (gh only): notify mode must report even while
        # Steam runs. Only an actual install needs Steam and the sidecar gone.
        report = check_updates(force=True)
        installed = report.get('installed') or '?'
        latest = report.get('latest') or ''
        if not latest:
            return record({'result': 'error', 'reason': report.get('checkError') or 'no release found'})
        target = str(pending.get('tag') or '') if pending else ''
        if not target:
            target = latest
        if not pending and version_key(target) <= version_key(installed):
            return record({'result': 'current', 'version': installed})
        if mode == 'notify' and not pending:
            return record({'result': 'available', 'version': target, 'installed': installed})
        reasons = refuse_reasons(for_install=True)
        if reasons:
            return record({'result': 'deferred', 'reason': reasons[0]})

        work = Path(tempfile.mkdtemp(prefix='luna-autoupdate-'))
        try:
            bundle, error = download_release(target, work)
            if bundle is None:
                return record({'result': 'error', 'reason': error, 'version': target})
            code = install_bundle(bundle)
            if code == 0:
                globals()['_LUNA_REPLACED'] = True
        finally:
            shutil.rmtree(work, ignore_errors=True)
        if code == 0 and pending_path.exists():
            pending_path.unlink()
        return record({'result': 'applied' if code == 0 else 'failed',
                       'version': target, 'installedBefore': installed, 'exit': code})
    finally:
        _release_lock(lock_path)


# Files under ~/.local/share/slssteam-integrations/ that make up a backup.
# Volatile compressor state (jobs, logs, queue, status) is deliberately left
# out: it is rebuilt on the next run. Everything here is small, local JSON.
BACKUP_PATHS = (
    'playtime',
    'presence.json',
    'hltb.json',
    'hltb-library.json',
    'luna.json',
    'sgdb.json',
    'compressor/settings.json',
)


def _backup_entries():
    """(relative path, absolute path) pairs that exist right now."""
    base = data_dir()
    entries = []
    for rel in BACKUP_PATHS:
        path = base / rel
        if path.is_dir():
            for child in sorted(path.rglob('*')):
                if child.is_file():
                    entries.append((str(child.relative_to(base)), child))
        elif path.is_file():
            entries.append((rel, path))
    return entries


def cmd_backup(options):
    """Write one archive with the Luna user data plus a hash manifest."""
    entries = _backup_entries()
    if not entries:
        print('nothing to back up: %s holds no Luna data' % data_dir())
        return EXIT_OK
    out_dir = Path(options.out).expanduser() if options.out else Path(tempfile.gettempdir())
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime('%Y%m%d-%H%M%S')
    archive = out_dir / ('luna-backup-%s.tar.gz' % stamp)
    manifest = {'created': int(time.time()),
                'createdBy': installed_version(),
                'files': {rel: sha256(path) for rel, path in entries}}
    with tempfile.TemporaryDirectory(prefix='luna-backup-') as work:
        stage = Path(work) / 'stage'
        (stage / 'data').mkdir(parents=True)
        for rel, path in entries:
            dest = stage / 'data' / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dest)
        (stage / 'manifest.json').write_text(json.dumps(manifest, indent=2, sort_keys=True) + '\n')
        with tarfile.open(archive, 'w:gz') as tar:
            tar.add(stage / 'manifest.json', arcname='luna-backup/manifest.json')
            tar.add(stage / 'data', arcname='luna-backup/data')
    print('backup : %s (%d file(s))' % (archive, len(entries)))
    print('note   : includes the SteamGridDB API key (sgdb.json); keep the archive private')
    return EXIT_OK


def cmd_restore(options):
    """Restore a backup archive after validating its manifest.

    Refuses while Steam or the sidecar runs (same rule as install), and never
    overwrites a file that is newer than the archived copy unless --force.
    """
    archive = Path(options.archive).expanduser() if options.archive else None
    if not archive or not archive.is_file():
        print('backup archive not found: %s' % (options.archive or ''), file=sys.stderr)
        return 2
    if steam_running() or lumen_running():
        print('REFUSING to restore: Steam or the Lumen sidecar is running. '
              'Close it completely (Steam -> Exit) first.')
        return EXIT_REFUSED
    with tempfile.TemporaryDirectory(prefix='luna-restore-') as work:
        extract_dir = Path(work) / 'unpacked'
        extract_dir.mkdir()
        try:
            _safe_extract(archive, extract_dir)
        except (tarfile.TarError, ValueError, OSError) as error:
            print('REFUSING: cannot safely unpack the archive: %s' % error, file=sys.stderr)
            return 1
        roots = [entry for entry in extract_dir.iterdir() if entry.is_dir()]
        root = roots[0] if len(roots) == 1 else extract_dir
        manifest_path = root / 'manifest.json'
        data_root = root / 'data'
        if not manifest_path.is_file() or not data_root.is_dir():
            print('REFUSING: not a Luna backup (manifest.json + data/ required)', file=sys.stderr)
            return 1
        try:
            manifest = json.loads(manifest_path.read_text())
            pinned = manifest.get('files') or {}
        except ValueError:
            print('REFUSING: the backup manifest is not valid JSON', file=sys.stderr)
            return 1
        if not isinstance(pinned, dict):
            print('REFUSING: the backup manifest has no file list', file=sys.stderr)
            return 1
        problems, restored, skipped = [], 0, 0
        for rel in sorted(pinned):
            if '..' in Path(rel).parts or rel.startswith('/'):
                problems.append('unsafe path in manifest: %r' % rel)
                continue
            source = data_root / rel
            if not source.is_file():
                problems.append('archive is missing backed-up file: %s' % rel)
                continue
            if sha256(source) != pinned[rel]:
                problems.append('archived file does not match the manifest: %s' % rel)
                continue
            dest = data_dir() / rel
            if dest.is_file() and not options.force:
                try:
                    newer = dest.stat().st_mtime > source.stat().st_mtime
                except OSError:
                    newer = False
                if newer:
                    print('skip   : %s (current copy is newer; re-run with --force to overwrite)' % rel)
                    skipped += 1
                    continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_name(dest.name + '.luna-tmp')
            shutil.copy2(source, tmp)
            os.replace(tmp, dest)
            restored += 1
        if problems:
            print('REFUSING: the backup did not validate:')
            for problem in problems:
                print('  - %s' % problem, file=sys.stderr)
            return 1
    print('restored %d file(s), skipped %d newer file(s)' % (restored, skipped))
    return EXIT_OK


    print('restored %d file(s), skipped %d newer file(s)' % (restored, skipped))
    return EXIT_OK


# ---------------------------------------------------------------------------
# Steam client version management.
#
# A Steam client update can move the interfaces SLSsteam hooks, breaking
# installs and launches until the integration catches up. Luna cannot stop
# Valve's updater on its own, but it can pin the working build, keep a local
# cache of its packages, and put that build back with Steam closed.
# Everything here is machine-local (luna_tools_dir is excluded from sync):
# each machine pins the build it actually validated.

def steam_home():
    native = home() / '.local/share/Steam'
    alternate = home() / '.steam/steam'
    return alternate.resolve() if not native.exists() and alternate.is_dir() else native


def steam_tools_files():
    """SLSsteam binaries whose unexpected change invalidates a pin."""
    base = home() / '.local/share/SLSsteam'
    return {'SLSsteam.so': base / 'SLSsteam.so',
            'library-inject.so': base / 'library-inject.so'}


def steam_client_build():
    """Verified installed binaries, then the on-disk manifest, then runtime logs.

    A package manifest alone does NOT prove that its client was extracted.
    The install receipt is preferred only while the core executable hashes match.
    Newer clients log "Client version: 0" at startup, so a zero report is
    treated as "no information" rather than a build number.
    """
    receipt = read_json_file(luna_tools_dir() / 'steam-client-installed.json') or {}
    core = receipt.get('core') or {}
    if core:
        try:
            if all(sha256(steam_home() / _steam_payload_path(name)) == digest
                   for name, digest in core.items()):
                return str(receipt.get('build') or '')
        except (OSError, ValueError):
            pass
    try:
        manifest = (steam_home() / 'package' / 'steam_client_ubuntu12.manifest').read_text(
            encoding='utf-8', errors='replace')
        found = re.findall(r'"version"\s+"(\d+)"', manifest)
        manifest_build = next((build for build in reversed(found) if build != '0'), '')
        if manifest_build:
            return manifest_build
    except OSError:
        pass
    log = steam_home() / 'logs' / 'console_log.txt'
    try:
        size = log.stat().st_size
    except OSError:
        size = 0
    if size:
        try:
            with open(log, 'rb') as handle:
                handle.seek(max(0, size - 65536))
                tail = handle.read().decode('utf-8', errors='replace')
            found = [build for build in re.findall(r'Client version:\s*(\d+)', tail)
                     if build != '0']
            if found:
                return found[-1]
        except OSError:
            pass
    return ''


STEAM_CFG_BLOCK = ('BootStrapperInhibitAll=enable\n'
                     'BootStrapperForceSelfUpdate=disable\n')


# The hard lock (`luna steam lock`) also stops the launch-time update check and
# is written to both spellings of the file name that Steam's bootstrapper reads.
STEAM_HARD_CFG_BLOCK = ('BootStrapperInhibitAll=enable\n'
                        'BootStrapperInhibitUpdateOnLaunch=enable\n'
                        'BootStrapperForceSelfUpdate=false\n')
_STEAM_CFG_KEYS = re.compile(
    r'\s*BootStrapper(?:InhibitAll|InhibitUpdateOnLaunch|ForceSelfUpdate)\s*=', re.I)


def steam_cfg_path():
    return steam_home() / 'steam.cfg'


def steam_cfg_paths():
    """Both spellings the bootstrapper honors (distinct files on Linux)."""
    return [steam_home() / 'Steam.cfg', steam_home() / 'steam.cfg']


def steam_updates_blocked():
    try:
        content = steam_cfg_path().read_text()
    except OSError:
        return False
    return bool(re.search(r'^\s*BootStrapperInhibitAll\s*=\s*enable\s*$', content, re.M | re.I))


def ensure_steam_cfg_block(paths=None, block=None):
    """Write the bootstrapper inhibit block; Steam honors it on next launch.

    While a hard lock is recorded the hard block goes to both cfg files, so
    every code path that re-applies the block keeps the lock intact.
    """
    hard = steam_hard_lock_active()
    if block is None:
        block = STEAM_HARD_CFG_BLOCK if hard else STEAM_CFG_BLOCK
    if paths is None:
        paths = steam_cfg_paths() if hard else [steam_cfg_path()]
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            previous = path.read_text()
        except FileNotFoundError:
            previous = ''
        lines = [line for line in previous.splitlines() if not _STEAM_CFG_KEYS.match(line)]
        path.write_text('\n'.join(lines + block.splitlines()) + '\n')
    return paths[-1]


def remove_steam_cfg_block(paths=None):
    """Remove the block only if the file holds exactly what Luna wrote.

    Returns True when every file that held a Luna block was removed.
    """
    removed, kept = False, False
    for path in (paths if paths is not None else [steam_cfg_path()]):
        try:
            content = path.read_text()
        except OSError:
            continue
        if content in (STEAM_CFG_BLOCK, STEAM_HARD_CFG_BLOCK):
            try:
                path.unlink()
                removed = True
            except OSError:
                kept = True
        elif any(_STEAM_CFG_KEYS.match(line) for line in content.splitlines()):
            kept = True
    return removed and not kept


def pacman_conf_path():
    return Path(os.environ.get('LUNA_PACMAN_CONF', '/etc/pacman.conf'))


def pacman_steam_state():
    """Is the Steam client pacman-managed, and is it ignored on updates?

    No root needed: `pacman -Q` answers the first, the conf file the second.
    """
    managed, ignored = False, False
    try:
        result = subprocess.run(['pacman', '-Q', 'steam'],
                                capture_output=True, text=True, timeout=15)
        managed = result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return managed, ignored
    if not managed:
        return managed, ignored
    try:
        content = pacman_conf_path().read_text()
    except OSError:
        return managed, ignored
    for line in content.splitlines():
        stripped = line.split('#', 1)[0].strip()
        if not stripped or '=' not in stripped:
            continue
        key, _, value = stripped.partition('=')
        if key.strip().lower() == 'ignorepkg' and 'steam' in value.split():
            ignored = True
    return managed, ignored


def steam_pin_path():
    return luna_tools_dir() / 'steam-pin.json'


def steam_cache_dir():
    return luna_tools_dir() / 'steam-client-cache'


def steam_pin_copy_path():
    return luna_state_dir() / 'steam-pin.json'


def read_steam_pin():
    pin = read_json_file(steam_pin_path())
    if not isinstance(pin, dict) or not pin:
        pin = read_json_file(steam_pin_copy_path())  # the tools dir was replaced by a base install
    return pin if isinstance(pin, dict) else {}


def sync_steam_pin_copy():
    """Keep a copy of the pin outside the Lumen tree (best effort, quiet)."""
    try:
        pin = read_json_file(steam_pin_path())
        if isinstance(pin, dict) and pin and read_json_file(steam_pin_copy_path()) != pin:
            write_json_file(steam_pin_copy_path(), pin)
    except OSError:
        pass


def remove_steam_pin():
    for path in (steam_pin_path(), steam_pin_copy_path()):
        try:
            path.unlink()
        except OSError:
            pass


def steam_hard_lock_active():
    """Is the ubuntu12 rollback + read-only package lock recorded in the pin?"""
    lock = read_steam_pin().get('hardLock')
    return isinstance(lock, dict) and bool(lock)


def new_steam_pin(build):
    """A pin record for `build`; carries an active hard lock forward."""
    pin = {'clientBuild': str(build), 'slssteam': slssteam_hashes(),
           'at': int(time.time()), 'by': installed_version()}
    lock = read_steam_pin().get('hardLock')
    if isinstance(lock, dict) and lock:
        pin['hardLock'] = lock
    return pin


@contextlib.contextmanager
def steam_package_writable():
    """Let Luna write into SDIR/package despite the hard lock's read-only bit.

    A no-op when the directory is writable and no hard lock is recorded;
    otherwise (Luna's hard lock, or a read-only bit left by a manual/community
    lock) it is made writable and put back to read-only afterwards, even if
    the work raises.
    """
    package = steam_home() / 'package'
    relock = package.is_dir() and (steam_hard_lock_active() or not os.access(package, os.W_OK))
    if relock:
        package.chmod(package.stat().st_mode | stat.S_IWUSR)
    try:
        yield
    finally:
        if relock and package.is_dir():
            package.chmod(package.stat().st_mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))


def slssteam_hashes():
    """Current hashes of the SLSsteam binaries (missing -> '')."""
    hashes = {}
    for name, path in steam_tools_files().items():
        try:
            hashes[name] = sha256(path) if path.is_file() else ''
        except OSError:
            hashes[name] = ''
    return hashes


def _package_stats(root, with_hash=False):
    """Fingerprint of a package tree: {rel: [size, mtime_ns]} (+ sha256)."""
    stats = {}
    for path in sorted(root.rglob('*')):
        if path.is_file():
            try:
                st = path.stat()
                entry = [st.st_size, st.st_mtime_ns]
                if with_hash:
                    entry.append(sha256(path))
                stats[str(path.relative_to(root))] = entry
            except OSError:
                pass
    return stats


def _cache_manifest():
    manifest = read_json_file(steam_cache_dir() / 'manifest.json')
    return manifest if isinstance(manifest, dict) else {}


def _stats_match(left, right):
    """Size+mtime comparison (hashes, when present, are ignored: fast path)."""
    if not set(left).issubset(right):
        return False
    return all(left[key][:2] == right[key][:2] for key in left)


def packages_match_cache():
    """Do the installed packages still match the cached build?

    None when there is no usable cache; otherwise a size+mtime comparison,
    which is instant and catches any client update or partial download.
    """
    manifest = _cache_manifest()
    if not manifest or not (steam_cache_dir() / 'package').is_dir():
        return None
    target = steam_home() / 'package'
    if not target.is_dir():
        return False
    return _stats_match(manifest, _package_stats(target))


def _package_read_only():
    try:
        return not (steam_home() / 'package').stat().st_mode & stat.S_IWUSR
    except OSError:
        return False


def steam_state():
    """Everything the menu and preflight report about the Steam client."""
    build = steam_client_build()
    pin = read_steam_pin()
    pinned = str(pin.get('clientBuild') or '')
    cache_marker = steam_cache_dir() / 'build.txt'
    try:
        cached = cache_marker.read_text().strip()
    except OSError:
        cached = ''
    cache_ready = bool(cached) and (steam_cache_dir() / 'package').is_dir()
    packages_match = packages_match_cache() if cache_ready else None
    receipt = read_json_file(luna_tools_dir() / 'steam-client-installed.json') or {}
    binaries_match = None
    if receipt.get('core'):
        try:
            binaries_match = all(sha256(steam_home() / _steam_payload_path(name)) == digest
                                 for name, digest in receipt['core'].items())
        except (OSError, ValueError):
            binaries_match = False
    if cache_ready:
        match = packages_match is True and binaries_match is not False and build == pinned
    else:
        match = bool(build and pinned and build == pinned)
    current_hashes = slssteam_hashes()
    pinned_hashes = pin.get('slssteam') or {}
    tools_drift = [name for name, digest in current_hashes.items()
                   if digest and pinned_hashes.get(name) and digest != pinned_hashes[name]]
    pacman_managed, pacman_ignored = pacman_steam_state()
    return {
        'build': build,
        'pinned': pinned,
        'match': match,
        'packagesMatch': packages_match,
        'binariesMatch': binaries_match,
        'updatesBlocked': steam_updates_blocked(),
        'slssteam': current_hashes,
        'slssteamDrift': tools_drift,
        'cachedBuild': cached,
        'cacheReady': cache_ready,
        'pacmanManaged': pacman_managed,
        'pacmanIgnored': pacman_ignored,
        'hardLock': bool(pin.get('hardLock')),
        'packageReadOnly': _package_read_only(),
    }


def cached_client_build():
    try:
        return (steam_cache_dir() / 'build.txt').read_text().strip()
    except OSError:
        return ''


def steam_pending_path():
    return luna_tools_dir() / 'pending-steam.json'


def read_steam_pending():
    pending = read_json_file(steam_pending_path())
    return pending if isinstance(pending, dict) else {}


def steam_sh_path():
    override = os.environ.get('LUNA_STEAM_SH')
    if override:
        return Path(override)
    return steam_home() / 'steam.sh'


def _steam_force_update():
    """Run the Steam bootstrapper's forced update and exit.

    Called only with Steam closed. Returns (ok, message). The update block
    is lifted for the run and re-applied afterwards either way.
    """
    if steam_hard_lock_active():
        return False, ('the Steam hard lock is active (package/ read-only, ubuntu12 rollback); '
                       'run: luna steam unlock --now to let Steam update')
    try:
        _steam_client_closed()
    except ValueError as error:
        return False, str(error)
    had_block = steam_updates_blocked()
    try:
        steam_cfg_path().unlink()
    except OSError:
        pass
    try:
        result = subprocess.run([str(steam_sh_path()), '-forcesteamupdate',
                                 '-forcepackagedownload', '-exitsteam'],
                                capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.SubprocessError, subprocess.TimeoutExpired) as error:
        result = None
        failure = str(error)
    if had_block:
        try:
            ensure_steam_cfg_block()
        except OSError:
            pass
    if result is None:
        return False, 'could not run the Steam updater: %s' % failure
    if result.returncode != 0:
        tail = (result.stderr.strip() or result.stdout.strip())[-500:]
        return False, 'Steam updater exited %d: %s' % (result.returncode, tail)
    # The bootstrapper relaunches Steam after a successful update even with
    # -exitsteam. Shut it back down gracefully: the caller asked for Steam to
    # stay closed. Never kill processes; report if it is still up.
    note = ('; block re-applied' if had_block
            else '; updates currently allowed - pin the new build to block them')
    if steam_running():
        try:
            subprocess.run([str(steam_sh_path()), '-shutdown'],
                           capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError, subprocess.TimeoutExpired):
            pass
        deadline = time.monotonic() + 60
        while deadline > time.monotonic():
            if not (steam_running()):
                break
            time.sleep(2)
        if steam_running():
            return False, ('Steam updated but is still running after a shutdown request; '
                           'close it completely (Steam -> Exit) before pinning or installing' + note)
    return True, 'Steam client updated by Valve' + note


def prune_steam_client_backups(keep=5):
    """Keep only the newest `keep` client rollback backups.

    Each successful client install/recovery retains a full rollback copy, so
    without pruning this directory grows without bound. Only directories whose
    names are nanosecond timestamps (written by this installer) are touched;
    anything else is left alone.
    """
    backups = luna_tools_dir() / 'steam-client-backups'
    try:
        entries = [entry for entry in backups.iterdir()
                   if entry.is_dir() and entry.name.isdigit()]
    except OSError:
        return 0
    entries.sort(key=lambda entry: int(entry.name))
    removed = 0
    for entry in entries[:-keep] if len(entries) > keep else []:
        try:
            shutil.rmtree(entry, ignore_errors=False)
            removed += 1
        except OSError:
            pass
    return removed


def _cache_client_packages(build):
    """Snapshot the installed package tree into the local cache.

    The cached manifest must describe `build`; a stale on-disk manifest (or a
    mislabeled earlier cache) is refused instead of silently relabeled.
    """
    source = steam_home() / 'package'
    cache = steam_cache_dir()
    try:
        installed_manifest = (source / STEAM_MANIFEST_NAME).read_text(
            encoding='utf-8', errors='replace')
    except OSError:
        raise ValueError('no Steam package manifest at %s' % (source / STEAM_MANIFEST_NAME))
    found = re.findall(r'"version"\s+"(\d+)"', installed_manifest)
    installed_build = next((item for item in reversed(found) if item != '0'), '')
    if installed_build != build:
        raise ValueError('installed packages describe build %s, not %s; refusing to mislabel the cache'
                         % (installed_build or 'unknown', build))
    if cache.exists():
        shutil.rmtree(cache, ignore_errors=True)
    shutil.copytree(source, cache / 'package', symlinks=False)
    # copytree preserves the hard lock's read-only bit; the cache must stay deletable.
    (cache / 'package').chmod((cache / 'package').stat().st_mode | stat.S_IWUSR)
    (cache / 'build.txt').write_text(build + '\n')
    write_json_file(cache / 'manifest.json', _package_stats(cache / 'package', with_hash=True))
    return cache


STEAM_COMPAT_BUILD = '1791918634'
STEAM_COMPAT_MANIFEST_SHA256 = 'a87699c36d28721af5ca6ade7276f506d22a11f9829747167d96845fc080afa5'
STEAM_ARCHIVE_BASE = ('https://web.archive.org/web/20261014104109if_/'
                      'http://media.steampowered.com/client/')
STEAM_MANIFEST_NAME = 'steam_client_ubuntu12.manifest'
STEAM_CORE_FILES = ('ubuntu12_32/steam', 'ubuntu12_32/steamclient.so',
                    'ubuntu12_64/steamwebhelper')


def _steam_payload_path(name):
    """Valve ZIPs use both slash styles. Never accept account/game/cache paths."""
    path = PurePosixPath(name.replace('\\', '/'))
    protected = {'config', 'userdata', 'steamapps', 'appcache', 'logs', 'depotcache', 'package'}
    if (path.is_absolute() or not path.parts or '..' in path.parts
            or ':' in str(path) or path.parts[0].startswith('.')
            or path.parts[0].lower() in protected):
        raise ValueError('unsafe/protected Steam payload path: %s' % name)
    return path


def _steam_package_entries(data, want):
    manifest = parse_vdf(data.decode('utf-8'))
    body = manifest.get('ubuntu12') or {}
    if body.get('version') != want:
        raise ValueError('Steam manifest is not build %s' % want)
    entries = []
    for values in body.values():
        if not isinstance(values, dict) or 'file' not in values:
            continue
        compressed = 'zipvz' in values
        name = values.get('zipvz') if compressed else values['file']
        digest = values.get('sha2vz' if compressed else 'sha2', '')
        if (not re.fullmatch(r'[A-Za-z0-9_.-]+', name or '')
                or not re.fullmatch(r'[0-9a-f]{64}', digest)):
            raise ValueError('unsafe package name or missing SHA256 in Steam manifest')
        entries.append((name, digest, compressed))
    if not entries:
        raise ValueError('Steam manifest has no verified packages')
    return entries


def _download_steam_file(url, destination, expected, limit=512 * 1024 * 1024):
    request = urllib.request.Request(url, headers={'User-Agent': 'Luna-Steam-Installer'})
    with urllib.request.urlopen(request, timeout=60) as source, open(destination, 'wb') as out:
        size = 0
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > limit:
                raise ValueError('Steam download exceeds size limit')
            out.write(chunk)
    if sha256(destination) != expected:
        raise ValueError('Steam download SHA256 mismatch: %s' % url)


def _stage_steam_payload(packages, data, want, destination):
    """Verify each package, decode VZip and extract ONLY official client files."""
    entries = {}
    total = 0
    destination.mkdir()
    for name, expected, compressed in _steam_package_entries(data, want):
        source = packages / name
        if source.is_symlink() or sha256(source) != expected:
            raise ValueError('Steam package SHA256 mismatch: %s' % name)
        raw = source.read_bytes()
        if compressed:
            if raw[:3] != b'VZa' or raw[-2:] != b'zv' or len(raw) < 22:
                raise ValueError('invalid Steam VZip header: %s' % name)
            prop = raw[7]
            dictionary = struct.unpack('<I', raw[8:12])[0]
            if prop >= 225 or dictionary > 128 * 1024 * 1024:
                raise ValueError('invalid Steam LZMA properties')
            filters = [{'id': lzma.FILTER_LZMA1, 'dict_size': dictionary,
                        'lc': prop % 9, 'lp': (prop // 9) % 5, 'pb': prop // 45}]
            crc, size = struct.unpack('<II', raw[-10:-2])
            if size > 1024 * 1024 * 1024:
                raise ValueError('Steam archive exceeds size limit')
            decoder = lzma.LZMADecompressor(format=lzma.FORMAT_RAW, filters=filters)
            raw = decoder.decompress(raw[12:-10], max_length=size + 1)
            if not decoder.eof or len(raw) != size or zlib.crc32(raw) != crc:
                raise ValueError('Steam VZip integrity check failed: %s' % name)
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            for item in archive.infolist():
                relative = _steam_payload_path(item.filename)
                target = destination / relative
                if not target.parent.resolve().is_relative_to(destination.resolve()):
                    raise ValueError('Steam archive traverses a symlink: %s' % relative)
                if item.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                total += item.file_size
                if item.file_size > 512 * 1024 * 1024 or total > 4 * 1024 * 1024 * 1024:
                    raise ValueError('Steam payload exceeds size limit')
                content = archive.read(item)  # also verifies ZIP CRC
                link = stat.S_ISLNK(item.external_attr >> 16)
                if link:
                    link_target = content.decode('utf-8')
                    if not (target.parent / link_target).resolve().is_relative_to(destination.resolve()):
                        raise ValueError('Steam symlink escapes payload: %s' % relative)
                info = {'sha256': hashlib.sha256(content).hexdigest(), 'link': link}
                key = str(relative)
                if key in entries:
                    if entries[key] != info:
                        raise ValueError('conflicting Steam archive entry: %s' % key)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                if link:
                    target.symlink_to(link_target)
                else:
                    target.write_bytes(content)
                    # Valve's ZIPs mark Linux executables as 0666, even ELF files.
                    target.chmod(0o755 if content.startswith((b'\x7fELF', b'#!')) else 0o644)
                    stamp = time.mktime((*item.date_time, 0, 0, -1))
                    os.utime(target, (stamp, stamp))
                entries[key] = info
    if not set(STEAM_CORE_FILES).issubset(entries):
        raise ValueError('Steam payload is missing core client executables')
    return entries


def _steam_client_closed():
    if any(process_running(name) for name in ('steam', 'steamwebhelper')):
        raise ValueError('Steam or the Lumen sidecar is running; close it completely first')
    # Steam is closed: a leftover sidecar is stopped rather than refused.
    if lumen_running() and not stop_lumen_sidecar():
        raise ValueError('Steam or the Lumen sidecar is running; close it completely first')


def _install_steam_payload(payload, entries, want, packages):
    """Install extracted binaries transactionally (package/ unlocked meanwhile)."""
    with steam_package_writable():
        return _install_steam_payload_locked(payload, entries, want, packages)


def _install_steam_payload_locked(payload, entries, want, packages):
    """Install extracted binaries transactionally; retain a machine-local backup."""
    _steam_client_closed()
    root = steam_home().resolve()
    backup = luna_tools_dir() / 'steam-client-backups' / str(time.time_ns())
    backup.mkdir(parents=True)
    changed = []
    targets = []
    for name, info in entries.items():
        target = root / _steam_payload_path(name)
        try:
            if target.is_symlink() != info['link']:
                raise OSError('different file type')
            content = os.readlink(target).encode() if info['link'] else target.read_bytes()
            same = hashlib.sha256(content).hexdigest() == info['sha256']
            source = payload / name
            if same and (info['link'] or stat.S_IMODE(target.stat().st_mode) == stat.S_IMODE(source.stat().st_mode)):
                continue
        except OSError:
            pass
        targets.append((target, payload / name))
    data = (packages / STEAM_MANIFEST_NAME).read_bytes()
    for name in [STEAM_MANIFEST_NAME] + [entry[0] for entry in _steam_package_entries(data, want)]:
        source, target = packages / name, root / 'package' / name
        # Preserve package stats so Health can cheaply compare the installed cache.
        if (not target.is_file() or target.is_symlink()
                or source.stat().st_size != target.stat().st_size
                or source.stat().st_mtime_ns != target.stat().st_mtime_ns
                or sha256(source) != sha256(target)):
            targets.append((target, source))
    # Save/update the receipt and cfg in the same transaction as the binaries.
    receipt = luna_tools_dir() / 'steam-client-installed.json'
    marker = root / '.steam-enable-steamrt64-client'
    saved = []
    try:
        for index, (target, _) in enumerate(targets + [(receipt, None), (steam_cfg_path(), None), (marker, None)]):
            if not target.parent.resolve().is_relative_to(root) and target != receipt:
                raise ValueError('Steam destination escapes its root: %s' % target)
            if target.exists() and not target.is_symlink() and not target.is_file():
                raise ValueError('Steam destination is not a regular file: %s' % target)
            previous = backup / str(index)
            exists = target.exists() or target.is_symlink()
            if exists:
                shutil.copy2(target, previous, follow_symlinks=False)
            saved.append((target, previous if exists else None))
        write_json_file(backup / 'restore.json', {
            'build': want, 'files': [{'path': str(path), 'backup': old.name if old else None}
                                    for path, old in saved]})
        _steam_client_closed()  # recheck after slow backup/extraction
        for target, source in targets:
            target.parent.mkdir(parents=True, exist_ok=True)
            temp = target.with_name(target.name + '.luna-new')
            if temp.exists() or temp.is_symlink():
                raise ValueError('Steam temporary destination already exists: %s' % temp)
            try:
                shutil.copy2(source, temp, follow_symlinks=False)
                os.replace(temp, target)
            finally:
                if temp.exists() or temp.is_symlink():
                    temp.unlink()
            changed.append(target)
        for name, info in entries.items():
            target = root / name
            content = os.readlink(target).encode() if info['link'] else target.read_bytes()
            if hashlib.sha256(content).hexdigest() != info['sha256']:
                raise ValueError('installed Steam client verification failed: %s' % name)
        for target in (receipt, steam_cfg_path(), marker):
            changed.append(target)
        if marker.exists() or marker.is_symlink():
            marker.unlink()
        ensure_steam_cfg_block()
        write_json_file(receipt, {'build': want, 'at': int(time.time()), 'backup': str(backup),
                                 'core': {name: entries[name]['sha256'] for name in STEAM_CORE_FILES}})
    except Exception:
        for target, previous in reversed(saved):
            if target not in changed:
                continue
            if target.exists() or target.is_symlink():
                target.unlink()
            if previous is not None:
                shutil.copy2(previous, target, follow_symlinks=False)
        raise
    return backup


def ensure_compatible_steam():
    """Default install policy: fetch/verify the pinned Valve build, then extract it."""
    try:
        _steam_client_closed()
        if read_steam_pending():
            raise ValueError('a Steam task is queued; run: luna steam cancel before installing')
        if not (steam_home() / 'steam.sh').is_file():
            raise ValueError('Steam must be installed and opened once before installing Luna')
        tools = luna_tools_dir()
        tools.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='steam-client-stage-', dir=tools) as temporary:
            work = Path(temporary)
            packages = work / 'package'
            packages.mkdir()
            manifest = packages / STEAM_MANIFEST_NAME
            sources = (steam_home() / 'package', steam_cache_dir() / 'package')
            for source in sources:
                candidate = source / STEAM_MANIFEST_NAME
                if candidate.is_file() and sha256(candidate) == STEAM_COMPAT_MANIFEST_SHA256:
                    shutil.copy2(candidate, manifest)
                    break
            else:
                _download_steam_file(STEAM_ARCHIVE_BASE + 'steam_client_ubuntu12', manifest,
                                     STEAM_COMPAT_MANIFEST_SHA256, limit=1024 * 1024)
            data = manifest.read_bytes()
            for name, digest, _ in _steam_package_entries(data, STEAM_COMPAT_BUILD):
                destination = packages / name
                for source in sources:
                    candidate = source / name
                    if candidate.is_file() and not candidate.is_symlink() and sha256(candidate) == digest:
                        shutil.copy2(candidate, destination)
                        break
                else:
                    print('steam     : downloading %s' % name, flush=True)
                    try:
                        _download_steam_file('https://client-update.steamstatic.com/' + name, destination, digest)
                    except (OSError, ValueError):
                        _download_steam_file(STEAM_ARCHIVE_BASE + name, destination, digest)
            entries = _stage_steam_payload(packages, data, STEAM_COMPAT_BUILD, work / 'payload')
            # Keep a complete verified cache, not a copy of stale installed metadata.
            cache = steam_cache_dir()
            replacement = work / 'cache'
            prepared_stats = _package_stats(packages, with_hash=True)
            reuse_cache = (cached_client_build() == STEAM_COMPAT_BUILD
                           and _package_stats(cache / 'package', with_hash=True) == prepared_stats)
            if not reuse_cache:
                shutil.copytree(packages, replacement / 'package')
                (replacement / 'build.txt').write_text(STEAM_COMPAT_BUILD + '\n')
                write_json_file(replacement / 'manifest.json', prepared_stats)
            backup = _install_steam_payload(work / 'payload', entries, STEAM_COMPAT_BUILD, packages)
            if reuse_cache:
                write_json_file(cache / 'manifest.json', prepared_stats)
            else:
                if cache.exists():
                    os.replace(cache, backup / 'previous-cache')
                try:
                    os.replace(replacement, cache)
                except OSError:
                    if (backup / 'previous-cache').exists():
                        os.replace(backup / 'previous-cache', cache)
                    raise
            write_json_file(steam_pin_path(), new_steam_pin(STEAM_COMPAT_BUILD))
            pruned = prune_steam_client_backups()
        return True, ('installed and verified Steam client %s executables; updates blocked; backup: %s%s. '
                      'Runtime version will be confirmed on the next Steam launch.'
                      % (STEAM_COMPAT_BUILD, backup,
                         '; pruned %d old backup(s)' % pruned if pruned else ''))
    except (OSError, ValueError, lzma.LZMAError, zipfile.BadZipFile) as error:
        return False, 'Steam client installation failed (not reported as a downgrade): %s' % error


def _recover_steam_client(want):
    """Restore and verify actual cached Steam client executables for `want`.

    Returns (ok, message). Rechecks that Steam/Lumen are closed internally,
    verifies and extracts the packages, retains a rollback backup, and blocks
    self-updates. Copying package metadata alone can never report success.
    """
    if cached_client_build() != want or not (steam_cache_dir() / 'package').is_dir():
        return False, 'no cache for build %s; run: luna steam cache (Steam closed)' % want
    manifest = _cache_manifest()
    if manifest:
        cached_stats = {}
        for path in sorted((steam_cache_dir() / 'package').rglob('*')):
            if path.is_file():
                try:
                    cached_stats[str(path.relative_to(steam_cache_dir() / 'package'))] = True
                except OSError:
                    pass
        problems = [rel for rel, entry in manifest.items()
                    if not isinstance(entry, list) or len(entry) < 3]
        if problems or set(cached_stats) != set(manifest):
            return False, 'the cache is incomplete; re-run: luna steam cache (Steam closed)'
        for rel, entry in manifest.items():
            try:
                if sha256(steam_cache_dir() / 'package' / rel) != entry[2]:
                    return False, ('the cache failed its own integrity check (%s changed); '
                                     're-run: luna steam cache (Steam closed)' % rel)
            except OSError as error:
                return False, 'cannot read the cache: %s' % error
    # A package-only swap is not a recovery. Extract and verify the executables
    # first; never report success solely from build.txt or a copied manifest.
    try:
        _steam_client_closed()
        packages = steam_cache_dir() / 'package'
        data = (packages / STEAM_MANIFEST_NAME).read_bytes()
        if want == STEAM_COMPAT_BUILD and hashlib.sha256(data).hexdigest() != STEAM_COMPAT_MANIFEST_SHA256:
            raise ValueError('cached compatibility manifest SHA256 mismatch')
        with tempfile.TemporaryDirectory(prefix='steam-recover-', dir=luna_tools_dir()) as work:
            payload = Path(work) / 'payload'
            entries = _stage_steam_payload(packages, data, want, payload)
            backup = _install_steam_payload(payload, entries, want, packages)
    except (OSError, ValueError, lzma.LZMAError, zipfile.BadZipFile) as error:
        return False, 'could not recover Steam client executables: %s' % error
    pruned = prune_steam_client_backups()
    return True, ('recovered and verified Steam client %s executables; updates blocked; backup: %s%s'
                  % (want, backup, '; pruned %d old backup(s)' % pruned if pruned else ''))


def _apply_steam_update():
    """Valve-driven client update with the block lifted, then re-blocked.

    Called only with Steam closed (timer or --now). On success the local
    package cache is stale by definition and is dropped, so a later recover
    can never restore the pre-update build by accident.
    """
    ok, message = _steam_force_update()
    if not ok:
        return False, message
    try:
        shutil.rmtree(steam_cache_dir(), ignore_errors=True)
    except OSError:
        pass
    try:
        steam_pending_path().unlink()
    except OSError:
        pass
    build = steam_client_build()
    note = ('; cache is now stale and was dropped - re-pin after verifying, then re-cache'
            if not build else '; re-pin the new build %s after verifying, then re-cache' % build)
    return True, message + note


def steam_moon_state_dir():
    return home() / '.local/state/slsteam-moon'


def _find_steam_dir():
    """First Steam root that has a package/ dir, symlinks resolved."""
    candidates = [steam_home(), home() / '.local/share/Steam', home() / '.steam/steam',
                  home() / '.steam/root',
                  home() / '.var/app/com.valvesoftware.Steam/.local/share/Steam']
    for candidate in candidates:
        if (candidate / 'package').is_dir():
            return candidate.resolve()
    return None


def _steam_package_set_readonly(package, readonly):
    mode = package.stat().st_mode
    package.chmod(mode & ~0o222 if readonly else mode | stat.S_IWUSR)


def _http_get(url, limit=64 * 1024 * 1024):
    request = urllib.request.Request(url, headers={'User-Agent': 'Luna-Steam-Installer'})
    with urllib.request.urlopen(request, timeout=60) as source:
        data = source.read(limit + 1)
    if len(data) > limit:
        raise ValueError('download exceeds the size limit: %s' % url)
    return data


def _manifest_version(data):
    text = data.decode('utf-8', errors='replace') if isinstance(data, bytes) else data
    found = [item for item in re.findall(r'"version"\s+"(\d+)"', text) if item != '0']
    return found[0] if found else ''


def obtain_pinned_manifest(say):
    """The pinned known-good ubuntu12 manifest bytes, or None when it cannot be obtained.

    Order: Luna cache, then the pinned SteamTracking commit. A copy whose sha256 or
    version differs from the pins raises ValueError (fail closed, never used).
    """
    cached = steam_cache_dir() / 'package' / STEAM_MANIFEST_NAME
    try:
        data = cached.read_bytes()
        if hashlib.sha256(data).hexdigest() == KNOWN_GOOD_MANIFEST_SHA256:
            say('using the pinned ubuntu12 manifest from the Luna cache')
            return data
    except OSError:
        pass
    try:
        data = _http_get(KNOWN_GOOD_MANIFEST_URL)
    except (OSError, urllib.error.URLError) as error:
        say('WARN: could not download the pinned manifest (%s)' % error)
        return None
    if hashlib.sha256(data).hexdigest() != KNOWN_GOOD_MANIFEST_SHA256:
        raise ValueError('the manifest downloaded from SteamTracking %s does not match the pinned sha256; '
                         'refusing' % KNOWN_GOOD_MANIFEST_COMMIT[:12])
    if _manifest_version(data) != KNOWN_GOOD_UBUNTU12_BUILD:
        raise ValueError('the pinned manifest has version %s, expected %s; refusing'
                         % (_manifest_version(data), KNOWN_GOOD_UBUNTU12_BUILD))
    say('downloaded the pinned ubuntu12 manifest (SteamTracking %s)' % KNOWN_GOOD_MANIFEST_COMMIT[:12])
    return data


class ManifestServer:
    """127.0.0.1 server: the pinned manifest for the manifest paths, 302 to Valve's CDN otherwise."""

    MANIFEST_PATHS = ('steam_client_ubuntu12', 'steam_client_ubuntu12.manifest', 'client/steam_client_ubuntu12')

    def __init__(self, data):
        outer = self
        self.hits = 0

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                path = self.path.split('?', 1)[0].lstrip('/')
                if path in outer.MANIFEST_PATHS:
                    outer.hits += 1
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/octet-stream')
                    self.send_header('Content-Length', str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                if path.startswith('client/'):
                    path = path[len('client/'):]
                self.send_response(302)
                self.send_header('Location', STEAM_CDN + path)
                self.send_header('Content-Length', '0')
                self.end_headers()

            do_HEAD = do_GET  # noqa: N815

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.port = self.server.server_address[1]
        self.url = 'http://127.0.0.1:%d' % self.port

    def __enter__(self):
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)


def _pinned_rollback(sdir, data, say):
    """Run the launcher against the local manifest server; True once a manifest request was served."""
    candidates = [path for path in (sdir / 'ubuntu12_32' / 'steam', sdir / 'steam.sh') if path.is_file()]
    for launcher in candidates:
        with ManifestServer(data) as server:
            say('running %s against the local pinned manifest server (%s)' % (launcher.name, server.url))
            try:
                subprocess.run([str(launcher), '-forcemanifestsuffix', 'ubuntu12', '-forcesteamupdate',
                                '-forcepackagedownload', '-overridepackageurl', server.url, '-exitsteam'],
                               timeout=3600)  # a non-zero exit is tolerated
            except (OSError, subprocess.SubprocessError) as error:
                say('WARN: %s failed: %s' % (launcher.name, error))
            if server.hits > 0:
                return True
        say('WARN: %s never asked for the manifest; trying the next launcher' % launcher.name)
    return False


def _rollback_steam_client(sdir, say):
    """Kill Steam, wipe the steamrt64 client packages and fetch the ubuntu12 client.

    Returns (ok, message). Never touches the cfg/pin; the caller locks only
    after this succeeded. The bootstrapper download is ~500 MB.
    """
    package = sdir / 'package'
    binary = sdir / 'ubuntu12_32' / 'steam'
    if not binary.is_file():
        binary = sdir / 'steam.sh'
    if not binary.is_file():
        return False, 'no Steam launcher found under %s' % sdir
    try:
        pinned = obtain_pinned_manifest(say)
    except ValueError as error:
        return False, 'REFUSING: %s' % error
    uid = str(os.getuid())
    say('stopping Steam')
    for name in ('steam', 'steamwebhelper'):
        subprocess.run(['pkill', '-9', '-u', uid, '-x', name], capture_output=True)
    time.sleep(2)
    if not stop_lumen_sidecar():
        return False, 'the Lumen sidecar would not stop; run: pkill -u $(id -u) -x lumen'
    _steam_package_set_readonly(package, False)
    for stale in package.glob('steam_client_steamrt64*'):
        stale.unlink()
    for stale in steam_moon_state_dir().glob('safe_mode*'):
        stale.unlink()
    say('downloading the ubuntu12 client (about 500 MB); this can take a while')
    if pinned is not None:
        if not _pinned_rollback(sdir, pinned, say):
            return False, ('the Steam launcher never requested the pinned manifest, so the rollback did '
                           'not happen; package/ was left writable. Run: luna steam recover (offline) or retry')
        return True, 'ubuntu12 client downloaded (pinned manifest %s)' % KNOWN_GOOD_UBUNTU12_BUILD
    say('WARN: the pinned manifest is unavailable; falling back to the current ubuntu12 manifest from '
        "Valve's CDN, which Valve may have retired (SteamTracking deleted it on 2026-11-12)")
    # The Luna block would inhibit this very download; it is re-applied by the caller.
    try:
        subprocess.run([str(binary), '-forcemanifestsuffix', 'ubuntu12', '-exitsteam'],
                       timeout=3600)  # a non-zero exit is tolerated, as in the community script
    except (OSError, subprocess.SubprocessError) as error:
        return False, 'Steam bootstrapper failed: %s' % error
    return True, 'ubuntu12 client downloaded'


def _lock_steam_client(force, confirm):
    """`luna steam lock`: roll back to the ubuntu12 client and hard-lock updates."""
    sdir = _find_steam_dir()
    if sdir is None:
        return 1, 'no Steam installation with a package/ directory found'
    if sdir != steam_home().resolve():
        return 1, ('Steam found at %s, but Luna manages %s; refusing to lock a layout it '
                   'cannot pin' % (sdir, steam_home()))
    if read_steam_pending():
        return EXIT_REFUSED, 'a Steam task is queued; run: luna steam cancel first'
    running = [name for name in ('steam', 'steamwebhelper') if process_running(name)]
    if running and not force and not confirm():
        return EXIT_REFUSED, ('REFUSING: Steam is running and lock must kill it (kill -9, unsaved '
                              'state is lost). Close it, or re-run with --force.')

    def say(text):
        print('lock    : %s' % text, flush=True)

    if steamos_gaming_mode():
        say('WARN: SteamOS Gaming Mode detected; run lock from Desktop Mode')
    # The bootstrapper must be free to download: lift Luna's block for the run.
    had_block = steam_updates_blocked() or steam_hard_lock_active()
    remove_steam_cfg_block(steam_cfg_paths())
    try:
        ok, message = _rollback_steam_client(sdir, say)
    except OSError as error:
        ok, message = False, 'rollback failed: %s' % error
    if not ok:
        if had_block:
            ensure_steam_cfg_block()
        return 1, message
    say(message)
    build = steam_client_build()
    if not build:
        if had_block:
            ensure_steam_cfg_block()
        return 1, ('rolled back, but the client build cannot be determined; launch Steam once '
                   'and re-run: luna steam lock')
    tools = luna_tools_dir()
    tools.mkdir(parents=True, exist_ok=True)
    # Pin first (carrying the lock marker) so every block/cfg helper agrees.
    pin = new_steam_pin(build)
    pin['hardLock'] = {'rollback': 'ubuntu12', 'at': int(time.time()), 'by': installed_version()}
    write_json_file(steam_pin_path(), pin)
    # The old receipt/cache describe the previous build and would read as drift.
    shutil.rmtree(steam_cache_dir(), ignore_errors=True)
    try:
        write_json_file(tools / 'steam-client-installed.json', {
            'build': build, 'at': int(time.time()), 'backup': '',
            'core': {name: sha256(sdir / _steam_payload_path(name)) for name in STEAM_CORE_FILES}})
    except (OSError, ValueError):
        try:
            (tools / 'steam-client-installed.json').unlink()
        except OSError:
            pass
    try:
        _cache_client_packages(build)
    except (OSError, ValueError) as error:
        say('warning: could not cache build %s (%s); run: luna steam cache' % (build, error))
    ensure_steam_cfg_block()
    _steam_package_set_readonly(sdir / 'package', True)
    if steam_moon_state_dir().is_dir():
        (steam_moon_state_dir() / 'boot_fail_count').write_text('0\n')
    code, note = install_libcrypto(False, say)
    say(note if code == 0 else 'WARN: %s' % note)
    return 0, ('Steam rolled back to the ubuntu12 client (build %s), pinned and hard-locked: '
               'Steam.cfg + steam.cfg written, package/ read-only. Undo: luna steam unlock' % build)


# --------------------------------------------------------------------------- libcrypto.so.3

def _elf32_i386(data):
    """ELF class 1 (32-bit), little endian, machine 3 (i386)."""
    return len(data) >= 20 and data[:4] == b'\x7fELF' and data[4] == 1 and data[5] == 1 \
        and int.from_bytes(data[18:20], 'little') == 3


def _elf32_file(path):
    try:
        with open(path, 'rb') as handle:
            return _elf32_i386(handle.read(20))
    except OSError:
        return False


def libcrypto_target():
    return steam_home() / 'ubuntu12_32' / 'libcrypto.so.3'


def libcrypto_status():
    """(state, text); state in present/missing/invalid/no-steam."""
    target = libcrypto_target()
    if not target.parent.is_dir():
        return 'no-steam', 'libcrypto: %s does not exist (nothing to do)' % target.parent
    if not os.path.lexists(target):
        return 'missing', ('WARN: 32-bit libcrypto.so.3 is missing in ubuntu12_32; games may vanish from the '
                           'library. Run: luna steam libcrypto --install')
    if not _elf32_file(target):
        return 'invalid', 'WARN: %s is not a 32-bit ELF library; luna steam libcrypto --install --force' % target
    return 'present', 'libcrypto: 32-bit libcrypto.so.3 present in ubuntu12_32'


def install_libcrypto(force=False, say=print):
    """Put a 32-bit libcrypto.so.3 into <Steam>/ubuntu12_32. Returns (code, message)."""
    target = libcrypto_target()
    if not target.parent.is_dir():
        return 1, 'no %s; install/run Steam once first' % target.parent
    if os.path.lexists(target) and not force:
        if _elf32_file(target):
            return EXIT_OK, 'libcrypto.so.3 already present; kept (use --force to replace)'
        return EXIT_REFUSED, ('REFUSING to overwrite %s (not a 32-bit ELF library); use --force' % target)
    data, source = None, ''
    for candidate in LIBCRYPTO_SYSTEM_PATHS:
        path = Path(candidate)
        if path.is_file() and _elf32_file(path):
            try:
                data, source = path.read_bytes(), 'system %s' % path
            except OSError:
                continue
            break
    if data is None:
        try:
            data = _http_get(LIBCRYPTO_URL, limit=32 * 1024 * 1024)
        except (OSError, urllib.error.URLError) as error:
            return 1, 'no system 32-bit libcrypto.so.3 and the pinned download failed: %s' % error
        if hashlib.sha256(data).hexdigest() != LIBCRYPTO_SHA256:
            return 1, 'REFUSING: the downloaded libcrypto.so.3 does not match the pinned sha256'
        source = 'pinned SteamRollbackrLite asset'
        if not _elf32_i386(data[:20]):
            return 1, 'REFUSING: the downloaded libcrypto.so.3 is not a 32-bit ELF library'
    temporary = target.with_name('libcrypto.so.3.luna-tmp')
    try:
        temporary.write_bytes(data)
        temporary.chmod(0o755)
        os.replace(temporary, target)
    except OSError as error:
        try:
            temporary.unlink()
        except OSError:
            pass
        return 1, 'could not write %s: %s' % (target, error)
    return EXIT_OK, 'installed 32-bit libcrypto.so.3 into ubuntu12_32 (from %s)' % source


def remove_libcrypto():
    target = libcrypto_target()
    if not os.path.lexists(target):
        return EXIT_OK, 'libcrypto.so.3 is not installed in ubuntu12_32; nothing to remove'
    try:
        target.unlink()
    except OSError as error:
        return 1, 'could not remove %s: %s' % (target, error)
    return EXIT_OK, 'removed %s' % target


def cmd_libcrypto(options):
    if getattr(options, 'remove', False):
        code, message = remove_libcrypto()
    elif getattr(options, 'install', False):
        code, message = install_libcrypto(getattr(options, 'force', False))
    else:
        return print(libcrypto_status()[1]) or EXIT_OK
    print(message, file=sys.stdout if code == 0 else sys.stderr)
    return code


# --------------------------------------------------------------------------- manifest history

def format_history(commits):
    """Lines for a SteamTracking commits API payload (pure; the network part is separate)."""
    lines = []
    for item in commits if isinstance(commits, list) else []:
        sha = str(item.get('sha') or '')
        commit = item.get('commit') or {}
        message = str(commit.get('message') or '').splitlines()[0] if commit.get('message') else ''
        when = str((commit.get('author') or {}).get('date') or '')[:10]
        marks = []
        if sha == KNOWN_GOOD_MANIFEST_COMMIT:
            marks.append('KNOWN-GOOD (pinned)')
        if message.lower().startswith('delete'):
            marks.append('manifest deleted')
        lines.append('%s  %s  %s%s' % (sha[:12], when, message, ('   <- ' + ', '.join(marks)) if marks else ''))
    return lines


def cmd_history(options):
    try:
        data = json.loads(_http_get(STEAMTRACKING_HISTORY_URL, limit=4 * 1024 * 1024))
    except urllib.error.HTTPError as error:
        if error.code in (403, 429):
            print('history: GitHub rate limit reached (HTTP %d); try again later.' % error.code, file=sys.stderr)
        else:
            print('history: GitHub answered HTTP %d' % error.code, file=sys.stderr)
        return 1
    except (OSError, urllib.error.URLError, ValueError) as error:
        print('history: cannot reach GitHub (%s); the pinned known-good commit is %s'
              % (error, KNOWN_GOOD_MANIFEST_COMMIT), file=sys.stderr)
        return 1
    print('SteamTracking ClientManifest/steam_client_ubuntu12 (newest first):')
    for line in format_history(data):
        print('  ' + line)
    return EXIT_OK


# --------------------------------------------------------------------------- environment warnings

def flatpak_steam_only():
    return (home() / '.var/app/com.valvesoftware.Steam').is_dir() and not steam_home().is_dir()


FLATPAK_WARNING = 'WARN: Steam is the Flatpak build; Luna supports native Steam only'


def steamos_gaming_mode():
    try:
        text = Path(OS_RELEASE_PATH).read_text()
    except OSError:
        return False
    if not re.search(r'^ID="?steamos"?\s*$', text, re.M):
        return False
    try:
        return subprocess.run(['pgrep', '-u', str(os.getuid()), '-f', 'gamescope-session'],
                              capture_output=True).returncode == 0
    except OSError:
        return False


def _notify(title, text):
    """Best-effort desktop notification; never fails the job."""
    if os.environ.get('LUNA_NO_NOTIFY'):
        return
    try:
        subprocess.run(['notify-send', '-a', 'Luna', title, text], capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        pass


def _launch_steam_detached():
    try:
        subprocess.Popen(['setsid', 'steam'], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        return True
    except OSError as error:
        print('could not launch Steam: %s' % error, file=sys.stderr)
        return False


def _close_steam_gracefully(timeout=90):
    """`steam -shutdown`, then wait for the client to exit. True when it is gone."""
    if not process_running('steam'):
        return True
    try:
        subprocess.run(['steam', '-shutdown'], capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        pass
    for _ in range(timeout):
        if not process_running('steam'):
            return True
        time.sleep(1)
    return False


def _unlock_steam_client(now):
    """`luna steam unlock`: undo the hard lock and the pin."""
    if not (steam_running()) and stop_lumen_sidecar():
        pass  # a lingering sidecar from an earlier session is stopped, never a reason to refuse
    package = steam_home() / 'package'
    if package.is_dir():
        _steam_package_set_readonly(package, False)
        print('package/ is writable again')
    if remove_steam_cfg_block(steam_cfg_paths()):
        print('update block removed: Steam may update itself again')
    elif any(path.exists() for path in steam_cfg_paths()):
        print('note: Steam.cfg/steam.cfg left in place (they hold more than the Luna block)')
    try:
        steam_pin_path().unlink()
        remove_steam_pin()
        print('pin and hard-lock marker removed')
    except OSError:
        print('no pin to remove')
    if now:
        if process_running('steam'):
            print('Steam is already running; restart it to let it self-update')
        else:
            try:
                subprocess.Popen(['setsid', 'steam'], stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 start_new_session=True)
                print('Steam launched; it will self-update to the latest client')
            except OSError as error:
                print('could not launch Steam: %s' % error, file=sys.stderr)
                return 1
    return EXIT_OK


def refresh_pending_path():
    return luna_tools_dir() / 'pending-refresh.json'


def read_refresh_pending():
    pending = read_json_file(refresh_pending_path())
    return pending if isinstance(pending, dict) else {}


def _refresh_steam_side():
    """Update Steam to Valve's latest, then pin and cache the new build.

    Caller guarantees Steam and the sidecar are closed. Fails closed at each
    step with the exact next action; never kills processes and never mislabels
    the cache. Returns (ok, message, build).
    """
    try:
        _steam_client_closed()
    except ValueError as error:
        return False, str(error), ''
    if read_steam_pending():
        return False, 'a Steam task is already queued; run: luna steam cancel first', ''
    ok, message = _steam_force_update()
    if not ok:
        return False, message, ''
    build = steam_client_build()
    if not build:
        return False, ('Steam updated, but the new build cannot be determined; '
                       'launch Steam once, then run: luna steam pin [--build N]'), ''
    pin = new_steam_pin(build)
    try:
        luna_tools_dir().mkdir(parents=True, exist_ok=True)
        write_json_file(steam_pin_path(), pin)
        ensure_steam_cfg_block()
        _cache_client_packages(build)
    except (OSError, ValueError) as error:
        return False, 'Steam updated to %s, but pin/cache failed: %s' % (build, error), build
    return True, 'Steam client %s updated, pinned, cached and blocked' % build, build


def cmd_refresh(options):
    """Wizard: bring Steam to Valve's latest and reinstall Luna on top.

    Steam and Lumen must already be closed (this command never kills
    anything). Steps: update, pin, cache, install add-ons keeping the new
    client, verify. Any failure stops the wizard with the exact next action.
    """
    if options.dry_run:
        print('refresh : would update Steam (closed), pin, cache, install Luna --keep-steam-client, verify')
        print('\nDRY RUN: Steam stays untouched, nothing was written.')
        return EXIT_OK
    if steam_hard_lock_active() and not (getattr(options, 'unlock_first', False)
                                         or getattr(options, 'allow_unlocked_refresh', False)):
        print('REFUSING to refresh: the ubuntu12 hard lock is active (Steam is pinned on purpose).\n'
              '  To move Steam to Valve\'s latest, run: luna refresh --unlock-first\n'
              '  (lifts the hard lock, updates, pins the new build with the standard update block).',
              file=sys.stderr)
        return EXIT_REFUSED
    reasons = refuse_reasons(for_install=True, stop_sidecar=True)
    if reasons:
        print('REFUSING to refresh:')
        for reason in reasons:
            print('  - %s' % reason)
        return EXIT_REFUSED
    if steam_hard_lock_active() and getattr(options, 'unlock_first', False):
        print('refresh : lifting the hard lock first')
        code = _unlock_steam_client(False)
        if code != EXIT_OK:
            return code
    ok, message, build = _refresh_steam_side()
    print('refresh : %s' % message, file=sys.stdout if ok else sys.stderr)
    if not ok:
        return 1
    install_options = argparse.Namespace(only=[], force=False, dry_run=False,
                                         keep_steam_client=True, no_steam_machine=False,
                                         migrate_steam_machine=False)
    code = cmd_install(install_options)
    if code != EXIT_OK:
        print('refresh : Steam is on %s, but the Luna install needs attention (exit %d)'
              % (build, code), file=sys.stderr)
        return code
    verify_options = argparse.Namespace(sources=False, source_dir=None)
    code = cmd_verify(verify_options)
    if code != EXIT_OK:
        print('refresh : installed, but verification failed (exit %d)' % code, file=sys.stderr)
        return code
    print('\nrefresh : Steam %s + Luna installed and verified. Restart Steam to load the add-ons.' % build)
    return EXIT_OK


def cmd_steam(options):
    """Pin, cache and recover the Steam client build (see `luna steam --help`)."""
    action = getattr(options, 'steam_action', '') or 'status'
    if action == 'status':
        state = steam_state()
        print('client : %s' % (state['build'] or 'unknown (Steam never logged a version here)'))
        print('pinned : %s' % (state['pinned'] or 'none (run: luna steam pin)'))
        print('packages: %s' % ('match the pin' if state['match']
                                 else 'DIFFER from the pin - run: luna steam recover (Steam closed)'
                                 if state['pinned'] and state['cacheReady']
                                 else 'unknown (no cache)'))
        print('updates: %s' % ('blocked (steam.cfg)' if state['updatesBlocked'] else 'NOT blocked'))
        if state['hardLock']:
            print('lock   : HARD (ubuntu12 rollback, package/ %s)'
                  % ('read-only' if state['packageReadOnly']
                     else 'WRITABLE - re-run: luna steam lock'))
        if state['pacmanManaged']:
            print('pacman : Steam is system-managed%s'
                  % (', ignored on updates' if state['pacmanIgnored']
                     else ' and NOT ignored - add "IgnorePkg = steam" to /etc/pacman.conf (needs sudo)'))
        for name, digest in sorted(state['slssteam'].items()):
            print('%-16s %s' % (name + ':', digest[:12] if digest else '(missing)'))
        if state['slssteamDrift']:
            print('WARN   : SLSsteam changed since the pin: %s' % ', '.join(state['slssteamDrift']))
        for line in heal_steam_lock(apply=False)['notes']:  # read-only: status never repairs
            if 'SLSsteam changed' not in line:
                print('WARN   : %s' % line)
        warning = launchopts_warning()
        if warning:
            print('WARN   : %s' % warning)
        print(libcrypto_status()[1])
        print('cache  : %s' % ('build ' + state['cachedBuild'] if state['cacheReady']
                                else 'empty (run: luna steam cache with Steam closed)'))
        pending = read_steam_pending()
        if pending:
            print('queued : %s (requested %s; timer applies it while Steam is closed, or: luna steam cancel)'
                  % (pending.get('action', '?'),
                     time.strftime('%Y-%m-%d %H:%M', time.localtime(int(pending.get('at') or 0)))))
        return EXIT_OK
    if action == 'heal':
        result = heal_steam_lock()
        if not result['pinned']:
            print('heal  : nothing to heal; no Steam build is pinned (luna steam pin / luna steam lock)')
            return EXIT_OK
        print_heal(result)
        if not result['fixed'] and not result['notes']:
            print('heal  : update block and read-only state are intact')
        return EXIT_OK
    if action == 'launchopts':
        return cmd_launchopts(options)
    if action == 'libcrypto':
        return cmd_libcrypto(options)
    if action == 'history':
        return cmd_history(options)
    if action == 'ignore-pkg':
        return cmd_ignore_pkg(options)
    if action == 'pin':
        build = options.build or steam_client_build()
        if not build:
            print('cannot determine the client build; pass --build N explicitly', file=sys.stderr)
            return 2
        pin = new_steam_pin(build)
        luna_tools_dir().mkdir(parents=True, exist_ok=True)
        write_json_file(steam_pin_path(), pin)
        ensure_steam_cfg_block()
        print('pinned Steam client %s with current SLSsteam hashes; updates blocked via steam.cfg' % build)
        return EXIT_OK
    if action == 'lock':
        def confirm():
            if not sys.stdin.isatty():
                return False
            answer = input('Steam is running and will be killed (kill -9). Continue? [y/N] ')
            return answer.strip().lower() in ('y', 'yes')
        code, message = _lock_steam_client(getattr(options, 'force', False), confirm)
        print(message, file=sys.stdout if code == 0 else sys.stderr)
        if getattr(options, 'relaunch', False):
            # Detached-job mode (in-Steam UI): report, then bring Steam back even on failure.
            _notify('Luna: Steam updates blocked' if code == 0 else 'Luna: could not block Steam updates',
                    message[:200])
            _launch_steam_detached()
        return code
    if action == 'unlock':
        if getattr(options, 'relaunch', False):
            # Close Steam first so the next start runs the bootstrapper and self-updates.
            if not _close_steam_gracefully():
                print('Steam did not close in time; nothing was changed', file=sys.stderr)
                _notify('Luna: Steam update not started', 'Steam did not close; try again.')
                return 1
            code = _unlock_steam_client(False)
            if code == EXIT_OK:
                _notify('Luna: Steam unblocked', 'Steam is restarting and will update to the latest client.')
                _launch_steam_detached()
            else:
                _notify('Luna: could not unblock Steam', 'See the Luna steam job log.')
            return code
        return _unlock_steam_client(getattr(options, 'now', False))
    if action == 'unpin':
        if steam_hard_lock_active():
            print('the hard lock is active (package/ read-only); run: luna steam unlock',
                  file=sys.stderr)
            return 1
        try:
            steam_pin_path().unlink()
            remove_steam_pin()
            print('pin removed')
        except OSError:
            print('no pin to remove')
        if remove_steam_cfg_block():
            print('update block removed: Steam may update itself again')
        else:
            print('note: steam.cfg left untouched (it holds more than the Luna block)')
        return EXIT_OK
    if action == 'cache':
        if steam_running():
            # Queue it: the timer runs while Steam is closed and caches then.
            existing = read_steam_pending()
            if existing:
                print('a Steam task is already queued (%s); run: luna steam cancel first'
                      % existing.get('action', '?'), file=sys.stderr)
                return 1
            write_json_file(steam_pending_path(), {'action': 'cache', 'at': int(time.time())})
            print('Steam is running: caching queued for the next closed-Steam window')
            return EXIT_OK
        source = steam_home() / 'package'
        if not source.is_dir():
            print('no Steam package directory to cache at %s' % source, file=sys.stderr)
            return 1
        build = str(options.build or '') or steam_client_build()
        if not build or build == '0':
            print('cannot determine the client build; launch Steam once, then retry', file=sys.stderr)
            return 1
        try:
            cache = _cache_client_packages(build)
        except (OSError, ValueError) as error:
            print('cannot cache the Steam client: %s' % error, file=sys.stderr)
            return 1
        print('cached Steam client %s (%s)' % (build, cache))
        return EXIT_OK
    if action == 'update':
        # The blessed path for a Steam client update: queue it here (menu or
        # shell); the timer applies it while Steam is closed, then re-blocks.
        # --now applies it right away instead (Steam must already be closed).
        if steam_hard_lock_active():
            print('the hard lock is active; run: luna steam unlock --now to let Steam update',
                  file=sys.stderr)
            return 1
        if getattr(options, 'now', False):
            if steam_running() or lumen_running():
                print('REFUSING: Steam or the Lumen sidecar is running. '
                      'Close it completely (Steam -> Exit) first.')
                return EXIT_REFUSED
            ok, message = _apply_steam_update()
            print(message, file=sys.stderr if not ok else sys.stdout)
            return EXIT_OK if ok else 1
        existing = read_steam_pending()
        if existing:
            print('a Steam task is already queued (%s); run: luna steam cancel first'
                  % existing.get('action', '?'), file=sys.stderr)
            return 1
        write_json_file(steam_pending_path(), {'action': 'update', 'at': int(time.time())})
        if steam_running():
            print('Steam update queued: it applies the next time Steam is closed '
                  '(timer), then updates are blocked again')
        else:
            print('Steam update queued: enable the timer or run: luna steam update --now')
        return EXIT_OK
    if action == 'cancel':
        try:
            steam_pending_path().unlink()
            print('queued Steam task cancelled')
        except OSError:
            print('nothing queued')
        return EXIT_OK
    if action == 'recover':
        if steam_running():
            print('REFUSING to recover: Steam is running. '
                  'Close it completely (Steam -> Exit) first.')
            return EXIT_REFUSED
        if not stop_lumen_sidecar():
            print('REFUSING to recover: the Lumen sidecar would not stop. '
                  'Run: pkill -u $(id -u) -x lumen')
            return EXIT_REFUSED
        want = str(options.build or '')
        pin = read_steam_pin()
        if not want:
            want = str(pin.get('clientBuild') or '')
        if not want:
            print('nothing pinned; run: luna steam pin [--build N]', file=sys.stderr)
            return 2
        ok, message = _recover_steam_client(want)
        if not ok:
            print(message, file=sys.stderr)
            return 1
        print(message)
        print('launch Steam and verify, then run: luna steam pin')
        return EXIT_OK
    print('unknown steam action: %s (status|pin|unpin|cache|recover|update|cancel|lock|unlock|'
          'heal|launchopts|ignore-pkg)' % action, file=sys.stderr)
    return 2


def steam_drift_note():
    """One-line summary for preflight/Health, or '' when nothing is wrong."""
    state = steam_state()
    if state.get('binariesMatch') is False:
        return 'Steam client executables changed; close Steam and run: luna steam recover'
    if state['pinned'] and state['cacheReady'] and state['packagesMatch'] is False:
        return ('Steam client packages changed since the pin; close Steam and run: luna steam recover')
    if state['slssteamDrift']:
        return ('SLSsteam changed since the pin (%s); re-pin after verifying: luna steam pin'
                % ', '.join(state['slssteamDrift']))
    if state['pinned'] and state['pacmanManaged'] and not state['pacmanIgnored']:
        return ('Steam is pacman-managed and not ignored: a system update can replace the launcher; '
                'add "IgnorePkg = steam" under [options] in /etc/pacman.conf (needs sudo)')
    if state['pinned'] and not state['updatesBlocked']:
        return 'a Steam build is pinned but updates are not blocked; run: luna steam recover or re-add steam.cfg'
    if state['pinned'] and not state['cacheReady'] and state['build'] and state['build'] != state['pinned']:
        return ('Steam client %s does not match the pin %s and no cache exists; '
                'cache a known-good build: luna steam cache (Steam closed)'
                % (state['build'], state['pinned']))
    return ''


# --------------------------------------------------------------------------- self-heal

def _cfg_block_present(paths, block):
    """Does every file hold all of the block's key lines, with none contradicted?"""
    want = {line.strip().lower() for line in block.splitlines() if line.strip()}
    want_keys = {line.split('=', 1)[0] for line in want}
    for path in paths:
        try:
            lines = path.read_text().splitlines()
        except OSError:
            return False
        have = {line.strip().lower() for line in lines if _STEAM_CFG_KEYS.match(line)}
        if not want <= have:
            return False
        if any(line.split('=', 1)[0] in want_keys and line not in want for line in have):
            return False
    return True


def heal_steam_lock(apply=True):
    """Restore the update block a third-party installer may have removed.

    Applies when a Steam build is pinned (or the hard lock is recorded):
    re-writes steam.cfg (+ Steam.cfg under the hard lock) and puts package/
    back to read-only. Only cfg + chmod, so it is safe while Steam runs.
    SLSsteam hash drift is reported, never re-pinned silently (`luna steam pin`
    is the only thing that does that). Returns {'pinned', 'fixed', 'notes'}.
    """
    result = {'pinned': False, 'fixed': [], 'notes': []}
    pin = read_steam_pin()
    if not pin or not (pin.get('clientBuild') or pin.get('hardLock')):
        return result
    if apply:
        if not steam_pin_path().is_file():  # restore the primary from the copy
            with contextlib.suppress(OSError):
                write_json_file(steam_pin_path(), pin)
                result['fixed'].append('steam-pin.json was missing; restored from %s' % steam_pin_copy_path())
        sync_steam_pin_copy()
    if not steam_home().is_dir():
        return result
    result['pinned'] = True
    hard = steam_hard_lock_active()
    paths = steam_cfg_paths() if hard else [steam_cfg_path()]
    block = STEAM_HARD_CFG_BLOCK if hard else STEAM_CFG_BLOCK
    names = ' + '.join(path.name for path in paths)
    if not _cfg_block_present(paths, block):
        if apply:
            ensure_steam_cfg_block()
            result['fixed'].append('update block was missing or changed; re-wrote %s' % names)
        else:
            result['notes'].append('update block missing or changed in %s (heal would re-write it)' % names)
    package = steam_home() / 'package'
    if hard and package.is_dir() and package.stat().st_mode & 0o222:
        if apply:
            _steam_package_set_readonly(package, True)
            result['fixed'].append('package/ was writable; made it read-only again')
        else:
            result['notes'].append('package/ is writable (heal would make it read-only)')
    now = slssteam_hashes()
    drift = sorted(name for name, digest in (pin.get('slssteam') or {}).items() if now.get(name, '') != digest)
    if drift:
        result['notes'].append('SLSsteam changed since the pin (%s); NOT re-pinned. '
                               'Verify it, then run: luna steam pin' % ', '.join(drift))
    lib_state, lib_text = libcrypto_status()
    if lib_state == 'missing':
        if apply:
            code, message = install_libcrypto(False)
            if code == 0:
                result['fixed'].append(message)
            else:
                result['notes'].append('%s (%s)' % (lib_text.replace('WARN: ', ''), message))
        else:
            result['notes'].append(lib_text.replace('WARN: ', '') + ' (heal would install it)')
    build = str(pin.get('clientBuild') or '')
    if build and (cached_client_build() != build or not (steam_cache_dir() / 'package').is_dir()):
        result['notes'].append('no known-good cache for build %s; run: luna steam cache (Steam closed)' % build)
    if result['fixed']:
        try:
            luna_tools_dir().mkdir(parents=True, exist_ok=True)
            with open(luna_tools_dir() / 'steam-heal.log', 'a') as log:
                for line in result['fixed']:
                    log.write('%s %s\n' % (time.strftime('%Y-%m-%d %H:%M:%S'), line))
        except OSError:
            pass
    return result


def print_heal(result, prefix='heal  '):
    for line in result['fixed']:
        print('%s: FIXED: %s' % (prefix, line))
    for line in result['notes']:
        print('%s: WARN: %s' % (prefix, line))


# --------------------------------------------------------------------------- launch options

_VDF_APPID = re.compile(rb'^\s*"(\d+)"\s*$')
_VDF_LAUNCH = re.compile(rb'^(\s*"LaunchOptions"\s+")((?:[^"\\]|\\.)*)(".*)$', re.S)
_ENV_ASSIGN = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*=')


def _missing_launch_paths(option):
    """Absolute (or ~/) paths in a launch option that do not exist."""
    try:
        tokens = shlex.split(option)
    except ValueError:
        tokens = option.split()
    missing = []
    for token in tokens:
        if '%command%' in token or '://' in token or _ENV_ASSIGN.match(token):
            continue
        candidate = token.split('=', 1)[1] if token.startswith('-') and '=' in token else token
        if candidate.startswith('~/'):
            path = home() / candidate[2:]
        elif candidate.startswith('/') and len(candidate) > 1:
            path = Path(candidate)
        else:
            continue
        if not os.path.lexists(path) and candidate not in missing:
            missing.append(candidate)
    return missing


def _scan_launch_file(path):
    """One pass over a localconfig.vdf (bytes, never decoded as a whole).

    Returns (lines or None, findings); lines are kept only when something was
    found, so `--fix` can rewrite the file without reading it again.
    """
    try:
        data = path.read_bytes()
    except OSError:
        return None, []
    if b'LaunchOptions' not in data:
        return None, []
    lines = data.split(b'\n')
    findings, appid = [], ''
    for index, line in enumerate(lines):
        if b'"' not in line:
            continue
        found = _VDF_APPID.match(line)
        if found:
            appid = found.group(1).decode()
            continue
        if b'LaunchOptions' not in line:
            continue
        match = _VDF_LAUNCH.match(line)
        if not match or not match.group(2):
            continue
        option = re.sub(r'\\(["\\])', r'\1', match.group(2).decode('utf-8', errors='surrogateescape'))
        missing = _missing_launch_paths(option)
        if missing:
            findings.append({'file': path, 'line': index, 'appid': appid,
                             'option': option, 'missing': missing})
    return (lines if findings else None), findings


def scan_launch_options():
    """Every launch option whose absolute path is gone: [{appid, option, missing, file, line}]."""
    found = []
    for user in _steam_user_configs():
        _, findings = _scan_launch_file(user / 'config' / 'localconfig.vdf')
        found.extend(findings)
    return found


def launchopts_warning():
    count = len(scan_launch_options())
    if not count:
        return ''
    return ('%d Steam launch option(s) point at paths that no longer exist (the game may fail '
            'to start); inspect: luna steam launchopts' % count)


def fix_launch_options():
    """Back up each affected localconfig.vdf, then clear only the offending options."""
    cleared, stamp = 0, time.strftime('%Y%m%d-%H%M%S')
    for user in _steam_user_configs():
        path = user / 'config' / 'localconfig.vdf'
        lines, findings = _scan_launch_file(path)
        if not findings:
            continue
        backup = path.with_name('localconfig.vdf.luna-bak-%s' % stamp)
        shutil.copy2(path, backup)
        for finding in findings:
            match = _VDF_LAUNCH.match(lines[finding['line']])
            lines[finding['line']] = match.group(1) + match.group(3)
            cleared += 1
        temporary = path.with_name(path.name + '.luna-tmp')
        temporary.write_bytes(b'\n'.join(lines))
        shutil.copystat(path, temporary)
        os.replace(temporary, path)
        print('launchopts: backup %s' % backup)
    return cleared


def cmd_launchopts(options):
    findings = scan_launch_options()
    if not findings:
        print('launchopts: every absolute path in your launch options exists')
        return EXIT_OK
    for item in findings:
        print('app %-10s %s\n    missing: %s' % (item['appid'] or '?', item['option'],
                                                  ', '.join(item['missing'])))
    if not getattr(options, 'fix', False):
        print('\n%d option(s) affected. Close Steam, then: luna steam launchopts --fix '
              '(backs up localconfig.vdf, clears only these options)' % len(findings))
        return EXIT_OK
    if steam_running():
        print('REFUSING to fix: Steam is running and would overwrite localconfig.vdf on exit. '
              'Close it completely first.', file=sys.stderr)
        return EXIT_REFUSED
    cleared = fix_launch_options()
    print('launchopts: cleared %d option(s)' % cleared)
    return EXIT_OK


# --------------------------------------------------------------------------- pacman IgnorePkg

def ignore_pkg_command():
    return ['sudo', 'sed', '-i', r'/^\[options\]/a IgnorePkg = steam', str(pacman_conf_path())]


def ignore_pkg_hint():
    """One line when a pacman-owned Steam could be replaced by a system update ('' otherwise)."""
    try:
        managed, ignored = pacman_steam_state()
    except Exception:  # noqa: BLE001 - a hint must never fail an install
        return ''
    if managed and not ignored:
        return ('hint      : Steam is pacman-managed and not in IgnorePkg; run: luna steam ignore-pkg')
    return ''


def cmd_ignore_pkg(options):
    managed, ignored = pacman_steam_state()
    if not managed:
        print('pacman : Steam is not installed through pacman; nothing to do')
        return EXIT_OK
    if ignored:
        print('pacman : "steam" is already in IgnorePkg')
        return EXIT_OK
    command = ignore_pkg_command()
    print('pacman : Steam is pacman-managed and NOT in IgnorePkg: a system update can replace it.')
    print('Run (needs sudo):\n    %s' % ' '.join(shlex.quote(part) for part in command))
    if not getattr(options, 'apply', False):
        print('or let Luna do it: luna steam ignore-pkg --apply')
        return EXIT_OK
    if not sys.stdin.isatty():
        print('REFUSING: --apply needs an interactive terminal for the sudo prompt '
              '(Luna never runs sudo unattended).', file=sys.stderr)
        return EXIT_REFUSED
    return subprocess.run(command).returncode


# --------------------------------------------------------------------------- bootstrap

UPSTREAM_REPO = 'swwayps/luatools-moon'
UPSTREAM_INSTALLER_COMMIT = '2c739b5349a972bb1932ac9457b0acf91b09e044'
UPSTREAM_INSTALLER_SHA256 = '1c6896832d32d6d17c5635daf95b539763955f7ea1d9bd4446e24c2e8a5806c2'

# Known-good ubuntu12 client (the manifest "version" field; ubuntu12_32/steam and
# steamclient.so were built 2026-10-22). Bump these two lines when a newer
# rollback client has been validated (see README "Bumping the known-good build").
KNOWN_GOOD_UBUNTU12_BUILD = '1792618285'
KNOWN_GOOD_UBUNTU12_DATE = 'Oct 22 2026'
# Valve deleted the ubuntu12 manifests from SteamTracking HEAD on 2026-11-12, so the exact
# manifest is pinned by commit + sha256. To bump: pick a commit from `luna steam history`,
# download its file, set the three values (version must equal KNOWN_GOOD_UBUNTU12_BUILD).
KNOWN_GOOD_MANIFEST_COMMIT = '4acc42276f213dd41516da7863064fffb8711f6d'
KNOWN_GOOD_MANIFEST_SHA256 = '5e474d18586e39cac7650382c25c7b4ca1ec2bed785f4eaa36f4ca6d3c9b5d60'
KNOWN_GOOD_MANIFEST_URL = ('https://raw.githubusercontent.com/SteamTracking/SteamTracking/%s/'
                           'ClientManifest/steam_client_ubuntu12' % KNOWN_GOOD_MANIFEST_COMMIT)
STEAMTRACKING_HISTORY_URL = ('https://api.github.com/repos/SteamTracking/SteamTracking/commits'
                             '?path=ClientManifest/steam_client_ubuntu12&per_page=30')
STEAM_CDN = 'https://client-fastly.steamstatic.com/client/'
# 32-bit libcrypto.so.3 (OpenSSL 3.5.7, ELF32 i386) from the MIT-licensed SteamRollbackrLite.
LIBCRYPTO_URL = ('https://raw.githubusercontent.com/foodiemonster/SteamRollbackrLite/'
                 '26e7faa545306f034e6d2d4af5e2c02e05dcdf6f/assets/libcrypto.so.3')
LIBCRYPTO_SHA256 = 'c1a4d22c176c8869b3f91083e5c9ff7921da791294397c7db2814281fc99467f'
LIBCRYPTO_SYSTEM_PATHS = ['/usr/lib32/libcrypto.so.3', '/usr/lib/i386-linux-gnu/libcrypto.so.3',
                          '/usr/lib/libcrypto.so.3']
OS_RELEASE_PATH = '/etc/os-release'

# One table for every system package: (group, probe command or None, {manager: packages}).
# `probe` skips the row when that command exists; otherwise the package manager is asked.
# Native Steam only (no flatpak). 32-bit rows need multilib/i386 (see system_package_plan).
_BASE_TOOL_ROWS = [('core', tool, {m: [tool] for m in ('pacman', 'apt-get', 'dnf')})
                   for tool in ('curl', 'jq', 'tar', 'unzip', 'git', 'python3', 'zenity')]
SYSTEM_PACKAGES = [
    ('steam', 'steam', {'pacman': ['steam'], 'apt-get': ['steam-installer'], 'dnf': ['steam']}),
    # Optional/harmless: Arch's lib32-openssl is OpenSSL 4 (libcrypto.so.4 only), so the real
    # libcrypto.so.3 fix on every distro is `luna steam libcrypto --install` (run by lock/bootstrap).
    ('core', None, {'pacman': ['lib32-openssl'], 'apt-get': ['libssl3:i386'], 'dnf': ['openssl-libs.i686']}),
] + _BASE_TOOL_ROWS + [
    ('mangohud', None, {'pacman': ['mangohud', 'lib32-mangohud'], 'apt-get': ['mangohud', 'mangohud:i386'],
                        'dnf': ['mangohud', 'mangohud.i686']}),
    ('gamescope', 'gamescope', {m: ['gamescope'] for m in ('pacman', 'apt-get', 'dnf')}),
    ('gamemode', None, {'pacman': ['gamemode', 'lib32-gamemode'], 'apt-get': ['gamemode', 'gamemode:i386'],
                        'dnf': ['gamemode', 'gamemode.i686']}),
]
BOOTSTRAP_TOOLS = tuple(row[1] for row in _BASE_TOOL_ROWS)
VULKAN_NOTE = ('GPU drivers are not installed by Luna: make sure your Vulkan driver (and its 32-bit part) is '
               'present, e.g. vulkan-radeon/lib32-vulkan-radeon, nvidia-utils/lib32-nvidia-utils, '
               'mesa-vulkan-drivers:i386.')


def _package_installed(manager, package):
    if manager == 'pacman':
        command = ['pacman', '-Q', package]
    elif manager == 'apt-get':
        command = ['dpkg-query', '-W', '-f=${Status}', package]
    else:
        command = ['rpm', '-q', package]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    if manager == 'apt-get':
        return result.returncode == 0 and 'install ok installed' in (result.stdout or '')
    return result.returncode == 0


def _pacman_multilib_enabled():
    try:
        lines = pacman_conf_path().read_text().splitlines()
    except OSError:
        return False
    return any(line.strip() == '[multilib]' for line in lines)


def system_package_plan(manager, options=None):
    """(packages, pre_commands, notes) still needed for `manager`; nothing is run except read-only probes."""
    skip = {'mangohud': getattr(options, 'no_mangohud', False),
            'gamescope': getattr(options, 'no_gamescope', False),
            'gamemode': getattr(options, 'no_gamemode', False)}
    notes, packages, pre = [], [], []
    for group, probe, names in SYSTEM_PACKAGES:
        if skip.get(group) or manager not in names:
            continue
        if probe and shutil.which(probe):
            continue
        if group == 'steam' and (steam_home() / 'steam.sh').is_file():
            continue
        packages += [name for name in names[manager] if not _package_installed(manager, name)]
    packages = list(dict.fromkeys(packages))
    if manager == 'pacman':
        if not _pacman_multilib_enabled():
            blocked = [name for name in packages if name == 'steam' or name.startswith('lib32-')]
            if blocked:
                notes.append('[multilib] is not enabled in %s: uncomment the [multilib] section and its '
                             'Include line, run `sudo pacman -Syu`, then re-run luna bootstrap (skipped: %s)'
                             % (pacman_conf_path(), ', '.join(blocked)))
                packages = [name for name in packages if name not in blocked]
    elif manager == 'apt-get':
        if any(':i386' in name or name == 'steam-installer' for name in packages):
            try:
                foreign = subprocess.run(['dpkg', '--print-foreign-architectures'], capture_output=True,
                                         text=True, timeout=30).stdout.split()
            except (OSError, subprocess.SubprocessError):
                foreign = []
            if 'i386' not in foreign:
                pre.append(['sudo', 'dpkg', '--add-architecture', 'i386'])
            pre.append(['sudo', 'apt-get', 'update'])
    elif manager == 'dnf' and 'steam' in packages:
        notes.append('Fedora ships Steam in RPM Fusion; enable the rpmfusion-nonfree repository if the install fails')
    return packages, pre, notes


def install_system_packages(options, say):
    """Step 1 of bootstrap. Returns the commands it could not run (for the summary), never fails silently."""
    dry = getattr(options, 'dry_run', False)
    if getattr(options, 'no_system_packages', False) or getattr(options, 'skip_deps', False):
        say('system packages: skipped (--no-system-packages)')
        return
    manager, install = detect_package_manager()
    say('distro package manager: %s' % (manager or 'none detected (pacman/apt/dnf)'))
    if not manager:
        say('install these yourself: steam, 32-bit libcrypto/openssl, curl jq tar unzip git python3 zenity, '
            'mangohud (+32-bit), gamescope, gamemode (+32-bit)')
        return
    packages, pre, notes = system_package_plan(manager, options)
    for note in notes:
        say('NOTE: %s' % note)
    say(VULKAN_NOTE)
    if not packages:
        say('system packages: all present')
        return
    commands = pre + [['sudo'] + install + packages]
    say('system packages missing: %s' % ', '.join(packages))
    text = '\n    '.join(' '.join(shlex.quote(part) for part in command) for command in commands)
    if dry:
        say('would run (sudo prompt):\n    %s' % text)
        return
    if not sys.stdin.isatty():
        say('NOT installed (non-interactive: Luna never runs sudo unattended). Run yourself:\n    %s' % text)
        return
    if input('bootstrap : install them now with sudo? [y/N] ').strip().lower() not in ('y', 'yes'):
        say('skipped by choice; run yourself:\n    %s' % text)
        return
    for command in commands:
        if subprocess.run(command).returncode != 0:
            say('WARN: `%s` failed; continuing with what is present' % ' '.join(command))
            return


def known_good_line():
    """(ok, text): is the installed ubuntu12 manifest the recorded known-good build?"""
    try:
        text = (steam_home() / 'package' / STEAM_MANIFEST_NAME).read_text(encoding='utf-8', errors='replace')
        found = [build for build in re.findall(r'"version"\s+"(\d+)"', text) if build != '0']
        build = found[-1] if found else ''
    except OSError:
        build = ''
    if build == KNOWN_GOOD_UBUNTU12_BUILD:
        return True, ('Steam client: known-good ubuntu12 build %s (%s)'
                      % (KNOWN_GOOD_UBUNTU12_BUILD, KNOWN_GOOD_UBUNTU12_DATE))
    return False, ('WARN: Steam client: manifest version %s, known-good is ubuntu12 build %s (%s); '
                   'Valve may have republished it, or run: luna steam lock'
                   % (build or 'unreadable', KNOWN_GOOD_UBUNTU12_BUILD, KNOWN_GOOD_UBUNTU12_DATE))


def base_state():
    """'ready', 'partial' or 'none' for the Moon/Lumen/LuaTools base."""
    paths = [lumen_root() / 'lua' / 'boot.lua', lumen_root() / 'luatools' / 'backend' / 'main.lua',
             home() / '.local/share/SLSsteam/path/steam']
    present = [path.exists() for path in paths]
    return 'ready' if all(present) else 'partial' if any(present) else 'none'


def detect_package_manager():
    for name, install in (('pacman', ['pacman', '-S', '--needed']),
                          ('apt-get', ['apt-get', 'install', '-y']),
                          ('dnf', ['dnf', 'install', '-y'])):
        if shutil.which(name):
            return name, install
    return '', []


def fetch_base_installer(ref, expected_sha, destination):
    """Download the LuaTools-Moon installer at `ref`; verify sha256 when one is expected."""
    override = os.environ.get('LUNA_TEST_INSTALLER')  # tests only: a local fake installer
    if override:
        data = Path(override).read_bytes()
        destination.write_bytes(data)
        return hashlib.sha256(data).hexdigest()
    url = 'https://raw.githubusercontent.com/%s/%s/install.sh' % (UPSTREAM_REPO, ref)
    with urllib.request.urlopen(url, timeout=60) as response:
        data = response.read(4 * 1024 * 1024)
    digest = hashlib.sha256(data).hexdigest()
    if expected_sha and digest != expected_sha:
        raise ValueError('LuaTools-Moon installer checksum mismatch (got %s); aborting' % digest)
    destination.write_bytes(data)
    return digest


INSTALLER_TIMEOUT = 30 * 60


class InstallerTimeout(Exception):
    """The upstream installer ran past INSTALLER_TIMEOUT and was killed."""


def _kill_process_group(proc):
    """TERM, then KILL, the installer's whole process group (it is its own session)."""
    with contextlib.suppress(OSError):
        os.killpg(proc.pid, signal.SIGTERM)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    with contextlib.suppress(OSError):
        os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(subprocess.TimeoutExpired, OSError):
        proc.wait(timeout=10)


def _inhibit_prefix():
    """systemd-inhibit command prefix that blocks shutdown/sleep, or [] (silent fallback)."""
    if os.environ.get('LUNA_NO_INHIBIT') or not shutil.which('systemd-inhibit'):
        return []
    prefix = ['systemd-inhibit', '--what=shutdown:sleep', '--mode=block', '--who=Luna',
              '--why=Updating Lumen: do not shut down']
    try:
        probe = subprocess.run(prefix + ['true'], capture_output=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return []
    return prefix if probe.returncode == 0 else []


def verify_base_installer(ref, expected_sha):
    """Download the installer once, verify it, return its sha256 (nothing is run)."""
    with tempfile.TemporaryDirectory(prefix='luna-base-') as work:
        return fetch_base_installer(ref, expected_sha, Path(work) / 'install.sh')


def run_base_installer(ref, expected_sha, extra, timeout=None):
    """Run the upstream installer in its own session with a time limit.

    Returns its exit code; raises InstallerTimeout after killing the process
    group, and kills the group on any other exception too (SIGTERM included).
    """
    timeout = INSTALLER_TIMEOUT if timeout is None else timeout
    with tempfile.TemporaryDirectory(prefix='luna-base-') as work:
        script = Path(work) / 'luatools-moon-install.sh'
        digest = fetch_base_installer(ref, expected_sha, script)
        print('base      : installer %s@%s sha256 %s' % (UPSTREAM_REPO, ref[:12], digest), flush=True)
        command = _inhibit_prefix() + ['bash', str(script), '--nolaunch'] + list(extra)
        proc = subprocess.Popen(command, start_new_session=True)
        try:
            return proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_process_group(proc)
            raise InstallerTimeout('the base installer ran longer than %d minutes and was killed'
                                   % max(1, int(timeout // 60)))
        except BaseException:
            _kill_process_group(proc)
            raise


def _base_extra(options):
    extra = []
    for flag in ('slsteam_channel', 'plugin_channel', 'lumen_channel'):
        value = getattr(options, flag, None)
        if value:
            extra += ['--' + flag.replace('_', '-'), value]
    if getattr(options, 'keep_millennium', False):
        extra.append('--keep-millennium')
    return extra


ONELINER_ENV = 'LUNA_INSTALL_ONELINER'
ONELINER_FLAGS_ENV = 'LUNA_INSTALL_FLAGS'
ONELINER_URL = 'https://raw.githubusercontent.com/pgm1207/Luna/main/install.sh'


def bootstrap_flags(options):
    """The bootstrap flags this run was given, in the order `luna bootstrap` takes them."""
    parts = []
    if getattr(options, 'lock_steam_client', False):
        parts.append('--lock-steam-client')
    if getattr(options, 'no_system_packages', False):
        parts.append('--no-system-packages')
    if getattr(options, 'keep_steam_client', False):
        parts.append('--keep-steam-client')
    if getattr(options, 'steam_build', ''):
        parts += ['--steam-build', str(options.steam_build)]
    for key, flag in (('slsteam_channel', '--slsteam-channel'),
                      ('plugin_channel', '--plugin-channel'),
                      ('lumen_channel', '--lumen-channel')):
        value = getattr(options, key, '') or ''
        if value:
            parts += [flag, value]
    if getattr(options, 'keep_millennium', False):
        parts.append('--keep-millennium')
    if getattr(options, 'no_steam_machine', False):
        parts.append('--no-steam-machine')
    if getattr(options, 'lumen', False):
        parts.append('--lumen')
    return parts


def installed_launcher():
    """The `luna` command a finished install leaves behind, when it really exists."""
    launcher = home() / '.local/bin/luna'
    return launcher if launcher.is_file() and os.access(launcher, os.X_OK) else None


def resume_command(options):
    """The exact command that continues this pipeline after a failure.

    Run from install.sh's temporary tree, that is the one-liner itself: the temp
    directory is deleted when install.sh exits, and a fresh machine has no `luna`
    command yet. install.sh passes the flags the user gave in LUNA_INSTALL_FLAGS,
    so the pasted command is the one that ran. From a checkout or an installed
    copy the command names the code that is running, and `luna bootstrap` is only
    offered when that launcher is actually installed.
    """
    flags = bootstrap_flags(options)
    suffixes = (' ' + ' '.join(flags)) if flags else ''
    if os.environ.get(ONELINER_ENV) == '1':
        given = (os.environ.get(ONELINER_FLAGS_ENV) or '').strip() or suffixes.strip()
        return 'curl -fsSL %s | bash -s -- %s' % (ONELINER_URL, given)
    this_code = 'python3 %s bootstrap%s' % (Path(__file__).resolve(), suffixes)
    if installed_launcher() is not None:
        command = 'luna bootstrap' + suffixes
        if ROOT != _INSTALLED_ROOT:
            # A stale launcher or installed copy would run other code: name this one too.
            command += '  (this code: %s)' % this_code
        return command
    return this_code


def bootstrap_stop(options, step, reason, code=1):
    """Stop the pipeline at `step`: what failed, what state the machine is in, how to resume."""
    print('\nbootstrap : STOPPED at %s: %s' % (step, reason), file=sys.stderr)
    print('bootstrap : state: %s' % bootstrap_state_note(), file=sys.stderr)
    print('bootstrap : fix that, then resume with: %s' % resume_command(options), file=sys.stderr)
    return code


_BOOTSTRAP_LUNA_BEFORE = None  # fingerprint of the Luna tree taken before its half of the run


def luna_state_token():
    """Cheap fingerprint of the Luna-owned paths, to say honestly whether a run changed them."""
    paths = [addons_dir(), luna_home() / 'bin', luna_tools_dir() / 'luna.py',
             luna_tools_dir() / 'runtime', home() / '.local/bin/luna']
    token = []
    for path in paths:
        try:
            info = path.stat()
            token.append((str(path), info.st_mtime_ns, info.st_size))
        except OSError:
            token.append((str(path), 0, 0))
    return token


def bootstrap_state_note():
    """The machine's state at a bootstrap stop, in the terms the user needs to act on."""
    base = {'ready': 'Moon/Lumen/LuaTools base kept as it was',
            'partial': 'Moon/Lumen/LuaTools base is partial (its upstream installer must repair it)',
            'none': 'Moon/Lumen/LuaTools base not installed'}[base_state()]
    if install_marker_path().is_file():
        luna = ('Luna partially changed: this run wrote part of it and could not undo every '
                'change (`luna status` reports the interrupted install)')
    elif _BOOTSTRAP_LUNA_BEFORE is not None and luna_state_token() != _BOOTSTRAP_LUNA_BEFORE:
        luna = 'Luna was installed by this run'
    else:
        luna = 'Luna unchanged by this run'
    return '%s; %s' % (base, luna)


def bootstrap_preflight(options, say, dry):
    """Steam must be closed before anything is written, on either host.

    This used to run inside the Steam/Lumen steps, after the packages and after
    Luna's own tooling had already been written, and its wording said "Lumen"
    even when this run serves the luna host. One refusal, first, named for the
    host it serves.
    """
    host = serving_host()
    if not steam_running():
        return EXIT_OK
    if dry:
        # A dry run writes nothing, so a running Steam is not a reason to stop:
        # it only means a real run would refuse at this point.
        say('note: Steam is running; a real run would stop here until Steam is closed')
        return EXIT_OK
    return bootstrap_stop(options, 'Steam', (
        'Steam is running. Quit Steam (Steam → Exit) and run the command again. '
        'This run installs %s for the %s host and writes nothing while Steam is up.'
        % ('the Moon/Lumen base and Luna' if host == 'lumen' else 'Luna',
           host_label(host))), EXIT_REFUSED)


def bootstrap_tooling(options, say, dry):
    """Install the tooling of the code that is running before anything can use an old copy.

    A bootstrap from a checkout or an unpacked bundle used to leave the copy from
    an earlier run (and the `luna` launcher, and every child process) pointed at
    that old tree. Such a copy has no runtime/ tree, so it takes ROOT from its own
    directory and looks for vendor/MANIFEST.json in the Lumen tree: the "is
    missing; run scripts/vendor-update.sh" dead end. Deploy the current runtime
    tree and luna.py first; then the old copy is replaced, never consulted.
    """
    tools = luna_tools_dir()
    target = tools / 'luna.py'
    if ROOT == _INSTALLED_ROOT:
        problems = runtime_problems()
        if problems:
            return bootstrap_stop(options, 'Tooling', (
                'the installed copy this run started from cannot serve it (%s). Installing the '
                'current tooling first needs its source: run this from a checkout or an unpacked '
                'bundle (python3 <checkout>/scripts/luna.py bootstrap)' % problems[0]))
        say('tooling: running from the installed copy %s (runtime tree verified)' % target)
        return EXIT_OK
    missing = ([rel for rel, source in runtime_sources() if not source.is_file()]
               + ([] if MANIFEST_PATH.is_file() else ['vendor/MANIFEST.json']))
    if missing:
        return bootstrap_stop(options, 'Tooling', (
            'the code that is running is incomplete: %s is missing under %s. An older installed '
            'copy cannot fix itself; start the run from a checkout or an unpacked release '
            '(python3 <checkout>/scripts/luna.py bootstrap) so the current tooling is installed '
            'first' % (missing[0], ROOT)))
    if dry:
        say('would install the current tooling first (%s and its runtime/ tree)' % target)
        return EXIT_OK
    try:
        write_tooling()
    except OSError as error:
        return bootstrap_stop(options, 'Tooling', (
            'could not install the current tooling to %s: %s' % (tools, error)))
    say('installed the current tooling first: %s (runtime/ %s)' % (target, tools / 'runtime'))
    return EXIT_OK


def bootstrap_channels(options):
    """The owner's channels file when it exists, CLI flags on top, stable otherwise."""
    channels = lumen_channels_effective()
    flags = {'slsteam_moon': 'slsteam_channel', 'plugin': 'plugin_channel', 'lumen': 'lumen_channel'}
    for key, _flag in LUMEN_CHANNEL_FLAGS:
        value = getattr(options, flags[key], '') or ''
        if value:
            channels[key] = value
    return channels


def bootstrap_steam(options, say, dry):
    """Step 1: pin and hard-lock the Steam client. Never moves it to Valve's latest."""
    keep = getattr(options, 'keep_steam_client', False)
    steam_build = getattr(options, 'steam_build', '') or ''
    if keep:
        say('Steam client: kept as is (--keep-steam-client); not pinned')
        return EXIT_OK
    if (steam_home() / 'steam.sh').is_file():
        say('Steam directory exists (%s)' % steam_home())
    elif dry:
        say('would run `steam -exitsteam` once to create %s (timeout 30 min)' % steam_home())
        say('would then pin and hard-lock the ubuntu12 client %s' % KNOWN_GOOD_UBUNTU12_BUILD)
        return EXIT_OK
    elif not shutil.which('steam'):
        return bootstrap_stop(options, 'Steam', (
            'Steam is not installed for this user (%s is missing). Install native Steam first '
            '(Arch: sudo pacman -S steam with [multilib] enabled; Debian/Ubuntu: sudo apt install '
            'steam-installer), launch it once so it finishes its first update, then resume'
            % steam_home()))
    else:
        say('running Steam once to create %s (steam -exitsteam)' % steam_home())
        try:
            subprocess.run(['steam', '-exitsteam'], timeout=1800)
        except (OSError, subprocess.SubprocessError) as error:
            say('Steam first run did not finish: %s' % error)
        if not (steam_home() / 'steam.sh').is_file():
            return bootstrap_stop(options, 'Steam', (
                '%s is still missing; launch Steam once, then resume' % steam_home()))
    if steam_build:
        if steam_build != STEAM_COMPAT_BUILD:
            return bootstrap_stop(options, 'Steam', '--steam-build supports only the verified '
                                  'historical build %s' % STEAM_COMPAT_BUILD, EXIT_REFUSED)
        if dry:
            say('would install/verify build %s, pin it and block updates' % STEAM_COMPAT_BUILD)
            return EXIT_OK
        ok, message = ensure_compatible_steam()
        say(message)
        if not ok:
            return bootstrap_stop(options, 'Steam', message)
        ensure_steam_cfg_block()
        sync_steam_pin_copy()
    elif (steam_hard_lock_active() and read_steam_pin().get('clientBuild')
          and steam_state().get('binariesMatch') is not False):
        say('Steam client: ubuntu12 hard lock already active (build %s); kept'
            % read_steam_pin().get('clientBuild'))
        print_heal(heal_steam_lock(), 'bootstrap ')
    else:
        if dry:
            say('would roll back to the ubuntu12 client %s, pin, cache and hard-lock it '
                '(Steam.cfg + steam.cfg, package/ read-only)' % KNOWN_GOOD_UBUNTU12_BUILD)
            return EXIT_OK
        code, message = _lock_steam_client(False, lambda: False)
        say(message)
        if code != EXIT_OK:
            return bootstrap_stop(options, 'Steam', message, code)
        sync_steam_pin_copy()
    pin = read_steam_pin()
    build = str(pin.get('clientBuild') or '')
    cfg = steam_cfg_path()
    if not build or not cfg.is_file() or not steam_updates_blocked():
        return bootstrap_stop(options, 'Steam', (
            'the pin or steam.cfg is missing (pin %r, %s, blocked=%s)'
            % (build or 'none', cfg, steam_updates_blocked())))
    say('verified: Steam client pinned to %s and update-locked (%s)' % (build, cfg))
    return EXIT_OK


def bootstrap_lumen(options, say, dry):
    """Step 2: the pinned LuaTools-Moon base, with the owner's channels."""
    state = base_state()
    if state == 'partial':
        return bootstrap_stop(options, 'Lumen', (
            'a partial Moon/Lumen installation was found; repair it with its upstream installer '
            'first (Luna never removes or replaces base components)'), EXIT_REFUSED)
    if state == 'ready':
        say('Moon/Lumen/LuaTools base already installed; kept')
        return EXIT_OK
    channels = bootstrap_channels(options)
    listing = ', '.join('%s=%s' % item for item in channels.items())
    if dry:
        say('would install the pinned LuaTools-Moon base (commit %s, sha256 %s) with channels: %s'
            % (UPSTREAM_INSTALLER_COMMIT[:12], UPSTREAM_INSTALLER_SHA256[:12], listing))
        return EXIT_OK
    reasons = [reason for reason in refuse_reasons(for_install=False) if 'Steam is running' in reason]
    if reasons:
        return bootstrap_stop(options, 'Lumen', reasons[0], EXIT_REFUSED)
    channels_file = lumen_channels_path()
    saved = channels_file.read_bytes() if channels_file.is_file() else None
    extra = lumen_installer_extra(channels, lumen_millennium_choice())
    say('installing the pinned LuaTools-Moon base (commit %s, sha256 %s) with channels: %s'
        % (UPSTREAM_INSTALLER_COMMIT[:12], UPSTREAM_INSTALLER_SHA256[:12], listing))
    try:
        code = run_base_installer(UPSTREAM_INSTALLER_COMMIT, UPSTREAM_INSTALLER_SHA256, extra)
    except InstallerTimeout as error:
        return bootstrap_stop(options, 'Lumen', str(error))
    except (OSError, ValueError) as error:
        return bootstrap_stop(options, 'Lumen', str(error), EXIT_REFUSED)
    if saved is not None and (not channels_file.is_file()
                              or channels_file.read_bytes() != saved):
        try:
            channels_file.parent.mkdir(parents=True, exist_ok=True)
            channels_file.write_bytes(saved)
            say('restored the owner channels file (the installer rewrote it)')
        except OSError:
            pass
    if code != 0:
        return bootstrap_stop(options, 'Lumen',
                              'the pinned base installer failed (exit %d)' % code)
    if base_state() != 'ready':
        return bootstrap_stop(options, 'Lumen', (
            'the base installer exited 0 but boot.lua, the LuaTools backend or SLSsteam is missing'))
    say('verified: Lumen boot.lua, LuaTools backend and SLSsteam are installed')
    # The base installer drops steam.cfg and may replace Lumen/sls-luna. The pin
    # was taken first on purpose; re-assert the block without moving Steam.
    pin = read_steam_pin()
    if pin:
        if not steam_pin_path().is_file():
            write_json_file(steam_pin_path(), pin)
        ensure_steam_cfg_block()
        sync_steam_pin_copy()
        if not steam_updates_blocked() or not steam_cfg_path().is_file():
            return bootstrap_stop(options, 'Lumen',
                                  'the Steam update block was lost during the base install')
        say('verified: the Steam pin and update block survived the base install')
    return EXIT_OK


def bootstrap_luna(options, say, dry):
    """Step 3: Luna on top, then check the bridge, the block and `luna status`."""
    lumen = bool(getattr(options, 'lumen', False)) or active_host() == 'lumen'
    install_options = argparse.Namespace(
        only=[], force=getattr(options, 'force', False), dry_run=dry,
        keep_steam_client=True, steam_build='', lumen=lumen,
        lock_steam_client=getattr(options, 'lock_steam_client', False),
        no_steam_machine=getattr(options, 'no_steam_machine', False),
        migrate_steam_machine=getattr(options, 'migrate_steam_machine', False))
    if dry:
        say('would verify the payloads and install the Luna add-ons, bridge, tooling, '
            'shortcuts and the update timer')
        return EXIT_OK
    try:
        manifest = load_manifest()
    except SystemExit as error:
        return bootstrap_stop(options, 'Luna', (
            '%s. The code running here is %s, so the pinned payloads are not beside it; start '
            'the run from a checkout or an unpacked release, never from an older installed copy'
            % (error, ROOT)))
    problems = _manifest_problems(manifest, ROOT)
    missing = [problem for problem in problems if problem.startswith('missing ')]
    if missing:
        return bootstrap_stop(options, 'Luna', (
            'the code that is running is incomplete: %s' % missing[0]))
    if problems:
        say('WARN: %d payload(s) differ from vendor/MANIFEST.json (development checkout); '
            'the installed bytes are still verified below' % len(problems))
    say('installing Luna on top')
    code = cmd_install(install_options)
    if code != EXIT_OK:
        return bootstrap_stop(options, 'Luna', 'the add-on install failed (exit %d)' % code, code)
    if lumen:
        check = patcher('check')
        if check.returncode != 0 or 'bridge installed' not in (check.stdout or ''):
            return bootstrap_stop(options, 'Luna', 'the bridge patch is not installed (%s)'
                                  % (check.stderr.strip() or check.stdout.strip()), EXIT_PATCH)
        if 'lumen self-update blocked' not in (check.stdout or ''):
            return bootstrap_stop(options, 'Luna', "Lumen's own updater is not blocked", EXIT_PATCH)
        say('verified: bridge installed, Lumen self-update blocked')
    else:
        if not (luna_home() / 'bin/luna-host').is_file():
            return bootstrap_stop(options, 'Luna', 'the luna-host binary was not installed')
        say('verified: luna-host installed and serving Luna directly')
    status = cmd_status(argparse.Namespace(only=[], strict=False))
    if status != EXIT_OK:
        return bootstrap_stop(options, 'Luna', (
            'luna status reports drift or an incomplete install (exit %d)' % status), status)
    say('verified: luna status clean')
    return EXIT_OK


def cmd_bootstrap(options):
    """Fresh-PC setup: packages, optional Steam pin/Lumen base, then Luna."""
    global _TARGET_HOST, _BOOTSTRAP_LUNA_BEFORE
    dry = getattr(options, 'dry_run', False)
    lumen = bool(getattr(options, 'lumen', False)) or active_host() == 'lumen'
    pin = getattr(options, 'lock_steam_client', False)
    _TARGET_HOST = 'lumen' if lumen else 'luna'
    say = lambda text: print('bootstrap : %s' % text, flush=True)  # noqa: E731
    if flatpak_steam_only():
        say(FLATPAK_WARNING)
    # Steam closed first: nothing below may write while it is up, and the refusal
    # names the host this run serves (it used to fire late and say "Lumen").
    code = bootstrap_preflight(options, say, dry)
    if code != EXIT_OK:
        return code
    # Then replace any older installed copy with the code that is running, so no
    # step (or child process) can be answered by a copy without a runtime tree.
    code = bootstrap_tooling(options, say, dry)
    if code != EXIT_OK:
        return code
    install_system_packages(options, say)
    if lumen or pin:
        code = bootstrap_steam(options, say, dry)
        if code != EXIT_OK:
            return code
    else:
        say('Steam client: left alone (no pin; use --lock-steam-client to pin it)')
    if lumen:
        code = bootstrap_lumen(options, say, dry)
        if code != EXIT_OK:
            return code
    else:
        say('host: luna (standalone); Lumen/LuaTools are not installed (use --lumen to opt in)')
    _BOOTSTRAP_LUNA_BEFORE = luna_state_token()
    code = bootstrap_luna(options, say, dry)
    if code != EXIT_OK:
        return code
    if dry:
        say('would then enable the update timer and run doctor')
        print('\nDRY RUN: nothing was written.')
        return EXIT_OK
    keep = getattr(options, 'keep_steam_client', False)
    if lumen or pin:
        print_heal(heal_steam_lock())
        if not keep and libcrypto_status()[0] == 'missing':
            code, note = install_libcrypto(False)
            say(note if code == 0 else 'WARN: %s' % note)
        ok, line = known_good_line()
        say(line if not keep else 'Steam client kept as is (--keep-steam-client); known-good check skipped')
        if not keep and steam_state().get('cacheReady') is False:
            say('WARN: the Steam client cache is missing; run: luna steam cache (Steam closed)')
        if pacman_steam_state()[0]:
            cmd_ignore_pkg(argparse.Namespace(apply=sys.stdin.isatty()))
    cmd_timer(argparse.Namespace(action='on'))
    print('\n== luna doctor ==')
    cmd_doctor(argparse.Namespace())
    print('\nbootstrap : done. Start Steam to load Luna. Update later with: luna update-all')
    return EXIT_OK


def cmd_update_all(options):
    """Update Luna add-ons, re-verify the base, keep the lock; never upgrades Steam.

    Scope: only Luna's own add-ons and (with --base-latest) the Moon/Lumen base.
    The Steam client is never touched: the add-on path runs `install` with
    LUNA_KEEP_STEAM_CLIENT=1 and the base path re-applies the lock afterwards.
    """
    dry = getattr(options, 'dry_run', False)
    base_latest = getattr(options, 'base_latest', False)
    if base_latest and not getattr(options, 'yes', False) and not dry:
        print('REFUSING: --base-latest moves the Moon/Lumen base to the unpinned upstream main. '
              'Re-run with --yes to confirm (the installer sha256 is shown first).', file=sys.stderr)
        return EXIT_REFUSED
    source = ('bundle %s' % options.bundle if getattr(options, 'bundle', None)
              else 'url %s' % options.url if getattr(options, 'url', None) else 'the latest release (gh)')
    if dry:
        print('update-all: would update the Luna add-ons from %s' % source)
        print('update-all: would re-verify the base against the pinned installer (commit %s)'
              % UPSTREAM_INSTALLER_COMMIT[:12])
        if base_latest:
            print('update-all: would fetch the upstream MAIN installer, show its sha256 and run it (--yes)')
        print('update-all: would re-apply the Steam lock/heal; Steam itself is never upgraded while locked')
        print('update-all: ' + steam_plan_line())
        print('\nDRY RUN: nothing was written.')
        return EXIT_OK
    if getattr(options, 'terminal', False):
        return cmd_update_all_terminal(options)
    pin_before = str(read_steam_pin().get('clientBuild') or '')
    print('update-all: ' + steam_plan_line())
    status = EXIT_OK
    code = cmd_update(argparse.Namespace(
        bundle=getattr(options, 'bundle', None), url=getattr(options, 'url', None),
        latest=not (getattr(options, 'bundle', None) or getattr(options, 'url', None)),
        dry_run=False, only=[]))
    if code != EXIT_OK:
        print('update-all: the add-on update did not complete (exit %d)' % code, file=sys.stderr)
        status = code
    if base_state() != 'ready':
        if active_host() == 'luna' and base_state() == 'none':
            print('update-all: Lumen base: not installed (standalone host); add-ons only')
        else:
            print('update-all: the Moon/Lumen base is not complete; run: luna bootstrap', file=sys.stderr)
            status = status or 1
    elif base_latest:
        # Same lock, same code path as `luna lumen update --installer-latest --yes`.
        code = cmd_lumen_guard(argparse.Namespace(
            lumen_action='update', dry_run=False, yes=True, queue=False, json=False, check=False, backup='',
            installer_latest=True, reinstall=False, keep_millennium=False, remove_millennium=False,
            with_settings=False))
        status = status or code
    else:
        try:
            with tempfile.TemporaryDirectory(prefix='luna-base-') as work:
                digest = fetch_base_installer(UPSTREAM_INSTALLER_COMMIT, UPSTREAM_INSTALLER_SHA256,
                                              Path(work) / 'install.sh')
            print('update-all: base verified against the pinned installer (sha256 %s); pin not moved'
                  % digest[:12])
        except (OSError, ValueError) as error:
            print('update-all: WARN: could not re-verify the base installer (%s)' % error,
                  file=sys.stderr)
    print_heal(heal_steam_lock(), 'update-all')
    print('update-all: ' + known_good_line()[1])
    pin_after = str(read_steam_pin().get('clientBuild') or '')
    if pin_before and pin_after != pin_before:
        print('update-all: SCOPE VIOLATION: the update path moved the Steam client pin %s -> %s; '
              'the Steam client must not move here. Re-pin with: luna steam lock'
              % (pin_before, pin_after), file=sys.stderr)
        status = status or 1
    if read_steam_pending():
        print('update-all: note: a Steam client job was queued by something else (%s); update-all '
              'never queues Steam client work and leaves it alone'
              % (read_steam_pending().get('action') or 'unknown'), file=sys.stderr)
    print('update-all: ' + steam_plan_line())
    if not steam_hard_lock_active() and not read_steam_pin():
        print('update-all: Steam is not locked; to freeze the client run: luna steam lock')
    return status


# --------------------------------------------------------------------------- visible terminal updates
#
# `luna update-all --terminal` and `luna lumen update --terminal` do the planning
# and the Steam shutdown here, then run the ordinary command (without --terminal)
# inside a terminal emulator so the owner watches it. The child output is teed to
# a log under Luna's state dir; the parent never holds the writer lock while the
# child needs it.

STEAM_SHUTDOWN_TIMEOUT = 60
TERMINAL_EMULATORS = ('foot', 'alacritty', 'kitty', 'wezterm', 'gnome-terminal', 'konsole', 'xterm')


def steam_plan_line():
    """The scope line: updating Luna never moves the Steam client build."""
    build = str(read_steam_pin().get('clientBuild') or '')
    if build:
        return 'Steam client: unchanged (pinned %s)' % build
    return ('Steam client: unchanged (no pin recorded; Steam is not touched - '
            'run: luna steam lock to pin it)')


def steam_update_processes():
    """The processes an update needs gone: the client and its web helper."""
    return [name for name in ('steam', 'steamwebhelper') if process_running(name)]


def close_steam_for_update(timeout=STEAM_SHUTDOWN_TIMEOUT, probe=None, run=None, sleep=None):
    """Ask Steam itself to quit (`steam -shutdown`) and wait for it; never signals it.

    Returns (ok, note). After `timeout` seconds with either process still up it
    refuses with the processes named, so the caller changes nothing.
    """
    probe = probe or steam_update_processes
    run = run or subprocess.run
    sleep = sleep or time.sleep
    if not probe():
        return True, 'Steam is not running'
    try:
        run(['steam', '-shutdown'], capture_output=True, timeout=30)
    except FileNotFoundError:
        return False, ('could not run `steam -shutdown` (steam is not on PATH); '
                       'close Steam with Steam -> Exit and run the command again')
    except (OSError, subprocess.SubprocessError) as error:
        return False, ('could not run `steam -shutdown` (%s); close Steam with Steam -> Exit'
                       % error)
    deadline = time.monotonic() + max(0, int(timeout))
    while time.monotonic() < deadline:
        if not probe():
            return True, 'Steam quit'
        sleep(1)
    return False, ('Steam was still running after %d s (%s); close it with Steam -> Exit and run '
                   'the command again. Nothing was changed.'
                   % (timeout, ', '.join(probe()) or 'steam'))


def update_log_path(now=None):
    """~/.local/share/Luna/state/update-<timestamp>.log ($LUNA_HOME aware)."""
    stamp = time.strftime('%Y%m%d-%H%M%S', time.localtime(now) if now is not None else time.localtime())
    return luna_state_dir() / ('update-%s.log' % stamp)


def terminal_shell_command(command, log_path):
    """One `bash -lc` line: run the command, tee into the log, keep the window open."""
    inner = ' '.join(shlex.quote(str(part)) for part in command)
    log = shlex.quote(str(log_path))
    return ("%s 2>&1 | tee -a %s; status=${PIPESTATUS[0]}; "
            "printf '\\n[luna] exit %%s; log: %%s\\n' \"$status\" %s; "
            "read -r -p '[luna] press Enter to close this window ' _ || true"
            % (inner, log, log))


def terminal_argv(emulator, shell_command, prefix=None):
    """The argv that opens `emulator` running one shell command."""
    exe = list(prefix or [emulator])
    run = ['bash', '-lc', shell_command]
    if emulator == 'kitty':
        return exe + run
    if emulator == 'wezterm':
        return exe + ['start', '--'] + run
    if emulator == 'gnome-terminal':
        return exe + ['--'] + run
    return exe + ['-e'] + run  # foot, alacritty, konsole, xterm, and any $TERMINAL


def find_terminal(which=None):
    """(emulator, argv prefix) from $TERMINAL first, then the known list; ('', []) when none."""
    which = which or shutil.which
    requested = os.environ.get('TERMINAL', '').strip()
    candidates = ([requested] if requested else []) + list(TERMINAL_EMULATORS)
    for candidate in candidates:
        try:
            parts = shlex.split(candidate)
        except ValueError:
            continue
        if not parts:
            continue
        found = which(parts[0])
        if not found:
            continue
        return os.path.basename(parts[0]), [found] + parts[1:]
    return '', []


def run_update_in_terminal(command, log_path=None, spawn=None, which=None):
    """Open `command` in a terminal, output teed to `log_path`.

    Returns (used, how, log_path, argv). `used` is the emulator name,
    'current-terminal' or 'subprocess' (so the caller can say which was used).
    """
    log_path = log_path or update_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    shell_command = terminal_shell_command(command, log_path)
    emulator, prefix = find_terminal(which)
    with contextlib.suppress(OSError):
        with open(log_path, 'a') as log:
            log.write('%s luna: %s (terminal: %s)\n'
                      % (time.strftime('%Y-%m-%d %H:%M:%S'), ' '.join(str(p) for p in command),
                         emulator or 'none found'))
    if emulator:
        argv = terminal_argv(emulator, shell_command, prefix)
        spawn = spawn or (lambda args: subprocess.Popen(args, start_new_session=True))
        try:
            spawn(argv)
        except OSError as error:
            return '', 'could not start %s (%s)' % (emulator, error), log_path, argv
        return emulator, 'opened %s; the update runs there and is teed to the log' % emulator, log_path, argv
    if sys.stdin.isatty() and sys.stdout.isatty():
        code = subprocess.call(['bash', '-lc', shell_command])
        return ('current-terminal', 'no terminal emulator found; ran in this terminal (exit %d)'
                % code, log_path, [])
    with open(log_path, 'ab') as log:
        code = subprocess.call(command, stdout=log, stderr=subprocess.STDOUT)
    return ('subprocess', 'no terminal emulator found; ran without a window (exit %d)' % code,
            log_path, [])


def confirm_terminal_update(options, label='this update'):
    """Yes/--yes or refuse; never guesses when stdin is not a terminal."""
    if getattr(options, 'yes', False):
        return True
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print('REFUSING: %s needs confirmation; re-run with --yes. Nothing was changed.' % label,
              file=sys.stderr)
        return False
    answer = input('Continue with %s? [y/N] ' % label).strip().lower()
    if answer in ('y', 'yes'):
        return True
    print('REFUSING: not confirmed. Nothing was changed.', file=sys.stderr)
    return False


def refuse_while_busy():
    """True (and refused, EXIT_BUSY) when another luna command owns the writer lock."""
    if _update_lock_held_by_other():
        print('REFUSING: another luna command (or a queued update) owns the update lock; '
              'wait for it to finish. Nothing was changed.', file=sys.stderr)
        return True
    return False


def report_terminal_launch(used, how, log_path):
    if used:
        print('terminal  : %s' % how)
        print('log       : %s' % log_path)
        return EXIT_OK
    print('terminal  : %s' % how, file=sys.stderr)
    return 1


def cmd_update_all_terminal(options):
    """`luna update-all --terminal`: plan, confirm, close Steam, run with a window."""
    if refuse_while_busy():
        return EXIT_BUSY
    print('update-all: plan')
    for line in update_all_plan_lines(options):
        print(line)
    if not confirm_terminal_update(options, label='update-all'):
        return EXIT_REFUSED
    ok, note = close_steam_for_update(timeout=getattr(options, 'steam_timeout', STEAM_SHUTDOWN_TIMEOUT))
    print('steam     : %s' % note)
    if not ok:
        print('REFUSING: %s' % note, file=sys.stderr)
        return EXIT_REFUSED
    command = [sys.executable, str(Path(__file__).resolve()), 'update-all', '--yes']
    for flag in ('bundle', 'url'):
        value = getattr(options, flag, None)
        if value:
            command += ['--' + flag, value]
    if getattr(options, 'base_latest', False):
        command.append('--base-latest')
    for name in getattr(options, 'only', None) or []:
        command += ['--only', name]
    used, how, log_path, _argv = run_update_in_terminal(command)
    return report_terminal_launch(used, how, log_path)


def update_all_plan_lines(options):
    """The plan text `update-all --terminal` prints (and dry-run echoes)."""
    source = ('bundle %s' % options.bundle if getattr(options, 'bundle', None)
              else 'url %s' % options.url if getattr(options, 'url', None) else 'the latest release (gh)')
    lines = ['update-all:   add-ons    : %s' % source,
             'update-all:   channels   : %s' % ', '.join(
                 '%s=%s' % item for item in lumen_channels_effective().items())]
    if getattr(options, 'base_latest', False):
        lines.append('update-all:   Lumen base : upstream MAIN installer (unpinned; its sha256 is shown first)')
    else:
        lines.append('update-all:   Lumen base : re-verify the pinned installer %s (pin not moved)'
                     % UPSTREAM_INSTALLER_COMMIT[:12])
    lines.append('update-all:   ' + steam_plan_line())
    return lines


def cmd_lumen_update_terminal(options):
    """`luna lumen update --terminal`: plan, confirm, close Steam, run with a window."""
    if getattr(options, 'queue', False):
        print('REFUSING: --terminal and --queue cannot be combined. Nothing was changed.',
              file=sys.stderr)
        return EXIT_REFUSED
    if refuse_while_busy():
        return EXIT_BUSY
    print('lumen     : plan')
    for line in lumen_plan_lines():
        print(line)
    print('lumen     : channels: %s' % ', '.join(
        '%s=%s' % item for item in lumen_channels_effective().items()))
    print('lumen     : ' + steam_plan_line() + '; the base installer runs with --nolaunch')
    latest = getattr(options, 'installer_latest', False)
    if latest:
        print('lumen     : would run the upstream MAIN installer (NOT pinned; its sha256 is shown first), '
              'then re-attach Luna: bridge, self-update block, Steam lock')
    else:
        print('lumen     : would run the PINNED installer %s (sha256 %s), then re-attach Luna'
              % (UPSTREAM_INSTALLER_COMMIT[:12], UPSTREAM_INSTALLER_SHA256[:12]))
    if not confirm_terminal_update(options, label='the Lumen update'):
        return EXIT_REFUSED
    ok, note = close_steam_for_update(timeout=getattr(options, 'steam_timeout', STEAM_SHUTDOWN_TIMEOUT))
    print('steam     : %s' % note)
    if not ok:
        print('REFUSING: %s' % note, file=sys.stderr)
        return EXIT_REFUSED
    command = [sys.executable, str(Path(__file__).resolve()), 'lumen', 'update', '--yes']
    for flag in ('installer_latest', 'reinstall', 'keep_millennium', 'remove_millennium'):
        option = '--' + flag.replace('_', '-')
        if getattr(options, flag, False):
            command.append(option)
    used, how, log_path, _argv = run_update_in_terminal(command)
    return report_terminal_launch(used, how, log_path)


# --------------------------------------------------------------------------- Lumen base update
#
# `luna lumen update` is the Luna-driven way to move the Moon/Lumen base. Lumen's
# own About-page updater is blocked (patch-lumen.py, BLOCK v1) because it would
# replace the Lua tree and wipe the Luna bridge. The base installer
# (swwayps/luatools-moon) is the same one `update-all --base-latest` runs; this
# wraps it with a backup before and a re-attach (boot.lua bridge, update block,
# Steam lock) after, and leaves a restorable backup if the new upstream cannot
# be patched.

# Everything the upstream installer replaces or deletes, as opaque trees/files:
# (backup name, path, names inside it the installer itself keeps and we skip).
LUMEN_KEEP_BACKUPS = 2
LUMEN_JOB_ATTEMPTS = 3
LUMEN_BIG_BYTES = 500 * 1024 * 1024
SLSTEAM_KEPT = ('backup', 'system-launcher-backup')


def lumen_backup_items():
    root = lumen_root()
    items = [(name, root / name, ()) for name in
             ('lua', 'lumen', 'versions.json', 'update-channels.json', 'luatools')]
    items.append(('slsteam', home() / '.local/share/SLSsteam', SLSTEAM_KEPT))
    # Depot keys and settings: upstream says it leaves these alone; kept so a
    # restore (and the post-update check) can prove it.
    items.append(('slsteam-config', home() / '.config/SLSsteam', ()))
    for name in ('steam.cfg', 'Steam.cfg'):
        items.append((name, steam_home() / name, ()))
    # The installer deletes these without asking (luatools data dirs always,
    # Millennium unless it is kept); with them in the backup a rollback is whole.
    for name, path in LUATOOLS_DATA_DIRS:
        items.append((name, path(), ()))
    if lumen_millennium_choice() != 'keep':
        for name, path in MILLENNIUM_USER_DIRS:
            items.append((name, path(), ()))
    return items


LUATOOLS_DATA_DIRS = (
    ('luatools-data', lambda: home() / '.local/share/luatools'),
    ('luatools-config', lambda: home() / '.config/luatools'),
    ('luatools-home', lambda: home() / '.luatools'),
)
MILLENNIUM_USER_DIRS = (
    ('millennium-config', lambda: Path(os.environ.get('XDG_CONFIG_HOME') or home() / '.config') / 'millennium'),
    ('millennium-data', lambda: Path(os.environ.get('XDG_DATA_HOME') or home() / '.local/share') / 'millennium'),
    ('millennium-steam', lambda: steam_home() / 'millennium'),
)
MILLENNIUM_SYSTEM_DIRS = (Path('/usr/lib/millennium'), Path('/usr/share/millennium'))


def lumen_options_path():
    return luna_tools_dir() / 'lumen-options.json'


def lumen_millennium_choice():
    """'keep', 'remove' or '' (no recorded choice)."""
    data = read_json_file(lumen_options_path())
    value = data.get('millennium') if isinstance(data, dict) else ''
    return value if value in ('keep', 'remove') else ''


def set_lumen_millennium_choice(choice):
    data = read_json_file(lumen_options_path())
    data = data if isinstance(data, dict) else {}
    data['millennium'] = choice
    write_json_file(lumen_options_path(), data)


def millennium_present():
    """Paths of an existing Millennium install the base installer would remove."""
    found = [path() for _name, path in MILLENNIUM_USER_DIRS if path().exists()]
    found += [path for path in MILLENNIUM_SYSTEM_DIRS if path.exists()]
    return found


LUMEN_CHANNEL_FLAGS = (('slsteam_moon', '--slsteam-channel'), ('plugin', '--plugin-channel'),
                       ('lumen', '--lumen-channel'))
LUMEN_CHANNEL_VALUES = ('stable', 'beta')


def lumen_channels():
    """The owner's update channels from Lumen's update-channels.json ({} when none)."""
    data = read_json_file(lumen_channels_path())
    if not isinstance(data, dict):
        return {}
    return {key: data[key] for key, _flag in LUMEN_CHANNEL_FLAGS if data.get(key) in LUMEN_CHANNEL_VALUES}


def lumen_channels_path():
    """Lumen's own channel file; the base installer reads it and is passed the flags too."""
    return lumen_root() / 'update-channels.json'


def lumen_channels_effective():
    """The three component channels, with the default (stable) filled in."""
    chosen = lumen_channels()
    return {key: chosen.get(key) or 'stable' for key, _flag in LUMEN_CHANNEL_FLAGS}


def set_lumen_channels(value, only=None):
    """Write update-channels.json atomically (temp file + rename) and return the channels.

    Raises ValueError for an unknown channel or component, so the CLI can refuse
    before anything is written.
    """
    if value not in LUMEN_CHANNEL_VALUES:
        raise ValueError('channel must be stable or beta, not %r' % value)
    names = list(only or [key for key, _flag in LUMEN_CHANNEL_FLAGS])
    unknown = [name for name in names if name not in [key for key, _flag in LUMEN_CHANNEL_FLAGS]]
    if unknown:
        raise ValueError('unknown component%s: %s (choose from %s)' % (
            's' if len(unknown) > 1 else '', ', '.join(unknown),
            ', '.join(key for key, _flag in LUMEN_CHANNEL_FLAGS)))
    data = read_json_file(lumen_channels_path())
    data = data if isinstance(data, dict) else {}
    for key, _flag in LUMEN_CHANNEL_FLAGS:  # explicit defaults survive a hand-edited file
        if data.get(key) not in LUMEN_CHANNEL_VALUES:
            data[key] = 'stable'
    for name in names:
        data[name] = value
    write_json_file(lumen_channels_path(), data)  # mkstemp + os.replace: never half a file
    return lumen_channels_effective()


def lumen_channels_audit_path():
    """Where a channel change is recorded (same state/message/at shape as the update result)."""
    return luna_tools_dir() / 'lumen-channels.json'


def record_lumen_channels(message, **extra):
    """Record a channel change in the CLI's audit form, in its own file so it never
    overwrites the Lumen update result the Updates page shows."""
    payload = {'state': 'applied', 'message': message, 'at': int(time.time())}
    payload.update(extra)
    write_json_file(lumen_channels_audit_path(), payload)
    return payload


def lumen_installer_extra(channels, millennium):
    """Arguments for the base installer so the owner's choices survive the update."""
    extra = []
    for key, flag in LUMEN_CHANNEL_FLAGS:
        extra += [flag, channels.get(key) or 'stable']
    if millennium == 'keep':
        extra.append('--keep-millennium')
    return extra


def _walk_files(path, ignore=()):
    """(relative path, absolute path) for every file and symlink, sorted."""
    if path.is_file() or path.is_symlink():
        return [(path.name, path)]
    found = []
    for current, dirs, files in os.walk(path):
        here = Path(current)
        if here == path:
            dirs[:] = [d for d in dirs if d not in ignore]
            files = [f for f in files if f not in ignore]
        for d in list(dirs):
            if (here / d).is_symlink():  # a link to a directory is recorded, not followed
                found.append(((here / d).relative_to(path).as_posix(), here / d))
                dirs.remove(d)
        for name in files:
            found.append(((here / name).relative_to(path).as_posix(), here / name))
    return sorted(found)


def _tree_size(path, ignore=()):
    total = 0
    for _rel, entry in _walk_files(path, ignore):
        with contextlib.suppress(OSError):
            total += entry.lstat().st_size
    return total


def _human(size):
    size = float(size)
    for unit in ('B', 'K', 'M', 'G'):
        if size < 1024 or unit == 'G':
            return ('%d%s' % (size, unit)) if unit == 'B' else ('%.1f%s' % (size, unit))
        size /= 1024


def _entry_digest(entry):
    if entry.is_symlink():
        return 'link:' + os.readlink(entry)
    digest = hashlib.sha256()
    with entry.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _manifest_of(path, ignore=()):
    return {rel: _entry_digest(entry) for rel, entry in _walk_files(path, ignore)}


def lumen_backup_plan():
    """[(name, path, bytes, present)] for the plan text and the backup."""
    rows = []
    for name, path, ignore in lumen_backup_items():
        present = path.exists() or path.is_symlink()
        rows.append((name, path, _tree_size(path, ignore) if present else 0, present))
    return rows


def lumen_pending_path():
    return luna_tools_dir() / 'pending-lumen.json'


def lumen_result_path():
    return luna_tools_dir() / 'lumen-update.json'


def luna_state_dir():
    """Machine-local Luna state OUTSIDE ~/.local/share/Lumen: $LUNA_HOME/state
    (default ~/.local/share/Luna/state). The upstream installer replaces the Lumen
    tree, so backups and the Steam pin copy must not live in it."""
    return luna_home() / 'state'


def lumen_backup_root():
    return luna_state_dir() / 'lumen-backups'


def lumen_legacy_backup_root():
    """Where backups lived before (inside the Lumen tree); still read, migrated on demand."""
    return luna_tools_dir() / 'lumen-backups'


def lumen_backup_roots():
    roots = [lumen_backup_root()]
    if lumen_legacy_backup_root() != roots[0]:
        roots.append(lumen_legacy_backup_root())
    return roots


def lumen_backup_path(name):
    """The directory of a backup by name, wherever it currently is."""
    for root in lumen_backup_roots():
        if (root / name / 'backup.json').is_file():
            return root / name
    return lumen_backup_root() / name


def migrate_lumen_backups():
    """Move old in-tree backups to the state dir. Safe to interrupt: copy to a hidden
    name, verify, rename, and only then delete the old copy; leftovers are cleaned up."""
    legacy, root = lumen_legacy_backup_root(), lumen_backup_root()
    if legacy == root or not legacy.is_dir():
        return []
    moved = []
    for old in sorted(legacy.iterdir()):
        if not (old / 'backup.json').is_file():
            continue
        target = root / old.name
        staging = root / ('.migrating-' + old.name)
        try:
            root.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                shutil.rmtree(staging, ignore_errors=True)
                shutil.copytree(old, staging, symlinks=True)
                os.replace(staging, target)
            ok, _problem = verify_lumen_backup(target)
            if ok:
                shutil.rmtree(old, ignore_errors=True)
                moved.append(old.name)
        except OSError as error:
            print('lumen     : could not migrate backup %s yet (%s); it stays where it is' % (old.name, error),
                  file=sys.stderr)
    return moved


def read_lumen_pending():
    pending = read_json_file(lumen_pending_path())
    return pending if isinstance(pending, dict) else {}


def record_lumen_result(state, message='', **extra):
    """Status the Updates page reads: queued | running | applied | failed | cancelled."""
    payload = {'state': state, 'message': message, 'at': int(time.time())}
    payload.update(extra)
    write_json_file(lumen_result_path(), payload)
    return payload


def lumen_tooling_problems():
    """Static check: files the running copy needs to re-attach and reinstall Luna."""
    problems = []
    if not PATCHER.is_file():
        problems.append('scripts/patch-lumen.py is missing (%s)' % PATCHER)
    if not BRIDGE_SOURCE.is_file():
        problems.append('bridge/bridge.lua is missing (%s)' % BRIDGE_SOURCE)
    if not MANIFEST_PATH.is_file():
        problems.append('vendor/MANIFEST.json is missing (%s)' % MANIFEST_PATH)
    if not (ROOT / 'scripts' / 'steam-machine.py').is_file():
        problems.append('scripts/steam-machine.py is missing (%s)' % (ROOT / 'scripts'))
    problems += runtime_problems()
    absent = ['%s/%s' % (name, rel) for name in ADDONS for rel, _dest in ADDON_FILES[name]
              if not payload_path(name, rel).is_file()]
    if absent:
        problems.append('add-on sources are missing: %s%s'
                        % (', '.join(absent[:6]), '…' if len(absent) > 6 else ''))
    return problems


def lumen_preflight():
    """Everything a base update needs, checked before anything is touched.

    A missing patcher or source tree used to surface only after the upstream
    installer had already replaced Lumen, which loses Luna. Refuse first.
    """
    problems = lumen_tooling_problems() + runtime_problems(full=True)
    if not problems:
        check = patcher('check')
        detail = check.stderr.strip() or check.stdout.strip()
        if check.returncode != 0:
            problems.append('the installed Lumen tree cannot be patched: %s'
                            % (detail or 'patch-lumen.py check failed'))
    if problems:
        return problems
    rows = lumen_backup_plan()
    needed = sum(row[2] for row in rows) + 32 * 1024 * 1024
    root = lumen_backup_root()
    try:
        root.mkdir(parents=True, exist_ok=True)
        probe = root / '.write-probe'
        probe.write_text('ok')
        probe.unlink()
        free = shutil.disk_usage(root).free
    except OSError as error:
        return ['the backup directory %s is not writable: %s' % (root, error)]
    if free < needed:
        problems.append('not enough free space for the backup in %s (%s needed, %s free)'
                        % (root, _human(needed), _human(free)))
    return problems


APPLY_WAIT_LIMIT = 6 * 3600
STEAM_CLOSED_STABLE_SECONDS = 10


def queued_update_exists():
    return bool(read_lumen_pending() or read_refresh_pending() or read_steam_pending()
                or read_json_file(luna_tools_dir() / 'pending.json'))


def wait_for_steam_closed(limit=APPLY_WAIT_LIMIT, stable=STEAM_CLOSED_STABLE_SECONDS,
                          probe=None, has_work=None, clock=time.monotonic, sleep=time.sleep):
    """Wait for Steam to stay shut, so a self-restart cannot trigger the update.

    Returns 'ready' once steam and steamwebhelper have both been gone for
    `stable` seconds continuously, 'cancelled' when the queued job is removed,
    or 'timeout' after `limit`. Never signals Steam.
    """
    probe = probe or (lambda: steam_running())
    has_work = has_work or queued_update_exists
    deadline = clock() + limit
    closed_since = None
    while clock() < deadline:
        if not has_work():
            return 'cancelled'
        if probe():
            closed_since = None
        else:
            if closed_since is None:
                closed_since = clock()
            elif clock() - closed_since >= stable:
                return 'ready'
        sleep(1)
    return 'timeout'



def read_lumen_versions():
    """{component: tag} from Lumen's own versions.json; empty when unreadable."""
    data = read_json_file(lumen_root() / 'versions.json')
    if not isinstance(data, dict):
        return {}
    return {str(name): str(entry.get('tag') or '') for name, entry in data.items()
            if isinstance(entry, dict)}


def read_lumen_fingerprints():
    """{component: {tag, id, size, channel}} as the installer stamped it in versions.json."""
    data = read_json_file(lumen_root() / 'versions.json')
    if not isinstance(data, dict):
        return {}
    return {str(name): {key: entry.get(key) for key in ('tag', 'id', 'size', 'channel', 'asset_at')}
            for name, entry in data.items() if isinstance(entry, dict)}


# (versions.json key, repo, beta file on the repo's beta branch, stable asset regex, stable mode)
LUMEN_COMPONENTS = (
    ('slsteam_moon', 'swwayps/slsteam-moon', 'dist/slsteam-moon-linux.zip',
     r'^slsteam-moon-linux-.*-lumen\.zip$', 'any'),
    ('lumen', 'swwayps/lumen', 'dist/lumen-linux.zip', r'^lumen-linux\.zip$', 'latest'),
    ('plugin', 'swwayps/luatools-moon', 'dist/luatools-linux.zip', r'^luatools-linux\.zip$', 'latest'),
)
UPSTREAM_META_TIMEOUT = 8


def _public_json(url):
    """GET public release metadata: no credentials, no cookies, generic user agent."""
    request = urllib.request.Request(url, headers={'Accept': 'application/vnd.github+json',
                                                   'User-Agent': 'luna-update-check'})
    with urllib.request.urlopen(request, timeout=UPSTREAM_META_TIMEOUT) as response:
        return json.loads(response.read(8 * 1024 * 1024).decode('utf-8'))


def _upstream_component(repo, beta_path, glob, mode, channel):
    """(info, note): what the installer would fetch for one component, or (None, why)."""
    note = ''
    if channel == 'beta':
        try:
            meta = _public_json('https://api.github.com/repos/%s/contents/%s?ref=beta' % (repo, beta_path))
            if isinstance(meta, dict) and meta.get('sha') and meta.get('download_url'):
                return {'tag': 'beta', 'id': meta['sha'], 'size': meta.get('size'), 'channel': 'beta'}, ''
        except urllib.error.HTTPError as error:
            if error.code not in (404, 422):
                return None, 'beta lookup failed (HTTP %s)' % error.code
        except (OSError, ValueError) as error:
            return None, 'beta lookup failed (%s)' % error
        note = 'no beta build published, the installer falls back to stable'
    try:
        if mode == 'any':
            releases = _public_json('https://api.github.com/repos/%s/releases?per_page=50' % repo)
            candidates = [(release, asset) for release in releases if not release.get('draft')
                          and not release.get('prerelease') for asset in release.get('assets') or []
                          if re.search(glob, asset.get('name') or '')]
            release, asset = candidates[0] if candidates else ({}, {})
        else:
            release = _public_json('https://api.github.com/repos/%s/releases/latest' % repo)
            matches = [a for a in release.get('assets') or [] if re.search(glob, a.get('name') or '')]
            asset = matches[0] if matches else {}
    except urllib.error.HTTPError as error:
        return None, 'release lookup failed (HTTP %s%s)' % (
            error.code, ', rate limited' if error.code in (403, 429) else '')
    except (OSError, ValueError, AttributeError, TypeError) as error:
        return None, 'release lookup failed (%s)' % error
    if not asset:
        return None, 'no matching release asset found'
    return {'tag': release.get('tag_name'), 'id': asset.get('id'), 'size': asset.get('size'),
            'channel': 'stable'}, note


def lumen_upstream_state(channels=None, installed=None, fetch=None):
    """Per component: installed vs what upstream has on the owner's channel.

    Only public, unauthenticated release metadata is read. Each row:
    {component, channel, installed, available, status, note} with status
    'newer', 'current' or 'unknown'. Overall 'newer' when any component is
    newer, 'current' when all are, else 'unknown' (offline, rate limited).
    """
    channels = lumen_channels() if channels is None else channels
    installed = read_lumen_fingerprints() if installed is None else installed
    fetch = fetch or _upstream_component
    rows = []
    for key, repo, beta_path, glob, mode in LUMEN_COMPONENTS:
        channel = channels.get(key) or 'stable'
        have = installed.get(key) or {}
        info, note = fetch(repo, beta_path, glob, mode, channel)
        row = {'component': key, 'channel': channel, 'installed': have or None, 'available': info,
               'note': note}
        if info is None:
            row['status'] = 'unknown'
        elif not have:
            row['status'] = 'newer'
            row['note'] = (note + '; ' if note else '') + 'no installed record in versions.json'
        elif (str(have.get('tag')) == str(info.get('tag')) and str(have.get('id')) == str(info.get('id'))
              and str(have.get('size')) == str(info.get('size'))):
            row['status'] = 'current'
        else:
            row['status'] = 'newer'
        rows.append(row)
    statuses = {row['status'] for row in rows}
    overall = 'newer' if 'newer' in statuses else 'unknown' if 'unknown' in statuses else 'current'
    return {'overall': overall, 'components': rows, 'channels': channels}


def describe_upstream_row(row):
    have = row['installed'] or {}
    got = row['available'] or {}
    label = lambda item: ('%s %s' % (item.get('tag') or '?', item.get('channel') or '?')) if item else '?'  # noqa: E731
    text = '%-12s %-6s installed %-14s available %-14s %s' % (
        row['component'], row['channel'], label(have), label(got) if got else 'unknown', row['status'])
    return text + ('  (%s)' % row['note'] if row['note'] else '')


def lumen_changed(before, after):
    """{component: 'old -> new'} for what the installer actually changed; {} when nothing."""
    changed = {}
    for key in sorted(set(before) | set(after)):
        old, new = before.get(key) or {}, after.get(key) or {}
        if (old.get('tag'), old.get('id'), old.get('size'), old.get('channel')) != \
                (new.get('tag'), new.get('id'), new.get('size'), new.get('channel')):
            changed[key] = '%s %s -> %s %s' % (old.get('tag') or '-', old.get('channel') or '',
                                                new.get('tag') or '-', new.get('channel') or '')
    return changed


def lumen_backups():
    """Backups newest first (directory names are timestamps)."""
    found = {}
    for root in lumen_backup_roots():
        if root.is_dir():
            for path in root.iterdir():
                if (path / 'backup.json').is_file():
                    found.setdefault(path.name, path)  # the state dir wins over a half-migrated copy
    return sorted(found.values(), key=lambda path: path.name, reverse=True)


def lumen_backup_info():
    rows = []
    for path in lumen_backups():
        meta = read_json_file(path / 'backup.json') or {}
        rows.append({'name': path.name, 'at': int(meta.get('at') or 0),
                     'bytes': int(meta.get('bytes') or 0), 'failed': bool(meta.get('failed')),
                     'items': meta.get('items') or []})
    return rows


def verify_lumen_backup(path):
    """(ok, problem): file count and sha256 of every file against backup.json."""
    meta = read_json_file(path / 'backup.json') or {}
    manifest = meta.get('manifest')
    if not isinstance(manifest, dict):
        return False, 'backup %s has no manifest' % path.name
    count = 0
    for item in meta.get('items') or []:
        recorded = manifest.get(item)
        if not isinstance(recorded, dict) or not (path / item).exists() and not (path / item).is_symlink():
            return False, 'backup %s is missing %s' % (path.name, item)
        actual = _manifest_of(path / item)
        if actual != recorded:
            gone = sorted(set(recorded) - set(actual))[:3]
            odd = sorted(k for k in actual if k in recorded and actual[k] != recorded[k])[:3]
            return False, 'backup %s does not match its manifest in %s (%s)' % (
                path.name, item, ', '.join(gone + odd) or 'extra files')
        count += len(actual)
    if count != int(meta.get('files') or -1):
        return False, 'backup %s file count %d != %s' % (path.name, count, meta.get('files'))
    return True, ''


def backup_lumen_tree():
    """Copy (cp -a semantics) everything the installer replaces or deletes and
    verify the copy against a sha256 manifest; returns the backup directory."""
    dest = lumen_backup_root() / time.strftime('%Y%m%d-%H%M%S')
    n = 0
    while dest.exists():
        n += 1
        dest = lumen_backup_root() / ('%s-%d' % (time.strftime('%Y%m%d-%H%M%S'), n))
    dest.mkdir(parents=True)
    items, absent, manifest, total, files = [], [], {}, 0, 0
    try:
        for name, source, ignore in lumen_backup_items():
            if not (source.exists() or source.is_symlink()):
                absent.append(name)
                continue
            if source.is_dir() and not source.is_symlink():
                shutil.copytree(source, dest / name, symlinks=True,
                                ignore=(shutil.ignore_patterns(*ignore) if ignore else None))
            else:
                shutil.copy2(source, dest / name, follow_symlinks=False)
            expected = _manifest_of(source, ignore)
            copied = _manifest_of(dest / name)
            if expected != copied:
                raise OSError('copy of %s does not match the source' % source)
            items.append(name)
            manifest[name] = copied
            files += len(copied)
            total += _tree_size(dest / name)
        write_json_file(dest / 'backup.json', {
            'at': int(time.time()), 'items': items, 'absent': absent, 'versions': read_lumen_versions(),
            'manifest': manifest, 'files': files, 'bytes': total,
            'sources': {name: str(path) for name, path, _i in lumen_backup_items()}})
    except OSError:
        shutil.rmtree(dest, ignore_errors=True)
        raise
    return dest


def prune_lumen_backups(keep=LUMEN_KEEP_BACKUPS):
    """After a successful update: keep the newest `keep`; failed-update backups stay."""
    good = [path for path in lumen_backups() if not (read_json_file(path / 'backup.json') or {}).get('failed')]
    for stale in good[keep:]:
        shutil.rmtree(stale, ignore_errors=True)


def mark_lumen_backup_failed(path):
    meta = read_json_file(path / 'backup.json') or {}
    meta['failed'] = True
    write_json_file(path / 'backup.json', meta)


def _swap_in(source, target, keep=()):
    """Stage a copy next to the target, then rename into place."""
    staged = target.with_name('.%s.luna-restore' % target.name)
    trash = target.with_name('.%s.luna-replaced' % target.name)
    for leftover in (staged, trash):
        if leftover.is_dir() and not leftover.is_symlink():
            shutil.rmtree(leftover, ignore_errors=True)
        elif leftover.exists() or leftover.is_symlink():
            leftover.unlink()
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir() and not source.is_symlink():
        shutil.copytree(source, staged, symlinks=True)
    else:
        shutil.copy2(source, staged, follow_symlinks=False)
    for name in keep:  # what the installer left in place stays in place
        if (target / name).exists() or (target / name).is_symlink():
            if (staged / name).exists() or (staged / name).is_symlink():
                shutil.rmtree(staged / name, ignore_errors=True)
            os.replace(target / name, staged / name)
    if target.exists() or target.is_symlink():
        os.replace(target, trash)
    os.replace(staged, target)
    if trash.is_dir() and not trash.is_symlink():
        shutil.rmtree(trash, ignore_errors=True)
    elif trash.exists() or trash.is_symlink():
        trash.unlink()


def restore_lumen_backup(name='', skip=(), record=True):
    """Put a backup back (Steam and Lumen stopped). Returns (ok, message).

    `skip` names backup items to leave alone (an automatic rollback leaves the
    user's SLSsteam settings alone); `record=False` leaves the result file to
    the caller.
    """
    backups = lumen_backups()
    chosen = next((path for path in backups if not name or path.name == name), None)
    if chosen is None:
        return False, 'no Lumen backup found in %s' % lumen_backup_root()
    if steam_running() or lumen_running():
        return False, 'close Steam and stop the Lumen sidecar first'
    ok, problem = verify_lumen_backup(chosen)
    if not ok:
        return False, 'refusing to restore, nothing was changed: %s' % problem
    meta = read_json_file(chosen / 'backup.json') or {}
    wanted = {item_name: (path, ignore) for item_name, path, ignore in lumen_backup_items()}
    for item in meta.get('items') or []:
        if item in wanted and item not in skip:
            _swap_in(chosen / item, wanted[item][0], wanted[item][1])
    for item in meta.get('absent') or []:  # it did not exist before the update
        if item in wanted and item not in skip and (wanted[item][0].exists() or wanted[item][0].is_symlink()):
            if wanted[item][0].is_dir() and not wanted[item][0].is_symlink():
                shutil.rmtree(wanted[item][0], ignore_errors=True)
            else:
                wanted[item][0].unlink()
    if record:
        record_lumen_result('restored', 'restored the Lumen backup %s' % chosen.name, backup=chosen.name,
                            changed={}, changedText='none')
    return True, 'restored the Lumen backup %s (%d files verified; Lumen, luatools, SLSsteam and its settings as they were)' % (
        chosen.name, int(meta.get('files') or 0))


def check_user_config_after_update(backup):
    """Settings the user had vs now. Missing files come back verbatim; files the
    installer changed are left alone and listed (config vs program files cannot
    be told apart without reading upstream code, so nothing is guessed)."""
    restored, changed = [], []
    meta = read_json_file(backup / 'backup.json') or {}
    base = home() / '.config/SLSsteam'
    for rel, digest in sorted((meta.get('manifest') or {}).get('slsteam-config', {}).items()):
        current = base / rel
        if not (current.exists() or current.is_symlink()):
            current.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(backup / 'slsteam-config' / rel, current, follow_symlinks=False)
            restored.append(str(current))
        elif _entry_digest(current) != digest:
            changed.append(str(current))
    return restored, changed


def lumen_plan_lines():
    rows = lumen_backup_plan()
    lines = ['lumen: would back up (verified, sha256 manifest) to %s:' % lumen_backup_root()]
    for name, path, size, present in rows:
        note = ' (installer keeps %s)' % ', '.join(SLSTEAM_KEPT) if name == 'slsteam' else ''
        big = '  ** over 500 MB **' if size > LUMEN_BIG_BYTES else ''
        lines.append('lumen:   %-15s %-8s %s%s%s' % (name, _human(size) if present else 'absent', path, note, big))
    lines.append('lumen: total %s; the newest %d backups are kept (a failed update\'s is never pruned)'
                 % (_human(sum(row[2] for row in rows)), LUMEN_KEEP_BACKUPS))
    return lines


def lumen_update_blockers():
    """Why a Lumen update cannot run now (empty list: it can)."""
    if active_host() != 'lumen':
        return ['luna-host is the active host; Lumen is not in use. Switch back with: luna host rollback']
    if base_state() != 'ready':
        return ['the Moon/Lumen base is not complete; run: luna bootstrap']
    # A lingering sidecar is stopped by the update itself (Steam is closed then).
    return [reason for reason in refuse_reasons(for_install=True)
            if 'Lumen sidecar is still running' not in reason]


class _UpdateFailed(Exception):
    def __init__(self, message, code=1):
        super().__init__(message)
        self.code = code


PHASE_TEXT = {'backup': 'backing up Lumen before the update (do not shut down)',
              'installer': 'running the base installer (do not shut down)',
              'reattach': 're-attaching Luna to the new Lumen (do not shut down)',
              'rollback': 'rolling back to the pre-update Lumen (do not shut down)'}


def _journal(ctx, phase):
    """Write the `running` record: pid, phase and (once it exists) the backup."""
    ctx['phase'] = phase
    fields = {key: ctx[key] for key in ('backup', 'before', 'beforeFp', 'lumenWasActive', 'startedAt', 'installer')
              if ctx.get(key) is not None}
    return record_lumen_result('running', PHASE_TEXT[phase], pid=os.getpid(),
                               pidStart=_proc_start(os.getpid()), phase=phase, **fields)


def _heal_quietly():
    try:
        heal_steam_lock()
    except Exception as error:  # noqa: BLE001 - the lock repair must never mask the real failure
        print('heal      : failed: %s' % error, file=sys.stderr)


def _queue_lumen_job(action, ctx, reason=''):
    job = {'action': action, 'at': int(time.time()), 'id': '%d-%d' % (os.getpid(), time.time_ns()),
           'reason': reason, 'ctx': {key: ctx.get(key) for key in
                                     ('backup', 'before', 'beforeFp', 'lumenWasActive', 'startedAt', 'installer')}}
    write_json_file(lumen_pending_path(), job)
    return start_apply_service()


def _restart_lumen(ctx, restart=True):
    if restart and ctx.get('lumenWasActive'):
        systemctl_user('start', LUMEN_UNIT)


def _rollback(ctx, reason):
    """Put the verified pre-update backup back. Returns (rolled_back, message)."""
    name = ctx['backup']
    path = lumen_backup_path(name)
    _journal(ctx, 'rollback')
    _heal_quietly()
    if steam_running() or not stop_lumen_sidecar():
        mark_lumen_backup_failed(path)
        _queue_lumen_job('rollback', ctx, reason)
        return False, ('Steam or the Lumen sidecar is running, so the rollback could not run now; '
                       'it is queued and starts when Steam is closed (or run: luna lumen restore --backup %s)' % name)
    try:
        ok, message = restore_lumen_backup(name, skip=('slsteam-config',), record=False)
    except Exception as error:  # noqa: BLE001
        ok, message = False, 'rollback failed: %s' % error
    _heal_quietly()
    if ok:
        meta = read_json_file(path / 'backup.json') or {}
        meta['rolledBack'] = True
        write_json_file(path / 'backup.json', meta)
    else:
        mark_lumen_backup_failed(path)
    return ok, message


def _changed_fields(ctx, rolled=False):
    changed = {} if rolled else lumen_changed(ctx.get('beforeFp') or {}, read_lumen_fingerprints())
    return {'changed': changed, 'changedText': '; '.join('%s %s' % item for item in sorted(changed.items())) or 'none'}


def _fail(ctx, reason, code):
    """Every failure after the backup ends here, in a known state."""
    print('FAILED: %s' % reason, file=sys.stderr)
    if not ctx.get('installerStarted'):
        shutil.rmtree(lumen_backup_path(ctx['backup']), ignore_errors=True)
        _restart_lumen(ctx)
        record_lumen_result('failed', '%s (nothing was changed)' % reason, before=ctx.get('before'),
                            changed={}, changedText='none')
        _notify('Lumen update failed', '%s (nothing was changed)' % reason)
        return code
    rolled, message = _rollback(ctx, reason)
    if rolled:
        _restart_lumen(ctx)
    record_lumen_result('failed', '%s; %s' % (reason, 'rolled back to the pre-update Lumen' if rolled else message),
                        backup=ctx['backup'], before=ctx.get('before'), rolledBack=rolled,
                        rollbackPending=bool(read_lumen_pending().get('action') == 'rollback'),
                        reason=reason, **_changed_fields(ctx, rolled))
    _notify('Lumen update failed', '%s. %s' % (reason, 'Rolled back to the previous Lumen.' if rolled else message))
    print('lumen     : %s' % ('rolled back' if rolled else message), file=sys.stderr)
    return code


def _complete_update(ctx, restart_lumen=True):
    """The re-attach half; raises _UpdateFailed. Returns an exit code."""
    backup = lumen_backup_path(ctx['backup'])
    _journal(ctx, 'reattach')
    problems = lumen_tooling_problems()
    if problems:
        raise _UpdateFailed('tooling incomplete after the installer: %s' % '; '.join(problems), EXIT_PATCH)
    if steam_running():  # started again meanwhile: finish when it is closed once more
        _heal_quietly()
        applier = _queue_lumen_job('reattach', ctx, 'Steam was started during the update')
        record_lumen_result('queued', 'Lumen was updated; close Steam once more to finish re-attaching Luna',
                            backup=ctx['backup'], before=ctx.get('before'), applier=applier)
        print('lumen     : Steam is running; re-attach is queued for when it closes', file=sys.stderr)
        return EXIT_OK
    check = patcher('check')
    if check.returncode != 0:
        raise _UpdateFailed('Lumen upstream changed; Luna cannot patch the new version yet (%s)'
                            % (check.stderr.strip() or check.stdout.strip() or 'no detail'), EXIT_PATCH)
    # no_steam_machine: the base installer never touches Luna's SteamOS shortcuts (H6).
    code = cmd_install(argparse.Namespace(only=[], force=True, dry_run=False, keep_steam_client=True,
                                          no_steam_machine=True, migrate_steam_machine=False))
    after = patcher('check')
    if code != 0 or 'bridge installed' not in after.stdout:
        raise _UpdateFailed('Lumen was updated but Luna could not re-attach (install exit %d)' % code,
                            code or EXIT_PATCH)
    warnings = []
    if active_host() == 'lumen' and wrapper_patched():
        unpatch_wrapper()
        print('base      : wrapper unpatched for the lumen host')
    restored, changed = check_user_config_after_update(backup)
    for path in restored:
        print('config    : restored (verbatim from the backup): %s' % path)
    for path in changed:
        print('config    : changed by the installer, left as is: %s' % path)
    if changed:
        warnings.append('SLSsteam settings differ from before; compare %s with %s'
                        % (backup / 'slsteam-config', home() / '.config/SLSsteam'))
        print('WARNING: %s' % warnings[-1], file=sys.stderr)
    if 'lumen self-update blocked' not in after.stdout:
        warnings.append("Lumen's own updater is NOT blocked after this update")
        print('WARNING: %s' % warnings[-1], file=sys.stderr)
    _restart_lumen(ctx, restart_lumen)
    prune_lumen_backups()
    before = ctx.get('before') or {}
    record_lumen_result('applied', 'Lumen updated and Luna re-attached', backup=ctx['backup'],
                        before=before, after=read_lumen_versions(), warnings=warnings,
                        configRestored=restored, configChanged=changed,
                        preserved=ctx.get('preserved') or [], **_changed_fields(ctx))
    with contextlib.suppress(OSError):  # supersede any stale queued job and its text
        lumen_pending_path().unlink()
    _notify('Lumen update finished', 'Lumen %s -> %s; Luna re-attached. Restart Steam to load it.'
            % (before.get('lumen') or '?', read_lumen_versions().get('lumen') or '?'))
    print('lumen     : updated and Luna re-attached; restart Steam to load it')
    return EXIT_OK


def lumen_installer_choice(installer_latest):
    """(ref, sha256, label, error): the pinned verified installer unless `main` was asked for."""
    try:
        if installer_latest:
            digest = verify_base_installer('main', None)
            return 'main', digest, 'upstream main (sha256 %s, NOT pinned)' % digest, ''
        verify_base_installer(UPSTREAM_INSTALLER_COMMIT, UPSTREAM_INSTALLER_SHA256)
    except (OSError, ValueError) as error:
        return '', '', '', 'could not verify the base installer: %s' % error
    return (UPSTREAM_INSTALLER_COMMIT, UPSTREAM_INSTALLER_SHA256,
            'pinned %s (sha256 %s, verified)' % (UPSTREAM_INSTALLER_COMMIT[:12], UPSTREAM_INSTALLER_SHA256[:12]), '')


def _refuse_update(message):
    record_lumen_result('failed', message)
    _notify('Lumen update refused', message)
    print('REFUSING base update: %s' % message, file=sys.stderr)
    print('Nothing was changed.', file=sys.stderr)
    return EXIT_REFUSED


def apply_base_update(restart_lumen=True, installer_latest=False, reinstall=False):
    """Preflight, backup, installer, re-attach. Steam must be closed.

    Every failure after the installer started ends in a known state: the
    pre-update backup is put back automatically, the Steam lock is re-applied
    in a `finally`, and the result file says failed + rolledBack. Never ends
    with Lumen replaced and Luna detached unless a queued job says so.
    """
    early = lumen_tooling_problems() + runtime_problems(full=True)  # local and cheap: refuse before any network call
    if early:
        return _refuse_update('tooling incomplete: %s' % '; '.join(early))
    if not reinstall:
        verdict = lumen_upstream_state()
        compared = [{'component': row['component'], 'channel': row['channel'], 'status': row['status'],
                     'installed': row['installed'], 'available': row['available'], 'note': row['note']}
                    for row in verdict['components']]
        lines = [describe_upstream_row(row) for row in verdict['components']]
        if verdict['overall'] == 'current':
            message = 'already current on your channels; nothing was changed'
            record_lumen_result('current', message, compared=compared, changed={}, changedText='none')
            with contextlib.suppress(OSError):
                lumen_pending_path().unlink()
            print('lumen     : %s' % message)
            print('\n'.join('lumen     : ' + line for line in lines))
            return EXIT_OK
        if verdict['overall'] == 'unknown':
            why = '; '.join('%s: %s' % (row['component'], row['note'] or 'unknown')
                            for row in verdict['components'] if row['status'] == 'unknown')
            message = 'could not tell whether upstream has anything newer (%s); nothing was changed. ' \
                      'Try again later, or force it with --reinstall' % why
            record_lumen_result('unknown', message, compared=compared, changed={}, changedText='none')
            print('lumen     : %s' % message, file=sys.stderr)
            return 1
    if steam_running():
        message = 'Steam is running. Close it completely (Steam -> Exit) before changing Lumen.'
        record_lumen_result('failed', message)
        print('REFUSING base update: %s' % message, file=sys.stderr)
        return EXIT_REFUSED
    problems = lumen_preflight()
    if problems:
        return _refuse_update('tooling incomplete: %s' % '; '.join(problems))
    ref, sha, label, error = lumen_installer_choice(installer_latest)
    if error:
        return _refuse_update(error)
    millennium = lumen_millennium_choice()
    if millennium_present() and not millennium:
        return _refuse_update(
            'Millennium is installed and the base installer removes it by default; no choice is recorded. '
            'Re-run with --keep-millennium or --remove-millennium')
    channels = lumen_channels()
    extra = lumen_installer_extra(channels, millennium)
    channels_file = lumen_root() / 'update-channels.json'
    try:
        channels_bytes = channels_file.read_bytes()
    except OSError:
        channels_bytes = None
    ctx = {'before': read_lumen_versions(), 'beforeFp': read_lumen_fingerprints(), 'startedAt': int(time.time()), 'installer': label,
           'lumenWasActive': systemctl_user('is-active', LUMEN_UNIT).stdout.strip() == 'active',
           'installerStarted': False, 'preserved': []}
    _journal(ctx, 'backup')
    _notify('Lumen update started', 'Backing up, then updating Lumen. Do not shut down or log out.')
    if ctx['lumenWasActive']:
        systemctl_user('stop', LUMEN_UNIT)
    refuse_reasons(for_install=True, stop_sidecar=True)
    try:
        backup = backup_lumen_tree()
    except OSError as error_:
        _restart_lumen(ctx)
        record_lumen_result('failed', 'could not back up Lumen first: %s' % error_)
        print('REFUSING base update: could not back up Lumen first: %s' % error_, file=sys.stderr)
        return 1
    ctx['backup'] = backup.name
    print('base      : backed up Lumen, luatools, SLSsteam, its settings and steam.cfg to %s (verified)' % backup)
    try:
        _journal(ctx, 'installer')
        if steam_running():  # H2: nothing has changed yet, so abort cleanly
            shutil.rmtree(backup, ignore_errors=True)
            _restart_lumen(ctx)
            record_lumen_result('queued', 'Steam was started before the installer began; nothing was changed. '
                                          'It runs again when Steam is closed')
            print('REFUSING base update: Steam started; nothing was changed.', file=sys.stderr)
            return EXIT_REFUSED
        if read_steam_pin():
            ensure_steam_cfg_block()
        ctx['installerStarted'] = True
        code = run_base_installer(ref, sha, extra)
        if code != 0:
            raise _UpdateFailed('the base installer exited with %d' % code, code)
        if channels_bytes is not None:
            try:
                if channels_file.read_bytes() != channels_bytes:
                    channels_file.write_bytes(channels_bytes)
                    ctx['preserved'].append('update-channels.json')
                    print('base      : restored update-channels.json (the installer rewrote it)')
            except OSError:
                channels_file.write_bytes(channels_bytes)
                ctx['preserved'].append('update-channels.json')
        return _complete_update(ctx, restart_lumen)
    except _UpdateFailed as failure:
        return _fail(ctx, str(failure), failure.code)
    except InstallerTimeout as failure:
        return _fail(ctx, str(failure), 1)
    except BaseException as failure:  # SystemExit from SIGTERM included: roll back, then propagate
        code = _fail(ctx, 'base update %s: %s' % ('interrupted' if not isinstance(failure, Exception)
                                                  else 'failed', failure or type(failure).__name__), 1)
        if not isinstance(failure, Exception):
            raise
        return code
    finally:
        if ctx.get('installerStarted'):
            _heal_quietly()


def lumen_running_record():
    record = read_json_file(lumen_result_path())
    return record if isinstance(record, dict) and record.get('state') == 'running' else {}


def recover_interrupted_lumen():
    """Recover a `running` record whose writer died. Returns (action, message) or None.

    action: 'busy' (a live luna still owns it, left alone), 'recovered' or 'failed'.
    """
    record = lumen_running_record()
    if not record:
        return None
    pid = int(record.get('pid') or 0)
    if pid == os.getpid():
        return None
    if pid_is_live_luna(pid, record.get('pidStart') or 0):
        return 'busy', 'another luna process (pid %d) is still updating Lumen' % pid
    ctx = {key: record.get(key) for key in ('backup', 'before', 'beforeFp', 'lumenWasActive', 'startedAt', 'installer')}
    ctx['installerStarted'] = bool(ctx.get('backup'))
    phase = record.get('phase') or ''
    print('lumen     : recovering an interrupted update (phase %s, pid %s is gone)' % (phase or '?', pid))
    if not ctx['backup'] or phase == 'backup':
        for stray in (lumen_backup_root().iterdir() if lumen_backup_root().is_dir() else []):
            if stray.name.startswith('.migrating-') or not (stray / 'backup.json').is_file() and stray.is_dir():
                shutil.rmtree(stray, ignore_errors=True)
        _restart_lumen(ctx)
        record_lumen_result('failed', 'the update was interrupted before the installer started; nothing was changed')
        return 'recovered', 'interrupted before the installer; nothing was changed'
    if phase == 'reattach':
        try:
            code = _complete_update(ctx)
            return ('recovered' if code == 0 else 'failed'), 'finished the interrupted re-attach'
        except _UpdateFailed as failure:
            return 'failed', 'exit %d' % _fail(ctx, str(failure), failure.code)
        finally:
            _heal_quietly()
    rolled, message = _rollback(ctx, 'the update was interrupted during phase %s' % (phase or '?'))
    if rolled:
        _restart_lumen(ctx)
    record_lumen_result('failed', 'the update was interrupted; %s' % (
        'rolled back to the pre-update Lumen' if rolled else message),
        backup=ctx['backup'], before=ctx.get('before'), rolledBack=rolled,
        rollbackPending=bool(read_lumen_pending().get('action') == 'rollback'))
    return ('recovered' if rolled else 'failed'), message


def lumen_status(check=False):
    """Everything the CLI and the Updates page show about the Lumen base."""
    host = active_host()
    tooling = lumen_tooling_problems()
    state = {'host': host, 'baseState': base_state(), 'versions': read_lumen_versions(),
             'pending': bool(read_lumen_pending()), 'result': read_json_file(lumen_result_path()) or {},
             'backups': lumen_backup_info(),
             'installerPinned': UPSTREAM_INSTALLER_COMMIT[:12], 'steamRunning': bool(
                 steam_running()),
             'tooling': {'ok': not tooling, 'problems': tooling},
             'toolingIncomplete': ('tooling incomplete: ' + '; '.join(tooling)) if tooling else '',
             'applyService': APPLY_UNIT if systemd_available() else 'timer-only'}
    result = (patcher('check') if not tooling and host == 'lumen' and boot_file().is_file()
              else None)
    out = result.stdout if result is not None else ''
    state['bridge'] = ('tooling incomplete' if tooling else
                       'installed' if 'bridge installed' in out else
                       'not installed' if out else 'unknown')
    state['selfUpdateBlocked'] = (not tooling) and 'lumen self-update blocked' in out
    state['patchable'] = bool(result is not None and result.returncode == 0)
    state['channels'] = lumen_channels_effective()
    state['channelsFile'] = str(lumen_channels_path())
    state['millennium'] = {'choice': lumen_millennium_choice(), 'present': [str(p) for p in millennium_present()]}
    state['installer'] = {'mode': 'pinned', 'commit': UPSTREAM_INSTALLER_COMMIT,
                          'sha256': UPSTREAM_INSTALLER_SHA256}
    pending = read_lumen_pending()
    state['job'] = {'action': pending.get('action'), 'attempts': int(pending.get('attempts') or 0),
                    'lastError': pending.get('lastError') or ''} if pending else None
    record = lumen_running_record()
    state['interrupted'] = bool(record and not pid_is_live_luna(record.get('pid'), record.get('pidStart') or 0))
    if check:
        state['upstream'] = lumen_upstream_state()
        try:
            with tempfile.TemporaryDirectory(prefix='luna-base-') as work:
                digest = fetch_base_installer('main', None, Path(work) / 'install.sh')
            state['installerMainSha'] = digest[:12]
            state['upstreamMoved'] = digest != UPSTREAM_INSTALLER_SHA256
        except (OSError, ValueError) as error:
            state['upstreamError'] = str(error)
    return state


def cmd_lumen_channels(options):
    """`luna lumen channels [set <stable|beta> [--only NAME]]`."""
    action = (getattr(options, 'channels_action', '') or '').strip().lower()
    names = [name for name in (getattr(options, 'only', None) or [])]
    if action in ('', 'show', 'status'):
        channels = lumen_channels_effective()
        for key, _flag in LUMEN_CHANNEL_FLAGS:
            print('%-12s %s' % (key, channels[key]))
        print('file      : %s%s' % (lumen_channels_path(),
                                   '' if lumen_channels_path().is_file() else ' (not written; stable applies)'))
        print('change    : luna lumen channels set <stable|beta> [--only NAME]')
        return EXIT_OK
    if action != 'set':
        print('unknown channels action: %s (choose from show, set)' % action, file=sys.stderr)
        return 2
    value = (getattr(options, 'channels_value', '') or '').strip().lower()
    if value not in LUMEN_CHANNEL_VALUES:
        print('REFUSING: channel must be stable or beta, not %r. Nothing was changed.' % (value or ''), file=sys.stderr)
        return EXIT_REFUSED
    if getattr(options, 'dry_run', False):
        print('channels  : would set %s for %s' % (value, ', '.join(names or [key for key, _flag in LUMEN_CHANNEL_FLAGS])))
        print('file      : %s' % lumen_channels_path())
        print('\nDRY RUN: nothing was written.')
        return EXIT_OK
    try:
        channels = set_lumen_channels(value, names)
    except ValueError as error:
        print('REFUSING: %s. Nothing was changed.' % error, file=sys.stderr)
        return EXIT_REFUSED
    except OSError as error:
        print('REFUSING: could not write %s (%s). Nothing was changed.'
              % (lumen_channels_path(), error), file=sys.stderr)
        return 1
    changed = names or [key for key, _flag in LUMEN_CHANNEL_FLAGS]
    message = 'channels set to %s for %s' % (value, ', '.join(changed))
    record_lumen_channels(message, channels=channels, changed={name: value for name in changed})
    for key, _flag in LUMEN_CHANNEL_FLAGS:
        print('%-12s %s' % (key, channels[key]))
    print('channels  : %s' % message)
    print('file      : %s (written atomically)' % lumen_channels_path())
    if value == 'beta':
        print('note      : a component with no beta build published upstream stays on stable; '
              '`luna lumen status --check` says so per component')
    return EXIT_OK


def cmd_lumen(options):
    action = getattr(options, 'lumen_action', 'status')
    if action == 'channels':
        return cmd_lumen_channels(options)
    if action in ('update', 'restore') and active_host() == 'luna' and base_state() == 'none':
        print('lumen     : not installed (the active host is luna, standalone); '
              'opt in with: luna install --lumen')
        return EXIT_OK
    if action == 'status':
        state = lumen_status(check=getattr(options, 'check', False))
        if getattr(options, 'json', False):
            print(json.dumps(state, sort_keys=True))
            return EXIT_OK
        print('host      : %s%s' % (host_label(state['host']), '' if state['host'] == 'lumen'
                                    else ' (Lumen updates only apply while Lumen is the active host)'))
        base = state['baseState']
        print('base      : %s' % ('not installed (standalone host)' if (
            state['host'] == 'luna' and base == 'none') else base))
        print('versions  : %s' % (', '.join('%s %s' % item for item in sorted(state['versions'].items())) or 'unknown'))
        print('channels  : %s' % ', '.join('%s=%s' % (key, value)
                                          for key, value in state['channels'].items()))
        if state['toolingIncomplete']:
            print(state['toolingIncomplete'])
            print('bridge    : unknown (tooling incomplete)')
            print('self-update block: unknown (tooling incomplete)')
        else:
            print('bridge    : %s' % state['bridge'])
            print('self-update block: %s' % ('in place' if state['selfUpdateBlocked'] else 'NOT in place'))
        print('apply     : %s' % ('prompt (luna-apply.service waits for Steam to exit)'
                                  if state['applyService'] == APPLY_UNIT
                                  else 'timer-only (systemd user manager unavailable)'))
        if 'upstream' in state:
            print('upstream  : %s' % state['upstream']['overall'])
            for row in state['upstream']['components']:
                print('  ' + describe_upstream_row(row))
        print('installer : pinned %s (default; main only with --installer-latest --yes)%s' % (state['installerPinned'], '' if 'upstreamMoved' not in state else
              ('; upstream main has moved (sha %s)' % state['installerMainSha'] if state['upstreamMoved']
               else '; upstream main is the same')))
        pending = read_lumen_pending()
        print('update    : %s' % ('queued, applies when Steam is closed%s' % (
            ' (starts on its own when Steam exits)' if (state['applyService'] == APPLY_UNIT)
            else ' (30-minute timer; systemd unavailable)') if pending
            else (state['result'].get('state', 'none') + (': ' + state['result']['message']
                  if state['result'].get('message') else ''))))
        print('backups   : %s' % ('; '.join('%s (%s, %s%s)' % (
            item['name'], time.strftime('%Y-%m-%d %H:%M', time.localtime(item['at'])), _human(item['bytes']),
            ', failed update' if item['failed'] else '') for item in state['backups']) or 'none'))
        return EXIT_OK
    if action == 'cancel':
        if not read_lumen_pending():
            print('lumen     : nothing is queued')
            return EXIT_OK
        stop_apply_service()
        with contextlib.suppress(OSError):
            lumen_pending_path().unlink()
        record_lumen_result('cancelled', 'the queued Lumen update was cancelled')
        print('lumen     : queued update cancelled')
        return EXIT_OK
    if action in ('restore', 'update') and not getattr(options, 'dry_run', False):
        for name in migrate_lumen_backups():
            print('lumen     : moved backup %s to %s' % (name, lumen_backup_root()))
        recovery = recover_interrupted_lumen()
        if recovery and recovery[0] == 'busy':
            print('REFUSING: %s' % recovery[1], file=sys.stderr)
            return EXIT_BUSY
        if recovery:
            print('lumen     : %s; check `luna lumen status`, then run the command again' % recovery[1])
            return EXIT_OK if recovery[0] == 'recovered' else 1
    if action == 'restore':
        name = getattr(options, 'backup', '') or ''
        chosen = next((b for b in lumen_backup_info() if not name or b['name'] == name), None)
        if chosen is None:
            print('lumen     : no Lumen backup found in %s' % lumen_backup_root(), file=sys.stderr)
            return 1
        with_settings = getattr(options, 'with_settings', False)
        overwrites = [item for item in chosen['items'] if with_settings or item != 'slsteam-config']
        print('lumen     : restore backup %s (%s, %s) -> overwrites: %s%s' % (
            chosen['name'], time.strftime('%Y-%m-%d %H:%M', time.localtime(chosen['at'])), _human(chosen['bytes']),
            ', '.join(overwrites), '' if with_settings else '; ~/.config/SLSsteam is left alone (--with-settings)'))
        if not getattr(options, 'yes', False):
            answer = ''
            if sys.stdin.isatty():
                answer = input('Type the backup name %s to restore it: ' % chosen['name']).strip()
            if answer != chosen['name']:
                print('REFUSING: restoring replaces the current Lumen; re-run with --yes (or confirm '
                      'interactively). Nothing was changed.', file=sys.stderr)
                return EXIT_REFUSED
        ok, message = restore_lumen_backup(chosen['name'], skip=() if with_settings else ('slsteam-config',))
        print('lumen     : %s' % message, file=sys.stdout if ok else sys.stderr)
        return EXIT_OK if ok else 1
    if action != 'update':
        print('unknown lumen action: %s' % action, file=sys.stderr)
        return 2
    dry = getattr(options, 'dry_run', False)
    latest = getattr(options, 'installer_latest', False)
    if getattr(options, 'terminal', False) and not dry:
        # The child (same command without --terminal) is the one that takes the
        # writer lock, so this parent only plans, confirms and closes Steam.
        return cmd_lumen_update_terminal(options)
    if latest and (getattr(options, 'queue', False) or not (getattr(options, 'yes', False) or dry)):
        print('REFUSING: --installer-latest runs the unpinned upstream main; it needs --yes, '
              'a terminal run and cannot be queued.', file=sys.stderr)
        return EXIT_REFUSED
    if getattr(options, 'keep_millennium', False) and getattr(options, 'remove_millennium', False):
        print('REFUSING: choose one of --keep-millennium and --remove-millennium', file=sys.stderr)
        return EXIT_REFUSED
    if not dry:
        for flag, choice in (('keep_millennium', 'keep'), ('remove_millennium', 'remove')):
            if getattr(options, flag, False):
                set_lumen_millennium_choice(choice)
    blockers = lumen_update_blockers()
    steam_only = bool(blockers) and all('Steam is running' in reason for reason in blockers)
    if dry:
        print('\n'.join(lumen_plan_lines()))
        if getattr(options, 'installer_latest', False):
            print('lumen: would run the upstream MAIN installer (NOT pinned; its sha256 is shown first), '
                  'then re-attach Luna: bridge, self-update block, Steam lock')
        else:
            print('lumen: would run the PINNED installer %s (sha256 %s, verified before anything changes), '
                  'then re-attach Luna: bridge, self-update block, Steam lock'
                  % (UPSTREAM_INSTALLER_COMMIT[:12], UPSTREAM_INSTALLER_SHA256[:12]))
        print('lumen: the installer fetches the newest component releases on your channels: %s'
              % ', '.join('%s=%s' % item for item in lumen_channels_effective().items()))
        print('lumen: update-channels.json is passed on and restored if the installer rewrites it')
        print('lumen: %s; the base installer runs with --nolaunch' % steam_plan_line())
        print('lumen: afterwards: settings the installer removed come back verbatim, changed ones are listed, '
              'the wrapper is unpatched for the lumen host')
        print('lumen: on any failure after the installer starts the backup is put back automatically '
              '(result: failed, rolledBack); the Steam lock is re-applied either way')
        for reason in blockers:
            print('lumen: would refuse now: %s' % reason)
        if any('Steam is running' in reason for reason in blockers):
            print('note: Steam is running; a real run would stop here until Steam is closed')
        print('\nDRY RUN: nothing was written.')
        return EXIT_OK
    if getattr(options, 'queue', False) and (not blockers or steam_only):
        problems = lumen_tooling_problems()
        if problems:
            message = 'tooling incomplete: %s' % '; '.join(problems)
            record_lumen_result('failed', message)
            print('REFUSING to queue the Lumen update: %s' % message, file=sys.stderr)
            return EXIT_REFUSED
        write_json_file(lumen_pending_path(), {'action': 'update', 'at': int(time.time())})
        applier = start_apply_service()
        message = ('will update when Steam is closed; it starts on its own once Steam exits'
                   if applier == 'service' else
                   'will update when Steam is closed (next 30-minute timer check; systemd unavailable)')
        record_lumen_result('queued', message, applier=applier)
        print('lumen     : update queued; %s' % message)
        return EXIT_OK
    if blockers:
        print('REFUSING Lumen update:', file=sys.stderr)
        for reason in blockers:
            print('  - %s' % reason, file=sys.stderr)
        if steam_only:
            print('Close Steam, or queue it with: luna lumen update --queue', file=sys.stderr)
        return EXIT_REFUSED
    if not getattr(options, 'yes', False):
        print('REFUSING: this moves the Moon/Lumen base to the upstream main installer. '
              'Re-run with --yes (see --dry-run for the plan).', file=sys.stderr)
        return EXIT_REFUSED
    return apply_base_update(installer_latest=latest, reinstall=getattr(options, 'reinstall', False))


def run_lumen_recovery_job(job):
    """A queued re-attach or rollback left by a run that could not finish with Steam open."""
    ctx = dict(job.get('ctx') or {})
    ctx['installerStarted'] = True
    if not ctx.get('backup'):
        return 1
    if job.get('action') == 'rollback':
        rolled, message = _rollback(ctx, job.get('reason') or 'queued rollback')
        if rolled:
            _restart_lumen(ctx)
        record_lumen_result('failed', 'queued rollback: %s' % ('rolled back to the pre-update Lumen' if rolled else message),
                            backup=ctx['backup'], rolledBack=rolled, reason=job.get('reason') or '')
        return 0 if rolled else 1
    try:
        return _complete_update(ctx)
    except _UpdateFailed as failure:
        return _fail(ctx, str(failure), failure.code)
    finally:
        _heal_quietly()


_LUNA_REPLACED = False  # set once this run installed a new Luna release


def run_update_in_new_luna():
    """The Luna release installed earlier in this run replaced luna.py and the runtime tree;
    this process still runs the OLD code. Run the Lumen step with the NEW installed copy."""
    problems = installed_copy_problems()
    if problems:
        # Re-exec'ing a copy whose runtime tree is missing or from another build sends
        # the next step looking for pins that are not there. Repair, never delegate.
        print('REFUSING to run the Lumen step from the installed copy:', file=sys.stderr)
        for problem in problems:
            print('  - %s' % problem, file=sys.stderr)
        print('Fix that by installing the current tooling first: luna install --force '
              '(or python3 <checkout>/scripts/luna.py install --force)', file=sys.stderr)
        return EXIT_REFUSED
    command = [sys.executable, str(luna_tools_dir() / 'luna.py'), 'lumen', 'update', '--yes']
    return subprocess.run(command, env=dict(os.environ, **{LOCK_HELD_ENV: '1'})).returncode


def apply_queued_lumen(record_extra):
    """Timer side of the queued update. Returns True when it ran (any outcome)."""
    pending = read_lumen_pending()
    if not pending:
        return False
    blockers = lumen_update_blockers()
    if pending.get('action') in ('reattach', 'rollback'):
        blockers = ['Steam is running. Close it completely (Steam -> Exit) before changing Lumen.'] \
            if steam_running() else []
    if blockers:
        record_extra['lumenPending'] = True
        record_extra['lumenReason'] = blockers[0]
        record_lumen_result('queued', 'will update when Steam is closed (waiting: %s)' % blockers[0])
        return False
    action = pending.get('action')
    try:
        if action in ('reattach', 'rollback'):
            code = run_lumen_recovery_job(pending)
        else:
            if _LUNA_REPLACED:
                code = run_update_in_new_luna()
            else:
                code = apply_base_update(installer_latest=False)  # a queued job never runs upstream main
    except Exception as error:  # noqa: BLE001 - bounded retries, then the job is dropped and reported
        attempts = int(pending.get('attempts') or 0) + 1
        message = '%s: %s' % (type(error).__name__, error)
        if attempts >= LUMEN_JOB_ATTEMPTS:
            with contextlib.suppress(OSError):
                lumen_pending_path().unlink()
            record_lumen_result('failed', 'the queued Lumen update raised an error %d times and was dropped '
                                          '(last: %s); nothing is queued any more' % (attempts, message),
                                attempts=attempts, lastError=message, dropped=True)
            record_extra['lumen'] = 'dropped'
            return True
        pending['attempts'] = attempts
        pending['lastError'] = message
        write_json_file(lumen_pending_path(), pending)
        record_lumen_result('failed', 'the queued Lumen update raised an error (attempt %d of %d, will retry): %s'
                            % (attempts, LUMEN_JOB_ATTEMPTS, message), attempts=attempts, lastError=message)
        record_extra['lumen'] = 'failed'
        return True
    with contextlib.suppress(OSError):
        if read_lumen_pending() == pending and (read_json_file(lumen_result_path()) or {}).get('state') != 'queued':
            lumen_pending_path().unlink()
    record_extra['lumen'] = 'applied' if code == 0 else 'failed'
    record_extra['lumenExit'] = code
    return True


# --------------------------------------------------------------------------- luna clean

CLEAN_DAY = 86400


def _age_text(seconds):
    days = seconds / CLEAN_DAY
    return '%.1f days' % days if days >= 1 else '%.1f hours' % (seconds / 3600)


def _newest_mtime(path):
    newest = 0.0
    with contextlib.suppress(OSError):
        newest = path.lstat().st_mtime
        if path.is_dir() and not path.is_symlink():
            for current, dirs, files in os.walk(path):
                for name in dirs + files:
                    with contextlib.suppress(OSError):
                        newest = max(newest, (Path(current) / name).lstat().st_mtime)
    return newest


def clean_candidates(now=None):
    """[(path, bytes, age_seconds, removable, reason)] for Luna's own housekeeping.

    Only paths inside Luna's tools and state dirs are ever listed; the writer lock
    decides about interrupted-stage leftovers. Kept items say why they are kept.
    """
    now = time.time() if now is None else now
    tools, state = luna_tools_dir(), luna_state_dir()
    lock_busy = _update_lock_held_by_other()
    rows = []

    def add(path, removable, reason):
        size = _tree_size(path) if (path.exists() or path.is_symlink()) else 0
        rows.append((path, size, max(0.0, now - _newest_mtime(path)), removable, reason))

    if tools.is_dir():
        for path in sorted(tools.glob('steam-client-stage-*')):
            if lock_busy:
                add(path, False, 'kept: a luna command holds the writer lock, it may be using this stage dir')
            elif now - _newest_mtime(path) < CLEAN_DAY:
                add(path, False, 'kept: modified in the last day')
            else:
                add(path, True, 'safe: leftover of an interrupted Steam client install; no luna holds the lock')
        receipt = read_json_file(tools / 'steam-client-installed.json') or {}
        installed_at = int(receipt.get('at') or 0)
        backups = tools / 'steam-client-backups'
        entries = sorted((e for e in backups.iterdir() if e.is_dir() and e.name.isdigit()),
                         key=lambda e: int(e.name)) if backups.is_dir() else []
        for index, path in enumerate(entries):
            made = int(path.name) / 1e9
            if index == len(entries) - 1:
                add(path, False, 'kept: the newest client rollback copy')
            elif made >= installed_at:
                add(path, False, 'kept: made after the currently installed client')
            else:
                add(path, True, 'safe: an older rollback copy, from before the installed client; the newest is kept')
        if (tools / 'steam-client-cache').exists():
            add(tools / 'steam-client-cache', False, 'kept: `luna steam recover` and `lock` restore from this cache')
        for pattern in ('luna.py.bak-*', 'steam-machine.py.bak-*'):
            for path in sorted(tools.glob(pattern)):
                old = now - _newest_mtime(path) >= 14 * CLEAN_DAY
                add(path, old, 'safe: a hand-made copy of tooling older than 14 days' if old
                    else 'kept: younger than 14 days')
        for name in ('undo', 'archive'):
            if (tools / name).is_dir():
                for path in sorted((tools / name).iterdir()):
                    old = now - _newest_mtime(path) >= 30 * CLEAN_DAY
                    add(path, old, 'safe: %s entry older than 30 days' % name if old
                        else 'kept: %s entries are kept for 30 days' % name)
        for name in ('__pycache__', 'runtime.new', 'runtime.old'):
            path = tools / name
            if path.exists():
                if name == '__pycache__':
                    add(path, True, 'safe: bytecode cache, rebuilt on demand')
                elif lock_busy:
                    add(path, False, 'kept: a luna command holds the writer lock')
                else:
                    add(path, True, 'safe: leftover of an interrupted runtime deploy')
    good = [path for path in lumen_backups()]
    for index, path in enumerate(good):
        meta = read_json_file(path / 'backup.json') or {}
        if index < LUMEN_KEEP_BACKUPS:
            add(path, False, 'kept: one of the newest %d Lumen backups' % LUMEN_KEEP_BACKUPS)
        elif meta.get('failed') or lumen_running_record().get('backup') == path.name:
            add(path, False, 'kept: a failed update\'s backup (or the running update\'s)')
        else:
            add(path, True, 'safe: older than the newest %d Lumen backups and not from a failed update' % LUMEN_KEEP_BACKUPS)
    if state.is_dir():
        for path in sorted(state.glob('lumen-backups/.migrating-*')):
            add(path, not lock_busy, 'safe: leftover of an interrupted backup migration' if not lock_busy
                else 'kept: a luna command holds the writer lock')
    stray_dir = home() / '.config/systemd/user'
    if stray_dir.is_dir():
        for path in sorted(stray_dir.glob('.*.tmp.*')):
            add(path, False, 'kept: outside Luna\'s own dirs; remove it by hand if you recognise it')
    return rows


def _inside_luna_dirs(path):
    resolved = path.parent.resolve() / path.name
    for root in (luna_tools_dir(), luna_state_dir()):
        try:
            resolved.relative_to(root.resolve())
            return True
        except ValueError:
            continue
    return False


def cmd_clean(options):
    """List what Luna can tidy up; delete only with --yes."""
    rows = clean_candidates()
    yes = getattr(options, 'yes', False) and not getattr(options, 'dry_run', False)
    removable = [row for row in rows if row[3] and _inside_luna_dirs(row[0])]
    for path, size, age, can, reason in rows:
        print('%-8s %9s %10s  %s\n          %s' % ('REMOVE' if can and _inside_luna_dirs(path) else 'keep',
                                                   _human(size), _age_text(age), path, reason))
    print('\ntotal removable %s in %d item(s); kept %s in %d item(s)' % (
        _human(sum(row[1] for row in removable)), len(removable),
        _human(sum(row[1] for row in rows if row not in removable)), len(rows) - len(removable)))
    if not yes:
        print('DRY RUN: nothing was deleted. Re-run with --yes to delete the REMOVE items.')
        return EXIT_OK
    for path, size, _age, _can, _reason in removable:
        try:
            if path.is_symlink() or path.is_file():
                path.unlink()
            else:
                shutil.rmtree(path)
            print('removed %s (%s)' % (path, _human(size)))
        except OSError as error:
            print('could not remove %s: %s' % (path, error), file=sys.stderr)
    return EXIT_OK


cmd_clean_guard = lambda options: (run_locked(cmd_clean, options) if getattr(options, 'yes', False)  # noqa: E731
                                   and not getattr(options, 'dry_run', False) else cmd_clean(options))


def _selftest_module():
    """Load luna_selftest.py beside luna.py, or from the deployed runtime tree."""
    import importlib.util
    here = Path(__file__).resolve().parent
    candidates = (here / 'luna_selftest.py',
                  ROOT / 'scripts' / 'luna_selftest.py',
                  here / 'runtime' / 'scripts' / 'luna_selftest.py')
    path = next((item for item in candidates if item.is_file()), None)
    if path is None:
        print('FAIL: luna_selftest.py is missing beside luna.py; reinstall Luna '
              '(luna install --force).', file=sys.stderr)
        return None
    if str(path.parent) not in sys.path:
        sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location('luna_selftest', str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def cmd_selftest(options):
    """Live menu checks against the running Steam over CDP (never writes)."""
    module = _selftest_module()
    if module is None:
        return EXIT_CORRUPT
    code, _report = module.run_selftest(
        json_output=getattr(options, 'json', False),
        keep_open=getattr(options, 'keep_open', False),
        host=active_host(),
        backend_names=module.backend_rpc_names(ROOT / 'addons'),
    )
    return code


def cmd_doctor(options):
    """Print-only health check: preflight, update-block heal check, launch options, pacman.

    `--report` writes one redacted, shareable text file instead (and prints its path).
    """
    if getattr(options, 'report', False):
        module = _selftest_module()
        if module is None:
            return EXIT_CORRUPT
        out = Path(options.out).expanduser() if getattr(options, 'out', None) else None
        try:
            path = module.build_report(out=out, luna=sys.modules[__name__],
                                       luna_py=Path(__file__).resolve())
        except OSError as error:
            print('FAIL: could not write the report: %s' % error, file=sys.stderr)
            return EXIT_CORRUPT
        print('wrote %s' % path)
        return EXIT_OK
    print('host       : %s' % host_label())
    code = cmd_preflight(argparse.Namespace(only=[], no_heal=True))
    print('\n== update block (heal check, nothing is changed) ==')
    result = heal_steam_lock(apply=False)
    if not result['pinned']:
        print('no Steam build is pinned (luna steam pin / luna steam lock)')
    elif not result['notes']:
        print('ok: update block and read-only state are intact')
    print_heal(result, 'doctor')
    print('\n== Steam client ==')
    print(known_good_line()[1])
    print(libcrypto_status()[1])
    if flatpak_steam_only():
        print(FLATPAK_WARNING)
    print('\n== bridge ==')
    for line in bridge_status()[1]:
        print(line)
    print('\n== launch options ==')
    findings = scan_launch_options()
    if not findings:
        print('ok: no launch option points at a missing path')
    for item in findings:
        print('WARN: app %s: %s (missing: %s)' % (item['appid'] or '?', item['option'],
                                                   ', '.join(item['missing'])))
    print('\n== pacman ==')
    print(ignore_pkg_hint() or 'ok: nothing to do')
    print('\n== payloads (local only, no network) ==')
    problems = _manifest_problems(load_manifest(), ROOT)
    if problems:
        print('WARN: %d file(s) do not match vendor/MANIFEST.json:' % len(problems))
        for problem in problems[:10]:
            print('  - %s' % problem)
        code = EXIT_CORRUPT
    else:
        print('ok: every payload matches vendor/MANIFEST.json')
    marker = read_json_file(install_marker_path()) or {}
    if marker:
        print('WARN: the last install was interrupted (pid %s, started %s); run: luna install --force'
              % (marker.get('pid') or '?',
                 time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(int(marker.get('at') or 0)))
                 if marker.get('at') else 'unknown time'))
        code = EXIT_CORRUPT
    return code


def cmd_install(options):
    """Serve the active host; `--lumen` opts in to the full Lumen pipeline."""
    global _TARGET_HOST
    target = 'lumen' if getattr(options, 'lumen', False) else active_host()
    _TARGET_HOST = target
    try:
        return _cmd_install(options, target)
    finally:
        _TARGET_HOST = None


def _cmd_install(options, target):
    dry = getattr(options, 'dry_run', False)
    lumen_base = target == 'lumen' and getattr(options, 'lumen', False)
    if lumen_base:
        # The base installer must not run while Steam is open. Check that one
        # guard before touching anything; the rest are re-checked below.
        blocked = [reason for reason in refuse_reasons(for_install=True)
                   if 'Steam is running' in reason]
        if dry and blocked:
            # A dry run writes nothing: report the plan instead of stopping.
            print('note: Steam is running; a real run would stop here until Steam is closed')
        elif blocked:
            print('REFUSING to install:')
            for reason in blocked:
                print('  - %s' % reason)
            return EXIT_REFUSED
        say = lambda text: print('install   : %s' % text, flush=True)  # noqa: E731
        code = bootstrap_steam(options, say, dry)
        if code != EXIT_OK:
            return code
        code = bootstrap_lumen(options, say, dry)
        if code != EXIT_OK:
            return code
        if not dry and (not host_state_path().is_file() or active_host() != 'lumen'):
            _write_host_state('lumen', 'luna')
            unpatch_wrapper()
            systemctl_user('disable', '--now', HOST_UNIT)
            systemctl_user('daemon-reload')
            systemctl_user('enable', '--now', LUMEN_UNIT)
    targets = selected_addons(options)
    reasons = refuse_reasons(for_install=True)
    if dry and any('Steam is running' in reason for reason in reasons):
        # Steam being up only stops a run that would write; a dry run shows the plan.
        print('note: Steam is running; a real run would stop here until Steam is closed')
    if dry:
        reasons = [reason for reason in reasons if 'Steam is running' not in reason]
    if reasons:
        print('REFUSING to install:')
        for reason in reasons:
            print('  - %s' % reason)
        return EXIT_REFUSED
    # Only once the run is known to be allowed: this is a write.
    if target == 'luna' and not lumen_base and not dry and not prefer_luna():
        _write_host_state('luna', active_host())
    print('host      : %s' % host_label(target))
    if target == 'lumen':
        check = patcher('check')
        if check.returncode != 0:
            print('REFUSING to install: %s' % (check.stderr.strip() or check.stdout.strip()),
                  file=sys.stderr)
            return EXIT_PATCH
    else:
        check = None
    rows = plan(targets)
    to_write = [row for row in rows if row[4] in ('new', 'differs')]
    # A base (Lumen/Moon) update replaces boot.lua and wipes the bridge while
    # the payloads stay current. That is not "nothing to do": reinstalling the
    # bridge is part of every install, or Luna silently leaves the menu.
    needs_bridge = bool(check is not None and 'bridge compatible, not installed' in check.stdout)
    print('plan for %s:' % ', '.join(targets))
    for addon, label, source, dest, state in rows:
        print('  %-9s %-22s %s' % (addon, label, state))
    if needs_bridge:
        print('  bridge    boot.lua               reinstall (a base update wiped it)')
    steam_step = ((target == 'lumen' and not getattr(options, 'lumen', False))
                  or (target == 'luna' and getattr(options, 'lock_steam_client', False)))
    if dry:
        if not getattr(options, 'no_steam_machine', False):
            code = steam_machine_action('install', dry_run=True,
                                        migrate=getattr(options, 'migrate_steam_machine', False))
            if code:
                return code
        if steam_step and not getattr(options, 'keep_steam_client', False):
            if getattr(options, 'steam_build', ''):
                print('steam     : would install/verify build %s executables, cache, pin and block updates'
                      % STEAM_COMPAT_BUILD)
            else:
                print('steam     : would roll back to the ubuntu12 client, pin, cache and hard-lock it '
                      '(Steam.cfg + steam.cfg, package/ read-only); skipped when already locked')
        install_luna_host(dry_run=True)
        print('\nDRY RUN: %d file(s) would change%s.' % (len(to_write),
              ' + bridge reinstall' if needs_bridge else ', nothing was written'))
        return EXIT_OK
    differing = [row for row in to_write if row[4] == 'differs']
    if differing and not options.force:
        print('\nREFUSING to overwrite %d file(s) that differ from the pinned payloads:' % len(differing))
        for addon, label, source, dest, state in differing:
            print('  %s/%s (installed %s)' % (addon, label, dest))
        print('Re-run with --force if you really want to replace them.')
        return EXIT_NEEDS_FORCE

    if steam_step and not getattr(options, 'keep_steam_client', False):
        steam_build = getattr(options, 'steam_build', '') or ''
        if steam_build:
            if steam_build != STEAM_COMPAT_BUILD:
                print('REFUSING: --steam-build supports only the verified historical build %s'
                      % STEAM_COMPAT_BUILD, file=sys.stderr)
                return EXIT_REFUSED
            ok, message = ensure_compatible_steam()
            print('steam     : %s' % message, file=sys.stdout if ok else sys.stderr)
            if not ok:
                return 1
        elif steam_hard_lock_active() and steam_state().get('binariesMatch') is not False:
            print('steam     : ubuntu12 hard lock already active; keeping it (build %s)'
                  % (read_steam_pin().get('clientBuild') or '?'))
            print_heal(heal_steam_lock(), 'steam     ')
        else:
            code, message = _lock_steam_client(False, lambda: False)
            print('steam     : %s' % message, file=sys.stdout if code == 0 else sys.stderr)
            if code != 0:
                return 1
    # Shortcuts run before any add-on byte changes: a helper failure must not
    # report a successful install of the payloads as a failure (or the other way
    # round), and the run stays repeatable.
    if not getattr(options, 'no_steam_machine', False):
        code = steam_machine_action('install', migrate=getattr(options, 'migrate_steam_machine', False))
        if code:
            print('FAILED: SteamOS Mode shortcuts could not be updated; '
                  'no add-on files were changed by this run.', file=sys.stderr)
            return code
    if not to_write and not needs_bridge:
        write_install_snapshot()
        write_tooling()
        write_apply_unit()
        retire_lumen_hook()
        install_luna_host()
        if read_steam_pin():
            ensure_steam_cfg_block()
        print('\nAlready installed and identical to the pinned payloads. Nothing to do.')
        clear_install_marker()
        hint = ignore_pkg_hint()
        if hint:
            print(hint)
        return EXIT_OK
    only_bridge = needs_bridge and not to_write

    undo_root = archive_previous(differing, 'undo')
    if undo_root:
        print('undo      : previous copies of %d differing file(s) saved to %s'
              % (len(differing), undo_root))
    apply = Apply()
    write_install_marker(targets, force=bool(getattr(options, 'force', False)))
    try:
        apply.mkdir(addons_dir())
        for addon, label, source, dest, state in rows:
            if state in ('new', 'differs'):
                apply.write(source, dest)
        result = patcher('install') if target == 'lumen' else None
        if result is not None and result.returncode != 0:
            raise RuntimeError('boot.lua patch failed: %s' % (result.stderr.strip() or result.stdout.strip()))
        apply.did_patch = result is not None
    except BaseException as error:  # noqa: BLE001 - Ctrl-C/SIGTERM must roll back too
        failures = apply.rollback()
        if not failures:
            # The tree is consistent again, so the marker would be a lie.
            clear_install_marker()
        print('FAILED: %s' % error, file=sys.stderr)
        if failures:
            print('%d change(s) could not be undone; `luna status` now reports an '
                  'interrupted install.' % failures, file=sys.stderr)
        else:
            print('Every add-on change made by this run was rolled back.', file=sys.stderr)
        if steam_step and not getattr(options, 'keep_steam_client', False):
            print('The separately verified Steam client installation remains in place; '
                  'its rollback backup was retained.', file=sys.stderr)
        if isinstance(error, Exception):
            return 1
        raise

    write_install_snapshot()
    write_tooling()
    write_apply_unit()
    retire_lumen_hook()
    install_luna_host()
    if read_steam_pin():
        # Base installers drop steam.cfg; a pin means updates must stay
        # blocked, so re-apply Luna's block right away (Steam is closed here).
        ensure_steam_cfg_block()
        print('steam.cfg : update block re-applied (a Steam build is pinned)')
    print('\ninstalled: %s (host: %s)'
          % (', '.join('%s %s' % (name, addon_version(name)) for name in targets),
             host_label(target)))
    if only_bridge:
        print('bridge   : reinstalled into boot.lua (payloads were already current)')
    print('bridge   : %s' % (addons_dir() / 'bridge.lua'))
    if target == 'lumen':
        print('boot.lua : %s' % patcher('check').stdout.strip())
    else:
        print('host     : luna-host serves %s (no Lumen boot.lua patch)'
              % (addons_dir()))
    hint = ignore_pkg_hint()
    if hint:
        print(hint)
    print('\nRestart Steam (Steam -> Exit, then launch it again) to load the add-ons.')
    clear_install_marker()
    return EXIT_OK


def cmd_uninstall(options):
    reasons = refuse_reasons(for_install=False)
    if reasons:
        print('REFUSING to uninstall:')
        for reason in reasons:
            print('  - %s' % reason)
        return EXIT_REFUSED
    targets = selected_addons(options) if options.only else installed_addons()
    if not targets:
        print('Nothing installed for the selected add-ons.')
        return EXIT_OK
    remaining = tuple(name for name in installed_addons() if name not in targets)
    if 'luna' in targets:
        retire_lumen_hook()
    if remaining:
        for name in targets:
            shutil.rmtree(addons_dir() / name, ignore_errors=True)
            print('removed %s; %s still installed, bridge and boot.lua left in place'
                  % (name, ', '.join(remaining)))
        return EXIT_OK
    # Last one out: restore Lumen before deleting the payload it points at, so a
    # failed restore can never leave a live bridge aiming at removed files.
    if active_host() == 'lumen':
        result = patcher('uninstall')
        if result.returncode != 0:
            print('REFUSING to remove the last add-on: %s'
                  % (result.stderr.strip() or result.stdout.strip()), file=sys.stderr)
            return EXIT_PATCH
        print(result.stdout.strip())
    else:
        undo_standalone_setup()
    for name in targets:
        shutil.rmtree(addons_dir() / name, ignore_errors=True)
        print('removed %s' % name)
    bridge = addons_dir() / 'bridge.lua'
    if bridge.is_file():
        bridge.unlink()
        print('removed bridge.lua')
    snapshot = addons_dir() / 'luna-manifest.json'
    if snapshot.is_file():
        snapshot.unlink()
    # A full uninstall must not leave the unattended worker behind: without
    # the payloads it would only fail, and with them gone a timer could
    # reinstall after the user asked for removal.
    try:
        unit_dir, service, timer = unit_paths()
        systemctl_user('disable', '--now', 'luna-autoupdate.timer')
        stop_apply_service()
        for path in (timer, service, apply_unit_paths()[1]):
            try:
                if path.exists():
                    path.unlink()
            except OSError:
                pass
        systemctl_user('daemon-reload')
        pending = luna_tools_dir() / 'pending.json'
        try:
            if pending.exists():
                pending.unlink()
        except OSError:
            pass
        print('unattended updates: disabled and queued updates cleared')
    except Exception:  # noqa: BLE001 - uninstall must succeed even if systemd is absent
        pass
    print('Local playtime and presence data were left untouched.')
    launcher = home() / '.local/bin/luna'
    if launcher.is_file() and not launcher.is_symlink() and ours_cli_launcher(launcher):
        launcher.unlink()
    if steam_machine_action('uninstall'):
        print('Some SteamOS Mode shortcuts could not be removed; custom edits were preserved.', file=sys.stderr)
    return EXIT_OK


def untraceable_pins(manifest):
    """Pins whose source_commit is not a commit id, so the bytes cannot be traced."""
    found = []
    for section in ('shared', 'addons'):
        for name, entry in sorted(manifest.get(section, {}).items()):
            commit = str(entry.get('source_commit', ''))
            if not re.fullmatch(r'[0-9a-f]{40}', commit):
                found.append('%s (%s)' % (name, commit or 'no commit'))
    return found


def warn_untraceable(manifest, stream=sys.stdout):
    found = untraceable_pins(manifest)
    if found:
        print('\nWARNING: pinned to no commit, so these bytes cannot be traced to a source '
              'revision: %s. Commit the source and re-run vendor.' % ', '.join(found), file=stream)


def check_upstream(manifest):
    """Fetch branch heads into disposable repositories; never alter the checkout."""
    problems = []
    env = dict(os.environ, GIT_TERMINAL_PROMPT='0', GIT_ASKPASS='/bin/false')
    def git(*args, cwd=None):
        return subprocess.run(['git', *args], cwd=cwd, env=env, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=60, check=True)
    with tempfile.TemporaryDirectory(prefix='luna-upstream-') as tmp:
        for name, entry in manifest.get('addons', {}).items():
            pin = entry.get('source_commit', '')
            if not re.fullmatch(r'[0-9a-f]{40}', pin):
                problems.append('%s: invalid source commit' % name)
                continue
            try:
                url = entry.get('source_repo', '')
                if name in FIRST_PARTY:
                    # A checkout knows its own origin (forks, renames); an
                    # installed copy or an unpacked release has no git, so it
                    # falls back to the repository the manifest names.
                    try:
                        # Only when ROOT itself is the checkout: git would
                        # otherwise discover an enclosing, unrelated repository.
                        top = git('rev-parse', '--show-toplevel', cwd=ROOT).stdout.decode().strip()
                        if top and Path(top).resolve() == Path(ROOT).resolve():
                            url = git('remote', 'get-url', 'origin', cwd=ROOT).stdout.decode().strip() or url
                    except (OSError, subprocess.SubprocessError):
                        pass
                repo = Path(tmp) / name
                git('init', '--bare', str(repo))
                git('fetch', '--quiet', '--', url,
                    'refs/heads/main' if name in FIRST_PARTY else 'HEAD', cwd=repo)
                head = git('rev-parse', 'FETCH_HEAD', cwd=repo).stdout.decode().strip()
                try:
                    git('cat-file', '-e', pin + '^{commit}', cwd=repo)
                except subprocess.CalledProcessError:
                    git('fetch', '--quiet', '--', url, pin, cwd=repo)
                paths = sorted({('addons/%s/' % name if name in FIRST_PARTY else '')
                                + meta['source_path'] for meta in entry.get('files', {}).values()})
                if not paths:
                    problems.append('%s: no pinned source files' % name)
                    continue
                pathspecs = [':(literal)' + path for path in paths]
                ancestor = subprocess.run(['git', 'merge-base', '--is-ancestor', pin, head],
                                          cwd=repo, env=env, timeout=60)
                if ancestor.returncode not in (0, 1):
                    raise subprocess.CalledProcessError(ancestor.returncode, 'git merge-base')
                files = git('diff', '--no-renames', '--name-only', '-z', pin, head,
                            '--', *pathspecs, cwd=repo).stdout.decode(
                    'utf-8', errors='replace').split('\0')
                files = [path for path in files if path]
                if ancestor.returncode == 1:
                    counts = git('rev-list', '--left-right', '--count', pin + '...' + head,
                                 cwd=repo).stdout.decode().split()
                    print('  %s: diverged (pin-only %s commit(s), upstream-only %s commit(s)); '
                          'changed pinned files:' % (name, counts[0], counts[1]))
                    problems.append('%s: diverged upstream history' % name)
                elif files:
                    count = int(git('rev-list', '--count', pin + '..' + head,
                                    '--', *pathspecs, cwd=repo).stdout)
                    print('  %s: behind by %d commit(s); changed files:' % (name, count))
                    problems.append('%s: stale upstream pin' % name)
                else:
                    print('  %s: ok (pinned files unchanged)' % name)
                for path in files:
                    print('    %s' % path)
            except (OSError, subprocess.SubprocessError, KeyError) as exc:
                print('  %s: unreachable (upstream comparison unavailable: %s)' % (name, exc))
    if problems:
        print('Refresh: bash scripts/vendor-update.sh && python3 scripts/luna.py verify '
              '&& python3 scripts/luna.py install')
    return problems


def cmd_verify(options):
    print('host       : %s' % host_label())
    manifest = load_manifest()
    problems = []
    checked = 0
    expected = {}
    for name in ADDONS:
        entry = manifest.get('addons', {}).get(name)
        if not entry:
            problems.append('manifest has no entry for %s' % name)
            continue
        expected_paths = set()
        for rel, meta in entry.get('files', {}).items():
            path = payload_path(name, meta['source_path'])
            expected_paths.add(path)
            if not path.is_file():
                problems.append('missing payload %s/%s' % (name, rel))
                continue
            if sha256(path) != meta['sha256']:
                problems.append('payload %s/%s does not match the manifest' % (name, rel))
            else:
                checked += 1
        expected[name] = expected_paths
    for rel, meta in manifest.get('shared', {}).items():
        path = ROOT / rel
        if not path.is_file():
            problems.append('missing shared file %s' % rel)
            continue
        if sha256(path) != meta['sha256']:
            problems.append('shared file %s does not match the manifest' % rel)
        else:
            checked += 1
    # A payload directory must contain exactly what the manifest lists, otherwise
    # a removed or renamed file could survive in the checkout unnoticed.
    for name, expected_paths in expected.items():
        directory = ROOT / 'addons' / name
        for path in sorted(directory.rglob('*')):
            if path.is_file() and path not in expected_paths:
                problems.append('unexpected file in the payload: %s' % path.relative_to(ROOT))
    print('manifest   : %d file(s) verified, generated %s' % (checked, manifest.get('generated', '?')))
    for name in ADDONS:
        entry = manifest.get('addons', {}).get(name, {})
        print('  %-9s %s %s @ %s' % (name, entry.get('plugin_version', '?'),
                                     entry.get('source_repo', '?'), entry.get('source_commit', '?')[:12]))
    if options.sources:
        for name, entry in manifest.get('addons', {}).items():
            if name not in SOURCED:
                continue
            base = Path(options.source_dir) if options.source_dir else (home() / 'Documents/Scripts')
            source = base / ('slsteam-' + name)
            if not source.is_dir():
                print('  skip %s: no source checkout at %s' % (name, source))
                continue
            for rel, meta in entry.get('files', {}).items():
                try:
                    data = subprocess.run(['git', 'show', entry['source_commit'] + ':' + meta['source_path']],
                                          cwd=source, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                          check=True).stdout
                    if hashlib.sha256(data).hexdigest() != meta['sha256']:
                        problems.append('%s: %s differs from the pinned source commit (re-run vendor)'
                                        % (name, meta['source_path']))
                except (OSError, subprocess.CalledProcessError, KeyError):
                    problems.append('%s: cannot read %s at pinned source commit'
                                    % (name, meta['source_path']))
            print('  %-9s compared against %s at %s' % (name, source, entry.get('source_commit', '?')))
    if getattr(options, 'upstream', False):
        problems.extend(check_upstream(manifest))
    if problems:
        print('\nPROBLEMS:')
        for problem in problems:
            print('  - %s' % problem)
        return 1
    warn_untraceable(manifest)
    print('\nOK: the payloads match the manifest.')
    return EXIT_OK


def cmd_vendor(options):
    # --only re-pins the named add-ons and keeps every other manifest entry as
    # it is, so refreshing one payload never silently moves the others.
    only = set(selected_addons(options)) if options.only else None
    sources = {}
    for name in SOURCED:
        if only is not None and name not in only:
            continue
        source = Path(options.source_dir or (home() / 'Documents/Scripts' / ('slsteam-' + name)))
        if not (source / '.git').is_dir():
            print('FAIL: no git checkout for %s at %s (pass --source-dir)' % (name, source), file=sys.stderr)
            return 1
        sources[name] = source

    def head(source):
        return subprocess.run(['git', '-C', str(source), 'rev-parse', 'HEAD'],
                              capture_output=True, text=True).stdout.strip()

    def show(source, commit, path):
        result = subprocess.run(['git', '-C', str(source), 'show', '%s:%s' % (commit, path)],
                                capture_output=True)
        if result.returncode != 0:
            raise SystemExit('FAIL: %s has no %s at %s' % (source, path, commit[:12]))
        return result.stdout

    def archive(source, commit, paths, dest):
        if dest.exists():
            shutil.rmtree(dest)
        dest.mkdir(parents=True, exist_ok=True)
        producer = subprocess.Popen(['git', '-C', str(source), 'archive', '--format=tar', commit, '--'] + list(paths),
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        consumer = subprocess.Popen(['tar', '-x', '-C', str(dest)], stdin=producer.stdout,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        producer.stdout.close()
        consumer.communicate()
        producer.wait()
        if producer.returncode != 0 or consumer.returncode != 0:
            raise SystemExit('FAIL: git archive from %s failed: %s'
                             % (source, producer.stderr.read().decode(errors='replace')))

    generated = time.strftime('%Y-%m-%dT%H:%M:%S%z')
    if only is None:
        manifest = {'generated': generated, 'shared': {}, 'addons': {}}
    else:
        manifest = json.loads(MANIFEST_PATH.read_text())
        manifest['generated'] = generated
    repo_commit = subprocess.run(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'],
                                 capture_output=True, text=True).stdout.strip()
    if only is None:
        vendor_shared(manifest, sources['playtime'], head, show, repo_commit)
    # First-party add-ons come from this repository, so they are pinned to this
    # checkout's HEAD - and only when the bytes are HEAD's, so a pin always names
    # a commit that really contains them.
    for name in FIRST_PARTY:
        if only is not None and name not in only:
            continue
        files = {}
        for source_rel, dest_rel in ADDON_FILES[name]:
            path = payload_path(name, source_rel)
            if not path.is_file():
                raise SystemExit('FAIL: this repository has no addons/%s/%s' % (name, source_rel))
            rel = path.relative_to(ROOT).as_posix()
            committed = subprocess.run(['git', '-C', str(ROOT), 'show', '%s:%s' % (repo_commit, rel)],
                                       capture_output=True)
            if committed.returncode != 0 or committed.stdout != path.read_bytes():
                raise SystemExit('FAIL: %s differs from HEAD; commit it before pinning' % rel)
            files[dest_rel] = {'source_path': source_rel, 'sha256': sha256(path)}
        manifest['addons'][name] = {
            'source_repo': 'https://github.com/pgm1207/Luna',
            'source_commit': repo_commit,
            'plugin_version': addon_version(name),
            'files': files,
        }
        print('pinned addons/%s from this repository@%s (%d files)' % (name, repo_commit[:12], len(files)))
    for name, source in sources.items():
        commit = head(source)
        paths = sorted({source_path for source_path, _ in ADDON_FILES[name]})
        archive(source, commit, paths, ROOT / 'addons' / name)
        files = {}
        for source_rel, dest_rel in ADDON_FILES[name]:
            path = payload_path(name, source_rel)
            if not path.is_file():
                raise SystemExit('FAIL: %s did not produce %s' % (name, source_rel))
            files[dest_rel] = {'source_path': source_rel, 'sha256': sha256(path)}
        manifest['addons'][name] = {
            'source_repo': 'https://github.com/pgm1207/slsteam-' + name,
            'source_commit': commit,
            'plugin_version': addon_version(name),
            'files': files,
        }
        print('pinned addons/%s from %s@%s (%d files)' % (name, source.name, commit[:12], len(files)))
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2, sort_keys=True) + '\n')
    print('wrote %s' % MANIFEST_PATH)
    return EXIT_OK


def vendor_shared(manifest, shared_source, head, show, repo_commit):
    # The shared bridge and patcher come from the playtime project; both projects
    # ship byte-identical copies, and `verify --sources` keeps them honest.
    shared_commit = head(shared_source)
    for rel, source_path in (('bridge/bridge.lua', 'bridge/bridge.lua'),
                             ('scripts/patch-lumen.py', 'scripts/patch-lumen.py')):
        (ROOT / rel).write_bytes(show(shared_source, shared_commit, source_path))
        manifest['shared'][rel] = {'source_repo': 'https://github.com/pgm1207/slsteam-playtime',
                                   'source_commit': shared_commit, 'source_path': source_path,
                                   'sha256': sha256(ROOT / rel)}
        print('pinned %s from %s@%s' % (rel, shared_source.name, shared_commit[:12]))
    # First-party scripts come from this repository, pinned to its HEAD.
    manifest['shared']['scripts/steam-machine.py'] = {
        'source_repo': 'https://github.com/pgm1207/Luna', 'source_commit': repo_commit,
        'source_path': 'scripts/steam-machine.py', 'sha256': sha256(ROOT / 'scripts/steam-machine.py')}


def steam_machine_action(action, dry_run=False, migrate=False):
    script = ROOT / 'scripts/steam-machine.py'
    if not script.is_file():
        script = luna_tools_dir() / 'steam-machine.py'
    if not script.is_file():
        if action == 'uninstall':
            return EXIT_OK
        print('Steam Machine helper missing; reinstall Luna from a complete bundle.', file=sys.stderr)
        return 1
    command = [sys.executable, str(script), action]
    if dry_run:
        command.append('--dry-run')
    if migrate:
        command.append('--migrate-legacy')
    return subprocess.run(command).returncode


def cmd_launchers(options):
    return steam_machine_action('install', options.dry_run, options.migrate_legacy)


def cmd_lumen_guard(options):
    """Lumen's apply path mutates the base, so it takes the same writer lock."""
    action = getattr(options, 'lumen_action', 'status')
    dry = getattr(options, 'dry_run', False)
    terminal = action == 'update' and getattr(options, 'terminal', False) and not dry
    mutating = (action in ('update', 'restore', 'cancel')
                or (action == 'channels' and getattr(options, 'channels_action', '') == 'set')) and not dry
    if terminal:
        # Never hold the lock here: the window runs `luna lumen update --yes`,
        # which takes it itself.
        return cmd_lumen(options)
    if mutating:
        return run_locked(cmd_lumen, options)
    return cmd_lumen(options)


STEAM_LOCKED_ACTIONS = ('lock', 'unlock', 'recover', 'update', 'cache')


def cmd_steam_guard(options):
    """Steam lock/unlock/recover/update/cache take the writer lock, so a queued Lumen or
    Luna job (whose waiter polls for a closed Steam) cannot start in the middle of them."""
    if getattr(options, 'steam_action', 'status') in STEAM_LOCKED_ACTIONS:
        return run_locked(cmd_steam, options)
    return cmd_steam(options)


cmd_refresh_locked = locked(cmd_refresh)

# One lock for every command that writes: timer, menu, shell.
cmd_install_locked = locked(cmd_install)
cmd_uninstall_locked = locked(cmd_uninstall)
cmd_launchers_locked = locked(cmd_launchers)
cmd_host_locked = locked(cmd_host)
cmd_backup_locked = locked(cmd_backup)
cmd_restore_locked = locked(cmd_restore)
cmd_hook_locked = locked(cmd_hook)


def build_parser():
    parser = argparse.ArgumentParser(prog='luna', description=__doc__,
                                     epilog=EXIT_EPILOG,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command')
    for name, handler in (('preflight', cmd_preflight), ('status', cmd_status),
                          ('install', cmd_install_locked), ('uninstall', cmd_uninstall_locked),
                          ('verify', cmd_verify), ('vendor', cmd_vendor),
                          ('release', cmd_release), ('update', cmd_update),
                          ('check', cmd_check), ('autoupdate', cmd_autoupdate),
                          ('reconcile', cmd_reconcile),
                          ('backup', cmd_backup_locked), ('restore', cmd_restore_locked),
                          ('steam', cmd_steam_guard),
                          ('refresh', cmd_refresh_locked),
                          ('bootstrap', cmd_bootstrap),
                          ('update-all', cmd_update_all),
                          ('doctor', cmd_doctor),
                          ('selftest', cmd_selftest),
                          ('launchers', cmd_launchers_locked),
                           ('timer', cmd_timer), ('hook', cmd_hook_locked), ('host', cmd_host_locked),
                          ('lumen', cmd_lumen_guard), ('clean', cmd_clean_guard)):
        child = sub.add_parser(name, aliases=['up'] if name == 'update-all' else [])
        child.set_defaults(handler=handler)
        child.add_argument('--only', action='append', default=[],
                           help='restrict to one add-on (repeatable): %s' % ', '.join(ADDONS))
        if name == 'status':
            child.add_argument('--live', action='store_true', help='probe running backends read-only')
            child.add_argument('--json', action='store_true', help='print status as JSON')
            child.add_argument('--strict', action='store_true',
                               help='exit 1 instead of 6 when a payload differs (CI gate)')
        if name == 'selftest':
            child.add_argument('--json', action='store_true',
                               help='print one JSON object instead of PASS/FAIL lines')
            child.add_argument('--keep-open', action='store_true',
                               help='leave the menu open and skip the close/no-hidden-view checks')
        if name == 'doctor':
            child.add_argument('--report', action='store_true',
                               help='write one redacted report file for sharing instead of '
                                    'printing the health check (the last line is its path)')
            child.add_argument('--out', metavar='FILE',
                               help='where --report writes (default '
                                    '~/luna-report-<timestamp>.txt)')
        if name == 'install':
            child.add_argument('--lumen', action='store_true',
                               help='opt in to the Lumen stack: pin Steam, install Lumen/LuaTools/'
                                    'SLSsteam with the pinned installer, then Luna on top; '
                                    'without it installs on Luna\'s own standalone host')
            child.add_argument('--lock-steam-client', action='store_true',
                               help='standalone host only: pin and hard-lock the Steam client '
                                    'explicitly (never automatic without --lumen)')
            child.add_argument('--force', action='store_true',
                               help='replace installed files that differ from the pinned payloads')
            child.add_argument('--dry-run', action='store_true', help='print the plan and stop')
            child.add_argument('--keep-steam-client', action='store_true',
                               default=os.environ.get('LUNA_KEEP_STEAM_CLIENT') == '1',
                               help='leave the Steam client alone (no ubuntu12 hard lock)')
            child.add_argument('--steam-build', metavar='BUILD',
                               help='opt in to the historical verified build %s instead of the '
                                    'default ubuntu12 hard lock' % STEAM_COMPAT_BUILD)
            child.add_argument('--no-steam-machine', action='store_true',
                               help='do not generate SteamOS Mode application shortcuts')
            child.add_argument('--migrate-steam-machine', action='store_true',
                               help='back up, rename and upgrade recognized legacy launchers to SteamOS Mode')
        if name == 'bootstrap':
            child.add_argument('--lumen', action='store_true',
                               help='opt in to today\'s full Lumen pipeline: Steam pin, then '
                                    'Lumen/LuaTools/SLSsteam via the pinned installer, then Luna; '
                                    'without it a fresh machine gets Luna\'s standalone host only')
            child.add_argument('--lock-steam-client', action='store_true',
                               help='standalone host only: pin and hard-lock the Steam client '
                                    'explicitly (never automatic without --lumen)')
            child.add_argument('--dry-run', action='store_true', help='print the plan and stop')
            child.add_argument('--force', action='store_true',
                               help='replace installed add-on files that differ')
            child.add_argument('--skip-deps', '--no-system-packages', dest='no_system_packages',
                               action='store_true',
                               help='skip the whole system package step (no sudo prompt)')
            child.add_argument('--no-mangohud', action='store_true', help='do not install mangohud (+32-bit)')
            child.add_argument('--no-gamescope', action='store_true', help='do not install gamescope')
            child.add_argument('--no-gamemode', action='store_true', help='do not install gamemode (+32-bit)')
            child.add_argument('--keep-steam-client', action='store_true',
                               help='leave the Steam client alone (no ubuntu12 hard lock)')
            child.add_argument('--steam-build', metavar='BUILD',
                               help='opt in to the historical build %s instead of the hard lock'
                                    % STEAM_COMPAT_BUILD)
            child.add_argument('--no-steam-machine', action='store_true',
                               help='do not generate SteamOS Mode application shortcuts')
            child.add_argument('--migrate-steam-machine', action='store_true',
                               help='back up, rename and upgrade recognized legacy launchers')
            for flag in ('--slsteam-channel', '--plugin-channel', '--lumen-channel'):
                child.add_argument(flag, help='pass stable or beta to the base installer')
            child.add_argument('--keep-millennium', action='store_true',
                               help='pass through to the base installer')
        if name == 'update-all':
            child.add_argument('--dry-run', action='store_true', help='print the plan and stop')
            child.add_argument('--bundle', help='update the add-ons from a local bundle tarball')
            child.add_argument('--url', help='update the add-ons from a bundle URL')
            child.add_argument('--base-latest', action='store_true',
                               help='also move the Moon/Lumen base to upstream main (needs --yes; '
                                    'default: only re-verify the pinned installer)')
            child.add_argument('--yes', action='store_true', help='confirm --base-latest')
            child.add_argument('--terminal', action='store_true',
                               help='close Steam (steam -shutdown), then run the same update in a '
                                    'terminal window with its output in ~/.local/share/Luna/state/')
        if name == 'clean':
            child.add_argument('--dry-run', action='store_true', help='list only (the default)')
            child.add_argument('--yes', action='store_true', help='delete the REMOVE items')
        if name == 'refresh':
            child.add_argument('--dry-run', action='store_true',
                               help='print the plan without updating or writing')
            child.add_argument('--unlock-first', action='store_true',
                               help='lift the ubuntu12 hard lock, then update Steam (refresh refuses '
                                    'while the lock is active)')
        if name == 'launchers':
            child.add_argument('--dry-run', action='store_true', help='show launcher changes without writing')
            child.add_argument('--migrate-legacy', action='store_true',
                               help='back up and replace recognized legacy Gamescope+Steam launchers')
        if name == 'reconcile':
            child.add_argument('--dry-run', action='store_true',
                               help='report what would merge without writing')
        if name == 'backup':
            child.add_argument('--out', help='output directory (default: system temp dir)')
        if name == 'restore':
            child.add_argument('archive', help='backup archive to restore')
            child.add_argument('--force', action='store_true',
                               help='overwrite files that are newer than the backup')
        if name == 'steam':
            child.add_argument('steam_action', nargs='?', default='status',
                               choices=['status', 'pin', 'unpin', 'cache', 'recover',
                                        'update', 'cancel', 'lock', 'unlock',
                                        'heal', 'launchopts', 'ignore-pkg', 'libcrypto', 'history'],
                               help='what to do (default: status)')
            child.add_argument('--fix', action='store_true',
                               help='with launchopts: back up localconfig.vdf and clear the options '
                                    'that point at missing paths (Steam must be closed)')
            child.add_argument('--install', action='store_true',
                               help='with libcrypto: install the 32-bit libcrypto.so.3 into ubuntu12_32')
            child.add_argument('--remove', action='store_true',
                               help='with libcrypto: remove ubuntu12_32/libcrypto.so.3')
            child.add_argument('--apply', action='store_true',
                               help='with ignore-pkg: run the sudo command (interactive only)')
            child.add_argument('--force', action='store_true',
                               help='with lock: kill a running Steam without asking')
            child.add_argument('--relaunch', action='store_true',
                               help='with lock/unlock: notify the result and reopen Steam detached '
                                    '(unlock closes a running Steam first); for the in-Steam UI')
            child.add_argument('--build', help='client build for pin/cache/recover (default: detected/pinned)')
            child.add_argument('--now', action='store_true',
                               help='with update: apply right away instead of queueing (Steam must be closed); '
                                    'with unlock: launch Steam so it self-updates')
        if name == 'check':
            child.add_argument('--force', action='store_true', help='ignore the 6h cache')
        if name == 'autoupdate':
            child.add_argument('--verbose', action='store_true', help='print the decision')
            child.add_argument('--wait-steam', action='store_true',
                               help='wait until Steam and steamwebhelper stay closed, then apply '
                                    '(what luna-apply.service runs)')
        if name == 'timer':
            child.add_argument('action', choices=['on', 'off', 'status'],
                                help='enable, disable or report the unattended-update timer')
        if name == 'hook':
            child.add_argument('action', choices=['on', 'off', 'status', 'repair'],
                               help='retired: only "repair" still works (reinstalls the bridge)')
        if name == 'host':
            child.add_argument('host_action', nargs='?', default='status',
                               choices=['status', 'cutover', 'rollback', 'patch-wrapper', 'unpatch-wrapper'],
                               help='show the plugin host, switch to luna-host, or switch back to Lumen')
        if name == 'lumen':
            child.add_argument('lumen_action', nargs='?', default='status',
                               choices=['status', 'update', 'cancel', 'restore', 'channels'],
                               help='Lumen base: show status, update (Luna re-attaches after), '
                                    'cancel a queued update, restore the pre-update backup, or '
                                    'show/change the per-component update channels')
            child.add_argument('channels_action', nargs='?', default='',
                               help='channels: "set" to change a channel (default: show them)')
            child.add_argument('channels_value', nargs='?', default='',
                               help='channels set: stable or beta')
            child.add_argument('--terminal', action='store_true',
                               help='update: close Steam (steam -shutdown), then run the same update '
                                    'in a terminal window with its output in ~/.local/share/Luna/state/')
            child.add_argument('--dry-run', action='store_true', help='print the plan and stop')
            child.add_argument('--yes', action='store_true', help='confirm the update')
            child.add_argument('--queue', action='store_true',
                               help='queue the update for the timer instead of running it now')
            child.add_argument('--installer-latest', action='store_true',
                               help='update: run the unpinned upstream main installer (needs --yes; never queued)')
            child.add_argument('--reinstall', action='store_true',
                               help='update: run the full update even when nothing newer is published')
            child.add_argument('--with-settings', action='store_true',
                               help='restore: also put back ~/.config/SLSsteam (default: left alone)')
            child.add_argument('--keep-millennium', action='store_true',
                               help='update: keep an installed Millennium (recorded for later runs)')
            child.add_argument('--remove-millennium', action='store_true',
                               help='update: let the installer remove Millennium (recorded for later runs)')
            child.add_argument('--json', action='store_true', help='status as JSON')
            child.add_argument('--check', action='store_true',
                               help='status: also compare installed vs upstream per component and channel '
                                    '(public metadata, read-only)')
            child.add_argument('--backup', default='', help='restore: backup name (default: newest)')
        if name == 'release':
            child.add_argument('--out', help='output directory (default: dist/)')
            child.add_argument('--publish', action='store_true',
                               help='create or refresh a GitHub release with the bundle (needs gh)')
        if name == 'update':
            child.add_argument('--bundle', help='install from a local bundle tarball')
            child.add_argument('--url', help='install from a bundle URL (a .sha256 beside it is verified)')
            child.add_argument('--latest', action='store_true',
                               help='download the newest release asset (needs gh)')
            child.add_argument('--dry-run', action='store_true', help='print the plan and stop')
        if name in ('verify', 'vendor'):
            child.add_argument('--sources', action='store_true',
                               help='also compare against local source checkouts')
            child.add_argument('--source-dir', help='parent directory holding slsteam-playtime / slsteam-presence')
        if name == 'verify':
            child.add_argument('--upstream', action='store_true',
                               help='fetch source branch heads and fail on stale pins (network required)')
    return parser


def main(argv=None):
    parser = build_parser()
    options = parser.parse_args(argv)
    if not getattr(options, 'handler', None):
        parser.print_help()
        return EXIT_OK
    if getattr(options, 'command', '') in ('lumen', 'autoupdate', 'update-all'):
        for problem in runtime_problems():
            print('WARNING: %s' % problem, file=sys.stderr)
    if getattr(options, 'command', '') in ('lumen', 'autoupdate', 'update-all', 'install') \
            and threading.current_thread() is threading.main_thread():
        def _stop(signum, _frame):
            raise SystemExit(128 + signum)  # so `finally` blocks (Steam lock, rollback) run
        old = {sig: signal.signal(sig, _stop) for sig in (signal.SIGTERM, signal.SIGHUP)}
        try:
            return options.handler(options)
        finally:
            for sig, handler in old.items():
                signal.signal(sig, handler)
    return options.handler(options)


if __name__ == '__main__':
    sys.exit(main())
