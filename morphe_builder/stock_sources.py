"""Configured, version-preserving alternatives to APKMirror acquisition."""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import quote, urlsplit

from .http import HttpClient
from .models import AppConfig
from .versions import normalize_version, version_key

PROVIDER_ORDER = ("github_mirror", "direct", "github", "archive", "apkmirror", "uptodown", "apkpure", "apkcombo")


def is_stable_version(version: str) -> bool:
    return bool(re.match(r"^v?\d", version)) and not re.search(
        r"alpha|beta|preview|canary|nightly|(?:^|[._+\-])(?:rc|dev)\d*", version, re.IGNORECASE
    )


@dataclass(frozen=True)
class StockCandidate:
    provider: str
    url: str
    source_page: str
    version: str
    sha256: str | None = None


class StockSources:
    def __init__(self, http: HttpClient) -> None:
        self.http = http
        self._indexes: dict[str, Any] = {}
        self._failures: dict[str, Exception] = {}
        # One instance per preparation; blocked endpoints aren't hammered per version.
        self._marketplaces: Any = None

    def _json(self, url: str, *, github: bool = False) -> Any:
        if url in self._failures:
            raise RuntimeError(f"Source unavailable for this run: {self._failures[url]}")
        if url not in self._indexes:
            try:
                self._indexes[url] = self.http.get_json(url, github=github)
            except Exception as exc:
                self._failures[url] = exc
                raise
        return self._indexes[url]

    def iter_candidates(self, provider: str, app: AppConfig, version: str | None) -> Iterator[StockCandidate]:
        errors: list[str] = []
        yielded = False
        for template in app.download_sources.get(provider, ()):
            try:
                url = template.format(package=app.package, version=version or "", arch=app.target_abi)
                if provider == "github_mirror":
                    candidates = self._github_mirror(url, app, version)
                elif provider == "github":
                    candidates = self._github(url, app, version)
                elif provider == "archive":
                    candidates = self._archive(url, app, version)
                elif provider == "direct":
                    candidates = iter([StockCandidate(provider, url, url, version)]) if version else iter(())
                elif provider in {"uptodown", "apkpure", "apkcombo"}:
                    candidates = self._marketplace(provider, url, app, version)
                else:
                    raise RuntimeError(f"Unsupported stock provider: {provider}")
                for candidate in candidates:
                    if urlsplit(candidate.url).scheme != "https":
                        continue
                    if not is_stable_version(candidate.version):
                        continue
                    if version and normalize_version(candidate.version) != normalize_version(version):
                        continue
                    yielded = True
                    yield candidate
            except Exception as exc:
                errors.append(f"{provider}: {exc}")
        if errors and not yielded:
            raise RuntimeError("; ".join(errors))

    def discover_versions(self, provider: str, app: AppConfig) -> list[str]:
        if provider == "github":
            release_versions: set[str] = set()
            release_errors: list[str] = []
            for url in app.download_sources.get(provider, ()):
                try:
                    repo = self._repo(url)
                    releases = self._json(f"https://api.github.com/repos/{repo}/releases", github=True)
                    release_versions.update(
                        normalize_version(str(release.get("tag_name", "")))
                        for release in releases
                        if not release.get("draft")
                        and not release.get("prerelease")
                        and is_stable_version(str(release.get("tag_name", "")))
                    )
                except Exception as exc:
                    release_errors.append(str(exc))
            if release_errors and not release_versions:
                raise RuntimeError("; ".join(release_errors))
            return sorted(release_versions, key=version_key, reverse=True)
        if provider not in {"uptodown", "apkpure", "apkcombo"}:
            return []
        from .marketplaces import MarketplaceSources

        if self._marketplaces is None:
            self._marketplaces = MarketplaceSources(self.http)
        versions: set[str] = set()
        errors: list[str] = []
        for url in app.download_sources.get(provider, ()):
            try:
                versions.update(self._marketplaces.discover_versions(provider, url, app))
            except Exception as exc:
                errors.append(str(exc))
        if errors and not versions:
            raise RuntimeError("; ".join(errors))
        return sorted((version for version in versions if is_stable_version(version)), key=version_key, reverse=True)

    @staticmethod
    def _repo(url: str) -> str:
        parsed = urlsplit(url)
        parts = parsed.path.strip("/").split("/")
        if parsed.scheme != "https" or parsed.hostname != "github.com" or len(parts) < 2:
            raise RuntimeError("GitHub stock source must be an HTTPS github.com repository URL")
        owner, repo = parts[:2]
        if not re.fullmatch(r"[\w.-]+", owner) or not re.fullmatch(r"[\w.-]+", repo):
            raise RuntimeError("Invalid GitHub stock repository")
        return f"{owner}/{repo}"

    @staticmethod
    def _asset_version(name: str, app: AppConfig) -> tuple[str, str] | None:
        pattern = (
            rf"^{re.escape(app.package)}-(.+?)(?:-\d+)?-"
            r"(arm64-v8a|all|universal|common|arm-v7a|x86|x86_64)\.(apk|apkm|apks|xapk)$"
        )
        match = re.fullmatch(pattern, name, re.IGNORECASE)
        if not match or match[3].lower() not in app.formats:
            return None
        if match[2] not in {app.target_abi, "all", "universal", "common"}:
            return None
        return normalize_version(match[1]), match[2]

    @staticmethod
    def _digest(asset: dict[str, Any]) -> str | None:
        value = str(asset.get("digest") or "")
        if re.fullmatch(r"sha256:[0-9a-fA-F]{64}", value):
            return value.split(":", 1)[1].lower()
        return None

    def _github_mirror(self, url: str, app: AppConfig, version: str | None) -> Iterator[StockCandidate]:
        # A cache has specific versions; it is not an authority on upstream's latest release.
        if version is None:
            return
        repo = self._repo(url)
        endpoint = f"https://api.github.com/repos/{repo}/releases/tags/{quote(app.package, safe='')}"
        release = self._json(endpoint, github=True)
        found: list[tuple[int, StockCandidate]] = []
        for asset in release.get("assets", []):
            parsed = self._asset_version(str(asset.get("name", "")), app)
            if not parsed or parsed[0] != normalize_version(version):
                continue
            download_url = str(asset.get("browser_download_url", ""))
            if urlsplit(download_url).hostname != "github.com":
                continue
            candidate = StockCandidate("github_mirror", download_url, url, parsed[0], self._digest(asset))
            found.append((0 if parsed[1] == app.target_abi else 1, candidate))
        for _, candidate in sorted(found, key=lambda item: item[0]):
            yield candidate

    def _github(self, url: str, app: AppConfig, version: str | None) -> Iterator[StockCandidate]:
        repo = self._repo(url)
        endpoint = f"https://api.github.com/repos/{repo}/releases"
        # Official release tags, unlike cache tags, describe actual upstream versions.
        releases = self._json(endpoint, github=True)
        releases = sorted(
            (release for release in releases if not release.get("draft") and not release.get("prerelease")),
            key=lambda release: version_key(str(release.get("tag_name", ""))),
            reverse=True,
        )
        for release in releases:
            resolved = normalize_version(str(release.get("tag_name", "")))
            if not is_stable_version(resolved) or (version and resolved != normalize_version(version)):
                continue
            for asset in release.get("assets", []):
                name = str(asset.get("name", ""))
                if PurePosixPath(name).suffix.lstrip(".").lower() not in app.formats:
                    continue
                if re.search(r"(?:arm-v7a|armeabi|x86|android.?tv)", name, re.IGNORECASE):
                    continue
                download_url = str(asset.get("browser_download_url", ""))
                if urlsplit(download_url).hostname != "github.com":
                    continue
                yield StockCandidate(
                    "github", download_url, str(release.get("html_url") or url), resolved, self._digest(asset)
                )

    def _archive(self, url: str, app: AppConfig, version: str | None) -> Iterator[StockCandidate]:
        if version is None:
            return
        parsed = urlsplit(url)
        parts = parsed.path.strip("/").split("/")
        if parsed.hostname != "archive.org" or len(parts) < 2 or parts[0] != "download":
            raise RuntimeError("Archive source must be an archive.org/download/<item> URL")
        item = parts[1]
        prefix = "/".join(parts[2:]).rstrip("/") + "/"
        index = self._json(f"https://archive.org/metadata/{quote(item, safe='')}")
        found: list[tuple[int, StockCandidate]] = []
        for file in index.get("files", []):
            name = str(file.get("name", ""))
            if not name.startswith(prefix) or ".." in PurePosixPath(name).parts:
                continue
            match = self._asset_version(PurePosixPath(name).name, app)
            if not match or match[0] != normalize_version(version):
                continue
            download_url = f"https://archive.org/download/{quote(item, safe='')}/{quote(name, safe='/')}"
            found.append(
                (0 if match[1] == app.target_abi else 1, StockCandidate("archive", download_url, url, match[0]))
            )
        for _, candidate in sorted(found, key=lambda pair: pair[0]):
            yield candidate

    def _marketplace(self, provider: str, url: str, app: AppConfig, version: str | None) -> Iterator[StockCandidate]:
        from .marketplaces import MarketplaceSources

        if self._marketplaces is None:
            self._marketplaces = MarketplaceSources(self.http)
        for download_url, page, resolved in self._marketplaces.iter_candidates(provider, url, app, version):
            yield StockCandidate(provider, download_url, page, resolved)
