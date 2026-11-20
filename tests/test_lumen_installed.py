#!/usr/bin/env python3
"""The installed copy must be able to finish a Lumen update by itself.

The bug this covers: `luna install` only deployed luna.py into
<Lumen>/sls-luna, so the copy the timer and the in-Steam panel run had no
patch-lumen.py, bridge or add-on sources. A queued Lumen update would run the
upstream installer and then lose Luna, because it could not re-patch Lumen or
re-install the add-ons.
"""
import hashlib
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
REPO_LUNA = HERE / 'scripts' / 'luna.py'

# Never pop a desktop notification from an install/update test run.
os.environ.setdefault('LUNA_NO_NOTIFY', '1')

sys.path.insert(0, str(HERE / 'tests'))
from test_luna import UPSTREAM, stub_bin  # noqa: E402


UPSTREAM_ABOUT = '''local about = {}
function about.update_script(...)
  print('stock updater')
end
return about
'''


def tree_digest(*roots):
    found = {}
    for root in roots:
        for path in sorted(Path(root).rglob('*')):
            rel = '%s/%s' % (Path(root).name, path.relative_to(root))
            if path.is_symlink():
                found[rel] = 'link:' + os.readlink(path)
            elif path.is_file():
                found[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return found


class InstalledCopyTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp(prefix='luna-installed-'))
        self.home = self.temp / 'home'
        lumen = self.home / '.local/share/Lumen'
        (lumen / 'lua').mkdir(parents=True)
        (lumen / 'lua/about.lua').write_text(UPSTREAM_ABOUT)
        (lumen / 'luatools/backend').mkdir(parents=True)
        (lumen / 'luatools/backend/main.lua').write_text('-- present')
        (self.home / '.local/share/SLSsteam/path').mkdir(parents=True)
        (self.home / '.local/share/SLSsteam/path/steam').write_text('-- present')
        self.boot = lumen / 'lua/boot.lua'
        self.boot.write_text(UPSTREAM)
        self.addons = lumen / 'sls-addons'
        self.tools = lumen / 'sls-luna'
        self.runtime = self.tools / 'runtime'
        self.data = self.home / '.local/share/slssteam-integrations'
        self.data.mkdir(parents=True)
        (self.data / 'presence.json').write_text('{"version":1,"owned":{}}')
        self.bin = stub_bin(self.home)
        self.luna_home = self.home / '.local/share/Luna'
        host_src = self.temp / 'host-src'
        (host_src / 'bin').mkdir(parents=True)
        (host_src / 'bin/luna-host').write_text('#!/bin/sh\nexit 0\n')
        (host_src / 'bin/luna-host').chmod(0o755)
        (host_src / 'lua/luna').mkdir(parents=True)
        (host_src / 'lua/luna/boot.lua').write_text('-- boot')
        self.env = dict(os.environ, HOME=str(self.home), LUNA_HOME=str(self.luna_home),
                        LUNA_HOST_SRC=str(host_src),
                        XDG_DATA_HOME=str(self.home / '.local/share'),
                        XDG_RUNTIME_DIR=str(self.home / 'run'),
                        PATH=str(self.bin) + os.pathsep + os.environ.get('PATH', ''))

    def tearDown(self):
        shutil.rmtree(self.temp, ignore_errors=True)

    def run_cli(self, script, *args, running=''):
        stub_bin(self.home, running)
        command = [sys.executable, str(script)] + list(args)
        if args and args[0] == 'install':
            command += ['--keep-steam-client', '--no-steam-machine']
        return subprocess.run(command, capture_output=True, text=True, env=self.env)

    def install_from_repo(self):
        result = self.run_cli(REPO_LUNA, 'install')
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        return result

    def test_install_deploys_the_whole_runtime_tree(self):
        self.install_from_repo()
        for rel in ('scripts/patch-lumen.py', 'scripts/steam-machine.py',
                    'bridge/bridge.lua', 'vendor/MANIFEST.json',
                    'addons/luna/plugin/public/panel.js', 'addons/luna/plugin/backend/main.lua'):
            self.assertTrue((self.runtime / rel).is_file(), rel)
        unit = self.home / '.config/systemd/user/luna-apply.service'
        self.assertTrue(unit.is_file())
        text = unit.read_text()
        self.assertIn('autoupdate --wait-steam', text)
        self.assertNotIn('PartOf=', text)

    def test_installed_copy_reports_healthy_and_can_reinstall(self):
        self.install_from_repo()
        status = self.run_cli(self.tools / 'luna.py', 'lumen', 'status')
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertIn('bridge    : installed', status.stdout)
        self.assertIn('self-update block: in place', status.stdout)
        self.assertNotIn('tooling incomplete', status.stdout)
        # Simulate the upstream installer replacing boot.lua mid-update; the
        # installed copy must be able to re-attach from its own runtime tree.
        self.boot.write_text(UPSTREAM)
        reinstall = self.run_cli(self.tools / 'luna.py', 'install', '--force')
        self.assertEqual(reinstall.returncode, 0, reinstall.stderr + reinstall.stdout)
        self.assertEqual(self.boot.read_text().count('SLS-ADDONS-INTEGRATION'), 1)
        self.assertTrue((self.addons / 'luna/plugin.json').is_file())

    def test_preflight_refuses_incomplete_tooling_and_changes_nothing(self):
        self.install_from_repo()
        (self.runtime / 'scripts/patch-lumen.py').unlink()
        before = tree_digest(self.boot.parent, self.addons, self.data)
        result = self.run_cli(self.tools / 'luna.py', 'lumen', 'update', '--yes')
        self.assertNotEqual(result.returncode, 0)
        combined = result.stdout + result.stderr
        self.assertIn('tooling incomplete', combined)
        self.assertIn('patch-lumen.py', combined)
        self.assertIn('Nothing was changed', combined)
        self.assertEqual(tree_digest(self.boot.parent, self.addons, self.data), before)
        self.assertFalse((self.tools / 'lumen-backups').exists())
        state = self.run_cli(self.tools / 'luna.py', 'lumen', 'status', '--json')
        self.assertIn('tooling incomplete', state.stdout)
        self.assertIn('"toolingIncomplete"', state.stdout)

    def test_runtime_tree_is_verified_against_this_luna(self):
        import json
        self.install_from_repo()
        manifest = json.loads((self.runtime / 'RUNTIME.json').read_text())
        self.assertEqual(manifest['lunaSha256'], hashlib.sha256((self.tools / 'luna.py').read_bytes()).hexdigest())
        self.assertIn('scripts/patch-lumen.py', manifest['files'])
        self.assertFalse((self.tools / 'runtime.new').exists())
        ok = self.run_cli(self.tools / 'luna.py', 'lumen', 'status')
        self.assertNotIn('does not match this luna.py', ok.stdout + ok.stderr)
        # A hand-edited runtime file: preflight (full hash) refuses, with the repair command.
        patcher = self.runtime / 'scripts/steam-machine.py'
        patcher.write_text(patcher.read_text() + '\n# edited\n')
        refused = self.run_cli(self.tools / 'luna.py', 'lumen', 'update', '--yes')
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn('runtime tree does not match this luna.py', refused.stdout + refused.stderr)
        self.assertIn('install --force', refused.stdout + refused.stderr)
        # A changed luna.py no longer matches the tree it was deployed with.
        self.run_cli(REPO_LUNA, 'install', '--force')
        (self.tools / 'luna.py').write_text((self.tools / 'luna.py').read_text() + '\n# edited\n')
        status = self.run_cli(self.tools / 'luna.py', 'lumen', 'status')
        self.assertIn('runtime tree does not match this luna.py', status.stdout + status.stderr)

    def test_rescue_copy_survives_a_base_install_that_replaces_the_lumen_tree(self):
        self.install_from_repo()
        rescue = self.luna_home / 'state/rescue'
        self.assertTrue((rescue / 'luna.py').is_file())
        self.assertTrue((rescue / 'runtime/RUNTIME.json').is_file())
        shutil.rmtree(self.tools)
        shutil.rmtree(self.addons)
        status = self.run_cli(rescue / 'luna.py', 'lumen', 'status')
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertNotIn('tooling incomplete', status.stdout)
        # The shell launcher falls back to the rescue copy.
        launcher = self.temp / 'launcher.sh'
        launcher.write_text(__import__('importlib').util.module_from_spec(
            importlib.util.spec_from_file_location('l', REPO_LUNA)).__class__ and load_luna().CLI_LAUNCHER)
        run = subprocess.run(['sh', str(launcher), 'lumen', 'status'], capture_output=True, text=True, env=self.env)
        self.assertEqual(run.returncode, 0, run.stderr)
        # And `luna install` from the rescue copy brings the tooling and add-ons back.
        back = self.run_cli(rescue / 'luna.py', 'install')
        self.assertEqual(back.returncode, 0, back.stderr + back.stdout)
        self.assertTrue((self.tools / 'luna.py').is_file())
        self.assertTrue((self.addons / 'luna/plugin.json').is_file())

    def test_queue_starts_and_cancel_stops_the_waiter(self):
        self.install_from_repo()
        queued = self.run_cli(self.tools / 'luna.py', 'lumen', 'update', '--queue')
        self.assertEqual(queued.returncode, 0, queued.stderr + queued.stdout)
        self.assertTrue((self.tools / 'pending-lumen.json').is_file())
        self.assertIn('starts on its own', queued.stdout)
        log = (self.home / 'systemctl.log').read_text()
        self.assertIn('start --no-block luna-apply.service', log)
        result = __import__('json').loads((self.tools / 'lumen-update.json').read_text())
        self.assertEqual(result['applier'], 'service')
        cancelled = self.run_cli(self.tools / 'luna.py', 'lumen', 'cancel')
        self.assertEqual(cancelled.returncode, 0)
        self.assertFalse((self.tools / 'pending-lumen.json').exists())
        self.assertIn('stop luna-apply.service', (self.home / 'systemctl.log').read_text())


