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
        patcher = mock.patch.dict(os.environ, {'LUNA_FLEET_CONFIG': str(self.config),
                                               'LUNA_SIGNING_DIR': str(Path(temp.name) / 'signing')})
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
        self.run_cmd(options(fleet_action='add', hosts=['me@host-1.example.invalid']))
        self.assertEqual(json.loads(self.config.read_text())['hosts'], ['laptop', 'me@host-1.example.invalid'])
        self.assertIn('remote  laptop', self.run_cmd(options(fleet_action='list'))[1])
        self.assertEqual(self.run_cmd(options(fleet_action='remove', hosts=['laptop']))[0], 0)
        self.assertEqual(luna.fleet_load_hosts(), ['me@host-1.example.invalid'])
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
    def install(self, remote, opts, bundle=Path('/tmp/luna-1.0.tar.gz'), mktemp='/tmp/luna-fleet.AbC123\n'):
        calls = []

        def fake_ssh(host, script, timeout=120):
            calls.append(script)
            if script == luna.FLEET_STATUS_SCRIPT:
                return remote
            if script == luna.FLEET_MKTEMP_SCRIPT:
                return 0, mktemp, ''
            return 0, 'done\n', ''
        with mock.patch.object(luna, 'fleet_ssh', side_effect=fake_ssh), \
                mock.patch.object(luna.subprocess, 'run',
                                  return_value=subprocess.CompletedProcess([], 0, '', '')) as run:
            result = luna.fleet_install_host('h', bundle, opts)
        return result, calls, run

    # -- review finding 5: nothing unvalidated reaches a remote script ----------
    def test_a_hostile_remote_temp_dir_is_refused_before_anything_is_sent(self):
        for hostile in ("/tmp/a'b'c/luna-fleet.AbC123", "/tmp/../etc/luna-fleet.AbC123", '/tmp/luna-fleet.abc',
                        '/tmp/luna-fleet.AbC123; rm -rf ~', '/tmp/luna-fleet.AbC123\n/etc', 'luna-fleet.AbC123',
                        '/tmp/luna fleet/luna-fleet.AbC123', '/tmp/luna-fleet.AbC12$', '/', '/tmp/x/../luna-fleet.AbC123',
                        '/tmp/luna-fleet.AbC123/extra'):
            mktemp = hostile if '\n' in hostile else hostile + '\n'
            (ok, message), calls, run = self.install(remote_out(probe=probe()), options(), mktemp=mktemp)
            self.assertFalse(ok, hostile)
            self.assertIn('temp dir', message, hostile)
            self.assertFalse(any(c.startswith('rm -rf') for c in calls), hostile)
            self.assertEqual(calls, [luna.FLEET_STATUS_SCRIPT, luna.FLEET_MKTEMP_SCRIPT], hostile)
            run.assert_not_called()
        # the last output line wins when the remote profile prints noise first
        (ok, message), _, _ = self.install(remote_out(probe=probe()), options(),
                                           mktemp='welcome\n/home/u/tmp/luna-fleet.AbC123\n')
        self.assertTrue(ok, message)

    def test_host_names_may_not_carry_a_trailing_newline(self):
        for bad in ('laptop\n', 'u@laptop\n', 'laptop\n\n'):
            self.assertFalse(luna.fleet_valid_host(bad), repr(bad))
            self.assertIsNone(luna.FLEET_HOST_RE.fullmatch(bad), repr(bad))

    def test_the_bundle_name_and_add_on_names_are_checked_before_use(self):
        (ok, message), calls, run = self.install(remote_out(probe=probe()), options(),
                                                 bundle=Path("/tmp/luna-1.0';touch x;'.tar.gz"))
        self.assertFalse(ok)
        self.assertIn('bundle name', message)
        self.assertFalse(any('update --bundle' in c for c in calls))
        (ok, message), calls, _ = self.install(remote_out(probe=probe()), options(only=["sgdb';touch x;'"]))
        self.assertFalse(ok)
        self.assertIn('add-on', message)
        self.assertFalse(any('update --bundle' in c for c in calls))

    def test_every_remote_script_value_is_a_validated_literal(self):
        (ok, _), calls, _ = self.install(remote_out(probe=probe()), options(only=['sgdb', 'luna'], stop_timeout=5))
        self.assertTrue(ok)
        update = [c for c in calls if 'update --bundle' in c][0]
        self.assertEqual(update.count('\n'), 1)
        self.assertIn("--bundle '/tmp/luna-fleet.AbC123/luna-1.0.tar.gz'", update)

    def test_the_stop_timeout_is_forced_to_an_int(self):
        (ok, _), calls, _ = self.install(remote_out(steam=True, probe=probe()),
                                         options(restart_steam=True, stop_timeout='5; reboot'))
        stop = [c for c in calls if 'steam -shutdown' in c]
        self.assertTrue(not ok or all('reboot' not in c for c in stop))
        self.assertFalse(any('reboot' in c for c in calls))

    def test_trust_key_blob_is_checked_again_at_the_last_moment(self):
        with mock.patch.object(luna, 'signing_parse_pubkey', return_value=("AAAA'; touch /tmp/x; '", '')), \
                mock.patch.object(luna, 'fleet_ssh') as ssh:
            ok, message = luna.fleet_trust_signing_key('h')
        self.assertFalse(ok)
        ssh.assert_not_called()

    # -- rollback protection -----------------------------------------------------
    def test_allow_downgrade_is_forwarded_only_when_the_user_gave_it(self):
        (ok, _), calls, _ = self.install(remote_out(probe=probe()), options())
        self.assertTrue(ok)
        self.assertFalse(any('--allow-downgrade' in c for c in calls))
        (ok, _), calls, _ = self.install(remote_out(probe=probe()), options(allow_downgrade=True))
        self.assertTrue(ok)
        update = [c for c in calls if 'update --bundle' in c]
        self.assertEqual(len(update), 1)
        self.assertIn('--allow-downgrade', update[0])
        (ok, message), _, _ = self.install(remote_out(probe=probe()), options(allow_downgrade=True, dry_run=True), bundle=None)
        self.assertIn('--allow-downgrade', message)

    def test_a_downgrade_refusal_on_the_host_is_reported(self):
        def fake_ssh(host, script, timeout=120):
            if script == luna.FLEET_STATUS_SCRIPT:
                return remote_out(probe=probe())
            if script == luna.FLEET_MKTEMP_SCRIPT:
                return 0, '/tmp/luna-fleet.AbC123\n', ''
            if 'update --bundle' in script:
                return 1, '', 'REFUSING: bundle 0.1.0 is older than 0.2.0\n'
            return 0, '', ''
        with mock.patch.object(luna, 'fleet_ssh', side_effect=fake_ssh), \
                mock.patch.object(luna.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '', '')):
            ok, message = luna.fleet_install_host('h', Path('/tmp/luna-1.0.tar.gz'), options())
        self.assertFalse(ok)
        self.assertIn('older than', message)

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

    def test_copies_the_signature_only_when_it_exists(self):
        with tempfile.TemporaryDirectory(prefix='luna-fleet-sig-') as work:
            bundle = Path(work) / 'luna-1.0.tar.gz'
            bundle.write_bytes(b'x')
            (_, _), _, run = self.install(remote_out(probe=probe()), options(), bundle=bundle)
            self.assertNotIn(str(bundle) + '.sig', run.call_args[0][0])
            Path(str(bundle) + '.sig').write_bytes(b'sig')
            (ok, message), _, run = self.install(remote_out(probe=probe()), options(), bundle=bundle)
            self.assertTrue(ok, message)
            scp = run.call_args[0][0]
            self.assertIn(str(bundle) + '.sig', scp)
            self.assertIn(str(bundle) + '.sha256', scp)

    def test_trust_signing_key_is_opt_in_and_sends_only_the_public_key(self):
        import base64
        blob = base64.b64encode(luna.SIGN_BLOB_PREFIX + bytes(range(32))).decode()
        with tempfile.TemporaryDirectory(prefix='luna-fleet-key-') as work:
            (Path(work) / (luna.SIGN_KEY_NAME + '.pub')).write_text('ssh-ed25519 %s luna-release\n' % blob)
            (Path(work) / luna.SIGN_KEY_NAME).write_text('PRIVATE-DO-NOT-SEND')
            with mock.patch.dict(os.environ, {'LUNA_SIGNING_DIR': work}):
                (_, _), plain, _ = self.install(remote_out(probe=probe()), options())
                (ok, message), calls, _ = self.install(remote_out(probe=probe()),
                                                       options(trust_signing_key=True))
        self.assertFalse(any('signing trust' in c for c in plain))
        self.assertTrue(ok, message)
        trust = [c for c in calls if 'signing trust' in c]
        self.assertEqual(len(trust), 1)
        self.assertIn(blob, trust[0])
        self.assertNotIn('PRIVATE', ''.join(calls))
        self.assertLess(max(i for i, c in enumerate(calls) if 'update --bundle' in c),
                        calls.index(trust[0]))

    def test_trust_signing_key_without_a_local_key_fails_visibly(self):
        with tempfile.TemporaryDirectory(prefix='luna-fleet-nokey-') as work, \
                mock.patch.dict(os.environ, {'LUNA_SIGNING_DIR': work}):
            (ok, message), _, _ = self.install(remote_out(probe=probe()), options(trust_signing_key=True))
        self.assertFalse(ok)
        self.assertIn('NOT trusted', message)

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
                return 0, '/tmp/luna-fleet.AbC123\n', ''
            if 'steam -shutdown' in script:
                return 1, '', ''
            return 0, '', ''
        with mock.patch.object(luna, 'fleet_ssh', side_effect=fake_ssh), \
                mock.patch.object(luna.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '', '')):
            ok, message = luna.fleet_install_host('h', Path('/tmp/luna-b.tar.gz'), options(restart_steam=True))
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



