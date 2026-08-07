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

## `patches.json`

Patch settings merge in this order:

1. Global defaults.
2. Per-app settings.
3. Per-source defaults.
4. Per-source/per-app settings.

An explicit enable removes the same patch from the disable set, and vice versa.

## `tools.json`

Pins the Android build-tools version used for `aapt`, `apksigner`, and `zipalign`. Bundle merging and native-library stripping are handled by Morphe CLI itself; no separate APKEditor download is used.
