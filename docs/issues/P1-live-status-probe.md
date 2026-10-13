# P1: report live add-on health, not just on-disk state

`status` answers "are the right files installed". It cannot answer "is the running
client actually using them": a Lumen sidecar that survived a restart, an add-on
that failed to register, or a bridge that loaded one add-on and skipped the other
all look identical on disk.

The add-on projects already have the pieces: a read-only CEF/DevTools client, the
`SlsPlaytimeGet` payload and the `SlsPresenceContractProbe` RPC.

## Acceptance criteria

- `status --live` reports, per add-on, whether its backend answers and what it
  says about its own health, plus whether `boot.lua`'s marker is reflected in the
  running process.
- Detect and report a sidecar that predates the installed files (compare the
  sidecar's start time with the payload mtimes).
- Stay read-only: no RPC that changes state, no writes.
- Degrade cleanly when Steam is closed or the debug port is unavailable, with a
  clear message rather than a stack trace.
