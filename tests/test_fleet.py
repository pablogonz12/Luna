"""luna fleet with ssh/rsync/scp mocked; nothing here touches the network or real Steam."""
import argparse
import importlib.util
import io
import json
import contextlib
import os
os.environ.setdefault('LUNA_GUI_CHECKS', '0')  # hermetic: tests never probe the real desktop (test_gui_check manages it itself)
os.environ.setdefault('LUNA_LIVE_CHECKS', '0')  # hermetic: tests never contact a real Steam (test_live_probe uses a fake endpoint)
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location('luna_fleet_test', Path(__file__).resolve().parents[1] / 'scripts/luna.py')
luna = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(luna)

STEAM = {'build': '100', 'pinned': '100', 'match': True, 'packagesMatch': True, 'updatesBlocked': True,
         'hardLock': False, 'cachedBuild': '100', 'cacheReady': True}


def remote_out(sha='aa', steam=False, lumen=False, probe=None, legacy_steam=None):
    lines = ['SHA ' + sha, 'STEAMRUN %d' % steam, 'LUMENRUN %d' % lumen]
    if probe is not None:
        lines.append('PROBE ' + json.dumps(probe))
    else:
        lines.append('LEGACY')
        lines += ['ST ' + line for line in (legacy_steam or [])]
    return 0, '\n'.join(lines) + '\n', ''


def probe(sha='aa', addons=None, pinned='100', version='1.0'):
    return {'lunaSha': sha, 'lunaVersion': version, 'addons': addons or {'luna': 'match', 'sgdb': 'absent'},
            'steam': dict(STEAM, pinned=pinned), 'steamRunning': False, 'lumenRunning': False}


def options(**kw):
    base = dict(fleet_action='status', hosts=[], json=False, dry_run=False, restart_steam=False,
                force=False, only=[], stop_timeout=1)
    base.update(kw)
    return argparse.Namespace(**base)


class FleetTestCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='luna-fleet-test-')
        self.addCleanup(temp.cleanup)
        self.config = Path(temp.name) / 'fleet.json'
        patcher = mock.patch.dict(os.environ, {'LUNA_FLEET_CONFIG': str(self.config)})
        patcher.start()
        self.addCleanup(patcher.stop)
        doctor = mock.patch.object(luna, 'doctor_findings', return_value=[])
        doctor.start()
        self.addCleanup(doctor.stop)

    def run_cmd(self, opts):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = luna.cmd_fleet(opts)
        return code, out.getvalue(), err.getvalue()


class DoctorSummaryTests(FleetTestCase):
    def test_remote_doctor_counts_reach_the_record_and_notes(self):
        report = {'findings': [{'severity': 'warn'}, {'severity': 'warn'}, {'severity': 'error'},
                               {'severity': 'ok'}]}
        code, out, err = remote_out(probe=probe())
        out += 'DOCTOR ' + json.dumps(report) + '\n'
        record = luna.fleet_parse_status(code, out, err)
        record['host'] = 'deck'
        self.assertEqual(record['doctor'], {'warn': 2, 'error': 1})
        self.assertEqual(luna._fleet_doctor(record['doctor']), '2w 1e')
        self.assertEqual(luna._fleet_doctor({'warn': 0, 'error': 0}), 'ok')
        self.assertTrue(any('doctor reports 2 warning' in n for n in luna.fleet_notes([record])))

    def test_remote_without_doctor_is_unknown_not_an_error(self):
        record = luna.fleet_parse_status(*remote_out(probe=probe()))
        self.assertNotIn('doctor', record)
        record['host'] = 'old'
        self.assertEqual(luna.fleet_row(record)[-1], '?')

    def test_status_script_asks_the_remote_for_doctor_json(self):
        self.assertIn('doctor --json', luna.FLEET_STATUS_SCRIPT)


