# The single place that names the GitHub account the add-on payloads come from.
# Override with LUNA_GITHUB_OWNER to point a fork at its own repositories.
GITHUB_OWNER = os.environ.get('LUNA_GITHUB_OWNER') or 'pgm1207'
GITHUB_URL = 'https://github.com/' + GITHUB_OWNER


def home():
    return Path(os.environ.get('HOME') or Path.home())


def lumen_root():
    return Path(os.environ.get('LUMEN_DIR') or (home() / '.local/share/Lumen'))


def addons_dir():
    return lumen_root() / 'sls-addons'


def boot_file():
    return lumen_root() / 'lua' / 'boot.lua'


def data_dir():
    return home() / '.local/share/slssteam-integrations'


def luna_tools_dir():
    """Machine-local Luna tooling: the standalone updater copy and its units.

    It sits under the Lumen directory on purpose: that tree is excluded from the
    user's Syncthing folders, so two machines never fight over the copies.
    """
    return lumen_root() / 'sls-luna'


def machine_id():
    """Stable per-machine id. Machine-local on purpose (never synced): it is
    how one machine tells its own heartbeat apart from the other's."""
    path = luna_tools_dir() / 'machine-id'
    try:
        existing = path.read_text().strip()
        if existing:
            return existing
    except OSError:
        pass
    import uuid
    ident = uuid.uuid4().hex[:12]
    try:
        luna_tools_dir().mkdir(parents=True, exist_ok=True)
        path.write_text(ident + '\n')
    except OSError:
        pass
    return ident


HEARTBEAT_TTL = 2 * 3600
HEARTBEAT_PRUNE = 7 * 24 * 3600


def heartbeat_dir():
    """Synced heartbeats: data_dir *is* shared between machines, so each
    machine's periodic check-in is visible to the other."""
    return data_dir() / 'heartbeats'


def write_heartbeat():
    """Record this machine's presence for the other machine to see."""
    try:
        directory = heartbeat_dir()
        directory.mkdir(parents=True, exist_ok=True)
        entry = {'machine': machine_id(),
                 'at': int(time.time()),
                 'steamRunning': bool(process_running('steam') or process_running('steamwebhelper')),
                 'luna': installed_version()}
        (directory / (entry['machine'] + '.json')).write_text(json.dumps(entry) + '\n')
        now = time.time()
        for child in directory.glob('*.json'):
            try:
                if now - child.stat().st_mtime > HEARTBEAT_PRUNE:
                    child.unlink()
            except OSError:
                pass
    except OSError:
        pass


def read_peers(max_age=HEARTBEAT_TTL):
    """Heartbeats from *other* machines fresh enough to matter."""
    me = machine_id()
    peers = []
    try:
        files = sorted(heartbeat_dir().glob('*.json'))
    except OSError:
        return peers
    now = time.time()
    for path in files:
        if path.stem == me:
            continue
        try:
            entry = json.loads(path.read_text())
            at = int(entry.get('at') or 0)
        except (OSError, ValueError, TypeError, AttributeError):
            continue
        if now - at > max_age:
            continue
        peers.append(entry)
    return peers


def read_json_file(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def write_json_file(path, data):
    """Atomic: the Lua backend reads these files while Python writes them."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.' + path.name + '.', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w') as handle:
            handle.write(json.dumps(data, indent=2, sort_keys=True) + '\n')
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return path


# A hung gh must not hang its caller: the Health page waits on `check`, and a
# oneshot timer unit that never exits is never started again.
GH_TIMEOUT = 30
GH_DOWNLOAD_TIMEOUT = 600


def find_gh():
    """The gh CLI, from PATH or the usual user locations."""
    found = shutil.which('gh')
    if found:
        return found
    for candidate in (home() / '.local/bin/gh', Path('/usr/local/bin/gh'), Path('/usr/bin/gh')):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def installed_version():
    """The luna add-on version actually on disk.

    Read from the installed payload first, so the standalone updater copy (which
    has no repository around it) reports the truth too.
    """
    for candidate in (addons_dir() / 'luna' / 'plugin.json',
                      payload_path('luna', 'plugin/plugin.json')):
        try:
            return str(json.loads(candidate.read_text()).get('version', '?'))
        except (OSError, ValueError):
            continue
    return '?'


def version_key(text):
    """Sortable tuple for tags like v0.14.3 or 0.14.3-rc1 (best effort)."""
    numbers = []
    for part in str(text or '').strip().lstrip('v').replace('-', '.').split('.'):
        digits = ''.join(ch for ch in part if ch.isdigit())
        numbers.append(int(digits) if digits else 0)
    while len(numbers) < 3:
        numbers.append(0)
    return tuple(numbers[:3])


def payload_path(addon, source_rel):
    """Where a payload file lives in this repo (mirrors the add-on project)."""
    return ROOT / 'addons' / addon / source_rel


def sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def process_running(name):
    """True when a process with this exact name belongs to this user."""
    try:
        result = subprocess.run(['pgrep', '-u', str(os.getuid()), '-x', name],
                                capture_output=True, text=True)
    except FileNotFoundError:
        return False
    # pgrep's contract is the exit status; it prints PIDs but the status is what
    # says whether anything matched.
    return result.returncode == 0


def load_manifest():
    if not MANIFEST_PATH.is_file():
        raise SystemExit('FAIL: %s is missing; run scripts/vendor-update.sh' % MANIFEST_PATH)
    return json.loads(MANIFEST_PATH.read_text())


def addon_version(name):
    payload = payload_path(name, 'plugin/plugin.json')
    if not payload.is_file():
        return '?'
    try:
        return json.loads(payload.read_text()).get('version', '?')
    except ValueError:
        return '?'


