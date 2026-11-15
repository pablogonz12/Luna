# ---------------------------------------------------------------------------
# Steam client version management.
#
# A Steam client update can move the interfaces SLSsteam hooks, breaking
# installs and launches until the integration catches up. Luna cannot stop
# Valve's updater on its own, but it can pin the working build, keep a local
# cache of its packages, and put that build back with Steam closed.
# Everything here is machine-local (luna_tools_dir is excluded from sync):
# each machine pins the build it actually validated.

def steam_home():
    native = home() / '.local/share/Steam'
    alternate = home() / '.steam/steam'
    return alternate.resolve() if not native.exists() and alternate.is_dir() else native


def steam_tools_files():
    """SLSsteam binaries whose unexpected change invalidates a pin."""
    base = home() / '.local/share/SLSsteam'
    return {'SLSsteam.so': base / 'SLSsteam.so',
            'library-inject.so': base / 'library-inject.so'}


def steam_client_build():
    """Verified installed binaries, then the on-disk manifest, then runtime logs.

    A package manifest alone does NOT prove that its client was extracted.
    The install receipt is preferred only while the core executable hashes match.
    Newer clients log "Client version: 0" at startup, so a zero report is
    treated as "no information" rather than a build number.
    """
    receipt = read_json_file(luna_tools_dir() / 'steam-client-installed.json') or {}
    core = receipt.get('core') or {}
    if core:
        try:
            if all(sha256(steam_home() / _steam_payload_path(name)) == digest
                   for name, digest in core.items()):
                return str(receipt.get('build') or '')
        except (OSError, ValueError):
            pass
    try:
        manifest = (steam_home() / 'package' / 'steam_client_ubuntu12.manifest').read_text(
            encoding='utf-8', errors='replace')
        found = re.findall(r'"version"\s+"(\d+)"', manifest)
        manifest_build = next((build for build in reversed(found) if build != '0'), '')
        if manifest_build:
            return manifest_build
    except OSError:
        pass
    log = steam_home() / 'logs' / 'console_log.txt'
    try:
        size = log.stat().st_size
    except OSError:
        size = 0
    if size:
        try:
            with open(log, 'rb') as handle:
                handle.seek(max(0, size - 65536))
                tail = handle.read().decode('utf-8', errors='replace')
            found = [build for build in re.findall(r'Client version:\s*(\d+)', tail)
                     if build != '0']
            if found:
                return found[-1]
        except OSError:
            pass
    return ''


STEAM_CFG_BLOCK = ('BootStrapperInhibitAll=enable\n'
                     'BootStrapperForceSelfUpdate=disable\n')


# The hard lock (`luna steam lock`) also stops the launch-time update check and
# is written to both spellings of the file name that Steam's bootstrapper reads.
STEAM_HARD_CFG_BLOCK = ('BootStrapperInhibitAll=enable\n'
                        'BootStrapperInhibitUpdateOnLaunch=enable\n'
                        'BootStrapperForceSelfUpdate=false\n')
_STEAM_CFG_KEYS = re.compile(
    r'\s*BootStrapper(?:InhibitAll|InhibitUpdateOnLaunch|ForceSelfUpdate)\s*=', re.I)


def steam_cfg_path():
    return steam_home() / 'steam.cfg'


def steam_cfg_paths():
    """Both spellings the bootstrapper honors (distinct files on Linux)."""
    return [steam_home() / 'Steam.cfg', steam_home() / 'steam.cfg']


def steam_updates_blocked():
    try:
        content = steam_cfg_path().read_text()
    except OSError:
        return False
    return bool(re.search(r'^\s*BootStrapperInhibitAll\s*=\s*enable\s*$', content, re.M | re.I))


def ensure_steam_cfg_block(paths=None, block=None):
    """Write the bootstrapper inhibit block; Steam honors it on next launch.

    While a hard lock is recorded the hard block goes to both cfg files, so
    every code path that re-applies the block keeps the lock intact.
    """
    hard = steam_hard_lock_active()
    if block is None:
        block = STEAM_HARD_CFG_BLOCK if hard else STEAM_CFG_BLOCK
    if paths is None:
        paths = steam_cfg_paths() if hard else [steam_cfg_path()]
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            previous = path.read_text()
        except FileNotFoundError:
            previous = ''
        lines = [line for line in previous.splitlines() if not _STEAM_CFG_KEYS.match(line)]
        path.write_text('\n'.join(lines + block.splitlines()) + '\n')
    return paths[-1]


def remove_steam_cfg_block(paths=None):
    """Remove the block only if the file holds exactly what Luna wrote.

    Returns True when every file that held a Luna block was removed.
    """
    removed, kept = False, False
    for path in (paths if paths is not None else [steam_cfg_path()]):
        try:
            content = path.read_text()
        except OSError:
            continue
        if content in (STEAM_CFG_BLOCK, STEAM_HARD_CFG_BLOCK):
            try:
                path.unlink()
                removed = True
            except OSError:
                kept = True
        elif any(_STEAM_CFG_KEYS.match(line) for line in content.splitlines()):
            kept = True
    return removed and not kept


def pacman_conf_path():
    return Path(os.environ.get('LUNA_PACMAN_CONF', '/etc/pacman.conf'))


def pacman_steam_state():
    """Is the Steam client pacman-managed, and is it ignored on updates?

    No root needed: `pacman -Q` answers the first, the conf file the second.
    """
    managed, ignored = False, False
    try:
        result = subprocess.run(['pacman', '-Q', 'steam'],
                                capture_output=True, text=True, timeout=15)
        managed = result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return managed, ignored
    if not managed:
        return managed, ignored
    try:
        content = pacman_conf_path().read_text()
    except OSError:
        return managed, ignored
    for line in content.splitlines():
        stripped = line.split('#', 1)[0].strip()
        if not stripped or '=' not in stripped:
            continue
        key, _, value = stripped.partition('=')
        if key.strip().lower() == 'ignorepkg' and 'steam' in value.split():
            ignored = True
    return managed, ignored


def steam_pin_path():
    return luna_tools_dir() / 'steam-pin.json'


def steam_cache_dir():
    return luna_tools_dir() / 'steam-client-cache'


def read_steam_pin():
    pin = read_json_file(steam_pin_path())
    return pin if isinstance(pin, dict) else {}


