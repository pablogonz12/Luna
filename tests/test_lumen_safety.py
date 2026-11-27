#!/usr/bin/env python3
"""A failed or interrupted Lumen update must end in a known state.

Everything runs in a temp HOME with a fake installer; nothing touches the live
tree, systemd, Steam or the network.
"""
import argparse
import hashlib
import importlib.util
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parents[1]
LUNA = HERE / 'scripts' / 'luna.py'
os.environ.setdefault('LUNA_NO_NOTIFY', '1')
os.environ.setdefault('LUNA_NO_INHIBIT', '1')


class SafetyBase(unittest.TestCase):
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp(prefix='luna-safety-'))
        self.addCleanup(shutil.rmtree, self.temp, True)
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
        (self.sls / 'SLSsteam.so').write_text('so v1')
        self.cfg = self.home / '.config/SLSsteam'
        self.cfg.mkdir(parents=True)
        (self.cfg / 'config.yaml').write_text('user: settings\n')
        self.steam_home = self.home / '.local/share/Steam'
        self.steam_home.mkdir(parents=True)
        (self.steam_home / 'steam.cfg').write_text('BootStrapperInhibitAll=enable\n')
        env = mock.patch.dict(os.environ, {'HOME': str(self.home), 'LUMEN_DIR': str(self.lumen),
                                           'LUNA_HOME': str(self.home / '.local/share/Luna'),
                                           'XDG_DATA_HOME': str(self.home / '.local/share'),
                                           'XDG_CONFIG_HOME': str(self.home / '.config')})
        env.start()
        self.addCleanup(env.stop)
        spec = importlib.util.spec_from_file_location('luna_safety_mod', str(LUNA))
        self.m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.m)
        self.steam_up = False
        self.installer_action = 'ok'
        self.patchable = True
        self.calls = []
        self.systemctl = []
        self.heals = []
        self.extra_seen = None

        def installer(ref, sha, extra, timeout=None):
            self.calls.append(ref)
            self.extra_seen = list(extra)
            (self.steam_home / 'steam.cfg').unlink(missing_ok=True)
            (self.lumen / 'lumen').write_text('binary v2')
            (self.lumen / 'lua/boot.lua').write_text('boot v2 upstream')
            (self.lumen / 'lua/about.lua').write_text('about v2 upstream')
            (self.lumen / 'update-channels.json').write_text('{"lumen":"stable"}')
            if self.installer_action == 'fail':
                return 1
            if self.installer_action == 'raise':
                raise RuntimeError('boom')
            if self.installer_action == 'sigterm':
                raise SystemExit(143)
            return 0

        def fake_patcher(mode, *extra):
            boot = (self.lumen / 'lua/boot.lua').read_text()
            about = (self.lumen / 'lua/about.lua').read_text()
            if not self.patchable and 'upstream' in boot:
                return subprocess.CompletedProcess([], 1, '', 'anchor not found')
            out = 'bridge installed\n' if 'SLS-ADDONS-INTEGRATION' in boot else 'bridge not installed\n'
            out += 'lumen self-update blocked\n' if 'LUNA-LUMEN-UPDATE-BLOCK' in about else ''
            return subprocess.CompletedProcess([], 0, out, '')

        def install(options):
            self.calls.append('luna-install')
            self.install_options = options
            (self.lumen / 'lua/boot.lua').write_text('boot v2 SLS-ADDONS-INTEGRATION')
            (self.lumen / 'lua/about.lua').write_text('about v2 LUNA-LUMEN-UPDATE-BLOCK: begin')
            return 0

        def systemctl(*args, **kwargs):
            self.systemctl.append(args)
            return subprocess.CompletedProcess([], 0, 'active' if args[:1] == ('is-active',) else '', '')

        patches = {'process_running': lambda name: self.steam_up and name in ('steam', 'steamwebhelper'),
                   'run_base_installer': installer, 'patcher': fake_patcher, 'cmd_install': install,
                   'verify_base_installer': lambda ref, sha: 'f' * 64,
                   'lumen_upstream_state': lambda *a, **k: {'overall': 'newer', 'components': [], 'channels': {}},
                   'systemctl_user': systemctl,
                   'heal_steam_lock': lambda *a, **k: self.heals.append(1) or {'pinned': True, 'fixed': [], 'notes': []},
                   'read_steam_pin': lambda: {'clientBuild': '1'},
                   'ensure_steam_cfg_block': lambda *a, **k: None,
                   'start_apply_service': lambda: 'service'}
        for name, value in patches.items():
            patch = mock.patch.object(self.m, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        real_run = subprocess.run

        def guarded(cmd, *args, **kwargs):
            # Tripwire: a systemctl that escapes the mocks would drive the
            # developer's real user units (it once started luna-apply.service).
            if isinstance(cmd, (list, tuple)) and cmd and os.path.basename(str(cmd[0])) == 'systemctl':
                raise AssertionError('test reached the real systemctl: %r' % (list(cmd),))
            return real_run(cmd, *args, **kwargs)
        tripwire = mock.patch.object(subprocess, 'run', guarded)
        tripwire.start()
        self.addCleanup(tripwire.stop)

    # -- helpers
    def snapshot(self):
        out = {}
        for root in (self.lumen, self.sls, self.cfg, self.steam_home):
            for path in sorted(root.rglob('*')):
                rel = str(path)
                if 'sls-luna' in rel or '.luna-' in rel:
                    continue
                if path.is_symlink():
                    out[rel] = 'link:' + os.readlink(path)
                elif path.is_file():
                    out[rel] = path.read_bytes()
        return out

    def update(self, **kwargs):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            try:
                return self.m.apply_base_update(**kwargs)
            except SystemExit as error:
                return 'exit-%s' % error.code

    def result(self):
        return self.m.read_json_file(self.m.lumen_result_path()) or {}

    def lumen_cmd(self, **kwargs):
        options = argparse.Namespace(lumen_action='update', dry_run=False, yes=False, queue=False, json=False,
                                     check=False, backup='', installer_latest=False, reinstall=False, with_settings=False,
                                     keep_millennium=False, remove_millennium=False)
        for key, value in kwargs.items():
            setattr(options, key, value)
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return self.m.cmd_lumen(options)

    def dead_pid(self):
        """A pid that cannot exist: one above pid_max. (A reaped child's pid can be recycled
        by any process on a busy machine, which made liveness tests flaky.)"""
        try:
            top = int(Path('/proc/sys/kernel/pid_max').read_text())
        except (OSError, ValueError):
            top = 4194304
        return top + 1

    def fresh(self):
        spec = importlib.util.spec_from_file_location('luna_safety_fresh', str(LUNA))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module


class SafetyCase(SafetyBase):
    # -- C1
    def test_installer_failure_rolls_back_to_an_identical_tree(self):
        before = self.snapshot()
        self.installer_action = 'fail'
        self.assertNotEqual(self.update(), 0)
        self.assertEqual(self.snapshot(), before)
        self.assertTrue((self.steam_home / 'steam.cfg').is_file(), 'update lock back')
        record = self.result()
        self.assertEqual(record['state'], 'failed')
        self.assertIs(record['rolledBack'], True)
        self.assertIn('exited with 1', record['message'])
        self.assertIn(('start', 'lumen.service'), [call[:2] for call in self.systemctl], 'lumen restarted')
        self.assertTrue(self.heals)

    def test_installer_deletes_the_lock_then_crashes(self):
        self.installer_action = 'raise'
        self.assertNotEqual(self.update(), 0)
        self.assertTrue((self.steam_home / 'steam.cfg').is_file())
        self.assertTrue(self.heals, 'heal_steam_lock ran in the finally')
        self.assertEqual((self.lumen / 'lumen').read_text(), 'binary v1')
        self.assertIs(self.result()['rolledBack'], True)

    def test_patch_anchors_missing_rolls_back(self):
        self.patchable = False
        self.assertNotEqual(self.update(), 0)
        self.assertEqual((self.lumen / 'lumen').read_text(), 'binary v1')
        self.assertNotIn('luna-install', self.calls)
        record = self.result()
        self.assertEqual((record['state'], record['rolledBack']), ('failed', True))
        self.assertIn('cannot patch', record['message'])

    def test_steam_running_at_rollback_queues_a_rollback_job(self):
        self.installer_action = 'fail'
        real = self.m.run_base_installer

        def installer_then_steam(*args, **kwargs):
            code = real(*args, **kwargs)
            self.steam_up = True
            return code

        with mock.patch.object(self.m, 'run_base_installer', installer_then_steam):
            self.update()
        record = self.result()
        self.assertEqual(record['state'], 'failed')
        self.assertIs(record['rolledBack'], False)
        self.assertEqual(self.m.read_lumen_pending()['action'], 'rollback')
        self.assertTrue(self.heals)
        self.assertEqual((self.lumen / 'lumen').read_text(), 'binary v2', 'nothing swapped under a running Steam')
        # The waiter later runs the job once Steam is closed.
        self.steam_up = False
        with redirect_stdout(io.StringIO()):
            self.assertEqual(self.m.run_lumen_recovery_job(self.m.read_lumen_pending()), 0)
        self.assertEqual((self.lumen / 'lumen').read_text(), 'binary v1')

    # -- C2
    def running_record(self, phase, pid=None, backup=None):
        write = self.m.write_json_file
        write(self.m.lumen_result_path(), {'state': 'running', 'phase': phase, 'pid': pid or self.dead_pid(),
                                           'lumenWasActive': True, 'at': int(time.time()),
                                           **({'backup': backup} if backup else {})})

    def test_dead_pid_in_backup_phase_changes_nothing(self):
        stray = self.m.lumen_backup_root() / '20200101-000000'
        stray.mkdir(parents=True)
        (stray / 'half').write_text('x')
        before = self.snapshot()
        self.running_record('backup')
        with redirect_stdout(io.StringIO()):
            action, _ = self.m.recover_interrupted_lumen()
        self.assertEqual(action, 'recovered')
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(stray.exists())
        self.assertEqual(self.result()['state'], 'failed')

    def test_dead_pid_in_installer_phase_rolls_back(self):
        backup = self.m.backup_lumen_tree()
        before = self.snapshot()
        (self.lumen / 'lumen').write_text('binary half')
        (self.steam_home / 'steam.cfg').unlink()
        self.running_record('installer', backup=backup.name)
        with redirect_stdout(io.StringIO()):
            action, _ = self.m.recover_interrupted_lumen()
        self.assertEqual(action, 'recovered')
        self.assertEqual(self.snapshot(), before)
        self.assertIs(self.result()['rolledBack'], True)

    def test_dead_pid_in_reattach_phase_only_reattaches(self):
        backup = self.m.backup_lumen_tree()
        (self.lumen / 'lumen').write_text('binary v2')
        (self.lumen / 'lua/boot.lua').write_text('boot v2 upstream')
        self.running_record('reattach', backup=backup.name)
        with redirect_stdout(io.StringIO()):
            action, _ = self.m.recover_interrupted_lumen()
        self.assertEqual(action, 'recovered')
        self.assertEqual(self.calls, ['luna-install'], 'the installer is not re-run')
        self.assertEqual((self.lumen / 'lumen').read_text(), 'binary v2')
        self.assertEqual(self.result()['state'], 'applied')

    def test_live_luna_pid_is_left_alone(self):
        proc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)', 'luna.py'])
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        self.running_record('installer', pid=proc.pid, backup='x')
        before = self.snapshot()
        action, _ = self.m.recover_interrupted_lumen()
        self.assertEqual(action, 'busy')
        self.assertEqual(self.result()['state'], 'running')
        self.assertEqual(self.snapshot(), before)

    def test_a_live_pid_that_is_not_luna_counts_as_reused(self):
        proc = subprocess.Popen(['sleep', '60'])
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        self.assertFalse(self.m.pid_is_live_luna(proc.pid))
        self.assertFalse(self.m.pid_is_live_luna(self.dead_pid()))

    def test_a_live_process_with_an_empty_command_line_is_not_luna(self):
        # The cause of the old flake: a recycled pid whose /proc cmdline reads empty
        # (exiting process) used to count as alive.
        with mock.patch.object(self.m, '_pid_cmdline', lambda pid: ''):
            self.assertFalse(self.m.pid_is_live_luna(os.getpid()))
        with mock.patch.object(self.m, '_pid_cmdline', lambda pid: 'python luna.py autoupdate'):
            self.assertTrue(self.m.pid_is_live_luna(os.getpid()))

    def test_update_command_recovers_before_doing_anything_else(self):
        backup = self.m.backup_lumen_tree()
        (self.lumen / 'lumen').write_text('binary half')
        self.running_record('installer', backup=backup.name)
        self.assertEqual(self.lumen_cmd(yes=True), 0)
        self.assertEqual(self.calls, [], 'recovery only; the update is not started in the same run')
        self.assertEqual((self.lumen / 'lumen').read_text(), 'binary v1')

    def test_sigterm_mid_installer_restores_the_lock_and_clears_running(self):
        self.installer_action = 'sigterm'
        self.assertEqual(self.update(), 'exit-143')
        self.assertTrue((self.steam_home / 'steam.cfg').is_file())
        self.assertTrue(self.heals)
        record = self.result()
        self.assertNotEqual(record['state'], 'running')
        self.assertIs(record['rolledBack'], True)

    def test_main_installs_a_term_handler_for_mutating_commands(self):
        seen = {}

        def handler(options):
            seen['handler'] = signal.getsignal(signal.SIGTERM)
            return 0

        with mock.patch.object(self.m, 'build_parser') as parser:
            parser.return_value.parse_args.return_value = argparse.Namespace(command='lumen', handler=handler)
            self.m.main([])
        self.assertTrue(callable(seen['handler']))
        self.assertNotEqual(seen['handler'], signal.SIG_DFL)

    def test_unit_stops_gently(self):
        with mock.patch.object(self.m.subprocess, 'run'):
            text = self.m.write_apply_unit().read_text()
        self.assertIn('KillMode=mixed', text)
        self.assertIn('TimeoutStopSec=300', text)

    # -- H2
    def test_steam_appearing_before_the_installer_aborts_unchanged(self):
        real = self.m.backup_lumen_tree

        def backup_then_steam():
            path = real()
            self.steam_up = True
            return path

        before = self.snapshot()
        with mock.patch.object(self.m, 'backup_lumen_tree', backup_then_steam):
            self.assertEqual(self.update(), self.m.EXIT_REFUSED)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.m.lumen_backups(), [], 'the fresh backup is removed')
        record = self.result()
        self.assertEqual(record['state'], 'queued')
        self.assertNotIn('backup', record)

    # -- H1
    def test_pinned_hash_mismatch_refuses_unchanged(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, _n):
                return b'echo tampered\n'

        before = self.snapshot()
        fresh = self.fresh()
        with mock.patch.object(self.m, 'verify_base_installer', fresh.verify_base_installer), \
                mock.patch('urllib.request.urlopen', lambda *a, **k: Response()):
            self.assertEqual(self.update(), self.m.EXIT_REFUSED)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.m.lumen_backups(), [])
        self.assertIn('checksum mismatch', self.result()['message'])

    def test_default_is_the_pinned_installer(self):
        self.assertEqual(self.update(), 0)
        self.assertEqual(self.calls[0], self.m.UPSTREAM_INSTALLER_COMMIT)

    def test_main_needs_the_explicit_flags_and_is_never_queued(self):
        self.assertEqual(self.lumen_cmd(installer_latest=True), self.m.EXIT_REFUSED, 'no --yes')
        self.assertEqual(self.lumen_cmd(installer_latest=True, queue=True, yes=True), self.m.EXIT_REFUSED)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.lumen_cmd(installer_latest=True, yes=True), 0)
        self.assertEqual(self.calls[0], 'main')

    def test_a_queued_job_runs_the_pinned_installer_even_if_the_file_says_main(self):
        self.m.write_json_file(self.m.lumen_pending_path(),
                               {'action': 'update', 'installer': 'main', 'installer_latest': True, 'at': 1})
        extra = {}
        self.assertTrue(self.m.apply_queued_lumen(extra))
        self.assertEqual(self.calls[0], self.m.UPSTREAM_INSTALLER_COMMIT)
        self.assertFalse(self.m.read_lumen_pending())

    # -- M5
    def test_channels_are_passed_and_the_file_is_preserved(self):
        original = '{"slsteam_moon":"beta","lumen":"beta","plugin":"beta"}'
        (self.lumen / 'update-channels.json').write_text(original)
        self.assertEqual(self.update(), 0)
        for flag in ('--slsteam-channel', '--plugin-channel', '--lumen-channel'):
            self.assertEqual(self.extra_seen[self.extra_seen.index(flag) + 1], 'beta')
        self.assertEqual((self.lumen / 'update-channels.json').read_text(), original)
        self.assertIn('update-channels.json', self.result()['preserved'])

    def test_millennium_without_a_recorded_choice_refuses_unchanged(self):
        (self.home / '.local/share/millennium').mkdir(parents=True)
        before = self.snapshot()
        self.assertEqual(self.update(), self.m.EXIT_REFUSED)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.snapshot(), before)
        self.assertIn('--keep-millennium', self.result()['message'])
        self.assertEqual(self.lumen_cmd(yes=True, keep_millennium=True), 0)
        self.assertIn('--keep-millennium', self.extra_seen)

    def test_luatools_data_dirs_are_in_the_backup(self):
        (self.home / '.luatools').mkdir()
        (self.home / '.luatools/state').write_text('mine')
        self.installer_action = 'fail'
        self.update()
        self.assertEqual((self.home / '.luatools/state').read_text(), 'mine')

    # -- M3
    def test_timeout_kills_the_whole_process_group(self):
        marker = self.temp / 'child.pid'
        script = 'sleep 600 &\necho $! > %s\nwait\n' % marker

        def fake_fetch(ref, sha, destination):
            destination.write_text(script)
            return 'f' * 64

        real = self.fresh()
        with mock.patch.object(real, 'fetch_base_installer', fake_fetch), \
                mock.patch.object(real, '_inhibit_prefix', lambda: []), redirect_stdout(io.StringIO()):
            with self.assertRaises(real.InstallerTimeout):
                real.run_base_installer('ref', None, [], timeout=1)
        child = int(marker.read_text())
        time.sleep(0.3)
        with self.assertRaises(ProcessLookupError):
            os.kill(child, 0)


