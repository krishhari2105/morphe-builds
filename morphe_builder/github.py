from __future__ import annotations

import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote

from .http import HttpClient
from .manifest import sha256_file
from .models import DownloadResult, ReleaseAsset, ReleaseInfo, SourceConfig


API = "https://api.github.com"


class GitHubError(RuntimeError):
    pass


class GitHubClient:
    def __init__(self, token: str | None = None, repository: str | None = None) -> None:
        self.http = HttpClient(token=token or os.environ.get("GITHUB_TOKEN"))
        self.repository = repository or os.environ.get("GITHUB_REPOSITORY")

    @staticmethod
    def _asset(raw: dict[str, Any]) -> ReleaseAsset:
        return ReleaseAsset(
            id=int(raw["id"]),
            name=str(raw["name"]),
            api_url=str(raw["url"]),
            browser_url=str(raw["browser_download_url"]),
            size=int(raw.get("size", 0)),
            digest=raw.get("digest"),
        )

    @classmethod
    def _release(cls, repo: str, raw: dict[str, Any]) -> ReleaseInfo:
        return ReleaseInfo(
            id=int(raw["id"]),
            repo=repo,
            tag=str(raw["tag_name"]),
            name=str(raw.get("name") or raw["tag_name"]),
            prerelease=bool(raw.get("prerelease")),
            draft=bool(raw.get("draft")),
            published_at=raw.get("published_at"),
            assets=tuple(cls._asset(asset) for asset in raw.get("assets", [])),
        )

    def list_releases(self, repo: str, *, max_pages: int = 5) -> list[ReleaseInfo]:
        releases: list[ReleaseInfo] = []
        for page in range(1, max_pages + 1):
            raw = self.http.get_json(
                f"{API}/repos/{repo}/releases",
                github=True,
                params={"per_page": 100, "page": page},
            )
            if not isinstance(raw, list):
                raise GitHubError(f"Unexpected releases response for {repo}")
            releases.extend(self._release(repo, item) for item in raw)
            if len(raw) < 100:
                break
        return releases

    def select_release(self, repo: str, channel: str) -> ReleaseInfo:
        releases = [release for release in self.list_releases(repo) if not release.draft]
        if channel == "stable":
            releases = [release for release in releases if not release.prerelease]
        elif channel == "prerelease":
            releases = [release for release in releases if release.prerelease]
        elif channel != "latest":
            raise GitHubError(f"Unsupported release channel: {channel}")
        if not releases:
            raise GitHubError(f"No {channel} release found for {repo}")
        releases.sort(key=lambda release: release.published_at or "", reverse=True)
        return releases[0]

    @staticmethod
    def select_asset(release: ReleaseInfo, pattern: str) -> ReleaseAsset:
        regex = re.compile(pattern)
        matches = [asset for asset in release.assets if regex.fullmatch(asset.name)]
        if len(matches) != 1:
            names = ", ".join(asset.name for asset in release.assets) or "none"
            raise GitHubError(
                f"Expected exactly one asset matching {pattern!r} in {release.repo}@{release.tag}; "
                f"found {len(matches)}. Assets: {names}"
            )
        return matches[0]

    def resolve_source(self, source: SourceConfig) -> tuple[ReleaseInfo, ReleaseAsset, ReleaseInfo, ReleaseAsset]:
        patches_release = self.select_release(source.patches_repo, source.channel)
        cli_channel = "latest" if source.channel == "prerelease" else source.channel
        cli_release = self.select_release(source.cli_repo, cli_channel)
        patches_asset = self.select_asset(patches_release, source.patches_asset_regex)
        cli_asset = self.select_asset(cli_release, source.cli_asset_regex)
        return patches_release, patches_asset, cli_release, cli_asset

    def download_asset(self, asset: ReleaseAsset, destination: Path) -> DownloadResult:
        expected_digest = asset.digest.removeprefix("sha256:") if asset.digest else None
        return self.http.download(
            asset.api_url,
            destination,
            github=True,
            expected_sha256=expected_digest,
            expected_size=asset.size or None,
            extra_headers={"Accept": "application/octet-stream"},
        )

    def get_release_by_tag(self, tag: str, repo: str | None = None) -> ReleaseInfo | None:
        target_repo = repo or self.repository
        if not target_repo:
            raise GitHubError("No target GitHub repository configured")
        try:
            raw = self.http.get_json(
                f"{API}/repos/{target_repo}/releases/tags/{quote(tag, safe='')}",
                github=True,
            )
        except Exception as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status == 404:
                return None
            raise
        return self._release(target_repo, raw)

    def delete_release(self, release: ReleaseInfo) -> None:
        self.http.request_json(
            "DELETE",
            f"{API}/repos/{release.repo}/releases/{release.id}",
            github=True,
        )

    def prune_build_releases(
        self,
        source: str,
        current: ReleaseInfo,
        source_keys: Iterable[str],
    ) -> list[ReleaseInfo]:
        releases = self.list_releases(current.repo, max_pages=10)
        ordered_sources = sorted(source_keys, key=len, reverse=True)
        deleted: list[ReleaseInfo] = []
        for release in releases:
            if release.id == current.id or release.draft:
                continue
            release_source = next(
                (key for key in ordered_sources if release.tag.startswith(f"build-{key}-")),
                None,
            )
            if release_source != source:
                continue
            self.delete_release(release)
            deleted.append(release)
        return deleted

    def create_release(self, tag: str, name: str, body: str, repo: str | None = None) -> ReleaseInfo:
        target_repo = repo or self.repository
        if not target_repo:
            raise GitHubError("No target GitHub repository configured")
        raw = self.http.request_json(
            "POST",
            f"{API}/repos/{target_repo}/releases",
            github=True,
            json_data={
                "tag_name": tag,
                "name": name,
                "body": body,
                "draft": False,
                "prerelease": False,
                "generate_release_notes": False,
            },
        )
        return self._release(target_repo, raw)

    def upload_asset(self, release: ReleaseInfo, path: Path, content_type: str = "application/octet-stream") -> None:
        if not self.repository:
            raise GitHubError("No target GitHub repository configured")
        upload_url = f"https://uploads.github.com/repos/{self.repository}/releases/{release.id}/assets"
        headers = self.http._headers(github=True, extra={"Content-Type": content_type})
        last_error: Exception | None = None
        for attempt in range(1, 4):
            try:
                with path.open("rb") as handle:
                    response = self.http.session.post(
                        upload_url,
                        headers=headers,
                        params={"name": path.name},
                        data=handle,
                        timeout=(20, 300),
                    )
                response.raise_for_status()
                return
            except Exception as exc:
                last_error = exc
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if attempt == 3 or (status is not None and status < 500 and status != 429):
                    raise
                time.sleep(attempt * 2)
        raise GitHubError(f"Failed to upload {path.name}: {last_error}")

    def _asset_matches(self, asset: ReleaseAsset, path: Path) -> bool:
        local_digest = sha256_file(path)
        if asset.digest:
            return asset.digest.removeprefix("sha256:").lower() == local_digest
        with tempfile.TemporaryDirectory(prefix="morphe-release-check-") as temp:
            downloaded = Path(temp) / asset.name
            self.download_asset(asset, downloaded)
            return sha256_file(downloaded) == local_digest

    def publish_release(
        self,
        tag: str,
        name: str,
        body: str,
        files: Iterable[Path],
    ) -> ReleaseInfo:
        paths = list(files)
        expected_names = {path.name for path in paths}
        if len(expected_names) != len(paths):
            raise GitHubError("Release contains duplicate local asset names")

        release = self.get_release_by_tag(tag)
        if release is None:
            release = self.create_release(tag, name, body)
        else:
            extra = {asset.name for asset in release.assets} - expected_names
            if extra:
                raise GitHubError(f"Release {tag} has unexpected assets: {sorted(extra)}")

        existing_by_name: dict[str, list[ReleaseAsset]] = {}
        for asset in release.assets:
            existing_by_name.setdefault(asset.name, []).append(asset)
        for path in paths:
            matches = existing_by_name.get(path.name, [])
            if len(matches) > 1:
                raise GitHubError(f"Release {tag} has duplicate asset name: {path.name}")
            if matches:
                if not self._asset_matches(matches[0], path):
                    raise GitHubError(f"Release {tag} asset digest differs: {path.name}")
                continue
            self.upload_asset(release, path)

        verified = self.get_release_by_tag(tag)
        if verified is None:
            raise GitHubError(f"Release {tag} disappeared after publishing")
        assets_by_name = {asset.name: asset for asset in verified.assets}
        for path in paths:
            asset = assets_by_name.get(path.name)
            if asset is None or not self._asset_matches(asset, path):
                raise GitHubError(f"Release {tag} failed post-upload verification: {path.name}")
        return verified
