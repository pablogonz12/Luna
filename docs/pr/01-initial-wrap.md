# feat: wrap the SLSsteam-Moon add-ons into one installer

## Summary

Adds SLS Luna: one preflight, install, status and rollback for the Lumen add-ons,
so `playtime` and `presence` no longer have to be installed, upgraded and removed
one project at a time.

- **One boot hook.** A single reversible patch to `lua/boot.lua` installs the
  shared bridge, which already loads every add-on it finds. The second installer
  no longer has to reason about an already-patched file.
- **Pinned payloads.** `addons/` and the shared bridge and patcher are extracted
  with `git archive` from the add-on projects at exact commits and recorded with
  sha256 in `vendor/MANIFEST.json`. Installing needs no network and no second
  checkout, and `verify` proves the bytes are the reviewed ones.
- **Fail-closed refusals**, including the two failure modes found on this machine:
  Steam running, and a Lumen sidecar that survived `steam -shutdown` and keeps the
  old `boot.lua` in memory. Luna refuses and prints the command; it never kills a
  process for the user.
- **Transactional install and uninstall.** Every write is undone if a later step
  fails, and the last uninstall restores `boot.lua` before deleting the payload it
  points at. User data is never touched.
- **A path to upstream Lumen** is documented in `docs/UPSTREAM.md` and explicitly
  not started.

## Verification

- 19 installer tests: install both, install one, idempotence, dry run, every
  refusal, uninstall one and last, missing backup, divergent payload, tampering,
  data safety.
- A composition test that loads the **real** bridge with **both real** backends
  and asserts they register together, expose two UI and two control scripts, and
  that a deliberately broken add-on is skipped rather than fatal.
- `verify` fails on a tampered payload (negative test).
- Live on the target machine: preflight correctly refuses while Steam and the
  sidecar run; install, status, uninstall and both panels are exercised after a
  restart.

## Not in this PR

Hosting add-ons upstream in Lumen, live health probing, and data snapshot/restore
are tracked as issues.
