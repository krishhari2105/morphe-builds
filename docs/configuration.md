# Configuration

All app and patch-source metadata is centralized under `config/`.

## `apps.json`

Each app defines its display name, Android package, canonical APKMirror listing, accepted source formats, preferred variants, and target ABI. Package IDs must be unique. The current pipeline intentionally supports only `arm64-v8a` as a target policy.

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
