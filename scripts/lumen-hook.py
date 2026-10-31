#!/usr/bin/env python3
"""Reattach the installed Luna after Lumen updates; never reinstall the stack."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
import tempfile

BEGIN = '-- LUNA-POST-UPDATE-HOOK: begin v1'
END = '-- LUNA-POST-UPDATE-HOOK: end v1'
UNIT_MARK = '# Managed by Luna: Lumen update recovery v1'
DECORATOR = '''-- LUNA-POST-UPDATE-HOOK: begin v1
do
  local original_update_script = about.update_script
  function about.update_script(...)
    local script = original_update_script(...)
    local header = "#!/usr/bin/env bash\\n"
    local anchor = "status=$?\\n"
    if type(script) ~= 'string' or script:sub(1, #header) ~= header then
      error('Luna: unsupported Lumen update script; no update was started')
    end
    -- The installer supports this switch. Do not let it start Steam before
    -- Luna has reattached; the original terminal still asks for a restart.
    script = header .. "export SLS_NO_LAUNCH=1\\n" .. script:sub(#header + 1)
    local first, last = script:find(anchor, 1, true)
    if not first or script:find(anchor, last + 1, true) then
      error('Luna: unsupported Lumen updater status contract; no update was started')
    end
    local hook = [=[
if [ "$status" -eq 0 ]; then
  echo "Reattaching the installed Luna after the Lumen update..."
  python3 "$HOME/.local/share/slssteam-luna/lumen-hook.py" repair
  luna_status=$?
  if [ "$luna_status" -ne 0 ]; then
    echo "Base update finished, but Luna recovery needs attention. Leave Steam closed and run: luna hook repair"
    status="$luna_status"
  fi
fi
]=]
    return script:sub(1, last) .. hook .. script:sub(last + 1)
  end
end
-- LUNA-POST-UPDATE-HOOK: end v1
'''


def home():
    return Path(os.environ.get('HOME') or Path.home())


def store():
    # Outside Lumen: extracting a new Lumen release cannot overwrite the worker.
    return home() / '.local/share/slssteam-luna'


def digest(body):
    return hashlib.sha256(body).hexdigest()


def atomic(path, body, mode=0o600):
    if path.is_symlink():
        raise ValueError('refusing symlink: %s' % path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as out:
            out.write(body)
            out.flush()
            os.fsync(out.fileno())
        os.chmod(name, mode)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def read_config():
    path = store() / 'config.json'
    if not path.is_file() or path.is_symlink():
        raise ValueError('Luna update hook is not enabled')
    config = json.loads(path.read_text())
    if not isinstance(config, dict) or config.get('schema') != 1:
        raise ValueError('unsupported Luna update hook configuration')
    return config


def record(result, reason=''):
    payload = {'result': result, 'reason': reason}
    path = store() / 'status.json'
    body = (json.dumps(payload, indent=2, sort_keys=True) + '\n').encode()
    if not path.exists() or path.read_bytes() != body:
        atomic(path, body)
    print('Luna update hook: %s%s' % (result, ': ' + reason if reason else ''))


def updater_base(text):
    if BEGIN not in text and END not in text:
        return text
    if text.count(BEGIN) != 1 or text.count(END) != 1 or DECORATOR not in text:
        raise ValueError('Lumen updater hook was modified or is incomplete; refusing overwrite')
    return text.replace(DECORATOR, '', 1)


def patch_updater(text):
    base = updater_base(text)
    if base.count('function about.update_script(') != 1 or base.count('\nreturn about') != 1:
        raise ValueError('unsupported Lumen about.lua update hook contract')
    return base.replace('\nreturn about', '\n' + DECORATOR + 'return about', 1)


def patch_boot(patcher, text):
    updated = patcher['patch'](text)
    # The shared patcher treats its marker as idempotent. Recovery additionally
    # verifies the complete owned insertion so a partial/corrupt patch is not
    # mistaken for a healthy bridge.
    if (updated.count(patcher['MARK']) != 1 or updated.count(patcher['INSERT']) != 1
            or updated.count(patcher['CONTROL_NEW']) != 1
            or 'return sls_add_ui_assets(base)' not in updated
            or 'return sls_add_ui_assets({ polyfill = true, css = {}, js = js })' not in updated):
        raise ValueError('incomplete or modified Luna boot bridge; refusing overwrite')
    return updated


def running():
    for name in ('steam', 'steamwebhelper', 'lumen'):
        result = subprocess.run(['pgrep', '-u', str(os.getuid()), '-x', name],
                                capture_output=True, check=False)
        if result.returncode == 0:
            return name
        if result.returncode != 1:
            raise ValueError('could not reliably check running processes')
    return ''


def archive(path, body):
    dest = store() / 'backups' / (path.name + '.' + digest(body))
    if dest.exists():
        if dest.read_bytes() != body:
            raise ValueError('backup digest mismatch: %s' % dest)
    else:
        atomic(dest, body)


def repair(watch=False):
    config = read_config()
    if not config.get('enabled'):
        return 0
    root = home() / '.local/share/Lumen'
    bridge = root / 'sls-addons/bridge.lua'
    if bridge.is_symlink() or digest(bridge.read_bytes()) != config['bridge_sha256']:
        raise ValueError('installed bridge differs from the enrolled Luna; explicit re-enrolment required')
    for rel in ('sls-addons/luna/backend/main.lua', 'sls-addons/luna/public/panel.js'):
        if not (root / rel).is_file():
            raise ValueError('installed Luna payload missing: %s' % rel)
    patcher = runpy.run_path(str(store() / 'patch-lumen.py'))
    boot, about = root / 'lua/boot.lua', root / 'lua/about.lua'
    for path in (boot, about):
        if path.is_symlink():
            raise ValueError('refusing symlink: %s' % path)
    originals = {path: path.read_bytes() for path in (boot, about)}
    outputs = {boot: patch_boot(patcher, originals[boot].decode()),
               about: patch_updater(originals[about].decode())}
    changes = [path for path in (about, boot) if outputs[path].encode() != originals[path]]
    if not changes:
        record('current')
        return 0
    live = running()
    if live:
        record('deferred', '%s is running; close Steam and Lumen to finish recovery' % live)
        return 0 if watch else 2
    # Validate BOTH contracts before modifying either file. Recheck at commit so
    # an overlapping installer/user edit cannot silently be overwritten.
    for path in changes:
        archive(path, originals[path])
    original_backup = boot.with_name('boot.lua.sls-addons.original')
    if boot in changes:
        if original_backup.exists():
            archive(original_backup, original_backup.read_bytes())
        atomic(original_backup, originals[boot], boot.stat().st_mode & 0o777)
    written = []
    try:
        for path in changes:
            if running() or any(p.read_bytes() != originals[p] for p in originals if p not in written):
                raise ValueError('Lumen changed or started during recovery; refusing overwrite')
            atomic(path, outputs[path].encode(), path.stat().st_mode & 0o777)
            written.append(path)
    except Exception:
        for path in reversed(written):
            if path.read_bytes() == outputs[path].encode():
                atomic(path, originals[path], path.stat().st_mode & 0o777)
        raise
    record('repaired', 'installed Luna bridge restored and next-update hook armed; restart Steam')
    return 0


def units():
    directory = home() / '.config/systemd/user'
    return {name: directory / ('luna-lumen-hook.' + name) for name in ('service', 'path', 'timer')}


def systemctl(*args):
    return subprocess.run(['systemctl', '--user', *args], capture_output=True, text=True)


def enable():
    root = home() / '.local/share/Lumen'
    # Do not enrol an unknown/incomplete installation, or replace user add-ons.
    bridge = root / 'sls-addons/bridge.lua'
    for path in (bridge, root / 'sls-addons/luna/backend/main.lua', root / 'sls-addons/luna/public/panel.js'):
        if path.is_symlink() or not path.is_file():
            raise ValueError('installed Luna is missing or symlinked: %s' % path)
    patch_updater((root / 'lua/about.lua').read_text())
    if store().is_symlink():
        raise ValueError('refusing symlinked hook directory')
    store().mkdir(parents=True, exist_ok=True, mode=0o700)
    source = Path(__file__).resolve()
    patcher = source.with_name('patch-lumen.py')
    patch_boot(runpy.run_path(str(patcher)), (root / 'lua/boot.lua').read_text())
    for path in units().values():
        if path.exists() and UNIT_MARK not in path.read_text():
            raise ValueError('refusing to replace a custom systemd unit: %s' % path)
    atomic(store() / 'lumen-hook.py', source.read_bytes(), 0o700)
    atomic(store() / 'patch-lumen.py', patcher.read_bytes(), 0o600)
    atomic(store() / 'config.json', (json.dumps({'schema': 1, 'enabled': True,
           'bridge_sha256': digest(bridge.read_bytes())}, indent=2) + '\n').encode())
    # systemd specifiers/quotes are escaped; the fixed %h path also handles a
    # HOME containing spaces without interpolating it into an ExecStart token.
    python = (shutil.which('python3') or sys.executable).replace('%', '%%').replace('\\', '\\\\').replace('"', '\\"')
    texts = {
        'service': '[Unit]\nDescription=Reattach installed Luna after a Lumen update\n'
                   'StartLimitIntervalSec=60\nStartLimitBurst=5\n\n[Service]\nType=oneshot\n'
                   'ExecStart="%s" "%%h/.local/share/slssteam-luna/lumen-hook.py" repair --watch\n' % python,
        'path': '[Unit]\nDescription=Recover Luna when the base stack finishes updating\n\n[Path]\n'
                'PathChanged=%h/.local/share/Lumen/versions.json\nUnit=luna-lumen-hook.service\n\n'
                '[Install]\nWantedBy=default.target\n',
        'timer': '[Unit]\nDescription=Fallback for missed Luna/Lumen update hooks\n\n[Timer]\n'
                 'OnStartupSec=30\nOnUnitActiveSec=60\nUnit=luna-lumen-hook.service\n\n'
                 '[Install]\nWantedBy=timers.target\n',
    }
    for kind, path in units().items():
        atomic(path, (UNIT_MARK + '\n' + texts[kind]).encode(), 0o644)
    try:
        systemctl('daemon-reload')
        result = systemctl('enable', '--now', 'luna-lumen-hook.path', 'luna-lumen-hook.timer')
        if result.returncode:
            print('WARNING: external fallback unavailable; terminal post-update hook still works: ' + result.stderr.strip())
    except OSError as error:
        print('WARNING: systemd fallback unavailable: %s' % error)
    code = locked_repair()
    # Enrolment can be armed while Steam runs; the worker never kills it.
    return 0 if code == 2 else code


def disable():
    config = read_config()
    path = home() / '.local/share/Lumen/lua/about.lua'
    original = path.read_text()
    restored = updater_base(original)
    if original != restored and running():
        raise ValueError('close Steam and Lumen before disabling the updater hook')
    for unit in units().values():
        if unit.exists() and UNIT_MARK not in unit.read_text():
            raise ValueError('refusing to remove a custom systemd unit: %s' % unit)
    config['enabled'] = False
    atomic(store() / 'config.json', (json.dumps(config, indent=2) + '\n').encode())
    try:
        systemctl('disable', '--now', 'luna-lumen-hook.path', 'luna-lumen-hook.timer')
        systemctl('stop', 'luna-lumen-hook.service')
    except OSError:
        pass
    if original != restored:
        archive(path, original.encode())
        if path.read_text() != original:
            raise ValueError('Lumen updater changed during hook removal')
        atomic(path, restored.encode(), path.stat().st_mode & 0o777)
    for unit in units().values():
        if unit.exists():
            unit.unlink()
    try:
        systemctl('daemon-reload')
    except OSError:
        pass
    record('disabled', 'backups and installed add-ons retained')
    return 0


def locked_repair(watch=False):
    path = store() / 'repair.lock'
    if path.is_symlink():
        raise ValueError('refusing symlinked repair lock')
    with path.open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('Luna update hook: another recovery is running')
            return 0 if watch else 2
        return repair(watch)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['enable', 'disable', 'repair', 'status'])
    parser.add_argument('--watch', action='store_true')
    options = parser.parse_args()
    try:
        if options.action == 'enable':
            return enable()
        if options.action == 'disable':
            return disable()
        if options.action == 'status':
            print(json.dumps({'config': read_config(), 'status': json.loads((store() / 'status.json').read_text())}, sort_keys=True))
            return 0
        return locked_repair(options.watch)
    except (OSError, ValueError, KeyError) as error:
        if store().is_dir() and not store().is_symlink():
            record('error', str(error))
        else:
            print('Luna update hook: error: %s' % error, file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