def steam_hard_lock_active():
    """Is the ubuntu12 rollback + read-only package lock recorded in the pin?"""
    lock = read_steam_pin().get('hardLock')
    return isinstance(lock, dict) and bool(lock)


def new_steam_pin(build):
    """A pin record for `build`; carries an active hard lock forward."""
    pin = {'clientBuild': str(build), 'slssteam': slssteam_hashes(),
           'at': int(time.time()), 'by': installed_version()}
    lock = read_steam_pin().get('hardLock')
    if isinstance(lock, dict) and lock:
        pin['hardLock'] = lock
    return pin


@contextlib.contextmanager
def steam_package_writable():
    """Let Luna write into SDIR/package despite the hard lock's read-only bit.

    A no-op when the directory is writable and no hard lock is recorded;
    otherwise (Luna's hard lock, or a read-only bit left by a manual/community
    lock) it is made writable and put back to read-only afterwards, even if
    the work raises.
    """
    package = steam_home() / 'package'
    relock = package.is_dir() and (steam_hard_lock_active() or not os.access(package, os.W_OK))
    if relock:
        package.chmod(package.stat().st_mode | stat.S_IWUSR)
    try:
        yield
    finally:
        if relock and package.is_dir():
            package.chmod(package.stat().st_mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))


def slssteam_hashes():
    """Current hashes of the SLSsteam binaries (missing -> '')."""
    hashes = {}
    for name, path in steam_tools_files().items():
        try:
            hashes[name] = sha256(path) if path.is_file() else ''
        except OSError:
            hashes[name] = ''
    return hashes


def _package_stats(root, with_hash=False):
    """Fingerprint of a package tree: {rel: [size, mtime_ns]} (+ sha256)."""
    stats = {}
    for path in sorted(root.rglob('*')):
        if path.is_file():
            try:
                st = path.stat()
                entry = [st.st_size, st.st_mtime_ns]
                if with_hash:
                    entry.append(sha256(path))
                stats[str(path.relative_to(root))] = entry
            except OSError:
                pass
    return stats


def _cache_manifest():
    manifest = read_json_file(steam_cache_dir() / 'manifest.json')
    return manifest if isinstance(manifest, dict) else {}


def _stats_match(left, right):
    """Size+mtime comparison (hashes, when present, are ignored: fast path)."""
    if not set(left).issubset(right):
        return False
    return all(left[key][:2] == right[key][:2] for key in left)


def packages_match_cache():
    """Do the installed packages still match the cached build?

    None when there is no usable cache; otherwise a size+mtime comparison,
    which is instant and catches any client update or partial download.
    """
    manifest = _cache_manifest()
    if not manifest or not (steam_cache_dir() / 'package').is_dir():
        return None
    target = steam_home() / 'package'
    if not target.is_dir():
        return False
    return _stats_match(manifest, _package_stats(target))


def _package_read_only():
    try:
        return not (steam_home() / 'package').stat().st_mode & stat.S_IWUSR
    except OSError:
        return False


def steam_state():
    """Everything the menu and preflight report about the Steam client."""
    build = steam_client_build()
    pin = read_steam_pin()
    pinned = str(pin.get('clientBuild') or '')
    cache_marker = steam_cache_dir() / 'build.txt'
    try:
        cached = cache_marker.read_text().strip()
    except OSError:
        cached = ''
    cache_ready = bool(cached) and (steam_cache_dir() / 'package').is_dir()
    packages_match = packages_match_cache() if cache_ready else None
    receipt = read_json_file(luna_tools_dir() / 'steam-client-installed.json') or {}
    binaries_match = None
    if receipt.get('core'):
        try:
            binaries_match = all(sha256(steam_home() / _steam_payload_path(name)) == digest
                                 for name, digest in receipt['core'].items())
        except (OSError, ValueError):
            binaries_match = False
    if cache_ready:
        match = packages_match is True and binaries_match is not False and build == pinned
    else:
        match = bool(build and pinned and build == pinned)
    current_hashes = slssteam_hashes()
    pinned_hashes = pin.get('slssteam') or {}
    tools_drift = [name for name, digest in current_hashes.items()
                   if digest and pinned_hashes.get(name) and digest != pinned_hashes[name]]
    pacman_managed, pacman_ignored = pacman_steam_state()
    return {
        'build': build,
        'pinned': pinned,
        'match': match,
        'packagesMatch': packages_match,
        'binariesMatch': binaries_match,
        'updatesBlocked': steam_updates_blocked(),
        'slssteam': current_hashes,
        'slssteamDrift': tools_drift,
        'cachedBuild': cached,
        'cacheReady': cache_ready,
        'pacmanManaged': pacman_managed,
        'pacmanIgnored': pacman_ignored,
        'hardLock': bool(pin.get('hardLock')),
        'packageReadOnly': _package_read_only(),
    }


def cached_client_build():
    try:
        return (steam_cache_dir() / 'build.txt').read_text().strip()
    except OSError:
        return ''


def steam_pending_path():
    return luna_tools_dir() / 'pending-steam.json'


def read_steam_pending():
    pending = read_json_file(steam_pending_path())
    return pending if isinstance(pending, dict) else {}


def steam_sh_path():
    override = os.environ.get('LUNA_STEAM_SH')
    if override:
        return Path(override)
    return steam_home() / 'steam.sh'


