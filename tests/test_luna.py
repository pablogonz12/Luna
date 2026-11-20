#!/usr/bin/env python3
"""SLS Luna installer tests.

Everything runs against a disposable HOME with a minimal Lumen fixture, so no
real Lumen, Steam or user data is touched. `pgrep` is stubbed so the tests never
depend on whether the developer's own Steam is open.
"""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
LUNA = HERE / 'scripts' / 'luna.py'

# Tests drive real install/update paths; never pop a desktop notification.
os.environ.setdefault('LUNA_NO_NOTIFY', '1')

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
    systemctl.write_text('#!/bin/sh\necho "$*" >> "$HOME/systemctl.log"\n'
                         '[ -n "$LUNA_TEST_SYSTEMCTL_FAIL" ] && case "$*" in *"$LUNA_TEST_SYSTEMCTL_FAIL"*) exit 1;; esac\n'
                         'case "$2" in is-enabled) echo enabled;; is-active) echo active;; esac\nexit 0\n')
    systemctl.chmod(0o755)
    pkill = directory / 'pkill'
    pkill.write_text('#!/bin/sh\necho "pkill $*" >> "$HOME/systemctl.log"\nexit 0\n')
    pkill.chmod(0o755)
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
        (self.data / 'playtime/76561198000000000.json').write_text('{"version":2,"games":{}}')
        self.bin = stub_bin(self.home)
        # A prebuilt luna-host fixture: tests never compile or touch the real host/.
        self.host_src = self.temp / 'host-src'
        (self.host_src / 'bin').mkdir(parents=True)
        (self.host_src / 'bin/luna-host').write_text('#!/bin/sh\nexit 0\n')
        (self.host_src / 'bin/luna-host').chmod(0o755)
        (self.host_src / 'lua/luna').mkdir(parents=True)
        (self.host_src / 'lua/luna/boot.lua').write_text('-- boot')
        self.luna_home = self.home / '.local/share/Luna'
        self.env = dict(os.environ, HOME=str(self.home),
                        LUNA_HOME=str(self.luna_home), LUNA_HOST_SRC=str(self.host_src),
                        XDG_DATA_HOME=str(self.home / '.local/share'),
                        XDG_RUNTIME_DIR=str(self.home / 'run'),
                        PATH=str(self.bin) + os.pathsep + os.environ.get('PATH', ''))

    def tearDown(self):
        shutil.rmtree(self.temp, ignore_errors=True)

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
        # A differing payload is a local edit or drift, not corruption: its own
        # code (6) so scripts can tell it from a broken tree (1).
        self.assertEqual(result.returncode, 6)
        self.assertIn('differs', result.stdout)
        strict = self.luna('status', '--strict')
        self.assertEqual(strict.returncode, 1)

    # -- reliability: lock, rollback, undo, drift -------------------------
    def tools_dir(self):
        return self.home / '.local/share/Lumen/sls-luna'

    def hold_lock(self):
        tools = self.tools_dir()
        tools.mkdir(parents=True, exist_ok=True)
        (tools / 'update.lock').write_text(json.dumps(
            {'pid': os.getpid(), 'at': int(time.time())}))

    def test_install_refuses_while_another_command_holds_the_lock(self):
        self.hold_lock()
        result = self.luna('install')
        self.assertEqual(result.returncode, 5, result.stderr + result.stdout)
        self.assertIn('already running', result.stderr)
        self.assertIn('pid %d' % os.getpid(), result.stderr)
        self.assertFalse((self.addons / 'bridge.lua').exists(), 'nothing may be written while busy')

    def test_timer_skips_quietly_while_another_command_holds_the_lock(self):
        self.assertEqual(self.luna('install').returncode, 0)
        (self.data / 'luna.json').write_text(json.dumps({'version': 1, 'auto_update': 'install'}))
        self.hold_lock()
        result = self.luna('autoupdate')
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        last = json.loads((self.tools_dir() / 'autoupdate.json').read_text())
        self.assertEqual(last['result'], 'deferred')

    def test_force_archives_differing_payloads_before_overwriting(self):
        self.assertEqual(self.luna('install').returncode, 0)
        target = self.addons / 'playtime/public/panel.js'
        target.write_text('// my local hotfix\n')
        result = self.luna('install', '--force')
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn('undo', result.stdout)
        saved = list((self.tools_dir() / 'undo').rglob('panel.js'))
        self.assertTrue(saved, 'the overwritten bytes must be copied aside')
        self.assertEqual(saved[0].read_text(), '// my local hotfix\n')
        self.assertNotEqual(target.read_text(), '// my local hotfix\n')

    def test_status_reports_an_interrupted_install(self):
        self.assertEqual(self.luna('install').returncode, 0)
        marker = self.tools_dir() / 'install-in-progress.json'
        marker.write_text(json.dumps({'pid': 999999, 'at': int(time.time()), 'addons': ['luna']}))
        result = self.luna('status')
        self.assertEqual(result.returncode, 1)
        self.assertIn('last install was interrupted', result.stdout)
        self.assertEqual(self.luna('install').returncode, 0)
        self.assertFalse(marker.exists(), 'a verified install clears the marker')

    def test_keyboard_interrupt_rolls_back_the_payloads(self):
        import importlib.util
        self.assertEqual(self.luna('install').returncode, 0)
        target = self.addons / 'playtime/public/panel.js'
        target.write_text('// pre-install hotfix\n')
        spec = importlib.util.spec_from_file_location('luna_mod_ctrl_c', str(LUNA))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        def interrupt(mode, *extra):
            if mode == 'install':
                raise KeyboardInterrupt()
            return argparse.Namespace(returncode=0, stdout='bridge compatible, not installed', stderr='')

        module.patcher = interrupt
        saved = dict(os.environ)
        os.environ.update(self.env)
        try:
            options = argparse.Namespace(only=[], force=True, dry_run=False, keep_steam_client=True,
                                         no_steam_machine=True, migrate_steam_machine=False)
            with self.assertRaises(KeyboardInterrupt):
                module.cmd_install(options)
        finally:
            os.environ.clear()
            os.environ.update(saved)
        self.assertEqual(target.read_text(), '// pre-install hotfix\n',
                         'a Ctrl-C must put the previous payload back')
        self.assertFalse((self.tools_dir() / 'install-in-progress.json').exists())

    def test_check_is_single_flight(self):
        tools = self.tools_dir()
        tools.mkdir(parents=True, exist_ok=True)
        (tools / 'check.lock').write_text(json.dumps({'pid': os.getpid(), 'at': int(time.time())}))
        result = self.luna('check')
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn('busy', result.stdout)
        (tools / 'check.lock').unlink()
        self.assertEqual(self.luna('check').returncode, 0)
        self.assertFalse((tools / 'check.lock').exists(), 'a finished check releases its lock')

    def test_install_keeps_a_hand_edited_steam_machine_copy(self):
        self.assertEqual(self.luna('install').returncode, 0)
        tools = self.tools_dir()
        installed = tools / 'steam-machine.py'
        installed.write_text(installed.read_text() + '\n# local hotfix\n')
        dry = self.luna('install', '--dry-run')
        self.assertEqual(dry.returncode, 0, dry.stderr + dry.stdout)
        self.assertNotIn('needs attention', dry.stdout + dry.stderr)
        result = self.luna('install')
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertNotIn('needs attention', result.stdout + result.stderr)
        self.assertIn('kept (edited outside Luna)', result.stdout)
        self.assertIn('# local hotfix', installed.read_text())
        self.assertFalse((tools / 'archive').exists())

    def test_install_silently_upgrades_an_untouched_older_steam_machine_copy(self):
        self.assertEqual(self.luna('install').returncode, 0)
        tools = self.tools_dir()
        installed = tools / 'steam-machine.py'
        older = installed.read_text() + '\n# older Luna version\n'
        installed.write_text(older)
        state = json.loads((tools / 'steam-machine-shortcuts.json').read_text())
        state['files'][str(installed)]['sha256'] = hashlib.sha256(older.encode()).hexdigest()
        (tools / 'steam-machine-shortcuts.json').write_text(json.dumps(state))
        result = self.luna('install')
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertNotIn('kept (edited outside Luna)', result.stdout)
        self.assertNotIn('# older Luna version', installed.read_text())

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
            origin = Path(os.environ.get('HOME', str(Path.home()))) / 'Documents/Scripts' / ('slsteam-' + name)
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
                         'host/bin/luna-host', 'host/lua/luna/boot.lua',
                         'bridge/bridge.lua', 'addons/luna/plugin/plugin.json',
                         'addons/playtime/plugin/backend/main.lua'):
            self.assertIn(prefix + expected, names)

    def systemctl_log(self):
        path = self.home / 'systemctl.log'
        return path.read_text().splitlines() if path.is_file() else []

    def test_install_deploys_luna_host_side_by_side(self):
        result = self.luna('install')
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertTrue((self.luna_home / 'bin/luna-host').is_file())
        self.assertTrue(os.access(self.luna_home / 'bin/luna-host', os.X_OK))
        self.assertTrue((self.luna_home / 'lua/luna/boot.lua').is_file())
        self.assertTrue((self.luna_home / 'addons/luna/plugin.json').is_file()
                        or (self.luna_home / 'addons/luna/backend/main.lua').is_file())
        # Lumen keeps working: its sls-addons are still written.
        self.assertTrue(self.installed('luna', 'backend/main.lua'))
        unit = (self.home / '.config/systemd/user/luna-host.service').read_text()
        self.assertIn('# Managed by Luna: host v1', unit)
        self.assertIn('Restart=always', unit)
        self.assertIn('RestartSec=5', unit)
        self.assertIn('Environment=LUNA_HOME=%s' % self.luna_home, unit)
        self.assertIn('Environment=LUNA_LUA_DIR=%s' % (self.luna_home / 'lua'), unit)
        self.assertIn('ExecStart=%s' % (self.luna_home / 'bin/luna-host'), unit)
        # Never enabled by install while Lumen is the active host.
        self.assertFalse([line for line in self.systemctl_log() if 'enable' in line])
        # luatools has no plugin.json in this fixture: no legacy list.
        self.assertFalse((self.luna_home / 'legacy-plugins.json').exists())

    def test_install_lists_luatools_as_a_legacy_plugin_when_present(self):
        (self.home / '.local/share/Lumen/luatools/plugin.json').write_text('{"name":"luatools"}')
        self.assertEqual(self.luna('install').returncode, 0)
        listed = json.loads((self.luna_home / 'legacy-plugins.json').read_text())
        self.assertEqual(listed, [str(self.home / '.local/share/Lumen/luatools')])

    def test_install_keeps_a_custom_host_unit(self):
        unit = self.home / '.config/systemd/user/luna-host.service'
        unit.parent.mkdir(parents=True)
        unit.write_text('[Service]\nExecStart=/bin/true\n')
        result = self.luna('install')
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(unit.read_text(), '[Service]\nExecStart=/bin/true\n')

    def test_host_status_cutover_and_rollback(self):
        self.assertEqual(self.luna('install').returncode, 0)
        status = json.loads(self.luna('host', 'status').stdout)
        self.assertEqual(status['active'], 'lumen')
        self.assertTrue(status['binary']['present'])
        lumen_dir = self.home / '.local/share/Lumen'
        self.assertEqual(status['addons'], str(lumen_dir / 'sls-addons'), 'lumen host loads from sls-addons')
        self.assertEqual(status['state'], str(lumen_dir / 'sls-luna'))
        self.assertEqual(status['lunaHostAddons'], str(self.luna_home / 'addons'))
        self.assertEqual(status['lunaState'], str(self.luna_home / 'state'))
        self.assertIn('enabled', status['units']['luna'])
        # State written while Lumen is the host must follow the cutover.
        tools = self.home / '.local/share/Lumen/sls-luna'
        (tools / 'update-check.json').write_text('{"seen":1}')
        big = tools / 'steam-client-backups'
        (big / '1').mkdir(parents=True)
        (self.home / 'systemctl.log').unlink()
        result = self.luna('host', 'cutover')
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        log = self.systemctl_log()
        self.assertIn('--user disable --now lumen.service', log)
        self.assertIn('--user enable --now luna-host.service', log)
        self.assertEqual(json.loads((self.luna_home / 'host.json').read_text())['active'], 'luna')
        cut = json.loads(self.luna('host', 'status').stdout)
        self.assertEqual(cut['active'], 'luna')
        self.assertEqual(cut['addons'], str(self.luna_home / 'addons'), 'luna-host loads from its own addons')
        self.assertEqual(cut['state'], str(self.luna_home / 'state'))
        state = self.luna_home / 'state'
        self.assertEqual(json.loads((state / 'update-check.json').read_text()), {'seen': 1})
        self.assertTrue((state / 'steam-client-backups').is_symlink())
        self.assertTrue((state / 'luna.py').is_file())
        self.assertTrue((self.home / '.local/share/Lumen').is_dir())
        # The CLI launcher follows the active host.
        launcher = (self.home / '.local/bin/luna').read_text()
        self.assertIn('$LUNA_HOME/state/luna.py', launcher)
        result = self.luna('host', 'rollback')
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn('--user disable --now luna-host.service', self.systemctl_log())
        self.assertEqual(self.systemctl_log()[-1], '--user enable --now lumen.service')
        self.assertEqual(json.loads((self.luna_home / 'host.json').read_text())['active'], 'lumen')
        self.assertTrue((self.home / '.local/share/Lumen').is_dir())

    WRAPPER_SAMPLE = (
        '#!/bin/bash\nLUMEN_DIR="$HOME/.local/share/Lumen"\n'
        'if [ -x "$LUMEN_DIR/lumen" ] && ! pgrep -f "$LUMEN_DIR/lumen" >/dev/null 2>&1; then\n'
        '\ttouch "$HOME/.steam/steam/.cef-enable-remote-debugging" 2>/dev/null || true\n'
        '\techo LAUNCH\nfi\n')

    def make_wrapper(self, text=None):
        wrapper = self.home / '.local/share/SLSsteam/path/steam'
        wrapper.parent.mkdir(parents=True)
        wrapper.write_text(self.WRAPPER_SAMPLE if text is None else text)
        wrapper.chmod(0o755)
        return wrapper

    def run_wrapper(self, wrapper):
        (self.home / '.local/share/Lumen/lumen').write_text('#!/bin/sh\n')
        (self.home / '.local/share/Lumen/lumen').chmod(0o755)
        nopg = self.temp / 'nopgrep'
        nopg.mkdir(exist_ok=True)
        (nopg / 'pgrep').write_text('#!/bin/sh\nexit 1\n')
        (nopg / 'pgrep').chmod(0o755)
        env = dict(self.env, PATH=str(nopg) + os.pathsep + self.env['PATH'])
        return subprocess.run(['bash', str(wrapper)], capture_output=True, text=True, env=env).stdout

    def test_wrapper_patch_is_idempotent_reversible_and_skips_lumen(self):
        wrapper = self.make_wrapper()
        original = wrapper.read_text()
        self.assertEqual(self.run_wrapper(wrapper).strip(), 'LAUNCH')
        self.assertEqual(self.luna('host', 'patch-wrapper').returncode, 0)
        patched = wrapper.read_text()
        self.assertEqual(self.luna('host', 'patch-wrapper').returncode, 0)
        self.assertEqual(wrapper.read_text(), patched)
        self.assertEqual(patched.count('luna-patch: skip'), 1)
        self.assertEqual((wrapper.parent / 'steam.luna-orig').read_text(), original)
        self.assertTrue(json.loads(self.luna('host', 'status').stdout)['wrapperPatched'])
        self.assertEqual(subprocess.run(['bash', '-n', str(wrapper)]).returncode, 0)
        self.assertEqual(self.run_wrapper(wrapper).strip(), 'LAUNCH')  # host.json absent: Lumen active
        self.luna_home.mkdir(parents=True, exist_ok=True)
        (self.luna_home / 'host.json').write_text('{\n  "active": "luna"\n}\n')
        self.assertEqual(self.run_wrapper(wrapper).strip(), '')
        self.assertEqual(self.luna('host', 'unpatch-wrapper').returncode, 0)
        self.assertEqual(wrapper.read_text(), original)
        self.assertFalse(json.loads(self.luna('host', 'status').stdout)['wrapperPatched'])

    def test_wrapper_with_unknown_shape_warns_without_failing_install(self):
        wrapper = self.make_wrapper('#!/bin/bash\n# lumen moved\nexec true\n')
        (self.luna_home).mkdir(parents=True)
        (self.luna_home / 'host.json').write_text('{"active": "luna"}')
        result = self.luna('install')
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn('changed shape', result.stderr)
        self.assertNotIn('luna-patch', wrapper.read_text())

    def test_cutover_patches_wrapper_and_stops_lumen_rollback_restores(self):
        wrapper = self.make_wrapper()
        original = wrapper.read_text()
        self.assertEqual(self.luna('install').returncode, 0)
        self.assertEqual(self.luna('host', 'cutover').returncode, 0)
        self.assertIn('pkill -u %d -x lumen' % os.getuid(), self.systemctl_log())
        self.assertIn('luna-patch', wrapper.read_text())
        # install re-applies the patch after an upstream overwrite
        wrapper.write_text(original)
        self.assertEqual(self.luna('install').returncode, 0)
        self.assertIn('luna-patch', wrapper.read_text())
        self.assertEqual(self.luna('host', 'rollback').returncode, 0)
        self.assertEqual(wrapper.read_text(), original)

    def test_cutover_refuses_while_steam_runs(self):
        self.assertEqual(self.luna('install').returncode, 0)
        (self.home / 'systemctl.log').unlink(missing_ok=True)
        result = self.luna('host', 'cutover', running='steam')
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn('Steam is running', result.stdout)
        self.assertFalse([line for line in self.systemctl_log() if 'disable' in line or 'enable' in line])
        self.assertFalse((self.luna_home / 'host.json').exists())

    def test_cutover_refuses_when_the_host_is_not_installed(self):
        result = self.luna('host', 'cutover')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('luna install', result.stdout)

    def test_failed_cutover_restores_lumen(self):
        self.assertEqual(self.luna('install').returncode, 0)
        env = dict(self.env, LUNA_TEST_SYSTEMCTL_FAIL='enable --now luna-host')
        result = self.luna('host', 'cutover', env=env)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('--user enable --now lumen.service', self.systemctl_log())
        self.assertFalse((self.luna_home / 'host.json').exists())

    def test_tools_dir_follows_host_state(self):
        probe = ('import importlib.util,sys;'
                 's=importlib.util.spec_from_file_location("luna",%r);m=importlib.util.module_from_spec(s);'
                 's.loader.exec_module(m);print(m.luna_tools_dir())' % str(LUNA))
        run = lambda: subprocess.run([sys.executable, '-c', probe], capture_output=True, text=True,
                                     env=self.env).stdout.strip()
        self.assertEqual(run(), str(self.home / '.local/share/Lumen/sls-luna'))
        self.luna_home.mkdir(parents=True)
        (self.luna_home / 'host.json').write_text('{"active": "luna"}')
        self.assertEqual(run(), str(self.luna_home / 'state'))
        (self.luna_home / 'host.json').write_text('not json')
        self.assertEqual(run(), str(self.home / '.local/share/Lumen/sls-luna'))

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
        target = HERE / 'addons/luna/plugin/public/panel.js'
        original = target.read_bytes()
        try:
            target.write_bytes(original + b'\n// dirty\n')
            out = self.temp / 'dist-dirty'
            result = self.luna('release', '--out', str(out))
            self.assertNotEqual(result.returncode, 0, 'a dirty tree must not release')
            self.assertIn('does not match the manifest', result.stdout + result.stderr)
        finally:
            target.write_bytes(original)

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



    def test_plain_install_after_cutover_targets_the_luna_host(self):
        self.assertEqual(self.luna('install').returncode, 0)
        result = self.luna('host', 'cutover')
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        target = self.luna_home / 'addons/playtime/backend/main.lua'
        self.assertTrue(target.is_file())
        target.unlink()
        result = self.luna('install')
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn('host      : luna (standalone)', result.stdout)
        self.assertTrue(target.is_file(), 'a plain install must refresh the Luna host')
        self.assertTrue((self.addons / 'playtime/backend/main.lua').is_file(),
                        'the old Lumen tree is left alone')

    def test_host_json_lumen_is_never_downgraded_by_a_plain_install(self):
        self.luna_home.mkdir(parents=True, exist_ok=True)
        (self.luna_home / 'host.json').write_text('{"active": "lumen"}\n')
        result = self.luna('install')
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn('host      : lumen', result.stdout)
        self.assertEqual(json.loads((self.luna_home / 'host.json').read_text())['active'], 'lumen')
        self.assertTrue(self.installed('playtime', 'backend/main.lua'))


    def test_default_install_is_standalone_and_health_checks_pass(self):
        shutil.rmtree(self.home / '.local/share/Lumen', ignore_errors=True)
        result = self.luna('install')
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn('host      : luna (standalone)', result.stdout)
        self.assertFalse((self.home / '.local/share/Lumen').exists(),
                         'a plain install must not create a Lumen tree')
        self.assertTrue((self.luna_home / 'bin/luna-host').is_file())
        self.assertTrue((self.luna_home / 'addons/playtime/backend/main.lua').is_file())
        self.assertTrue((self.home / '.local/bin/luna').is_file())
        self.assertTrue((self.home / '.config/systemd/user/luna-host.service').is_file())
        self.assertFalse((self.home / '.local/share/SLSsteam/path/steam').exists(),
                         'no wrapper may be patched')
        for cfg in ('steam.cfg', 'Steam.cfg'):
            self.assertFalse((self.home / '.local/share/Steam' / cfg).exists())
        status = self.luna('status')
        self.assertEqual(status.returncode, 0, status.stdout + status.stderr)
        self.assertIn('host       : luna (standalone)', status.stdout)
        doctor = self.luna('doctor')
        self.assertEqual(doctor.returncode, 0, doctor.stdout + doctor.stderr)
        self.assertIn('host       : luna (standalone)', doctor.stdout)
        verify = self.luna('verify')
        self.assertEqual(verify.returncode, 0, verify.stdout + verify.stderr)
        self.assertIn('host       : luna (standalone)', verify.stdout)

    def test_steam_machine_launcher_is_generated_without_luatools(self):
        shutil.rmtree(self.home / '.local/share/Lumen', ignore_errors=True)
        self.assertFalse((self.home / '.local/share/Lumen/luatools').exists())
        result = self.luna('launchers')
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertTrue((self.home / '.local/bin/luna-steam-machine').is_file())
        self.assertTrue((self.home / '.local/share/applications/steamos-1920x1080-60.desktop').is_file())


