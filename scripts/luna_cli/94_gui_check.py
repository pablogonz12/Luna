# --- optional GUI checks -----------------------------------------------------
# Drives the `agent-desktop` CLI (AT-SPI accessibility tree) to see what the
# Steam client actually shows. Everything here degrades to "GUI checks skipped:
# <reason>" when the tool, the graphical session or an accessible Steam is
# missing, and none of it runs unless asked for (the shutdown diagnosis is the
# one exception and is a silent best-effort no-op). Set LUNA_GUI_CHECKS=0 to
# switch the whole layer off.

GUI_TOOL = 'agent-desktop'
GUI_APP = 'steamwebhelper'
GUI_TIMEOUT = 8
# `steam` takes the first flag and maps it to the second on steamwebhelper.
GUI_ACCESS_ARGS = ('-cef-force-accessibility', '--force-renderer-accessibility')

# Add-on selectors, ONE table, as (role, name, exact) matched case-insensitively.
# Steam's client is one big webarea; the Luna add-on sits in its menubar as
# `button 'Luna'` (always present, entry point) and opens an overlay whose nav
# tabs are groups with the class `sls-luna-tab`. Opening an add-on's page shows
# `heading '<Name>'`. Confirmed on a live signed-in client (2026-11-15).
GUI_LUNA_ENTRY = ('button', 'Luna', True)        # confirmed live (native id sls-luna-btn)
GUI_LUNA_OVERLAY = ('statictext', 'LUNA', True)  # confirmed live: overlay nav header, absent when closed
GUI_LUNA_CLOSE = ('button', '\u2715', True)       # confirmed live: closes the overlay page
GUI_TAB_CLASS = 'sls-luna-tab'                   # confirmed live: dom class of each overlay nav tab
GUI_PANEL_SELECTORS = {
    # addon: entry (always visible) and/or nav (tab label to click, navigation only) + panel (seen after the nav click)
    'luna': {'entry': GUI_LUNA_ENTRY, 'panel': GUI_LUNA_OVERLAY},                  # confirmed live
    'playtime': {'nav': 'Playtime', 'panel': ('heading', 'Playtime', True)},       # confirmed live
    'presence': {'nav': 'Presence', 'panel': ('heading', 'Presence', True)},       # confirmed live
    'compressor': {'nav': 'Compressor', 'panel': ('heading', 'Compressor', True)},  # confirmed live
    'sgdb': {'nav': 'SteamGridDB', 'panel': ('heading', 'SteamGridDB artwork', True)},   # confirmed live
    'graphics': {'nav': 'Graphics', 'panel': ('heading', 'Graphics (read-only)', True)},  # confirmed live
    # Pages of the Luna core add-on (not add-ons of their own): checked by `gui watch`,
    # never by verify, since ADDONS does not list them.
    'hltb': {'nav': 'HowLongToBeat', 'panel': ('heading', 'HowLongToBeat', True)},       # confirmed live
    'health': {'nav': 'Health', 'panel': ('heading', 'Health (local diagnostics)', True)},  # confirmed live
    'settings': {'nav': 'Settings', 'panel': ('heading', 'Settings', True)},             # confirmed live
    'about': {'nav': 'About', 'panel': ('heading', 'About SLS Luna', True)},             # confirmed live
}
# Library list: a ReactVirtualized grid, so only the rendered rows exist in the tree.
GUI_LIBRARY_TABLE = ('table', 'grid', True)      # confirmed live; rows are `cell '<game name>'`
GUI_LIBRARY_TOTAL = re.compile(r'^All Games \((?:\d+/)?(\d+)\)$')  # shelf header, e.g. 'All Games (9/1630)'
GUI_LIBRARY_TAB = ('statictext', 'LIBRARY', True)  # confirmed live: click it to open the Library
# STILL UNVERIFIED: a modal dialog with an `alert`/`dialog` role. Re-tried live 2026-11-16: menu
# items refuse semantic activation (POLICY_DENIED), `--headed`/`press` need a physical input
# device that Wayland does not give agent-desktop, so no modal could be opened by navigation.
# What IS confirmed: Steam's windows (Settings, Friends List, ...) are separate AT-SPI windows
# with `webarea '<title>'` and niri titles, never a dialog role (see gui_secondary_windows).
GUI_DIALOG_ROLES = ('alert', 'dialog', 'alertdialog')


def gui_enabled():
    return os.environ.get('LUNA_GUI_CHECKS', '1') != '0'


def gui_session_env(environ=None, runtime_dir=None, x11_dir='/tmp/.X11-unix'):
    """A copy of the environment with the display variables Steam and
    agent-desktop need. Values already set are never overridden; missing ones
    are resolved from $XDG_RUNTIME_DIR and the X11 socket directory."""
    env = dict(os.environ if environ is None else environ)
    runtime = Path(runtime_dir or env.get('XDG_RUNTIME_DIR') or '/run/user/%d' % os.getuid())
    try:
        if not env.get('WAYLAND_DISPLAY'):
            sockets = sorted(p.name for p in runtime.glob('wayland-*') if not p.name.endswith('.lock'))
            if sockets:
                env['WAYLAND_DISPLAY'] = sockets[0]
        if not env.get('NIRI_SOCKET'):
            sockets = sorted(runtime.glob('niri.*.sock'))
            if sockets:
                env['NIRI_SOCKET'] = str(sockets[0])
        if not env.get('DISPLAY'):
            sockets = sorted(p.name for p in Path(x11_dir).glob('X[0-9]*'))
            if sockets:
                env['DISPLAY'] = ':' + sockets[0][1:]
    except OSError:
        pass
    return env


def gui_have_session(env):
    return bool(env.get('WAYLAND_DISPLAY') or env.get('DISPLAY'))


def gui_find_tool(name):
    found = shutil.which(name)
    if found:
        return found
    candidate = home() / '.local/bin' / name
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    return None


