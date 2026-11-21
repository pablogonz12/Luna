#!/usr/bin/env python3
"""Live diagnostics for Luna: `luna selftest` and `luna doctor --report`.

The unit tests use a fake DOM, and every menu regression this year slipped past
them because of that. These two commands look at the panel that is really on
screen, over the same CDP port Steam's own web helper exposes, so a bug shows up
as a FAIL line instead of as an unusable menu.

`luna selftest`
    Connects to the CDP target titled "Steam" (Lumen/luna-host both expose the
    port in ~/.local/share/<host>/cef_port, or take $LUNA_CEF_PORT), opens the
    menu by clicking the launcher *inside the page*, walks every sidebar
    category, checks the read-only status RPCs and closes the menu again. It
    never sends a write RPC and never navigates Steam.

    exit 0 all checks passed, 1 a check failed, 2 Steam/CDP not reachable.

`luna doctor --report`
    One shareable text file: versions, hosts, Steam build and pin, systemd
    units, the tail of the user journals, the panel's Problems ring,
    `luna status`/`luna verify` and the last selftest result. Home paths,
    SteamIDs, token-like values and the Discord display name are redacted.
"""
import base64
import binascii
import json
import os
import re
import socket
import struct
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_UNREACHABLE = 2

# Where Steam (through Lumen or Luna's own host) publishes the CEF port. The
# first token is the port, the rest is a pid or a timestamp.
HOST_HOMES = (('lumen', 'Lumen'), ('luna', 'Luna'))
TARGET_TITLE = 'Steam'
SHARED_TARGET_TITLE = 'SharedJSContext'

STEP_TIMEOUT = 15.0
SELECT_TIMEOUT = 8.0
RPC_TIMEOUT = 6.0
RPC_BUDGET_MS = 2000
SWITCH_BUDGET_MS = 500
ROW_MIN_HEIGHT = 24
MAX_JOURNAL_LINES = 50

# Every read-only status RPC the menu itself calls while it paints. Anything
# that can write (update, install, queue, cancel, repair, restore, lock,
# refresh, check, channels set, terminal, Auto/Apply/Set/Save) is absent on
# purpose; a name that is not listed here is reported as skipped, never called.
READONLY_RPCS = (
    ('SlsLunaInfo', {}),
    ('SlsLunaHealth', {}),
    ('SlsLunaHost', {}),
    ('SlsLunaLuaTools', {}),
    ('SlsLunaGradient', {}),
    ('SlsLunaLaunchers', {'raw': '{"action": "status"}'}),
    ('SlsLunaLumen', {'raw': '{"action": "status"}'}),
    ('SlsLunaUpdate', {'raw': '{"action": "status"}'}),
    ('SlsLunaSteam', {'raw': '{"action": "status"}'}),
    ('SlsLunaRefresh', {'raw': '{"action": "status"}'}),
    ('SlsPlaytimeGet', {}),
    ('SlsPresenceGet', {}),
    ('SlsCompressorGet', {'raw': '{"light": true}'}),
    ('SlsHltbGet', {}),
    ('SlsSgdbGet', {}),
    ('SlsGraphicsGet', {}),
)
READONLY_NAMES = frozenset(name for name, _args in READONLY_RPCS)
# Belt and braces: an argument that names a writing action, or a handler whose
# name is one of the mutating prefixes, is refused before anything is sent.
WRITE_ACTIONS = frozenset(('update', 'install', 'queue', 'cancel', 'repair', 'restore',
                           'lock', 'unlock', 'refresh', 'check', 'channels-set', 'terminal',
                           'apply', 'set', 'save', 'create', 'run', 'fetch', 'put', 'edit',
                           'import', 'export', 'forget', 'undo', 'reset', 'auto', 'delete',
                           'remove', 'start', 'stop'))
WRITE_NAME = re.compile(r'(?i)(Set|Put|Run|Edit|Import|Export|Forget|Undo|Reset|Apply|Cancel|'
                        r'Fetch|Repair|Delete|Remove|Start|Stop)$')


def _is_readonly_call(name, args):
    """A read-only call: a listed name, no writing action, nothing that mutates."""
    if name not in READONLY_NAMES:
        return False, 'not on the read-only list'
    if WRITE_NAME.search(name):
        return False, 'the handler name looks like a write'
    raw = (args or {}).get('raw')
    action = ''
    if isinstance(raw, str):
        try:
            action = str((json.loads(raw) or {}).get('action') or '').lower()
        except (ValueError, AttributeError):
            return False, 'unreadable arguments'
    if action and action in WRITE_ACTIONS:
        return False, 'action %r writes' % action
    for key in ('action', 'cmd', 'method'):
        value = str((args or {}).get(key) or '').lower()
        if value and value in WRITE_ACTIONS:
            return False, 'argument %r writes' % value
    return True, ''


class Unreachable(RuntimeError):
    """Steam/CDP is not there: exit 2, with a message a user can act on."""


class CDPError(RuntimeError):
    """The connection is there but the protocol exchange failed."""


# --------------------------------------------------------------------------- CDP

def cef_port(prefer=()):
    """The CEF debugging port, from $LUNA_CEF_PORT or the host's cef_port file."""
    override = os.environ.get('LUNA_CEF_PORT', '').strip()
    if override:
        try:
            return int(override.split()[0])
        except (ValueError, IndexError):
            raise Unreachable('$LUNA_CEF_PORT is not a port number: %r' % override)
    order = list(prefer) + [name for name, _ in HOST_HOMES]
    tried = []
    for name in order:
        base = dict(HOST_HOMES).get(name)
        if not base:
            continue
        path = Path(os.environ.get('HOME') or Path.home()) / '.local/share' / base / 'cef_port'
        tried.append(str(path))
        try:
            token = path.read_text().split()[0]
        except (OSError, IndexError):
            continue
        if token.isdigit():
            return int(token)
    raise Unreachable('Steam is not exposing a debugging port (%s); start Steam, then run '
                      'luna selftest' % ', '.join(tried))


