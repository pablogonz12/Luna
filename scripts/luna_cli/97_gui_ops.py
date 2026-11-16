
# `luna gui watch`, the window backends and `luna gui restart-check`. Everything here
# builds on 94_gui_check.py (accessibility tree, capture) and 96_live_probe.py (CDP).


# ------------------------------------------------------------------ gui watch

def gui_watch_selector(options):
    """(role, name, exact) from --addon / --role / --name, or raises ValueError."""
    addon = getattr(options, 'addon', None)
    role = getattr(options, 'role', None)
    name = getattr(options, 'name', None)
    if addon:
        if role or name:
            raise ValueError('use either --addon KEY or --role/--name, not both')
        if addon not in GUI_PANEL_SELECTORS:
            raise ValueError('unknown add-on key %r (known: %s)' % (addon, ', '.join(sorted(GUI_PANEL_SELECTORS))))
        return GUI_PANEL_SELECTORS[addon]['panel']
    if not role and not name:
        raise ValueError('gui watch needs --role and/or --name, or --addon KEY')
    return role, name, not getattr(options, 'contains', False)


def gui_watch(options, sleep=time.sleep, snapshot=None, clock=time.time, out=None):
    """`luna gui watch`: sample the accessibility tree and print one timestamped line
    each time the element appears or disappears. Read-only: it only snapshots, it never
    clicks, so it does not navigate (open the page you want to watch yourself).

    Exit: 0 normally (Ctrl-C and --duration end it cleanly with a summary); 1 with
    --fail-if-vanished when the element disappeared after having been seen (the watch
    then stops at that moment); EXIT_SKIPPED when GUI checks cannot run; EXIT_REFUSED
    for a bad selector."""
    out = out or (lambda line: print(line, flush=True))
    try:
        role, name, exact = gui_watch_selector(options)
    except ValueError as error:
        print('REFUSING: %s' % error, file=sys.stderr)
        return EXIT_REFUSED
    reason = gui_unavailable_reason()
    if reason:
        print('GUI checks skipped: %s' % reason)
        return EXIT_SKIPPED
    interval = max(0.2, float(getattr(options, 'interval', 2.0) or 2.0))
    duration = getattr(options, 'duration', None)
    fail_on_vanish = getattr(options, 'fail_if_vanished', False)
    snapshot = snapshot or gui_snapshot
    label = ' '.join(part for part in (role or '*', repr(name) if name is not None else '') if part)
    started = clock()
    stats = {'samples': 0, 'unreadable': 0, 'appeared': 0, 'vanished': 0, 'seen': False}
    state = None  # None unknown, True present, False absent
    readable = True

    def stamp():
        now = clock()
        return '%s.%03d' % (time.strftime('%H:%M:%S', time.localtime(now)), int((now % 1) * 1000))

    out('%s watching %s every %.1fs%s (Ctrl-C to stop)'
        % (stamp(), label, interval, ' for %ds' % duration if duration is not None else ''))
    code = EXIT_OK
    try:
        while True:
            try:
                tree, error = snapshot()
            except Exception as problem:  # a vanished window must not end the watch
                tree, error = None, str(problem)
            if tree is None:
                stats['unreadable'] += 1
                if readable:
                    out('%s unreadable (%s); element state unchanged' % (stamp(), error or 'no snapshot'))
                readable = False
            else:
                if not readable and stats['samples']:
                    out('%s readable again' % stamp())
                readable = True
                stats['samples'] += 1
                count = len(gui_find_nodes(tree, role, name, exact))
                present = count > 0
                if present != state:
                    if present:
                        stats['appeared'] += 1
                        stats['seen'] = True
                        out('%s APPEARED  %s (%d match%s)' % (stamp(), label, count, '' if count == 1 else 'es'))
                    elif state is True or stats['seen']:
                        stats['vanished'] += 1
                        out('%s VANISHED  %s' % (stamp(), label))
                    else:
                        out('%s absent    %s (not seen yet)' % (stamp(), label))
                    state = present
                    if fail_on_vanish and stats['vanished']:
                        code = 1
                        break
            if duration is not None and clock() - started >= duration:
                break
            sleep(interval)
    except KeyboardInterrupt:
        out('%s interrupted' % stamp())
    out('summary: %d samples, %d unreadable, appeared %d, vanished %d, now %s, %.1fs'
        % (stats['samples'], stats['unreadable'], stats['appeared'], stats['vanished'],
           {None: 'unknown', True: 'present', False: 'absent'}[state], clock() - started))
    if code:
        out('FAIL: %s vanished after having been seen' % label)
    return code