class ConfigTests(FleetTestCase):
    def test_add_remove_list_roundtrip(self):
        self.assertEqual(self.run_cmd(options(fleet_action='add', hosts=['laptop']))[0], 0)
        self.run_cmd(options(fleet_action='add', hosts=['laptop']))  # idempotent
        self.run_cmd(options(fleet_action='add', hosts=['me@box.lan']))
        self.assertEqual(json.loads(self.config.read_text())['hosts'], ['laptop', 'me@box.lan'])
        self.assertIn('remote  laptop', self.run_cmd(options(fleet_action='list'))[1])
        self.assertEqual(self.run_cmd(options(fleet_action='remove', hosts=['laptop']))[0], 0)
        self.assertEqual(luna.fleet_load_hosts(), ['me@box.lan'])
        self.assertEqual(self.run_cmd(options(fleet_action='remove', hosts=['nope']))[0], 1)

    def test_host_validation_rejects_injection(self):
        for bad in ('-oProxyCommand=x', 'a;b', 'a b', '$(id)', 'a`b`', '', 'a/b', '-x@host', 'h\nx'):
            self.assertFalse(luna.fleet_valid_host(bad), bad)
            code, _, _ = self.run_cmd(options(fleet_action='add', hosts=[bad]))
            self.assertEqual(code, luna.EXIT_REFUSED)
        self.assertFalse(self.config.exists())
        for good in ('laptop', 'laptop-tailscale', 'u@10.0.0.2', 'a.b_c'):
            self.assertTrue(luna.fleet_valid_host(good), good)

    def test_invalid_hosts_in_file_are_ignored(self):
        self.config.write_text(json.dumps({'hosts': ['ok', '-bad', 'a;b', 'ok', 5]}))
        self.assertEqual(luna.fleet_load_hosts(), ['ok'])

    def test_ssh_gets_host_as_argv_after_double_dash(self):
        with mock.patch.object(luna.subprocess, 'run') as run:
            run.return_value = subprocess.CompletedProcess([], 0, '', '')
            luna.fleet_ssh('laptop', 'echo hi\n')
        argv = run.call_args[0][0]
        self.assertEqual(argv[0], 'ssh')
        self.assertIn('BatchMode=yes', argv)
        self.assertEqual(argv[argv.index('--') + 1:], ['laptop', 'bash', '-s'])
        self.assertEqual(run.call_args[1]['input'], 'echo hi\n')


class StatusTests(FleetTestCase):
    def local(self):
        return mock.patch.object(luna, 'probe_data', return_value=probe(sha='aa'))

    def test_aggregation_with_unreachable_and_old_remote(self):
        self.config.write_text(json.dumps({'hosts': ['good', 'down', 'old']}))
        answers = {'good': remote_out(sha='bb', steam=True, probe=probe(sha='bb', addons={'luna': 'drift'}, pinned='99')),
                   'down': (255, '', 'ssh: connect to host down port 22: Connection timed out\n'),
                   'old': remote_out(sha='cc', legacy_steam=['client : 100', 'pinned : 100',
                                                              'packages: match the pin', 'updates: blocked (steam.cfg)'])}
        with self.local(), mock.patch.object(luna, 'fleet_ssh', side_effect=lambda h, s, timeout=120: answers[h]):
            code, out, _ = self.run_cmd(options(json=True, hosts=[]))
        data = json.loads(out)
        by = {r['host']: r for r in data['hosts']}
        self.assertEqual(code, 1)  # an unreachable host fails the command
        self.assertFalse(by['down']['reachable'])
        self.assertTrue(by['old']['legacy'])
        self.assertEqual(by['old']['steam']['pinned'], '100')
        self.assertTrue(by['old']['steam']['packagesMatch'])
        self.assertTrue(by['good']['steamRunning'])
        notes = '\n'.join(data['notes'])
        self.assertIn('down: unreachable', notes)
        self.assertIn('old: older Luna', notes)
        self.assertIn('good: luna.py differs', notes)
        self.assertIn('good: add-on payloads drifted', notes)
        self.assertIn('good: Steam pin 99 differs', notes)

    def test_text_table_and_clean_exit(self):
        self.config.write_text(json.dumps({'hosts': ['good']}))
        with self.local(), mock.patch.object(luna, 'fleet_ssh', return_value=remote_out(probe=probe())):
            code, out, _ = self.run_cmd(options())
        self.assertEqual(code, 0)
        self.assertIn('HOST', out)
        self.assertIn('good', out)
        self.assertNotIn('note:', out)

    def test_luna_missing_on_remote(self):
        with self.local(), mock.patch.object(luna, 'fleet_ssh', return_value=(0, 'NOLUNA\n', '')):
            code, out, _ = self.run_cmd(options(hosts=['bare']))
        self.assertEqual(code, 1)
        self.assertIn('Luna is not installed', out)

    def test_ssh_timeout_is_unreachable(self):
        with mock.patch.object(luna.subprocess, 'run', side_effect=subprocess.TimeoutExpired('ssh', 1)):
            code, _, err = luna.fleet_ssh('h', 'x', timeout=1)
        self.assertEqual(code, 255)
        self.assertIn('timed out', err)