def list_targets(port, timeout=4.0):
    url = 'http://127.0.0.1:%d/json' % port
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            data = json.loads(response.read().decode('utf-8', 'replace'))
    except (OSError, ValueError) as error:
        raise Unreachable('Steam is not answering on %s (%s); start Steam, then run luna selftest'
                          % (url, error))
    if not isinstance(data, list):
        raise Unreachable('%s did not return a target list; start Steam, then run luna selftest' % url)
    return data


def pick_target(targets, title=TARGET_TITLE):
    """The Steam target, or the steamloopback one when Steam renamed the title."""
    for target in targets:
        if isinstance(target, dict) and target.get('title') == title:
            return target
    for target in targets:
        if isinstance(target, dict) and re.search(r'(steamloopback|steam://)', str(target.get('url') or '')):
            return target
    return None


def _is_websocket(target):
    return isinstance(target, dict) and str(target.get('webSocketDebuggerUrl') or '').startswith('ws://')


class WebSocket:
    """The smallest RFC 6455 client that can talk CDP.

    Python's standard library has no websocket client and Luna installs nothing,
    so this speaks the protocol directly: one masked text frame out, one frame
    in, ping/pong and close handled, no extensions.
    """

    def __init__(self, url, timeout=STEP_TIMEOUT):
        parts = urllib.parse.urlsplit(url)
        if parts.scheme != 'ws':
            raise CDPError('not a ws:// URL: %s' % url)
        self.host = parts.hostname or '127.0.0.1'
        self.port = parts.port or 80
        self.path = (parts.path or '/') + (('?' + parts.query) if parts.query else '')
        self.timeout = timeout
        self.sock = None
        self.buffer = b''

    def connect(self):
        try:
            self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        except OSError as error:
            raise Unreachable('could not open %s:%s (%s)' % (self.host, self.port, error))
        key = base64.b64encode(os.urandom(16)).decode('ascii')
        request = ('GET %s HTTP/1.1\r\nHost: %s:%d\r\nUpgrade: websocket\r\n'
                   'Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n'
                   'Sec-WebSocket-Version: 13\r\n\r\n'
                   % (self.path, self.host, self.port, key))
        self.sock.sendall(request.encode('ascii'))
        head = self._until(b'\r\n\r\n')
        status = head.split(b'\r\n', 1)[0]
        if b' 101' not in status:
            raise CDPError('websocket handshake refused: %s' % status.decode('latin-1', 'replace'))
        return self

    def _until(self, marker):
        while marker not in self.buffer:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise CDPError('the web helper closed the connection')
            self.buffer += chunk
        head, _, rest = self.buffer.partition(marker)
        self.buffer = rest
        return head

    def _exact(self, count):
        while len(self.buffer) < count:
            chunk = self.sock.recv(max(65536, count - len(self.buffer)))
            if not chunk:
                raise CDPError('the web helper closed the connection')
            self.buffer += chunk
        out, self.buffer = self.buffer[:count], self.buffer[count:]
        return out

    def _frame(self, opcode, payload):
        header = bytearray([0x80 | opcode])
        size = len(payload)
        if size < 126:
            header.append(0x80 | size)
        elif size < 65536:
            header.append(0x80 | 126)
            header += struct.pack('!H', size)
        else:
            header.append(0x80 | 127)
            header += struct.pack('!Q', size)
        mask = os.urandom(4)
        header += mask
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def send(self, text):
        self._frame(0x1, text.encode('utf-8'))

    def recv(self, timeout=None):
        """One text message, or None when `timeout` expires with nothing to read."""
        if timeout is not None:
            if not self.buffer:
                self.sock.settimeout(timeout)
        data = b''
        try:
            while True:
                head = self._exact(2)
                opcode = head[0] & 0x0F
                final = bool(head[0] & 0x80)
                masked = bool(head[1] & 0x80)
                length = head[1] & 0x7F
                if length == 126:
                    length = struct.unpack('!H', self._exact(2))[0]
                elif length == 127:
                    length = struct.unpack('!Q', self._exact(8))[0]
                mask = self._exact(4) if masked else None
                payload = self._exact(length)
                if mask:
                    payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
                if opcode == 0x8:
                    raise CDPError('the target closed the websocket')
                if opcode == 0x9:
                    self._frame(0xA, payload)
                    continue
                if opcode == 0xA:
                    continue
                data += payload
                if final:
                    return data.decode('utf-8', 'replace')
        except (socket.timeout, TimeoutError):
            return None
        except (OSError, struct.error, binascii.Error) as error:
            raise CDPError('websocket read failed: %s' % error)

    def close(self):
        if self.sock is None:
            return
        try:
            self._frame(0x8, b'')
        except (OSError, CDPError):
            pass
        try:
            self.sock.close()
        except OSError:
            pass
        self.sock = None


