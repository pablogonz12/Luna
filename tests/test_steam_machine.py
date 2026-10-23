"""Launcher tests use disposable homes, fake monitor data and mocked processes."""
import argparse
import contextlib
import fcntl
import importlib.util
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import types
import unittest
from unittest import mock


SPEC = importlib.util.spec_from_file_location('luna_steam_machine', Path(__file__).resolve().parents[1] / 'scripts/steam-machine.py')
machine = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(machine)


class SteamMachineTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='luna machine test ')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(Path, 'home', return_value=self.home).start()
        mock.patch.dict(os.environ, {'HOME': str(self.home), 'XDG_DATA_HOME': '',
                                    'XDG_RUNTIME_DIR': str(self.home / 'run'), 'NIRI_SOCKET': '',
                                    'XDG_CURRENT_DESKTOP': 'niri', 'WAYLAND_DISPLAY': 'wayland-test'}).start()
        mock.patch.object(machine, 'DRM_ROOT', self.home / 'drm').start()
        self.run_process = mock.patch.object(machine.subprocess, 'run', return_value=types.SimpleNamespace(returncode=1, stdout='')).start()
        self.which = mock.patch.object(machine.shutil, 'which', side_effect=lambda name: '/usr/bin/' + name if name in ('gamescope', 'env') else None).start()
        self.steam = self.home / '.local/share/SLSsteam/path/steam'
        self.steam.parent.mkdir(parents=True)
        self.steam.write_text('#!/bin/sh\nexit 0\n')
        self.steam.chmod(0o755)
        self.tools, self.apps, self.wrapper = machine.paths()

    def install(self, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            return machine.install(**kwargs)

    def options(self, **kwargs):
        values = dict(width=1920, height=1080, refresh=60, mouse_mode='auto', backend='sdl', print_command=False)
        values.update(kwargs)
        return argparse.Namespace(**values)

    def legacy(self, command=None):
        self.apps.mkdir(parents=True, exist_ok=True)
        target = self.apps / 'steam-machine-2k.desktop'
        if command is None:
            command = ('sh -c "killall steam 2>/dev/null; killall gamescope 2>/dev/null; '
                       'sleep 2; killall -9 steam 2>/dev/null; killall -9 gamescope 2>/dev/null; sleep 1; '
                       'gamescope --rt -O DP-2 -W 2560 -H 1440 -w 2560 -h 1440 -r 200 '
                       '--fullscreen --force-grab-cursor --steam -- steam -bigpicture"')
        target.write_text('[Desktop Entry]\nName=My 2K profile\nExec=' + command + '\nType=Application\n')
        return target

    def snapshot(self):
        return {str(p.relative_to(self.home)): p.read_bytes()
                for p in self.home.rglob('*') if p.is_file() and not p.is_symlink()}

    def test_generates_portable_shortcut_helper_and_free_pointer_action(self):
        self.assertEqual(self.install(), 0)
        entry = self.apps / 'luna-steam-machine-1920x1080-60.desktop'
        text = entry.read_text()
        self.assertIn('TryExec=gamescope', text)
        self.assertIn('Actions=FreePointer;', text)
        self.assertIn('--mouse-mode free', text)
        self.assertIn('"' + str(self.wrapper) + '"', text)
        self.assertTrue(os.access(self.wrapper, os.X_OK))
        self.assertTrue((self.tools / 'steam-machine.py').is_file())
        self.assertNotIn('killall', self.wrapper.read_text() + text)

    def test_niri_profiles_use_current_refresh_and_connected_tv_capability(self):
        outputs = {'DP-2': {'current_mode': 1, 'modes': [
            {'width': 2560, 'height': 1440, 'refresh_rate': 165000},
            {'width': 2560, 'height': 1440, 'refresh_rate': 199999}]},
            'HDMI-A-2': {'current_mode': None, 'modes': [
                {'width': 3840, 'height': 2160, 'refresh_rate': 60000}]}}
        self.run_process.return_value = types.SimpleNamespace(returncode=0, stdout=json.dumps(outputs))
        self.install()
        self.assertTrue((self.apps / 'luna-steam-machine-2560x1440-200.desktop').is_file())
        self.assertTrue((self.apps / 'luna-steam-machine-3840x2160-60.desktop').is_file())
        self.assertFalse(any('serial' in p.read_text().lower() for p in self.apps.glob('*.desktop')))

    def test_sysfs_fallback_does_not_include_disconnected_displays(self):
        for name, status, mode in [('card1-eDP-1', 'connected', '1920x1200'), ('card1-DP-1', 'disconnected', '7680x4320')]:
            directory = machine.DRM_ROOT / name
            directory.mkdir(parents=True)
            (directory / 'status').write_text(status)
            (directory / 'modes').write_text(mode + '\n')
        self.assertEqual(machine.discover_profiles(), [machine.profile(1920, 1080, 60), machine.profile(1920, 1200, 60)])

    def test_bad_monitor_json_falls_back_to_portable_profile(self):
        self.run_process.return_value = types.SimpleNamespace(returncode=0, stdout='[]')
        self.assertEqual(machine.discover_profiles(), [machine.profile(1920, 1080, 60)])

    def test_dry_run_does_not_write_files_or_directories(self):
        before = self.snapshot()
        self.install(dry_run=True, migrate_legacy=True)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(self.tools.exists())
        self.assertFalse(self.apps.exists())

    def test_reinstall_is_idempotent_and_preserves_identical_launcher(self):
        self.install()
        entry = self.apps / 'luna-steam-machine-1920x1080-60.desktop'
        inode, stamp = entry.stat().st_ino, entry.stat().st_mtime_ns
        self.install()
        self.assertEqual(entry.stat().st_ino, inode)
        self.assertEqual(entry.stat().st_mtime_ns, stamp)

    def test_legacy_profiles_are_not_overwritten_without_explicit_migration(self):
        entry = self.legacy()
        original = entry.read_bytes()
        self.install()
        self.assertEqual(entry.read_bytes(), original)

    def test_migrates_only_recognized_legacy_launcher_and_keeps_exact_backup(self):
        entry = self.legacy()
        original = entry.read_bytes()
        self.install(migrate_legacy=True)
        self.assertNotIn('killall', entry.read_text())
        self.assertIn('--width 2560 --height 1440 --refresh 200', entry.read_text())
        self.assertIn('Name=My 2K profile', entry.read_text())
        state = machine.read_state()
        self.assertEqual(Path(state['files'][str(entry)]['original']).read_bytes(), original)
        with contextlib.redirect_stdout(io.StringIO()):
            machine.uninstall()
        self.assertEqual(entry.read_bytes(), original)
        self.install()  # A restored unmanaged legacy entry must not block reinstall.
        self.assertEqual(entry.read_bytes(), original)

    def test_legacy_custom_commands_or_extra_flags_are_preserved(self):
        for command in ('sh -c "touch /secret; gamescope -W 2560 -H 1440 -r 200 -- steam -bigpicture"',
                        'gamescope --hdr-enabled -W 2560 -H 1440 -r 200 -- steam -bigpicture',
                        'gamescope -W 2560 -H 1440 -r 200 -- steam -bigpicture;echo nope'):
            entry = self.legacy(command)
            original = entry.read_bytes()
            self.install(migrate_legacy=True)
            self.assertEqual(entry.read_bytes(), original)

    def test_migration_deduplicates_only_unchanged_luna_generated_entries(self):
        self.run_process.return_value = types.SimpleNamespace(returncode=0, stdout=json.dumps({
            'DP-2': {'current_mode': 0, 'modes': [{'width': 2560, 'height': 1440, 'refresh_rate': 200000}]}}))
        entry = self.legacy()
        self.install()
        duplicate = self.apps / 'luna-steam-machine-2560x1440-200.desktop'
        self.assertTrue(duplicate.exists())
        self.install(migrate_legacy=True)
        self.assertFalse(duplicate.exists())
        self.assertTrue(entry.exists())

    def test_custom_edits_to_managed_entries_are_never_silently_overwritten(self):
        self.install()
        entry = self.apps / 'luna-steam-machine-1920x1080-60.desktop'
        entry.write_text('user custom launcher')
        with self.assertRaisesRegex(ValueError, 'changed outside Luna'):
            self.install()
        with contextlib.redirect_stdout(io.StringIO()):
            machine.uninstall()
        self.assertEqual(entry.read_text(), 'user custom launcher')

    def test_unmanaged_wrapper_collision_refuses_without_modifying_anything(self):
        self.wrapper.parent.mkdir(parents=True)
        self.wrapper.write_text('custom user wrapper')
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, 'unmanaged launcher'):
            self.install()
        self.assertEqual(before, self.snapshot())

    def test_launcher_symlink_is_not_followed_or_overwritten(self):
        outside = self.home / 'private'
        outside.write_text('private sentinel')
        self.wrapper.parent.mkdir(parents=True)
        self.wrapper.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            self.install()
        self.assertEqual(outside.read_text(), 'private sentinel')

    def test_install_write_failure_restores_original_files(self):
        original_write = machine.atomic_write

        def fail(path, *args):
            if path == self.wrapper:
                raise OSError('simulated disk failure')
            return original_write(path, *args)

        before = self.snapshot()
        with mock.patch.object(machine, 'atomic_write', side_effect=fail):
            with self.assertRaisesRegex(OSError, 'simulated'):
                self.install()
        self.assertEqual(self.snapshot(), before)

    def test_mouse_compatibility_uses_sdl_and_relative_capture_on_niri(self):
        command = machine.launch_command(self.options())
        self.assertIn('--force-grab-cursor', command)
        self.assertEqual(command[command.index('--backend') + 1], 'sdl')
        self.assertIn('--steam', command)
        self.assertEqual(command[-2:], [str(self.steam), '-gamepadui'])
        self.assertNotIn('-O', command, 'embedded-only output selection must not masquerade as nested monitor routing')

    def test_free_pointer_action_keeps_sdl_without_forcing_capture(self):
        command = machine.launch_command(self.options(mouse_mode='free'))
        self.assertNotIn('--force-grab-cursor', command)
        self.assertEqual(command[command.index('--backend') + 1], 'sdl')

    def test_non_niri_auto_mouse_mode_does_not_force_relative_capture(self):
        with mock.patch.dict(os.environ, {'XDG_CURRENT_DESKTOP': 'KDE', 'NIRI_SOCKET': ''}):
            self.assertNotIn('--force-grab-cursor', machine.launch_command(self.options()))

    def test_unsafe_wayland_forced_capture_combination_is_refused(self):
        with self.assertRaisesRegex(ValueError, 'Wayland backend can freeze'):
            machine.launch_command(self.options(backend='wayland', mouse_mode='captured'))

    def test_child_driver_settings_keep_games_inside_gamescope(self):
        command = machine.launch_command(self.options())
        separator = command.index('--')
        self.assertIn('WAYLAND_DISPLAY', command[separator + 1:])
        for value in ('SDL_VIDEODRIVER=x11', 'SDL_VIDEO_DRIVER=x11', 'GDK_BACKEND=x11', 'QT_QPA_PLATFORM=xcb'):
            self.assertIn(value, command[separator + 1:])

    def test_existing_cpu_policy_helper_is_preserved_when_available(self):
        cpu = self.home / '.local/bin/ccd-launch'
        cpu.parent.mkdir(parents=True)
        cpu.write_text('#!/bin/sh\nexec "$@"\n')
        cpu.chmod(0o755)
        command = machine.launch_command(self.options())
        start = command.index(str(cpu))
        self.assertEqual(command[start:start + 4], [str(cpu), '--group', 'auto', '--'])

    def test_missing_gamescope_or_moon_wrapper_reports_clear_errors(self):
        self.which.side_effect = lambda name: None
        with self.assertRaisesRegex(ValueError, 'Gamescope is not installed'):
            machine.launch_command(self.options())
        self.steam.unlink()
        with self.assertRaisesRegex(ValueError, 'wrapper is missing'):
            machine.launch_command(self.options())

    def test_rejects_invalid_dimensions_and_refresh(self):
        for options in (self.options(width=-1), self.options(refresh=0), self.options(height=99999)):
            with self.assertRaises(ValueError):
                machine.launch_command(options)

    def test_print_command_does_not_launch_or_create_a_lock(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(machine.run(self.options(print_command=True)), 0)
        self.assertIn('--backend sdl', output.getvalue())
        self.run_process.assert_not_called()
        self.assertFalse((self.home / 'run').exists())

    def test_running_steam_refuses_instead_of_killing_or_forwarding(self):
        with mock.patch.object(machine, 'steam_running', return_value=True):
            with self.assertRaisesRegex(ValueError, 'No processes were killed'):
                machine.run(self.options())
        self.run_process.assert_not_called()

    def test_duplicate_machine_session_is_locked_out(self):
        runtime = self.home / 'run'
        runtime.mkdir()
        with (runtime / 'luna-steam-machine.lock').open('a+') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(ValueError, 'already running'):
                machine.run(self.options())
        self.run_process.assert_not_called()

    def test_host_driver_environment_is_separate_from_child_overrides(self):
        self.run_process.return_value = types.SimpleNamespace(returncode=0)
        with mock.patch.object(machine, 'steam_running', return_value=False):
            self.assertEqual(machine.run(self.options()), 0)
        kwargs = self.run_process.call_args.kwargs
        self.assertEqual(kwargs['env']['SDL_VIDEODRIVER'], 'wayland,x11')
        self.assertEqual(kwargs['env']['SDL_VIDEO_DRIVER'], 'wayland,x11')

    def test_account_game_and_playtime_files_are_untouched(self):
        sentinels = ['.local/share/Steam/config/loginusers.vdf', '.local/share/Steam/userdata/private',
                     '.local/share/Steam/steamapps/save', '.local/share/slssteam-integrations/presence.json']
        for name in sentinels:
            path = self.home / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('private sentinel')
        self.install()
        with contextlib.redirect_stdout(io.StringIO()):
            machine.uninstall()
        for name in sentinels:
            self.assertEqual((self.home / name).read_text(), 'private sentinel')


if __name__ == '__main__':
    unittest.main()
