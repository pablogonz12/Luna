"""GUI-check layer against fake agent-desktop / niri / grim executables on PATH.

The fakes replay trimmed real captures from tests/fixtures/gui; nothing here
touches the real desktop, Steam or the network."""
import argparse
import contextlib
import importlib.util
import inspect
import io
import json
import os
import re
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import time
import unittest
from unittest import mock

os.environ.setdefault('LUNA_LIVE_CHECKS', '0')  # hermetic: tests never contact a real Steam

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
    if os.environ.get('FAKE_CLIP'):
        json.dump({'type': 'image/png', 'data': 'PNGBYTES'}, open(os.environ['FAKE_CLIP'], 'w'))
elif args[:3] == ['msg', '--json', 'windows']:
    sys.stdout.write(open(cfg['windows']).read())
else:
    sys.exit(1)
'''


FAKE_WLPASTE = '''#!/usr/bin/env python3
import json, os, sys
path = os.environ['FAKE_CLIP']
args = sys.argv[1:]
try:
    clip = json.load(open(path))
except OSError:
    clip = None
if cfg_fail := os.environ.get('FAKE_CLIP_FAIL'):
    sys.exit(1)
if clip is None:
    sys.stderr.write('Nothing is copied\\n')
    sys.exit(1)
if '--list-types' in args:
    print(clip['type'])
    for extra in clip.get('extra', []):
        print(extra)
else:
    sys.stdout.write(clip['data'])
'''

FAKE_WLCOPY = '''#!/usr/bin/env python3
import json, os, sys
path = os.environ['FAKE_CLIP']
args = sys.argv[1:]
with open(path + '.log', 'a') as log:
    log.write(' '.join(args) + '\\n')
if '--clear' in args:
    if os.path.exists(path):
        os.unlink(path)
else:
    json.dump({'type': args[args.index('--type') + 1], 'data': sys.stdin.read()}, open(path, 'w'))
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
        self.clip = self.work / 'clip.json'
        os.environ['FAKE_CLIP'] = str(self.clip)
        os.environ.pop('FAKE_CLIP_FAIL', None)
        self.install_tool('wl-paste', FAKE_WLPASTE)
        self.install_tool('wl-copy', FAKE_WLCOPY)

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
        self.assertEqual(luna.gui_classify(fixture_tree('main-library.json')), 'main')
        self.assertEqual(luna.gui_classify(self.load('luna-presence.json')), 'main')
        # the login window has neither tab
        self.assertNotEqual(luna.gui_classify(self.load('login-snapshot.json')), 'main')
        # a lone Luna button is not enough: the Store/Library tabs decide
        self.assertEqual(luna.gui_classify(tree(('button', 'Luna'))['data']['tree']), 'unknown')

    def test_real_secondary_window_shape(self):
        # Observed live (steam://open/settings): a separate AT-SPI window with one
        # webarea named after the window, tabs and buttons, and NO dialog/alert role.
        window = self.load('secondary-window-settings.json')
        self.assertEqual(luna.gui_secondary_windows(window), ['Steam Settings'])
        self.assertEqual(luna.gui_dialogs(window), [])
        self.assertEqual(luna.gui_classify(window), 'unknown')  # neither main, login nor modal
        self.assertTrue(luna.gui_find_nodes(window, 'tab', 'Account'))
        self.assertFalse(luna.gui_find_nodes(window, 'button', 'Close'))
        self.assertEqual(luna.gui_secondary_windows(self.load('main-store.json')), [])
        self.assertEqual(luna.gui_secondary_windows(self.load('login-snapshot.json')), [])

    def test_dialog_and_unknown(self):
        # NOT observed live: no real modal could be opened safely. This synthetic tree
        # only pins the assumption the code makes about the dialog/alert roles.
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
        files = {'hltb': 'luna-hltb.json', 'sgdb': 'luna-steamgriddb.json', 'graphics': 'luna-graphics.json',
                 'health': 'luna-health.json', 'settings': 'luna-settings.json', 'about': 'luna-about.json'}
        pages = {a: self.load(f) for a, f in files.items()}
        for addon in ('playtime', 'presence', 'compressor'):
            pages[addon] = self.load('luna-%s.json' % addon)
        for addon, page in pages.items():
            # every confirmed page: found on its own page, absent on every other page
            self.assertTrue(luna.gui_overlay_open(page), addon)
            self.assertTrue(luna.gui_match(page, luna.GUI_PANEL_SELECTORS[addon]['panel']), addon)
            for other, other_page in pages.items():
                if other != addon:
                    self.assertFalse(luna.gui_match(other_page, luna.GUI_PANEL_SELECTORS[addon]['panel']),
                                     '%s matched on %s' % (addon, other))
            tab = luna.gui_find_tab(page, luna.GUI_PANEL_SELECTORS[addon]['nav'])
            self.assertIn('sls-luna-tab', tab['dom_classes'], addon)
        self.assertFalse([a for a in ('hltb', 'health', 'settings', 'about') if a in luna.ADDONS])
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
    def test_skipped_without_tool(self):
        code, out, _ = self.verify((True, True))
        self.assertEqual(code, luna.EXIT_SKIPPED)
        self.assertEqual(luna.EXIT_SKIPPED, 5)
        self.assertNotIn(luna.EXIT_SKIPPED, (luna.EXIT_OK, 1, luna.EXIT_REFUSED, luna.EXIT_NEEDS_FORCE, luna.EXIT_PATCH))
        self.assertIn('GUI checks skipped: agent-desktop is not installed', out)
        self.assertFalse((self.work / 'report.md').exists())

    def test_skipped_when_steam_not_running_or_not_accessible(self):
        self.install_agent()
        code, out, _ = self.verify((False, False))
        self.assertEqual(code, luna.EXIT_SKIPPED)
        self.assertIn('GUI checks skipped: Steam is not running', out)
        code, out, _ = self.verify((True, False))
        self.assertEqual(code, luna.EXIT_SKIPPED)
        self.assertIn('not started in accessible mode', out)

    def test_login_screen_fails_with_sign_in_message(self):
        self.install_agent()
        self.install_niri()
        with mock.patch.object(luna, 'gui_process_started', return_value=time.time() - 3600), \
                mock.patch.object(luna, 'gui_logon_evidence', return_value=('none', '')):  # not a real Steam's
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

    def clip_state(self):
        return json.loads(self.clip.read_text()) if self.clip.exists() else None

    def test_text_clipboard_survives_the_capture(self):
        self.install_window_niri()
        self.clip.write_text(json.dumps({'type': 'text/plain;charset=utf-8', 'data': 'precious', 'extra': ['TEXT']}))
        shot, note = self.capture()
        self.assertIsNotNone(shot)
        self.assertEqual(self.clip_state(), {'type': 'text/plain;charset=utf-8', 'data': 'precious'})
        self.assertNotIn('clipboard', note)

    def test_image_clipboard_keeps_its_type_and_empty_clipboard_is_cleared(self):
        self.install_window_niri()
        self.clip.write_text(json.dumps({'type': 'image/jpeg', 'data': 'JPG'}))
        self.capture()
        self.assertEqual(self.clip_state(), {'type': 'image/jpeg', 'data': 'JPG'})
        self.clip.unlink()
        self.capture()
        self.assertIsNone(self.clip_state())  # empty before, empty after (wl-copy --clear)
        self.assertIn('--clear', (self.work / 'clip.json.log').read_text())

    def test_clipboard_not_restored_note_without_wl_clipboard(self):
        self.install_window_niri()
        real = shutil.which
        with mock.patch.object(luna.shutil, 'which',
                               side_effect=lambda n, *a, **k: None if n.startswith('wl-') else real(n, *a, **k)):
            shot, note = self.capture()
        self.assertIsNotNone(shot)  # captured anyway
        self.assertIn('clipboard not restored: wl-clipboard (wl-paste/wl-copy) is not installed', note)
        self.assertEqual(self.clip_state()['type'], 'image/png')  # niri's image was left alone

    def test_unreadable_clipboard_is_noted_and_not_overwritten(self):
        self.install_window_niri()
        os.environ['FAKE_CLIP_FAIL'] = '1'
        shot, note = self.capture()
        self.assertIsNotNone(shot)
        self.assertIn('wl-paste could not read it', note)
        self.assertFalse((self.work / 'clip.json.log').exists())

    def test_cli_flag_exists(self):
        with mock.patch.object(luna, 'gui_unavailable_reason', return_value='x'):
            self.assertEqual(self.run_cli('gui', 'verify', '--full-screen')[0], luna.EXIT_SKIPPED)


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
        self.assertEqual(code, luna.EXIT_SKIPPED)
        self.assertIn('GUI checks skipped: disabled (LUNA_GUI_CHECKS=0)', out)
        self.assertEqual(self.run_cli('gui', 'launch')[0], luna.EXIT_SKIPPED)
        self.assertEqual(self.run_cli('gui')[0], 0)  # status only reports


