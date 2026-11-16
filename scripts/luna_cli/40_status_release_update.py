# --------------------------------------------------------------------------- commands

def cmd_preflight(options):
    print('SLS Luna %s' % (load_manifest().get('generated', '?')[:10]))
    print('lumen root : %s' % lumen_root())
    print('add-ons    : %s' % ', '.join('%s %s' % (name, addon_version(name)) for name in ADDONS))
    steam_root = home() / '.local/share/Steam'
    resolved = Path(os.path.realpath(home() / '.steam/steam')) if (home() / '.steam/steam').exists() else None
    print('steam root : %s' % (resolved if resolved else steam_root))
    manifests = 0
    plugdir = steam_root / 'config' / 'stplug-in'
    if plugdir.is_dir():
        manifests = sum(1 for entry in plugdir.glob('*.lua') if entry.stem.isdigit())
    print('manifests  : %d' % manifests)
    print('installed  : %s' % (', '.join(installed_addons()) or 'none'))
    print_component_inventory()
    boot = boot_file()
    if boot.is_file():
        content = boot.read_text(errors='replace')
        print('boot.lua   : %s, %d marker(s), backup %s'
              % ('patched' if MARKER in content else 'unpatched', content.count(MARKER),
                 'present' if boot.with_name('boot.lua.sls-addons.original').is_file() else 'absent'))
    check = patcher('check')
    print('anchors    : %s' % (check.stdout.strip() or check.stderr.strip() or 'no output'))
    data = data_dir()
    if data.is_dir():
        ledgers = sorted(p.name for p in (data / 'playtime').glob('*.json')) if (data / 'playtime').is_dir() else []
        print('data       : %s (playtime ledgers: %s; presence: %s)'
              % (data, ', '.join(ledgers) or 'none',
                 'present' if (data / 'presence.json').is_file() else 'absent'))
    else:
        print('data       : %s (absent - no playtime recorded yet)' % data)
    conflicts = [p for user in _steam_user_configs()
                 for p in (user / 'config').glob('localconfig.sync-conflict-*.vdf')]
    if conflicts:
        print('sync       : %d localconfig conflict(s) waiting - close Steam on both machines, '
              'let Syncthing settle, then run: luna reconcile' % len(conflicts))
    else:
        print('sync       : no localconfig conflicts')
    for peer in read_peers():
        ago = int(time.time() - int(peer.get('at') or 0))
        print('peer       : %s seen %dm ago, Steam %s, Luna %s%s'
              % (peer.get('machine', '?'), ago // 60,
                 'running - avoid Steam on both machines at once'
                 if peer.get('steamRunning') else 'closed',
                 peer.get('luna', '?'),
                 '' if peer.get('steamRunning') else ''))
    drift = steam_drift_note()
    if drift:
        print('steam      : %s' % drift)
    else:
        state = steam_state()
        print('steam      : client %s%s, updates %s'
              % (state['build'] or '?',
                 (', pinned %s' % state['pinned']) if state['pinned'] else ', not pinned',
                 'blocked' if state['updatesBlocked'] else 'allowed'))
    reasons = refuse_reasons(for_install=True)
    if reasons:
        print('\nREFUSALS (install/uninstall would stop):')
        for reason in reasons:
            print('  - %s' % reason)
        return EXIT_REFUSED
    print('\nREADY: nothing here was modified.')
    return EXIT_OK