class LumenUpdateTestCase(unittest.TestCase):
    """`luna lumen`: backup, installer, re-attach. The installer and processes are faked."""

    def setUp(self):
        import importlib.util
        from unittest import mock
        self.mock = mock
        self.temp = Path(tempfile.mkdtemp(prefix='luna-lumen-'))
        self.home = self.temp / 'home'
        self.lumen = self.home / '.local/share/Lumen'
        (self.lumen / 'lua').mkdir(parents=True)
        (self.lumen / 'luatools/backend').mkdir(parents=True)
        (self.lumen / 'luatools/backend/main.lua').write_text('-- present')
        (self.lumen / 'lua/boot.lua').write_text('boot v1 SLS-ADDONS-INTEGRATION')
        (self.lumen / 'lua/about.lua').write_text('about v1 LUNA-LUMEN-UPDATE-BLOCK: begin')
        (self.lumen / 'lumen').write_text('binary v1')
        (self.lumen / 'versions.json').write_text(json.dumps({'lumen': {'tag': 'v2.8'}}))
        self.sls = self.home / '.local/share/SLSsteam'
        (self.sls / 'path').mkdir(parents=True)
        (self.sls / 'path/steam').write_text('wrapper v1')
        (self.sls / 'path/steam.luna-orig').write_text('orig v1')
        (self.sls / 'SLSsteam.so').write_text('so v1')
        os.chmod(self.sls / 'SLSsteam.so', 0o755)
        os.symlink('SLSsteam.so', self.sls / 'link.so')
        (self.sls / 'backup').mkdir()
        (self.sls / 'backup/keep.txt').write_text('installer keeps this')
        (self.lumen / 'luatools/backend/extra.lua').write_text('luatools v1')
        self.cfg = self.home / '.config/SLSsteam'
        self.cfg.mkdir(parents=True)
        (self.cfg / 'config.yaml').write_text('user: settings\n')
        (self.cfg / 'notes.txt').write_text('mine')
        self.steam_home = self.home / '.local/share/Steam'
        self.steam_home.mkdir(parents=True)
        (self.steam_home / 'steam.cfg').write_text('BootStrapperInhibitAll=enable\n')
        patcher = mock.patch.dict(os.environ, {'HOME': str(self.home), 'LUMEN_DIR': str(self.lumen),
                                               'LUNA_HOME': str(self.home / '.local/share/Luna'),
                                               'XDG_DATA_HOME': str(self.home / '.local/share')})
        patcher.start()
        self.addCleanup(patcher.stop)
        spec = importlib.util.spec_from_file_location('luna_lumen_mod', str(LUNA))
        self.m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.m)
        self.steam_running = False
        self.installer_ok = True
        self.patchable = True
        self.installs = []
        self.config_after = {}
        self.wrapper_patched_by_upstream = False

        def running(name):
            return self.steam_running and name in ('steam', 'steamwebhelper')

        def installer(ref, sha, extra):
            self.installs.append(ref)
            if not self.installer_ok:
                return 1
            (self.lumen / 'lua/boot.lua').write_text('boot v2 upstream')
            (self.lumen / 'lua/about.lua').write_text('about v2 upstream')
            (self.lumen / 'lumen').write_text('binary v2')
            (self.lumen / 'versions.json').write_text(json.dumps({'lumen': {'tag': 'v2.9'}}))
            # what upstream replaces or deletes besides lua/lumen:
            shutil.rmtree(self.lumen / 'luatools')
            (self.lumen / 'luatools/backend').mkdir(parents=True)
            (self.lumen / 'luatools/backend/main.lua').write_text('luatools v2')
            for entry in self.sls.iterdir():
                if entry.name != 'backup':
                    shutil.rmtree(entry) if entry.is_dir() and not entry.is_symlink() else entry.unlink()
            (self.sls / 'SLSsteam.so').write_text('so v2')
            (self.sls / 'path').mkdir()
            (self.sls / 'path/steam').write_text('wrapper v2 LUNA-PATCH' if self.wrapper_patched_by_upstream
                                                  else 'wrapper v2')
            (self.steam_home / 'steam.cfg').unlink(missing_ok=True)
            for name, text in self.config_after.items():
                if text is None:
                    (self.cfg / name).unlink()
                else:
                    (self.cfg / name).write_text(text)
            return 0

        def fake_patcher(mode, *extra):
            boot = (self.lumen / 'lua/boot.lua').read_text()
            about = (self.lumen / 'lua/about.lua').read_text()
            if not self.patchable and 'upstream' in boot:
                return subprocess.CompletedProcess([], 1, '', 'anchor not found')
            out = ('bridge installed\n' if 'SLS-ADDONS-INTEGRATION' in boot else 'bridge not installed\n')
            out += 'lumen self-update blocked\n' if 'LUNA-LUMEN-UPDATE-BLOCK' in about else ''
            return subprocess.CompletedProcess([], 0, out, '')

        def install(options):
            self.installs.append('luna-install')
            (self.lumen / 'lua/boot.lua').write_text('boot v2 SLS-ADDONS-INTEGRATION')
            (self.lumen / 'lua/about.lua').write_text('about v2 LUNA-LUMEN-UPDATE-BLOCK: begin')
            return 0

        for name, value in (('process_running', running), ('run_base_installer', installer),
                            ('verify_base_installer', lambda ref, sha: 'f' * 64),
                            ('lumen_upstream_state', lambda *a, **k: {'overall': 'newer', 'components': [], 'channels': {}}),
                            ('patcher', fake_patcher), ('cmd_install', install),
                            ('systemctl_user', lambda *a, **k: subprocess.CompletedProcess([], 0, 'inactive', ''))):
            patch = mock.patch.object(self.m, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    def tearDown(self):
        shutil.rmtree(self.temp, ignore_errors=True)

    def run_lumen(self, action, **kwargs):
        options = argparse.Namespace(lumen_action=action, dry_run=False, yes=False, queue=False,
                                     json=False, check=False, backup='')
        for key, value in kwargs.items():
            setattr(options, key, value)
        return self.m.cmd_lumen(options)

    def test_status_json_reports_versions_and_block(self):
        import io, contextlib
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(self.run_lumen('status', json=True), 0)
        state = json.loads(out.getvalue())
        self.assertEqual(state['host'], 'lumen')
        self.assertEqual(state['versions'], {'lumen': 'v2.8'})
        self.assertTrue(state['selfUpdateBlocked'])
        self.assertEqual(state['bridge'], 'installed')
        self.assertFalse(state['pending'])

    def test_dry_run_prints_the_plan_and_writes_nothing(self):
        import io, contextlib
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(self.run_lumen('update', dry_run=True), 0)
        self.assertIn('would back up', out.getvalue())
        self.assertIn('re-attach Luna', out.getvalue())
        self.assertEqual(self.installs, [])
        self.assertEqual(self.m.lumen_backups(), [])

    def test_update_refuses_while_steam_runs_and_can_queue(self):
        import io, contextlib
        self.steam_running = True
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.run_lumen('update', yes=True), self.m.EXIT_REFUSED)
        self.assertEqual(self.installs, [])
        self.assertEqual(self.run_lumen('update', queue=True), 0)
        self.assertTrue(self.m.read_lumen_pending())
        self.assertEqual(self.installs, [], 'queueing must not run the installer')
        self.assertEqual(self.run_lumen('cancel'), 0)
        self.assertFalse(self.m.read_lumen_pending())

    def test_queued_update_waits_for_steam_then_runs(self):
        self.m.write_json_file(self.m.lumen_pending_path(), {'action': 'update'})
        self.steam_running = True
        extra = {}
        self.assertFalse(self.m.apply_queued_lumen(extra))
        self.assertTrue(extra['lumenPending'])
        self.assertEqual(self.installs, [])
        self.steam_running = False
        self.assertTrue(self.m.apply_queued_lumen(extra))
        self.assertEqual(extra['lumen'], 'applied')
        self.assertFalse(self.m.read_lumen_pending())

    def test_update_reattaches_luna_after_the_installer(self):
        self.assertEqual(self.run_lumen('update', yes=True), 0)
        self.assertEqual(self.installs, [self.m.UPSTREAM_INSTALLER_COMMIT, 'luna-install'], 'pinned installer first, Luna on top')
        self.assertEqual((self.lumen / 'lumen').read_text(), 'binary v2')
        self.assertIn('SLS-ADDONS-INTEGRATION', (self.lumen / 'lua/boot.lua').read_text())
        self.assertIn('LUNA-LUMEN-UPDATE-BLOCK', (self.lumen / 'lua/about.lua').read_text())
        self.assertEqual(self.m.read_json_file(self.m.lumen_result_path())['state'], 'applied')
        self.assertEqual(len(self.m.lumen_backups()), 1)

    def test_unpatchable_upstream_leaves_a_restorable_backup(self):
        import io, contextlib
        self.patchable = False
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertNotEqual(self.run_lumen('update', yes=True), 0)
        self.assertEqual(self.m.read_json_file(self.m.lumen_result_path())['state'], 'failed')
        self.assertNotIn('luna-install', self.installs)
        self.assertTrue(self.m.read_json_file(self.m.lumen_result_path())['rolledBack'])
        self.assertEqual((self.lumen / 'lumen').read_text(), 'binary v1', 'rolled back automatically')
        self.m.mark_lumen_backup_failed(self.m.lumen_backups()[0])
        self.assertEqual(self.run_lumen('restore', yes=True), 0)
        self.assertEqual((self.lumen / 'lumen').read_text(), 'binary v1')
        self.assertIn('SLS-ADDONS-INTEGRATION', (self.lumen / 'lua/boot.lua').read_text())
        self.assertEqual(json.loads((self.lumen / 'versions.json').read_text())['lumen']['tag'], 'v2.8')

    def test_failed_installer_keeps_the_backup(self):
        import io, contextlib
        self.installer_ok = False
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertNotEqual(self.run_lumen('update', yes=True), 0)
        self.assertEqual(len(self.m.lumen_backups()), 1)

    def snapshot(self):
        """Every file/link under the trees the installer touches, with content."""
        out = {}
        for root in (self.lumen, self.sls, self.cfg, self.steam_home):
            for path in sorted(root.rglob('*')):
                rel = str(path)
                if 'sls-luna' in rel or '.luna-' in rel or 'Steam/package' in rel:
                    continue
                if path.is_symlink():
                    out[rel] = 'link:' + os.readlink(path)
                elif path.is_file():
                    out[rel] = (path.read_bytes(), oct(path.stat().st_mode))
        return out

    def test_backup_covers_every_tree_the_installer_touches(self):
        backup = self.m.backup_lumen_tree()
        meta = json.loads((backup / 'backup.json').read_text())
        for item in ('lua', 'lumen', 'versions.json', 'luatools', 'slsteam', 'slsteam-config', 'steam.cfg'):
            self.assertIn(item, meta['items'])
        self.assertIn('update-channels.json', meta['absent'])
        self.assertEqual((backup / 'slsteam/path/steam.luna-orig').read_text(), 'orig v1')
        self.assertTrue((backup / 'slsteam/link.so').is_symlink())
        self.assertEqual(oct((backup / 'slsteam/SLSsteam.so').stat().st_mode & 0o777), '0o755')
        self.assertFalse((backup / 'slsteam/backup').exists(), 'the installer keeps its own backup dir')
        self.assertEqual((backup / 'slsteam-config/config.yaml').read_text(), 'user: settings\n')
        self.assertEqual(self.m.verify_lumen_backup(backup), (True, ''))
        self.assertGreater(meta['files'], 8)

    def test_restore_round_trip_returns_the_exact_pre_update_state(self):
        before = self.snapshot()
        self.assertEqual(self.run_lumen('update', yes=True), 0)
        self.assertNotEqual(self.snapshot(), before)
        self.assertEqual(self.run_lumen('restore', yes=True), 0)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual((self.sls / 'backup/keep.txt').read_text(), 'installer keeps this')

    def test_restore_removes_a_steam_cfg_that_did_not_exist_before(self):
        (self.steam_home / 'steam.cfg').unlink()
        backup = self.m.backup_lumen_tree()
        (self.steam_home / 'steam.cfg').write_text('late')
        ok, _msg = self.m.restore_lumen_backup(backup.name)
        self.assertTrue(ok)
        self.assertFalse((self.steam_home / 'steam.cfg').exists())

    def test_restore_refuses_a_damaged_backup_and_changes_nothing(self):
        backup = self.m.backup_lumen_tree()
        (self.lumen / 'lumen').write_text('binary v-now')
        (backup / 'slsteam/SLSsteam.so').write_text('tampered')
        ok, message = self.m.restore_lumen_backup(backup.name)
        self.assertFalse(ok)
        self.assertIn('manifest', message)
        self.assertEqual((self.lumen / 'lumen').read_text(), 'binary v-now')

    def test_restore_refuses_while_steam_or_lumen_runs(self):
        self.m.backup_lumen_tree()
        self.steam_running = True
        ok, message = self.m.restore_lumen_backup()
        self.assertFalse(ok)
        self.assertIn('close Steam', message)
        self.steam_running = False
        with self.mock.patch.object(self.m, 'process_running', lambda name: name == 'lumen'):
            self.assertFalse(self.m.restore_lumen_backup()[0])

    def test_only_two_backups_survive_a_successful_update(self):
        for index in range(3):
            self.m.backup_lumen_tree()
            time.sleep(0.01)
        # distinct names even within one second
        self.assertEqual(self.run_lumen('update', yes=True), 0)
        self.assertEqual(len(self.m.lumen_backups()), self.m.LUMEN_KEEP_BACKUPS)

    def test_a_failed_updates_backup_is_never_pruned(self):
        import io, contextlib
        self.installer_ok = False
        with contextlib.redirect_stderr(io.StringIO()), \
                self.mock.patch.object(self.m, 'restore_lumen_backup', lambda *a, **k: (False, 'no')):
            self.assertNotEqual(self.run_lumen('update', yes=True), 0)
        failed = [item['name'] for item in self.m.lumen_backup_info() if item['failed']]
        self.assertEqual(len(failed), 1)
        self.installer_ok = True
        for _ in range(3):
            self.assertEqual(self.run_lumen('update', yes=True), 0)
        names = [item['name'] for item in self.m.lumen_backup_info()]
        self.assertIn(failed[0], names)
        self.assertEqual(len(names), self.m.LUMEN_KEEP_BACKUPS + 1)

    def test_removed_user_config_comes_back_and_changed_config_is_only_listed(self):
        import io, contextlib
        self.config_after = {'config.yaml': None, 'notes.txt': 'rewritten by upstream'}
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.run_lumen('update', yes=True), 0)
        self.assertEqual((self.cfg / 'config.yaml').read_text(), 'user: settings\n')
        self.assertEqual((self.cfg / 'notes.txt').read_text(), 'rewritten by upstream', 'never guess')
        self.assertIn('restored (verbatim from the backup): %s' % (self.cfg / 'config.yaml'), out.getvalue())
        self.assertIn('changed by the installer, left as is: %s' % (self.cfg / 'notes.txt'), out.getvalue())
        self.assertIn('configChanged', self.m.read_json_file(self.m.lumen_result_path()))

    def test_wrapper_is_unpatched_for_the_lumen_host_after_an_update(self):
        calls = []
        with self.mock.patch.object(self.m, 'wrapper_patched', lambda: True), \
                self.mock.patch.object(self.m, 'unpatch_wrapper', lambda: calls.append(1)):
            self.assertEqual(self.run_lumen('update', yes=True), 0)
        self.assertEqual(calls, [1])

    def test_dry_run_plan_lists_sizes_for_every_tree(self):
        import io, contextlib
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.run_lumen('update', dry_run=True)
        text = out.getvalue()
        for name in ('luatools', 'slsteam ', 'slsteam-config', 'steam.cfg', 'total'):
            self.assertIn(name, text)
        self.assertIn('installer keeps backup', text)

    def test_status_lists_backups_with_date_and_size(self):
        import io, contextlib
        self.m.backup_lumen_tree()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.run_lumen('status')
        line = [l for l in out.getvalue().splitlines() if l.startswith('backups')][0]
        self.assertRegex(line, r'\d{4}-\d\d-\d\d \d\d:\d\d, [\d.]+[BKMG]')


