#!/usr/bin/env python3
"""Tests for `luna selftest` and `luna doctor --report`.

Everything runs against a fake CDP endpoint this file starts (a real socket
speaking /json + one websocket), so the tests never touch the developer's
running Steam, never send a write RPC and need no browser.
"""
import json
import os
import re
import socket
import socketserver
import struct
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
SCRIPTS = HERE / 'scripts'
sys.path.insert(0, str(SCRIPTS))

import luna_selftest  # noqa: E402

TITLES = {'playtime': 'Playtime', 'presence': 'Presence', 'compressor': 'Compressor',
          'hltb': 'HowLongToBeat', 'steamgrid': 'SteamGridDB artwork',
          'graphics': 'Graphics (read-only)', 'health': 'Health (local diagnostics)',
          'updates': 'Updates', 'lumen': 'Lumen', 'settings': 'Settings',
          'about': 'About SLS Luna'}
TABS = [{'id': ident, 'label': label}
        for ident, label in (('playtime', 'Playtime'), ('presence', 'Presence'),
                             ('updates', 'Updates'), ('settings', 'Settings'),
                             ('about', 'About'))]


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

    def __init__(self, tabs=None, select=None, exception_on=None, close=None, probe=None):
        self.tabs = tabs or TABS
        self.select = select or {}
        self.exception_on = exception_on
        self.close_answer = close or {'overlays': 0, 'launchers': 1,
                                      'browserVisible': True, 'hiddenView': False}
        self.probe_override = probe or {}
        self.calls = []
        self.lock = threading.Lock()

    def record(self, method, params):
        with self.lock:
            self.calls.append((method, params))

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
            return {'ok': True, 'name': name, 'ms': 4,
                    'reply': {'success': True, 'ok': True}, 'keys': 2}, None
        if 'luna-selftest:close' in expression:
            return dict(self.close_answer), None
        if 'luna-selftest:embed' in expression:
            return {'embedded': True, 'lumenAbove': True, 'sidebarClickable': True,
                    'rowHeight': 40.0}, None
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


if __name__ == '__main__':
    unittest.main()
