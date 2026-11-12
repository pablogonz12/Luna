# --------------------------------------------------------------------------- games
# `luna games`: which Steam games are installed on which machine, and queueing
# installs on another one. Strictly read-only locally; `install` only asks the
# REMOTE Steam to install (steam://install/<appid>), it never copies game
# files. Unlock-only games (SLSsteam/Lumen: config/stplug-in/<appid>.lua) can
# only be installed where that lua exists, so the diff reports that per game.

GAMES_TOOL_RE = re.compile(r'^(Proton\b|Steam Linux Runtime|Steamworks Common Redistributables|SteamVR\b)')
GAMES_INSTALLED_FLAG = 4  # StateFlags bit: fully installed
GAMES_FREE_MARGIN = 0     # bytes kept free on top of the game size (none: Steam does its own check)


def games_libraries():
    """steamapps directories of this machine: the default one plus libraryfolders.vdf entries."""
    base = steam_home() / 'steamapps'
    found = [base]
    try:
        data = parse_vdf((base / 'libraryfolders.vdf').read_text(errors='replace'))
    except OSError:
        data = {}
    folders = data.get('libraryfolders') or data.get('LibraryFolders') or {}
    for key in sorted(folders, key=lambda k: (not str(k).isdigit(), int(k) if str(k).isdigit() else 0, str(k))):
        entry = folders[key]
        path = entry.get('path') if isinstance(entry, dict) else (entry if isinstance(entry, str) and
                                                                  not str(key).isalpha() else None)
        if path:
            found.append(Path(path) / 'steamapps')
    result = []
    seen = set()
    for item in found:
        try:
            marker = str(item.resolve())
        except OSError:
            marker = str(item)
        if marker not in seen and item.is_dir():
            seen.add(marker)
            result.append(item)
    return result


def games_lua_ids():
    """appids that have a stplug-in lua on this machine."""
    folder = steam_home() / 'config/stplug-in'
    ids = set()
    try:
        for entry in folder.iterdir():
            if entry.suffix == '.lua' and entry.stem.isdigit():
                ids.add(int(entry.stem))
    except OSError:
        pass
    return ids


def games_parse_manifest(path):
    """Installed-game record from an appmanifest_N.acf, or None (tool, partial, unreadable)."""
    try:
        state = parse_vdf(path.read_text(errors='replace')).get('AppState')
    except OSError:
        return None
    if not isinstance(state, dict) or not str(state.get('appid', '')).isdigit():
        return None
    try:
        flags = int(state.get('StateFlags') or 0)
        size = int(state.get('SizeOnDisk') or 0)
    except ValueError:
        flags, size = 0, 0
    name = str(state.get('name') or state.get('installdir') or state['appid'])
    if not flags & GAMES_INSTALLED_FLAG or GAMES_TOOL_RE.match(name):
        return None
    return {'id': int(state['appid']), 'name': name, 'size': size}


def games_scan_local():
    """{'items': {appid: record}, 'lua': set, 'free': bytes, 'libraries': [...]} for this machine."""
    libraries = games_libraries()
    lua = games_lua_ids()
    items = {}
    for library in libraries:
        for manifest in sorted(library.glob('appmanifest_*.acf')):
            record = games_parse_manifest(manifest)
            if record is None or record['id'] in items:
                continue
            record['lib'] = str(library.parent)
            record['pfx'] = (library / 'compatdata' / str(record['id'])).is_dir()
            record['lua'] = record['id'] in lua
            items[record['id']] = record
    free = 0
    if libraries:
        try:
            free = shutil.disk_usage(str(libraries[0])).free
        except OSError:
            pass
    return {'items': items, 'lua': lua, 'free': free, 'libraries': [str(p.parent) for p in libraries]}


def games_probe_block():
    """Compact, additive `games` section of `luna probe --json`."""
    scan = games_scan_local()
    return {'items': [[g['id'], g['name'], g['size'], int(g['lua'])] for g in
                      sorted(scan['items'].values(), key=lambda g: g['id'])],
            'lua': sorted(scan['lua']), 'free': scan['free']}


def games_from_probe(block):
    """Inverse of games_probe_block (remote side); None when the block is malformed."""
    try:
        items = {}
        for appid, name, size, lua in block['items']:
            items[int(appid)] = {'id': int(appid), 'name': str(name), 'size': int(size),
                                 'lib': '', 'pfx': None, 'lua': bool(lua)}
        return {'items': items, 'lua': {int(x) for x in block['lua']}, 'free': int(block['free'])}
    except (KeyError, TypeError, ValueError):
        return None


