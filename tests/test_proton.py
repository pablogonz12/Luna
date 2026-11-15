"""luna proton with a temp HOME and a mocked network; never touches the real Steam."""
import hashlib
import importlib.util
import io
import json
import os
os.environ.setdefault('LUNA_GUI_CHECKS', '0')  # hermetic: tests never probe the real desktop (test_gui_check manages it itself)
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
        self.assertEqual([f['severity'] for f in found], ['warn'])
        self.assertIn('luna proton install GE-Proton8-1', found[0]['fix'])
        self.assertEqual(luna.proton_installed()[0]['version'], 'GE-Proton9-1')


if __name__ == '__main__':
    unittest.main()
