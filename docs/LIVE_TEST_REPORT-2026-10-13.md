# Live test report - SLS Luna on CachyOS (2026-10-13)

First run of the wrapper against the real stack. Everything below was executed on
this machine; the parts that were **not** verified are listed at the end.

## Environment

| Item | Value |
| --- | --- |
| OS / session | CachyOS (Arch-based), niri on Wayland |
| Steam | native Linux Steam, client build `1788652215` |
| Lumen / LuaTools-Moon / SLSsteam-Moon | `v2.9` each |
| Add-ons | `playtime 0.4.0` (`ed597cbe`), `presence 0.3.0` (`e77326af`) |
| Manifests / shortcuts | 643 numeric manifests, 322 non-Steam shortcuts |
| Steam account | one recorded account (last digits only here) |

## Offline suites

```bash
python3 -m unittest discover -s tests -p 'test_*.py'   # 19 tests
bash scripts/test-backend.sh                           # bridge + both real backends
python3 scripts/luna.py verify                         # pinned payload integrity
python3 -m py_compile scripts/luna.py tests/*.py
luac -p tests/bridge-smoke.lua bridge/bridge.lua addons/*/plugin/backend/main.lua
```

All pass. The installer suite runs against a disposable `HOME` with a Lumen
fixture and a stubbed `pgrep`, so it does not care whether the developer's own
Steam is open. The composition test loads the **shipped** payloads: both backends
register into one registry, both asset lists carry two scripts, and a deliberately
corrupted `presence` backend is skipped while `playtime` still registers.

`verify` was also checked negatively: tampering with one byte of
`addons/presence/plugin/public/panel.js` makes it fail and name the file.

## Live cycle

| Step | Observed |
| --- | --- |
| `preflight` with Steam open | exit 2, refused: "Steam is running" **and** "A Lumen sidecar is still running ... pkill -u $(id -u) -x lumen" |
| Steam stopped | exited through its own **Steam → Exit**; no force-kill |
| Sidecar survived the exit | `install` **refused with exit 2** while the stale `lumen` process was alive, exactly the failure the presence work found; stopped with the printed command |
| `preflight` again | READY, nothing modified |
| `uninstall` | removed both add-ons and the bridge; `boot.lua` **byte-identical** to the pristine original captured before any add-on existed |
| `install` | both add-ons + bridge written, `boot.lua` patched exactly once, exit 0 |
| `status` | every payload file `same`; `boot.lua patched (marker x1, backup present)` |
| Steam restarted | `playtime` healthy: 643 manifests, 322 shortcuts, 65 records |
| | `presence` healthy and enabled, still owning its placeholder mapping |
| Shell | **both** panels mounted: `__SlsPlaytimePanel` and `__SlsPresencePanel` true, both buttons and panels present |

### Nothing of the user's was disturbed

```
data files unchanged : True
  playtime/76561198000000042.json              92f3804b23eab283
  playtime/76561198000000042.json.bak          31f8e9a335260a81
  playtime/76561198000000042.json.pre-decky-import-20261013-120816  a4b6c6fe6afc7618
  presence.json                                718dbb28ab65a124
presence mapping kept: True  {"3808690": 2167367540}
shortcut library     : 322 -> 322 entries
```

The presence mapping surviving the cycle matters: it is the record that presence
owns that placeholder shortcut, so the placeholder is reconciled after the
reinstall instead of being orphaned.

## What was not verified

1. **A Lumen release that moves the anchors.** The patcher's fail-closed path is
   covered by a test with a drifted fixture, not by a new Lumen.
2. **A second add-on set / version downgrade.** `--force` and `verify` cover the
   code paths; only the pinned versions above were installed here.
3. **`vendor-update.sh` against a moved upstream.** The pinning was exercised by
   re-running it locally; a real upstream commit was not fetched.
4. **The refusals' exit codes as a contract for other tools.** They are asserted
   in tests and observed here (2 for refusals, 3 for needing `--force`, 4 for a
   patch problem), but nothing else consumes them yet.

No Steam credentials, tokens or account names are recorded here. The install only
writes under `~/.local/share/Lumen/sls-addons` and the reversible `boot.lua`
patch; user data under `~/.local/share/slssteam-integrations` was byte-identical
afterwards.