def load_luna():
    spec = importlib.util.spec_from_file_location('luna_under_test', REPO_LUNA)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class WaiterTestCase(unittest.TestCase):
    def setUp(self):
        self.luna = load_luna()

    def test_triggers_only_after_the_stable_closed_window(self):
        clock = FakeClock()
        seen = []

        def probe():
            seen.append(clock.now)
            return clock.now < 5  # Steam exits at t=5

        verdict = self.luna.wait_for_steam_closed(limit=100, stable=10,
                                                  probe=probe, has_work=lambda: True,
                                                  clock=clock, sleep=clock.sleep)
        self.assertEqual(verdict, 'ready')
        self.assertEqual(clock.now, 15)  # 5 closed + 10 stable, not at 5

    def test_a_steam_self_restart_resets_the_window(self):
        clock = FakeClock()

        def probe():  # gone 0-8, back 9-12, gone from 13
            return 9 <= clock.now <= 12

        verdict = self.luna.wait_for_steam_closed(limit=100, stable=10, probe=probe,
                                                  has_work=lambda: True,
                                                  clock=clock, sleep=clock.sleep)
        self.assertEqual(verdict, 'ready')
        self.assertGreaterEqual(clock.now, 23)

    def test_cancel_and_timeout(self):
        clock = FakeClock()
        self.assertEqual(self.luna.wait_for_steam_closed(
            limit=100, stable=10, probe=lambda: True, has_work=lambda: False,
            clock=clock, sleep=clock.sleep), 'cancelled')
        clock = FakeClock()
        self.assertEqual(self.luna.wait_for_steam_closed(
            limit=60, stable=10, probe=lambda: True, has_work=lambda: True,
            clock=clock, sleep=clock.sleep), 'timeout')
        self.assertEqual(clock.now, 60)


if __name__ == '__main__':
    unittest.main()