class CDP:
    """One CDP target: Runtime/Log events are collected while calls are answered."""

    def __init__(self, url, timeout=STEP_TIMEOUT):
        self.ws = WebSocket(url, timeout)
        self.next_id = 0
        self.events = []
        self.timeout = timeout

    def connect(self):
        self.ws.connect()
        return self

    def call(self, method, params=None, timeout=None):
        self.next_id += 1
        wanted = self.next_id
        self.ws.send(json.dumps({'id': wanted, 'method': method, 'params': params or {}}))
        deadline = time.time() + (timeout or self.timeout)
        while True:
            left = deadline - time.time()
            if left <= 0:
                raise CDPError('no answer to %s within %.0f s' % (method, timeout or self.timeout))
            message = self.ws.recv(left)
            if message is None:
                continue
            try:
                body = json.loads(message)
            except ValueError:
                continue
            if not isinstance(body, dict):
                continue
            if body.get('id') == wanted:
                if body.get('error'):
                    error = body['error']
                    raise CDPError('%s failed: %s' % (method, error.get('message') or error))
                return body.get('result') or {}
            if body.get('method'):
                self.events.append(body)

    def drain(self, seconds=0.0):
        """Collect events that are already on the wire (never blocks long)."""
        if seconds > 0:
            deadline = time.time() + seconds
            while time.time() < deadline:
                message = self.ws.recv(min(0.05, max(0.0, deadline - time.time())))
                if message is None:
                    continue
                try:
                    body = json.loads(message)
                except ValueError:
                    continue
                if isinstance(body, dict) and body.get('method'):
                    self.events.append(body)

    def take_events(self):
        events, self.events = self.events, []
        return events

    def evaluate(self, expression, timeout=STEP_TIMEOUT):
        result = self.call('Runtime.evaluate', {
            'expression': expression,
            'returnByValue': True,
            'awaitPromise': True,
            'userGesture': True,
        }, timeout=timeout)
        details = result.get('exceptionDetails')
        if details:
            text = (details.get('exception') or {}).get('description') or details.get('text')
            raise CDPError('the page threw: %s' % str(text or 'unknown exception')[:400])
        remote = result.get('result') or {}
        if 'value' in remote:
            return remote['value']
        if remote.get('type') == 'undefined':
            return None
        return remote.get('description')

    def close(self):
        self.ws.close()


# ------------------------------------------------------------------ page scripts
# Every expression carries a `luna-selftest:<name>` marker so a fake target in
# the tests can answer it without running JavaScript.

PROBE_JS = r'''/*luna-selftest:probe*/ (() => {
  const hook = window.__SlsLunaSelftest;
  const state = (hook && typeof hook.state === 'function') ? hook.state() : null;
  const side = document.querySelector('#sls-luna-overlay .sls-luna-side');
  return {
    build: String(window.__SlsLunaPanelBuild || ''),
    hook: Boolean(hook && typeof hook.tabs === 'function' && typeof hook.state === 'function'),
    launchers: document.querySelectorAll('#sls-luna-btn').length,
    overlays: document.querySelectorAll('#sls-luna-overlay').length,
    open: Boolean(document.getElementById('sls-luna-overlay')),
    tabs: (hook && typeof hook.tabs === 'function') ? hook.tabs() : [],
    rows: side ? side.querySelectorAll('.sls-luna-tab').length : 0,
    problems: state ? (state.problems || []) : [],
    embedding: state ? Boolean(state.embedding) : false,
    hiddenView: state ? Boolean(state.hiddenView) : false,
    route: state ? String(state.route || '') : '',
    tab: state ? String(state.tab || '') : ''
  };
})()'''

OPEN_JS = r'''/*luna-selftest:open*/ (async () => {
  const wait = ms => new Promise(done => setTimeout(done, ms));
  const frame = () => new Promise(done => {
    const raf = window.requestAnimationFrame;
    if (typeof raf !== 'function') { setTimeout(done, 30); return; }
    setTimeout(done, 120); raf(() => raf(done)); /* hidden windows draw no frames */
  });
  const button = document.getElementById('sls-luna-btn');
  if (!button) return { ok: false, error: 'the Luna launcher is not in the menubar' };
  const started = performance.now();
  button.click();
  const until = Date.now() + 5000;
  while (!document.getElementById('sls-luna-overlay') && Date.now() < until) await wait(50);
  await frame();
  const overlay = document.getElementById('sls-luna-overlay');
  return {
    ok: Boolean(overlay),
    ms: Math.round(performance.now() - started),
    overlays: document.querySelectorAll('#sls-luna-overlay').length,
    visible: Boolean(overlay && overlay.style.visibility !== 'hidden')
  };
})()'''

SELECT_JS = r'''/*luna-selftest:select*/ (async () => {
  const id = __ID__;
  const wait = ms => new Promise(done => setTimeout(done, ms));
  const frame = () => new Promise(done => {
    const raf = window.requestAnimationFrame;
    if (typeof raf !== 'function') { setTimeout(done, 30); return; }
    setTimeout(done, 120); raf(() => raf(done)); /* hidden windows draw no frames */
  });
  const hook = window.__SlsLunaSelftest;
  if (!hook || typeof hook.tabs !== 'function') {
    return { ok: false, error: 'the panel has no __SlsLunaSelftest hook' };
  }
  const tabs = hook.tabs() || [];
  const index = tabs.findIndex(entry => entry && entry.id === id);
  if (index < 0) return { ok: false, error: 'no sidebar category ' + id };
  const rows = Array.from(document.querySelectorAll('#sls-luna-overlay .sls-luna-side .sls-luna-tab'))
    .filter(row => String(row.className || '').indexOf('sls-luna-subpage') < 0);
  if (!rows[index]) return { ok: false, error: 'no sidebar row for ' + id };
  const started = performance.now();
  rows[index].click();
  await frame();
  const elapsedMs = Math.round((performance.now() - started) * 10) / 10;
  const panes = Array.from(document.querySelectorAll('#sls-luna-overlay .sls-luna-pane'));
  const visible = panes.filter(pane => pane.style.display !== 'none'
    && pane.getClientRects().length > 0 && pane.getBoundingClientRect().height > 0);
  const text = visible.map(pane => String(pane.textContent || '').replace(/\s+/g, ' ').trim());
  const emptyState = visible.some(pane => Boolean(
    pane.querySelector('.sls-luna-note, .sls-luna-empty, .sls-luna-error')));
  const head = document.querySelector('#sls-luna-overlay .sls-luna-head h3');
  const heights = Array.from(document.querySelectorAll('#sls-luna-overlay .sls-luna-side .sls-luna-tab'))
    .map(row => ({
      label: String(row.textContent || '').replace(/\s+/g, ' ').trim(),
      height: Math.round(row.getBoundingClientRect().height * 10) / 10
    }));
  const state = (typeof hook.state === 'function') ? hook.state() : {};
  return {
    ok: true, id, panes: panes.length, visible: visible.length, text, emptyState,
    heading: head ? String(head.textContent || '').trim() : '',
    elapsedMs, heights, tab: String((state && state.tab) || '')
  };
})()'''

