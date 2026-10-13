# P1: replace the boot.lua patch with an upstream Lumen add-on hook

The installer edits four exact anchors in `lua/boot.lua`. That is the only way to
host add-ons today, and it breaks whenever Lumen reshapes those functions - the
patcher fails closed, but installation stops until the anchors are re-derived.

## Acceptance criteria

- Propose the capability described in `docs/UPSTREAM.md` to the Lumen project:
  an add-on directory loader with `register(registry)`, per-channel asset
  decoration and a conditional `callServerMethod` polyfill.
- Keep the current contract intact for add-ons: no payload change required.
- Support both paths for one release: detect the upstream hook and stop patching;
  otherwise patch as today.
- On uninstall, restore `boot.lua` from backup only if it is still patched, so a
  Lumen that already ships the hook is never rewritten.

## Not yet

Deliberately not started. The patch works and is verified on this machine; a
premature proposal would lock in a contract before two add-ons have exercised it.
