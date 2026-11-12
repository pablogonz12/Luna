"""Synthetic Valve-format packages only; never touches the real Steam or network."""
import argparse
import contextlib
import hashlib
import importlib.util
import io
import json
import lzma
import os
from pathlib import Path
import shutil
import stat
import struct
import tempfile
import time
import unittest
from unittest import mock
import zipfile
import zlib

from test_luna import UPSTREAM


SPEC = importlib.util.spec_from_file_location('luna_client_test', Path(__file__).resolve().parents[1] / 'scripts/luna.py')
luna = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(luna)


class SteamClientTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='luna-steam-client-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.root = self.home / '.local/share/Steam'
        self.packages = self.root / 'package'
        self.packages.mkdir(parents=True)
        (self.root / 'steam.sh').write_text('#!/bin/sh\n# newer bootstrap\n')
        for name in luna.STEAM_CORE_FILES:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'newer-client')
        (self.root / 'logs').mkdir()
        (self.root / 'logs/console_log.txt').write_text('Client version: 999\n')
        for name in ('userdata/private', 'config/loginusers.vdf', 'steamapps/private-save'):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('private sentinel')
        for name in ('lua/boot.lua', 'luatools/backend/main.lua'):
            path = self.home / '.local/share/Lumen' / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(UPSTREAM if name.endswith('boot.lua') else '-- fixture')
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(luna, 'home', return_value=self.home).start()
        mock.patch.object(luna, 'process_running', return_value=False).start()
        mock.patch.dict(os.environ, {'HOME': str(self.home),
                                    'XDG_DATA_HOME': str(self.home / '.local/share'),
                                    'XDG_RUNTIME_DIR': str(self.home / 'run')}).start()
        self.network = mock.patch.object(luna.urllib.request, 'urlopen', side_effect=AssertionError('unexpected network')).start()
        # The hook worker and the SteamOS launchers have their own suites; run for
        # real here they read the host's niri/DRM outputs and print hook errors.
        self.hook = mock.patch.object(luna, 'lumen_hook', return_value=0).start()
        self.launchers = mock.patch.object(luna, 'steam_machine_action', return_value=0).start()
        self.make_packages()

    def make_packages(self, extra=None, compressed=True, corrupt_crc=False):
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_STORED) as archive:
            archive.writestr('steam.sh', b'#!/bin/sh\n# synthetic compatibility launcher\n')
            for name in luna.STEAM_CORE_FILES:
                archive.writestr(name, b'\x7fELFsynthetic-compatible-' + name.encode())
            link = zipfile.ZipInfo('ubuntu12_32\\video\\codec.so')
            link.create_system = 3
            link.external_attr = (stat.S_IFLNK | 0o755) << 16
            archive.writestr(link, b'../steamclient.so')
            for name, content in (extra or {}).items():
                archive.writestr(name, content)
        raw = output.getvalue()
        if compressed:
            filters = [{'id': lzma.FILTER_LZMA1, 'dict_size': 1 << 23, 'lc': 3, 'lp': 0, 'pb': 2}]
            payload = (b'VZa' + struct.pack('<I', 0) + b'\x5d' + struct.pack('<I', 1 << 23)
                       + lzma.compress(raw, format=lzma.FORMAT_RAW, filters=filters)
                       + struct.pack('<II', zlib.crc32(raw) ^ int(corrupt_crc), len(raw)) + b'zv')
            filename = 'synthetic-client.zip.vz.fixture'
            fields = '"zipvz" "%s" "sha2vz" "%s"' % (filename, hashlib.sha256(payload).hexdigest())
        else:
            payload = raw
            filename = 'synthetic-client.zip'
            fields = '"sha2" "%s"' % hashlib.sha256(payload).hexdigest()
        manifest = ('"ubuntu12" { "version" "%s" "client" { "file" "%s" %s } }'
                    % (luna.STEAM_COMPAT_BUILD, filename, fields)).encode()
        (self.packages / filename).write_bytes(payload)
        self.manifest = self.packages / luna.STEAM_MANIFEST_NAME
        self.manifest.write_bytes(manifest)
        self.archive = self.packages / filename
        mock.patch.object(luna, 'STEAM_COMPAT_MANIFEST_SHA256', hashlib.sha256(manifest).hexdigest()).start()

    def install(self, **kwargs):
        options = argparse.Namespace(only=[], force=False, dry_run=False, keep_steam_client=False, **kwargs)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return luna.cmd_install(options)

    def snapshot(self):
        return {str(path.relative_to(self.home)): path.read_bytes()
                for path in self.home.rglob('*') if path.is_file() and not path.is_symlink()}

    def bump_updater(self):
        updater = self.home / 'fake-refresh-update.sh'
        updater.write_text(
            '#!/bin/sh\nif [ "$1" = "-shutdown" ]; then exit 0; fi\n'
            'sed -i \'s/"version"[[:space:]]*"[0-9]*"/"version" "999"/\' "%s"\n'
            'echo "[x] Client version: 999" >> "%s"\nexit 0\n'
            % (self.manifest, self.root / 'logs/console_log.txt'))
        updater.chmod(0o755)
        return updater

    def test_refresh_updates_pins_caches_and_reinstalls(self):
        updater = self.bump_updater()
        with mock.patch.dict(os.environ, {'LUNA_STEAM_SH': str(updater)}):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(luna.cmd_refresh(argparse.Namespace(dry_run=False)), 0)
        self.assertIn('999', out.getvalue())
        self.assertEqual(luna.read_steam_pin()['clientBuild'], '999')
        self.assertEqual((luna.steam_cache_dir() / 'build.txt').read_text().strip(), '999')
        self.assertIn('BootStrapperInhibitAll=enable', (self.root / 'steam.cfg').read_text())
        self.assertTrue((self.home / '.local/share/Lumen/sls-addons/luna/backend/main.lua').is_file())
        boot = (self.home / '.local/share/Lumen/lua/boot.lua').read_text()
        self.assertEqual(boot.count('SLS-ADDONS-INTEGRATION'), 1)
        self.assertTrue(luna.steam_state()['match'])

    def test_refresh_dry_run_writes_nothing(self):
        before = self.snapshot()
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(luna.cmd_refresh(argparse.Namespace(dry_run=True)), 0)
        self.assertIn('DRY RUN', out.getvalue())
        self.assertEqual(before, self.snapshot())

    def test_refresh_refuses_while_steam_runs(self):
        before = self.snapshot()
        luna.process_running.side_effect = lambda name: name == 'steam'
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(luna.cmd_refresh(argparse.Namespace(dry_run=False)), 2)
        finally:
            luna.process_running.side_effect = None
        self.assertEqual(before, self.snapshot())
        self.network.assert_not_called()

    def test_default_install_extracts_binaries_and_installs_luna(self):
        marker = self.root / '.steam-enable-steamrt64-client'
        marker.touch()
        (self.root / 'steam.cfg').write_text('CustomSetting=yes\nBootStrapperInhibitAll=disable\n')
        self.assertEqual(self.install(), 0)
        for name in luna.STEAM_CORE_FILES:
            self.assertEqual((self.root / name).read_bytes(), b'\x7fELFsynthetic-compatible-' + name.encode())
            self.assertTrue(os.access(self.root / name, os.X_OK))
        self.assertEqual(luna.steam_client_build(), luna.STEAM_COMPAT_BUILD, 'stale logs must not override verified executables')
        self.assertTrue((self.home / '.local/share/Lumen/sls-addons/luna/plugin.json').is_file())
        self.assertTrue((self.root / 'ubuntu12_32/video/codec.so').is_symlink())
        self.assertFalse(marker.exists())
        self.assertIn('CustomSetting=yes', (self.root / 'steam.cfg').read_text())
        self.assertTrue(luna.steam_updates_blocked())
        self.assertTrue(luna.packages_match_cache())
        self.assertEqual(luna.read_steam_pin()['clientBuild'], luna.STEAM_COMPAT_BUILD)
        receipt = json.loads((luna.luna_tools_dir() / 'steam-client-installed.json').read_text())
        self.assertTrue((Path(receipt['backup']) / 'restore.json').is_file())
        for name in ('userdata/private', 'config/loginusers.vdf', 'steamapps/private-save'):
            self.assertEqual((self.root / name).read_text(), 'private sentinel')
        self.network.assert_not_called()
        self.hook.assert_called_with('enable')
        self.assertEqual(self.launchers.call_args.args[0], 'install')

    def test_untraceable_pins_are_named(self):
        commit = 'a' * 40
        manifest = {'shared': {'bridge/bridge.lua': {'source_commit': 'unreleased'}},
                    'addons': {'luna': {'source_commit': commit},
                               'presence': {'source_commit': 'uncommitted-worktree'},
                               'sgdb': {}}}
        self.assertEqual(luna.untraceable_pins(manifest),
                         ['bridge/bridge.lua (unreleased)', 'presence (uncommitted-worktree)', 'sgdb (no commit)'])
        self.assertEqual(luna.untraceable_pins({'addons': {'luna': {'source_commit': commit}}}), [])

    def test_a_hung_gh_is_bounded(self):
        fake = self.home / 'gh'
        fake.write_text('#!/bin/sh\nexec sleep 30\n')
        fake.chmod(0o755)
        with mock.patch.object(luna, 'find_gh', return_value=str(fake)), \
                mock.patch.object(luna, 'GH_TIMEOUT', 0.5), \
                mock.patch.object(luna, 'GH_DOWNLOAD_TIMEOUT', 0.5):
            started = time.monotonic()
            value, error = luna.gh_json(['release', 'view'])
            self.assertIsNone(value)
            self.assertIn('did not answer', error)
            bundle, error = luna.download_release('v1', self.home)
            self.assertIsNone(bundle)
            self.assertIn('did not finish', error)
            self.assertLess(time.monotonic() - started, 10)

    def test_regular_zip_packages_are_supported(self):
        self.make_packages(compressed=False)
        ok, message = luna.ensure_compatible_steam()
        self.assertTrue(ok, message)

    def test_corrupted_package_refuses_before_installing_addons(self):
        self.archive.write_bytes(b'corrupt')
        before = self.snapshot()
        self.network.side_effect = OSError('network unavailable')
        self.assertEqual(self.install(), 1)
        self.assertEqual((self.root / luna.STEAM_CORE_FILES[0]).read_bytes(), b'newer-client')
        self.assertFalse((self.home / '.local/share/Lumen/sls-addons').exists())
        self.assertEqual(before, self.snapshot())

    def test_dry_run_has_no_network_or_disk_changes(self):
        before = self.snapshot()
        options = argparse.Namespace(only=[], force=False, dry_run=True, keep_steam_client=False)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(luna.cmd_install(options), 0)
        self.assertIn('would install/verify build', output.getvalue())
        self.assertEqual(before, self.snapshot())
        self.network.assert_not_called()

    def test_running_client_refuses_without_mutating_any_file(self):
        before = self.snapshot()
        luna.process_running.side_effect = lambda name: name == 'steam'
        ok, message = luna.ensure_compatible_steam()
        self.assertFalse(ok)
        self.assertIn('running', message)
        self.assertEqual(before, self.snapshot())

    def test_recovery_replaces_executables_even_with_matching_package_metadata(self):
        ok, message = luna.ensure_compatible_steam()
        self.assertTrue(ok, message)
        target = self.root / luna.STEAM_CORE_FILES[0]
        target.write_bytes(b'newer-client-again')
        self.assertTrue(luna.packages_match_cache())
        ok, message = luna._recover_steam_client(luna.STEAM_COMPAT_BUILD)
        self.assertTrue(ok, message)
        self.assertEqual(target.read_bytes(), b'\x7fELFsynthetic-compatible-' + luna.STEAM_CORE_FILES[0].encode())

    def test_cfg_write_failure_rolls_back_all_client_files_and_marker(self):
        marker = self.root / '.steam-enable-steamrt64-client'
        marker.touch()
        before = {str(path.relative_to(self.root)): path.read_bytes()
                  for path in self.root.rglob('*') if path.is_file()}
        with mock.patch.object(luna, 'ensure_steam_cfg_block', side_effect=OSError('simulated disk failure')):
            ok, message = luna.ensure_compatible_steam()
        self.assertFalse(ok, message)
        after = {str(path.relative_to(self.root)): path.read_bytes()
                 for path in self.root.rglob('*') if path.is_file()}
        self.assertEqual(before, after)
        self.assertTrue(marker.exists())
        self.assertFalse((luna.luna_tools_dir() / 'steam-client-installed.json').exists())

    def test_idempotent_install_preserves_identical_executable(self):
        self.assertTrue(luna.ensure_compatible_steam()[0])
        target = self.root / luna.STEAM_CORE_FILES[0]
        before = target.stat()
        self.assertTrue(luna.ensure_compatible_steam()[0])
        self.assertEqual(target.stat().st_ino, before.st_ino)
        self.assertEqual(target.stat().st_mtime_ns, before.st_mtime_ns)
        receipt = json.loads((luna.luna_tools_dir() / 'steam-client-installed.json').read_text())
        self.assertFalse((Path(receipt['backup']) / 'previous-cache').exists(), 'identical reinstalls must not duplicate the package cache')

    def test_tampered_receipt_core_is_not_reported_as_verified(self):
        self.assertTrue(luna.ensure_compatible_steam()[0])
        (self.root / luna.STEAM_CORE_FILES[0]).write_bytes(b'not-the-installed-client')
        # The on-disk manifest (ground truth) wins over the stale runtime log.
        self.assertEqual(luna.steam_client_build(), luna.STEAM_COMPAT_BUILD)
        self.assertFalse(luna.steam_state()['match'])
        self.assertIn('executables changed', luna.steam_drift_note())

    def test_zero_version_reports_carry_no_information(self):
        (self.root / 'logs/console_log.txt').write_text('Client version: 0\n')
        (luna.luna_tools_dir() / 'steam-client-installed.json').unlink(missing_ok=True)
        self.assertEqual(luna.steam_client_build(), luna.STEAM_COMPAT_BUILD)
        (self.root / 'package/steam_client_ubuntu12.manifest').unlink()
        (self.root / 'logs/console_log.txt').write_text('Client version: 999\nClient version: 0\n')
        self.assertEqual(luna.steam_client_build(), '999')
        (self.root / 'logs/console_log.txt').write_text('Client version: 0\n')
        self.assertEqual(luna.steam_client_build(), '')

    def test_cache_refuses_a_build_the_manifest_does_not_describe(self):
        with self.assertRaisesRegex(ValueError, 'not 1791918635'):
            luna._cache_client_packages('1791918635')
        self.assertFalse((luna.steam_cache_dir() / 'package').exists())

    def test_cache_accepts_an_explicit_build_matching_the_manifest(self):
        options = argparse.Namespace(steam_action='cache', build=luna.STEAM_COMPAT_BUILD, now=False)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(luna.cmd_steam(options), 0)
        self.assertEqual((luna.steam_cache_dir() / 'build.txt').read_text().strip(), luna.STEAM_COMPAT_BUILD)

    def test_client_backups_are_pruned_to_the_newest_five(self):
        backups = luna.luna_tools_dir() / 'steam-client-backups'
        backups.mkdir(parents=True)
        (backups / 'notes.txt').write_text('operator file, never touched')
        for index in range(7):
            (backups / str(1000 + index)).mkdir()
        self.assertEqual(luna.prune_steam_client_backups(), 2)
        remaining = sorted(p.name for p in backups.iterdir() if p.is_dir())
        self.assertEqual(remaining, [str(1002 + index) for index in range(5)])
        self.assertTrue((backups / 'notes.txt').is_file())

    def test_updater_shuts_steam_back_down_after_a_relaunch(self):
        shutdowns = self.home / 'shutdowns.log'
        updater = self.home / 'fake-steam-update.sh'
        updater.write_text('#!/bin/sh\necho "$@" >> "%s"\nexit 0\n' % shutdowns)
        updater.chmod(0o755)
        state = {'calls': 0}
        def relaunched_once(name):
            state['calls'] += 1
            return state['calls'] == 4
        luna.process_running.side_effect = relaunched_once
        with mock.patch.dict(os.environ, {'LUNA_STEAM_SH': str(updater)}):
            ok, message = luna._steam_force_update()
        self.assertTrue(ok, message)
        self.assertIn('updated by Valve', message)
        self.assertIn('-shutdown', shutdowns.read_text())
        self.assertGreaterEqual(state['calls'], 7)

    def test_protected_and_traversal_archive_paths_are_rejected(self):
        for name in ('../escape', '/absolute', 'userdata/credentials', 'steamapps/game', 'config/loginusers.vdf',
                     'ubuntu12_32/../../escape', 'C:\\escape'):
            with self.subTest(name=name):
                self.make_packages(extra={name: b'bad'})
                before = self.snapshot()
                ok, message = luna.ensure_compatible_steam()
                self.assertFalse(ok)
                self.assertIn('unsafe/protected', message)
                self.assertEqual(before, self.snapshot())

    def test_vzip_crc_is_verified_independently_of_package_sha256(self):
        self.make_packages(corrupt_crc=True)
        ok, message = luna.ensure_compatible_steam()
        self.assertFalse(ok)
        self.assertIn('VZip integrity', message)
        self.assertEqual((self.root / luna.STEAM_CORE_FILES[0]).read_bytes(), b'newer-client')

    def test_missing_core_executables_are_rejected(self):
        with mock.patch.object(luna, 'STEAM_CORE_FILES', (*luna.STEAM_CORE_FILES, 'missing/steam')):
            ok, message = luna.ensure_compatible_steam()
        self.assertFalse(ok)
        self.assertIn('missing core', message)

    def test_existing_destination_symlink_cannot_write_outside_steam(self):
        outside = self.home / 'outside'
        outside.mkdir()
        (outside / 'steam').write_text('preserve me')
        shutil.rmtree(self.root / 'ubuntu12_32')
        (self.root / 'ubuntu12_32').symlink_to(outside, target_is_directory=True)
        ok, message = luna.ensure_compatible_steam()
        self.assertFalse(ok)
        self.assertIn('escapes', message)
        self.assertEqual((outside / 'steam').read_text(), 'preserve me')

    def test_offline_cache_can_supply_both_manifest_and_packages(self):
        self.assertTrue(luna.ensure_compatible_steam()[0])
        self.manifest.unlink()
        self.archive.unlink()
        self.assertTrue(luna.ensure_compatible_steam()[0])
        self.network.assert_not_called()

    def test_fresh_install_downloads_verified_manifest_and_hash_named_package(self):
        files = {self.manifest.name: self.manifest.read_bytes(), self.archive.name: self.archive.read_bytes()}
        self.manifest.unlink()
        self.archive.unlink()

        def open_url(request, **kwargs):
            name = request.full_url.rsplit('/', 1)[-1]
            if name == 'steam_client_ubuntu12':
                name += '.manifest'
            return io.BytesIO(files[name])

        self.network.side_effect = open_url
        ok, message = luna.ensure_compatible_steam()
        self.assertTrue(ok, message)
        self.assertEqual(self.network.call_count, 2)
        self.assertTrue(luna.packages_match_cache())

    def test_download_checksum_mismatch_is_rejected(self):
        self.network.side_effect = None
        self.network.return_value = io.BytesIO(b'wrong archive contents')
        with self.assertRaisesRegex(ValueError, 'SHA256 mismatch'):
            luna._download_steam_file('https://example.invalid/archive', self.home / 'download', '0' * 64)

    def test_internal_recovery_and_updater_refuse_running_steam(self):
        self.assertTrue(luna.ensure_compatible_steam()[0])
        luna.process_running.side_effect = lambda name: name == 'steam'
        before = self.snapshot()
        self.assertFalse(luna._recover_steam_client(luna.STEAM_COMPAT_BUILD)[0])
        self.assertFalse(luna._steam_force_update()[0])
        self.assertEqual(before, self.snapshot())

    def test_queued_steam_update_must_be_cancelled_before_installing(self):
        luna.write_json_file(luna.steam_pending_path(), {'action': 'update'})
        before = self.snapshot()
        ok, message = luna.ensure_compatible_steam()
        self.assertFalse(ok)
        self.assertIn('luna steam cancel', message)
        self.assertEqual(before, self.snapshot())

    def test_skip_flag_installs_addons_without_touching_client(self):
        options = argparse.Namespace(only=[], force=False, dry_run=False, keep_steam_client=True)
        before = {str(path.relative_to(self.root)): path.read_bytes()
                  for path in self.root.rglob('*') if path.is_file()}
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(luna.cmd_install(options), 0)
        after = {str(path.relative_to(self.root)): path.read_bytes()
                 for path in self.root.rglob('*') if path.is_file()}
        self.assertEqual(before, after)
        self.network.assert_not_called()

    def test_disabled_or_commented_inhibit_is_not_reported_as_blocked(self):
        for text in ('BootStrapperInhibitAll=disable\n', '#BootStrapperInhibitAll=enable\n'):
            (self.root / 'steam.cfg').write_text(text)
            self.assertFalse(luna.steam_updates_blocked())

    def test_alternate_native_steam_symlink_is_supported(self):
        alternate = self.home / 'alternate-steam'
        self.root.rename(alternate)
        link = self.home / '.steam/steam'
        link.parent.mkdir()
        link.symlink_to(alternate, target_is_directory=True)
        self.assertEqual(luna.steam_home(), alternate)
        ok, message = luna.ensure_compatible_steam()
        self.assertTrue(ok, message)


