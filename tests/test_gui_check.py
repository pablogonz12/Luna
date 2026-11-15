"""GUI-check layer against fake agent-desktop / niri / grim executables on PATH.

The fakes replay trimmed real captures from tests/fixtures/gui; nothing here
touches the real desktop, Steam or the network."""
import argparse
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import time
import unittest
from unittest import mock


SPEC = importlib.util.spec_from_file_location('luna_gui_test', Path(__file__).resolve().parents[1] / 'scripts/luna.py')
luna = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(luna)

FIXTURES = Path(__file__).resolve().parent / 'fixtures/gui'

FAKE_AGENT = '''#!/usr/bin/env python3
import json, os, sys
cfg = json.load(open(os.environ['FAKE_GUI_CFG']))
command = sys.argv[1]
if cfg.get('fail'):
    print(json.dumps({'version': '2.6', 'ok': False, 'command': command,
                      'error': {'code': cfg['fail'], 'message': 'fake failure'}}))
    sys.exit(1)
if command == 'snapshot':
    counter = cfg['counter']
    try:
        index = int(open(counter).read())
    except OSError:
        index = 0
    open(counter, 'w').write(str(index + 1))
    files = cfg['snapshots']
    name = files[min(index, len(files) - 1)]
    if name is None:
        print(json.dumps({'version': '2.6', 'ok': False, 'command': 'snapshot',
                          'error': {'code': 'APP_NOT_FOUND', 'message': 'no window'}}))
        sys.exit(1)
    sys.stdout.write(open(name).read())
else:
    sys.stdout.write(open(cfg[command]).read())
'''


def tree(*named):
    """A minimal snapshot envelope whose window holds (role, name) children."""
    def node(item):
        role, name = item[:2]
        return {'role': role, 'name': name, 'children': [node(c) for c in (item[2] if len(item) > 2 else [])]}
    children = [node(item) for item in named]
    root = {'role': 'window', 'children': [{'role': 'webarea', 'name': 'Steam', 'children': children}]}
    return {'version': '2.6', 'ok': True, 'command': 'snapshot', 'data': {'app': 'steamwebhelper', 'tree': root}}


FAKE_NIRI = '''#!/usr/bin/env python3
import json, os, struct, sys, time
cfg = json.load(open(os.environ['FAKE_NIRI_CFG']))
args = sys.argv[1:]
with open(cfg['log'], 'a') as log:
    log.write(' '.join(args) + '\\n')
if args[:3] == ['msg', 'action', 'screenshot-window']:
    if cfg.get('shot') == 'fail':
        sys.exit(1)
    path = args[args.index('--path') + 1]
    if not os.path.isabs(path):
        sys.exit(2)
    if cfg.get('shot') == 'nothing':
        sys.exit(0)
    w, h = cfg['png']
    time.sleep(cfg.get('delay', 0))
    open(path, 'wb').write(b'\\x89PNG\\r\\n\\x1a\\n' + struct.pack('>I', 13) + b'IHDR' + struct.pack('>II', w, h) + b'\\x08\\x06\\x00\\x00\\x00')
elif args[:3] == ['msg', '--json', 'windows']:
    sys.stdout.write(open(cfg['windows']).read())
else:
    sys.exit(1)
'''


def fixture_tree(name):
    return json.loads((FIXTURES / name).read_text())['data']['tree']


class Sim:
    """A fake Steam built from the real (scrubbed) snapshots: the Store tab with the Luna
    overlay closed, or the overlay showing one add-on page. Records every click."""

    def __init__(self, click_ok=True):
        self.page = None
        self.clicks = []
        self.click_ok = click_ok

    def snapshot(self, *args, **kwargs):
        return fixture_tree('main-store.json' if self.page is None else 'luna-%s.json' % self.page), None

    def click(self, node, *args, **kwargs):
        name = node.get('name')
        self.clicks.append(name)
        if not self.click_ok:
            return {'ok': False, 'error': 'refused'}
        if node['role'] == 'button' and name == 'Luna':
            self.page = self.page or 'playtime'
        elif name == '\u2715':
            self.page = None
        elif name in ('Playtime', 'Presence', 'Compressor'):
            self.page = name.lower()
        return {'ok': True}


class GuiFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='luna-gui-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / 'home'
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.work = self.root / 'work'
        self.work.mkdir()
        for addon in ('luna', 'playtime', 'presence', 'compressor'):
            (self.home / '.local/share/Lumen/sls-addons' / addon).mkdir(parents=True)
        self.cfg = {'counter': str(self.work / 'counter'),
                    'list-windows': str(FIXTURES / 'list-windows-login.json'),
                    'snapshots': [str(FIXTURES / 'login-snapshot.json')]}
        self.addCleanup(mock.patch.stopall)
        mock.patch.dict(os.environ, {'HOME': str(self.home), 'PATH': str(self.bin) + ':/usr/bin:/bin',
                                    'XDG_RUNTIME_DIR': str(self.work / 'run'),
                                    'FAKE_GUI_CFG': str(self.work / 'cfg.json'),
                                    'WAYLAND_DISPLAY': 'wayland-9', 'DISPLAY': ':9',
                                    'NIRI_SOCKET': str(self.work / 'niri.sock')}).start()
        os.environ.pop('LUNA_GUI_CHECKS', None)
        mock.patch.object(luna, 'home', return_value=self.home).start()
        self.write_cfg()
        # Default fakes fail, so the host's real niri/grim are never used.
        self.install_tool('niri', '#!/bin/sh\nexit 1\n')
        self.install_tool('grim', '#!/bin/sh\nexit 1\n')

    def write_cfg(self):
        (self.work / 'cfg.json').write_text(json.dumps(self.cfg))
        (self.work / 'counter').unlink(missing_ok=True)

    def install_tool(self, name, body):
        path = self.bin / name
        path.write_text(body)
        path.chmod(path.stat().st_mode | stat.S_IEXEC)

    def install_agent(self):
        self.install_tool('agent-desktop', FAKE_AGENT)

    def install_niri(self):
        self.install_tool('niri', '#!/bin/sh\ncat "%s"\n' % (FIXTURES / 'niri-windows-login.json'))

    def install_grim(self):
        self.install_tool('grim', '#!/bin/sh\nprintf PNG > "$1"\n')

    def install_window_niri(self, shot='ok', png=(640, 480), delay=0, windows=None):
        """A fake niri: lists a main 'Steam' window (640x480) plus a menu helper, and handles
        `msg action screenshot-window --id N --path ABS` by writing a PNG of `png` size."""
        windows = windows if windows is not None else [
            {'id': 7, 'title': '', 'app_id': 'steam', 'layout': {'window_size': [900, 900]}},
            {'id': 5, 'title': 'Steam', 'app_id': 'steam', 'layout': {'window_size': [640, 480]}},
            {'id': 9, 'title': 'Other', 'app_id': 'brave', 'layout': {'window_size': [3000, 2000]}}]
        (self.work / 'niri-windows.json').write_text(json.dumps(windows))
        (self.work / 'niri-cfg.json').write_text(json.dumps({'log': str(self.work / 'niri.log'), 'shot': shot,
                                                            'png': list(png), 'delay': delay,
                                                            'windows': str(self.work / 'niri-windows.json')}))
        os.environ['FAKE_NIRI_CFG'] = str(self.work / 'niri-cfg.json')
        self.install_tool('niri', FAKE_NIRI)

    def niri_log(self):
        path = self.work / 'niri.log'
        return path.read_text().splitlines() if path.exists() else []

    def snapshot_file(self, name, *named):
        path = self.work / name
        path.write_text(json.dumps(tree(*named)))
        return str(path)

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = luna.main(list(args))
        return code, out.getvalue(), err.getvalue()


