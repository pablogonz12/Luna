# Third-party notices and provenance

This file records upstream projects relevant to SLS Luna. It is not a substitute
for the license files distributed with each component. Preserve upstream notices
when code or binaries are copied or redistributed, and review each component's
terms for the specific distribution.

| Project | Source | Role / provenance | License status checked for this project |
| --- | --- | --- | --- |
| LuaTools-Moon | [swwayps/luatools-moon](https://github.com/swwayps/luatools-moon) | Current stack prerequisite and base installer source. `install.sh` downloads the upstream installer at a pinned commit, verifies its SHA256, and invokes it; SLS Luna does not copy that installer code | Repository displays AGPL-3.0; its own notices apply to its plugin and installer |
| slsteam-moon | [swwayps/slsteam-moon](https://github.com/swwayps/slsteam-moon) | Native Steam integration targeted by the stack; described upstream as a fork of SLSsteam | Repository displays AGPL-3.0; no core code copied into this repository |
| SLSsteam | [AceSLS/SLSsteam](https://github.com/AceSLS/SLSsteam) | Upstream project from which slsteam-moon is forked | Repository displays AGPL-3.0; retain original notices through the fork lineage |
| Lumen | [swwayps/lumen](https://github.com/swwayps/lumen) | Runtime/bridge used to host plugin and add-on code | No license file was visible when this notice was written; clarify before redistribution |
| CloudRedirect | [Selectively11/CloudRedirect](https://github.com/Selectively11/CloudRedirect) | Optional cloud-save component integrated by LuaTools-Moon | Review its current license before bundling or redistribution |
| Decky Loader | [SteamDeckHomebrew/decky-loader](https://github.com/SteamDeckHomebrew/decky-loader) | Potential optional Steam Deck integration; not installed by Luna today | Upstream repository displays GPL-2.0; no Decky code copied or bundled |
| btrfs-game-compressor | [pgm1207/btrfs-game-compressor](https://github.com/pgm1207/btrfs-game-compressor) | Separate external tool invoked by the compressor add-on; not installed by this repository | Review the tool repository's current license before redistribution |
| SteamGridDB | [steamgriddb.com](https://www.steamgriddb.com/) | Artwork source for the Luna picker; the user's own API key is stored locally and images are fetched directly from SteamGridDB | Artwork belongs to its uploaders; the API is used under SteamGridDB's terms with the user's key, and nothing is redistributed |
| SLS Luna add-on source repositories | [playtime](https://github.com/pgm1207/slsteam-playtime), [presence](https://github.com/pgm1207/slsteam-presence), and this repository | Source commits and file hashes for currently pinned payloads are recorded in [`vendor/MANIFEST.json`](vendor/MANIFEST.json) | Preserve the notices and licenses present in each source repository |

## Attribution policy

SLS Luna is an independent project, not an official release of or endorsed by
LuaTools-Moon, slsteam-moon, SLSsteam, Lumen, CloudRedirect, or Valve. Where code
from an upstream project is adapted, retain its notices and identify the upstream
revision and Luna's modifications. Using an upstream API does not transfer
authorship of that project's implementation to SLS Luna.
