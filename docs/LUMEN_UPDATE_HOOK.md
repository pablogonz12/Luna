# Luna reattachment after Lumen updates

Development implementation, 2026-10-28. No upstream fork, system launcher patch,
new network service, administrator access or game-executable modification is used.

## Why Luna disappeared

Lumen's **Update All** launches a detached shell script that downloads/runs the
base stack installer. Lumen extraction replaces `lua/boot.lua` and `lua/about.lua`;
the Luna payload directories survive, but the bridge and menu entry stop loading.
Recent base installers can also auto-launch Steam before a post-update repair.

## Primary hook

`scripts/lumen-hook.py enable` enrols the **already installed** bridge hash and
copies its worker and the guarded bridge patcher into
`~/.local/share/slssteam-luna/`, outside Lumen's replaceable tree. It decorates
`about.update_script` without changing its flags, channel selection or installer
URL. The generated terminal script:

1. Sets the installer's supported `SLS_NO_LAUNCH=1` switch for this run only.
2. Runs the original base installer and captures its exit status as before.
3. Only on success, invokes the independent recovery worker **before** the
   original completion message/pause.
4. Reattaches the installed bridge and re-arms the updater decoration in the new
   `about.lua`, making the next menu update follow the same path.

Steam is then restarted by the user, as the terminal message requests. Failed
base installs do not claim Luna recovery. Failed Luna recovery is visibly reported
as requiring attention; it does not silently print the normal success message.

## Fallback and safety

User units `luna-lumen-hook.path`, `.timer`, and `.service` watch `versions.json`
(written after base/plugin installation), with a once-per-minute fallback for
missed events or manual updates. Healthy checks do not rewrite Lumen or status
files. No resident polling process is added. Without a user systemd manager the
terminal hook still works, but enabling reports that the fallback is unavailable.

The worker never kills/restarts Steam or Lumen. If either is running, it records
`deferred` and retries on a later fallback tick. Both upstream contracts are
validated before either file changes; edits are atomic, locked between recovery
workers, backed up by content hash, and checked again before commit. A failure
rolls back changes from that attempt only when their content still matches.
Custom units, symlinks at critical targets and modified hook blocks are refused.

Recovery neither downloads nor replaces add-ons, touches Steam policy/Proton,
nor restores a whole userdata directory. A bridge-hash change requires explicit
re-enrolment (`luna hook on`, also part of a normal Luna installation). Full Luna
uninstall, or removing just the Luna shell, disarms recovery so it cannot restore
something the user deliberately removed. Backups are retained.

```sh
python3 scripts/luna.py hook on
python3 scripts/luna.py hook status
python3 scripts/luna.py hook repair
python3 scripts/luna.py hook off
journalctl --user -u luna-lumen-hook.service
```

Normal installation also creates a non-overwriting `~/.local/bin/luna` command.
Its `hook` subcommand delegates directly to the independent worker, so recovery
does not depend on the updater copy inside Lumen. A pre-existing custom command
is preserved. The worker can always be called directly:

```sh
python3 "$HOME/.local/share/slssteam-luna/lumen-hook.py" status
python3 "$HOME/.local/share/slssteam-luna/lumen-hook.py" repair
```

## Evidence and limits

`tests/test_lumen_hook.py` uses disposable HOME directories and fake base
installers. It executes the generated shell script through two successive
successful updates, checks failure propagation, standalone recovery, deferred
running processes, rollback, idempotence, locks, custom-unit preservation and
normal/partial uninstall. Steam policy and installed local payloads are compared
byte-for-byte. These tests do not download/install an actual future base release.

The current Lumen updater contract is guarded, not a promise about all future
upstream implementations. Unknown contracts require a compatibility update;
there is no unsafe full-reinstall fallback. A manual update that starts Steam
before recovery can run will remain deferred until Steam/Lumen are closed.

### Local installation evidence, 2026-10-28

With the user's permission, Steam/Lumen were closed, the installed bridge was
enrolled and restored, the update hook and user units were enabled, and Steam was
started through its existing injected launcher. CDP then confirmed the Luna RPC,
the loaded Luna panel and a visible `sls-luna-btn` labelled **Luna**. Presence
reported healthy with zero owned helpers. The installed add-on payloads were not
reinstalled; the newer Presence development payload remains source-only.

The recovery worker and CLI match the reviewed source; both fallback units are
enabled/active. Permanent rollback originals are in
`~/.local/share/slssteam-luna/backups/`. Temporary diagnostic/userdata snapshots
under `/tmp/opencode/` were cleared by a subsequent server restart, so a final
before/after userdata-hash comparison could not be completed and is not claimed.

Local checks passed: **202 Node tests**, **144 Python tests** (including 21 hook
tests), the Lua suites, syntax checks and manifest/source verification. The two
updates were simulated, not real downloads/reinstalls; no hosted CI or publishing
is claimed. Server restarts did not require reinstalling the hook.
