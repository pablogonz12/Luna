#!/usr/bin/env python3
"""Release signing: `luna release` signs, `luna update` verifies against a local trust file.

Hermetic: a disposable HOME, throwaway ed25519 keys generated inside the temp dir, no
network (--url talks to a throwaway server on 127.0.0.1). Nothing reads ~/.ssh or the real ~/.config/luna.
"""
import hashlib
import io
import json
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
from unittest import mock

from test_luna import HERE, LUNA, UPSTREAM, stub_bin

KEYGEN = shutil.which('ssh-keygen')


def load_luna():
    import importlib.util
    spec = importlib.util.spec_from_file_location('luna_signing_mod', str(LUNA))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@unittest.skipUnless(KEYGEN, 'ssh-keygen is not installed')
class SigningTestCase(unittest.TestCase):
    base_dir = None
    base_bundle = None

    @classmethod
    def setUpClass(cls):
        cls.base_dir = Path(tempfile.mkdtemp(prefix='luna-sign-base-'))
        env = dict(os.environ, HOME=str(cls.base_dir), XDG_CONFIG_HOME=str(cls.base_dir / 'cfg'),
                   LUNA_SIGNING_DIR=str(cls.base_dir / 'nokey'), LUNA_ANCHOR_HOME=str(cls.base_dir / 'anchor'))
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
                        LUNA_ANCHOR_HOME=str(self.temp / 'anchor'),
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
        url = self.serve(self.dist) + '/' + bundle.name
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
        # Review finding 1: emptying the trust file must never silently turn enforcement off.
        self.assertIn('STILL ENFORCING', last.stdout)
        self.assertIn('signing disable', last.stdout)
        self.assertIn('ENFORCING', self.luna('signing', 'status').stdout)
        self.assertEqual(self.luna('signing', 'disable').returncode, 0)
        self.assertIn('not enforcing', self.luna('signing', 'status').stdout)

    # -- review finding 1: enforcement must not depend on the environment -----
    def tools(self, home=None):
        return Path(home or self.home) / '.local/share/Lumen/sls-luna'

    def anchor_tools(self):
        return self.temp / 'anchor/.local/share/Lumen/sls-luna'

    def test_trust_records_an_enforcement_marker_in_both_places(self):
        self.trust(self.genkey('a'))
        for directory in (self.tools(), self.anchor_tools()):
            data = json.loads((directory / 'signing-policy.json').read_text())
            self.assertIs(data['enforce'], True)
            self.assertEqual(data['trustFile'], str(self.cfg / 'release-signers'))
        self.assertIn('marker', self.luna('signing', 'status').stdout)

    def test_a_different_environment_cannot_switch_enforcement_off(self):
        self.trust(self.genkey('a'))
        bundle = self.bundle()
        expected = str(self.cfg / 'release-signers')
        variants = {
            'other LUNA_SIGNING_DIR': dict(self.env, LUNA_SIGNING_DIR=str(self.temp / 'elsewhere')),
            'other XDG_CONFIG_HOME': {k: v for k, v in dict(self.env, XDG_CONFIG_HOME=str(self.temp / 'x')).items()
                                      if k != 'LUNA_SIGNING_DIR'},
        }
        for label, env in variants.items():
            text = self.refused(bundle, 'trust file', env=env)
            self.assertIn(expected, text, label)
            self.assertIn('signing disable', text, label)
        # a brand new HOME (a timer or ssh session with a different environment) is caught by the anchor
        other_home = self.temp / 'otherhome'
        other_home.mkdir()
        env = {k: v for k, v in dict(self.env, HOME=str(other_home), XDG_CONFIG_HOME=str(other_home / 'c')).items()
               if k != 'LUNA_SIGNING_DIR'}
        self.assertIn(expected, self.refused(bundle, 'trust file', env=env))
        # and the trust file simply being deleted
        (self.cfg / 'release-signers').unlink()
        self.assertIn(expected, self.refused(bundle, 'trust file'))
        self.refused(bundle, 'trust file', '--allow-unsigned')

    def test_a_machine_with_keys_but_no_marker_heals_itself(self):
        key = self.genkey('a')
        self.trust(key)
        for directory in (self.tools(), self.anchor_tools()):
            (directory / 'signing-policy.json').unlink()
        self.assertIn('ENFORCING', self.luna('signing', 'status').stdout)
        for directory in (self.tools(), self.anchor_tools()):
            self.assertTrue((directory / 'signing-policy.json').is_file())
            (directory / 'signing-policy.json').unlink()
        bundle = self.bundle()
        self.sign(bundle, key)
        self.assertEqual(self.update(bundle).returncode, 0)  # update heals it as well
        self.assertTrue((self.tools() / 'signing-policy.json').is_file())

    def test_a_marker_that_is_unreadable_fails_closed(self):
        self.trust(self.genkey('a'))
        (self.tools() / 'signing-policy.json').write_text('{not json')
        self.refused(self.bundle(), 'enforcement marker')

    def test_untrusting_the_last_key_keeps_enforcing_until_disabled(self):
        a = self.genkey('a')
        self.trust(a)
        last = self.luna('signing', 'untrust', str(a) + '.pub')
        self.assertEqual(last.returncode, 0, last.stderr)
        text = self.refused(self.bundle(), 'no usable trusted key')
        self.assertIn('signing disable', text)
        disabled = self.luna('signing', 'disable')
        self.assertEqual(disabled.returncode, 0, disabled.stdout + disabled.stderr)
        self.assertEqual(self.update(self.bundle()).returncode, 0)
        for directory in (self.tools(), self.anchor_tools()):
            self.assertIs(json.loads((directory / 'signing-policy.json').read_text())['enforce'], False)
        self.trust(a)  # trusting a key again turns enforcement back on
        self.assertIn('ENFORCING', self.luna('signing', 'status').stdout)
        self.refused(self.bundle(), 'no signature')

    def test_init_also_records_the_marker(self):
        self.assertEqual(self.luna('signing', 'init').returncode, 0)
        self.assertIs(json.loads((self.anchor_tools() / 'signing-policy.json').read_text())['enforce'], True)

    # -- review finding 3: --url hardening ---------------------------------
    def serve(self, directory):
        import functools
        import http.server
        import threading

        class Handler(http.server.SimpleHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                targets = {'/redir-file': 'file:///etc/hostname', '/redir-http': 'http://203.0.113.9/x.tar.gz',
                           '/redir-ftp': 'ftp://203.0.113.9/x.tar.gz'}
                if self.path in targets:
                    self.send_response(302)
                    self.send_header('Location', targets[self.path])
                    self.end_headers()
                    return
                super().do_GET()
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(Handler, directory=str(directory)))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return 'http://127.0.0.1:%d' % server.server_address[1]

    def test_url_refuses_every_scheme_but_https_and_loopback_http(self):
        bundle = self.bundle()
        for url in (bundle.as_uri(), 'ftp://127.0.0.1/x.tar.gz', 'http://example.invalid/x.tar.gz',
                    'https://user:pw@example.invalid/x.tar.gz', 'gopher://example.invalid/x', '//example.invalid/x',
                    'http://127.0.0.1.example.invalid/x.tar.gz'):
            result = self.luna('update', '--url', url, '--dry-run')
            self.assertNotEqual(result.returncode, 0, url)
            self.assertIn('REFUSING', result.stderr, url)
            self.assertIn('https', result.stderr, url)
            self.assertNotIn('Traceback', result.stderr)

    def test_url_follows_redirects_only_to_https(self):
        base = self.serve(self.dist)
        for path in ('/redir-file', '/redir-http', '/redir-ftp'):
            result = self.luna('update', '--url', base + path, '--dry-run')
            self.assertNotEqual(result.returncode, 0, path)
            self.assertIn('redirect', result.stderr, path)
            self.assertIn('REFUSING', result.stderr, path)

    def test_download_has_a_size_cap_and_streams(self):
        module = load_luna()
        base = self.serve(self.dist)
        payload = self.dist / 'big.bin'
        payload.write_bytes(b'x' * 5000)
        dest = self.temp / 'out.bin'
        ok, why = module.url_download(base + '/big.bin', dest, 4000)
        self.assertFalse(ok)
        self.assertIn('larger than', why)
        self.assertFalse(dest.exists(), 'an oversized download leaves nothing behind')
        ok, why = module.url_download(base + '/big.bin', dest, 5000)
        self.assertTrue(ok, why)
        self.assertEqual(dest.stat().st_size, 5000)
        # the optional small files have their own small caps
        self.assertLessEqual(module.URL_SIDECAR_MAX, 8192)
        self.assertLessEqual(module.SIG_MAX_BYTES, 8192)
        self.assertLessEqual(module.URL_BUNDLE_MAX, 256 * 1024 * 1024)
        self.assertGreater(module.URL_BUNDLE_MAX, 10 * 1024 * 1024)

    def test_an_oversized_sidecar_is_refused_not_ignored(self):
        bundle = self.bundle()
        Path(str(bundle) + '.sha256').write_text('0' * 100000)
        result = self.luna('update', '--url', self.serve(self.dist) + '/' + bundle.name, '--dry-run')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('REFUSING', result.stderr)
        self.assertIn('.sha256', result.stderr)

    def test_redirect_handler_allows_https_only(self):
        import urllib.request
        module = load_luna()
        handler = module.HttpsOnlyRedirect()
        request = urllib.request.Request('https://example.invalid/a')
        self.assertIsNotNone(handler.redirect_request(request, None, 302, 'Found', {}, 'https://other.invalid/b'))
        for bad in ('http://other.invalid/b', 'file:///etc/passwd', 'ftp://other.invalid/b', 'http://127.0.0.1/b'):
            with self.assertRaises(module.UrlRefused):
                handler.redirect_request(request, None, 302, 'Found', {}, bad)

    # -- rollback protection ---------------------------------------------------
    def base_version(self):
        with tarfile.open(self.base_bundle) as tar:
            member = next(m for m in tar.getmembers() if m.name.endswith('/vendor/MANIFEST.json'))
            return json.loads(tar.extractfile(member).read())['addons']['luna']['plugin_version']

    def retag(self, version):
        """A copy of the base bundle whose bundle version is `version` (file list and sidecar refreshed)."""
        with tarfile.open(self.base_bundle) as tar:
            members = [(m, tar.extractfile(m).read() if m.isfile() else None) for m in tar.getmembers()]
        root = members[0][0].name.split('/')[0]
        files = {m.name[len(root) + 1:]: data for m, data in members if m.isfile()}
        manifest = json.loads(files['vendor/MANIFEST.json'])
        manifest['addons']['luna']['plugin_version'] = version
        files['vendor/MANIFEST.json'] = json.dumps(manifest).encode()
        listing = json.loads(files['RELEASE-FILES.json'])
        listing['files']['vendor/MANIFEST.json'] = hashlib.sha256(files['vendor/MANIFEST.json']).hexdigest()
        files['RELEASE-FILES.json'] = json.dumps(listing).encode()
        target = self.dist / ('luna-retag-%s.tar.gz' % version)
        with tarfile.open(target, 'w:gz') as tar:
            for rel, data in sorted(files.items()):
                info = tarfile.TarInfo('%s/%s' % (root, rel))
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        self.write_sidecar(target)
        return target

    def bumped(self, step=1):
        major, minor, patch = (int(x) for x in self.base_version().split('.'))
        return '%d.%d.%d' % (major, minor, patch + step)

    def mark(self, home=None):
        path = self.tools(home) / 'update-state.json'
        return json.loads(path.read_text())['min_version'] if path.is_file() else None

    def signed(self, version, key):
        bundle = self.retag(version)
        self.sign(bundle, key)
        return bundle

    def test_rollback_is_refused_equal_is_allowed_newer_raises_the_mark(self):
        key = self.genkey('a')
        self.trust(key)
        newer = self.signed(self.bumped(), key)
        first = self.luna('update', '--bundle', str(newer))
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertEqual(self.mark(), self.bumped())
        self.assertEqual(json.loads((self.anchor_tools() / 'update-state.json').read_text())['min_version'], self.bumped())
        older = self.signed(self.base_version(), key)
        refused = self.luna('update', '--bundle', str(older))
        self.assertNotEqual(refused.returncode, 0, refused.stdout)
        self.assertIn('REFUSING', refused.stderr)
        self.assertIn('older than', refused.stderr)
        self.assertIn('--allow-downgrade', refused.stderr)
        dry = self.luna('update', '--bundle', str(older), '--dry-run')
        self.assertNotEqual(dry.returncode, 0, 'the dry run must predict the refusal')
        self.assertEqual(self.mark(), self.bumped())
        equal = self.luna('update', '--bundle', str(self.signed(self.bumped(), key)))  # a rebuild
        self.assertEqual(equal.returncode, 0, equal.stdout + equal.stderr)
        newest = self.signed(self.bumped(2), key)
        self.assertEqual(self.luna('update', '--bundle', str(newest)).returncode, 0)
        self.assertEqual(self.mark(), self.bumped(2))

    def test_allow_downgrade_installs_but_never_lowers_the_mark(self):
        key = self.genkey('a')
        self.trust(key)
        self.assertEqual(self.luna('update', '--bundle', str(self.signed(self.bumped(), key))).returncode, 0)
        older = self.signed(self.base_version(), key)
        done = self.luna('update', '--bundle', str(older), '--allow-downgrade')
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn('WARNING', done.stderr)
        self.assertEqual(self.mark(), self.bumped())

    def test_an_unsigned_install_never_raises_the_mark(self):
        key = self.genkey('a')
        self.trust(key)
        unsigned = self.retag(self.bumped(9))
        done = self.luna('update', '--bundle', str(unsigned), '--allow-unsigned')
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIsNone(self.mark())
        self.assertEqual(self.luna('update', '--bundle', str(self.signed(self.bumped(), key)), '--dry-run').returncode, 0)
        self.assertIsNone(self.mark(), 'a dry run never writes the mark')

    def test_the_mark_is_written_only_after_the_installer_exits_zero(self):
        key = self.genkey('a')
        self.trust(key)
        bundle = self.signed(self.bumped(), key)
        module = load_luna()
        fake_verify = mock.patch.object(module, 'signing_verify', return_value=('SHA256:test', ''))
        with mock.patch.dict(os.environ, self.env), fake_verify:  # the installer is faked, so is ssh-keygen
            with mock.patch.object(module.subprocess, 'run', return_value=subprocess.CompletedProcess([], 3)):
                self.assertEqual(module.install_bundle(bundle), 3)
            self.assertIsNone(self.mark(), 'a failed installer must not raise the mark')
            with mock.patch.object(module.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)):
                self.assertEqual(module.install_bundle(bundle), 0)
            self.assertEqual(self.mark(), self.bumped())

    def test_a_corrupt_state_file_refuses_while_enforcing_only(self):
        key = self.genkey('a')
        state = self.tools() / 'update-state.json'
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text('{"min_version": "garbage"')
        # not enforcing: nothing to protect, nothing to refuse
        self.assertEqual(self.update(self.retag(self.bumped())).returncode, 0)
        self.trust(key)
        for content in ('{"min_version": "garbage"', '{"min_version": "1.2"}', '[]', '{"min_version": 7}'):
            state.write_text(content)
            text = self.refused(self.signed(self.bumped(), key), 'update-state.json')
            self.assertIn('REFUSING', text)
            self.assertEqual(state.read_text(), content, 'a damaged state file is never silently reset')

    def test_a_machine_that_does_not_enforce_may_install_older_bundles(self):
        newer = self.retag(self.bumped(3))
        self.assertEqual(self.luna('update', '--bundle', str(newer)).returncode, 0)
        self.assertIsNone(self.mark())
        self.assertEqual(self.luna('update', '--bundle', str(self.retag(self.base_version()))).returncode, 0)

    def test_the_bundle_version_must_be_plain_numbers(self):
        for version in ('1.0.0-rc1', '1.0', 'v1.0.0', '1.0.0\n', '01.a.0', '1.0.0.0'):
            bundle = self.retag(version)
            text = self.refused(bundle, 'bundle version')
            self.assertIn('x.y.z', text)

    def test_trust_signing_key_is_unaffected_and_status_shows_the_floor(self):
        key = self.genkey('a')
        self.trust(key)
        self.assertEqual(self.luna('update', '--bundle', str(self.signed(self.bumped(), key))).returncode, 0)
        self.assertIn('minimum bundle version: %s' % self.bumped(), self.luna('signing', 'status').stdout)

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