class SteamHardLockTestCase(unittest.TestCase):
    """`luna steam lock/unlock` against a fake Steam dir; every process call is mocked."""

    ROLLED_BACK = '777'

    make_packages = SteamClientTestCase.make_packages

    def setUp(self):
        # Reuse the fake-Steam fixture without inheriting (and re-running) its tests.
        SteamClientTestCase.setUp(self)
        self.calls = []
        self.state = self.home / '.local/state/slsteam-moon'
        self.state.mkdir(parents=True)
        (self.state / 'boot_fail_count').write_text('3\n')
        (self.state / 'safe_mode_on').write_text('1')
        (self.packages / 'steam_client_steamrt64.manifest').write_text('x')
        (self.packages / 'steam_client_steamrt64_x').write_text('x')
        mock.patch.object(luna.time, 'sleep').start()

        def fake_run(command, *args, **kwargs):
            self.calls.append(list(command))
            if '-forcemanifestsuffix' in command:
                # The rolled-back client rewrites the on-disk manifest to its build.
                self.manifest.write_text(self.manifest.read_text().replace(
                    '"version" "%s"' % luna.STEAM_COMPAT_BUILD, '"version" "%s"' % self.ROLLED_BACK))
                return mock.Mock(returncode=7)  # non-zero must be tolerated
            return mock.Mock(returncode=1, stdout='', stderr='')
        mock.patch.object(luna.subprocess, 'run', side_effect=fake_run).start()

    def steam(self, action, **kwargs):
        options = argparse.Namespace(steam_action=action, build=None, now=False, force=False, **kwargs)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = luna.cmd_steam(options)
        return code, out.getvalue() + err.getvalue()

    def test_lock_rolls_back_pins_and_locks(self):
        (self.root / 'steam.cfg').write_text('# mine\nBootStrapperInhibitAll=disable\n')
        code, output = self.steam('lock')
        self.assertEqual(code, 0, output)
        self.assertIn(['%s' % (self.root / 'ubuntu12_32/steam'), '-forcemanifestsuffix', 'ubuntu12', '-exitsteam'],
                      self.calls)
        self.assertFalse(list(self.packages.glob('steam_client_steamrt64*')))
        self.assertFalse(list(self.state.glob('safe_mode*')))
        self.assertEqual((self.state / 'boot_fail_count').read_text().strip(), '0')
        for name in ('Steam.cfg', 'steam.cfg'):
            content = (self.root / name).read_text()
            self.assertIn('BootStrapperInhibitAll=enable', content)
            self.assertIn('BootStrapperInhibitUpdateOnLaunch=enable', content)
            self.assertIn('BootStrapperForceSelfUpdate=false', content)
        self.assertNotIn('=disable', (self.root / 'steam.cfg').read_text())
        self.assertEqual(stat.S_IMODE(self.packages.stat().st_mode) & 0o222, 0)
        pin = luna.read_steam_pin()
        self.assertEqual(pin['clientBuild'], self.ROLLED_BACK)
        self.assertEqual(pin['hardLock']['rollback'], 'ubuntu12')
        state = luna.steam_state()
        self.assertTrue(state['match'])
        self.assertTrue(state['hardLock'] and state['packageReadOnly'])
        self.assertEqual(luna.steam_drift_note(), '')
        self.assertIn('HARD', self.steam('status')[1])

    def test_lock_needs_force_or_confirmation_to_kill_a_running_steam(self):
        mock.patch.object(luna, 'process_running', side_effect=lambda name: name == 'steam').start()
        code, output = self.steam('lock')
        self.assertEqual(code, luna.EXIT_REFUSED)
        self.assertIn('--force', output)
        self.assertFalse([call for call in self.calls if call[0] == 'pkill'])
        self.assertFalse(luna.steam_pin_path().exists())
        options = argparse.Namespace(steam_action='lock', build=None, now=False, force=True)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(luna.cmd_steam(options), 0)
        killed = sorted(call[-1] for call in self.calls if call[0] == 'pkill')
        self.assertEqual(killed, ['steam', 'steamwebhelper'])

    def test_unlock_undoes_lock_and_launches_only_with_now(self):
        self.assertEqual(self.steam('lock')[0], 0)
        launched = mock.patch.object(luna.subprocess, 'Popen').start()
        code, output = self.steam('unlock')
        self.assertEqual(code, 0, output)
        launched.assert_not_called()
        self.assertTrue(os.access(self.packages, os.W_OK))
        self.assertFalse((self.root / 'Steam.cfg').exists())
        self.assertFalse((self.root / 'steam.cfg').exists())
        self.assertFalse(luna.steam_pin_path().exists())
        self.assertFalse(luna.steam_hard_lock_active())
        self.assertEqual(self.steam('lock')[0], 0)
        self.assertEqual(self.steam('unlock')[0], 0)
        options = argparse.Namespace(steam_action='unlock', build=None, now=True, force=False)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(luna.cmd_steam(options), 0)
        self.assertEqual(launched.call_args[0][0], ['setsid', 'steam'])

    def test_relaunch_modes_notify_and_reopen_steam_detached(self):
        launched = mock.patch.object(luna.subprocess, 'Popen').start()
        options = argparse.Namespace(steam_action='lock', build=None, now=False, force=True, relaunch=True)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(luna.cmd_steam(options), 0)
        self.assertEqual(launched.call_args[0][0], ['setsid', 'steam'])
        self.assertTrue(any(call[0] == 'notify-send' and 'blocked' in call[-2] for call in self.calls))
        launched.reset_mock()
        self.calls.clear()
        running = {'steam': True}
        mock.patch.object(luna, 'process_running', side_effect=lambda name: running.get(name, False)).start()

        def fake_run(command, *args, **kwargs):
            self.calls.append(list(command))
            if command[:2] == ['steam', '-shutdown']:
                running['steam'] = False
            return mock.Mock(returncode=0, stdout='', stderr='')
        luna.subprocess.run.side_effect = fake_run
        options = argparse.Namespace(steam_action='unlock', build=None, now=False, force=False, relaunch=True)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(luna.cmd_steam(options), 0)
        self.assertLess(self.calls.index(['steam', '-shutdown']),
                        next(i for i, c in enumerate(self.calls) if c[0] == 'notify-send'))
        self.assertEqual(launched.call_args[0][0], ['setsid', 'steam'])
        self.assertFalse(luna.steam_hard_lock_active())

    def test_unlock_relaunch_changes_nothing_when_steam_will_not_close(self):
        self.assertEqual(self.steam('lock')[0], 0)
        mock.patch.object(luna, 'process_running', return_value=True).start()
        launched = mock.patch.object(luna.subprocess, 'Popen').start()
        options = argparse.Namespace(steam_action='unlock', build=None, now=False, force=False, relaunch=True)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(luna.cmd_steam(options), 1)
        launched.assert_not_called()
        self.assertTrue(luna.steam_hard_lock_active())

    def test_hard_lock_survives_repin_and_guards_unpin_and_update(self):
        self.assertEqual(self.steam('lock')[0], 0)
        self.assertEqual(self.steam('pin')[0], 0)
        self.assertTrue(luna.steam_hard_lock_active())
        self.assertEqual(self.steam('unpin')[0], 1)
        self.assertEqual(self.steam('update')[0], 1)
        self.assertFalse(luna.steam_pending_path().exists())

    def test_package_writes_work_under_the_read_only_lock(self):
        self.assertEqual(self.steam('lock')[0], 0)
        self.assertFalse(os.access(self.packages, os.W_OK))
        code, output = self.steam('recover')
        self.assertEqual(code, 0, output)
        self.assertEqual(stat.S_IMODE(self.packages.stat().st_mode) & 0o222, 0)
        self.assertTrue(luna.steam_hard_lock_active())
        # Re-caching a read-only package tree must leave a deletable cache.
        luna._cache_client_packages(self.ROLLED_BACK)
        shutil.rmtree(luna.steam_cache_dir())
        self.assertEqual(self.steam('unlock')[0], 0)


