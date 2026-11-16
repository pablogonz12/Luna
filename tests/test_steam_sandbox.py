"""Integration tests for the Steam-client operations against a throwaway Steam tree.

Unlike test_steam_client.py (focused unit-ish checks) this builds a realistic
sandbox HOME - Valve-style manifest with several hash-named .zip.vz packages,
ubuntu12_32/, steam.sh, steam.cfg variants, SLSsteam placeholders, localconfig,
Lumen dirs - and runs Luna's real verify/extract/install/recover/cache/pin/
lock/unlock code on it. Only the network, process checks and Steam launching
are replaced. Nothing may be written outside the temp HOME (guarded below).
"""
import argparse
import contextlib
import hashlib
import importlib.util
import io
import lzma
import os
os.environ.setdefault('LUNA_GUI_CHECKS', '0')  # hermetic: tests never probe the real desktop (test_gui_check manages it itself)
os.environ.setdefault('LUNA_LIVE_CHECKS', '0')  # hermetic: tests never contact a real Steam (test_live_probe uses a fake endpoint)
from pathlib import Path
import stat
import sys
import struct
import tempfile
import unittest
from unittest import mock
import zipfile
import zlib

from test_luna import UPSTREAM

SPEC = importlib.util.spec_from_file_location(
    'luna_sandbox_test', Path(__file__).resolve().parents[1] / 'scripts/luna.py')
luna = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(luna)

REAL_HOME = Path(os.path.expanduser('~'))
REAL_GUARDED = [REAL_HOME / '.local/share/Steam/package', REAL_HOME / '.local/share/Steam/steam.cfg',
                REAL_HOME / '.local/share/Steam/Steam.cfg', REAL_HOME / '.local/share/Lumen']
BUILD = '1791918634'
HARD_BLOCK_KEYS = ('BootStrapperInhibitAll=enable', 'BootStrapperInhibitUpdateOnLaunch=enable')


def _mtime_state(paths):
    state = {}
    for base in paths:
        try:
            state[str(base)] = (base.stat().st_mtime_ns, stat.S_IMODE(base.stat().st_mode))
        except OSError:
            state[str(base)] = None
    return state


def make_vz(raw):
    filters = [{'id': lzma.FILTER_LZMA1, 'dict_size': 1 << 20, 'lc': 3, 'lp': 0, 'pb': 2}]
    return (b'VZa' + struct.pack('<I', 0) + b'\x5d' + struct.pack('<I', 1 << 20)
            + lzma.compress(raw, format=lzma.FORMAT_RAW, filters=filters)
            + struct.pack('<II', zlib.crc32(raw), len(raw)) + b'zv')


def make_zip(files):
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w', compression=zipfile.ZIP_STORED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return out.getvalue()


