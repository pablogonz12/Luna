# --------------------------------------------------------------------------- fleet
# `luna fleet`: see and manage Luna on several machines over SSH. Hostnames
# reach ssh/rsync/scp as argv only (never through a shell string), remote
# scripts go to `bash -s` on stdin (the remote login shell may be fish), and
# every connection is BatchMode with a connect timeout, so a dead host cannot
# hang the command or prompt for a password.

FLEET_HOST_RE = re.compile(r'^(?:[A-Za-z0-9_][A-Za-z0-9_.-]*@)?[A-Za-z0-9][A-Za-z0-9._-]*$')
FLEET_SSH_OPTS = ('-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8',
                  '-o', 'ServerAliveInterval=10', '-o', 'ServerAliveCountMax=3')
FLEET_REMOTE_LUNA = '$HOME/.local/share/Lumen/sls-luna/luna.py'
FLEET_REMOTE_CACHE = '.local/share/Lumen/sls-luna/steam-client-cache'  # relative to $HOME


def fleet_config_path():
    override = os.environ.get('LUNA_FLEET_CONFIG')
    if override:
        return Path(override).expanduser()
    base = os.environ.get('XDG_CONFIG_HOME') or str(home() / '.config')
    return Path(base) / 'luna' / 'fleet.json'


def fleet_valid_host(host):
    return isinstance(host, str) and len(host) <= 253 and bool(FLEET_HOST_RE.match(host))


def fleet_load_hosts():
    data = read_json_file(fleet_config_path())
    hosts = data.get('hosts') if isinstance(data, dict) else None
    result = []
    for host in hosts if isinstance(hosts, list) else []:
        if fleet_valid_host(host) and host not in result:
            result.append(host)
    return result


def fleet_save_hosts(hosts):
    write_json_file(fleet_config_path(), {'hosts': list(hosts)})


