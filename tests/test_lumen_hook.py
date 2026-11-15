"""Isolated post-update recovery; no live Steam, downloads or systemd changes."""
import fcntl
import json
import os
os.environ.setdefault('LUNA_GUI_CHECKS', '0')  # hermetic: tests never probe the real desktop (test_gui_check manages it itself)
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from test_luna import UPSTREAM, stub_bin

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / 'scripts/lumen-hook.py'
ABOUT = '''local about = {}
function about.update_script(install_url, flag)
  return table.concat({
    "#!/usr/bin/env bash", "set -u",
    'bash "' .. install_url .. '" ' .. (flag or ''),
    "status=$?", 'echo "original status: $status"', 'exit "$status"', ""
  }, "\\n")
end
return about
'''


class LumenHookTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='luna-hook-', dir=os.environ.get('TMPDIR'))
        self.home = Path(self.temp.name) / 'home with spaces'
        self.root = self.home / '.local/share/Lumen'
        self.store = self.home / '.local/share/slssteam-luna'
        self.boot = self.root / 'lua/boot.lua'
        self.about = self.root / 'lua/about.lua'
        for path, text in {
            self.boot: UPSTREAM, self.about: ABOUT,
            self.root / 'sls-addons/bridge.lua': '-- installed bridge; do not replace',
            self.root / 'sls-addons/luna/backend/main.lua': '-- installed backend',
            self.root / 'sls-addons/luna/public/panel.js': '// installed panel',
            self.root / 'sls-addons/presence/public/agent.js': '// retained local hotfix',
            self.home / '.local/share/slssteam-integrations/presence.json': '{"owned":{}}',
            self.home / '.local/share/Steam/steam.cfg': 'custom-policy\n',
            self.home / '.local/share/Steam/config/config.vdf': 'custom Proton choice\n',
        }.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        self.bin = stub_bin(self.home)
        self.env = dict(os.environ, HOME=str(self.home),
                        PATH=str(self.bin) + os.pathsep + os.environ.get('PATH', ''))

    def tearDown(self):
        self.temp.cleanup()

    def run_hook(self, action, *args, copied=False, running=''):
        stub_bin(self.home, running)
        script = self.store / 'lumen-hook.py' if copied else HELPER
        return subprocess.run([sys.executable, str(script), action, *args],
                              env=self.env, capture_output=True, text=True, timeout=20)

    def enable(self):
        result = self.run_hook('enable')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def generated_script(self, installer, flags=''):
        lua = shutil.which('lua5.4') or shutil.which('lua')
        self.assertIsNotNone(lua, 'Lua is required by the full local gate')
        expression = 'local m=dofile(%s); io.write(m.update_script(%s,%s))' % (
            json.dumps(str(self.about)), json.dumps(str(installer)), json.dumps(flags))
        result = subprocess.run([lua, '-'], input=expression, env=self.env,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_enable_repairs_boot_and_installs_an_external_self_rearming_hook(self):
        self.enable()
        self.assertEqual(self.boot.read_text().count('SLS-ADDONS-INTEGRATION'), 1)
        self.assertEqual(self.about.read_text().count('-- LUNA-POST-UPDATE-HOOK: begin'), 1)
        self.assertTrue((self.store / 'lumen-hook.py').is_file())
        self.assertTrue((self.store / 'patch-lumen.py').is_file())
        self.assertEqual(self.store.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.boot.parent / 'boot.lua.sls-addons.original').read_text(), UPSTREAM)

    def test_repair_never_replaces_installed_payloads_or_user_policy(self):
        protected = [p for base in (self.root / 'sls-addons', self.home / '.local/share/Steam',
                                   self.home / '.local/share/slssteam-integrations')
                     for p in base.rglob('*') if p.is_file()]
        before = {p: p.read_bytes() for p in protected}
        self.enable()
        self.assertEqual(before, {p: p.read_bytes() for p in protected})

    def test_standalone_worker_recovers_repeated_updates_without_source_checkout(self):
        self.enable()
        for suffix in ('\n-- upstream 2', '\n-- upstream 3', ''):
            self.boot.write_text(UPSTREAM + suffix)
            self.about.write_text(ABOUT)
            result = self.run_hook('repair', copied=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('SLS-ADDONS-INTEGRATION', self.boot.read_text())
            self.assertIn('LUNA-POST-UPDATE-HOOK', self.about.read_text())
            self.assertEqual((self.boot.parent / 'boot.lua.sls-addons.original').read_text(), UPSTREAM + suffix)

    def test_idempotent_repairs_do_not_rewrite_lumen_or_status(self):
        self.enable()
        self.run_hook('repair', copied=True)  # settle status to 'current'
        files = [self.boot, self.about, self.store / 'status.json']
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in files}
        result = self.run_hook('repair', copied=True)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(before, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in files})

    def test_unknown_boot_contract_never_modifies_either_lumen_file(self):
        self.enable()
        self.boot.write_text('-- incompatible upstream')
        self.about.write_text(ABOUT)
        result = self.run_hook('repair', copied=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.boot.read_text(), '-- incompatible upstream')
        self.assertEqual(self.about.read_text(), ABOUT)

    def test_unknown_updater_contract_never_modifies_boot(self):
        self.enable()
        self.boot.write_text(UPSTREAM)
        self.about.write_text('return {}\n')
        result = self.run_hook('repair', copied=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.boot.read_text(), UPSTREAM)
        self.assertEqual(self.about.read_text(), 'return {}\n')

    def test_a_partial_bridge_marker_does_not_count_as_healthy(self):
        self.enable()
        broken = '-- SLS-ADDONS-INTEGRATION: 2026-10\n' + UPSTREAM
        self.boot.write_text(broken)
        self.about.write_text(ABOUT)
        result = self.run_hook('repair', copied=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('incomplete', result.stdout)
        self.assertEqual(self.boot.read_text(), broken)
        self.assertEqual(self.about.read_text(), ABOUT)

    def test_running_steam_defers_without_killing_or_modifying_it(self):
        result = self.run_hook('enable', running='steam')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.boot.read_text(), UPSTREAM)
        self.assertEqual(self.about.read_text(), ABOUT)
        self.assertEqual(json.loads((self.store / 'status.json').read_text())['result'], 'deferred')
        result = self.run_hook('repair', '--watch', copied=True, running='lumen')
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.boot.read_text(), UPSTREAM)
        self.assertEqual(self.run_hook('repair', copied=True).returncode, 0)

    def test_external_watch_units_cover_completion_and_missed_events(self):
        self.enable()
        units = self.home / '.config/systemd/user'
        self.assertIn('PathChanged=%h/.local/share/Lumen/versions.json',
                      (units / 'luna-lumen-hook.path').read_text())
        self.assertIn('OnUnitActiveSec=60', (units / 'luna-lumen-hook.timer').read_text())
        service = (units / 'luna-lumen-hook.service').read_text()
        self.assertIn('"%h/.local/share/slssteam-luna/lumen-hook.py" repair --watch', service)
        self.assertNotIn(' install', service.split('ExecStart=', 1)[1])

    def test_custom_watch_unit_is_never_overwritten(self):
        unit = self.home / '.config/systemd/user/luna-lumen-hook.service'
        unit.parent.mkdir(parents=True)
        unit.write_text('custom unit')
        result = self.run_hook('enable')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(unit.read_text(), 'custom unit')
        self.assertEqual(self.boot.read_text(), UPSTREAM)

    def test_custom_luna_command_is_not_overwritten_by_tooling_install(self):
        launcher = self.home / '.local/bin/luna'
        launcher.parent.mkdir(parents=True)
        launcher.write_bytes(b'\x7fELFcustom command\xff')
        namespace = runpy.run_path(str(ROOT / 'scripts/luna.py'))
        with patch.dict(os.environ, self.env):
            namespace['write_tooling']()
        self.assertEqual(launcher.read_bytes(), b'\x7fELFcustom command\xff')

    def test_changed_bridge_requires_explicit_reenrolment(self):
        self.enable()
        bridge = self.root / 'sls-addons/bridge.lua'
        bridge.write_text('-- different user bridge')
        self.boot.write_text(UPSTREAM)
        result = self.run_hook('repair', copied=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.boot.read_text(), UPSTREAM)
        self.assertEqual(bridge.read_text(), '-- different user bridge')

    def test_lock_serializes_repairs(self):
        self.enable()
        self.boot.write_text(UPSTREAM)
        with (self.store / 'repair.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.run_hook('repair', '--watch', copied=True)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(self.boot.read_text(), UPSTREAM)
        self.assertEqual(self.run_hook('repair', copied=True).returncode, 0)

    def test_disable_keeps_addons_and_user_changes_outside_its_block(self):
        self.enable()
        self.about.write_text('-- user note\n' + self.about.read_text())
        result = self.run_hook('disable', copied=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.about.read_text(), '-- user note\n' + ABOUT)
        self.assertIn('SLS-ADDONS-INTEGRATION', self.boot.read_text())
        self.assertFalse((self.home / '.config/systemd/user/luna-lumen-hook.timer').exists())
        before = self.boot.read_bytes()
        self.assertEqual(self.run_hook('repair', copied=True).returncode, 0)
        self.assertEqual(self.boot.read_bytes(), before)

    def test_tampered_hook_block_is_not_silently_replaced(self):
        self.enable()
        text = self.about.read_text().replace('local original_update_script', 'local modified_update_script', 1)
        self.about.write_text(text)
        self.boot.write_text(UPSTREAM)
        result = self.run_hook('repair', copied=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.about.read_text(), text)
        self.assertEqual(self.boot.read_text(), UPSTREAM)

    def test_successful_menu_updates_repair_before_original_completion_and_rearm_twice(self):
        self.enable()
        (self.home / 'pristine-boot').write_text(UPSTREAM)
        (self.home / 'pristine-about').write_text(ABOUT)
        installer = self.home / 'fake-installer.sh'
        installer.write_text('''#!/bin/sh
set -eu
[ "$SLS_NO_LAUNCH" = 1 ]
[ "$1" = --noplugin ]
cp "$HOME/pristine-boot" "$HOME/.local/share/Lumen/lua/boot.lua"
cp "$HOME/pristine-about" "$HOME/.local/share/Lumen/lua/about.lua"
''')
        for _ in range(2):
            script = self.generated_script(installer, '--noplugin')
            self.assertTrue(script.startswith('#!/usr/bin/env bash\n'))
            result = subprocess.run(['bash'], input=script, env=self.env,
                                    capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertLess(result.stdout.index('installed Luna bridge restored'), result.stdout.index('original status:'))
            self.assertEqual(self.boot.read_text().count('SLS-ADDONS-INTEGRATION'), 1)
            self.assertEqual(self.about.read_text().count('-- LUNA-POST-UPDATE-HOOK: begin'), 1)

    def test_failed_base_update_does_not_claim_or_attempt_luna_recovery(self):
        self.enable()
        installer = self.home / 'failed-installer.sh'
        installer.write_text('exit 7\n')
        before = {p: p.read_bytes() for p in (self.boot, self.about)}
        result = subprocess.run(['bash'], input=self.generated_script(installer),
                                env=self.env, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 7)
        self.assertNotIn('Reattaching', result.stdout)
        self.assertEqual(before, {p: p.read_bytes() for p in before})

    def test_failed_luna_repair_after_a_successful_base_update_is_not_reported_as_success(self):
        self.enable()
        installer = self.home / 'incompatible-installer.sh'
        installer.write_text('printf "%s\\n" "-- incompatible upstream" > "$HOME/.local/share/Lumen/lua/boot.lua"\n')
        result = subprocess.run(['bash'], input=self.generated_script(installer),
                                env=self.env, capture_output=True, text=True, timeout=20)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Luna recovery needs attention', result.stdout)
        self.assertNotIn('installed Luna bridge restored', result.stdout)

    def test_a_commit_failure_rolls_back_the_other_lumen_file(self):
        self.enable()
        self.boot.write_text(UPSTREAM)
        self.about.write_text(ABOUT)
        namespace = runpy.run_path(str(HELPER))
        repair = namespace['repair']
        write = namespace['atomic']

        def fail_boot(path, *args, **kwargs):
            if path == self.boot:
                raise OSError('simulated disk error')
            return write(path, *args, **kwargs)

        repair.__globals__['atomic'] = fail_boot
        with patch.dict(os.environ, self.env), self.assertRaisesRegex(OSError, 'disk error'):
            repair()
        self.assertEqual(self.boot.read_text(), UPSTREAM)
        self.assertEqual(self.about.read_text(), ABOUT)

    def test_removing_only_luna_disarms_recovery_without_removing_other_addons(self):
        self.enable()
        backend = self.root / 'luatools/backend/main.lua'
        backend.parent.mkdir(parents=True)
        backend.write_text('-- fixture')
        # A second discoverable add-on keeps the shared bridge in use.
        other = self.root / 'sls-addons/presence/backend/main.lua'
        other.parent.mkdir(parents=True)
        other.write_text('-- fixture')
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/luna.py'),
                                 'uninstall', '--only', 'luna'], env=self.env,
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(other.is_file())
        self.assertIn('SLS-ADDONS-INTEGRATION', self.boot.read_text())
        self.assertNotIn('LUNA-POST-UPDATE-HOOK', self.about.read_text())
        self.assertFalse(json.loads((self.store / 'config.json').read_text())['enabled'])

    def test_normal_installer_arms_hook_and_full_uninstall_disables_it(self):
        backend = self.root / 'luatools/backend/main.lua'
        backend.parent.mkdir(parents=True)
        backend.write_text('-- fixture')
        command = [sys.executable, str(ROOT / 'scripts/luna.py')]
        installed = subprocess.run(command + ['install', '--force', '--keep-steam-client', '--no-steam-machine'],
                                   env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        self.assertIn('LUNA-POST-UPDATE-HOOK', self.about.read_text())
        status = subprocess.run(command + ['hook', 'status'], env=self.env,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(status.returncode, 0, status.stdout + status.stderr)
        self.assertTrue(json.loads(status.stdout)['config']['enabled'])
        launcher = self.home / '.local/bin/luna'
        self.assertTrue(launcher.is_file())
        standalone_tool = self.root / 'sls-luna/luna.py'
        saved_tool = standalone_tool.read_bytes()
        standalone_tool.unlink()
        independent = subprocess.run([str(launcher), 'hook', 'status'], env=self.env,
                                     capture_output=True, text=True, timeout=10)
        self.assertEqual(independent.returncode, 0, independent.stdout + independent.stderr)
        standalone_tool.write_bytes(saved_tool)
        uninstalled = subprocess.run(command + ['uninstall'], env=self.env,
                                     capture_output=True, text=True, timeout=30)
        self.assertEqual(uninstalled.returncode, 0, uninstalled.stdout + uninstalled.stderr)
        self.assertNotIn('SLS-ADDONS-INTEGRATION', self.boot.read_text())
        self.assertNotIn('LUNA-POST-UPDATE-HOOK', self.about.read_text())
        self.assertFalse(json.loads((self.store / 'config.json').read_text())['enabled'])
        self.assertFalse(launcher.exists())


if __name__ == '__main__':
    unittest.main()