class SandboxCase(unittest.TestCase):
    def setUp(self):
        self._real_before = _mtime_state(REAL_GUARDED)
        self.temp = tempfile.TemporaryDirectory(prefix='luna-sandbox-')
        self.addCleanup(self._cleanup)
        self.home = Path(self.temp.name).resolve()
        self.root = self.home / '.local/share/Steam'
        self.package = self.root / 'package'
        self.lumen = self.home / '.local/share/Lumen'
        self.addCleanup(mock.patch.stopall)
        mock.patch.dict(os.environ, {'HOME': str(self.home), 'XDG_DATA_HOME': str(self.home / '.local/share'),
                                     'XDG_RUNTIME_DIR': str(self.home / 'run')}).start()
        os.environ.pop('LUMEN_DIR', None)
        os.environ.pop('LUNA_STEAM_SH', None)
        mock.patch.object(Path, 'home', return_value=self.home).start()
        self.running = set()
        mock.patch.object(luna, 'process_running', side_effect=lambda name: name in self.running).start()
        # No real network: urlopen serves only what a test puts in self.cdn
        # (keyed by package file name); anything else is an error.
        self.cdn = {}
        self.fetched = []

        def fake_urlopen(request, timeout=None):
            name = request.full_url.rsplit('/', 1)[-1]
            self.fetched.append(name)
            if name not in self.cdn:
                raise OSError('offline sandbox: %s' % request.full_url)
            return io.BytesIO(self.cdn[name])

        mock.patch.object(luna.urllib.request, 'urlopen', side_effect=fake_urlopen).start()
        mock.patch.object(luna, 'pacman_steam_state', return_value=(False, False)).start()
        # Launching Steam (setsid steam / steam) is recorded, never executed;
        # fake updater scripts under the temp HOME still run for real.
        real_popen = luna.subprocess.Popen
        self.launches = []

        def guarded_popen(args, *a, **k):
            if str(args[0]) in ('setsid', 'steam'):
                self.launches.append(args)
                return mock.Mock()
            if str(args[0]) in (sys.executable, 'git'):  # patch-lumen.py, aimed at the sandbox Lumen
                return real_popen(args, *a, **k)
            assert str(args[0]).startswith(str(self.home)), 'unexpected process: %r' % (args,)
            return real_popen(args, *a, **k)

        mock.patch.object(luna.subprocess, 'Popen', side_effect=guarded_popen).start()
        self.build_world()

    def _cleanup(self):
        # Restore write bits so TemporaryDirectory can delete read-only package dirs.
        for path in [self.package, self.lumen / 'sls-luna/steam-client-cache/package']:
            if path.is_dir():
                path.chmod(0o755)
        self.temp.cleanup()
        self.assertEqual(self._real_before, _mtime_state(REAL_GUARDED),
                         'a test touched the real Steam/Lumen directories')

    # ---- fixture -------------------------------------------------------
    def build_world(self):
        self.package.mkdir(parents=True)
        client = make_zip({
            'steam.sh': b'#!/bin/sh\n# compat launcher\n',
            'ubuntu12_32/steam': b'\x7fELF-steam',
            'ubuntu12_32/steamclient.so': b'\x7fELF-steamclient',
            'ubuntu12_64/steamwebhelper': b'\x7fELF-webhelper',
            'ubuntu12_32/steam-runtime.txt': b'plain\n',
        })
        extra = make_zip({'ubuntu12_32/libextra.so': b'\x7fELF-extra', 'public/readme.txt': b'hi\n'})
        plain = make_zip({'resources/strings.txt': b'strings\n'})
        client_vz, extra_vz = make_vz(client), make_vz(extra)
        files = {
            'bins_ubuntu12.zip.vz.%s_%d' % ('a' * 40, len(client_vz)): client_vz,
            'bins_misc_ubuntu12.zip.vz.%s_%d' % ('b' * 40, len(extra_vz)): extra_vz,
            'resources_all.zip.%s' % ('c' * 40): plain,
        }
        names = list(files)
        manifest = ['"ubuntu12"', '{', '\t"version"\t\t"%s"' % BUILD]
        for index, (name, data) in enumerate(files.items()):
            digest = hashlib.sha256(data).hexdigest()
            manifest += ['\t"pkg%d"' % index, '\t{']
            if '.vz.' in name:
                manifest += ['\t\t"file"\t\t"%s"' % name.split('.vz.')[0] + '.zip.' + 'd' * 40,
                             '\t\t"size"\t\t"1"', '\t\t"sha2"\t\t"%s"' % ('0' * 64),
                             '\t\t"zipvz"\t\t"%s"' % name, '\t\t"sha2vz"\t\t"%s"' % digest]
            else:
                manifest += ['\t\t"file"\t\t"%s"' % name, '\t\t"size"\t\t"1"', '\t\t"sha2"\t\t"%s"' % digest]
            manifest += ['\t}']
        manifest += ['}', '']
        self.manifest_bytes = '\n'.join(manifest).encode()
        mock.patch.object(luna, 'STEAM_COMPAT_MANIFEST_SHA256',
                          hashlib.sha256(self.manifest_bytes).hexdigest()).start()
        self.pkg_names = names
        self.pkg_files = files
        # Installed package/ tree: the manifest, the verified packages and
        # unrelated leftovers a real Steam keeps there.
        (self.package / luna.STEAM_MANIFEST_NAME).write_bytes(self.manifest_bytes)
        for name, data in files.items():
            (self.package / name).write_bytes(data)
        (self.package / 'steam_client_steamrt64.manifest').write_text('"steamrt64"{"version" "9"}\n')
        # Old (pre-install) client binaries.
        (self.root / 'ubuntu12_32').mkdir(parents=True)
        (self.root / 'ubuntu12_64').mkdir()
        (self.root / 'steam.sh').write_text('#!/bin/sh\n# newer steamrt64 bootstrap\n')
        for name in luna.STEAM_CORE_FILES:
            (self.root / name).write_bytes(b'\x7fELF-newer-' + name.encode())
            (self.root / name).chmod(0o755)
        (self.root / 'config').mkdir()
        (self.root / 'config/localconfig.vdf').write_text('"UserLocalConfigStore" {}\n')
        (self.root / 'config/loginusers.vdf').write_text('private sentinel\n')
        (self.root / 'logs').mkdir()
        (self.root / 'logs/console_log.txt').write_text('Client version: 0\n')
        slssteam = self.home / '.local/share/SLSsteam'
        slssteam.mkdir(parents=True)
        (slssteam / 'SLSsteam.so').write_bytes(b'\x7fELF-sls')
        (slssteam / 'library-inject.so').write_bytes(b'\x7fELF-inject')
        (self.lumen / 'lua').mkdir(parents=True)
        (self.lumen / 'lua/boot.lua').write_text(UPSTREAM)
        (self.lumen / 'luatools/backend').mkdir(parents=True)
        (self.lumen / 'luatools/backend/main.lua').write_text('-- fixture')
        self.tools = luna.luna_tools_dir()
        self.assertEqual(self.tools, self.lumen / 'sls-luna')

    # ---- helpers -------------------------------------------------------
    def foreign_lock(self, keys=HARD_BLOCK_KEYS):
        """A community lock: read-only package/, cfg files, no Luna pin record."""
        # The newer Steam that ran before the manual lock left its own manifest,
        # so Luna has to replace it inside the read-only directory (the crash).
        (self.package / luna.STEAM_MANIFEST_NAME).write_text('"ubuntu12"{"version" "1999999999"}\n')
        self.cdn['steam_client_ubuntu12'] = self.manifest_bytes  # Luna re-fetches the pinned manifest
        for name in ('steam.cfg', 'Steam.cfg'):
            (self.root / name).write_text('\n'.join(keys) + '\n')
        self.package.chmod(0o555)

    def mode(self, path):
        return stat.S_IMODE(Path(path).stat().st_mode)

    def package_writable(self):
        return bool(self.package.stat().st_mode & stat.S_IWUSR)

    def install(self):
        return luna.ensure_compatible_steam()

    def steam_cli(self, action, **kwargs):
        options = argparse.Namespace(steam_action=action, build=kwargs.pop('build', ''), **kwargs)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = luna.cmd_steam(options)
        return code, out.getvalue() + err.getvalue()

    def tree(self):
        return {str(p.relative_to(self.home)): p.read_bytes()
                for p in self.home.rglob('*') if p.is_file() and not p.is_symlink()}

    def install_ok(self):
        ok, message = self.install()
        self.assertTrue(ok, message)
        return message


