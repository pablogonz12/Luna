# ===========================================================================
# luna saves: share game saves that have no Steam Cloud between machines.
#
# Syncthing already syncs ~/.local, so a real directory under
# ~/.local/share/game-saves/<key>/ reaches the other machine; the game's own
# save directory (a Proton prefix, ~/.config/unity3d/...) becomes a symlink into
# that store. saves.json in the store maps key -> save locations (relative to
# the prefix / ~/.config / $HOME, so they are valid on every machine).
# Nothing is ever deleted: whatever Luna replaces is renamed to a timestamped
# `<name>.luna-backup-<stamp>` sibling (or stored under _backups/ in the store).
# ===========================================================================

SAVES_PROTON_ROOTS = ('AppData/Local', 'AppData/LocalLow', 'AppData/Roaming', 'Documents', 'Saved Games')
SAVES_NOISE = {
    'microsoft', 'temp', 'packages', 'crashdumps', 'd3dscache', 'nvidia', 'amd', 'openvr', 'dxvk',
    'unrealengine', 'crashreportclient', 'steam', 'epic', 'epic games', 'ubisoft', 'ubisoft game launcher',
    'videos', 'templates', 'pictures', 'music', 'downloads', 'desktop', 'my music', 'my pictures',
    'my videos', 'programs', 'wine', 'mesa_shader_cache', 'cache', 'unity', 'goldberg uplayemu saves',
    'oculus', 'metaxr', 'easyanticheat', 'gameanalytics', 'unity technologies', 'crashes',
}
SAVES_SKIP_STUDIOS = {'ludeon studios'}  # RimWorld has its own Syncthing folder
SAVES_SYNC_SAFE = ('.local/share/Steam',)


def saves_store():
    return home() / '.local/share/game-saves'


def saves_map_path():
    return saves_store() / 'saves.json'


def saves_load_map():
    data = read_json_file(saves_map_path())
    if not isinstance(data, dict) or not isinstance(data.get('games'), dict):
        return {'version': 1, 'games': {}}
    return data


def saves_write_map(data):
    saves_store().mkdir(parents=True, exist_ok=True)
    target = saves_map_path()
    temp = target.with_name('.saves.json.%d.tmp' % os.getpid())
    temp.write_text(json.dumps(data, indent=2, sort_keys=True) + '\n')
    os.replace(temp, target)


def saves_libraries():
    """Steam library roots that exist on this machine (Steam home first)."""
    roots = [steam_home()]
    vdf = steam_home() / 'steamapps/libraryfolders.vdf'
    try:
        roots.extend(Path(m) for m in re.findall(r'"path"\s+"([^"]+)"', vdf.read_text(errors='replace')))
    except OSError:
        pass
    seen, result = set(), []
    for root in roots:
        try:
            real = os.path.realpath(root)
        except OSError:
            continue
        if real not in seen and os.path.isdir(os.path.join(real, 'steamapps')):
            seen.add(real)
            result.append(Path(real))
    return result


def saves_installed():
    """{appid: {'name', 'installdir', 'library'}} from appmanifest_*.acf."""
    games = {}
    for library in saves_libraries():
        for manifest in sorted((library / 'steamapps').glob('appmanifest_*.acf')):
            try:
                text = manifest.read_text(errors='replace')
            except OSError:
                continue

            def field(name):
                found = re.search(r'"%s"\s+"([^"]*)"' % name, text)
                return found.group(1) if found else ''
            appid = field('appid') or manifest.stem.split('_', 1)[1]
            games.setdefault(appid, {'name': field('name') or appid, 'installdir': field('installdir'),
                                     'library': library})
    return games


def saves_has_cloud(appid):
    userdata = steam_home() / 'userdata'
    try:
        return any((u / str(appid) / 'remotecache.vdf').is_file() for u in userdata.iterdir())
    except OSError:
        return False


def saves_prefix_user(appid):
    """Wine user dir of the app's Proton prefix on this machine, or None."""
    for library in saves_libraries():
        user = library / 'steamapps/compatdata' / str(appid) / 'pfx/drive_c/users/steamuser'
        if user.is_dir():
            return user
    return None


def saves_resolve(kind, rel, appid=None):
    """Local Path of a save location, or None when its root does not exist here."""
    if kind == 'proton':
        user = saves_prefix_user(appid)
        return user / rel if user else None
    if kind == 'config':
        return home() / '.config' / rel
    if kind == 'home':
        return home() / rel
    return None


def saves_store_path(key, kind, rel):
    return saves_store() / key / kind / rel