class SessionAndRunner(GuiFixture):
    def test_env_resolution_fills_only_missing_values(self):
        run = self.work / 'run'
        x11 = self.work / 'x11'
        run.mkdir()
        x11.mkdir()
        (run / 'wayland-3').touch()
        (run / 'wayland-3.lock').touch()
        (run / 'niri.wayland-3.77.sock').touch()
        (x11 / 'X7').touch()
        env = luna.gui_session_env({'XDG_RUNTIME_DIR': str(run)}, x11_dir=str(x11))
        self.assertEqual(env['WAYLAND_DISPLAY'], 'wayland-3')
        self.assertEqual(env['DISPLAY'], ':7')
        self.assertTrue(env['NIRI_SOCKET'].endswith('niri.wayland-3.77.sock'))
        kept = luna.gui_session_env({'XDG_RUNTIME_DIR': str(run), 'DISPLAY': ':0', 'WAYLAND_DISPLAY': 'w1',
                                     'NIRI_SOCKET': '/x'}, x11_dir=str(x11))
        self.assertEqual((kept['DISPLAY'], kept['WAYLAND_DISPLAY'], kept['NIRI_SOCKET']), (':0', 'w1', '/x'))

    def test_missing_tool_is_unavailable_not_an_error(self):
        answer = luna.gui_run(['status'])
        self.assertTrue(answer['unavailable'])
        self.assertFalse(answer['ok'])

    def test_error_envelope_and_timeout_are_clean(self):
        self.install_agent()
        self.cfg['fail'] = 'PLATFORM_NOT_SUPPORTED'
        self.write_cfg()
        answer = luna.gui_run(['snapshot'])
        self.assertFalse(answer['ok'])
        self.assertTrue(answer['unavailable'])
        self.assertIn('PLATFORM_NOT_SUPPORTED', answer['error'])
        self.install_tool('agent-desktop', '#!/bin/sh\nsleep 5\n')
        self.assertIn('timed out', luna.gui_run(['status'], timeout=0.3)['error'])

    def test_windows_prefer_niri_then_fall_back_to_agent_desktop(self):
        self.install_agent()
        self.install_niri()
        windows = luna.gui_windows()
        self.assertEqual(luna.gui_titles(windows), ['Sign in to Steam'])
        self.assertEqual(windows[0]['source'], 'niri')
        os.environ.pop('NIRI_SOCKET')
        windows = luna.gui_windows(env=dict(os.environ))
        self.assertEqual([w['source'] for w in windows], ['agent-desktop'])
        self.assertEqual(luna.gui_titles(windows), [])

    def test_accessible_mode_detection_reads_cmdlines(self):
        proc = self.work / 'proc'
        for pid, argv in (('10', ['/usr/lib/steam/steam', '-cef-force-accessibility']),
                          ('11', ['sleep', '5'])):
            (proc / pid).mkdir(parents=True)
            (proc / pid / 'cmdline').write_bytes(b'\0'.join(a.encode() for a in argv) + b'\0')
        self.assertEqual(luna.gui_steam_state(str(proc)), (True, True))
        (proc / '10' / 'cmdline').write_bytes(b'/usr/lib/steam/steam\0')
        self.assertEqual(luna.gui_steam_state(str(proc)), (True, False))
        (proc / '12').mkdir()
        (proc / '12' / 'cmdline').write_bytes(b'steamwebhelper\0--force-renderer-accessibility\0')
        self.assertEqual(luna.gui_steam_state(str(proc)), (True, True))
        shutil.rmtree(proc)
        self.assertEqual(luna.gui_steam_state(str(proc)), (False, False))