class FreshInstallTests(SandboxCase):
    def test_fresh_install_extracts_verifies_pins_and_caches(self):
        self.install_ok()
        self.assertEqual((self.root / 'ubuntu12_32/steam').read_bytes(), b'\x7fELF-steam')
        self.assertEqual((self.root / 'ubuntu12_32/libextra.so').read_bytes(), b'\x7fELF-extra')
        self.assertEqual((self.root / 'public/readme.txt').read_bytes(), b'hi\n')
        self.assertEqual(self.mode(self.root / 'ubuntu12_32/steam'), 0o755)
        self.assertEqual(self.mode(self.root / 'ubuntu12_32/steam-runtime.txt'), 0o644)
        self.assertEqual(self.mode(self.root / 'ubuntu12_32'), self.mode(self.root / 'ubuntu12_32'))
        self.assertTrue(self.package_writable())
        # cfg, pin, cache, receipt, backup
        self.assertEqual((self.root / 'steam.cfg').read_text(), luna.STEAM_CFG_BLOCK)
        self.assertEqual(luna.read_steam_pin()['clientBuild'], BUILD)
        self.assertEqual(luna.cached_client_build(), BUILD)
        self.assertTrue(luna.packages_match_cache())
        state = luna.steam_state()
        self.assertTrue(state['match'])
        self.assertTrue(state['binariesMatch'])
        self.assertEqual(luna.steam_client_build(), BUILD)
        backups = list((self.tools / 'steam-client-backups').iterdir())
        self.assertEqual(len(backups), 1)
        self.assertTrue((backups[0] / 'restore.json').is_file())
        # protected data untouched, nothing stray left behind
        self.assertEqual((self.root / 'config/loginusers.vdf').read_text(), 'private sentinel\n')
        self.assertEqual(list(self.home.rglob('*.luna-new')), [])
        self.assertEqual(list(self.tools.glob('steam-client-stage-*')), [])

    def test_second_install_is_idempotent(self):
        self.install_ok()
        stamps = {p: p.stat().st_mtime_ns for p in (self.root / 'ubuntu12_32').iterdir()}
        self.install_ok()
        self.assertEqual(stamps, {p: p.stat().st_mtime_ns for p in (self.root / 'ubuntu12_32').iterdir()})

    def test_missing_steam_sh_refuses(self):
        (self.root / 'steam.sh').unlink()
        ok, message = self.install()
        self.assertFalse(ok)
        self.assertIn('installed and opened once', message)