def cmd_status(options):
    targets = selected_addons(options)
    manifest = load_manifest()
    installed = installed_addons()
    boot = boot_file()
    content = boot.read_text(errors='replace') if boot.is_file() else ''
    patched = MARKER in content
    print('boot.lua   : %s (marker x%d, backup %s)'
          % ('patched' if patched else 'unpatched', content.count(MARKER),
             'present' if boot.with_name('boot.lua.sls-addons.original').is_file() else 'absent'))
    print('processes  : steam=%s lumen=%s'
          % (process_running('steam') or process_running('steamwebhelper'), process_running('lumen')))
    print_component_inventory()
    rows = plan(targets)
    print('%-9s %-22s %s' % ('add-on', 'file', 'state'))
    degraded = False
    for addon, label, source, dest, state in rows:
        print('%-9s %-22s %s' % (addon, label, state))
        if state == 'differs' and addon in installed:
            degraded = True
        if state == 'missing-source':
            degraded = True
        if state == 'new' and addon in installed:
            # The directory claims the add-on is present but an expected
            # file is missing: that is corruption, not a fresh install.
            degraded = True
    for name in targets:
        if name not in installed:
            print('note       : %s is not installed' % name)
    if installed and not patched:
        print('note       : add-ons are installed but boot.lua is not patched; '
              'run scripts/install-local.sh')
        degraded = True
    print('data       : %s' % ('present' if data_dir().is_dir() else 'absent'))
    print('manifest   : %d add-on(s) pinned from %s'
          % (len(manifest.get('addons', {})), ', '.join(
              sorted({entry.get('source_repo', '?') for entry in manifest.get('addons', {}).values()}))))
    if getattr(options, 'live', False):
        # Read-only probe of the running client; see 96_live_probe.py.
        try:
            degraded = print_live_status(targets, installed) or degraded
        except Exception as error:  # the probe must never turn status into a traceback
            print('live       : unavailable - %s' % error)
    return 1 if degraded else EXIT_OK


def write_install_snapshot():
    """Record what this installer put on disk, for the Health page.

    Versions and sources identify the release; file hashes let the Health
    page report drift without a checkout. The full pins stay in
    vendor/MANIFEST.json (and in `luna verify`).
    """
    manifest = load_manifest()
    files = {}
    for name in ADDONS:
        entry = (manifest.get('addons', {}).get(name) or {}).get('files', {})
        hashes = {}
        for rel, meta in entry.items():
            path = payload_path(name, meta.get('source_path', ''))
            try:
                hashes[rel] = sha256(path) if path.is_file() else ''
            except OSError:
                hashes[rel] = ''
        files[name] = hashes
    snapshot = {
        'generated': manifest.get('generated', ''),
        'installedAt': int(time.time()),
        'source': repo_revision(),
        'versions': {name: addon_version(name) for name in ADDONS},
        'sources': {name: str((manifest.get('addons', {}).get(name) or {})
                              .get('source_commit', ''))[:12] for name in ADDONS},
        'files': files,
    }
    path = addons_dir() / 'luna-manifest.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(snapshot, indent=2, sort_keys=True) + '\n')
    return path


def repo_revision():
    try:
        result = subprocess.run(['git', '-C', str(ROOT), 'rev-parse', '--short', 'HEAD'],
                                capture_output=True, text=True)
        if result.returncode == 0:
            return result.stdout.strip()
    except OSError:
        pass
    return ''


def _manifest_problems(manifest, root):
    """Payload bytes vs manifest pins, for a checkout or an extracted bundle."""
    problems = []
    for name in ADDONS:
        entry = manifest.get('addons', {}).get(name)
        if not entry:
            problems.append('manifest has no entry for %s' % name)
            continue
        for rel, meta in entry.get('files', {}).items():
            path = root / 'addons' / name / meta['source_path']
            if not path.is_file():
                problems.append('missing payload %s/%s' % (name, rel))
                continue
            if sha256(path) != meta['sha256']:
                problems.append('payload %s/%s does not match the manifest' % (name, rel))
    for rel, meta in manifest.get('shared', {}).items():
        path = root / rel
        if not path.is_file():
            problems.append('missing shared file %s' % rel)
        elif sha256(path) != meta['sha256']:
            problems.append('shared file %s does not match the manifest' % rel)
    return problems


RELEASE_LISTING = 'RELEASE-FILES.json'
RELEASE_FORMAT = 2


def _release_listing(entries):
    """{relative path: sha256} for every regular file the bundle will carry."""
    listing = {}
    for arcname, path in entries:
        files = sorted(p for p in path.rglob('*')) if path.is_dir() else [path]
        for item in files:
            if item.is_symlink():
                raise ValueError('refusing to release a symlink: %s' % item)
            if item.is_file():
                rel = arcname if not path.is_dir() else '%s/%s' % (arcname, item.relative_to(path).as_posix())
                listing[rel] = sha256(item)
    return listing


