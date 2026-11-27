#!/usr/bin/env python3
"""Tests for `luna selftest` and `luna doctor --report`.

Everything runs against a fake CDP endpoint this file starts (a real socket
speaking /json + one websocket), so the tests never touch the developer's
running Steam, never send a write RPC and need no browser.
"""
import json
import argparse
import contextlib
import io
import os
import re
import socket
import socketserver
import subprocess
import struct
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
SCRIPTS = HERE / 'scripts'
sys.path.insert(0, str(SCRIPTS))

import luna_selftest  # noqa: E402
import luna  # noqa: E402


class EmbedProbeTest(unittest.TestCase):
    def test_ancestor_hit_is_not_clickable_but_row_descendant_is(self):
        fixture = r"""
const rect={left:0,top:0,right:100,bottom:40,width:100,height:40};
const child={tagName:'SPAN'};
const row={tagName:'BUTTON',className:'sls-luna-tab',textContent:'Lumen',
  getClientRects:()=>[rect],getBoundingClientRect:()=>rect,
  classList:{contains:()=>true},contains:x=>x===child};
const side={getBoundingClientRect:()=>rect,querySelectorAll:()=>[row]};
const luna={tagName:'DIV',querySelector:s=>s==='.sls-luna-side'?side:null,
  contains:()=>false,getBoundingClientRect:()=>rect};
const body={tagName:'BODY',contains:()=>true};
global.document={getElementById:id=>id==='sls-luna-overlay'?luna:null,
  elementFromPoint:()=>process.argv[1]==='ancestor'?body:child};
global.getComputedStyle=()=>({zIndex:'0',display:'block'});
console.log(JSON.stringify(PROBE));
""".replace('PROBE', luna_selftest.EMBED_JS)
        for target, expected in [('ancestor', False), ('child', True)]:
            result = subprocess.run(['node', '-e', fixture, target], check=True,
                                    capture_output=True, text=True)
            self.assertEqual(json.loads(result.stdout)['sidebarClickable'], expected)

TITLES = {'playtime': 'Playtime', 'presence': 'Presence', 'compressor': 'Compressor',
          'hltb': 'HowLongToBeat', 'steamgrid': 'SteamGridDB artwork',
          'graphics': 'Graphics (read-only)', 'health': 'Health (local diagnostics)',
          'updates': 'Updates', 'lumen': 'Lumen', 'settings': 'Settings',
          'about': 'About SLS Luna'}
TABS = [{'id': ident, 'label': label}
        for ident, label in (('playtime', 'Playtime'), ('presence', 'Presence'),
                             ('updates', 'Updates'), ('settings', 'Settings'),
                             ('about', 'About'))]
TABS_WITH_LUMEN = TABS + [{'id': 'lumen', 'label': 'Lumen'}]


def _recv(sock, count):
    data = b''
    while len(data) < count:
        chunk = sock.recv(count - len(data))
        if not chunk:
            raise ConnectionError('closed')
        data += chunk
    return data


def _read_frame(sock):
    head = _recv(sock, 2)
    opcode = head[0] & 0x0F
    masked = head[1] & 0x80
    length = head[1] & 0x7F
    if length == 126:
        length = struct.unpack('!H', _recv(sock, 2))[0]
    elif length == 127:
        length = struct.unpack('!Q', _recv(sock, 8))[0]
    mask = _recv(sock, 4) if masked else None
    payload = _recv(sock, length) if length else b''
    if mask:
        payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    return opcode, payload


def _send_frame(sock, opcode, payload):
    header = bytearray([0x80 | opcode])
    size = len(payload)
    if size < 126:
        header.append(size)
    elif size < 65536:
        header.append(126)
        header += struct.pack('!H', size)
    else:
        header.append(127)
        header += struct.pack('!Q', size)
    sock.sendall(bytes(header) + payload)


