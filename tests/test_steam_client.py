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

# Runs luna.py code paths that can notify; silence them.
os.environ.setdefault('LUNA_NO_NOTIFY', '1')


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
        # Lock/heal try the pinned manifest and libcrypto downloads: offline unless a test says otherwise.
        mock.patch.object(luna, '_http_get', side_effect=OSError('offline')).start()
        mock.patch.object(luna, 'LIBCRYPTO_SYSTEM_PATHS', []).start()
        mock.patch.object(luna, 'OS_RELEASE_PATH', str(self.home / 'os-release')).start()
        # The SteamOS launchers have their own suite; run for real here they
        # read the host's niri/DRM outputs.
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
        # The historical build is opt-in now; the default is the ubuntu12 hard lock.
        kwargs.setdefault('steam_build', luna.STEAM_COMPAT_BUILD)
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
        options = argparse.Namespace(only=[], force=False, dry_run=True, keep_steam_client=False,
                                     steam_build=luna.STEAM_COMPAT_BUILD)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(luna.cmd_install(options), 0)
        self.assertIn('would install/verify build', output.getvalue())
        options.steam_build = ''
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(luna.cmd_install(options), 0)
        self.assertIn('ubuntu12', output.getvalue())
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

    # This is the one test that asserts notify-send is attempted; empty value
    # means "set but not suppressing" (only a non-empty value silences it).
    @mock.patch.dict(os.environ, {'LUNA_NO_NOTIFY': ''})
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

    # -- heal / launchopts / refresh / ignore-pkg / sidecar / bootstrap ----------
    def test_heal_restores_deleted_cfg_and_readonly_package(self):
        self.assertEqual(self.steam('lock')[0], 0)
        for name in ('Steam.cfg', 'steam.cfg'):
            (self.root / name).unlink()
        os.chmod(self.packages, 0o755)
        before_pin = luna.steam_pin_path().read_bytes()
        code, output = self.steam('heal')
        self.assertEqual(code, 0, output)
        for name in ('Steam.cfg', 'steam.cfg'):
            self.assertIn('BootStrapperInhibitAll=enable', (self.root / name).read_text())
        self.assertEqual(stat.S_IMODE(self.packages.stat().st_mode) & 0o222, 0)
        self.assertEqual(before_pin, luna.steam_pin_path().read_bytes())
        self.assertTrue((luna.luna_tools_dir() / 'steam-heal.log').is_file())

    def test_heal_without_a_pin_does_nothing_and_status_is_read_only(self):
        result = luna.heal_steam_lock()
        self.assertFalse(result['pinned'])
        self.assertFalse((self.root / 'steam.cfg').exists())
        self.assertEqual(self.steam('lock')[0], 0)
        (self.root / 'steam.cfg').unlink()
        self.steam('status')
        self.assertFalse((self.root / 'steam.cfg').exists())

    def test_lock_leaves_cache_equal_to_the_pinned_build(self):
        self.assertEqual(self.steam('lock')[0], 0)
        self.assertEqual(luna.read_steam_pin()['clientBuild'], self.ROLLED_BACK)
        self.assertEqual((luna.steam_cache_dir() / 'build.txt').read_text().strip(), self.ROLLED_BACK)
        self.assertTrue((luna.steam_cache_dir() / 'package').is_dir())
        self.assertEqual(self.steam('pin')[0], 0)
        self.assertEqual((luna.steam_cache_dir() / 'build.txt').read_text().strip(),
                         luna.read_steam_pin()['clientBuild'])

    def _localconfig(self, option):
        config = self.home / '.local/share/Steam/userdata/42/config'
        config.mkdir(parents=True, exist_ok=True)
        path = config / 'localconfig.vdf'
        path.write_text('"UserLocalConfigStore"\n{\n\t"apps"\n\t{\n\t\t"480"\n\t\t{\n'
                        '\t\t\t"LaunchOptions"\t\t"%s"\n\t\t}\n\t\t"220"\n\t\t{\n'
                        '\t\t\t"LaunchOptions"\t\t"-novid"\n\t\t}\n\t}\n}\n' % option)
        return path

    def launchopts(self, **kwargs):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = luna.cmd_steam(argparse.Namespace(
                steam_action='launchopts', build=None, now=False, force=False, **kwargs))
        return code, out.getvalue() + err.getvalue()

    def test_launchopts_detects_missing_path_and_fix_clears_only_it(self):
        path = self._localconfig('/gone/dir/tool %command%')
        code, output = self.launchopts(fix=False)
        self.assertEqual(code, 0)
        self.assertIn('/gone/dir/tool', output)
        self.assertEqual(path.read_text().count('/gone/dir/tool'), 1)
        self.assertIn('launch option', luna.launchopts_warning())
        code, output = self.launchopts(fix=True)
        self.assertEqual(code, 0, output)
        text = path.read_text()
        self.assertNotIn('/gone/dir', text)
        self.assertIn('-novid', text)
        backups = list(path.parent.glob('localconfig.vdf.luna-bak-*'))
        self.assertEqual(len(backups), 1)
        self.assertIn('/gone/dir/tool', backups[0].read_text())
        self.assertEqual(luna.launchopts_warning(), '')

    def test_launchopts_fix_refuses_while_steam_runs(self):
        path = self._localconfig('/gone/dir/tool %command%')
        original = path.read_text()
        mock.patch.object(luna, 'process_running', side_effect=lambda name: name == 'steam').start()
        code, output = self.launchopts(fix=True)
        self.assertEqual(code, luna.EXIT_REFUSED)
        self.assertEqual(path.read_text(), original)
        self.assertFalse(list(path.parent.glob('*luna-bak*')))

    def test_refresh_refuses_under_the_hard_lock(self):
        self.assertEqual(self.steam('lock')[0], 0)
        before = luna.steam_pin_path().read_bytes()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = luna.cmd_refresh(argparse.Namespace(dry_run=False))
        self.assertEqual(code, luna.EXIT_REFUSED)
        self.assertIn('--unlock-first', err.getvalue())
        self.assertTrue(luna.steam_hard_lock_active())
        self.assertEqual(before, luna.steam_pin_path().read_bytes())

    def _ignore_pkg(self, **kwargs):
        conf = self.home / 'pacman.conf'
        conf.write_text('[options]\nHoldPkg = pacman glibc\n')
        mock.patch.dict(os.environ, {'LUNA_PACMAN_CONF': str(conf)}).start()
        calls = self.calls

        def fake_run(command, *args, **kw):
            calls.append(list(command))
            return mock.Mock(returncode=0 if command[:2] == ['pacman', '-Q'] else 1, stdout='', stderr='')
        luna.subprocess.run.side_effect = fake_run
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = luna.cmd_steam(argparse.Namespace(
                steam_action='ignore-pkg', build=None, now=False, force=False, **kwargs))
        return code, out.getvalue(), conf

    def test_ignore_pkg_prints_the_command_and_never_runs_sudo_without_apply(self):
        code, output, conf = self._ignore_pkg(apply=False)
        self.assertEqual(code, 0)
        self.assertIn('IgnorePkg = steam', output)
        self.assertIn(str(conf), output)
        self.assertFalse([call for call in self.calls if call and call[0] == 'sudo'])
        self.assertNotIn('IgnorePkg', conf.read_text())

    def test_ignore_pkg_apply_needs_a_terminal(self):
        mock.patch.object(luna.sys.stdin, 'isatty', return_value=False).start()
        code, _output, _conf = self._ignore_pkg(apply=True)
        self.assertEqual(code, luna.EXIT_REFUSED)
        self.assertFalse([call for call in self.calls if call and call[0] == 'sudo'])

    def test_lumen_sidecar_is_stopped_once_steam_is_closed(self):
        running = {'lumen': True}
        mock.patch.object(luna, 'process_running', side_effect=lambda name: running.get(name, False)).start()

        def fake_run(command, *args, **kw):
            self.calls.append(list(command))
            if command[0] == 'pkill':
                running['lumen'] = False
            return mock.Mock(returncode=0, stdout='', stderr='')
        luna.subprocess.run.side_effect = fake_run
        self.assertTrue(luna.stop_lumen_sidecar())
        self.assertIn(['pkill', '-u', str(os.getuid()), '-x', 'lumen'], self.calls)
        self.calls.clear()
        self.assertTrue(luna.stop_lumen_sidecar())
        self.assertEqual(self.calls, [])

    def test_lumen_sidecar_is_never_stopped_while_steam_runs(self):
        mock.patch.object(luna, 'process_running', return_value=True).start()
        self.assertTrue(any('Steam is running' in reason
                            for reason in luna.refuse_reasons(for_install=True, stop_sidecar=True)))
        self.assertFalse([call for call in self.calls if call[0] == 'pkill'])

    def test_bootstrap_and_update_all_dry_runs_write_nothing(self):
        snapshot = lambda: sorted(str(p) for p in self.home.rglob('*'))  # noqa: E731
        mock.patch.object(luna, 'base_state', return_value='none').start()
        before = snapshot()
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(luna.cmd_bootstrap(argparse.Namespace(dry_run=True)), 0)
            self.assertEqual(luna.cmd_update_all(argparse.Namespace(dry_run=True)), 0)
        self.assertIn('bootstrap', out.getvalue())
        self.assertIn('update-all: would', out.getvalue())
        self.assertEqual(before, snapshot())
        self.assertFalse([call for call in self.calls if call and call[0] == 'sudo'])

    # -- system packages / known-good build --------------------------------------
    def _pm(self, manager, installed=(), tools=(), foreign=(), multilib=True):
        """Fake package manager: `installed` packages, `tools` on PATH, dpkg foreign archs."""
        conf = self.home / 'pacman.conf'
        conf.write_text('[options]\n' + ('[multilib]\nInclude = /etc/pacman.d/mirrorlist\n' if multilib else ''))
        mock.patch.dict(os.environ, {'LUNA_PACMAN_CONF': str(conf)}).start()
        pm_tool = {'apt-get': 'apt-get', 'pacman': 'pacman', 'dnf': 'dnf'}[manager]
        mock.patch.object(luna.shutil, 'which',
                          side_effect=lambda name: '/bin/' + name if name == pm_tool or name in tools else None).start()
        steam_sh = self.home / '.local/share/Steam/steam.sh'
        if steam_sh.exists():
            steam_sh.unlink()

        def fake_run(command, *args, **kw):
            self.calls.append(list(command))
            if command[0] in ('pacman', 'rpm') and command[1] in ('-Q', '-q'):
                return mock.Mock(returncode=0 if command[2] in installed else 1, stdout='', stderr='')
            if command[0] == 'dpkg-query':
                ok = command[-1] in installed
                return mock.Mock(returncode=0 if ok else 1, stdout='install ok installed' if ok else '', stderr='')
            if command[:2] == ['dpkg', '--print-foreign-architectures']:
                return mock.Mock(returncode=0, stdout=' '.join(foreign), stderr='')
            return mock.Mock(returncode=0, stdout='', stderr='')
        luna.subprocess.run.side_effect = fake_run

    def _bootstrap_packages(self, tty=False, answer='y', **kwargs):
        self.calls.clear()
        out = io.StringIO()
        options = argparse.Namespace(dry_run=kwargs.pop('dry_run', False), **kwargs)
        with mock.patch.object(luna.sys.stdin, 'isatty', return_value=tty), \
                mock.patch('builtins.input', return_value=answer), contextlib.redirect_stdout(out):
            luna.install_system_packages(options, lambda text: print(text))
        return out.getvalue(), [call for call in self.calls if call and call[0] in ('sudo',)]

    def test_pacman_packages_and_sudo_prompt(self):
        self._pm('pacman')
        output, sudo = self._bootstrap_packages(tty=True)
        self.assertEqual(len(sudo), 1)
        command = sudo[0]
        self.assertEqual(command[:4], ['sudo', 'pacman', '-S', '--needed'])
        for name in ('steam', 'lib32-openssl', 'mangohud', 'lib32-mangohud', 'gamescope', 'gamemode',
                     'lib32-gamemode', 'zenity', 'jq', 'curl', 'unzip', 'git', 'tar', 'python3'):
            self.assertIn(name, command)
        self.assertIn('Vulkan', output)

    def test_apt_adds_i386_then_updates_then_installs(self):
        self._pm('apt-get')
        _output, sudo = self._bootstrap_packages(tty=True)
        self.assertEqual(sudo[0], ['sudo', 'dpkg', '--add-architecture', 'i386'])
        self.assertEqual(sudo[1], ['sudo', 'apt-get', 'update'])
        for name in ('steam-installer', 'libssl3:i386', 'mangohud:i386', 'gamemode:i386', 'zenity'):
            self.assertIn(name, sudo[2])
        self.calls.clear()
        self._pm('apt-get', foreign=['i386'])
        _output, sudo = self._bootstrap_packages(tty=True)
        self.assertNotIn(['sudo', 'dpkg', '--add-architecture', 'i386'], sudo)

    def test_dnf_packages(self):
        self._pm('dnf')
        _output, sudo = self._bootstrap_packages(tty=True)
        self.assertEqual(sudo[0][:4], ['sudo', 'dnf', 'install', '-y'])
        for name in ('openssl-libs.i686', 'mangohud.i686', 'gamemode.i686', 'gamescope', 'steam'):
            self.assertIn(name, sudo[0])

    def test_installed_packages_are_skipped_and_opt_outs_work(self):
        everything = {'steam', 'lib32-openssl', 'mangohud', 'lib32-mangohud', 'gamemode', 'lib32-gamemode'}
        self._pm('pacman', installed=everything, tools={'curl', 'jq', 'tar', 'unzip', 'git', 'python3',
                                                         'zenity', 'gamescope', 'steam'})
        output, sudo = self._bootstrap_packages(tty=True)
        self.assertEqual(sudo, [])
        self.assertIn('all present', output)
        self._pm('pacman', installed={'steam', 'lib32-openssl'}, tools={'curl', 'jq', 'tar', 'unzip', 'git',
                                                                         'python3', 'zenity'})
        _output, sudo = self._bootstrap_packages(tty=True, no_mangohud=True, no_gamemode=True)
        self.assertEqual(sudo[0][4:], ['gamescope'])
        output, sudo = self._bootstrap_packages(tty=True, no_system_packages=True)
        self.assertEqual(sudo, [])
        self.assertIn('skipped', output)

    def test_non_interactive_prints_command_and_never_runs_sudo(self):
        self._pm('pacman')
        output, sudo = self._bootstrap_packages(tty=False)
        self.assertEqual(sudo, [])
        self.assertIn('NOT installed', output)
        self.assertIn('sudo pacman -S --needed', output)

    def test_dry_run_prints_commands_without_running_them(self):
        self._pm('apt-get')
        output, sudo = self._bootstrap_packages(tty=True, dry_run=True)
        self.assertEqual(sudo, [])
        self.assertIn('would run', output)
        self.assertIn('sudo dpkg --add-architecture i386', output)
        self.assertFalse([call for call in self.calls if call[:2] in (['apt-get', 'install'], ['apt-get', 'update'])])

    def test_missing_multilib_is_reported_and_32bit_skipped(self):
        self._pm('pacman', multilib=False)
        output, sudo = self._bootstrap_packages(tty=True)
        self.assertIn('[multilib]', output)
        self.assertNotIn('steam', sudo[0][4:])
        self.assertFalse([name for name in sudo[0] if name.startswith('lib32-')])
        self.assertIn('mangohud', sudo[0])

    def test_known_good_build_verified_or_warned(self):
        manifest = self.root / 'package' / 'steam_client_ubuntu12.manifest'
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text('"manifest"\n{\n\t"version" "%s"\n}\n' % luna.KNOWN_GOOD_UBUNTU12_BUILD)
        ok, line = luna.known_good_line()
        self.assertTrue(ok)
        self.assertEqual(line, 'Steam client: known-good ubuntu12 build 1792618285 (Oct 22 2026)')
        manifest.write_text('"manifest"\n{\n\t"version" "1700000000"\n}\n')
        ok, line = luna.known_good_line()
        self.assertFalse(ok)
        self.assertTrue(line.startswith('WARN:'))
        self.assertIn('1700000000', line)

    # -- pinned manifest rollback / libcrypto / history --------------------------
    PINNED = ('"manifest"\n{\n\t"version" "777"\n}\n').encode()
    ELF32 = b'\x7fELF\x01\x01\x01' + b'\0' * 9 + b'\x03\x00\x03\x00' + b'stub-libcrypto'
    ELF64 = b'\x7fELF\x02\x01\x01' + b'\0' * 9 + b'\x03\x00\x3e\x00' + b'stub'

    def _pin_fixture(self, hit=True, served=None):
        """Pin the fixture manifest and make the fake launcher fetch it from the local server."""
        import http.client
        mock.patch.object(luna, 'KNOWN_GOOD_MANIFEST_SHA256', hashlib.sha256(self.PINNED).hexdigest()).start()
        mock.patch.object(luna, 'KNOWN_GOOD_UBUNTU12_BUILD', '777').start()
        luna._http_get.side_effect = lambda url, limit=0: self.PINNED
        launches = []
        calls = self.calls

        def fake_run(command, *args, **kw):
            calls.append(list(command))
            if '-overridepackageurl' in command:
                launches.append(list(command))
                if hit:
                    url = command[command.index('-overridepackageurl') + 1]
                    connection = http.client.HTTPConnection(url.replace('http://', ''))
                    connection.request('GET', '/steam_client_ubuntu12')
                    body = connection.getresponse().read()
                    if served is not None:
                        served.append(body)
                    self.manifest.write_bytes(body)
                return mock.Mock(returncode=7)
            return mock.Mock(returncode=1, stdout='', stderr='')
        luna.subprocess.run.side_effect = fake_run
        return launches

    def test_pinned_manifest_rollback_uses_the_local_server(self):
        served = []
        launches = self._pin_fixture(served=served)
        code, output = self.steam('lock')
        self.assertEqual(code, 0, output)
        self.assertEqual(served, [self.PINNED])
        self.assertEqual(len(launches), 1)
        for flag in ('-forcemanifestsuffix', 'ubuntu12', '-forcesteamupdate', '-forcepackagedownload', '-exitsteam'):
            self.assertIn(flag, launches[0])
        self.assertTrue(launches[0][launches[0].index('-overridepackageurl') + 1].startswith('http://127.0.0.1:'))
        self.assertFalse([call for call in self.calls if call[-2:] == ['ubuntu12', '-exitsteam']])
        self.assertTrue(luna.steam_hard_lock_active())
        self.assertEqual(luna.read_steam_pin()['clientBuild'], '777')
        self.assertIn('pinned ubuntu12 manifest', output)

    def test_pinned_manifest_comes_from_the_cache_first(self):
        self._pin_fixture()
        luna._http_get.side_effect = OSError('cache must be used, not the network')
        cache = luna.steam_cache_dir() / 'package'
        cache.mkdir(parents=True)
        (cache / luna.STEAM_MANIFEST_NAME).write_bytes(self.PINNED)
        self.assertEqual(self.steam('lock')[0], 0)

    def test_pinned_manifest_sha_mismatch_is_refused(self):
        launches = self._pin_fixture()
        luna._http_get.side_effect = lambda url, limit=0: self.PINNED + b'tampered'
        code, output = self.steam('lock')
        self.assertEqual(code, 1)
        self.assertIn('REFUSING', output)
        self.assertEqual(launches, [])
        self.assertFalse([call for call in self.calls if call[0] == 'pkill'])
        self.assertFalse(luna.steam_pin_path().exists())

    def test_manifest_server_serves_manifest_and_redirects_everything_else(self):
        import http.client
        with luna.ManifestServer(self.PINNED) as server:
            connection = http.client.HTTPConnection('127.0.0.1', server.port)
            for path in ('/steam_client_ubuntu12', '/steam_client_ubuntu12.manifest',
                         '/client/steam_client_ubuntu12'):
                connection.request('GET', path)
                response = connection.getresponse()
                self.assertEqual((response.status, response.read()), (200, self.PINNED))
            self.assertEqual(server.hits, 3)
            connection.request('GET', '/client/ubuntu12_32/pkg_1.zip.vz')
            response = connection.getresponse()
            response.read()
            self.assertEqual(response.status, 302)
            self.assertEqual(response.getheader('Location'),
                             'https://client-fastly.steamstatic.com/client/ubuntu12_32/pkg_1.zip.vz')
            connection.request('GET', '/other.zip')
            response = connection.getresponse()
            response.read()
            self.assertEqual(response.getheader('Location'), 'https://client-fastly.steamstatic.com/client/other.zip')
            self.assertEqual(server.hits, 3)

    def test_zero_manifest_hits_fail_and_leave_package_writable(self):
        launches = self._pin_fixture(hit=False)
        code, output = self.steam('lock')
        self.assertEqual(code, 1)
        self.assertEqual(len(launches), 2)  # ubuntu12_32/steam, then steam.sh
        self.assertIn('never requested the pinned manifest', output)
        self.assertTrue(os.access(self.packages, os.W_OK))
        self.assertFalse(luna.steam_hard_lock_active())
        self.assertFalse(luna.steam_pin_path().exists())

    def test_unobtainable_pinned_manifest_falls_back_with_a_warning(self):
        code, output = self.steam('lock')  # setUp: offline, empty cache
        self.assertEqual(code, 0, output)
        self.assertIn('WARN', output)
        self.assertIn('retired', output)
        self.assertTrue([call for call in self.calls if call[-3:] == ['-forcemanifestsuffix', 'ubuntu12', '-exitsteam']])

    def _lib(self):
        return self.root / 'ubuntu12_32' / 'libcrypto.so.3'

    def test_libcrypto_installs_from_a_system_elf32(self):
        system = self.home / 'lib32'
        system.mkdir()
        (system / 'libcrypto.so.3').write_bytes(self.ELF32)
        mock.patch.object(luna, 'LIBCRYPTO_SYSTEM_PATHS', [str(system / 'libcrypto.so.3')]).start()
        self.assertIn('missing', luna.libcrypto_status()[1])
        code, message = luna.install_libcrypto()
        self.assertEqual(code, 0, message)
        self.assertIn('system', message)
        self.assertEqual(self._lib().read_bytes(), self.ELF32)
        self.assertEqual(stat.S_IMODE(self._lib().stat().st_mode), 0o755)
        self.assertEqual(luna.libcrypto_status()[0], 'present')
        self.assertEqual(luna.install_libcrypto()[0], 0)  # idempotent

    def test_libcrypto_downloads_the_pinned_asset_and_checks_hash_and_elf(self):
        mock.patch.object(luna, 'LIBCRYPTO_SHA256', hashlib.sha256(self.ELF32).hexdigest()).start()
        luna._http_get.side_effect = lambda url, limit=0: self.ELF32
        code, message = luna.install_libcrypto()
        self.assertEqual(code, 0, message)
        self.assertIn('SteamRollbackrLite', message)
        self._lib().unlink()
        luna._http_get.side_effect = lambda url, limit=0: self.ELF32 + b'x'
        self.assertEqual(luna.install_libcrypto()[0], 1)  # hash mismatch
        self.assertFalse(self._lib().exists())
        # a matching hash on a non-ELF32 payload is refused too
        mock.patch.object(luna, 'LIBCRYPTO_SHA256', hashlib.sha256(self.ELF64).hexdigest()).start()
        luna._http_get.side_effect = lambda url, limit=0: self.ELF64
        self.assertEqual(luna.install_libcrypto()[0], 1)
        self.assertFalse(self._lib().exists())

    def test_libcrypto_ignores_a_64bit_system_library(self):
        system = self.home / 'lib'
        system.mkdir()
        (system / 'libcrypto.so.3').write_bytes(self.ELF64)
        mock.patch.object(luna, 'LIBCRYPTO_SYSTEM_PATHS', [str(system / 'libcrypto.so.3')]).start()
        code, message = luna.install_libcrypto()
        self.assertEqual(code, 1)  # falls through to the (offline) download
        self.assertFalse(self._lib().exists())

    def test_libcrypto_never_overwrites_without_force_and_remove_works(self):
        system = self.home / 'lib32'
        system.mkdir()
        (system / 'libcrypto.so.3').write_bytes(self.ELF32)
        mock.patch.object(luna, 'LIBCRYPTO_SYSTEM_PATHS', [str(system / 'libcrypto.so.3')]).start()
        self._lib().write_bytes(b'something else')
        code, message = luna.install_libcrypto()
        self.assertEqual(code, luna.EXIT_REFUSED)
        self.assertEqual(self._lib().read_bytes(), b'something else')
        self.assertEqual(luna.install_libcrypto(force=True)[0], 0)
        self.assertEqual(self._lib().read_bytes(), self.ELF32)
        code, _message = luna.remove_libcrypto()
        self.assertEqual(code, 0)
        self.assertFalse(self._lib().exists())
        self.assertEqual(luna.remove_libcrypto()[0], 0)

    def test_heal_installs_a_missing_libcrypto_only_when_pinned_and_status_reports_it(self):
        system = self.home / 'lib32'
        system.mkdir()
        (system / 'libcrypto.so.3').write_bytes(self.ELF32)
        mock.patch.object(luna, 'LIBCRYPTO_SYSTEM_PATHS', [str(system / 'libcrypto.so.3')]).start()
        self.assertFalse(luna.heal_steam_lock()['pinned'])
        self.assertFalse(self._lib().exists())
        self.assertEqual(self.steam('lock')[0], 0)  # lock itself installs it
        self.assertTrue(self._lib().exists())
        self._lib().unlink()
        self.assertTrue(any('libcrypto' in line for line in luna.heal_steam_lock(apply=False)['notes']))
        self.assertFalse(self._lib().exists())
        self.assertTrue(any('libcrypto' in line for line in luna.heal_steam_lock()['fixed']))
        self.assertTrue(self._lib().exists())
        self.assertIn('libcrypto: 32-bit libcrypto.so.3 present', self.steam('status')[1])

    def test_history_marks_known_good_and_deletions(self):
        payload = [
            {'sha': '21f29ef59b3df12a223b47c068f0dd05ac29f686', 'commit': {
                'message': 'Delete retired ubuntu12 client manifests\n\nbody',
                'author': {'date': '2026-11-12T10:00:00Z'}}},
            {'sha': luna.KNOWN_GOOD_MANIFEST_COMMIT, 'commit': {
                'message': 'Update', 'author': {'date': '2026-10-22T10:00:00Z'}}}]
        lines = luna.format_history(payload)
        self.assertIn('manifest deleted', lines[0])
        self.assertIn('2026-11-12', lines[0])
        self.assertNotIn('body', lines[0])
        self.assertIn('KNOWN-GOOD', lines[1])
        luna._http_get.side_effect = lambda url, limit=0: json.dumps(payload).encode()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(luna.cmd_history(argparse.Namespace()), 0)
        self.assertIn('KNOWN-GOOD', out.getvalue())
        luna._http_get.side_effect = luna.urllib.error.HTTPError('u', 403, 'rate', {}, None)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(luna.cmd_history(argparse.Namespace()), 1)
        self.assertIn('rate limit', err.getvalue())
        luna._http_get.side_effect = OSError('offline')
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(luna.cmd_history(argparse.Namespace()), 1)

    def test_flatpak_and_gaming_mode_warnings(self):
        shutil.rmtree(self.root)
        (self.home / '.var/app/com.valvesoftware.Steam').mkdir(parents=True)
        self.assertTrue(luna.flatpak_steam_only())
        osr = self.home / 'os-release'
        osr.write_text('NAME="SteamOS"\nID=steamos\n')
        self.assertFalse(luna.steamos_gaming_mode())  # no gamescope-session running (pgrep mocked to fail)
        luna.subprocess.run.side_effect = lambda *a, **k: mock.Mock(returncode=0)
        self.assertTrue(luna.steamos_gaming_mode())



