def cmd_install(options):
    targets = selected_addons(options)
    reasons = refuse_reasons(for_install=True)
    if reasons:
        print('REFUSING to install:')
        for reason in reasons:
            print('  - %s' % reason)
        return EXIT_REFUSED
    check = patcher('check')
    if check.returncode != 0:
        print('REFUSING to install: %s' % (check.stderr.strip() or check.stdout.strip()), file=sys.stderr)
        return EXIT_PATCH
    rows = plan(targets)
    to_write = [row for row in rows if row[4] in ('new', 'differs')]
    # A base (Lumen/Moon) update replaces boot.lua and wipes the bridge while
    # the payloads stay current. That is not "nothing to do": reinstalling the
    # bridge is part of every install, or Luna silently leaves the menu.
    needs_bridge = 'bridge compatible, not installed' in check.stdout
    print('plan for %s:' % ', '.join(targets))
    for addon, label, source, dest, state in rows:
        print('  %-9s %-22s %s' % (addon, label, state))
    if needs_bridge:
        print('  bridge    boot.lua               reinstall (a base update wiped it)')
    if options.dry_run:
        if not getattr(options, 'no_steam_machine', False):
            code = steam_machine_action('install', dry_run=True,
                                        migrate=getattr(options, 'migrate_steam_machine', False))
            if code:
                return code
        needed, why = steam_client_needed(options)
        if needed:
            print('steam     : would install/verify build %s executables, cache, pin and block updates (%s)'
                  % (STEAM_COMPAT_BUILD, why))
            print('steam     : %s' % steam_client_summary(getattr(options, 'client_cache', None)))
        else:
            print('steam     : Steam client untouched (%s)' % why)
        print('\nDRY RUN: %d file(s) would change%s.' % (len(to_write),
              ' + bridge reinstall' if needs_bridge else ', nothing was written'))
        return EXIT_OK
    differing = [row for row in to_write if row[4] == 'differs']
    if differing and not options.force:
        print('\nREFUSING to overwrite %d file(s) that differ from the pinned payloads:' % len(differing))
        for addon, label, source, dest, state in differing:
            print('  %s/%s (installed %s)' % (addon, label, dest))
        print('Re-run with --force if you really want to replace them.')
        return EXIT_NEEDS_FORCE

    touch_client, why = steam_client_needed(options)
    if touch_client:
        print('steam     : installing client build %s (%s)' % (STEAM_COMPAT_BUILD, why), flush=True)
        ok, message = ensure_compatible_steam(getattr(options, 'client_cache', None))
        print('steam     : %s' % message, file=sys.stdout if ok else sys.stderr)
        if not ok:
            return 1
    else:
        print('steam     : client untouched (%s)' % why)
    if not to_write and not needs_bridge:
        if not getattr(options, 'no_steam_machine', False):
            code = steam_machine_action('install', migrate=getattr(options, 'migrate_steam_machine', False))
            if code:
                return code
        write_install_snapshot()
        write_tooling()
        arm_lumen_hook()
        if read_steam_pin():
            ensure_steam_cfg_block()
        print('\nAlready installed and identical to the pinned payloads. Nothing to do.')
        return EXIT_OK
    only_bridge = needs_bridge and not to_write

    apply = Apply()
    try:
        apply.mkdir(addons_dir())
        for addon, label, source, dest, state in rows:
            if state in ('new', 'differs'):
                apply.write(source, dest)
        result = patcher('install')
        if result.returncode != 0:
            raise RuntimeError('boot.lua patch failed: %s' % (result.stderr.strip() or result.stdout.strip()))
        apply.did_patch = True
    except Exception as error:  # noqa: BLE001 - report and undo whatever was done
        apply.rollback()
        print('FAILED: %s' % error, file=sys.stderr)
        print('Every add-on change made by this run was rolled back.', file=sys.stderr)
        if touch_client:
            print('The separately verified Steam client installation remains in place; '
                  'its rollback backup was retained.', file=sys.stderr)
        return 1

    write_install_snapshot()
    write_tooling()
    arm_lumen_hook()
    if not getattr(options, 'no_steam_machine', False):
        code = steam_machine_action('install', migrate=getattr(options, 'migrate_steam_machine', False))
        if code:
            print('Add-ons installed, but SteamOS Mode shortcuts need attention; no custom launchers were overwritten.', file=sys.stderr)
            return code
    if read_steam_pin():
        # Base installers drop steam.cfg; a pin means updates must stay
        # blocked, so re-apply Luna's block right away (Steam is closed here).
        ensure_steam_cfg_block()
        print('steam.cfg : update block re-applied (a Steam build is pinned)')
    print('\ninstalled: %s' % ', '.join('%s %s' % (name, addon_version(name)) for name in targets))
    if only_bridge:
        print('bridge   : reinstalled into boot.lua (payloads were already current)')
    print('bridge   : %s' % (addons_dir() / 'bridge.lua'))
    print('boot.lua : %s' % patcher('check').stdout.strip())
    print('\nRestart Steam (Steam -> Exit, then launch it again) to load the add-ons.')
    return EXIT_OK


