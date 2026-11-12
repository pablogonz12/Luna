# ---------------------------------------------------------------------------
# Multi-machine reconcile: Steam's localconfig.vdf is Syncthing-synced so both
# machines share recents, collections and per-game state, but two Steam
# instances writing it concurrently produce sync-conflict copies and
# last-writer-wins clobbering ("gaming mode forgot what desktop mode played").
# Reconcile merges the conflict copies into the live file (max LastPlayed per
# app, union of recent-played entries) instead of picking one file blindly.
# Idempotent: re-running over archived conflicts changes nothing.

_VDF_TOKEN = re.compile(r'"((?:[^"\\]|\\.)*)"|([{}])')
_NONSTEAM_MARKER = 0x02000000


def _vdf_unescape(text):
    return text.replace('\\\\', '\x00').replace('\\"', '"').replace('\x00', '\\')


def _vdf_escape(text):
    return text.replace('\\', '\\\\').replace('"', '\\"')


def parse_vdf(text):
    """Minimal Valve KeyValues parser: quoted strings, braces, last-wins."""
    root = {}
    stack = [root]
    key = None
    for match in _VDF_TOKEN.finditer(text):
        brace = match.group(2)
        if brace == '{':
            child = {}
            if key is not None:
                stack[-1][key] = child
                key = None
            stack.append(child)
        elif brace == '}':
            if len(stack) > 1:
                stack.pop()
            key = None
        else:
            value = _vdf_unescape(match.group(1))
            if key is None:
                key = value
            else:
                stack[-1][key] = value
                key = None
    return root


def render_vdf(node, level=0):
    """Render parsed KeyValues back; Steam rewrites the file itself, so only
    structure and values (not original whitespace) are preserved."""
    lines = []
    pad = '\t' * level
    for key, value in node.items():
        escaped = _vdf_escape(str(key))
        if isinstance(value, dict):
            lines.append('%s"%s"\n%s{\n%s%s}\n'
                         % (pad, escaped, pad, render_vdf(value, level + 1), pad))
        else:
            lines.append('%s"%s"\t\t"%s"\n' % (pad, escaped, _vdf_escape(str(value))))
    return ''.join(lines)


def _steam_section(config):
    """The Software/Valve/Steam mapping inside a parsed localconfig, or {}."""
    try:
        return config['UserLocalConfigStore']['Software']['Valve']['Steam']
    except (KeyError, TypeError):
        return {}


def _decode_recent(blob):
    """RecentLocalPlayedGameIDs hex -> [(kind, id, ts)] or None.

    Entries are (appid, 0, lastplayed) for Steam games and
    (0x02000000, shortcutid, lastplayed) for non-Steam shortcuts.
    """
    try:
        raw = bytes.fromhex(blob)
    except (ValueError, TypeError):
        return None
    if not raw or len(raw) % 12:
        return None
    entries = []
    for offset in range(0, len(raw), 12):
        first, second, third = struct.unpack('<III', raw[offset:offset + 12])
        if first == _NONSTEAM_MARKER:
            entries.append(('shortcut', second, third))
        elif second == 0 and third > 0:
            entries.append(('app', first, third))
        else:
            return None
    return entries


def _encode_recent(entries):
    """Entries back to hex, most recent first (Steam's own order)."""
    ordered = sorted(entries, key=lambda entry: entry[2], reverse=True)
    raw = bytearray()
    for kind, ident, stamp in ordered:
        if kind == 'shortcut':
            raw += struct.pack('<III', _NONSTEAM_MARKER, ident, stamp)
        else:
            raw += struct.pack('<III', ident, 0, stamp)
    return raw.hex()


def _merge_recent(blobs):
    """Union of recent-played entries, newest timestamp wins per game."""
    merged = {}
    for blob in blobs:
        entries = _decode_recent(blob or '')
        if entries is None:
            return None
        for kind, ident, stamp in entries:
            key = (kind, ident)
            if stamp > merged.get(key, 0):
                merged[key] = stamp
    return _encode_recent([(kind, ident, stamp) for (kind, ident), stamp in merged.items()])


