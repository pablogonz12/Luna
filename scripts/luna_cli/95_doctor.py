# ===========================================================================
# luna doctor: aggregate drift/problem detection (read-only unless --fix).
# Each check yields findings: (id, severity, message, fix command, fix callable
# or None). Only checks whose callable is safe AND reversible set one; anything
# needing sudo, overwriting user-visible files or killing processes never does.
# ===========================================================================

DOCTOR_SEVERITIES = ('ok', 'info', 'warn', 'error')


def _finding(ident, severity, message, fix='', apply=None):
    return {'id': ident, 'severity': severity, 'message': message, 'fix': fix,
            'fixable': apply is not None, '_apply': apply}


def _doctor_adopt():
    code, message = adopt_steam_lock()
    if code != 0:
        raise OSError(message)
    return message


def _doctor_cfg_restore():
    ensure_steam_cfg_block()
    return 'restored the Luna update-lock block in %s' % ' and '.join(p.name for p in steam_cfg_paths())


def _doctor_steam_lock():
    found = []
    steam = steam_home()
    package = steam / 'package'
    pin_exists = bool(read_steam_pin())
    hard = steam_hard_lock_active()
    cfgs = [path for path in steam_cfg_paths() if path.is_file()]

    def has_keys(path):
        try:
            return any(_STEAM_CFG_KEYS.match(line) for line in path.read_text().splitlines())
        except OSError:
            return False
    cfg_locked = [path for path in cfgs if has_keys(path)]
    read_only = package.is_dir() and _package_read_only()
    # Luna's soft block writes only steam.cfg and needs a pin; the hard lock
    # writes both spellings and the read-only bit and records hardLock.
    foreign = []
    if read_only and not hard:
        foreign.append('%s is read-only' % package)
    for path in cfg_locked:
        if not pin_exists:
            foreign.append('%s has BootStrapper* lines' % path)
        elif not hard and path.name == 'Steam.cfg':
            foreign.append('%s has BootStrapper* lines (Luna only writes it for a hard lock)' % path)
    if foreign:
        adoptable = adoptable_steam_lock()[0]
        found.append(_finding(
            'steam.foreign-lock', 'warn',
            'Steam update lock not recorded by Luna (%s); `luna install` may fail on it' % '; '.join(foreign),
            ('luna steam lock --adopt   # record it as Luna\'s lock (Steam untouched; undo: luna steam unlock); '
             if adoptable else
             'luna steam lock   # take the lock over (kills Steam, rolls back to ubuntu12); ')
            + 'or release: chmod u+w %s && rm -f %s' % (package, ' '.join(str(p) for p in steam_cfg_paths())),
            (lambda: _doctor_adopt()) if adoptable else None))
    pin = read_steam_pin()
    if hard:
        missing = [path.name for path in steam_cfg_paths() if path not in cfg_locked]
        if missing or (package.is_dir() and not read_only):
            what = ('missing/empty ' + ', '.join(missing)) if missing else 'package/ is writable'
            found.append(_finding(
                'steam.lock-missing', 'warn',
                'Luna recorded a hard Steam lock but it is incomplete (%s)' % what,
                'luna steam lock',
                (lambda: _doctor_cfg_restore()) if missing else None))
    elif pin and not steam_updates_blocked():
        found.append(_finding(
            'steam.lock-missing', 'warn',
            'a Steam build is pinned but %s does not block updates' % steam_cfg_path(),
            'luna doctor --fix   # rewrites steam.cfg (undo: luna steam unpin)',
            lambda: _doctor_cfg_restore()))
    if not found and (pin or cfgs):
        found.append(_finding('steam.lock', 'ok', 'Steam update lock matches Luna\'s record'))
    return found


def _doctor_pins():
    """(source label, {addon: (pinned version, {rel: sha256})}) from the checkout's
    vendor/MANIFEST.json, else from the install-time snapshot (luna-manifest.json) -
    the installed copy has no checkout. (None, {}) when neither exists."""
    if MANIFEST_PATH.is_file():
        manifest = load_manifest()
        return 'vendor/MANIFEST.json', {
            name: (str(entry.get('plugin_version') or ''),
                   {rel: meta.get('sha256') for rel, meta in (entry.get('files') or {}).items()})
            for name, entry in (manifest.get('addons') or {}).items()}
    snapshot = read_json_file(addons_dir() / 'luna-manifest.json') or {}
    files = snapshot.get('files') if isinstance(snapshot.get('files'), dict) else {}
    versions = snapshot.get('versions') if isinstance(snapshot.get('versions'), dict) else {}
    pins = {name: (str(versions.get(name) or ''), dict(hashes))
            for name, hashes in files.items() if isinstance(hashes, dict) and hashes}
    return ('install snapshot' if pins else None), pins


