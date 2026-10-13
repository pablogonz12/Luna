# Test plan and release gates

## Automated

| Suite | Command | Covers |
| --- | --- | --- |
| Installer | `python3 -m unittest discover -s tests -p 'test_*.py'` | install both, install one, idempotence, dry run, refusals (Steam, Lumen sidecar, drifted `boot.lua`, divergent payload), uninstall one / last, backup-missing refusal, user data untouched, status reporting, manifest verification |
| Composition | `bash scripts/test-backend.sh` | the real bridge with both **real** backends: both register into one registry, both asset lists carry two scripts, and a deliberately broken add-on is skipped instead of stopping the healthy one |
| Payload pinning | `python3 scripts/luna.py verify` | every payload and shared file matches `vendor/MANIFEST.json`, and no file exists that the manifest does not list |
| Drift | `python3 scripts/luna.py verify --sources` | the pinned bytes equal the local add-on checkouts (skipped where they are absent) |
| Syntax | CI | `python3 -m py_compile`, `luac -p`, `bash -n` |

The installer suite runs against a disposable `HOME` with a minimal Lumen fixture
and a stubbed `pgrep`, so it never depends on the developer's own Steam being
open or closed.

## On-device gates

1. `preflight` reports the Steam root, manifest count, both payload versions,
   `boot.lua` state and the data directory, and refuses while Steam or a Lumen
   sidecar is running.
2. `install` with Steam closed and no sidecar: both add-ons load after a restart,
   `boot.lua` shows exactly one marker, and `status` reports every file `same`.
3. Live: the Playtime panel reports a healthy agent and plausible manifest and
   shortcut counts; the Presence panel reports a healthy agent.
4. `uninstall --only presence` leaves playtime working after a restart;
   `uninstall` restores `boot.lua` byte-for-byte.
5. User data: the playtime ledger and `presence.json` are byte-identical across
   install, upgrade and uninstall.
6. A second add-on version (re-run `vendor-update.sh`, then install) replaces the
   payload and `status` returns to `same` everywhere.

## Evidence requirements

Record the environment, the commands, the observed output and any screenshot.
State which gates were **not** run and why. Never commit Steam credentials, tokens
or account identifiers.
