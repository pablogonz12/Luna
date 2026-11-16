

# --------------------------------------------------------------------------- release signing
# `luna release` signs the bundle with a dedicated ed25519 key (OpenSSH
# signatures, `ssh-keygen -Y sign`); `luna update` verifies the detached
# `<bundle>.sig` against the machine-local trust file BEFORE anything is
# unpacked or run. The trust file (release-signers) is local configuration: it
# is never shipped in a bundle and never written by install/update. Only
# `luna signing init|trust|untrust` change it. Nothing here touches ~/.ssh.

SIGN_NAMESPACE = 'luna-release'
SIGN_PRINCIPAL = 'luna-release'
SIGN_KEY_NAME = 'luna-release-ed25519'
SIGN_SIGNERS_NAME = 'release-signers'
SIG_MAX_BYTES = 8192            # a real ed25519 signature is ~400 bytes
SIGNERS_MAX_BYTES = 65536
# ssh-ed25519 blob = string "ssh-ed25519" + string(32 bytes) = 51 bytes = 68 base64 chars.
SIGN_BLOB_PREFIX = b'\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20'
SIGN_KEY_RE = re.compile(r'^ssh-ed25519 ([A-Za-z0-9+/]{68})(?:[ \t]+\S.*)?$')
SIGN_SIGNERS_RE = re.compile(r'^%s namespaces="%s" ssh-ed25519 ([A-Za-z0-9+/]{68})(?:[ \t]+\S.*)?$'
                             % (SIGN_PRINCIPAL, SIGN_NAMESPACE))


def signing_dir():
    override = os.environ.get('LUNA_SIGNING_DIR')
    if override:
        return Path(override).expanduser()
    base = os.environ.get('XDG_CONFIG_HOME') or str(home() / '.config')
    return Path(base) / 'luna'


def signing_key_path():
    return signing_dir() / SIGN_KEY_NAME


def signing_pub_path():
    return signing_dir() / (SIGN_KEY_NAME + '.pub')


def signers_path():
    return signing_dir() / SIGN_SIGNERS_NAME


def signing_blob_valid(blob):
    try:
        raw = base64.b64decode(blob, validate=True)
    except ValueError:
        return False
    return len(raw) == 51 and raw.startswith(SIGN_BLOB_PREFIX)


def signing_fingerprint(blob):
    digest = hashlib.sha256(base64.b64decode(blob)).digest()
    return 'SHA256:' + base64.b64encode(digest).decode().rstrip('=')


def signing_parse_pubkey(text):
    """The base64 blob of the single ed25519 public key in `text`, or (None, why)."""
    blobs = []
    for line in str(text).splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        match = SIGN_KEY_RE.match(line)
        if not match or not signing_blob_valid(match.group(1)):
            return None, 'not an ssh-ed25519 public key line: %.60r' % line
        if match.group(1) not in blobs:
            blobs.append(match.group(1))
    if len(blobs) != 1:
        return None, 'expected exactly one ssh-ed25519 public key, found %d' % len(blobs)
    return blobs[0], ''


def signers_load():
    """(trusted blobs, unrecognised line count, error). Fails closed on an unreadable file."""
    path = signers_path()
    try:
        with open(str(path), 'rb') as handle:
            data = handle.read(SIGNERS_MAX_BYTES + 1)
    except FileNotFoundError:
        return [], 0, ''
    except OSError as error:
        return [], 0, 'cannot read %s: %s' % (path, error)
    if len(data) > SIGNERS_MAX_BYTES:
        return [], 0, '%s is implausibly large' % path
    keys, junk = [], 0
    for line in data.decode('utf-8', 'replace').splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        match = SIGN_SIGNERS_RE.match(line)
        if match and signing_blob_valid(match.group(1)):
            if match.group(1) not in keys:
                keys.append(match.group(1))
        else:
            junk += 1  # never trusted: only our own canonical line shape counts
    if junk and not keys:
        return [], junk, '%s has content but no valid key line; refusing to guess' % path
    return keys, junk, ''