# ------------------------------------------------------- gui restart-check
#
# One full Steam restart with before/after evidence: does the Library order (and the sort
# state), the add-on panels and the sign-in survive it? Steps (each one is a function so
# the tests can drive them): refuse while a game runs or a download is active; record the
# state; shut Steam down through _close_steam_gracefully (never kills); report a surviving
# Lumen sidecar; relaunch in accessible mode; compare; write one report; put Steam back
# into the mode it started in (--final overrides, --keep-steam keeps it accessible).

GUI_FINAL_MODES = ('accessible', 'normal', 'stopped')
GUI_DOWNLOAD_LINE = re.compile(r'^Downloads - (\d+) of (\d+) Items Complete$')


def gui_running_games(proc='/proc'):
    """[{'pid', 'appid'}] for running games: a `reaper` process or a `SteamLaunch AppId=`
    command line. Read-only /proc scan."""
    found = []
    try:
        entries = [p for p in Path(proc).iterdir() if p.name.isdigit()]
    except OSError:
        return found
    for entry in entries:
        try:
            argv = [a for a in (entry / 'cmdline').read_bytes().decode('utf-8', 'replace').split('\0') if a]
            comm = (entry / 'comm').read_text().strip() if (entry / 'comm').exists() else ''
        except OSError:
            continue
        joined = ' '.join(argv)
        match = re.search(r'AppId=(\d+)', joined)
        if comm == 'reaper' or (argv and Path(argv[0]).name == 'reaper') or 'SteamLaunch' in joined and match:
            found.append({'pid': int(entry.name), 'appid': match.group(1) if match else '?'})
    return found


def gui_active_download(tree=None):
    """Why Steam looks busy downloading, or ''. Reads only: the Downloads line of the
    main window ('Downloads - 3 of 4 Items Complete') and steamapps/downloading/."""
    if tree:
        for node in gui_walk(tree):
            match = GUI_DOWNLOAD_LINE.match(str(node.get('name') or ''))
            if match and int(match.group(1)) < int(match.group(2)):
                return 'the Downloads queue shows %s of %s items complete' % match.groups()
    try:
        folder = steam_home() / 'steamapps' / 'downloading'
        if folder.is_dir() and any(folder.iterdir()):
            return 'steamapps/downloading/ is not empty'
    except OSError:
        pass
    return ''


def gui_read_order(open_library=True, attempts=1, sleep=time.sleep):
    """Library order for the comparison: the complete list from the running client over
    CDP when it answers, else the visible rows of the accessibility tree (opening the
    Library tab, a navigation click). Returns {'source', 'names', 'total', 'sort'} or
    {'source': 'unavailable', 'why': ...}."""
    why = ''
    for attempt in range(max(1, attempts)):  # the Library list can be unmounted for a moment
        try:
            order = live_library_order()
            return {'source': 'cdp', 'names': list(order['names']), 'total': order.get('rows') or len(order['names']),
                    'sort': order.get('sort')}
        except Exception as error:  # LiveProbeError and anything unexpected: fall back
            why = 'CDP: %s' % error
        if attempt + 1 < attempts:
            sleep(2)
    cdp_error = why
    try:
        tree, error = gui_snapshot(depth=20)
        order = gui_library_order(tree) if tree else None
        if order is None and open_library and tree:
            shallow, _ = gui_snapshot()
            tab = (gui_find_nodes(shallow, *GUI_LIBRARY_TAB) or [None])[0] if shallow else None
            if tab is not None and gui_click_node(tab).get('ok'):
                for _ in range(8):
                    time.sleep(0.75)
                    tree, _ = gui_snapshot(depth=20)
                    order = gui_library_order(tree) if tree else None
                    if order:
                        break
        if order:
            return {'source': 'tree (visible rows only)', 'names': order['names'], 'total': order.get('total'),
                    'sort': None, 'why': cdp_error}
        why += '; tree: %s' % (error or 'no Library list on screen')
    except Exception as error:
        why += '; tree: %s' % error
    return {'source': 'unavailable', 'why': why, 'names': [], 'total': None, 'sort': None}


