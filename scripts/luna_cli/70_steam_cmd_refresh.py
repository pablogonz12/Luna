def cmd_refresh(options):
    """Wizard: bring Steam to Valve's latest and reinstall Luna on top.

    Steam and Lumen must already be closed (this command never kills
    anything). Steps: update, pin, cache, install add-ons keeping the new
    client, verify. Any failure stops the wizard with the exact next action.
    """
    if options.dry_run:
        print('refresh : would update Steam (closed), pin, cache, install Luna --keep-steam-client, verify')
        print('\nDRY RUN: Steam stays untouched, nothing was written.')
        return EXIT_OK
    reasons = refuse_reasons(for_install=True)
    if reasons:
        print('REFUSING to refresh:')
        for reason in reasons:
            print('  - %s' % reason)
        return EXIT_REFUSED
    ok, message, build = _refresh_steam_side()
    print('refresh : %s' % message, file=sys.stdout if ok else sys.stderr)
    if not ok:
        return 1
    install_options = argparse.Namespace(only=[], force=False, dry_run=False,
                                         keep_steam_client=True, no_steam_machine=False,
                                         migrate_steam_machine=False)
    code = cmd_install(install_options)
    if code != EXIT_OK:
        print('refresh : Steam is on %s, but the Luna install needs attention (exit %d)'
              % (build, code), file=sys.stderr)
        return code
    verify_options = argparse.Namespace(sources=False, source_dir=None)
    code = cmd_verify(verify_options)
    if code != EXIT_OK:
        print('refresh : installed, but verification failed (exit %d)' % code, file=sys.stderr)
        return code
    print('\nrefresh : Steam %s + Luna installed and verified. Restart Steam to load the add-ons.' % build)
    return EXIT_OK


