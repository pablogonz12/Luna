def cmd_check(options):
    """Print the update check as JSON (the Luna menu reads it)."""
    write_heartbeat()
    print(json.dumps(check_updates(force=getattr(options, 'force', False)), sort_keys=True))
    return EXIT_OK


def cmd_timer(options):
    """Enable, disable or report the unattended-update timer."""
    unit_dir, service, timer = unit_paths()
    if options.action == 'status':
        enabled = subprocess.run(['systemctl', '--user', 'is-enabled', 'luna-autoupdate.timer'],
                                 capture_output=True, text=True)
        active = subprocess.run(['systemctl', '--user', 'is-active', 'luna-autoupdate.timer'],
                                capture_output=True, text=True)
        print(json.dumps({'enabled': enabled.stdout.strip() or 'unknown',
                          'active': active.stdout.strip() or 'unknown',
                          'units': str(unit_dir)}, sort_keys=True))
        return EXIT_OK
    if options.action == 'on':
        write_tooling()
        unit_dir.mkdir(parents=True, exist_ok=True)
        service.write_text(
            '[Unit]\n'
            'Description=SLS Luna updates (applies a newer release when Steam is closed)\n'
            'After=graphical-session.target\n\n'
            '[Service]\n'
            'Type=oneshot\n'
            'Environment=PATH=%%h/.local/bin:/usr/local/bin:/usr/bin\n'
            'ExecStart=%s %s autoupdate\n' % (sys.executable, luna_tools_dir() / 'luna.py'))
        timer.write_text(
            '[Unit]\n'
            'Description=Check the SLS Luna release channel every 30 minutes\n\n'
            '[Timer]\n'
            'OnCalendar=*:0/30\n'
            'Persistent=true\n'
            'RandomizedDelaySec=120\n\n'
            '[Install]\n'
            'WantedBy=timers.target\n')
        subprocess.run(['systemctl', '--user', 'daemon-reload'], capture_output=True)
        result = subprocess.run(['systemctl', '--user', 'enable', '--now', 'luna-autoupdate.timer'],
                                capture_output=True, text=True)
        if result.returncode != 0:
            print('FAILED: %s' % (result.stderr.strip() or result.stdout.strip()), file=sys.stderr)
            return 1
        print('unattended updates: on (%s)' % timer)
        return EXIT_OK
    if options.action == 'off':
        subprocess.run(['systemctl', '--user', 'disable', '--now', 'luna-autoupdate.timer'],
                       capture_output=True)
        for path in (timer, service):
            if path.exists():
                path.unlink()
        subprocess.run(['systemctl', '--user', 'daemon-reload'], capture_output=True)
        print('unattended updates: off')
        return EXIT_OK
    print('unknown timer action: %s' % options.action, file=sys.stderr)
    return 2


def lumen_hook(action):
    """Use the source worker or the independent installed copy; no full install."""
    candidates = (ROOT / 'scripts/lumen-hook.py',
                  home() / '.local/share/slssteam-luna/lumen-hook.py')
    script = next((path for path in candidates if path.is_file()), None)
    if script is None:
        print('Lumen update hook worker missing; install Luna from a complete bundle.', file=sys.stderr)
        return 1
    return subprocess.run([sys.executable, str(script), action]).returncode


def cmd_hook(options):
    return lumen_hook({'on': 'enable', 'off': 'disable'}.get(options.action, options.action))


def arm_lumen_hook():
    if (addons_dir() / 'luna/backend/main.lua').is_file():
        if lumen_hook('enable'):
            print('WARNING: add-ons installed, but automatic Lumen recovery could not be armed. '
                  'Inspect: luna hook status', file=sys.stderr)


