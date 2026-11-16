# --- live health probe -------------------------------------------------------
# `status --live` and the library-order reader talk to the RUNNING Steam client
# through its Chromium DevTools port (the one Lumen itself injects through). There
# was no CDP client in this repository to reuse (the add-ons run inside
# SharedJSContext and never speak CDP), so this is a minimal stdlib one: an HTTP
# GET for discovery plus a WebSocket handshake with client-side masking and text
# frames. Everything here is read-only by construction:
#   * RPCs go through live_rpc(), which refuses any name outside LIVE_RPC_ALLOWLIST;
#   * the only JavaScript ever evaluated is built by this file from the constants
#     below (LIVE_READ_EXPRESSIONS and the RPC wrapper), never from user input;
#   * no clicks, no navigation, no files written, no process touched.
# Set LUNA_LIVE_CHECKS=0 to switch the whole layer off (tests do).

LIVE_HOST = '127.0.0.1'
LIVE_TIMEOUT = 6.0            # seconds per HTTP request / per evaluated expression
LIVE_MAX_MESSAGE = 64 << 20   # a 1,600-game library order is ~100 KB; this is a hard cap
LIVE_TARGET_TITLE = 'SharedJSContext'
LIVE_MAIN_TITLE = 'Steam'     # the main window page: where the Library list is mounted

# The ONLY backend methods `status --live` may call. Each was read in its backend
# source (addons/*/plugin/backend/main.lua) and performs no write, spawn or other
# state change (the Lua `registry.<name>` handlers contain only reads):
#   SlsLunaInfo               returns the version, installed add-on names, prefs.
#   SlsLunaHealth             build_health(): file/marker/version report, "Read-only diagnostics".
#   SlsPlaytimeGet            snapshot of the ledger + `runtime` health; the 30 s inventory
#                             cache is memory only (the ledger migration runs at load, not here).
#   SlsPresenceGet            state + `runtime` health; reads only.
#   SlsPresenceContractProbe  documented "Read-only contract probe"; echoes the argument shape.
#   SlsSgdbGet                load_config + grid_dir; reports whether a key exists, masked.
#   SlsGraphicsGet            reads os-release, /proc and hardware files; nothing is written.
#   SlsHltbGet                load_store + load_library; local reads only.
# Deliberately NOT here: SlsCompressorGet (the first call may start a scan process and
# writes its attempt stamp), and every Put/Set/Edit/Run/Apply/Update/Refresh/Report call.
LIVE_RPC_ALLOWLIST = frozenset({
    'SlsLunaInfo', 'SlsLunaHealth', 'SlsPlaytimeGet', 'SlsPresenceGet',
    'SlsPresenceContractProbe', 'SlsSgdbGet', 'SlsGraphicsGet', 'SlsHltbGet',
})

# Per add-on: the allow-listed RPC that reports its health, or None when it has no
# read-only one (reported as "no read-only health RPC", never guessed).
LIVE_ADDON_PROBES = {
    'luna': 'SlsLunaHealth',
    'sgdb': 'SlsSgdbGet',
    'graphics': 'SlsGraphicsGet',
    'playtime': 'SlsPlaytimeGet',
    'presence': 'SlsPresenceGet',
    'compressor': None,
}


class LiveProbeError(Exception):
    """A clear, one-line reason the live client cannot be read."""


def live_enabled():
    return os.environ.get('LUNA_LIVE_CHECKS', '1') != '0'


def live_proc_dir():
    return os.environ.get('LUNA_PROC_DIR') or '/proc'


# ----------------------------------------------------------------- discovery

def live_candidate_ports(proc=None, lumen=None):
    """Debug ports worth trying, best first, without hardcoding any number.

    1. $LUNA_CDP_PORT (explicit override);
    2. Lumen's own `cef_port` file (what the sidecar connects to);
    3. `--remote-debugging-port=N` on this user's steamwebhelper command lines."""
    ports = []

    def add(value):
        try:
            number = int(str(value).strip())
        except ValueError:
            return
        if 0 < number < 65536 and number not in ports:
            ports.append(number)

    if os.environ.get('LUNA_CDP_PORT'):
        add(os.environ['LUNA_CDP_PORT'])
        return ports
    try:
        add((Path(lumen or lumen_root()) / 'cef_port').read_text(errors='replace').split()[0])
    except (OSError, IndexError, ValueError):
        pass
    try:
        entries = list(Path(proc or live_proc_dir()).iterdir())
    except OSError:
        entries = []
    uid = os.getuid()
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            if entry.stat().st_uid != uid:
                continue
            argv = (entry / 'cmdline').read_bytes().split(b'\0')
        except OSError:
            continue
        if os.path.basename(argv[0].decode('utf-8', 'replace')) != 'steamwebhelper':
            continue
        for arg in argv[1:]:
            text = arg.decode('utf-8', 'replace')
            if text.startswith('--remote-debugging-port='):
                add(text.split('=', 1)[1])
    return ports