import urllib.error  # noqa: E402


class FakeApi:
    """Stands in for the public GitHub metadata endpoint; no network."""

    def __init__(self, **by_fragment):
        self.by_fragment = by_fragment
        self.urls = []

    def __call__(self, url):
        self.urls.append(url)
        for fragment, response in self.by_fragment.items():
            if fragment in url:
                if isinstance(response, Exception):
                    raise response
                return response
        raise urllib.error.HTTPError(url, 404, 'Not Found', {}, None)


def release(tag, asset_id, size, name):
    return {'tag_name': tag, 'draft': False, 'prerelease': False,
            'assets': [{'name': name, 'id': asset_id, 'size': size}]}


STABLE = {
    'slsteam-moon/releases?': [release('v2.9', 1, 10, 'slsteam-moon-linux-2.9-lumen.zip')],
    'lumen/releases/latest': release('v2.9', 2, 20, 'lumen-linux.zip'),
    'luatools-moon/releases/latest': release('v2.9', 3, 30, 'luatools-linux.zip'),
}
INSTALLED_STABLE = {'slsteam_moon': {'tag': 'v2.9', 'id': 1, 'size': 10, 'channel': 'stable'},
                    'lumen': {'tag': 'v2.9', 'id': 2, 'size': 20, 'channel': 'stable'},
                    'plugin': {'tag': 'v2.9', 'id': 3, 'size': 30, 'channel': 'stable'}}


