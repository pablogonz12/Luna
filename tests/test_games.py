"""luna games with fake Steam libraries and mocked ssh; nothing touches real Steam or the network."""
import contextlib
import importlib.util
import io
import json
import os
os.environ.setdefault('LUNA_GUI_CHECKS', '0')  # hermetic: tests never probe the real desktop (test_gui_check manages it itself)
os.environ.setdefault('LUNA_LIVE_CHECKS', '0')  # hermetic: tests never contact a real Steam (test_live_probe uses a fake endpoint)
from pathlib import Path
import tempfile
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location('luna_games_test', Path(__file__).resolve().parents[1] / 'scripts/luna.py')
luna = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(luna)

GB = 10 ** 9


def acf(appid, name, size, flags=4):
    return ('"AppState"\n{\n\t"appid"\t\t"%d"\n\t"name"\t\t"%s"\n\t"StateFlags"\t\t"%d"\n'
            '\t"SizeOnDisk"\t\t"%d"\n}\n' % (appid, name.replace('"', '\\"'), flags, size))


def make_library(root, games, extra=None):
    steamapps = Path(root) / 'steamapps'
    steamapps.mkdir(parents=True, exist_ok=True)
    for appid, name, size, *flags in games:
        (steamapps / ('appmanifest_%d.acf' % appid)).write_text(acf(appid, name, size, *flags))
    return steamapps


class FakeSteam:
    def __init__(self, tmp):
        self.home = Path(tmp)
        self.steam = self.home / '.local/share/Steam'
        (self.steam / 'config/stplug-in').mkdir(parents=True)
        self.second = self.home / 'storage'

    def setup(self):
        make_library(self.steam, [(10, 'Alpha', 5 * GB), (20, 'Beta', 30 * GB), (30, 'Proton 9.0', GB),
                                  (40, 'Partial', GB, 1026)])
        make_library(self.second, [(50, 'Gamma "Q"', 2 * GB)])
        (self.steam / 'steamapps/libraryfolders.vdf').write_text(
            '"libraryfolders"\n{\n\t"0"\n\t{\n\t\t"path"\t\t"%s"\n\t}\n\t"1"\n\t{\n\t\t"path"\t\t"%s"\n\t}\n}\n'
            % (self.steam, self.second))
        (self.steam / 'steamapps/compatdata/10').mkdir(parents=True)
        (self.steam / 'config/stplug-in/20.lua').write_text('addappid(20)\n')
        (self.steam / 'config/stplug-in/notes.txt').write_text('x')


def remote_script_out(apps, lua=(), free=100 * GB, steam=True):
    lines = ['FREE\t%d' % free]
    for appid, name, size, flags, pfx in apps:
        lines.append('APP\t%d\t%d\t%d\t%d\t%s' % (appid, size, flags, pfx, name))
    lines.append('LUA\t' + ' '.join(map(str, lua)) + ' ')
    lines.append('STEAMRUN %d' % steam)
    return 0, '\n'.join(lines) + '\n', ''


class GamesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fake = FakeSteam(self.tmp.name)
        self.fake.setup()
        patch = mock.patch.dict(os.environ, {'HOME': self.tmp.name,
                                             'LUNA_FLEET_CONFIG': str(Path(self.tmp.name) / 'fleet.json')})
        patch.start()
        self.addCleanup(patch.stop)

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = luna.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_scan_parses_libraries_and_skips_tools_and_partials(self):
        scan = luna.games_scan_local()
        self.assertEqual(sorted(scan['items']), [10, 20, 50])
        self.assertTrue(scan['items'][10]['pfx'])
        self.assertFalse(scan['items'][20]['pfx'])
        self.assertTrue(scan['items'][20]['lua'])
        self.assertFalse(scan['items'][10]['lua'])
        self.assertEqual(scan['items'][50]['name'], 'Gamma "Q"')
        self.assertEqual(scan['items'][50]['lib'], str(self.fake.second))
        self.assertEqual(scan['lua'], {20})

    def test_list_json(self):
        code, out, _ = self.run_cli('games', 'list', '--json')
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual([g['id'] for g in data['games']], [10, 20, 50])
        code, out, _ = self.run_cli('games', 'list')
        self.assertIn('3 game(s)', out)

    def test_probe_block_roundtrip(self):
        block = luna.games_probe_block()
        parsed = luna.games_from_probe(json.loads(json.dumps(block)))
        self.assertEqual(sorted(parsed['items']), [10, 20, 50])
        self.assertTrue(parsed['items'][20]['lua'])
        self.assertIsNone(luna.games_from_probe({'items': 'bad'}))
        self.assertIn('games', luna.probe_data())

    def test_parse_remote_fallback_and_probe(self):
        code, out, err = remote_script_out([(10, 'Alpha', 5 * GB, 4, 1), (60, 'Tool', GB, 4, 0),
                                            (70, 'Proton 9.0', GB, 4, 0), (80, 'Half', GB, 2, 0)], lua=[10, 99])
        record = luna.games_parse_remote(code, out, err)
        self.assertEqual(sorted(record['items']), [10, 60])
        self.assertTrue(record['items'][10]['pfx'])
        self.assertTrue(record['items'][10]['lua'])
        self.assertEqual(record['lua'], {10, 99})
        self.assertEqual(record['free'], 100 * GB)
        self.assertTrue(record['steamRunning'])
        probe_line = 'PROBE ' + json.dumps({'games': luna.games_probe_block()}) + '\nSTEAMRUN 0\n'
        record = luna.games_parse_remote(0, probe_line, '')
        self.assertEqual(sorted(record['items']), [10, 20, 50])
        self.assertFalse(record['steamRunning'])
        self.assertFalse(luna.games_parse_remote(255, '', 'no route\n')['reachable'])

    def test_parse_remote_unescapes_vdf_names(self):
        raw = 'The \\"Best\\" Game \\\\ 2'
        out = 'APP\t10\t5\t4\t0\t%s\nSTEAMRUN 1\n' % raw
        record = luna.games_parse_remote(0, out, '')
        self.assertEqual(record['items'][10]['name'], 'The "Best" Game \\ 2')

    def test_diff_flags_blocked_and_too_big(self):
        remote = luna.games_parse_remote(*remote_script_out([(10, 'Alpha', 5 * GB, 4, 0), (99, 'Only There', GB, 4, 0)],
                                                            lua=[], free=10 * GB))
        result = luna.games_diff(luna.games_scan_local(), remote)
        rows = {r['appid']: r for r in result['games']}
        self.assertEqual(rows[10]['where'], 'both')
        self.assertEqual(rows[99]['where'], 'remote')
        self.assertTrue(rows[20]['blocked'])          # unlock-only, no lua on target
        self.assertFalse(rows[20]['fits'])            # 30 GB > 10 GB free
        self.assertFalse(rows[50]['blocked'])
        self.assertTrue(rows[50]['fits'])
        summary = result['summary']
        self.assertEqual((summary['both'], summary['missingOnTarget'], summary['blocked'], summary['targetOnly']),
                         (1, 2, 1, 1))
        self.assertEqual(summary['missingBytes'], 32 * GB)

    def test_diff_command_uses_ssh_and_json(self):
        with mock.patch.object(luna, 'fleet_ssh', return_value=remote_script_out([], lua=[20])) as ssh:
            code, out, _ = self.run_cli('games', 'diff', 'laptop', '--json')
        self.assertEqual(code, 0)
        self.assertEqual(ssh.call_args[0][0], 'laptop')
        data = json.loads(out)
        self.assertEqual(data['summary']['blocked'], 0)
        with mock.patch.object(luna, 'fleet_ssh', return_value=(255, '', 'down\n')):
            self.assertEqual(self.run_cli('games', 'diff', 'laptop')[0], 1)
        self.assertEqual(self.run_cli('games', 'diff', 'bad host;rm')[0], luna.EXIT_REFUSED)

    def test_install_refuses_missing_lua_and_space(self):
        with mock.patch.object(luna, 'fleet_ssh', return_value=remote_script_out([], lua=[], free=10 * GB)) as ssh:
            code, _, err = self.run_cli('games', 'install', 'laptop', '20', '--dry-run')
            self.assertEqual(code, luna.EXIT_REFUSED)
            self.assertIn('stplug-in/20.lua', err)
            code, _, err = self.run_cli('games', 'install', 'laptop', '50', '10', '--dry-run')
            self.assertEqual(code, 0)
            self.assertEqual(ssh.call_count, 2)  # one probe per command, nothing installed
        with mock.patch.object(luna, 'fleet_ssh', return_value=remote_script_out([], lua=[20], free=4 * GB)):
            code, _, err = self.run_cli('games', 'install', 'laptop', '20', '--dry-run')
            self.assertEqual(code, luna.EXIT_REFUSED)
            self.assertIn('only 4.0 GB free', err)

    def test_install_needs_steam_running(self):
        calls = []

        def fake_ssh(host, script, timeout=120):
            calls.append(script)
            return remote_script_out([], lua=[20], steam=False) if 'APP' in script or 'STEAMRUN' in script \
                else (0, '', '')

        with mock.patch.object(luna, 'fleet_ssh', side_effect=fake_ssh):
            code, _, err = self.run_cli('games', 'install', 'laptop', '20')
        self.assertEqual(code, luna.EXIT_REFUSED)
        self.assertIn('Steam is not running', err)
        self.assertEqual(len(calls), 1)

    def test_install_queues_via_steam_uri(self):
        calls = []

        def fake_ssh(host, script, timeout=120):
            calls.append(script)
            if 'STEAMRUN' in script:
                return remote_script_out([(10, 'Alpha', GB, 4, 0)], lua=[20], steam=True)
            return 0, '', ''

        with mock.patch.object(luna, 'fleet_ssh', side_effect=fake_ssh):
            code, out, _ = self.run_cli('games', 'install', 'laptop', '20', '10', '50')
        self.assertEqual(code, 0)
        self.assertIn('already installed', out)
        uris = [c for c in calls if 'steam://install/' in c]
        self.assertEqual(len(uris), 2)
        self.assertIn('systemd-run --user --quiet --collect steam steam://install/20', uris[0])
        self.assertIn('systemd-run --user --quiet --collect steam steam://install/50', uris[1])

    def test_install_rejects_bad_arguments(self):
        self.assertEqual(self.run_cli('games', 'install', 'laptop')[0], luna.EXIT_REFUSED)
        self.assertEqual(self.run_cli('games', 'install', 'laptop', '12; rm')[0], luna.EXIT_REFUSED)


if __name__ == '__main__':
    unittest.main()