LIVE_LOOPBACK = ('127.0.0.1', 'localhost', '::1')


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """The debug port never redirects; following one could leave the loopback interface."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def live_http_json(port, path, timeout=LIVE_TIMEOUT, host=LIVE_HOST):
    """GET http://127.0.0.1:<port><path> -> parsed JSON. Raises OSError/ValueError.
    Refuses any non-loopback host and never follows a redirect (HTTP 3xx -> OSError)."""
    if host not in LIVE_LOOPBACK:
        raise ValueError('refusing a non-loopback DevTools host: %s' % host)
    request = urllib.request.Request('http://%s:%d%s' % ('[::1]' if host == '::1' else host, port, path))
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)  # never via a proxy
    with opener.open(request, timeout=timeout) as response:
        return json.loads(response.read(LIVE_MAX_MESSAGE).decode('utf-8', 'replace'))


STEAM_PROCESS_NAMES = ('steam', 'steamwebhelper', 'steam.sh')


def steam_process_scan(proc=None):
    """THE Steam detector (live probe, GUI checks and the GE-Proton manager all use it).
    {'running', 'accessible', 'error'} for this user's processes, read from /proc only. A
    process counts when its `comm` or the basename of argv[0] is steam, steamwebhelper or
    steam.sh. `error` is non-empty when the answer cannot be trusted (the listing failed,
    the real /proc shows no processes, or an entry of this user could not be read for a
    reason other than having just exited); callers that must not guess fail closed on it."""
    root = Path(proc or live_proc_dir())
    result = {'running': False, 'accessible': False, 'error': ''}
    try:
        entries = list(root.iterdir())
    except OSError as error:
        result['error'] = 'cannot list %s: %s' % (root, error)
        return result
    numeric = [e for e in entries if e.name.isdigit()]
    if not numeric and os.path.realpath(str(root)) == '/proc':
        result['error'] = 'no processes are visible in /proc'
        return result
    uid = os.getuid()
    for entry in numeric:
        try:
            if entry.stat().st_uid != uid:
                continue
            try:
                comm = (entry / 'comm').read_text(errors='replace').strip()
            except FileNotFoundError:
                comm = ''  # tests build /proc look-alikes with only a cmdline
            argv = (entry / 'cmdline').read_bytes().split(b'\0')
        except (FileNotFoundError, ProcessLookupError, NotADirectoryError):
            continue  # the process exited while we looked
        except OSError as error:
            result['error'] = result['error'] or 'cannot read %s: %s' % (entry, error)
            continue
        program = os.path.basename(argv[0].decode('utf-8', 'replace')) if argv else ''
        if comm not in STEAM_PROCESS_NAMES and program not in STEAM_PROCESS_NAMES:
            continue
        result['running'] = True
        args = [a.decode('utf-8', 'replace') for a in argv[1:]]
        if any(a in GUI_ACCESS_ARGS or a.startswith('--force-renderer-accessibility') for a in args):
            result['accessible'] = True
    return result


def live_steam_running(proc=None):
    """True when this user has a Steam process (see steam_process_scan). Informational:
    it answers False when /proc cannot be read; destructive callers use steam_process_scan."""
    return steam_process_scan(proc)['running']


def live_connect_info(timeout=LIVE_TIMEOUT):
    """{'port', 'targets', 'version'} for the first debug port that answers, or
    raises LiveProbeError with a one-line explanation."""
    if not live_enabled():
        raise LiveProbeError('live checks are disabled (LUNA_LIVE_CHECKS=0)')
    ports = live_candidate_ports()
    if not ports:
        if not live_steam_running():
            raise LiveProbeError('Steam is not running, so there is no client to probe')
        raise LiveProbeError('no DevTools debug port found: Steam runs, but neither Lumen\'s cef_port '
                             'nor a --remote-debugging-port argument is present (is Lumen active?)')
    problems = []
    for port in ports:
        try:
            targets = live_http_json(port, '/json/list', timeout)
        except (OSError, ValueError) as error:
            problems.append('port %d: %s' % (port, getattr(error, 'reason', None) or error))
            continue
        if isinstance(targets, list):
            return {'port': port, 'targets': [t for t in targets if isinstance(t, dict)]}
        problems.append('port %d: unexpected /json/list answer' % port)
    if not live_steam_running():
        raise LiveProbeError('Steam is not running (the stale debug port %s does not answer)'
                             % ', '.join(str(p) for p in ports))
    raise LiveProbeError('the DevTools debug port is unavailable (%s)' % '; '.join(problems))


def live_pick_target(targets, title=LIVE_TARGET_TITLE):
    for target in targets:
        if target.get('type') == 'page' and target.get('title') == title and target.get('webSocketDebuggerUrl'):
            return target
    if title != LIVE_TARGET_TITLE:
        return None
    for target in targets:  # title can lag behind while the page loads: fall back to its URL
        if (target.get('type') == 'page' and target.get('webSocketDebuggerUrl')
                and 'steamloopback.host/index.html' in str(target.get('url', ''))):
            return target
    return None


# --------------------------------------------------------- minimal WebSocket

class LiveSocket:
    """Just enough RFC 6455 for CDP: handshake, masked text frames out, text
    frames in (fragmentation, ping/pong and close handled)."""

    def __init__(self, url, timeout=LIVE_TIMEOUT):
        match = re.match(r'^ws://([^/:]+):(\d+)(/.*)$', url or '')
        if not match or match.group(1) not in (LIVE_HOST, 'localhost'):
            raise LiveProbeError('refusing a non-local DevTools address')
        self.timeout = timeout
        host, port, path = match.group(1), int(match.group(2)), match.group(3)
        try:
            self.sock = socket.create_connection((LIVE_HOST, port), timeout=timeout)
        except OSError as error:
            raise LiveProbeError('could not connect to the DevTools port %d: %s' % (port, error))
        self.buffer = b''
        key = base64.b64encode(os.urandom(16)).decode()
        try:
            self.sock.sendall(('GET %s HTTP/1.1\r\nHost: %s:%d\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n'
                               'Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n'
                               % (path, host, port, key)).encode())
            head = self._read_until(b'\r\n\r\n')
        except OSError as error:
            self.close()
            raise LiveProbeError('DevTools handshake failed: %s' % error)
        lines = head.decode('latin-1').split('\r\n')
        accept = base64.b64encode(hashlib.sha1((key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
        headers = {k.strip().lower(): v.strip() for k, _, v in (l.partition(':') for l in lines[1:]) if k}
        if ' 101 ' not in lines[0] or headers.get('sec-websocket-accept') != accept:
            self.close()
            raise LiveProbeError('DevTools refused the WebSocket upgrade (%s)' % lines[0][:60])

    def _read_until(self, marker):
        while marker not in self.buffer:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise OSError('connection closed')
            self.buffer += chunk
            if len(self.buffer) > LIVE_MAX_MESSAGE:
                raise OSError('message too large')
        head, _, self.buffer = self.buffer.partition(marker)
        return head

    def _read_exact(self, count):
        while len(self.buffer) < count:
            chunk = self.sock.recv(max(65536, count - len(self.buffer)))
            if not chunk:
                raise OSError('connection closed')
            self.buffer += chunk
        data, self.buffer = self.buffer[:count], self.buffer[count:]
        return data

    def _send_frame(self, opcode, payload):
        header = bytes([0x80 | opcode])
        size = len(payload)
        if size < 126:
            header += bytes([0x80 | size])
        elif size < 65536:
            header += bytes([0x80 | 126]) + struct.pack('>H', size)
        else:
            header += bytes([0x80 | 127]) + struct.pack('>Q', size)
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(header + mask + masked)

    def send_text(self, text):
        self._send_frame(0x1, text.encode('utf-8'))

    def recv_text(self, deadline):
        """The next complete text message; raises LiveProbeError on timeout/close."""
        message = b''
        try:
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise socket.timeout()
                self.sock.settimeout(left)
                first, second = self._read_exact(2)
                opcode, length = first & 0x0F, second & 0x7F
                if length == 126:
                    length = struct.unpack('>H', self._read_exact(2))[0]
                elif length == 127:
                    length = struct.unpack('>Q', self._read_exact(8))[0]
                if length > LIVE_MAX_MESSAGE:
                    raise LiveProbeError('DevTools sent an oversized message')
                mask = self._read_exact(4) if second & 0x80 else None
                payload = self._read_exact(length)
                if mask:
                    payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
                if opcode == 0x8:
                    raise LiveProbeError('DevTools closed the connection')
                if opcode == 0x9:
                    self._send_frame(0xA, payload)
                    continue
                if opcode == 0xA:
                    continue
                message += payload
                if len(message) > LIVE_MAX_MESSAGE:
                    raise LiveProbeError('DevTools sent an oversized message')
                if first & 0x80:
                    return message.decode('utf-8', 'replace')
        except socket.timeout:
            raise LiveProbeError('DevTools did not answer in time')
        except OSError as error:
            raise LiveProbeError('DevTools connection failed: %s' % error)

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


# ------------------------------------------------------------------ evaluation

# Named read-only expressions. Each only reads state of the page; the library ones
# are documented where they are used. Never add anything here that assigns, calls a
# setter, dispatches an event or navigates.
LIVE_READ_EXPRESSIONS = {
    # Is Lumen's RPC bridge present, and did the Luna agent run (window.__SlsLunaMenu)?
    'bridge': '''(() => ({
        rpc: typeof (window.Millennium && window.Millennium.callServerMethod) === 'function',
        lunaMenu: !!window.__SlsLunaMenu,
        lunaSort: (window.__SlsLunaMenu && window.__SlsLunaMenu.sort) ? {
          value: window.__SlsLunaMenu.sort.value, seen: window.__SlsLunaMenu.sort.seen,
          games: window.__SlsLunaMenu.sort.games, order: window.__SlsLunaMenu.sort.order || null } : null,
        href: String(location.href).split('?')[0] }))()''',
    # The Library list's own row model, read from the mounted list component (main
    # window). `visibleRows` is the exact array its row renderer indexes into, so
    # this is the displayed order of the whole list, not just the on-screen rows.
    # Found through the React fiber of the virtualized grid; only getters are read.
    'library': '''(() => {
        const grid = document.querySelector('.ReactVirtualized__Grid');
        if (!grid) return {found: false, reason: 'the Library list is not mounted (open the Library tab)'};
        const key = Object.keys(grid).find(k => k.startsWith('__reactFiber'));
        let fiber = key ? grid[key] : null;
        let list = null;
        for (let depth = 0; fiber && depth < 16 && !list; depth++, fiber = fiber.return) {
          const node = fiber.stateNode;
          if (node && !(node instanceof Element) && typeof node.GetRowCount === 'function'
              && 'visibleRows' in node) list = node;
        }
        if (!list) return {found: false, reason: 'the Library list component was not found (Steam UI changed?)'};
        const rows = Array.from(list.visibleRows || []).map(row => row.bIsCollection
          ? ['c', String(row.strCollectionId), String((row.rgStrNames || [''])[0]), Number(row.nFilteredSize) || 0,
             row.bIsCollapsed ? 1 : 0]
          : ['a', String(row.strCollectionId), String((row.appOverview && row.appOverview.display_name)
              || (row.rgStrNames || [''])[0] || ''), Number(row.appOverview && row.appOverview.appid) || 0]);
        const ls = {};
        for (const name of ['slsLunaSortLive', 'slsLunaSortDiag']) {
          const value = localStorage.getItem(name);
          if (value !== null) ls[name] = value.slice(0, 600);
        }
        return {found: true, rowCount: list.GetRowCount(), rows, storage: ls};
      })()''',
    # The HowLongToBeat-sorted grid as the main window renders it, in rendered order and
    # complete (not just the on-screen capsules). Two mounted shapes exist: the Home shelf
    # (a class instance with m_fnSort whose carousel child receives `childElements`, one
    # React element per game keyed `app_list_<appid>`, observed live 2026-11-16: 1263 for
    # one collection while only ~10 capsules were in the DOM) and the collection grid (props
    # `childSections` with eSortBy, which Luna's wrapper replaces by one sorted section).
    # Only views whose eSortBy is Luna's value 12 are returned; every other mounted view
    # is only counted. Reads fiber props and localStorage; assigns, calls and dispatches nothing.
    'hltb_grid': '''(() => {
        const SORT = 12;
        const views = [], sorts = {}, seen = new Set();
        let mounted = 0;
        const nameFrom = element => {
          let node = element;
          for (let depth = 0; depth < 8 && node && node.props; depth++) {
            const app = node.props.app || node.props.overview;
            if (app && app.appid) return [Number(app.appid), String(app.display_name || '')];
            node = Array.isArray(node.props.children) ? node.props.children[0] : node.props.children;
          }
          return null;
        };
        const kids = fiber => {
          let found = null;
          const budget = {n: 0};
          const walk = f => {
            for (; f && !found && budget.n < 4000; f = f.sibling) {
              budget.n++;
              const p = f.memoizedProps;
              if (p && Array.isArray(p.childElements) && p.childElements.length > 0) { found = p.childElements; return; }
              if (f.child) walk(f.child);
            }
          };
          walk(fiber.child);
          return {list: found, exhausted: !found && budget.n >= 4000};
        };
        for (const el of document.querySelectorAll('*')) {
          const key = Object.keys(el).find(k => k.startsWith('__reactFiber'));
          if (!key) continue;
          let fiber = el[key];
          for (let depth = 0; fiber && depth < 60; depth++, fiber = fiber.return) {
            if (seen.has(fiber)) break;
            seen.add(fiber);
            const p = fiber.memoizedProps, node = fiber.stateNode;
            if (!p || typeof p !== 'object') continue;
            const typeName = (fiber.type && (fiber.type.displayName || fiber.type.name)) || '';
            if (p.showcase && typeof p.showcase.eSortBy === 'number' && p.collectionId !== undefined
                && node && !(node instanceof Element) && typeof node.m_fnSort === 'function') {
              mounted++;
              sorts[p.showcase.eSortBy] = (sorts[p.showcase.eSortBy] || 0) + 1;
              if (p.showcase.eSortBy !== SORT) continue;
              const walked = kids(fiber);
              const rows = [];
              for (const child of walked.list || []) {
                const named = nameFrom(child);
                const id = Number(String(child && child.key || '').replace(/^app_list_/, '')) || (named ? named[0] : 0);
                rows.push([id, named ? named[1] : '']);
              }
              views.push({kind: 'shelf', id: String(p.collectionId), count: rows.length, exhausted: walked.exhausted,
                          total: Number(node.state && node.state.nItemsDisplayed) || null, rows});
            } else if (typeName !== 'SlsLunaHltbGrid' && Array.isArray(p.childSections) && typeof p.eSortBy === 'number') {
              mounted++;
              sorts[p.eSortBy] = (sorts[p.eSortBy] || 0) + 1;
              if (p.eSortBy !== SORT) continue;
              const rows = [];
              for (const section of p.childSections) {
                for (const child of (section && section.children) || []) {
                  const overview = (child && child.m_overview) || child || {};
                  rows.push([Number(overview.appid) || 0, String(overview.display_name || overview.name || '')]);
                }
              }
              views.push({kind: 'grid', id: String(p.collectionId || p.strCollectionId || ''), count: rows.length,
                          exhausted: false, total: null, rows});
            }
          }
        }
        return {mounted, sorts, live: localStorage.getItem('slsLunaSortLive'), views};
      })()''',
}


def live_rpc_expression(name, args=None):
    """JavaScript for one allow-listed RPC. Refuses anything else, in code."""
    if name not in LIVE_RPC_ALLOWLIST:
        raise LiveProbeError('refused: %s is not an allow-listed read-only RPC' % name)
    return ('(async () => { const m = window.Millennium; '
            'if (!m || typeof m.callServerMethod !== "function") return {error: "Millennium.callServerMethod is missing"}; '
            'const out = await Promise.race([Promise.resolve(m.callServerMethod("lumen", %s, %s)), '
            'new Promise((_, no) => setTimeout(() => no(new Error("no answer from the backend")), %d))]); '
            'return {value: out}; })()'
            % (json.dumps(name), json.dumps(args or {}), int(LIVE_TIMEOUT * 1000) - 500))


class LiveSession:
    """One DevTools connection to the SharedJSContext page."""

    def __init__(self, socket_factory=LiveSocket, timeout=LIVE_TIMEOUT, title=LIVE_TARGET_TITLE):
        self.timeout = timeout
        info = live_connect_info(timeout)
        target = live_pick_target(info['targets'], title)
        if target is None:
            raise LiveProbeError('the DevTools port answers but has no %s page yet (Steam is still starting?)'
                                 % title)
        self.port = info['port']
        self.ws = socket_factory(target['webSocketDebuggerUrl'], timeout)
        self.next_id = 0

    def close(self):
        self.ws.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _evaluate(self, expression):
        self.next_id += 1
        ident = self.next_id
        self.ws.send_text(json.dumps({'id': ident, 'method': 'Runtime.evaluate', 'params': {
            'expression': expression, 'returnByValue': True, 'awaitPromise': True,
            'userGesture': False, 'silent': True}}))
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                message = json.loads(self.ws.recv_text(deadline))
            except ValueError:
                continue
            if message.get('id') != ident:
                continue  # an event, or a stale answer
            if 'error' in message:
                raise LiveProbeError('DevTools error: %s' % str((message['error'] or {}).get('message', '?'))[:120])
            result = message.get('result') or {}
            if result.get('exceptionDetails'):
                details = result['exceptionDetails']
                text = ((details.get('exception') or {}).get('description') or details.get('text') or 'exception')
                raise LiveProbeError('the page raised: %s' % str(text).splitlines()[0][:120])
            return (result.get('result') or {}).get('value')

    def read(self, name):
        """Evaluate one of the named LIVE_READ_EXPRESSIONS."""
        if name not in LIVE_READ_EXPRESSIONS:
            raise LiveProbeError('refused: %s is not a known read expression' % name)
        return self._evaluate(LIVE_READ_EXPRESSIONS[name])

    def rpc(self, name, args=None):
        """Call one allow-listed read-only backend RPC. Returns the parsed JSON object;
        raises LiveProbeError when it does not answer."""
        answer = self._evaluate(live_rpc_expression(name, args))
        if not isinstance(answer, dict):
            raise LiveProbeError('%s returned nothing' % name)
        if answer.get('error'):
            raise LiveProbeError(str(answer['error'])[:120])
        value = answer.get('value')
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                raise LiveProbeError('%s returned something that is not JSON' % name)
        if not isinstance(value, dict):
            raise LiveProbeError('%s returned an unexpected shape' % name)
        return value


# ----------------------------------------------------------------- sidecar

def live_sidecar(proc=None):
    """{'pid', 'started'} (epoch seconds) of this user's Lumen sidecar, or None.
    Start time = /proc boot time + the process's starttime ticks."""
    root = Path(proc or live_proc_dir())
    uid = os.getuid()
    try:
        entries = list(root.iterdir())
    except OSError:
        return None
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            if entry.stat().st_uid != uid or (entry / 'comm').read_text(errors='replace').strip() != 'lumen':
                continue
            stat_text = (entry / 'stat').read_text(errors='replace')
            fields = stat_text[stat_text.rindex(')') + 2:].split()
            ticks = int(fields[19])  # field 22 overall; `pid (comm)` precede the split
            btime = next(int(line.split()[1]) for line in (root / 'stat').read_text(errors='replace').splitlines()
                         if line.startswith('btime '))
        except (OSError, ValueError, IndexError, StopIteration):
            continue
        try:
            hertz = os.sysconf('SC_CLK_TCK')
        except (ValueError, OSError):
            hertz = 100
        return {'pid': int(entry.name), 'started': btime + ticks / float(hertz)}
    return None