class ForeignLockRegressionTests(SandboxCase):
    """Regression for fa4df17: a community lock left package/ read-only."""

    def test_install_over_foreign_readonly_package_and_cfg(self):
        self.foreign_lock()
        self.assertFalse(os.access(self.package, os.W_OK))
        self.assertFalse(luna.steam_hard_lock_active())
        self.install_ok()
        # packages were really refreshed through the temporarily writable dir
        self.assertEqual((self.root / 'ubuntu12_32/steam').read_bytes(), b'\x7fELF-steam')
        self.assertTrue(luna.packages_match_cache())
        # the foreign read-only bit is put back, not silently dropped
        self.assertFalse(self.package_writable())
        self.assertEqual(self.mode(self.package) & 0o222, 0)
        self.assertEqual(list(self.package.glob('*.luna-new')), [])
        # Luna's soft block replaced the foreign inhibit lines in steam.cfg
        self.assertIn('BootStrapperInhibitAll=enable', (self.root / 'steam.cfg').read_text())

    def test_foreign_lock_with_missing_package_file_is_repaired(self):
        name = self.pkg_names[0]
        (self.package / name).chmod(0o644)
        # corrupt-by-size manifest-listed package in a read-only dir
        self.package.chmod(0o755)
        (self.package / name).write_bytes(b'truncated')
        self.package.chmod(0o555)
        self.cdn[name] = self.pkg_files[name]  # nothing cached: re-download, verified
        self.install_ok()
        self.assertEqual(self.fetched, [name])
        self.assertEqual((self.package / name).read_bytes(), self.pkg_files[name])
        self.assertFalse(self.package_writable())

    def test_failed_install_still_restores_readonly_bit(self):
        self.foreign_lock()
        with mock.patch.object(luna, '_stage_steam_payload', side_effect=ValueError('boom')):
            ok, _ = self.install()
        self.assertFalse(ok)
        self.assertFalse(self.package_writable())

    def test_recover_over_foreign_readonly_package(self):
        self.install_ok()
        (self.root / 'ubuntu12_32/steam').write_bytes(b'newer')
        self.foreign_lock()
        ok, message = luna._recover_steam_client(BUILD)
        self.assertTrue(ok, message)
        self.assertEqual((self.root / 'ubuntu12_32/steam').read_bytes(), b'\x7fELF-steam')
        self.assertFalse(self.package_writable())

    def test_cmd_install_over_foreign_lock(self):
        self.foreign_lock()
        options = argparse.Namespace(only=[], force=False, dry_run=False, keep_steam_client=False,
                                     no_steam_machine=True, migrate_steam_machine=False)
        with mock.patch.object(luna, 'lumen_hook', return_value=0), \
                mock.patch.object(luna, 'steam_machine_action', return_value=0), \
                contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()):
            try:
                luna.cmd_install(options)
            except SystemExit:
                pass
        self.assertNotIn('PermissionError', out.getvalue())
        self.assertEqual((self.root / 'ubuntu12_32/steam').read_bytes(), b'\x7fELF-steam')


