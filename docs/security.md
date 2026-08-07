# Security

## Signing key

The existing `morphe.keystore` was committed to a public repository with known credentials. It must be considered exposed. It is retained only because changing certificates would prevent existing family installations from updating without uninstalling.

Migration procedure:

1. Base64-encode the exact existing keystore and store it as `SIGNING_KEYSTORE_B64`.
2. Configure alias/password secrets and `SIGNING_CERT_SHA256`.
3. Run a no-publish build.
4. Compare its certificate fingerprint with an existing installed/released APK.
5. Remove the tracked file only after the match is confirmed.

Do not upload decoded keystores as Actions artifacts or include secrets in logs. Rewriting Git history cannot revoke copies of the exposed key.

Downloaded Morphe JAR/MPP code runs only in a read-only, unsigned preparation job. Checkout credentials are not persisted, the tracked legacy key is removed first, and token/password/keystore variables are stripped from the Java child environment. Signing happens on a separate runner with pinned Android `zipalign`/`apksigner`; that runner never executes Morphe code.

## Downloads

The HTTP client uses HTTPS, timeouts, bounded retries, redirect limits, cross-origin authorization removal, temporary files, size checks, content-length checks, challenge/HTML detection, and SHA-256 verification for GitHub assets when GitHub provides a digest.

Manual URLs are untrusted input and receive the same package/version/format validation as automatic downloads.

## Bundles

Bundle inspection rejects path traversal, absolute paths, symlinks, duplicate normalized paths, excessive entry count/size, suspicious compression ratios, mixed packages, mixed versions, missing base APKs, and sources without arm64 support. Inspection extracts only nested APK entries into a temporary directory and never rewrites the source bundle.

## Supply chain

GitHub Actions are pinned to full commit SHAs. Python dependencies are exact-version pinned. Patch/CLI release asset identities and calculated digests are recorded in `build-manifest.json`.

The normal workflow has no credential or runtime dependency on the old private `base-apks` repository.
