# Lumen (optional host)

`luna install` and `luna bootstrap` default to Luna's own `luna-host` in
`~/.local/share/Luna` (no Lumen, LuaTools, wrapper patch or Steam pin). Add
`--lumen` to opt in to the previous full pipeline: pin the Steam client, install
Lumen/LuaTools/SLSsteam with the pinned installer, then install Luna on top and
switch the active host to Lumen. An existing install whose `host.json` says
`lumen` is never downgraded by a plain `luna install`; switching that way stays an
explicit `luna host cutover`, while `--lumen` on a luna-host machine performs the
full Lumen install and switches after the usual Steam-closed check.