RPC_JS = r'''/*luna-selftest:rpc*/ (async () => {
  const name = __NAME__;
  const args = __ARGS__;
  const started = performance.now();
  try {
    const raw = await window.Millennium.callServerMethod('lumen', name, args);
    const value = (typeof raw === 'string') ? JSON.parse(raw) : raw;
    return {
      ok: true, name, ms: Math.round(performance.now() - started), reply: value,
      keys: (value && typeof value === 'object') ? Object.keys(value).length : 0
    };
  } catch (error) {
    return {
      ok: false, name, ms: Math.round(performance.now() - started),
      error: String((error && (error.message || error)) || 'no answer')
    };
  }
})()'''

EMBED_JS = r'''/*luna-selftest:embed*/ (() => {
  const luna = document.getElementById('sls-luna-overlay');
  const lumen = document.getElementById('lumen-settings-overlay');
  const row = document.querySelector('#sls-luna-overlay .sls-luna-side .sls-luna-tab');
  let hitInside = false;
  if (row && row.getBoundingClientRect) {
    const box = row.getBoundingClientRect();
    const hit = document.elementFromPoint(box.left + box.width / 2, box.top + box.height / 2);
    hitInside = Boolean(hit && hit.closest && hit.closest('#sls-luna-overlay'));
  }
  const zIndex = node => (node ? Number(getComputedStyle(node).zIndex) || 0 : 0);
  return {
    embedded: Boolean(luna && lumen && luna.contains(lumen)),
    lumenAbove: Boolean(lumen) && zIndex(lumen) >= zIndex(luna),
    sidebarClickable: hitInside,
    rowHeight: row ? Math.round(row.getBoundingClientRect().height * 10) / 10 : 0
  };
})()'''

CLOSE_JS = r'''/*luna-selftest:close*/ (async () => {
  const wait = ms => new Promise(done => setTimeout(done, ms));
  const frame = () => new Promise(done => {
    const raf = window.requestAnimationFrame;
    if (typeof raf !== 'function') { setTimeout(done, 30); return; }
    setTimeout(done, 120); raf(() => raf(done)); /* hidden windows draw no frames */
  });
  const overlay = document.getElementById('sls-luna-overlay');
  if (overlay) {
    const close = overlay.querySelector('.sls-luna-x');
    if (close) close.click();
    else document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
  }
  const until = Date.now() + 3000;
  while (document.getElementById('sls-luna-overlay') && Date.now() < until) await wait(50);
  await frame();
  let view = null;
  try { view = JSON.parse(localStorage.getItem('slsLunaViewResult') || 'null'); } catch (_) { /* none */ }
  const hook = window.__SlsLunaSelftest;
  const state = (hook && typeof hook.state === 'function') ? hook.state() : null;
  return {
    overlays: document.querySelectorAll('#sls-luna-overlay').length,
    launchers: document.querySelectorAll('#sls-luna-btn').length,
    browserVisible: (view && typeof view.browserVisible === 'boolean') ? view.browserVisible : null,
    hiddenView: state ? Boolean(state.hiddenView) : null
  };
})()'''

ROUTE_JS = r'''/*luna-selftest:route*/ (() => {
  try {
    const manager = window.MainWindowBrowserManager;
    const location = manager && manager.m_history && manager.m_history.location;
    return { route: (location && typeof location.pathname === 'string') ? location.pathname : null };
  } catch (error) {
    return { route: null, error: String((error && error.message) || error) };
  }
})()'''

BROWSER_ROUTE = re.compile(r'^/(browser|store|community|app|account|login|join|checkout|cart|'
                           r'wishlist|points|search)(/|$)')


# ------------------------------------------------------------------- selftest

class Report:
    """Collects one PASS/FAIL/SKIP/INFO line per check and prints as it goes."""

    def __init__(self, out=None, quiet=False):
        self.lines = []
        self.out = out or (lambda line: print(line))
        self.quiet = quiet

    def add(self, status, name, detail=''):
        self.lines.append({'status': status, 'name': name, 'detail': str(detail)})
        if not self.quiet:
            self.out('%-5s %s%s' % (status, name, (': ' + str(detail)) if detail else ''))
        return status

    def passed(self, name, detail=''):
        return self.add('PASS', name, detail)

    def failed(self, name, detail=''):
        return self.add('FAIL', name, detail)

    def skipped(self, name, detail=''):
        return self.add('SKIP', name, detail)

    def info(self, name, detail=''):
        return self.add('INFO', name, detail)

    @property
    def ok(self):
        return all(line['status'] != 'FAIL' for line in self.lines)

    @property
    def counts(self):
        result = {}
        for line in self.lines:
            result[line['status']] = result.get(line['status'], 0) + 1
        return result


def _event_text(event):
    method = event.get('method')
    params = event.get('params') or {}
    if method == 'Runtime.exceptionThrown':
        details = params.get('exceptionDetails') or {}
        thrown = details.get('exception') or {}
        return 'exception', str(thrown.get('description') or details.get('text') or 'exception')
    if method == 'Runtime.consoleAPICalled':
        kind = str(params.get('type') or '')
        if kind != 'error':
            return '', ''
        parts = []
        for arg in params.get('args') or []:
            if isinstance(arg, dict):
                parts.append(str(arg.get('value', arg.get('description', arg.get('type', '')))))
        return 'console.' + kind, ' '.join(parts)
    if method == 'Log.entryAdded':
        entry = params.get('entry') or {}
        if str(entry.get('level') or '') != 'error':
            return '', ''
        return 'log.error', str(entry.get('text') or '')
    return '', ''


INTERESTING = re.compile(r'(?i)sls|luna|lumen|panel|agent')


def page_problems(events):
    """The exception/console errors that concern the panel, as text lines."""
    found = []
    for event in events:
        kind, text = _event_text(event)
        text = (text or '').strip()
        if kind and text and INTERESTING.search(text):
            found.append('%s: %s' % (kind, text.replace('\n', ' ')[:300]))
    return found


