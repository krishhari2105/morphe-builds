# Troubleshooting

## APKMirror blocked the workflow

Symptoms include HTTP 403/429, `Just a moment`, CAPTCHA/Cloudflare text, HTML instead of an APK, or no recognized variant/download link.

Open the app/version on APKMirror, proceed through its normal download UI, copy the final direct link, and rerun with `base_urls` JSON. Do not add CAPTCHA-bypass code or aggressive retry loops.

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