def fleet_ssh(host, script, timeout=120):
    """Run a bash script on HOST over ssh. Returns (code, stdout, stderr); code 255 = ssh failed."""
    command = ['ssh'] + list(FLEET_SSH_OPTS) + ['--', host, 'bash', '-s']
    try:
        result = subprocess.run(command, input=script, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 255, '', 'timed out after %ds' % timeout
    except OSError as error:
        return 255, '', str(error)
    return result.returncode, result.stdout, result.stderr


def probe_data():
    """Machine-readable state of THIS machine's Luna install (works from the installed copy:
    needs no checkout). Drift = an installed add-on file differs from its pin."""
    snapshot = read_json_file(addons_dir() / 'luna-manifest.json') or {}
    snap_files = snapshot.get('files') if isinstance(snapshot.get('files'), dict) else {}
    manifest = {}
    if MANIFEST_PATH.is_file():
        try:
            manifest = json.loads(MANIFEST_PATH.read_text())
        except ValueError:
            manifest = {}
    installed = installed_addons()
    addons = {}
    for name in ADDONS:
        if name not in installed:
            addons[name] = 'absent'
            continue
        # What the installer recorded wins (that is what drift is measured from);
        # a checkout's MANIFEST is the fallback for installs that predate the snapshot.
        pins = dict(snap_files.get(name) or {})
        if not pins:
            pins = {rel: meta.get('sha256', '') for rel, meta in
                    ((manifest.get('addons') or {}).get(name) or {}).get('files', {}).items()}
        if not pins:
            addons[name] = 'unknown'
            continue
        state = 'match'
        for rel, expected in pins.items():
            path = addons_dir() / name / rel
            if not path.is_file():
                state = 'missing'
                break
            try:
                if sha256(path) != expected:
                    state = 'drift'
            except OSError:
                state = 'missing'
                break
        addons[name] = state
    script = luna_tools_dir() / 'luna.py'
    if not script.is_file():
        script = Path(__file__).resolve()
    steam = steam_state()
    versions = snapshot.get('versions') if isinstance(snapshot.get('versions'), dict) else {}
    return {
        'schema': 1,
        'lunaVersion': str(versions.get('luna') or addon_version('luna')),
        'lunaSha': sha256(script),
        'source': str(snapshot.get('source') or ''),
        'versions': versions,
        'sources': snapshot.get('sources') if isinstance(snapshot.get('sources'), dict) else {},
        'addons': addons,
        'payloadsMatch': all(state in ('match', 'absent') for state in addons.values())
                         and any(state == 'match' for state in addons.values()),
        'steam': {key: steam[key] for key in ('build', 'pinned', 'match', 'packagesMatch', 'updatesBlocked',
                                                 'hardLock', 'cachedBuild', 'cacheReady')},
        'steamRunning': process_running('steam') or process_running('steamwebhelper'),
        'lumenRunning': process_running('lumen'),
    }


def cmd_probe(options):
    data = probe_data()
    if options.json:
        print(json.dumps(data, sort_keys=True))
    else:
        for key, value in sorted(data.items()):
            print('%-14s %s' % (key, value))
    return EXIT_OK


FLEET_STATUS_SCRIPT = r'''
p="$HOME/.local/share/Lumen/sls-luna/luna.py"
if [ ! -f "$p" ]; then echo NOLUNA; exit 0; fi
echo "SHA $(sha256sum "$p" | cut -d' ' -f1)"
s=0; l=0
pgrep -u "$(id -u)" -x steam >/dev/null && s=1
pgrep -u "$(id -u)" -x steamwebhelper >/dev/null && s=1
pgrep -u "$(id -u)" -x lumen >/dev/null && l=1
echo "STEAMRUN $s"
echo "LUMENRUN $l"
if out=$(python3 "$p" probe --json 2>/dev/null) && [ -n "$out" ]; then
  echo "PROBE $out"
else
  echo LEGACY
  python3 "$p" steam status 2>/dev/null | sed 's/^/ST /'
fi
# doctor exits 1/2 when it has findings, so only its output decides support
d=$(python3 "$p" doctor --json 2>/dev/null | tr -d '\n')
case "$d" in "{"*) echo "DOCTOR $d";; esac
'''


def fleet_parse_status(code, out, err):
    """Turn the remote status script's output into a host record."""
    if code == 255:
        return {'reachable': False, 'error': (err.strip().splitlines() or ['ssh failed'])[-1]}
    record = {'reachable': True}
    legacy_lines = []
    for line in out.splitlines():
        tag, _, rest = line.partition(' ')
        if tag == 'NOLUNA':
            record['installed'] = False
            return record
        if tag == 'SHA':
            record['lunaSha'] = rest.strip()
        elif tag == 'STEAMRUN':
            record['steamRunning'] = rest.strip() == '1'
        elif tag == 'LUMENRUN':
            record['lumenRunning'] = rest.strip() == '1'
        elif tag == 'PROBE':
            try:
                probe = json.loads(rest)
            except ValueError:
                continue
            if isinstance(probe, dict):
                seen = {key: record.get(key) for key in ('steamRunning', 'lumenRunning')}
                record.update(probe)
                for key, value in seen.items():
                    if value is not None:
                        record[key] = value or bool(probe.get(key))
                record['installed'] = True
        elif tag == 'DOCTOR':
            try:
                report = json.loads(rest)
            except ValueError:
                continue
            if isinstance(report, dict) and isinstance(report.get('findings'), list):
                record['doctor'] = doctor_summary(report['findings'])
        elif tag == 'LEGACY':
            record['legacy'] = True
        elif tag == 'ST':
            legacy_lines.append(rest)
    if record.get('legacy'):
        record['installed'] = True
        steam = {}
        for line in legacy_lines:
            key, _, value = line.partition(':')
            key, value = key.strip(), value.strip()
            if key == 'client':
                steam['build'] = value
            elif key == 'pinned':
                steam['pinned'] = '' if value.startswith('none') else value
            elif key == 'packages':
                steam['packagesMatch'] = True if value.startswith('match') else (
                    False if value.startswith('DIFFER') else None)
            elif key == 'updates':
                steam['updatesBlocked'] = value.startswith('blocked')
            elif key == 'lock':
                steam['hardLock'] = value.startswith('HARD')
        record['steam'] = steam
    elif 'installed' not in record:
        record['installed'] = bool(record.get('lunaSha'))
    return record


def fleet_collect(host):
    """Status record for one host name, or for this machine when host is None."""
    if host is None:
        record = probe_data()
        record['reachable'] = True
        record['installed'] = True
        record['host'] = os.uname().nodename
        record['local'] = True
        try:
            record['doctor'] = doctor_summary(doctor_findings())
        except Exception:  # a broken check must not hide the fleet table
            pass
        return record
    code, out, err = fleet_ssh(host, FLEET_STATUS_SCRIPT)
    record = fleet_parse_status(code, out, err)
    record['host'] = host
    record['local'] = False
    return record


def fleet_notes(records):
    """Cross-host findings, relative to this machine (the first record)."""
    notes = []
    reference = records[0] if records else {}
    for record in records:
        if not record.get('reachable'):
            notes.append('%s: unreachable (%s)' % (record['host'], record.get('error', '?')))
            continue
        if record.get('installed') is False:
            notes.append('%s: Luna is not installed' % record['host'])
            continue
        if record.get('legacy'):
            notes.append('%s: older Luna without `probe --json`; add-on state unknown '
                         '(run: luna fleet install %s --force)' % (record['host'], record['host']))
        doctor = record.get('doctor')
        if doctor and (doctor.get('warn') or doctor.get('error')):
            notes.append('%s: doctor reports %d warning(s), %d error(s) (run `luna doctor` there)'
                         % (record['host'], doctor.get('warn', 0), doctor.get('error', 0)))
        if record is reference:
            continue
        if record.get('lunaSha') and reference.get('lunaSha') and record['lunaSha'] != reference['lunaSha']:
            notes.append('%s: luna.py differs from %s' % (record['host'], reference['host']))
        drifted = [n for n, s in (record.get('addons') or {}).items() if s in ('drift', 'missing')]
        if drifted:
            notes.append('%s: add-on payloads drifted/missing: %s' % (record['host'], ', '.join(drifted)))
        ref_pin = (reference.get('steam') or {}).get('pinned')
        pin = (record.get('steam') or {}).get('pinned')
        if ref_pin and pin != ref_pin:
            notes.append('%s: Steam pin %s differs from %s (%s)'
                         % (record['host'], pin or 'none', reference['host'], ref_pin))
    return notes


def _fleet_yes(value):
    return '?' if value is None else ('yes' if value else 'no')


def _fleet_doctor(summary):
    """'ok' / '2w 1e' / '?' (remote too old for `doctor --json`)."""
    if not isinstance(summary, dict):
        return '?'
    if not summary.get('warn') and not summary.get('error'):
        return 'ok'
    return '%dw %de' % (summary.get('warn', 0), summary.get('error', 0))


def fleet_row(record):
    if not record.get('reachable'):
        return [record['host'], 'UNREACHABLE', '', '', '', '', '', '', '']
    if record.get('installed') is False:
        return [record['host'], 'no luna', '', '', '', '', '', '', '']
    steam = record.get('steam') or {}
    if record.get('legacy'):
        addons = 'unknown'
    else:
        states = set((record.get('addons') or {}).values()) - {'absent'}
        addons = 'match' if states == {'match'} else ('drift' if states else 'none')
    lock = 'HARD' if steam.get('hardLock') else ('blocked' if steam.get('updatesBlocked') else 'open')
    return [record['host'], record.get('lunaVersion') or '?', (record.get('lunaSha') or '')[:8] or '?',
            addons, steam.get('pinned') or 'none', _fleet_yes(steam.get('packagesMatch')), lock,
            _fleet_yes(record.get('steamRunning')), _fleet_doctor(record.get('doctor'))]


def fleet_status(hosts, as_json):
    records = [fleet_collect(None)] + [fleet_collect(host) for host in hosts]
    notes = fleet_notes(records)
    if as_json:
        print(json.dumps({'hosts': records, 'notes': notes}, indent=2, sort_keys=True))
    else:
        header = ['HOST', 'LUNA', 'SHA', 'ADD-ONS', 'STEAM PIN', 'PKGS', 'UPDATES', 'RUNNING', 'DOCTOR']
        rows = [header] + [fleet_row(record) for record in records]
        widths = [max(len(str(row[i])) for row in rows) for i in range(len(header))]
        for row in rows:
            print('  '.join(str(cell).ljust(widths[i]) for i, cell in enumerate(row)).rstrip())
        for note in notes:
            print('note: %s' % note)
    bad = any(not r.get('reachable') or r.get('installed') is False for r in records)
    return 1 if bad else EXIT_OK


def fleet_push_cache(host, dry_run=False):
    """rsync the verified local Steam client cache to HOST when its build differs. (code, message)"""
    state = steam_state()
    build = state['cachedBuild']
    if not state['cacheReady'] or not build:
        return 1, 'no local cache to push (run: luna steam cache, with Steam closed)'
    if state['pinned'] and state['pinned'] != build:
        return 1, 'local cache is build %s but the pin is %s; refusing to push a stale cache' % (build, state['pinned'])
    if state['packagesMatch'] is False:
        return 1, 'local cache failed verification against its manifests; refusing to push it'
    code, out, err = fleet_ssh(host, 'cat "$HOME/%s/build.txt" 2>/dev/null; '
                                     '[ -d "$HOME/%s/package" ] && echo HAS_PACKAGE; true'
                               % (FLEET_REMOTE_CACHE, FLEET_REMOTE_CACHE))
    if code == 255:
        return 1, 'unreachable: %s' % ((err.strip().splitlines() or ['ssh failed'])[-1])
    lines = out.split()
    if lines[:1] == [build] and 'HAS_PACKAGE' in lines:
        return 0, 'already at build %s; nothing to push' % build
    if dry_run:
        return 0, 'would push cache build %s (remote has %s)' % (build, lines[0] if lines and lines[0] != 'HAS_PACKAGE' else 'none')
    src = str(steam_cache_dir()) + '/'
    dest = '%s:%s/' % (host, FLEET_REMOTE_CACHE)
    rsh = 'ssh ' + ' '.join(FLEET_SSH_OPTS)
    base = ['rsync', '-a', '--partial', '--delete', '-e', rsh]
    # build.txt goes last: a half-copied cache never claims to be complete.
    steps = [base + ['--exclude', '/build.txt', '--', src, dest],
             base + ['--include', '/build.txt', '--exclude', '*', '--', src, dest]]
    for step in steps:
        try:
            result = subprocess.run(step, capture_output=True, text=True)
        except OSError as error:
            return 1, 'rsync not runnable: %s' % error
        if result.returncode != 0:
            return 1, 'rsync failed (%d): %s' % (result.returncode, (result.stderr.strip().splitlines() or ['?'])[-1])
    return 0, ('pushed cache build %s (a later install there finds it in its standard cache '
               'dir; no --client-cache needed)' % build)


FLEET_STOP_STEAM_SCRIPT = r'''
uid="$(id -u)"
up() { for n in steam steamwebhelper lumen; do pgrep -u "$uid" -x "$n" >/dev/null && return 0; done; return 1; }
if pgrep -u "$uid" -x steam >/dev/null; then timeout 30 steam -shutdown >/dev/null 2>&1; fi
for i in $(seq 1 %d); do up || exit 0; sleep 1; done
exit 1
'''

FLEET_START_STEAM_SCRIPT = r'''
[ -n "$XDG_RUNTIME_DIR" ] || export XDG_RUNTIME_DIR="/run/user/$(id -u)"
systemd-run --user --quiet --collect steam
'''

FLEET_MKTEMP_SCRIPT = 'mktemp -d "${TMPDIR:-/tmp}/luna-fleet.XXXXXX"\n'


def fleet_build_bundle(out_dir):
    """Build the release bundle from this checkout with `luna release`. Returns (path, error)."""
    buffer = io.StringIO()
    errors = io.StringIO()
    with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(errors):
        code = cmd_release(argparse.Namespace(out=str(out_dir), publish=False, only=[]))
    bundles = sorted(Path(out_dir).glob('luna-*.tar.gz'))
    if code != 0 or not bundles:
        return None, (buffer.getvalue() + errors.getvalue()).strip() or 'luna release failed'
    return bundles[-1], ''


def fleet_install_host(host, bundle, options):
    """Install the bundle on one host. Returns (ok, message)."""
    code, out, err = fleet_ssh(host, FLEET_STATUS_SCRIPT)
    record = fleet_parse_status(code, out, err)
    if not record.get('reachable'):
        return False, 'unreachable: %s' % record.get('error', '?')
    if record.get('installed') is False:
        return False, 'Luna is not installed there; bootstrap it once by hand (copy a bundle, run scripts/install-local.sh)'
    drifted = [n for n, s in (record.get('addons') or {}).items() if s in ('drift', 'missing')]
    if record.get('legacy') and not options.force:
        return False, 'REFUSED: older Luna cannot report payload drift; re-run with --force to overwrite regardless'
    if drifted and not options.force:
        return False, 'REFUSED: installed payloads differ from their pins (%s); inspect them or pass --force' % ', '.join(drifted)
    running = bool(record.get('steamRunning')) or bool(record.get('lumenRunning'))
    if running and not options.restart_steam:
        return False, 'REFUSED: Steam/Lumen is running there; close it or pass --restart-steam'
    if options.dry_run:
        steps = ['copy bundle', 'luna update --bundle' + ''.join(' --only ' + n for n in options.only)]
        if running:
            steps = ['stop Steam (steam -shutdown, wait)'] + steps + ['relaunch Steam']
        return True, 'dry-run: would ' + ', '.join(steps)
    if bundle is None:
        return False, 'no bundle'
    code, out, err = fleet_ssh(host, FLEET_MKTEMP_SCRIPT, timeout=30)
    remote_dir = out.strip().splitlines()[-1] if out.strip() else ''
    if code != 0 or not remote_dir.startswith('/'):
        return False, 'could not create a temp dir there: %s' % (err.strip() or out.strip() or code)
    stopped = False
    try:
        copy = ['scp', '-q'] + list(FLEET_SSH_OPTS) + ['--', str(bundle), str(bundle) + '.sha256',
                                                        '%s:%s/' % (host, remote_dir)]
        result = subprocess.run(copy, capture_output=True, text=True)
        if result.returncode != 0:
            return False, 'scp failed: %s' % ((result.stderr.strip().splitlines() or ['?'])[-1])
        if running:
            code, _, _ = fleet_ssh(host, FLEET_STOP_STEAM_SCRIPT % options.stop_timeout,
                                   timeout=options.stop_timeout + 60)
            stopped = True
            if code != 0:
                return False, 'Steam did not exit within %ds; nothing was installed' % options.stop_timeout
        remote_bundle = '%s/%s' % (remote_dir, Path(bundle).name)
        update = ['python3', '"%s"' % FLEET_REMOTE_LUNA, 'update', '--bundle', "'%s'" % remote_bundle]
        for name in options.only:
            update += ['--only', name]
        code, out, err = fleet_ssh(host, ' '.join(update) + '\n', timeout=900)
        tail = (out + err).strip().splitlines()[-1:] or ['']
        if code != 0:
            return False, 'install failed (%d): %s' % (code, tail[0])
        return True, 'installed' + (' (Steam restarted)' if stopped else '')
    finally:
        fleet_ssh(host, 'rm -rf -- \'%s\'\n' % remote_dir, timeout=30)
        if stopped:
            code, _, err = fleet_ssh(host, FLEET_START_STEAM_SCRIPT, timeout=30)
            if code != 0:
                print('%s: WARNING: could not relaunch Steam: %s' % (host, err.strip()), file=sys.stderr)


def cmd_fleet(options):
    action = options.fleet_action
    hosts = list(options.hosts)
    configured = fleet_load_hosts()
    if action in ('add', 'remove'):
        if len(hosts) != 1:
            print('usage: luna fleet %s HOST' % action, file=sys.stderr)
            return EXIT_REFUSED
        host = hosts[0]
        if not fleet_valid_host(host):
            print('REFUSING: %r is not a plain hostname (letters, digits, . _ -, optional user@)' % host,
                  file=sys.stderr)
            return EXIT_REFUSED
        if action == 'add':
            if host not in configured:
                configured.append(host)
                fleet_save_hosts(configured)
            print('fleet: %s' % ', '.join(configured))
        else:
            if host not in configured:
                print('%s is not in the fleet' % host, file=sys.stderr)
                return 1
            configured.remove(host)
            fleet_save_hosts(configured)
            print('removed %s' % host)
        return EXIT_OK
    if action == 'list':
        print('local   (%s)' % os.uname().nodename)
        for host in configured:
            print('remote  %s' % host)
        return EXIT_OK
    for host in hosts:
        if not fleet_valid_host(host):
            print('REFUSING: %r is not a plain hostname' % host, file=sys.stderr)
            return EXIT_REFUSED
    if action == 'status':
        return fleet_status(hosts or configured, options.json)
    for name in options.only:
        if name not in ADDONS:
            print('unknown add-on %r (known: %s)' % (name, ', '.join(ADDONS)), file=sys.stderr)
            return EXIT_REFUSED
    targets = hosts or configured
    if not targets:
        print('no hosts: luna fleet add HOST', file=sys.stderr)
        return EXIT_REFUSED
    failed = False
    if action == 'push-cache':
        for host in targets:
            code, message = fleet_push_cache(host, options.dry_run)
            failed = failed or code != 0
            print('%-20s %s' % (host, message))
        return 1 if failed else EXIT_OK
    # install
    options.stop_timeout = 120
    with tempfile.TemporaryDirectory(prefix='luna-fleet-') as work:
        bundle = None
        if not options.dry_run:
            bundle, error = fleet_build_bundle(work)
            if bundle is None:
                print('REFUSING: cannot build the bundle: %s' % error, file=sys.stderr)
                return 1
        for host in targets:
            ok, message = fleet_install_host(host, bundle, options)
            failed = failed or not ok
            print('%-20s %s %s' % (host, 'ok  ' if ok else 'FAIL', message))
    return 1 if failed else EXIT_OK



