"""luna proton with a temp HOME and a mocked network; never touches the real Steam."""
import hashlib
import importlib.util
import io
import json
import os
os.environ.setdefault('LUNA_GUI_CHECKS', '0')  # hermetic: tests never probe the real desktop (test_gui_check manages it itself)
os.environ.setdefault('LUNA_LIVE_CHECKS', '0')  # hermetic: tests never contact a real Steam (test_live_probe uses a fake endpoint)
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest import mock
import urllib.error

SPEC = importlib.util.spec_from_file_location('luna_proton_test', Path(__file__).resolve().parents[1] / 'scripts/luna.py')
luna = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(luna)

TAG = 'GE-Proton12-1'
CONFIG = '''"InstallConfigStore"
{
	"Software"
	{
		"Valve"
		{
			"Steam"
			{
				"CompatToolMapping"
				{
					"100"
					{
						"name"		"GE-Proton9-1"
						"config"		""
					}
					"200"
					{
						"name"		"proton_9"
					}
					"300"
					{
						"name"		"GE-Proton8-1"
					}
				}
			}
		}
	}
}
'''


class Fake:
    def __init__(self, body, headers=None):
        self.io, self.headers = io.BytesIO(body), headers or {}

    def read(self, n=-1):
        return self.io.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class ProtonTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='luna-proton-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / 'home'
        self.steam = self.home / '.local/share/Steam'
        (self.steam / 'config').mkdir(parents=True)
        (self.steam / 'config/config.vdf').write_text(CONFIG)
        patch = mock.patch.object(luna, 'home', lambda: self.home)
        patch.start()
        self.addCleanup(patch.stop)
        sleep = mock.patch.object(luna.time, 'sleep')
        sleep.start()
        self.addCleanup(sleep.stop)
        self.tools = self.steam / 'compatibilitytools.d'
        self.tools.mkdir()
        self.network = {}
        self.proc = Path(self.temp.name) / 'proc'
        self.proc.mkdir()
        env = mock.patch.dict(os.environ, {'LUNA_PROC_DIR': str(self.proc)})
        env.start()
        self.addCleanup(env.stop)

    def tool(self, name):
        d = self.tools / name
        d.mkdir()
        (d / 'version').write_text('1 %s\n' % name)
        (d / 'proton').write_text('x' * 10)

    def tarball(self, top=TAG, extra=None):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode='w:gz') as t:
            data = b'#!/bin/sh\n'
            info = tarfile.TarInfo('%s/proton' % top)
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))
            if extra:
                info = tarfile.TarInfo(extra)
                info.size = 1
                t.addfile(info, io.BytesIO(b'x'))
        return buf.getvalue()

    def serve(self, tar, sha=None, size=None):
        sha = sha or hashlib.sha512(tar).hexdigest()
        rel = {'tag_name': TAG, 'assets': [
            {'name': TAG + '.tar.gz', 'browser_download_url': 'https://x/t.tgz', 'size': size if size is not None else len(tar)},
            {'name': TAG + '.sha512sum', 'browser_download_url': 'https://x/t.sum', 'size': 1}]}
        calls = []

        def opener(request, timeout=60):
            url = request.full_url
            calls.append(url)
            if url.endswith('/latest') or '/tags/' in url:
                return Fake(json.dumps(rel).encode())
            if url.endswith('.sum'):
                return Fake(('%s  %s.tar.gz\n' % (sha, TAG)).encode())
            return Fake(tar, {'Content-Length': str(len(tar))})
        patch = mock.patch.object(luna, '_proton_open', opener)
        patch.start()
        self.addCleanup(patch.stop)
        return calls

    def test_status_lists_tools_and_missing_mapping(self):
        self.tool('GE-Proton9-1')
        self.serve(self.tarball())
        data = luna.proton_status_data()
        self.assertEqual([t['name'] for t in data['tools']], ['GE-Proton9-1'])
        self.assertEqual(data['latest'], TAG)
        self.assertTrue(data['updateAvailable'])
        self.assertEqual(data['missing'], {'GE-Proton8-1': ['300']})

    def test_status_offline_is_unknown_not_error(self):
        def boom(request, timeout=60):
            raise urllib.error.URLError('offline')
        with mock.patch.object(luna, '_proton_open', boom):
            self.assertEqual(luna.proton_status_data()['latest'], 'unknown')
            self.assertEqual(luna.proton_status(False), 0)

    def test_install_ok_and_refuses_second_time(self):
        self.serve(self.tarball())
        self.assertEqual(luna.proton_install('latest'), 0)
        self.assertTrue((self.tools / TAG / 'proton').is_file())
        self.assertEqual([p.name for p in self.tools.iterdir()], [TAG])  # no temp dir left
        calls = self.serve(self.tarball())
        self.assertEqual(luna.proton_install('latest'), 0)
        self.assertFalse([c for c in calls if c.endswith('.tgz')])  # already installed: no download

    def test_install_sha_mismatch_installs_nothing(self):
        self.serve(self.tarball(), sha='0' * 128)
        self.assertEqual(luna.proton_install('latest'), 1)
        self.assertEqual(list(self.tools.iterdir()), [])

    def test_install_rejects_path_traversal_and_bad_layout(self):
        self.serve(self.tarball(extra='../evil'))
        self.assertEqual(luna.proton_install('latest'), 1)
        self.assertFalse((self.tools / 'evil').exists())
        self.serve(self.tarball(top='other'))
        self.assertEqual(luna.proton_install('latest'), 1)
        self.assertEqual(list(self.tools.iterdir()), [])

    def test_install_dry_run_and_free_space(self):
        calls = self.serve(self.tarball())
        self.assertEqual(luna.proton_install('latest', dry_run=True), 0)
        self.assertFalse([c for c in calls if c.endswith('.tgz')])
        self.assertEqual(list(self.tools.iterdir()), [])
        self.serve(self.tarball(), size=10 ** 15)
        self.assertEqual(luna.proton_install('latest'), 1)

    def test_download_retries_on_429(self):
        tar = self.tarball()
        self.serve(tar)
        inner = luna._proton_open
        state = {'n': 0}

        def flaky(request, timeout=60):
            if request.full_url.endswith('.tgz') and state['n'] < 2:
                state['n'] += 1
                raise urllib.error.HTTPError(request.full_url, 429, 'slow', {}, None)
            return inner(request, timeout)
        with mock.patch.object(luna, '_proton_open', flaky):
            self.assertEqual(luna.proton_install('latest'), 0)
        self.assertEqual(state['n'], 2)

    def test_remove_refuses_mapped_unless_force(self):
        self.tool('GE-Proton9-1')
        self.assertEqual(luna.proton_remove('GE-Proton9-1'), luna.EXIT_REFUSED)
        self.assertTrue((self.tools / 'GE-Proton9-1').exists())
        self.assertEqual(luna.proton_remove('9-1', force=True), 0)
        self.assertFalse((self.tools / 'GE-Proton9-1').exists())
        self.assertEqual(luna.proton_remove('../x'), 1)

    def test_prune_keeps_newest_and_mapped(self):
        for n in ('GE-Proton9-1', 'GE-Proton9-2', 'GE-Proton9-10', 'GE-Proton9-11'):
            self.tool(n)
        self.assertEqual(luna.proton_prune(1, dry_run=True), 0)
        self.assertEqual(len(list(self.tools.iterdir())), 4)
        self.assertEqual(luna.proton_prune(1), 0)
        self.assertEqual(sorted(p.name for p in self.tools.iterdir()), ['GE-Proton9-1', 'GE-Proton9-11'])

    def test_arch_suffixed_release(self):
        base = TAG + '-x86_64'
        tar = self.tarball(top=base)
        sha = hashlib.sha512(tar).hexdigest()
        rel = {'tag_name': TAG, 'assets': [
            {'name': base + '.tar.gz', 'browser_download_url': 'https://x/t.tgz', 'size': len(tar)},
            {'name': base + '.sha512sum', 'browser_download_url': 'https://x/t.sum', 'size': 1}]}

        def opener(request, timeout=60):
            url = request.full_url
            if url.endswith('/latest'):
                return Fake(json.dumps(rel).encode())
            if url.endswith('.sum'):
                return Fake(('%s  %s.tar.gz\n' % (sha, base)).encode())
            return Fake(tar, {'Content-Length': str(len(tar))})
        with mock.patch.object(luna, '_proton_open', opener):
            self.assertEqual(luna.proton_install('latest'), 0)
            self.assertTrue((self.tools / base / 'proton').is_file())
            self.assertEqual(luna.proton_install('latest'), 0)
        self.assertLess(luna.proton_version_key('GE-Proton9-2'), luna.proton_version_key(base))
        self.assertEqual(luna.proton_remove(TAG), 0)

    def test_doctor_and_probe(self):
        self.tool('GE-Proton9-1')
        found = luna._doctor_proton()
        self.assertEqual([f['id'] for f in found], ['proton.no-manifest', 'proton.missing-tool'])
        self.assertEqual([f['severity'] for f in found], ['info', 'warn'])  # info never fails doctor
        self.assertIn('luna proton install GE-Proton8-1', found[1]['fix'])
        self.assertEqual(luna.proton_installed()[0]['version'], 'GE-Proton9-1')