def live_payload_newest(lumen=None):
    """(mtime, path) of the newest installed file the sidecar loads: boot.lua and
    everything under sls-addons/. (0, '') when nothing is installed."""
    base = Path(lumen or lumen_root())
    newest = (0.0, '')
    candidates = [base / 'lua' / 'boot.lua']
    for folder, _, names in os.walk(str(base / 'sls-addons')):
        candidates.extend(Path(folder) / name for name in names)
    for path in candidates:
        try:
            stamp = path.stat().st_mtime
        except OSError:
            continue
        if stamp > newest[0]:
            newest = (stamp, str(path.relative_to(base)))
    return newest


def live_sidecar_state(proc=None, lumen=None, slack=2.0):
    """{'state': 'absent'|'current'|'stale', 'pid', 'started', 'newest', 'newestPath'}.
    A sidecar started before the newest payload file keeps the old boot.lua and
    backends in memory; the fix is to restart Steam (the sidecar goes with it)."""
    sidecar = live_sidecar(proc)
    newest, path = live_payload_newest(lumen)
    if not sidecar:
        return {'state': 'absent', 'newest': newest, 'newestPath': path}
    state = 'stale' if newest > sidecar['started'] + slack else 'current'
    return dict(sidecar, state=state, newest=newest, newestPath=path)