def _heading_matches(heading, label):
    heading = str(heading or '').strip().lower()
    label = str(label or '').strip().lower()
    if not heading or not label:
        return False
    return heading.startswith(label) or label.startswith(heading)


def state_dir():
    """Machine-local Luna state, matching luna.luna_state_dir()."""
    home = Path(os.environ.get('LUNA_HOME') or
                (Path(os.environ.get('HOME') or Path.home()) / '.local/share/Luna'))
    return home / 'state'


def save_result(result):
    path = state_dir() / 'selftest-last.json'
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result, indent=2, sort_keys=True) + '\n')
    except OSError:
        pass
    return path


def load_result():
    try:
        return json.loads((state_dir() / 'selftest-last.json').read_text())
    except (OSError, ValueError):
        return None


def _evaluate(cdp, expression, timeout=STEP_TIMEOUT):
    value = cdp.evaluate(expression, timeout=timeout)
    if not isinstance(value, dict):
        raise CDPError('the page returned no structured answer')
    return value


def _check_tabs(report, cdp, probe):
    """Every sidebar category: one visible pane, content, heading, rows, timing."""
    tabs = probe.get('tabs') or []
    if not isinstance(tabs, list) or not tabs:
        report.failed('categories', 'the panel reported no sidebar categories')
        return
    embedding = bool(probe.get('embedding'))
    for entry in tabs:
        if not isinstance(entry, dict):
            continue
        ident = str(entry.get('id') or '')
        label = str(entry.get('label') or ident)
        cdp.take_events()
        try:
            answer = _evaluate(cdp, SELECT_JS.replace('__ID__', json.dumps(ident)),
                               timeout=SELECT_TIMEOUT)
        except (CDPError, Unreachable) as error:
            report.failed('tab %s' % ident, 'the switch did not answer: %s' % error)
            continue
        events = cdp.take_events()
        cdp.drain(0.05)
        events += cdp.take_events()
        problems = page_problems(events)
        if not answer.get('ok'):
            report.failed('tab %s' % ident, answer.get('error') or 'the switch failed')
            continue
        failures = []
        visible = answer.get('visible')
        panes = answer.get('panes')
        if visible != 1:
            failures.append('%s of %s panes are on screen (exactly one must be)'
                            % (visible, panes))
        texts = answer.get('text') or []
        text_length = sum(len(str(item)) for item in texts if isinstance(item, str))
        empty_state = bool(answer.get('emptyState'))
        if visible == 1 and not text_length and not empty_state:
            failures.append('the page is empty: no text and no empty-state element')
        if not _heading_matches(answer.get('heading'), label):
            failures.append('heading %r does not match %r'
                            % (answer.get('heading') or '', label))
        heights = answer.get('heights') or []
        short = [row for row in heights if isinstance(row, dict)
                 and float(row.get('height') or 0) < ROW_MIN_HEIGHT]
        if short:
            failures.append('sidebar row %r is %spx tall (needs >= %d)'
                            % (short[0].get('label'), short[0].get('height'), ROW_MIN_HEIGHT))
        if problems:
            failures.append(problems[0])
        elapsed = float(answer.get('elapsedMs') or 0)
        if elapsed >= SWITCH_BUDGET_MS:
            failures.append('the switch took %.0f ms (budget %d ms)' % (elapsed, SWITCH_BUDGET_MS))
        if ident == 'lumen' and embedding and not text_length:
            # Embedded Lumen paints into the pane; an empty pane is correct there.
            failures = [item for item in failures if 'the page is empty' not in item]
        if answer.get('tab') and answer.get('tab') != ident:
            failures.append('the panel still reports %r as the active tab' % answer.get('tab'))
        if failures:
            report.failed('tab %s' % ident, '; '.join(failures))
        else:
            report.passed('tab %s' % ident,
                          '1 pane, %d chars, heading %r, rows >= %dpx, %.0f ms'
                          % (text_length, str(answer.get('heading') or ''), ROW_MIN_HEIGHT, elapsed))


def _check_rpcs(report, cdp, backend_names=()):
    """Read-only status RPCs only: no write word is ever sent."""
    for name, args in READONLY_RPCS:
        allowed, reason = _is_readonly_call(name, args)
        if not allowed:
            report.failed('rpc %s' % name, 'refused: %s' % reason)
            continue
        expression = (RPC_JS.replace('__NAME__', json.dumps(name))
                      .replace('__ARGS__', json.dumps(args)))
        started = time.time()
        try:
            answer = _evaluate(cdp, expression, timeout=RPC_TIMEOUT)
        except (CDPError, Unreachable) as error:
            report.failed('rpc %s' % name, 'no answer: %s' % error)
            continue
        elapsed_ms = int((time.time() - started) * 1000)
        ms = int(answer.get('ms') or elapsed_ms)
        if not answer.get('ok'):
            report.failed('rpc %s' % name, str(answer.get('error') or 'the call failed'))
            continue
        keys = int(answer.get('keys') or 0)
        reply = answer.get('reply')
        if reply is None or not isinstance(reply, dict) or keys == 0:
            report.failed('rpc %s' % name, 'the reply is nil or not a structure: %r'
                          % (str(reply)[:120]))
            continue
        if ms >= RPC_BUDGET_MS:
            report.failed('rpc %s' % name, 'answered in %d ms (budget %d ms)'
                          % (ms, RPC_BUDGET_MS))
            continue
        note = ''
        if reply.get('success') is False:
            note = ' (backend says: %s)' % str(reply.get('error') or 'no reason')[:120]
        report.passed('rpc %s' % name, '%d key(s), %d ms%s' % (keys, ms, note))
    skipped = sorted(set(backend_names) - READONLY_NAMES)
    if skipped:
        report.skipped('refused/writing RPCs', 'not called: ' + ', '.join(skipped))
    return skipped