def signers_text(keys):
    lines = ['# Luna release trust anchor (machine-local; managed by `luna signing`).',
             '# Only these keys may sign bundles accepted by `luna update`.']
    lines += ['%s namespaces="%s" ssh-ed25519 %s' % (SIGN_PRINCIPAL, SIGN_NAMESPACE, blob) for blob in keys]
    return '\n'.join(lines) + '\n'


def signers_save(keys):
    path = signers_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.' + path.name + '.', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w') as handle:
            handle.write(signers_text(keys))
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def signing_read_sig(sig_path):
    """('' | 'absent' | 'bad', bytes, reason) for the detached signature beside a bundle."""
    try:
        info = os.lstat(str(sig_path))
    except FileNotFoundError:
        return 'absent', b'', ''
    except OSError as error:
        return 'bad', b'', 'cannot stat the signature: %s' % error
    if not stat.S_ISREG(info.st_mode):
        return 'bad', b'', 'the signature is not a regular file (symlink or special file)'
    if info.st_size > SIG_MAX_BYTES:
        return 'bad', b'', 'the signature file is implausibly large (%d bytes)' % info.st_size
    try:
        fd = os.open(str(sig_path), os.O_RDONLY | os.O_NOFOLLOW | getattr(os, 'O_NONBLOCK', 0))
        with os.fdopen(fd, 'rb') as handle:
            data = handle.read(SIG_MAX_BYTES + 1)
    except OSError as error:
        return 'bad', b'', 'cannot read the signature: %s' % error
    if len(data) > SIG_MAX_BYTES:
        return 'bad', b'', 'the signature file is implausibly large'
    return 'present', data, ''


def signing_verify(bundle, sig_bytes, keys):
    """Verify `sig_bytes` over the bundle against `keys`. Returns (fingerprint, error)."""
    keygen = shutil.which('ssh-keygen')
    if not keygen:
        return '', 'ssh-keygen is not installed'
    with tempfile.TemporaryDirectory(prefix='luna-sig-') as work:
        sig_file = Path(work) / 'bundle.sig'
        sig_file.write_bytes(sig_bytes)
        signers = Path(work) / 'signers'
        signers.write_text(signers_text(keys))  # rebuilt from validated keys only
        try:
            with open(str(bundle), 'rb') as handle:
                result = subprocess.run([keygen, '-Y', 'verify', '-f', str(signers), '-I', SIGN_PRINCIPAL,
                                         '-n', SIGN_NAMESPACE, '-s', str(sig_file)],
                                        stdin=handle, capture_output=True, text=True, timeout=120)
        except (OSError, subprocess.TimeoutExpired) as error:
            return '', 'ssh-keygen could not run: %s' % error
    if result.returncode != 0:
        detail = (result.stderr.strip() or result.stdout.strip() or 'verification failed').splitlines()[-1]
        return '', detail
    match = re.search(r'\b(SHA256:[A-Za-z0-9+/]+)', result.stdout)
    trusted = {signing_fingerprint(blob) for blob in keys}
    if not match or match.group(1) not in trusted:
        return '', 'the signing key is not one of the trusted keys'
    return match.group(1), ''