def _merge_seen(values):
    """Union of GamepadHomeWhatsNewSeen vecSeenIDs ("appid:timestamp")."""
    merged = {}
    sections = {}
    for value in values:
        try:
            parsed = json.loads(value or '')
            regions = parsed.get('mapSections') or {}
        except (ValueError, AttributeError):
            return None
        if not isinstance(regions, dict):
            return None
        for name, region in regions.items():
            if not isinstance(region, dict):
                continue
            seen = sections.setdefault(name, {})
            for item in region.get('vecSeenIDs') or []:
                try:
                    ident, stamp = str(item).split(':')
                    stamp = int(stamp)
                except ValueError:
                    continue
                if stamp > seen.get(ident, 0):
                    seen[ident] = stamp
    for name, seen in sections.items():
        merged[name] = {'vecSeenIDs': ['%s:%d' % (ident, stamp)
                                       for ident, stamp in sorted(seen.items())]}
    return json.dumps({'version': 1, 'mapSections': merged}, sort_keys=True)


def _steam_user_configs():
    root = home() / '.local/share/Steam/userdata'
    if not root.is_dir():
        return []
    return sorted(path for path in root.iterdir()
                  if (path / 'config' / 'localconfig.vdf').is_file())


def _merge_localconfig(documents):
    """Merge parsed localconfig documents (newest last); returns (doc, notes).

    Recency keys merge by max; every other key comes from the newest document
    that carries it, so collections and settings keep syncing via the file.
    """
    merged = {}
    notes = []
    for key in set().union(*(doc.keys() for doc in documents)):
        if key == 'UserLocalConfigStore':
            continue
        candidates = [doc[key] for doc in documents if key in doc]
        merged[key] = candidates[-1]
    stores = [doc.get('UserLocalConfigStore') for doc in documents]
    merged_store = {}
    for key in set().union(*(s.keys() for s in stores if isinstance(s, dict))):
        if key == 'Software':
            continue
        candidates = [s[key] for s in stores if isinstance(s, dict) and key in s]
        merged_store[key] = candidates[-1]
    softwares = [s.get('Software') for s in stores if isinstance(s, dict)]
    merged_software = {}
    for key in set().union(*(s.keys() for s in softwares if isinstance(s, dict))):
        if key == 'Valve':
            continue
        candidates = [s[key] for s in softwares if isinstance(s, dict) and key in s]
        merged_software[key] = candidates[-1]
    valves = [s.get('Valve') for s in softwares if isinstance(s, dict)]
    merged_valve = {}
    for key in set().union(*(s.keys() for s in valves if isinstance(s, dict))):
        if key == 'Steam':
            continue
        candidates = [s[key] for s in valves if isinstance(s, dict) and key in s]
        merged_valve[key] = candidates[-1]
    steams = [s.get('Steam') for s in valves if isinstance(s, dict)]
    merged_steam = {}
    for key in set().union(*(s.keys() for s in steams if isinstance(s, dict))):
        if key in ('Apps', 'RecentLocalPlayedGameIDs') or key.startswith('GamepadHomeWhatsNewSeen'):
            continue
        candidates = [s[key] for s in steams if isinstance(s, dict) and key in s]
        merged_steam[key] = candidates[-1]
    # Per-app LastPlayed merges by max; other per-app keys from the newest file.
    apps_list = [s.get('Apps') for s in steams if isinstance(s, dict) and isinstance(s.get('Apps'), dict)]
    merged_apps = {}
    for appid in set().union(*(a.keys() for a in apps_list)):
        records = [a[appid] for a in apps_list if appid in a and isinstance(a[appid], dict)]
        if not records:
            continue
        record = dict(records[-1])
        best = 0
        for older in records:
            try:
                stamp = int(older.get('LastPlayed') or 0)
            except (ValueError, TypeError):
                continue
            best = max(best, stamp)
        if best:
            try:
                current = int(record.get('LastPlayed') or 0)
            except (ValueError, TypeError):
                current = 0
            if best != current:
                record['LastPlayed'] = str(best)
                notes.append('app %s LastPlayed -> %d' % (appid, best))
        merged_apps[appid] = record
    if merged_apps:
        merged_steam['Apps'] = merged_apps
    # Recent-played blob: union, newest timestamp wins per game.
    recent_blobs = [s.get('RecentLocalPlayedGameIDs') for s in steams
                    if isinstance(s, dict) and s.get('RecentLocalPlayedGameIDs')]
    if recent_blobs:
        merged_blob = _merge_recent(recent_blobs)
        if merged_blob is not None:
            if not steams or merged_blob != (steams[-1].get('RecentLocalPlayedGameIDs') or ''):
                notes.append('recent-played list merged from %d file(s)' % len(recent_blobs))
            merged_steam['RecentLocalPlayedGameIDs'] = merged_blob
        else:
            merged_steam['RecentLocalPlayedGameIDs'] = steams[-1].get('RecentLocalPlayedGameIDs')
    # Gamepad/Deck seen-state: union per section.
    seen_keys = {key for s in steams if isinstance(s, dict) for key in s
                 if key.startswith('GamepadHomeWhatsNewSeen')}
    for key in sorted(seen_keys):
        values = [s[key] for s in steams if isinstance(s, dict) and s.get(key)]
        merged_seen = _merge_seen(values) if len(values) > 1 else values[0]
        if merged_seen is None:
            merged_seen = values[-1]
        elif values and merged_seen != values[-1]:
            notes.append('%s seen-state merged' % key)
        merged_steam[key] = merged_seen
    merged_valve['Steam'] = merged_steam
    merged_software['Valve'] = merged_valve
    merged_store['Software'] = merged_software
    merged['UserLocalConfigStore'] = merged_store
    return merged, notes


