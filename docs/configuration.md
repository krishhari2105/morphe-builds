# Configuration

All app and patch-source metadata is centralized under `config/`.

## `apps.json`

Each app defines its display name, Android package, canonical APKMirror listing, accepted source formats, preferred variants, and target ABI. Package IDs must be unique. The current pipeline intentionally supports only `arm64-v8a` as a target policy.

### Stock download sources and publisher trust

`download_sources` maps provider names to arrays of HTTPS URLs. Providers are attempted in this order for **each exact version**: `github_mirror`, `direct`, `github`, `archive`, APKMirror (using `apkmirror_url`), `uptodown`, `apkpure`, `apkcombo`. Unconfigured providers are skipped. Supported URL template fields are `{package}`, `{version}`, and `{arch}`.

- `github_mirror`: repository URL whose release tags are package IDs and assets use `<package>-<version>[-<versionCode>]-<arch>.<format>`. `nullcpy/apks` is configured as a third-party stock mirror, not a publisher or a latest-version authority.
- `direct`: a binary HTTPS URL, optionally templated by version. Downloaded metadata must still match the request.
- `github`: an official upstream repository URL; only non-draft, non-prerelease releases from its current release listing are considered. Proton VPN uses its official Android repository. Older versions absent from the listing may require another source or a manual URL.
- `archive`: `https://archive.org/download/<item>/<package-directory>`; a memoized metadata index selects the exact filename, rather than guessing a URL.
- `uptodown`: canonical `/android` app page. `apkpure` and `apkcombo`: canonical app page ending in the Android package ID. Only publicly exposed download links are used; challenge-gated sources can remain unavailable.

`signing_cert_sha256` is a required, nonempty array of trusted publisher certificate SHA-256 fingerprints. Every input APK (including every split) must pass full `apksigner verify` and have the same trusted signer. For SDK-targeted v3/v3.1 key rotation, the canonical pinned fingerprint is the oldest-platform signer in the single cryptographically verified signing lineage; rotated keys are accepted only after apksigner verifies the rotation proof. Source-stamp certificates and public-key digests are never treated as publisher certificates. Manual inputs and old cache entries are checked too. `signing_cert_source` records the independent publisher evidence used to review the pins. The checked-in pins were retrieved from each publisher's official `/.well-known/assetlinks.json` on 2026-11-12; these records are **not** fetched or trusted dynamically during a build. Review publisher key rotations against official evidence before updating the pins; never learn a new pin automatically from a mirror.

GitHub API requests use `GITHUB_TOKEN` when present; it is not sent to third-party source pages or binary URLs. Public metadata and binary availability checks succeeded for both configured GitHub/Archive alternatives for YouTube 21.30.216, Music 9.30.53, and Reddit 2026.40.0. This does not authenticate their contents; input certificate validation remains mandatory at build time.

When adding an app:

1. Add its package and canonical APKMirror page.
2. Add a sanitized CLI-output fixture/test if its version format is unusual.
3. Run `python -m morphe_builder validate-config`.
4. Run a no-publish build before enabling it for family use.

## `patch-sources.json`

A source defines:

- Patch repository and Morphe desktop/CLI repository.
- Anchored regexes for exactly one `.mpp` and one `-all.jar` release asset.
- `stable`, `prerelease`, or `latest` channel semantics.
- Whether the two-hour watcher includes it.
- Supported app keys (`["*"]` lets CLI compatibility decide).

`morphe` and `morphe-dev` are scheduled. Other sources remain manual to avoid unexpected high-frequency builds.

### Piko and X-Shim

Current Piko releases use `patch_mode: single-mpp`: the normal Piko `.mpp` bundle is passed to one Morphe patch command. Piko 3.8.0 supports X `12.7.1-release.0`, and X-Shim is not used for X 12.5.0 and newer. Compatibility remains dynamic because the builder asks the resolved MPP for `list-versions`; the documented version is informational. The historical X-Shim was a separate Morphe source used with older X versions through a combined Manager session; this repository does not invent unsupported multi-bundle CLI flags or run shim and Piko as two sequential patch commands.

## `patches.json`

Patch settings merge in this order:

1. Global defaults.
2. Per-app settings.
3. Per-source defaults.
4. Per-source/per-app settings.

An explicit enable removes the same patch from the disable set, and vice versa.

## `tools.json`

Pins the Android build-tools version used for `aapt`, `apksigner`, and `zipalign`. Bundle merging and native-library stripping are handled by Morphe CLI itself; no separate APKEditor download is used.
