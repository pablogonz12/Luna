#!/usr/bin/env python3
"""`luna doctor` tests: a fake HOME/Steam tree per check, and --fix safety."""
import hashlib
import json
import os
import stat
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