# Remote scan for hosts whose installed Luna has no games probe: plain bash/sed,
# no Python needed. Tab-separated, name last.
GAMES_REMOTE_SCRIPT = r'''
S="$HOME/.local/share/Steam"; [ -d "$S" ] || S="$HOME/.steam/steam"
if [ -f "$HOME/.local/share/Lumen/sls-luna/luna.py" ]; then
  out=$(python3 "$HOME/.local/share/Lumen/sls-luna/luna.py" probe --json 2>/dev/null)
  case "$out" in *'"games"'*) echo "PROBE $out";; *) out="";; esac
fi
if [ -z "$out" ]; then
  libs="$S"
  for p in $(sed -n 's/^[[:space:]]*"path"[[:space:]]*"\(.*\)".*/\1/p' "$S/steamapps/libraryfolders.vdf" 2>/dev/null | tr ' ' '\001'); do
    libs="$libs
$(printf '%s' "$p" | tr '\001' ' ')"
  done
  printf '%s\n' "$libs" | awk 'NF && !seen[$0]++' | while IFS= read -r lib; do
    [ -d "$lib/steamapps" ] || continue
    [ "$lib" = "$S" ] && printf 'FREE\t%s\n' "$(df -B1 --output=avail "$lib/steamapps" | tail -1 | tr -d ' ')"
    for f in "$lib"/steamapps/appmanifest_*.acf; do
      [ -f "$f" ] || continue
      id=$(sed -n 's/^[[:space:]]*"appid"[[:space:]]*"\([0-9]*\)".*/\1/p' "$f" | head -1)
      sz=$(sed -n 's/^[[:space:]]*"SizeOnDisk"[[:space:]]*"\([0-9]*\)".*/\1/p' "$f" | head -1)
      fl=$(sed -n 's/^[[:space:]]*"StateFlags"[[:space:]]*"\([0-9]*\)".*/\1/p' "$f" | head -1)
      nm=$(sed -n 's/^[[:space:]]*"name"[[:space:]]*"\(.*\)".*/\1/p' "$f" | head -1)
      pf=0; [ -d "$lib/steamapps/compatdata/$id" ] && pf=1
      printf 'APP\t%s\t%s\t%s\t%s\t%s\n' "$id" "${sz:-0}" "${fl:-0}" "$pf" "$nm"
    done
  done
  printf 'LUA\t%s\n' "$(ls "$S/config/stplug-in" 2>/dev/null | sed -n 's/^\([0-9][0-9]*\)\.lua$/\1/p' | tr '\n' ' ')"
fi
uid="$(id -u)"; s=0
pgrep -u "$uid" -x steam >/dev/null && s=1
pgrep -u "$uid" -x steamwebhelper >/dev/null && s=1
echo "STEAMRUN $s"
'''


def games_parse_remote(code, out, err):
    """Remote record {'reachable', 'items', 'lua', 'free', 'steamRunning'} from GAMES_REMOTE_SCRIPT output."""
    if code == 255:
        return {'reachable': False, 'error': (err.strip().splitlines() or ['ssh failed'])[-1]}
    record = {'reachable': True, 'items': {}, 'lua': set(), 'free': 0, 'steamRunning': False}
    for line in out.splitlines():
        if line.startswith('STEAMRUN '):
            record['steamRunning'] = line.split()[1] == '1'
        elif line.startswith('PROBE '):
            try:
                parsed = games_from_probe(json.loads(line[6:]).get('games'))
            except (ValueError, AttributeError):
                parsed = None
            if parsed:
                record.update(parsed)
        else:
            parts = line.split('\t')
            if parts[0] == 'FREE' and len(parts) > 1 and parts[1].isdigit():
                record['free'] = int(parts[1])
            elif parts[0] == 'LUA' and len(parts) > 1:
                record['lua'] = {int(x) for x in parts[1].split() if x.isdigit()}
            elif parts[0] == 'APP' and len(parts) >= 6 and parts[1].isdigit():
                try:
                    flags, size = int(parts[3] or 0), int(parts[2] or 0)
                except ValueError:
                    continue
                name = parts[5] or parts[1]
                if flags & GAMES_INSTALLED_FLAG and not GAMES_TOOL_RE.match(name):
                    record['items'].setdefault(int(parts[1]), {
                        'id': int(parts[1]), 'name': name, 'size': size, 'lib': '',
                        'pfx': parts[4] == '1', 'lua': None})
    for appid, game in record['items'].items():
        if game['lua'] is None:
            game['lua'] = appid in record['lua']
    return record