class FakeResponse:
    def __init__(self, body):
        self.body = io.BytesIO(body)
        self.headers = {'Content-Length': str(len(body))}

    def read(self, size=-1):
        return self.body.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def http_error(code, retry_after=None):
    headers = {'Retry-After': retry_after} if retry_after else {}
    return luna.urllib.error.HTTPError('https://example.invalid/x', code, 'err', headers, None)


class SteamDownloadTestCase(SteamClientTestCase):
    def download(self, **kwargs):
        dest = self.home / 'download.bin'
        with contextlib.redirect_stdout(io.StringIO()) as out:
            luna._download_steam_file('https://example.invalid/x', dest, hashlib.sha256(b'payload').hexdigest(), **kwargs)
        return dest, out.getvalue()

    def test_retry_on_429_then_success_honours_retry_after(self):
        self.network.side_effect = [http_error(429, '7'), http_error(503), FakeResponse(b'payload')]
        with mock.patch.object(luna.time, 'sleep') as sleep:
            dest, out = self.download()
        self.assertEqual(dest.read_bytes(), b'payload')
        self.assertEqual(sleep.call_args_list[0], mock.call(7.0))
        self.assertEqual(sleep.call_count, 2)
        self.assertGreater(sleep.call_args_list[1].args[0], 0)
        self.assertIn('retry 1/', out)

    def test_gives_up_after_the_attempts(self):
        self.network.side_effect = http_error(429)
        with mock.patch.object(luna.time, 'sleep') as sleep, self.assertRaises(OSError) as raised:
            self.download(attempts=3)
        self.assertIn('429', str(raised.exception))
        self.assertEqual(sleep.call_count, 2)

    def test_404_is_not_retried(self):
        self.network.side_effect = http_error(404)
        with mock.patch.object(luna.time, 'sleep') as sleep, self.assertRaises(OSError):
            self.download()
        sleep.assert_not_called()

    def test_hash_mismatch_is_not_retried(self):
        self.network.side_effect = [FakeResponse(b'other')]
        with mock.patch.object(luna.time, 'sleep') as sleep, self.assertRaises(ValueError):
            self.download()
        sleep.assert_not_called()

    def test_final_error_names_the_cache_workaround(self):
        self.archive.unlink()
        self.network.side_effect = http_error(429)
        with mock.patch.object(luna.time, 'sleep'), contextlib.redirect_stdout(io.StringIO()):
            ok, message = luna.ensure_compatible_steam()
        self.assertFalse(ok)
        self.assertIn('--client-cache', message)
        self.assertIn('LUNA_STEAM_CACHE', message)

    def test_extra_cache_dir_avoids_the_network(self):
        extra = self.home / 'copied-cache'
        extra.mkdir()
        shutil.copy2(self.archive, extra / self.archive.name)
        self.archive.unlink()
        with contextlib.redirect_stdout(io.StringIO()) as out:
            ok, message = luna.ensure_compatible_steam(str(extra))
        self.assertTrue(ok, message)
        self.assertIn('need 1 file(s), 0.0 MB to download, 1 reused from cache', out.getvalue())
        self.network.assert_not_called()

    def test_extra_cache_with_bad_hash_is_ignored(self):
        extra = self.home / 'copied-cache'
        extra.mkdir()
        (extra / self.archive.name).write_bytes(b'tampered')
        self.assertIsNone(luna._find_steam_package([extra], self.archive.name, hashlib.sha256(b'x').hexdigest()))