def _check_lumen(report, cdp, host, embedding):
    if str(host or '').lower() != 'lumen':
        report.skipped('Lumen embedding', 'the host is %s, not lumen' % (host or 'unknown'))
        return
    if not embedding:
        report.skipped('Lumen embedding', 'embedding is off or unavailable in this session')
        return
    try:
        answer = _evaluate(cdp, EMBED_JS, timeout=SELECT_TIMEOUT)
    except (CDPError, Unreachable) as error:
        report.failed('Lumen embedding', 'no answer: %s' % error)
        return
    failures = []
    if not answer.get('embedded'):
        failures.append('Lumen is not inside the Luna window')
    if not answer.get('lumenAbove'):
        failures.append('the Lumen overlay is not above the Luna pane')
    if not answer.get('sidebarClickable'):
        failures.append('elementFromPoint on a sidebar row does not land in #sls-luna-overlay')
    if failures:
        report.failed('Lumen embedding', '; '.join(failures))
    else:
        report.passed('Lumen embedding', 'embedded, above, sidebar clickable')


def _check_route(report, targets, fallback=''):
    """Report-only: where Steam is, and whether Luna there would navigate."""
    shared = next((target for target in targets
                   if isinstance(target, dict) and target.get('title') == SHARED_TARGET_TITLE), None)
    route = None
    source = ''
    note = ''
    if shared and _is_websocket(shared):
        cdp = None
        try:
            cdp = CDP(shared['webSocketDebuggerUrl'], timeout=6.0).connect()
            answer = _evaluate(cdp, ROUTE_JS, timeout=6.0)
            route = answer.get('route')
            source = SHARED_TARGET_TITLE
        except (CDPError, Unreachable) as error:
            note = 'SharedJSContext did not answer: %s' % error
        finally:
            if cdp:
                cdp.close()
    if route is None and fallback:
        route = fallback
        source = 'the panel'
    if route is None:
        report.skipped('Store route', note or 'no route is readable (nothing was navigated)')
        return None
    if BROWSER_ROUTE.search(str(route)):
        report.info('Store route', '%s (%s): opening Luna there would navigate Steam to the '
                                  'Library first (this check never navigates)' % (route, source))
    else:
        report.info('Store route', '%s (%s): opening Luna there needs no navigation'
                                  % (route, source))
    return route


def run_selftest(json_output=False, keep_open=False, host='', quiet=False,
                 out=None, backend_names=()):
    """Run every live check. Returns (exit code, report)."""
    report = Report(out=out, quiet=quiet or json_output)
    result = {'ok': False, 'at': int(time.time()), 'host': str(host or ''), 'build': '',
              'route': None, 'checks': []}
    cdp = None
    try:
        port = cef_port(prefer=(host,))
        targets = list_targets(port)
        steam = pick_target(targets)
        report.info('CDP port', '127.0.0.1:%d, %d target(s)' % (port, len(targets)))
        if not steam or not _is_websocket(steam):
            report.failed('Steam target', 'no CDP target titled %r; open Steam\u2019s Library '
                                          'window, then run luna selftest' % TARGET_TITLE)
            code = EXIT_UNREACHABLE
            return _finish(report, result, code, json_output, out)
        cdp = CDP(steam['webSocketDebuggerUrl']).connect()
        try:
            cdp.call('Runtime.enable')
            cdp.call('Log.enable')
        except CDPError:
            pass  # older builds answer neither; evaluation still works
        probe = _evaluate(cdp, PROBE_JS)
        result['build'] = str(probe.get('build') or '')
        problems = probe.get('problems') or []

        if probe.get('build'):
            report.passed('panel build', str(probe.get('build')))
        else:
            report.failed('panel build', 'window.__SlsLunaPanelBuild is not set (the panel did '
                                         'not mount in this target)')
        if not probe.get('hook'):
            report.failed('panel hook', 'window.__SlsLunaSelftest is missing; reinstall Luna '
                                        '(its panel build is too old for selftest)')
        launchers = int(probe.get('launchers') or 0)
        if launchers == 1:
            report.passed('launcher', 'exactly one Luna launcher in the menubar')
        else:
            report.failed('launcher', '%d Luna launchers found (exactly one expected)' % launchers)
        overlays = int(probe.get('overlays') or 0)
        if overlays == 0:
            report.passed('overlays', 'no duplicate overlay before opening')
        else:
            report.failed('overlays', '%d Luna overlays are already open' % overlays)
        if problems:
            report.info('problems ring', '%d entry/entries: %s'
                        % (len(problems), '; '.join(str(item)[:80] for item in problems[:3])))

        opened = None
        try:
            opened = _evaluate(cdp, OPEN_JS)
        except (CDPError, Unreachable) as error:
            report.failed('open menu', 'the launcher click failed: %s' % error)
        if opened is not None:
            if opened.get('ok') and opened.get('overlays') == 1:
                report.passed('open menu', 'overlay up in %s ms, %s'
                              % (opened.get('ms'), 'visible' if opened.get('visible') else 'hidden'))
            else:
                report.failed('open menu', opened.get('error') or
                              'the overlay did not appear after clicking the launcher')
        if opened is not None and opened.get('ok'):
            _check_tabs(report, cdp, probe)
            _check_lumen(report, cdp, host, bool(probe.get('embedding')))
            result['route'] = _check_route(report, targets, str(probe.get('route') or ''))
        _check_rpcs(report, cdp, backend_names)

        if keep_open:
            report.skipped('close menu', '--keep-open: the menu was left as it is')
        elif opened is not None and opened.get('ok'):
            try:
                closed = _evaluate(cdp, CLOSE_JS, timeout=SELECT_TIMEOUT)
            except (CDPError, Unreachable) as error:
                report.failed('close menu', 'no answer: %s' % error)
            else:
                failures = []
                if int(closed.get('overlays') or 0) != 0:
                    failures.append('%s overlay(s) remain' % closed.get('overlays'))
                if closed.get('hiddenView'):
                    failures.append('a browser view is left hidden')
                if closed.get('browserVisible') is False:
                    failures.append('Steam reports its browser view as not visible')
                if failures:
                    report.failed('close menu', '; '.join(failures))
                else:
                    report.passed('close menu', 'no overlay and no hidden browser view left')
    except Unreachable as error:
        report.failed('connection', str(error))
        return _finish(report, result, EXIT_UNREACHABLE, json_output, out)
    except (CDPError, OSError) as error:
        report.failed('connection', str(error))
        return _finish(report, result, EXIT_FAIL, json_output, out)
    finally:
        if cdp is not None:
            cdp.close()
    return _finish(report, result, EXIT_OK if report.ok else EXIT_FAIL, json_output, out)