class LumenChannelsTestCase(unittest.TestCase):
    """`luna lumen channels`: show, set, validate, write atomically, and feed --check."""

    def setUp(self):
        import importlib.util
        from unittest import mock
        self.mock = mock
        self.temp = Path(tempfile.mkdtemp(prefix='luna-channels-'))
        self.home = self.temp / 'home'
        self.lumen = self.home / '.local/share/Lumen'
        (self.lumen / 'lua').mkdir(parents=True)
        (self.lumen / 'luatools/backend').mkdir(parents=True)
        (self.lumen / 'luatools/backend/main.lua').write_text('-- present')
        (self.lumen / 'lua/boot.lua').write_text('boot v1')
        patcher = mock.patch.dict(os.environ, {'HOME': str(self.home), 'LUMEN_DIR': str(self.lumen),
                                               'LUNA_HOME': str(self.home / '.local/share/Luna'),
                                               'XDG_DATA_HOME': str(self.home / '.local/share')})
        patcher.start()
        self.addCleanup(patcher.stop)
        spec = importlib.util.spec_from_file_location('luna_channels_mod', str(LUNA))
        self.m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.m)

    def tearDown(self):
        shutil.rmtree(self.temp, ignore_errors=True)

    def options(self, **kwargs):
        options = argparse.Namespace(lumen_action='channels', channels_action='', channels_value='',
                                     only=[], json=False, check=False, dry_run=False, yes=False)
        for key, value in kwargs.items():
            setattr(options, key, value)
        return options

    def run_channels(self, **kwargs):
        import contextlib
        import io
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = self.m.cmd_lumen(self.options(**kwargs))
        return code, out.getvalue(), err.getvalue()

    def test_show_reports_all_three_and_defaults_to_stable(self):
        code, out, _err = self.run_channels()
        self.assertEqual(code, self.m.EXIT_OK)
        for name in ('lumen', 'plugin', 'slsteam_moon'):
            self.assertRegex(out, r'(?m)^%s\s+stable$' % name)
        self.assertIn('(not written; stable applies)', out)
        self.assertFalse(self.m.lumen_channels_path().exists())

    def test_set_writes_all_three_atomically_and_audits_the_change(self):
        code, out, _err = self.run_channels(channels_action='set', channels_value='beta')
        self.assertEqual(code, self.m.EXIT_OK)
        data = json.loads(self.m.lumen_channels_path().read_text())
        self.assertEqual(data, {'lumen': 'beta', 'plugin': 'beta', 'slsteam_moon': 'beta'})
        # mkstemp + os.replace leaves no scratch file behind
        leftovers = [p.name for p in self.m.lumen_channels_path().parent.iterdir()
                     if p.name.startswith('.update-channels.json')]
        self.assertEqual(leftovers, [])
        audit = self.m.read_json_file(self.m.lumen_channels_audit_path())
        self.assertEqual(audit['state'], 'applied')
        self.assertEqual(audit['changed'], {'lumen': 'beta', 'plugin': 'beta', 'slsteam_moon': 'beta'})
        self.assertIn('channels set to beta', out)
        self.assertEqual(self.m.lumen_channels_effective()['plugin'], 'beta')

    def test_set_only_one_component_leaves_the_others_stable(self):
        code, _out, _err = self.run_channels(channels_action='set', channels_value='beta', only=['plugin'])
        self.assertEqual(code, self.m.EXIT_OK)
        self.assertEqual(json.loads(self.m.lumen_channels_path().read_text()),
                         {'lumen': 'stable', 'plugin': 'beta', 'slsteam_moon': 'stable'})

    def test_set_refuses_an_unknown_value_without_writing(self):
        code, _out, err = self.run_channels(channels_action='set', channels_value='nightly')
        self.assertEqual(code, self.m.EXIT_REFUSED)
        self.assertIn('stable or beta', err)
        self.assertFalse(self.m.lumen_channels_path().exists())

    def test_set_refuses_an_unknown_component_without_writing(self):
        code, _out, err = self.run_channels(channels_action='set', channels_value='beta', only=['moon'])
        self.assertEqual(code, self.m.EXIT_REFUSED)
        self.assertIn('unknown component', err)
        self.assertFalse(self.m.lumen_channels_path().exists())

    def test_upstream_state_compares_against_the_chosen_channel(self):
        self.m.write_json_file(self.m.lumen_channels_path(),
                               {'lumen': 'beta', 'plugin': 'beta', 'slsteam_moon': 'beta'})
        asked = []

        def fetch(repo, beta_path, glob, mode, channel):
            asked.append((repo, channel))
            return {'tag': 'v9', 'id': 1, 'size': 2, 'channel': channel}, ''

        verdict = self.m.lumen_upstream_state(installed={}, fetch=fetch)
        self.assertEqual(sorted({channel for _repo, channel in asked}), ['beta'])
        self.assertEqual({row['channel'] for row in verdict['components']}, {'beta'})
        self.assertEqual(verdict['overall'], 'newer')
        self.assertEqual(verdict['channels']['plugin'], 'beta')

    def test_a_beta_with_no_published_build_says_so_plainly(self):
        import urllib.error
        calls = []

        def fake_public_json(url):
            calls.append(url)
            if 'ref=beta' in url:
                raise urllib.error.HTTPError(url, 404, 'Not Found', {}, None)
            return {'tag_name': 'v3.0', 'assets': [{'id': 7, 'name': 'lumen-linux.zip', 'size': 9}]}

        with self.mock.patch.object(self.m, '_public_json', fake_public_json):
            info, note = self.m._upstream_component('swwayps/lumen', 'dist/lumen-linux.zip',
                                                    r'^lumen-linux\.zip$', 'latest', 'beta')
        self.assertEqual(info['channel'], 'stable')
        self.assertIn('no beta build published', note)

    def test_status_check_compares_against_the_chosen_channel(self):
        self.m.write_json_file(self.m.lumen_channels_path(),
                               {'lumen': 'stable', 'plugin': 'beta', 'slsteam_moon': 'stable'})
        asked = []

        def fetch(repo, beta_path, glob, mode, channel):
            asked.append(channel)
            return {'tag': 'v9', 'id': 1, 'size': 2, 'channel': channel}, ''

        with self.mock.patch.object(self.m, '_upstream_component', fetch), \
                self.mock.patch.object(self.m, 'fetch_base_installer', lambda *a, **k: 'f' * 64), \
                self.mock.patch.object(self.m, 'patcher',
                                       lambda *a, **k: subprocess.CompletedProcess([], 1, '', '')):
            state = self.m.lumen_status(check=True)
        by_name = {row['component']: row['channel'] for row in state['upstream']['components']}
        self.assertEqual(by_name, {'slsteam_moon': 'stable', 'plugin': 'beta', 'lumen': 'stable'})
        self.assertEqual(sorted(asked), ['beta', 'stable', 'stable'])


