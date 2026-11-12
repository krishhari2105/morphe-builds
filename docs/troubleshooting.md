# Troubleshooting

## APKMirror blocked the workflow

Symptoms include HTTP 403/429, `Just a moment`, CAPTCHA/Cloudflare text, HTML instead of an APK, or no recognized variant/download link.

The builder now tries configured stock mirrors and Archive.org before APKMirror, then public Uptodown/APKPure/APKCombo links. A blocked or rate-limited provider does not stop the other providers. Observed 403/429/challenges suppress further attempts to that provider during the preparation; rate limits are not retried automatically. Other transient connection/server failures still have bounded transport retries.

A browser can download successfully while Actions receives 403 because network IP reputation and browser/session checks differ. A User-Agent header cannot guarantee access. Do not add CAPTCHA-bypass code, copied browser cookies, or aggressive retry loops.

If every source fails, use `base_url` for one selected app, or `base_urls` JSON for several. Supply a direct HTTPS URL to an original publisher-signed APK/bundle from a source you trust; it need not be APKMirror. A copied APKMirror link can still expire or be blocked on the runner. All manual inputs must pass publisher-certificate, package, version, archive, and ABI validation.

## Untrusted input publisher certificate

This is different from the signing certificate used for the patched output. The builder rejects an unsigned source, a source signed by an unknown publisher, or bundle splits with different signers. Review the configured `signing_cert_source` official publisher evidence and the source file before changing `signing_cert_sha256`. Never disable validation or copy a certificate fingerprint from the same untrusted mirror merely to make a build pass.

## Latest stable version cannot be discovered

For a patch reporting `Any`, the builder uses official GitHub releases or public store metadata to discover stable versions, then uses the full fallback chain for those exact versions. If all discovery sources are blocked, it fails rather than presenting an old cache entry or the newest mirror upload as upstream's latest. Use an explicit supported `version_overrides` value or an authenticated manual source URL.

## Resource table validation fails after patching

If Morphe reports a successful patch but `aapt` rejects the output with an invalid resource reference (for example, an `android:icon` error), do not bypass APK validation or publish the APK. `config/tools.json` enables `force_full_resource_encode`, which passes Morphe Patcher's documented full-resource-encoding JVM property and avoids the incremental resource-table encoder regression.

Keep this setting enabled until a newer Morphe Patcher release has been verified against the newest compatible app version. It preserves the newest app and patch bundle; it only trades some patching speed for a valid resource table.

## No compatible versions

Run:

```bash
python -m morphe_builder list-versions --source morphe
```

If the package is absent, that patch bundle does not support the app. If CLI output changed, update the parser and its fixture tests rather than guessing a version.

## Morphe development source is skipped

`morphe-dev` requires an actual GitHub prerelease. The watcher logs and skips it when no prerelease exists; it never substitutes the newest stable release.

## Source has no arm64 support

The builder rejects native sources whose ABI set does not contain `arm64-v8a`. Select another APKMirror variant. Architecture-independent APKs are accepted.

## Bundle patching fails

The builder does not pre-merge or alter APKM/APKS/XAPK files. Morphe CLI receives the original bundle and performs its own temporary merge. Check the uploaded patch-result JSON and Morphe logs. If source validation succeeded but merging failed, report the exact bundle format and Morphe CLI tag upstream.

## Signature or alignment verification fails

Confirm all signing secrets represent the same existing key and that `SIGNING_CERT_SHA256` contains only the expected certificate digest (colons are accepted). Do not publish an output that fails `apksigner verify` or `zipalign -c`.

## Existing deterministic release conflicts

A release with the same tag but missing/different assets indicates a previous interrupted or manually modified publish. Investigate it rather than overwriting. Delete or repair that release only after reviewing its manifest and assets.