def saves_files(path):
    """[(relative path, size)] of regular files below path, not following symlinks."""
    out = []
    for dirpath, _dirs, names in os.walk(str(path)):
        for name in names:
            full = os.path.join(dirpath, name)
            if os.path.isfile(full) and not os.path.islink(full):
                out.append((os.path.relpath(full, str(path)), os.path.getsize(full)))
    return sorted(out)


def saves_has_data(path):
    path = Path(path)
    return path.is_dir() and not path.is_symlink() and bool(saves_files(path))


def saves_slug(text):
    return re.sub(r'[^a-z0-9]+', '-', text.lower()).strip('-') or 'save'


def saves_norm(text):
    return re.sub(r'[^a-z0-9]', '', text.lower())


def saves_stamp():
    return time.strftime('%Y%m%d-%H%M%S')


def saves_same_fs(a, b):
    return os.stat(str(a)).st_dev == os.stat(str(b)).st_dev


def saves_backup_name(path):
    path = Path(path)
    target = path.with_name('%s.luna-backup-%s' % (path.name, saves_stamp()))
    n = 1
    while target.exists() or target.is_symlink():
        n += 1
        target = path.with_name('%s.luna-backup-%s-%d' % (path.name, saves_stamp(), n))
    return target


def saves_copy_verified(source, dest):
    """Copy a tree to dest (must not exist) via a temp sibling; verify by sha256."""
    temp = dest.with_name('.%s.luna-tmp-%d' % (dest.name, os.getpid()))
    if temp.exists():
        shutil.rmtree(str(temp))
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(str(source), str(temp), symlinks=True)
    want, have = saves_files(source), saves_files(temp)
    if want != have or any(sha256(Path(source) / r) != sha256(temp / r) for r, _ in want):
        shutil.rmtree(str(temp))
        raise OSError('copy of %s did not verify' % source)
    os.rename(str(temp), str(dest))