def cmd_release(options):
    """Build a bundle another machine can install with `luna update`."""
    manifest = load_manifest()  # fail early when the pins are missing
    problems = _manifest_problems(manifest, ROOT)
    if problems:
        print('REFUSING to release: the working tree does not match the manifest:')
        for problem in problems:
            print('  - %s' % problem)
        print('Run scripts/vendor-update.sh, then verify, before releasing.', file=sys.stderr)
        return 1
    warn_untraceable(manifest, sys.stderr)
    version = addon_version('luna')
    out_dir = Path(options.out).expanduser() if options.out else ROOT / 'dist'
    out_dir.mkdir(parents=True, exist_ok=True)
    name = 'luna-%s' % version
    bundle = out_dir / (name + '.tar.gz')
    entries = [
        ('addons', ROOT / 'addons'),
        ('vendor/MANIFEST.json', MANIFEST_PATH),
        ('scripts/luna.py', ROOT / 'scripts' / 'luna.py'),
        ('scripts/lumen-hook.py', ROOT / 'scripts' / 'lumen-hook.py'),
        ('scripts/steam-machine.py', ROOT / 'scripts' / 'steam-machine.py'),
        ('scripts/patch-lumen.py', PATCHER),
        ('bridge/bridge.lua', BRIDGE_SOURCE),
    ]
    readme = ROOT / 'README.md'
    if readme.is_file():
        entries.append(('README.md', readme))
    changelog = ROOT / 'CHANGELOG.md'
    if changelog.is_file():
        entries.append(('CHANGELOG.md', changelog))
    listing = _release_listing(entries)
    listing_bytes = (json.dumps({'format': RELEASE_FORMAT, 'files': listing},
                                indent=1, sort_keys=True) + '\n').encode()
    with tarfile.open(bundle, 'w:gz') as tar:
        for arcname, path in entries:
            tar.add(path, arcname='%s/%s' % (name, arcname), recursive=path.is_dir())
        info = tarfile.TarInfo('%s/%s' % (name, RELEASE_LISTING))
        info.size = len(listing_bytes)
        info.mode = 0o644
        info.mtime = int(time.time())
        tar.addfile(info, io.BytesIO(listing_bytes))
    digest = sha256(bundle)
    checksum = out_dir / (bundle.name + '.sha256')
    checksum.write_text('%s  %s\n' % (digest, bundle.name))
    print('bundle : %s' % bundle)
    print('sha256 : %s' % digest)
    sig_file = Path(str(bundle) + '.sig')
    if os.path.lexists(str(sig_file)):
        sig_file.unlink()  # a stale signature of an earlier build of this version must not linger
    signed = False
    if signing_key_path().is_file():
        fingerprint, sign_error = signing_sign(bundle)
        if sign_error:
            print('REFUSING to release: the bundle could not be signed: %s' % sign_error, file=sys.stderr)
            return 1
        signed = True
        print('signed : %s (%s)' % (fingerprint, sig_file.name))
    else:
        print('signed : NO (no release key at %s; the bundle is unsigned. Create one: luna signing init)'
              % signing_key_path())
    print('version: %s (%s)' % (version, ', '.join('%s %s' % (n, addon_version(n)) for n in ADDONS)))
    if options.publish:
        gh = shutil.which('gh')
        if not gh:
            print('gh CLI not found; publish manually: %s' % bundle, file=sys.stderr)
            return 1
        tag = 'v%s' % version
        view = subprocess.run([gh, 'release', 'view', tag], capture_output=True, text=True)
        assets = [str(bundle), str(checksum)] + ([str(sig_file)] if signed else [])
        if view.returncode == 0:
            args = [gh, 'release', 'upload', tag] + assets + ['--clobber']
        else:
            args = [gh, 'release', 'create', tag] + assets + [
                    '--title', 'Luna %s' % version,
                    '--notes', ('Luna %s: pinned payloads plus scripts/luna.py.\n\n'
                                'Install on another machine: `luna update --latest` (needs gh).'
                                % version)]
        result = subprocess.run(args, capture_output=True, text=True)
        if result.returncode != 0:
            print('gh failed: %s' % (result.stderr.strip() or result.stdout.strip()), file=sys.stderr)
            return 1
        print('published: %s' % tag)
    return EXIT_OK


