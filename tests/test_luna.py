#!/usr/bin/env python3
"""SLS Luna installer tests.

Everything runs against a disposable HOME with a minimal Lumen fixture, so no
real Lumen, Steam or user data is touched. `pgrep` is stubbed so the tests never
depend on whether the developer's own Steam is open.
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
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
    # gh: what the updater uses. `release view` reports $LUNA_TEST_TAG and
    # `release download` copies $LUNA_TEST_BUNDLE to the requested --dir.
    gh = directory / 'gh'
    gh.write_text('''#!/bin/sh
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
    ;;
  *) exit 0 ;;
esac
''')
    gh.chmod(0o755)
    # systemctl: the timer tests only need the calls to succeed.
    systemctl = directory / 'systemctl'
    systemctl.write_text('#!/bin/sh\ncase "$2" in is-enabled) echo enabled;; is-active) echo active;; esac\nexit 0\n')
    systemctl.chmod(0o755)
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
        (self.data / 'playtime/76561190000000000.json').write_text('{"version":2,"games":{}}')
        self.bin = stub_bin(self.home)
        self.env = dict(os.environ, HOME=str(self.home),
                        PATH=str(self.bin) + os.pathsep + os.environ.get('PATH', ''))

    def tearDown(self):
        shutil.rmtree(self.temp, ignore_errors=True)

    def luna(self, *args, running='', env=None):
        stub_bin(self.home, running)
        environment = dict(env or self.env)
        return subprocess.run([sys.executable, str(LUNA)] + list(args),
                              capture_output=True, text=True, env=environment)

    # -- helpers ---------------------------------------------------------
    def installed(self, name, rel):
        return (self.addons / name / rel).is_file()

    def data_snapshot(self):
        return {str(path.relative_to(self.data)): path.read_bytes()
                for path in sorted(self.data.rglob('*')) if path.is_file()}

    # -- install ---------------------------------------------------------
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

    def test_dry_run_writes_nothing(self):
        result = self.luna('install', '--dry-run')
        self.assertEqual(result.returncode, 0)
        self.assertIn('DRY RUN', result.stdout)
        self.assertFalse(self.addons.exists())
        self.assertEqual(self.boot.read_text(), UPSTREAM)

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
        target = HERE / 'addons/presence/plugin/public/panel.js'
        original = target.read_bytes()
        try:
            target.write_bytes(b'// tampered\n')
            result = self.luna('verify')
            self.assertEqual(result.returncode, 1)
            self.assertIn('does not match the manifest', result.stdout)
        finally:
            target.write_bytes(original)

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
        for expected in ('vendor/MANIFEST.json', 'scripts/luna.py', 'scripts/patch-lumen.py',
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


if __name__ == '__main__':
    unittest.main(verbosity=2)