ELF32 = b'\x7fELF\x01\x01\x01' + bytes(9)
ELF64 = b'\x7fELF\x02\x01\x01' + bytes(9)


class SteamClientArchTestCase(unittest.TestCase):
    """Which client is reported: the one Valve's steam.sh selects (steamrt64
    unless .steam-disable-steamrt64-client exists or steamrt64/steam is absent)."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='luna-steam-arch-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.root = self.home / '.local/share/Steam'
        (self.root / 'package').mkdir(parents=True)
        (self.root / 'logs').mkdir()
        # Newer clients log 0: no information, so the manifest decides.
        (self.root / 'logs/console_log.txt').write_text('Client version: 0\n')
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(luna, 'home', return_value=self.home).start()
        mock.patch.object(luna, 'process_running', return_value=False).start()
        # LUNA_HOME / LUMEN_DIR too: luna_tools_dir() follows them, and an
        # inherited one would point this fixture's receipt at a real install.
        mock.patch.dict(os.environ, {'HOME': str(self.home),
                                    'XDG_DATA_HOME': str(self.home / '.local/share'),
                                    'LUNA_HOME': str(self.home / '.local/share/Luna'),
                                    'LUMEN_DIR': str(self.home / '.local/share/Lumen')}).start()
        os.environ.pop('STEAM_FORCE_CLIENT', None)
        self.sls = self.home / '.local/share/SLSsteam'
        self.sls.mkdir(parents=True)

    def write(self, rel, data, mode=0o755):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(mode)

    def manifest(self, client, build):
        self.write('package/steam_client_%s.manifest' % client,
                   ('"%s" { "version" "%s" }' % (client, build)).encode(), 0o644)

    def this_machine(self):
        """The live layout after the 64-bit switch: both binaries, both
        manifests, a stale ubuntu12 one, no marker, an old ubuntu12 receipt."""
        self.write('ubuntu12_32/steam', ELF32)
        self.write('steamrt64/steam', ELF64)
        self.manifest('ubuntu12', '1792618285')
        self.manifest('steamrt64', '1794536281')
        tools = luna.luna_tools_dir()
        tools.mkdir(parents=True, exist_ok=True)
        core = {}
        for name in luna.STEAM_CORE_FILES:
            path = self.root / luna._steam_payload_path(name)
            if not path.exists():
                self.write(luna._steam_payload_path(name), ELF32)
            core[name] = luna.sha256(path)
        luna.write_json_file(tools / 'steam-client-installed.json',
                             {'build': '1792618285', 'core': core})

    def status_text(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            luna.cmd_steam(argparse.Namespace(steam_action='status'))
        return out.getvalue()

    def test_a_64_bit_install_reports_the_steamrt64_build_not_the_stale_ubuntu12_one(self):
        self.this_machine()
        self.assertEqual(luna.steam_client_kind(), 'steamrt64')
        self.assertEqual(luna.steam_client_build(), '1794536281')
        state = luna.steam_state()
        self.assertEqual(state['client'], 'steamrt64')
        self.assertIsNone(state['binariesMatch'], 'an ubuntu12 receipt says nothing about a 64-bit client')
        self.assertIn('client : 1794536281 (steamrt64, 64-bit)', self.status_text())

    def test_the_disable_marker_or_a_missing_steamrt64_binary_selects_ubuntu12(self):
        self.this_machine()
        self.write('.steam-disable-steamrt64-client', b'', 0o644)
        self.assertEqual(luna.steam_client_kind(), 'ubuntu12')
        self.assertEqual(luna.steam_client_build(), '1792618285')
        (self.root / '.steam-disable-steamrt64-client').unlink()
        (self.root / 'steamrt64/steam').unlink()
        self.assertEqual(luna.steam_client_build(), '1792618285')
        self.assertIn('(ubuntu12, 32-bit)', self.status_text())

    def test_an_slssteam_built_for_the_other_client_is_reported_as_a_mismatch(self):
        self.this_machine()
        (self.sls / 'SLSsteam.so').write_bytes(ELF32)
        text = self.status_text()
        self.assertIn('SLSsteam arch:   32-bit', text)
        self.assertIn('MISMATCH', text)
        self.assertIn('cannot load', luna.steam_drift_note())
        (self.sls / 'SLSsteam.so').write_bytes(ELF64)
        text = self.status_text()
        self.assertIn('SLSsteam arch:   64-bit', text)
        self.assertNotIn('MISMATCH', text)

    def test_steam_force_client_and_a_non_executable_steamrt64_select_ubuntu12(self):
        self.this_machine()
        with mock.patch.dict(os.environ, {'STEAM_FORCE_CLIENT': 'ubuntu12_32'}):
            self.assertEqual(luna.steam_client_kind(), 'ubuntu12')
            self.assertEqual(luna.steam_client_build(), '1792618285')
        (self.root / 'steamrt64/steam').chmod(0o644)
        self.assertEqual(luna.steam_client_kind(), 'ubuntu12')

    def test_a_leftover_32_bit_hard_lock_still_names_the_64_bit_client_and_its_mismatch(self):
        self.this_machine()
        (self.sls / 'SLSsteam.so').write_bytes(ELF32)
        luna.write_json_file(luna.steam_pin_path(), {'clientBuild': '1792618285',
                                                     'hardLock': {'rollback': 'ubuntu12'}})
        with self.assertRaises(ValueError):
            luna.active_steam_elf_class()  # update operations still fail closed
        self.assertEqual(luna.steam_client_kind(), 'steamrt64')
        self.assertEqual(luna.steam_client_build(), '1794536281')
        self.assertIn('cannot load', luna.slssteam_mismatch_note(luna.steam_state()))

    def test_pin_cache_recover_and_update_refuse_a_64_bit_client_and_change_nothing(self):
        self.this_machine()
        before = sorted((p, p.stat().st_mtime_ns) for p in self.home.rglob('*') if p.is_file())
        for action in ('pin', 'cache', 'recover', 'update'):
            err = io.StringIO()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                code = luna.cmd_steam(argparse.Namespace(steam_action=action, build=None, now=False,
                                                         force=False, relaunch=False, only=[]))
            self.assertEqual(code, luna.EXIT_REFUSED, action)
            self.assertIn('64-bit steamrt64', err.getvalue())
        with self.assertRaises(ValueError):
            luna._cache_client_packages('1794536281')
        after = sorted((p, p.stat().st_mtime_ns) for p in self.home.rglob('*') if p.is_file())
        self.assertEqual(before, after)

    def test_refresh_refuses_a_64_bit_client_before_writing_the_pin_or_block(self):
        self.this_machine()
        before = sorted((p, p.stat().st_mtime_ns) for p in self.home.rglob('*') if p.is_file())
        with mock.patch.object(luna, '_steam_force_update', side_effect=AssertionError('must not update')):
            ok, message, build = luna._refresh_steam_side()
        self.assertFalse(ok)
        self.assertIn('64-bit steamrt64', message)
        self.assertFalse(luna.steam_pin_path().exists())
        after = sorted((p, p.stat().st_mtime_ns) for p in self.home.rglob('*') if p.is_file())
        self.assertEqual(before, after)

    def test_refresh_stops_before_pinning_when_the_updater_moves_steam_to_64_bit(self):
        self.this_machine()
        self.write('.steam-disable-steamrt64-client', b'', 0o644)

        def update():
            (self.root / '.steam-disable-steamrt64-client').unlink()
            return True, 'updated'
        with mock.patch.object(luna, '_steam_force_update', side_effect=update):
            ok, message, _ = luna._refresh_steam_side()
        self.assertFalse(ok)
        self.assertIn('now runs the 64-bit client', message)
        self.assertFalse(luna.steam_pin_path().exists())

    def test_status_and_heal_write_nothing_on_an_unpinned_64_bit_install(self):
        self.this_machine()
        (self.sls / 'SLSsteam.so').write_bytes(ELF64)
        before = sorted((p, p.stat().st_mtime_ns) for p in self.home.rglob('*') if p.is_file())
        text = self.status_text()
        self.assertIn('pinned : none (64-bit pinning', text)
        self.assertEqual(luna.heal_steam_lock(apply=True)['fixed'], [])
        after = sorted((p, p.stat().st_mtime_ns) for p in self.home.rglob('*') if p.is_file())
        self.assertEqual(before, after)
        self.assertTrue((self.root / 'package/steam_client_steamrt64.manifest').is_file())


if __name__ == '__main__':
    unittest.main()