class TerminalUpdateTestCase(unittest.TestCase):
    """`--terminal`: plan, confirmation, `steam -shutdown`, the emulator argv and the log."""

    def setUp(self):
        import importlib.util
        from unittest import mock
        self.mock = mock
        self.temp = Path(tempfile.mkdtemp(prefix='luna-terminal-'))
        self.home = self.temp / 'home'
        self.lumen = self.home / '.local/share/Lumen'
        for name in ('lua', 'luatools/backend'):
            (self.lumen / name).mkdir(parents=True)
        (self.lumen / 'luatools/backend/main.lua').write_text('-- present')
        (self.lumen / 'lua/boot.lua').write_text('boot v1')
        patcher = mock.patch.dict(os.environ, {'HOME': str(self.home), 'LUMEN_DIR': str(self.lumen),
                                               'LUNA_HOME': str(self.home / '.local/share/Luna'),
                                               'XDG_DATA_HOME': str(self.home / '.local/share'),
                                               'PATH': str(self.temp / 'bin') + os.pathsep + os.environ.get('PATH', '')})
        patcher.start()
        self.addCleanup(patcher.stop)
        spec = importlib.util.spec_from_file_location('luna_terminal_mod', str(LUNA))
        self.m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.m)
        self.calls = []
        self.spawned = []
        self.running = []

    def tearDown(self):
        shutil.rmtree(self.temp, ignore_errors=True)

    def options(self, **kwargs):
        options = argparse.Namespace(dry_run=False, yes=False, terminal=True, only=[], bundle=None, url=None,
                                     base_latest=False, steam_timeout=1, lumen_action='update',
                                     installer_latest=False, reinstall=False, keep_millennium=False,
                                     remove_millennium=False, queue=False, json=False, check=False, backup='')
        for key, value in kwargs.items():
            setattr(options, key, value)
        return options

    def patch_steam(self, processes, clear_on_shutdown=True):
        self.running = list(processes)

        def fake_run(command, *args, **kwargs):
            self.calls.append(list(command))
            if '-shutdown' in command and clear_on_shutdown:
                self.running = []
            return self.mock.Mock(returncode=0)

        def fake_popen(argv, *args, **kwargs):
            self.spawned.append(list(argv))
            return self.mock.Mock(pid=4242)
        for patcher in (self.mock.patch.object(self.m, 'process_running',
                                              lambda name: name in self.running),
                        self.mock.patch.object(self.m.subprocess, 'run', fake_run),
                        self.mock.patch.object(self.m.subprocess, 'Popen', fake_popen)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_terminal_argv_for_every_known_emulator_family(self):
        shell = 'echo hi'
        expected = {
            'foot': ['foot', '-e', 'bash', '-lc', shell],
            'alacritty': ['alacritty', '-e', 'bash', '-lc', shell],
            'kitty': ['kitty', 'bash', '-lc', shell],
            'wezterm': ['wezterm', 'start', '--', 'bash', '-lc', shell],
            'gnome-terminal': ['gnome-terminal', '--', 'bash', '-lc', shell],
            'konsole': ['konsole', '-e', 'bash', '-lc', shell],
            'xterm': ['xterm', '-e', 'bash', '-lc', shell],
        }
        for name, argv in expected.items():
            self.assertEqual(self.m.terminal_argv(name, shell), argv)
        self.assertEqual(self.m.terminal_argv('foot', shell, ['/usr/bin/foot'])[0], '/usr/bin/foot')

    def test_terminal_discovery_respects_TERMINAL_and_the_path_order(self):
        with self.mock.patch.dict(os.environ, {'TERMINAL': '/opt/bin/wezterm --class luna'}):
            self.assertEqual(
                self.m.find_terminal(which=lambda name: name if name.endswith('wezterm') else None),
                ('wezterm', ['/opt/bin/wezterm', '--class', 'luna']))
        with self.mock.patch.dict(os.environ, {'TERMINAL': ''}):
            self.assertEqual(
                self.m.find_terminal(which=lambda name: '/usr/bin/alacritty' if name == 'alacritty' else None),
                ('alacritty', ['/usr/bin/alacritty']))
            self.assertEqual(self.m.find_terminal(which=lambda name: None), ('', []))

    def test_update_all_terminal_asks_steam_to_quit_then_opens_a_window(self):
        import contextlib
        import io
        self.m.write_json_file(self.m.steam_pin_path(), {'clientBuild': '1620000000'})
        self.patch_steam(['steam', 'steamwebhelper'])
        with self.mock.patch.object(self.m, 'find_terminal', lambda which=None: ('foot', ['/usr/bin/foot'])):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = self.m.cmd_update_all_terminal(self.options(yes=True))
        text = out.getvalue()
        self.assertEqual(code, self.m.EXIT_OK)
        self.assertEqual(self.calls, [['steam', '-shutdown']], 'Steam is asked, never killed')
        self.assertEqual(len(self.spawned), 1)
        argv = self.spawned[0]
        self.assertEqual(argv[:4], ['/usr/bin/foot', '-e', 'bash', '-lc'])
        shell = argv[4]
        self.assertIn('update-all', shell)
        self.assertIn('--yes', shell)
        self.assertNotIn('--terminal', shell)
        self.assertIn('tee -a', shell)
        self.assertIn(str(self.m.luna_state_dir() / 'update-'), shell)
        logs = list(self.m.luna_state_dir().glob('update-*.log'))
        self.assertEqual(len(logs), 1)
        self.assertIn('luna: ', logs[0].read_text())
        self.assertIn('Steam client: unchanged (pinned 1620000000)', text)
        self.assertIn('Steam quit', text)
        self.assertIn('log       : %s' % logs[0], text)

    def test_lumen_update_terminal_runs_the_same_command_without_the_flag(self):
        import contextlib
        import io
        self.patch_steam(['steam'])
        with self.mock.patch.object(self.m, 'find_terminal', lambda which=None: ('kitty', ['/usr/bin/kitty'])):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = self.m.cmd_lumen_update_terminal(self.options(yes=True, lumen_action='update',
                                                                     installer_latest=True))
        self.assertEqual(code, self.m.EXIT_OK)
        self.assertEqual(self.calls, [['steam', '-shutdown']])
        shell = self.spawned[0][-1]
        self.assertIn('lumen update --yes', shell)
        self.assertIn('--installer-latest', shell)
        self.assertNotIn('--terminal', shell)
        text = out.getvalue()
        self.assertIn('Steam client: unchanged', text)
        self.assertIn('would run the upstream MAIN installer', text)

    def test_terminal_refuses_when_steam_will_not_quit_and_never_kills_it(self):
        import contextlib
        import io
        self.patch_steam(['steam', 'steamwebhelper'], clear_on_shutdown=False)
        with self.mock.patch.object(self.m, 'find_terminal', lambda which=None: ('foot', ['/usr/bin/foot'])), \
                self.mock.patch.object(self.m.time, 'sleep', lambda _seconds: None):
            err = io.StringIO()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                code = self.m.cmd_update_all_terminal(self.options(yes=True))
        self.assertEqual(code, self.m.EXIT_REFUSED)
        self.assertEqual(self.calls, [['steam', '-shutdown']])
        self.assertIn('still running', err.getvalue())
        self.assertEqual(self.spawned, [])
        self.assertFalse([call for call in self.calls
                          if any(word in part for part in call for word in ('kill', 'pkill', '-9'))])

    def test_terminal_needs_confirmation_without_yes(self):
        import contextlib
        import io
        self.patch_steam([])
        with self.mock.patch.object(self.m.sys.stdin, 'isatty', lambda: False), \
                self.mock.patch.object(self.m.sys.stdout, 'isatty', lambda: False):
            err = io.StringIO()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                code = self.m.cmd_update_all_terminal(self.options())
        self.assertEqual(code, self.m.EXIT_REFUSED)
        self.assertIn('needs confirmation', err.getvalue())
        self.assertEqual(self.calls, [], 'nothing may be run before confirmation')
        self.assertEqual(self.spawned, [])

    def test_terminal_refuses_when_a_job_already_holds_the_lock(self):
        import contextlib
        import io
        with self.mock.patch.object(self.m, '_update_lock_held_by_other', lambda: True):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                code = self.m.cmd_update_all_terminal(self.options(yes=True))
        self.assertEqual(code, self.m.EXIT_BUSY)
        self.assertIn('REFUSING', err.getvalue())
        self.assertEqual(self.calls, [])

    def test_run_update_in_terminal_falls_back_without_an_emulator(self):
        recorded = []

        def fake_call(command, *args, **kwargs):
            recorded.append(list(command))
            return 0
        log = self.home / 'state' / 'fallback.log'
        with self.mock.patch.object(self.m, 'find_terminal', lambda which=None: ('', [])), \
                self.mock.patch.object(self.m.sys.stdin, 'isatty', lambda: False), \
                self.mock.patch.object(self.m.sys.stdout, 'isatty', lambda: False), \
                self.mock.patch.object(self.m.subprocess, 'call', fake_call):
            used, how, path, argv = self.m.run_update_in_terminal(['/bin/true'], log_path=log)
        self.assertEqual(used, 'subprocess')
        self.assertIn('no terminal emulator found', how)
        self.assertIn('without a window', how)
        self.assertEqual(argv, [])
        self.assertEqual(recorded, [['/bin/true']])
        self.assertTrue(path.is_file())

    def test_run_update_in_terminal_uses_this_terminal_when_one_is_available(self):
        recorded = []

        def fake_call(command, *args, **kwargs):
            recorded.append(list(command))
            return 3
        with self.mock.patch.object(self.m, 'find_terminal', lambda which=None: ('', [])), \
                self.mock.patch.object(self.m.sys.stdin, 'isatty', lambda: True), \
                self.mock.patch.object(self.m.sys.stdout, 'isatty', lambda: True), \
                self.mock.patch.object(self.m.subprocess, 'call', fake_call):
            used, how, _path, _argv = self.m.run_update_in_terminal(['/bin/true'])
        self.assertEqual(used, 'current-terminal')
        self.assertIn('ran in this terminal (exit 3)', how)
        self.assertEqual(recorded[0][:2], ['bash', '-lc'])

    def test_update_all_plan_says_the_steam_client_is_unchanged(self):
        import contextlib
        import io
        self.m.write_json_file(self.m.steam_pin_path(), {'clientBuild': '1620000000'})
        out = io.StringIO()
        with self.mock.patch.object(self.m, 'base_state', lambda: 'ready'), contextlib.redirect_stdout(out):
            self.assertEqual(self.m.cmd_update_all(self.options(dry_run=True, terminal=False)), 0)
        text = out.getvalue()
        self.assertIn('Steam client: unchanged (pinned 1620000000)', text)
        self.assertIn('update-all: would', text)
        self.assertFalse(self.m.steam_pending_path().exists(), 'update-all must queue no Steam client work')
        self.assertEqual(self.m.read_steam_pending(), {})


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