class SteamInstallDecisionTestCase(SteamClientTestCase):
    def run_install(self, **kwargs):
        options = dict(only=[], force=False, dry_run=False, keep_steam_client=False)
        options.update(kwargs)
        with mock.patch.object(luna, 'ensure_compatible_steam', return_value=(True, 'ok')) as ensure, \
                contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()):
            code = luna.cmd_install(argparse.Namespace(**options))
        return code, ensure, out.getvalue()

    def pin_intact(self):
        self.assertTrue(luna.ensure_compatible_steam()[0])

    def test_only_with_no_pin_still_installs_client(self):
        code, ensure, _ = self.run_install(only=['luna'])
        self.assertEqual(code, 0)
        ensure.assert_called_once()

    def test_only_skips_client_when_pin_is_intact(self):
        self.pin_intact()
        code, ensure, out = self.run_install(only=['luna'])
        self.assertEqual(code, 0)
        ensure.assert_not_called()
        self.assertIn('client untouched', out)

    def test_steam_client_flag_forces_it(self):
        self.pin_intact()
        _, ensure, _ = self.run_install(only=['luna'], steam_client=True)
        ensure.assert_called_once()

    def test_no_steam_client_flag_skips_it_even_without_pin(self):
        _, ensure, _ = self.run_install(no_steam_client=True)
        ensure.assert_not_called()

    def test_full_install_still_verifies_client(self):
        self.pin_intact()
        _, ensure, _ = self.run_install()
        ensure.assert_called_once()

    def test_drift_triggers_client_even_with_only(self):
        self.pin_intact()
        (self.root / luna.STEAM_CORE_FILES[0]).write_bytes(b'drifted')
        _, ensure, _ = self.run_install(only=['luna'])
        ensure.assert_called_once()

    def test_dry_run_reports_summary_offline(self):
        code, ensure, out = self.run_install(dry_run=True)
        self.assertEqual(code, 0)
        ensure.assert_not_called()
        self.assertIn('need 1 file(s), 0.0 MB to download, 1 reused from cache', out)
        self.network.assert_not_called()

    def test_dry_run_without_manifest_says_size_unknown(self):
        self.manifest.unlink()
        _, _, out = self.run_install(dry_run=True)
        self.assertIn('size unknown until manifest download', out)
        self.network.assert_not_called()

    def test_dry_run_only_with_intact_pin_leaves_client_alone(self):
        self.pin_intact()
        _, _, out = self.run_install(dry_run=True, only=['luna'])
        self.assertIn('Steam client untouched', out)


if __name__ == '__main__':
    unittest.main()