# --------------------------------------------------------------- status --live

def live_stamp(value):
    return time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(value)) if value else '?'


def live_addon_report(session, addon):
    """(answers, healthy, detail) for one add-on through its allow-listed health RPC."""
    method = LIVE_ADDON_PROBES.get(addon)
    if method is None:
        return None, None, 'no read-only health RPC'
    try:
        payload = session.rpc(method)
    except LiveProbeError as error:
        return False, False, 'no answer: %s' % error
    if payload.get('success') is False:
        return True, False, 'backend error: %s' % str(payload.get('error', '?'))[:80]
    facts, problems = [], []
    running = payload.get('version')
    if addon == 'luna':
        health = payload.get('health') or {}
        running = health.get('version', running)
        drift = (health.get('install') or {}).get('drift') or []
        if drift:
            problems.append('install drift: %s' % '; '.join(str(d) for d in drift[:2]))
        facts.append('%d add-on(s) listed' % len(((health.get('lumen') or {}).get('addons')) or []))
    elif addon == 'sgdb':
        facts.append('API key %s' % ('set' if payload.get('hasKey') else 'not set'))
        if payload.get('gridError'):
            problems.append('grid dir: %s' % str(payload['gridError'])[:60])
    elif addon == 'graphics':
        facts.append('%d GPU(s)' % len(payload.get('gpus') or []))
    elif addon in ('playtime', 'presence'):
        runtime = payload.get('runtime') or {}
        if runtime.get('healthy') is True:
            facts.append('agent reports healthy')
        else:
            problems.append('agent reports unhealthy%s' % (': %s' % runtime['error'] if runtime.get('error') else
                                                            ' (no report yet)' if not runtime.get('healthy') else ''))
        if addon == 'presence':
            try:
                probe = session.rpc('SlsPresenceContractProbe', {'raw': '{"a":1}'})
                facts.append('contract %s' % ('ok' if probe.get('argType') == 'string' and probe.get('decoded') else 'MISMATCH'))
                if not (probe.get('argType') == 'string' and probe.get('decoded')):
                    problems.append('RPC argument contract changed')
            except LiveProbeError as error:
                problems.append('contract probe: %s' % error)
    disk = None
    try:
        disk = json.loads((addons_dir() / addon / 'plugin.json').read_text(errors='replace')).get('version')
    except (OSError, ValueError):
        pass
    if running:
        facts.insert(0, 'v%s' % running)
        if disk and str(disk) != str(running):
            problems.append('running v%s but v%s is installed' % (running, disk))
    return True, not problems, '; '.join(facts + problems) or 'answers'