def saves_move_tree(source, dest):
    """Move a real directory to dest (not existing). Cross-filesystem: copy+verify,
    then the original is renamed to a backup (returned), never deleted."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if saves_same_fs(source, dest.parent):
        os.rename(str(source), str(dest))
        return None
    saves_copy_verified(source, dest)
    backup = saves_backup_name(source)
    os.rename(str(source), str(backup))
    return backup


def saves_remove_if_empty(path):
    """Remove a directory tree that holds no files (empty dirs only)."""
    if os.path.isdir(str(path)) and not os.path.islink(str(path)) and not saves_files(path):
        shutil.rmtree(str(path))


def saves_game_running(appid):
    if not appid:
        return False
    try:
        result = subprocess.run(['pgrep', '-u', str(os.getuid()), '-f', 'AppId=%s( |$)' % appid],
                                capture_output=True, text=True)
    except FileNotFoundError:
        return False
    return result.returncode == 0


def saves_link_points_to(link, target):
    return os.path.islink(str(link)) and os.path.realpath(str(link)) == os.path.realpath(str(target))


def saves_dir_bytes(path):
    return sum(size for _, size in saves_files(path))


# -- discovery ---------------------------------------------------------------

def saves_match_game(name, installed):
    """installed appid whose name / installdir matches a Unity <Game> directory."""
    want = saves_norm(name)
    if not want:
        return None
    for appid, info in installed.items():
        if want in (saves_norm(info['name']), saves_norm(info['installdir'])):
            return appid
    if len(want) >= 6:  # "Hollow Knight Silksong" vs "Hollow Knight: Silksong (Demo)"
        for appid, info in installed.items():
            known = saves_norm(info['name'])
            if known.startswith(want) or want.startswith(known) and len(known) >= 6:
                return appid
    return None


def saves_scan():
    """Candidate save locations: dicts with key, appid, name, kind, rel, path, linked, cloud."""
    installed = saves_installed()
    mapped = saves_load_map()['games']
    rows = []

    def add(key, appid, name, kind, rel, path, cloud):
        store = saves_store_path(key, kind, rel)
        rows.append({'key': key, 'appid': appid, 'name': name, 'kind': kind, 'rel': rel, 'path': str(path),
                     'files': 0 if os.path.islink(str(path)) else len(saves_files(path)),
                     'bytes': 0 if os.path.islink(str(path)) else saves_dir_bytes(path),
                     'linked': saves_link_points_to(path, store), 'steamCloud': cloud,
                     'inStore': saves_has_data(store), 'inMap': rel in [
                         l.get('rel') for l in mapped.get(key, {}).get('links', []) if l.get('kind') == kind]})

    for appid, info in sorted(installed.items(), key=lambda kv: kv[1]['name'].lower()):
        user = saves_prefix_user(appid)
        if not user:
            continue
        cloud = saves_has_cloud(appid)
        for root in SAVES_PROTON_ROOTS:
            base = user / root
            if not base.is_dir() or base.is_symlink():
                continue
            for entry in sorted(base.iterdir()):
                if entry.name.lower() in SAVES_NOISE or not entry.is_dir():
                    continue
                rel = '%s/%s' % (root, entry.name)
                if entry.is_symlink() and not saves_link_points_to(entry, saves_store_path(str(appid), 'proton', rel)):
                    continue
                if entry.is_symlink() or saves_has_data(entry):
                    add(str(appid), str(appid), info['name'], 'proton', rel, entry, cloud)
    unity = home() / '.config/unity3d'
    if unity.is_dir():
        for studio in sorted(unity.iterdir()):
            if not studio.is_dir() or studio.name.lower() in SAVES_SKIP_STUDIOS or studio.is_symlink():
                continue
            for game in sorted(studio.iterdir()):
                if not game.is_dir():
                    continue
                rel = 'unity3d/%s/%s' % (studio.name, game.name)
                appid = saves_match_game(game.name, installed)
                key = appid or saves_slug('%s-%s' % (studio.name, game.name))
                if game.is_symlink():
                    # linked under whichever key its target says
                    target = os.path.realpath(str(game))
                    if not target.startswith(os.path.realpath(str(saves_store())) + os.sep):
                        continue
                    key = os.path.relpath(target, os.path.realpath(str(saves_store()))).split(os.sep)[0]
                elif not saves_has_data(game):
                    continue
                add(key, appid, installed[appid]['name'] if appid else game.name, 'config', rel, game,
                    saves_has_cloud(appid) if appid else False)
    return rows


def saves_candidates(rows):
    """Scan rows worth syncing: no Steam Cloud (linked rows always kept)."""
    return [r for r in rows if r['linked'] or not r['steamCloud']]


# -- targets -----------------------------------------------------------------

def saves_classify_path(arg):
    """Path argument -> (key, name, kind, rel, appid)."""
    path = Path(os.path.abspath(os.path.expanduser(arg)))
    parts = path.parts
    if 'compatdata' in parts:
        i = parts.index('compatdata')
        marker = ('pfx', 'drive_c', 'users', 'steamuser')
        if len(parts) > i + 6 and parts[i + 2:i + 6] == marker:
            appid = parts[i + 1]
            info = saves_installed().get(appid, {})
            return appid, info.get('name', appid), 'proton', '/'.join(parts[i + 6:]), appid
        raise ValueError('%s is inside a Proton prefix but not below its steamuser directory' % path)
    config = home() / '.config'
    try:
        rel = path.relative_to(config)
    except ValueError:
        rel = None
    if rel is not None and len(rel.parts) == 3 and rel.parts[0] == 'unity3d':
        appid = saves_match_game(rel.parts[2], saves_installed())
        name = saves_installed()[appid]['name'] if appid else rel.parts[2]
        return (appid or saves_slug('%s-%s' % rel.parts[1:]), name, 'config', str(rel), appid)
    try:
        rel = path.relative_to(home())
    except ValueError:
        raise ValueError('%s is not below %s' % (path, home()))
    if not rel.parts or str(path).startswith(str(saves_store())):
        raise ValueError('refusing to link %s' % path)
    return saves_slug(rel.name), rel.name, 'home', str(rel), None


def saves_targets(arg):
    """[(key, name, appid, kind, rel)] for an appid, key or path argument."""
    if os.sep in arg or arg.startswith(('~', '.')):
        key, name, kind, rel, appid = saves_classify_path(arg)
        return [(key, name, appid, kind, rel)]
    games = saves_load_map()['games']
    found = {}
    for link in games.get(arg, {}).get('links', []):
        found[(link['kind'], link['rel'])] = (arg, games[arg].get('name', arg), games[arg].get('appid'),
                                              link['kind'], link['rel'])
    for row in saves_scan():
        if row['key'] == arg or row['appid'] == arg:
            found[(row['kind'], row['rel'])] = (row['key'], row['name'], row['appid'], row['kind'], row['rel'])
    return list(found.values())


def saves_record(key, name, appid, kind, rel):
    data = saves_load_map()
    game = data['games'].setdefault(key, {'name': name, 'appid': appid, 'links': []})
    game['name'] = name
    if not any(l.get('kind') == kind and l.get('rel') == rel for l in game['links']):
        game['links'].append({'kind': kind, 'rel': rel})
    saves_write_map(data)


# -- link / unlink -----------------------------------------------------------

def saves_link_one(target, prefer=None, dry_run=False):
    """Link one location. Returns (status, message); status in
    linked|already|skipped|conflict|error."""
    key, name, appid, kind, rel = target
    local = saves_resolve(kind, rel, appid)
    label = '%s [%s]' % (name, rel)
    if local is None:
        return 'skipped', '%s: no Proton prefix on this machine - launch the game once, then retry' % label
    store = saves_store_path(key, kind, rel)
    if os.path.islink(str(local)):
        if saves_link_points_to(local, store):
            if not dry_run:
                saves_record(key, name, appid, kind, rel)
            return 'already', '%s: already linked' % label
        return 'error', '%s: %s is a symlink to %s, not to the store' % (label, local, os.path.realpath(str(local)))
    if kind == 'proton' and saves_prefix_user(appid) is None:
        return 'skipped', '%s: no Proton prefix on this machine - launch the game once, then retry' % label
    if saves_game_running(appid):
        return 'error', '%s: the game is running; close it first' % label
    local_has, store_has = saves_has_data(local), saves_has_data(store)
    if local.exists() and not local.is_dir():
        return 'error', '%s: %s is not a directory' % (label, local)
    if not local_has and not store_has:
        return 'skipped', '%s: no save data here or in the store yet' % label
    if local_has and store_has and prefer is None:
        return 'conflict', ('%s: both this machine and the store have saves; pick one with '
                            '--prefer local|store (the other side is backed up first)' % label)
    use_local = local_has and (not store_has or prefer == 'local')
    notes = []
    if dry_run:
        return 'linked', '%s: would link %s -> %s (%s)' % (
            label, local, store, 'move local into store' if use_local else 'use store, back up local')
    store.parent.mkdir(parents=True, exist_ok=True)
    if use_local:
        if store.exists() or store.is_symlink():  # store data being replaced (prefer local)
            backup = saves_store() / '_backups' / ('%s-%s-%s' % (key, saves_stamp(), saves_slug(rel)))
            backup.parent.mkdir(parents=True, exist_ok=True)
            os.rename(str(store), str(backup))
            notes.append('previous store copy kept in %s' % backup)
        backup = saves_move_tree(local, store)
        if backup:
            notes.append('original kept as %s' % backup)
    else:
        if local_has:
            backup = saves_backup_name(local)
            os.rename(str(local), str(backup))
            notes.append('local copy backed up to %s' % backup)
        else:
            saves_remove_if_empty(local)
    local.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(str(store), str(local))
    saves_record(key, name, appid, kind, rel)
    return 'linked', '%s: linked%s' % (label, ('; ' + '; '.join(notes)) if notes else '')


def saves_unlink_one(target, dry_run=False):
    key, name, appid, kind, rel = target
    local = saves_resolve(kind, rel, appid)
    label = '%s [%s]' % (name, rel)
    store = saves_store_path(key, kind, rel)
    if local is None or not os.path.islink(str(local)):
        return 'skipped', '%s: not linked here' % label
    if not saves_link_points_to(local, store):
        return 'error', '%s: %s does not point into the store' % (label, local)
    if saves_game_running(appid):
        return 'error', '%s: the game is running; close it first' % label
    if dry_run:
        return 'linked', '%s: would copy the store back to %s and remove the symlink' % (label, local)
    if store.is_dir():
        temp = local.with_name('.%s.luna-unlink-%d' % (local.name, os.getpid()))
        saves_copy_verified(store, temp)
        os.unlink(str(local))
        os.rename(str(temp), str(local))
    else:
        os.unlink(str(local))
    return 'linked', '%s: unlinked (a copy stays in the store at %s)' % (label, store)


def saves_all_targets():
    targets = []
    for key, game in sorted(saves_load_map()['games'].items()):
        for link in game.get('links', []):
            targets.append((key, game.get('name', key), game.get('appid'), link['kind'], link['rel']))
    return targets


def saves_conflicts():
    store = saves_store()
    return sorted(p for p in store.rglob('*.sync-conflict-*')) if store.is_dir() else []


def saves_link_state(target):
    """linked|unlinked|broken|wrong|no-prefix|store-missing for a mapped location."""
    key, _name, appid, kind, rel = target
    local = saves_resolve(kind, rel, appid)
    store = saves_store_path(key, kind, rel)
    if local is None:
        return 'no-prefix'
    if os.path.islink(str(local)):
        if saves_link_points_to(local, store):
            return 'linked' if store.exists() else 'store-missing'
        return 'broken' if not local.exists() else 'wrong'
    return 'unlinked'


def cmd_saves(options):
    action = options.saves_action
    if action in ('scan', 'status'):
        return saves_cmd_report(options)
    prefer = options.prefer
    if options.all:
        if options.target:
            print('luna saves %s: give a target or --all, not both' % action, file=sys.stderr)
            return EXIT_REFUSED
        targets = saves_all_targets()
        if not targets:
            print('no linked games in %s yet' % saves_map_path())
            return EXIT_OK
    elif options.target:
        try:
            targets = saves_targets(options.target)
        except ValueError as error:
            print('luna saves: %s' % error, file=sys.stderr)
            return EXIT_REFUSED
        if not targets:
            print('luna saves: nothing found for %s (try `luna saves scan`)' % options.target, file=sys.stderr)
            return 1
    else:
        print('luna saves %s: give an appid/path or --all' % action, file=sys.stderr)
        return EXIT_REFUSED
    failed = False
    for target in targets:
        try:
            if action == 'link':
                status, message = saves_link_one(target, prefer, options.dry_run)
            else:
                status, message = saves_unlink_one(target, options.dry_run)
        except OSError as error:
            status, message = 'error', '%s [%s]: %s' % (target[1], target[4], error)
        print('%-8s %s' % (status.upper(), message))
        failed = failed or status in ('error', 'conflict')
    return 1 if failed else EXIT_OK


def saves_cmd_report(options):
    if options.saves_action == 'scan':
        rows = saves_candidates(saves_scan())
        hidden = [r for r in rows if r['appid'] is None]
        if not options.json and not options.all:
            rows = [r for r in rows if r['appid'] is not None]
        if options.json:
            print(json.dumps({'version': 1, 'candidates': rows}, indent=2, sort_keys=True))
            return EXIT_OK
        if not rows:
            print('no unsynced save candidates found')
            return EXIT_OK
        for row in rows:
            state = 'linked' if row['linked'] else ('in store' if row['inStore'] else 'not linked')
            appid = row['appid'] or '-'
            print('%-8s %-34s %-12s %6.1f MB  %s' % (appid, row['name'][:34], state,
                                                     row['bytes'] / 1e6, row['rel']))
        if hidden and not options.all:
            print('(%d Unity save dir(s) match no installed game; `luna saves scan --all` lists them)' % len(hidden))
        todo = [r for r in rows if not r['linked']]
        if todo:
            print('\n%d candidate(s) not linked: luna saves link <appid|path>' % len(todo))
        return EXIT_OK
    # status
    entries = []
    for target in saves_all_targets():
        entries.append({'key': target[0], 'name': target[1], 'kind': target[3], 'rel': target[4],
                        'state': saves_link_state(target)})
    conflicts = [str(p) for p in saves_conflicts()]
    if options.json:
        print(json.dumps({'version': 1, 'store': str(saves_store()), 'links': entries,
                          'conflicts': conflicts}, indent=2, sort_keys=True))
    else:
        print('store: %s' % saves_store())
        for e in entries:
            print('%-14s %-30s %s' % (e['state'].upper(), e['name'][:30], e['rel']))
        for path in conflicts:
            print('CONFLICT       %s' % path)
        if not entries:
            print('no linked games')
    bad = [e for e in entries if e['state'] in ('broken', 'wrong', 'store-missing')]
    return 1 if bad or conflicts else EXIT_OK


def _doctor_saves():
    data = saves_load_map()
    if not data['games']:
        return []
    found = []
    states = [(t, saves_link_state(t)) for t in saves_all_targets()]
    bad = [(t, s) for t, s in states if s in ('broken', 'wrong', 'store-missing')]
    for target, state in bad:
        found.append(_finding('saves.link', 'warn', 'save link for %s (%s) is %s' % (target[1], target[4], state),
                              'luna saves status; luna saves unlink/link %s' % (target[2] or target[0])))
    pending = [t for t, s in states if s == 'unlinked']
    if pending:
        found.append(_finding('saves.unlinked', 'info', '%d synced save location(s) not linked on this machine: %s'
                              % (len(pending), ', '.join(sorted({t[1] for t in pending})[:4])),
                              'luna saves link --all'))
    conflicts = saves_conflicts()
    if conflicts:
        found.append(_finding('saves.conflicts', 'warn', '%d Syncthing conflict file(s) in %s: %s'
                              % (len(conflicts), saves_store(), ', '.join(str(p) for p in conflicts[:3])),
                              'compare each with its original and keep one'))
    if not found:
        found.append(_finding('saves.links', 'ok', 'game save links healthy (%d)' % len(states)))
    return found
# ======================== end luna saves ===================================