def _steam_force_update():
    """Run the Steam bootstrapper's forced update and exit.

    Called only with Steam closed. Returns (ok, message). The update block
    is lifted for the run and re-applied afterwards either way.
    """
    if steam_hard_lock_active():
        return False, ('the Steam hard lock is active (package/ read-only, ubuntu12 rollback); '
                       'run: luna steam unlock --now to let Steam update')
    try:
        _steam_client_closed()
    except ValueError as error:
        return False, str(error)
    had_block = steam_updates_blocked()
    try:
        steam_cfg_path().unlink()
    except OSError:
        pass
    try:
        result = subprocess.run([str(steam_sh_path()), '-forcesteamupdate',
                                 '-forcepackagedownload', '-exitsteam'],
                                capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.SubprocessError, subprocess.TimeoutExpired) as error:
        result = None
        failure = str(error)
    if had_block:
        try:
            ensure_steam_cfg_block()
        except OSError:
            pass
    if result is None:
        return False, 'could not run the Steam updater: %s' % failure
    if result.returncode != 0:
        tail = (result.stderr.strip() or result.stdout.strip())[-500:]
        return False, 'Steam updater exited %d: %s' % (result.returncode, tail)
    # The bootstrapper relaunches Steam after a successful update even with
    # -exitsteam. Shut it back down gracefully: the caller asked for Steam to
    # stay closed. Never kill processes; report if it is still up.
    note = ('; block re-applied' if had_block
            else '; updates currently allowed - pin the new build to block them')
    relaunched = ''
    if process_running('steam') or process_running('steamwebhelper'):
        windows = gui_describe_blockers()  # best effort; '' when the GUI layer is unavailable
        relaunched = '; the updater relaunched Steam (%s)' % windows if windows else ''
        try:
            subprocess.run([str(steam_sh_path()), '-shutdown'],
                           capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError, subprocess.TimeoutExpired):
            pass
        deadline = time.monotonic() + 60
        while deadline > time.monotonic():
            if not (process_running('steam') or process_running('steamwebhelper')):
                break
            time.sleep(2)
        if process_running('steam') or process_running('steamwebhelper'):
            still = gui_describe_blockers()
            return False, ('Steam updated but is still running after a shutdown request; '
                           'close it completely (Steam -> Exit) before pinning or installing' + note
                           + (' [%s]' % still if still else ''))
        sidecar = gui_lumen_note()
        if sidecar:
            note += '; ' + sidecar
    return True, 'Steam client updated by Valve' + relaunched + note


def prune_steam_client_backups(keep=5):
    """Keep only the newest `keep` client rollback backups.

    Each successful client install/recovery retains a full rollback copy, so
    without pruning this directory grows without bound. Only directories whose
    names are nanosecond timestamps (written by this installer) are touched;
    anything else is left alone.
    """
    backups = luna_tools_dir() / 'steam-client-backups'
    try:
        entries = [entry for entry in backups.iterdir()
                   if entry.is_dir() and entry.name.isdigit()]
    except OSError:
        return 0
    entries.sort(key=lambda entry: int(entry.name))
    removed = 0
    for entry in entries[:-keep] if len(entries) > keep else []:
        try:
            shutil.rmtree(entry, ignore_errors=False)
            removed += 1
        except OSError:
            pass
    return removed


def _cache_client_packages(build):
    """Snapshot the installed package tree into the local cache.

    The cached manifest must describe `build`; a stale on-disk manifest (or a
    mislabeled earlier cache) is refused instead of silently relabeled.
    """
    source = steam_home() / 'package'
    cache = steam_cache_dir()
    try:
        installed_manifest = (source / STEAM_MANIFEST_NAME).read_text(
            encoding='utf-8', errors='replace')
    except OSError:
        raise ValueError('no Steam package manifest at %s' % (source / STEAM_MANIFEST_NAME))
    found = re.findall(r'"version"\s+"(\d+)"', installed_manifest)
    installed_build = next((item for item in reversed(found) if item != '0'), '')
    if installed_build != build:
        raise ValueError('installed packages describe build %s, not %s; refusing to mislabel the cache'
                         % (installed_build or 'unknown', build))
    if cache.exists():
        shutil.rmtree(cache, ignore_errors=True)
    shutil.copytree(source, cache / 'package', symlinks=False)
    # copytree preserves the hard lock's read-only bit; the cache must stay deletable.
    (cache / 'package').chmod((cache / 'package').stat().st_mode | stat.S_IWUSR)
    (cache / 'build.txt').write_text(build + '\n')
    write_json_file(cache / 'manifest.json', _package_stats(cache / 'package', with_hash=True))
    return cache


STEAM_COMPAT_BUILD = '1791918634'
STEAM_COMPAT_MANIFEST_SHA256 = 'a87699c36d28721af5ca6ade7276f506d22a11f9829747167d96845fc080afa5'
STEAM_ARCHIVE_BASE = ('https://web.archive.org/web/20261014104109if_/'
                      'http://media.steampowered.com/client/')
STEAM_MANIFEST_NAME = 'steam_client_ubuntu12.manifest'
STEAM_CORE_FILES = ('ubuntu12_32/steam', 'ubuntu12_32/steamclient.so',
                    'ubuntu12_64/steamwebhelper')


def _steam_payload_path(name):
    """Valve ZIPs use both slash styles. Never accept account/game/cache paths."""
    path = PurePosixPath(name.replace('\\', '/'))
    protected = {'config', 'userdata', 'steamapps', 'appcache', 'logs', 'depotcache', 'package'}
    if (path.is_absolute() or not path.parts or '..' in path.parts
            or ':' in str(path) or path.parts[0].startswith('.')
            or path.parts[0].lower() in protected):
        raise ValueError('unsafe/protected Steam payload path: %s' % name)
    return path


def _steam_package_entries(data, want):
    manifest = parse_vdf(data.decode('utf-8'))
    body = manifest.get('ubuntu12') or {}
    if body.get('version') != want:
        raise ValueError('Steam manifest is not build %s' % want)
    entries = []
    for values in body.values():
        if not isinstance(values, dict) or 'file' not in values:
            continue
        compressed = 'zipvz' in values
        name = values.get('zipvz') if compressed else values['file']
        digest = values.get('sha2vz' if compressed else 'sha2', '')
        if (not re.fullmatch(r'[A-Za-z0-9_.-]+', name or '')
                or not re.fullmatch(r'[0-9a-f]{64}', digest)):
            raise ValueError('unsafe package name or missing SHA256 in Steam manifest')
        entries.append((name, digest, compressed))
    if not entries:
        raise ValueError('Steam manifest has no verified packages')
    return entries


STEAM_DOWNLOAD_ATTEMPTS = 5
STEAM_RETRY_STATUS = (408, 425, 429, 500, 502, 503, 504)
STEAM_CACHE_HINT = ('copy the verified package files into a local directory and pass '
                    '--client-cache DIR (or set LUNA_STEAM_CACHE=DIR), e.g. from another '
                    "machine's ~/.local/share/Lumen/sls-luna/steam-client-cache/package")


def _retry_after_seconds(error):
    """Seconds from a Retry-After header (delta-seconds form), else None."""
    try:
        value = error.headers.get('Retry-After') if error.headers else None
        return max(0.0, min(float(value), 300.0)) if value is not None else None
    except (TypeError, ValueError):
        return None