class FakeSteam:
    """Canned page answers, plus every call the selftest makes."""

    def __init__(self, tabs=None, select=None, exception_on=None, close=None, probe=None,
                 embed=None, rpc_ms=None):
        self.tabs = tabs or TABS
        self.select = select or {}
        self.exception_on = exception_on
        self.close_answer = close or {'overlays': 0, 'launchers': 1,
                                      'browserVisible': True, 'hiddenView': False}
        # The owner's live shape: sidebar on the left, Lumen to its right with
        # pointer-events: none, so elementFromPoint still returns the row.
        self.embed_answer = {
            'embedded': True, 'inside': True, 'lumenVisible': True, 'lumenAbove': True,
            'sidebarClickable': True, 'rect': [1019, 464, 700, 555], 'rowHeight': 40.0,
            'sidebarRect': [819, 399, 200, 620], 'sidebarRows': 9, 'sidebarCandidates': 9,
            'sidebarRow': {'label': 'Lumen', 'active': True}, 'probePoint': [919, 420],
            'hit': 'div.sls-luna-tab.active', 'lumenPointerEvents': 'none', 'lumenZ': '100000'}
        self.embed_answer.update(embed or {})
        self.probe_override = probe or {}
        # name -> [ms, ...]: each call takes the next value, the last one repeats.
        self.rpc_ms = dict(rpc_ms or {})
        self.calls = []
        self.lock = threading.Lock()

    def next_ms(self, name):
        values = self.rpc_ms.get(name)
        if not values:
            return 4
        return values.pop(0) if len(values) > 1 else values[0]

    def record(self, method, params):
        with self.lock:
            self.calls.append((method, params))

    def expressions(self, marker):
        hits = []
        for method, params in self.calls:
            expression = str((params or {}).get('expression') or '')
            if method == 'Runtime.evaluate' and marker in expression:
                hits.append(expression)
        return hits

    def rpc_names(self):
        names = []
        for method, params in self.calls:
            if method != 'Runtime.evaluate':
                continue
            expression = str((params or {}).get('expression') or '')
            hit = re.search(r'const name = ("[^"]+");', expression)
            if hit:
                names.append(json.loads(hit.group(1)))
        return names

    def answer(self, expression):
        probe = {'build': 'bv-5', 'hook': True, 'launchers': 1, 'overlays': 0, 'open': False,
                 'tabs': self.tabs, 'rows': len(self.tabs), 'problems': [],
                 'embedding': False, 'hiddenView': False, 'route': '/library/home',
                 'tab': 'playtime'}
        probe.update(self.probe_override)
        if 'luna-selftest:probe' in expression:
            return probe, None
        if 'luna-selftest:open' in expression:
            return {'ok': True, 'ms': 140, 'overlays': 1, 'visible': True}, None
        if 'luna-selftest:select' in expression:
            ident = json.loads(re.search(r'const id = ("[^"]+");', expression).group(1))
            notification = None
            if ident == self.exception_on:
                notification = {
                    'method': 'Runtime.exceptionThrown',
                    'params': {'exceptionDetails': {
                        'text': 'Uncaught',
                        'exception': {'description': 'TypeError: sls-luna-panel broke'}}},
                }
            if ident in self.select:
                return self.select[ident], notification
            return {
                'ok': True, 'id': ident, 'panes': 1, 'visible': 1,
                'text': ['%s content for the test' % TITLES.get(ident, ident)],
                'emptyState': True, 'heading': TITLES.get(ident, ident),
                'elapsedMs': 12.0,
                'heights': [{'label': tab['label'], 'height': 40.0} for tab in self.tabs],
                'tab': ident,
            }, notification
        if 'luna-selftest:rpc' in expression:
            name = json.loads(re.search(r'const name = ("[^"]+");', expression).group(1))
            args = json.loads(re.search(r'const args = (.+);', expression).group(1))
            reply = {'success': True, 'ok': True}
            if name == 'SlsHltbGet':
                reply = ({'success': True, 'unchanged': True, 'rev': args['rev']}
                         if args.get('rev')
                         else {'success': True, 'rev': 'rev-1',
                               'games': {'1': {'minutes': 90}}, 'count': 1})
            return {'ok': True, 'name': name, 'ms': self.next_ms(name),
                    'reply': reply, 'keys': len(reply)}, None
        if 'luna-selftest:close' in expression:
            return dict(self.close_answer), None
        if 'luna-selftest:embed' in expression:
            return dict(self.embed_answer), None
        if 'luna-selftest:route' in expression:
            return {'route': '/store/'}, None
        return None, None