def cmd_uninstall(options):
    reasons = refuse_reasons(for_install=False)
    if reasons:
        print('REFUSING to uninstall:')
        for reason in reasons:
            print('  - %s' % reason)
        return EXIT_REFUSED
    targets = selected_addons(options) if options.only else installed_addons()
    if not targets:
        print('Nothing installed for the selected add-ons.')
        return EXIT_OK
    remaining = tuple(name for name in installed_addons() if name not in targets)
    hook_config = home() / '.local/share/slssteam-luna/config.json'
    if 'luna' in targets and hook_config.is_file() and (read_json_file(hook_config) or {}).get('enabled'):
        if lumen_hook('disable'):
            print('REFUSING uninstall: could not safely disable automatic Lumen recovery.', file=sys.stderr)
            return EXIT_PATCH
    if remaining:
        for name in targets:
            shutil.rmtree(addons_dir() / name, ignore_errors=True)
            print('removed %s; %s still installed, bridge and boot.lua left in place'
                  % (name, ', '.join(remaining)))
        return EXIT_OK
    # Last one out: restore Lumen before deleting the payload it points at, so a
    # failed restore can never leave a live bridge aiming at removed files.
    result = patcher('uninstall')
    if result.returncode != 0:
        print('REFUSING to remove the last add-on: %s'
              % (result.stderr.strip() or result.stdout.strip()), file=sys.stderr)
        return EXIT_PATCH
    print(result.stdout.strip())
    for name in targets:
        shutil.rmtree(addons_dir() / name, ignore_errors=True)
        print('removed %s' % name)
    bridge = addons_dir() / 'bridge.lua'
    if bridge.is_file():
        bridge.unlink()
        print('removed bridge.lua')
    snapshot = addons_dir() / 'luna-manifest.json'
    if snapshot.is_file():
        snapshot.unlink()
    # A full uninstall must not leave the unattended worker behind: without
    # the payloads it would only fail, and with them gone a timer could
    # reinstall after the user asked for removal.
    try:
        unit_dir, service, timer = unit_paths()
        subprocess.run(['systemctl', '--user', 'disable', '--now', 'luna-autoupdate.timer'],
                       capture_output=True)
        for path in (timer, service):
            try:
                if path.exists():
                    path.unlink()
            except OSError:
                pass
        subprocess.run(['systemctl', '--user', 'daemon-reload'], capture_output=True)
        pending = luna_tools_dir() / 'pending.json'
        try:
            if pending.exists():
                pending.unlink()
        except OSError:
            pass
        print('unattended updates: disabled and queued updates cleared')
    except Exception:  # noqa: BLE001 - uninstall must succeed even if systemd is absent
        pass
    print('Local playtime and presence data were left untouched.')
    launcher = home() / '.local/bin/luna'
    if launcher.is_file() and not launcher.is_symlink() and launcher.read_bytes() == CLI_LAUNCHER.encode():
        launcher.unlink()
    if steam_machine_action('uninstall'):
        print('Some SteamOS Mode shortcuts could not be removed; custom edits were preserved.', file=sys.stderr)
    return EXIT_OK


def untraceable_pins(manifest):
    """Pins whose source_commit is not a commit id, so the bytes cannot be traced."""
    found = []
    for section in ('shared', 'addons'):
        for name, entry in sorted(manifest.get(section, {}).items()):
            commit = str(entry.get('source_commit', ''))
            if not re.fullmatch(r'[0-9a-f]{40}', commit):
                found.append('%s (%s)' % (name, commit or 'no commit'))
    return found


def warn_untraceable(manifest, stream=sys.stdout):
    found = untraceable_pins(manifest)
    if found:
        print('\nWARNING: pinned to no commit, so these bytes cannot be traced to a source '
              'revision: %s. Commit the source and re-run vendor.' % ', '.join(found), file=stream)


