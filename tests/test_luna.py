#!/usr/bin/env python3
"""SLS Luna installer tests.

Everything runs against a disposable HOME with a minimal Lumen fixture, so no
real Lumen, Steam or user data is touched. `pgrep` is stubbed so the tests never
depend on whether the developer's own Steam is open.
"""
import hashlib
import io
import json
import os
os.environ.setdefault('LUNA_GUI_CHECKS', '0')  # hermetic: tests never probe the real desktop (test_gui_check manages it itself)
os.environ.setdefault('LUNA_LIVE_CHECKS', '0')  # hermetic: tests never contact a real Steam (test_live_probe uses a fake endpoint)
import tempfile as _tempfile
# hermetic: the signing-enforcement anchor lives under the passwd home by default; tests never touch it.
os.environ.setdefault('LUNA_ANCHOR_HOME', _tempfile.mkdtemp(prefix='luna-anchor-default-'))
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
LUNA = HERE / 'scripts' / 'luna.py'

# The upstream shape the patcher accepts: four anchors, once each.
UPSTREAM = '''local registry = {}
local lua_dir = os.getenv("LUMEN_LUA_DIR") or "lua"
local function build_menu_assets()
 local js = {}
 return { polyfill = true, css = {}, js = js }
end
local function build_webview_assets()
 local base = build_menu_assets()
 return base
end

local loop = require("loop")
loop.run({
 channels = {
    { titles = { ["SharedJSContext"] = true }, control = true,
      assets = { polyfill = nil, css = {},
        js = {
          gamepad_toast_js,
          auto_fix_launch_guard_js,
        } } },
  }
})
'''


def stub_bin(home, running=''):
    """A pgrep that reports exactly the processes named in `running`."""
    directory = home / 'test-bin'
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / 'pgrep'
    # Mirrors pgrep: exit 0 and a pid when the name matches, exit 1 otherwise.
    script.write_text('#!/bin/sh\nname="$4"\ncase ",%s," in *",$name,"*) echo 4242; exit 0;; esac\nexit 1\n' % running)
    script.chmod(0o755)
    # pacman: tests must not see the developer's real system packages.
    pacman = directory / 'pacman'
    pacman.write_text('#!/bin/sh\nexit 1\n')
    pacman.chmod(0o755)
    # gh: what the updater uses. `release view` reports $LUNA_TEST_TAG and
    # `release download` copies $LUNA_TEST_BUNDLE to the requested --dir.
    gh = directory / 'gh'
    gh.write_text('''#!/bin/sh
[ -z "${LUNA_TEST_GHLOG:-}" ] || echo "$*" >> "$LUNA_TEST_GHLOG"
case "$1 $2" in
  "release view")
    tag="${LUNA_TEST_TAG:-v9.9.9}"
    printf '{"tagName":"%s","assets":[]}\\n' "$tag"
    ;;
  "release download")
    dir=""
    while [ $# -gt 0 ]; do
      case "$1" in --dir) dir="$2"; shift 2 ;; *) shift ;; esac
    done
    [ -n "$dir" ] || exit 3
    [ -n "${LUNA_TEST_BUNDLE:-}" ] || exit 4
    cp "$LUNA_TEST_BUNDLE" "$dir/luna-test.tar.gz" || exit 5
    [ -f "$LUNA_TEST_BUNDLE.sha256" ] && cp "$LUNA_TEST_BUNDLE.sha256" "$dir/luna-test.tar.gz.sha256"
    [ -f "$LUNA_TEST_BUNDLE.sig" ] && cp "$LUNA_TEST_BUNDLE.sig" "$dir/luna-test.tar.gz.sig"
    exit 0
    ;;
  *) exit 0 ;;
esac
''')
    gh.chmod(0o755)
    # systemctl: the timer tests only need the calls to succeed.
    systemctl = directory / 'systemctl'
    systemctl.write_text('#!/bin/sh\ncase "$2" in is-enabled) echo enabled;; is-active) echo active;; esac\nexit 0\n')
    systemctl.chmod(0o755)
    niri = directory / 'niri'
    niri.write_text('#!/bin/sh\nexit 1\n')
    niri.chmod(0o755)
    return directory


class LunaTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp(prefix='luna-test-'))
        self.home = self.temp / 'home'
        lumen = self.home / '.local/share/Lumen'
        (lumen / 'lua').mkdir(parents=True)
        (lumen / 'luatools/backend').mkdir(parents=True)
        (lumen / 'luatools/backend/main.lua').write_text('-- present')
        self.boot = lumen / 'lua/boot.lua'
        self.boot.write_text(UPSTREAM)
        self.addons = lumen / 'sls-addons'
        self.data = self.home / '.local/share/slssteam-integrations'
        self.data.mkdir(parents=True)
        (self.data / 'presence.json').write_text('{"version":1,"owned":{}}')
        (self.data / 'playtime').mkdir()
        (self.data / 'playtime/76561198000000042.json').write_text('{"version":2,"games":{}}')
        self.bin = stub_bin(self.home)
        self.env = dict(os.environ, HOME=str(self.home),
                        XDG_DATA_HOME=str(self.home / '.local/share'),
                        XDG_RUNTIME_DIR=str(self.home / 'run'),
                        XDG_CONFIG_HOME=str(self.home / '.config'),
                        LUNA_SIGNING_DIR=str(self.home / '.config/luna'),  # never the real signing key
                        LUNA_ANCHOR_HOME=str(self.temp / 'anchor'),  # never the real enforcement anchor
                        PATH=str(self.bin) + os.pathsep + os.environ.get('PATH', ''))

    def tearDown(self):
        shutil.rmtree(self.temp, ignore_errors=True)

    def tree_copy(self):
        """A throwaway copy of what verify/release read; tests never write inside the checkout."""
        tree = self.temp / 'tree'
        if not tree.exists():
            (tree / 'scripts').mkdir(parents=True)
            shutil.copy2(LUNA, tree / 'scripts' / 'luna.py')
            for rel in ('addons', 'vendor', 'bridge'):
                shutil.copytree(HERE / rel, tree / rel, ignore=shutil.ignore_patterns('__pycache__'))
        return tree

    def luna_in(self, tree, *args):
        stub_bin(self.home, '')
        return subprocess.run([sys.executable, str(tree / 'scripts' / 'luna.py')] + list(args),
                              env=self.env, capture_output=True, text=True)

    def luna(self, *args, running='', env=None, keep_steam_client=True):
        stub_bin(self.home, running)
        environment = dict(env or self.env)
        command = [sys.executable, str(LUNA)] + list(args)
        # These are add-on fixtures without Valve payloads. The default client
        # installation has its own synthetic-package integration tests.
        if args and args[0] == 'install' and keep_steam_client:
            command.append('--keep-steam-client')
        return subprocess.run(command,
                              capture_output=True, text=True, env=environment)

    # -- helpers ---------------------------------------------------------
    def installed(self, name, rel):
        return (self.addons / name / rel).is_file()

    def data_snapshot(self):
        return {str(path.relative_to(self.data)): path.read_bytes()
                for path in sorted(self.data.rglob('*')) if path.is_file()}

    def steam_fixture(self, build):
        steam = self.home / '.local/share/Steam'
        package = steam / 'package'
        package.mkdir(parents=True, exist_ok=True)
        archive = package / 'test-client.zip'
        with zipfile.ZipFile(archive, 'w') as out:
            for name in ('ubuntu12_32/steam', 'ubuntu12_32/steamclient.so', 'ubuntu12_64/steamwebhelper'):
                out.writestr(name, b'\x7fELFsynthetic-build-' + build.encode())
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        (package / 'steam_client_ubuntu12.manifest').write_text(
            '"ubuntu12" { "version" "%s" "client" { "file" "test-client.zip" "sha2" "%s" } }'
            % (build, digest))
        return package

    # -- install ---------------------------------------------------------
    def test_install_hints_at_the_gui_check_on_a_terminal_only(self):
        import pty

        def read_all(fd):
            chunks = []
            try:
                while True:
                    data = os.read(fd, 4096)
                    if not data:
                        break
                    chunks.append(data)
            except OSError:  # EIO once the slave side is closed and drained
                pass
            return b''.join(chunks).decode(errors='replace')
        stub_bin(self.home)
        agent = self.bin / 'agent-desktop'
        agent.write_text('#!/bin/sh\nexit 1\n')
        agent.chmod(0o755)
        env = dict(self.env, LUNA_GUI_CHECKS='1', WAYLAND_DISPLAY='wayland-9')
        command = [sys.executable, str(LUNA), 'install', '--keep-steam-client']

        def install(environment, terminal):
            """Run a fresh install; returns its stdout as the user would see it."""
            if not terminal:
                done = subprocess.run(command, capture_output=True, text=True, env=environment)
                self.assertEqual(done.returncode, 0, done.stderr + done.stdout)
                return done.stdout
            master, slave = pty.openpty()
            try:
                done = subprocess.run(command, stdout=slave, stderr=subprocess.PIPE, env=environment)
                os.close(slave)
                text = read_all(master)
            finally:
                os.close(master)
            self.assertEqual(done.returncode, 0, done.stderr)
            return text

        def reset():
            self.assertEqual(self.luna('uninstall').returncode, 0)

        text = install(env, True)
        self.assertEqual(text.count('luna gui verify'), 1, text)
        self.assertIn('luna status --live', text)
        reset()
        self.assertNotIn('luna gui', install(env, False))  # not a terminal: nothing extra
        reset()
        self.assertNotIn('luna gui', install(dict(env, LUNA_GUI_CHECKS='0'), True))

    def test_install_both_addons_patches_once(self):
        result = self.luna('install')
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        for name in ('presence', 'playtime'):
            for rel in ('backend/main.lua', 'public/agent.js', 'public/panel.js', 'plugin.json'):
                self.assertTrue(self.installed(name, rel), '%s/%s missing' % (name, rel))
        # The first-party shell and compressor ship no agent.js.
        for name in ('luna', 'compressor'):
            for rel in ('backend/main.lua', 'public/panel.js', 'plugin.json'):
                self.assertTrue(self.installed(name, rel), '%s/%s missing' % (name, rel))
        self.assertTrue((self.addons / 'bridge.lua').is_file())
        content = self.boot.read_text()
        self.assertEqual(content.count('SLS-ADDONS-INTEGRATION'), 1)
        self.assertTrue((self.boot.parent / 'boot.lua.sls-addons.original').is_file())

    def test_install_is_idempotent(self):
        self.luna('install')
        before = self.boot.read_bytes()
        result = self.luna('install')
        self.assertEqual(result.returncode, 0)
        self.assertIn('Nothing to do', result.stdout)
        self.assertEqual(self.boot.read_bytes(), before)

    def test_install_reinstalls_bridge_after_a_base_update(self):
        self.assertEqual(self.luna('install').returncode, 0)
        # A Lumen/Moon update replaces boot.lua and wipes the bridge while the
        # payloads stay current. Install must repair that, not claim idleness.
        self.boot.write_text(UPSTREAM)
        result = self.luna('install')
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn('reinstall', result.stdout)
        self.assertEqual(self.boot.read_text().count('SLS-ADDONS-INTEGRATION'), 1)
        again = self.luna('install')
        self.assertIn('Nothing to do', again.stdout)

    def test_dry_run_writes_nothing(self):
        result = self.luna('install', '--dry-run')
        self.assertEqual(result.returncode, 0)
        self.assertIn('DRY RUN', result.stdout)
        self.assertFalse(self.addons.exists())
        self.assertFalse((self.home / '.local/share/applications').exists())
        self.assertEqual(self.boot.read_text(), UPSTREAM)

    def test_install_generates_steam_machine_shortcuts_automatically(self):
        result = self.luna('install')
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        entry = self.home / '.local/share/applications/steamos-1920x1080-60.desktop'
        self.assertIn('Actions=FreePointer;', entry.read_text())
        self.assertTrue((self.home / '.local/bin/luna-steam-machine').is_file())
        self.assertTrue((self.home / '.local/share/Lumen/sls-luna/steam-machine.py').is_file())

    def test_install_can_explicitly_skip_shortcuts(self):
        self.assertEqual(self.luna('install', '--no-steam-machine').returncode, 0)
        self.assertFalse((self.home / '.local/bin/luna-steam-machine').exists())

    def test_identical_reinstall_repairs_missing_shortcut(self):
        self.assertEqual(self.luna('install').returncode, 0)
        entry = self.home / '.local/share/applications/steamos-1920x1080-60.desktop'
        entry.unlink()
        self.assertEqual(self.luna('install').returncode, 0)
        self.assertTrue(entry.is_file())

    def test_full_uninstall_removes_owned_shortcuts_but_not_custom_ones(self):
        self.assertEqual(self.luna('install').returncode, 0)
        custom = self.home / '.local/share/applications/my-steam-machine.desktop'
        custom.write_text('custom user launcher')
        self.assertEqual(self.luna('uninstall').returncode, 0)
        self.assertFalse((self.home / '.local/bin/luna-steam-machine').exists())
        self.assertEqual(custom.read_text(), 'custom user launcher')

    def test_install_only_one_addon(self):
        result = self.luna('install', '--only', 'playtime')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.installed('playtime', 'backend/main.lua'))
        self.assertFalse((self.addons / 'presence').exists())
        self.assertTrue((self.addons / 'bridge.lua').is_file())

    def test_rejects_unknown_addon(self):
        result = self.luna('install', '--only', 'nonsense')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('unknown add-on', result.stderr)

    # -- refusals --------------------------------------------------------
    def test_refuses_while_steam_is_running(self):
        result = self.luna('install', running='steam')
        self.assertEqual(result.returncode, 2)
        self.assertIn('Steam is running', result.stdout + result.stderr)
        self.assertFalse(self.addons.exists())
        self.assertEqual(self.boot.read_text(), UPSTREAM)

    def test_refuses_while_a_lumen_sidecar_survives(self):
        # A sidecar from an earlier session keeps the old boot.lua in memory, which
        # makes a successful install look ineffective. Never kill it for the user.
        result = self.luna('install', running='lumen')
        self.assertEqual(result.returncode, 2)
        combined = result.stdout + result.stderr
        self.assertIn('sidecar is still running', combined)
        self.assertIn('pkill -u $(id -u) -x lumen', combined)
        self.assertFalse(self.addons.exists())

    def test_refuses_on_a_drifted_boot_lua(self):
        self.boot.write_text('-- some other Lumen build\n')
        result = self.luna('install')
        self.assertEqual(result.returncode, 4)
        self.assertIn('REFUSING', result.stderr + result.stdout)
        self.assertFalse(self.addons.exists())
        self.assertEqual(self.boot.read_text(), '-- some other Lumen build\n')

    def test_refuses_to_overwrite_a_divergent_payload(self):
        self.luna('install')
        target = self.addons / 'playtime/backend/main.lua'
        target.write_text('-- locally modified\n')
        result = self.luna('install')
        self.assertEqual(result.returncode, 3)
        self.assertIn('--force', result.stdout)
        self.assertEqual(target.read_text(), '-- locally modified\n')
        forced = self.luna('install', '--force')
        self.assertEqual(forced.returncode, 0, forced.stderr)
        self.assertNotEqual(target.read_text(), '-- locally modified\n')

    # -- uninstall -------------------------------------------------------
    def test_uninstall_one_keeps_the_bridge_and_the_patch(self):
        self.luna('install')
        result = self.luna('uninstall', '--only', 'presence')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.addons / 'presence').exists())
        self.assertTrue(self.installed('playtime', 'backend/main.lua'))
        self.assertTrue((self.addons / 'bridge.lua').is_file())
        self.assertEqual(self.boot.read_text().count('SLS-ADDONS-INTEGRATION'), 1)

    def test_uninstall_last_restores_boot_lua_and_removes_the_bridge(self):
        self.luna('install')
        result = self.luna('uninstall')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.boot.read_text(), UPSTREAM)
        self.assertFalse((self.addons / 'bridge.lua').exists())
        for name in ('presence', 'playtime'):
            self.assertFalse((self.addons / name).exists())

    def test_uninstall_refuses_without_a_backup(self):
        self.luna('install')
        (self.boot.parent / 'boot.lua.sls-addons.original').unlink()
        result = self.luna('uninstall')
        self.assertEqual(result.returncode, 4)
        # Nothing may be deleted while Lumen is still patched.
        self.assertTrue(self.installed('playtime', 'backend/main.lua'))
        self.assertTrue(self.installed('presence', 'backend/main.lua'))

    def test_uninstall_refuses_while_steam_runs(self):
        self.luna('install')
        result = self.luna('uninstall', running='steam')
        self.assertEqual(result.returncode, 2)
        self.assertTrue(self.installed('playtime', 'backend/main.lua'))

    # -- data safety -----------------------------------------------------
    def test_user_data_is_never_touched(self):
        before = self.data_snapshot()
        self.luna('install')
        self.luna('uninstall')
        self.assertEqual(self.data_snapshot(), before)

    # -- reporting -------------------------------------------------------
    def test_status_reports_a_healthy_install(self):
        self.luna('install')
        result = self.luna('status')
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn('patched', result.stdout)
        self.assertIn('same', result.stdout)

    def test_preflight_reports_required_and_optional_components_read_only(self):
        result = self.luna('preflight')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r'Lumen runtime\s+present')
        self.assertRegex(result.stdout, r'LuaTools backend\s+present')
        self.assertRegex(result.stdout, r'CloudRedirect\s+not detected')
        self.assertRegex(result.stdout, r'Decky Loader\s+not detected')
        self.assertRegex(result.stdout, r'Compressor CLI\s+not detected')
        self.assertFalse((self.home / 'homebrew').exists())
        self.assertFalse((self.home / '.local/share/CloudRedirect').exists())

    def test_preflight_detects_optional_component_artifacts_without_installing_them(self):
        cloud = self.home / '.local/share/CloudRedirect'
        cloud.mkdir(parents=True)
        (cloud / 'cloud_redirect.so').write_bytes(b'hook')
        (cloud / 'cloud_redirect_cli').write_bytes(b'cli')
        decky = self.home / 'homebrew/services'
        decky.mkdir(parents=True)
        loader = decky / 'PluginLoader'
        loader.write_bytes(b'loader')
        loader.chmod(0o755)
        (decky / '.loader.version').write_text('v3-test')
        compressor = self.home / '.local/bin/btrfs-game-compressor'
        compressor.parent.mkdir(parents=True)
        compressor.write_bytes(b'tool')
        compressor.chmod(0o755)

        result = self.luna('preflight')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r'CloudRedirect\s+present')
        self.assertRegex(result.stdout, r'Decky Loader\s+present')
        self.assertIn('(version v3-test)', result.stdout)
        self.assertRegex(result.stdout, r'Compressor CLI\s+present')

    def test_status_flags_a_modified_payload(self):
        self.luna('install')
        (self.addons / 'playtime/public/panel.js').write_text('// changed\n')
        result = self.luna('status')
        self.assertEqual(result.returncode, 1)
        self.assertIn('differs', result.stdout)

    def test_verify_detects_a_tampered_payload(self):
        tree = self.tree_copy()
        (tree / 'addons/presence/plugin/public/panel.js').write_bytes(b'// tampered\n')
        result = self.luna_in(tree, 'verify')
        self.assertEqual(result.returncode, 1)
        self.assertIn('does not match the manifest', result.stdout)

    def test_verify_accepts_the_pinned_payloads(self):
        result = self.luna('verify')
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_verify_compares_against_local_sources_when_asked(self):
        sources = self.home / 'Documents/Scripts'
        sources.mkdir(parents=True)
        for name in ('playtime', 'presence'):
            origin = Path(os.environ.get('HOME', '/home/user')) / 'Documents/Scripts' / ('slsteam-' + name)
            if not (origin / '.git').is_dir():
                self.skipTest('no local %s checkout to compare against' % name)
            shutil.copytree(origin, sources / ('slsteam-' + name),
                            ignore=shutil.ignore_patterns('.git', '__pycache__'))
        manifest = json.loads((HERE / 'vendor/MANIFEST.json').read_text())
        env = dict(self.env, LUNA_TEST_SOURCES='1')
        result = subprocess.run([sys.executable, str(LUNA), 'verify', '--sources',
                                 '--source-dir', str(sources)],
                                capture_output=True, text=True, env=env)
        self.assertIn('compared against', result.stdout)
        self.assertEqual(result.returncode, 0, result.stdout)

    # -- release / update / snapshot -------------------------------------
    def bundle_version(self):
        return json.loads((HERE / 'addons/luna/plugin/plugin.json').read_text())['version']

    def test_install_records_a_manifest_snapshot(self):
        result = self.luna('install')
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        snapshot = json.loads((self.addons / 'luna-manifest.json').read_text())
        self.assertEqual(snapshot['versions']['luna'], self.bundle_version())
        self.assertIn('playtime', snapshot['versions'])
        self.assertTrue(snapshot['installedAt'] > 0)

    def test_release_builds_a_bundle_other_machines_can_use(self):
        out = self.temp / 'dist'
        result = self.luna('release', '--out', str(out))
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        bundles = sorted(out.glob('luna-*.tar.gz'))
        self.assertEqual(len(bundles), 1)
        sidecar = Path(str(bundles[0]) + '.sha256')
        self.assertTrue(sidecar.is_file(), 'the checksum sidecar is missing')
        digest = sidecar.read_text().split()[0]
        self.assertEqual(hashlib.sha256(bundles[0].read_bytes()).hexdigest(), digest)
        with tarfile.open(bundles[0]) as tar:
            names = tar.getnames()
        prefix = 'luna-%s/' % self.bundle_version()
        for expected in ('vendor/MANIFEST.json', 'scripts/luna.py', 'scripts/steam-machine.py', 'scripts/patch-lumen.py',
                         'bridge/bridge.lua', 'addons/luna/plugin/plugin.json',
                         'addons/playtime/plugin/backend/main.lua'):
            self.assertIn(prefix + expected, names)

    def test_update_installs_from_a_verified_bundle(self):
        out = self.temp / 'dist'
        self.assertEqual(self.luna('release', '--out', str(out)).returncode, 0)
        bundle = next(out.glob('luna-*.tar.gz'))
        result = self.luna('update', '--bundle', str(bundle))
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn('sha256 : verified', result.stdout)
        for name in ('playtime', 'presence'):
            self.assertTrue(self.installed(name, 'backend/main.lua'))
        self.assertEqual(self.boot.read_text().count('SLS-ADDONS-INTEGRATION'), 1)
        self.assertEqual(self.luna('verify').returncode, 0)

    def test_update_refuses_a_tampered_bundle(self):
        out = self.temp / 'dist'
        self.assertEqual(self.luna('release', '--out', str(out)).returncode, 0)
        bundle = next(out.glob('luna-*.tar.gz'))
        bundle.write_bytes(bundle.read_bytes() + b'x')
        result = self.luna('update', '--bundle', str(bundle))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('sha256 mismatch', result.stderr + result.stdout)
        self.assertFalse(self.addons.exists(), 'a tampered bundle must not be installed')

    def test_update_dry_run_writes_nothing(self):
        out = self.temp / 'dist'
        self.assertEqual(self.luna('release', '--out', str(out)).returncode, 0)
        bundle = next(out.glob('luna-*.tar.gz'))
        result = self.luna('update', '--bundle', str(bundle), '--dry-run')
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn('DRY RUN', result.stdout)
        self.assertFalse(self.addons.exists())

    # -- update checks, unattended updates, timer ------------------------
    def bundle_and_env(self, extra=None):
        out = self.temp / 'dist'
        self.assertEqual(self.luna('release', '--out', str(out)).returncode, 0)
        bundle = next(out.glob('luna-*.tar.gz'))
        env = dict(self.env, LUNA_TEST_BUNDLE=str(bundle),
                   LUNA_TEST_TAG='v%s' % self.bundle_version())
        if extra:
            env.update(extra)
        return bundle, env

    def test_install_keeps_a_standalone_updater_copy(self):
        self.assertEqual(self.luna('install').returncode, 0)
        copy = self.home / '.local/share/Lumen/sls-luna/luna.py'
        self.assertTrue(copy.is_file(), 'the updater copy is missing')
        self.assertEqual(copy.read_bytes(), (HERE / 'scripts' / 'luna.py').read_bytes())

    def test_check_reports_the_installed_and_latest_versions(self):
        self.assertEqual(self.luna('install').returncode, 0)
        _, env = self.bundle_and_env()
        result = self.luna('check', '--force', env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report['installed'], self.bundle_version())
        self.assertEqual(report['latest'], 'v%s' % self.bundle_version())
        self.assertFalse(report['available'], 'the same version cannot be an update')
        cached = json.loads((self.home / '.local/share/Lumen/sls-luna/update-check.json').read_text())
        self.assertEqual(cached['latest'], report['latest'])
        # A newer release is offered.
        result = self.luna('check', '--force', env=dict(env, LUNA_TEST_TAG='v99.0.0'))
        report = json.loads(result.stdout)
        self.assertTrue(report['available'], 'a newer release must be offered')

    def test_autoupdate_applies_a_newer_release_when_steam_is_closed(self):
        self.assertEqual(self.luna('install').returncode, 0)
        # Pretend the installed add-on is old, and let the stub offer the real one.
        plugin = self.addons / 'luna' / 'plugin.json'
        stale = json.loads(plugin.read_text())
        stale['version'] = '0.0.1'
        plugin.write_text(json.dumps(stale))
        (self.data / 'luna.json').write_text(json.dumps({'version': 1, 'auto_update': 'install'}))
        _, env = self.bundle_and_env()
        result = self.luna('autoupdate', '--verbose', env=env)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        last = json.loads((self.home / '.local/share/Lumen/sls-luna/autoupdate.json').read_text())
        self.assertEqual(last['result'], 'applied', result.stdout)
        self.assertEqual(json.loads((self.addons / 'luna' / 'plugin.json').read_text())['version'],
                         self.bundle_version(), 'the newer payloads must be installed')

    def test_autoupdate_defers_while_steam_runs_and_never_installs(self):
        self.assertEqual(self.luna('install').returncode, 0)
        # Make an update available so the Steam guard, not the
        # up-to-date check, decides the outcome.
        plugin = self.addons / 'luna' / 'plugin.json'
        stale = json.loads(plugin.read_text())
        stale['version'] = '0.0.1'
        plugin.write_text(json.dumps(stale))
        (self.data / 'luna.json').write_text(json.dumps({'version': 1, 'auto_update': 'install'}))
        _, env = self.bundle_and_env()
        result = self.luna('autoupdate', '--verbose', env=env, running='steam,')
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        last = json.loads((self.home / '.local/share/Lumen/sls-luna/autoupdate.json').read_text())
        self.assertEqual(last['result'], 'deferred')
        self.assertIn('Steam', last['reason'])

    def test_autoupdate_does_nothing_when_the_mode_is_off(self):
        self.assertEqual(self.luna('install').returncode, 0)
        _, env = self.bundle_and_env()
        result = self.luna('autoupdate', '--verbose', env=env)
        self.assertEqual(result.returncode, 0)
        self.assertFalse((self.home / '.local/share/Lumen/sls-luna/autoupdate.json').exists())

    def test_timer_on_status_off_manages_the_units(self):
        self.assertEqual(self.luna('install').returncode, 0)
        unit_dir = self.home / '.config/systemd/user'
        result = self.luna('timer', 'on')
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        service = unit_dir / 'luna-autoupdate.service'
        timer = unit_dir / 'luna-autoupdate.timer'
        self.assertTrue(service.is_file() and timer.is_file(), 'the units were not written')
        self.assertIn('autoupdate', service.read_text())
        self.assertIn('OnCalendar', timer.read_text())
        status = json.loads(self.luna('timer', 'status').stdout)
        self.assertEqual(status['enabled'], 'enabled')
        self.assertEqual(self.luna('timer', 'off').returncode, 0)
        self.assertFalse(timer.exists() and service.exists(), 'the units were not removed')

    def test_release_refuses_a_dirty_tree(self):
        tree = self.tree_copy()
        target = tree / 'addons/luna/plugin/public/panel.js'
        target.write_bytes(target.read_bytes() + b'\n// dirty\n')
        out = self.temp / 'dist-dirty'
        result = self.luna_in(tree, 'release', '--out', str(out))
        self.assertNotEqual(result.returncode, 0, 'a dirty tree must not release')
        self.assertIn('does not match the manifest', result.stdout + result.stderr)

    def test_update_refuses_a_bundle_with_a_bad_inner_manifest(self):
        out = self.temp / 'dist-inner'
        self.assertEqual(self.luna('release', '--out', str(out)).returncode, 0)
        import io as _io
        bundle = next(out.glob('luna-*.tar.gz'))
        raw = bundle.read_bytes()
        # Flip one payload byte inside the compressed stream: the outer
        # checksum is recomputed below, so only the inner manifest check
        # can catch it.
        mutated = bytearray(raw)
        mutated[len(mutated) // 2] ^= 0x01
        bundle.write_bytes(bytes(mutated))
        (Path(str(bundle) + '.sha256')).write_text(
            '%s  %s\n' % (__import__('hashlib').sha256(bytes(mutated)).hexdigest(), bundle.name))
        result = self.luna('update', '--bundle', str(bundle))
        self.assertNotEqual(result.returncode, 0, 'a corrupt bundle must not install')
        self.assertFalse(self.addons.exists(), 'a corrupt bundle must not install')

    def test_update_refuses_a_flipped_byte_anywhere_in_the_stream(self):
        # The gzip CRC catches damage the file list would also catch; both layers are kept.
        out = self.temp / 'dist-flip'
        self.assertEqual(self.luna('release', '--out', str(out)).returncode, 0)
        bundle = next(out.glob('luna-*.tar.gz'))
        raw = bundle.read_bytes()
        for fraction in (0.1, 0.35, 0.6, 0.9):
            mutated = bytearray(raw)
            mutated[int(len(mutated) * fraction)] ^= 0x01
            bundle.write_bytes(bytes(mutated))
            Path(str(bundle) + '.sha256').write_text(
                '%s  %s\n' % (__import__('hashlib').sha256(bytes(mutated)).hexdigest(), bundle.name))
            result = self.luna('update', '--bundle', str(bundle))
            self.assertNotEqual(result.returncode, 0, 'corrupt bundle accepted at %s' % fraction)
            self.assertFalse(self.addons.exists())

    # -- bundle file list: every executed file is pinned, in both directions
    def rewrite_bundle(self, mutate):
        """Release a bundle, let `mutate(members)` edit {name: bytes|None-for-dir}, repack with a fresh sidecar."""
        out = self.temp / 'dist-rewrite'
        self.assertEqual(self.luna('release', '--out', str(out)).returncode, 0)
        bundle = next(out.glob('luna-*.tar.gz'))
        with tarfile.open(bundle) as tar:
            members = [(m, tar.extractfile(m).read() if m.isfile() else None) for m in tar.getmembers()]
        members = mutate(members)
        with tarfile.open(bundle, 'w:gz') as tar:
            for info, data in members:
                tar.addfile(info, io.BytesIO(data) if data is not None else None)
        Path(str(bundle) + '.sha256').write_text(
            '%s  %s\n' % (hashlib.sha256(bundle.read_bytes()).hexdigest(), bundle.name))
        return bundle

    def assert_refused(self, bundle, needle, *extra):
        result = self.luna('update', '--bundle', str(bundle), *extra)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(needle, result.stdout + result.stderr)
        self.assertFalse(self.addons.exists(), 'nothing may be installed from a refused bundle')

    def tamper(self, suffix, payload=b'\n# evil\n'):
        def mutate(members):
            out = []
            for info, data in members:
                if info.name.endswith(suffix):
                    data = data + payload
                    info.size = len(data)
                out.append((info, data))
            return out
        return mutate

    def test_update_refuses_a_tampered_unpinned_luna_py(self):
        bundle = self.rewrite_bundle(self.tamper('/scripts/luna.py'))
        self.assert_refused(bundle, 'scripts/luna.py does not match')

    def test_update_refuses_a_tampered_install_sh(self):
        # install.sh is not shipped; if one appears (or is listed and changed) it is refused.
        def mutate(members):
            root = members[0][0].name.split('/')[0]
            info = tarfile.TarInfo(root + '/install.sh')
            info.size = 5
            return members + [(info, b'evil\n')]
        self.assert_refused(self.rewrite_bundle(mutate), 'unlisted file in the bundle: install.sh')

    def test_update_refuses_an_extra_unlisted_file(self):
        def mutate(members):
            root = members[0][0].name.split('/')[0]
            info = tarfile.TarInfo(root + '/scripts/extra.py')
            info.size = 1
            return members + [(info, b'x')]
        self.assert_refused(self.rewrite_bundle(mutate), 'unlisted file in the bundle: scripts/extra.py')

    def test_update_refuses_a_missing_listed_file(self):
        bundle = self.rewrite_bundle(lambda m: [x for x in m if not x[0].name.endswith('/README.md')])
        self.assert_refused(bundle, 'listed file is missing: README.md')

    def test_update_refuses_a_duplicate_member(self):
        def mutate(members):
            last = [x for x in members if x[0].name.endswith('/scripts/luna.py')][0]
            info = tarfile.TarInfo(last[0].name)
            info.size = 5
            return members + [(info, b'evil\n')]
        self.assert_refused(self.rewrite_bundle(mutate), 'duplicate bundle member')

    def test_update_refuses_a_symlink_member(self):
        def mutate(members):
            root = members[0][0].name.split('/')[0]
            info = tarfile.TarInfo(root + '/scripts/link.py')
            info.type = tarfile.SYMTYPE
            info.linkname = '/etc/passwd'
            return members + [(info, None)]
        self.assert_refused(self.rewrite_bundle(mutate), 'link or special file')

    def test_update_refuses_a_legacy_bundle_unless_allowed(self):
        legacy = self.rewrite_bundle(lambda m: [x for x in m if not x[0].name.endswith('/RELEASE-FILES.json')])
        self.assert_refused(legacy, '--allow-legacy-bundle')
        result = self.luna('update', '--bundle', str(legacy), '--allow-legacy-bundle', '--dry-run')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('legacy bundle', result.stderr)

    def test_update_without_a_sidecar_says_it_is_self_consistency_only(self):
        out = self.temp / 'dist-nosidecar'
        self.assertEqual(self.luna('release', '--out', str(out)).returncode, 0)
        bundle = next(out.glob('luna-*.tar.gz'))
        Path(str(bundle) + '.sha256').unlink()
        result = self.luna('update', '--bundle', str(bundle), '--dry-run')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('self-consistency', result.stdout)

    def test_status_degrades_on_a_missing_installed_file(self):
        self.assertEqual(self.luna('install').returncode, 0)
        (self.addons / 'presence/public/panel.js').unlink()
        result = self.luna('status')
        self.assertEqual(result.returncode, 1, 'a missing file must degrade status')
        self.assertIn('presence', result.stdout)
        self.assertIn('new', result.stdout)

    def test_notify_records_available_while_steam_runs(self):
        self.assertEqual(self.luna('install').returncode, 0)
        plugin = self.addons / 'luna' / 'plugin.json'
        stale = json.loads(plugin.read_text())
        stale['version'] = '0.0.1'
        plugin.write_text(json.dumps(stale))
        (self.data / 'luna.json').write_text(json.dumps({'version': 1, 'auto_update': 'notify'}))
        _, env = self.bundle_and_env()
        result = self.luna('autoupdate', '--verbose', env=env, running='steam,')
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        last = json.loads((self.home / '.local/share/Lumen/sls-luna/autoupdate.json').read_text())
        # Notify is read-only: Steam running must not block the report.
        self.assertEqual(last['result'], 'available')

    def test_stale_update_lock_is_cleared(self):
        self.assertEqual(self.luna('install').returncode, 0)
        lock = self.home / '.local/share/Lumen/sls-luna/update.lock'
        # A dead owner from yesterday must not wedge updates forever.
        lock.write_text(json.dumps({'pid': 424242, 'at': 1}))
        out = self.temp / 'dist-lock'
        self.assertEqual(self.luna('release', '--out', str(out)).returncode, 0)
        bundle = next(out.glob('luna-*.tar.gz'))
        result = self.luna('update', '--bundle', str(bundle), '--dry-run')
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertFalse(lock.exists(), 'a successful run releases the lock')

    def test_live_update_lock_refuses_a_second_update(self):
        import os as _os, time as _time
        self.assertEqual(self.luna('install').returncode, 0)
        lock = self.home / '.local/share/Lumen/sls-luna/update.lock'
        lock.write_text(json.dumps({'pid': _os.getpid(), 'at': int(_time.time())}))
        out = self.temp / 'dist-lock-live'
        self.assertEqual(self.luna('release', '--out', str(out)).returncode, 0)
        bundle = next(out.glob('luna-*.tar.gz'))
        result = self.luna('update', '--bundle', str(bundle), '--dry-run')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('already running', result.stderr + result.stdout)

    # -- security review: update hardening ---------------------------------
    def module(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location('luna_mod_%d' % id(self), str(HERE / 'scripts' / 'luna.py'))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def repack(self, edit, tag='repack'):
        """Release a bundle, let `edit(files)` change {relative path: bytes}, then rewrite the
        file list and the sidecar so only deliberate content differences remain."""
        out = self.temp / ('dist-' + tag)
        self.assertEqual(self.luna('release', '--out', str(out)).returncode, 0)
        bundle = next(out.glob('luna-*.tar.gz'))
        with tarfile.open(bundle) as tar:
            root = tar.getnames()[0].split('/')[0]
            files = {m.name[len(root) + 1:]: tar.extractfile(m).read() for m in tar.getmembers() if m.isfile()}
        edit(files)
        files.pop('RELEASE-FILES.json', None)
        listing = {rel: hashlib.sha256(data).hexdigest() for rel, data in files.items()}
        files['RELEASE-FILES.json'] = json.dumps({'format': 2, 'files': listing}).encode()
        with tarfile.open(bundle, 'w:gz') as tar:
            for rel, data in sorted(files.items()):
                info = tarfile.TarInfo('%s/%s' % (root, rel))
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        Path(str(bundle) + '.sha256').write_text(
            '%s  %s\n' % (hashlib.sha256(bundle.read_bytes()).hexdigest(), bundle.name))
        return bundle

    def assert_clean_refusal(self, result):
        text = result.stdout + result.stderr
        self.assertNotEqual(result.returncode, 0, text)
        self.assertIn('REFUSING', text)
        self.assertNotIn('Traceback', text)
        self.assertFalse(self.addons.exists(), 'nothing may be installed from a refused bundle')
        return text

    def test_dry_run_executes_nothing_from_the_bundle(self):
        canary = self.temp / 'canary'

        def edit(files):
            files['scripts/luna.py'] = b'import os, pathlib\npathlib.Path(os.environ["LUNA_CANARY"]).write_text("ran")\n'
        bundle = self.repack(edit)
        result = self.luna('update', '--bundle', str(bundle), '--dry-run', env=dict(self.env, LUNA_CANARY=str(canary)))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('DRY RUN', result.stdout)
        self.assertFalse(canary.exists(), '--dry-run executed code from the bundle')
        real = self.luna('update', '--bundle', str(bundle), env=dict(self.env, LUNA_CANARY=str(canary)))
        self.assertTrue(canary.exists(), 'the real update is the one that runs the bundle installer')

    def test_update_refuses_an_empty_sidecar_cleanly(self):
        out = self.temp / 'dist-empty-sidecar'
        self.assertEqual(self.luna('release', '--out', str(out)).returncode, 0)
        bundle = next(out.glob('luna-*.tar.gz'))
        for content in ('', '\n  \n', 'nothex\n'):
            Path(str(bundle) + '.sha256').write_text(content)
            self.assert_clean_refusal(self.luna('update', '--bundle', str(bundle), '--dry-run'))

    def test_update_refuses_a_wrongly_shaped_manifest_or_listing_cleanly(self):
        def with_file(name, data):
            def edit(files):
                files[name] = data
            return edit
        cases = [('vendor/MANIFEST.json', b'[]'), ('vendor/MANIFEST.json', b'{"addons": []}'),
                 ('vendor/MANIFEST.json', b'{"addons": {"luna": []}}'),
                 ('vendor/MANIFEST.json', b'{"addons": {"luna": {"plugin_version": "0.0.1", "files": []}}}'),
                 ('vendor/MANIFEST.json', b'"text"')]
        for index, (name, data) in enumerate(cases):
            bundle = self.repack(with_file(name, data), tag='shape%d' % index)
            self.assert_clean_refusal(self.luna('update', '--bundle', str(bundle), '--dry-run'))
        # a listing that is not an object, and one with non-text digests (written after repack's own)
        bundle = self.repack(lambda files: None, tag='listing')
        with tarfile.open(bundle) as tar:
            members = [(m, tar.extractfile(m).read()) for m in tar.getmembers() if m.isfile()]
        for index, replacement in enumerate((b'[]', b'{"format": 2, "files": []}', b'{"format": 2, "files": {"a": 5}}')):
            with tarfile.open(bundle, 'w:gz') as tar:
                for info, data in members:
                    if info.name.endswith('/RELEASE-FILES.json'):
                        data = replacement
                        info.size = len(data)
                    tar.addfile(info, io.BytesIO(data))
            Path(str(bundle) + '.sha256').write_text(
                '%s  %s\n' % (hashlib.sha256(bundle.read_bytes()).hexdigest(), bundle.name))
            self.assert_clean_refusal(self.luna('update', '--bundle', str(bundle), '--dry-run'))

    def test_autoupdate_records_a_failure_on_any_exception(self):
        from unittest import mock
        self.assertEqual(self.luna('install').returncode, 0)
        plugin = self.addons / 'luna' / 'plugin.json'
        stale = json.loads(plugin.read_text())
        stale['version'] = '0.0.1'
        plugin.write_text(json.dumps(stale))
        (self.data / 'luna.json').write_text(json.dumps({'version': 1, 'auto_update': 'install'}))
        bundle, env = self.bundle_and_env()
        module = self.module()
        out = io.StringIO()
        with mock.patch.dict(os.environ, env), mock.patch.object(module, 'install_bundle', side_effect=RuntimeError('boom')), \
                mock.patch.object(module, 'refuse_reasons', return_value=[]):
            import contextlib
            with contextlib.redirect_stdout(out):
                module.cmd_autoupdate(__import__('argparse').Namespace(verbose=True))
        last = json.loads((self.home / '.local/share/Lumen/sls-luna/autoupdate.json').read_text())
        self.assertEqual(last['result'], 'failed', last)
        self.assertIn('boom', last.get('reason', ''))

    def test_autoupdate_never_passes_allow_downgrade(self):
        from unittest import mock
        self.assertEqual(self.luna('install').returncode, 0)
        plugin = self.addons / 'luna' / 'plugin.json'
        stale = json.loads(plugin.read_text())
        stale['version'] = '0.0.1'
        plugin.write_text(json.dumps(stale))
        (self.data / 'luna.json').write_text(json.dumps({'version': 1, 'auto_update': 'install'}))
        bundle, env = self.bundle_and_env()
        module = self.module()
        import contextlib
        with mock.patch.dict(os.environ, env), mock.patch.object(module, 'install_bundle', return_value=0) as install, \
                mock.patch.object(module, 'refuse_reasons', return_value=[]), contextlib.redirect_stdout(io.StringIO()):
            module.cmd_autoupdate(__import__('argparse').Namespace(verbose=False))
        self.assertTrue(install.called)
        self.assertFalse(install.call_args[1].get('allow_downgrade'), install.call_args)

    def pending_autoupdate(self, tag):
        if not self.addons.exists():
            self.assertEqual(self.luna('install').returncode, 0)
        tools = self.home / '.local/share/Lumen/sls-luna'
        (tools / 'pending.json').write_text(json.dumps({'tag': tag, 'requestedAt': 1}))
        ghlog = self.temp / 'gh.log'
        bundle, env = self.bundle_and_env({'LUNA_TEST_GHLOG': str(ghlog)})
        result = self.luna('autoupdate', '--verbose', env=env)
        last = json.loads((tools / 'autoupdate.json').read_text())
        return result, last, (ghlog.read_text() if ghlog.exists() else '')

    def test_autoupdate_rejects_a_malformed_pending_tag_before_gh(self):
        for tag in ('--repo=evil/x', 'v1.0', 'v1.0.0;rm', 'v1.0.0\n--x', '../v1.0.0', 'latest'):
            result, last, ghlog = self.pending_autoupdate(tag)
            self.assertEqual(last['result'], 'error', (tag, last))
            self.assertIn('tag', last['reason'])
            self.assertNotIn('release download', ghlog, tag)

    def test_autoupdate_requires_the_bundle_version_to_equal_the_tag(self):
        self.assertEqual(self.luna('install').returncode, 0)
        plugin = self.addons / 'luna' / 'plugin.json'
        stale = json.loads(plugin.read_text())
        stale['version'] = '0.0.1'
        plugin.write_text(json.dumps(stale))
        result, last, _ = self.pending_autoupdate('v0.0.9')  # the stub serves the real bundle whatever the tag
        self.assertEqual(last['result'], 'failed', last)
        self.assertEqual(last['version'], 'v0.0.9')
        self.assertIn('tag', result.stderr + result.stdout)
        self.assertEqual(json.loads(plugin.read_text())['version'], '0.0.1', 'a mismatching bundle must not install')

    def test_update_accepts_a_bundle_that_drops_an_addon_and_refuses_bad_names(self):
        def drop(files):
            manifest = json.loads(files['vendor/MANIFEST.json'])
            del manifest['addons']['presence']
            files['vendor/MANIFEST.json'] = json.dumps(manifest).encode()
            for rel in [r for r in files if r.startswith('addons/presence/')]:
                del files[rel]
        bundle = self.repack(drop, tag='drop')
        result = self.luna('update', '--bundle', str(bundle), '--dry-run')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        def evil(files):
            manifest = json.loads(files['vendor/MANIFEST.json'])
            manifest['addons']['../evil'] = {'files': {'x': {'sha256': '0' * 64, 'source_path': 'x'}}}
            files['vendor/MANIFEST.json'] = json.dumps(manifest).encode()
        text = self.assert_clean_refusal(self.luna('update', '--bundle', str(self.repack(evil, tag='evil')), '--dry-run'))
        self.assertIn('add-on name', text)

        def traversal(files):
            manifest = json.loads(files['vendor/MANIFEST.json'])
            manifest['addons']['presence']['files']['plugin.json']['source_path'] = '../../vendor/MANIFEST.json'
            files['vendor/MANIFEST.json'] = json.dumps(manifest).encode()
        self.assert_clean_refusal(self.luna('update', '--bundle', str(self.repack(traversal, tag='trav')), '--dry-run'))

        def no_luna(files):
            manifest = json.loads(files['vendor/MANIFEST.json'])
            del manifest['addons']['luna']
            files['vendor/MANIFEST.json'] = json.dumps(manifest).encode()
        self.assert_clean_refusal(self.luna('update', '--bundle', str(self.repack(no_luna, tag='noluna')), '--dry-run'))

    def test_update_lock_is_a_real_lock_not_a_pid_guess(self):
        import fcntl
        self.assertEqual(self.luna('install').returncode, 0)
        lock = self.home / '.local/share/Lumen/sls-luna/update.lock'
        out = self.temp / 'dist-flock'
        self.assertEqual(self.luna('release', '--out', str(out)).returncode, 0)
        bundle = next(out.glob('luna-*.tar.gz'))
        # Another holder whose recorded pid is dead and whose timestamp is ancient: the old code stole it.
        holder = open(str(lock), 'w')
        self.addCleanup(holder.close)
        holder.write(json.dumps({'pid': 424242, 'at': 1}))
        holder.flush()
        fcntl.flock(holder, fcntl.LOCK_EX)
        result = self.luna('update', '--bundle', str(bundle), '--dry-run')
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('already running', result.stderr + result.stdout)
        self.assertTrue(lock.exists(), 'a refused update must leave the holder\'s lock alone')
        fcntl.flock(holder, fcntl.LOCK_UN)
        self.assertEqual(self.luna('update', '--bundle', str(bundle), '--dry-run').returncode, 0)

    def test_release_never_removes_a_lock_somebody_else_holds(self):
        import fcntl
        module = self.module()
        with __import__('unittest').mock.patch.dict(os.environ, self.env):
            lock = Path(os.environ['HOME']) / 'l.lock'
            self.assertEqual(module._acquire_lock(lock), (True, ''))
            os.unlink(str(lock))  # a stale-lock stealer replaced our file...
            other = open(str(lock), 'w')
            self.addCleanup(other.close)
            fcntl.flock(other, fcntl.LOCK_EX)
            module._release_lock(lock)  # ...and our late release must not delete theirs
            self.assertTrue(lock.exists())

    def test_full_uninstall_disables_the_timer(self):
        self.assertEqual(self.luna('install').returncode, 0)
        self.assertEqual(self.luna('timer', 'on').returncode, 0)
        unit_dir = self.home / '.config/systemd/user'
        self.assertTrue((unit_dir / 'luna-autoupdate.timer').is_file())
        self.assertEqual(self.luna('uninstall').returncode, 0)
        self.assertFalse((unit_dir / 'luna-autoupdate.timer').exists(),
                         'uninstall must not leave the worker behind')

    # -- reconcile -----------------------------------------------------
    def render_vdf(self, doc):
        import importlib.util
        spec = importlib.util.spec_from_file_location('luna_mod', str(HERE / 'scripts' / 'luna.py'))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.render_vdf(doc)

    def test_reconcile_merges_newest_lastplayed_and_archives_conflicts(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location('luna_mod', str(HERE / 'scripts' / 'luna.py'))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        config = self.home / '.local/share/Steam/userdata/1/config'
        config.mkdir(parents=True)
        live_apps = {'10': {'LastPlayed': '100', 'Playtime': '5'},
                     '20': {'LastPlayed': '200', 'Playtime': '0'}}
        live = {'UserLocalConfigStore': {'Software': {'Valve': {'Steam': {'Apps': live_apps}}}}}
        (config / 'localconfig.vdf').write_text(module.render_vdf(live))
        old_apps = {'10': {'LastPlayed': '999', 'Playtime': '0'},
                    '30': {'LastPlayed': '50', 'Playtime': '0'}}
        old = {'UserLocalConfigStore': {'Software': {'Valve': {'Steam': {'Apps': old_apps}}}}}
        conflict = config / 'localconfig.sync-conflict-20261001-000000-AAA.vdf'
        conflict.write_text(module.render_vdf(old))
        # The conflict is the older copy; the live file is newest and wins
        # every non-recency key.
        import os as _os, time as _time
        _os.utime(conflict, (_time.time() - 86400, _time.time() - 86400))
        result = self.luna('reconcile', '--dry-run')
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn('would merge', result.stdout)
        self.assertTrue((config / 'localconfig.sync-conflict-20261001-000000-AAA.vdf').is_file(),
                        'dry run must not move anything')
        result = self.luna('reconcile')
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        merged = module.parse_vdf((config / 'localconfig.vdf').read_text())
        apps = merged['UserLocalConfigStore']['Software']['Valve']['Steam']['Apps']
        self.assertEqual(apps['10']['LastPlayed'], '999', 'newest timestamp must win')
        self.assertEqual(apps['10']['Playtime'], '5', 'other keys come from the live file')
        self.assertEqual(apps['20']['LastPlayed'], '200')
        self.assertEqual(apps['30']['LastPlayed'], '50', 'apps only in the conflict must survive')
        archives = sorted(config.glob('localconfig.reconciled-*'))
        self.assertEqual(len(archives), 1)
        self.assertTrue((archives[0] / 'localconfig.sync-conflict-20261001-000000-AAA.vdf').is_file())
        # Idempotent: a second run finds nothing to do.
        self.assertIn('no conflicts', self.luna('reconcile').stdout)

    def test_reconcile_merges_recent_blob_and_seen_state(self):
        import importlib.util, struct
        spec = importlib.util.spec_from_file_location('luna_mod', str(HERE / 'scripts' / 'luna.py'))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        config = self.home / '.local/share/Steam/userdata/2/config'
        config.mkdir(parents=True)
        blob_live = struct.pack('<IIIIII', 10, 0, 100, 0x02000000, 777, 50).hex()
        blob_old = struct.pack('<IIIIII', 10, 0, 90, 20, 0, 60).hex()
        seen_live = '{"version":1,"mapSections":{"A":{"vecSeenIDs":["1:100"]}}}'
        seen_old = '{"version":1,"mapSections":{"A":{"vecSeenIDs":["1:90","2:50"]}}}'
        live = {'UserLocalConfigStore': {'Software': {'Valve': {'Steam': {
            'Apps': {}, 'RecentLocalPlayedGameIDs': blob_live,
            'GamepadHomeWhatsNewSeen_9': seen_live}}}}}
        old = {'UserLocalConfigStore': {'Software': {'Valve': {'Steam': {
            'Apps': {}, 'RecentLocalPlayedGameIDs': blob_old,
            'GamepadHomeWhatsNewSeen_9': seen_old}}}}}
        (config / 'localconfig.vdf').write_text(module.render_vdf(live))
        (config / 'localconfig.sync-conflict-20261001-000000-BBB.vdf').write_text(module.render_vdf(old))
        self.assertEqual(self.luna('reconcile').returncode, 0)
        merged = module.parse_vdf((config / 'localconfig.vdf').read_text())
        steam = merged['UserLocalConfigStore']['Software']['Valve']['Steam']
        entries = module._decode_recent(steam['RecentLocalPlayedGameIDs'])
        by_id = {(kind, ident): stamp for kind, ident, stamp in entries}
        self.assertEqual(by_id[('app', 10)], 100)
        self.assertEqual(by_id[('shortcut', 777)], 50)
        self.assertEqual(by_id[('app', 20)], 60)
        seen = json.loads(steam['GamepadHomeWhatsNewSeen_9'])
        self.assertIn('1:100', seen['mapSections']['A']['vecSeenIDs'])
        self.assertIn('2:50', seen['mapSections']['A']['vecSeenIDs'])

    def test_reconcile_refuses_while_steam_runs(self):
        config = self.home / '.local/share/Steam/userdata/3/config'
        config.mkdir(parents=True)
        (config / 'localconfig.vdf').write_text('"UserLocalConfigStore"\n{\n}\n')
        (config / 'localconfig.sync-conflict-20261001-000000-CCC.vdf').write_text(
            '"UserLocalConfigStore"\n{\n}\n')
        result = self.luna('reconcile', running='steam,')
        self.assertEqual(result.returncode, 2)
        self.assertTrue((config / 'localconfig.sync-conflict-20261001-000000-CCC.vdf').is_file(),
                        'refusal must not move anything')

    def test_backup_restore_round_trip(self):
        (self.data / 'playtime').mkdir(parents=True, exist_ok=True)
        (self.data / 'playtime/1.json').write_text('{"version":2,"games":{}}')
        (self.data / 'presence.json').write_text('{"version":1}')
        out = self.temp / 'backups'
        self.assertEqual(self.luna('backup', '--out', str(out)).returncode, 0)
        archives = sorted(out.glob('luna-backup-*.tar.gz'))
        self.assertEqual(len(archives), 1)
        snapshot = {p.relative_to(self.data): p.read_bytes()
                    for p in sorted(self.data.rglob('*')) if p.is_file()}
        (self.data / 'playtime/1.json').write_text('{"version":2,"games":{"9":{}}}')
        (self.data / 'presence.json').write_text('{"version":1,"owned":{"1":2}}')
        (self.data / 'hltb.json').write_text('{"version":1,"games":{}}')
        # The mutations are newer than the backup, so reverting to the
        # snapshot is explicit (--force); the plain path is covered below.
        result = self.luna('restore', str(archives[0]), '--force')
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        restored = {p.relative_to(self.data): p.read_bytes()
                    for p in sorted(self.data.rglob('*')) if p.is_file()}
        self.assertEqual({k: v for k, v in restored.items() if k in snapshot}, snapshot,
                         'restore must reproduce the backup byte-for-byte')
        self.assertEqual(restored[Path('hltb.json')], b'{"version":1,"games":{}}',
                         'files added after the backup are left alone')

    def test_restore_never_overwrites_newer_without_force(self):
        (self.data / 'presence.json').write_text('{"version":1}')
        out = self.temp / 'backups-newer'
        self.assertEqual(self.luna('backup', '--out', str(out)).returncode, 0)
        archive = next(out.glob('luna-backup-*.tar.gz'))
        import time as _time
        _time.sleep(1.1)
        (self.data / 'presence.json').write_text('{"version":1,"owned":{"1":2}}')
        result = self.luna('restore', str(archive))
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn('newer', result.stdout)
        self.assertIn('"owned"', (self.data / 'presence.json').read_text(),
                      'the newer copy must survive without --force')
        result = self.luna('restore', str(archive), '--force')
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertNotIn('"owned"', (self.data / 'presence.json').read_text())

    def test_restore_refuses_a_tampered_archive(self):
        (self.data / 'presence.json').write_text('{"version":1}')
        out = self.temp / 'backups-tamper'
        self.assertEqual(self.luna('backup', '--out', str(out)).returncode, 0)
        archive = next(out.glob('luna-backup-*.tar.gz'))
        mutated = bytearray(archive.read_bytes())
        mutated[len(mutated) // 2] ^= 0x01
        archive.write_bytes(bytes(mutated))
        (self.data / 'presence.json').write_text('{"version":2}')
        result = self.luna('restore', str(archive))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.data / 'presence.json').read_text(), '{"version":2}',
                         'a tampered archive must change nothing')

    def test_restore_refuses_while_steam_runs(self):
        (self.data / 'presence.json').write_text('{"version":1}')
        out = self.temp / 'backups-running'
        self.assertEqual(self.luna('backup', '--out', str(out)).returncode, 0)
        archive = next(out.glob('luna-backup-*.tar.gz'))
        result = self.luna('restore', str(archive), running='steam,')
        self.assertEqual(result.returncode, 2)
        self.assertIn('running', result.stdout + result.stderr)

    def test_preflight_nudges_reconcile_and_warns_about_peers(self):
        import time as _time
        config = self.home / '.local/share/Steam/userdata/9/config'
        config.mkdir(parents=True)
        (config / 'localconfig.vdf').write_text('"UserLocalConfigStore"\n{\n}\n')
        (config / 'localconfig.sync-conflict-20261001-000000-DDD.vdf').write_text(
            '"UserLocalConfigStore"\n{\n}\n')
        beats = self.data / 'heartbeats'
        beats.mkdir(parents=True)
        beats.joinpath('othermachine.json').write_text(json.dumps(
            {'machine': 'othermachine', 'at': int(_time.time()) - 600,
             'steamRunning': True, 'luna': '0.14.7'}))
        result = self.luna('preflight')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('1 localconfig conflict(s)', result.stdout)
        self.assertIn('luna reconcile', result.stdout)
        self.assertIn('othermachine', result.stdout)
        self.assertIn('both machines', result.stdout)

    def test_check_writes_a_heartbeat(self):
        self.assertEqual(self.luna('install').returncode, 0)
        _, env = self.bundle_and_env()
        self.assertEqual(self.luna('check', '--force', env=env).returncode, 0)
        beats = sorted((self.home / '.local/share/slssteam-integrations/heartbeats').glob('*.json'))
        self.assertEqual(len(beats), 1, 'check must leave exactly our own heartbeat')
        entry = json.loads(beats[0].read_text())
        self.assertIn('at', entry)

    def test_steam_pin_cache_recover_round_trip(self):
        steam = self.home / '.local/share/Steam'
        (steam / 'logs').mkdir(parents=True)
        (steam / 'logs/console_log.txt').write_text('[x] Client version: 111\n[x] Client version: 222\n')
        package = self.steam_fixture('222')
        good = (package / 'test-client.zip').read_bytes()
        sls = self.home / '.local/share/SLSsteam'
        sls.mkdir(parents=True)
        (sls / 'SLSsteam.so').write_text('so-bytes')
        status = self.luna('steam', 'status')
        self.assertEqual(status.returncode, 0)
        self.assertIn('222', status.stdout)
        self.assertIn('none (run: luna steam pin)', status.stdout)
        self.assertEqual(self.luna('steam', 'pin').returncode, 0)
        self.assertIn('pinned', self.luna('steam', 'status').stdout)
        self.assertEqual(self.luna('steam', 'cache').returncode, 0)
        # An unwanted update lands: new build, changed packages.
        (steam / 'logs/console_log.txt').write_text('[x] Client version: 999\n')
        (package / 'test-client.zip').write_text('build-999-bytes')
        (steam / 'ubuntu12_32').mkdir()
        (steam / 'ubuntu12_32/steam').write_text('new executable')
        drifted = self.luna('steam', 'status')
        self.assertIn('pinned : 222', drifted.stdout)
        self.assertIn('DIFFER', drifted.stdout)
        result = self.luna('steam', 'recover')
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual((package / 'test-client.zip').read_bytes(), good,
                          'recover must put the cached packages back')
        self.assertEqual((steam / 'ubuntu12_32/steam').read_bytes(), b'\x7fELFsynthetic-build-222')
        self.assertIn('BootStrapperInhibitAll', (steam / 'steam.cfg').read_text())
        fixed = self.luna('steam', 'status')
        self.assertIn('222', fixed.stdout)
        self.assertIn('match the pin', fixed.stdout)

    def test_steam_recover_refuses_while_steam_runs(self):
        steam = self.home / '.local/share/Steam'
        (steam / 'logs').mkdir(parents=True)
        (steam / 'logs/console_log.txt').write_text('Client version: 222\n')
        (steam / 'package').mkdir(parents=True)
        self.assertEqual(self.luna('steam', 'pin').returncode, 0)
        result = self.luna('steam', 'recover', running='steam,')
        self.assertEqual(result.returncode, 2)
        self.assertIn('running', result.stdout + result.stderr)

    def test_autoupdate_recovers_a_drifted_client_in_install_mode(self):
        import json as _json
        self.assertEqual(self.luna('install').returncode, 0)
        steam = self.home / '.local/share/Steam'
        (steam / 'logs').mkdir(parents=True)
        (steam / 'logs/console_log.txt').write_text('Client version: 222\n')
        package = self.steam_fixture('222')
        good = (package / 'test-client.zip').read_bytes()
        self.assertEqual(self.luna('steam', 'pin').returncode, 0)
        self.assertEqual(self.luna('steam', 'cache').returncode, 0)
        (steam / 'logs/console_log.txt').write_text('Client version: 999\n')
        (package / 'test-client.zip').write_text('bad')
        (self.data / 'luna.json').write_text(_json.dumps({'version': 1, 'auto_update': 'install'}))
        _, env = self.bundle_and_env()
        result = self.luna('autoupdate', '--verbose', env=env)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual((package / 'test-client.zip').read_bytes(), good,
                          'install mode must restore the pinned client first')
        last = _json.loads((self.home / '.local/share/Lumen/sls-luna/autoupdate.json').read_text())
        self.assertEqual(last['steamPinned'], '222')
        self.assertEqual(last['steamDrift'], '')

    def test_steam_pin_blocks_updates_and_unpin_restores(self):
        self.assertEqual(self.luna('steam', 'pin', '--build', '111').returncode, 0)
        cfg = self.home / '.local/share/Steam/steam.cfg'
        self.assertIn('BootStrapperInhibitAll', cfg.read_text())
        self.assertEqual(self.luna('steam', 'unpin').returncode, 0)
        self.assertFalse(cfg.exists(), 'unpin removes exactly the block Luna wrote')
        cfg.write_text('BootStrapperInhibitAll=enable\nCustom=1\n')
        self.assertEqual(self.luna('steam', 'unpin').returncode, 0)
        self.assertTrue(cfg.is_file(), 'unpin must not touch a customized steam.cfg')

    def test_install_reapplies_update_block_when_pinned(self):
        self.assertEqual(self.luna('steam', 'pin', '--build', '111').returncode, 0)
        (self.home / '.local/share/Steam/steam.cfg').unlink()
        self.assertEqual(self.luna('install').returncode, 0)
        self.assertIn('BootStrapperInhibitAll',
                      (self.home / '.local/share/Steam/steam.cfg').read_text())

    def test_preflight_warns_about_pacman_managed_steam(self):
        pbin = self.temp / 'pbin'
        pbin.mkdir(parents=True)
        pacman = pbin / 'pacman'
        pacman.write_text('#!/bin/sh\n[ "$2" = "steam" ] && exit 0\nexit 1\n')
        pacman.chmod(0o755)
        conf = self.home / 'pacman.conf'
        conf.write_text('[options]\n')
        env = dict(self.env, PATH=str(pbin) + os.pathsep + self.env['PATH'],
                   LUNA_PACMAN_CONF=str(conf))
        self.assertEqual(self.luna('steam', 'pin', '--build', '111').returncode, 0)
        out = self.luna('preflight', env=env)
        self.assertIn('IgnorePkg = steam', out.stdout)
        conf.write_text('[options]\nIgnorePkg = steam\n')
        out = self.luna('preflight', env=env)
        self.assertNotIn('IgnorePkg = steam', out.stdout)

    def test_steam_update_queues_and_cancel_clears(self):
        self.assertEqual(self.luna('steam', 'update').returncode, 0)
        pending = json.loads((self.home / '.local/share/Lumen/sls-luna/pending-steam.json').read_text())
        self.assertEqual(pending['action'], 'update')
        result = self.luna('steam', 'update')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('already queued', result.stdout + result.stderr)
        self.assertEqual(self.luna('steam', 'cancel').returncode, 0)
        self.assertFalse((self.home / '.local/share/Lumen/sls-luna/pending-steam.json').exists())
        self.assertEqual(self.luna('steam', 'status').returncode, 0)

    def test_steam_update_now_applies_with_stub_updater(self):
        steam = self.home / '.local/share/Steam'
        (steam / 'logs').mkdir(parents=True)
        (steam / 'logs/console_log.txt').write_text('Client version: 222\n')
        package = steam / 'package'
        package.mkdir(parents=True)
        (package / 'bins.dat').write_text('old-bytes')
        (steam / 'steam.cfg').write_text('BootStrapperInhibitAll=enable\nBootStrapperForceSelfUpdate=disable\n')
        updater = self.home / 'fake-steam.sh'
        updater.write_text('#!/bin/sh\n'
                           'echo "[x] Client version: 999" >> "%s"\n'
                           'echo new-bytes > "%s"\n' % (steam / 'logs/console_log.txt',
                                                        package / 'bins.dat'))
        updater.chmod(0o755)
        env = dict(self.env, LUNA_STEAM_SH=str(updater))
        result = self.luna('steam', 'update', '--now', env=env)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual((package / 'bins.dat').read_text().strip(), 'new-bytes')
        self.assertIn('BootStrapperInhibitAll', (steam / 'steam.cfg').read_text())
        status = self.luna('steam', 'status', env=env)
        self.assertIn('999', status.stdout)

    def test_autoupdate_applies_queued_steam_update(self):
        self.assertEqual(self.luna('install').returncode, 0)
        steam = self.home / '.local/share/Steam'
        (steam / 'logs').mkdir(parents=True)
        (steam / 'logs/console_log.txt').write_text('Client version: 222\n')
        (steam / 'package').mkdir(parents=True)
        updater = self.home / 'fake-steam-auto.sh'
        updater.write_text('#!/bin/sh\necho "[x] Client version: 333" >> "%s"\n'
                           % (steam / 'logs/console_log.txt'))
        updater.chmod(0o755)
        env = dict(self.env, LUNA_STEAM_SH=str(updater))
        self.assertEqual(self.luna('steam', 'update', env=env).returncode, 0)
        (self.data / 'luna.json').write_text(json.dumps({'version': 1, 'auto_update': 'notify'}))
        _, benv = self.bundle_and_env()
        benv = dict(benv, LUNA_STEAM_SH=str(updater))
        result = self.luna('autoupdate', '--verbose', env=benv)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertFalse((self.home / '.local/share/Lumen/sls-luna/pending-steam.json').exists(),
                         'a consumed queue must be cleared')
        last = json.loads((self.home / '.local/share/Lumen/sls-luna/autoupdate.json').read_text())
        self.assertEqual(last['steamBuild'], '333')

    def test_autoupdate_applies_a_queued_refresh_with_steam_closed(self):
        self.assertEqual(self.luna('install').returncode, 0)
        steam = self.home / '.local/share/Steam'
        (steam / 'logs').mkdir(parents=True)
        (steam / 'logs/console_log.txt').write_text('Client version: 222\n')
        package = steam / 'package'
        package.mkdir(parents=True)
        (package / 'bins.dat').write_text('refresh-bytes')
        (package / 'steam_client_ubuntu12.manifest').write_text('"ubuntu12"\n{\n"version"\t\t"222"\n}\n')
        updater = self.home / 'fake-steam-refresh.sh'
        updater.write_text('#!/bin/sh\n'
                          'if [ "$1" = "-shutdown" ]; then exit 0; fi\n'
                          'sed -i \'s/"version"[[:space:]]*"[0-9]*"/"version" "999"/\' "%s"\n'
                          'echo "[x] Client version: 999" >> "%s"\n'
                          % (package / 'steam_client_ubuntu12.manifest',
                             steam / 'logs/console_log.txt'))
        updater.chmod(0o755)
        tools = self.home / '.local/share/Lumen/sls-luna'
        (tools / 'pending-refresh.json').write_text(json.dumps({'at': 1}))
        (self.data / 'luna.json').write_text(json.dumps({'version': 1, 'auto_update': 'off'}))
        _, benv = self.bundle_and_env()
        benv = dict(benv, LUNA_STEAM_SH=str(updater))
        result = self.luna('autoupdate', '--verbose', env=benv)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        last = json.loads((tools / 'autoupdate.json').read_text())
        self.assertIn('999', last.get('refresh', ''), 'the timer must report the refresh')
        self.assertEqual(last['steamPinned'], '999')
        self.assertEqual((tools / 'steam-client-cache/build.txt').read_text().strip(), '999')
        self.assertFalse((tools / 'pending-refresh.json').exists(),
                         'a consumed refresh must be cleared')

    def test_steam_pin_falls_back_to_package_manifest(self):
        steam = self.home / '.local/share/Steam'
        package = steam / 'package'
        package.mkdir(parents=True)
        (package / 'bins.dat').write_text('x')
        (package / 'steam_client_ubuntu12.manifest').write_text('"x"\n{\n"version"\t\t"424242"\n}\n')
        self.assertEqual(self.luna('steam', 'pin').returncode, 0)
        status = self.luna('steam', 'status')
        self.assertIn('424242', status.stdout)
        self.assertIn('match the pin', status.stdout)

    def test_the_standalone_copy_can_manage_its_own_units(self):
        """The timer entry point runs the copy, which must not copy itself from
        a repository that is not there (the live bug found on 2026-10-15)."""
        self.assertEqual(self.luna('install').returncode, 0)
        copy = self.home / '.local/share/Lumen/sls-luna/luna.py'
        self.assertTrue(copy.is_file())
        result = subprocess.run([sys.executable, str(copy), 'timer', 'on'],
                                capture_output=True, text=True, env=self.env)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertTrue(copy.is_file(), 'the copy must survive its own timer action')
        self.assertTrue((self.home / '.config/systemd/user/luna-autoupdate.timer').is_file())
        check = subprocess.run([sys.executable, str(copy), 'check', '--force'],
                               capture_output=True, text=True, env=dict(self.env, LUNA_TEST_TAG='v1.0.0'))
        self.assertEqual(check.returncode, 0, check.stderr)
        self.assertIn('"installed"', check.stdout)



class VendorTestCase(unittest.TestCase):
    """`vendor` rewrites the repository it lives in, so it runs on a copy."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='luna-vendor-')
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name) / 'repo'
        for rel in ('scripts', 'addons', 'vendor', 'bridge'):
            shutil.copytree(HERE / rel, self.repo / rel, ignore=shutil.ignore_patterns('__pycache__'))
        self.git('init', '-q')
        self.git('add', '-A')
        self.git('-c', 'user.name=t', '-c', 'user.email=t@example.invalid', 'commit', '-qm', 'fixture')
        # No sibling checkouts: a run that needs playtime/presence must say so.
        self.env = dict(os.environ, HOME=self.temp.name)

    def git(self, *args):
        subprocess.run(['git', '-C', str(self.repo), *args], check=True, capture_output=True)

    def vendor(self, *args):
        return subprocess.run([sys.executable, str(self.repo / 'scripts/luna.py'), 'vendor', *args],
                              env=self.env, capture_output=True, text=True)

    def test_only_repins_the_named_addon_and_keeps_the_rest(self):
        manifest_path = self.repo / 'vendor/MANIFEST.json'
        before = json.loads(manifest_path.read_text())
        panel = self.repo / 'addons/luna/plugin/public/panel.js'
        panel.write_text(panel.read_text() + '\n// change\n')
        self.git('-c', 'user.name=t', '-c', 'user.email=t@example.invalid', 'commit', '-qam', 'change')
        result = self.vendor('--only', 'luna')
        self.assertEqual(result.returncode, 0, result.stderr)
        after = json.loads(manifest_path.read_text())
        self.assertEqual(after['shared'], before['shared'])
        for name in before['addons']:
            if name != 'luna':
                self.assertEqual(after['addons'][name], before['addons'][name], name)
        self.assertEqual(after['addons']['luna']['files']['public/panel.js']['sha256'],
                         hashlib.sha256(panel.read_bytes()).hexdigest())
        head = subprocess.run(['git', '-C', str(self.repo), 'rev-parse', 'HEAD'],
                              capture_output=True, text=True).stdout.strip()
        self.assertEqual(after['addons']['luna']['source_commit'], head)

    def test_refuses_to_pin_bytes_that_are_not_committed(self):
        manifest_path = self.repo / 'vendor/MANIFEST.json'
        before = manifest_path.read_bytes()
        panel = self.repo / 'addons/luna/plugin/public/panel.js'
        panel.write_text(panel.read_text() + '\n// uncommitted\n')
        result = self.vendor('--only', 'luna')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('commit it before pinning', result.stderr)
        self.assertEqual(manifest_path.read_bytes(), before)


if __name__ == '__main__':
    unittest.main(verbosity=2)
