# ---------------------------------------------------------------------------
# luna proton: GE-Proton (Steam custom compatibility tool) per machine.
#
# ~/.local/share/Steam is NOT synced, so every machine installs its own tools.
# Steam must be restarted to list a new tool. Per-game mappings live in Steam's
# config/config.vdf (CompatToolMapping); Luna only READS them, never edits them.
# ---------------------------------------------------------------------------

PROTON_REPO = 'GloriousEggroll/proton-ge-custom'
PROTON_API = 'https://api.github.com/repos/%s/releases' % PROTON_REPO
PROTON_TTL = 6 * 3600
PROTON_PREFIX = 'GE-Proton'
PROTON_SIZE_LIMIT = 4 * 1024 ** 3


def proton_tools_dir():
    return steam_home() / 'compatibilitytools.d'


def _proton_open(request, timeout=60):
    """Single network entry point (tests replace it)."""
    return urllib.request.urlopen(request, timeout=timeout)


def _proton_request(url):
    return urllib.request.Request(url, headers={'User-Agent': 'Luna-Proton', 'Accept': 'application/vnd.github+json'})


def proton_version_key(name):
    name = re.sub(r'-(x86_64|aarch64)$', '', str(name))
    return tuple(int(n) for n in re.findall(r'\d+', name)) or (0,)


def _proton_arch_forms(name):
    """Releases ship as GE-ProtonN-M-x86_64 (older ones without the suffix); Steam maps either."""
    plain = re.sub(r'-(x86_64|aarch64)$', '', str(name))
    return {plain, plain + '-x86_64'}


def proton_normalize(version):
    """'11-11' / 'GE-Proton11-11' -> 'GE-Proton11-11'; rejects path-like input."""
    text = str(version or '').strip()
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', text):
        raise ValueError('invalid Proton version: %r' % text)
    return text if not text[0].isdigit() else PROTON_PREFIX + text


def _proton_dir_size(path):
    total = 0
    for root, _dirs, files in os.walk(str(path)):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


def proton_installed():
    """[{name, version, internal, size, path}] for each tool dir, newest first."""
    base = proton_tools_dir()
    tools = []
    try:
        entries = sorted(p for p in base.iterdir() if p.is_dir() and not p.name.startswith('.'))
    except OSError:
        return []
    for path in entries:
        version = path.name
        try:
            fields = (path / 'version').read_text(errors='replace').split()
            if len(fields) >= 2:
                version = fields[-1]
        except OSError:
            pass
        internal = []
        try:
            internal = re.findall(r'^\s{0,6}"([^"]+)"\s*(?://.*)?$\s*\{',
                                  (path / 'compatibilitytool.vdf').read_text(errors='replace').split('"compat_tools"', 1)[-1],
                                  re.M)
        except OSError:
            pass
        link = path.is_symlink()
        tools.append({'name': path.name, 'version': version, 'internal': internal, 'symlink': link,
                      'size': 0 if link else _proton_dir_size(path), 'path': str(path)})
    tools.sort(key=lambda t: proton_version_key(t['name']), reverse=True)
    return tools


def _vdf_tokens(text):
    for match in re.finditer(r'"((?:[^"\\]|\\.)*)"|([{}])|//[^\n]*', text):
        if match.group(1) is not None:
            yield match.group(1)
        elif match.group(2):
            yield match.group(2)


def _vdf_block(tokens, pos):
    """Parse after '{' -> ({key: str|dict}, next position)."""
    out = {}
    while pos < len(tokens):
        tok = tokens[pos]
        if tok == '}':
            return out, pos + 1
        if pos + 1 < len(tokens) and tokens[pos + 1] == '{':
            out[tok], pos = _vdf_block(tokens, pos + 2)
        elif pos + 1 < len(tokens):
            out[tok] = tokens[pos + 1]
            pos += 2
        else:
            break
    return out, pos


def proton_mappings():
    """{appid: tool name} from config.vdf CompatToolMapping (read-only). {} if unreadable."""
    try:
        text = (steam_home() / 'config/config.vdf').read_text(errors='replace')
    except OSError:
        return {}
    tokens = list(_vdf_tokens(text))
    for i, tok in enumerate(tokens):
        if tok == 'CompatToolMapping' and i + 1 < len(tokens) and tokens[i + 1] == '{':
            block, _ = _vdf_block(tokens, i + 2)
            return {app: str(v.get('name') or '') for app, v in block.items()
                    if isinstance(v, dict) and v.get('name')}
    return {}


def _proton_names(tool):
    names = {tool['name']} | set(tool.get('internal') or [])
    for name in list(names):
        names |= _proton_arch_forms(name)
    return names


def proton_mapped_to(tool, mappings=None):
    names = _proton_names(tool)
    return sorted(app for app, name in (proton_mappings() if mappings is None else mappings).items()
                  if name in names)


def proton_missing_mappings(tools=None, mappings=None):
    """{tool name: [appids]} for GE-Proton tools games are mapped to but are not installed."""
    tools = proton_installed() if tools is None else tools
    have = set()
    for tool in tools:
        have |= _proton_names(tool)
    missing = {}
    for app, name in (proton_mappings() if mappings is None else mappings).items():
        if name.startswith(PROTON_PREFIX) and name not in have:
            missing.setdefault(name, []).append(app)
    return missing