def _finish(report, result, code, json_output, out=None):
    counts = report.counts
    result['ok'] = bool(report.ok and code == EXIT_OK)
    result['code'] = code
    result['checks'] = report.lines
    result['counts'] = counts
    save_result(result)
    say = out or (lambda line: print(line))
    if json_output:
        say(json.dumps(result, indent=2, sort_keys=True))
    else:
        say('%s: %d passed, %d failed, %d skipped, %d note(s)'
            % ('all checks passed' if code == EXIT_OK else 'FAILED',
               counts.get('PASS', 0), counts.get('FAIL', 0), counts.get('SKIP', 0),
               counts.get('INFO', 0)))
    return code, report


# --------------------------------------------------------------------- doctor

def _run(argv, timeout=20):
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return done.returncode, done.stdout, done.stderr
    except (OSError, subprocess.SubprocessError) as error:
        return 127, '', str(error)


STEAM_ID = re.compile(r'\b7656119\d{10}\b')
SECRET = re.compile(r'(?i)(["\']?(?:token|access[_-]?token|refresh[_-]?token|secret|'
                    r'api[_-]?key|password|passwd|authorization|auth[_-]?token|'
                    r'client[_-]?secret)["\']?\s*[:=]\s*)("[^"]*"|\'[^\']*\'|\S+)')
HOME_PATH = re.compile(r'(?:/home/[^/\s:]+|/Users/[^/\s:]+)')


def redact(text, home_dir=None, extra=()):
    """Make a report shareable: home paths, SteamIDs, secrets and names."""
    value = str(text if text is not None else '')
    homes = []
    if home_dir:
        homes.append(str(home_dir))
    if os.environ.get('HOME'):
        homes.append(os.environ['HOME'])
    try:
        homes.append(str(Path.home()))
    except RuntimeError:
        pass
    for candidate in sorted(set(homes), key=len, reverse=True):
        if candidate and candidate != '/':
            value = value.replace(candidate, '~')
            value = value.replace(candidate.rstrip('/'), '~')
    value = HOME_PATH.sub('~', value)
    value = STEAM_ID.sub('<steamid>', value)
    value = SECRET.sub(r'\1<redacted>', value)
    for name in extra:
        name = str(name or '').strip()
        if len(name) >= 2:
            value = re.sub(re.escape(name), '<discord-name>', value)
    return value


def _discord_names():
    """Display names to scrub, read the same way the backend reads them."""
    path = (Path(os.environ.get('LUMEN_DIR') or
                 (Path(os.environ.get('HOME') or Path.home()) / '.local/share/Lumen'))
            / 'luatools' / 'backend' / 'data' / 'lua_tools_auth.json')
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return ()
    if not isinstance(data, dict):
        return ()
    names = []
    for key in ('name', 'display_name', 'username', 'global_name', 'account'):
        find_name(data, key, names)
    return tuple(names)


def find_name(node, key, names, depth=0):
    if depth > 4 or not isinstance(node, dict):
        return
    for name, value in node.items():
        if str(name).lower() == key and isinstance(value, str) and value.strip():
            names.append(value.strip())
        elif isinstance(value, dict):
            find_name(value, key, names, depth + 1)


def _systemd_lines(runner, units):
    lines = []
    for unit in units:
        code, out, err = runner(['systemctl', '--user', 'show', '--property=LoadState,'
                                 'ActiveState,UnitFileState', '--value', unit], 15)
        values = [line.strip() for line in (out or '').splitlines() if line.strip()]
        if code != 0 or not values:
            detail = (err or out or 'not installed').strip().splitlines()[:1]
            lines.append('%-26s %s' % (unit, detail[0] if detail else 'not installed'))
            continue
        while len(values) < 3:
            values.append('')
        lines.append('%-26s load=%s active=%s enabled=%s'
                     % (unit, values[0] or '-', values[1] or '-', values[2] or '-'))
    return lines


def _journal_lines(runner, unit, lines=MAX_JOURNAL_LINES):
    code, out, err = runner(['journalctl', '--user', '-u', unit, '-n', str(lines), '--no-pager'], 20)
    text = (out or '').strip()
    if text:
        return text.splitlines()
    detail = (err or '').strip().splitlines()
    if code == 0:
        return ['(the journal has no entries for %s)' % unit]
    return ['(no journal for %s: %s)' % (unit, detail[0] if detail else 'journalctl unavailable')]