def games_collect_remote(host):
    code, out, err = fleet_ssh(host, GAMES_REMOTE_SCRIPT, timeout=60)
    record = games_parse_remote(code, out, err)
    record['host'] = host
    return record


def games_diff(local, remote):
    """Per-game comparison of this machine (local) against a target record."""
    rows = []
    free = remote.get('free', 0)
    for appid in sorted(set(local['items']) | set(remote['items'])):
        here, there = local['items'].get(appid), remote['items'].get(appid)
        row = {'appid': appid, 'name': (here or there)['name'],
               'localSize': here['size'] if here else None,
               'remoteSize': there['size'] if there else None,
               'where': 'both' if here and there else ('local' if here else 'remote')}
        if here and not there:
            unlock_only = bool(here['lua'])
            row['unlockOnly'] = unlock_only
            row['luaOnTarget'] = appid in remote['lua']
            row['blocked'] = unlock_only and not row['luaOnTarget']
            row['fits'] = here['size'] + GAMES_FREE_MARGIN <= free
        rows.append(row)
    missing = [r for r in rows if r['where'] == 'local']
    return {'games': rows,
            'summary': {'both': sum(r['where'] == 'both' for r in rows),
                        'missingOnTarget': len(missing),
                        'missingBytes': sum(r['localSize'] for r in missing),
                        'blocked': sum(r['blocked'] for r in missing),
                        'tooBig': sum(not r['fits'] for r in missing),
                        'allFit': sum(r['localSize'] for r in missing) <= free,
                        'targetOnly': sum(r['where'] == 'remote' for r in rows),
                        'targetFree': free}}


def games_gb(value):
    return '%.1f' % (value / 1e9) if value is not None else '-'


def games_resolve_host(options):
    host = getattr(options, 'host', None) or (fleet_load_hosts() or [None])[0]
    if not host:
        print('no host: luna games diff HOST (or luna fleet add HOST)', file=sys.stderr)
        return None
    if not fleet_valid_host(host):
        print('REFUSING: %r is not a plain hostname' % host, file=sys.stderr)
        return None
    return host


def games_list(options):
    scan = games_scan_local()
    games = sorted(scan['items'].values(), key=lambda g: g['name'].lower())
    if options.json:
        print(json.dumps({'games': games, 'libraries': scan['libraries'], 'free': scan['free']}, sort_keys=True))
        return EXIT_OK
    print('%-9s %-44s %8s  %-4s %-6s %s' % ('APPID', 'NAME', 'GB', 'PFX', 'UNLOCK', 'LIBRARY'))
    for game in games:
        print('%-9d %-44s %8s  %-4s %-6s %s' % (game['id'], game['name'][:44], games_gb(game['size']),
                                                'yes' if game['pfx'] else '-', 'yes' if game['lua'] else 'no',
                                                game['lib']))
    print('%d game(s), %s GB; %s GB free in the default library' % (
        len(games), games_gb(sum(g['size'] for g in games)), games_gb(scan['free'])))
    return EXIT_OK


def games_diff_cmd(options):
    host = games_resolve_host(options)
    if host is None:
        return EXIT_REFUSED
    remote = games_collect_remote(host)
    if not remote['reachable']:
        print('%s: unreachable (%s)' % (host, remote.get('error', '?')), file=sys.stderr)
        return 1
    result = games_diff(games_scan_local(), remote)
    result['host'] = host
    if options.json:
        print(json.dumps(result, sort_keys=True))
        return EXIT_OK
    summary = result['summary']
    print('%-9s %-40s %8s  %s' % ('APPID', 'NAME (missing on %s)' % host, 'GB', 'STATUS'))
    for row in sorted((r for r in result['games'] if r['where'] == 'local'),
                      key=lambda r: -r['localSize']):
        status = 'ok'
        if row['blocked']:
            status = 'BLOCKED: no stplug-in lua on target'
        elif not row['fits']:
            status = 'does not fit'
        elif row['unlockOnly']:
            status = 'ok (unlock-only, lua present)'
        print('%-9d %-40s %8s  %s' % (row['appid'], row['name'][:40], games_gb(row['localSize']), status))
    print('both: %d | missing on %s: %d (%s GB; %d blocked by missing lua, %d too big alone%s) | '
          'only on %s: %d | %s GB free there' % (
              summary['both'], host, summary['missingOnTarget'], games_gb(summary['missingBytes']),
              summary['blocked'], summary['tooBig'], '' if summary['allFit'] else '; not all fit together',
              host, summary['targetOnly'], games_gb(summary['targetFree'])))
    return EXIT_OK