class HardLockTests(SandboxCase):
    def hard_lock(self):
        """Run the real lock code with only the 500 MB bootstrapper download faked."""
        self.install_ok()
        # The bootstrapper download re-creates the ubuntu12 packages; here the
        # installed (verified) packages already are that state.
        with mock.patch.object(luna, '_rollback_steam_client', return_value=(True, 'ok')):
            code, message = luna._lock_steam_client(False, lambda: False)
        self.assertEqual(code, 0, message)

    def test_lock_state_on_disk(self):
        self.hard_lock()
        self.assertFalse(self.package_writable())
        self.assertTrue(luna.steam_hard_lock_active())
        for name in ('steam.cfg', 'Steam.cfg'):
            self.assertEqual((self.root / name).read_text(), luna.STEAM_HARD_CFG_BLOCK)
        self.assertEqual(luna.cached_client_build(), BUILD)
        self.assertFalse((luna.steam_cache_dir() / 'package').stat().st_mode & 0o200 == 0)
        state = luna.steam_state()
        self.assertTrue(state['hardLock'] and state['packageReadOnly'])

    def test_locked_recover_and_cache_work_and_keep_the_lock(self):
        self.hard_lock()
        (self.root / 'ubuntu12_32/steamclient.so').write_bytes(b'drifted')
        self.assertFalse(luna.steam_state()['binariesMatch'])
        code, message = self.steam_cli('recover')
        self.assertEqual(code, 0, message)
        self.assertEqual((self.root / 'ubuntu12_32/steamclient.so').read_bytes(), b'\x7fELF-steamclient')
        self.assertFalse(self.package_writable())
        self.assertTrue(luna.steam_hard_lock_active())
        self.assertEqual((self.root / 'steam.cfg').read_text(), luna.STEAM_HARD_CFG_BLOCK)
        self.assertEqual((self.root / 'Steam.cfg').read_text(), luna.STEAM_HARD_CFG_BLOCK)
        code, message = self.steam_cli('cache')
        self.assertEqual(code, 0, message)
        self.assertTrue(luna.packages_match_cache())
        self.assertFalse(self.package_writable())

    def test_locked_install_keeps_both_cfgs_hard(self):
        self.hard_lock()
        self.install_ok()
        self.assertFalse(self.package_writable())
        self.assertEqual((self.root / 'Steam.cfg').read_text(), luna.STEAM_HARD_CFG_BLOCK)
        self.assertTrue(luna.steam_hard_lock_active())

    def test_update_and_unpin_refused_while_locked(self):
        self.hard_lock()
        for action in ('update', 'unpin'):
            before = self.tree()
            code, message = self.steam_cli(action, now=True)
            self.assertEqual(code, 1, action)
            self.assertIn('hard lock', message)
            self.assertEqual(before, self.tree())
        self.assertFalse(luna._steam_force_update()[0])

    def test_timer_applied_update_after_unlock(self):
        self.hard_lock()
        self.steam_cli('unlock')
        self.assertTrue(self.package_writable())
        updater = self.home / 'fake-steam.sh'
        updater.write_text('#!/bin/sh\n[ "$1" = "-shutdown" ] && exit 0\n'
                           'echo valve-update > "%s"\n'
                           'sed -i \'s/"version"[[:space:]]*"[0-9]*"/"version" "2000000000"/\' "%s"\n'
                           'exit 0\n' % (self.root / 'ubuntu12_32/steam', self.package / luna.STEAM_MANIFEST_NAME))
        updater.chmod(0o755)
        self.steam_cli('pin', build=BUILD)  # soft pin: Luna cfg block present
        luna.write_json_file(luna.steam_pending_path(), {'action': 'update', 'at': 1})
        with mock.patch.dict(os.environ, {'LUNA_STEAM_SH': str(updater)}):
            ok, message = luna._apply_steam_update()
        self.assertTrue(ok, message)
        self.assertFalse(luna.steam_cache_dir().exists())          # stale cache dropped
        self.assertFalse(luna.steam_pending_path().exists())       # task consumed
        self.assertEqual(luna.steam_client_build(), '2000000000')
        self.assertTrue(luna.steam_updates_blocked())              # block re-applied

    def test_unlock_restores_perms_and_keeps_foreign_cfg_keys(self):
        self.hard_lock()
        for name in ('steam.cfg', 'Steam.cfg'):
            path = self.root / name
            path.write_text(path.read_text() + 'SomeForeignKey=1\n')
        code, _ = self.steam_cli('unlock')
        self.assertEqual(code, 0)
        self.assertTrue(self.package_writable())
        self.assertEqual(self.mode(self.package) & 0o700, 0o700)
        self.assertFalse(luna.steam_hard_lock_active())
        self.assertFalse(luna.steam_pin_path().exists())
        for name in ('steam.cfg', 'Steam.cfg'):
            self.assertEqual((self.root / name).read_text().count('SomeForeignKey=1'), 1, name)

    def test_unlock_removes_pure_luna_cfg_files(self):
        self.hard_lock()
        self.steam_cli('unlock')
        self.assertFalse((self.root / 'steam.cfg').exists())
        self.assertFalse((self.root / 'Steam.cfg').exists())
        self.assertTrue(self.package_writable())
        self.assertEqual(self.launches, [])

    def test_unlock_now_launches_only_when_asked(self):
        self.hard_lock()
        self.steam_cli('unlock', now=True)
        self.assertEqual(len(self.launches), 1)

    def test_unlock_leaves_a_foreign_only_cfg_alone(self):
        self.foreign_lock()
        code, message = self.steam_cli('unlock')
        self.assertEqual(code, 0)
        # Not Luna's content: cfg files stay, but package/ is made writable
        self.assertTrue((self.root / 'steam.cfg').exists())
        self.assertIn('left in place', message)