def cmd_verify(options):
    manifest = load_manifest()
    problems = []
    checked = 0
    expected = {}
    for name in ADDONS:
        entry = manifest.get('addons', {}).get(name)
        if not entry:
            problems.append('manifest has no entry for %s' % name)
            continue
        expected_paths = set()
        for rel, meta in entry.get('files', {}).items():
            path = payload_path(name, meta['source_path'])
            expected_paths.add(path)
            if not path.is_file():
                problems.append('missing payload %s/%s' % (name, rel))
                continue
            if sha256(path) != meta['sha256']:
                problems.append('payload %s/%s does not match the manifest' % (name, rel))
            else:
                checked += 1
        expected[name] = expected_paths
    for rel, meta in manifest.get('shared', {}).items():
        path = ROOT / rel
        if not path.is_file():
            problems.append('missing shared file %s' % rel)
            continue
        if sha256(path) != meta['sha256']:
            problems.append('shared file %s does not match the manifest' % rel)
        else:
            checked += 1
    # A payload directory must contain exactly what the manifest lists, otherwise
    # a removed or renamed file could survive in the checkout unnoticed.
    for name, expected_paths in expected.items():
        directory = ROOT / 'addons' / name
        for path in sorted(directory.rglob('*')):
            if path.is_file() and path not in expected_paths:
                problems.append('unexpected file in the payload: %s' % path.relative_to(ROOT))
    print('manifest   : %d file(s) verified, generated %s' % (checked, manifest.get('generated', '?')))
    for name in ADDONS:
        entry = manifest.get('addons', {}).get(name, {})
        print('  %-9s %s %s @ %s' % (name, entry.get('plugin_version', '?'),
                                     entry.get('source_repo', '?'), entry.get('source_commit', '?')[:12]))
    if options.sources:
        for name, entry in manifest.get('addons', {}).items():
            if name not in SOURCED:
                continue
            base = Path(options.source_dir) if options.source_dir else (home() / 'Documents/Scripts')
            source = base / ('slsteam-' + name)
            # A plain copy is fine; a git checkout is not required to compare bytes.
            if not source.is_dir():
                print('  skip %s: no source checkout at %s' % (name, source))
                continue
            for rel, meta in entry.get('files', {}).items():
                origin = source / meta['source_path']
                if not origin.is_file():
                    problems.append('%s: source file missing: %s' % (name, origin))
                elif sha256(origin) != meta['sha256']:
                    problems.append('%s: %s differs from the source checkout (re-run vendor)'
                                    % (name, meta['source_path']))
            print('  %-9s compared against %s' % (name, source))
    if problems:
        print('\nPROBLEMS:')
        for problem in problems:
            print('  - %s' % problem)
        return 1
    warn_untraceable(manifest)
    print('\nOK: the payloads match the manifest.')
    return EXIT_OK


def cmd_vendor(options):
    # --only re-pins the named add-ons and keeps every other manifest entry as
    # it is, so refreshing one payload never silently moves the others.
    only = set(selected_addons(options)) if options.only else None
    sources = {}
    for name in SOURCED:
        if only is not None and name not in only:
            continue
        source = Path(options.source_dir or (home() / 'Documents/Scripts' / ('slsteam-' + name)))
        if not (source / '.git').is_dir():
            print('FAIL: no git checkout for %s at %s (pass --source-dir)' % (name, source), file=sys.stderr)
            return 1
        sources[name] = source

    def head(source):
        return subprocess.run(['git', '-C', str(source), 'rev-parse', 'HEAD'],
                              capture_output=True, text=True).stdout.strip()

    def show(source, commit, path):
        result = subprocess.run(['git', '-C', str(source), 'show', '%s:%s' % (commit, path)],
                                capture_output=True)
        if result.returncode != 0:
            raise SystemExit('FAIL: %s has no %s at %s' % (source, path, commit[:12]))
        return result.stdout

    def archive(source, commit, paths, dest):
        if dest.exists():
            shutil.rmtree(dest)
        dest.mkdir(parents=True, exist_ok=True)
        producer = subprocess.Popen(['git', '-C', str(source), 'archive', '--format=tar', commit, '--'] + list(paths),
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        consumer = subprocess.Popen(['tar', '-x', '-C', str(dest)], stdin=producer.stdout,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        producer.stdout.close()
        consumer.communicate()
        producer.wait()
        if producer.returncode != 0 or consumer.returncode != 0:
            raise SystemExit('FAIL: git archive from %s failed: %s'
                             % (source, producer.stderr.read().decode(errors='replace')))

    generated = time.strftime('%Y-%m-%dT%H:%M:%S%z')
    if only is None:
        manifest = {'generated': generated, 'shared': {}, 'addons': {}}
    else:
        manifest = json.loads(MANIFEST_PATH.read_text())
        manifest['generated'] = generated
    repo_commit = subprocess.run(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'],
                                 capture_output=True, text=True).stdout.strip()
    if only is None:
        vendor_shared(manifest, sources['playtime'], head, show, repo_commit)
    # First-party add-ons come from this repository, so they are pinned to this
    # checkout's HEAD - and only when the bytes are HEAD's, so a pin always names
    # a commit that really contains them.
    for name in FIRST_PARTY:
        if only is not None and name not in only:
            continue
        files = {}
        for source_rel, dest_rel in ADDON_FILES[name]:
            path = payload_path(name, source_rel)
            if not path.is_file():
                raise SystemExit('FAIL: this repository has no addons/%s/%s' % (name, source_rel))
            rel = path.relative_to(ROOT).as_posix()
            committed = subprocess.run(['git', '-C', str(ROOT), 'show', '%s:%s' % (repo_commit, rel)],
                                       capture_output=True)
            if committed.returncode != 0 or committed.stdout != path.read_bytes():
                raise SystemExit('FAIL: %s differs from HEAD; commit it before pinning' % rel)
            files[dest_rel] = {'source_path': source_rel, 'sha256': sha256(path)}
        manifest['addons'][name] = {
            'source_repo': 'https://github.com/pgm1207/Luna',
            'source_commit': repo_commit,
            'plugin_version': addon_version(name),
            'files': files,
        }
        print('pinned addons/%s from this repository@%s (%d files)' % (name, repo_commit[:12], len(files)))
    for name, source in sources.items():
        commit = head(source)
        paths = sorted({source_path for source_path, _ in ADDON_FILES[name]})
        archive(source, commit, paths, ROOT / 'addons' / name)
        files = {}
        for source_rel, dest_rel in ADDON_FILES[name]:
            path = payload_path(name, source_rel)
            if not path.is_file():
                raise SystemExit('FAIL: %s did not produce %s' % (name, source_rel))
            files[dest_rel] = {'source_path': source_rel, 'sha256': sha256(path)}
        manifest['addons'][name] = {
            'source_repo': 'https://github.com/pgm1207/slsteam-' + name,
            'source_commit': commit,
            'plugin_version': addon_version(name),
            'files': files,
        }
        print('pinned addons/%s from %s@%s (%d files)' % (name, source.name, commit[:12], len(files)))
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2, sort_keys=True) + '\n')
    print('wrote %s' % MANIFEST_PATH)
    return EXIT_OK


