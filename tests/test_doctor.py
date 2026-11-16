#!/usr/bin/env python3
"""`luna doctor` tests: a fake HOME/Steam tree per check, and --fix safety."""
import hashlib
import json
import os
os.environ.setdefault('LUNA_GUI_CHECKS', '0')  # hermetic: tests never probe the real desktop (test_gui_check manages it itself)
os.environ.setdefault('LUNA_LIVE_CHECKS', '0')  # hermetic: tests never contact a real Steam (test_live_probe uses a fake endpoint)
import stat
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_luna  # noqa: E402


class DoctorTestCase(test_luna.LunaTestCase):
    def setUp(self):
        super().setUp()
        self.steam = self.home / '.local/share/Steam'
        self.steam.mkdir(parents=True)
        self.tools = self.home / '.local/share/Lumen/sls-luna'
        self.tools.mkdir(parents=True)

    def doctor(self, *args, **kwargs):
        return self.luna('doctor', '--json', *args, **kwargs)

    def findings(self, *args, **kwargs):
        result = self.doctor(*args, **kwargs)
        return {f['id']: f for f in json.loads(result.stdout)['findings']}, result

    def pin(self, **extra):
        (self.tools / 'steam-pin.json').write_text(json.dumps(dict({'clientBuild': '1'}, **extra)))

    def read_only_package(self):
        package = self.steam / 'package'
        package.mkdir()
        package.chmod(0o555)
        self.addCleanup(lambda: package.exists() and package.chmod(0o755))
        return package

    def sha(self, path):
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()

    # -- baseline ---------------------------------------------------------
    def test_clean_machine_exits_zero_and_json_shape(self):
        found, result = self.findings()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(json.loads(result.stdout)['version'], 1)
        for item in found.values():
            self.assertIn(item['severity'], ('ok', 'info'))
            self.assertEqual(set(item), {'id', 'severity', 'message', 'fix', 'fixable'})

    def test_human_table(self):
        (self.steam / 'steam.cfg').write_text('BootStrapperInhibitAll=enable\n')
        result = self.luna('doctor')
        self.assertEqual(result.returncode, 1)
        self.assertIn('WARN', result.stdout)
        self.assertIn('steam.foreign-lock', result.stdout)
        self.assertIn('fix:', result.stdout)

    # -- steam lock -------------------------------------------------------
    def test_foreign_lock_read_only_package(self):
        self.read_only_package()
        found, result = self.findings()
        self.assertEqual(result.returncode, 1)
        self.assertEqual(found['steam.foreign-lock']['severity'], 'warn')
        self.assertFalse(found['steam.foreign-lock']['fixable'])

    def adoptable_lock(self, build='1'):
        """`luna install` over a manual lock: pin + cfg block + read-only package/, no hardLock."""
        package = self.read_only_package()
        (self.steam / 'logs').mkdir()
        (self.steam / 'logs/console_log.txt').write_text('Client version: %s\n' % build)
        (self.steam / 'steam.cfg').write_text('BootStrapperInhibitAll=enable\n')
        self.pin()
        return package

    def test_foreign_lock_is_adoptable_when_packages_match_the_pin(self):
        self.adoptable_lock()
        found, result = self.findings()
        item = found['steam.foreign-lock']
        self.assertTrue(item['fixable'])
        self.assertIn('luna steam lock --adopt', item['fix'])

    def test_foreign_lock_is_not_adoptable_when_build_differs_from_pin(self):
        self.adoptable_lock(build='2')
        found, _ = self.findings()
        self.assertFalse(found['steam.foreign-lock']['fixable'])

    def test_fix_adopts_the_lock_without_touching_steam(self):
        package = self.adoptable_lock()
        cfg = (self.steam / 'steam.cfg').read_bytes()
        found, result = self.findings('--fix')
        self.assertEqual(result.returncode, 0, result.stdout)
        pin = json.loads((self.tools / 'steam-pin.json').read_text())
        self.assertIsInstance(pin['hardLock'], dict)
        self.assertEqual(pin['clientBuild'], '1')
        self.assertEqual(stat.S_IMODE(package.stat().st_mode) & 0o222, 0)
        self.assertTrue(cfg in (self.steam / 'steam.cfg').read_bytes())
        found, result = self.findings()
        self.assertNotIn('steam.foreign-lock', found)
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_foreign_lock_cfg_without_pin(self):
        (self.steam / 'steam.cfg').write_text('BootStrapperInhibitAll=enable\n')
        found, _ = self.findings()
        self.assertIn('steam.foreign-lock', found)

    def test_luna_soft_block_is_not_foreign(self):
        self.pin()
        (self.steam / 'steam.cfg').write_text('BootStrapperInhibitAll=enable\n'
                                              'BootStrapperForceSelfUpdate=disable\n')
        found, result = self.findings()
        self.assertNotIn('steam.foreign-lock', found)
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_luna_hard_lock_is_not_foreign(self):
        self.pin(hardLock={'at': 1})
        block = ('BootStrapperInhibitAll=enable\nBootStrapperInhibitUpdateOnLaunch=enable\n'
                 'BootStrapperForceSelfUpdate=false\n')
        (self.steam / 'steam.cfg').write_text(block)
        (self.steam / 'Steam.cfg').write_text(block)
        self.read_only_package()
        found, result = self.findings()
        self.assertNotIn('steam.foreign-lock', found)
        self.assertNotIn('steam.lock-missing', found)
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_pin_without_cfg_is_fixable_and_fix_works(self):
        self.pin()
        found, result = self.findings()
        self.assertTrue(found['steam.lock-missing']['fixable'])
        self.assertEqual(result.returncode, 1)
        found, result = self.findings('--fix')
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn('BootStrapperInhibitAll=enable', (self.steam / 'steam.cfg').read_text())
        self.assertNotIn('steam.lock-missing', self.findings()[0])

    def test_hard_lock_missing_cfg_fix_restores_both_files(self):
        self.pin(hardLock={'at': 1})
        self.read_only_package()
        found, _ = self.findings()
        self.assertTrue(found['steam.lock-missing']['fixable'])
        self.findings('--fix')
        self.assertTrue((self.steam / 'steam.cfg').is_file())
        self.assertTrue((self.steam / 'Steam.cfg').is_file())

    def test_fix_prints_its_own_result_not_another_fix_hint(self):
        self.adoptable_lock()
        apps = self.home / '.local/share/applications'
        apps.mkdir(parents=True)
        launcher = apps / 'a.desktop'
        launcher.write_text('x')
        (self.tools / 'steam-machine-shortcuts.json').write_text(json.dumps(
            {'files': {str(launcher): {'sha256': '0' * 64}}, 'profiles': {}}))
        out = self.luna('doctor', '--fix').stdout.splitlines()
        at = next(i for i, l in enumerate(out) if l.startswith('FIXED steam.foreign-lock'))
        self.assertIn('result: adopted the existing read-only Steam lock for build 1', out[at + 1])
        self.assertNotIn('fix:', out[at + 1])

    def test_fix_never_touches_foreign_lock(self):
        package = self.read_only_package()
        cfg = self.steam / 'steam.cfg'
        cfg.write_text('BootStrapperInhibitAll=enable\n')
        before = cfg.read_bytes()
        self.doctor('--fix')
        self.assertEqual(cfg.read_bytes(), before)
        self.assertFalse(package.stat().st_mode & stat.S_IWUSR)
        self.assertFalse((self.steam / 'Steam.cfg').exists())

    def test_fix_refused_while_steam_runs(self):
        self.pin()
        found, result = self.findings('--fix', running='steam')
        self.assertFalse((self.steam / 'steam.cfg').exists())
        self.assertEqual(result.returncode, 1)

    # -- payloads ---------------------------------------------------------
    def payload(self, version, body='x', patched=True):
        if patched:
            self.boot.write_text(self.boot.read_text() + '\n-- SLS-ADDONS-INTEGRATION\n')
        directory = self.addons / 'luna'
        (directory / 'backend').mkdir(parents=True, exist_ok=True)
        (directory / 'plugin.json').write_text(json.dumps({'version': version}))
        (directory / 'backend/main.lua').write_text(body)
        return directory

    def test_payload_dev_build_is_info(self):
        self.payload('0.0.1-dev')
        found, result = self.findings()
        self.assertEqual(found['addon.luna.drift']['severity'], 'info')
        self.assertIn('dev build', found['addon.luna.drift']['message'])
        self.assertEqual(result.returncode, 0)

    def test_payload_older_and_newer_release(self):
        self.payload('0.0.1')
        found, result = self.findings()
        self.assertIn('older Luna release', found['addon.luna.drift']['message'])
        self.assertEqual(result.returncode, 1)
        self.payload('99.0.0')
        found, _ = self.findings()
        self.assertIn('newer Luna release', found['addon.luna.drift']['message'])

    def test_fix_never_overwrites_payload(self):
        directory = self.payload('0.0.1', body='mine')
        self.doctor('--fix')
        self.assertEqual((directory / 'backend/main.lua').read_text(), 'mine')
        self.assertEqual(json.loads((directory / 'plugin.json').read_text())['version'], '0.0.1')

    # -- installed luna.py ------------------------------------------------
    def test_installed_luna_copy_differs(self):
        (self.tools / 'luna.py').write_text('# old\n')
        found, result = self.findings()
        self.assertEqual(found['luna.copy']['severity'], 'warn')
        self.assertFalse(found['luna.copy']['fixable'])
        self.doctor('--fix')
        self.assertEqual((self.tools / 'luna.py').read_text(), '# old\n')

    # -- launchers --------------------------------------------------------
    def test_launcher_changed_outside_luna(self):
        apps = self.home / '.local/share/applications'
        apps.mkdir(parents=True)
        launcher = apps / 'steamos-3840x2160-60.desktop'
        launcher.write_text('[Desktop Entry]\nExec=mine\n')
        (self.tools / 'steam-machine-shortcuts.json').write_text(json.dumps(
            {'files': {str(launcher): {'sha256': '0' * 64}}, 'profiles': {}}))
        found, result = self.findings()
        self.assertIn('steamos-3840x2160-60.desktop', found['launchers.changed']['message'])
        self.assertIn('luna launchers', found['launchers.changed']['fix'])
        self.doctor('--fix')
        self.assertEqual(launcher.read_text(), '[Desktop Entry]\nExec=mine\n')

    # -- installed copy (no checkout next to luna.py) ---------------------
    def installed_copy_findings(self):
        import shutil, tempfile
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        (root / 'sls-luna').mkdir()
        shutil.copy2(test_luna.LUNA, root / 'sls-luna' / 'luna.py')
        result = subprocess.run([sys.executable, str(root / 'sls-luna' / 'luna.py'), 'doctor', '--json'],
                                capture_output=True, text=True, env=dict(self.env))
        return {f['id']: f for f in json.loads(result.stdout)['findings']}, result

    def test_installed_copy_without_snapshot_is_info_not_error(self):
        found, result = self.installed_copy_findings()
        self.assertEqual(found['addons.manifest']['severity'], 'info')
        self.assertNotEqual(json.loads(result.stdout)['exit'], 2)

    def test_installed_copy_uses_install_snapshot(self):
        plugin = self.addons / 'sgdb'
        (plugin / 'backend').mkdir(parents=True)
        (plugin / 'backend' / 'main.lua').write_text('one')
        (self.addons / 'luna-manifest.json').write_text(json.dumps(
            {'versions': {'sgdb': '1.0.0'}, 'files': {'sgdb': {'backend/main.lua': self.sha(plugin / 'backend' / 'main.lua')}}}))
        found, _ = self.installed_copy_findings()
        self.assertNotIn('addons.manifest', found)
        self.assertEqual(found['addons.payloads']['severity'], 'ok')
        (plugin / 'backend' / 'main.lua').write_text('edited')
        found, _ = self.installed_copy_findings()
        self.assertEqual(found['addon.sgdb.drift']['severity'], 'warn')

    def test_launcher_unchanged_is_ok(self):
        launcher = self.home / 'a.desktop'
        launcher.write_text('same')
        (self.tools / 'steam-machine-shortcuts.json').write_text(json.dumps(
            {'files': {str(launcher): {'sha256': self.sha(launcher)}}, 'profiles': {}}))
        found, _ = self.findings()
        self.assertEqual(found['launchers']['severity'], 'ok')

    # -- pacman -----------------------------------------------------------
    def pacman_env(self, conf_text):
        stub = self.home / 'pacman-bin'
        stub.mkdir()
        pacman = stub / 'pacman'
        pacman.write_text('#!/bin/sh\nexit 0\n')
        pacman.chmod(0o755)
        conf = self.home / 'pacman.conf'
        conf.write_text(conf_text)
        # The helper stub dir is re-created by self.luna(), so put ours later
        # in PATH order is not possible; point PATH at ours first and rely on
        # the helper writing pgrep etc. only into test-bin.
        return dict(self.env, LUNA_PACMAN_CONF=str(conf),
                    PATH=str(stub) + os.pathsep + self.env['PATH'])

    def test_pacman_not_ignored_prints_exact_line_never_fixed(self):
        env = self.pacman_env('[options]\nHoldPkg = pacman\n')
        found, result = self.findings(env=env)
        self.assertEqual(found['pacman.ignorepkg']['severity'], 'warn')
        self.assertIn('IgnorePkg = steam', found['pacman.ignorepkg']['fix'])
        self.assertFalse(found['pacman.ignorepkg']['fixable'])
        self.assertEqual(result.returncode, 1)
        self.doctor('--fix', env=env)
        self.assertNotIn('IgnorePkg', (self.home / 'pacman.conf').read_text())

    def test_pacman_ignored_is_ok(self):
        env = self.pacman_env('[options]\nIgnorePkg = steam foo\n')
        found, result = self.findings(env=env)
        self.assertEqual(found['pacman.ignorepkg']['severity'], 'ok')

    # -- updater records --------------------------------------------------
    def test_update_errors_recorded(self):
        (self.tools / 'update-check.json').write_text(
            json.dumps({'checkError': 'error connecting to api.github.com'}))
        (self.tools / 'autoupdate.json').write_text(
            json.dumps({'result': 'current', 'steamDrift': 'Steam client executables changed'}))
        found, result = self.findings()
        self.assertIn('api.github.com', found['update.check']['message'])
        self.assertIn('executables changed', found['update.steam-drift']['message'])
        self.assertEqual(result.returncode, 1)

    def test_autoupdate_error_result(self):
        (self.tools / 'autoupdate.json').write_text(json.dumps({'result': 'error', 'reason': 'boom'}))
        found, _ = self.findings()
        self.assertIn('boom', found['update.autoupdate']['message'])

    # -- bridge / sidecar -------------------------------------------------
    def test_unpatched_boot_with_addons_is_error(self):
        self.payload('0.0.1-dev', patched=False)
        found, result = self.findings()
        self.assertEqual(found['bridge.boot']['severity'], 'error')
        self.assertEqual(result.returncode, 2)

    def test_stale_sidecar_without_steam(self):
        found, _ = self.findings(running='lumen')
        self.assertEqual(found['lumen.sidecar']['severity'], 'warn')
        self.assertFalse(found['lumen.sidecar']['fixable'])
        found, _ = self.findings(running='lumen,steam')
        self.assertNotIn('lumen.sidecar', found)

    # -- sync conflicts ---------------------------------------------------
    def test_sync_conflicts(self):
        config = self.steam / 'userdata/1/config'
        config.mkdir(parents=True)
        (config / 'localconfig.vdf').write_text('"x"{}')
        (config / 'localconfig.sync-conflict-20260101-000000-ABCDEFG.vdf').write_text('"x"{}')
        other = self.tools / 'foo.sync-conflict-20260101-000000-ABCDEFG.json'
        other.write_text('{}')
        found, result = self.findings()
        self.assertIn('luna reconcile', found['sync.localconfig']['fix'])
        self.assertIn('foo.sync-conflict', found['sync.conflicts']['message'])
        self.doctor('--fix')
        self.assertTrue(other.exists())
        self.assertEqual(result.returncode, 1)


if __name__ == '__main__':
    unittest.main()
