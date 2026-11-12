# Workflows

## Build patched apps

`build.yml` is the reusable implementation workflow. Manual runs use source-specific wrappers so GitHub can display valid app choices for each source:

- `build-morphe.yml`: YouTube, YouTube Music, Reddit.
- `build-morphe-dev.yml`: YouTube, YouTube Music, Reddit.
- `build-piko.yml`: X/Twitter only.
- `build-piko-dev.yml`: X/Twitter only.
- `build-de-revanced.yml`: Google Photos only.
- `build-hoo-dles.yml`: Proton VPN only.

Each wrapper offers `all` plus its permitted app choices, then calls `build.yml` with a fixed source. This avoids invalid source/app combinations that cannot be represented by dependent GitHub Actions dropdowns.

The reusable inputs are:

- `patch_sources`: fixed by manual wrappers; used by scheduled/repository-dispatch callers.
- `apps`: `all` or a source-permitted app key.
- `version_overrides`: JSON object mapping app keys to exact versions.
- `base_url`: one direct HTTPS download for the selected app; the app key is inferred automatically.
- `base_urls`: advanced JSON object mapping multiple app keys to direct HTTPS downloads.
- `publish`: create/reuse a deterministic GitHub Release when true.

Each source runs independently. Within one source, publishing is all-or-nothing: a failed requested app prevents a partial release. Diagnostic manifests and any successful local outputs are retained as a 14-day workflow artifact.

The workflow uses separate runners. The read-only preparation job downloads and executes Morphe with `--unsigned`, receives no signing secrets, does not persist checkout credentials, and removes the tracked legacy key before Java runs. A second runner downloads the prepared APKs, aligns/signs them with Android build-tools, validates them, and publishes with a write token. No downloaded Morphe code executes in the signing/publishing job.

## Watch Morphe releases

`upstream-watch.yml` runs daily at 08:00 IST (02:30 UTC). It resolves the newest stable/prerelease patch release and current Morphe CLI release. If no release tag starts with that exact source/patch/CLI identity, it calls the reusable build workflow.

GitHub scheduled workflows can be delayed. Upstream repositories cannot directly trigger this repository unless they cooperate or an external dispatcher is configured, so polling remains the reliable mechanism.

## Family download website

`deploy-pages.yml` generates a static catalog from published releases and deploys it to GitHub Pages. It runs after a release is published, on pushes to `main`, or manually. The generator reads each complete `build-manifest.json`, selects the newest build for each visible app in `config/catalog.json`, and links to the existing GitHub Release assets.

YouTube and YouTube Music are featured by default. To show another app, set its `visible` value to `true` in `config/catalog.json` and push to `main`. The repository must have **Settings → Pages → Source: GitHub Actions** enabled once before the first deployment.

## Base acquisition order

The resolved patch MPP is queried with `list-versions`; those concrete compatible versions are normalized and attempted newest-first. An explicit `version_overrides` value is treated as one exact request and is never silently replaced by another version.

An explicit manual URL takes precedence and must pass the same input authentication and compatibility checks. Otherwise, **for each version newest-first**, the builder attempts: exact validated Actions cache → configured GitHub stock mirror → direct URL → official GitHub releases → Archive.org → APKMirror → Uptodown → APKPure → APKCombo. A missing/blocked/invalid provider falls through without changing the requested version. All providers for the newer version are exhausted before considering an older supported stable version.

When the patch source reports `Any`, the builder means “newest discoverable stable release,” not an arbitrary mirrored APK:

1. Discover stable versions from the first usable upstream authority: configured official GitHub releases, APKMirror, Uptodown, APKPure, then APKCombo. Public version metadata can be used even when that store's binary download is gated.
2. Sort that authority's bounded discovered versions newest-first, then use the same exact-version cache/provider chain above. Mirror and Archive.org inventories do not establish what upstream's latest is.
3. Validate package, resolved version, archive integrity, arm64 support, and publisher signatures (including all bundle splits) before caching or patching.
4. If all discovery authorities are blocked, fail with a manual-source remedy instead of silently treating a stale cache entry as latest.

Build metadata records the provider, source page, final download URL, requested/resolved versions, resolution mode, input SHA-256, and publisher certificate SHA-256. Cache metadata preserves provider, final URL, requested version, resolution mode, and verified signer alongside its version name/code and file hash. Cache hits are reauthenticated against current publisher pins.

The active workflow has no dependency on the old private `base-apks` repository.

After validation, the original source file is copied into a source-scoped Actions cache. Tools and bases use separate immutable cache generations, and only the newest two generations per source/type are retained. Failed preparation jobs do not save a new generation. Raw bases are not uploaded to a public release.

## Morphe source handling

APK, APKM, APKS, and XAPK files are passed to Morphe CLI unchanged. Morphe creates and removes its own temporary merged APK for bundle inputs. The builder only inspects source metadata before patching.

If source native architectures are exactly `arm64-v8a`, or there are no native libraries, no stripping option is used. If arm64 and other architectures coexist, the command receives `--striplibs=arm64-v8a`, which filters the rebuilt patched APK rather than editing the source.

## Deterministic releases

Tags include source, exact patch tag, exact CLI tag, and a digest of base/tool/patch/signing inputs. An identical rerun reuses the existing release. A conflicting existing release fails instead of silently replacing assets.
