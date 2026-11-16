# One GE-Proton version per machine

**Rule:** keep exactly one GE-Proton installed in `~/.local/share/Steam/compatibilitytools.d`.
`luna proton prune` (default `--keep 1`) enforces it; `proton status`, `install` and
`luna doctor` warn when there are two or more.

## Failure mode (diagnosed 2026-11-15)

- At start Steam runs a driver query (`d3ddriverquery64.exe`) once per registered compatibility
  tool, all with app id 0, so every tool shares `steamapps/compatdata/0/pfx`.
- A Proton prefix holds ~600 symlinks per architecture (`drive_c/windows/system32`, `syswow64`)
  pointing straight at DLLs inside the tool that last set it up
  (`<tool>/files/lib/wine/{x86_64,i386}-windows/*.dll`).
- When another version's `wineboot` prefix update runs while the links point into the first one,
  it writes *through* the symlinks. Observed: 1,208 Windows DLLs of GE-Proton11-12 (616
  x86_64, 592 i386) were overwritten in place with GE-Proton11-7's; the unix `.so` side stayed
  untouched, so `rundll32`/`wineopenxr` asserted with a dialog on every Steam start.
  Reproduced in isolation with `wine wineboot -u` of one version against the other's prefix.
- `compatdata/0/version`, `config_info` and `pfx/.update-timestamp` (the `wine.inf` mtime of the
  last updating build) show who last set the prefix up.

## What Luna does about it

- `install` records a manifest (`~/.local/share/Lumen/sls-luna/proton-manifests/NAME.json`)
  from the sha512-verified tree; `luna proton verify` detects changed/missing/extra files.
  Manifests are never made from the current disk state (`--rebuild` re-downloads).
- Cheap heuristics (no hashing) in `proton status` / `doctor`: DLLs bulk-newer than the tool's
  `version` file; prefix links that dangle or point into a tool other than the one named in
  `config_info`/`version`. Skipped on purpose: comparing DLL size/mtime across installed versions,
  since many DLLs are legitimately identical between releases (false positives).
- `install`/`remove`/`prune` refuse while Steam or a process running from the tool is up.
- A version any game is mapped to is never removed by `prune`; if it stays next to a newer one the
  hazard remains, so remap those games and prune again.

## Recovery

Close Steam, `luna proton verify`, then `luna proton remove NAME --force && luna proton install NAME`.