def gui_read_state(sleep=time.sleep, grace=GUI_LOGIN_GRACE):
    """Everything compared across the restart: screen, add-on panels, Library order."""
    try:
        tree, _ = gui_snapshot()
    except Exception:
        tree = None
    titles = gui_titles(gui_windows())
    screen = gui_classify(tree, titles) if tree is not None else 'unknown'
    signal = ''
    if screen == 'login':  # a sign-in window alone is not proof (see gui_judge_screen)
        started = gui_process_started()
        screen, signal = gui_judge_screen(screen, titles, time.time() - started if started else None,
                                          grace, gui_logon_evidence(started - 2) if started else ('unknown', ''))
    panels = {}
    if screen == 'main':
        panels = {name: item['state'] for name, item in
                  gui_rendered(gui_addons_with_panel(), sleep=sleep).items()}
    return {'screen': screen, 'signal': signal, 'panels': panels, 'order': gui_read_order(attempts=3, sleep=sleep) if screen == 'main' else
            {'source': 'unavailable', 'why': 'not on the main screen', 'names': [], 'total': None, 'sort': None},
            'download': gui_active_download(tree)}


def gui_stable_sort(sort):
    """The sort state without its clock: Luna stamps slsLunaSortDiag.at on every start, which
    says when the sort ran, not what it is."""
    if not isinstance(sort, dict):
        return sort
    out = json.loads(json.dumps(sort))
    diag = (out.get('stored') or {}).get('slsLunaSortDiag')
    if isinstance(diag, dict):
        diag.pop('at', None)
    return out


def gui_compare_states(before, after, warnings=None):
    """(rows, differences): rows are (what, before, after, 'same'|'DIFFERENT'|'n/a').
    Things that could not be compared are appended to `warnings`."""
    rows, differences = [], []
    warnings = [] if warnings is None else warnings

    def row(what, left, right, same=None):
        same = (left == right) if same is None else same
        rows.append((what, left, right, 'same' if same else 'DIFFERENT'))
        if not same:
            differences.append(what)
    if str(before['screen']).startswith('n/a'):  # normal mode or not running: no tree to compare
        rows.append(('screen', before['screen'], after['screen'], 'n/a'))
    else:
        row('screen', before['screen'], after['screen'])
        for name in sorted(set(before['panels']) | set(after['panels'])):
            row('panel: %s' % name, before['panels'].get(name, 'n/a'), after['panels'].get(name, 'n/a'))
    left, right = before['order'], after['order']
    if left['source'] == 'unavailable' or right['source'] == 'unavailable':
        rows.append(('library order', left.get('why') or '%d rows' % len(left['names']),
                     right.get('why') or '%d rows' % len(right['names']), 'n/a'))
        differences.append('library order could not be read on both sides')
        return rows, differences
    if left['source'] != right['source']:
        # A tree read (visible rows only, no sort state) and a CDP read (every row, duplicates
        # included) cannot prove anything about each other: say so instead of a false DIFFERENT.
        shown = lambda o: '%d rows via %s%s' % (len(o['names']), o['source'],  # noqa: E731
                                               ' [%s]' % o['why'] if o.get('why') else '')
        rows.append(('library order', shown(left), shown(right), 'n/a'))
        rows.append(('sort state', 'n/a', 'n/a', 'n/a'))
        warnings.append('library order and sort state could not be compared: before was read via %s, after via %s'
                        % (left['source'], right['source']))
        return rows, differences
    common = 'tree' in left['source']
    same, lines = gui_library_compare(left['names'], right['names'], common=common)
    shown = lambda o: '%d rows via %s' % (len(o['names']), o['source'])  # noqa: E731
    rows.append(('library order' + (' (relative, visible rows)' if common else ''), shown(left), shown(right),
                 'same' if same else 'DIFFERENT'))
    if not same:
        differences.append('library order (%s)' % '; '.join(lines[:3]))
    if left['sort'] is not None or right['sort'] is not None:
        row('sort state', json.dumps(gui_stable_sort(left['sort']), sort_keys=True),
            json.dumps(gui_stable_sort(right['sort']), sort_keys=True))
    return rows, differences