class Classification(GuiFixture):
    def load(self, name):
        return json.loads((FIXTURES / name).read_text())['data']['tree']

    def test_login_fixture_is_login_and_matches_by_role_and_name(self):
        tree_ = self.load('login-snapshot.json')
        self.assertEqual(luna.gui_classify(tree_), 'login')
        self.assertTrue(luna.gui_find_nodes(tree_, 'button', 'Sign in'))
        self.assertTrue(luna.gui_find_nodes(tree_, 'checkbox', 'Remember me'))
        self.assertEqual(luna.gui_classify(None, ['Sign in to Steam']), 'login')

    def test_real_main_window_is_main(self):
        store = self.load('main-store.json')
        self.assertEqual(luna.gui_classify(store), 'main')
        self.assertEqual(luna.gui_classify(self.load('main-library.json')), 'main')
        self.assertEqual(luna.gui_classify(self.load('luna-presence.json')), 'main')
        # the login window has neither tab
        self.assertNotEqual(luna.gui_classify(self.load('login-snapshot.json')), 'main')
        # a lone Luna button is not enough: the Store/Library tabs decide
        self.assertEqual(luna.gui_classify(tree(('button', 'Luna'))['data']['tree']), 'unknown')

    def test_dialog_and_unknown(self):
        # UNVERIFIED shape: no real dialog could be opened (menu items refuse activation),
        # so this fixture is synthetic and only pins the assumption the code makes.
        dialog = tree(('alert', 'Update available'), ('button', 'Restart'))['data']['tree']
        self.assertEqual(luna.gui_classify(dialog), 'dialog')
        self.assertEqual(luna.gui_dialogs(dialog)[0]['name'], 'Update available')
        self.assertEqual(luna.gui_classify(None), 'unknown')

    def test_steam_menu_exit_is_findable_by_role_and_name(self):
        menu = fixture_tree('steam-menu.json')
        self.assertEqual(luna.gui_find_nodes(menu, 'menuitem', 'Exit')[0]['name'], 'Exit')
        self.assertTrue(luna.gui_find_nodes(menu, 'menuitem', 'Sign Out...'))
        # find only: Luna never clicks menu items (they refuse semantic activation)
        self.assertNotIn('Click', luna.gui_find_nodes(menu, 'menuitem', 'Exit')[0].get('available_actions', []))

    def test_selectors_match_the_real_tree(self):
        store = self.load('main-store.json')
        self.assertTrue(luna.gui_match(store, luna.GUI_LUNA_ENTRY))
        self.assertFalse(luna.gui_overlay_open(store))
        for addon in ('playtime', 'presence', 'compressor'):
            page = self.load('luna-%s.json' % addon)
            self.assertTrue(luna.gui_overlay_open(page))
            self.assertTrue(luna.gui_match(page, luna.GUI_PANEL_SELECTORS[addon]['panel']), addon)
            self.assertFalse(luna.gui_match(store, luna.GUI_PANEL_SELECTORS[addon]['panel']), addon)
            tab = luna.gui_find_tab(page, luna.GUI_PANEL_SELECTORS[addon]['nav'])
            self.assertIn('sls-luna-tab', tab['dom_classes'])
        self.assertTrue(luna.gui_find_nodes(self.load('luna-playtime.json'), *luna.GUI_LUNA_CLOSE))


