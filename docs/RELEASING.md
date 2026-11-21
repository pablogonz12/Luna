# Releasing SLS Luna

The one-liner installs the newest **published release** tag, not `main`, so a
release is only useful if its tree is the one CI tested.

1. Land the work on `main` and wait for `Test SLS Luna` to be green.
2. Bump the version in the payload itself: `version` in
   `addons/luna/plugin/plugin.json` and `local VERSION` in
   `addons/luna/plugin/backend/main.lua` (the release reads the bundle version
   from those, not from the manifest), plus the CHANGELOG entry. Commit that,
   then re-pin with `scripts/vendor-update.sh` (it refuses uncommitted
   first-party bytes) and commit the manifest.
3. Prove the tree locally (or trust the green run): `bash scripts/test-all.sh`.
4. Commit, push to `main`, wait for green again.
5. Tag that commit and push the tag:

       git tag v0.14.19 && git push origin v0.14.19

## What CI does with the tag

`.github/workflows/release.yml` runs on `push` of a `v*` tag:

- runs the full `scripts/test-all.sh` suite on the tagged tree;
- checks the tag equals `v<luna plugin_version>` from the manifest;
- runs `python3 scripts/luna.py release --out dist --publish`, which builds
  `dist/luna-<version>.tar.gz`, writes `dist/luna-<version>.tar.gz.sha256`, and
  creates the GitHub release with both attached.

If the suite fails, no release is created.

## What users get

`install.sh` asks `api.github.com/.../releases/latest` for the tag, downloads
that tag's tree and installs it. `--main` (or `LUNA_REF=main`) takes the branch
tip; `LUNA_REF=<tag|sha>` pins one. If GitHub has no release or the API is
unreachable, it prints one line and installs `main`.

Releases cut before the standalone host (v0.14.17 and older) have no
`bootstrap`; the installer says so and uses `main` instead.