def signing_sign(bundle):
    """Sign the bundle with the release key. Returns (fingerprint, error)."""
    key, pub = signing_key_path(), signing_pub_path()
    keygen = shutil.which('ssh-keygen')
    if not keygen:
        return '', 'ssh-keygen is not installed'
    blob, why = signing_parse_pubkey(pub.read_text() if pub.is_file() else '')
    if blob is None:
        return '', 'the public key %s is missing or unreadable (%s)' % (pub, why)
    sig = Path(str(bundle) + '.sig')
    with contextlib.suppress(OSError):
        sig.unlink()
    try:
        result = subprocess.run([keygen, '-Y', 'sign', '-f', str(key), '-n', SIGN_NAMESPACE, str(bundle)],
                                capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as error:
        return '', 'ssh-keygen could not run: %s' % error
    if result.returncode != 0 or not sig.is_file():
        return '', (result.stderr.strip() or 'signing failed').splitlines()[-1]
    # Self-check against the matching public key so a bad signature is never published.
    fingerprint, error = signing_verify(bundle, sig.read_bytes(), [blob])
    if error:
        with contextlib.suppress(OSError):
            sig.unlink()
        return '', 'the fresh signature did not verify: %s' % error
    return fingerprint, ''


def check_bundle_signature(staged, sig_path, allow_unsigned):
    """Authenticity gate, run before anything is unpacked. True means: go on."""
    keys, _junk, load_error = signers_load()
    state, sig_bytes, reason = signing_read_sig(sig_path)
    enforcing = bool(keys) or bool(load_error)
    if load_error:
        print('REFUSING: the trust file is unusable: %s' % load_error, file=sys.stderr)
        return False
    if state == 'bad':
        print('REFUSING: the bundle has a signature file that cannot be used (%s). '
              'This is refused even with --allow-unsigned.' % reason, file=sys.stderr)
        return False
    if state == 'absent':
        if not enforcing:
            print('note   : authenticity NOT checked: the bundle is unsigned and no trusted release key is '
                  'configured on this machine. To enable it: luna signing trust PUBKEYFILE '
                  '(the release machine creates one with `luna signing init`)')
            return True
        if allow_unsigned:
            print('WARNING: the bundle is UNSIGNED; continuing because --allow-unsigned was given',
                  file=sys.stderr)
            return True
        print('REFUSING: the bundle has no signature (.sig) but this machine trusts release key(s) '
              '(%s). If you accept an unsigned bundle, repeat with --allow-unsigned.'
              % ', '.join(signing_fingerprint(k) for k in keys), file=sys.stderr)
        return False
    # A signature is present.
    if not keys:
        print('note   : the bundle is signed, but authenticity is NOT checked: no trusted release key is '
              'configured here. Enable it with: luna signing trust PUBKEYFILE')
        return True
    if not shutil.which('ssh-keygen'):
        if allow_unsigned:
            print('WARNING: ssh-keygen is missing, so the signature cannot be verified; continuing because '
                  '--allow-unsigned was given', file=sys.stderr)
            return True
        print('REFUSING: this machine trusts release keys but ssh-keygen is not installed, so the '
              'signature cannot be verified. Install openssh, or repeat with --allow-unsigned.',
              file=sys.stderr)
        return False
    fingerprint, error = signing_verify(staged, sig_bytes, keys)
    if error:
        print('REFUSING: the bundle signature is not valid for a trusted key (%s). '
              'This is refused even with --allow-unsigned.' % error, file=sys.stderr)
        return False
    print('signed : verified, signer %s (namespace %s)' % (fingerprint, SIGN_NAMESPACE))
    return True


def _signing_read_key_arg(arg):
    if arg == '-':
        return sys.stdin.read(SIGNERS_MAX_BYTES)
    candidate = Path(arg).expanduser()
    try:
        if candidate.is_file():
            if candidate.stat().st_size > SIGNERS_MAX_BYTES:
                return ''
            return candidate.read_text()
    except OSError:
        return ''
    return arg


def cmd_signing(options):
    action = options.signing_action
    arg = options.key
    keygen = shutil.which('ssh-keygen')
    if action == 'status':
        keys, junk, error = signers_load()
        key, pub = signing_key_path(), signing_pub_path()
        print('signing dir    : %s' % signing_dir())
        print('ssh-keygen     : %s' % (keygen or 'MISSING (cannot sign or verify)'))
        blob, _ = signing_parse_pubkey(pub.read_text() if pub.is_file() else '')
        if key.is_file() and blob:
            mode = stat.S_IMODE(key.stat().st_mode)
            print('signing key    : present, %s' % signing_fingerprint(blob))
            print('  private key  : %s (mode %o%s)' % (key, mode, '' if not mode & 0o077 else ' - TOO OPEN, chmod 600'))
            print('  public key   : %s' % pub)
        else:
            print('signing key    : none here (luna release writes unsigned bundles; create one: luna signing init)')
        print('trust file     : %s' % signers_path())
        if error:
            print('  PROBLEM      : %s' % error)
        for item in keys:
            print('  trusted      : %s%s' % (signing_fingerprint(item), '  (this machine\'s own key)' if item == blob else ''))
        if junk and keys:
            print('  ignored      : %d line(s) in the trust file are not valid key lines' % junk)
        if error:
            print('policy         : REFUSING every update until the trust file is fixed')
        elif keys:
            print('policy         : ENFORCING: unsigned or wrongly signed bundles are refused '
                  '(--allow-unsigned overrides only the missing-signature case)')
        else:
            print('policy         : not enforcing: unsigned bundles are accepted (authenticity unchecked); '
                  'luna signing trust PUBKEYFILE turns it on')
        return EXIT_OK
    if action == 'init':
        key, pub = signing_key_path(), signing_pub_path()
        if os.path.lexists(str(key)) or os.path.lexists(str(pub)):
            print('REFUSING: a signing key already exists (%s); it is never overwritten' % key, file=sys.stderr)
            return EXIT_REFUSED
        if not keygen:
            print('REFUSING: ssh-keygen is not installed', file=sys.stderr)
            return EXIT_REFUSED
        signing_dir().mkdir(parents=True, exist_ok=True, mode=0o700)
        result = subprocess.run([keygen, '-q', '-t', 'ed25519', '-N', '', '-C', SIGN_PRINCIPAL, '-f', str(key)],
                                capture_output=True, text=True)
        if result.returncode != 0 or not key.is_file() or not pub.is_file():
            print('ssh-keygen failed: %s' % (result.stderr.strip() or result.returncode), file=sys.stderr)
            return 1
        os.chmod(key, 0o600)
        blob, why = signing_parse_pubkey(pub.read_text())
        if blob is None:
            print('the new public key is unreadable: %s' % why, file=sys.stderr)
            return 1
        keys, _junk, error = signers_load()
        if error:
            print('key created, but the trust file is unusable: %s' % error, file=sys.stderr)
            return 1
        if blob not in keys:
            signers_save(keys + [blob])
        print('created  : %s (private, mode 600, no passphrase)' % key)
        print('public   : %s' % pub)
        print('trusted  : %s (added to %s)' % (signing_fingerprint(blob), signers_path()))
        print('next     : copy the public key to other machines and run: luna signing trust %s' % pub.name)
        return EXIT_OK
    if action in ('trust', 'untrust'):
        if not arg:
            print('usage: luna signing %s PUBKEY_OR_FILE%s' % (action, ' | SHA256:FINGERPRINT' if action == 'untrust' else ''),
                  file=sys.stderr)
            return EXIT_REFUSED
        keys, _junk, error = signers_load()
        if error:
            print('REFUSING: the trust file is unusable: %s (fix or remove it by hand)' % error, file=sys.stderr)
            return EXIT_REFUSED
        if action == 'untrust' and arg.startswith('SHA256:'):
            matches = [k for k in keys if signing_fingerprint(k) == arg]
            if not matches:
                print('no trusted key with fingerprint %s' % arg, file=sys.stderr)
                return 1
            blob = matches[0]
        else:
            blob, why = signing_parse_pubkey(_signing_read_key_arg(arg))
            if blob is None:
                print('REFUSING: %s' % why, file=sys.stderr)
                return EXIT_REFUSED
        if action == 'trust':
            if blob in keys:
                print('already trusted: %s' % signing_fingerprint(blob))
                return EXIT_OK
            signers_save(keys + [blob])
            print('trusted  : %s (%s)' % (signing_fingerprint(blob), signers_path()))
            print('from now on `luna update` here refuses bundles without a valid signature from a trusted key')
            return EXIT_OK
        if blob not in keys:
            print('not trusted: %s' % signing_fingerprint(blob), file=sys.stderr)
            return 1
        remaining = [k for k in keys if k != blob]
        signers_save(remaining)
        print('untrusted: %s' % signing_fingerprint(blob))
        if not remaining:
            print('note     : no trusted keys left: unsigned bundles are accepted again (authenticity unchecked)')
        return EXIT_OK
    return EXIT_REFUSED