class Rendered(GuiFixture):
    def render(self, sim, addons=('luna', 'playtime', 'presence', 'compressor'), **extra):
        return luna.gui_rendered(list(addons), samples=2, sleep=lambda s: None, snapshot=sim.snapshot,
                                 click=sim.click, **extra)

    def test_all_found_navigates_and_closes_the_overlay(self):
        sim = Sim()
        result = self.render(sim)
        self.assertEqual({k: v['state'] for k, v in result.items()}, dict.fromkeys(result, 'found'))
        # Playtime is already showing when the overlay opens, so it is not clicked again
        self.assertEqual(sim.clicks, ['Luna', 'Presence', 'Compressor', '\u2715'])
        self.assertIsNone(sim.page)

    def test_overlay_already_open_is_left_open(self):
        sim = Sim()
        sim.page = 'playtime'
        self.render(sim, addons=('presence',))
        self.assertEqual(sim.clicks, ['Presence'])
        self.assertEqual(sim.page, 'presence')

    def test_no_navigate_checks_only_the_entry_point(self):
        sim = Sim()
        result = self.render(sim, navigate=False)
        self.assertEqual(result['luna']['state'], 'found')
        self.assertEqual(result['playtime'], {'state': 'skipped', 'hits': 0, 'samples': 0,
                                              'reason': 'navigation disabled'})
        self.assertEqual(sim.clicks, [])

    def test_refused_click_is_missing_or_skipped(self):
        sim = Sim(click_ok=False)
        result = self.render(sim)
        self.assertEqual(result['playtime']['state'], 'skipped')
        self.assertEqual(result['playtime']['reason'], 'the Luna overlay did not open')
        self.assertEqual(result['luna']['state'], 'found')
        sim = Sim()
        sim.page = 'playtime'
        sim.click_ok = False
        self.assertEqual(self.render(sim, addons=('compressor',))['compressor']['state'], 'missing')

    def test_unstable_when_the_entry_point_vanishes(self):
        store, gone = fixture_tree('main-store.json'), fixture_tree('login-snapshot.json')
        frames = iter([store, store, gone])  # first read, then two samples
        result = luna.gui_rendered(['luna'], samples=2, sleep=lambda s: None,
                                   snapshot=lambda: (next(frames), None), navigate=False)
        self.assertEqual((result['luna']['state'], result['luna']['hits']), ('unstable', 1))
        frames = iter([gone, gone, gone])
        result = luna.gui_rendered(['luna'], samples=2, sleep=lambda s: None,
                                   snapshot=lambda: (next(frames), None), navigate=False)
        self.assertEqual(result['luna']['state'], 'missing')

    def test_no_snapshot_is_skipped(self):
        result = luna.gui_rendered(['playtime'], samples=2, sleep=lambda s: None,
                                   snapshot=lambda: (None, 'no window'))
        self.assertEqual(result['playtime']['state'], 'skipped')
        self.assertEqual(result['playtime']['reason'], 'no window')


