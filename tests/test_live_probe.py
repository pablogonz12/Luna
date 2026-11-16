"""`status --live` and the live library order against a FAKE DevTools endpoint.

A local stdlib HTTP + WebSocket server (one thread) plays Steam's debug port. Nothing
here may reach a real Steam: HOME, the Lumen directory and /proc are disposable, and
`socket.create_connection` is guarded so any connection to a port other than the
fake one fails the test."""
import base64
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import socket
import struct
import tempfile
import threading
import time
import unittest
from unittest import mock

os.environ.setdefault('LUNA_GUI_CHECKS', '0')
os.environ.setdefault('LUNA_LIVE_CHECKS', '0')

SPEC = importlib.util.spec_from_file_location('luna_live_test', Path(__file__).resolve().parents[1] / 'scripts/luna.py')
luna = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(luna)

_REAL_CONNECT = socket.create_connection


def recv_exact(conn, count):
    data = b''
    while len(data) < count:
        chunk = conn.recv(count - len(data))
        if not chunk:
            raise OSError('closed')
        data += chunk
    return data


class FakeDevTools:
    """Serves /json/version, /json/list and one WebSocket per target. `answer(expr)`
    returns the Runtime.evaluate value; every expression is recorded."""

    def __init__(self, answer, targets=('SharedJSContext', 'Steam'), mute=False, fragment=False):
        self.answer, self.expressions, self.mute, self.fragment = answer, [], mute, fragment
        self.titles = targets
        self.server = socket.socket()
        self.server.bind(('127.0.0.1', 0))
        self.server.listen(8)
        self.port = self.server.getsockname()[1]
        self.stop = False
        threading.Thread(target=self.serve, daemon=True).start()

    def close(self):
        self.stop = True
        try:
            self.server.close()
        except OSError:
            pass

    def serve(self):
        while not self.stop:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            threading.Thread(target=self.handle, args=(conn,), daemon=True).start()

    def handle(self, conn):
        try:
            head = b''
            while b'\r\n\r\n' not in head:
                head += conn.recv(4096)
            lines = head.decode().split('\r\n')
            path = lines[0].split()[1]
            headers = {k.lower(): v.strip() for k, _, v in (l.partition(':') for l in lines[1:]) if k}
            if path.startswith('/json'):
                targets = [{'type': 'page', 'title': t, 'url': 'https://steamloopback.host/index.html' if t in ('SharedJSContext', 'Steam') else 'about:blank',
                            'webSocketDebuggerUrl': 'ws://127.0.0.1:%d/devtools/page/%d' % (self.port, i)}
                           for i, t in enumerate(self.titles)]
                body = json.dumps(targets).encode()
                conn.sendall(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n' % len(body) + body)
                return
            accept = base64.b64encode(hashlib.sha1((headers['sec-websocket-key']
                                                    + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
            conn.sendall(('HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n'
                          'Sec-WebSocket-Accept: %s\r\n\r\n' % accept).encode())
            while True:
                first, second = recv_exact(conn, 2)
                length = second & 0x7F
                if length == 126:
                    length = struct.unpack('>H', recv_exact(conn, 2))[0]
                elif length == 127:
                    length = struct.unpack('>Q', recv_exact(conn, 8))[0]
                assert second & 0x80, 'client frames must be masked'
                mask = recv_exact(conn, 4)
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(recv_exact(conn, length)))
                message = json.loads(payload)
                self.expressions.append(message['params']['expression'])
                if self.mute:
                    continue
                reply = json.dumps(self.answer(message['params']['expression']))
                event = json.dumps({'method': 'Runtime.consoleAPICalled', 'params': {}})
                self.frame(conn, event)
                self.frame(conn, self.wrap(message['id'], reply))
        except (OSError, AssertionError, ValueError, KeyError):
            pass
        finally:
            conn.close()

    @staticmethod
    def wrap(ident, value_json):
        return json.dumps({'id': ident, 'result': {'result': {'type': 'object', 'value': json.loads(value_json)}}})

    def frame(self, conn, text):
        data = text.encode()
        pieces = [data[:len(data) // 2], data[len(data) // 2:]] if self.fragment and len(data) > 4 else [data]
        for index, piece in enumerate(pieces):
            opcode = 0x1 if index == 0 else 0x0
            fin = 0x80 if index == len(pieces) - 1 else 0
            size = len(piece)
            head = bytes([fin | opcode])
            head += bytes([size]) if size < 126 else bytes([126]) + struct.pack('>H', size) if size < 65536 \
                else bytes([127]) + struct.pack('>Q', size)
            conn.sendall(head + piece)


def rpc_answer(replies):
    """An `answer(expr)` that replies to RPC wrappers by method name; `replies[name]` is
    a dict (the backend payload, JSON-encoded like Lumen does), an Exception to simulate a
    silent backend, or missing (-> error). Named expressions get their canned value."""
    def answer(expression):
        for key, value in replies.items():
            if key in ('bridge', 'library') and expression == luna.LIVE_READ_EXPRESSIONS[key]:
                return value
        for name, value in replies.items():
            if json.dumps(name) in expression and 'callServerMethod' in expression:
                if isinstance(value, Exception):
                    return {'error': 'no answer from the backend'}
                return {'value': json.dumps(value)}
        if 'callServerMethod' in expression:
            return {'error': 'no answer from the backend'}
        return None
    return answer


HEALTHY = {
    'SlsLunaHealth': {'success': True, 'health': {'version': '9.9.9', 'install': {'drift': []},
                                                  'lumen': {'addons': [{'name': 'luna'}, {'name': 'sgdb'}]}}},
    'SlsSgdbGet': {'success': True, 'version': '1.0.0', 'hasKey': False, 'gridError': ''},
    'SlsGraphicsGet': {'success': True, 'version': '1.0.0', 'gpus': [{'vendor_name': 'X'}]},
    'SlsPlaytimeGet': {'success': True, 'runtime': {'healthy': True, 'error': ''}},
    'SlsPresenceGet': {'success': True, 'runtime': {'healthy': True, 'error': ''}},
    'SlsPresenceContractProbe': {'success': True, 'argType': 'string', 'decoded': True},
    'bridge': {'rpc': True, 'lunaMenu': True, 'lunaSort': {'value': 12, 'seen': 1, 'games': 3, 'order': 'short'}},
}

LIBRARY = {'found': True, 'rowCount': 6, 'storage': {'slsLunaSortLive': '4'}, 'rows': [
    ['c', 'favorite', 'Favorites', 1, 0], ['a', 'favorite', 'Alpha Game', 10],
    ['c', 'all', 'All', 3, 0], ['a', 'all', 'Alpha Game', 10], ['a', 'all', 'Beta Game', 20],
    ['c', 'hidden', 'Hidden', 7, 1]]}


class LiveCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='luna-live-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / 'home'
        self.lumen = self.home / '.local/share/Lumen'
        self.proc = self.tmp / 'proc'
        for folder in (self.lumen / 'lua', self.lumen / 'sls-addons', self.proc):
            folder.mkdir(parents=True)
        (self.lumen / 'lua/boot.lua').write_text('-- ' + luna.MARKER + '\n')
        for name, version in (('luna', '9.9.9'), ('sgdb', '1.0.0'), ('graphics', '1.0.0'),
                              ('playtime', '1.0.0'), ('presence', '1.0.0'), ('compressor', '1.0.0')):
            (self.lumen / 'sls-addons' / name).mkdir()
            (self.lumen / 'sls-addons' / name / 'plugin.json').write_text(json.dumps({'version': version}))
        self.servers = []
        patcher = mock.patch.dict(os.environ, {'HOME': str(self.home), 'LUMEN_DIR': str(self.lumen),
                                               'LUNA_PROC_DIR': str(self.proc), 'LUNA_LIVE_CHECKS': '1',
                                               'LUNA_GUI_CHECKS': '0'})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop('LUNA_CDP_PORT', None)
        self.allowed_ports = set()

        def guarded(address, *args, **kwargs):
            if address[1] not in self.allowed_ports:
                raise AssertionError('test tried to reach a real port: %r' % (address,))
            return _REAL_CONNECT(address, *args, **kwargs)
        guard = mock.patch.object(socket, 'create_connection', guarded)
        guard.start()
        self.addCleanup(guard.stop)

    def fake(self, replies=None, **kwargs):
        merged = dict(HEALTHY)
        merged.update(replies or {})
        merged['library'] = LIBRARY
        server = FakeDevTools(rpc_answer(merged), **kwargs)
        self.servers.append(server)
        self.addCleanup(server.close)
        self.allowed_ports.add(server.port)
        (self.lumen / 'cef_port').write_text(str(server.port))
        return server

    def fake_process(self, pid, comm, cmdline=b'', start_ticks=0, boot=1_000_000):
        entry = self.proc / str(pid)
        entry.mkdir(exist_ok=True)
        (entry / 'comm').write_text(comm + '\n')
        (entry / 'cmdline').write_bytes(cmdline)
        (entry / 'stat').write_text('%d (%s) S %s' % (pid, comm, ' '.join(['0'] * 18 + [str(start_ticks)] + ['0'] * 10)))
        (self.proc / 'stat').write_text('cpu 1 2 3\nbtime %d\n' % boot)

    def status(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = luna.main(['status', '--live'] + list(args))
        return code, out.getvalue(), err.getvalue()


class StatusLive(LiveCase):
    def test_healthy_addons(self):
        server = self.fake()
        self.fake_process(10, 'lumen', start_ticks=0, boot=int(time.time()) + 100)  # started "in the future"
        code, out, err = self.status('--only', 'luna', '--only', 'sgdb', '--only', 'presence', '--only', 'compressor')
        live = out[out.index('live '):]
        self.assertIn('DevTools on 127.0.0.1:%d' % server.port, live)
        self.assertIn('reflected in the running process', live)
        self.assertRegex(live, r'luna\s+answers\s+v9\.9\.9')
        self.assertRegex(live, r'presence\s+answers\s+agent reports healthy; contract ok')
        self.assertRegex(live, r'compressor\s+n/a\s+no read-only health RPC')
        self.assertIn('n/a (disabled (LUNA_GUI_CHECKS=0))', live)
        self.assertNotIn('Traceback', out + err)
        # Strictly read-only on the wire: only allow-listed RPC names reached the client.
        names = {name for e in server.expressions for name in luna.re.findall(r'"(Sls\w+)"', e)}
        self.assertTrue(names)
        self.assertLessEqual(names, luna.LIVE_RPC_ALLOWLIST)

    def test_rendered_column_from_gui_layer(self):
        self.fake()
        fake = {'luna': {'state': 'found', 'hits': 3, 'samples': 3}}
        with mock.patch.object(luna, 'gui_unavailable_reason', return_value=None), \
                mock.patch.object(luna, 'gui_rendered', return_value=fake) as rendered:
            code, out, _ = self.status('--only', 'luna')
        self.assertIn('found (3/3)', out)
        self.assertFalse(rendered.call_args.kwargs.get('navigate', True), 'status --live must not click')

    def test_backend_that_does_not_answer(self):
        self.fake({'SlsPlaytimeGet': RuntimeError('x')})
        code, out, _ = self.status('--only', 'playtime', '--only', 'luna')
        self.assertRegex(out, r'playtime\s+SILENT\s+no answer')
        self.assertEqual(code, 1)

    def test_unhealthy_report_and_version_mismatch(self):
        self.fake({'SlsPlaytimeGet': {'success': True, 'runtime': {'healthy': False, 'error': 'ledger locked'}},
                   'SlsSgdbGet': {'success': True, 'version': '0.9.0', 'hasKey': True, 'gridError': ''}})
        code, out, _ = self.status('--only', 'playtime', '--only', 'sgdb')
        self.assertIn('agent reports unhealthy: ledger locked', out)
        self.assertIn('running v0.9.0 but v1.0.0 is installed', out)
        self.assertEqual(code, 1)

    def test_no_addon_backend_means_marker_not_reflected(self):
        self.fake({name: RuntimeError('x') for name in HEALTHY if name.startswith('Sls')})
        code, out, _ = self.status('--only', 'luna')
        self.assertIn('NOT reflected', out)
        self.assertEqual(code, 1)

    def test_stale_sidecar(self):
        self.fake()
        now = int(time.time())
        self.fake_process(11, 'lumen', start_ticks=0, boot=now - 3600)  # up for an hour
        for path in [self.lumen / 'lua/boot.lua'] + list((self.lumen / 'sls-addons').rglob('plugin.json')):
            os.utime(path, (now - 7200, now - 7200))
        os.utime(self.lumen / 'sls-addons/luna/plugin.json', (now - 60, now - 60))  # installed a minute ago
        code, out, _ = self.status('--only', 'luna')
        self.assertIn('STALE', out)
        self.assertIn('sls-addons/luna/plugin.json', out)
        self.assertEqual(code, 1)

    def test_current_sidecar(self):
        self.fake()
        now = int(time.time())
        for path in [self.lumen / 'lua/boot.lua'] + list((self.lumen / 'sls-addons').rglob('plugin.json')):
            os.utime(path, (now - 7200, now - 7200))
        self.fake_process(12, 'lumen', start_ticks=0, boot=now - 3600)
        state = luna.live_sidecar_state()
        self.assertEqual((state['state'], state['pid']), ('current', 12))

    def test_port_from_steamwebhelper_command_line(self):
        server = self.fake()
        (self.lumen / 'cef_port').unlink()
        self.fake_process(20, 'steamwebhelper', b'/x/steamwebhelper\0--remote-debugging-port=%d\0' % server.port)
        self.assertEqual(luna.live_candidate_ports(), [server.port])
        code, out, _ = self.status('--only', 'luna')
        self.assertIn('DevTools on 127.0.0.1:%d' % server.port, out)

    def test_port_unavailable(self):
        free = socket.socket()
        free.bind(('127.0.0.1', 0))
        port = free.getsockname()[1]
        free.close()
        self.allowed_ports.add(port)
        (self.lumen / 'cef_port').write_text(str(port))
        self.fake_process(30, 'steamwebhelper', b'steamwebhelper\0')
        code, out, err = self.status('--only', 'luna')
        self.assertRegex(out, r'live\s+: unavailable - the DevTools debug port is unavailable \(port %d' % port)
        self.assertNotIn('Traceback', out + err)
        with contextlib.redirect_stdout(io.StringIO()):
            plain = luna.main(['status', '--only', 'luna'])
        self.assertEqual(code, plain, 'an unreachable client must not change the exit status')

    def test_steam_closed(self):
        code, out, err = self.status('--only', 'luna')
        self.assertIn('live       : unavailable - Steam is not running, so there is no client to probe', out)
        self.assertNotIn('Traceback', out + err)

    def test_no_shared_context_target(self):
        self.fake(targets=('Welcome to Steam',))
        code, out, _ = self.status('--only', 'luna')
        self.assertIn('has no SharedJSContext page yet', out)

    def test_plain_status_unchanged(self):
        self.fake()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            luna.main(['status', '--only', 'luna'])
        self.assertNotIn('live ', out.getvalue())
        self.assertFalse(self.servers[0].expressions, 'plain status must not touch the client')

    def test_kill_switch(self):
        self.fake()
        with mock.patch.dict(os.environ, {'LUNA_LIVE_CHECKS': '0'}):
            code, out, _ = self.status('--only', 'luna')
        self.assertIn('live checks are disabled', out)
        self.assertFalse(self.servers[0].expressions)


class Transport(LiveCase):
    def test_refuses_non_allowlisted_rpc(self):
        server = self.fake()
        with luna.LiveSession() as session:
            for name in ('SlsPlaytimeEdit', 'SlsLunaPrefsSet', 'SlsCompressorGet', 'SlsCompressorRun', 'x");alert(1);("'):
                with self.assertRaisesRegex(luna.LiveProbeError, 'not an allow-listed read-only RPC'):
                    session.rpc(name)
            with self.assertRaisesRegex(luna.LiveProbeError, 'not a known read expression'):
                session.read('window.location = "x"')
        self.assertEqual(server.expressions, [], 'a refused call must never be sent')

    def test_allowlist_has_no_mutating_names(self):
        for name in luna.LIVE_RPC_ALLOWLIST:
            self.assertRegex(name, r'^Sls\w+(Get|Info|Health|ContractProbe)$')
        self.assertFalse(any(v not in luna.LIVE_RPC_ALLOWLIST for v in luna.LIVE_ADDON_PROBES.values() if v))

    def test_fragmented_frames_and_events_are_handled(self):
        self.fake(fragment=True)
        with luna.LiveSession() as session:
            self.assertEqual(session.rpc('SlsSgdbGet')['version'], '1.0.0')

    def test_timeout_is_a_clear_error(self):
        self.fake(mute=True)
        with luna.LiveSession(timeout=0.4) as session:
            with self.assertRaisesRegex(luna.LiveProbeError, 'did not answer in time'):
                session.rpc('SlsSgdbGet')

    def test_refuses_non_local_websocket(self):
        with self.assertRaisesRegex(luna.LiveProbeError, 'non-local'):
            luna.LiveSocket('ws://example.com:9222/devtools/page/1')

    def test_cdp_port_override(self):
        server = self.fake()
        (self.lumen / 'cef_port').write_text('1')
        with mock.patch.dict(os.environ, {'LUNA_CDP_PORT': str(server.port)}):
            self.assertEqual(luna.live_candidate_ports(), [server.port])


HLTB_GRID = {'mounted': 8, 'sorts': {'1': 4, '4': 2, '12': 2}, 'live': '12', 'views': [
    {'kind': 'shelf', 'id': 'uc-FIXTURE', 'count': 1263, 'total': 1263, 'rows': [
        [832080, 'Back to the Moon'], [812810, 'missed messages.'], [3277530, 'Aeonic']]},
    {'kind': 'shelf', 'id': 'uc-FIXTURE', 'count': 1263, 'total': 1263, 'rows': [
        [832080, 'Back to the Moon'], [812810, 'missed messages.'], [3277530, 'Aeonic']]},
]}


class HltbGrid(LiveCase):
    def test_reads_the_sorted_grid_and_dedupes_fibers(self):
        server = self.fake()
        server.answer = lambda expression: HLTB_GRID if expression == luna.LIVE_READ_EXPRESSIONS['hltb_grid'] else None
        grid = luna.live_hltb_grid()
        self.assertTrue(grid['active'])
        self.assertEqual(len(grid['views']), 1)
        self.assertEqual(grid['views'][0]['appids'], [832080, 812810, 3277530])
        self.assertEqual(grid['liveSort'], '12')

    def test_not_active_when_no_view_uses_the_sort(self):
        server = self.fake()
        server.answer = lambda expression: {'mounted': 4, 'sorts': {'1': 4}, 'live': '1', 'views': []}
        grid = luna.live_hltb_grid()
        self.assertFalse(grid['active'])
        self.assertEqual(grid['views'], [])

    def test_the_expression_is_read_only(self):
        expression = luna.LIVE_READ_EXPRESSIONS['hltb_grid']
        for word in ('setItem', 'removeItem', '.click(', 'dispatchEvent', 'fetch(', 'callServerMethod'):
            self.assertNotIn(word, expression)

    def test_unlisted_expression_is_refused(self):
        self.fake()
        with luna.LiveSession() as session:
            with self.assertRaises(luna.LiveProbeError):
                session.read('not_allowed')


class LibraryOrder(LiveCase):
    def test_order_names_sections_and_sort_state(self):
        self.fake()
        order = luna.live_library_order()
        self.assertEqual(order['names'], ['Alpha Game', 'Alpha Game', 'Beta Game'])
        self.assertEqual((order['rendered'], order['unique'], order['rows']), (3, 2, 6))
        self.assertEqual([(s['id'], s['start'], s['collapsed']) for s in order['sections']],
                         [('favorite', 0, False), ('all', 1, False), ('hidden', 3, True)])
        self.assertEqual(order['sort']['stored'], {'slsLunaSortLive': 4})
        self.assertEqual(order['sort']['luna']['value'], 12)
        self.assertFalse(order['virtualized'])

    def test_command_save_and_compare(self):
        self.fake()
        saved = self.tmp / 'order.json'
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(luna.main(['library-order', '--save', str(saved)]), 0)
        self.assertEqual(json.loads(saved.read_text())['names'], ['Alpha Game', 'Alpha Game', 'Beta Game'])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(luna.main(['library-order', '--compare', str(saved)]), 0)
        self.assertIn('matches', out.getvalue())
        data = json.loads(saved.read_text())
        data['names'].reverse()
        saved.write_text(json.dumps(data))
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            self.assertEqual(luna.main(['library-order', '--compare', str(saved)]), 1)
        self.assertIn('differs', err.getvalue())

    def test_library_not_mounted(self):
        server = self.fake()
        server.answer = lambda expression: {'found': False, 'reason': 'the Library list is not mounted (open the Library tab)'}
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(luna.main(['library-order']), 1)
        self.assertIn('not mounted', err.getvalue())
        self.assertNotIn('Traceback', err.getvalue())

    def test_steam_closed(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(luna.main(['library-order']), 1)
        self.assertIn('Steam is not running', err.getvalue())


if __name__ == '__main__':
    unittest.main()