class PushCacheTests(FleetTestCase):
    def state(self, **kw):
        base = {'cachedBuild': '100', 'cacheReady': True, 'pinned': '100', 'packagesMatch': True}
        base.update(kw)
        return mock.patch.object(luna, 'steam_state', return_value=base)

    def test_skips_when_remote_has_same_build(self):
        with self.state(), mock.patch.object(luna, 'fleet_ssh', return_value=(0, '100\nHAS_PACKAGE\n', '')), \
                mock.patch.object(luna.subprocess, 'run') as run:
            code, message = luna.fleet_push_cache('h')
        self.assertEqual(code, 0)
        self.assertIn('nothing to push', message)
        run.assert_not_called()

    def test_pushes_when_missing_or_different_build_marker_last(self):
        for remote in ((0, '', ''), (0, '99\nHAS_PACKAGE\n', ''), (0, '100\n', '')):
            with self.state(), mock.patch.object(luna, 'fleet_ssh', return_value=remote), \
                    mock.patch.object(luna.subprocess, 'run') as run:
                run.return_value = subprocess.CompletedProcess([], 0, '', '')
                code, message = luna.fleet_push_cache('h')
            self.assertEqual(code, 0, remote)
            self.assertEqual(run.call_count, 2)
            first, second = run.call_args_list[0][0][0], run.call_args_list[1][0][0]
            self.assertEqual(first[0], 'rsync')
            self.assertIn('/build.txt', first[first.index('--exclude') + 1])
            self.assertIn('--include', second)
            self.assertTrue(first[-1].startswith('h:'))

    def test_dry_run_does_not_rsync(self):
        with self.state(), mock.patch.object(luna, 'fleet_ssh', return_value=(0, '', '')), \
                mock.patch.object(luna.subprocess, 'run') as run:
            code, message = luna.fleet_push_cache('h', dry_run=True)
        self.assertEqual(code, 0)
        self.assertIn('would push', message)
        run.assert_not_called()

    def test_refuses_stale_unverified_or_empty_local_cache(self):
        for kw in ({'cacheReady': False}, {'pinned': '101'}, {'packagesMatch': False}):
            with self.state(**kw), mock.patch.object(luna, 'fleet_ssh') as ssh:
                code, _ = luna.fleet_push_cache('h')
            self.assertEqual(code, 1, kw)
            ssh.assert_not_called()

    def test_unreachable_and_rsync_failure(self):
        with self.state(), mock.patch.object(luna, 'fleet_ssh', return_value=(255, '', 'no route\n')):
            self.assertEqual(luna.fleet_push_cache('h')[0], 1)
        with self.state(), mock.patch.object(luna, 'fleet_ssh', return_value=(0, '', '')), \
                mock.patch.object(luna.subprocess, 'run',
                                  return_value=subprocess.CompletedProcess([], 23, '', 'boom\n')):
            code, message = luna.fleet_push_cache('h')
        self.assertEqual(code, 1)
        self.assertIn('rsync failed', message)


