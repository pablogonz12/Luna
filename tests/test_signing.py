#!/usr/bin/env python3
"""Release signing: `luna release` signs, `luna update` verifies against a local trust file.

Hermetic: a disposable HOME, throwaway ed25519 keys generated inside the temp dir, no
network (--url uses file://). Nothing reads ~/.ssh or the real ~/.config/luna.
"""
import hashlib
import io
import os
os.environ.setdefault('LUNA_GUI_CHECKS', '0')  # hermetic: tests never probe the real desktop (test_gui_check manages it itself)
os.environ.setdefault('LUNA_LIVE_CHECKS', '0')  # hermetic: tests never contact a real Steam (test_live_probe uses a fake endpoint)
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

from test_luna import HERE, LUNA, UPSTREAM, stub_bin

KEYGEN = shutil.which('ssh-keygen')


@unittest.skipUnless(KEYGEN, 'ssh-keygen is not installed')
class SigningTestCase(unittest.TestCase):
    base_dir = None
    base_bundle = None

    @classmethod
    def setUpClass(cls):
        cls.base_dir = Path(tempfile.mkdtemp(prefix='luna-sign-base-'))
        env = dict(os.environ, HOME=str(cls.base_dir), XDG_CONFIG_HOME=str(cls.base_dir / 'cfg'),
                   LUNA_SIGNING_DIR=str(cls.base_dir / 'nokey'))
        out = cls.base_dir / 'dist'
        done = subprocess.run([sys.executable, str(LUNA), 'release', '--out', str(out)],
                              capture_output=True, text=True, env=env)
        assert done.returncode == 0, done.stdout + done.stderr
        cls.base_bundle = next(out.glob('luna-*.tar.gz'))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.base_dir, ignore_errors=True)

    def setUp(self):
        self.temp = Path(tempfile.mkdtemp(prefix='luna-sign-test-'))
        self.addCleanup(shutil.rmtree, self.temp, ignore_errors=True)
        self.home = self.temp / 'home'
        lumen = self.home / '.local/share/Lumen'
        (lumen / 'lua').mkdir(parents=True)
        (lumen / 'luatools/backend').mkdir(parents=True)
        (lumen / 'luatools/backend/main.lua').write_text('-- present')
        (lumen / 'lua/boot.lua').write_text(UPSTREAM)
        self.addons = lumen / 'sls-addons'
        data = self.home / '.local/share/slssteam-integrations'
        data.mkdir(parents=True)
        self.bin = stub_bin(self.home)
        self.cfg = self.home / '.config/luna'
        self.env = dict(os.environ, HOME=str(self.home),
                        XDG_DATA_HOME=str(self.home / '.local/share'),
                        XDG_RUNTIME_DIR=str(self.home / 'run'),
                        XDG_CONFIG_HOME=str(self.home / '.config'),
                        LUNA_SIGNING_DIR=str(self.cfg),
                        PATH=str(self.bin) + os.pathsep + os.environ.get('PATH', ''))
        self.keys = self.temp / 'keys'
        self.keys.mkdir()
        self.dist = self.temp / 'dist'
        self.dist.mkdir()

    # -- helpers ---------------------------------------------------------
    def luna(self, *args, env=None):
        return subprocess.run([sys.executable, str(LUNA)] + list(args),
                              capture_output=True, text=True, env=env or self.env)

    def genkey(self, name):
        path = self.keys / name
        subprocess.run([KEYGEN, '-q', '-t', 'ed25519', '-N', '', '-C', 'test-' + name, '-f', str(path)],
                       check=True, capture_output=True)
        return path

    def trust(self, key):
        result = self.luna('signing', 'trust', str(key) + '.pub')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def bundle(self):
        """A fresh copy of the released (unsigned) bundle with a matching sidecar."""
        target = self.dist / self.base_bundle.name
        shutil.copyfile(self.base_bundle, target)
        self.write_sidecar(target)
        return target

    @staticmethod
    def write_sidecar(bundle):
        Path(str(bundle) + '.sha256').write_text(
            '%s  %s\n' % (hashlib.sha256(bundle.read_bytes()).hexdigest(), bundle.name))

    def sign(self, bundle, key, namespace='luna-release', data=None):
        """Detached signature for `data` (default: the bundle), stored beside the bundle."""
        source = bundle
        if data is not None:
            source = self.temp / 'signed-content'
            source.write_bytes(data)
        sig = Path(str(source) + '.sig')
        sig.unlink(missing_ok=True)
        subprocess.run([KEYGEN, '-Y', 'sign', '-f', str(key), '-n', namespace, str(source)],
                       check=True, capture_output=True)
        wanted = Path(str(bundle) + '.sig')
        if sig != wanted:
            wanted.unlink(missing_ok=True)
            shutil.move(str(sig), str(wanted))
        return wanted

    def update(self, bundle, *extra, env=None):
        return self.luna('update', '--bundle', str(bundle), '--dry-run', *extra, env=env)

    def refused(self, bundle, needle, *extra, env=None):
        result = self.update(bundle, *extra, env=env)
        text = result.stdout + result.stderr
        self.assertNotEqual(result.returncode, 0, text)
        self.assertIn('REFUSING', text)
        self.assertIn(needle, text)
        self.assertNotIn('bundle :', result.stdout, 'nothing may be unpacked or run after a refusal')
        self.assertNotIn('DRY RUN', result.stdout)
        self.assertFalse(self.addons.exists())
        return text

    def fingerprint(self, key):
        out = subprocess.run([KEYGEN, '-l', '-f', str(key) + '.pub'], capture_output=True, text=True).stdout
        return out.split()[1]

    # -- the policy table ------------------------------------------------
    def test_no_trusted_key_accepts_unsigned_with_a_note(self):
        result = self.update(self.bundle())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('authenticity NOT checked', result.stdout)
        self.assertIn('luna signing trust', result.stdout)

    def test_no_trusted_key_with_a_signature_still_says_it_was_not_checked(self):
        bundle = self.bundle()
        self.sign(bundle, self.genkey('a'))
        result = self.update(bundle)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('authenticity is NOT checked', result.stdout)

    def test_valid_signature_installs_and_prints_the_signer(self):
        key = self.genkey('a')
        self.trust(key)
        bundle = self.bundle()
        self.sign(bundle, key)
        trust_file = self.cfg / 'release-signers'
        before = trust_file.read_bytes()
        result = self.luna('update', '--bundle', str(bundle))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('signed : verified, signer %s' % self.fingerprint(key), result.stdout)
        self.assertTrue((self.addons / 'playtime/backend/main.lua').is_file())
        self.assertEqual(trust_file.read_bytes(), before, 'install must never write the trust file')
        self.assertEqual(sorted(p.name for p in self.cfg.iterdir()), ['release-signers'])

    def test_stripped_signature_is_refused_and_names_the_flag(self):
        key = self.genkey('a')
        self.trust(key)
        bundle = self.bundle()  # a validly signed bundle whose .sig was removed in transit
        self.sign(bundle, key)
        Path(str(bundle) + '.sig').unlink()
        self.refused(bundle, '--allow-unsigned')

    def test_allow_unsigned_overrides_only_a_missing_signature(self):
        self.trust(self.genkey('a'))
        result = self.update(self.bundle(), '--allow-unsigned')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('WARNING', result.stderr)
        self.assertIn('UNSIGNED', result.stderr)

    def test_signature_from_another_key_is_refused_even_with_allow_unsigned(self):
        self.trust(self.genkey('a'))
        bundle = self.bundle()
        self.sign(bundle, self.genkey('b'))
        self.refused(bundle, 'not valid for a trusted key')
        self.refused(bundle, 'not valid for a trusted key', '--allow-unsigned')

    def test_signature_for_a_different_bundle_is_refused(self):
        key = self.genkey('a')
        self.trust(key)
        bundle = self.bundle()
        self.sign(bundle, key, data=bundle.read_bytes() + b'other bundle')
        self.refused(bundle, 'not valid for a trusted key', '--allow-unsigned')

    def test_tampered_bundle_with_the_original_signature_is_refused(self):
        key = self.genkey('a')
        self.trust(key)
        bundle = self.bundle()
        self.sign(bundle, key)
        bundle.write_bytes(bundle.read_bytes() + b'x')
        self.write_sidecar(bundle)  # the attacker fixes the checksum; they cannot fix the signature
        self.refused(bundle, 'not valid for a trusted key', '--allow-unsigned')

    def test_wrong_namespace_is_refused(self):
        key = self.genkey('a')
        self.trust(key)
        bundle = self.bundle()
        self.sign(bundle, key, namespace='file')
        self.refused(bundle, 'not valid for a trusted key', '--allow-unsigned')

    def test_signature_symlink_is_refused(self):
        key = self.genkey('a')
        self.trust(key)
        bundle = self.bundle()
        real = self.sign(bundle, key)
        elsewhere = self.temp / 'elsewhere.sig'
        shutil.move(str(real), str(elsewhere))
        real.symlink_to(elsewhere)  # perfectly valid signature, but reached through a link
        self.refused(bundle, 'cannot be used', '--allow-unsigned')

    def test_oversized_or_garbage_signature_is_refused(self):
        self.trust(self.genkey('a'))
        bundle = self.bundle()
        sig = Path(str(bundle) + '.sig')
        sig.write_bytes(b'A' * (4 * 1024 * 1024))
        self.refused(bundle, 'implausibly large', '--allow-unsigned')
        sig.write_text('-----BEGIN SSH SIGNATURE-----\ngarbage\n-----END SSH SIGNATURE-----\n')
        self.refused(bundle, 'not valid for a trusted key', '--allow-unsigned')
        sig.write_bytes(b'')
        self.refused(bundle, 'not valid for a trusted key', '--allow-unsigned')

    def test_extra_untrusted_lines_in_the_trust_file_trust_nothing_new(self):
        a, b = self.genkey('a'), self.genkey('b')
        self.trust(a)
        blob_b = (Path(str(b) + '.pub').read_text().split()[1])
        trust_file = self.cfg / 'release-signers'
        with open(trust_file, 'a') as handle:  # shapes ssh-keygen itself would honour, but Luna must not
            handle.write('* ssh-ed25519 %s\n' % blob_b)
            handle.write('luna-release cert-authority ssh-ed25519 %s\n' % blob_b)
            handle.write('luna-release namespaces="file" ssh-ed25519 %s\n' % blob_b)
            handle.write('someone@else ssh-ed25519 %s\n' % blob_b)
        bundle = self.bundle()
        self.sign(bundle, b)
        self.refused(bundle, 'not valid for a trusted key', '--allow-unsigned')
        status = self.luna('signing', 'status')
        self.assertIn('ignored', status.stdout)
        self.sign(bundle, a)  # the genuine key still works
        self.assertEqual(self.update(bundle).returncode, 0)

    def test_a_trust_file_with_only_junk_fails_closed(self):
        self.cfg.mkdir(parents=True)
        blob = Path(str(self.genkey('b')) + '.pub').read_text().split()[1]
        (self.cfg / 'release-signers').write_text('* ssh-ed25519 %s\n' % blob)
        self.refused(self.bundle(), 'trust file is unusable', '--allow-unsigned')

    def test_a_bundle_cannot_carry_its_own_trust_anchor(self):
        a, attacker = self.genkey('a'), self.genkey('attacker')
        self.trust(a)
        bundle = self.bundle()
        before = (self.cfg / 'release-signers').read_bytes()
        with tarfile.open(bundle) as tar:
            members = [(m, tar.extractfile(m).read() if m.isfile() else None) for m in tar.getmembers()]
        root = members[0][0].name.split('/')[0]
        blob = Path(str(attacker) + '.pub').read_text().split()[1]
        planted = ('luna-release namespaces="luna-release" ssh-ed25519 %s\n' % blob).encode()
        for name in ('release-signers', 'scripts/release-signers', 'allowed_signers'):
            info = tarfile.TarInfo('%s/%s' % (root, name))
            info.size = len(planted)
            members.append((info, planted))
        with tarfile.open(bundle, 'w:gz') as tar:
            for info, data in members:
                tar.addfile(info, io.BytesIO(data) if data is not None else None)
        self.write_sidecar(bundle)
        self.sign(bundle, attacker)  # signed by the key the bundle itself claims to be trusted
        self.refused(bundle, 'not valid for a trusted key', '--allow-unsigned')
        self.assertEqual((self.cfg / 'release-signers').read_bytes(), before)

    def test_trusted_keys_without_ssh_keygen_refuse_unless_allowed(self):
        key = self.genkey('a')
        self.trust(key)
        bundle = self.bundle()
        self.sign(bundle, key)
        env = dict(self.env, PATH=str(self.bin))  # no ssh-keygen reachable
        self.refused(bundle, 'ssh-keygen is not installed', env=env)
        result = self.update(bundle, '--allow-unsigned', env=env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('cannot be verified', result.stderr)

    # -- the other download paths ----------------------------------------
    def test_url_fetches_and_verifies_the_signature(self):
        key = self.genkey('a')
        self.trust(key)
        bundle = self.bundle()
        self.sign(bundle, key)
        url = bundle.as_uri()
        ok = self.luna('update', '--url', url, '--dry-run')
        self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)
        self.assertIn('signed : verified', ok.stdout)
        Path(str(bundle) + '.sig').unlink()
        gone = self.luna('update', '--url', url, '--dry-run')
        self.assertNotEqual(gone.returncode, 0)
        self.assertIn('--allow-unsigned', gone.stderr)
        Path(str(bundle) + '.sig').write_bytes(b'B' * 100000)
        big = self.luna('update', '--url', url, '--dry-run', '--allow-unsigned')
        self.assertNotEqual(big.returncode, 0)
        self.assertIn('implausibly large', big.stderr)

    def test_latest_downloads_the_signature_and_refuses_it_stripped(self):
        key = self.genkey('a')
        self.trust(key)
        bundle = self.bundle()
        self.sign(bundle, key)
        env = dict(self.env, LUNA_TEST_BUNDLE=str(bundle), LUNA_TEST_TAG='v9.9.9')
        ok = self.luna('update', '--latest', '--dry-run', env=env)
        self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)
        self.assertIn('signed : verified', ok.stdout)
        Path(str(bundle) + '.sig').unlink()
        stripped = self.luna('update', '--latest', '--dry-run', env=env)
        self.assertNotEqual(stripped.returncode, 0)
        self.assertIn('--allow-unsigned', stripped.stderr)

    # -- luna release ----------------------------------------------------
    def test_release_signs_when_a_key_exists_and_the_update_verifies_it(self):
        self.assertEqual(self.luna('signing', 'init').returncode, 0)
        out = self.temp / 'rel'
        result = self.luna('release', '--out', str(out))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        bundle = next(out.glob('luna-*.tar.gz'))
        sig = Path(str(bundle) + '.sig')
        self.assertTrue(sig.is_file())
        self.assertIn('signed : SHA256:', result.stdout)
        update = self.luna('update', '--bundle', str(bundle), '--dry-run')
        self.assertEqual(update.returncode, 0, update.stdout + update.stderr)
        self.assertIn('signed : verified', update.stdout)

    def test_release_without_a_key_is_unsigned_and_drops_a_stale_signature(self):
        out = self.temp / 'rel'
        out.mkdir()
        first = self.luna('release', '--out', str(out))
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertIn('signed : NO', first.stdout)
        bundle = next(out.glob('luna-*.tar.gz'))
        Path(str(bundle) + '.sig').write_text('stale')
        again = self.luna('release', '--out', str(out))
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertFalse(Path(str(bundle) + '.sig').exists())

    # -- luna signing ----------------------------------------------------
    def test_init_creates_a_private_key_trusts_it_and_never_overwrites(self):
        result = self.luna('signing', 'init')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        key = self.cfg / 'luna-release-ed25519'
        self.assertEqual(stat.S_IMODE(key.stat().st_mode), 0o600)
        fingerprint = self.fingerprint(key)
        self.assertIn(fingerprint, result.stdout)
        self.assertNotIn('PRIVATE KEY', result.stdout + result.stderr)
        self.assertFalse((self.home / '.ssh').exists(), '~/.ssh must never be touched')
        original = key.read_bytes()
        again = self.luna('signing', 'init')
        self.assertEqual(again.returncode, 2)
        self.assertIn('never overwritten', again.stderr)
        self.assertEqual(key.read_bytes(), original)
        status = self.luna('signing', 'status')
        self.assertIn(fingerprint, status.stdout)
        self.assertIn('ENFORCING', status.stdout)
        self.assertNotIn('PRIVATE KEY', status.stdout)

    def test_trust_and_untrust_by_file_text_and_fingerprint(self):
        a, b = self.genkey('a'), self.genkey('b')
        self.assertIn('not enforcing', self.luna('signing', 'status').stdout)
        self.trust(a)
        pub_b = Path(str(b) + '.pub').read_text().strip()
        self.assertEqual(self.luna('signing', 'trust', pub_b).returncode, 0)  # literal key text
        self.assertIn('already trusted', self.luna('signing', 'trust', pub_b).stdout)
        status = self.luna('signing', 'status').stdout
        self.assertIn(self.fingerprint(a), status)
        self.assertIn(self.fingerprint(b), status)
        self.assertEqual(self.luna('signing', 'untrust', self.fingerprint(b)).returncode, 0)
        self.assertNotIn(self.fingerprint(b), self.luna('signing', 'status').stdout)
        last = self.luna('signing', 'untrust', str(a) + '.pub')
        self.assertEqual(last.returncode, 0, last.stderr)
        self.assertIn('unsigned bundles are accepted again', last.stdout)
        self.assertIn('not enforcing', self.luna('signing', 'status').stdout)

    def test_trust_refuses_things_that_are_not_one_ed25519_public_key(self):
        a, b = self.genkey('a'), self.genkey('b')
        both = self.temp / 'both.pub'
        both.write_text(Path(str(a) + '.pub').read_text() + Path(str(b) + '.pub').read_text())
        for bad in ('hello', str(a), str(both), 'ssh-rsa AAAAB3NzaC1yc2E', 'ssh-ed25519 AAAA'):
            result = self.luna('signing', 'trust', bad)
            self.assertEqual(result.returncode, 2, bad + result.stdout)
        self.assertFalse((self.cfg / 'release-signers').exists())


if __name__ == '__main__':
    unittest.main()