class CurrentnessCase(SafetyBase):
    def setUp(self):
        super().setUp()
        real = self.fresh()
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(self.m, 'lumen_upstream_state', real.lumen_upstream_state).start()
        self.api_module = real
        (self.lumen / 'versions.json').write_text(json.dumps(INSTALLED_STABLE))

    def api(self, **responses):
        api = FakeApi(**responses)
        patch = mock.patch.object(self.api_module, '_public_json', api)
        patch.start()
        self.addCleanup(patch.stop)
        # lumen_upstream_state reads versions through the fresh module's own paths (same env)
        return api

    def channels(self, **values):
        (self.lumen / 'update-channels.json').write_text(json.dumps(values))

    def test_newer_on_beta_runs_the_update(self):
        self.channels(lumen='beta')
        self.api(**{'lumen/contents': {'sha': 'abc', 'size': 99, 'download_url': 'u'}}, **STABLE)
        self.assertEqual(self.update(), 0)
        self.assertEqual(self.calls[0], self.m.UPSTREAM_INSTALLER_COMMIT)

    def test_same_beta_build_is_current_and_touches_nothing(self):
        self.channels(lumen='beta')
        fingerprints = dict(INSTALLED_STABLE, lumen={'tag': 'beta', 'id': 'abc', 'size': 99, 'channel': 'beta'})
        (self.lumen / 'versions.json').write_text(json.dumps(fingerprints))
        self.api(**{'lumen/contents': {'sha': 'abc', 'size': 99, 'download_url': 'u'}}, **STABLE)
        before = self.snapshot()
        self.assertEqual(self.update(), 0)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.m.lumen_backups(), [])
        record = self.result()
        self.assertEqual((record['state'], record['changedText']), ('current', 'none'))
        self.assertEqual({row['component'] for row in record['compared']}, {'slsteam_moon', 'lumen', 'plugin'})
        self.assertEqual(self.snapshot(), before)
        self.assertNotIn('backup', record)

    def test_stable_only_newer_while_on_beta_counts_through_the_fallback(self):
        self.channels(slsteam_moon='beta', lumen='stable', plugin='stable')
        api = self.api(**dict(STABLE, **{'slsteam-moon/releases?': [release('v3.0', 9, 10, 'slsteam-moon-linux-3.0-lumen.zip')]}))
        state = self.m.lumen_upstream_state()
        self.assertEqual(state['overall'], 'newer')
        row = next(r for r in state['components'] if r['component'] == 'slsteam_moon')
        self.assertEqual(row['status'], 'newer')
        self.assertIn('falls back to stable', row['note'])
        self.assertTrue(any('ref=beta' in url for url in api.urls))

    def test_rate_limited_does_nothing_and_says_why(self):
        self.api(**{'api.github.com': urllib.error.HTTPError('u', 403, 'rate limit', {}, io.BytesIO(b''))})
        before = self.snapshot()
        self.assertEqual(self.update(), 1)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.snapshot(), before)
        record = self.result()
        self.assertEqual(record['state'], 'unknown')
        self.assertIn('rate limited', record['message'])

    def test_malformed_metadata_is_unknown_not_current(self):
        self.api(**{'api.github.com': 'not what the API returns'})
        self.assertEqual(self.m.lumen_upstream_state()['overall'], 'unknown')
        self.assertEqual(self.update(), 1)
        self.assertEqual(self.calls, [])

    def test_offline_is_unknown(self):
        self.api(**{'api.github.com': OSError('network is unreachable')})
        self.assertEqual(self.m.lumen_upstream_state()['overall'], 'unknown')

    def test_reinstall_forces_the_full_run_even_when_current(self):
        self.api(**STABLE)
        self.assertEqual(self.m.lumen_upstream_state()['overall'], 'current')
        self.assertEqual(self.lumen_cmd(yes=True), 0)
        self.assertEqual(self.calls, [], 'current: no installer')
        self.assertEqual(self.lumen_cmd(yes=True, reinstall=True), 0)
        self.assertEqual(self.calls[0], self.m.UPSTREAM_INSTALLER_COMMIT)

    def test_current_supersedes_a_stale_queued_job_and_text(self):
        self.api(**STABLE)
        self.m.record_lumen_result('queued', 'will update when Steam is closed')
        self.m.write_json_file(self.m.lumen_pending_path(), {'action': 'update', 'at': 1})
        self.assertTrue(self.m.apply_queued_lumen({}))
        self.assertEqual(self.result()['state'], 'current')
        self.assertFalse(self.m.read_lumen_pending())

    def test_changed_lists_old_to_new_per_component(self):
        self.api(**{'lumen/releases/latest': release('v3.0', 7, 70, 'lumen-linux.zip'),
                    **{k: v for k, v in STABLE.items() if 'lumen/releases' not in k}})
        original = self.m.run_base_installer

        def installer(ref, sha, extra, timeout=None):
            code = original(ref, sha, extra)
            versions = json.loads((self.lumen / 'versions.json').read_text())
            versions['lumen'] = {'tag': 'v3.0', 'id': 7, 'size': 70, 'channel': 'stable'}
            (self.lumen / 'versions.json').write_text(json.dumps(versions))
            return code

        with mock.patch.object(self.m, 'run_base_installer', installer):
            self.assertEqual(self.update(), 0)
        record = self.result()
        self.assertEqual(record['changed'], {'lumen': 'v2.9 stable -> v3.0 stable'})
        self.assertIn('backup', record)

    def test_a_failed_run_records_no_change_and_a_backup_only_when_the_installer_started(self):
        self.api(**{'lumen/releases/latest': release('v3.0', 7, 70, 'lumen-linux.zip'),
                    **{k: v for k, v in STABLE.items() if 'lumen/releases' not in k}})
        self.installer_action = 'fail'
        self.update()
        record = self.result()
        self.assertEqual(record['changedText'], 'none')
        self.assertIn('backup', record)
        self.steam_up = True
        self.update()
        self.assertNotIn('backup', self.result())

    def test_status_check_json_has_per_component_rows(self):
        self.channels(lumen='beta')
        self.api(**{'lumen/contents': {'sha': 'abc', 'size': 99, 'download_url': 'u'}}, **STABLE)
        with redirect_stdout(io.StringIO()):
            state = self.m.lumen_status(check=True)
        self.assertEqual(state['upstream']['overall'], 'newer')
        self.assertEqual({row['component'] for row in state['upstream']['components']},
                         {'slsteam_moon', 'lumen', 'plugin'})