def gui_restart_report(report):
    lines = ['# Steam restart-cycle check', '', 'Run: %s (by `luna gui restart-check`; nothing was killed, '
             'no credentials were typed).' % report['when'], '',
             '## Steps', '', '| step | result |', '|---|---|']
    lines += ['| %s | %s |' % item for item in report['steps']]
    lines += ['', '## Before / after', '', '| what | before | after | |', '|---|---|---|---|']
    lines += ['| %s | %s | %s | %s |' % tuple(str(x).replace('|', '/') for x in item) for item in report['rows']]
    lines += ['', '## Timing', '', '- shutdown: %s' % report['shutdown'], '- relaunch to window: %s' % report['relaunch'],
              '- Lumen sidecar after shutdown: %s' % report['sidecar'],
              '- final state: %s' % report['final'], '', '## How each conclusion was reached', '']
    lines += ['- %s' % item for item in report.get('signals') or ['(no screen was judged)']]
    lines += ['', '## Evidence', '', report['screenshot'], '',
              '## Result', '', report['result'], '']
    return '\n'.join(lines)


def gui_wait_for_screen(timeout, sleep, clock, grace=GUI_LOGIN_GRACE, age0=0.0, logon=None):
    """Poll a (re)started Steam. Returns (screen, tree, signal): 'main', 'dialog' and 'login'
    end the wait; 'login' only after the sign-in window outlasted `grace` seconds of Steam
    uptime with no log-on attempt in connection_log. 'exited' (the process is gone after
    its first seconds) ends it too. Anything else is 'starting'/'unknown' at the timeout.
    `age0` = seconds Steam has already been up (None = unknown)."""
    logon = logon or gui_logon_evidence
    begin = clock()
    since = time.time() - (age0 or 0) - 2
    while True:
        age = None if age0 is None else age0 + clock() - begin
        tree, _ = gui_snapshot()
        titles = gui_titles(gui_windows())
        if tree is not None:
            raw = gui_classify(tree, titles)
        else:
            raw = 'login' if any('sign in to steam' in t.lower() for t in titles) else 'main' if 'Steam' in titles else 'unknown'
        screen, signal = gui_judge_screen(raw, titles, age, grace, logon(since))
        if screen == 'unknown':
            screen, signal = 'starting', 'no recognisable window yet (titles: %s)' % (', '.join(titles) or 'none')
        if screen in ('main', 'dialog', 'login'):
            return screen, tree, signal
        if age is not None and age >= 6 and not gui_steam_state()[0]:
            return 'exited', tree, 'the Steam process is gone %.0f s after the launch; %s' % (age, gui_exit_hint(since))
        if clock() - begin >= timeout:
            return screen, tree, 'timed out after %.0f s: %s' % (clock() - begin, signal)
        sleep(3)


def gui_wait_normal_window(timeout, sleep, clock, grace=GUI_LOGIN_GRACE, logon=None):
    """After a normal (non-accessible) launch there is no tree, so: window titles, the
    log-on evidence and time. Returns (state, signal) with state main/login/starting/exited."""
    logon = logon or gui_logon_evidence
    begin = clock()
    since = time.time() - 2
    while True:
        age = clock() - begin
        titles = gui_titles(gui_windows())
        raw = 'login' if any('sign in to steam' in t.lower() for t in titles) else 'main' if 'Steam' in titles else 'unknown'
        state, signal = gui_judge_screen(raw, titles, age, grace, logon(since))
        if state in ('main', 'login'):
            return state, signal
        if age >= 6 and not gui_steam_state()[0]:
            return 'exited', 'the Steam process is gone %.0f s after the launch; %s' % (age, gui_exit_hint(since))
        if age >= timeout:
            return 'starting', 'timed out after %.0f s: %s' % (age, signal if raw != 'unknown' else
                                                              'no Steam window (titles: %s)' % (', '.join(titles) or 'none'))
        sleep(3)