def live_collect(addons, proc=None, lumen=None):
    """Everything `status --live` shows, as one dict. Never raises."""
    report = {'available': False, 'reason': '', 'port': None, 'addons': {}, 'bridge': None, 'degraded': False}
    report['sidecar'] = live_sidecar_state(proc, lumen)
    boot = (Path(lumen) if lumen else lumen_root()) / 'lua' / 'boot.lua'
    try:
        report['bootMarkers'] = boot.read_text(errors='replace').count(MARKER)
    except OSError:
        report['bootMarkers'] = 0
    try:
        session = LiveSession()
    except LiveProbeError as error:
        report['reason'] = str(error)
        return report
    except Exception as error:  # a broken socket must never become a traceback
        report['reason'] = 'live probe failed: %s' % error
        return report
    try:
        with session:
            report['available'] = True
            report['port'] = session.port
            try:
                report['bridge'] = session.read('bridge')
            except LiveProbeError as error:
                report['bridge'] = {'error': str(error)}
            for addon in addons:
                report['addons'][addon] = dict(zip(('answers', 'healthy', 'detail'), live_addon_report(session, addon)))
    except LiveProbeError as error:
        report['reason'] = str(error)
        report['available'] = False
    except Exception as error:
        report['reason'] = 'live probe failed: %s' % error
        report['available'] = False
    return report