class FakeHandler(socketserver.BaseRequestHandler):
    def handle(self):
        sock = self.request
        sock.settimeout(20)
        buffer = b''
        while b'\r\n\r\n' not in buffer:
            chunk = sock.recv(65536)
            if not chunk:
                return
            buffer += chunk
        head, _, rest = buffer.partition(b'\r\n\r\n')
        lines = head.decode('latin-1', 'replace').split('\r\n')
        request = lines[0].split(' ')
        path = request[1] if len(request) > 1 else '/'
        port = self.server.server_address[1]
        if path.startswith('/json'):
            targets = [
                {'id': 'PAGE1', 'title': 'Steam', 'type': 'page',
                 'url': 'https://steamloopback.host/',
                 'webSocketDebuggerUrl': 'ws://127.0.0.1:%d/devtools/page/PAGE1' % port},
                {'id': 'SJS', 'title': 'SharedJSContext', 'type': 'other',
                 'url': '',
                 'webSocketDebuggerUrl': 'ws://127.0.0.1:%d/devtools/page/SJS' % port},
            ]
            body = json.dumps(targets).encode()
            sock.sendall(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n'
                         b'Content-Length: ' + str(len(body)).encode() + b'\r\n\r\n' + body)
            return
        if 'Sec-WebSocket-Key' in head.decode('latin-1', 'replace'):
            import base64
            import hashlib
            key = ''
            for line in lines:
                if line.lower().startswith('sec-websocket-key:'):
                    key = line.split(':', 1)[1].strip()
            accept = base64.b64encode(hashlib.sha1(
                (key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
            sock.sendall(('HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n'
                          'Connection: Upgrade\r\nSec-WebSocket-Accept: %s\r\n\r\n'
                          % accept).encode())
            steam = self.server.steam
            while True:
                try:
                    opcode, payload = _read_frame(sock)
                except (ConnectionError, OSError):
                    return
                if opcode == 0x8:
                    return
                if opcode == 0x9:
                    _send_frame(sock, 0xA, payload)
                    continue
                if opcode != 0x1:
                    continue
                try:
                    message = json.loads(payload.decode())
                except ValueError:
                    continue
                method = message.get('method')
                params = message.get('params') or {}
                if method == 'Runtime.evaluate':
                    steam.record(method, params)
                    answer, notification = steam.answer(str(params.get('expression') or ''))
                    if notification:
                        _send_frame(sock, 0x1, json.dumps(notification).encode())
                    _send_frame(sock, 0x1, json.dumps({
                        'id': message.get('id'),
                        'result': {'result': {'type': 'object', 'value': answer}},
                    }).encode())
                else:
                    steam.record(method, params)
                    _send_frame(sock, 0x1, json.dumps({
                        'id': message.get('id'), 'result': {},
                    }).encode())
            return
        sock.sendall(b'HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n')


class FakeServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, steam):
        super().__init__(('127.0.0.1', 0), FakeHandler)
        self.steam = steam
        self.thread = None

    def start(self):
        self.thread = threading.Thread(target=self.serve_forever, daemon=True)
        self.thread.start()
        return self.server_address[1]

    def stop(self):
        self.shutdown()
        self.server_close()


class SelftestBase(unittest.TestCase):
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp(prefix='luna-selftest-'))
        self.addCleanup(lambda: __import__('shutil').rmtree(self.temp, True))
        self.old_home = os.environ.get('HOME')
        self.old_port = os.environ.get('LUNA_CEF_PORT')
        os.environ['HOME'] = str(self.temp)
        os.environ.pop('LUNA_CEF_PORT', None)
        self.addCleanup(self._restore)

    def _restore(self):
        if self.old_home is None:
            os.environ.pop('HOME', None)
        else:
            os.environ['HOME'] = self.old_home
        if self.old_port is None:
            os.environ.pop('LUNA_CEF_PORT', None)
        else:
            os.environ['LUNA_CEF_PORT'] = self.old_port

    def serve(self, steam):
        server = FakeServer(steam)
        port = server.start()
        self.addCleanup(server.stop)
        os.environ['LUNA_CEF_PORT'] = str(port)
        return port

    def run_selftest(self, **kwargs):
        lines = []
        kwargs.setdefault('out', lines.append)
        kwargs.setdefault('backend_names', ['SlsLunaInfo', 'SlsLunaPrefsSet', 'SlsSgdbApply'])
        code, report = luna_selftest.run_selftest(**kwargs)
        return code, report, '\n'.join(lines)

    def fail_text(self, report):
        return ' '.join(line['name'] + ': ' + line['detail']
                        for line in report.lines if line['status'] == 'FAIL')


class SelftestTests(SelftestBase):
    def test_all_pass_exits_zero_and_never_writes(self):
        steam = FakeSteam()
        self.serve(steam)
        code, report, text = self.run_selftest()
        self.assertEqual(code, 0, text)
        self.assertTrue(report.ok, text)
        names = steam.rpc_names()
        self.assertTrue(names, 'no RPC was exercised')
        for name in names:
            self.assertIn(name, luna_selftest.READONLY_NAMES)
        for method, params in steam.calls:
            if method != 'Runtime.evaluate':
                continue
            expression = str((params or {}).get('expression') or '')
            if 'luna-selftest:rpc' not in expression:
                continue
            self.assertNotIn('luna-selftest:close', expression)
            for word in ('install', 'queue', 'cancel', 'repair', 'restore',
                         'channels-set', 'terminal'):
                self.assertNotIn('"%s"' % word, expression, 'a write action was sent')
        self.assertIn('PASS', text)
        self.assertIn('close menu', text)

    def test_stacked_panes_fail_with_the_reason(self):
        steam = FakeSteam(select={'updates': {
            'ok': True, 'id': 'updates', 'panes': 3, 'visible': 2,
            'text': ['old', 'current'], 'emptyState': True, 'heading': 'Updates',
            'elapsedMs': 9.0, 'heights': [{'label': 'Updates', 'height': 40.0}],
            'tab': 'updates'}})
        self.serve(steam)
        code, report, text = self.run_selftest()
        self.assertEqual(code, 1, text)
        self.assertIn('tab updates', self.fail_text(report))
        self.assertIn('2 of 3 panes are on screen', self.fail_text(report))

    def test_zero_height_sidebar_row_fails(self):
        steam = FakeSteam(select={'settings': {
            'ok': True, 'id': 'settings', 'panes': 1, 'visible': 1,
            'text': ['Settings'], 'emptyState': True, 'heading': 'Settings',
            'elapsedMs': 9.0,
            'heights': [{'label': 'Playtime', 'height': 0.0},
                        {'label': 'Settings', 'height': 40.0}], 'tab': 'settings'}})
        self.serve(steam)
        code, report, _text = self.run_selftest()
        self.assertEqual(code, 1)
        self.assertIn('sidebar row', self.fail_text(report))
        self.assertIn('needs >= 24', self.fail_text(report))

    def test_exception_during_switch_fails(self):
        steam = FakeSteam(exception_on='presence')
        self.serve(steam)
        code, report, _text = self.run_selftest()
        self.assertEqual(code, 1)
        failed = self.fail_text(report)
        self.assertIn('tab presence', failed)
        self.assertIn('exception', failed)
        self.assertIn('sls-luna-panel broke', failed)

    def test_unreachable_port_exits_two_with_a_usable_message(self):
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
        os.environ['LUNA_CEF_PORT'] = str(port)  # nothing listens any more
        code, _report, text = self.run_selftest()
        self.assertEqual(code, 2)
        self.assertIn('start Steam, then run luna selftest', text)

    def test_empty_page_fails_the_content_check(self):
        steam = FakeSteam(select={'about': {
            'ok': True, 'id': 'about', 'panes': 1, 'visible': 1, 'text': [],
            'emptyState': False, 'heading': 'About SLS Luna', 'elapsedMs': 8.0,
            'heights': [{'label': 'About', 'height': 40.0}], 'tab': 'about'}})
        self.serve(steam)
        code, report, _text = self.run_selftest()
        self.assertEqual(code, 1)
        self.assertIn('the page is empty', self.fail_text(report))

    def test_json_output_is_one_object(self):
        steam = FakeSteam()
        self.serve(steam)
        lines = []
        code, _report = luna_selftest.run_selftest(json_output=True, out=lines.append)
        self.assertEqual(code, 0)
        self.assertEqual(len(lines), 1)
        payload = json.loads(lines[0])
        self.assertTrue(payload['ok'])
        self.assertTrue(payload['checks'])

    def test_keep_open_skips_the_close_checks(self):
        steam = FakeSteam()
        self.serve(steam)
        code, report, text = self.run_selftest(keep_open=True)
        self.assertEqual(code, 0, text)
        self.assertIn('--keep-open', text)

    def test_overlay_left_open_is_a_note_and_gets_closed(self):
        steam = FakeSteam(probe={'overlays': 1, 'open': True})
        self.serve(steam)
        code, report, text = self.run_selftest()
        self.assertEqual(code, 0, text)
        self.assertIn('was already open', text)
        self.assertIn('INFO  overlays', text)
        self.assertIn('closed the overlay that was already open', text)
        # One close before the launcher click, one for the close check at the end.
        self.assertEqual(len(steam.expressions('luna-selftest:close')), 2)

    def test_two_open_overlays_still_fail(self):
        steam = FakeSteam(probe={'overlays': 2, 'open': True})
        self.serve(steam)
        code, report, _text = self.run_selftest()
        self.assertEqual(code, 1)
        self.assertIn('2 Luna overlays are already open', self.fail_text(report))

    def test_embedded_lumen_tab_passes_and_the_embedding_check_runs(self):
        steam = FakeSteam(tabs=TABS_WITH_LUMEN, probe={'embedding': True},
                          select={'lumen': _embedded_lumen()})
        self.serve(steam)
        code, report, text = self.run_selftest(host='lumen')
        self.assertEqual(code, 0, text)
        self.assertIn('embedded Lumen', text)
        self.assertIn('PASS  Lumen embedding', text)
        self.assertTrue(steam.expressions('luna-selftest:embed'))

    def test_embedded_lumen_tab_fails_with_the_reason(self):
        cases = [
            ({'lumenPanels': 0}, '0 of 9 Lumen tab panels'),
            ({'lumenPanels': 2}, '2 of 9 Lumen tab panels'),
            ({'lumenInside': False}, 'not inside Luna'),
            ({'lumenVisible': False}, 'is not visible'),
            ({'visible': 1}, '1 Luna pane(s) are on screen'),
        ]
        for patch, expected in cases:
            with self.subTest(patch=patch):
                select = _embedded_lumen(**patch)
                steam = FakeSteam(tabs=TABS_WITH_LUMEN, probe={'embedding': True},
                                  select={'lumen': select})
                self.serve(steam)
                code, report, _text = self.run_selftest(host='lumen')
                self.assertEqual(code, 1)
                failed = self.fail_text(report)
                self.assertIn('tab lumen', failed)
                self.assertIn(expected, failed)

    def test_embedding_false_keeps_the_normal_pane_rule(self):
        select = {'lumen': {'ok': True, 'id': 'lumen', 'panes': 1, 'visible': 1,
                            'text': ['Lumen content'], 'emptyState': False,
                            'heading': 'Lumen', 'elapsedMs': 9.0,
                            'heights': [{'label': 'Lumen', 'height': 40.0}], 'tab': 'lumen',
                            'embedding': False, 'lumenPresent': True}}
        steam = FakeSteam(tabs=TABS_WITH_LUMEN, select={'lumen': dict(select['lumen'])})
        self.serve(steam)
        code, _report, text = self.run_selftest(host='lumen')
        self.assertEqual(code, 0, text)
        self.assertIn('PASS  tab lumen', text)
        self.assertIn('state().embedding is false', text)

        empty = dict(select['lumen'])
        empty.update({'panes': 1, 'visible': 0, 'text': [], 'emptyState': False})
        steam = FakeSteam(tabs=TABS_WITH_LUMEN, select={'lumen': empty})
        self.serve(steam)
        code, report, _text = self.run_selftest(host='lumen')
        self.assertEqual(code, 1)
        self.assertIn('0 of 1 panes are on screen', self.fail_text(report))

    def test_lumen_embedded_check_selects_the_category_again(self):
        # Live shape: state().embedding is false while another tab is selected,
        # and true only inside the Lumen category.
        steam = FakeSteam(tabs=TABS_WITH_LUMEN, probe={'embedding': False},
                          select={'lumen': _embedded_lumen()})
        self.serve(steam)
        code, report, text = self.run_selftest(host='lumen')
        self.assertEqual(code, 0, text)
        self.assertIn('PASS  tab lumen', text)
        self.assertIn('PASS  Lumen embedding', text)
        self.assertNotIn('SKIP  Lumen embedding', text)
        selects = [item for item in steam.expressions('luna-selftest:select')
                   if '"lumen"' in item]
        self.assertEqual(len(selects), 2, 'the walk and the embedding check each select Lumen')

    def test_lumen_tab_without_embedding_skips_the_embedding_check(self):
        pane = {'ok': True, 'id': 'lumen', 'panes': 1, 'visible': 1, 'text': ['Lumen'],
                'emptyState': False, 'heading': 'Lumen', 'elapsedMs': 9.0,
                'heights': [{'label': 'Lumen', 'height': 40.0}], 'tab': 'lumen',
                'embedding': False, 'lumenPresent': True}
        steam = FakeSteam(tabs=TABS_WITH_LUMEN, select={'lumen': pane})
        self.serve(steam)
        code, _report, text = self.run_selftest(host='lumen')
        self.assertEqual(code, 0, text)
        self.assertIn('SKIP  Lumen embedding: state().embedding is false', text)
        self.assertIn('drawn as one of Luna', text)

    def test_embedded_lumen_to_the_right_with_pointer_events_none_passes(self):
        steam = FakeSteam(tabs=TABS_WITH_LUMEN, probe={'embedding': True},
                          select={'lumen': _embedded_lumen()})
        self.serve(steam)
        code, report, text = self.run_selftest(host='lumen')
        self.assertEqual(code, 0, text)
        self.assertIn('PASS  Lumen embedding', text)
        self.assertIn('sidebar clickable at (919,420) (pointer-events: none)', text)

    def test_lumen_covering_the_sidebar_fails_naming_the_element(self):
        steam = FakeSteam(tabs=TABS_WITH_LUMEN, probe={'embedding': True},
                          select={'lumen': _embedded_lumen()},
                          embed={'sidebarClickable': False,
                                 'hit': 'div#lumen-settings-overlay.lumen-shell.open',
                                 'lumenPointerEvents': 'auto'})
        self.serve(steam)
        code, report, _text = self.run_selftest(host='lumen')
        self.assertEqual(code, 1)
        failed = self.fail_text(report)
        self.assertIn('Lumen embedding', failed)
        self.assertIn('not hittable at (919,420)', failed)
        self.assertIn('div#lumen-settings-overlay.lumen-shell.open', failed)
        self.assertIn('sidebar rect [819,399,200,620]', failed)
        self.assertIn('Lumen rect [1019,464,700,555]', failed)
        self.assertIn('pointer-events auto', failed)
        self.assertIn('z-index 100000', failed)

    def test_sidebar_rows_out_of_view_are_skipped_not_failed(self):
        # Every row is zero-height or scrolled away: nothing can be hit, and that
        # is a skip with the reason, never a failure.
        steam = FakeSteam(tabs=TABS_WITH_LUMEN, probe={'embedding': True},
                          select={'lumen': _embedded_lumen()},
                          embed={'sidebarClickable': False, 'sidebarCandidates': 0,
                                 'sidebarRows': 3, 'probePoint': None, 'hit': None,
                                 'sidebarRow': None})
        self.serve(steam)
        code, report, text = self.run_selftest(host='lumen')
        self.assertEqual(code, 0, text)
        self.assertIn('SKIP  Lumen embedding', text)
        self.assertIn('all zero-height or scrolled out of view', text)
        self.assertNotIn('FAIL  Lumen embedding', text)

    def test_zero_height_sidebar_rows_are_ignored(self):
        # Two rows skipped for height/scroll, one real candidate hit correctly.
        steam = FakeSteam(tabs=TABS_WITH_LUMEN, probe={'embedding': True},
                          select={'lumen': _embedded_lumen()},
                          embed={'sidebarRows': 3, 'sidebarCandidates': 1})
        self.serve(steam)
        code, report, text = self.run_selftest(host='lumen')
        self.assertEqual(code, 0, text)
        self.assertIn('PASS  Lumen embedding', text)

    def test_rpc_timing_waits_for_the_backend_queue(self):
        steam = FakeSteam(rpc_ms={'SlsLunaInfo': [2900, 5, 6]})
        self.serve(steam)
        code, _report, text = self.run_selftest()
        self.assertEqual(code, 0, text)
        self.assertIn('warm-up', text)
        self.assertIn('the backend was idle', text)

    def test_rpc_over_budget_gets_one_retry(self):
        steam = FakeSteam(rpc_ms={'SlsLunaInfo': [4, 2900, 5]})
        self.serve(steam)
        code, _report, text = self.run_selftest()
        self.assertEqual(code, 0, text)
        self.assertIn('5 ms on retry (first call 2900 ms)', text)

    def test_rpc_slow_twice_still_fails_with_both_numbers(self):
        steam = FakeSteam(rpc_ms={'SlsLunaInfo': [4, 2900, 2900]})
        self.serve(steam)
        code, report, _text = self.run_selftest()
        self.assertEqual(code, 1)
        failed = self.fail_text(report)
        self.assertIn('rpc SlsLunaInfo', failed)
        self.assertIn('answered in 2900 ms then 2900 ms on retry', failed)

    def test_hltb_rev_poll_reports_both_sizes(self):
        steam = FakeSteam()
        self.serve(steam)
        code, report, text = self.run_selftest()
        self.assertEqual(code, 0, text)
        line = [item for item in report.lines if item['name'] == 'rpc SlsHltbGet rev']
        self.assertEqual(len(line), 1, text)
        self.assertEqual(line[0]['status'], 'PASS', line[0]['detail'])
        self.assertIn('unchanged: true', line[0]['detail'])
        self.assertIn(' -> ', line[0]['detail'])
        rev_calls = [item for item in steam.expressions('luna-selftest:rpc')
                     if '"SlsHltbGet"' in item and '"rev"' in item]
        self.assertEqual(len(rev_calls), 1, 'the second call must send the rev it was given')


def _embedded_lumen(**patch):
    """The page answer for the embedded Lumen category, as the live panel gives it."""
    answer = {
        'ok': True, 'id': 'lumen', 'panes': 9, 'visible': 0, 'text': [], 'emptyState': False,
        'heading': 'Lumen', 'elapsedMs': 30.0,
        'heights': [{'label': tab['label'], 'height': 40.0} for tab in TABS_WITH_LUMEN],
        'tab': 'lumen', 'embedding': True, 'lumenPresent': True, 'lumenVisible': True,
        'lumenInside': True, 'lumenRect': [700, 555], 'lumenPanels': 1, 'lumenPanelCount': 9,
    }
    answer.update(patch)
    return answer


class DoctorTests(SelftestBase):
    def stub_luna(self):
        return types.SimpleNamespace(
            __file__=str(self.temp / 'luna.py'),
            ADDONS=('luna', 'sgdb'),
            addon_version=lambda name: {'luna': '0.14.18', 'sgdb': '1.2.3'}.get(name, '?'),
            host_label=lambda host=None: 'lumen',
            active_host=lambda: 'lumen',
            serving_host=lambda: 'lumen',
            host_state_path=lambda: self.temp / 'host.json',
            luna_state_dir=lambda: self.temp / 'state',
            luna_tools_dir=lambda: self.temp / 'tools',
            known_good_line=lambda: (True, 'Steam client: known-good ubuntu12 build 1792618285'),
            heal_steam_lock=lambda apply=False: {'pinned': True, 'notes': []},
        )

    def runner(self, missing_journal=True):
        calls = []

        def run(argv, timeout=20):
            calls.append(list(argv))
            if argv and argv[0] == 'journalctl':
                if missing_journal:
                    return 1, '', 'No journal files were found.'
                return 0, 'Nov 21 10:00 luna[1]: started\nNov 21 10:01 luna[1]: ok', ''
            if argv and argv[0] == 'systemctl':
                return 0, 'loaded\nactive\nenabled', ''
            if len(argv) > 1 and argv[1] == 'status':
                return 0, 'host       : lumen\nluna       : 0.14.18', ''
            if len(argv) > 1 and argv[1] == 'verify':
                return 0, 'manifest   : 12 file(s) verified', ''
            return 0, '', ''

        return run, calls

    def test_report_redacts_home_steamid_and_tokens(self):
        text = luna_selftest.redact(
            'path %s/.local/share/Luna/state/x\nSteamID 76561198012345678\n'
            'token: abc123secret\n"api_key": "deadbeef"\nname Luna Owner'
            % self.temp,
            home_dir=self.temp, extra=('Luna Owner',))
        self.assertNotIn(str(self.temp), text)
        self.assertIn('~/.local/share/Luna/state/x', text)
        self.assertNotIn('76561198012345678', text)
        self.assertIn('<steamid>', text)
        self.assertNotIn('abc123secret', text)
        self.assertNotIn('deadbeef', text)
        self.assertIn('<redacted>', text)
        self.assertNotIn('Luna Owner', text)

    def test_report_survives_missing_journals(self):
        run, _calls = self.runner(missing_journal=True)
        out = self.temp / 'report.txt'
        (self.temp / 'luna.py').write_text('# stub\n')
        path = luna_selftest.build_report(out=out, luna=self.stub_luna(), runner=run,
                                          home_dir=self.temp, luna_py=self.temp / 'luna.py')
        self.assertEqual(path, out)
        text = path.read_text()
        for section in ('== versions ==', '== host ==', '== Steam client ==',
                        '== systemd (user) ==', '== journals ==', '== Problems ring ==',
                        '== luna status ==', '== luna verify ==', '== last selftest =='):
            self.assertIn(section, text)
        self.assertIn('No journal files were found', text)
        self.assertIn('known-good ubuntu12 build 1792618285', text)

    def test_report_uses_the_default_name_and_redacts_the_file(self):
        run, _calls = self.runner(missing_journal=False)
        (self.temp / 'luna.py').write_text('# stub\n')
        steamid = '76561198012345678'
        stub = self.stub_luna()
        stub.known_good_line = lambda: (True, 'Steam client: build 1792618285 for %s' % steamid)
        luna_selftest.state_dir().mkdir(parents=True, exist_ok=True)
        (luna_selftest.state_dir() / 'selftest-last.json').write_text(json.dumps({
            'at': int(time.time()), 'ok': False, 'code': 1, 'build': 'bv-5',
            'counts': {'PASS': 1, 'FAIL': 1},
            'checks': [{'status': 'FAIL', 'name': 'tab updates', 'detail': 'the page is empty'}],
        }))
        path = luna_selftest.build_report(luna=stub, runner=run, home_dir=self.temp,
                                          luna_py=self.temp / 'luna.py')
        self.assertEqual(path.parent, self.temp)
        self.assertTrue(path.name.startswith('luna-report-'))
        text = path.read_text()
        self.assertIn('tab updates: the page is empty', text)
        self.assertNotIn(steamid, text)
        self.assertIn('<steamid>', text)


class LiveStatusTest(SelftestBase):
    def setUp(self):
        super().setUp()
        self.freshness = luna.sidecar_freshness
        self.boot = self.temp / 'boot.lua'
        self.boot.write_text(luna.MARKER)
        for name, value in [('active_host', 'lumen'), ('installed_addons', ('playtime', 'presence')),
                            ('bridge_status', ('ok', ['attached']))]:
            patcher = mock.patch.object(luna, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for name, value in [('boot_file', self.boot), ('sidecar_freshness', {'stale': False}),
                            ('read_json_file', {'pid': 42, 'format': 2, 'attachedAt': 1,
                                                'addons': ['playtime', 'presence']}),
                            ('pid_alive', True)]:
            patcher = mock.patch.object(luna, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(luna, '_selftest_module', return_value=luna_selftest)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_steam_closed(self):
        with mock.patch.object(luna, 'steam_running', return_value=False):
            result = luna.live_status(('playtime',))
        self.assertIn('Steam is closed', result['message'])
        self.assertIsNone(result['addons']['playtime']['answered'])
        self.assertTrue(result['bridge']['attached'])
        self.assertEqual(result['bridge']['addon_count'], 2)

    def test_missing_port(self):
        with mock.patch.object(luna, 'steam_running', return_value=True), \
             mock.patch.object(luna_selftest, 'cef_port', side_effect=luna_selftest.Unreachable('port missing')):
            result = luna.live_status(('playtime',))
        self.assertIn('port missing', result['message'])
        self.assertIsNone(result['addons']['playtime']['answered'])

    def test_backend_failure_and_allowlist(self):
        steam = FakeSteam()
        original = steam.answer
        def answer(expression):
            if 'const name = "SlsPresenceGet";' in expression:
                return {'ok': False, 'error': 'backend unavailable'}, None
            return original(expression)
        steam.answer = answer
        server = FakeServer(steam)
        port = server.start()
        self.addCleanup(server.stop)
        with mock.patch.object(luna, 'steam_running', return_value=True), \
             mock.patch.object(luna_selftest, 'cef_port', return_value=port):
            result = luna.live_status(('playtime', 'presence'))
        self.assertTrue(result['addons']['playtime']['answered'])
        self.assertFalse(result['addons']['presence']['answered'])
        self.assertEqual(result['addons']['presence']['error'], 'backend unavailable')
        self.assertEqual(set(steam.rpc_names()), {'SlsPlaytimeGet', 'SlsPresenceGet'})
        self.assertTrue(all(luna_selftest._is_readonly_call(name, {})[0] for name in steam.rpc_names()))

    def test_stale_sidecar(self):
        with mock.patch.object(luna, 'process_pids', return_value=[42]), \
             mock.patch.object(luna, '_proc_start', return_value=1), \
             mock.patch.object(luna, 'plan', return_value=[]):
            for host in ('lumen', 'luna'):
                result = self.freshness(host, ('playtime',))
                self.assertTrue(result['stale'])
                self.assertIn('restart needed', result['message'])

    def test_a_replaced_host_executable_alone_makes_the_sidecar_stale(self):
        # Only the binary changed: the payload files keep their old mtimes.
        os.utime(self.boot, (1, 1))
        executable = self.temp / 'host-exe'
        executable.write_text('new build')
        lumen = self.temp / 'lumen-root'
        lumen.mkdir()
        (lumen / 'lumen').write_text('new build')
        with mock.patch.object(luna, 'process_pids', return_value=[42]), \
             mock.patch.object(luna, '_proc_start', return_value=1), \
             mock.patch.object(luna, 'plan', return_value=[]), \
             mock.patch.object(luna, 'host_binary', return_value=executable), \
             mock.patch.object(luna, 'lumen_root', return_value=lumen):
            for host in ('lumen', 'luna'):
                self.assertTrue(self.freshness(host, ('playtime',))['stale'], host)

    def test_a_non_object_heartbeat_does_not_crash(self):
        with mock.patch.object(luna, 'read_json_file', return_value=['old']), \
             mock.patch.object(luna, 'steam_running', return_value=False):
            result = luna.live_status(('playtime',))
        self.assertIn('Steam is closed', result['message'])
        self.assertIsNone(result['addons']['playtime']['answered'])

    def test_plain_language_health(self):
        cases = [
            ({'installed': True, 'answered': False, 'error': 'timeout after 5 s'},
             'PROBLEM', 'backend did not answer (timeout after 5 s)'),
            ({'installed': True, 'answered': True, 'health': {'success': False, 'error': 'broken'}},
             'PROBLEM', 'backend reports failure: broken'),
            ({'installed': True, 'answered': True, 'health': {'recovered': True, 'recoveredReason': '.bak'}},
             'ok', 'recovered (.bak)'),
            ({'installed': True, 'answered': True, 'health': {
                'runtime': {'healthy': False, 'error': 'bad', 'accountMismatch': True,
                            'helpersPaused': True, 'helperPauseReason': 'waiting'},
                'discord': {'publishing': False, 'reason': 'no-app-id'},
                'tool': {'found': False}, 'unknown': 'DO NOT PRINT'}},
             'PROBLEM', 'runtime unhealthy; runtime error: bad; account mismatch; helpers paused (waiting); '
                        'Discord not publishing (no app id); compressor tool not found'),
        ]
        for state, severity, text in cases:
            actual, detail = luna.live_addon_summary(state)
            self.assertEqual(actual, severity)
            self.assertIn(text, detail)
            self.assertNotIn('DO NOT PRINT', detail)

    def test_live_cli_exit_and_json(self):
        def run(state, stale, json_output=False, disk_code=0, host='lumen'):
            live = {'host': host, 'addons': {'playtime': state}, 'sidecar': {'stale': stale, 'message': 'sidecar'},
                    'message': 'probed', 'bridge': {'marker_present': True, 'attached': True, 'addon_count': 1}}
            command = luna.cmd_status
            options = argparse.Namespace(only=[], live=True, json=json_output, strict=False)
            output = io.StringIO()
            with mock.patch.object(luna, 'cmd_status', return_value=disk_code), \
                 mock.patch.object(luna, 'live_status', return_value=live), \
                 contextlib.redirect_stdout(output):
                code = command(options)
            return code, output.getvalue()
        healthy = {'installed': True, 'answered': True, 'health': {'runtime': {'healthy': True}}}
        code, text = run(healthy, False)
        self.assertEqual(code, 0)
        self.assertIn('ok - backend answered; runtime healthy', text)
        self.assertIn('all 1 add-ons answering; restart not needed', text)
        self.assertNotIn('"answered"', text)
        code, text = run({'installed': True, 'answered': False, 'error': 'timeout'}, False)
        self.assertEqual(code, 1)
        self.assertIn('look at playtime', text)
        code, text = run(healthy, True, json_output=True)
        self.assertEqual(code, 1)
        data = json.loads(text)
        self.assertEqual(data['exit_code'], 1)
        self.assertEqual(data['live']['addons']['playtime']['health']['runtime']['healthy'], True)
        code, text = run(healthy, True)
        self.assertEqual(code, 1)
        self.assertIn('restart Steam to load the installed files', text)
        code, text = run(healthy, True, host='luna')
        self.assertEqual(code, 1)
        self.assertIn('restart luna-host (systemctl --user restart %s) to load the installed files' % luna.HOST_UNIT,
                      text)
        self.assertEqual(run(healthy, False, disk_code=6)[0], 6)


if __name__ == '__main__':
    unittest.main()