def _fetch_small(url, dest, limit):
    """Download at most `limit` bytes to `dest`; on any failure or overflow leave nothing (or garbage
    of limit+1 bytes, which signing_read_sig refuses as oversized)."""
    try:
        with urllib.request.urlopen(url, timeout=60) as response:
            data = response.read(limit + 1)
    except Exception:  # noqa: BLE001 - absent or unreachable counts as "no file"
        return False
    Path(dest).write_bytes(data)
    return True


def cmd_update(options):
    """Verify a released bundle and install it with the bundled script."""
    lock_path = _update_lock()
    acquired, reason = _acquire_lock(lock_path)
    if not acquired:
        print('REFUSING: %s' % reason, file=sys.stderr)
        return 1
    temp_dir = Path(tempfile.mkdtemp(prefix='luna-update-'))
    try:
        bundle = None
        if options.bundle:
            bundle = Path(options.bundle).expanduser()
        elif options.latest:
            tag, tag_error = latest_release()
            if not tag:
                print('gh release lookup failed: %s' % tag_error, file=sys.stderr)
                return 1
            bundle, error = download_release(tag, temp_dir)
            if bundle is None:
                print('gh release download failed: %s' % error, file=sys.stderr)
                return 1
        elif options.url:
            bundle = temp_dir / 'luna-bundle.tar.gz'
            try:
                urllib.request.urlretrieve(options.url, bundle)
            except Exception as error:  # noqa: BLE001 - report the URL failure
                print('download failed: %s' % error, file=sys.stderr)
                return 1
            try:
                urllib.request.urlretrieve(options.url + '.sha256', str(bundle) + '.sha256')
            except Exception:  # noqa: BLE001 - the sidecar is optional for --url
                pass
            if not Path(str(bundle) + '.sha256').is_file():
                print('warning: no .sha256 beside the URL; only the bundle\'s own manifest can be '
                      'checked (self-consistency, not independent). Prefer --latest.', file=sys.stderr)
            # A missing, blocked or failed .sig is indistinguishable from "unsigned": the policy
            # in check_bundle_signature then refuses it wherever a trusted key is configured.
            _fetch_small(options.url + '.sig', str(bundle) + '.sig', SIG_MAX_BYTES)
        else:
            print('Point at a bundle: --bundle FILE, --url URL or --latest', file=sys.stderr)
            return 2
        return install_bundle(bundle, only=options.only, dry_run=options.dry_run,
                              allow_legacy=getattr(options, 'allow_legacy_bundle', False),
                              allow_unsigned=getattr(options, 'allow_unsigned', False))
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
        _release_lock(lock_path)


LUNA_REPO = 'pgm1207/Luna'
UPDATE_TTL = 6 * 3600


def gh_json(args):
    gh = find_gh()
    if not gh:
        return None, 'the gh CLI is not installed (update checks use your own GitHub auth)'
    try:
        result = subprocess.run([gh] + args, capture_output=True, text=True, timeout=GH_TIMEOUT)
    except subprocess.TimeoutExpired:
        return None, 'gh did not answer within %d seconds' % GH_TIMEOUT
    if result.returncode != 0:
        return None, (result.stderr.strip() or result.stdout.strip() or 'gh failed')
    try:
        return json.loads(result.stdout), ''
    except ValueError:
        return None, 'gh returned something that is not JSON'


def latest_release():
    data, error = gh_json(['release', 'view', '--json', 'tagName,assets', '-R', LUNA_REPO])
    if data is None:
        return '', error
    return str(data.get('tagName') or ''), ''


def check_updates(force=False):
    """Compare the installed version with the newest release; cached for 6h."""
    cache_path = luna_tools_dir() / 'update-check.json'
    cached = read_json_file(cache_path) or {}
    now = int(time.time())
    installed = installed_version()
    if (not force and cached.get('latest') and cached.get('checkedAt') and not cached.get('checkError')
            and now - int(cached['checkedAt']) < UPDATE_TTL):
        cached['installed'] = installed
        cached['available'] = version_key(cached.get('latest')) > version_key(installed)
        return cached
    latest, error = latest_release()
    if not latest and error:
        # A failed lookup keeps the last known release, so an offline check
        # does not hide an update that was already found.
        latest = str(cached.get('latest') or '')
    report = {
        'installed': installed,
        'latest': latest,
        'checkedAt': now,
        'checkError': error,
        'available': bool(latest) and version_key(latest) > version_key(installed),
    }
    write_json_file(cache_path, report)
    return report