def live_boot_line(report):
    markers = report.get('bootMarkers', 0)
    bridge = report.get('bridge') or {}
    answered = any(item.get('answers') for item in report['addons'].values())
    if not markers:
        return ('boot.lua is unpatched on disk%s' % ('; the running process still serves add-ons '
                '(it predates the unpatch: restart Steam)' if answered else ''), False)
    if not answered:
        return ('marker x%d on disk, NOT reflected: no add-on backend answers in the running Lumen '
                '(restart Steam; check lumen logs)' % markers, False)
    if not bridge.get('rpc') or not bridge.get('lunaMenu'):
        return ('marker x%d on disk, backends answer, but the front end is missing (%s)'
                % (markers, 'no Millennium RPC bridge' if not bridge.get('rpc') else 'window.__SlsLunaMenu absent'), False)
    return ('marker x%d on disk, reflected in the running process (backends answer, RPC bridge and '
            'window.__SlsLunaMenu present)' % markers, True)


def live_rendered_column(addons):
    """{addon: text} for the 'rendered' column. Read-only: navigation stays off."""
    reason = gui_unavailable_reason()
    if reason:
        return {a: 'n/a (%s)' % reason for a in addons}
    try:
        results = gui_rendered(list(addons), navigate=False)
    except Exception as error:
        return {a: 'n/a (%s)' % error for a in addons}
    column = {}
    for addon in addons:
        item = results.get(addon) or {}
        if addon not in GUI_PANEL_SELECTORS:
            column[addon] = 'n/a (no GUI panel selector)'
        elif item.get('state') == 'skipped':
            column[addon] = 'n/a (%s; run luna gui verify)' % (item.get('reason') or 'skipped')
        else:
            column[addon] = '%s (%d/%d)' % (item.get('state', '?'), item.get('hits', 0), item.get('samples', 0))
    return column