def gui_run(args, timeout=GUI_TIMEOUT, env=None):
    """Run `agent-desktop ARGS`. Returns {'ok', 'data', 'error', 'unavailable'};
    never raises. `unavailable` is set when the binary is missing or unusable."""
    tool = gui_find_tool(GUI_TOOL)
    if not tool:
        return {'ok': False, 'data': None, 'error': '%s is not installed' % GUI_TOOL, 'unavailable': True}
    try:
        result = subprocess.run([tool] + list(args), capture_output=True, text=True,
                                timeout=timeout, env=env or gui_session_env())
    except subprocess.TimeoutExpired:
        return {'ok': False, 'data': None, 'error': '%s timed out' % GUI_TOOL, 'unavailable': False}
    except (OSError, subprocess.SubprocessError) as error:
        return {'ok': False, 'data': None, 'error': str(error), 'unavailable': True}
    try:
        envelope = json.loads(result.stdout)
    except ValueError:
        envelope = None
    if not isinstance(envelope, dict):
        return {'ok': False, 'data': None, 'unavailable': False,
                'error': (result.stderr.strip() or 'no JSON envelope from %s' % GUI_TOOL)[:200]}
    if envelope.get('ok'):
        return {'ok': True, 'data': envelope.get('data'), 'error': None, 'unavailable': False}
    error = envelope.get('error')
    code = error.get('code') if isinstance(error, dict) else error
    message = error.get('message') if isinstance(error, dict) else ''
    return {'ok': False, 'data': None, 'unavailable': code == 'PLATFORM_NOT_SUPPORTED',
            'error': ('%s %s' % (code or 'error', message or '')).strip()[:200]}


def gui_desktop_name(env=None):
    env = env or os.environ
    return (env.get('XDG_CURRENT_DESKTOP') or env.get('XDG_SESSION_DESKTOP') or env.get('DESKTOP_SESSION')
            or 'this desktop')


def gui_backend(env=None):
    """Which window backend is in use. Two exist: 'niri' (tested: real window titles and
    window-only capture) and 'generic' (agent-desktop list-windows, no capture). No other
    compositor is guessed at: there, capture says "not supported on <desktop>: <why>"."""
    env = env or gui_session_env()
    if shutil.which('niri') and env.get('NIRI_SOCKET'):
        return {'name': 'niri', 'windows': 'niri msg --json windows',
                'capture': 'niri msg action screenshot-window', 'note': ''}
    why = 'niri is not installed' if not shutil.which('niri') else 'no NIRI_SOCKET (not a niri session)'
    return {'name': 'generic', 'windows': 'agent-desktop list-windows (window titles are empty)',
            'capture': None,
            'note': 'not supported on %s: window-only screenshots need niri (%s)' % (gui_desktop_name(os.environ), why)}


def gui_windows(timeout=GUI_TIMEOUT, env=None):
    """Open Steam windows as [{'title', 'source'}]. niri first (real titles),
    agent-desktop as the fallback (titles are empty strings there)."""
    env = env or gui_session_env()
    niri = shutil.which('niri')
    if gui_backend(env)['name'] == 'niri':
        try:
            result = subprocess.run([niri, 'msg', '--json', 'windows'], capture_output=True,
                                    text=True, timeout=timeout, env=env)
            windows = json.loads(result.stdout) if result.returncode == 0 else None
        except (OSError, subprocess.SubprocessError, ValueError):
            windows = None
        if isinstance(windows, list):
            return [{'title': str(w.get('title') or ''), 'source': 'niri'}
                    for w in windows if isinstance(w, dict) and w.get('app_id') == 'steam']
    answer = gui_run(['list-windows', '--app', GUI_APP], timeout=timeout, env=env)
    if answer['ok'] and isinstance(answer['data'], list):
        return [{'title': str(w.get('title') or ''), 'source': 'agent-desktop'}
                for w in answer['data']
                if isinstance(w, dict) and str(w.get('app_name', '')).lower() == GUI_APP]
    return []


def gui_titles(windows):
    return [w['title'] for w in windows if w.get('title')]


def gui_walk(node):
    """Every node of a snapshot tree (depth-first)."""
    stack = [node]
    while stack:
        current = stack.pop()
        if not isinstance(current, dict):
            continue
        yield current
        stack.extend(reversed(current.get('children') or []))


def gui_find_nodes(tree, role=None, name=None, exact=True):
    """Nodes matching role and name (case-insensitive). Refs change on every
    snapshot, so callers search by role+name each time and never store refs."""
    wanted = (name or '').lower()
    found = []
    for node in gui_walk(tree):
        if role and str(node.get('role', '')).lower() != role:
            continue
        label = str(node.get('name') or '').lower()
        if name is not None and not (label == wanted if exact else wanted in label):
            continue
        found.append(node)
    return found


def gui_snapshot(surface=None, timeout=GUI_TIMEOUT, env=None, scope='main', deadline=None, depth=None):
    """(tree or None, error text). Steam exposes many AT-SPI windows under
    steamwebhelper (main window, content, one per popup menu), so a plain
    `snapshot --app` is ambiguous: list them and snapshot by window id.
    scope 'main' reads the largest window (where the menubar, Library and the
    Luna overlay live); 'all' merges every window under one synthetic root."""
    def budget():
        return timeout if deadline is None else max(0.3, min(timeout, deadline - time.monotonic()))
    listing = gui_run(['list-windows', '--app', GUI_APP], timeout=budget(), env=env)
    windows = []
    if listing['ok'] and isinstance(listing['data'], list):
        windows = [w for w in listing['data'] if isinstance(w, dict) and w.get('id')]
        windows.sort(key=lambda w: -(float((w.get('bounds') or {}).get('width') or 0)
                                      * float((w.get('bounds') or {}).get('height') or 0)))
        windows = windows[:1] if scope == 'main' else windows[:20]
    base = ['snapshot', '--app', GUI_APP] + (['--surface', surface] if surface else []) \
        + (['--max-depth', str(depth)] if depth else [])
    base += ['--timeout-ms', '15000']  # a short snapshot timeout makes refs 'ambiguous' on big windows
    trees, error = [], listing['error']
    for window in windows or [None]:
        if deadline is not None and trees and time.monotonic() >= deadline:
            break
        answer = gui_run(base + (['--window-id', window['id']] if window else []), timeout=budget(), env=env)
        if not answer['ok']:
            error = answer['error']
            continue
        tree = (answer['data'] or {}).get('tree') if isinstance(answer['data'], dict) else None
        if isinstance(tree, dict):
            trees.append(tree)
    if not trees:
        return None, error or 'snapshot had no tree'
    return (trees[0] if len(trees) == 1 else {'role': 'root', 'children': trees}), None


def gui_node_actions(node):
    return node.get('available_actions') or []


def gui_click_node(node, timeout=GUI_TIMEOUT):
    """Semantic click on a node found in a fresh snapshot (navigation only!)."""
    ref = node.get('ref_id') if isinstance(node, dict) else None
    if not ref:
        return {'ok': False, 'error': 'node has no ref'}
    return gui_run(['click', ref, '--timeout-ms', '15000'], timeout=max(timeout, 20))