def _safe_extract(bundle, extract_dir):
    """Unpack without allowing absolute paths or parent-directory escapes.

    A gzip bundle is first read to its end: tarfile stops at the tar end marker and never
    reaches the gzip CRC trailer, so a damaged stream that still decodes would otherwise
    extract silently. Members must be plain files or directories (no symlinks, hardlinks or
    devices) and names must be unique, so a later member cannot replace a verified one."""
    with open(str(bundle), 'rb') as probe:
        gzipped = probe.read(2) == b'\x1f\x8b'
    if gzipped:
        try:
            with gzip.open(str(bundle), 'rb') as stream:
                while stream.read(1024 * 1024):
                    pass
        except (OSError, EOFError, zlib.error) as error:
            raise ValueError('the bundle is corrupt (gzip stream check failed: %s)' % error) from error
    with tarfile.open(bundle) as tar:
        members = tar.getmembers()
        seen = set()
        for member in members:
            name = member.name
            if not name or name.startswith('/') or '..' in Path(name).parts:
                raise ValueError('REFUSING: unsafe path in bundle: %r' % name)
            if not (member.isfile() or member.isdir()):
                raise ValueError('REFUSING: bundle member %r is a link or special file' % name)
            key = Path(name).as_posix().rstrip('/')
            if key in seen:
                raise ValueError('REFUSING: duplicate bundle member %r' % name)
            seen.add(key)
        try:
            tar.extractall(extract_dir, filter='data')
        except TypeError:  # Python < 3.12 has no extraction filter
            tar.extractall(extract_dir)


def _listing_problems(root):
    """Check an extracted bundle against RELEASE-FILES.json, in both directions.

    Runs in the already-installed code before anything from the bundle executes. Returns
    (problems, listing_present)."""
    path = root / RELEASE_LISTING
    if not path.is_file():
        return [], False
    try:
        data = json.loads(path.read_text())
        files = data['files']
        if data.get('format') != RELEASE_FORMAT or not isinstance(files, dict):
            raise ValueError('unsupported format')
    except (ValueError, KeyError, OSError, AttributeError) as error:
        return ['%s is unreadable: %s' % (RELEASE_LISTING, error)], True
    problems = []
    for rel, digest in sorted(files.items()):
        if rel.startswith('/') or '..' in Path(rel).parts:
            problems.append('listed path is unsafe: %r' % rel)
            continue
        item = root / rel
        if item.is_symlink() or not item.is_file():
            problems.append('listed file is missing: %s' % rel)
        elif sha256(item) != digest:
            problems.append('%s does not match %s' % (rel, RELEASE_LISTING))
    for item in sorted(root.rglob('*')):
        rel = item.relative_to(root).as_posix()
        if item.is_symlink() or (item.is_file() and rel != RELEASE_LISTING and rel not in files):
            problems.append('unlisted file in the bundle: %s' % rel)
    return problems, True


def install_bundle(bundle, only=None, dry_run=False, allow_legacy=False, allow_unsigned=False):
    """Verify a bundle and run the installer inside it.

    The bundle is first copied into a private directory, and the checksum, the
    signature and the extraction all use that one copy, so the bytes that were
    verified are the bytes that get unpacked."""
    bundle = Path(bundle)
    if not bundle.is_file():
        print('bundle not found: %s' % bundle, file=sys.stderr)
        return 2
    with tempfile.TemporaryDirectory(prefix='luna-stage-') as stage:
        staged = Path(stage) / 'bundle.tar.gz'
        try:
            shutil.copyfile(str(bundle), str(staged))
        except OSError as error:
            print('REFUSING: cannot read the bundle: %s' % error, file=sys.stderr)
            return 1
        return _install_staged(bundle, staged, only, dry_run, allow_legacy, allow_unsigned)