def print_live_status(targets, installed, proc=None, lumen=None):
    """The `status --live` section. Returns True when something is degraded."""
    probed = [a for a in targets if a in installed]
    report = live_collect(probed, proc, lumen)
    degraded = False
    sidecar = report['sidecar']
    print()
    if report['available']:
        print('live       : DevTools on 127.0.0.1:%d, SharedJSContext reachable' % report['port'])
    else:
        print('live       : unavailable - %s' % report['reason'])
    if sidecar['state'] == 'absent':
        print('sidecar    : no Lumen process running')
    else:
        verdict = 'up to date'
        if sidecar['state'] == 'stale':
            verdict = ('STALE - installed file %s changed at %s, after the sidecar started; it still runs the '
                       'old code (restart Steam)' % (sidecar['newestPath'], live_stamp(sidecar['newest'])))
            degraded = True
        print('sidecar    : lumen pid %d started %s; newest installed file %s - %s'
              % (sidecar['pid'], live_stamp(sidecar['started']), live_stamp(sidecar['newest']), verdict))
    if not report['available']:
        return degraded
    line, ok = live_boot_line(report)
    print('boot.lua   : %s' % line)
    degraded = degraded or not ok
    if probed:
        rendered = live_rendered_column(probed)
        print('%-10s %-9s %-52s %s' % ('add-on', 'backend', 'health', 'rendered'))
        for addon in probed:
            item = report['addons'].get(addon) or {}
            answers = item.get('answers')
            backend = 'n/a' if answers is None else 'answers' if answers else 'SILENT'
            health = item.get('detail', '')
            print('%-10s %-9s %-52s %s' % (addon, backend, health[:52] + ('...' if len(health) > 52 else ''),
                                           rendered.get(addon, '')))
            if len(health) > 52:
                print('%-10s %-9s   %s' % ('', '', health))
            if answers is False or item.get('healthy') is False:
                degraded = True
    return degraded


# ------------------------------------------------------------ library order

def live_library_order(session=None):
    """The complete Library list, in the order Steam displays it, read from the
    running client (the mounted list component's own row model: `visibleRows`).

    Returns {'names': [...], 'rendered': n, 'total': n, 'virtualized': False,
    'unique': n, 'sections': [{'id','name','size','collapsed','start'}], 'rows': n,
    'source': ..., 'sort': {...}}. `names` lists every game row in display order
    (a game filed under two collections appears under both), with no section
    headers. `rendered`/`total`/`virtualized` mirror gui_library_order() so both
    reads compare with gui_library_compare().

    What it proves: the order the Library SIDEBAR list displays, for all rows, not
    just those on screen. What it does not prove: the order of a collection grid
    sorted with Luna's HowLongToBeat option (value 12); that grid is rendered
    per page and the sidebar never uses it. `sort` carries what Luna and Steam
    report about the sort state so it can be compared across a restart alongside
    the order. Raises LiveProbeError when the Library is not mounted."""
    owner = session is None
    session = session or LiveSession(title=LIVE_MAIN_TITLE)
    try:
        data = session.read('library')
    finally:
        if owner:
            session.close()
    if not isinstance(data, dict) or not data.get('found'):
        raise LiveProbeError((data or {}).get('reason') or 'the Library list could not be read')
    names, sections = [], []
    for row in data.get('rows') or []:
        if row and row[0] == 'c':
            sections.append({'id': row[1], 'name': row[2], 'size': row[3], 'collapsed': bool(row[4]),
                             'start': len(names)})
        elif row and row[0] == 'a':
            names.append(str(row[2]))
    if not names:  # still loading (or unreadable): an empty list proves nothing, so it is never a result
        raise LiveProbeError('the Library list is mounted but shows no games yet (still loading?)')
    sort = {'stored': {}, 'luna': None}
    for key, value in (data.get('storage') or {}).items():
        try:
            sort['stored'][key] = json.loads(value)
        except ValueError:
            sort['stored'][key] = value
    if owner:
        try:
            with LiveSession() as shared:
                sort['luna'] = (shared.read('bridge') or {}).get('lunaSort')
        except LiveProbeError:
            pass
    return {'names': names, 'rendered': len(names), 'total': len(names), 'virtualized': False,
            'unique': len(set(names)), 'rows': len(data.get('rows') or []), 'sections': sections,
            'source': 'Library list component visibleRows (main window, read-only)', 'sort': sort}


