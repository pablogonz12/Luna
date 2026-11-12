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
            fields = (path / 'version').read_text().split()
            if len(fields) >= 2:
                version = fields[-1]
        except OSError:
            pass
        internal = []
        try:
            internal = re.findall(r'^\s{0,6}"([^"]+)"\s*(?://.*)?$\s*\{',
                                  (path / 'compatibilitytool.vdf').read_text().split('"compat_tools"', 1)[-1],
                                  re.M)
        except OSError:
            pass
        tools.append({'name': path.name, 'version': version, 'internal': internal,
                      'size': _proton_dir_size(path), 'path': str(path)})
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


def proton_status_data():
    tools = proton_installed()
    try:
        latest = proton_release()['tag'] or 'unknown'
    except OSError:
        latest = 'unknown'
    mappings = proton_mappings()
    newest = max((t['name'] for t in tools if t['name'].startswith(PROTON_PREFIX)),
                 key=proton_version_key, default='')
    return {'tools': [{k: t[k] for k in ('name', 'version', 'size')} for t in tools],
            'latest': latest,
            'updateAvailable': bool(latest != 'unknown' and
                                    (not newest or proton_version_key(latest) > proton_version_key(newest))),
            'mappedGames': sum(1 for _ in mappings),
            'missing': proton_missing_mappings(tools, mappings),
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
        print('installed : %-28s %-16s %s' % (tool['name'], tool['version'], _format_mb(tool['size'])))
    note = '  (update available: luna proton install)' if data['updateAvailable'] else ''
    print('latest    : %s%s' % (data['latest'], note))
    for name, apps in sorted(data['missing'].items()):
        print('missing   : %s is mapped by %d game(s) (appid %s); run: luna proton install %s'
              % (name, len(apps), ', '.join(sorted(apps)[:6]), name))
    return EXIT_OK


def _proton_safe_extract(archive, dest):
    root = os.path.realpath(dest)
    for member in archive.getmembers():
        target = os.path.realpath(os.path.join(root, member.name))
        if not (target == root or target.startswith(root + os.sep)):
            raise ValueError('unsafe path in archive: %s' % member.name)
        if member.issym() and os.path.isabs(member.linkname):
            raise ValueError('absolute symlink in archive: %s' % member.name)
        if member.islnk():
            link = os.path.realpath(os.path.join(root, member.linkname))
            if not link.startswith(root + os.sep):
                raise ValueError('unsafe hard link in archive: %s' % member.name)
        if member.isdev():
            raise ValueError('device node in archive: %s' % member.name)
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
        except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
            if attempt == STEAM_DOWNLOAD_ATTEMPTS:
                raise OSError('%s: %s' % (url, error)) from error
            delay, reason = None, str(error)
        if delay is None:
            delay = min(60.0, 2.0 ** attempt) * (0.5 + random.random() / 2)
        print('proton    : %s; retry %d/%d in %.0fs' % (reason, attempt, STEAM_DOWNLOAD_ATTEMPTS - 1, delay), flush=True)
        time.sleep(delay)


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
    need = max(release['tarSize'], 300 * 1024 ** 2) * 4  # tarball + extracted tree + slack
    base.mkdir(parents=True, exist_ok=True) if not dry_run else None
    probe = base if base.exists() else base.parent
    free = shutil.disk_usage(str(probe)).free
    if free < need:
        print('proton    : not enough free space in %s: %s free, ~%s needed' % (probe, _format_mb(free), _format_mb(need)))
        return 1
    if dry_run:
        print('proton    : would install %s (%s) into %s' % (tag, _format_mb(release['tarSize']), base))
        print('proton    : would verify against %s' % release['sumUrl'])
        return EXIT_OK
    work = Path(tempfile.mkdtemp(prefix='.luna-proton-', dir=str(base)))
    try:
        try:
            with _proton_open(_proton_request(release['sumUrl'])) as source:
                text = source.read(65536).decode('utf-8', 'replace')
        except (OSError, urllib.error.URLError) as error:
            print('proton    : cannot fetch checksum: %s' % error)
            return 1
        expected = next((line.split()[0].lower() for line in text.splitlines()
                         if line.split() and line.split()[-1].lstrip('*') == asset + '.tar.gz'), '')
        if not re.fullmatch(r'[0-9a-f]{128}', expected):
            print('proton    : no valid sha512 for %s.tar.gz in the checksum file' % asset)
            return 1
        tarball = work / (asset + '.tar.gz')
        actual = _proton_download(release['tarUrl'], tarball, release['tarSize'], tag)
        if actual != expected:
            print('proton    : SHA512 mismatch for %s; nothing installed' % tag)
            return 1
        print('proton    : sha512 verified; extracting')
        extract = work / 'x'
        extract.mkdir()
        with tarfile.open(str(tarball), 'r:gz') as archive:
            _proton_safe_extract(archive, str(extract))
        tops = [p for p in extract.iterdir()]
        if len(tops) != 1 or not tops[0].is_dir() or tops[0].name not in (tag, asset):
            print('proton    : unexpected archive layout (expected a single %s/ directory)' % asset)
            return 1
        if (base / tops[0].name).exists():
            print('proton    : %s appeared meanwhile; leaving it alone' % tops[0].name)
            return 1
        os.rename(str(tops[0]), str(base / tops[0].name))
    except (OSError, ValueError, tarfile.TarError) as error:
        print('proton    : install failed: %s' % error)
        return 1
    finally:
        shutil.rmtree(str(work), ignore_errors=True)
    print('proton    : installed %s; restart Steam to see it in the compatibility list' % tag)
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
    apps = proton_mapped_to(tool)
    if apps and not force:
        print('proton    : refusing: %d game(s) are mapped to %s (appid %s); use --force'
              % (len(apps), name, ', '.join(apps[:6])))
        return EXIT_REFUSED
    if dry_run:
        print('proton    : would remove %s' % name)
        return EXIT_OK
    shutil.rmtree(tool['path'])
    print('proton    : removed %s' % name)
    return EXIT_OK


def proton_prune(keep, dry_run=False):
    if keep < 0:
        print('proton    : --keep must be >= 0')
        return 1
    mappings = proton_mappings()
    ge = [t for t in proton_installed() if t['name'].startswith(PROTON_PREFIX)]
    victims = ge[keep:]
    if not victims:
        print('proton    : nothing to prune (%d GE-Proton installed, keeping %d)' % (len(ge), keep))
        return EXIT_OK
    for tool in victims:
        apps = proton_mapped_to(tool, mappings)
        if apps:
            print('proton    : keeping %s (mapped by %d game(s))' % (tool['name'], len(apps)))
        elif dry_run:
            print('proton    : would remove %s' % tool['name'])
        else:
            shutil.rmtree(tool['path'])
            print('proton    : removed %s' % tool['name'])
    return EXIT_OK


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
    return proton_prune(options.keep, options.dry_run)


def _doctor_proton():
    found = []
    for name, apps in sorted(proton_missing_mappings().items()):
        found.append(_finding('proton.missing-tool', 'warn',
                              '%d game(s) are mapped to %s, which is not installed on this machine'
                              % (len(apps), name), 'luna proton install %s' % name))
    return found