def _install_staged(bundle, staged, only, dry_run, allow_legacy, allow_unsigned):
    checksum_file = Path(str(bundle) + '.sha256')
    if checksum_file.is_file():
        expected = checksum_file.read_text().split()[0].strip().lower()
        actual = sha256(staged)
        if expected != actual:
            print('REFUSING: sha256 mismatch (bundle %s, sidecar %s)'
                  % (actual[:12], expected[:12]), file=sys.stderr)
            return 1
        print('sha256 : verified (%s)' % actual[:12])
    else:
        print('note   : no .sha256 beside the bundle: integrity is checked only against the bundle\'s '
              'own manifest (self-consistency), not an independent checksum')
    # Authenticity: decided by the INSTALLED code and the machine-local trust file, before unpacking.
    if not check_bundle_signature(staged, Path(str(bundle) + '.sig'), allow_unsigned):
        return 1
    with tempfile.TemporaryDirectory(prefix='luna-bundle-') as work:
        extract_dir = Path(work) / 'unpacked'
        extract_dir.mkdir()
        try:
            _safe_extract(staged, extract_dir)
        except (tarfile.TarError, ValueError, OSError) as error:
            print('REFUSING: cannot safely unpack the bundle: %s' % error, file=sys.stderr)
            return 1
        roots = [entry for entry in extract_dir.iterdir() if entry.is_dir()]
        root = roots[0] if len(roots) == 1 else extract_dir
        script = root / 'scripts' / 'luna.py'
        if not script.is_file():
            print('REFUSING: the bundle does not contain scripts/luna.py', file=sys.stderr)
            return 1
        version = '?'
        bundled_manifest = root / 'vendor' / 'MANIFEST.json'
        if not bundled_manifest.is_file():
            print('REFUSING: the bundle does not contain vendor/MANIFEST.json', file=sys.stderr)
            return 1
        try:
            manifest = json.loads(bundled_manifest.read_text())
            version = str((manifest.get('addons', {}).get('luna') or {})
                          .get('plugin_version', '?'))
        except ValueError:
            print('REFUSING: the bundled manifest is not valid JSON', file=sys.stderr)
            return 1
        problems = _manifest_problems(manifest, root)
        if problems:
            print('REFUSING: the bundle contents do not match its own manifest:')
            for problem in problems:
                print('  - %s' % problem, file=sys.stderr)
            return 1
        listing_problems, listed = _listing_problems(root)
        if listing_problems:
            print('REFUSING: the bundle contents do not match its file list:', file=sys.stderr)
            for problem in listing_problems:
                print('  - %s' % problem, file=sys.stderr)
            return 1
        if not listed:
            # Old format: scripts/luna.py (which runs next) is not covered by any hash.
            if not allow_legacy:
                print('REFUSING: this bundle predates %s, so scripts/luna.py, which would run next, '
                      'is not verified. If you trust where it came from, repeat with '
                      '--allow-legacy-bundle.' % RELEASE_LISTING, file=sys.stderr)
                return 1
            print('WARNING: legacy bundle: scripts/luna.py is NOT covered by any hash; '
                  'continuing because --allow-legacy-bundle was given', file=sys.stderr)
        print('bundle : %s (Luna %s, manifest verified%s)'
              % (bundle.name, version, '' if listed else ', legacy: luna.py unverified'))
        # Add-on updates must not undo a deliberate client update from Health.
        command = [sys.executable, str(script), 'install', '--force']
        for name in only or []:
            command += ['--only', name]
        if dry_run:
            command.append('--dry-run')
        result = subprocess.run(command, env=dict(os.environ, LUNA_KEEP_STEAM_CLIENT='1'))
        return result.returncode


def download_release(tag, work):
    """Fetch a release's bundle + checksum with the user's gh auth."""
    gh = find_gh()
    if not gh:
        return None, 'the gh CLI is not installed'
    if not tag:
        return None, 'no release tag to download'
    try:
        result = subprocess.run([gh, 'release', 'download', tag,
                                 '--pattern', 'luna-*.tar.gz',
                                 '--pattern', 'luna-*.tar.gz.sha256',
                                 '--pattern', 'luna-*.tar.gz.sig',
                                 '--dir', str(work), '--clobber', '-R', LUNA_REPO],
                                capture_output=True, text=True, timeout=GH_DOWNLOAD_TIMEOUT)
    except subprocess.TimeoutExpired:
        return None, 'the release download did not finish within %d seconds' % GH_DOWNLOAD_TIMEOUT
    if result.returncode != 0:
        return None, (result.stderr.strip() or result.stdout.strip() or 'download failed')
    found = sorted(work.glob('luna-*.tar.gz'))
    if not found:
        return None, 'the release has no bundle asset'
    bundle = found[-1]
    if not Path(str(bundle) + '.sha256').is_file():
        return None, 'the release has no checksum sidecar; refusing an unauthenticated bundle'
    return bundle, ''


