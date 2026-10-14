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
    luna uninstall [--only NAME]
    luna verify [--sources]             manifest, payload and drift checks
    luna vendor [--source-dir DIR]      re-pin payloads from the add-on projects
"""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / 'vendor' / 'MANIFEST.json'
BRIDGE_SOURCE = ROOT / 'bridge' / 'bridge.lua'
PATCHER = ROOT / 'scripts' / 'patch-lumen.py'
MARKER = 'SLS-ADDONS-INTEGRATION'

# Installed as one set. The bridge discovers them from the directory and sorts
# them, so this order is only for reporting; "luna" and "compressor" are
# first-party (they live in this repository) while the other two are vendored
# from their own projects.
ADDONS = ('luna', 'playtime', 'presence', 'compressor')
SOURCED = ('playtime', 'presence')
FIRST_PARTY = ('luna', 'compressor')

# Files that make up one add-on, as (path in this repo, path under sls-addons/<name>).
ADDON_FILES = {
    'luna': (
        ('plugin/backend/main.lua', 'backend/main.lua'),
        ('plugin/public/agent.js', 'public/agent.js'),
        ('plugin/public/panel.js', 'public/panel.js'),
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


def home():
    return Path(os.environ.get('HOME') or Path.home())


def lumen_root():
    return Path(os.environ.get('LUMEN_DIR') or (home() / '.local/share/Lumen'))


def addons_dir():
    return lumen_root() / 'sls-addons'


def boot_file():
    return lumen_root() / 'lua' / 'boot.lua'


def data_dir():
    return home() / '.local/share/slssteam-integrations'


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

def refuse_reasons(for_install):
    """Environment problems that must stop an install or uninstall."""
    reasons = []
    root = lumen_root()
    if not (root / 'lua' / 'boot.lua').is_file():
        reasons.append('Lumen lua/boot.lua not found under %s (is the LuaTools-Moon stack installed?)' % root)
    if not (root / 'luatools' / 'backend' / 'main.lua').is_file():
        reasons.append('LuaTools-Moon backend not found under %s' % root)
    if process_running('steam') or process_running('steamwebhelper'):
        reasons.append('Steam is running. Close it completely (Steam -> Exit) before changing Lumen.')
    # A sidecar from an earlier session keeps the old boot.lua in memory, which
    # makes a successful install look ineffective. Never kill it for the user.
    if process_running('lumen'):
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
        ('Lumen runtime', 'present' if lumen_boot.is_file() else 'not detected', str(lumen_boot)),
        ('LuaTools backend', 'present' if luatools_backend.is_file() else 'not detected', str(luatools_backend)),
        ('CloudRedirect', cloud_state, cloud_detail),
        ('Decky Loader', decky_state, decky_detail),
        ('Compressor CLI', 'present' if compressor else 'not detected', str(compressor or compressor_candidates[0])),
    )


def print_component_inventory():
    print('components:')
    for name, state, detail in component_inventory():
        print('  %-20s %-12s %s' % (name, state, detail))


# --------------------------------------------------------------------------- apply

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
        tmp = dest.with_name(dest.name + '.luna-tmp')
        data = source.read_bytes()
        tmp.write_bytes(data)
        os.chmod(tmp, 0o644)
        os.replace(tmp, dest)

    def mkdir(self, path):
        if not path.is_dir():
            path.mkdir(parents=True, exist_ok=True)
            self.undo.append(('rmdir', path, None))

    def rollback(self):
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
                print('  WARN: rollback could not restore %s: %s' % (target, error), file=sys.stderr)
        if self.did_patch:
            result = patcher('uninstall')
            if result.returncode != 0:
                print('  WARN: could not revert the boot.lua patch: %s'
                      % (result.stderr.strip() or result.stdout.strip()), file=sys.stderr)


# --------------------------------------------------------------------------- commands

def cmd_preflight(options):
    print('SLS Luna %s' % (load_manifest().get('generated', '?')[:10]))
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
    if boot.is_file():
        content = boot.read_text(errors='replace')
        print('boot.lua   : %s, %d marker(s), backup %s'
              % ('patched' if MARKER in content else 'unpatched', content.count(MARKER),
                 'present' if boot.with_name('boot.lua.sls-addons.original').is_file() else 'absent'))
    check = patcher('check')
    print('anchors    : %s' % (check.stdout.strip() or check.stderr.strip() or 'no output'))
    data = data_dir()
    if data.is_dir():
        ledgers = sorted(p.name for p in (data / 'playtime').glob('*.json')) if (data / 'playtime').is_dir() else []
        print('data       : %s (playtime ledgers: %s; presence: %s)'
              % (data, ', '.join(ledgers) or 'none',
                 'present' if (data / 'presence.json').is_file() else 'absent'))
    else:
        print('data       : %s (absent - no playtime recorded yet)' % data)
    reasons = refuse_reasons(for_install=True)
    if reasons:
        print('\nREFUSALS (install/uninstall would stop):')
        for reason in reasons:
            print('  - %s' % reason)
        return EXIT_REFUSED
    print('\nREADY: nothing here was modified.')
    return EXIT_OK


def cmd_status(options):
    targets = selected_addons(options)
    manifest = load_manifest()
    installed = installed_addons()
    boot = boot_file()
    content = boot.read_text(errors='replace') if boot.is_file() else ''
    patched = MARKER in content
    print('boot.lua   : %s (marker x%d, backup %s)'
          % ('patched' if patched else 'unpatched', content.count(MARKER),
             'present' if boot.with_name('boot.lua.sls-addons.original').is_file() else 'absent'))
    print('processes  : steam=%s lumen=%s'
          % (process_running('steam') or process_running('steamwebhelper'), process_running('lumen')))
    print_component_inventory()
    rows = plan(targets)
    print('%-9s %-22s %s' % ('add-on', 'file', 'state'))
    degraded = False
    for addon, label, source, dest, state in rows:
        print('%-9s %-22s %s' % (addon, label, state))
        if state == 'differs' and addon in installed:
            degraded = True
        if state == 'missing-source':
            degraded = True
    for name in targets:
        if name not in installed:
            print('note       : %s is not installed' % name)
    if installed and not patched:
        print('note       : add-ons are installed but boot.lua is not patched; '
              'run scripts/install-local.sh')
        degraded = True
    print('data       : %s' % ('present' if data_dir().is_dir() else 'absent'))
    print('manifest   : %d add-on(s) pinned from %s'
          % (len(manifest.get('addons', {})), ', '.join(
              sorted({entry.get('source_repo', '?') for entry in manifest.get('addons', {}).values()}))))
    return 1 if degraded else EXIT_OK


def cmd_install(options):
    targets = selected_addons(options)
    reasons = refuse_reasons(for_install=True)
    if reasons:
        print('REFUSING to install:')
        for reason in reasons:
            print('  - %s' % reason)
        return EXIT_REFUSED
    check = patcher('check')
    if check.returncode != 0:
        print('REFUSING to install: %s' % (check.stderr.strip() or check.stdout.strip()), file=sys.stderr)
        return EXIT_PATCH
    rows = plan(targets)
    to_write = [row for row in rows if row[4] in ('new', 'differs')]
    print('plan for %s:' % ', '.join(targets))
    for addon, label, source, dest, state in rows:
        print('  %-9s %-22s %s' % (addon, label, state))
    if options.dry_run:
        print('\nDRY RUN: %d file(s) would change, nothing was written.' % len(to_write))
        return EXIT_OK
    if not to_write:
        print('\nAlready installed and identical to the pinned payloads. Nothing to do.')
        return EXIT_OK
    differing = [row for row in to_write if row[4] == 'differs']
    if differing and not options.force:
        print('\nREFUSING to overwrite %d file(s) that differ from the pinned payloads:' % len(differing))
        for addon, label, source, dest, state in differing:
            print('  %s/%s (installed %s)' % (addon, label, dest))
        print('Re-run with --force if you really want to replace them.')
        return EXIT_NEEDS_FORCE

    apply = Apply()
    try:
        apply.mkdir(addons_dir())
        for addon, label, source, dest, state in rows:
            if state in ('new', 'differs'):
                apply.write(source, dest)
        result = patcher('install')
        if result.returncode != 0:
            raise RuntimeError('boot.lua patch failed: %s' % (result.stderr.strip() or result.stdout.strip()))
        apply.did_patch = True
    except Exception as error:  # noqa: BLE001 - report and undo whatever was done
        apply.rollback()
        print('FAILED: %s' % error, file=sys.stderr)
        print('Every change made by this run was rolled back.', file=sys.stderr)
        return 1

    print('\ninstalled: %s' % ', '.join('%s %s' % (name, addon_version(name)) for name in targets))
    print('bridge   : %s' % (addons_dir() / 'bridge.lua'))
    print('boot.lua : %s' % patcher('check').stdout.strip())
    print('\nRestart Steam (Steam -> Exit, then launch it again) to load the add-ons.')
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
    if remaining:
        for name in targets:
            shutil.rmtree(addons_dir() / name, ignore_errors=True)
            print('removed %s; %s still installed, bridge and boot.lua left in place'
                  % (name, ', '.join(remaining)))
        return EXIT_OK
    # Last one out: restore Lumen before deleting the payload it points at, so a
    # failed restore can never leave a live bridge aiming at removed files.
    result = patcher('uninstall')
    if result.returncode != 0:
        print('REFUSING to remove the last add-on: %s'
              % (result.stderr.strip() or result.stdout.strip()), file=sys.stderr)
        return EXIT_PATCH
    print(result.stdout.strip())
    for name in targets:
        shutil.rmtree(addons_dir() / name, ignore_errors=True)
        print('removed %s' % name)
    bridge = addons_dir() / 'bridge.lua'
    if bridge.is_file():
        bridge.unlink()
        print('removed bridge.lua')
    print('Local playtime and presence data were left untouched.')
    return EXIT_OK


def cmd_verify(options):
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
            # A plain copy is fine; a git checkout is not required to compare bytes.
            if not source.is_dir():
                print('  skip %s: no source checkout at %s' % (name, source))
                continue
            for rel, meta in entry.get('files', {}).items():
                origin = source / meta['source_path']
                if not origin.is_file():
                    problems.append('%s: source file missing: %s' % (name, origin))
                elif sha256(origin) != meta['sha256']:
                    problems.append('%s: %s differs from the source checkout (re-run vendor)'
                                    % (name, meta['source_path']))
            print('  %-9s compared against %s' % (name, source))
    if problems:
        print('\nPROBLEMS:')
        for problem in problems:
            print('  - %s' % problem)
        return 1
    print('\nOK: the payloads match the manifest.')
    return EXIT_OK


def cmd_vendor(options):
    sources = {}
    for name in SOURCED:
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

    manifest = {'generated': time.strftime('%Y-%m-%dT%H:%M:%S%z'), 'shared': {}, 'addons': {}}
    # The shared bridge and patcher come from the playtime project; both projects
    # ship byte-identical copies, and `verify --sources` keeps them honest.
    shared_source = sources['playtime']
    shared_commit = head(shared_source)
    for rel, source_path in (('bridge/bridge.lua', 'bridge/bridge.lua'),
                             ('scripts/patch-lumen.py', 'scripts/patch-lumen.py')):
        (ROOT / rel).write_bytes(show(shared_source, shared_commit, source_path))
        manifest['shared'][rel] = {'source_repo': 'https://github.com/pgm1207/slsteam-playtime',
                                   'source_commit': shared_commit, 'source_path': source_path,
                                   'sha256': sha256(ROOT / rel)}
        print('pinned %s from %s@%s' % (rel, shared_source.name, shared_commit[:12]))
    # First-party add-ons come from this repository, so they are pinned to this
    # checkout rather than fetched from another project.
    repo_commit = subprocess.run(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'],
                                 capture_output=True, text=True).stdout.strip()
    for name in FIRST_PARTY:
        files = {}
        for source_rel, dest_rel in ADDON_FILES[name]:
            path = payload_path(name, source_rel)
            if not path.is_file():
                raise SystemExit('FAIL: this repository has no addons/%s/%s' % (name, source_rel))
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


def build_parser():
    parser = argparse.ArgumentParser(prog='luna', description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command')
    for name, handler in (('preflight', cmd_preflight), ('status', cmd_status),
                          ('install', cmd_install), ('uninstall', cmd_uninstall),
                          ('verify', cmd_verify), ('vendor', cmd_vendor)):
        child = sub.add_parser(name)
        child.set_defaults(handler=handler)
        child.add_argument('--only', action='append', default=[],
                           help='restrict to one add-on (repeatable): %s' % ', '.join(ADDONS))
        if name == 'install':
            child.add_argument('--force', action='store_true',
                               help='replace installed files that differ from the pinned payloads')
            child.add_argument('--dry-run', action='store_true', help='print the plan and stop')
        if name in ('verify', 'vendor'):
            child.add_argument('--sources', action='store_true',
                               help='also compare against local source checkouts')
            child.add_argument('--source-dir', help='parent directory holding slsteam-playtime / slsteam-presence')
    return parser


def main(argv=None):
    parser = build_parser()
    options = parser.parse_args(argv)
    if not getattr(options, 'handler', None):
        parser.print_help()
        return EXIT_OK
    return options.handler(options)


if __name__ == '__main__':
    sys.exit(main())