def cmd_autoupdate(options):
    """Timer entry point: check quietly and apply when Steam is closed.

    Modes come from the Health page switch (data/luna.json -> auto_update):
    "off" does nothing unless a manual update was queued, "notify" only
    records what it found, "install" applies a newer release. A queued update
    (pending.json) is honoured in any mode but still needs Steam closed.
    """
    tools = luna_tools_dir()
    pending_path = tools / 'pending.json'
    result_path = tools / 'autoupdate.json'
    lock_path = _update_lock()
    prefs = read_json_file(data_dir() / 'luna.json') or {}
    mode = str(prefs.get('auto_update') or 'off')
    pending = read_json_file(pending_path) or {}
    pending_refresh = read_refresh_pending()
    verbose = bool(getattr(options, 'verbose', False))
    write_heartbeat()
    extra_result = {}

    def record(payload):
        payload.update(extra_result)
        payload['mode'] = mode
        payload['at'] = int(time.time())
        snapshot = steam_state()
        payload['steamBuild'] = snapshot['build']
        payload['steamPinned'] = snapshot['pinned']
        payload['steamDrift'] = steam_drift_note()
        queued = read_steam_pending()
        payload['steamPending'] = str(queued.get('action') or '') if queued else ''
        write_json_file(result_path, payload)
        if verbose:
            print(json.dumps(payload, sort_keys=True))
        return EXIT_OK

    if mode not in ('notify', 'install') and not pending and not pending_refresh:
        return EXIT_OK
    acquired, reason = _acquire_lock(lock_path)
    if not acquired:
        return record({'result': 'deferred', 'reason': reason})
    refresh_message = ''
    try:
        # An explicit refresh request (Health page or `luna refresh --dry-run`
        # planned it, the timer applies it): Steam to Valve's latest, then pin
        # and cache, then hand the Luna add-ons to the normal bundle path
        # below so the bridge and payloads land verified. Needs Steam closed
        # like every other mutation; otherwise it waits for the next run.
        if read_refresh_pending():
            reasons = refuse_reasons(for_install=True)
            if reasons:
                return record({'result': 'deferred', 'reason': reasons[0],
                               'refreshPending': True})
            ok, message, build = _refresh_steam_side()
            if not ok:
                return record({'result': 'error', 'reason': message,
                               'refreshPending': True})
            refresh_message = message
            extra_result['refresh'] = message
            try:
                refresh_pending_path().unlink()
            except OSError:
                pass
            pending = {'tag': '', 'requestedAt': int(time.time())}
            write_json_file(pending_path, pending)
        # The timer only runs while Steam is closed, which is exactly when a
        # pinned client can be restored safely. In install mode (explicit
        # consent via pin + mode) a drifted client is recovered from the local
        # cache before anything else; every mode reports the drift.
        snapshot = steam_state()
        if (snapshot['pinned'] and snapshot['cacheReady']
                and (snapshot['packagesMatch'] is False or snapshot.get('binariesMatch') is False)
                and (mode == 'install' or pending)):
            ok, message = _recover_steam_client(snapshot['pinned'])
            if ok:
                snapshot = steam_state()
            else:
                return record({'result': 'error', 'reason': message})
        # Queued Steam work (Health menu or `luna steam update|cache`) runs here:
        # the timer only fires while Steam is closed, which both need.
        pending_steam = read_steam_pending()
        if pending_steam:
            steam_job = pending_steam.get('action')
            if steam_job == 'update':
                ok, message = _apply_steam_update()
            elif steam_job == 'cache':
                build = steam_client_build()
                if not build:
                    return record({'result': 'error',
                                    'reason': 'cannot determine the client build for caching'})
                try:
                    _cache_client_packages(build)
                    ok, message = True, 'cached Steam client %s' % build
                except OSError as error:
                    ok, message = False, 'could not cache the client: %s' % error
                if ok:
                    try:
                        steam_pending_path().unlink()
                    except OSError:
                        pass
            else:
                try:
                    steam_pending_path().unlink()
                except OSError:
                    pass
                ok, message = True, 'dropped unknown queued Steam task'
            if not ok:
                return record({'result': 'error', 'reason': message})
            # Fall through: a Steam change may come with owed Luna updates.
        # A check is read-only (gh only): notify mode must report even while
        # Steam runs. Only an actual install needs Steam and the sidecar gone.
        report = check_updates(force=True)
        installed = report.get('installed') or '?'
        latest = report.get('latest') or ''
        if not latest:
            return record({'result': 'error', 'reason': report.get('checkError') or 'no release found'})
        target = str(pending.get('tag') or '') if pending else ''
        if not target:
            target = latest
        if not pending and version_key(target) <= version_key(installed):
            return record({'result': 'current', 'version': installed})
        if mode == 'notify' and not pending:
            return record({'result': 'available', 'version': target, 'installed': installed})
        reasons = refuse_reasons(for_install=True)
        if reasons:
            return record({'result': 'deferred', 'reason': reasons[0]})

        # The tag reaches `gh release download` and names the version the bundle must carry:
        # pending.json is writable by anything running as the user, so it is validated first.
        if not isinstance(target, str) or not re.fullmatch(r'v?[0-9]+\.[0-9]+\.[0-9]+', target):
            return record({'result': 'error', 'reason': 'refusing a release tag that is not vX.Y.Z: %.40r' % (target,)})
        work = Path(tempfile.mkdtemp(prefix='luna-autoupdate-'))
        try:
            bundle, error = download_release(target, work)
            if bundle is None:
                return record({'result': 'error', 'reason': error, 'version': target})
            # Never --allow-downgrade here: an unattended update must not roll back.
            code = install_bundle(bundle, expect_version=target.lstrip('v'))
        finally:
            shutil.rmtree(work, ignore_errors=True)
        if code == 0 and pending_path.exists():
            pending_path.unlink()
        return record({'result': 'applied' if code == 0 else 'failed',
                       'version': target, 'installedBefore': installed, 'exit': code})
    except Exception as error:  # noqa: BLE001 - the timer must always leave a trace of why it failed
        return record({'result': 'failed', 'reason': str(error) or error.__class__.__name__})
    finally:
        _release_lock(lock_path)


# Files under ~/.local/share/slssteam-integrations/ that make up a backup.
# Volatile compressor state (jobs, logs, queue, status) is deliberately left
# out: it is rebuilt on the next run. Everything here is small, local JSON.
BACKUP_PATHS = (
    'playtime',
    'presence.json',
    'hltb.json',
    'hltb-library.json',
    'luna.json',
    'sgdb.json',
    'compressor/settings.json',
)


