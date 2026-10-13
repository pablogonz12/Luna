#!/usr/bin/env python3
"""SLS Luna installer tests.

Everything runs against a disposable HOME with a minimal Lumen fixture, so no
real Lumen, Steam or user data is touched. `pgrep` is stubbed so the tests never
depend on whether the developer's own Steam is open.
"""
import json
import os
import shutil
import subprocess
import sys
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


if __name__ == '__main__':
    unittest.main(verbosity=2)