def _update_lock():
    """Single-writer lock so timer, menu and shell never update concurrently."""
    tools = luna_tools_dir()
    try:
        tools.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return tools / 'update.lock'


LOCK_TTL = 3600


def _acquire_lock(lock_path):
    """Claim the update lock; clear it when stale (dead owner or too old).

    Returns (True, '') on success or (False, reason). A kill -9 between
    acquire and release must never wedge updates forever.
    """
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        pass
    except OSError:
        return True, ''
    else:
        try:
            os.write(fd, json.dumps(
                {'pid': os.getpid(), 'at': int(time.time())}).encode())
        finally:
            os.close(fd)
        return True, ''
    try:
        info = json.loads(lock_path.read_text())
        owner, started = int(info.get('pid') or 0), int(info.get('at') or 0)
    except (OSError, ValueError, TypeError, AttributeError):
        owner, started = 0, 0
    alive = False
    if owner > 0:
        try:
            os.kill(owner, 0)
            alive = True
        except (OSError, ProcessLookupError, PermissionError):
            alive = False
    if alive and time.time() - started < LOCK_TTL:
        return False, 'another update is already running (%s)' % lock_path
    try:
        lock_path.unlink()
    except OSError:
        pass
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except (FileExistsError, OSError):
        return False, 'another update is already running (%s)' % lock_path
    try:
        os.write(fd, json.dumps({'pid': os.getpid(), 'at': int(time.time())}).encode())
    finally:
        os.close(fd)
    return True, ''


def _release_lock(lock_path):
    try:
        lock_path.unlink()
    except OSError:
        pass


CLI_LAUNCHER = '''#!/bin/sh
# Managed by SLS Luna CLI v1
# Recovery remains available even if a base update replaces Lumen's tooling.
if [ "${1:-}" = hook ] && [ -f "$HOME/.local/share/slssteam-luna/lumen-hook.py" ]; then
    shift
    action="${1:-status}"
    case "$action" in on) action=enable;; off) action=disable;; esac
    [ "$#" -eq 0 ] || shift
    exec python3 "$HOME/.local/share/slssteam-luna/lumen-hook.py" "$action" "$@"
fi
exec python3 "$HOME/.local/share/Lumen/sls-luna/luna.py" "$@"
'''


def write_tooling():
    """Keep a standalone updater copy the timer can run without a checkout.

    Called both by the installer (from a checkout) and by the copy itself, so
    the source is *this* file, never a fixed path relative to it.
    """
    tools = luna_tools_dir()
    tools.mkdir(parents=True, exist_ok=True)
    target = tools / 'luna.py'
    source = Path(__file__).resolve()
    try:
        same = source.samefile(target)
    except OSError:
        same = False
    if not same:
        shutil.copy2(source, target)
        target.chmod(0o755)
    launcher = home() / '.local/bin/luna'
    if launcher.is_symlink() or (launcher.exists() and
                                (not launcher.is_file() or launcher.read_bytes() != CLI_LAUNCHER.encode())):
        print('WARNING: custom luna command preserved; use the installed Python tooling directly.', file=sys.stderr)
    elif not launcher.exists():
        launcher.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(launcher, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o755)
        except FileExistsError:
            print('WARNING: luna command appeared concurrently; it was left untouched.', file=sys.stderr)
        else:
            with os.fdopen(fd, 'wb') as out:
                out.write(CLI_LAUNCHER.encode())
    return target


def unit_paths():
    unit_dir = home() / '.config/systemd/user'
    return unit_dir, unit_dir / 'luna-autoupdate.service', unit_dir / 'luna-autoupdate.timer'


