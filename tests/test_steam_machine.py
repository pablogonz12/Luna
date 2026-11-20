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

os.environ.setdefault('LUNA_NO_NOTIFY', '1')

SPEC = importlib.util.spec_from_file_location('luna_steam_machine', Path(__file__).resolve().parents[1] / 'scripts/steam-machine.py')
machine = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(machine)

FIXTURES = Path(__file__).resolve().parent / 'fixtures'
OLD_4K_LAUNCHER = FIXTURES / 'steamos-3840x2160-60.desktop'


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

    def legacy(self, command=None, name='steam-machine-2k.desktop'):
        self.apps.mkdir(parents=True, exist_ok=True)
        target = self.apps / name
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
        entry = self.apps / 'steamos-1920x1080-60.desktop'
        text = entry.read_text()
        self.assertIn('Name=SteamOS (1080p 60)', text)
        self.assertIn('X-Luna-SteamOS=true', text)
        self.assertIn('TryExec=gamescope', text)
        self.assertIn('Actions=FreePointer;', text)
        self.assertIn('--mouse-mode free', text)
        self.assertIn('"' + str(self.wrapper) + '"', text)
        self.assertTrue(os.access(self.wrapper, os.X_OK))
        self.assertTrue((self.tools / 'steam-machine.py').is_file())
        self.assertNotIn('killall', self.wrapper.read_text() + text)

    def test_shortcut_names_use_the_steamos_scheme(self):
        self.assertEqual(machine.entry_label(machine.profile(1920, 1080, 60)), 'SteamOS (1080p 60)')
        self.assertEqual(machine.entry_label(machine.profile(2560, 1440, 200)), 'SteamOS (2K 200)')
        self.assertEqual(machine.entry_label(machine.profile(2880, 1800, 120)), 'SteamOS (3K 120)')
        self.assertEqual(machine.entry_label(machine.profile(3840, 2160, 60)), 'SteamOS (4K 60)')
        self.assertEqual(machine.entry_label(machine.profile(3440, 1440, 100)), 'SteamOS (3440x1440 100)')
        self.assertEqual(machine.entry_name(machine.profile(2560, 1440, 200)), 'steamos-2560x1440-200.desktop')

    def test_niri_profiles_use_current_refresh_and_connected_tv_capability(self):
        outputs = {'DP-2': {'current_mode': 1, 'modes': [
            {'width': 2560, 'height': 1440, 'refresh_rate': 165000},
            {'width': 2560, 'height': 1440, 'refresh_rate': 199999}]},
            'HDMI-A-2': {'current_mode': None, 'modes': [
                {'width': 3840, 'height': 2160, 'refresh_rate': 60000}]}}
        self.run_process.return_value = types.SimpleNamespace(returncode=0, stdout=json.dumps(outputs))
        self.install()
        self.assertTrue((self.apps / 'steamos-2560x1440-200.desktop').is_file())
        self.assertTrue((self.apps / 'steamos-3840x2160-60.desktop').is_file())
        self.assertIn('Name=SteamOS (4K 60)', (self.apps / 'steamos-3840x2160-60.desktop').read_text())
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
        entry = self.apps / 'steamos-1920x1080-60.desktop'
        inode, stamp = entry.stat().st_ino, entry.stat().st_mtime_ns
        self.install()
        self.assertEqual(entry.stat().st_ino, inode)
        self.assertEqual(entry.stat().st_mtime_ns, stamp)
        self.assertFalse((self.tools / 'archive').exists(),
                         'an identical launcher is neither rewritten nor archived')

    LUNA_OUTPUTS = {'HDMI-A-2': {'current_mode': 0, 'modes': [
        {'width': 3840, 'height': 2160, 'refresh_rate': 60000}]}}

    def install_4k(self, **kwargs):
        self.run_process.return_value = types.SimpleNamespace(returncode=0, stdout=json.dumps(self.LUNA_OUTPUTS))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = machine.install(**kwargs)
        return code, out.getvalue()

    def test_untouched_luna_written_file_upgrades_silently(self):
        self.install_4k()
        entry = self.apps / 'steamos-3840x2160-60.desktop'
        older = entry.read_bytes().replace(b'TryExec=gamescope\n', b'')
        entry.write_bytes(older)
        state = machine.read_state()
        state['files'][str(entry)]['sha256'] = machine.digest(older)  # Luna wrote the older text
        machine.atomic_write(self.tools / 'steam-machine-shortcuts.json', json.dumps(state).encode(), 0o600)
        code, output = self.install_4k()
        self.assertEqual(code, 0)
        self.assertIn(b'TryExec=gamescope', entry.read_bytes())
        self.assertNotIn('kept', output)
        self.assertFalse((self.tools / 'archive').exists())

    def test_hand_edited_helper_is_kept_and_install_succeeds(self):
        self.install()
        helper = self.tools / 'steam-machine.py'
        edited = helper.read_bytes() + b'# owner edit\n'
        helper.write_bytes(edited)
        code, output = self.install_4k()
        self.assertEqual(code, 0)
        self.assertEqual(helper.read_bytes(), edited)
        self.assertIn('kept (edited outside Luna): %s' % helper, output)
        self.assertIn('--adopt', output)
        self.assertFalse((self.tools / 'archive').exists())

    def test_wrapper_prefixed_launcher_is_kept_and_reports_when_behind(self):
        self.install_4k()
        entry = self.apps / 'steamos-3840x2160-60.desktop'
        wrapped = entry.read_bytes().replace(b'Exec=', b'Exec=/home/u/.local/bin/on-desktop --show desktop -- ')
        entry.write_bytes(wrapped)
        # Luna's own record is of an older text, so its current version has moved on.
        state = machine.read_state()
        state['files'][str(entry)]['sha256'] = '1' * 64
        machine.atomic_write(self.tools / 'steam-machine-shortcuts.json', json.dumps(state).encode(), 0o600)
        code, output = self.install_4k()
        self.assertEqual(code, 0)
        self.assertEqual(entry.read_bytes(), wrapped)
        self.assertIn('kept (edited outside Luna): %s' % entry, output)
        self.assertIn('behind', output)

    def test_second_run_is_quiet_and_status_still_lists_kept_files(self):
        self.install_4k()
        helper = self.tools / 'steam-machine.py'
        helper.write_bytes(helper.read_bytes() + b'# owner edit\n')
        self.install_4k()
        code, output = self.install_4k()
        self.assertEqual(code, 0)
        self.assertNotIn('kept', output)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(machine.status(show_diff=True), 0)
        self.assertIn('kept (edited outside Luna): %s' % helper, out.getvalue())
        self.assertIn('owner edit', out.getvalue())
        self.assertIn(str(helper), machine.read_state()['kept'])

    def test_dry_run_reports_kept_files_without_writing(self):
        self.install()
        helper = self.tools / 'steam-machine.py'
        helper.write_bytes(helper.read_bytes() + b'# owner edit\n')
        before = self.snapshot()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(machine.install(dry_run=True), 0)
        self.assertIn('would keep: kept (edited outside Luna): %s' % helper, out.getvalue())
        self.assertEqual(self.snapshot(), before)

    def test_explicit_adopt_archives_then_overwrites(self):
        self.install_4k()
        entry = self.apps / 'steamos-3840x2160-60.desktop'
        wrapped = entry.read_bytes().replace(b'Exec=', b'Exec=on-desktop -- ')
        entry.write_bytes(wrapped)
        code, output = self.install_4k(adopt=[str(entry)])
        self.assertEqual(code, 0)
        self.assertNotEqual(entry.read_bytes(), wrapped)
        self.assertNotIn(b'on-desktop', entry.read_bytes())
        archived = list((self.tools / 'archive').glob('steamos-3840x2160-60.desktop.*'))
        self.assertEqual([path.read_bytes() for path in archived], [wrapped])
        self.assertNotIn(str(entry), machine.read_state()['kept'])
        self.assertEqual(machine.read_state()['files'][str(entry)]['sha256'], machine.digest(entry.read_bytes()))

    def test_adopt_of_unknown_path_is_refused_and_nothing_changes(self):
        self.install()
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, 'not a file Luna manages'):
            self.install(adopt=['/etc/passwd'])
        self.assertEqual(self.snapshot(), before)

    def test_install_force_style_arguments_do_not_adopt(self):
        parser_calls = []
        with mock.patch.object(machine, 'install', side_effect=lambda *a: parser_calls.append(a) or 0):
            with mock.patch.object(machine.sys, 'argv', ['x', 'install', '--migrate-legacy']):
                machine.main()
        self.assertEqual(parser_calls, [(False, True, [])])

    def test_unmarked_foreign_launcher_is_untouched(self):
        self.install()
        entry = self.apps / 'steamos-1920x1080-60.desktop'
        foreign = OLD_4K_LAUNCHER.read_bytes().replace(b'X-Luna-SteamOS=true\n', b'')
        entry.write_bytes(foreign)
        state = machine.read_state()
        del state['files'][str(entry)]
        machine.atomic_write(self.tools / 'steam-machine-shortcuts.json', json.dumps(state).encode(), 0o600)
        code, output = self.install_4k()
        self.assertEqual(code, 0)
        self.assertEqual(entry.read_bytes(), foreign)
        self.assertIn('kept (not a Luna file): %s' % entry, output)
        self.assertFalse((self.tools / 'archive').exists())

    def test_marker_without_a_record_is_kept_not_overwritten(self):
        self.install_4k()
        entry = self.apps / 'steamos-3840x2160-60.desktop'
        drifted = OLD_4K_LAUNCHER.read_bytes()
        self.assertIn(b'X-Luna-SteamOS=true', drifted)
        entry.write_bytes(drifted)
        state = machine.read_state()
        del state['files'][str(entry)]
        machine.atomic_write(self.tools / 'steam-machine-shortcuts.json', json.dumps(state).encode(), 0o600)
        code, output = self.install_4k()
        self.assertEqual(code, 0)
        self.assertEqual(entry.read_bytes(), drifted)
        self.assertIn('kept (edited outside Luna): %s' % entry, output)
        self.assertNotIn(str(entry), machine.read_state()['files'])

    def test_legacy_profiles_are_not_overwritten_without_explicit_migration(self):
        entry = self.legacy()
        original = entry.read_bytes()
        self.install()
        self.assertEqual(entry.read_bytes(), original)

    def test_migration_adopts_and_renames_keeping_an_exact_backup(self):
        entry = self.legacy()
        original = entry.read_bytes()
        self.install(migrate_legacy=True)
        new = self.apps / 'steamos-2560x1440-200.desktop'
        self.assertTrue(new.is_file(), 'the SteamOS shortcut must be created')
        self.assertIn('Name=SteamOS (2K 200)', new.read_text())
        self.assertIn('--width 2560 --height 1440 --refresh 200', new.read_text())
        # The user's own file is never rewritten: it is backed up, retired,
        # and the new shortcut takes its place.
        self.assertFalse(entry.exists(), 'the adopted legacy file must be retired')
        state = machine.read_state()
        info = state['files'][str(new)]
        self.assertEqual(Path(info['original']).read_bytes(), original)
        self.assertEqual(info['migrated_from'], str(entry))
        with contextlib.redirect_stdout(io.StringIO()):
            machine.uninstall()
        self.assertEqual(entry.read_bytes(), original, 'uninstall must restore the adopted original')
        self.assertFalse(new.exists())
        self.install()  # A restored legacy entry must not block reinstall.
        self.assertEqual(entry.read_bytes(), original)

    def test_legacy_custom_commands_or_extra_flags_are_preserved(self):
        for command in ('sh -c "touch /secret; gamescope -W 2560 -H 1440 -r 200 -- steam -bigpicture"',
                        'gamescope --hdr-enabled -W 2560 -H 1440 -r 200 -- steam -bigpicture',
                        'gamescope -W 2560 -H 1440 -r 200 -- steam -bigpicture;echo nope'):
            entry = self.legacy(command)
            original = entry.read_bytes()
            self.install(migrate_legacy=True)
            self.assertEqual(entry.read_bytes(), original)

    def test_retirement_replaces_only_unchanged_luna_generated_names(self):
        self.install()
        # Simulate a previous Luna version's owned shortcut for the same mode.
        old = self.apps / 'luna-steam-machine-1920x1080-60.desktop'
        old.write_text('previous Luna generated content')
        state = machine.read_state()
        state['files'][str(old)] = {'sha256': machine.digest(old.read_bytes())}
        state['profiles'][old.name] = {'width': 1920, 'height': 1080, 'refresh': 60}
        machine.atomic_write(machine.paths()[0] / 'steam-machine-shortcuts.json',
                             (json.dumps(state, indent=2, sort_keys=True) + '\n').encode(), 0o600)
        edited = self.apps / 'steam-machine-old.desktop'
        edited.write_text('user edited old shortcut')
        state = machine.read_state()
        state['files'][str(edited)] = {'sha256': '0' * 64}
        state['profiles'][edited.name] = {'width': 1920, 'height': 1080, 'refresh': 60}
        machine.atomic_write(machine.paths()[0] / 'steam-machine-shortcuts.json',
                             (json.dumps(state, indent=2, sort_keys=True) + '\n').encode(), 0o600)
        self.install()
        self.assertFalse(old.exists(), 'unchanged Luna-owned names move to the SteamOS scheme')
        self.assertTrue((self.apps / 'steamos-1920x1080-60.desktop').is_file())
        self.assertEqual(edited.read_text(), 'user edited old shortcut',
                         'edited shortcuts are never silently replaced')

    def test_custom_edits_to_managed_entries_are_never_overwritten(self):
        self.install()
        entry = self.apps / 'steamos-1920x1080-60.desktop'
        entry.write_text('user custom launcher')
        self.assertEqual(self.install(), 0)
        self.assertEqual(entry.read_text(), 'user custom launcher')
        with contextlib.redirect_stdout(io.StringIO()):
            machine.uninstall()
        self.assertEqual(entry.read_text(), 'user custom launcher')

    def test_unmanaged_wrapper_collision_is_kept_untouched(self):
        self.wrapper.parent.mkdir(parents=True)
        self.wrapper.write_text('custom user wrapper')
        self.assertEqual(self.install(), 0)
        self.assertEqual(self.wrapper.read_text(), 'custom user wrapper')
        self.assertNotIn(str(self.wrapper), machine.read_state()['files'])

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

    def make_tool(self, name):
        tool = self.home / '.local/bin' / name
        tool.parent.mkdir(parents=True, exist_ok=True)
        tool.write_text('#!/bin/sh\nexec "$@"\n')
        tool.chmod(0o755)
        return tool

    def test_cpu_group_pinning_is_used_only_when_the_tool_exists(self):
        plain = machine.launch_command(self.options())
        self.assertNotIn('--group', plain)
        self.assertNotIn('/bin/sh', plain)
        self.assertEqual(plain[0], '/usr/bin/gamescope')
        cpu = self.make_tool('ccd-launch')
        command = machine.launch_command(self.options())
        self.assertEqual(command[:4], [str(cpu), '--group', 'all', '--'])
        start = command.index('e')
        self.assertEqual(command[start - 1:start + 2], ['--group', 'e', '--'])
        self.assertEqual(command[-2:], [str(self.steam), '-gamepadui'])

    def test_mangoapp_flag_and_env_scrub_do_not_need_extra_tools(self):
        command = machine.launch_command(self.options())
        self.assertIn('--mangoapp', command[:command.index('--')])
        tail = command[command.index('--') + 1:]
        for value in ('MANGOHUD=0', 'MANGOHUD', 'MANGOHUD_CONFIG', 'STEAM_USE_MANGOAPP'):
            self.assertIn(value, tail)

    def test_mangoapp_hotkey_helper_is_optional(self):
        self.assertNotIn('"$0" & exec "$@"', machine.launch_command(self.options()))
        hotkey = self.make_tool('mangoapp-hotkey')
        command = machine.launch_command(self.options())
        start = command.index('/bin/sh')
        self.assertEqual(command[start:start + 4], ['/bin/sh', '-c', '"$0" & exec "$@"', str(hotkey)])
        hotkey.chmod(0o644)
        self.assertNotIn('/bin/sh', machine.launch_command(self.options()), 'a non-executable helper is skipped')

    def test_run_pins_mangohud_config_and_parks_toggle_only_with_the_hotkey_helper(self):
        seen = []
        self.run_process.side_effect = lambda command, **kw: seen.append(kw.get('env')) or types.SimpleNamespace(returncode=0)
        options = self.options(print_command=False)
        with mock.patch.object(machine, 'steam_running', return_value=False), contextlib.redirect_stdout(io.StringIO()):
            machine.run(options)
            self.make_tool('mangoapp-hotkey')
            machine.run(options)
        pinned = str(self.home / '.config/MangoHud/MangoHud.conf')
        self.assertEqual(seen[0]['MANGOHUD_CONFIGFILE'], pinned)
        self.assertNotIn('MANGOHUD_CONFIG', seen[0])
        self.assertEqual(seen[1]['MANGOHUD_CONFIG'], 'read_cfg,toggle_hud=F24')

    def test_run_mouse_mode_defaults_to_free(self):
        captured = []
        with mock.patch.object(machine, 'run', side_effect=lambda o: captured.append(o.mouse_mode) or 0):
            with mock.patch.object(machine.sys, 'argv', ['x', 'run']):
                machine.main()
        self.assertEqual(captured, ['free'])

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

    def test_running_steam_is_terminated_before_launch(self):
        self.which.side_effect = lambda name: '/usr/bin/' + name
        self.run_process.return_value = types.SimpleNamespace(returncode=0)
        seen = {'n': 0}

        def running(name=None):
            seen['n'] += 1
            return seen['n'] == 1

        with mock.patch.object(machine, 'steam_running', side_effect=running):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(machine.run(self.options()), 0)
        commands = [call.args[0] for call in self.run_process.call_args_list]
        killed = {command[2] for command in commands if command[:2] == ['pkill', '-x']}
        self.assertEqual(killed, {'steam', 'steamwebhelper', 'lumen'})
        self.assertIn('/usr/bin/gamescope', commands[-1],
                      'the new session must launch after the stale one is gone')

    def test_stuck_steam_stack_refuses_instead_of_launching_broken(self):
        self.which.side_effect = lambda name: '/usr/bin/' + name
        with mock.patch.object(machine, 'steam_running', return_value=True):
            with mock.patch.object(machine.time, 'monotonic', side_effect=[0.0, 100.0]):
                with self.assertRaisesRegex(ValueError, 'did not exit'):
                    machine.run(self.options())
        gamescope_runs = [call for call in self.run_process.call_args_list
                          if call.args[0] and call.args[0][0] == '/usr/bin/gamescope']
        self.assertEqual(gamescope_runs, [], 'never launch into a half-dead session')

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
