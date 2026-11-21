#!/usr/bin/env python3
"""The fresh-machine pipeline: Steam pin+lock, pinned Lumen base, then Luna.

Every test runs in its own throwaway HOME with fake steam/systemctl/pgrep and
a fake upstream installer. Nothing here reaches the network, the real HOME or
the real systemd user manager.
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LUNA = ROOT / 'scripts/luna.py'
KNOWN_GOOD = '1792618285'

# The upstream boot.lua layout patch-lumen.py accepts (same fixture as test_luna).
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

ABOUT = """local about = {}
function about.update_script(...)
  return true
end
return about
"""


class PipelineTestCase(unittest.TestCase):
    def setUp(self):
        self.maxDiff = None
        self.temp = Path(tempfile.mkdtemp(prefix='luna-pipeline-'))
        self.home = self.temp / 'home'
        self.home.mkdir()
        self.bin = self.home / 'test-bin'
        self.bin.mkdir()
        self.fake('pgrep', '#!/bin/sh\nname="$4"\nexit 1\n')
        self.fake('pkill', '#!/bin/sh\nexit 0\n')
        self.fake('niri', '#!/bin/sh\nexit 1\n')
        self.fake('pacman', '#!/bin/sh\nexit 1\n')
        self.fake('systemctl', '#!/bin/sh\necho "$*" >> "$HOME/systemctl.log"\n'
                               'case "$2" in is-enabled) echo enabled;; is-active) echo active;; esac\n'
                               'exit 0\n')
        # Steam: installed, package/ present, a log of every launcher invocation.
        self.steam = self.home / '.local/share/Steam'
        (self.steam / 'package').mkdir(parents=True)
        self.fake_steam = self.steam / 'steam.sh'
        self.fake_steam.write_text(
            '#!/bin/sh\necho "$*" >> "$HOME/steam-invocations.log"\nexit 0\n')
        self.fake_steam.chmod(0o755)
        (self.steam / 'package/steam_client_ubuntu12.manifest').write_text(
            '"ubuntu12" { "version" "%s" "client" { "file" "x.zip" "sha2" "0" } }\n' % KNOWN_GOOD)
        (self.steam / 'package/x.zip').write_bytes(b'')
        # luna-host fixture: the pipeline never compiles host/.
        self.host_src = self.temp / 'host-src'
        (self.host_src / 'bin').mkdir(parents=True)
        (self.host_src / 'bin/luna-host').write_text('#!/bin/sh\nexit 0\n')
        (self.host_src / 'bin/luna-host').chmod(0o755)
        (self.host_src / 'lua/luna').mkdir(parents=True)
        (self.host_src / 'lua/luna/boot.lua').write_text('-- boot\n')
        self.installer_ok = self.make_installer('ok')
        self.installer_fail = self.make_installer('fail')
        self.installer_partial = self.make_installer('partial')
        self.env = dict(
            os.environ, HOME=str(self.home),
            XDG_DATA_HOME=str(self.home / '.local/share'),
            XDG_CONFIG_HOME=str(self.home / '.config'),
            XDG_STATE_HOME=str(self.home / '.local/state'),
            XDG_RUNTIME_DIR=str(self.home / 'run'),
            PATH=str(self.bin) + os.pathsep + os.environ.get('PATH', ''),
            LUNA_NO_NOTIFY='1', LUNA_HOST_SRC=str(self.host_src),
            # Any accidental HTTP/S attempt dies on localhost: tests stay offline.
            http_proxy='http://127.0.0.1:9', https_proxy='http://127.0.0.1:9',
            HTTP_PROXY='http://127.0.0.1:9', HTTPS_PROXY='http://127.0.0.1:9')

    def tearDown(self):
        shutil.rmtree(self.temp, ignore_errors=True)

    # -- fixtures --------------------------------------------------------
    def fake(self, name, body):
        path = self.bin / name
        path.write_text(body)
        path.chmod(0o755)

    def make_installer(self, action):
        """A local fake `swwayps/luatools-moon` installer (the LUNA_TEST_INSTALLER hook)."""
        helper = self.temp / ('installer_%s.py' % action)
        helper.write_text('''#!/usr/bin/env python3
import os, pathlib, sys
home = pathlib.Path(os.environ["HOME"])
with open(home / "lumen-installer-args.log", "a") as log:
    log.write(" ".join(sys.argv[1:]) + "\\n")
action = {action!r}
if action == "fail":
    print("fake installer failing", file=sys.stderr)
    sys.exit(7)
lumen = home / ".local/share/Lumen"
(lumen / "lua").mkdir(parents=True, exist_ok=True)
(lumen / "lua/boot.lua").write_text({upstream})
(lumen / "lua/about.lua").write_text({about})
if action != "partial":
    (lumen / "luatools/backend").mkdir(parents=True, exist_ok=True)
    (lumen / "luatools/backend/main.lua").write_text("-- backend")
    (home / ".local/share/SLSsteam/path").mkdir(parents=True, exist_ok=True)
    (home / ".local/share/SLSsteam/path/steam").write_text("")
# A real base install drops Steam's update cfg; the pipeline must re-assert it.
for name in ("steam.cfg", "Steam.cfg"):
    (home / ".local/share/Steam" / name).unlink(missing_ok=True)
'''.format(action=action, upstream=repr(UPSTREAM), about=repr(ABOUT)))
        script = self.temp / ('installer_%s.sh' % action)
        script.write_text('#!/bin/sh\nexec python3 %s "$@"\n' % helper)
        script.chmod(0o755)
        return script

    def luna(self, *args, installer=None, env=None):
        environment = dict(env or self.env)
        environment['LUNA_TEST_INSTALLER'] = str(installer or self.installer_ok)
        return subprocess.run([sys.executable, str(LUNA)] + list(args),
                              capture_output=True, text=True, env=environment, timeout=180)

    def bootstrap(self, *extra, installer=None, lumen=True):
        if lumen:
            extra = tuple(extra) + ('--lumen',)
        return self.luna('bootstrap', '--no-system-packages', *extra, installer=installer)

    # -- helpers ---------------------------------------------------------
    def tree(self, roots=None):
        """(relative path, size, mtime_ns) for every file under the chosen roots."""
        roots = roots or [self.home]
        found = {}
        for root in roots:
            for path in sorted(Path(root).rglob('*')):
                if path.is_file():
                    stat = path.stat()
                    found[str(path)] = (stat.st_size, stat.st_mtime_ns,
                                        hashlib.sha256(path.read_bytes()).hexdigest())
        return found

    def installed_tree(self, content_only=False):
        """Only the things an install owns (state/timestamps excluded)."""
        roots = [self.home / '.local/share/Lumen/sls-addons',
                 self.home / '.local/share/Luna/bin',
                 self.home / '.local/share/Luna/lua',
                 self.home / '.config/systemd/user']
        found = {key: value for key, value in self.tree(roots).items() if Path(key).exists()}
        if content_only:  # a rerun may rewrite identical bytes with a new mtime
            # luna-manifest.json intentionally carries installedAt; it is not a payload.
            return {key: (value[0], value[2]) for key, value in found.items()
                    if not key.endswith('luna-manifest.json')}
        return found

    def pin(self):
        return json.loads((self.home / '.local/share/Lumen/sls-luna/steam-pin.json').read_text())

    # -- tests -----------------------------------------------------------
    def test_fresh_run_orders_steam_then_lumen_then_luna(self):
        result = self.bootstrap('--slsteam-channel', 'beta')
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output)
        steam = output.index('verified: Steam client pinned to %s' % KNOWN_GOOD)
        lumen = output.index('verified: Lumen boot.lua')
        luna = output.index('verified: bridge installed, Lumen self-update blocked')
        self.assertLess(steam, lumen)
        self.assertLess(lumen, luna)
        # The pinned base is called with the owner's channels (CLI on top of stable).
        args = (self.home / 'lumen-installer-args.log').read_text().strip()
        self.assertIn('--nolaunch', args)
        self.assertIn('--slsteam-channel beta', args)
        self.assertIn('--plugin-channel stable', args)
        self.assertIn('--lumen-channel stable', args)
        # Gates: pin + both cfg files, base files, bridge + block + timer.
        self.assertEqual(self.pin()['clientBuild'], KNOWN_GOOD)
        self.assertIn('hardLock', self.pin())
        for name in ('steam.cfg', 'Steam.cfg'):
            self.assertIn('BootStrapperInhibitAll=enable', (self.steam / name).read_text())
        self.assertTrue((self.home / '.local/share/Lumen/luatools/backend/main.lua').is_file())
        self.assertIn('SLS-ADDONS-INTEGRATION',
                      (self.home / '.local/share/Lumen/lua/boot.lua').read_text())
        self.assertIn('LUNA-LUMEN-UPDATE-BLOCK',
                      (self.home / '.local/share/Lumen/lua/about.lua').read_text())
        self.assertTrue((self.home / '.config/systemd/user/luna-autoupdate.timer').is_file())

    def test_channels_file_is_honoured_when_present(self):
        lumen = self.home / '.local/share/Lumen'
        lumen.mkdir(parents=True)
        (lumen / 'update-channels.json').write_text('{"plugin": "beta"}')
        result = self.bootstrap()
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        args = (self.home / 'lumen-installer-args.log').read_text()
        self.assertIn('--plugin-channel beta', args)
        self.assertIn('--lumen-channel stable', args)

    def test_failed_lumen_step_stops_without_luna_and_prints_resume(self):
        result = self.bootstrap(installer=self.installer_fail)
        output = result.stdout + result.stderr
        self.assertNotEqual(result.returncode, 0, output)
        self.assertIn('STOPPED at Lumen', output)
        self.assertIn('resume with: luna bootstrap --no-system-packages', output)
        self.assertFalse((self.home / '.local/share/Lumen/sls-addons').exists())
        self.assertFalse((self.home / '.local/share/Luna/bin/luna-host').exists())
        self.assertFalse((self.home / '.config/systemd/user/luna-autoupdate.timer').exists())
        self.assertNotIn('installed: luna', output)
        # The Steam step completed before the base: its pin is still there.
        self.assertEqual(self.pin()['clientBuild'], KNOWN_GOOD)

    def test_lumen_gate_rejects_a_base_without_luatools(self):
        result = self.bootstrap(installer=self.installer_partial)
        output = result.stdout + result.stderr
        self.assertNotEqual(result.returncode, 0, output)
        self.assertIn('STOPPED at Lumen', output)
        self.assertIn('LuaTools backend', output)
        self.assertFalse((self.home / '.local/share/Lumen/sls-addons').exists())

    def test_rerun_is_a_noop_and_keeps_the_steam_pin(self):
        first = self.bootstrap()
        self.assertEqual(first.returncode, 0, first.stderr + first.stdout)
        before = self.installed_tree(content_only=True)
        invocations = (self.home / 'steam-invocations.log').read_text().splitlines()
        second = self.bootstrap()
        output = second.stdout + second.stderr
        self.assertEqual(second.returncode, 0, output)
        self.assertIn('hard lock already active', output)
        self.assertIn('base already installed; kept', output)
        self.assertIn('Already installed and identical', output)
        after = self.installed_tree(content_only=True)
        self.assertEqual(sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k)), [])
        self.assertEqual(self.pin()['clientBuild'], KNOWN_GOOD)
        # The Steam launcher is only ever asked for the pinned ubuntu12 client.
        self.assertEqual(invocations, (self.home / 'steam-invocations.log')
                         .read_text().splitlines())
        for call in invocations:
            self.assertIn('ubuntu12', call)
            self.assertNotIn('latest', call)

    def test_bootstrap_without_flag_installs_only_the_luna_host(self):
        result = self.bootstrap(lumen=False)
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output)
        self.assertFalse((self.home / '.local/share/Lumen').exists(), output)
        self.assertTrue((self.home / '.local/share/Luna/bin/luna-host').is_file())
        self.assertTrue((self.home / '.local/share/Luna/addons/luna/backend/main.lua').is_file())
        self.assertFalse((self.home / 'lumen-installer-args.log').exists())
        for name in ('steam.cfg', 'Steam.cfg'):
            self.assertFalse((self.steam / name).exists())
        self.assertIn('host: luna (standalone)', output)

    def test_install_lumen_flag_runs_steam_then_lumen_then_luna(self):
        result = self.luna('install', '--lumen', '--no-steam-machine',
                           installer=self.installer_ok)
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output)
        steam = output.index('verified: Steam client pinned to %s' % KNOWN_GOOD)
        lumen = output.index('verified: Lumen boot.lua')
        luna = output.index('installed:')
        self.assertLess(steam, lumen)
        self.assertLess(lumen, luna)
        self.assertTrue((self.home / 'lumen-installer-args.log').is_file())
        self.assertEqual(json.loads((self.home / '.local/share/Luna/host.json').read_text())['active'],
                         'lumen')
        self.assertTrue((self.home / '.local/share/Lumen/sls-addons/luna/backend/main.lua').is_file())
        self.assertTrue((self.home / '.local/share/Luna/bin/luna-host').is_file())

    def test_dry_run_writes_nothing(self):
        before = self.tree()
        result = self.bootstrap('--dry-run')
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn('DRY RUN: nothing was written.', result.stdout)
        self.assertEqual(before, self.tree())
        self.assertFalse((self.home / 'lumen-installer-args.log').exists())
        self.assertFalse((self.home / 'steam-invocations.log').exists())

    # -- an older install left behind ------------------------------------
    def old_install_layout(self):
        """A machine with a three-day-old Luna: Lumen tree, pre-runtime luna.py, host lumen."""
        lumen = self.home / '.local/share/Lumen'
        for rel, body in (('lua/boot.lua', UPSTREAM), ('lua/about.lua', ABOUT),
                          ('luatools/backend/main.lua', '-- backend\n')):
            (lumen / rel).parent.mkdir(parents=True, exist_ok=True)
            (lumen / rel).write_text(body)
        (self.home / '.local/share/SLSsteam/path').mkdir(parents=True, exist_ok=True)
        (self.home / '.local/share/SLSsteam/path/steam').write_text('')
        # The copy an older run left: it takes ROOT from the Lumen tree, and no runtime/
        # tree sits beside it. That is the layout behind the MANIFEST.json FAIL line.
        (lumen / 'sls-luna').mkdir(parents=True, exist_ok=True)
        shutil.copy2(LUNA, lumen / 'sls-luna/luna.py')
        (self.home / '.local/share/Luna').mkdir(parents=True, exist_ok=True)
        (self.home / '.local/share/Luna/host.json').write_text('{"active": "lumen"}\n')
        return lumen

    def test_bootstrap_over_an_old_install_replaces_the_stale_copy(self):
        lumen = self.old_install_layout()
        tools = lumen / 'sls-luna'
        # The installed copy on its own is exactly the reported failure: it looks for
        # vendor/MANIFEST.json under Lumen, where no checkout ever puts it.
        stale = subprocess.run([sys.executable, str(tools / 'luna.py'), 'bootstrap',
                                '--no-system-packages', '--keep-steam-client'],
                               capture_output=True, text=True, env=self.env, timeout=180)
        self.assertNotEqual(stale.returncode, 0)
        # It is refused before it can look for pins that are not under Lumen at all.
        self.assertIn('current tooling', stale.stderr)
        self.assertNotIn('vendor-update.sh', stale.stderr)
        result = self.bootstrap()
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output)
        # The current code was installed first, and the stale copy was replaced by it.
        self.assertIn('installed the current tooling first', output)
        self.assertEqual((tools / 'luna.py').read_bytes(), LUNA.read_bytes())
        self.assertEqual(json.loads((self.home / '.local/share/Luna/host.json').read_text())['active'],
                         'lumen')
        # The pinned payloads the run used now live in the installed copy's runtime tree.
        runtime = tools / 'runtime'
        self.assertTrue((runtime / 'vendor/MANIFEST.json').is_file())
        manifest = json.loads((runtime / 'RUNTIME.json').read_text())
        self.assertEqual(manifest['lunaSha256'], hashlib.sha256(LUNA.read_bytes()).hexdigest())
        self.assertTrue((runtime / 'scripts/patch-lumen.py').is_file())
        self.assertTrue((runtime / 'addons/luna/plugin/backend/main.lua').is_file())
        self.assertTrue((lumen / 'sls-addons/luna/backend/main.lua').is_file())
        # And the replaced copy serves the machine, instead of failing on its own pins.
        served = subprocess.run([sys.executable, str(tools / 'luna.py'), 'status'],
                                capture_output=True, text=True, env=self.env, timeout=180)
        self.assertEqual(served.returncode, 0, served.stdout + served.stderr)
        self.assertNotIn('vendor-update.sh', served.stderr)

    def test_steam_running_stops_bootstrap_before_any_write_on_both_hosts(self):
        self.old_install_layout()
        self.fake('pgrep', '#!/bin/sh\nexit 0\n')  # Steam and steamwebhelper are "up"
        for host in ('lumen', 'luna'):
            (self.home / '.local/share/Luna/host.json').write_text(
                '{"active": "%s"}\n' % host)
            before = self.tree()
            result = self.bootstrap(lumen=(host == 'lumen'))
            output = result.stdout + result.stderr
            self.assertEqual(result.returncode, 2, output)  # EXIT_REFUSED
            self.assertIn('Quit Steam (Steam → Exit) and run the command again', output)
            self.assertIn('luna (standalone)' if host == 'luna' else 'for the lumen host', output)
            self.assertNotIn('before changing Lumen', output)
            # Nothing was written on either host: no tooling, no payloads, no Steam work.
            self.assertFalse((self.home / '.local/share/Lumen/sls-luna/runtime').exists())
            self.assertFalse((self.home / '.local/share/Lumen/sls-addons').exists())
            self.assertFalse((self.home / 'steam-invocations.log').exists())
            self.assertFalse((self.home / 'lumen-installer-args.log').exists())
            self.assertEqual(before, self.tree())

    def test_dry_run_with_steam_running_prints_the_plan_and_writes_nothing(self):
        self.fake('pgrep', '#!/bin/sh\nexit 0\n')  # Steam and steamwebhelper are "up"
        note = 'note: Steam is running; a real run would stop here until Steam is closed'
        for host in ('lumen', 'luna'):
            (self.home / '.local/share/Luna/host.json').parent.mkdir(parents=True, exist_ok=True)
            (self.home / '.local/share/Luna/host.json').write_text('{"active": "%s"}\n' % host)
            before = self.tree()
            result = self.bootstrap('--dry-run', lumen=(host == 'lumen'))
            output = result.stdout + result.stderr
            self.assertEqual(result.returncode, 0, output)
            self.assertIn(note, output)
            self.assertNotIn('STOPPED', output)
            self.assertIn('DRY RUN: nothing was written.', result.stdout)
            self.assertEqual(before, self.tree(), 'a dry run wrote something')
        # A dry install writes nothing either, and still plans.
        before = self.tree()
        result = self.luna('install', '--dry-run')
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output)
        self.assertIn(note, output)
        self.assertIn('DRY RUN:', result.stdout)
        self.assertEqual(before, self.tree())

    def test_real_run_still_refuses_before_any_write_with_steam_running(self):
        self.fake('pgrep', '#!/bin/sh\nexit 0\n')
        before = self.tree()
        result = self.luna('install')
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn('REFUSING to install:', result.stdout)
        self.assertIn('Quit Steam (Steam → Exit)', result.stdout)
        self.assertEqual(before, self.tree())

    def test_resume_hint_is_the_one_liner_when_install_sh_runs_it(self):
        self.fake('pgrep', '#!/bin/sh\nexit 0\n')
        env = dict(self.env, LUNA_INSTALL_ONELINER='1',
                   LUNA_INSTALL_FLAGS='--no-system-packages --lumen --no-gamemode')
        result = self.luna('bootstrap', '--no-system-packages', '--lumen', env=env)
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 2, output)
        self.assertIn('resume with: curl -fsSL '
                      'https://raw.githubusercontent.com/pgm1207/Luna/main/install.sh '
                      '| bash -s -- --no-system-packages --lumen --no-gamemode', output)
        # The temp tree install.sh deletes is never offered, and no `luna` command
        # exists yet on a fresh machine.
        self.assertNotIn('sls-luna-install', output)
        self.assertNotIn('resume with: luna bootstrap', output)

    def test_resume_hint_names_the_launcher_only_when_it_exists(self):
        self.fake('pgrep', '#!/bin/sh\nexit 0\n')
        result = self.luna('bootstrap', '--no-system-packages', '--lumen')
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 2, output)
        self.assertIn('resume with: python3 %s bootstrap --no-system-packages --lumen' % LUNA, output)
        self.assertNotIn('resume with: luna bootstrap', output)
        launcher = self.home / '.local/bin/luna'
        launcher.parent.mkdir(parents=True, exist_ok=True)
        launcher.write_text('#!/bin/sh\nexit 0\n')
        launcher.chmod(0o755)
        result = self.luna('bootstrap', '--no-system-packages', '--lumen')
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 2, output)
        self.assertIn('resume with: luna bootstrap --no-system-packages --lumen', output)

    def test_broken_runtime_tree_is_repaired_not_delegated_to(self):
        lumen = self.old_install_layout()
        first = self.bootstrap()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        tools = lumen / 'sls-luna'
        # A half-updated tree: luna.py from the checkout, runtime from neither build.
        (tools / 'runtime/RUNTIME.json').write_text('{"lunaSha256": "0", "files": {}}\n')
        shutil.rmtree(tools / 'runtime/vendor')
        delegated = subprocess.run([sys.executable, str(tools / 'luna.py'), 'bootstrap',
                                    '--no-system-packages', '--keep-steam-client'],
                                   capture_output=True, text=True, env=self.env, timeout=180)
        self.assertEqual(delegated.returncode, 1)
        self.assertIn('installing the current tooling first', delegated.stderr.lower())
        self.assertNotIn('vendor-update.sh', delegated.stderr)
        repaired = self.bootstrap()
        output = repaired.stdout + repaired.stderr
        self.assertEqual(repaired.returncode, 0, output)
        self.assertTrue((tools / 'runtime/vendor/MANIFEST.json').is_file())
        manifest = json.loads((tools / 'runtime/RUNTIME.json').read_text())
        self.assertEqual(manifest['lunaSha256'], hashlib.sha256(LUNA.read_bytes()).hexdigest())


if __name__ == '__main__':
    unittest.main()