def cmd_reconcile(options):
    """Merge Syncthing sync-conflict copies of localconfig.vdf into the live file.

    Keeps full-file sync (same hours/recents/collections on both machines) but
    replaces last-writer-wins with max-wins for recency. Refuses while Steam or
    the Lumen sidecar runs; consumed conflicts are archived next to the file.
    """
    if process_running('steam') or process_running('steamwebhelper') or process_running('lumen'):
        print('REFUSING to reconcile: Steam or the Lumen sidecar is running. '
              'Close it completely (Steam -> Exit), let Syncthing settle, then retry.')
        return EXIT_REFUSED
    users = _steam_user_configs()
    if not users:
        print('no Steam userdata with localconfig.vdf found')
        return EXIT_OK
    changed = False
    for user in users:
        config = user / 'config'
        live = config / 'localconfig.vdf'
        conflicts = sorted(config.glob('localconfig.sync-conflict-*.vdf'))
        if not conflicts:
            print('%s: no conflicts' % user.name)
            continue
        documents = []
        order = []
        for path in [live] + conflicts:
            try:
                documents.append(parse_vdf(path.read_text(encoding='utf-8', errors='replace')))
                order.append(path)
            except OSError as error:
                print('  WARN: cannot read %s: %s' % (path.name, error))
        if len(documents) < 2:
            print('%s: only one readable copy, nothing to merge' % user.name)
            continue
        # Oldest first so the newest file wins every non-recency key.
        ranked = sorted(zip(order, documents), key=lambda pair: pair[0].stat().st_mtime)
        merged, notes = _merge_localconfig([doc for _, doc in ranked])
        if options.dry_run:
            print('%s: %d conflict(s) would merge:' % (user.name, len(conflicts)))
            for note in notes or ['no recency differences']:
                print('  - %s' % note)
            continue
        stamp = time.strftime('%Y%m%d-%H%M%S')
        archive = config / ('localconfig.reconciled-' + stamp)
        archive.mkdir(parents=True, exist_ok=True)
        tmp = live.with_name('localconfig.vdf.luna-tmp')
        tmp.write_text(render_vdf(merged))
        os.replace(tmp, live)
        for path in conflicts:
            path.replace(archive / path.name)
        changed = True
        print('%s: merged %d conflict(s) into localconfig.vdf (archived in %s)'
              % (user.name, len(conflicts), archive.name))
        for note in notes:
            print('  - %s' % note)
    return EXIT_OK