class Verify(GuiFixture):
    def options(self, **extra):
        values = dict(gui_action='verify', launch=False, wait=1, report=str(self.work / 'report.md'),
                      screenshots=str(self.work / 'shots'), only=[], json=False)
        values.update(extra)
        return argparse.Namespace(**values)

    def verify(self, state, **extra):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(luna, 'gui_steam_state', return_value=state), \
                mock.patch.object(luna.time, 'sleep'), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = luna.gui_verify(self.options(**extra), sleep=lambda s: None)
        return code, out.getvalue(), err.getvalue()

    def test_skipped_without_tool(self):
        code, out, _ = self.verify((True, True))
        self.assertEqual(code, 0)
        self.assertIn('GUI checks skipped: agent-desktop is not installed', out)
        self.assertFalse((self.work / 'report.md').exists())

    def test_skipped_when_steam_not_running_or_not_accessible(self):
        self.install_agent()
        self.assertIn('GUI checks skipped: Steam is not running', self.verify((False, False))[1])
        self.assertIn('not started in accessible mode', self.verify((True, False))[1])

    def test_login_screen_fails_with_sign_in_message(self):
        self.install_agent()
        self.install_niri()
        code, out, err = self.verify((True, True))
        self.assertEqual(code, 1)
        self.assertIn('sign in first', err)
        report = (self.work / 'report.md').read_text()
        self.assertIn('login', report)
        self.assertIn('FAIL', report)

    def test_panels_pass_and_report_written(self):
        self.install_agent()
        self.install_window_niri()
        self.install_grim()  # must stay unused: the default capture is the Steam window only
        sim = Sim()
        with mock.patch.object(luna, 'gui_snapshot', sim.snapshot), mock.patch.object(luna, 'gui_click_node', sim.click):
            code, out, err = self.verify((True, True))
        self.assertEqual((code, err), (0, ''))
        report = (self.work / 'report.md').read_text()
        self.assertIn('| playtime | found | 3/3 |', report)
        self.assertIn('| luna | found | 3/3 |', report)
        self.assertIn('PASS', report)
        self.assertTrue(list((self.work / 'shots').glob('gui-verify-*.png')))
        self.assertIn('window only, 640x480 px (niri window 640x480)', report)
        self.assertIsNone(sim.page)  # the overlay is closed again

    def test_missing_panel_fails(self):
        self.install_agent()
        sim = Sim()
        sim.click_ok = False
        sim.page = 'playtime'  # overlay open, but page tabs cannot be clicked
        with mock.patch.object(luna, 'gui_snapshot', sim.snapshot), mock.patch.object(luna, 'gui_click_node', sim.click):
            code, out, err = self.verify((True, True), only=['presence'])
        self.assertEqual(code, 1)
        self.assertIn('presence panel: missing', err)
        self.assertIn('## Evidence\n\nnone (no window-only screenshot', (self.work / 'report.md').read_text())

    def test_no_navigate_passes_without_clicking(self):
        self.install_agent()
        sim = Sim()
        with mock.patch.object(luna, 'gui_snapshot', sim.snapshot), mock.patch.object(luna, 'gui_click_node', sim.click):
            code, out, err = self.verify((True, True), no_navigate=True)
        self.assertEqual((code, err), (0, ''))
        self.assertEqual(sim.clicks, [])

    def test_no_window_fails(self):
        self.install_agent()
        self.cfg['snapshots'] = [None]
        self.write_cfg()
        with mock.patch.object(luna, 'gui_wait_for_window', return_value=None):
            code, _, err = self.verify((True, True))
        self.assertEqual(code, 1)
        self.assertIn('no Steam window', err)

    def test_launch_never_adds_flags_to_normal_launcher(self):
        self.assertNotIn('accessibility', ' '.join(['setsid', 'steam']))
        with mock.patch.object(luna.subprocess, 'Popen') as popen, \
                mock.patch.object(luna, 'gui_steam_state', return_value=(False, False)):
            ok, _ = luna.gui_launch_accessible()
        self.assertTrue(ok)
        self.assertEqual(popen.call_args[0][0], ['setsid', 'steam', '-cef-force-accessibility'])
        self.assertEqual(popen.call_args[1]['env']['ACCESSIBILITY_ENABLED'], '1')
        with mock.patch.object(luna.subprocess, 'Popen') as popen:
            luna._launch_steam_detached()
        self.assertEqual(popen.call_args[0][0], ['setsid', 'steam'])
        with mock.patch.object(luna.subprocess, 'Popen') as popen, \
                mock.patch.object(luna, 'gui_steam_state', return_value=(True, False)):
            ok, message = luna.gui_launch_accessible()
        self.assertFalse(ok)
        popen.assert_not_called()
        self.assertIn('already running', message)

    def test_status_command_degrades(self):
        with mock.patch.object(luna, 'gui_steam_state', return_value=(False, False)):
            code, out, _ = self.run_cli('gui')
        self.assertEqual(code, 0)
        self.assertIn('GUI checks skipped', out)
        self.assertIn('tool       : missing', out)