class ProtonSafetyTest(ProtonTest):
    def full_tool(self, name, dlls=30, old=True):
        d = self.tools / name
        (d / 'files/lib/wine/x86_64-windows').mkdir(parents=True)
        (d / 'version').write_text('1 %s\n' % name)
        (d / 'proton').write_text('x')
        stamp = (d / 'version').stat().st_mtime - 600
        for i in range(dlls):
            f = d / 'files/lib/wine/x86_64-windows' / ('m%d.dll' % i)
            f.write_text('dll-%s-%d' % (name, i))
            if old:
                os.utime(str(f), (stamp, stamp))
        return d

    def prefix(self, links, owner=None):
        compat = self.steam / 'steamapps/compatdata/0'
        sysdir = compat / 'pfx/drive_c/windows/system32'
        sysdir.mkdir(parents=True)
        for name, target in links.items():
            os.symlink(str(target), str(sysdir / name))
        if owner:
            (compat / 'config_info').write_text('1\n%s/%s/files/share\n' % (self.tools, owner))
            (compat / 'version').write_text('1 %s\n' % owner)

    def ids(self):
        return [i['id'] for i in luna.proton_health()]

    def test_install_writes_manifest_and_verify(self):
        self.serve(self.tarball())
        self.assertEqual(luna.proton_install('latest'), 0)
        manifest = luna.proton_manifest_load(TAG)
        self.assertEqual(list(manifest['files']), ['proton'])
        self.assertEqual(manifest['files']['proton'][0], 10)
        self.assertFalse((self.tools / TAG / 'luna-manifest.json').exists())  # outside the tool dir
        self.assertEqual(luna.proton_verify(TAG), 0)
        self.assertTrue(luna.proton_status_data()['tools'][0]['manifest'])

    def test_verify_changed_missing_extra(self):
        self.serve(self.tarball())
        luna.proton_install('latest')
        tool = self.tools / TAG
        (tool / 'proton').write_text('#!/bin/sh\n!')            # same size would be 10; change content
        (tool / 'proton').write_text('#!/bin/sh\nX')
        (tool / 'new.txt').write_text('x')
        (tool / '__pycache__').mkdir()
        (tool / '__pycache__/a.pyc').write_text('x')               # ignored
        out = io.StringIO()
        with mock.patch('sys.stdout', out):
            self.assertEqual(luna.proton_verify(TAG), 1)
        self.assertIn('1 changed, 0 missing, 1 extra', out.getvalue())
        (tool / 'proton').unlink()
        (tool / 'new.txt').unlink()
        out = io.StringIO()
        with mock.patch('sys.stdout', out):
            self.assertEqual(luna.proton_verify(TAG), 1)
        self.assertIn('0 changed, 1 missing, 0 extra', out.getvalue())

    def test_verify_without_manifest_never_blesses_disk_then_rebuild(self):
        self.tool(TAG)
        out = io.StringIO()
        with mock.patch('sys.stdout', out):
            self.assertEqual(luna.proton_verify(TAG), 1)
        self.assertIn('--rebuild', out.getvalue())
        self.assertIsNone(luna.proton_manifest_load(TAG))
        self.serve(self.tarball())                                  # the real release: proton is '#!/bin/sh\n'
        with mock.patch('sys.stdout', io.StringIO()) as out2:
            self.assertEqual(luna.proton_verify(TAG, rebuild=True), 1)  # disk differs from the verified source
        self.assertIn('1 changed', out2.getvalue())
        self.assertEqual(luna.proton_manifest_load(TAG)['source'], 'rebuild')
        (self.tools / TAG / 'proton').write_bytes(b'#!/bin/sh\n')
        (self.tools / TAG / 'version').unlink()
        self.assertEqual(luna.proton_verify(TAG), 0)

    def test_verify_against_reference_file(self):
        self.serve(self.tarball())
        luna.proton_install('latest')
        ref = Path(self.temp.name) / 'ref.sha256'
        ref.write_text('%s  %s/proton\n' % (hashlib.sha256(b'#!/bin/sh\n').hexdigest(), TAG))
        self.assertEqual(luna.proton_verify(TAG, against=str(ref)), 0)
        ref.write_text('%s  %s/proton\n' % ('0' * 64, TAG))
        self.assertEqual(luna.proton_verify(TAG, against=str(ref)), 1)

    def test_status_flags_bulk_newer_dlls_only(self):
        self.full_tool('GE-Proton9-1')
        self.assertEqual(luna.proton_health(), [])
        for f in list((self.tools / 'GE-Proton9-1/files/lib/wine/x86_64-windows').iterdir())[:25]:
            future = f.stat().st_mtime + 86400
            os.utime(str(f), (future, future))
        found = luna.proton_health()
        self.assertEqual([i['id'] for i in found], ['proton.dll-mtimes'])
        self.assertIn('25 of 30', found[0]['message'])
        self.assertIn('luna proton verify', found[0]['fix'])
        out = io.StringIO()
        with mock.patch('sys.stdout', out):
            luna.proton_status(False)
        self.assertIn('warning   : GE-Proton9-1: 25 of 30', out.getvalue())

    def test_few_newer_dlls_are_not_flagged(self):
        self.full_tool('GE-Proton9-1', dlls=400)
        f = self.tools / 'GE-Proton9-1/files/lib/wine/x86_64-windows/m1.dll'
        os.utime(str(f), (f.stat().st_mtime + 86400,) * 2)
        self.assertEqual(luna.proton_health(), [])

    def test_prefix_foreign_and_dangling_links(self):
        self.full_tool('GE-Proton9-1')
        other = self.tools / 'GE-Proton9-2'
        other.mkdir()
        (other / 'a.dll').write_text('x')
        os.symlink(str(self.tools / 'GE-Proton9-1/files/lib/wine/x86_64-windows/m1.dll'), str(self.steam / 'ok-link'))
        self.prefix({'good.dll': self.tools / 'GE-Proton9-1/files/lib/wine/x86_64-windows/m1.dll',
                     'foreign.dll': other / 'a.dll',
                     'gone.dll': self.tools / 'GE-Proton8-9/files/x.dll',
                     'plain.dll': '/usr/lib/x.dll'}, owner='GE-Proton9-1')
        found = {i['id']: i for i in luna.proton_prefix_findings()}
        self.assertEqual(sorted(found), ['proton.prefix-dangling', 'proton.prefix-foreign'])
        self.assertIn('GE-Proton8-9', found['proton.prefix-dangling']['message'])
        self.assertIn('GE-Proton9-2', found['proton.prefix-foreign']['message'])

    def test_clean_prefix_has_no_findings(self):
        self.full_tool('GE-Proton9-1')
        self.prefix({'good.dll': self.tools / 'GE-Proton9-1/files/lib/wine/x86_64-windows/m1.dll'},
                    owner='GE-Proton9-1')
        self.assertEqual(luna.proton_health(), [])

    def test_multi_version_warning_in_status_install_doctor(self):
        self.tool('GE-Proton9-1')
        self.tool('GE-Proton9-2')
        out = io.StringIO()
        with mock.patch('sys.stdout', out):
            luna.proton_status(False)
        self.assertIn('2 GE-Proton versions are installed', out.getvalue())
        self.assertIn('luna proton prune', out.getvalue())
        self.assertIn('proton.multi-version', [f['id'] for f in luna._doctor_proton()])
        self.serve(self.tarball())
        out = io.StringIO()
        with mock.patch('sys.stdout', out):
            self.assertEqual(luna.proton_install('latest'), 0)
        self.assertIn('WARNING', out.getvalue())
        self.assertIn('was NOT removed', out.getvalue())
        self.assertTrue((self.tools / 'GE-Proton9-1').exists())

    def test_single_version_no_warning(self):
        self.tool('GE-Proton9-2')
        self.assertNotIn('proton.multi-version', self.ids())

    def test_prune_default_is_one_and_pinned_survives(self):
        parser = luna.build_parser()
        self.assertEqual(parser.parse_args(['proton', 'prune']).keep, 1)
        for n in ('GE-Proton9-1', 'GE-Proton9-2', 'GE-Proton9-3'):   # 9-1 is mapped by appid 100
            self.tool(n)
        self.assertEqual(luna.proton_prune(1), 0)
        self.assertEqual(sorted(p.name for p in self.tools.iterdir()), ['GE-Proton9-1', 'GE-Proton9-3'])
        self.assertEqual(luna.proton_prune(0), 0)                    # even keep 0 spares the pinned one
        self.assertEqual([p.name for p in self.tools.iterdir()], ['GE-Proton9-1'])

    def test_prune_refuses_when_config_unreadable(self):
        self.tool('GE-Proton9-2')
        self.tool('GE-Proton9-3')
        with mock.patch.object(luna.os, 'access', return_value=False):
            self.assertEqual(luna.proton_prune(1), luna.EXIT_REFUSED)
        self.assertEqual(len(list(self.tools.iterdir())), 2)

    def fake_proc(self, pid, comm, cmdline=''):
        d = self.proc / str(pid)
        d.mkdir()
        (d / 'comm').write_text(comm + '\n')
        (d / 'cmdline').write_bytes(cmdline.replace(' ', '\0').encode())

    def test_refuse_while_steam_runs(self):
        self.tool('GE-Proton9-2')
        self.tool('GE-Proton9-3')
        self.fake_proc(4242, 'steam')
        self.assertEqual(luna.proton_remove('9-2'), luna.EXIT_REFUSED)
        self.assertEqual(luna.proton_remove('9-2', force=True), luna.EXIT_REFUSED)
        self.assertEqual(luna.proton_prune(1), luna.EXIT_REFUSED)
        self.serve(self.tarball())
        self.assertEqual(luna.proton_install('latest'), luna.EXIT_REFUSED)
        self.assertEqual(len(list(self.tools.iterdir())), 2)
        self.assertEqual(luna.proton_prune(1, dry_run=True), 0)       # dry run still allowed

    def test_refuse_while_wine_runs_from_tool(self):
        self.tool('GE-Proton9-2')
        self.tool('GE-Proton9-3')
        self.fake_proc(77, 'wineserver', '%s/GE-Proton9-2/files/bin/wineserver -f' % self.tools)
        self.assertEqual(luna.proton_remove('9-2'), luna.EXIT_REFUSED)
        self.assertEqual(luna.proton_prune(1), luna.EXIT_REFUSED)
        self.assertTrue((self.tools / 'GE-Proton9-2').exists())
        self.assertEqual(luna.proton_remove('9-3'), 0)

    # ---- review findings 5-9, 11 -------------------------------------------------------

    def run_quiet(self, func, *args, **kwargs):
        with mock.patch('sys.stdout', new=io.StringIO()) as out:
            code = func(*args, **kwargs)
        return code, out.getvalue()

    def test_symlinked_tool_dir_is_never_followed_or_deleted(self):
        outside = Path(self.temp.name) / 'outside'
        outside.mkdir()
        (outside / 'precious').write_text('keep')
        (outside / 'version').write_text('1 GE-Proton9-2\n')
        os.symlink(str(outside), str(self.tools / 'GE-Proton9-2'))
        self.tool('GE-Proton9-3')
        code, _ = self.run_quiet(luna.proton_remove, '9-2', force=True)
        self.assertEqual(code, luna.EXIT_REFUSED)
        code, out = self.run_quiet(luna.proton_prune, 1)
        self.assertEqual(code, luna.EXIT_REFUSED)
        self.assertIn('symlink', out)
        self.assertTrue((self.tools / 'GE-Proton9-2').is_symlink())
        self.assertEqual((outside / 'precious').read_text(), 'keep')

    def test_rmtree_failure_is_reported_not_a_traceback(self):
        self.tool('GE-Proton9-2')
        with mock.patch.object(luna.shutil, 'rmtree', side_effect=OSError('read-only')):
            code, out = self.run_quiet(luna.proton_remove, '9-2')
        self.assertEqual(code, luna.EXIT_REFUSED)
        self.assertIn('read-only', out)

    @unittest.skipIf(hasattr(os, 'geteuid') and os.geteuid() == 0, 'root ignores file modes')
    def test_remove_refuses_when_config_is_unreadable_unless_forced(self):
        self.tool('GE-Proton9-2')
        self.tool('GE-Proton9-3')
        config = self.steam / 'config/config.vdf'
        os.chmod(str(config), 0)
        self.addCleanup(os.chmod, str(config), 0o644)
        code, out = self.run_quiet(luna.proton_remove, '9-2')
        self.assertEqual(code, luna.EXIT_REFUSED)
        self.assertIn('cannot read', out)
        self.assertTrue((self.tools / 'GE-Proton9-2').exists())
        self.assertEqual(self.run_quiet(luna.proton_remove, '9-2', force=True)[0], 0)

    def test_install_rechecks_steam_right_before_the_rename(self):
        self.serve(self.tarball())
        with mock.patch.object(luna, 'proton_busy_reason', side_effect=['', 'Steam is running']):
            code, _ = self.run_quiet(luna.proton_install, 'latest')
        self.assertEqual(code, luna.EXIT_REFUSED)
        self.assertEqual([p.name for p in self.tools.iterdir() if not p.name.startswith('.')], [])

    def test_remove_rechecks_steam_right_before_the_delete(self):
        self.tool('GE-Proton9-2')
        with mock.patch.object(luna, 'proton_busy_reason', return_value='Steam is running'):
            self.assertEqual(self.run_quiet(luna.proton_remove, '9-2')[0], luna.EXIT_REFUSED)
        self.assertTrue((self.tools / 'GE-Proton9-2').exists())

    def test_unlistable_process_table_fails_closed(self):
        self.tool('GE-Proton9-2')
        self.proc.rmdir()                                  # LUNA_PROC_DIR points at nothing
        code, out = self.run_quiet(luna.proton_remove, '9-2', force=True)
        self.assertEqual(code, luna.EXIT_REFUSED)
        self.assertIn('cannot tell whether Steam is running', out)
        self.assertTrue((self.tools / 'GE-Proton9-2').exists())

    @unittest.skipIf(hasattr(os, 'geteuid') and os.geteuid() == 0, 'root ignores file modes')
    def test_unreadable_process_entry_fails_closed(self):
        self.tool('GE-Proton9-2')
        self.fake_proc(55, 'bash', 'bash')
        os.chmod(str(self.proc / '55/cmdline'), 0)
        code, _ = self.run_quiet(luna.proton_remove, '9-2', force=True)
        self.assertEqual(code, luna.EXIT_REFUSED)
        self.assertTrue((self.tools / 'GE-Proton9-2').exists())

    def test_steam_found_by_argv0_when_comm_differs(self):
        self.fake_proc(61, 'SteamRuntime', '/home/u/.steam/steam/ubuntu12_32/steam -foo')
        self.assertIn('Steam is running', luna.proton_busy_reason())

    def test_proc_override_is_ignored_for_a_real_home_steam(self):
        import pwd
        real = Path(pwd.getpwuid(os.getuid()).pw_dir)
        with mock.patch.object(luna, 'home', lambda: real):
            self.assertEqual(luna.proton_proc_dir(), '/proc')
        self.assertEqual(luna.proton_proc_dir(), str(self.proc))

    def tar_with(self, members):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode='w:gz') as archive:
            for name, kind, link in members:
                info = tarfile.TarInfo(name)
                if kind == 'sym':
                    info.type, info.linkname = tarfile.SYMTYPE, link
                    archive.addfile(info)
                elif kind == 'dir':
                    info.type = tarfile.DIRTYPE
                    archive.addfile(info)
                else:
                    info.size = 1
                    archive.addfile(info, io.BytesIO(b'x'))
        buf.seek(0)
        return tarfile.open(fileobj=buf, mode='r:gz')

    def test_extract_rejects_a_symlink_chain_that_escapes(self):
        dest = Path(self.temp.name) / 'ex'
        dest.mkdir()
        archive = self.tar_with([('GE', 'dir', None), ('GE/a', 'sym', '../..'), ('GE/a/x', 'file', None)])
        with self.assertRaises(ValueError):
            luna._proton_safe_extract(archive, str(dest))
        self.assertFalse((Path(self.temp.name) / 'x').exists())
        self.assertEqual(list(dest.iterdir()), [])

    def test_extract_rejects_escape_through_a_link_to_a_link(self):
        dest = Path(self.temp.name) / 'ex2'
        dest.mkdir()
        archive = self.tar_with([('GE', 'dir', None), ('GE/up', 'sym', '..'), ('GE/up2', 'sym', 'up/..'),
                                 ('GE/up2/y', 'file', None)])
        with self.assertRaises(ValueError):
            luna._proton_safe_extract(archive, str(dest))

    def test_extract_keeps_legitimate_internal_links(self):
        dest = Path(self.temp.name) / 'ex3'
        dest.mkdir()
        archive = self.tar_with([('GE', 'dir', None), ('GE/lib', 'dir', None), ('GE/lib/f', 'file', None),
                                 ('GE/lib64', 'sym', 'lib'), ('GE/files', 'dir', None),
                                 ('GE/files/l', 'sym', '../lib/f')])
        luna._proton_safe_extract(archive, str(dest))
        self.assertTrue((dest / 'GE/lib64').is_symlink())
        self.assertEqual((dest / 'GE/files/l').read_text(), 'x')

    def test_malformed_manifest_is_reported_not_a_traceback(self):
        self.tool('GE-Proton9-2')
        path = luna.proton_manifest_path('GE-Proton9-2')
        for bad in ({'version': 1, 'files': {'proton': 'oops'}}, {'version': 1, 'files': {'proton': [1]}},
                    {'version': 1, 'files': {}, 'links': ['x']}, {'version': 1, 'files': {'p': ['3', 'h']}}):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(bad))
            self.assertEqual(luna.proton_manifest_state('GE-Proton9-2'), 'unreadable')
            code, out = self.run_quiet(luna.proton_verify, '9-2')
            self.assertEqual(code, 1)
            self.assertIn('manifest unreadable', out)
            self.assertEqual(luna.proton_status_data()['tools'][0]['manifest'], False)
            self.assertIn('proton.manifest-unreadable', [f['id'] for f in luna._doctor_proton()])
        with self.assertRaises(ValueError):
            luna.proton_compare(str(self.tools / 'GE-Proton9-2'), {'proton': 'oops'}, {})
        self.assertEqual(luna.proton_manifest_state('GE-Proton9-3'), 'missing')

    def test_non_utf8_version_file_does_not_crash_listing(self):
        self.tool('GE-Proton9-2')
        (self.tools / 'GE-Proton9-2/version').write_bytes(b'1 GE-\xff\xfe\n')
        self.assertEqual([t['name'] for t in luna.proton_installed()], ['GE-Proton9-2'])


if __name__ == '__main__':
    unittest.main()