class PinCacheTests(SandboxCase):
    def test_pin_unpin_roundtrip_preserves_foreign_cfg_keys(self):
        (self.root / 'steam.cfg').write_text('ForeignKey=1\nBootStrapperInhibitAll=disable\n')
        code, _ = self.steam_cli('pin', build=BUILD)
        self.assertEqual(code, 0)
        content = (self.root / 'steam.cfg').read_text()
        self.assertIn('ForeignKey=1', content)
        self.assertTrue(luna.steam_updates_blocked())
        self.assertNotIn('InhibitAll=disable', content)
        code, message = self.steam_cli('unpin')
        self.assertEqual(code, 0)
        self.assertFalse(luna.steam_pin_path().exists())
        self.assertIn('ForeignKey=1', (self.root / 'steam.cfg').read_text())
        self.assertIn('left untouched', message)

    def test_cache_then_recover_roundtrip_and_detects_tamper(self):
        self.steam_cli('pin', build=BUILD)
        code, message = self.steam_cli('cache')
        self.assertEqual(code, 0, message)
        self.assertTrue(luna.steam_state()['match'])
        # corrupt the cache -> recovery refuses and nothing changes
        victim = next((luna.steam_cache_dir() / 'package').glob('bins_ubuntu12*'))
        victim.write_bytes(victim.read_bytes()[:-1] + b'X')
        before = self.tree()
        code, message = self.steam_cli('recover')
        self.assertEqual(code, 1)
        self.assertIn('integrity', message)
        self.assertEqual(before, self.tree())

    def test_cache_refuses_wrong_build_label(self):
        code, message = self.steam_cli('cache', build='42')
        self.assertEqual(code, 1)
        self.assertIn('refusing to mislabel', message)
        self.assertFalse(luna.steam_cache_dir().exists())

    def test_recover_without_cache_or_pin(self):
        self.assertEqual(self.steam_cli('recover')[0], 2)
        self.assertEqual(self.steam_cli('recover', build=BUILD)[0], 1)

    def test_offline_install_prefers_cached_packages_when_installed_ones_are_bad(self):
        self.install_ok()
        for name in self.pkg_names:
            (self.package / name).write_bytes(b'garbage')
        self.install_ok()
        for name, data in self.pkg_files.items():
            self.assertEqual((self.package / name).read_bytes(), data)