def gui_find_tab(tree, name):
    """The Luna overlay nav tab (a group with the sls-luna-tab class) whose label is `name`."""
    for node in gui_walk(tree):
        if GUI_TAB_CLASS in (node.get('dom_classes') or []) and gui_find_nodes(node, 'statictext', name):
            return node
    return None


def gui_overlay_open(tree):
    return bool(gui_find_nodes(tree, GUI_LUNA_OVERLAY[0], GUI_LUNA_OVERLAY[1], GUI_LUNA_OVERLAY[2]))


def gui_dialogs(tree):
    """[{'name', 'buttons'}] for dialog/alert nodes in a tree."""
    dialogs = []
    for node in gui_walk(tree):
        if str(node.get('role', '')).lower() in GUI_DIALOG_ROLES:
            buttons = [str(b.get('name')) for b in gui_find_nodes(node, 'button') if b.get('name')]
            dialogs.append({'name': str(node.get('name') or ''), 'buttons': buttons})
    return dialogs


# Steam keeps its popup menus alive as AT-SPI windows with a named webarea ('Help Root Menu',
# 'Account Menu', '... Supernav', ...). Observed live (2026-11-16): they exist from startup,
# are never niri windows, and stay in the tree after a menu was opened and closed again, so
# they say nothing about an open window.
GUI_POPUP_NAME = re.compile(r'(Root Menu|Supernav|Account Menu|Notifications Menu)$')


def gui_secondary_windows(tree, titles=None):
    """Titles of Steam's own secondary windows (Settings, Friends List, ...). Observed live:
    each is a separate AT-SPI window holding one `webarea '<title>'`; it has no dialog or
    alert role and no Close button of its own (the AT-SPI title is empty, niri has the
    title). The main window's webarea is 'Steam' and the sign-in window's 'Sign in to Steam';
    popup-menu webareas (GUI_POPUP_NAME) are never windows. When the compositor's window
    `titles` are known (niri) only names that also appear there count: a real window is
    always a niri window."""
    found = [str(node.get('name')) for node in gui_walk(tree)
             if node.get('role') == 'webarea' and node.get('name')
             and node.get('name') not in ('Steam', 'Sign in to Steam')
             and not GUI_POPUP_NAME.search(str(node.get('name')))]
    if titles:
        found = [name for name in found if any(name.strip().lower() == t.strip().lower() for t in titles)]
    return found


def gui_classify(tree, titles=()):
    """'login', 'main', 'dialog' or 'unknown'."""
    if any('sign in to steam' in t.lower() for t in titles):
        return 'login'
    if not isinstance(tree, dict):
        return 'unknown'
    if gui_find_nodes(tree, 'webarea', 'Sign in to Steam') or (
            gui_find_nodes(tree, 'button', 'Sign in') and gui_find_nodes(tree, 'textfield', 'PASSWORD')):
        return 'login'
    if gui_dialogs(tree):
        return 'dialog'
    if gui_find_nodes(tree, 'statictext', 'LIBRARY') and gui_find_nodes(tree, 'statictext', 'STORE'):
        return 'main'
    return 'unknown'


# A "Sign in to Steam" window is NOT proof that the user has to sign in: Steam shows the same
# window while it auto-logs-on at startup (observed live: ~37 s from process start to the
# logon response, the main window "Steam" after that). 'login' is therefore only concluded
# once the window has outlasted this grace period with no log-on attempt in connection_log.
GUI_LOGIN_GRACE = 120
GUI_LOG_STAMP = re.compile(r'^\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\]')


def gui_log_epoch(line):
    match = GUI_LOG_STAMP.match(line)
    if not match:
        return None
    try:
        return time.mktime(time.strptime(match.group(1), '%Y-%m-%d %H:%M:%S'))
    except (ValueError, OverflowError):
        return None


def gui_log_tail(name, limit=200000):
    try:
        path = steam_home() / 'logs' / name
        with open(path, 'rb') as handle:
            handle.seek(0, 2)
            handle.seek(max(0, handle.tell() - limit))
            return handle.read().decode('utf-8', 'replace').splitlines()
    except OSError:
        return []


def gui_logon_evidence(since, lines=None):
    """(state, line) from Steam's connection_log.txt for events after `since` (epoch):
    'logged-on', 'logging-on' (a log-on was started or is connecting), 'none' (nothing
    since: the client is not trying to log on) or 'unknown' (no log). The last event wins."""
    lines = gui_log_tail('connection_log.txt') if lines is None else lines
    if not lines:
        return 'unknown', ''
    state, shown = 'none', ''
    for line in lines:
        when = gui_log_epoch(line)
        if when is None or when < since:
            continue
        if 'LogOff()' in line or '[Logged Off' in line and 'ConnectionDisconnected' in line:
            state, shown = 'none', line
        elif ('RecvMsgClientLogOnResponse' in line and "'OK'" in line) or '[Logged On' in line:
            state, shown = 'logged-on', line
        elif ('LogOn() called' in line or '[Connecting' in line) and state != 'logged-on':
            state, shown = 'logging-on', line
    return state, shown[:160]


def gui_process_started(proc='/proc'):
    """Epoch start time of this user's oldest Steam client process, or None."""
    root, uid, best = Path(proc), os.getuid(), None
    try:
        btime = next(int(line.split()[1]) for line in (root / 'stat').read_text().splitlines() if line.startswith('btime '))
        entries = list(root.iterdir())
    except (OSError, StopIteration, ValueError):
        return None
    try:
        hertz = os.sysconf('SC_CLK_TCK')
    except (ValueError, OSError):
        hertz = 100
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            if entry.stat().st_uid != uid:
                continue
            argv = (entry / 'cmdline').read_bytes().split(b'\0')
            if os.path.basename(argv[0].decode('utf-8', 'replace')) not in ('steam', 'steam.sh'):
                continue
            text = (entry / 'stat').read_text()
            started = btime + int(text[text.rindex(')') + 2:].split()[19]) / float(hertz)
        except (OSError, ValueError, IndexError):
            continue
        best = started if best is None else min(best, started)
    return best