def _proton_json(url):
    last = None
    for attempt in range(1, STEAM_DOWNLOAD_ATTEMPTS + 1):
        try:
            with _proton_open(_proton_request(url), 20) as source:
                return json.loads(source.read(8 * 1024 * 1024).decode('utf-8'))
        except urllib.error.HTTPError as error:
            last = error
            if error.code not in STEAM_RETRY_STATUS:
                raise OSError('HTTP %s from %s' % (error.code, url)) from error
            delay = _retry_after_seconds(error)
            error.close()
        except (urllib.error.URLError, TimeoutError, ConnectionError, ValueError) as error:
            last, delay = error, None
        if attempt == STEAM_DOWNLOAD_ATTEMPTS:
            break
        time.sleep(delay if delay is not None else min(30.0, 2.0 ** attempt) * (0.5 + random.random() / 2))
    raise OSError('%s: %s' % (url, last))


def _proton_release_summary(data):
    assets = {a.get('name'): a for a in data.get('assets') or [] if isinstance(a, dict)}
    tag = str(data.get('tag_name') or '')
    base = next((b for b in (tag + '-x86_64', tag) if (b + '.tar.gz') in assets), tag)
    tar, tsum = assets.get(base + '.tar.gz') or {}, assets.get(base + '.sha512sum') or {}
    return {'tag': tag, 'base': base, 'tarUrl': tar.get('browser_download_url', ''), 'tarSize': int(tar.get('size') or 0),
            'sumUrl': tsum.get('browser_download_url', '')}


def proton_release(version='latest', force=False):
    """Release summary (cached 6h for latest); raises OSError on network failure."""
    cache = luna_tools_dir() / 'proton-latest.json'
    if version == 'latest':
        cached = read_json_file(cache) or {}
        if (not force and cached.get('tag') and int(cached.get('checkedAt') or 0) > time.time() - PROTON_TTL):
            return cached
        summary = _proton_release_summary(_proton_json(PROTON_API + '/latest'))
        if summary['tag']:
            summary['checkedAt'] = int(time.time())
            try:
                write_json_file(cache, summary)
            except OSError:
                pass
        return summary
    return _proton_release_summary(_proton_json('%s/tags/%s' % (PROTON_API, re.sub(r'-(x86_64|aarch64)$', '', proton_normalize(version)))))


# ------------------------------------------------------------------ safety
#
# Failure mode this section guards against (docs/PROTON_SINGLE_VERSION.md):
# Steam runs a driver query once per registered compat tool, all with app id 0, so
# every tool shares <steam>/steamapps/compatdata/0/pfx. That prefix holds ~600
# symlinks per architecture straight into the DLLs of the tool that last set it up;
# when a second GE-Proton version's wineboot updates it, it writes THROUGH those
# links and overwrites the other version's Windows DLLs in place. Rule: one
# GE-Proton version per machine. Nothing below hashes anything except `verify`.

PROTON_MANIFEST_VERSION = 1
PROTON_DLL_DIRS = ('files/lib/wine/x86_64-windows', 'files/lib/wine/i386-windows',
                   'files/lib64/wine/x86_64-windows', 'files/lib64/wine/i386-windows')
PROTON_MTIME_SLACK = 3600       # DLLs are built shortly BEFORE the `version` file is written
PROTON_MTIME_MIN_FILES = 20
PROTON_PREFIX_SYSDIRS = ('drive_c/windows/system32', 'drive_c/windows/syswow64')


def proton_manifest_dir():
    return luna_tools_dir() / 'proton-manifests'


def proton_manifest_path(name):
    return proton_manifest_dir() / (name + '.json')


def _proton_manifest_valid(data, size_optional=False):
    """True when `data` has the exact shape proton_compare relies on."""
    if not (isinstance(data, dict) and data.get('version') == PROTON_MANIFEST_VERSION):
        return False
    files, links = data.get('files'), data.get('links', {})
    if not isinstance(files, dict) or not isinstance(links, dict):
        return False
    for rel, entry in files.items():
        if not (isinstance(rel, str) and isinstance(entry, list) and len(entry) == 2
                and ((isinstance(entry[0], int) and not isinstance(entry[0], bool)) or (size_optional and entry[0] is None))
                and isinstance(entry[1], str)):
            return False
    return all(isinstance(k, str) and isinstance(v, str) for k, v in links.items())


def proton_manifest_state(name):
    """'ok' | 'missing' | 'unreadable' (present but malformed, or not valid JSON)."""
    path = proton_manifest_path(name)
    if not os.path.lexists(str(path)):
        return 'missing'
    return 'ok' if _proton_manifest_valid(read_json_file(path)) else 'unreadable'


def proton_manifest_load(name):
    data = read_json_file(proton_manifest_path(name))
    return data if _proton_manifest_valid(data) else None


def proton_ge_tools(tools=None):
    return [t for t in (proton_installed() if tools is None else tools) if t['name'].startswith(PROTON_PREFIX)]


def proton_multi_warning(tools=None):
    """One-paragraph warning when more than one GE-Proton is installed, else ''."""
    ge = proton_ge_tools(tools)
    if len(ge) < 2:
        return ''
    return ('%d GE-Proton versions are installed (%s). Steam probes every registered tool against one shared '
            'prefix (compatdata/0/pfx) whose DLL symlinks point into whichever tool set it up last, so one '
            "version's wineboot can overwrite the other's Windows DLLs in place. Keep one: "
            'luna proton prune (or luna proton remove VERSION).' % (len(ge), ', '.join(t['name'] for t in ge)))