class FailureTests(SandboxCase):
    def test_interrupted_extraction_leaves_the_tree_untouched(self):
        before = self.tree()
        with mock.patch.object(luna.zipfile.ZipFile, 'read', side_effect=OSError('disk full')):
            ok, message = self.install()
        self.assertFalse(ok)
        self.assertIn('disk full', message)
        self.assertEqual(before, {k: v for k, v in self.tree().items()
                                  if 'sls-luna' not in k})
        self.assertFalse(luna.steam_pin_path().exists())

    def test_interrupted_install_midway_rolls_back_with_backup(self):
        steam_before = {k: v for k, v in self.tree().items() if k.startswith('.local/share/Steam/')}
        (self.root / 'steam.cfg').write_text('ForeignKey=1\n')
        steam_before['.local/share/Steam/steam.cfg'] = b'ForeignKey=1\n'
        real_replace = os.replace
        calls = []

        def flaky(src, dst, *a, **k):
            calls.append(dst)
            if len(calls) == 4:
                raise OSError('simulated crash')
            return real_replace(src, dst, *a, **k)

        with mock.patch.object(luna.os, 'replace', side_effect=flaky):
            ok, message = self.install()
        self.assertFalse(ok)
        self.assertGreaterEqual(len(calls), 4)
        self.assertIn('simulated crash', message)
        steam_after = {k: v for k, v in self.tree().items() if k.startswith('.local/share/Steam/')}
        self.assertEqual(steam_before, steam_after)
        self.assertEqual(list(self.home.rglob('*.luna-new')), [])
        self.assertTrue(self.package_writable())
        backups = list((self.tools / 'steam-client-backups').iterdir())
        self.assertEqual(len(backups), 1)
        self.assertTrue((backups[0] / 'restore.json').is_file())
        self.assertFalse(luna.steam_pin_path().exists())
        self.assertFalse((self.tools / 'steam-client-installed.json').exists())
        # the machine is recoverable: a clean retry succeeds
        self.install_ok()

    def test_interrupted_install_under_foreign_lock_restores_lock_and_files(self):
        self.foreign_lock()
        steam_before = {k: v for k, v in self.tree().items() if k.startswith('.local/share/Steam/')}
        with mock.patch.object(luna, 'ensure_steam_cfg_block', side_effect=OSError('cfg fail')):
            ok, _ = self.install()
        self.assertFalse(ok)
        self.assertEqual(steam_before, {k: v for k, v in self.tree().items()
                                        if k.startswith('.local/share/Steam/')})
        self.assertFalse(self.package_writable())

    def test_steam_running_refuses_every_mutation(self):
        for name in ('steam', 'steamwebhelper', 'lumen'):
            self.running = {name}
            before = self.tree()
            ok, message = self.install()
            self.assertFalse(ok, name)
            self.assertIn('running', message)
            ok, message = luna._recover_steam_client(BUILD)
            self.assertFalse(ok, name)
            code, text = self.steam_cli('recover', build=BUILD)
            self.assertEqual(code, luna.EXIT_REFUSED, name)
            code, text = self.steam_cli('update', now=True)
            self.assertEqual(code, luna.EXIT_REFUSED, name)
            self.assertFalse(luna._steam_force_update()[0])
            self.assertEqual(before, self.tree(), name)

    def test_lock_refuses_running_steam_without_force_and_never_kills(self):
        self.running = {'steam'}
        with mock.patch.object(luna.subprocess, 'run', side_effect=AssertionError('pkill')):
            code, message = luna._lock_steam_client(False, lambda: False)
        self.assertEqual(code, luna.EXIT_REFUSED)
        self.assertFalse(self.package_writable() is False)

    def test_queued_task_blocks_install(self):
        luna.write_json_file(luna.steam_pending_path(), {'action': 'update', 'at': 1})
        ok, message = self.install()
        self.assertFalse(ok)
        self.assertIn('queued', message)

    def test_tampered_package_without_a_good_source_is_refused(self):
        for name in self.pkg_names:
            (self.package / name).write_bytes(b'bad')
            self.cdn[name] = b'also bad'  # a hostile mirror
        before = self.tree()
        ok, message = self.install()
        self.assertFalse(ok)
        self.assertIn('SHA256 mismatch', message)
        self.assertEqual(before, {k: v for k, v in self.tree().items() if 'sls-luna' not in k})
        self.assertFalse(luna.steam_cache_dir().exists())
        self.assertEqual((self.root / 'ubuntu12_32/steam').read_bytes(), b'\x7fELF-newer-ubuntu12_32/steam')


if __name__ == '__main__':
    unittest.main()