class JobAndLockCase(SafetyBase):
    def test_a_job_that_keeps_raising_is_dropped_after_three_attempts(self):
        self.m.write_json_file(self.m.lumen_pending_path(), {'action': 'update', 'at': 1})
        with mock.patch.object(self.m, 'apply_base_update', side_effect=RuntimeError('kaboom')):
            for attempt in (1, 2):
                self.assertTrue(self.m.apply_queued_lumen({}))
                self.assertEqual(self.m.read_lumen_pending()['attempts'], attempt)
            self.assertTrue(self.m.apply_queued_lumen({}))
        self.assertFalse(self.m.read_lumen_pending())
        record = self.result()
        self.assertTrue(record['dropped'])
        self.assertIn('kaboom', record['message'])

    def test_queued_job_runs_when_the_luna_release_check_errors(self):
        self.m.write_json_file(self.m.lumen_pending_path(), {'action': 'update', 'at': 1})
        ran = []
        options = argparse.Namespace(wait_steam=False, verbose=False)
        with mock.patch.object(self.m, 'check_updates', lambda force=False: {'checkError': 'gh missing'}), \
                mock.patch.object(self.m, 'apply_base_update', lambda **k: ran.append(k) or 0), \
                mock.patch.object(self.m, 'write_heartbeat', lambda: None), \
                redirect_stdout(io.StringIO()):
            self.m.cmd_autoupdate(options)
        self.assertEqual(ran, [{'installer_latest': False}])
        self.assertFalse(self.m.read_lumen_pending())

    def test_update_all_base_latest_uses_the_lumen_path_and_lock(self):
        seen = []
        options = argparse.Namespace(dry_run=False, base_latest=True, yes=True, bundle=None, url=None)
        with mock.patch.object(self.m, 'cmd_update', lambda o: 0), \
                mock.patch.object(self.m, 'apply_base_update', lambda **k: seen.append(k) or 0), \
                mock.patch.object(self.m, 'base_state', lambda: 'ready'), \
                mock.patch.object(self.m, 'lumen_update_blockers', lambda: []), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(self.m.cmd_update_all(options), 0)
            self.assertEqual(seen, [{'installer_latest': True, 'reinstall': False}])
            lock = self.m._update_lock()
            lock.write_text(json.dumps({'pid': os.getpid(), 'at': int(time.time())}))
            self.assertEqual(self.m.cmd_update_all(options), self.m.EXIT_BUSY)

    def test_steam_mutations_take_the_writer_lock_but_status_does_not(self):
        calls = []
        with mock.patch.object(self.m, 'cmd_steam', lambda o: calls.append(o.steam_action) or 0):
            self.m._update_lock().write_text(json.dumps({'pid': os.getpid(), 'at': int(time.time())}))
            for action in ('lock', 'unlock', 'recover', 'update', 'cache'):
                with redirect_stderr(io.StringIO()):
                    self.assertEqual(self.m.cmd_steam_guard(argparse.Namespace(steam_action=action)),
                                     self.m.EXIT_BUSY, action)
            self.assertEqual(self.m.cmd_steam_guard(argparse.Namespace(steam_action='status')), 0)
            with redirect_stderr(io.StringIO()):
                self.assertEqual(self.m.cmd_refresh_locked(argparse.Namespace(dry_run=False)), self.m.EXIT_BUSY)
        self.assertEqual(calls, ['status'])
        self.m._update_lock().unlink()
        with mock.patch.object(self.m, 'cmd_steam', lambda o: 0):
            self.assertEqual(self.m.cmd_steam_guard(argparse.Namespace(steam_action='lock')), 0)
            self.assertFalse(self.m._update_lock().exists(), 'released afterwards')

    def test_the_waiter_does_not_start_while_a_steam_operation_holds_the_lock(self):
        proc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)', 'luna.py'])
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        self.m._update_lock().write_text(json.dumps({'pid': proc.pid, 'at': int(time.time())}))
        self.assertTrue(self.m._update_lock_held_by_other())
        self.m._update_lock().unlink()
        self.assertFalse(self.m._update_lock_held_by_other())

    # -- H4
    def test_restore_needs_confirmation_and_leaves_settings_alone(self):
        backup = self.m.backup_lumen_tree()
        (self.lumen / 'lumen').write_text('binary newer')
        (self.cfg / 'config.yaml').write_text('user: changed later\n')
        before = self.snapshot()
        with mock.patch.object(self.m.sys.stdin, 'isatty', lambda: False):
            self.assertEqual(self.lumen_cmd(lumen_action='restore'), self.m.EXIT_REFUSED)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.lumen_cmd(lumen_action='restore', yes=True), 0)
        self.assertEqual((self.lumen / 'lumen').read_text(), 'binary v1')
        self.assertEqual((self.cfg / 'config.yaml').read_text(), 'user: changed later\n', 'settings kept')
        self.assertEqual(self.lumen_cmd(lumen_action='restore', yes=True, with_settings=True), 0)
        self.assertEqual((self.cfg / 'config.yaml').read_text(), 'user: settings\n')
        self.assertEqual(self.result()['state'], 'restored')
        self.assertTrue(backup.exists())

    def test_cancel_with_nothing_queued_keeps_the_last_result(self):
        self.m.record_lumen_result('applied', 'done')
        self.assertEqual(self.lumen_cmd(lumen_action='cancel'), 0)
        self.assertEqual(self.result()['state'], 'applied')


