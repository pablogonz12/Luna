# Presence regression suite

The mirrored tests come from `slsteam-presence@4cdd63d`. Only plugin paths
change to target `addons/presence/plugin/`; the source is a committed revision.
`luna-extra.test.cjs` and `luna-extra-backend.lua` hold Luna-only coverage.

`bash scripts/test-all.sh` runs the Node suites and both isolated Lua backend
tests without a sibling checkout or live Steam. Passing mocks does not replace
desktop or Gaming Mode validation.