class Watch(GuiFixture):
    PRESENT = tree(('button', 'Luna'))['data']['tree']
    ABSENT = tree(('button', 'Other'))['data']['tree']

    def run_watch(self, frames, interrupt_after=None, **extra):
        """Drive gui_watch with a scripted sequence of snapshots and a fake clock."""
        now = [1000.0]
        frames = list(frames)
        calls = []

        def snapshot():
            calls.append(1)
            frame = frames[min(len(calls) - 1, len(frames) - 1)]
            return (frame, None) if isinstance(frame, dict) else (None, frame)

        def sleep(seconds):
            if interrupt_after is not None and len(calls) >= interrupt_after:
                raise KeyboardInterrupt
            now[0] += seconds
        values = dict(role='button', name='Luna', contains=False, addon=None, interval=1.0, duration=None,
                      fail_if_vanished=False)
        values.update(extra)
        lines = []
        with mock.patch.object(luna, 'gui_unavailable_reason', return_value=None):
            code = luna.gui_watch(argparse.Namespace(**values), sleep=sleep, snapshot=snapshot,
                                  clock=lambda: now[0], out=lines.append)
        return code, lines

    def test_prints_appear_and_vanish_with_timestamps_and_a_summary(self):
        code, lines = self.run_watch([self.ABSENT, self.PRESENT, self.PRESENT, self.ABSENT, self.PRESENT],
                                     duration=4)
        self.assertEqual(code, 0)
        self.assertTrue(re.match(r'\d\d:\d\d:\d\d\.\d{3} absent    button \'Luna\' \(not seen yet\)', lines[1]), lines)
        kinds = [line.split()[1] for line in lines[1:-1]]
        self.assertEqual(kinds, ['absent', 'APPEARED', 'VANISHED', 'APPEARED'])
        self.assertIn('appeared 2, vanished 1, now present', lines[-1])

    def test_fail_if_vanished_stops_at_the_vanish_and_exits_1(self):
        code, lines = self.run_watch([self.PRESENT, self.PRESENT, self.ABSENT, self.PRESENT], fail_if_vanished=True)
        self.assertEqual(code, 1)
        self.assertIn('VANISHED', ' '.join(lines))
        self.assertIn('FAIL: button', lines[-1])
        self.assertNotIn('APPEARED', ' '.join(lines[lines.index([l for l in lines if 'VANISHED' in l][0]):]))

    def test_never_seen_is_not_a_vanish(self):
        code, lines = self.run_watch([self.ABSENT], duration=3, fail_if_vanished=True)
        self.assertEqual(code, 0)
        self.assertNotIn('VANISHED', ' '.join(lines))

    def test_ctrl_c_ends_cleanly_with_a_summary(self):
        code, lines = self.run_watch([self.PRESENT], interrupt_after=2)
        self.assertEqual(code, 0)
        self.assertIn('interrupted', ' '.join(lines))
        self.assertTrue(lines[-1].startswith('summary: 2 samples'))

    def test_unreadable_snapshots_are_not_a_vanish(self):
        code, lines = self.run_watch([self.PRESENT, 'no window', 'no window', self.PRESENT], duration=3,
                                     fail_if_vanished=True)
        self.assertEqual(code, 0)
        text = '\n'.join(lines)
        self.assertEqual(text.count('unreadable'), 2)  # one line, plus the summary counter
        self.assertIn('readable again', text)
        self.assertEqual(text.count('APPEARED'), 1)
        self.assertIn('vanished 0', lines[-1])

    def test_addon_key_and_contains_and_bad_selectors(self):
        page = fixture_tree('luna-presence.json')
        code, lines = self.run_watch([page], duration=1, role=None, name=None, addon='presence')
        self.assertIn('APPEARED  heading', ' '.join(lines))
        code, lines = self.run_watch([page], duration=1, role='heading', name='Pres', contains=True)
        self.assertIn('APPEARED', ' '.join(lines))
        for bad in ({'role': None, 'name': None}, {'addon': 'nope'}, {'addon': 'presence', 'role': 'button'}):
            with contextlib.redirect_stderr(io.StringIO()) as err:
                code, _ = self.run_watch([page], **bad)
            self.assertEqual(code, luna.EXIT_REFUSED)
            self.assertIn('REFUSING', err.getvalue())

    def test_skipped_exit_code_and_read_only(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = luna.gui_watch(argparse.Namespace(role='button', name='Luna', contains=False, addon=None,
                                                     interval=1, duration=1, fail_if_vanished=False))
        self.assertEqual(code, luna.EXIT_SKIPPED)
        self.assertIn('GUI checks skipped', out.getvalue())
        self.assertNotIn('gui_click_node', inspect.getsource(luna.gui_watch))

    def test_cli_wiring(self):
        with mock.patch.object(luna, 'gui_unavailable_reason', return_value=None), \
                mock.patch.object(luna, 'gui_snapshot', return_value=(self.PRESENT, None)), \
                mock.patch.object(luna.time, 'sleep'):
            code, out, _ = self.run_cli('gui', 'watch', '--role', 'button', '--name', 'Luna', '--duration', '0',
                                        '--interval', '0.2')
        self.assertEqual(code, 0)
        self.assertIn('APPEARED', out)


class Hooks(GuiFixture):
    class Tty(io.StringIO):
        def isatty(self):
            return True

    def test_install_hint_needs_terminal_tool_session_and_checks_enabled(self):
        self.install_agent()
        with mock.patch.object(subprocess, 'Popen') as popen, mock.patch.object(subprocess, 'run') as run:
            hint = luna.gui_install_hint(self.Tty())
            self.assertIn('luna gui verify', hint)
            self.assertIn('luna status --live', hint)
            self.assertNotIn('\n', hint)  # one line
            self.assertEqual(luna.gui_install_hint(io.StringIO()), '')  # pipe / unattended updater
            os.environ['LUNA_GUI_CHECKS'] = '0'
            self.assertEqual(luna.gui_install_hint(self.Tty()), '')
            os.environ.pop('LUNA_GUI_CHECKS')
            os.environ.pop('WAYLAND_DISPLAY')
            os.environ.pop('DISPLAY')
            with mock.patch.object(luna, 'gui_session_env', return_value={}):
                self.assertEqual(luna.gui_install_hint(self.Tty()), '')
            popen.assert_not_called()  # the hint never starts, stops or restarts Steam
            run.assert_not_called()
        (self.bin / 'agent-desktop').unlink()
        self.assertEqual(luna.gui_install_hint(self.Tty()), '')

    def test_verify_launch_puts_steam_back_unless_kept(self):
        self.install_agent()
        for extra, expect_close in (({}, True), ({'keep_steam': True}, False)):
            with mock.patch.object(luna, 'gui_launch_accessible', return_value=(True, 'launched')), \
                    mock.patch.object(luna, 'gui_wait_for_window', return_value=None), \
                    mock.patch.object(luna, '_close_steam_gracefully', return_value=True) as close:
                code, out, err = self.verify((False, False), launch=True, **extra)
            self.assertEqual(code, 1)  # no window appeared: still reported as a failure
            self.assertEqual(close.called, expect_close, extra)
            self.assertEqual('closing it again' in out, expect_close)

    def test_steam_not_launched_by_us_is_never_closed(self):
        self.install_agent()
        with mock.patch.object(luna, 'gui_wait_for_window', return_value=None), \
                mock.patch.object(luna, '_close_steam_gracefully') as close:
            self.verify((True, True), launch=True)
            self.verify((True, False), launch=True)  # skipped: normal mode, not ours to touch
            self.verify((False, False))  # no --launch: skipped
        close.assert_not_called()

    def test_failed_launch_closes_nothing_and_a_stuck_steam_is_reported(self):
        self.install_agent()
        with mock.patch.object(luna, 'gui_launch_accessible', return_value=(False, 'no')), \
                mock.patch.object(luna, '_close_steam_gracefully') as close:
            self.assertEqual(self.verify((False, False), launch=True)[0], 1)
        close.assert_not_called()
        with mock.patch.object(luna, 'gui_launch_accessible', return_value=(True, 'launched')), \
                mock.patch.object(luna, 'gui_wait_for_window', return_value=None), \
                mock.patch.object(luna, 'gui_describe_blockers', return_value='open Steam windows: "Steam"'), \
                mock.patch.object(luna, '_close_steam_gracefully', return_value=False):
            code, out, err = self.verify((False, False), launch=True)
        self.assertIn('did not exit', err)
        self.assertIn('open Steam windows: "Steam"', err)


class Backend(GuiFixture):
    def test_niri_backend_when_niri_and_its_socket_exist(self):
        backend = luna.gui_backend(luna.gui_session_env())
        self.assertEqual(backend['name'], 'niri')
        self.assertEqual(backend['capture'], 'niri msg action screenshot-window')
        self.assertEqual(backend['note'], '')

    def test_generic_backend_without_a_niri_session_names_what_is_missing(self):
        os.environ.pop('NIRI_SOCKET')
        os.environ['XDG_CURRENT_DESKTOP'] = 'sway'
        backend = luna.gui_backend(luna.gui_session_env())
        self.assertEqual(backend['name'], 'generic')
        self.assertIsNone(backend['capture'])
        self.assertIn('not supported on sway: window-only screenshots need niri', backend['note'])
        self.assertIn('no NIRI_SOCKET', backend['note'])
        real = shutil.which
        with mock.patch.object(luna.shutil, 'which',
                               side_effect=lambda n, *a, **k: None if n == 'niri' else real(n, *a, **k)):
            self.assertIn('niri is not installed', luna.gui_backend(luna.gui_session_env())['note'])

    def test_generic_backend_lists_windows_through_agent_desktop_and_never_captures(self):
        self.install_agent()
        self.install_grim()
        os.environ.pop('NIRI_SOCKET')
        os.environ['XDG_CURRENT_DESKTOP'] = 'GNOME'
        windows = luna.gui_windows(env=luna.gui_session_env())
        self.assertEqual({w['source'] for w in windows}, {'agent-desktop'})
        shot, note = luna.gui_capture(self.work / 'shots', 'a.png')
        self.assertIsNone(shot)
        self.assertIn('not supported on GNOME', note)
        self.assertIn('--full-screen', note)
        self.assertFalse((self.work / 'shots/a.png').exists())

    def test_status_prints_and_reports_the_backend(self):
        with mock.patch.object(luna, 'gui_steam_state', return_value=(False, False)):
            code, out, _ = self.run_cli('gui')
            self.assertIn('backend    : niri (windows: niri msg --json windows; capture: niri msg action '
                          'screenshot-window)', out)
            os.environ.pop('NIRI_SOCKET')
            os.environ['XDG_CURRENT_DESKTOP'] = 'KDE'
            code, out, _ = self.run_cli('gui')
            self.assertIn('backend    : generic', out)
            self.assertIn('capture: none', out)
            self.assertIn('not supported on KDE', out)
            info = json.loads(self.run_cli('gui', '--json')[1])
        self.assertEqual(info['backend']['name'], 'generic')


class Restart(GuiFixture):
    ORDER = ['Alpha', 'Bravo', 'Charlie']

    def state(self, order=None, screen='main', panels=None, source='cdp', sort=None):
        return {'screen': screen, 'panels': panels if panels is not None else {'luna': 'found', 'playtime': 'found'},
                'order': {'source': source, 'names': list(order or self.ORDER), 'total': 3, 'sort': sort},
                'download': ''}

    def run_check(self, start=(True, True), states=None, games=(), download='', close=(True, True), survivor=None,
                  launch=(True, 'launched'), screen='main', normal='main', **extra):
        """Drive gui_restart_check against a fake Steam. Returns (code, out, err, mocks)."""
        states = list(states or [self.state(), self.state()])
        report = self.work / 'restart.md'
        values = dict(report=str(report), screenshots=str(self.work / 'shots'), force=False, final=None,
                      keep_steam=False, shutdown_timeout=5, relaunch_wait=6, settle=0, full_screen=False)
        values.update(extra)
        closes = list(close)
        clock = [0.0]

        def sleep(seconds):
            clock[0] += seconds
        m = {}
        patches = {
            'gui_unavailable_reason': mock.Mock(return_value=None),
            'gui_steam_state': mock.Mock(return_value=start),
            'gui_running_games': mock.Mock(return_value=list(games)),
            'gui_active_download': mock.Mock(return_value=download),
            'gui_snapshot': mock.Mock(return_value=({'role': 'window'}, None)),
            'gui_read_state': mock.Mock(side_effect=states),
            '_close_steam_gracefully': mock.Mock(side_effect=lambda timeout=90: closes.pop(0) if closes else True),
            'gui_lumen_survivor': mock.Mock(return_value=survivor),
            'gui_launch_accessible': mock.Mock(return_value=launch),
            'gui_wait_for_screen': mock.Mock(return_value=(screen, {'role': 'window'}, 'fake signal')),
            '_launch_steam_detached': mock.Mock(return_value=True),
            'gui_wait_normal_window': mock.Mock(return_value=(normal, 'fake signal')),
            'gui_capture': mock.Mock(return_value=(self.work / 'shot.png', 'saved, window only')),
            'gui_describe_blockers': mock.Mock(return_value='open Steam windows: "Steam"'),
        }
        out, err = io.StringIO(), io.StringIO()
        with contextlib.ExitStack() as stack:
            for name, mocked in patches.items():
                m[name] = mocked
                stack.enter_context(mock.patch.object(luna, name, mocked))
            stack.enter_context(contextlib.redirect_stdout(out))
            stack.enter_context(contextlib.redirect_stderr(err))
            code = luna.gui_restart_check(argparse.Namespace(**values), sleep=sleep, clock=lambda: clock[0])
        self.report_path = report
        return code, out.getvalue(), err.getvalue(), m

    def test_report_lists_signals_and_sidecar_is_only_a_warning(self):
        code, out, err, m = self.run_check(survivor=('4242', '2026-11-15 14:00'))
        self.assertEqual(code, 0, err)
        self.assertIn('WARNING:', err)
        text = self.report_path.read_text()
        self.assertIn('## How each conclusion was reached', text)
        self.assertIn('relaunch: main (fake signal)', text)
        self.assertIn('WARNING', text)

    def test_exit_during_startup_fails_with_reason(self):
        code, out, err, m = self.run_check(screen='exited')
        self.assertEqual(code, 1)
        self.assertIn('exited during startup', err)


    def test_mixed_order_sources_are_not_a_difference(self):
        tree = self.state(source='tree (visible rows only)')
        tree['order']['sort'] = None
        tree['order']['why'] = 'CDP: the Library list could not be read'
        warnings = []
        rows, diffs = luna.gui_compare_states(tree, self.state(), warnings)
        self.assertEqual(diffs, [])
        self.assertIn('could not be compared', warnings[0])
        self.assertIn('n/a', [r[3] for r in rows])

    def test_after_state_waits_for_panels_that_were_found_before(self):
        late = self.state(panels={'luna': 'found', 'playtime': 'missing'})
        code, out, err, m = self.run_check(states=[self.state(), late, self.state()], settle=60)
        self.assertEqual(code, 0, err)
        self.assertEqual(m['gui_read_state'].call_count, 3)

    def test_sort_state_ignores_its_timestamp(self):
        a, b = self.state(sort={'stored': {'slsLunaSortDiag': {'at': 1, 'seen': 3}}, 'luna': {'value': 12}}), \
            self.state(sort={'stored': {'slsLunaSortDiag': {'at': 2, 'seen': 3}}, 'luna': {'value': 12}})
        self.assertEqual(luna.gui_compare_states(a, b)[1], [])
        b['order']['sort']['luna']['value'] = 4
        self.assertEqual(luna.gui_compare_states(a, b)[1], ['sort state'])

    def test_identical_state_passes_and_writes_report_and_table(self):
        code, out, err, m = self.run_check()
        self.assertEqual(code, 0, err)
        text = self.report_path.read_text()
        self.assertIn('PASS', text)
        self.assertIn('| library order', text)
        self.assertIn('shot.png', text)
        self.assertIn('before/after:', out)
        m['gui_capture'].assert_called_once()
        self.assertEqual(m['_close_steam_gracefully'].call_count, 1)  # started accessible: final stays accessible

    def test_order_or_sort_difference_exits_1(self):
        code, out, err, _ = self.run_check(states=[self.state(), self.state(order=['Bravo', 'Alpha', 'Charlie'])])
        self.assertEqual(code, 1)
        self.assertIn('library order', err)
        self.assertIn('FAIL', self.report_path.read_text())
        code, _, err, _ = self.run_check(states=[self.state(sort={'k': 'a'}), self.state(sort={'k': 'b'})])
        self.assertEqual(code, 1)
        self.assertIn('sort state', err)

    def test_panel_that_is_missing_after_is_a_failure(self):
        code, _, err, _ = self.run_check(states=[self.state(panels={'luna': 'found'}), self.state(panels={'luna': 'missing'})])
        self.assertEqual(code, 1)
        self.assertIn('luna', err)

    def test_refuses_a_running_game_and_an_active_download_and_force_overrides(self):
        code, _, err, m = self.run_check(games=[{'pid': 7, 'appid': '440'}])
        self.assertEqual((code, luna.EXIT_NEEDS_FORCE), (3, 3))
        self.assertIn('AppId 440', err)
        m['_close_steam_gracefully'].assert_not_called()
        m['gui_launch_accessible'].assert_not_called()
        code, _, err, m = self.run_check(download='the Downloads queue shows 1 of 4 items complete')
        self.assertEqual(code, 3)
        self.assertIn('downloading', err)
        m['_close_steam_gracefully'].assert_not_called()
        code, _, err, m = self.run_check(games=[{'pid': 7, 'appid': '440'}], force=True)
        self.assertEqual(code, 0, err)
        self.assertIn('overridden with --force', self.report_path.read_text())

    def test_bad_final_and_skipped(self):
        code, _, err, m = self.run_check(final='nope')
        self.assertEqual(code, luna.EXIT_REFUSED)
        self.assertIn('--final', err)
        m['gui_steam_state'].assert_not_called()
        out = io.StringIO()
        with mock.patch.object(luna, 'gui_unavailable_reason', return_value='agent-desktop is not installed'), \
                contextlib.redirect_stdout(out):
            code = luna.gui_restart_check(argparse.Namespace(final=None, keep_steam=False))
        self.assertEqual(code, luna.EXIT_SKIPPED)
        self.assertIn('GUI checks skipped', out.getvalue())

    def test_shutdown_failure_reports_blockers_and_kills_nothing(self):
        with mock.patch.object(luna.os, 'kill') as kill, mock.patch.object(luna.subprocess, 'Popen') as popen:
            code, _, err, m = self.run_check(close=(False,))
        self.assertEqual(code, 1)
        self.assertIn('did not exit', err)
        self.assertIn('open Steam windows: "Steam"', err)
        kill.assert_not_called()
        m['gui_launch_accessible'].assert_not_called()  # no relaunch over a stuck Steam
        self.assertIn('did not exit', self.report_path.read_text())

    def test_sidecar_survivor_is_reported(self):
        code, _, _, _ = self.run_check(survivor=('4242', '2026-11-15 14:00'))
        text = self.report_path.read_text()
        self.assertIn('STILL RUNNING (pid 4242, started 2026-11-15 14:00)', text)
        code, _, _, _ = self.run_check()
        self.assertIn('Lumen sidecar after shutdown: gone', self.report_path.read_text())

    def test_relaunch_failure(self):
        code, _, err, m = self.run_check(launch=(False, 'no graphical session'))
        self.assertEqual(code, 1)
        self.assertIn('could not relaunch Steam: no graphical session', err)
        m['gui_wait_for_screen'].assert_not_called()

    def test_login_screen_after_relaunch_stops_without_another_restart(self):
        code, _, err, m = self.run_check(screen='login')
        self.assertEqual(code, 1)
        self.assertIn('sign-in screen', err)
        self.assertEqual(m['_close_steam_gracefully'].call_count, 1)
        m['_launch_steam_detached'].assert_not_called()
        self.assertEqual(m['gui_launch_accessible'].call_count, 1)
        self.assertIn('not restarted again', self.report_path.read_text())

    def test_start_modes(self):
        # normal mode: no tree before, so nothing but the CDP order is recorded; final stays normal
        with mock.patch.object(luna, 'gui_read_order', return_value=self.state()['order']):
            code, _, err, m = self.run_check(start=(True, False), states=[self.state()])
        self.assertEqual(code, 0, err)
        text = self.report_path.read_text()
        self.assertIn('state before (normal)', text)
        self.assertEqual(m['_close_steam_gracefully'].call_count, 2)  # shutdown, then back to normal
        m['_launch_steam_detached'].assert_called_once()
        self.assertIn('normal mode, signed in', text)
        # not running: nothing to compare, shutdown skipped, final stopped
        code, _, err, m = self.run_check(start=(False, False), states=[self.state()])
        self.assertEqual(code, 0, err)
        text = self.report_path.read_text()
        self.assertIn('skipped: Steam was not running', text)
        self.assertIn('stopped (it was not running before)', text)
        self.assertEqual(m['_close_steam_gracefully'].call_count, 1)  # only the final close

    def test_final_state_rules(self):
        _, _, _, m = self.run_check(final='normal')
        self.assertEqual(m['_launch_steam_detached'].call_count, 1)
        self.assertIn('normal mode, signed in', self.report_path.read_text())
        _, _, _, m = self.run_check(final='stopped')
        self.assertEqual(m['_close_steam_gracefully'].call_count, 2)
        m['_launch_steam_detached'].assert_not_called()
        self.assertIn('stopped', self.report_path.read_text())
        _, _, _, m = self.run_check(start=(True, False), states=[self.state()], keep_steam=True)
        self.assertEqual(m['_launch_steam_detached'].call_count, 0)  # --keep-steam implies accessible
        self.assertIn('left running in accessible mode', self.report_path.read_text())

    def test_normal_final_failures_are_exit_1(self):
        code, _, err, m = self.run_check(final='normal', normal='login')
        self.assertEqual(code, 1)
        self.assertIn('sign-in screen', self.report_path.read_text())
        self.assertEqual(m['_launch_steam_detached'].call_count, 1)  # no loop of restarts
        code, _, err, m = self.run_check(final='normal', close=(True, False))
        self.assertEqual(code, 1)
        m['_launch_steam_detached'].assert_not_called()

    def test_settle_waits_for_the_cdp_order(self):
        states = [self.state(), self.state(source='tree (visible rows only)'), self.state()]
        code, _, err, m = self.run_check(states=states, settle=60)
        self.assertEqual(m['gui_read_state'].call_count, 3)
        self.assertEqual(code, 0, err)
        code, _, _, m = self.run_check(states=[self.state(), self.state(source='tree (visible rows only)')], settle=0)
        self.assertEqual(m['gui_read_state'].call_count, 2)  # no waiting at 0

    def test_normal_relaunch_has_no_accessibility_flag_and_accessible_one_has(self):
        log = self.work / 'steam.log'
        self.install_tool('steam', '#!/bin/sh\necho "$ACCESSIBILITY_ENABLED|$@" >> "%s"\n' % log)
        self.install_tool('setsid', '#!/bin/sh\nexec "$@"\n')
        os.environ['ACCESSIBILITY_ENABLED'] = '1'  # a stale value must not leak into the normal launch
        with mock.patch.object(luna, 'gui_steam_state', return_value=(False, False)):
            ok, _ = luna.gui_launch_accessible()
        self.assertTrue(ok)
        luna._launch_steam_detached()
        for _ in range(40):
            if log.exists() and len(log.read_text().splitlines()) >= 2:
                break
            time.sleep(0.1)
        accessible, normal = sorted(log.read_text().splitlines(), key=lambda l: l.startswith('|'))
        self.assertTrue(accessible.startswith('1|') and luna.GUI_ACCESS_ARGS[0] in accessible, accessible)
        self.assertNotIn(luna.GUI_ACCESS_ARGS[0], normal)

    def test_read_order_prefers_cdp_and_falls_back_to_the_tree(self):
        with mock.patch.object(luna, 'live_library_order', return_value={'names': ['A', 'B'], 'rows': 9, 'sort': {'x': 1}}):
            order = luna.gui_read_order()
        self.assertEqual((order['source'], order['total'], order['sort']), ('cdp', 9, {'x': 1}))
        with mock.patch.object(luna, 'live_library_order', side_effect=RuntimeError('no devtools')), \
                mock.patch.object(luna, 'gui_snapshot', return_value=(fixture_tree('main-library.json'), None)):
            order = luna.gui_read_order(open_library=False)
        self.assertTrue(order['source'].startswith('tree'), order)
        self.assertTrue(order['names'])
        with mock.patch.object(luna, 'live_library_order', side_effect=RuntimeError('no devtools')), \
                mock.patch.object(luna, 'gui_snapshot', return_value=(None, 'no window')):
            self.assertEqual(luna.gui_read_order(open_library=False)['source'], 'unavailable')

    def test_running_games_and_downloads_detection(self):
        proc = self.work / 'proc'
        for pid, comm, cmd in (('11', 'reaper', 'reaper\0SteamLaunch\0AppId=440\0--'), ('12', 'bash', 'bash')):
            (proc / pid).mkdir(parents=True)
            (proc / pid / 'comm').write_text(comm + '\n')
            (proc / pid / 'cmdline').write_bytes(cmd.encode())
        self.assertEqual(luna.gui_running_games(str(proc)), [{'pid': 11, 'appid': '440'}])
        busy = {'role': 'window', 'children': [{'role': 'statictext', 'name': 'Downloads - 3 of 4 Items Complete'}]}
        idle = {'role': 'window', 'children': [{'role': 'statictext', 'name': 'Downloads - 4 of 4 Items Complete'}]}
        with mock.patch.object(luna, 'steam_home', return_value=self.work / 'steam'):
            self.assertIn('3 of 4', luna.gui_active_download(busy))
            self.assertEqual(luna.gui_active_download(idle), '')
            (self.work / 'steam/steamapps/downloading/440').mkdir(parents=True)
            self.assertIn('downloading', luna.gui_active_download(idle))

    def test_cli_wiring(self):
        with mock.patch.object(luna, 'gui_restart_check', return_value=3) as check:
            code, _, _ = self.run_cli('gui', 'restart-check', '--force', '--final', 'normal', '--settle', '5')
        self.assertEqual(code, 3)
        options = check.call_args[0][0]
        self.assertTrue(options.force)
        self.assertEqual((options.final, options.settle), ('normal', 5))


class LoginJudgement(GuiFixture):
    """A "Sign in to Steam" window is also the startup window: only persistence makes it `login`."""
    NO = ('none', '')

    def test_judge_branches(self):
        j = luna.gui_judge_screen
        self.assertEqual(j('main', ['Steam'], 5, 120, self.NO)[0], 'main')
        self.assertEqual(j('dialog', [], 5, 120, self.NO)[0], 'dialog')
        self.assertEqual(j('login', [], 40, 120, ('logged-on', 'x'))[0], 'starting')
        self.assertEqual(j('login', [], 400, 120, ('logging-on', 'x'))[0], 'starting')
        self.assertEqual(j('login', [], None, 120, self.NO)[0], 'starting')
        self.assertEqual(j('login', [], 30, 120, self.NO)[0], 'starting')
        screen, signal = j('login', [], 130, 120, self.NO)
        self.assertEqual(screen, 'login')
        self.assertIn('persisted', signal)
        self.assertEqual(j('unknown', [], 5, 120, self.NO)[0], 'unknown')

    def test_logon_evidence_last_event_wins(self):
        stamp = lambda t, text: '[2026-11-15 14:%s] %s' % (t, text)
        since = time.mktime((2026, 11, 15, 14, 0, 0, 0, 0, -1))
        lines = [stamp('01:00', 'LogOn() called'), stamp('01:30', "RecvMsgClientLogOnResponse() : 'OK'")]
        self.assertEqual(luna.gui_logon_evidence(since, lines)[0], 'logged-on')
        self.assertEqual(luna.gui_logon_evidence(since, lines[:1])[0], 'logging-on')
        self.assertEqual(luna.gui_logon_evidence(since, lines + [stamp('02:00', 'LogOff()')])[0], 'none')
        self.assertEqual(luna.gui_logon_evidence(since + 3600, lines)[0], 'none')
        self.assertEqual(luna.gui_logon_evidence(since, [])[0], 'unknown')

    def test_exit_hint_reads_the_logs(self):
        with mock.patch.object(luna, 'gui_log_tail', return_value=['[2026-11-15 14:41:49] User.StartShutdown took 3ms']):
            self.assertIn('clean shutdown', luna.gui_exit_hint(0))
        with mock.patch.object(luna, 'gui_log_tail', return_value=[]):
            self.assertIn('crash or a kill', luna.gui_exit_hint(0))

    def wait(self, titles, logon=('none', ''), alive=True, age0=0.0, timeout=300, grace=120):
        """Poll with a window-title script: one entry per poll, the last one repeats."""
        clock, polls, script = [0.0], [0], list(titles)

        def titles_now():
            item = script[min(polls[0], len(script) - 1)]
            polls[0] += 1
            return [item]

        def sleep(seconds):
            clock[0] += seconds
        with mock.patch.object(luna, 'gui_snapshot', return_value=(None, None)), \
                mock.patch.object(luna, 'gui_windows', return_value=[]), \
                mock.patch.object(luna, 'gui_titles', side_effect=lambda _w: titles_now()), \
                mock.patch.object(luna, 'gui_steam_state', return_value=(alive, alive)), \
                mock.patch.object(luna, 'gui_exit_hint', return_value='hint'):
            return luna.gui_wait_for_screen(timeout, sleep, lambda: clock[0], grace=grace, age0=age0,
                                            logon=lambda _since: logon)

    def test_startup_window_then_main_is_main(self):
        screen, _, signal = self.wait(['Sign in to Steam'] * 6 + ['Steam'])
        self.assertEqual(screen, 'main')

    def test_real_form_persisting_past_grace_is_login(self):
        screen, _, signal = self.wait(['Sign in to Steam'])
        self.assertEqual(screen, 'login')
        self.assertIn('no log-on attempt', signal)

    def test_signin_window_with_logon_in_progress_stays_starting(self):
        screen, _, signal = self.wait(['Sign in to Steam'], logon=('logging-on', 'LogOn() called'), timeout=60)
        self.assertEqual(screen, 'starting')
        self.assertIn('timed out', signal)

    def test_steam_exiting_during_startup_is_exited(self):
        screen, _, signal = self.wait(['Sign in to Steam'], alive=False)
        self.assertEqual(screen, 'exited')
        self.assertIn('hint', signal)

    def test_normal_window_waits_then_reports_main(self):
        clock = [0.0]
        seq = iter([['Sign in to Steam']] * 3 + [['Steam']] * 5)
        with mock.patch.object(luna, 'gui_windows', return_value=[]), \
                mock.patch.object(luna, 'gui_titles', side_effect=lambda _w: next(seq)), \
                mock.patch.object(luna, 'gui_steam_state', return_value=(True, True)):
            state, signal = luna.gui_wait_normal_window(300, lambda s: clock.__setitem__(0, clock[0] + s),
                                                        lambda: clock[0], logon=lambda _s: self.NO)
        self.assertEqual(state, 'main')

    def test_status_prints_starting_with_signal(self):
        out = io.StringIO()
        with mock.patch.object(luna, 'gui_enabled', return_value=True), \
                mock.patch.object(luna, 'gui_steam_state', return_value=(True, False)), \
                mock.patch.object(luna, 'gui_have_session', return_value=True), \
                mock.patch.object(luna, 'gui_windows', return_value=[]), \
                mock.patch.object(luna, 'gui_titles', return_value=['Sign in to Steam']), \
                mock.patch.object(luna, 'gui_process_started', return_value=time.time() - 10), \
                mock.patch.object(luna, 'gui_logon_evidence', return_value=self.NO), \
                contextlib.redirect_stdout(out):
            luna.gui_status(argparse.Namespace(json=False))
        text = out.getvalue()
        self.assertIn('screen     : starting', text)
        self.assertIn('grace 120', text)

    def test_login_grace_flag_is_wired(self):
        args = luna.build_parser().parse_args(['gui', 'restart-check', '--login-grace', '45'])
        self.assertEqual(args.login_grace, 45)


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

    def test_reports_a_real_secondary_window(self):
        self.install_agent()
        self.install_tool('niri', '#!/bin/sh\ncat "%s"\n' % (FIXTURES / 'niri-windows-secondary.json'))
        self.cfg['snapshots'] = [str(FIXTURES / 'secondary-window-settings.json')]
        self.write_cfg()
        with mock.patch.object(luna, 'gui_steam_state', return_value=(True, True)):
            text = luna.gui_describe_blockers()
        self.assertIn('open Steam windows: "Steam", "Steam Settings"', text)
        self.assertIn('window "Steam Settings" is open (a normal window, not a modal', text)

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
        started = time.mktime((2026, 11, 14, 10, 0, 0, 0, 0, -1))
        with mock.patch.object(luna, 'process_running', return_value=True), \
                mock.patch.object(luna, 'live_sidecar', return_value={'pid': 4242, 'started': started}):
            self.assertEqual(luna.gui_lumen_survivor(), ('4242', '2026-11-14 10:00:00'))
            self.assertIn('pid 4242, started 2026-11-14 10:00:00', luna.gui_lumen_note())
        with mock.patch.object(luna, 'process_running', return_value=True), \
                mock.patch.object(luna, 'live_sidecar', return_value=None):
            self.assertEqual(luna.gui_lumen_survivor(), ('?', '?'))


if __name__ == '__main__':
    unittest.main()