def proton_proc_dir():
    """/proc, unless a test points LUNA_PROC_DIR at a look-alike AND the Steam home is not
    under the real user's home: on a real Steam install the override is ignored, so it can
    never hide a running Steam from a destructive command."""
    override = os.environ.get('LUNA_PROC_DIR')
    if not override:
        return '/proc'
    try:
        import pwd
        real_home = os.path.realpath(pwd.getpwuid(os.getuid()).pw_dir)
    except (ImportError, KeyError, OSError):
        return '/proc'
    steam = os.path.realpath(str(steam_home()))
    return '/proc' if steam == real_home or steam.startswith(real_home + os.sep) else override


def _proton_config_unreadable():
    """Path of config.vdf when it exists but cannot be read (mappings unknown), else ''."""
    config = steam_home() / 'config/config.vdf'
    if not os.path.lexists(str(config)):
        return ''
    if not os.access(str(config), os.R_OK):
        return str(config)
    try:
        with open(str(config), 'rb') as handle:
            handle.read(1)
    except FileNotFoundError:
        return ''
    except OSError:
        return str(config)
    return ''


def _proton_remove_tree(tool):
    """Delete one tool dir + manifest after the final safety checks. '' on success, else why not."""
    path = tool['path']
    if os.path.islink(path):
        return 'it is a symlink; never followed or deleted here'
    busy = proton_busy_reason(path)
    if busy:
        return busy
    try:
        shutil.rmtree(path)
    except OSError as error:
        return 'could not delete it: %s' % error
    try:
        proton_manifest_path(tool['name']).unlink()
    except OSError:
        pass
    return ''


def proton_busy_reason(path=None, proc=None):
    """Why a tool directory must not be replaced or removed right now, or ''.
    Steam up (it owns the shared prefix links) or any wine process running from `path`."""
    proc = proc or proton_proc_dir()
    scan = steam_process_scan(proc)
    if scan['running']:
        return 'Steam is running; close it completely first (its prefix links point into the tool directory)'
    if scan['error']:  # fail CLOSED: an unreadable process table is not "Steam is not running"
        return 'cannot tell whether Steam is running (%s); not touching the tool directory' % scan['error']
    if path:
        root = os.path.realpath(str(path))
        try:
            entries = [e for e in Path(proc).iterdir() if e.name.isdigit()]
        except OSError as error:
            return 'cannot list the processes (%s); not touching the tool directory' % error
        uid = os.getuid()
        for entry in entries:
            try:
                if entry.stat().st_uid != uid:
                    continue
                hit = os.path.realpath(os.readlink(str(entry / 'exe'))).startswith(root + os.sep) \
                    if (entry / 'exe').is_symlink() else False
                if not hit:
                    hit = any(a.startswith(root + os.sep) or a == root for a in
                              (entry / 'cmdline').read_bytes().decode('utf-8', 'replace').split('\0'))
            except (FileNotFoundError, ProcessLookupError, NotADirectoryError):
                continue  # exited while we looked
            except OSError as error:
                return 'cannot inspect process %s (%s); not touching the tool directory' % (entry.name, error)
            if hit:
                return 'a process (pid %s) is running from %s; stop it first' % (entry.name, path)
    return ''


