# The optional Lumen pipeline

`luna install` and `luna bootstrap` default to Luna's own `luna-host`. Nothing
else is needed: no Lumen, no LuaTools, no SLSsteam and no Steam client change.

`--lumen` opts in to the older, full pipeline:

1. Pin and hard-lock the Steam client (roll back to the ubuntu12 build, write
   the update block, make `package/` read-only).
2. Run the pinned upstream installer for Lumen, LuaTools and SLSsteam, verified
   by commit and SHA256.
3. Install the Luna add-ons on top and switch the active host to Lumen.

## Why it is opt-in

- It changes the Steam client, and an older client misses newer Steam fixes.
- It installs and depends on third-party projects with their own licences and
  update paths.
- Luna's own host is enough for the add-ons; the pipeline exists for existing
  Lumen installs and for setups that still need the upstream base.

## Existing Lumen installs

A plain `luna install` on such a machine targets the active host. It refreshes
Luna's add-ons and leaves Lumen, LuaTools, SLSsteam, the Steam pin and the host
selection untouched. `~/.local/share/Luna/host.json` keeps `"active": "lumen"`.

Nothing moves to luna-host until an explicit `luna host cutover`. A plain
`luna install` never downgrades a Lumen install.

## Switching hosts

Steam must be closed.

```bash
luna host status      # JSON: active host, unit states, binary version, add-on dir
luna host cutover     # disable lumen.service, enable luna-host.service
luna host rollback    # reverse it; ~/.local/share/Lumen is not deleted
```

`cutover` copies Luna's small state files to `~/.local/share/Luna/state` and
links the large Steam-client backups instead of moving them.

## Updating the Lumen base

These commands matter only while Lumen is the active host.

```bash
luna lumen status                         # versions, channels, base state
luna lumen update --dry-run               # plan only
luna lumen update                         # back up, run the base installer, re-attach Luna
luna lumen update --queue                 # applied by the timer once Steam is closed
luna lumen update --terminal              # close Steam and run it in a terminal, logged
luna lumen cancel                         # cancel a queued update
luna lumen restore                        # undo the last update from its backup
luna lumen channels                       # show per-component channels
luna lumen channels set stable            # or: set beta
```

`luna update-all` updates Luna's add-ons and re-verifies the Lumen base; it never
moves the Steam client build.

## Requirements and caveats

The opt-in path needs network access to the pinned upstream installer and Steam
client packages, `git`, `curl`, `jq`, `tar`, `unzip`, and the usual `cc` and
`make` for `luna-host`. The installer refuses while Steam or a Lumen sidecar is
running. Third-party installers can remove Steam's update block; `luna steam
heal` restores it. See [`ARCHITECTURE.md`](ARCHITECTURE.md) for the component
boundaries and `THIRD_PARTY_NOTICES.md` for licences.