def cmd_steam(options):
    """Pin, cache and recover the Steam client build (see `luna steam --help`)."""
    action = getattr(options, 'steam_action', '') or 'status'
    if action == 'status':
        state = steam_state()
        print('client : %s' % (state['build'] or 'unknown (Steam never logged a version here)'))
        print('pinned : %s' % (state['pinned'] or 'none (run: luna steam pin)'))
        print('packages: %s' % ('match the pin' if state['match']
                                 else 'DIFFER from the pin - run: luna steam recover (Steam closed)'
                                 if state['pinned'] and state['cacheReady']
                                 else 'unknown (no cache)'))
        print('updates: %s' % ('blocked (steam.cfg)' if state['updatesBlocked'] else 'NOT blocked'))
        if state['hardLock']:
            print('lock   : HARD (ubuntu12 rollback, package/ %s)'
                  % ('read-only' if state['packageReadOnly']
                     else 'WRITABLE - re-run: luna steam lock'))
        if state['pacmanManaged']:
            print('pacman : Steam is system-managed%s'
                  % (', ignored on updates' if state['pacmanIgnored']
                     else ' and NOT ignored - add "IgnorePkg = steam" to /etc/pacman.conf (needs sudo)'))
        for name, digest in sorted(state['slssteam'].items()):
            print('%-16s %s' % (name + ':', digest[:12] if digest else '(missing)'))
        if state['slssteamDrift']:
            print('WARN   : SLSsteam changed since the pin: %s' % ', '.join(state['slssteamDrift']))
        print('cache  : %s' % ('build ' + state['cachedBuild'] if state['cacheReady']
                                else 'empty (run: luna steam cache with Steam closed)'))
        pending = read_steam_pending()
        if pending:
            print('queued : %s (requested %s; timer applies it while Steam is closed, or: luna steam cancel)'
                  % (pending.get('action', '?'),
                     time.strftime('%Y-%m-%d %H:%M', time.localtime(int(pending.get('at') or 0)))))
        return EXIT_OK
    if action == 'pin':
        build = options.build or steam_client_build()
        if not build:
            print('cannot determine the client build; pass --build N explicitly', file=sys.stderr)
            return 2
        pin = new_steam_pin(build)
        luna_tools_dir().mkdir(parents=True, exist_ok=True)
        write_json_file(steam_pin_path(), pin)
        ensure_steam_cfg_block()
        print('pinned Steam client %s with current SLSsteam hashes; updates blocked via steam.cfg' % build)
        return EXIT_OK
    if action == 'lock' and getattr(options, 'adopt', False):
        code, message = adopt_steam_lock()
        print(message, file=sys.stdout if code == 0 else sys.stderr)
        return code
    if action == 'lock':
        def confirm():
            if not sys.stdin.isatty():
                return False
            blockers = gui_describe_blockers()  # best effort; '' when unavailable
            if blockers:
                print('Steam currently shows: %s' % blockers)
            answer = input('Steam is running and will be killed (kill -9). Continue? [y/N] ')
            return answer.strip().lower() in ('y', 'yes')
        code, message = _lock_steam_client(getattr(options, 'force', False), confirm)
        print(message, file=sys.stdout if code == 0 else sys.stderr)
        if getattr(options, 'relaunch', False):
            # Detached-job mode (in-Steam UI): report, then bring Steam back even on failure.
            _notify('Luna: Steam updates blocked' if code == 0 else 'Luna: could not block Steam updates',
                    message[:200])
            _launch_steam_detached()
        return code
    if action == 'unlock':
        if getattr(options, 'relaunch', False):
            # Close Steam first so the next start runs the bootstrapper and self-updates.
            if not _close_steam_gracefully():
                print('Steam did not close in time; nothing was changed', file=sys.stderr)
                _notify('Luna: Steam update not started', 'Steam did not close; try again.')
                return 1
            code = _unlock_steam_client(False)
            if code == EXIT_OK:
                _notify('Luna: Steam unblocked', 'Steam is restarting and will update to the latest client.')
                _launch_steam_detached()
            else:
                _notify('Luna: could not unblock Steam', 'See the Luna steam job log.')
            return code
        return _unlock_steam_client(getattr(options, 'now', False))
    if action == 'unpin':
        if steam_hard_lock_active():
            print('the hard lock is active (package/ read-only); run: luna steam unlock',
                  file=sys.stderr)
            return 1
        try:
            steam_pin_path().unlink()
            print('pin removed')
        except OSError:
            print('no pin to remove')
        if remove_steam_cfg_block():
            print('update block removed: Steam may update itself again')
        else:
            print('note: steam.cfg left untouched (it holds more than the Luna block)')
        return EXIT_OK
    if action == 'cache':
        if process_running('steam') or process_running('steamwebhelper'):
            # Queue it: the timer runs while Steam is closed and caches then.
            existing = read_steam_pending()
            if existing:
                print('a Steam task is already queued (%s); run: luna steam cancel first'
                      % existing.get('action', '?'), file=sys.stderr)
                return 1
            write_json_file(steam_pending_path(), {'action': 'cache', 'at': int(time.time())})
            print('Steam is running: caching queued for the next closed-Steam window')
            return EXIT_OK
        source = steam_home() / 'package'
        if not source.is_dir():
            print('no Steam package directory to cache at %s' % source, file=sys.stderr)
            return 1
        build = str(options.build or '') or steam_client_build()
        if not build or build == '0':
            print('cannot determine the client build; launch Steam once, then retry', file=sys.stderr)
            return 1
        try:
            cache = _cache_client_packages(build)
        except (OSError, ValueError) as error:
            print('cannot cache the Steam client: %s' % error, file=sys.stderr)
            return 1
        print('cached Steam client %s (%s)' % (build, cache))
        return EXIT_OK
    if action == 'update':
        # The blessed path for a Steam client update: queue it here (menu or
        # shell); the timer applies it while Steam is closed, then re-blocks.
        # --now applies it right away instead (Steam must already be closed).
        if steam_hard_lock_active():
            print('the hard lock is active; run: luna steam unlock --now to let Steam update',
                  file=sys.stderr)
            return 1
        if getattr(options, 'now', False):
            if process_running('steam') or process_running('steamwebhelper') or process_running('lumen'):
                print('REFUSING: Steam or the Lumen sidecar is running. '
                      'Close it completely (Steam -> Exit) first.')
                return EXIT_REFUSED
            ok, message = _apply_steam_update()
            print(message, file=sys.stderr if not ok else sys.stdout)
            return EXIT_OK if ok else 1
        existing = read_steam_pending()
        if existing:
            print('a Steam task is already queued (%s); run: luna steam cancel first'
                  % existing.get('action', '?'), file=sys.stderr)
            return 1
        write_json_file(steam_pending_path(), {'action': 'update', 'at': int(time.time())})
        if process_running('steam') or process_running('steamwebhelper'):
            print('Steam update queued: it applies the next time Steam is closed '
                  '(timer), then updates are blocked again')
        else:
            print('Steam update queued: enable the timer or run: luna steam update --now')
        return EXIT_OK
    if action == 'cancel':
        try:
            steam_pending_path().unlink()
            print('queued Steam task cancelled')
        except OSError:
            print('nothing queued')
        return EXIT_OK
    if action == 'recover':
        if process_running('steam') or process_running('steamwebhelper') or process_running('lumen'):
            print('REFUSING to recover: Steam or the Lumen sidecar is running. '
                  'Close it completely (Steam -> Exit) first.')
            return EXIT_REFUSED
        want = str(options.build or '')
        pin = read_steam_pin()
        if not want:
            want = str(pin.get('clientBuild') or '')
        if not want:
            print('nothing pinned; run: luna steam pin [--build N]', file=sys.stderr)
            return 2
        ok, message = _recover_steam_client(want)
        if not ok:
            print(message, file=sys.stderr)
            return 1
        print(message)
        print('launch Steam and verify, then run: luna steam pin')
        return EXIT_OK
    print('unknown steam action: %s (status|pin|unpin|cache|recover|update|cancel|lock|unlock)' % action, file=sys.stderr)
    return 2


def steam_drift_note():
    """One-line summary for preflight/Health, or '' when nothing is wrong."""
    state = steam_state()
    if state.get('binariesMatch') is False:
        return 'Steam client executables changed; close Steam and run: luna steam recover'
    if state['pinned'] and state['cacheReady'] and state['packagesMatch'] is False:
        return ('Steam client packages changed since the pin; close Steam and run: luna steam recover')
    if state['slssteamDrift']:
        return ('SLSsteam changed since the pin (%s); re-pin after verifying: luna steam pin'
                % ', '.join(state['slssteamDrift']))
    if state['pinned'] and state['pacmanManaged'] and not state['pacmanIgnored']:
        return ('Steam is pacman-managed and not ignored: a system update can replace the launcher; '
                'add "IgnorePkg = steam" under [options] in /etc/pacman.conf (needs sudo)')
    if state['pinned'] and not state['updatesBlocked']:
        return 'a Steam build is pinned but updates are not blocked; run: luna steam recover or re-add steam.cfg'
    if state['pinned'] and not state['cacheReady'] and state['build'] and state['build'] != state['pinned']:
        return ('Steam client %s does not match the pin %s and no cache exists; '
                'cache a known-good build: luna steam cache (Steam closed)'
                % (state['build'], state['pinned']))
    return ''