class Capture(GuiFixture):
    def capture(self, **extra):
        return luna.gui_capture(self.work / 'shots', 'a.png', **extra)

    def test_window_only_uses_niri_with_the_main_window_id_and_absolute_path(self):
        self.install_window_niri()
        self.install_grim()
        shot, note = self.capture()
        self.assertEqual(shot, self.work / 'shots/a.png')
        self.assertIn('window only, 640x480 px (niri window 640x480)', note)
        self.assertEqual(luna.gui_png_size(shot), (640, 480))
        calls = [c for c in self.niri_log() if 'screenshot-window' in c]
        self.assertEqual(calls, ['msg action screenshot-window --id 5 --path %s' % (self.work / 'shots/a.png')])

    def test_main_window_falls_back_to_the_largest_steam_window(self):
        self.install_window_niri(windows=[
            {'id': 3, 'title': '', 'app_id': 'steam', 'layout': {'window_size': [100, 100]}},
            {'id': 4, 'title': 'Friends List', 'app_id': 'steam', 'layout': {'window_size': [800, 600]}}])
        self.assertEqual(luna.gui_main_window_id(luna.gui_session_env())[0], 4)

    def test_late_file_is_waited_for(self):
        self.install_window_niri(delay=1)
        self.assertEqual(luna.gui_png_size(self.capture()[0]), (640, 480))

    def test_no_niri_skips_and_never_falls_back_to_grim(self):
        self.install_grim()
        shot, note = self.capture()
        self.assertIsNone(shot)
        self.assertIn('use --full-screen', note)
        self.assertFalse((self.work / 'shots/a.png').exists())

    def test_failed_or_missing_file_is_not_reported_saved(self):
        self.install_grim()
        for mode in ('fail', 'nothing'):
            self.install_window_niri(shot=mode)
            with mock.patch.object(luna.time, 'monotonic', side_effect=[0, 1, 9, 10, 11, 12, 13, 14]):
                shot, note = self.capture()
            self.assertIsNone(shot, mode)
            self.assertIn('niri', note)
        self.install_window_niri(windows=[])
        self.assertIsNone(self.capture()[0])

    def test_full_screen_is_opt_in(self):
        self.install_window_niri()
        self.install_grim()
        shot, note = self.capture(full_screen=True)
        self.assertEqual((shot, note), (self.work / 'shots/a.png', 'saved (full screen)'))
        self.assertEqual([c for c in self.niri_log() if 'screenshot-window' in c], [])

    def test_cli_flag_exists(self):
        with mock.patch.object(luna, 'gui_unavailable_reason', return_value='x'):
            self.assertEqual(self.run_cli('gui', 'verify', '--full-screen')[0], 0)


class Library(GuiFixture):
    def order(self):
        return luna.gui_library_order(fixture_tree('main-library.json'))

    def test_order_reads_cells_in_list_order_and_reports_virtualization(self):
        order = self.order()
        self.assertEqual((order['rendered'], order['total'], order['virtualized']), (54, 1630, True))
        self.assertEqual(order['names'][:2], ['Game 001', 'Game 002'])
        self.assertEqual(len(set(order['names'])), 54)
        self.assertIsNone(luna.gui_library_order(fixture_tree('main-store.json')))

    def test_compare(self):
        names = self.order()['names']
        self.assertEqual(luna.gui_library_compare(names, list(names)), (True, []))
        swapped = [names[1], names[0]] + names[2:]
        same, lines = luna.gui_library_compare(names, swapped)
        self.assertFalse(same)
        self.assertIn("+Game 002", lines)
        # a scrolled window only shares part of the list: relative order is what counts
        same, _ = luna.gui_library_compare(names, names[10:30] + ['Other'], common=True)
        self.assertTrue(same)
        same, _ = luna.gui_library_compare(names, [names[12], names[11]], common=True)
        self.assertFalse(same)
        self.assertFalse(luna.gui_library_compare(names, ['Nothing shared'], common=True)[0])

    def run_library(self, *args, tree_name='main-library.json'):
        frame = fixture_tree(tree_name)
        with mock.patch.object(luna, 'gui_snapshot', lambda *a, **k: (frame, None)), \
                mock.patch.object(luna, 'gui_unavailable_reason', return_value=None):
            return self.run_cli('gui', 'library', *args)

    def test_command_save_and_compare_exit_codes(self):
        saved = self.work / 'order.json'
        code, out, _ = self.run_library('--save', str(saved))
        self.assertEqual(code, 0)
        self.assertIn('54 rendered of ~1630', out)
        self.assertEqual(json.loads(saved.read_text())['rendered'], 54)
        code, out, _ = self.run_library('--compare', str(saved))
        self.assertEqual((code, 'matches' in out), (0, True))
        data = json.loads(saved.read_text())
        data['names'][0], data['names'][1] = data['names'][1], data['names'][0]
        saved.write_text(json.dumps(data))
        code, _, err = self.run_library('--compare', str(saved))
        self.assertEqual(code, 1)
        self.assertIn('library order differs', err)
        saved.write_text('{"nope": 1}')
        self.assertEqual(self.run_library('--compare', str(saved))[0], 1)

    def test_command_json_and_missing_list(self):
        code, out, _ = self.run_library('--json')
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)['total'], 1630)
        code, _, err = self.run_library(tree_name='main-store.json')
        self.assertEqual(code, 1)
        self.assertIn('Library list is not showing', err)

    def test_open_clicks_the_library_tab(self):
        calls = []
        frames = iter([fixture_tree('main-store.json')] * 2 + [fixture_tree('main-library.json')] * 3)
        def click(node, *a, **k):
            calls.append((node['role'], node['name']))
            return {'ok': True}
        with mock.patch.object(luna, 'gui_snapshot', lambda *a, **k: (next(frames), None)), \
                mock.patch.object(luna, 'gui_click_node', click), \
                mock.patch.object(luna.time, 'sleep'), \
                mock.patch.object(luna, 'gui_unavailable_reason', return_value=None):
            code, out, _ = self.run_cli('gui', 'library', '--open')
        self.assertEqual((code, calls), (0, [('statictext', 'LIBRARY')]))

    def test_disabled_by_environment(self):
        os.environ['LUNA_GUI_CHECKS'] = '0'
        code, out, _ = self.run_cli('gui', 'library')
        self.assertEqual(code, 0)
        self.assertIn('GUI checks skipped: disabled (LUNA_GUI_CHECKS=0)', out)