def _proton_progress(label, total):
    """Throttled byte progress for hashing: \\r on a TTY, sparse lines otherwise."""
    state = {'last': 0.0}
    tty = sys.stdout.isatty()

    def update(done, final=False):
        now = time.monotonic()
        if not final and now - state['last'] < (0.3 if tty else 5.0):
            return
        state['last'] = now
        text = '%s of %s (%d%%)' % (_format_mb(done), _format_mb(total), 100 * done // total if total else 100)
        if tty:
            print('\r\033[Kproton    : %s  %s' % (label, text), end='\n' if final else '', flush=True)
        else:
            print('proton    :   %s  %s' % (label, text), flush=True)
    return update


def _proton_walk(root):
    """({rel: size} regular files, {rel: link target}) under root; __pycache__ is ignored
    (Proton's own python writes it on first run)."""
    files, links = {}, {}
    base = str(root)
    for current, dirs, names in os.walk(base):
        dirs[:] = sorted(d for d in dirs if d != '__pycache__')
        for name in sorted(names + [d for d in dirs if os.path.islink(os.path.join(current, d))]):
            full = os.path.join(current, name)
            rel = os.path.relpath(full, base)
            try:
                if os.path.islink(full):
                    links[rel] = os.readlink(full)
                elif name.endswith('.pyc') and os.path.basename(current) == '__pycache__':
                    continue
                else:
                    files[rel] = os.lstat(full).st_size
            except OSError:
                pass
    return files, links


def _proton_hash_file(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def proton_build_manifest(root, name, source, tag=''):
    """Hash a tree (progress shown). Only ever call this on a tree that came from a
    sha512-verified download, never on an install of unknown state."""
    files, links = _proton_walk(root)
    total = sum(files.values())
    progress = _proton_progress('hashing %s' % name, total)
    done, out = 0, {}
    for rel in sorted(files):
        out[rel] = [files[rel], _proton_hash_file(os.path.join(str(root), rel))]
        done += files[rel]
        progress(done)
    progress(done, final=True)
    return {'version': PROTON_MANIFEST_VERSION, 'name': name, 'tag': tag, 'source': source,
            'createdAt': int(time.time()), 'files': out, 'links': links}


def proton_compare(root, expected, expected_links=None, label='verifying'):
    """Compare a tree with {rel: [size|None, sha256]} (+ optional {rel: link target}).
    Returns {'changed': [], 'missing': [], 'extra': [], 'checked': n}. ValueError on a bad shape."""
    if not _proton_manifest_valid({'version': PROTON_MANIFEST_VERSION, 'files': expected,
                                   'links': {} if expected_links is None else expected_links}, size_optional=True):
        raise ValueError('malformed manifest data')
    files, links = _proton_walk(root)
    changed, missing = [], []
    todo = [rel for rel in sorted(expected) if rel in files]
    for rel in sorted(expected):
        if rel not in files:
            (changed if rel in links else missing).append(rel)
    total = sum(files[r] for r in todo)
    progress = _proton_progress('%s %s' % (label, os.path.basename(str(root))), total)
    done = 0
    for rel in todo:
        size, sha = expected[rel][0], expected[rel][1]
        if size is not None and size != files[rel]:
            changed.append(rel)
        else:
            try:
                if _proton_hash_file(os.path.join(str(root), rel)) != sha:
                    changed.append(rel)
            except OSError:
                missing.append(rel)
        done += files[rel]
        progress(done)
    progress(done, final=True)
    extra = [rel for rel in files if rel not in expected and rel not in (expected_links or {})]
    if expected_links is not None:
        for rel, target in expected_links.items():
            if rel not in links:
                (changed if rel in files else missing).append(rel)
            elif links[rel] != target:
                changed.append(rel)
        extra += [rel for rel in links if rel not in expected_links and rel not in expected]
    return {'changed': sorted(set(changed)), 'missing': sorted(set(missing)),
            'extra': sorted(set(extra)), 'checked': len(expected)}


def _proton_report(name, result):
    bad = result['changed'] or result['missing'] or result['extra']
    if not bad:
        print('verify    : %s OK (%d files match)' % (name, result['checked']))
        return True
    print('verify    : %s MISMATCH: %d changed, %d missing, %d extra (of %d files)'
          % (name, len(result['changed']), len(result['missing']), len(result['extra']), result['checked']))
    for kind in ('changed', 'missing', 'extra'):
        for rel in result[kind][:5]:
            print('            %-8s %s' % (kind, rel))
        if len(result[kind]) > 5:
            print('            %-8s ... and %d more' % (kind, len(result[kind]) - 5))
    print('verify    : fix: luna proton remove %s --force && luna proton install %s  (Steam closed)'
          % (name, name))
    return False


def _proton_read_reference(path):
    """sha256sum-format file -> {rel: [None, sha]}; the leading <tool>/ component is dropped."""
    out = {}
    for line in Path(path).read_text(errors='replace').splitlines():
        match = re.match(r'^([0-9a-fA-F]{64})\s[ *]?(.+)$', line)
        if match:
            rel = match.group(2)
            rel = rel.split('/', 1)[1] if '/' in rel else rel
            out[rel] = [None, match.group(1).lower()]
    return out


def proton_verify(version=None, rebuild=False, against=None):
    """0 = every checked tool matches; 1 = mismatch, missing manifest or error."""
    tools = proton_ge_tools()
    if version:
        try:
            name = proton_normalize(version)
        except ValueError as error:
            print('verify    : %s' % error)
            return 1
        tools = [t for t in tools if t['name'] in _proton_arch_forms(name)]
        if not tools:
            print('verify    : %s is not installed' % name)
            return 1
    if not tools:
        print('verify    : no GE-Proton installed')
        return EXIT_OK
    if (rebuild or against) and len(tools) != 1:
        print('verify    : --rebuild/--against need exactly one VERSION')
        return 1
    ok = True
    for tool in tools:
        name = tool['name']
        if against:
            try:
                reference = _proton_read_reference(against)
            except OSError as error:
                print('verify    : cannot read %s: %s' % (against, error))
                return 1
            result = proton_compare(tool['path'], reference, None)
            # a reference lists regular files only: links in the tree are not "extra"
            result['extra'] = [r for r in result['extra'] if not os.path.islink(os.path.join(tool['path'], r))]
            ok = _proton_report(name, result) and ok
            continue
        if rebuild:
            code = _proton_rebuild(tool)
            ok = ok and code == EXIT_OK
            continue
        if proton_manifest_state(name) == 'unreadable':
            print('verify    : %s: manifest unreadable or malformed (%s); it is not trusted. Run: '
                  'luna proton verify %s --rebuild' % (name, proton_manifest_path(name), name))
            ok = False
            continue
        manifest = proton_manifest_load(name)
        if not manifest:
            print('verify    : %s has no manifest (installed before manifests existed, or by hand). '
                  'It is never created from the current disk state; run: luna proton verify %s --rebuild '
                  '(re-downloads and sha512-checks the release)' % (name, name))
            ok = False
            continue
        ok = _proton_report(name, proton_compare(tool['path'], manifest['files'], manifest.get('links'))) and ok
    return EXIT_OK if ok else 1


def _proton_rebuild(tool):
    """Create the manifest from a freshly downloaded, sha512-verified release, then check
    the installed tree against it."""
    name = tool['name']
    try:
        release = proton_release(re.sub(r'-(x86_64|aarch64)$', '', name), force=True)
    except (OSError, ValueError) as error:
        print('verify    : cannot reach the release API: %s' % error)
        return 1
    base = proton_tools_dir()
    work = Path(tempfile.mkdtemp(prefix='.luna-proton-', dir=str(base)))
    try:
        top = _proton_fetch_verified(release, work)
        if top is None:
            return 1
        if top.name not in _proton_arch_forms(name):
            print('verify    : the release unpacks as %s, not %s; not recording a manifest' % (top.name, name))
            return 1
        manifest = proton_build_manifest(top, name, 'rebuild', release['tag'])
    except (OSError, ValueError, tarfile.TarError) as error:
        print('verify    : rebuild failed: %s' % error)
        return 1
    finally:
        shutil.rmtree(str(work), ignore_errors=True)
    try:
        write_json_file(proton_manifest_path(name), manifest)
    except OSError as error:
        print('verify    : cannot write the manifest: %s' % error)
        return 1
    print('verify    : manifest for %s written from the verified release (%d files) -> %s'
          % (name, len(manifest['files']), proton_manifest_path(name)))
    result = proton_compare(tool['path'], manifest['files'], manifest['links'])
    return EXIT_OK if _proton_report(name, result) else 1


# ------------------------------------------------------------ cheap heuristics

def _proton_dll_finding(tool):
    base = Path(tool['path'])
    try:
        ref = (base / 'version').stat().st_mtime + PROTON_MTIME_SLACK
    except OSError:
        return None
    total = newer = 0
    dirs = []
    for rel in PROTON_DLL_DIRS:
        count = 0
        try:
            with os.scandir(str(base / rel)) as entries:
                for entry in entries:
                    if not entry.name.lower().endswith(('.dll', '.exe', '.sys', '.drv')):
                        continue
                    try:
                        mtime = entry.stat(follow_symlinks=False).st_mtime
                    except OSError:
                        continue
                    total += 1
                    if mtime > ref:
                        newer += 1
                        count += 1
        except OSError:
            continue
        if count:
            dirs.append('%s (%d)' % (rel.split('wine/', 1)[-1], count))
    if newer >= PROTON_MTIME_MIN_FILES and newer * 20 >= total:
        return {'id': 'proton.dll-mtimes', 'severity': 'warn', 'tool': tool['name'],
                'message': '%s: %d of %d Windows DLLs are newer than the tool itself in %s; a wineboot prefix '
                           'update probably overwrote them with another version\'s' % (tool['name'], newer, total, ', '.join(dirs)),
                'fix': 'luna proton verify %s (add --rebuild if it has no manifest), then reinstall it' % tool['name']}
    return None


def _proton_prefix_owner(compat):
    """Tool names compatdata/0 says last set the prefix up (config_info paths, version text)."""
    names = set()
    try:
        names |= set(re.findall(r'compatibilitytools\.d/([A-Za-z0-9._-]+)', (compat / 'config_info').read_text(errors='replace')))
    except OSError:
        pass
    try:
        text = (compat / 'version').read_text(errors='replace')
        for tool in proton_installed():
            if tool['name'] in text or (tool['version'] and tool['version'] in text.split()):
                names.add(tool['name'])
    except OSError:
        pass
    return names


def proton_prefix_findings():
    """Dangling or foreign symlinks in the shared compatdata/0 prefix (readlink only)."""
    compat = steam_home() / 'steamapps/compatdata/0'
    tools_dir = str(proton_tools_dir())
    roots = {tools_dir, os.path.realpath(tools_dir)}
    seen, dangling = {}, {}
    for sysdir in PROTON_PREFIX_SYSDIRS:
        folder = compat / 'pfx' / sysdir
        try:
            entries = list(os.scandir(str(folder)))
        except OSError:
            continue
        for entry in entries:
            if not entry.is_symlink():
                continue
            target = os.path.normpath(os.path.join(str(folder), os.readlink(entry.path)))
            for root in roots:
                if target.startswith(root + os.sep):
                    tool = target[len(root) + 1:].split(os.sep)[0]
                    seen[tool] = seen.get(tool, 0) + 1
                    if not os.path.exists(target):
                        dangling[tool] = dangling.get(tool, 0) + 1
                    break
    found = []
    for tool, count in sorted(dangling.items()):
        found.append({'id': 'proton.prefix-dangling', 'severity': 'warn', 'tool': tool,
                      'message': '%d link(s) in the shared prefix compatdata/0/pfx point into %s, which %s'
                                 % (count, tool, 'is missing files' if (proton_tools_dir() / tool).is_dir() else 'no longer exists'),
                      'fix': 'close Steam, then luna proton verify; Steam repairs the prefix on its next start with the one installed tool'})
    owner = _proton_prefix_owner(compat)
    owner_forms = set()
    for name in owner:
        owner_forms |= _proton_arch_forms(name)
    foreign = {t: c for t, c in seen.items() if t not in dangling and owner and t not in owner_forms}
    mixed = len(seen) > 1 and not owner
    for tool, count in sorted(foreign.items()):
        found.append({'id': 'proton.prefix-foreign', 'severity': 'warn', 'tool': tool,
                      'message': '%d link(s) in compatdata/0/pfx point into %s but the prefix was last set up by %s: '
                                 'the next prefix update can overwrite %s\'s DLLs' % (count, tool, ', '.join(sorted(owner)), tool),
                      'fix': 'keep one GE-Proton (luna proton prune), then luna proton verify'})
    if mixed:
        found.append({'id': 'proton.prefix-foreign', 'severity': 'warn', 'tool': '',
                      'message': 'links in compatdata/0/pfx point into %d different tools (%s)' % (len(seen), ', '.join(sorted(seen))),
                      'fix': 'keep one GE-Proton (luna proton prune), then luna proton verify'})
    return found


def proton_health(tools=None):
    """Cheap, hash-free findings: [{id, severity, message, fix, tool}]."""
    tools = proton_ge_tools(tools)
    found = []
    warning = proton_multi_warning(tools)
    if warning:
        found.append({'id': 'proton.multi-version', 'severity': 'warn', 'tool': '', 'message': warning,
                      'fix': 'luna proton prune'})
    for tool in tools:
        item = _proton_dll_finding(tool)
        if item:
            found.append(item)
    found.extend(proton_prefix_findings())
    return found


def proton_status_data():
    tools = proton_installed()
    try:
        latest = proton_release()['tag'] or 'unknown'
    except OSError:
        latest = 'unknown'
    mappings = proton_mappings()
    newest = max((t['name'] for t in tools if t['name'].startswith(PROTON_PREFIX)),
                 key=proton_version_key, default='')
    return {'tools': [dict({k: t[k] for k in ('name', 'version', 'size')},
                           manifest=proton_manifest_load(t['name']) is not None) for t in tools],
            'latest': latest,
            'updateAvailable': bool(latest != 'unknown' and
                                    (not newest or proton_version_key(latest) > proton_version_key(newest))),
            'mappedGames': sum(1 for _ in mappings),
            'missing': proton_missing_mappings(tools, mappings),
            'warnings': proton_health(tools),
            'toolsDir': str(proton_tools_dir())}


def proton_status(as_json):
    data = proton_status_data()
    if as_json:
        print(json.dumps(data, indent=2, sort_keys=True))
        return EXIT_OK
    print('proton    : %s' % data['toolsDir'])
    if not data['tools']:
        print('installed : none')
    for tool in data['tools']:
        print('installed : %-28s %-16s %s%s' % (tool['name'], tool['version'], _format_mb(tool['size']),
                                                '' if tool['manifest'] or not tool['name'].startswith(PROTON_PREFIX)
                                                else '  (no manifest: luna proton verify %s --rebuild)' % tool['name']))
    note = '  (update available: luna proton install)' if data['updateAvailable'] else ''
    print('latest    : %s%s' % (data['latest'], note))
    for name, apps in sorted(data['missing'].items()):
        print('missing   : %s is mapped by %d game(s) (appid %s); run: luna proton install %s'
              % (name, len(apps), ', '.join(sorted(apps)[:6]), name))
    for item in data['warnings']:
        print('warning   : %s\n            fix: %s' % (item['message'], item['fix']))
    return EXIT_OK


def _proton_virtual_resolve(links, name, limit=40):
    """Resolve `name` (relative to the extraction root) through the symlinks the archive has
    created so far, in a virtual filesystem. Returns the normalised parts, or None when the
    path escapes the root. `links` maps a normalised member path (tuple) to its target text."""
    pending = [p for p in name.split('/') if p not in ('', '.')]
    done = []
    hops = 0
    while pending:
        part = pending.pop(0)
        if part == '..':
            if not done:
                return None
            done.pop()
            continue
        done.append(part)
        target = links.get(tuple(done))
        if target is not None:
            hops += 1
            if hops > limit or target.startswith('/'):
                return None
            done.pop()
            pending = [p for p in target.split('/') if p not in ('', '.')] + pending
    return done


def _proton_safe_extract(archive, dest):
    """Validate every member against a virtual filesystem (so `a -> ../..` followed by `a/x`
    is caught), then extract with the tarfile 'data' filter where Python has it."""
    links = {}
    for member in archive.getmembers():
        if member.isdev():
            raise ValueError('device node in archive: %s' % member.name)
        if member.name.startswith('/'):
            raise ValueError('unsafe path in archive: %s' % member.name)
        parent = _proton_virtual_resolve(links, os.path.dirname(member.name.rstrip('/')))
        if parent is None:
            raise ValueError('unsafe path in archive: %s' % member.name)
        leaf = os.path.basename(member.name.rstrip('/'))
        if leaf in ('', '.', '..'):
            if member.isdir() and leaf in ('', '.'):
                continue
            raise ValueError('unsafe path in archive: %s' % member.name)
        here = tuple(parent) + (leaf,)
        if member.issym():
            if os.path.isabs(member.linkname):
                raise ValueError('absolute symlink in archive: %s' % member.name)
            if _proton_virtual_resolve(links, '/'.join(parent + [member.linkname])) is None:
                raise ValueError('symlink escapes the archive: %s -> %s' % (member.name, member.linkname))
            links[here] = member.linkname
        elif member.islnk():
            if os.path.isabs(member.linkname) or _proton_virtual_resolve(links, member.linkname) is None:
                raise ValueError('unsafe hard link in archive: %s' % member.name)
            links.pop(here, None)
        else:
            links.pop(here, None)
    if hasattr(tarfile, 'data_filter'):
        archive.extractall(dest, filter='data')
    else:
        archive.extractall(dest)


def _proton_download(url, destination, total=0, label=None):
    """Stream `url` to `destination` with retry/backoff; returns sha512 hex."""
    for attempt in range(1, STEAM_DOWNLOAD_ATTEMPTS + 1):
        digest = hashlib.sha512()
        progress = _Progress(label or 'download', total)
        try:
            with _proton_open(_proton_request(url)) as source, open(destination, 'wb') as out:
                try:
                    progress.total = int(source.headers.get('Content-Length') or total)
                except (AttributeError, TypeError, ValueError):
                    pass
                size = 0
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > PROTON_SIZE_LIMIT:
                        raise ValueError('download exceeds size limit')
                    digest.update(chunk)
                    out.write(chunk)
                    progress.update(size)
            progress.update(size, final=True)
            return digest.hexdigest()
        except urllib.error.HTTPError as error:
            if error.code not in STEAM_RETRY_STATUS or attempt == STEAM_DOWNLOAD_ATTEMPTS:
                raise OSError('HTTP %s from %s' % (error.code, url)) from error
            delay, reason = _retry_after_seconds(error), 'HTTP %s' % error.code
            error.close()
        except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
            if attempt == STEAM_DOWNLOAD_ATTEMPTS:
                raise OSError('%s: %s' % (url, error)) from error
            delay, reason = None, str(error)
        if delay is None:
            delay = min(60.0, 2.0 ** attempt) * (0.5 + random.random() / 2)
        print('proton    : %s; retry %d/%d in %.0fs' % (reason, attempt, STEAM_DOWNLOAD_ATTEMPTS - 1, delay), flush=True)
        time.sleep(delay)


def _proton_fetch_verified(release, work):
    """Download the release tarball into `work`, check its sha512 and unpack it.
    Returns the single extracted top-level directory, or None (reason printed)."""
    tag = release['tag']
    asset = release.get('base') or tag
    try:
        with _proton_open(_proton_request(release['sumUrl'])) as source:
            text = source.read(65536).decode('utf-8', 'replace')
    except (OSError, urllib.error.URLError) as error:
        print('proton    : cannot fetch checksum: %s' % error)
        return None
    expected = next((line.split()[0].lower() for line in text.splitlines()
                     if line.split() and line.split()[-1].lstrip('*') == asset + '.tar.gz'), '')
    if not re.fullmatch(r'[0-9a-f]{128}', expected):
        print('proton    : no valid sha512 for %s.tar.gz in the checksum file' % asset)
        return None
    tarball = work / (asset + '.tar.gz')
    actual = _proton_download(release['tarUrl'], tarball, release['tarSize'], tag)
    if actual != expected:
        print('proton    : SHA512 mismatch for %s; nothing installed' % tag)
        return None
    print('proton    : sha512 verified; extracting')
    extract = work / 'x'
    extract.mkdir()
    with tarfile.open(str(tarball), 'r:gz') as archive:
        _proton_safe_extract(archive, str(extract))
    tops = [p for p in extract.iterdir()]
    if len(tops) != 1 or not tops[0].is_dir() or tops[0].name not in (tag, asset):
        print('proton    : unexpected archive layout (expected a single %s/ directory)' % asset)
        return None
    return tops[0]


def _proton_space_ok(release, base):
    need = max(release['tarSize'], 300 * 1024 ** 2) * 4  # tarball + extracted tree + slack
    probe = base if base.exists() else base.parent
    free = shutil.disk_usage(str(probe)).free
    if free < need:
        print('proton    : not enough free space in %s: %s free, ~%s needed' % (probe, _format_mb(free), _format_mb(need)))
        return False
    return True


def proton_install(version='latest', dry_run=False):
    try:
        release = proton_release(version, force=True)
    except (OSError, ValueError) as error:
        print('proton    : cannot reach the release API: %s' % error)
        return 1
    tag = release['tag']
    if not tag or not release['tarUrl'] or not release['sumUrl']:
        print('proton    : release %s has no .tar.gz + .sha512sum assets' % (tag or version))
        return 1
    base = proton_tools_dir()
    asset = release.get('base') or tag
    present = next((n for n in (tag, asset) if (base / n).exists()), '')
    if present:
        print('proton    : %s is already installed (%s); nothing to do' % (tag, base / present))
        return EXIT_OK
    if not dry_run:
        busy = proton_busy_reason()
        if busy:
            print('proton    : refusing to install: %s' % busy)
            return EXIT_REFUSED
        base.mkdir(parents=True, exist_ok=True)
    if not _proton_space_ok(release, base):
        return 1
    if dry_run:
        print('proton    : would install %s (%s) into %s' % (tag, _format_mb(release['tarSize']), base))
        print('proton    : would verify against %s' % release['sumUrl'])
        print('proton    : would record an integrity manifest in %s' % proton_manifest_dir())
        return EXIT_OK
    work = Path(tempfile.mkdtemp(prefix='.luna-proton-', dir=str(base)))
    try:
        top = _proton_fetch_verified(release, work)
        if top is None:
            return 1
        if (base / top.name).exists():
            print('proton    : %s appeared meanwhile; leaving it alone' % top.name)
            return 1
        manifest = proton_build_manifest(top, top.name, 'install', tag)  # from the sha512-verified tree
        busy = proton_busy_reason()  # the download took minutes: Steam may have started meanwhile
        if busy:
            print('proton    : refusing to install: %s' % busy)
            return EXIT_REFUSED
        if (base / top.name).exists() or os.path.islink(str(base / top.name)):
            print('proton    : %s appeared meanwhile; leaving it alone' % top.name)
            return 1
        os.rename(str(top), str(base / top.name))
        name = top.name
    except (OSError, ValueError, tarfile.TarError) as error:
        print('proton    : install failed: %s' % error)
        return 1
    finally:
        shutil.rmtree(str(work), ignore_errors=True)
    try:
        write_json_file(proton_manifest_path(name), manifest)
    except OSError as error:
        print('proton    : installed, but the integrity manifest could not be written: %s' % error)
    print('proton    : installed %s; restart Steam to see it in the compatibility list' % tag)
    warning = proton_multi_warning()
    if warning:
        print('proton    : WARNING: %s' % warning)
        print('proton    : the old version was NOT removed; run: luna proton prune')
    return EXIT_OK


def proton_remove(version, force=False, dry_run=False):
    try:
        name = proton_normalize(version)
    except ValueError as error:
        print('proton    : %s' % error)
        return 1
    tool = next((t for t in proton_installed() if t['name'] in _proton_arch_forms(name)), None)
    if not tool:
        print('proton    : %s is not installed' % name)
        return 1
    if tool.get('symlink'):
        print('proton    : refusing: %s is a symlink (%s); it is never followed or deleted here' % (name, tool['path']))
        return EXIT_REFUSED
    unreadable = _proton_config_unreadable()
    if unreadable and not force:
        print('proton    : refusing: cannot read %s, so games mapped to %s cannot be detected; use --force'
              % (unreadable, name))
        return EXIT_REFUSED
    apps = proton_mapped_to(tool)
    if apps and not force:
        print('proton    : refusing: %d game(s) are mapped to %s (appid %s); use --force'
              % (len(apps), name, ', '.join(apps[:6])))
        return EXIT_REFUSED
    if dry_run:
        print('proton    : would remove %s' % name)
        return EXIT_OK
    problem = _proton_remove_tree(tool)
    if problem:
        print('proton    : refusing to remove %s: %s' % (name, problem))
        return EXIT_REFUSED
    print('proton    : removed %s' % name)
    return EXIT_OK


def proton_prune(keep, dry_run=False):
    """Remove all but the newest `keep` GE-Proton. A version any game is mapped to is
    never removed here (only `remove --force` does that)."""
    if keep < 0:
        print('proton    : --keep must be >= 0')
        return 1
    unreadable = _proton_config_unreadable()
    if unreadable:
        print('proton    : cannot read %s, so mapped versions cannot be protected; nothing pruned' % unreadable)
        return EXIT_REFUSED
    mappings = proton_mappings()
    ge = proton_ge_tools()
    victims = ge[keep:]
    if not victims:
        print('proton    : nothing to prune (%d GE-Proton installed, keeping %d)' % (len(ge), keep))
        return EXIT_OK
    if not dry_run:
        busy = proton_busy_reason()
        if busy:
            print('proton    : refusing to prune: %s' % busy)
            return EXIT_REFUSED
    refused = False
    for tool in victims:
        apps = proton_mapped_to(tool, mappings)
        if apps:
            print('proton    : keeping %s (mapped by %d game(s))' % (tool['name'], len(apps)))
        elif tool.get('symlink'):
            print('proton    : not removing %s: it is a symlink (%s); remove the link yourself if you mean to'
                  % (tool['name'], tool['path']))
            refused = True
        elif dry_run:
            print('proton    : would remove %s' % tool['name'])
        else:
            problem = _proton_remove_tree(tool)
            if problem:
                print('proton    : not removing %s: %s' % (tool['name'], problem))
                refused = True
                continue
            print('proton    : removed %s' % tool['name'])
    warning = proton_multi_warning()
    if warning and not dry_run:
        print('proton    : WARNING: a mapped version is still installed next to the newest one. '
              'Remap those games to the newest tool in Steam, then run: luna proton prune')
    return EXIT_REFUSED if refused else EXIT_OK


def cmd_proton(options):
    action = options.proton_action
    if action == 'status':
        return proton_status(options.json)
    if action == 'install':
        return proton_install(options.proton_version or 'latest', options.dry_run)
    if action == 'remove':
        if not options.proton_version:
            print('proton    : remove needs a VERSION')
            return 1
        return proton_remove(options.proton_version, options.force, options.dry_run)
    if action == 'verify':
        return proton_verify(options.proton_version, options.rebuild, options.against)
    return proton_prune(options.keep, options.dry_run)


def _doctor_proton():
    found = []
    for item in proton_health():
        found.append(_finding(item['id'], item['severity'], item['message'], item['fix']))
    for tool in proton_ge_tools():
        if proton_manifest_state(tool['name']) == 'unreadable':
            found.append(_finding('proton.manifest-unreadable', 'warn',
                                  '%s: the integrity manifest is unreadable or malformed, so it is not trusted' % tool['name'],
                                  'luna proton verify %s --rebuild' % tool['name']))
        elif not proton_manifest_load(tool['name']):
            found.append(_finding('proton.no-manifest', 'info',
                                  '%s has no integrity manifest, so corruption cannot be proven' % tool['name'],
                                  'luna proton verify %s --rebuild' % tool['name']))
    for name, apps in sorted(proton_missing_mappings().items()):
        found.append(_finding('proton.missing-tool', 'warn',
                              '%d game(s) are mapped to %s, which is not installed on this machine'
                              % (len(apps), name), 'luna proton install %s' % name))
    return found