def _doctor_payloads():
    found = []
    source, pins = _doctor_pins()
    if source is None:
        return [_finding('addons.manifest', 'info',
                         'no vendor/MANIFEST.json (no checkout) and no install snapshot; '
                         'add-on payloads not checked', 'luna install   # records a snapshot')]
    installed = installed_addons()
    clean = True
    for name in installed:
        pinned, pin_files = pins.get(name, ('', {}))
        try:
            have = str(json.loads((addons_dir() / name / 'plugin.json').read_text()).get('version') or '')
        except (OSError, ValueError):
            have = ''
        changed, missing = [], []
        for rel, expected in pin_files.items():
            path = addons_dir() / name / rel
            try:
                if not path.is_file():
                    missing.append(rel)
                elif sha256(path) != expected:
                    changed.append(rel)
            except OSError:
                changed.append(rel)
        if not changed and not missing:
            continue
        clean = False
        if '-dev' in have:
            kind, fix = 'dev build %s' % have, 'luna install --only %s --force   # replaces the dev build' % name
        elif have and pinned and version_key(have) < version_key(pinned):
            kind, fix = 'older Luna release (%s, pinned %s)' % (have, pinned), 'luna install --only %s' % name
        elif have and pinned and version_key(have) > version_key(pinned):
            kind, fix = 'newer Luna release (%s, pinned %s)' % (have, pinned), 'luna update --latest   # or keep it'
        else:
            kind, fix = 'locally modified (%s)' % (have or '?'), 'luna install --only %s --force   # overwrites edits' % name
        found.append(_finding(
            'addon.%s.drift' % name, 'info' if '-dev' in have else 'warn',
            '%s differs from the pinned payload: %s [%s]'
            % (name, kind, ', '.join(changed + ['%s (missing)' % m for m in missing])), fix))
    if clean and installed:
        found.append(_finding('addons.payloads', 'ok', '%d installed add-on(s) match %s' % (len(installed), source)))
    return found


def _doctor_installed_luna():
    installed = luna_tools_dir() / 'luna.py'
    if not installed.is_file():
        return []
    try:
        if installed.resolve() == Path(__file__).resolve():
            return []
        if sha256(installed) == sha256(Path(__file__).resolve()):
            return [_finding('luna.copy', 'ok', 'installed luna.py matches this one')]
    except OSError:
        return []
    return [_finding('luna.copy', 'warn',
                     '%s differs from the running luna.py' % installed,
                     'scripts/install-local.sh   # or: luna install')]


def _doctor_launchers():
    state = read_json_file(luna_tools_dir() / 'steam-machine-shortcuts.json') or {}
    files = state.get('files') if isinstance(state, dict) else None
    if not isinstance(files, dict) or not files:
        return []
    changed, missing = [], []
    for name, info in sorted(files.items()):
        path = Path(name)
        if not path.exists():
            missing.append(path)
            continue
        try:
            if sha256(path) != (info or {}).get('sha256'):
                changed.append(path)
        except OSError:
            changed.append(path)
    found = []
    review = 'luna launchers --dry-run   # review; Luna refuses to overwrite edits, back up/adopt with --migrate-legacy'
    if changed:
        found.append(_finding('launchers.changed', 'warn',
                              'launcher(s) changed outside Luna: %s' % ', '.join(p.name for p in changed), review))
    if missing:
        found.append(_finding('launchers.missing', 'info',
                              'managed launcher(s) missing: %s' % ', '.join(p.name for p in missing),
                              'luna launchers'))
    if not found:
        found.append(_finding('launchers', 'ok', '%d managed launcher file(s) unchanged' % len(files)))
    return found


def _doctor_pacman():
    managed, ignored = pacman_steam_state()
    if managed and not ignored:
        return [_finding('pacman.ignorepkg', 'warn',
                         'Steam is pacman-managed and not in IgnorePkg: a system update can replace the launcher',
                         'sudo sed -i \'/^\\[options\\]/a IgnorePkg = steam\' %s' % pacman_conf_path())]
    if managed:
        return [_finding('pacman.ignorepkg', 'ok', 'Steam is in IgnorePkg')]
    return []


def _doctor_updater():
    found = []
    tools = luna_tools_dir()
    auto = read_json_file(tools / 'autoupdate.json') or {}
    check = read_json_file(tools / 'update-check.json') or {}
    if auto.get('result') in ('error', 'failed'):
        found.append(_finding('update.autoupdate', 'warn',
                              'last unattended update %s: %s' % (auto.get('result'), auto.get('reason') or '?'),
                              'luna autoupdate --verbose'))
    if check.get('checkError'):
        found.append(_finding('update.check', 'warn', 'update check failed: %s' % check['checkError'],
                              'luna check --force'))
    if auto.get('steamDrift'):
        # The pacman message has its own check; avoid repeating it.
        if 'IgnorePkg' not in str(auto['steamDrift']):
            found.append(_finding('update.steam-drift', 'warn', 'Steam drift: %s' % auto['steamDrift'],
                                  'luna steam status'))
    if not found and (auto or check):
        found.append(_finding('update', 'ok', 'updater records show no errors'))
    return found


