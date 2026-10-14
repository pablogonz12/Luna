# Contributing to SLS Luna

SLS Luna is an independent add-on and UI project for the slsteam-moon/Lumen stack.
It uses other projects as dependencies; contributions must preserve provenance
and avoid presenting upstream work as Luna-authored.

## Current repository and planned direction

This repository currently installs and verifies Lumen add-ons. The planned custom
Luna menu integration is described in
[`docs/PROJECT_DIRECTION.md`](docs/PROJECT_DIRECTION.md). SLS Luna is not a fork
of LuaTools-Moon, slsteam-moon, or Lumen.

## Making changes

1. Open an issue or a short proposal for significant changes, especially changes
   to installation, Steam/Lumen integration, user-data formats, or dependencies.
2. Make changes on a focused branch and submit a pull request rather than pushing
   directly to the default branch.
3. Include tests and update the relevant documentation. Keep installation and
   removal reversible, fail closed on unexpected Steam/Lumen state, and preserve
   user data.
4. If adapting upstream code, first verify that its license permits the intended
   use; retain copyright and license notices and note the source revision. Label
   Luna-specific changes clearly. Prefer calling supported APIs over copying code
   when licensing or maintenance boundaries are unclear.
5. Do not include Steam credentials, account identifiers, user logs, game files,
   manifests, depot keys, or other private/proprietary data in commits or test
   fixtures.

## Upstream updates

Upstream updates should be pinned to an exact tag or commit and reviewed as a
normal change. Run the relevant test suites and record the updated source revision
and any local patches. Send generally useful fixes to the appropriate upstream
project as a separate PR when practical; product-specific features can remain
downstream.

## Tests

See [`docs/TEST_PLAN.md`](docs/TEST_PLAN.md). At minimum, run the tests relevant to
the files changed and explain any device-only checks that were not performed.