def gui_judge_screen(raw, titles, age, grace, logon):
    """(screen, signal): 'main', 'dialog', 'starting', 'login' or 'unknown', and the evidence
    behind the call. `raw` is gui_classify() (or, without a tree, the window titles),
    `age` the seconds since the Steam process started (None = unknown), `logon` the
    gui_logon_evidence() pair. A sign-in window is 'starting' until it has persisted for
    `grace` seconds without any log-on attempt; only then is it the real form."""
    if raw in ('main', 'dialog'):
        return raw, 'main window present (%s)' % ('window titled "Steam"' if 'Steam' in titles else 'accessibility tree')
    if raw == 'login':
        state, line = logon
        if state == 'logged-on':
            return 'starting', 'sign-in window is up but connection_log shows the log-on succeeded: %s' % line
        if state == 'logging-on':
            return 'starting', 'sign-in window is up and connection_log shows a log-on in progress: %s' % line
        if age is None:
            return 'starting', 'sign-in window is up; Steam start time unknown, so it cannot be told from the startup window'
        if age < grace:
            return 'starting', ('sign-in window up at %.0f s after start (grace %.0f s); no log-on attempt in connection_log '
                                'yet (log-on: %s)' % (age, grace, state))
        return 'login', ('sign-in window persisted: Steam up %.0f s (> grace %.0f s) and connection_log shows no '
                         'log-on attempt (%s)' % (age, grace, state))
    return raw, 'no recognisable Steam window'


def gui_exit_hint(since):
    """Why did Steam go away? What its own logs say about the time after `since`."""
    for name, marker in (('console_log.txt', 'StartShutdown'), ('bootstrap_log.txt', '] Shutdown')):
        hits = [line for line in gui_log_tail(name, 100000) if marker in line and (gui_log_epoch(line) or 0) >= since]
        if hits:
            return '%s logged a clean shutdown request: "%s" (something asked Steam to exit; not a crash)' % (name, hits[-1][:100])
    return 'no shutdown line in console_log.txt or bootstrap_log.txt after the launch: a crash or a kill'


def gui_steam_state(proc='/proc'):
    """(running, accessible): whether this user's Steam runs and whether it was
    started with the accessibility flags (read from /proc cmdlines)."""
    running = accessible = False
    try:
        entries = list(Path(proc).iterdir())
    except OSError:
        return False, False
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
        program = os.path.basename(argv[0].decode('utf-8', 'replace')) if argv else ''
        if program not in ('steam', 'steamwebhelper', 'steam.sh'):
            continue
        running = True
        args = [a.decode('utf-8', 'replace') for a in argv[1:]]
        if any(a in GUI_ACCESS_ARGS or a.startswith('--force-renderer-accessibility') for a in args):
            accessible = True
    return running, accessible


def gui_png_size(path):
    """(width, height) from a PNG header, or None."""
    try:
        with open(path, 'rb') as handle:
            head = handle.read(24)
    except OSError:
        return None
    if len(head) < 24 or head[:8] != b'\x89PNG\r\n\x1a\n':
        return None
    return int.from_bytes(head[16:20], 'big'), int.from_bytes(head[20:24], 'big')


def gui_main_window_id(env):
    """(niri id, (width, height)) of the main Steam window, or None. The main window is
    the one titled 'Steam' (else the largest steam window), never a helper or menu."""
    niri = shutil.which('niri')
    if not niri or not env.get('NIRI_SOCKET'):
        return None
    try:
        result = subprocess.run([niri, 'msg', '--json', 'windows'], capture_output=True, text=True,
                                timeout=GUI_TIMEOUT, env=env)
        windows = json.loads(result.stdout) if result.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    found = []
    for window in windows if isinstance(windows, list) else []:
        if not isinstance(window, dict) or window.get('app_id') != 'steam' or window.get('id') is None:
            continue
        size = ((window.get('layout') or {}).get('window_size')) or [0, 0]
        found.append((window.get('title') == 'Steam', size[0] * size[1], window['id'], tuple(size)))
    if not found:
        return None
    best = max(found)
    return best[2], best[3]


GUI_CLIP_PREFER = ('text/plain;charset=utf-8', 'image/png')


def gui_clip_tools():
    return shutil.which('wl-paste'), shutil.which('wl-copy')


def gui_clip_types(env):
    """MIME types on the clipboard: a list ([] = empty), or None when it cannot be read."""
    paste = shutil.which('wl-paste')
    try:
        result = subprocess.run([paste, '--list-types'], capture_output=True, timeout=5, env=env)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode == 0:
        return result.stdout.decode('utf-8', 'replace').split()
    return [] if b'nothing is copied' in result.stderr.lower() else None


def gui_clipboard_save(env):
    """(saved, note). saved is {'type', 'data'} or {'empty': True}, or None when it cannot
    be kept (then `note` says why). niri's screenshot-window overwrites the clipboard."""
    paste, copy = gui_clip_tools()
    if not paste or not copy:
        return None, 'clipboard not restored: wl-clipboard (wl-paste/wl-copy) is not installed'
    types = gui_clip_types(env)
    if types is None:
        return None, 'clipboard not restored: wl-paste could not read it'
    if not types:
        return {'empty': True}, ''
    kind = next((t for t in GUI_CLIP_PREFER if t in types), types[0])
    try:
        result = subprocess.run([paste, '--no-newline', '--type', kind], capture_output=True, timeout=10, env=env)
    except (OSError, subprocess.SubprocessError):
        return None, 'clipboard not restored: wl-paste failed'
    if result.returncode != 0:
        return None, 'clipboard not restored: wl-paste failed'
    return {'type': kind, 'data': result.stdout}, ''


