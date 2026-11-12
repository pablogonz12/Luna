# --------------------------------------------------------------------------- refusals

def refuse_reasons(for_install):
    """Environment problems that must stop an install or uninstall."""
    reasons = []
    root = lumen_root()
    if not (root / 'lua' / 'boot.lua').is_file():
        reasons.append('Lumen lua/boot.lua not found under %s (is the LuaTools-Moon stack installed?)' % root)
    if not (root / 'luatools' / 'backend' / 'main.lua').is_file():
        reasons.append('LuaTools-Moon backend not found under %s' % root)
    if process_running('steam') or process_running('steamwebhelper'):
        reasons.append('Steam is running. Close it completely (Steam -> Exit) before changing Lumen.')
    # A sidecar from an earlier session keeps the old boot.lua in memory, which
    # makes a successful install look ineffective. Never kill it for the user.
    if process_running('lumen'):
        reasons.append('A Lumen sidecar is still running (pid owner: %s). Stop it first:\n'
                       '    pkill -u $(id -u) -x lumen' % os.getuid())
    return reasons


def patcher(mode, *extra):
    args = [sys.executable, str(PATCHER), mode, '--lumen', str(lumen_root())]
    return subprocess.run(args + list(extra), capture_output=True, text=True)


# --------------------------------------------------------------------------- planning

def plan(targets):
    """What would change on disk: (addon, label, source, dest, state)."""
    rows = []
    bridge = addons_dir() / 'bridge.lua'
    rows.append(('shared', 'bridge.lua', BRIDGE_SOURCE, bridge, file_state(BRIDGE_SOURCE, bridge)))
    for name in targets:
        for source_rel, dest_rel in ADDON_FILES[name]:
            source = payload_path(name, source_rel)
            dest = addons_dir() / name / dest_rel
            rows.append((name, dest_rel, source, dest, file_state(source, dest)))
    return rows


def file_state(source, dest):
    if not source.is_file():
        return 'missing-source'
    if not dest.is_file():
        return 'new'
    if sha256(source) == sha256(dest):
        return 'same'
    return 'differs'


def selected_addons(options):
    if options.only:
        for name in options.only:
            if name not in ADDONS:
                raise SystemExit('FAIL: unknown add-on %r (known: %s)' % (name, ', '.join(ADDONS)))
        return tuple(name for name in ADDONS if name in options.only)
    return tuple(ADDONS)


def installed_addons():
    return tuple(name for name in ADDONS if (addons_dir() / name).is_dir())


def component_inventory():
    """Read-only inventory of the base stack and optional external tools."""
    home_dir = home()
    root = lumen_root()
    moon_wrapper = home_dir / '.local/share/SLSsteam/path/steam'
    lumen_boot = root / 'lua' / 'boot.lua'
    luatools_backend = root / 'luatools' / 'backend' / 'main.lua'

    cloud_dir = home_dir / '.local/share/CloudRedirect'
    cloud_files = (cloud_dir / 'cloud_redirect.so', cloud_dir / 'cloud_redirect_cli')
    cloud_present = tuple(path for path in cloud_files if path.is_file())
    if len(cloud_present) == len(cloud_files):
        cloud_state = 'present'
    elif cloud_present:
        cloud_state = 'partial'
    else:
        cloud_state = 'not detected'
    cloud_detail = ', '.join(str(path) for path in cloud_present) or str(cloud_dir)

    decky_services = home_dir / 'homebrew' / 'services'
    decky_loader = decky_services / 'PluginLoader'
    decky_version = decky_services / '.loader.version'
    if decky_loader.is_file() and os.access(decky_loader, os.X_OK):
        decky_state = 'present'
        try:
            version = decky_version.read_text().strip() if decky_version.is_file() else ''
        except OSError:
            version = ''
        decky_detail = '%s%s (service state not checked)' % (
            decky_loader, (' (version ' + version + ')') if version else '')
    elif decky_services.exists():
        decky_state = 'partial'
        decky_detail = str(decky_services)
    else:
        decky_state = 'not detected'
        decky_detail = str(decky_services)

    compressor_candidates = []
    configured = os.environ.get('SLS_COMPRESSOR_BIN')
    if configured:
        compressor_candidates.append(Path(configured).expanduser())
    compressor_candidates.extend((
        home_dir / '.local/bin/btrfs-game-compressor',
        Path('/usr/local/bin/btrfs-game-compressor'),
        Path('/usr/bin/btrfs-game-compressor'),
        home_dir / 'Documents/Scripts/btrfs-game-compressor/btrfs-game-compressor',
    ))
    compressor = next((path for path in compressor_candidates
                       if path.is_file() and os.access(path, os.X_OK)), None)

    return (
        ('slsteam-moon wrapper', 'present' if moon_wrapper.is_file() else 'not detected', str(moon_wrapper)),
        ('Lumen runtime', 'present' if lumen_boot.is_file() else 'not detected', str(lumen_boot)),
        ('LuaTools backend', 'present' if luatools_backend.is_file() else 'not detected', str(luatools_backend)),
        ('CloudRedirect', cloud_state, cloud_detail),
        ('Decky Loader', decky_state, decky_detail),
        ('Compressor CLI', 'present' if compressor else 'not detected', str(compressor or compressor_candidates[0])),
    )


def print_component_inventory():
    print('components:')
    for name, state, detail in component_inventory():
        print('  %-20s %-12s %s' % (name, state, detail))


# --------------------------------------------------------------------------- apply

class Apply:
    """Write files, remembering how to put the previous state back."""

    def __init__(self):
        self.undo = []
        self.did_patch = False

    def write(self, source, dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.is_file():
            self.undo.append(('restore', dest, dest.read_bytes()))
        else:
            self.undo.append(('remove', dest, None))
        tmp = dest.with_name(dest.name + '.luna-tmp')
        data = source.read_bytes()
        tmp.write_bytes(data)
        os.chmod(tmp, 0o644)
        os.replace(tmp, dest)

    def mkdir(self, path):
        if not path.is_dir():
            path.mkdir(parents=True, exist_ok=True)
            self.undo.append(('rmdir', path, None))

    def rollback(self):
        for action, target, payload in reversed(self.undo):
            try:
                if action == 'restore':
                    target.write_bytes(payload)
                elif action == 'remove':
                    if target.exists():
                        target.unlink()
                elif action == 'rmdir':
                    shutil.rmtree(target, ignore_errors=True)
            except OSError as error:
                print('  WARN: rollback could not restore %s: %s' % (target, error), file=sys.stderr)
        if self.did_patch:
            result = patcher('uninstall')
            if result.returncode != 0:
                print('  WARN: could not revert the boot.lua patch: %s'
                      % (result.stderr.strip() or result.stdout.strip()), file=sys.stderr)


