# Workflows

## Build patched apps

`build.yml` supports manual dispatch, reusable workflow calls, and a validated `repository_dispatch` source hint.

Inputs:

- `patch_sources`: a dropdown containing each configured source, `scheduled`, or `all`.
- `apps`: a dropdown containing `all` and each supported app. Manual dispatch intentionally selects one app at a time; scheduled builds still use `all`.
- `version_overrides`: JSON object mapping app keys to exact versions.
- `base_urls`: JSON object mapping app keys to direct HTTPS downloads.
- `publish`: create/reuse a deterministic GitHub Release when true.

Each source runs independently. Within one source, publishing is all-or-nothing: a failed requested app prevents a partial release. Diagnostic manifests and any successful local outputs are retained as a 14-day workflow artifact.

The workflow uses separate runners. The read-only preparation job downloads and executes Morphe with `--unsigned`, receives no signing secrets, does not persist checkout credentials, and removes the tracked legacy key before Java runs. A second runner downloads the prepared APKs, aligns/signs them with Android build-tools, validates them, and publishes with a write token. No downloaded Morphe code executes in the signing/publishing job.

## Watch Morphe releases

`upstream-watch.yml` runs at minute 17 every two hours. It resolves the newest stable/prerelease patch release and current Morphe CLI release. If no release tag starts with that exact source/patch/CLI identity, it calls the reusable build workflow.

GitHub scheduled workflows can be delayed. Upstream repositories cannot directly trigger this repository unless they cooperate or an external dispatcher is configured, so polling remains the reliable mechanism.

## Base acquisition order

For each compatible version:

1. Validated Actions cache.
2. An explicit manual URL, when supplied.
3. APKMirror best-effort HTML discovery.

The active workflow has no dependency on the old private `base-apks` repository.

After validation, the original source file is copied into the Actions cache. It is not uploaded to a public release.

## Morphe source handling

APK, APKM, APKS, and XAPK files are passed to Morphe CLI unchanged. Morphe creates and removes its own temporary merged APK for bundle inputs. The builder only inspects source metadata before patching.

If source native architectures are exactly `arm64-v8a`, or there are no native libraries, no stripping option is used. If arm64 and other architectures coexist, the command receives `--striplibs=arm64-v8a`, which filters the rebuilt patched APK rather than editing the source.

## Deterministic releases

Tags include source, exact patch tag, exact CLI tag, and a digest of base/tool/patch/signing inputs. An identical rerun reuses the existing release. A conflicting existing release fails instead of silently replacing assets.