def _format_mb(size):
    return '%.1f MB' % (size / 1e6)


class _Progress:
    """Throttled progress: carriage-return on a TTY, sparse plain lines otherwise."""

    def __init__(self, label, total=0):
        self.label, self.total = label, total
        self.tty = sys.stdout.isatty()
        self.last = 0.0
        self.step = 0

    def update(self, done, final=False):
        now = time.monotonic()
        if not final and now - self.last < (0.2 if self.tty else 5.0):
            return
        self.last = now
        text = ('%s of %s (%d%%)' % (_format_mb(done), _format_mb(self.total), 100 * done // self.total)
                if self.total else _format_mb(done))
        if self.tty:
            print('\r\033[Ksteam     : %s  %s' % (self.label, text), end='\n' if final else '', flush=True)
        else:
            print('steam     :   %s  %s' % (self.label, text), flush=True)


def _download_steam_file(url, destination, expected, limit=512 * 1024 * 1024,
                         label=None, total=0, attempts=None):
    """Download `url` to `destination`, verify sha256.

    Retries timeouts, connection errors and HTTP 408/425/429/5xx with
    exponential backoff + jitter (honouring Retry-After). A hash mismatch or
    other 4xx is final for this URL.
    """
    attempts = attempts or STEAM_DOWNLOAD_ATTEMPTS
    request = urllib.request.Request(url, headers={'User-Agent': 'Luna-Steam-Installer'})
    for attempt in range(1, attempts + 1):
        try:
            progress = _Progress(label, total) if label else None
            with urllib.request.urlopen(request, timeout=60) as source, open(destination, 'wb') as out:
                size = 0
                try:
                    header_size = int(source.headers.get('Content-Length') or 0)
                except (AttributeError, TypeError, ValueError):
                    header_size = 0
                if progress and header_size:
                    progress.total = header_size
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > limit:
                        raise ValueError('Steam download exceeds size limit')
                    out.write(chunk)
                    if progress:
                        progress.update(size)
            if progress:
                progress.update(size, final=True)
            break
        except urllib.error.HTTPError as error:
            if error.code not in STEAM_RETRY_STATUS or attempt == attempts:
                raise OSError('HTTP %s from %s' % (error.code, url)) from error
            delay = _retry_after_seconds(error)
            reason = 'HTTP %s' % error.code
        except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
            if attempt == attempts:
                raise OSError('%s: %s' % (url, error)) from error
            delay = None
            reason = str(error)
        if delay is None:
            delay = min(60.0, 2.0 ** attempt) * (0.5 + random.random() / 2)
        print('steam     : %s; retry %d/%d in %.0fs' % (reason, attempt, attempts - 1, delay), flush=True)
        time.sleep(delay)
    if sha256(destination) != expected:
        raise ValueError('Steam download SHA256 mismatch: %s' % url)


def _steam_package_sizes(data):
    """{package name: advertised size in bytes or 0} from the manifest."""
    body = parse_vdf(data.decode('utf-8')).get('ubuntu12') or {}
    sizes = {}
    for values in body.values():
        if isinstance(values, dict) and 'file' in values:
            compressed = 'zipvz' in values
            name = values.get('zipvz') if compressed else values['file']
            raw = str(values.get('sizevz' if compressed else 'size', ''))
            sizes[name] = int(raw) if raw.isdigit() else 0
    return sizes


def _stage_steam_payload(packages, data, want, destination):
    """Verify each package, decode VZip and extract ONLY official client files."""
    entries = {}
    total = 0
    destination.mkdir()
    for name, expected, compressed in _steam_package_entries(data, want):
        source = packages / name
        if source.is_symlink() or sha256(source) != expected:
            raise ValueError('Steam package SHA256 mismatch: %s' % name)
        raw = source.read_bytes()
        if compressed:
            if raw[:3] != b'VZa' or raw[-2:] != b'zv' or len(raw) < 22:
                raise ValueError('invalid Steam VZip header: %s' % name)
            prop = raw[7]
            dictionary = struct.unpack('<I', raw[8:12])[0]
            if prop >= 225 or dictionary > 128 * 1024 * 1024:
                raise ValueError('invalid Steam LZMA properties')
            filters = [{'id': lzma.FILTER_LZMA1, 'dict_size': dictionary,
                        'lc': prop % 9, 'lp': (prop // 9) % 5, 'pb': prop // 45}]
            crc, size = struct.unpack('<II', raw[-10:-2])
            if size > 1024 * 1024 * 1024:
                raise ValueError('Steam archive exceeds size limit')
            decoder = lzma.LZMADecompressor(format=lzma.FORMAT_RAW, filters=filters)
            raw = decoder.decompress(raw[12:-10], max_length=size + 1)
            if not decoder.eof or len(raw) != size or zlib.crc32(raw) != crc:
                raise ValueError('Steam VZip integrity check failed: %s' % name)
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            for item in archive.infolist():
                relative = _steam_payload_path(item.filename)
                target = destination / relative
                if not target.parent.resolve().is_relative_to(destination.resolve()):
                    raise ValueError('Steam archive traverses a symlink: %s' % relative)
                if item.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                total += item.file_size
                if item.file_size > 512 * 1024 * 1024 or total > 4 * 1024 * 1024 * 1024:
                    raise ValueError('Steam payload exceeds size limit')
                content = archive.read(item)  # also verifies ZIP CRC
                link = stat.S_ISLNK(item.external_attr >> 16)
                if link:
                    link_target = content.decode('utf-8')
                    if not (target.parent / link_target).resolve().is_relative_to(destination.resolve()):
                        raise ValueError('Steam symlink escapes payload: %s' % relative)
                info = {'sha256': hashlib.sha256(content).hexdigest(), 'link': link}
                key = str(relative)
                if key in entries:
                    if entries[key] != info:
                        raise ValueError('conflicting Steam archive entry: %s' % key)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                if link:
                    target.symlink_to(link_target)
                else:
                    target.write_bytes(content)
                    # Valve's ZIPs mark Linux executables as 0666, even ELF files.
                    target.chmod(0o755 if content.startswith((b'\x7fELF', b'#!')) else 0o644)
                    stamp = time.mktime((*item.date_time, 0, 0, -1))
                    os.utime(target, (stamp, stamp))
                entries[key] = info
    if not set(STEAM_CORE_FILES).issubset(entries):
        raise ValueError('Steam payload is missing core client executables')
    return entries


def _steam_client_closed():
    if any(process_running(name) for name in ('steam', 'steamwebhelper', 'lumen')):
        raise ValueError('Steam or the Lumen sidecar is running; close it completely first')


def _install_steam_payload(payload, entries, want, packages):
    """Install extracted binaries transactionally (package/ unlocked meanwhile)."""
    with steam_package_writable():
        return _install_steam_payload_locked(payload, entries, want, packages)


def _install_steam_payload_locked(payload, entries, want, packages):
    """Install extracted binaries transactionally; retain a machine-local backup."""
    _steam_client_closed()
    root = steam_home().resolve()
    backup = luna_tools_dir() / 'steam-client-backups' / str(time.time_ns())
    backup.mkdir(parents=True)
    changed = []
    targets = []
    for name, info in entries.items():
        target = root / _steam_payload_path(name)
        try:
            if target.is_symlink() != info['link']:
                raise OSError('different file type')
            content = os.readlink(target).encode() if info['link'] else target.read_bytes()
            same = hashlib.sha256(content).hexdigest() == info['sha256']
            source = payload / name
            if same and (info['link'] or stat.S_IMODE(target.stat().st_mode) == stat.S_IMODE(source.stat().st_mode)):
                continue
        except OSError:
            pass
        targets.append((target, payload / name))
    data = (packages / STEAM_MANIFEST_NAME).read_bytes()
    for name in [STEAM_MANIFEST_NAME] + [entry[0] for entry in _steam_package_entries(data, want)]:
        source, target = packages / name, root / 'package' / name
        # Preserve package stats so Health can cheaply compare the installed cache.
        if (not target.is_file() or target.is_symlink()
                or source.stat().st_size != target.stat().st_size
                or source.stat().st_mtime_ns != target.stat().st_mtime_ns
                or sha256(source) != sha256(target)):
            targets.append((target, source))
    # Save/update the receipt and cfg in the same transaction as the binaries.
    receipt = luna_tools_dir() / 'steam-client-installed.json'
    marker = root / '.steam-enable-steamrt64-client'
    saved = []
    try:
        for index, (target, _) in enumerate(targets + [(receipt, None), (steam_cfg_path(), None), (marker, None)]):
            if not target.parent.resolve().is_relative_to(root) and target != receipt:
                raise ValueError('Steam destination escapes its root: %s' % target)
            if target.exists() and not target.is_symlink() and not target.is_file():
                raise ValueError('Steam destination is not a regular file: %s' % target)
            previous = backup / str(index)
            exists = target.exists() or target.is_symlink()
            if exists:
                shutil.copy2(target, previous, follow_symlinks=False)
            saved.append((target, previous if exists else None))
        write_json_file(backup / 'restore.json', {
            'build': want, 'files': [{'path': str(path), 'backup': old.name if old else None}
                                    for path, old in saved]})
        _steam_client_closed()  # recheck after slow backup/extraction
        for target, source in targets:
            target.parent.mkdir(parents=True, exist_ok=True)
            temp = target.with_name(target.name + '.luna-new')
            if temp.exists() or temp.is_symlink():
                raise ValueError('Steam temporary destination already exists: %s' % temp)
            try:
                shutil.copy2(source, temp, follow_symlinks=False)
                os.replace(temp, target)
            finally:
                if temp.exists() or temp.is_symlink():
                    temp.unlink()
            changed.append(target)
        for name, info in entries.items():
            target = root / name
            content = os.readlink(target).encode() if info['link'] else target.read_bytes()
            if hashlib.sha256(content).hexdigest() != info['sha256']:
                raise ValueError('installed Steam client verification failed: %s' % name)
        for target in (receipt, steam_cfg_path(), marker):
            changed.append(target)
        if marker.exists() or marker.is_symlink():
            marker.unlink()
        ensure_steam_cfg_block()
        write_json_file(receipt, {'build': want, 'at': int(time.time()), 'backup': str(backup),
                                 'core': {name: entries[name]['sha256'] for name in STEAM_CORE_FILES}})
    except Exception:
        for target, previous in reversed(saved):
            if target not in changed:
                continue
            if target.exists() or target.is_symlink():
                target.unlink()
            if previous is not None:
                shutil.copy2(previous, target, follow_symlinks=False)
        raise
    return backup


def _steam_sources(extra_cache=None):
    """Directories searched (sha256-verified) before downloading, in order."""
    sources = []
    for raw in (extra_cache, os.environ.get('LUNA_STEAM_CACHE')):
        if raw:
            base = Path(raw).expanduser()
            sources.append(base / 'package' if (base / 'package').is_dir() else base)
    sources += [steam_home() / 'package', steam_cache_dir() / 'package']
    return sources


def _find_steam_package(sources, name, digest):
    for source in sources:
        candidate = source / name
        try:
            if candidate.is_file() and not candidate.is_symlink() and sha256(candidate) == digest:
                return candidate
        except OSError:
            continue
    return None


def steam_client_summary(extra_cache=None):
    """Read-only, offline description of what installing the client would fetch."""
    sources = _steam_sources(extra_cache)
    for source in sources:
        candidate = source / STEAM_MANIFEST_NAME
        try:
            if candidate.is_file() and sha256(candidate) == STEAM_COMPAT_MANIFEST_SHA256:
                data = candidate.read_bytes()
                break
        except OSError:
            continue
    else:
        return ('need the build %s manifest from Valve/archive.org (download size unknown until '
                'manifest download)' % STEAM_COMPAT_BUILD)
    sizes = _steam_package_sizes(data)
    need, reuse = [], 0
    for name, digest, _ in _steam_package_entries(data, STEAM_COMPAT_BUILD):
        if _find_steam_package(sources, name, digest):
            reuse += 1
        else:
            need.append(name)
    total = sum(sizes.get(name, 0) for name in need)
    return ('need %d file(s), %s to download, %d reused from cache'
            % (len(need) + reuse, _format_mb(total) if total or not need else 'size unknown', reuse))


def steam_client_needed(options):
    """(needed, reason). Install touches the Steam client only when this says so."""
    if getattr(options, 'keep_steam_client', False) or getattr(options, 'no_steam_client', False):
        return False, 'skipped by --no-steam-client / --keep-steam-client'
    if getattr(options, 'steam_client', False):
        return True, 'forced by --steam-client'
    pin = read_steam_pin()
    if not pin.get('clientBuild'):
        return True, 'no Steam client pin yet'
    if str(pin['clientBuild']) != STEAM_COMPAT_BUILD:
        return True, 'pin is build %s, Luna targets %s' % (pin['clientBuild'], STEAM_COMPAT_BUILD)
    state = steam_state()
    if state['build'] != state['pinned']:
        return True, 'Steam is build %s but the pin is %s' % (state['build'] or 'unknown', state['pinned'])
    if state['packagesMatch'] is False or state['binariesMatch'] is False:
        return True, 'Steam packages/executables drifted from the pin'
    if not getattr(options, 'only', None):
        return True, 'full install (verifies the pinned client; nothing is downloaded if it matches)'
    return False, 'pinned client build %s is intact; use --steam-client to force' % STEAM_COMPAT_BUILD


def ensure_compatible_steam(extra_cache=None):
    """Default install policy: fetch/verify the pinned Valve build, then extract it."""
    try:
        _steam_client_closed()
        if read_steam_pending():
            raise ValueError('a Steam task is queued; run: luna steam cancel before installing')
        if not (steam_home() / 'steam.sh').is_file():
            raise ValueError('Steam must be installed and opened once before installing Luna')
        tools = luna_tools_dir()
        tools.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='steam-client-stage-', dir=tools) as temporary:
            work = Path(temporary)
            packages = work / 'package'
            packages.mkdir()
            manifest = packages / STEAM_MANIFEST_NAME
            sources = _steam_sources(extra_cache)
            for source in sources:
                candidate = source / STEAM_MANIFEST_NAME
                if candidate.is_file() and sha256(candidate) == STEAM_COMPAT_MANIFEST_SHA256:
                    shutil.copy2(candidate, manifest)
                    break
            else:
                print('steam     : fetching the build %s manifest ...' % STEAM_COMPAT_BUILD, flush=True)
                _download_steam_file(STEAM_ARCHIVE_BASE + 'steam_client_ubuntu12', manifest,
                                     STEAM_COMPAT_MANIFEST_SHA256, limit=1024 * 1024)
            data = manifest.read_bytes()
            sizes = _steam_package_sizes(data)
            entries_list = _steam_package_entries(data, STEAM_COMPAT_BUILD)
            found = {name: _find_steam_package(sources, name, digest) for name, digest, _ in entries_list}
            todo = [name for name, _, _ in entries_list if not found[name]]
            print('steam     : need %d file(s), %s to download, %d reused from cache'
                  % (len(entries_list), _format_mb(sum(sizes.get(n, 0) for n in todo)),
                     len(entries_list) - len(todo)), flush=True)
            for index, (name, digest, _) in enumerate(entries_list, 1):
                destination = packages / name
                if found[name]:
                    shutil.copy2(found[name], destination)
                    continue
                label = '%s (%d/%d)' % (name, index, len(entries_list))
                print('steam     : downloading %s' % label, flush=True)
                try:
                    _download_steam_file('https://client-update.steamstatic.com/' + name, destination, digest,
                                         label=name, total=sizes.get(name, 0))
                except (OSError, ValueError) as first:
                    print('steam     : primary mirror failed (%s); trying the archive' % first, flush=True)
                    try:
                        _download_steam_file(STEAM_ARCHIVE_BASE + name, destination, digest,
                                             label=name, total=sizes.get(name, 0))
                    except (OSError, ValueError) as second:
                        raise ValueError('could not download %s: %s; %s. To avoid downloading, %s'
                                         % (name, first, second, STEAM_CACHE_HINT)) from second
            entries = _stage_steam_payload(packages, data, STEAM_COMPAT_BUILD, work / 'payload')
            # Keep a complete verified cache, not a copy of stale installed metadata.
            cache = steam_cache_dir()
            replacement = work / 'cache'
            prepared_stats = _package_stats(packages, with_hash=True)
            reuse_cache = (cached_client_build() == STEAM_COMPAT_BUILD
                           and _package_stats(cache / 'package', with_hash=True) == prepared_stats)
            if not reuse_cache:
                shutil.copytree(packages, replacement / 'package')
                (replacement / 'build.txt').write_text(STEAM_COMPAT_BUILD + '\n')
                write_json_file(replacement / 'manifest.json', prepared_stats)
            backup = _install_steam_payload(work / 'payload', entries, STEAM_COMPAT_BUILD, packages)
            if reuse_cache:
                write_json_file(cache / 'manifest.json', prepared_stats)
            else:
                if cache.exists():
                    os.replace(cache, backup / 'previous-cache')
                try:
                    os.replace(replacement, cache)
                except OSError:
                    if (backup / 'previous-cache').exists():
                        os.replace(backup / 'previous-cache', cache)
                    raise
            write_json_file(steam_pin_path(), new_steam_pin(STEAM_COMPAT_BUILD))
            pruned = prune_steam_client_backups()
        return True, ('installed and verified Steam client %s executables; updates blocked; backup: %s%s. '
                      'Runtime version will be confirmed on the next Steam launch.'
                      % (STEAM_COMPAT_BUILD, backup,
                         '; pruned %d old backup(s)' % pruned if pruned else ''))
    except (OSError, ValueError, lzma.LZMAError, zipfile.BadZipFile) as error:
        return False, 'Steam client installation failed (not reported as a downgrade): %s' % error


def _recover_steam_client(want):
    """Restore and verify actual cached Steam client executables for `want`.

    Returns (ok, message). Rechecks that Steam/Lumen are closed internally,
    verifies and extracts the packages, retains a rollback backup, and blocks
    self-updates. Copying package metadata alone can never report success.
    """
    if cached_client_build() != want or not (steam_cache_dir() / 'package').is_dir():
        return False, 'no cache for build %s; run: luna steam cache (Steam closed)' % want
    manifest = _cache_manifest()
    if manifest:
        cached_stats = {}
        for path in sorted((steam_cache_dir() / 'package').rglob('*')):
            if path.is_file():
                try:
                    cached_stats[str(path.relative_to(steam_cache_dir() / 'package'))] = True
                except OSError:
                    pass
        problems = [rel for rel, entry in manifest.items()
                    if not isinstance(entry, list) or len(entry) < 3]
        if problems or set(cached_stats) != set(manifest):
            return False, 'the cache is incomplete; re-run: luna steam cache (Steam closed)'
        for rel, entry in manifest.items():
            try:
                if sha256(steam_cache_dir() / 'package' / rel) != entry[2]:
                    return False, ('the cache failed its own integrity check (%s changed); '
                                     're-run: luna steam cache (Steam closed)' % rel)
            except OSError as error:
                return False, 'cannot read the cache: %s' % error
    # A package-only swap is not a recovery. Extract and verify the executables
    # first; never report success solely from build.txt or a copied manifest.
    try:
        _steam_client_closed()
        packages = steam_cache_dir() / 'package'
        data = (packages / STEAM_MANIFEST_NAME).read_bytes()
        if want == STEAM_COMPAT_BUILD and hashlib.sha256(data).hexdigest() != STEAM_COMPAT_MANIFEST_SHA256:
            raise ValueError('cached compatibility manifest SHA256 mismatch')
        with tempfile.TemporaryDirectory(prefix='steam-recover-', dir=luna_tools_dir()) as work:
            payload = Path(work) / 'payload'
            entries = _stage_steam_payload(packages, data, want, payload)
            backup = _install_steam_payload(payload, entries, want, packages)
    except (OSError, ValueError, lzma.LZMAError, zipfile.BadZipFile) as error:
        return False, 'could not recover Steam client executables: %s' % error
    pruned = prune_steam_client_backups()
    return True, ('recovered and verified Steam client %s executables; updates blocked; backup: %s%s'
                  % (want, backup, '; pruned %d old backup(s)' % pruned if pruned else ''))


def _apply_steam_update():
    """Valve-driven client update with the block lifted, then re-blocked.

    Called only with Steam closed (timer or --now). On success the local
    package cache is stale by definition and is dropped, so a later recover
    can never restore the pre-update build by accident.
    """
    ok, message = _steam_force_update()
    if not ok:
        return False, message
    try:
        shutil.rmtree(steam_cache_dir(), ignore_errors=True)
    except OSError:
        pass
    try:
        steam_pending_path().unlink()
    except OSError:
        pass
    build = steam_client_build()
    note = ('; cache is now stale and was dropped - re-pin after verifying, then re-cache'
            if not build else '; re-pin the new build %s after verifying, then re-cache' % build)
    return True, message + note


def steam_moon_state_dir():
    return home() / '.local/state/slsteam-moon'


def _find_steam_dir():
    """First Steam root that has a package/ dir, symlinks resolved."""
    candidates = [steam_home(), home() / '.local/share/Steam', home() / '.steam/steam',
                  home() / '.steam/root',
                  home() / '.var/app/com.valvesoftware.Steam/.local/share/Steam']
    for candidate in candidates:
        if (candidate / 'package').is_dir():
            return candidate.resolve()
    return None


def _steam_package_set_readonly(package, readonly):
    mode = package.stat().st_mode
    package.chmod(mode & ~0o222 if readonly else mode | stat.S_IWUSR)


def _rollback_steam_client(sdir, say):
    """Kill Steam, wipe the steamrt64 client packages and fetch the ubuntu12 client.

    Returns (ok, message). Never touches the cfg/pin; the caller locks only
    after this succeeded. The bootstrapper download is ~500 MB.
    """
    package = sdir / 'package'
    binary = sdir / 'ubuntu12_32' / 'steam'
    if not binary.is_file():
        binary = sdir / 'steam.sh'
    if not binary.is_file():
        return False, 'no Steam launcher found under %s' % sdir
    uid = str(os.getuid())
    say('stopping Steam')
    for name in ('steam', 'steamwebhelper'):
        subprocess.run(['pkill', '-9', '-u', uid, '-x', name], capture_output=True)
    time.sleep(2)
    _steam_package_set_readonly(package, False)
    for stale in package.glob('steam_client_steamrt64*'):
        stale.unlink()
    for stale in steam_moon_state_dir().glob('safe_mode*'):
        stale.unlink()
    say('downloading the ubuntu12 client (about 500 MB); this can take a while')
    # The Luna block would inhibit this very download; it is re-applied by the caller.
    try:
        subprocess.run([str(binary), '-forcemanifestsuffix', 'ubuntu12', '-exitsteam'],
                       timeout=3600)  # a non-zero exit is tolerated, as in the community script
    except (OSError, subprocess.SubprocessError) as error:
        return False, 'Steam bootstrapper failed: %s' % error
    return True, 'ubuntu12 client downloaded'


def _lock_steam_client(force, confirm):
    """`luna steam lock`: roll back to the ubuntu12 client and hard-lock updates."""
    sdir = _find_steam_dir()
    if sdir is None:
        return 1, 'no Steam installation with a package/ directory found'
    if sdir != steam_home().resolve():
        return 1, ('Steam found at %s, but Luna manages %s; refusing to lock a layout it '
                   'cannot pin' % (sdir, steam_home()))
    if read_steam_pending():
        return EXIT_REFUSED, 'a Steam task is queued; run: luna steam cancel first'
    running = [name for name in ('steam', 'steamwebhelper') if process_running(name)]
    if running and not force and not confirm():
        return EXIT_REFUSED, ('REFUSING: Steam is running and lock must kill it (kill -9, unsaved '
                              'state is lost). Close it, or re-run with --force.')

    def say(text):
        print('lock    : %s' % text, flush=True)

    # The bootstrapper must be free to download: lift Luna's block for the run.
    had_block = steam_updates_blocked() or steam_hard_lock_active()
    remove_steam_cfg_block(steam_cfg_paths())
    try:
        ok, message = _rollback_steam_client(sdir, say)
    except OSError as error:
        ok, message = False, 'rollback failed: %s' % error
    if not ok:
        if had_block:
            ensure_steam_cfg_block()
        return 1, message
    say(message)
    build = steam_client_build()
    if not build:
        if had_block:
            ensure_steam_cfg_block()
        return 1, ('rolled back, but the client build cannot be determined; launch Steam once '
                   'and re-run: luna steam lock')
    tools = luna_tools_dir()
    tools.mkdir(parents=True, exist_ok=True)
    # Pin first (carrying the lock marker) so every block/cfg helper agrees.
    pin = new_steam_pin(build)
    pin['hardLock'] = {'rollback': 'ubuntu12', 'at': int(time.time()), 'by': installed_version()}
    write_json_file(steam_pin_path(), pin)
    # The old receipt/cache describe the previous build and would read as drift.
    shutil.rmtree(steam_cache_dir(), ignore_errors=True)
    try:
        write_json_file(tools / 'steam-client-installed.json', {
            'build': build, 'at': int(time.time()), 'backup': '',
            'core': {name: sha256(sdir / _steam_payload_path(name)) for name in STEAM_CORE_FILES}})
    except (OSError, ValueError):
        try:
            (tools / 'steam-client-installed.json').unlink()
        except OSError:
            pass
    try:
        _cache_client_packages(build)
    except (OSError, ValueError) as error:
        say('warning: could not cache build %s (%s); run: luna steam cache' % (build, error))
    ensure_steam_cfg_block()
    _steam_package_set_readonly(sdir / 'package', True)
    if steam_moon_state_dir().is_dir():
        (steam_moon_state_dir() / 'boot_fail_count').write_text('0\n')
    return 0, ('Steam rolled back to the ubuntu12 client (build %s), pinned and hard-locked: '
               'Steam.cfg + steam.cfg written, package/ read-only. Undo: luna steam unlock' % build)


def adoptable_steam_lock():
    """Why a read-only package/ + pin can (or cannot) become Luna's hard lock. (ok, reason)

    Adoption only records the lock for a Steam that already is the pinned build and
    already has read-only package/ and the update block; it changes none of Steam's files.
    """
    if steam_hard_lock_active():
        return False, 'the hard lock is already recorded'
    pin = read_steam_pin()
    if not pin.get('clientBuild'):
        return False, 'no Steam pin recorded (run: luna steam pin)'
    package = steam_home() / 'package'
    if not package.is_dir():
        return False, 'no Steam package/ directory at %s' % package
    if not _package_read_only():
        return False, 'package/ is not read-only; nothing to adopt (use: luna steam lock)'
    if not steam_updates_blocked():
        return False, 'steam.cfg does not block updates (run: luna steam pin)'
    state = steam_state()
    if not state['match']:
        return False, ('Steam (build %s) does not match the pin (%s): run luna steam recover '
                       'first' % (state['build'] or '?', state['pinned']))
    return True, ''


def adopt_steam_lock():
    """`luna steam lock --adopt`: record hardLock for the pinned build as it stands.

    No rollback, no process killed, Steam's files untouched; only the pin record and
    Luna's own cfg block (both Steam.cfg and steam.cfg) are written. Undo: luna steam unlock.
    """
    ok, reason = adoptable_steam_lock()
    if not ok:
        return 1, 'cannot adopt the lock: %s' % reason
    pin = read_steam_pin()
    pin['hardLock'] = {'rollback': 'adopted', 'at': int(time.time()), 'by': installed_version()}
    write_json_file(steam_pin_path(), pin)
    ensure_steam_cfg_block()
    return 0, ('adopted the existing read-only Steam lock for build %s as Luna\'s hard lock '
               '(nothing rolled back or killed). Undo: luna steam unlock' % pin['clientBuild'])


def _notify(title, text):
    """Best-effort desktop notification; never fails the job."""
    try:
        subprocess.run(['notify-send', '-a', 'Luna', title, text], capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        pass


def _launch_steam_detached():
    try:
        subprocess.Popen(['setsid', 'steam'], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        return True
    except OSError as error:
        print('could not launch Steam: %s' % error, file=sys.stderr)
        return False


def _close_steam_gracefully(timeout=90):
    """`steam -shutdown`, then wait for the client to exit. True when it is gone."""
    if not process_running('steam'):
        return True
    try:
        subprocess.run(['steam', '-shutdown'], capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        pass
    for _ in range(timeout):
        if not process_running('steam'):
            sidecar = gui_lumen_note()
            if sidecar:
                print('note: %s' % sidecar, file=sys.stderr)
            return True
        time.sleep(1)
    return False


def _unlock_steam_client(now):
    """`luna steam unlock`: undo the hard lock and the pin."""
    package = steam_home() / 'package'
    if package.is_dir():
        _steam_package_set_readonly(package, False)
        print('package/ is writable again')
    if remove_steam_cfg_block(steam_cfg_paths()):
        print('update block removed: Steam may update itself again')
    elif any(path.exists() for path in steam_cfg_paths()):
        print('note: Steam.cfg/steam.cfg left in place (they hold more than the Luna block)')
    try:
        steam_pin_path().unlink()
        print('pin and hard-lock marker removed')
    except OSError:
        print('no pin to remove')
    if now:
        if process_running('steam'):
            print('Steam is already running; restart it to let it self-update')
        else:
            try:
                subprocess.Popen(['setsid', 'steam'], stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 start_new_session=True)
                print('Steam launched; it will self-update to the latest client')
            except OSError as error:
                print('could not launch Steam: %s' % error, file=sys.stderr)
                return 1
    return EXIT_OK


def refresh_pending_path():
    return luna_tools_dir() / 'pending-refresh.json'


def read_refresh_pending():
    pending = read_json_file(refresh_pending_path())
    return pending if isinstance(pending, dict) else {}


def _refresh_steam_side():
    """Update Steam to Valve's latest, then pin and cache the new build.

    Caller guarantees Steam and the sidecar are closed. Fails closed at each
    step with the exact next action; never kills processes and never mislabels
    the cache. Returns (ok, message, build).
    """
    try:
        _steam_client_closed()
    except ValueError as error:
        return False, str(error), ''
    if read_steam_pending():
        return False, 'a Steam task is already queued; run: luna steam cancel first', ''
    ok, message = _steam_force_update()
    if not ok:
        return False, message, ''
    build = steam_client_build()
    if not build:
        return False, ('Steam updated, but the new build cannot be determined; '
                       'launch Steam once, then run: luna steam pin [--build N]'), ''
    pin = new_steam_pin(build)
    try:
        luna_tools_dir().mkdir(parents=True, exist_ok=True)
        write_json_file(steam_pin_path(), pin)
        ensure_steam_cfg_block()
        _cache_client_packages(build)
    except (OSError, ValueError) as error:
        return False, 'Steam updated to %s, but pin/cache failed: %s' % (build, error), build
    return True, 'Steam client %s updated, pinned, cached and blocked' % build, build


