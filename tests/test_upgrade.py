#!/usr/bin/env python3
"""Upgrade tests: install an old release, then the current tree over it.

Each cell unpacks a real tagged release with `git archive` (never a checkout in
the working tree), installs it the way that release documented it into a
throwaway HOME with fake system tools first on PATH, and then runs the current
`bootstrap --force` over it. Nothing here touches the real HOME, Steam or
systemd, and no network is used.

A shallow CI clone has no history to unpack; the class skips with a reason and
says what CI needs (fetch-depth: 0).
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / 'fixtures' / 'upgrade'

# Tests drive real install paths; never pop a desktop notification.
os.environ.setdefault('LUNA_NO_NOTIFY', '1')

EXIT_REFUSED = 2


def git(*args):
    return subprocess.run(['git', '-C', str(HERE)] + list(args), capture_output=True, text=True)


def discover_refs():
    """The three newest tags plus the 'Luna drives Lumen updates' commit.

    Returns (refs, why_not). refs is empty when the history is not there.
    """
    if not (HERE / '.git').exists() and git('rev-parse', '--git-dir').returncode != 0:
        return [], 'no git history in %s' % HERE
    tags = git('tag', '--sort=-creatordate')
    if tags.returncode != 0:
        return [], 'git tags are unavailable here (shallow clone?): %s' % tags.stderr.strip()
    shallow = git('rev-parse', '--is-shallow-repository').stdout.strip() == 'true'
    refs = []
    for tag in [line for line in tags.stdout.split() if line][:3]:
        if git('cat-file', '-e', tag + ':install.sh').returncode == 0:
            refs.append(tag)
    # The history was rewritten once; look the commit up by message, not by hash.
    for ref in ('3c5d767',):
        if git('cat-file', '-e', ref + '^{commit}').returncode == 0:
            refs.append(ref)
            break
    else:
        found = git('log', '--all', '--format=%h', '-F', '--grep=Luna drives Lumen updates')
        commit = next((line for line in found.stdout.split() if line), '')
        if commit and git('cat-file', '-e', commit + '^{commit}').returncode == 0:
            refs.append(commit)
    if not refs:
        reason = 'no old release could be unpacked (history rewritten?)'
        if shallow:
            reason += '; CI needs actions/checkout with fetch-depth: 0'
        return [], reason
    return refs, ''


REFS, REFS_SKIP = discover_refs()


def sha256(data):
    return hashlib.sha256(data).hexdigest()


class UpgradeTestCase(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls):
        if not REFS:
            raise unittest.SkipTest(REFS_SKIP or 'old releases are unavailable')
        cls.temp = Path(tempfile.mkdtemp(prefix='luna-upgrade-'))
        cls.trees = {}
        cls.old_code = {}
        for ref in REFS:
            dest = cls.temp / 'trees' / ref
            dest.mkdir(parents=True)
            archive = subprocess.run(['git', '-C', str(HERE), 'archive', '--format=tar', ref],
                                     capture_output=True)
            if archive.returncode != 0:
                raise unittest.SkipTest('git archive %s failed (shallow clone? CI needs '
                                        'fetch-depth: 0): %s' % (ref, archive.stderr.decode('utf-8', 'replace')))
            subprocess.run(['tar', '-x', '-f', '-', '-C', str(dest)],
                           input=archive.stdout, check=True)
            cls.trees[ref] = dest
            cls.old_code[ref] = (dest / 'scripts' / 'luna.py').read_text(errors='replace')
        # Freeze the current tree once: the upgrade must be judged against one
        # consistent set of files, even while another change edits the checkout.
        cls.current = cls.temp / 'current'
        shutil.copytree(HERE, cls.current, ignore=shutil.ignore_patterns(
            '.git', 'tests', 'dist', 'docs', '__pycache__'))
        cls.mismatched = cls.payload_mismatches(cls.current)

    @classmethod
    def tearDownClass(cls):
        temp = getattr(cls, 'temp', None)
        if temp:
            shutil.rmtree(temp, ignore_errors=True)

    @staticmethod
    def payload_mismatches(root):
        """Sources in `root` that do not match its vendor/MANIFEST.json.

        A checkout caught mid-edit legitimately fails `verify` (the installed
        bytes differ from the pinned payload). CI runs on a clean tree, where
        verify must pass outright; here only exactly those paths are tolerated.
        """
        manifest = json.loads((root / 'vendor/MANIFEST.json').read_text())
        bad = set()
        for section in ('addons', 'shared'):
            for name, entry in (manifest.get(section) or {}).items():
                for dest, meta in (entry.get('files') or {}).items():
                    rel = meta.get('source_path') or ''
                    source = (root / 'addons' / name / rel) if section == 'addons' else (root / rel)
                    if not rel or not source.is_file() or sha256(source.read_bytes()) != meta.get('sha256'):
                        bad.add('%s/%s' % (name, dest))
        return bad

    # -- fixtures --------------------------------------------------------
    def new_cell(self, ref, host):
        """A throwaway HOME shaped like an old install of `ref` on `host`."""
        root = Path(tempfile.mkdtemp(dir=self.temp))
        home = root / 'home'
        share = home / '.local/share'
        (share / 'Luna').mkdir(parents=True)
        # Both hosts carry a Lumen tree: every old release requires it, and a
        # Luna-host machine reached host.json=luna through `luna host cutover`.
        lumen = share / 'Lumen'
        (lumen / 'lua').mkdir(parents=True)
        (lumen / 'luatools/backend').mkdir(parents=True)
        shutil.copy2(FIXTURES / 'upstream/boot.lua', lumen / 'lua/boot.lua')
        shutil.copy2(FIXTURES / 'upstream/about.lua', lumen / 'lua/about.lua')
        (lumen / 'luatools/backend/main.lua').write_text('-- present\n')
        wrapper = share / 'SLSsteam/path'
        wrapper.mkdir(parents=True)
        (wrapper / 'steam').write_text('#!/bin/sh\nexit 0\n')
        (wrapper / 'steam').chmod(0o755)
        (share / 'Luna/host.json').write_text(json.dumps({'active': host}) + '\n')
        # User data the upgrade must never touch.
        data = share / 'slssteam-integrations'
        (data / 'playtime').mkdir(parents=True)
        (data / 'playtime/76561198000000000.json').write_text(
            '{"version": 2, "games": {"10": {"minutes": 5}}}\n')
        (data / 'presence.json').write_text('{"version": 1, "owned": {"10": true}}\n')
        # A launcher the owner hand-edited after Luna wrote it (record differs).
        launcher = share / 'applications' / 'steamos-2560x1440-1.desktop'
        launcher.parent.mkdir(parents=True)
        launcher.write_text('[Desktop Entry]\nName=Mine\nX-Luna-SteamOS=true\n')
        tools = share / 'Lumen/sls-luna' if host == 'lumen' else share / 'Luna/state'
        (share / 'Lumen/sls-luna').mkdir(parents=True, exist_ok=True)
        tools.mkdir(parents=True, exist_ok=True)
        record = {'files': {str(launcher): {'sha256': sha256(b'luna wrote this once')}},
                  'profiles': {}}
        for place in {share / 'Lumen/sls-luna', share / 'Luna/state'}:
            place.mkdir(parents=True, exist_ok=True)
            (place / 'steam-machine-shortcuts.json').write_text(json.dumps(record))
        return root, home, launcher

    def environment(self, root, home):
        fakes = root / 'bin'
        shutil.copytree(FIXTURES / 'bin', fakes)
        host_src = root / 'host-src'
        (host_src / 'bin').mkdir(parents=True)
        (host_src / 'bin/luna-host').write_text('#!/bin/sh\nexit 0\n')
        (host_src / 'bin/luna-host').chmod(0o755)
        (host_src / 'lua/luna').mkdir(parents=True)
        (host_src / 'lua/luna/boot.lua').write_text('-- luna-host boot\n')
        keep = {key: value for key, value in os.environ.items()
                if not key.startswith('LUNA_')
                and key not in ('LUMEN_DIR', 'LUMEN_LUA_DIR', 'SLS_COMPRESSOR_BIN')}
        keep.update(
            HOME=str(home),
            PATH=str(fakes) + os.pathsep + os.environ.get('PATH', ''),
            LUNA_HOME=str(home / '.local/share/Luna'),
            LUNA_HOST_SRC=str(host_src),
            LUNA_NO_NOTIFY='1',
            LUNA_NO_INHIBIT='1',
            LUNA_TEST_INSTALLER=str(FIXTURES / 'fake-installer.sh'),
            XDG_DATA_HOME=str(home / '.local/share'),
            XDG_RUNTIME_DIR=str(home / 'run'),
        )
        return keep

    # -- snapshots and assertions ----------------------------------------
    def snapshot(self, root):
        """Every file under root, normalised so a repeated install is a no-op.

        `luna-manifest.json` records `installedAt` (a timestamp) on every write;
        everything else must be byte-identical across a second run.
        """
        out = {}
        for path in sorted(root.rglob('*')):
            if not path.is_file():
                continue
            rel = str(path.relative_to(root))
            if rel.endswith('.log'):
                continue
            data = path.read_bytes()
            if rel.endswith('luna-manifest.json'):
                try:
                    doc = json.loads(data)
                    doc.pop('installedAt', None)
                    data = json.dumps(doc, sort_keys=True).encode()
                except ValueError:
                    pass
            out[rel] = sha256(data)
        return out

    def run_luna(self, env, *args, script=None):
        command = [sys.executable, str(script or self.current / 'scripts/luna.py')] + list(args)
        return subprocess.run(command, capture_output=True, text=True, env=env, timeout=300)

    def assert_same_snapshot(self, before, after, message):
        added = sorted(set(after) - set(before))
        removed = sorted(set(before) - set(after))
        changed = sorted(rel for rel in set(before) & set(after) if before[rel] != after[rel])
        self.assertEqual((added, removed, changed), ([], [], []),
                         '%s (added=%s removed=%s changed=%s)'
                         % (message, added[:8], removed[:8], changed[:8]))

    def old_install(self, ref, env):
        tree = self.trees[ref]
        args = ['bash', str(tree / 'install.sh'), '--force', '--keep-steam-client']
        if 'def cmd_bootstrap' in self.old_code[ref]:
            args.append('--no-system-packages')
        return subprocess.run(args, capture_output=True, text=True, env=env, timeout=600)

    def tools_dir(self, home, host):
        share = home / '.local/share'
        return share / 'Luna/state' if host == 'luna' else share / 'Lumen/sls-luna'

    def assert_installed_copy(self, home, host, env):
        tools = self.tools_dir(home, host)
        installed = tools / 'luna.py'
        current = self.current / 'scripts/luna.py'
        self.assertTrue(installed.is_file(), 'installed copy missing: %s' % installed)
        self.assertEqual(installed.read_bytes(), current.read_bytes(),
                         'the installed CLI is not the current scripts/luna.py')
        manifest = json.loads((tools / 'runtime' / 'RUNTIME.json').read_text())
        self.assertEqual(manifest.get('lunaSha256'), sha256(installed.read_bytes()),
                         'RUNTIME.json does not belong to the installed luna.py')
        for rel, digest in sorted((manifest.get('files') or {}).items()):
            path = tools / 'runtime' / rel
            self.assertTrue(path.is_file(), 'runtime file missing: %s' % rel)
            self.assertEqual(sha256(path.read_bytes()), digest, 'runtime file differs: %s' % rel)
        # The installed copy must work on its own, without the checkout.
        for command in ('status', 'verify'):
            result = self.run_luna(env, command, script=installed)
            if result.returncode != 0 and command == 'verify' and self.mismatched:
                problems = [line.strip() for line in result.stdout.splitlines()
                            if line.strip().startswith('- ')]
                self.assertTrue(problems, 'installed `luna verify` failed:\n%s'
                                % (result.stdout + result.stderr))
                for line in problems:
                    match = re.match(r'^- payload (\S+) does not match the manifest$', line)
                    self.assertTrue(match and match.group(1) in self.mismatched,
                                    'installed `luna verify` failed for a real reason:\n%s'
                                    % (result.stdout + result.stderr))
                continue
            self.assertEqual(result.returncode, 0,
                             'installed `luna %s` failed:\n%s' % (command, result.stdout + result.stderr))
        launcher = home / '.local/bin/luna'
        self.assertTrue(launcher.is_file(), 'the luna launcher was not installed')
        result = subprocess.run(['sh', str(launcher), 'status'],
                                capture_output=True, text=True, env=env, timeout=300)
        self.assertEqual(result.returncode, 0,
                         'the installed launcher cannot run:\n%s' % (result.stdout + result.stderr))

    def check_cell(self, ref, host):
        root, home, launcher = self.new_cell(ref, host)
        env = self.environment(root, home)
        data_dir = home / '.local/share/slssteam-integrations'
        data_before = {str(p.relative_to(data_dir)): p.read_bytes()
                       for p in sorted(data_dir.rglob('*')) if p.is_file()}
        launcher_before = launcher.read_bytes()
        share = home / '.local/share'

        old = self.old_install(ref, env)
        self.assertEqual(old.returncode, 0,
                         'old %s install failed on the %s host:\n%s' % (ref, host, old.stdout + old.stderr))
        # The fixture reproduces the friend's 3-day-old install: a tooling copy
        # with no runtime tree beside it (the "vendor/MANIFEST.json is missing"
        # dead end) for releases older than the runtime tree.
        old_copy = share / 'Lumen/sls-luna/luna.py'
        if 'def deploy_runtime' not in self.old_code[ref]:
            self.assertTrue(old_copy.is_file(), 'old release did not leave an installed copy')
            self.assertFalse((share / 'Lumen/sls-luna/runtime').exists(),
                             'fixture is not the runtime-less old install')
        if host == 'luna':
            self.assertEqual(json.loads((share / 'Luna/host.json').read_text())['active'], 'luna')
        lumen_before = {str(p.relative_to(share / 'Lumen')): sha256(p.read_bytes())
                        for p in (share / 'Lumen').rglob('*') if p.is_file()}

        result = self.run_luna(env, 'bootstrap', '--force', '--keep-steam-client',
                               '--no-system-packages')
        self.assertEqual(result.returncode, 0, 'bootstrap failed over %s (%s):\n%s'
                         % (ref, host, result.stdout + result.stderr))
        self.assertEqual(json.loads((share / 'Luna/host.json').read_text())['active'], host,
                         'the upgrade changed the active host')
        self.assert_installed_copy(home, host, env)
        if host == 'luna':
            lumen_after = {str(p.relative_to(share / 'Lumen')): sha256(p.read_bytes())
                           for p in (share / 'Lumen').rglob('*') if p.is_file()}
            self.assertLessEqual(set(lumen_after), set(lumen_before),
                                 'the upgrade created Lumen files on a Luna-host machine: %s'
                                 % sorted(set(lumen_after) - set(lumen_before)))
            changed = sorted(rel for rel, digest in lumen_before.items()
                             if lumen_after.get(rel) != digest)
            self.assertEqual(changed, [],
                             'the upgrade modified Lumen files on a Luna-host machine')
        data_after = {str(p.relative_to(data_dir)): p.read_bytes()
                      for p in sorted(data_dir.rglob('*')) if p.is_file()}
        self.assertEqual(data_after, data_before, 'the upgrade changed the user data')
        self.assertEqual(launcher.read_bytes(), launcher_before,
                         'the upgrade overwrote a hand-edited SteamOS launcher')

        first = self.snapshot(home)
        again = self.run_luna(env, 'bootstrap', '--force', '--keep-steam-client',
                              '--no-system-packages')
        self.assertEqual(again.returncode, 0, 'the second bootstrap failed:\n%s'
                         % (again.stdout + again.stderr))
        self.assert_same_snapshot(first, self.snapshot(home),
                                  'running the upgrade twice was not a no-op')

        plain = self.run_luna(env, 'install', '--force', '--keep-steam-client')
        self.assertEqual(plain.returncode, 0, 'a plain install --force failed over %s (%s):\n%s'
                         % (ref, host, plain.stdout + plain.stderr))
        self.assertIn('Nothing to do', plain.stdout,
                      'a plain install after the upgrade still had work to do')
        self.assert_same_snapshot(first, self.snapshot(home),
                                  'a plain install changed the already-upgraded tree')
        shutil.rmtree(root, ignore_errors=True)

    # -- tests -----------------------------------------------------------
    def test_upgrade_matrix(self):
        for ref in REFS:
            hosts = ('lumen', 'luna') if 'host.json' in self.old_code[ref] else ('lumen',)
            for host in hosts:
                with self.subTest(release=ref, host=host):
                    self.check_cell(ref, host)

    def test_refuses_while_steam_runs(self):
        ref = REFS[0]
        root, home, _launcher = self.new_cell(ref, 'lumen')
        env = self.environment(root, home)
        old = self.old_install(ref, env)
        self.assertEqual(old.returncode, 0, old.stdout + old.stderr)
        before = self.snapshot(home)
        running = dict(env, LUNA_FAKE_RUNNING='steam')
        result = self.run_luna(running, 'bootstrap', '--force', '--keep-steam-client',
                               '--no-system-packages')
        self.assertEqual(result.returncode, EXIT_REFUSED,
                         'bootstrap did not refuse with Steam running:\n%s'
                         % (result.stdout + result.stderr))
        self.assertIn('Steam is running', result.stdout + result.stderr)
        self.assert_same_snapshot(before, self.snapshot(home),
                                  'a refused upgrade wrote something anyway')
        shutil.rmtree(root, ignore_errors=True)


if __name__ == '__main__':
    unittest.main()