class InstallTests(FleetTestCase):
    def install(self, remote, opts, bundle=Path('/tmp/luna-1.0.tar.gz')):
        calls = []

        def fake_ssh(host, script, timeout=120):
            calls.append(script)
            if script == luna.FLEET_STATUS_SCRIPT:
                return remote
            if script == luna.FLEET_MKTEMP_SCRIPT:
                return 0, '/tmp/luna-fleet.abc\n', ''
            return 0, 'done\n', ''
        with mock.patch.object(luna, 'fleet_ssh', side_effect=fake_ssh), \
                mock.patch.object(luna.subprocess, 'run',
                                  return_value=subprocess.CompletedProcess([], 0, '', '')) as run:
            result = luna.fleet_install_host('h', bundle, opts)
        return result, calls, run

    def test_refuses_when_steam_running(self):
        (ok, message), calls, run = self.install(remote_out(steam=True, probe=probe()), options())
        self.assertFalse(ok)
        self.assertIn('--restart-steam', message)
        self.assertEqual(calls, [luna.FLEET_STATUS_SCRIPT])  # nothing copied or run
        run.assert_not_called()

    def test_refuses_when_lumen_sidecar_running(self):
        (ok, _), _, _ = self.install(remote_out(lumen=True, probe=probe()), options())
        self.assertFalse(ok)

    def test_refuses_drift_without_force_and_legacy_without_force(self):
        (ok, message), _, _ = self.install(remote_out(probe=probe(addons={'luna': 'drift'})), options())
        self.assertFalse(ok)
        self.assertIn('luna', message)
        (ok, message), _, _ = self.install(remote_out(), options())
        self.assertFalse(ok)
        self.assertIn('--force', message)

    def test_installs_when_idle_and_cleans_up(self):
        (ok, message), calls, run = self.install(remote_out(probe=probe()), options(only=['sgdb']))
        self.assertTrue(ok, message)
        self.assertTrue(any('update --bundle' in c and '--only sgdb' in c for c in calls))
        self.assertTrue(any(c.startswith('rm -rf') for c in calls))
        self.assertFalse(any(c == luna.FLEET_START_STEAM_SCRIPT for c in calls))
        self.assertEqual(run.call_args[0][0][0], 'scp')

    def test_restart_steam_stops_installs_relaunches(self):
        (ok, message), calls, _ = self.install(remote_out(steam=True, probe=probe()),
                                               options(restart_steam=True))
        self.assertTrue(ok, message)
        stop = [i for i, c in enumerate(calls) if 'steam -shutdown' in c]
        update = [i for i, c in enumerate(calls) if 'update --bundle' in c]
        start = [i for i, c in enumerate(calls) if c == luna.FLEET_START_STEAM_SCRIPT]
        self.assertTrue(stop and update and start)
        self.assertLess(stop[0], update[0])
        self.assertLess(update[0], start[0])
        self.assertIn('systemd-run --user --quiet --collect steam', luna.FLEET_START_STEAM_SCRIPT)

    def test_steam_that_will_not_exit_blocks_install_but_is_relaunched(self):
        calls = []

        def fake_ssh(host, script, timeout=120):
            calls.append(script)
            if script == luna.FLEET_STATUS_SCRIPT:
                return remote_out(steam=True, probe=probe())
            if script == luna.FLEET_MKTEMP_SCRIPT:
                return 0, '/tmp/luna-fleet.abc\n', ''
            if 'steam -shutdown' in script:
                return 1, '', ''
            return 0, '', ''
        with mock.patch.object(luna, 'fleet_ssh', side_effect=fake_ssh), \
                mock.patch.object(luna.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '', '')):
            ok, message = luna.fleet_install_host('h', Path('/tmp/b.tar.gz'), options(restart_steam=True))
        self.assertFalse(ok)
        self.assertFalse(any('update --bundle' in c for c in calls))
        self.assertIn(luna.FLEET_START_STEAM_SCRIPT, calls)

    def test_force_overrides_drift(self):
        (ok, _), _, _ = self.install(remote_out(probe=probe(addons={'luna': 'drift'})), options(force=True))
        self.assertTrue(ok)

    def test_dry_run_changes_nothing(self):
        (ok, message), calls, run = self.install(remote_out(steam=True, probe=probe()),
                                                 options(restart_steam=True, dry_run=True), bundle=None)
        self.assertTrue(ok)
        self.assertIn('dry-run', message)
        self.assertEqual(calls, [luna.FLEET_STATUS_SCRIPT])
        run.assert_not_called()

    def test_unreachable_host_fails(self):
        (ok, message), _, _ = self.install((255, '', 'refused\n'), options())
        self.assertFalse(ok)
        self.assertIn('unreachable', message)

    def test_cmd_install_summary_exit_code(self):
        self.config.write_text(json.dumps({'hosts': ['a', 'b']}))
        results = {'a': (True, 'installed'), 'b': (False, 'REFUSED: Steam')}
        with mock.patch.object(luna, 'fleet_install_host', side_effect=lambda h, b, o: results[h]):
            code, out, _ = self.run_cmd(options(fleet_action='install', dry_run=True))
        self.assertEqual(code, 1)
        self.assertIn('FAIL', out)
        self.assertIn('ok', out)


class ProbeTests(unittest.TestCase):
    def test_parser_wiring(self):
        parser = luna.build_parser()
        opts = parser.parse_args(['fleet', 'install', 'laptop', '--only', 'sgdb', '--restart-steam', '--dry-run'])
        self.assertEqual((opts.fleet_action, opts.hosts, opts.only, opts.restart_steam), ('install', ['laptop'], ['sgdb'], True))
        self.assertTrue(parser.parse_args(['probe', '--json']).json)


if __name__ == '__main__':
    unittest.main()
