import importlib.util
import json
import tempfile
import unittest
import zipfile
from dataclasses import replace
from pathlib import Path

from morphe_builder.acquisition import AcquisitionError, BaseCache
from morphe_builder.apkmirror import ApkMirrorError, ApkMirrorRateLimited, ApkMirrorResolver
from morphe_builder.config import load_config
from morphe_builder.manifest import sha256_file
from morphe_builder.models import ApkInfo, DownloadResult
from morphe_builder.stock_sources import StockCandidate

CERT = "a" * 64


def write_apk(path, version, signer=CERT, package="com.google.android.youtube"):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("metadata.json", json.dumps({"version": version, "signer": signer, "package": package}))


class FakeAndroid:
    def validate_apk(self, path, *, expected_packages, expected_version=None, require_arm64=True):
        with zipfile.ZipFile(path) as archive:
            data = json.loads(archive.read("metadata.json"))
        if data["package"] not in expected_packages:
            raise RuntimeError("Package mismatch")
        if expected_version and data["version"] != expected_version:
            raise RuntimeError("Version mismatch")
        return ApkInfo(path, data["package"], data["version"], "100", native_code=("arm64-v8a",))

    def verify_signature(self, path):
        with zipfile.ZipFile(path) as archive:
            return json.loads(archive.read("metadata.json"))["signer"]


class FakeHttp:
    def __init__(self, payloads):
        self.payloads = payloads

    def download(self, url, destination, **kwargs):
        value = self.payloads[url]
        if isinstance(value, Exception):
            raise value
        write_apk(destination, **value)
        return DownloadResult(destination, destination.stat().st_size, sha256_file(destination), url)


class FakeSources:
    def __init__(self, candidates):
        self.candidates = candidates
        self.calls = []
        self.discovery = {}

    def discover_versions(self, provider, app):
        if provider in self.discovery:
            return self.discovery[provider]
        return [candidate.version for candidate in self.iter_candidates(provider, app, None)]

    def iter_candidates(self, provider, app, version):
        self.calls.append((provider, version))
        result = self.candidates.get((provider, version), [])
        if isinstance(result, Exception):
            raise result
        yield from result


class FakeMirror:
    def __init__(self, error=None, latest=()):
        self.error = error
        self.latest = latest
        self.calls = 0

    def iter_candidates(self, app, version):
        self.calls += 1
        if self.error:
            raise self.error
        return iter(())

    def discover_versions(self, app):
        self.calls += 1
        if self.error:
            raise self.error
        return [version for _, _, version in self.latest]


class BaseAcquisitionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = replace(load_config().apps["youtube"], signing_cert_sha256=(CERT,))
        self.cache = BaseCache(self.root / "cache")

    def acquirer(self, candidates, payloads, mirror=None):
        self.assertIsNotNone(
            importlib.util.find_spec("morphe_builder.base_acquisition"), "Fallback coordinator missing"
        )
        from morphe_builder.base_acquisition import BaseAcquirer

        self.sources = FakeSources(candidates)
        return BaseAcquirer(FakeHttp(payloads), mirror or FakeMirror(), FakeAndroid(), self.cache, self.sources)

    def candidate(self, provider, version, suffix=""):
        url = f"https://example.org/{provider}-{version}{suffix}.apk"
        return StockCandidate(provider, url, "https://example.org/app", version)

    def acquire(self, acquirer, versions, manual=None):
        return acquirer.acquire(self.app, versions, manual, self.root / "downloads")

    def test_newest_version_backup_wins_over_older_cached_apk(self):
        old = self.root / "old.apk"
        write_apk(old, "20.0")
        self.cache.store(self.app, old, version_name="20.0", version_code="100", source_page=None)
        fresh = self.candidate("github_mirror", "21.0")
        acquirer = self.acquirer({("github_mirror", "21.0"): [fresh]}, {fresh.url: {"version": "21.0"}})
        path, info, _, _, meta = self.acquire(acquirer, ["21.0", "20.0"])
        self.assertEqual(info.version_name, "21.0")
        self.assertEqual(meta["provider"], "github_mirror")
        self.assertEqual(meta["input_signing_certificate_sha256"], CERT)
        self.assertEqual(meta["input_sha256"], sha256_file(path))
        self.assertEqual(meta["final_url"], fresh.url)

    def test_403_on_apkmirror_falls_through_to_marketplace_same_version(self):
        candidate = self.candidate("uptodown", "21.0")
        acquirer = self.acquirer(
            {("uptodown", "21.0"): [candidate]},
            {candidate.url: {"version": "21.0"}},
            FakeMirror(ApkMirrorError("HTTP 403")),
        )
        self.assertEqual(self.acquire(acquirer, ["21.0"])[4]["provider"], "uptodown")

    def test_429_on_apkmirror_does_not_abort_other_sources(self):
        candidate = self.candidate("apkcombo", "21.0")
        acquirer = self.acquirer(
            {("apkcombo", "21.0"): [candidate]},
            {candidate.url: {"version": "21.0"}},
            FakeMirror(ApkMirrorRateLimited("60")),
        )
        self.assertEqual(self.acquire(acquirer, ["21.0"])[1].version_name, "21.0")

    def test_wrong_publisher_is_rejected_before_trying_next_source(self):
        bad = self.candidate("github_mirror", "21.0")
        good = self.candidate("archive", "21.0")
        acquirer = self.acquirer(
            {("github_mirror", "21.0"): [bad], ("archive", "21.0"): [good]},
            {bad.url: {"version": "21.0", "signer": "b" * 64}, good.url: {"version": "21.0"}},
        )
        self.assertEqual(self.acquire(acquirer, ["21.0"])[4]["provider"], "archive")
        self.assertEqual(len(list(self.cache.root.glob("*.json"))), 1)

    def test_wrong_version_payload_does_not_poison_cache(self):
        bad = self.candidate("github_mirror", "21.0")
        good = self.candidate("archive", "21.0")
        acquirer = self.acquirer(
            {("github_mirror", "21.0"): [bad], ("archive", "21.0"): [good]},
            {bad.url: {"version": "20.0"}, good.url: {"version": "21.0"}},
        )
        self.assertEqual(self.acquire(acquirer, ["21.0"])[1].version_name, "21.0")
        self.assertIsNone(self.cache.find(self.app, "20.0"))

    def test_blocked_apkmirror_is_not_hammered_across_versions(self):
        mirror = FakeMirror(ApkMirrorError("HTTP 403"))
        acquirer = self.acquirer({}, {}, mirror)
        with self.assertRaises(AcquisitionError):
            self.acquire(acquirer, ["21.0", "20.0"])
        self.assertEqual(mirror.calls, 1)

    def test_latest_discovery_ignores_mirror_inventory_and_uses_exact_backups(self):
        discovery = self.candidate("uptodown", "21.0")
        cached = self.candidate("github_mirror", "21.0")
        acquirer = self.acquirer(
            {("uptodown", None): [discovery], ("github_mirror", "21.0"): [cached]},
            {cached.url: {"version": "21.0"}},
            FakeMirror(ApkMirrorError("HTTP 403")),
        )
        self.assertEqual(self.acquire(acquirer, [None])[1].version_name, "21.0")
        self.assertNotIn(("github_mirror", None), self.sources.calls)

    def test_public_version_metadata_can_use_mirror_when_marketplace_download_is_gated(self):
        cached = self.candidate("github_mirror", "21.0")
        acquirer = self.acquirer(
            {("github_mirror", "21.0"): [cached]},
            {cached.url: {"version": "21.0"}},
            FakeMirror(ApkMirrorError("HTTP 403")),
        )
        self.sources.discovery["uptodown"] = ["21.0"]
        self.assertEqual(self.acquire(acquirer, [None])[4]["provider"], "github_mirror")

    def test_apkmirror_listing_newest_without_binary_is_acquired_from_backup(self):
        mirror = ApkMirrorResolver.__new__(ApkMirrorResolver)
        mirror._html = lambda url, referer=None: (
            '<a href="/apk/google-inc/youtube/youtube-22-0-release/">22.0</a>'
            '<a href="/apk/google-inc/youtube/youtube-21-0-release/">21.0</a>',
            url,
        )
        fresh = self.candidate("github_mirror", "22.0")
        acquirer = self.acquirer({("github_mirror", "22.0"): [fresh]}, {fresh.url: {"version": "22.0"}}, mirror)
        result = self.acquire(acquirer, [None])
        self.assertEqual(result[1].version_name, "22.0")
        self.assertEqual(result[4]["provider"], "github_mirror")
        self.assertEqual(result[4]["requested_version"], "Any")

    def test_latest_discovery_does_not_select_beta(self):
        beta = self.candidate("uptodown", "22.0-beta")
        stable = self.candidate("uptodown", "21.0")
        acquirer = self.acquirer(
            {("uptodown", None): [beta, stable], ("uptodown", "21.0"): [stable]}, {stable.url: {"version": "21.0"}}
        )
        self.assertEqual(self.acquire(acquirer, [None])[1].version_name, "21.0")

    def test_manual_url_has_priority_and_is_authenticated(self):
        acquirer = self.acquirer({}, {"https://example.org/manual.apk": {"version": "21.0"}})
        self.assertEqual(
            self.acquire(acquirer, ["21.0"], "https://example.org/manual.apk")[4]["provider"], "manual-url"
        )
        self.assertEqual(self.sources.calls, [])

    def test_cache_is_reauthenticated_instead_of_trusting_old_manifest(self):
        bad = self.root / "bad.apk"
        write_apk(bad, "21.0", signer="b" * 64)
        self.cache.store(self.app, bad, version_name="21.0", version_code="100", source_page=None)
        candidate = self.candidate("archive", "21.0")
        acquirer = self.acquirer({("archive", "21.0"): [candidate]}, {candidate.url: {"version": "21.0"}})
        self.assertEqual(self.acquire(acquirer, ["21.0"])[4]["provider"], "archive")

    def test_cache_preserves_provider_and_reauthenticates_on_reuse(self):
        fresh = self.candidate("github_mirror", "21.0")
        acquirer = self.acquirer({("github_mirror", "21.0"): [fresh]}, {fresh.url: {"version": "21.0"}})
        self.acquire(acquirer, ["21.0"])
        reused = self.acquire(acquirer, ["21.0"])
        self.assertEqual(reused[4]["acquisition"], "actions-cache")
        self.assertEqual(reused[4]["provider"], "github_mirror")
        self.assertEqual(reused[4]["final_url"], fresh.url)
        self.assertEqual(reused[4]["input_signing_certificate_sha256"], CERT)

    def test_latest_resolution_is_auditable_in_cache_and_build_metadata(self):
        discovery = self.candidate("uptodown", "21.0")
        cached = self.candidate("github_mirror", "21.0")
        acquirer = self.acquirer(
            {("uptodown", None): [discovery], ("github_mirror", "21.0"): [cached]}, {cached.url: {"version": "21.0"}}
        )
        result = self.acquire(acquirer, [None])
        self.assertEqual(result[4]["requested_version"], "Any")
        self.assertEqual(result[4]["resolution_mode"], "latest")
        self.assertEqual(self.cache.find(self.app, "21.0").requested_version, "Any")

    def test_failed_latest_discovery_does_not_mislabel_stale_cache_as_latest(self):
        old = self.root / "old.apk"
        write_apk(old, "20.0")
        self.cache.store(self.app, old, version_name="20.0", version_code="100", source_page=None)
        acquirer = self.acquirer({}, {}, FakeMirror(ApkMirrorError("HTTP 403")))
        with self.assertRaises(AcquisitionError):
            self.acquire(acquirer, [None])

    def test_all_failures_include_provider_context_and_manual_remedy(self):
        acquirer = self.acquirer(
            {("github_mirror", "21.0"): RuntimeError("mirror missing")}, {}, FakeMirror(ApkMirrorError("HTTP 403"))
        )
        with self.assertRaises(AcquisitionError) as caught:
            self.acquire(acquirer, ["21.0"])
        self.assertIn("github_mirror", str(caught.exception))
        self.assertIn("403", str(caught.exception))
        self.assertIn("base_urls", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