def build_report(out=None, luna=None, runner=None, home_dir=None, now=None,
                 journal_lines=MAX_JOURNAL_LINES, with_commands=True, luna_py=None):
    """Write one redacted report file and return its path."""
    runner = runner or _run
    home = Path(home_dir) if home_dir else Path(os.environ.get('HOME') or Path.home())
    if luna is None:
        luna = _load_luna()
    luna_py = Path(luna_py) if luna_py else Path(getattr(luna, '__file__', 'luna.py')).resolve()
    stamp = time.strftime('%Y%m%d-%H%M%S', time.localtime(now or time.time()))
    if out is None:
        out = home / ('luna-report-%s.txt' % stamp)
    else:
        out = Path(out).expanduser()
    sections = []

    def section(title, body):
        lines = body if isinstance(body, (list, tuple)) else [body]
        sections.append('== %s ==' % title)
        sections.extend(str(line) for line in lines)
        sections.append('')

    version = luna.addon_version('luna') if hasattr(luna, 'addon_version') else '?'
    host = luna.host_label() if hasattr(luna, 'host_label') else '?'
    section('versions', [
        'generated   : %s' % time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now or time.time())),
        'luna        : %s (payload %s)' % (version, getattr(luna, '__file__', '?')),
        'host        : %s' % host,
        'add-ons     : %s' % ', '.join('%s %s' % (name, luna.addon_version(name))
                                       for name in getattr(luna, 'ADDONS', ())),
    ])

    host_lines = []
    if hasattr(luna, 'active_host'):
        host_lines.append('active host : %s' % luna.host_label())
    if hasattr(luna, 'serving_host'):
        host_lines.append('serving     : %s' % luna.host_label(luna.serving_host()))
    for key in ('host_state_path', 'luna_state_dir', 'luna_tools_dir'):
        if hasattr(luna, key):
            try:
                host_lines.append('%-12s: %s' % (key.replace('_', ' '), getattr(luna, key)()))
            except OSError:
                pass
    section('host', host_lines or ['unknown'])

    steam_lines = []
    if hasattr(luna, 'known_good_line'):
        steam_lines.append(str(luna.known_good_line()[1]))
    if hasattr(luna, 'heal_steam_lock'):
        try:
            result = luna.heal_steam_lock(apply=False)
        except (OSError, subprocess.SubprocessError, SystemExit) as error:
            steam_lines.append('pin state    : unreadable (%s)' % error)
        else:
            pinned = result.get('pinned')
            steam_lines.append('pin state    : %s' % ('pinned' if pinned else 'not pinned'))
            notes = result.get('notes') or []
            steam_lines.extend('  - %s' % note for note in notes[:10])
            if pinned and not notes:
                steam_lines.append('  ok: update block and read-only state are intact')
    section('Steam client', steam_lines or ['unknown'])

    host_unit = 'luna-host.service' if (hasattr(luna, 'active_host')
                                        and luna.active_host() == 'luna') else 'lumen.service'
    units = [host_unit, 'luna-apply.service', 'luna-autoupdate.service', 'luna-autoupdate.timer']
    section('systemd (user)', _systemd_lines(runner, units))

    journal = []
    for unit in units:
        journal.append('-- %s (last %d lines) --' % (unit, journal_lines))
        journal.extend(_journal_lines(runner, unit, journal_lines))
        journal.append('')
    section('journals', journal)

    problems = _probe_problems(runner)
    section('Problems ring', problems)

    if with_commands and luna_py.is_file():
        for label, args in (('luna status', ['status']), ('luna verify', ['verify'])):
            code, stdout, stderr = runner([sys.executable, str(luna_py)] + args, 180)
            body = ['exit %s' % code]
            body.extend((stdout or '').rstrip().splitlines())
            if (stderr or '').strip():
                body.append('stderr:')
                body.extend(stderr.rstrip().splitlines())
            section(label, body)

    last = load_result()
    if last:
        counts = last.get('counts') or {}
        section('last selftest', [
            'when   : %s' % time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(last.get('at') or 0)),
            'result : %s (exit %s)' % ('passed' if last.get('ok') else 'failed', last.get('code')),
            'counts : %s' % ', '.join('%s=%s' % (key, counts[key]) for key in sorted(counts)),
            'build  : %s' % (last.get('build') or '?'),
        ] + ['  %-5s %s%s' % (line.get('status'), line.get('name'),
                             (': ' + line.get('detail')) if line.get('detail') else '')
           for line in (last.get('checks') or []) if line.get('status') == 'FAIL'][:20])
    else:
        section('last selftest', ['none yet (run: luna selftest)'])

    text = redact('\n'.join(sections).rstrip() + '\n', home_dir=home,
                  extra=_discord_names())
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text)
    return out


def _probe_problems(runner=None):
    """The panel's Problems ring, from the running Steam, when it is reachable."""
    try:
        port = cef_port()
        targets = list_targets(port)
    except Unreachable as error:
        return ['not reachable: %s' % error]
    steam = pick_target(targets)
    if not steam or not _is_websocket(steam):
        return ['not reachable: no Steam target is open (open the Library window)']
    cdp = None
    try:
        cdp = CDP(steam['webSocketDebuggerUrl'], timeout=6.0).connect()
        answer = _evaluate(cdp, PROBE_JS, timeout=6.0)
    except (CDPError, Unreachable) as error:
        return ['not reachable: %s' % error]
    finally:
        if cdp:
            cdp.close()
    problems = answer.get('problems') or []
    if not problems:
        return ['empty: no panel problem was recorded this session']
    return ['%d entry/entries:' % len(problems)] + ['  - %s' % str(item)[:300] for item in problems]


def _load_luna():
    import importlib.util
    path = Path(__file__).resolve().parent / 'luna.py'
    if not path.is_file():
        raise SystemExit('FAIL: luna.py is not beside luna_selftest.py')
    if str(path.parent) not in sys.path:
        sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location('luna_selftest_luna', str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def backend_rpc_names(root=None):
    """Every registry.RPC the add-on backends publish, for the skip list."""
    root = Path(root or (Path(__file__).resolve().parents[1] / 'addons'))
    names = set()
    for path in sorted(root.glob('*/plugin/backend/main.lua')):
        try:
            text = path.read_text(errors='replace')
        except OSError:
            continue
        names.update(re.findall(r'registry\.(Sls[A-Za-z0-9_]+)\s*=', text))
    return sorted(names)


def main(argv=None):
    """`python3 luna_selftest.py [--json] [--keep-open]` for a checkout run."""
    argv = list(sys.argv[1:] if argv is None else argv)
    code, _report = run_selftest(json_output='--json' in argv, keep_open='--keep-open' in argv,
                                 host=os.environ.get('LUNA_SELFTEST_HOST', ''),
                                 backend_names=backend_rpc_names())
    return code


if __name__ == '__main__':
    sys.exit(main())