class Blockers(GuiFixture):
    def test_noop_when_nothing_available(self):
        os.environ.pop('WAYLAND_DISPLAY')
        os.environ.pop('DISPLAY')
        with mock.patch.object(luna, 'gui_session_env', return_value={}):
            self.assertEqual(luna.gui_describe_blockers(), '')
        self.assertEqual(luna.gui_describe_blockers(), '')  # no niri, no agent-desktop
        os.environ['LUNA_GUI_CHECKS'] = '0'
        self.assertEqual(luna.gui_describe_blockers(), '')

    def test_never_raises(self):
        with mock.patch.object(luna, 'gui_windows', side_effect=RuntimeError('boom')):
            self.assertEqual(luna.gui_describe_blockers(), '')

    def test_reports_titles_and_dialog_buttons(self):
        self.install_agent()
        self.install_niri()
        self.cfg['snapshots'] = [self.snapshot_file('dlg.json', ('alert', 'Update available',
                                                     [('button', 'Restart'), ('button', 'Later')]))]
        self.write_cfg()
        with mock.patch.object(luna, 'gui_steam_state', return_value=(True, True)):
            text = luna.gui_describe_blockers()
        self.assertIn('"Sign in to Steam"', text)
        self.assertIn('dialog "Update available" with buttons: Restart, Later', text)

    def test_stays_within_budget(self):
        self.install_tool('agent-desktop', '#!/bin/sh\nsleep 10\n')
        self.install_tool('niri', '#!/bin/sh\nsleep 10\n')
        started = time.monotonic()
        with mock.patch.object(luna, 'gui_steam_state', return_value=(True, True)):
            self.assertEqual(luna.gui_describe_blockers(budget=1.0), '')
        self.assertLess(time.monotonic() - started, 4)

    def test_lumen_survivor_reuses_process_running(self):
        with mock.patch.object(luna, 'process_running', return_value=False):
            self.assertIsNone(luna.gui_lumen_survivor())
            self.assertEqual(luna.gui_lumen_note(), '')
        self.install_tool('pgrep', '#!/bin/sh\necho 4242\n')
        self.install_tool('ps', '#!/bin/sh\necho "Sat Nov 14 10:00:00 2026"\n')
        with mock.patch.object(luna, 'process_running', return_value=True):
            self.assertEqual(luna.gui_lumen_survivor(), ('4242', 'Sat Nov 14 10:00:00 2026'))
            self.assertIn('pid 4242', luna.gui_lumen_note())


if __name__ == '__main__':
    unittest.main()