def _doctor_bridge():
    found = []
    boot = boot_file()
    installed = installed_addons()
    if boot.is_file() and installed:
        content = boot.read_text(errors='replace')
        if MARKER not in content:
            found.append(_finding('bridge.boot', 'error',
                                  'add-ons are installed but Lumen boot.lua is unpatched (Lumen update?)',
                                  'luna hook repair   # Steam and Lumen closed'))
        else:
            found.append(_finding('bridge.boot', 'ok', 'boot.lua carries the bridge'))
    hook = read_json_file(home() / '.local/share/slssteam-luna/status.json')
    if installed and not (home() / '.local/share/slssteam-luna/config.json').is_file():
        found.append(_finding('hook.state', 'info', 'automatic bridge recovery after Lumen updates is off',
                              'luna hook on'))
    elif isinstance(hook, dict) and hook.get('result') in ('error', 'deferred'):
        found.append(_finding('hook.state', 'warn', 'Lumen update hook %s: %s'
                              % (hook['result'], hook.get('reason') or '?'), 'luna hook repair'))
    steam_up = process_running('steam') or process_running('steamwebhelper')
    if process_running('lumen') and not steam_up:
        found.append(_finding('lumen.sidecar', 'warn',
                              'a Lumen sidecar is running without Steam; it keeps the old boot.lua in memory',
                              'pkill -u $(id -u) -x lumen'))
    return found


def _doctor_conflicts():
    paths = []
    for base in (lumen_root(), data_dir()):
        if base.is_dir():
            paths.extend(p for p in base.rglob('*.sync-conflict-*') if p.is_file())
    steam = steam_home()
    userdata = steam / 'userdata'
    if userdata.is_dir():
        paths.extend(p for p in userdata.rglob('*.sync-conflict-*') if p.is_file())
    paths = sorted(set(paths))
    if not paths:
        return [_finding('sync.conflicts', 'ok', 'no Syncthing conflict files')]
    local = [p for p in paths if p.name.startswith('localconfig.sync-conflict-')]
    found = []
    if local:
        found.append(_finding('sync.localconfig', 'warn',
                              '%d localconfig.vdf conflict(s); Steam must be closed on both machines' % len(local),
                              'luna reconcile'))
    others = [p for p in paths if p not in local]
    if others:
        shown = ', '.join(str(p) for p in others[:3]) + (' ...' if len(others) > 3 else '')
        found.append(_finding('sync.conflicts', 'warn',
                              '%d Syncthing conflict file(s): %s' % (len(others), shown),
                              'compare each with its original, keep one, delete the other'))
    return found


def doctor_summary(findings):
    """{'warn': n, 'error': n} over doctor findings (dicts or tuples from doctor_findings)."""
    counts = {'warn': 0, 'error': 0}
    for item in findings:
        severity = item.get('severity') if isinstance(item, dict) else None
        if severity in counts:
            counts[severity] += 1
    return counts


def doctor_findings():
    findings = []
    for check in (_doctor_steam_lock, _doctor_payloads, _doctor_installed_luna, _doctor_launchers,
                  _doctor_pacman, _doctor_updater, _doctor_bridge, _doctor_conflicts, _doctor_saves):
        try:
            findings.extend(check())
        except Exception as error:  # a broken check must not hide the others
            findings.append(_finding('doctor.%s' % check.__name__.replace('_doctor_', ''), 'warn',
                                     'check could not run: %s' % error))
    return findings


def cmd_doctor(options):
    findings = doctor_findings()
    if options.fix:
        # Steam may rewrite what we touch; only fix while it is closed.
        steam_up = process_running('steam') or process_running('steamwebhelper')
        for item in findings:
            item['fixed'] = False
            if item['_apply'] and item['severity'] in ('warn', 'error'):
                if steam_up:
                    item['fixError'] = 'Steam is running; close it and retry'
                    continue
                try:
                    result = item['_apply']()
                    item['fixed'] = True
                    item['result'] = result if isinstance(result, str) and result else 'fixed'
                except OSError as error:
                    item['fixError'] = str(error)
    remaining = [i for i in findings if i['severity'] in ('warn', 'error') and not i.get('fixed')]
    code = 2 if any(i['severity'] == 'error' for i in remaining) else (1 if remaining else 0)
    public = [{k: v for k, v in i.items() if k != '_apply'} for i in findings]
    if options.json:
        print(json.dumps({'version': 1, 'machine': os.uname().nodename, 'exit': code,
                          'findings': public}, indent=2, sort_keys=True))
        return code
    order = {s: n for n, s in enumerate(reversed(DOCTOR_SEVERITIES))}
    for item in sorted(public, key=lambda i: order[i['severity']]):
        tag = 'FIXED' if item.get('fixed') else item['severity'].upper()
        print('%-5s %-22s %s' % (tag, item['id'], item['message']))
        if item.get('fixed'):
            print('      %-22s result: %s' % ('', item.get('result', 'fixed')))
        elif item.get('fixError'):
            print('      %-22s fix failed: %s' % ('', item['fixError']))
        elif item['fix'] and item['severity'] != 'ok' and not item.get('fixed'):
            print('      %-22s fix: %s%s' % ('', item['fix'], '  [--fix can apply]' if item['fixable'] else ''))
    print('\n%s' % ('doctor: all clear' if not remaining else
                    'doctor: %d finding(s) need attention' % len(remaining)))
    return code
# ======================== end luna doctor ==================================