def gui_restart_check(options, sleep=time.sleep, clock=time.monotonic):
    """`luna gui restart-check`. Exit 0 identical before/after, 1 any difference or failure,
    EXIT_NEEDS_FORCE (3) a game or download is active (--force overrides), EXIT_SKIPPED (5)
    GUI checks cannot run, EXIT_REFUSED (2) a bad --final."""
    final = getattr(options, 'final', None)
    if getattr(options, 'keep_steam', False):
        final = final or 'accessible'
    if final is not None and final not in GUI_FINAL_MODES:
        print('REFUSING: --final must be one of %s' % ', '.join(GUI_FINAL_MODES), file=sys.stderr)
        return EXIT_REFUSED
    reason = gui_unavailable_reason(need_accessible=False)
    if reason:
        print('GUI checks skipped: %s' % reason)
        return EXIT_SKIPPED
    running, accessible = gui_steam_state()
    start_mode = 'not running' if not running else 'accessible' if accessible else 'normal'
    final = final or {'accessible': 'accessible', 'normal': 'normal', 'not running': 'stopped'}[start_mode]
    steps, failures = [], []
    report = {'when': time.strftime('%Y-%m-%d %H:%M'), 'steps': steps, 'rows': [], 'shutdown': 'n/a',
              'relaunch': 'n/a', 'sidecar': 'n/a', 'final': 'n/a', 'screenshot': 'none', 'result': '', 'signals': [], 'warnings': []}

    def finish(code):
        stamp = time.strftime('%Y%m%d-%H%M%S')
        report['result'] = ('PASS: nothing changed across the restart' + ''.join('\n\nWARNING: %s' % w for w in report['warnings'])
                            if code == 0 else
                            'FAIL\n\n' + '\n'.join('- %s' % f for f in failures)) if code in (0, 1) else \
            'STOPPED\n\n' + '\n'.join('- %s' % f for f in failures)
        path = Path(getattr(options, 'report', None) or luna_tools_dir() / 'gui-reports' / ('gui-restart-%s.md' % stamp))
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(gui_restart_report(report), encoding='utf-8')
            print('report : %s' % path)
        except OSError as error:
            print('could not write the report: %s' % error, file=sys.stderr)
        for warning in report['warnings']:
            print('WARNING: %s' % warning, file=sys.stderr)
        for failure in failures:
            print('%s: %s' % ('FAIL' if code == 1 else 'STOPPED', failure), file=sys.stderr)
        return code

    # 2. refuse while something would be interrupted
    games = gui_running_games()
    busy = ['a game appears to be running (%s)' % ', '.join('AppId %s pid %d' % (g['appid'], g['pid']) for g in games)
            ] if games else []
    before_tree = None
    if running and accessible:
        try:
            before_tree, _ = gui_snapshot()
        except Exception:
            before_tree = None
    download = gui_active_download(before_tree) if running else ''
    if download:
        busy.append('Steam looks busy downloading (%s)' % download)
    if busy and not getattr(options, 'force', False):
        print('REFUSING to restart Steam: %s. Finish or pause it, or re-run with --force.' % '; '.join(busy),
              file=sys.stderr)
        return EXIT_NEEDS_FORCE
    if busy:
        steps.append(('refusal checks', 'overridden with --force: %s' % '; '.join(busy)))
    else:
        steps.append(('refusal checks', 'no game running, no active download'))

    # 1. before state
    if running and accessible:
        before = gui_read_state(sleep=sleep, grace=getattr(options, 'login_grace', GUI_LOGIN_GRACE))
        report['signals'].append('before: screen %s (%s)' % (before['screen'], before.get('signal') or 'accessibility tree'))
        if before['screen'] in ('login', 'starting'):
            print('STOPPED: Steam is at the %s screen; wait for it or sign in first' % before['screen'], file=sys.stderr)
            failures.append('Steam is %s before the restart (%s)' % (
                'at the sign-in screen' if before['screen'] == 'login' else 'still starting', before.get('signal')))
            return finish(1)
    elif running:  # normal mode: no tree, so only the CDP order can be recorded
        before = {'screen': 'n/a (normal mode)', 'panels': {},
                  'order': gui_read_order(open_library=False), 'download': ''}
    else:
        before = {'screen': 'n/a (not running)', 'panels': {},
                  'order': {'source': 'unavailable', 'why': 'Steam was not running', 'names': [], 'total': None,
                            'sort': None}, 'download': ''}
    steps.append(('state before (%s)' % start_mode, 'screen %s, %d panels, library order: %s'
                  % (before['screen'], len(before['panels']), '%d rows via %s%s' % (len(before['order']['names']),
                                                                                 before['order']['source'],
                                                                                 ' (%s)' % before['order']['why'] if before['order'].get('why') else '')
                     if before['order']['source'] != 'unavailable' else before['order'].get('why'))))

    # 3. shutdown through the project's own path
    if running:
        started = clock()
        gone = _close_steam_gracefully(timeout=getattr(options, 'shutdown_timeout', 90))
        report['shutdown'] = '%.0f s%s' % (clock() - started, '' if gone else ' (did not exit)')
        if not gone:
            blockers = gui_describe_blockers()
            steps.append(('shutdown', 'Steam did not exit within %ss; nothing was killed' % getattr(options, 'shutdown_timeout', 90)))
            failures.append('Steam did not exit%s. Close it yourself (Steam -> Exit) and run the check again.'
                            % ('; it shows: ' + blockers if blockers else ''))
            report['final'] = 'left running (shutdown failed)'
            return finish(1)
        steps.append(('shutdown', 'Steam exited after %s' % report['shutdown']))
        # 4. sidecar
        survivor = gui_lumen_survivor()
        if survivor:
            report['sidecar'] = 'STILL RUNNING (pid %s, started %s): it keeps the old boot.lua in memory' % survivor
            steps.append(('sidecar', report['sidecar']))
            # A warning, not a failure: the before/after comparison is still valid (it only
            # sees what the client shows), while the stale runtime is what `doctor` flags.
            report['warnings'].append('a Lumen sidecar survived the shutdown (pid %s, started %s); it was not '
                                      'killed, and new add-on payloads are not loaded by it' % survivor)
        else:
            report['sidecar'] = 'gone'
            steps.append(('sidecar', 'no Lumen sidecar survived'))
    else:
        steps.append(('shutdown', 'skipped: Steam was not running'))

    # 5. relaunch in accessible mode
    wait = getattr(options, 'relaunch_wait', 240)
    grace = getattr(options, 'login_grace', GUI_LOGIN_GRACE)
    ok, message = gui_launch_accessible()
    print(message, file=sys.stdout if ok else sys.stderr)
    if not ok:
        failures.append('could not relaunch Steam: %s' % message)
        steps.append(('relaunch', 'failed: %s' % message))
        return finish(1)
    started = clock()
    screen, tree, signal = gui_wait_for_screen(wait, sleep, clock, grace=grace)
    report['relaunch'] = '%.0f s to the %s screen' % (clock() - started, screen)
    report['signals'].append('relaunch: %s (%s)' % (screen, signal))
    steps.append(('relaunch (accessible)', report['relaunch']))
    if screen == 'login':
        report['final'] = 'left at the sign-in screen, in accessible mode (not restarted again)'
        failures.append('Steam is waiting at the sign-in screen (%s). It was left there; sign in yourself, '
                        'nothing was typed and Steam was not restarted again.' % signal)
        return finish(1)
    if screen == 'exited':
        report['final'] = 'Steam is not running (it exited during startup)'
        failures.append('Steam exited during startup: %s' % signal)
        return finish(1)
    if screen != 'main':
        report['final'] = 'left running in accessible mode'
        failures.append('no main Steam window appeared within %ds (%s: %s)' % (wait, screen, signal))
        return finish(1)

    # 6. after state, compare, evidence
    settle = clock() + getattr(options, 'settle', 90)
    while True:  # the Lumen plugin and its Library order take a while to come up
        after = gui_read_state(sleep=sleep)
        expected = {n for n, v in before['panels'].items() if v == 'found'}
        ready = (after['screen'] == 'main' and expected <= {n for n, v in after['panels'].items() if v == 'found'}
                 and (after['order']['source'] == 'cdp' or before['order']['source'] != 'cdp'))
        if ready or clock() >= settle:
            break
        sleep(5)
    steps.append(('state after', 'screen %s, %d panels, library order: %s' % (
        after['screen'], len(after['panels']), '%d rows via %s' % (len(after['order']['names']), after['order']['source'])
        if after['order']['source'] != 'unavailable' else after['order'].get('why'))))
    rows, differences = gui_compare_states(before, after, report['warnings'])
    report['rows'] = rows
    shots = Path(getattr(options, 'screenshots', None) or luna_tools_dir() / 'gui-shots')
    shot, note = gui_capture(shots, 'gui-restart-%s.png' % time.strftime('%Y%m%d-%H%M%S'),
                             full_screen=getattr(options, 'full_screen', False))
    report['screenshot'] = ('`%s` (%s)' % (shot, note)) if shot else 'none (%s)' % note
    if start_mode == 'not running':
        differences = [d for d in differences if not d.startswith('library order could not')]
        steps.append(('compare', 'nothing to compare: Steam was not running before'))
    failures.extend('differs after the restart: %s' % d for d in differences)
    for item in after['panels'] if after['panels'] else ():
        if after['panels'][item] != 'found' and 'panel: %s' % item not in differences:
            failures.append('panel %s is %s after the restart' % (item, after['panels'][item]))

    # 7. final state
    code = 1 if failures else EXIT_OK
    if final == 'accessible':
        report['final'] = 'left running in accessible mode (started %s)' % start_mode
    elif final == 'normal':
        if not _close_steam_gracefully(timeout=getattr(options, 'shutdown_timeout', 90)):
            report['final'] = 'left running in accessible mode (could not close it to go back to normal)'
            failures.append('could not close Steam to return it to normal mode: ' + (gui_describe_blockers() or 'unknown'))
            code = 1
        else:
            for key in ('WAYLAND_DISPLAY', 'DISPLAY', 'NIRI_SOCKET'):  # the detached launch needs the session
                if gui_session_env().get(key):
                    os.environ.setdefault(key, gui_session_env()[key])
            os.environ.pop('ACCESSIBILITY_ENABLED', None)
            started = clock()
            if not _launch_steam_detached():
                report['final'] = 'Steam is stopped (normal relaunch failed)'
                failures.append('could not relaunch Steam in normal mode')
                code = 1
            else:
                seen, signal = gui_wait_normal_window(wait, sleep, clock, grace=grace)
                report['signals'].append('normal relaunch: %s (%s)' % (seen, signal))
                report['final'] = 'normal mode, %s (%.0f s)' % ({'main': 'signed in', 'login': 'at the sign-in screen',
                                                                  'starting': 'still starting', 'exited': 'not running'}[seen],
                                                                 clock() - started)
                if seen != 'main':
                    failures.append('after returning to normal mode Steam shows: %s (%s)' % (seen, signal))
                    code = 1
    else:
        if _close_steam_gracefully(timeout=getattr(options, 'shutdown_timeout', 90)):
            report['final'] = 'stopped (it was not running before)'
        else:
            report['final'] = 'left running (could not close it)'
            failures.append('could not close Steam to restore "not running"')
            code = 1
    steps.append(('final state', report['final']))
    print('before/after:')
    for what, left, right, verdict in rows:
        print('  %-34s %-28s %-28s %s' % (what[:34], str(left)[:28], str(right)[:28], verdict))
    return finish(code)