def live_hltb_grid(session=None):
    """The HowLongToBeat-sorted grid(s) the main window renders right now, in rendered order.

    Returns {'mounted': n, 'sorts': {value: views}, 'liveSort': str|None, 'active': bool,
    'views': [{'kind', 'id', 'count', 'appids', 'names'}]} where `views` lists only views
    whose sort is Luna's HowLongToBeat value, one per (kind, collection), each with at least
    one row. `empty` lists sorted views that had no rows ({'kind','id','exhausted'}; `exhausted`
    = the fiber walk ran out of its node budget, so the emptiness is not a fact). `active` is False
    when no mounted view with rows uses it (a read-only fact; nothing is toggled to find out).
    Raises LiveProbeError when the main-window page cannot be read."""
    owner = session is None
    session = session or LiveSession(title=LIVE_MAIN_TITLE)
    try:
        data = session.read('hltb_grid')
    finally:
        if owner:
            session.close()
    if not isinstance(data, dict):
        raise LiveProbeError('the sorted grid could not be read')
    views, empty, taken = [], [], set()
    for view in data.get('views') or []:
        if not isinstance(view, dict):
            continue
        key = (view.get('kind'), view.get('id'))
        if key in taken or not isinstance(view.get('rows'), list):
            continue  # React keeps two fibers per mounted component
        taken.add(key)
        rows = [r for r in view['rows'] if isinstance(r, list) and len(r) >= 2]
        if not rows:  # a sorted view with no rows proves nothing: never part of `views`
            empty.append({'kind': str(view.get('kind')), 'id': str(view.get('id')),
                          'exhausted': bool(view.get('exhausted'))})
            continue
        views.append({'kind': str(view.get('kind')), 'id': str(view.get('id')), 'count': len(rows),
                      'appids': [int(r[0] or 0) for r in rows], 'names': [str(r[1] or '') for r in rows]})
    return {'mounted': int(data.get('mounted') or 0), 'sorts': dict(data.get('sorts') or {}),
            'liveSort': data.get('live'), 'active': bool(views), 'views': views, 'empty': empty}


def cmd_library_order(options):
    try:
        order = live_library_order()
    except LiveProbeError as error:
        print('live library order unavailable: %s' % error, file=sys.stderr)
        return 1
    except Exception as error:
        print('live library order failed: %s' % error, file=sys.stderr)
        return 1
    code = EXIT_OK
    if getattr(options, 'save', None):
        if not order['names']:  # unreachable while live_library_order raises on empty, kept as a guard
            print('FAIL: the Library list is empty; refusing to save an empty baseline', file=sys.stderr)
            return 1
        try:
            gui_private_write_json(Path(options.save), dict(order, savedAt=time.strftime('%Y-%m-%dT%H:%M:%S')))
        except OSError as problem:
            print('could not save: %s' % problem, file=sys.stderr)
            return 1
    if getattr(options, 'compare', None):
        saved = read_json_file(Path(options.compare))
        if not isinstance(saved, dict) or not isinstance(saved.get('names'), list):
            print('FAIL: %s is not a saved library order' % options.compare, file=sys.stderr)
            return 1
        same, lines = gui_library_compare(saved['names'], order['names'])
        if same:
            print('library order matches %s (%d rows)' % (options.compare, len(order['names'])))
        else:
            print('FAIL: library order differs from %s' % options.compare, file=sys.stderr)
            print('\n'.join(lines), file=sys.stderr)
            code = 1
        if saved.get('sort') != order['sort']:
            print('note: the sort state differs from the saved one (saved %s, now %s)'
                  % (json.dumps(saved.get('sort'), sort_keys=True), json.dumps(order['sort'], sort_keys=True)))
    if getattr(options, 'json', False):
        print(json.dumps(order, indent=2, ensure_ascii=False))
    elif not getattr(options, 'compare', None) or code:
        print('library order (%d game rows, %d unique, %d sections; source: %s)'
              % (len(order['names']), order['unique'], len(order['sections']), order['source']))
        print('sort state : %s' % json.dumps(order['sort'], sort_keys=True))
        limit = getattr(options, 'limit', 40)
        for number, name in enumerate(order['names'][:limit] if limit else order['names'], 1):
            print('%5d  %s' % (number, name))
        if limit and len(order['names']) > limit:
            print('  ... %d more (use --limit 0, --json or --save FILE)' % (len(order['names']) - limit))
    if getattr(options, 'save', None):
        print('saved   : %s' % options.save)
    return code