def games_install_cmd(options):
    host = games_resolve_host(options)
    if host is None:
        return EXIT_REFUSED
    appids = []
    for raw in options.appids:
        if not str(raw).isdigit():
            print('REFUSING: %r is not a numeric appid' % raw, file=sys.stderr)
            return EXIT_REFUSED
        if int(raw) not in appids:
            appids.append(int(raw))
    local = games_scan_local()
    remote = games_collect_remote(host)
    if not remote['reachable']:
        print('%s: unreachable (%s)' % (host, remote.get('error', '?')), file=sys.stderr)
        return 1
    queue, refused, needed = [], [], 0
    for appid in appids:
        here = local['items'].get(appid)
        name = (here or {}).get('name') or str(appid)
        if appid in remote['items']:
            print('%s (%d): already installed on %s, skipping' % (name, appid, host))
        elif (here and here['lua']) and appid not in remote['lua']:
            refused.append('%s (%d): unlock-only and %s has no stplug-in/%d.lua (sync it first)' % (
                name, appid, host, appid))
        elif here is None and appid not in remote['lua']:
            # Not installed here, so ownership/unlock state is unknown: let Steam on the target decide.
            queue.append((appid, name, None))
        elif here is not None and needed + here['size'] > remote['free']:
            refused.append('%s (%d): needs %s GB, only %s GB free on %s' % (
                name, appid, games_gb(here['size']), games_gb(max(remote['free'] - needed, 0)), host))
        else:
            needed += here['size'] if here else 0
            queue.append((appid, name, here['size'] if here else None))
    for line in refused:
        print('REFUSING ' + line, file=sys.stderr)
    for appid, name, size in queue:
        print('%s %s (%d) on %s%s' % ('would install' if options.dry_run else 'installing', name, appid, host,
                                      ' [%s GB]' % games_gb(size) if size else ' [size unknown]'))
    if options.dry_run or not queue:
        return EXIT_REFUSED if refused and not queue else EXIT_OK
    if not remote['steamRunning']:
        if not options.start_steam:
            print('REFUSING: Steam is not running on %s (start it there, or pass --start-steam)' % host,
                  file=sys.stderr)
            return EXIT_REFUSED
        code, _, err = fleet_ssh(host, FLEET_START_STEAM_SCRIPT, timeout=30)
        if code != 0:
            print('%s: could not start Steam: %s' % (host, err.strip()), file=sys.stderr)
            return 1
        print('started Steam on %s; give it a moment to load before the installs are picked up' % host)
    failed = False
    for appid, name, _ in queue:
        script = ('[ -n "$XDG_RUNTIME_DIR" ] || export XDG_RUNTIME_DIR="/run/user/$(id -u)"\n'
                  'steam steam://install/%d >/dev/null 2>&1\n' % appid)
        code, _, err = fleet_ssh(host, script, timeout=60)
        print('%-9d %s %s' % (appid, 'queued' if code == 0 else 'FAIL', name if code == 0 else err.strip()))
        failed = failed or code != 0
    return 1 if failed or refused else EXIT_OK


def cmd_games(options):
    action = options.games_action
    rest = list(getattr(options, 'games_args', None) or [])
    options.host = rest[0] if rest and action in ('diff', 'install') else None
    options.appids = rest[1:] if action == 'install' else []
    if action == 'install' and (not options.host or not options.appids):
        print('usage: luna games install HOST APPID... [--dry-run] [--start-steam]', file=sys.stderr)
        return EXIT_REFUSED
    if action in ('list', 'diff') and len(rest) > (1 if action == 'diff' else 0):
        print('usage: luna games list | luna games diff [HOST]', file=sys.stderr)
        return EXIT_REFUSED
    if action == 'list':
        return games_list(options)
    if action == 'diff':
        return games_diff_cmd(options)
    return games_install_cmd(options)