GUI_OK = {'tool': '/usr/bin/agent-desktop', 'session': {'WAYLAND_DISPLAY': 'wayland-1'}, 'steamRunning': True,
          'accessible': True, 'screen': 'main', 'windows': ['Steam'], 'backend': {'name': 'niri', 'windows': 'niri'}}


def gui_out(info=None, rc=0, raw=None):
    body = raw if raw is not None else json.dumps(info if info is not None else GUI_OK, indent=2)
    return 0, 'GUIRC %d\nGUI %s\n' % (rc, body.replace('\n', '')), ''


def verify_out(rc, data):
    return 0, 'VERIFYRC %d\nVERIFY %s\n' % (rc, json.dumps(data)), ''


class FleetGuiTests(FleetTestCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(luna, 'gui_status_info', return_value=dict(GUI_OK, steamRunning=False,
                                                                                accessible=False, screen='n/a'))
        patcher.start()
        self.addCleanup(patcher.stop)

    def gui(self, answers, verify=False, **kw):
        """Run `fleet gui` with canned ssh answers {host: {script: result}} or {host: result}."""
        calls = []

        def fake(host, script, timeout=120):
            calls.append((host, script))
            answer = answers[host]
            if isinstance(answer, dict):
                return answer[script]
            return answer
        with mock.patch.object(luna, 'fleet_ssh', side_effect=fake):
            code, out, err = self.run_cmd(options(fleet_action='gui', json=True, verify=verify,
                                                  hosts=list(answers), **kw))
        return code, json.loads(out), calls

    def test_good_host_and_local_steam_not_running(self):
        code, data, _ = self.gui({'h1': gui_out()})
        by = {r['host']: r for r in data['hosts']}
        self.assertEqual(code, 0)  # a stopped local Steam is a state, not a failure
        local = [r for r in data['hosts'] if r['local']][0]
        self.assertFalse(local['steamRunning'])
        r = by['h1']
        self.assertEqual((r['state'], r['agentDesktop'], r['steamRunning'], r['accessible'], r['screen']),
                         ('ok', True, True, True, 'main'))
        self.assertEqual(r['backend']['name'], 'niri')
        self.assertEqual(r['blockers'], [])
        self.assertNotIn('verify', r)

    def test_unreachable_host(self):
        code, data, _ = self.gui({'h1': (255, '', 'ssh: connect to host: timed out\n')})
        self.assertEqual(code, 1)
        self.assertEqual(data['hosts'][1]['state'], 'unreachable')
        self.assertIn('h1: unreachable', '\n'.join(data['notes']))

    def test_old_remote_luna_is_a_row_not_a_crash(self):
        for answer in [(0, 'GUIRC 2\nGUI \n', ''), gui_out(raw='usage: luna [-h] ...', rc=0),
                       gui_out(raw='[1, 2]'), (0, 'GUI {"tool": 1}\n', '')]:
            code, data, _ = self.gui({'h1': answer})
            self.assertEqual(data['hosts'][1]['state'], 'luna-too-old', answer)
            self.assertEqual(code, 1)
            self.assertIn('luna fleet install h1', '\n'.join(data['notes']))

    def test_no_luna(self):
        code, data, _ = self.gui({'h1': (0, 'NOLUNA\n', '')})
        self.assertEqual((code, data['hosts'][1]['state']), (1, 'no-luna'))

    def test_oversized_and_garbage_output(self):
        big = (0, 'GUIRC 0\nGUI ' + 'x' * (luna.FLEET_GUI_MAX_OUTPUT + 10) + '\n', '')
        code, data, _ = self.gui({'h1': big})
        self.assertEqual((code, data['hosts'][1]['state']), (1, 'bad-output'))
        code, data, _ = self.gui({'h1': (0, '\x1b[31mgarbage\x00\n', '')})
        self.assertEqual(data['hosts'][1]['state'], 'luna-too-old')

    def test_untrusted_fields_are_typed_and_sanitised(self):
        evil = dict(GUI_OK, steamRunning='yes', accessible=1, screen='main\x1b[2J', windows=['Steam', 'Ev\x1bil', 7],
                    backend='nope', tool=['x'])
        _, data, _ = self.gui({'h1': gui_out(evil)})
        r = data['hosts'][1]
        self.assertIsNone(r['steamRunning'])
        self.assertIsNone(r['accessible'])
        self.assertNotIn('\x1b', json.dumps(r, ensure_ascii=False))
        self.assertFalse(r['agentDesktop'])
        self.assertEqual(r['backend'], {})

    def test_steam_not_running_and_not_accessible_skip_verify_with_a_reason(self):
        answers = {'down': gui_out(dict(GUI_OK, steamRunning=False, accessible=False, screen='n/a', windows=[])),
                   'normal': gui_out(dict(GUI_OK, accessible=False))}
        code, data, calls = self.gui(answers, verify=True)
        by = {r['host']: r for r in data['hosts']}
        self.assertEqual(code, 0)  # skips alone do not fail
        self.assertEqual(by['down']['verify']['result'], 'skipped')
        self.assertIn('not running', by['down']['verify']['reason'])
        self.assertIn('accessible', by['normal']['verify']['reason'])
        self.assertIn('normal mode', '\n'.join(data['notes']))
        self.assertFalse(any(s == luna.FLEET_GUI_VERIFY_SCRIPT for _, s in calls))

    def test_missing_agent_desktop_login_and_blockers_notes(self):
        answers = {'a': gui_out(dict(GUI_OK, tool='missing')),
                   'b': gui_out(dict(GUI_OK, screen='login', windows=['Sign in to Steam'])),
                   'c': gui_out(dict(GUI_OK, windows=['Steam', 'Friends List', 'Settings']))}
        code, data, _ = self.gui(answers)
        notes = '\n'.join(data['notes'])
        self.assertEqual(code, 0)
        self.assertIn('a: agent-desktop is missing', notes)
        self.assertIn('b: Steam is at the sign-in screen', notes)
        self.assertIn('c: open window(s) in front of Steam: "Friends List", "Settings"', notes)
        self.assertEqual({r['host']: r['blockers'] for r in data['hosts']}['b'], [])

    def test_verify_pass_fail_skip_mapping(self):
        v = luna.FLEET_GUI_VERIFY_SCRIPT
        cases = [(verify_out(0, {'result': 'pass'}), 'pass', 0),
                 (verify_out(1, {'result': 'fail', 'failures': ['luna panel: missing', 'b', 'c', 'd']}), 'fail', 1),
                 (verify_out(5, {'result': 'skipped', 'reason': 'GUI checks off'}), 'skipped', 0),
                 ((0, 'VERIFYRC 2\nVERIFY \n', ''), 'fail', 1),  # old luna: no --json
                 (verify_out(7, {'result': 'pass'}), 'fail', 1)]  # a bogus code never reads as a pass
        for answer, result, code in cases:
            got, data, _ = self.gui({'h1': {luna.FLEET_GUI_SCRIPT: gui_out(), v: answer}}, verify=True)
            verdict = data['hosts'][1]['verify']
            self.assertEqual((verdict['result'], got), (result, code), answer)
            if result == 'fail':
                self.assertLessEqual(len(verdict['failures']), 3)
                self.assertTrue(verdict['failures'])
            if result == 'skipped':
                self.assertEqual(verdict['reason'], 'GUI checks off')

    def test_local_verify_uses_the_local_function(self):
        local = dict(GUI_OK)
        summary = {'result': 'fail', 'failures': ['sgdb panel: missing']}
        with mock.patch.object(luna, 'gui_status_info', return_value=local), \
             mock.patch.object(luna, 'gui_verify_data', return_value=(1, summary)) as verify, \
             mock.patch.object(luna, 'fleet_ssh') as ssh:
            code, out, _ = self.run_cmd(options(fleet_action='gui', json=True, verify=True))
        data = json.loads(out)
        self.assertEqual(code, 1)
        self.assertEqual(data['hosts'][0]['verify']['failures'], ['sgdb panel: missing'])
        verify.assert_called_once()
        ssh.assert_not_called()

    def test_text_table_has_verify_column_only_when_asked(self):
        with mock.patch.object(luna, 'fleet_ssh', return_value=gui_out()):
            _, plain, _ = self.run_cmd(options(fleet_action='gui', hosts=['h1']))
        with mock.patch.object(luna, 'fleet_ssh', side_effect=lambda h, s, timeout=120:
                               verify_out(0, {}) if s == luna.FLEET_GUI_VERIFY_SCRIPT else gui_out()):
            _, withv, _ = self.run_cmd(options(fleet_action='gui', hosts=['h1'], verify=True))
        self.assertNotIn('VERIFY', plain)
        self.assertIn('VERIFY', withv)
        self.assertIn('pass', withv)

    def test_json_shape(self):
        _, data, _ = self.gui({'h1': gui_out()})
        self.assertEqual(sorted(data), ['hosts', 'notes', 'verify'])
        self.assertFalse(data['verify'])
        for key in ('host', 'local', 'reachable', 'state', 'agentDesktop', 'steamRunning', 'accessible',
                    'screen', 'windows', 'blockers', 'backend'):
            self.assertIn(key, data['hosts'][1])

    def test_remote_scripts_never_start_or_stop_steam_or_change_mode(self):
        for script in (luna.FLEET_GUI_SCRIPT, luna.FLEET_GUI_VERIFY_SCRIPT):
            for forbidden in ('gui launch', '--launch', 'steam -', 'steam.sh', 'pkill', 'kill', 'shutdown',
                              'steam://', 'restart', 'systemctl', 'setsid', 'nohup', 'rm '):
                self.assertNotIn(forbidden, script)
        self.assertIn('gui status --json', luna.FLEET_GUI_SCRIPT)
        self.assertIn('gui verify --json', luna.FLEET_GUI_VERIFY_SCRIPT)

    def test_host_only_reaches_ssh_as_argv_after_double_dash(self):
        with mock.patch.object(luna.subprocess, 'run') as run:
            run.return_value = mock.Mock(returncode=0, stdout='NOLUNA\n', stderr='')
            self.run_cmd(options(fleet_action='gui', hosts=['laptop']))
        command = run.call_args[0][0]
        self.assertEqual(command[command.index('--') + 1], 'laptop')
        self.assertNotIn('laptop', run.call_args[1]['input'])

    def test_invalid_host_is_refused(self):
        code, _, err = self.run_cmd(options(fleet_action='gui', hosts=['a;b']))
        self.assertEqual(code, luna.EXIT_REFUSED)


class GuiVerifyJsonTests(unittest.TestCase):
    def test_json_summary_for_a_skip_keeps_exit_code_5(self):
        with mock.patch.object(luna, 'gui_unavailable_reason', return_value='Steam is not running'):
            code, summary = luna.gui_verify_data(argparse.Namespace(json=True))
        self.assertEqual((code, summary['result'], summary['reason']), (5, 'skipped', 'Steam is not running'))
        out = io.StringIO()
        with mock.patch.object(luna, 'gui_unavailable_reason', return_value='Steam is not running'), \
             contextlib.redirect_stdout(out):
            self.assertEqual(luna.gui_verify(argparse.Namespace(json=True)), 5)
        self.assertEqual(json.loads(out.getvalue())['result'], 'skipped')  # stdout is one JSON document


if __name__ == '__main__':
    unittest.main()