def _backup_entries():
    """(relative path, absolute path) pairs that exist right now."""
    base = data_dir()
    entries = []
    for rel in BACKUP_PATHS:
        path = base / rel
        if path.is_dir():
            for child in sorted(path.rglob('*')):
                if child.is_file():
                    entries.append((str(child.relative_to(base)), child))
        elif path.is_file():
            entries.append((rel, path))
    return entries


def cmd_backup(options):
    """Write one archive with the Luna user data plus a hash manifest."""
    entries = _backup_entries()
    if not entries:
        print('nothing to back up: %s holds no Luna data' % data_dir())
        return EXIT_OK
    out_dir = Path(options.out).expanduser() if options.out else Path(tempfile.gettempdir())
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime('%Y%m%d-%H%M%S')
    archive = out_dir / ('luna-backup-%s.tar.gz' % stamp)
    manifest = {'created': int(time.time()),
                'createdBy': installed_version(),
                'files': {rel: sha256(path) for rel, path in entries}}
    with tempfile.TemporaryDirectory(prefix='luna-backup-') as work:
        stage = Path(work) / 'stage'
        (stage / 'data').mkdir(parents=True)
        for rel, path in entries:
            dest = stage / 'data' / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dest)
        (stage / 'manifest.json').write_text(json.dumps(manifest, indent=2, sort_keys=True) + '\n')
        with tarfile.open(archive, 'w:gz') as tar:
            tar.add(stage / 'manifest.json', arcname='luna-backup/manifest.json')
            tar.add(stage / 'data', arcname='luna-backup/data')
    print('backup : %s (%d file(s))' % (archive, len(entries)))
    print('note   : includes the SteamGridDB API key (sgdb.json); keep the archive private')
    return EXIT_OK


def cmd_restore(options):
    """Restore a backup archive after validating its manifest.

    Refuses while Steam or the sidecar runs (same rule as install), and never
    overwrites a file that is newer than the archived copy unless --force.
    """
    archive = Path(options.archive).expanduser() if options.archive else None
    if not archive or not archive.is_file():
        print('backup archive not found: %s' % (options.archive or ''), file=sys.stderr)
        return 2
    if process_running('steam') or process_running('steamwebhelper') or process_running('lumen'):
        print('REFUSING to restore: Steam or the Lumen sidecar is running. '
              'Close it completely (Steam -> Exit) first.')
        return EXIT_REFUSED
    with tempfile.TemporaryDirectory(prefix='luna-restore-') as work:
        extract_dir = Path(work) / 'unpacked'
        extract_dir.mkdir()
        try:
            _safe_extract(archive, extract_dir)
        except (tarfile.TarError, ValueError, OSError) as error:
            print('REFUSING: cannot safely unpack the archive: %s' % error, file=sys.stderr)
            return 1
        roots = [entry for entry in extract_dir.iterdir() if entry.is_dir()]
        root = roots[0] if len(roots) == 1 else extract_dir
        manifest_path = root / 'manifest.json'
        data_root = root / 'data'
        if not manifest_path.is_file() or not data_root.is_dir():
            print('REFUSING: not a Luna backup (manifest.json + data/ required)', file=sys.stderr)
            return 1
        try:
            manifest = json.loads(manifest_path.read_text())
            pinned = manifest.get('files') or {}
        except ValueError:
            print('REFUSING: the backup manifest is not valid JSON', file=sys.stderr)
            return 1
        if not isinstance(pinned, dict):
            print('REFUSING: the backup manifest has no file list', file=sys.stderr)
            return 1
        problems, restored, skipped = [], 0, 0
        for rel in sorted(pinned):
            if '..' in Path(rel).parts or rel.startswith('/'):
                problems.append('unsafe path in manifest: %r' % rel)
                continue
            source = data_root / rel
            if not source.is_file():
                problems.append('archive is missing backed-up file: %s' % rel)
                continue
            if sha256(source) != pinned[rel]:
                problems.append('archived file does not match the manifest: %s' % rel)
                continue
            dest = data_dir() / rel
            if dest.is_file() and not options.force:
                try:
                    newer = dest.stat().st_mtime > source.stat().st_mtime
                except OSError:
                    newer = False
                if newer:
                    print('skip   : %s (current copy is newer; re-run with --force to overwrite)' % rel)
                    skipped += 1
                    continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_name(dest.name + '.luna-tmp')
            shutil.copy2(source, tmp)
            os.replace(tmp, dest)
            restored += 1
        if problems:
            print('REFUSING: the backup did not validate:')
            for problem in problems:
                print('  - %s' % problem, file=sys.stderr)
            return 1
    print('restored %d file(s), skipped %d newer file(s)' % (restored, skipped))
    return EXIT_OK


    print('restored %d file(s), skipped %d newer file(s)' % (restored, skipped))
    return EXIT_OK


