# Morphe Builds

One repository for discovering compatible app versions, acquiring source APK/APKM files, patching with Morphe-compatible patch bundles, producing arm64-family builds, and publishing reproducible GitHub Releases.

## What is automated

- Manual **Build patched apps** workflow with app/source/version controls.
- Morphe stable and development release checks every two hours.
- Exact Morphe patch and CLI release/asset resolution with SHA-256 verification.
- Compatible-version discovery through `morphe-desktop list-versions`.
- Best-effort APKMirror release and arm64/universal variant discovery.
- Actions cache reuse for validated base files. Raw bases are never public release assets.
- Direct APKM/APKS/XAPK input to Morphe CLI. Source files are not rewritten.
- `--striplibs=arm64-v8a` only when the source contains arm64 plus other native architectures; arm64-only and architecture-independent sources are left alone.
- APK package/version/ABI, alignment, and signing-certificate validation.
- Deterministic releases containing patched APKs, `build-manifest.json`, and `SHA256SUMS`.

APKMirror has no supported download API and can block GitHub-hosted runners. When that happens, the workflow prints the exact `base_urls` JSON needed for a manual rerun. The automation detects challenges; it does not bypass CAPTCHA or anti-bot protections.

## One-click build

1. Open **Actions → Build patched apps → Run workflow**.
2. Choose the patch source from the `patch_sources` dropdown.
3. Choose `all` or one app from the `apps` dropdown.
4. Normally leave `version_overrides` and `base_urls` as `{}`.
5. Run the workflow.

Supported app keys are `youtube`, `yt-music`, `reddit`, `twitter`, `spotify`, `gphotos`, and `proton-vpn`. Supported source keys are defined in [`config/patch-sources.json`](config/patch-sources.json).

### Manual APKMirror fallback

If Actions reports that APKMirror blocked or changed its page, copy the final APKMirror download link and rerun with:

```json
{
  "youtube": "https://www.apkmirror.com/wp-content/themes/APKMirror/download.php?...",
  "yt-music": "https://www.apkmirror.com/wp-content/themes/APKMirror/download.php?..."
}
```

Every manual URL is still checked for HTTPS, file integrity, package, version, and arm64 compatibility.

## Required GitHub Actions secrets

| Secret | Purpose |
|---|---|
| `SIGNING_KEYSTORE_B64` | Base64 of the existing Morphe keystore, retained for update compatibility. |
| `SIGNING_KEYSTORE_PASSWORD` | Optional; keystore password (empty by default). |
| `SIGNING_KEY_ALIAS` | Optional; defaults to `Morphe`. |
| `SIGNING_KEYSTORE_TYPE` | Optional; defaults to `BKS` for the existing key. |
| `SIGNING_KEY_PASSWORD` | Optional; defaults to `Morphe`. |
| `SIGNING_CERT_SHA256` | Optional but recommended expected certificate SHA-256 fingerprint. |

The tracked `morphe.keystore` remains only during migration. Configure and test the secrets before removing it. The key is already present in public Git history; moving it to a secret prevents continued accidental distribution but cannot make the old key private again.

See [workflow usage](docs/workflows.md), [configuration](docs/configuration.md), [security](docs/security.md), and [troubleshooting](docs/troubleshooting.md).

## Local commands

```bash
python -m pip install -r requirements.txt
python -m morphe_builder validate-config
python -m morphe_builder matrix --sources morphe,morphe-dev
python -m morphe_builder list-versions --source morphe
python -m unittest discover -s tests -v
```

A local build also needs Java 21, Android build-tools 35.0.0, and signing configuration. Preparation and signing are deliberately separate so downloaded Morphe code never runs in the process/job that holds repository-write or signing secrets:

```bash
python -m morphe_builder prepare --source morphe --apps youtube
python -m morphe_builder finalize --source morphe --no-publish
```