class StateCase(SafetyBase):
    """Backups outside the Lumen tree, pin copy, bridge heartbeat, clean, update-check."""

    def test_backups_live_in_the_state_dir_not_the_lumen_tree(self):
        backup = self.m.backup_lumen_tree()
        state = Path(os.environ['LUNA_HOME']) / 'state' / 'lumen-backups'
        self.assertEqual(backup.parent, state)
        self.assertFalse(str(backup).startswith(str(self.lumen)))

    def make_legacy_backup(self):
        backup = self.m.backup_lumen_tree()
        legacy = self.m.lumen_legacy_backup_root()
        legacy.mkdir(parents=True)
        shutil.move(str(backup), str(legacy / backup.name))
        return backup.name

    def test_old_backups_are_found_and_migrated_safely(self):
        name = self.make_legacy_backup()
        self.assertEqual([b.name for b in self.m.lumen_backups()], [name], 'status finds the old location')
        # An interrupted earlier migration left junk behind; it must not matter.
        junk = self.m.lumen_backup_root() / ('.migrating-' + name)
        junk.mkdir(parents=True)
        (junk / 'half').write_text('x')
        with redirect_stdout(io.StringIO()):
            self.assertEqual(self.m.migrate_lumen_backups(), [name])
        self.assertFalse((self.m.lumen_legacy_backup_root() / name).exists())
        self.assertEqual(self.m.verify_lumen_backup(self.m.lumen_backup_root() / name), (True, ''))
        self.assertFalse(junk.exists())
        self.assertEqual([b.name for b in self.m.lumen_backups()], [name])

    def test_restore_works_from_an_old_location_backup(self):
        name = self.make_legacy_backup()
        (self.lumen / 'lumen').write_text('binary changed')
        ok, _msg = self.m.restore_lumen_backup(name)
        self.assertTrue(ok)
        self.assertEqual((self.lumen / 'lumen').read_text(), 'binary v1')

    def test_the_pin_survives_loss_of_the_tools_dir(self):
        with mock.patch.object(self.m, 'read_steam_pin', self.fresh().read_steam_pin):
            pass
        m = self.fresh()  # unpatched pin functions
        m.write_json_file(m.steam_pin_path(), {'clientBuild': '123'})
        m.sync_steam_pin_copy()
        self.assertTrue(m.steam_pin_copy_path().is_file())
        self.assertNotIn(str(self.lumen), str(m.steam_pin_copy_path()))
        m.steam_pin_path().unlink()
        self.assertEqual(m.read_steam_pin()['clientBuild'], '123')
        m.remove_steam_pin()
        self.assertEqual(m.read_steam_pin(), {})

    def test_launcher_has_no_hook_branch_and_explains_a_missing_copy(self):
        self.assertNotIn('lumen-hook', self.m.CLI_LAUNCHER)
        self.assertIn('rescue/luna.py', self.m.CLI_LAUNCHER)
        self.assertIn('install --force', self.m.CLI_LAUNCHER)
        self.assertTrue(self.m.ours_cli_launcher_bytes(self.m.LEGACY_CLI_LAUNCHERS[0].encode())
                        if hasattr(self.m, 'ours_cli_launcher_bytes') else True)
        script = self.temp / 'luna-launcher'
        script.write_text(self.m.CLI_LAUNCHER)
        result = subprocess.run(['sh', str(script)], capture_output=True, text=True,
                                env=dict(os.environ, HOME=str(self.home)))
        self.assertEqual(result.returncode, 1)
        self.assertIn('Get Luna back', result.stderr)

    # -- bridge heartbeat (D)
    def heartbeat(self, **fields):
        self.m.luna_state_dir().mkdir(parents=True, exist_ok=True)
        self.m.write_json_file(self.m.bridge_heartbeat_path(), fields)

    def test_bridge_status_reports_attached_missing_and_failed(self):
        alive = {'time': int(time.time()), 'pid': os.getpid(), 'addons': ['luna', 'alpha'], 'failed': []}
        with mock.patch.object(self.m, 'lumen_running', lambda: True):
            level, lines = self.m.bridge_status()
            self.assertEqual(level, 'warn')
            self.assertIn('has not loaded since Steam started', lines[0])
            self.heartbeat(**alive)
            level, lines = self.m.bridge_status()
            self.assertEqual((level, lines[0][:27]), ('ok', 'attached: 2 add-ons, loaded'))
            self.heartbeat(**dict(alive, pid=self.dead_pid()))
            self.assertIn('has not loaded since Steam started', self.m.bridge_status()[1][0])
            self.heartbeat(**dict(alive, failed=[{'name': 'broken', 'error': 'failed to load: syntax'}]))
            level, lines = self.m.bridge_status()
            self.assertEqual(level, 'warn')
            self.assertIn('add-on broken did not load: failed to load: syntax', lines[-1])
        with mock.patch.object(self.m, 'lumen_running', lambda: False):
            self.heartbeat(**dict(alive, pid=self.dead_pid()))
            self.assertEqual(self.m.bridge_status()[0], 'info')

    # -- clean (E)
    def test_clean_keeps_what_recover_and_backups_need(self):
        tools = self.m.luna_tools_dir()
        old = time.time() - 40 * 86400
        stage = tools / 'steam-client-stage-abc'
        (stage / 'package').mkdir(parents=True)
        (stage / 'package/x').write_text('x' * 100)
        cache = tools / 'steam-client-cache'
        cache.mkdir()
        (cache / 'build.txt').write_text('1')
        backups = tools / 'steam-client-backups'
        for ns in (1000000000, 2000000000, 3000000000):
            (backups / str(ns)).mkdir(parents=True)
            (backups / str(ns) / 'f').write_text('b')
        self.m.write_json_file(tools / 'steam-client-installed.json', {'at': 4})
        (tools / 'archive').mkdir()
        (tools / 'archive/fresh').write_text('new')
        (tools / 'archive/stale').write_text('old')
        (tools / 'luna.py.bak-old').write_text('b')
        for path in (stage, stage / 'package', stage / 'package/x', tools / 'archive/stale', tools / 'luna.py.bak-old'):
            os.utime(path, (old, old))
        self.m.backup_lumen_tree()
        rows = {row[0].name: row for row in self.m.clean_candidates()}
        self.assertTrue(rows['steam-client-stage-abc'][3])
        self.assertFalse(rows['steam-client-cache'][3], 'recover/lock depend on the cache')
        self.assertFalse(rows['3000000000'][3], 'the newest client backup stays')
        self.assertTrue(rows['1000000000'][3])
        self.assertFalse(rows['fresh'][3], 'archive entries younger than 30 days stay')
        self.assertTrue(rows['stale'][3])
        self.assertTrue(rows['luna.py.bak-old'][3])
        self.assertFalse(any(row[3] for row in rows.values() if row[0].parent == self.m.lumen_backup_root()),
                         'the newest 2 Lumen backups stay')
        # A live luna holding the lock protects the stage dir.
        with mock.patch.object(self.m, '_update_lock_held_by_other', lambda: True):
            self.assertFalse({row[0].name: row for row in self.m.clean_candidates()}['steam-client-stage-abc'][3])
        # Default is a listing; --yes deletes only the REMOVE items.
        with redirect_stdout(io.StringIO()) as out:
            self.m.cmd_clean(argparse.Namespace(yes=False, dry_run=False))
        self.assertTrue(stage.exists())
        self.assertIn('DRY RUN', out.getvalue())
        with redirect_stdout(io.StringIO()):
            self.m.cmd_clean(argparse.Namespace(yes=True, dry_run=False))
        self.assertFalse(stage.exists())
        self.assertFalse((backups / '1000000000').exists())
        self.assertTrue((backups / '3000000000').exists() and cache.exists())
        self.assertTrue((tools / 'archive/fresh').exists())
        with redirect_stdout(io.StringIO()):
            self.m.cmd_clean(argparse.Namespace(yes=True, dry_run=True))  # --dry-run wins

    # -- update check (F3)
    def test_a_release_older_than_installed_is_not_an_update_and_has_a_source(self):
        with mock.patch.object(self.m, 'installed_version', lambda: '0.14.18'), \
                mock.patch.object(self.m, 'latest_release', lambda: ('v0.14.17', '')):
            report = self.m.check_updates(force=True)
        self.assertFalse(report['available'])
        self.assertTrue(report['newerThanLatest'])
        self.assertIn('newer than the latest release', report['note'])
        self.assertIn('GitHub releases', report['source'])
        with mock.patch.object(self.m, 'installed_version', lambda: '0.14.18'):
            cached = self.m.check_updates()
        self.assertTrue(cached['newerThanLatest'] and cached['source'])
        with mock.patch.object(self.m, 'installed_version', lambda: '0.14.17'), \
                mock.patch.object(self.m, 'latest_release', lambda: ('v0.14.18', '')):
            report = self.m.check_updates(force=True)
        self.assertTrue(report['available'] and not report['newerThanLatest'])

    def test_host_status_reports_the_active_hosts_real_paths(self):
        status = self.m.host_status()
        self.assertEqual(status['active'], 'lumen')
        self.assertEqual(status['addons'], str(self.lumen / 'sls-addons'))
        self.assertEqual(status['state'], str(self.lumen / 'sls-luna'))
        self.m.write_json_file(self.m.host_state_path(), {'active': 'luna'})
        status = self.m.host_status()
        self.assertEqual(status['addons'], str(self.m.luna_home() / 'addons'))
        self.assertEqual(status['state'], str(self.m.luna_home() / 'state'))

    # -- M4
    def test_after_a_luna_update_the_lumen_step_runs_in_the_new_copy(self):
        self.m.write_json_file(self.m.lumen_pending_path(), {'action': 'update', 'at': 1})
        seen = {}

        def fake_run(command, env=None, **kwargs):
            seen['command'], seen['env'] = command, env
            return subprocess.CompletedProcess(command, 0)

        with mock.patch.object(self.m, 'installed_copy_problems', lambda *a, **k: []), \
                mock.patch.object(self.m, '_LUNA_REPLACED', True), mock.patch.object(self.m.subprocess, 'run', fake_run), \
                mock.patch.object(self.m, 'apply_base_update', side_effect=AssertionError('old code must not drive it')):
            self.assertTrue(self.m.apply_queued_lumen({}))
        self.assertEqual(seen['command'][1:], [str(self.m.luna_tools_dir() / 'luna.py'), 'lumen', 'update', '--yes'])
        self.assertEqual(seen['env'][self.m.LOCK_HELD_ENV], '1')


if __name__ == '__main__':
    unittest.main()