def gui_clipboard_restore(saved, env, sleep=time.sleep):
    """Put `saved` back once niri's image has landed on the clipboard. Returns a note
    ('' when restored)."""
    copy = shutil.which('wl-copy')
    for _ in range(8):  # niri sets the clipboard a moment after the file appears
        if 'image/png' in (gui_clip_types(env) or []):
            break
        sleep(0.2)
    command = [copy, '--clear'] if saved.get('empty') else [copy, '--type', saved['type']]
    try:  # wl-copy forks to serve the data: no inherited pipes, or run() would wait for it
        subprocess.run(command, input=None if saved.get('empty') else saved['data'], timeout=10, env=env,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return 'clipboard not restored: wl-copy failed'
    return ''


def gui_capture(directory, name, full_screen=False):
    """Screenshot as evidence. (path or None, note).

    Default: only the main Steam window, through `niri msg action screenshot-window --path`
    (never anything else on screen). When that is impossible the screenshot is skipped.
    `full_screen` (explicit opt-in) captures everything visible with grim instead."""
    env = gui_session_env()
    target = Path(directory) / name
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        return None, 'screenshot skipped: %s' % error
    if full_screen:
        grim = shutil.which('grim')
        if not grim:
            return None, 'grim not installed; screenshot skipped'
        try:
            result = subprocess.run([grim, str(target)], capture_output=True, text=True, timeout=15, env=env)
        except (OSError, subprocess.SubprocessError) as error:
            return None, 'grim failed: %s' % error
        if result.returncode != 0 or not target.is_file():
            return None, 'grim failed: %s' % (result.stderr.strip()[:120] or 'exit %d' % result.returncode)
        return target, 'saved (full screen)'
    backend = gui_backend(env)
    if backend['name'] != 'niri':
        return None, 'no window-only screenshot: %s (use --full-screen to capture the whole screen)' % backend['note']
    main = gui_main_window_id(env)
    if main is None:
        return None, 'no window-only screenshot: niri or its Steam window is unavailable (use --full-screen to capture the whole screen)'
    window_id, window_size = main
    saved, clip_note = gui_clipboard_save(env)
    try:
        result = subprocess.run([shutil.which('niri'), 'msg', 'action', 'screenshot-window', '--id', str(window_id),
                                 '--path', str(target.resolve())], capture_output=True, text=True,
                                timeout=15, env=env)
    except (OSError, subprocess.SubprocessError) as error:
        return None, 'niri screenshot-window failed: %s' % error
    if result.returncode != 0:
        return None, 'niri screenshot-window failed: %s' % (result.stderr.strip()[:120] or 'exit %d' % result.returncode)
    deadline = time.monotonic() + 5  # the action is asynchronous
    while time.monotonic() < deadline and not (target.is_file() and target.stat().st_size > 0):
        time.sleep(0.2)
    if saved is not None:
        clip_note = gui_clipboard_restore(saved, env)
    size = gui_png_size(target) if target.is_file() else None
    extra = '; ' + clip_note if clip_note else ''
    if size is None:
        return None, 'niri did not write a PNG to %s%s' % (target, extra)
    return target, 'saved, window only, %dx%d px (niri window %dx%d)%s' % (size + tuple(window_size) + (extra,))


def gui_install_hint(stream=None):
    """One line printed after a successful install/update, or ''. Only on a terminal (never
    from the unattended updater or a pipe) and only when the GUI layer could run here. It
    never starts or restarts Steam: it only points at the commands."""
    stream = stream or sys.stdout
    try:
        if not (gui_enabled() and stream.isatty() and gui_find_tool(GUI_TOOL) and gui_have_session(gui_session_env())):
            return ''
    except Exception:
        return ''
    return ('Optional check once Steam is back: luna gui verify (add-on panels; Steam must run in accessible mode, '
            'see luna gui launch) or luna status --live.')


def gui_unavailable_reason(need_accessible=True):
    """Why GUI checks cannot run now, or None when they can."""
    if not gui_enabled():
        return 'disabled (LUNA_GUI_CHECKS=0)'
    if not gui_find_tool(GUI_TOOL):
        return '%s is not installed' % GUI_TOOL
    if not gui_have_session(gui_session_env()):
        return 'no graphical session (no WAYLAND_DISPLAY/DISPLAY)'
    running, accessible = gui_steam_state()
    if not running:
        return 'Steam is not running (start it with: luna gui launch)'
    if need_accessible and not accessible:
        return ('Steam was not started in accessible mode; close it and run: luna gui launch')
    return None


def gui_addons_with_panel(only=None):
    names = [n for n in ADDONS if n in GUI_PANEL_SELECTORS and (addons_dir() / n).is_dir()]
    return [n for n in names if not only or n in only]


def gui_match(tree, selector):
    role, name, exact = selector
    return bool(gui_find_nodes(tree, role, name, exact))


def gui_rendered(addons, samples=3, interval=0.7, sleep=time.sleep, snapshot=None, click=None,
                 navigate=True):
    """Per-add-on 'is its panel rendered' result, reusable by `status --live`.

    Returns {addon: {'state', 'hits', 'samples'}}: found (every sample), unstable
    (some samples: appeared then vanished), missing (none) or skipped (no usable
    snapshot / navigation off; 'reason' says why). The Luna entry point is read
    without touching anything. Add-on pages need the Luna overlay: with
    `navigate` it is opened with one navigation click, each page tab is visited,
    and the overlay is closed again if this call opened it. Never raises."""
    snapshot = snapshot or gui_snapshot
    click = click or gui_click_node

    def take():
        try:
            return snapshot()
        except Exception as error:  # best effort: never break the caller
            return None, str(error)

    def sample(selector, count):
        hits = taken = 0
        reason = None
        for index in range(max(1, count)):
            if index:
                sleep(interval)
            tree, error = take()
            if tree is None:
                reason = error or 'no snapshot'
                continue
            taken += 1
            hits += gui_match(tree, selector)
        return hits, taken, reason

    def verdict(hits, taken, reason):
        if not taken:
            return {'state': 'skipped', 'hits': 0, 'samples': 0, 'reason': reason}
        return {'state': 'found' if hits == taken else 'missing' if not hits else 'unstable',
                'hits': hits, 'samples': taken}

    def skipped(reason):
        return {'state': 'skipped', 'hits': 0, 'samples': 0, 'reason': reason}

    results = {}
    wanted = [a for a in addons if a in GUI_PANEL_SELECTORS]
    tree, error = take()
    if tree is None:
        return {a: skipped(error or 'no snapshot') for a in addons}
    entry_hits = {}
    if 'luna' in wanted:
        entry_hits['luna'] = sample(GUI_LUNA_ENTRY, samples)
    pages = [a for a in wanted if 'nav' in GUI_PANEL_SELECTORS[a]]
    opened_by_us = False
    try:
        if pages and not navigate:
            for name in pages:
                results[name] = skipped('navigation disabled')
        elif pages:
            tree, _ = take()
            if tree is not None and not gui_overlay_open(tree):
                button = (gui_find_nodes(tree, GUI_LUNA_ENTRY[0], GUI_LUNA_ENTRY[1]) or [None])[0]
                if button is not None and click(button).get('ok'):
                    opened_by_us = True
                    for _ in range(6):
                        sleep(0.5)
                        tree, _ = take()
                        if tree is not None and gui_overlay_open(tree):
                            break
            if tree is None or not gui_overlay_open(tree):
                for name in pages:
                    results[name] = skipped('the Luna overlay did not open')
            else:
                for name in pages:
                    label = GUI_PANEL_SELECTORS[name]['nav']
                    tab = gui_find_tab(tree, label)
                    # click the label inside the tab: the tab group itself is refused as
                    # ambiguous. A page that is already showing has a twin label in its
                    # heading, so do not click it again.
                    target = (gui_find_nodes(tab, 'statictext', label) or [None])[0] if tab else None
                    if gui_match(tree, GUI_PANEL_SELECTORS[name]['panel']):
                        pass
                    elif target is None or not click(target).get('ok'):
                        results[name] = {'state': 'missing', 'hits': 0, 'samples': 1,
                                         'reason': 'nav tab not found or not clickable'}
                        continue
                    sleep(interval)
                    results[name] = verdict(*sample(GUI_PANEL_SELECTORS[name]['panel'], samples))
                    tree, _ = take()  # fresh refs for the next tab
                    if tree is None:
                        break
    finally:
        if opened_by_us:
            try:
                tree, _ = take()
                close = (gui_find_nodes(tree, *GUI_LUNA_CLOSE) or [None])[0] if tree else None
                if close is not None:
                    click(close)
            except Exception:
                pass
    if 'luna' in wanted:
        hits, taken, reason = entry_hits['luna']
        results['luna'] = verdict(hits, taken, reason)
    return {a: results.get(a) or skipped('not checked') for a in addons}


def gui_library_order(tree):
    """Game names in the order the Library list shows them, as read from the tree.

    The list is a ReactVirtualized grid: only rendered rows (the visible part plus a
    small overscan) exist, so this is the visible order, not the whole library.
    Returns {'names', 'rendered', 'total', 'virtualized'} or None if no list is shown."""
    tables = gui_find_nodes(tree, GUI_LIBRARY_TABLE[0], GUI_LIBRARY_TABLE[1], GUI_LIBRARY_TABLE[2])
    if not tables:
        return None
    names = [str(c.get('name')) for c in gui_find_nodes(tables[0], 'cell') if c.get('name')]
    total = None
    for node in gui_walk(tree):
        match = GUI_LIBRARY_TOTAL.match(str(node.get('name') or '')) if node.get('role') == 'table' else None
        if match:
            total = int(match.group(1))
            break
    return {'names': names, 'rendered': len(names), 'total': total,
            'virtualized': 'ReactVirtualized__Grid' in (tables[0].get('dom_classes') or [])
            or bool(total and len(names) < total)}


def gui_library_compare(saved, current, common=False):
    """(same, lines). `common` compares only the relative order of names present in both
    lists (the visible window moves with the scroll position)."""
    import difflib
    a, b = list(saved), list(current)
    if common:
        shared = set(a) & set(b)
        if not shared:
            return False, ['no game names in common: scroll to the same place before comparing']
        a, b = [n for n in a if n in shared], [n for n in b if n in shared]
    if a == b:
        return True, []
    diff = list(difflib.unified_diff(a, b, 'saved', 'now', n=1, lineterm=''))
    return False, diff[:40] + (['...'] if len(diff) > 40 else [])


def gui_library(options):
    reason = gui_unavailable_reason()
    if reason:
        print('GUI checks skipped: %s' % reason)
        return EXIT_SKIPPED
    def read():  # depth 20 reaches the shelf headers that carry the total
        return gui_snapshot(depth=20)
    tree, error = read()
    order = gui_library_order(tree) if tree else None
    if order is None and getattr(options, 'open', False) and tree:
        shallow, _ = gui_snapshot()  # a shallow read: deep library snapshots make refs ambiguous
        tab = (gui_find_nodes(shallow, *GUI_LIBRARY_TAB) or [None])[0] if shallow else None
        if tab is not None and gui_click_node(tab).get('ok'):
            for _ in range(8):
                time.sleep(0.75)
                tree, error = read()
                order = gui_library_order(tree) if tree else None
                if order:
                    break
    if order is None:
        print('FAIL: the Library list is not showing%s (open the Library tab or pass --open)'
              % (': %s' % error if error else ''), file=sys.stderr)
        return 1
    stamp = time.strftime('%Y-%m-%dT%H:%M:%S')
    if getattr(options, 'save', None):
        try:
            write_json_file(Path(options.save), dict(order, savedAt=stamp))
        except OSError as problem:
            print('could not save: %s' % problem, file=sys.stderr)
            return 1
    code = EXIT_OK
    if getattr(options, 'compare', None):
        saved = read_json_file(Path(options.compare))
        if not isinstance(saved, dict) or not isinstance(saved.get('names'), list):
            print('FAIL: %s is not a saved Luna library order' % options.compare, file=sys.stderr)
            return 1
        same, lines = gui_library_compare(saved['names'], order['names'], getattr(options, 'common', False))
        if same:
            print('library order matches %s (%d rows%s)' % (
                options.compare, order['rendered'], ', relative order of shared names' if options.common else ''))
        else:
            print('FAIL: library order differs from %s' % options.compare, file=sys.stderr)
            print('\n'.join(lines), file=sys.stderr)
            code = 1
    if getattr(options, 'json', False):
        print(json.dumps(order, indent=2, ensure_ascii=False))
    elif not getattr(options, 'compare', None) or code:
        print('library list (visible rows only: %d rendered of ~%s; the list is virtualized, so this '
              'depends on the scroll position):' % (order['rendered'], order['total'] or '?'))
        for number, name in enumerate(order['names'], 1):
            print('%4d  %s' % (number, name))
    if getattr(options, 'save', None):
        print('saved   : %s' % options.save)
    return code


def gui_launch_accessible():
    """Start Steam detached in accessible mode. (ok, message). Never kills or
    restarts a running Steam; a normal launch never gets these flags."""
    running, accessible = gui_steam_state()
    if running:
        return False, ('Steam is already running%s; close it completely first (Steam -> Exit), then run: luna gui launch'
                       % (' (already in accessible mode)' if accessible else ' without accessibility'))
    env = gui_session_env()
    if not gui_have_session(env):
        return False, 'no graphical session (no WAYLAND_DISPLAY/DISPLAY): Steam would exit silently'
    env['ACCESSIBILITY_ENABLED'] = '1'
    try:
        subprocess.Popen(['setsid', 'steam', GUI_ACCESS_ARGS[0]], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True, env=env)
    except OSError as error:
        return False, 'could not launch Steam: %s' % error
    return True, 'Steam launched in accessible mode; its window appears after ~15 s'


def gui_wait_for_window(timeout, sleep=time.sleep):
    """Poll until a Steam window with an accessible tree exists. Returns tree/None."""
    deadline = time.monotonic() + timeout
    while True:
        tree, _ = gui_snapshot()
        if tree is not None:
            return tree
        if time.monotonic() >= deadline:
            return None
        sleep(2)


def gui_describe_blockers(budget=3.0):
    """Best effort: what Steam shows right now (window titles, dialog names and
    their buttons) as one sentence for shutdown failures. Returns '' when
    nothing is available. Never raises; takes at most about `budget` seconds."""
    try:
        if not gui_enabled():
            return ''
        deadline = time.monotonic() + budget
        env = gui_session_env()
        if not gui_have_session(env):
            return ''
        parts = []
        windows = gui_windows(timeout=max(0.5, budget / 3), env=env)
        titles = gui_titles(windows)
        if titles:
            parts.append('open Steam windows: %s' % ', '.join('"%s"' % t for t in titles))
        left = deadline - time.monotonic()
        if left > 0.8 and gui_find_tool(GUI_TOOL) and gui_steam_state()[1]:
            tree, _ = gui_snapshot(timeout=left, env=env, scope='all', deadline=deadline)
            if tree is not None:
                niri_titles = titles if any(w.get('source') == 'niri' for w in windows) else None
                for title in gui_secondary_windows(tree, niri_titles):
                    parts.append('window "%s" is open (a normal window, not a modal; close it in Steam)' % title)
                for dialog in gui_dialogs(tree):
                    parts.append('dialog "%s" with buttons: %s'
                                 % (dialog['name'] or '?', ', '.join(dialog['buttons']) or 'none'))
        return '; '.join(parts)
    except Exception:
        return ''


def gui_lumen_survivor():
    """(pid, started) of a Lumen sidecar still alive, or None. process_running('lumen') is
    the detection the preflight and doctor use; pid and start time come from the one
    /proc reader (live_sidecar in 96_live_probe.py)."""
    try:
        if not process_running('lumen'):
            return None
        sidecar = live_sidecar()
        if not sidecar:
            return ('?', '?')
        return str(sidecar['pid']), live_stamp(sidecar['started'])
    except Exception:
        return ('?', '?')


def gui_lumen_note():
    """One-line report of a surviving Lumen sidecar after Steam exited ('' if none)."""
    survivor = gui_lumen_survivor()
    if not survivor:
        return ''
    return ('a Lumen sidecar is still running after Steam exited (pid %s, started %s); it keeps the old '
            'boot.lua in memory - stop it with: pkill -u $(id -u) -x lumen' % survivor)


def gui_report_markdown(report):
    lines = ['# GUI check - SLS Luna (%s)' % report['when'], '',
             'Run by `luna gui verify` against the signed-in client (selectors: `GUI_PANEL_SELECTORS`).', '',
             '## Environment', '', '| Item | Value |', '| --- | --- |',
             '| Luna | `%s` |' % installed_version(),
             '| Screen | %s |' % report['screen'],
             '| Windows | %s |' % (', '.join('"%s"' % t for t in report['titles']) or 'none reported'),
             '| Accessible mode | %s |' % ('yes' if report['accessible'] else 'no'), '',
             '## Add-on panels', '', '| Add-on | Result | Samples |', '| --- | --- | --- |']
    for name, item in report['panels'].items():
        lines.append('| %s | %s | %d/%d |' % (name, item['state'], item['hits'], item['samples']))
    if not report['panels']:
        lines.append('| (none installed with a panel) | - | - |')
    lines += ['', '## Evidence', '', report['screenshot'], '', '## Result', '', report['result'], '']
    return '\n'.join(lines)


def gui_put_steam_back(close=None):
    """Close the Steam this command launched in accessible mode (final-state rule: it was
    not running before, so it is not left running, and accessible mode does not linger).
    Goes through the project's own graceful shutdown; never kills. True when it is gone."""
    print('Steam was started by this command in accessible mode: closing it again '
          '(use --keep-steam to leave it running)')
    close = close or _close_steam_gracefully
    if close():
        return True
    blockers = gui_describe_blockers()
    print('Steam did not exit within the timeout%s. It is still running in accessible mode; '
          'close it yourself (Steam -> Exit).' % ('; ' + blockers if blockers else ''), file=sys.stderr)
    return False


def gui_verify(options, sleep=time.sleep):
    """`luna gui verify`: exit 0 pass, 1 any failure, 5 (EXIT_SKIPPED) it could not run.
    When --launch started Steam, Steam is closed again at the end unless --keep-steam."""
    if getattr(options, 'json', False):
        code, summary = gui_verify_data(options, sleep)
        print(json.dumps(summary, sort_keys=True))
        return code
    launched = []
    try:
        return gui_verify_run(options, sleep, launched)
    finally:
        if launched and not getattr(options, 'keep_steam', False):
            gui_put_steam_back()


def gui_verify_data(options, sleep=time.sleep):
    """Run the verify and return (exit code, summary dict) with the human output swallowed.
    summary: result pass|fail|skipped, reason (skips), screen, failures [str], panels {name: {state, hits, samples}}."""
    summary = {}
    launched = []
    sink = io.StringIO()
    try:
        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(io.StringIO()):
            code = gui_verify_run(options, sleep, launched, summary)
    finally:
        if launched and not getattr(options, 'keep_steam', False):
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                gui_put_steam_back()
    summary.setdefault('result', {EXIT_OK: 'pass', EXIT_SKIPPED: 'skipped'}.get(code, 'fail'))
    summary['code'] = code
    return code, summary


def gui_verify_run(options, sleep, launched, summary=None):
    summary = summary if summary is not None else {}
    reason = gui_unavailable_reason(need_accessible=False)
    if reason and not (getattr(options, 'launch', False) and reason.startswith('Steam is not running')):
        print('GUI checks skipped: %s' % reason)
        summary.update(result='skipped', reason=reason)
        return EXIT_SKIPPED
    running, accessible = gui_steam_state()
    if not running:
        ok, message = gui_launch_accessible()
        print(message, file=sys.stdout if ok else sys.stderr)
        if not ok:
            summary.update(result='fail', failures=[message])
            return 1
        launched.append(True)
    elif not accessible:
        reason = 'Steam was not started in accessible mode; close it and run: luna gui launch'
        print('GUI checks skipped: %s' % reason)
        summary.update(result='skipped', reason=reason)
        return EXIT_SKIPPED
    tree = gui_wait_for_window(getattr(options, 'wait', 90), sleep=sleep)
    if tree is None:
        print('FAIL: no Steam window with an accessible tree appeared', file=sys.stderr)
        summary.update(result='fail', failures=['no Steam window with an accessible tree appeared'])
        return 1
    titles = gui_titles(gui_windows())
    screen = gui_classify(tree, titles)
    signal = ''
    if screen == 'login':  # the startup window looks the same: wait for the log-on to finish
        started = gui_process_started()
        screen, tree, signal = gui_wait_for_screen(getattr(options, 'login_grace', GUI_LOGIN_GRACE), sleep, time.monotonic,
                                                   grace=getattr(options, 'login_grace', GUI_LOGIN_GRACE),
                                                   age0=(time.time() - started) if started else None)
    failures = []
    panels = {}
    if screen == 'login':
        failures.append('Steam is at the sign-in screen: sign in first, then re-run luna gui verify (%s)' % signal)
    elif screen in ('starting', 'exited'):
        failures.append('Steam is not ready: %s' % signal)
    elif screen == 'unknown':
        failures.append('could not recognise the current Steam screen')
    else:
        panels = gui_rendered(gui_addons_with_panel(getattr(options, 'only', None) or None), sleep=sleep,
                              navigate=not getattr(options, 'no_navigate', False))
        for name, item in panels.items():
            if item['state'] == 'skipped' and item.get('reason') == 'navigation disabled':
                continue
            if item['state'] != 'found':
                failures.append('%s panel: %s%s' % (name, item['state'],
                                                    ' (%s)' % item['reason'] if item.get('reason') else ''))
    summary.update(result='fail' if failures else 'pass', screen=screen, failures=failures,
                   panels={name: {'state': item.get('state'), 'hits': item.get('hits'), 'samples': item.get('samples')}
                           for name, item in panels.items()})
    stamp = time.strftime('%Y%m%d-%H%M%S')
    shots = Path(getattr(options, 'screenshots', None) or luna_tools_dir() / 'gui-shots')
    shot, note = gui_capture(shots, 'gui-verify-%s.png' % stamp,
                             full_screen=getattr(options, 'full_screen', False))
    report = {'when': time.strftime('%Y-%m-%d %H:%M'), 'screen': screen, 'titles': titles,
              'accessible': True, 'panels': panels,
              'screenshot': ('`%s` (%s)' % (shot, note)) if shot else 'none (%s)' % note,
              'result': ('FAIL\n\n' + '\n'.join('- %s' % f for f in failures)) if failures else 'PASS'}
    path = Path(getattr(options, 'report', None)
                or luna_tools_dir() / 'gui-reports' / ('gui-verify-%s.md' % stamp))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(gui_report_markdown(report), encoding='utf-8')
        print('report : %s' % path)
    except OSError as error:
        print('could not write the report: %s' % error, file=sys.stderr)
    print('screen : %s' % screen)
    for name, item in panels.items():
        print('%-11s %s (%d/%d samples)' % (name + ':', item['state'], item['hits'], item['samples']))
    for failure in failures:
        print('FAIL: %s' % failure, file=sys.stderr)
    return 1 if failures else EXIT_OK


def gui_status_info(options):
    """The `luna gui status --json` dict for this machine (read-only)."""
    env = gui_session_env()
    tool = gui_find_tool(GUI_TOOL)
    running, accessible = gui_steam_state()
    info = {'tool': tool or 'missing',
            'session': {k: env.get(k, '') for k in ('WAYLAND_DISPLAY', 'DISPLAY', 'NIRI_SOCKET')},
            'steamRunning': running, 'accessible': accessible, 'screen': 'n/a', 'windows': [],
            'backend': {k: v for k, v in gui_backend(env).items() if v}}
    if gui_enabled() and running and gui_have_session(env):
        info['windows'] = gui_titles(gui_windows(env=env))
        if tool and accessible:
            tree, _ = gui_snapshot(env=env)
            raw = gui_classify(tree, info['windows'])
        elif any('sign in to steam' in t.lower() for t in info['windows']):
            raw = 'login'
        else:
            raw = 'main' if 'Steam' in info['windows'] else 'n/a'
        info['screen'] = raw
        if raw in ('login', 'main'):
            started = gui_process_started()
            age = time.time() - started if started else None
            logon = gui_logon_evidence(started - 2) if started else ('unknown', '')
            info['screen'], info['signal'] = gui_judge_screen(raw, info['windows'], age, getattr(options, 'login_grace', GUI_LOGIN_GRACE), logon)
    return info


def gui_status(options):
    info = gui_status_info(options)
    running, accessible = info['steamRunning'], info['accessible']
    if getattr(options, 'json', False):
        print(json.dumps(info, indent=2, sort_keys=True))
        return EXIT_OK
    print('tool       : %s' % info['tool'])
    print('session    : %s' % (' '.join('%s=%s' % (k, v) for k, v in info['session'].items() if v)
                               or 'none (no WAYLAND_DISPLAY/DISPLAY found)'))
    backend = info['backend']
    print('backend    : %s (windows: %s; capture: %s)' % (backend['name'], backend['windows'],
                                                        backend.get('capture') or 'none'))
    if backend.get('note'):
        print('             %s' % backend['note'])
    print('steam      : %s' % ('running' if running else 'not running'))
    print('accessible : %s' % ('yes' if accessible else 'no'))
    print('screen     : %s' % info['screen'])
    if info.get('signal') and info['screen'] in ('starting', 'login'):
        print('             %s' % info['signal'])
    print('windows    : %s' % (', '.join('"%s"' % t for t in info['windows']) or 'none'))
    reason = gui_unavailable_reason()
    if reason:
        print('GUI checks skipped: %s' % reason)
    return EXIT_OK


def cmd_gui(options):
    """Optional GUI checks through agent-desktop (see `luna gui --help`)."""
    action = getattr(options, 'gui_action', '') or 'status'
    if action == 'status':
        return gui_status(options)
    if action == 'library':
        return gui_library(options)
    if action == 'watch':
        return gui_watch(options)
    if action == 'restart-check':
        return gui_restart_check(options)
    if action == 'launch':
        if not gui_enabled():
            print('GUI checks skipped: disabled (LUNA_GUI_CHECKS=0)')
            return EXIT_SKIPPED
        ok, message = gui_launch_accessible()
        print(message, file=sys.stdout if ok else sys.stderr)
        return EXIT_OK if ok else 1
    return gui_verify(options)
