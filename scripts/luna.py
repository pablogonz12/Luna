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
    luna release [--out DIR] [--publish]  build a bundle tarball (+ sha256) for other machines
    luna update --bundle FILE | --url URL | --latest [--only NAME] [--dry-run]
                                        verify and install a released bundle
"""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
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


def luna_tools_dir():
    """Machine-local Luna tooling: the standalone updater copy and its units.

    It sits under the Lumen directory on purpose: that tree is excluded from the
    user's Syncthing folders, so two machines never fight over the copies.
    """
    return lumen_root() / 'sls-luna'


def read_json_file(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def write_json_file(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + '\n')
    return path


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
        if state == 'new' and addon in installed:
            # The directory claims the add-on is present but an expected
            # file is missing: that is corruption, not a fresh install.
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
    version = addon_version('luna')
    out_dir = Path(options.out).expanduser() if options.out else ROOT / 'dist'
    out_dir.mkdir(parents=True, exist_ok=True)
    name = 'luna-%s' % version
    bundle = out_dir / (name + '.tar.gz')
    entries = [
        ('addons', ROOT / 'addons'),
        ('vendor/MANIFEST.json', MANIFEST_PATH),
        ('scripts/luna.py', ROOT / 'scripts' / 'luna.py'),
        ('scripts/patch-lumen.py', PATCHER),
        ('bridge/bridge.lua', BRIDGE_SOURCE),
    ]
    readme = ROOT / 'README.md'
    if readme.is_file():
        entries.append(('README.md', readme))
    changelog = ROOT / 'CHANGELOG.md'
    if changelog.is_file():
        entries.append(('CHANGELOG.md', changelog))
    with tarfile.open(bundle, 'w:gz') as tar:
        for arcname, path in entries:
            tar.add(path, arcname='%s/%s' % (name, arcname), recursive=path.is_dir())
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
    try:
        lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(lock_fd)
    except FileExistsError:
        print('REFUSING: another update is already running (%s)' % lock_path, file=sys.stderr)
        return 1
    except OSError:
        pass
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
        try:
            lock_path.unlink()
        except OSError:
            pass


LUNA_REPO = 'pgm1207/Luna'
UPDATE_TTL = 6 * 3600


def gh_json(args):
    gh = find_gh()
    if not gh:
        return None, 'the gh CLI is not installed (update checks use your own GitHub auth)'
    result = subprocess.run([gh] + args, capture_output=True, text=True)
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


def check_updates(force=False):
    """Compare the installed version with the newest release; cached for 6h."""
    cache_path = luna_tools_dir() / 'update-check.json'
    cached = read_json_file(cache_path) or {}
    now = int(time.time())
    installed = installed_version()
    if (not force and cached.get('latest') and cached.get('checkedAt')
            and now - int(cached['checkedAt']) < UPDATE_TTL):
        cached['installed'] = installed
        cached['available'] = version_key(cached.get('latest')) > version_key(installed)
        return cached
    latest, error = latest_release()
    report = {
        'installed': installed,
        'latest': latest,
        'checkedAt': now,
        'checkError': error,
        'available': bool(latest) and version_key(latest) > version_key(installed),
    }
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
            _safe_extract(bundle, extract_dir)
        except (tarfile.TarError, ValueError, OSError) as error:
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
        problems = _manifest_problems(manifest, root)
        if problems:
            print('REFUSING: the bundle contents do not match its own manifest:')
            for problem in problems:
                print('  - %s' % problem, file=sys.stderr)
            return 1
        print('bundle : %s (Luna %s, manifest verified)' % (bundle.name, version))
        command = [sys.executable, str(script), 'install', '--force']
        for name in only or []:
            command += ['--only', name]
        if dry_run:
            command.append('--dry-run')
        result = subprocess.run(command)
        return result.returncode


def download_release(tag, work):
    """Fetch a release's bundle + checksum with the user's gh auth."""
    gh = find_gh()
    if not gh:
        return None, 'the gh CLI is not installed'
    if not tag:
        return None, 'no release tag to download'
    result = subprocess.run([gh, 'release', 'download', tag,
                             '--pattern', 'luna-*.tar.gz',
                             '--pattern', 'luna-*.tar.gz.sha256',
                             '--dir', str(work), '--clobber', '-R', LUNA_REPO],
                            capture_output=True, text=True)
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
    """Single-writer lock so timer, menu and shell never update concurrently."""
    tools = luna_tools_dir()
    try:
        tools.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return tools / 'update.lock'


def write_tooling():
    """Keep a standalone updater copy the timer can run without a checkout.

    Called both by the installer (from a checkout) and by the copy itself, so
    the source is *this* file, never a fixed path relative to it.
    """
    tools = luna_tools_dir()
    tools.mkdir(parents=True, exist_ok=True)
    target = tools / 'luna.py'
    source = Path(__file__).resolve()
    try:
        if source.samefile(target):
            return target
    except OSError:
        pass
    shutil.copy2(source, target)
    target.chmod(0o755)
    return target


def unit_paths():
    unit_dir = home() / '.config/systemd/user'
    return unit_dir, unit_dir / 'luna-autoupdate.service', unit_dir / 'luna-autoupdate.timer'


def cmd_check(options):
    """Print the update check as JSON (the Luna menu reads it)."""
    print(json.dumps(check_updates(force=getattr(options, 'force', False)), sort_keys=True))
    return EXIT_OK


def cmd_timer(options):
    """Enable, disable or report the unattended-update timer."""
    unit_dir, service, timer = unit_paths()
    if options.action == 'status':
        enabled = subprocess.run(['systemctl', '--user', 'is-enabled', 'luna-autoupdate.timer'],
                                 capture_output=True, text=True)
        active = subprocess.run(['systemctl', '--user', 'is-active', 'luna-autoupdate.timer'],
                                capture_output=True, text=True)
        print(json.dumps({'enabled': enabled.stdout.strip() or 'unknown',
                          'active': active.stdout.strip() or 'unknown',
                          'units': str(unit_dir)}, sort_keys=True))
        return EXIT_OK
    if options.action == 'on':
        write_tooling()
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
        subprocess.run(['systemctl', '--user', 'daemon-reload'], capture_output=True)
        result = subprocess.run(['systemctl', '--user', 'enable', '--now', 'luna-autoupdate.timer'],
                                capture_output=True, text=True)
        if result.returncode != 0:
            print('FAILED: %s' % (result.stderr.strip() or result.stdout.strip()), file=sys.stderr)
            return 1
        print('unattended updates: on (%s)' % timer)
        return EXIT_OK
    if options.action == 'off':
        subprocess.run(['systemctl', '--user', 'disable', '--now', 'luna-autoupdate.timer'],
                       capture_output=True)
        for path in (timer, service):
            if path.exists():
                path.unlink()
        subprocess.run(['systemctl', '--user', 'daemon-reload'], capture_output=True)
        print('unattended updates: off')
        return EXIT_OK
    print('unknown timer action: %s' % options.action, file=sys.stderr)
    return 2


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
    lock_path = _update_lock()
    prefs = read_json_file(data_dir() / 'luna.json') or {}
    mode = str(prefs.get('auto_update') or 'off')
    pending = read_json_file(pending_path) or {}
    verbose = bool(getattr(options, 'verbose', False))

    def record(payload):
        payload['mode'] = mode
        payload['at'] = int(time.time())
        write_json_file(result_path, payload)
        if verbose:
            print(json.dumps(payload, sort_keys=True))
        return EXIT_OK

    if mode not in ('notify', 'install') and not pending:
        return EXIT_OK
    try:
        lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(lock_fd)
    except FileExistsError:
        return record({'result': 'deferred', 'reason': 'another update is already running'})
    except OSError:
        pass
    try:
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
        finally:
            shutil.rmtree(work, ignore_errors=True)
        if code == 0 and pending_path.exists():
            pending_path.unlink()
        return record({'result': 'applied' if code == 0 else 'failed',
                       'version': target, 'installedBefore': installed, 'exit': code})
    finally:
        try:
            lock_path.unlink()
        except OSError:
            pass


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
        write_install_snapshot()
        write_tooling()
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

    write_install_snapshot()
    write_tooling()
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
    snapshot = addons_dir() / 'luna-manifest.json'
    if snapshot.is_file():
        snapshot.unlink()
    # A full uninstall must not leave the unattended worker behind: without
    # the payloads it would only fail, and with them gone a timer could
    # reinstall after the user asked for removal.
    try:
        unit_dir, service, timer = unit_paths()
        subprocess.run(['systemctl', '--user', 'disable', '--now', 'luna-autoupdate.timer'],
                       capture_output=True)
        for path in (timer, service):
            try:
                if path.exists():
                    path.unlink()
            except OSError:
                pass
        subprocess.run(['systemctl', '--user', 'daemon-reload'], capture_output=True)
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
                          ('verify', cmd_verify), ('vendor', cmd_vendor),
                          ('release', cmd_release), ('update', cmd_update),
                          ('check', cmd_check), ('autoupdate', cmd_autoupdate),
                          ('timer', cmd_timer)):
        child = sub.add_parser(name)
        child.set_defaults(handler=handler)
        child.add_argument('--only', action='append', default=[],
                           help='restrict to one add-on (repeatable): %s' % ', '.join(ADDONS))
        if name == 'install':
            child.add_argument('--force', action='store_true',
                               help='replace installed files that differ from the pinned payloads')
            child.add_argument('--dry-run', action='store_true', help='print the plan and stop')
        if name == 'check':
            child.add_argument('--force', action='store_true', help='ignore the 6h cache')
        if name == 'autoupdate':
            child.add_argument('--verbose', action='store_true', help='print the decision')
        if name == 'timer':
            child.add_argument('action', choices=['on', 'off', 'status'],
                               help='enable, disable or report the unattended-update timer')
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
