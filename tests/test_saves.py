#!/usr/bin/env python3
"""`luna saves` tests: fake HOMEs (desktop + laptop) sharing one Syncthing store dir."""
import contextlib
import importlib.util
import io
import json
import os
os.environ.setdefault('LUNA_GUI_CHECKS', '0')  # hermetic: tests never probe the real desktop (test_gui_check manages it itself)
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

LUNA = Path(__file__).resolve().parent.parent / 'scripts' / 'luna.py'


def load_luna():
    spec = importlib.util.spec_from_file_location('luna_saves_under_test', str(LUNA))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SavesTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp(prefix='luna-saves-'))
        self.addCleanup(shutil.rmtree, str(self.temp), True)
        self.luna = load_luna()
        self.patch_steam()
        self.shared = self.temp / 'shared-store'  # what Syncthing keeps identical
        self.shared.mkdir()
        self.desktop = self.make_home('desktop')
        self.laptop = self.make_home('laptop')
        self.current = None
        self.use(self.desktop)

    def make_home(self, name):
        home = self.temp / name
        (home / '.local/share').mkdir(parents=True)
        (home / '.config').mkdir()
        os.symlink(str(self.shared), str(home / '.local/share/game-saves'))
        self.add_game(home, '2124120', 'SULFUR')
        return home

    def add_game(self, home, appid, name, installdir=None, library=None):
        library = library or home / '.local/share/Steam'
        apps = library / 'steamapps'
        apps.mkdir(parents=True, exist_ok=True)
        (apps / ('appmanifest_%s.acf' % appid)).write_text(
            '"AppState"\n{\n\t"appid"\t\t"%s"\n\t"name"\t\t"%s"\n\t"installdir"\t\t"%s"\n}\n'
            % (appid, name, installdir or name))

    def use(self, home):
        self.current = home
        patcher = mock.patch.dict(os.environ, {'HOME': str(home)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def patch_steam(self):
        self.steam_running = mock.patch.object(self.luna, 'saves_steam_running', return_value=False).start()
        self.addCleanup(mock.patch.stopall)

    def switch(self, home):
        mock.patch.stopall()
        self.patch_steam()
        self.use(home)

    def prefix(self, home, appid='2124120', library=None):
        library = library or home / '.local/share/Steam'
        user = library / 'steamapps/compatdata' / appid / 'pfx/drive_c/users/steamuser'
        user.mkdir(parents=True, exist_ok=True)
        return user

    def save_dir(self, home, appid='2124120', rel='AppData/LocalLow/Perfect Random', data=None, library=None):
        directory = self.prefix(home, appid, library) / rel
        directory.mkdir(parents=True, exist_ok=True)
        for name, text in (data or {'slot1.sav': 'one'}).items():
            (directory / name).write_text(text)
        return directory

    def run_luna(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = self.luna.main(['saves'] + list(args))
        return code, out.getvalue(), err.getvalue()

    def store_of(self, appid='2124120', rel='AppData/LocalLow/Perfect Random'):
        return self.shared / appid / 'proton' / rel


class ScanTests(SavesTestCase):
    def test_scan_lists_proton_and_skips_noise_and_cloud(self):
        self.save_dir(self.desktop)
        self.save_dir(self.desktop, rel='AppData/Local/Microsoft')
        self.save_dir(self.desktop, rel='AppData/Local/Temp')
        self.add_game(self.desktop, '111', 'Cloudy')
        self.save_dir(self.desktop, '111', rel='AppData/LocalLow/Cloudy Studio')
        cloud = self.desktop / '.local/share/Steam/userdata/1/111'
        cloud.mkdir(parents=True)
        (cloud / 'remotecache.vdf').write_text('x')
        code, out, _ = self.run_luna('scan', '--json')
        rows = json.loads(out)['candidates']
        self.assertEqual([(r['appid'], r['rel'], r['linked']) for r in rows],
                         [('2124120', 'AppData/LocalLow/Perfect Random', False)])

    def test_scan_unity_matched_and_rimworld_skipped(self):
        self.add_game(self.desktop, '1', 'Hollow Knight Silksong')
        unity = self.desktop / '.config/unity3d'
        (unity / 'Team Cherry/Hollow Knight Silksong').mkdir(parents=True)
        (unity / 'Team Cherry/Hollow Knight Silksong/user1.dat').write_text('s')
        (unity / 'Ludeon Studios/RimWorld by Ludeon Studios').mkdir(parents=True)
        (unity / 'Ludeon Studios/RimWorld by Ludeon Studios/a.rws').write_text('r')
        rows = json.loads(self.run_luna('scan', '--json')[1])['candidates']
        self.assertEqual([(r['appid'], r['kind']) for r in rows], [('1', 'config')])
        code, out, _ = self.run_luna('scan')
        self.assertIn('Silksong', out)
        self.assertNotIn('RimWorld', out)


class LinkTests(SavesTestCase):
    def test_link_moves_into_store_and_symlinks(self):
        local = self.save_dir(self.desktop)
        code, out, _ = self.run_luna('link', '2124120')
        self.assertEqual(code, 0, out)
        self.assertTrue(local.is_symlink())
        self.assertEqual((self.store_of() / 'slot1.sav').read_text(), 'one')
        self.assertEqual((local / 'slot1.sav').read_text(), 'one')
        mapped = json.loads((self.shared / 'saves.json').read_text())
        self.assertEqual(mapped['games']['2124120']['links'],
                         [{'kind': 'proton', 'rel': 'AppData/LocalLow/Perfect Random'}])
        # idempotent
        self.assertIn('already linked', self.run_luna('link', '2124120')[1])
        scan = json.loads(self.run_luna('scan', '--json')[1])['candidates']
        self.assertTrue(scan[0]['linked'])

    def test_second_machine_link_all_backs_up_local_and_never_deletes(self):
        self.save_dir(self.desktop, data={'slot1.sav': 'desktop'})
        self.run_luna('link', '2124120')
        self.switch(self.laptop)
        # prefix missing: tell the user to launch the game
        code, out, _ = self.run_luna('link', '--all')
        self.assertIn('launch the game once', out)
        self.assertEqual(code, 0)
        local = self.save_dir(self.laptop, data={'slot1.sav': 'laptop'})
        # both sides have data -> refuse, nothing changes
        code, out, _ = self.run_luna('link', '--all')
        self.assertEqual(code, 1)
        self.assertIn('--prefer', out)
        self.assertFalse(local.is_symlink())
        self.assertEqual((self.store_of() / 'slot1.sav').read_text(), 'desktop')
        # prefer store: laptop copy backed up, symlink created
        code, out, _ = self.run_luna('link', '2124120', '--prefer', 'store')
        self.assertEqual(code, 0, out)
        self.assertTrue(local.is_symlink())
        backups = list(local.parent.glob('Perfect Random.luna-backup-*'))
        self.assertEqual(len(backups), 1)
        self.assertEqual((backups[0] / 'slot1.sav').read_text(), 'laptop')
        self.assertEqual((local / 'slot1.sav').read_text(), 'desktop')

    def test_second_machine_without_local_data_just_symlinks(self):
        self.save_dir(self.desktop)
        self.run_luna('link', '2124120')
        self.switch(self.laptop)
        self.prefix(self.laptop)
        code, out, _ = self.run_luna('link', '--all')
        self.assertEqual(code, 0, out)
        local = self.laptop / '.local/share/Steam/steamapps/compatdata/2124120/pfx/drive_c/users/steamuser/AppData/LocalLow/Perfect Random'
        self.assertTrue(local.is_symlink())
        self.assertEqual((local / 'slot1.sav').read_text(), 'one')

    def test_prefer_local_backs_up_store(self):
        self.save_dir(self.desktop, data={'slot1.sav': 'desktop'})
        self.run_luna('link', '2124120')
        self.switch(self.laptop)
        local = self.save_dir(self.laptop, data={'slot1.sav': 'laptop'})
        code, out, _ = self.run_luna('link', '2124120', '--prefer', 'local')
        self.assertEqual(code, 0, out)
        self.assertEqual((self.store_of() / 'slot1.sav').read_text(), 'laptop')
        backups = list((self.shared / '_backups').iterdir())
        self.assertEqual(len(backups), 1)
        self.assertEqual((backups[0] / 'slot1.sav').read_text(), 'desktop')
        self.assertTrue(local.is_symlink())

    def test_cross_filesystem_copies_verifies_and_keeps_original(self):
        library = self.temp / 'mnt-storage/SteamLibrary'
        self.add_game(self.desktop, '2333000', 'Silver Pines', library=library)
        (self.desktop / '.local/share/Steam/steamapps').mkdir(parents=True, exist_ok=True)
        (self.desktop / '.local/share/Steam/steamapps/libraryfolders.vdf').write_text(
            '"libraryfolders"\n{\n"1"\n{\n\t"path"\t\t"%s"\n}\n}\n' % library)
        local = self.save_dir(self.desktop, '2333000', 'AppData/LocalLow/Wych Elm',
                              {'a.sav': 'x'}, library=library)
        (local / 'sub').mkdir()
        (local / 'sub/b.sav').write_text('deep')
        with mock.patch.object(self.luna, 'saves_same_fs', lambda a, b: False):
            code, out, _ = self.run_luna('link', '2333000')
        self.assertEqual(code, 0, out)
        self.assertTrue(local.is_symlink())
        store = self.store_of('2333000', 'AppData/LocalLow/Wych Elm')
        self.assertEqual((store / 'sub/b.sav').read_text(), 'deep')
        backups = list(local.parent.glob('Wych Elm.luna-backup-*'))
        self.assertEqual(len(backups), 1)
        self.assertEqual((backups[0] / 'a.sav').read_text(), 'x')

    def test_running_game_refused(self):
        local = self.save_dir(self.desktop)
        with mock.patch.object(self.luna, 'saves_game_running', lambda appid: True):
            code, out, _ = self.run_luna('link', '2124120')
        self.assertEqual(code, 1)
        self.assertIn('running', out)
        self.assertFalse(local.is_symlink())

    def test_steam_running_refuses_proton_but_not_native(self):
        local = self.save_dir(self.desktop)
        self.steam_running.return_value = True
        code, out, _ = self.run_luna('link', '2124120')
        self.assertEqual(code, 1)
        self.assertIn('Steam is running', out)
        self.assertFalse(local.is_symlink())
        game = self.desktop / '.config/unity3d/Studio X/Game Y'
        game.mkdir(parents=True)
        (game / 'p.dat').write_text('p')
        code, out, _ = self.run_luna('link', str(game))
        self.assertEqual(code, 0, out)
        self.steam_running.return_value = False
        self.assertEqual(self.run_luna('link', '2124120')[0], 0)
        self.steam_running.return_value = True
        code, out, _ = self.run_luna('unlink', '2124120')
        self.assertEqual(code, 1)
        self.assertIn('Steam is running', out)
        self.assertTrue(local.is_symlink())

    def test_dry_run_changes_nothing(self):
        local = self.save_dir(self.desktop)
        code, out, _ = self.run_luna('link', '2124120', '--dry-run')
        self.assertEqual(code, 0)
        self.assertFalse(local.is_symlink())
        self.assertFalse((self.shared / 'saves.json').exists())

    def test_link_unity_path_and_foreign_path_refused(self):
        game = self.desktop / '.config/unity3d/Studio X/Game Y'
        game.mkdir(parents=True)
        (game / 'p.dat').write_text('p')
        code, out, _ = self.run_luna('link', str(game))
        self.assertEqual(code, 0, out)
        self.assertTrue(game.is_symlink())
        self.assertEqual((self.shared / 'studio-x-game-y/config/unity3d/Studio X/Game Y/p.dat').read_text(), 'p')
        code, _, err = self.run_luna('link', '/etc')
        self.assertNotEqual(code, 0)


class UnlinkStatusTests(SavesTestCase):
    def test_unlink_restores_real_dir_and_keeps_store(self):
        local = self.save_dir(self.desktop)
        self.run_luna('link', '2124120')
        code, out, _ = self.run_luna('unlink', '2124120')
        self.assertEqual(code, 0, out)
        self.assertFalse(local.is_symlink())
        self.assertEqual((local / 'slot1.sav').read_text(), 'one')
        self.assertTrue((self.store_of() / 'slot1.sav').exists())
        self.assertIn('not linked here', self.run_luna('unlink', '2124120')[1])

    def test_status_reports_broken_link_and_conflicts_and_doctor(self):
        local = self.save_dir(self.desktop)
        self.run_luna('link', '2124120')
        code, out, _ = self.run_luna('status')
        self.assertEqual(code, 0)
        self.assertIn('LINKED', out)
        (self.store_of() / 'slot1.sav.sync-conflict-20260101-000000-ABC').write_text('c')
        self.assertEqual(self.run_luna('status')[0], 1)
        found = {f['id']: f for f in self.luna._doctor_saves()}
        self.assertEqual(found['saves.conflicts']['severity'], 'warn')
        shutil.rmtree(str(self.store_of()))
        data = json.loads(self.run_luna('status', '--json')[1])
        self.assertEqual(data['links'][0]['state'], 'store-missing')
        found = {f['id']: f for f in self.luna._doctor_saves()}
        self.assertEqual(found['saves.link']['severity'], 'warn')

    def test_doctor_quiet_without_links_and_info_when_unlinked(self):
        self.assertEqual(self.luna._doctor_saves(), [])
        self.save_dir(self.desktop)
        self.run_luna('link', '2124120')
        self.switch(self.laptop)
        self.prefix(self.laptop)
        found = {f['id']: f for f in self.luna._doctor_saves()}
        self.assertEqual(found['saves.unlinked']['severity'], 'info')


if __name__ == '__main__':
    unittest.main()