def vendor_shared(manifest, shared_source, head, show, repo_commit):
    # The shared bridge and patcher come from the playtime project; both projects
    # ship byte-identical copies, and `verify --sources` keeps them honest.
    shared_commit = head(shared_source)
    for rel, source_path in (('bridge/bridge.lua', 'bridge/bridge.lua'),
                             ('scripts/patch-lumen.py', 'scripts/patch-lumen.py')):
        (ROOT / rel).write_bytes(show(shared_source, shared_commit, source_path))
        manifest['shared'][rel] = {'source_repo': 'https://github.com/pgm1207/slsteam-playtime',
                                   'source_commit': shared_commit, 'source_path': source_path,
                                   'sha256': sha256(ROOT / rel)}
        print('pinned %s from %s@%s' % (rel, shared_source.name, shared_commit[:12]))
    # First-party scripts come from this repository, pinned to its HEAD.
    manifest['shared']['scripts/steam-machine.py'] = {
        'source_repo': 'https://github.com/pgm1207/Luna', 'source_commit': repo_commit,
        'source_path': 'scripts/steam-machine.py', 'sha256': sha256(ROOT / 'scripts/steam-machine.py')}
    hook_rel = 'scripts/lumen-hook.py'
    committed_hook = subprocess.run(['git', '-C', str(ROOT), 'show', repo_commit + ':' + hook_rel],
                                    capture_output=True)
    hook_clean = committed_hook.returncode == 0 and committed_hook.stdout == (ROOT / hook_rel).read_bytes()
    manifest['shared'][hook_rel] = {
        'source_repo': 'https://github.com/pgm1207/Luna',
        'source_commit': repo_commit if hook_clean else 'uncommitted-worktree',
        'source_path': hook_rel, 'sha256': sha256(ROOT / hook_rel)}


def steam_machine_action(action, dry_run=False, migrate=False):
    script = ROOT / 'scripts/steam-machine.py'
    if not script.is_file():
        script = luna_tools_dir() / 'steam-machine.py'
    if not script.is_file():
        if action == 'uninstall':
            return EXIT_OK
        print('Steam Machine helper missing; reinstall Luna from a complete bundle.', file=sys.stderr)
        return 1
    command = [sys.executable, str(script), action]
    if dry_run:
        command.append('--dry-run')
    if migrate:
        command.append('--migrate-legacy')
    return subprocess.run(command).returncode


def cmd_launchers(options):
    return steam_machine_action('install', options.dry_run, options.migrate_legacy)


