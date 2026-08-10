from __future__ import annotations

import re
import time
from collections.abc import Iterator
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from .http import HttpClient
from .models import AppConfig, DownloadResult
from .versions import version_key


_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


class ApkMirrorError(RuntimeError):
    pass


class ApkMirrorRateLimited(ApkMirrorError):
    def __init__(self, retry_after: str | None = None) -> None:
        self.retry_after = retry_after
        detail = f"; retry after {retry_after}" if retry_after else ""
        super().__init__(f"APKMirror rate limited the request with HTTP 429{detail}")


@dataclass(frozen=True)
class Link:
    href: str
    text: str
    element_id: str | None = None
    title: str | None = None


class _LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[Link] = []
        self._current: dict[str, str | None] | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        values = dict(attrs)
        href = values.get("href")
        if href:
            self._current = {"href": href, "id": values.get("id"), "title": values.get("title")}
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._current is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._current is not None:
            self.links.append(
                Link(
                    href=str(self._current["href"]),
                    text=" ".join("".join(self._text).split()),
                    element_id=self._current["id"],
                    title=self._current["title"],
                )
            )
            self._current = None
            self._text = []


def _challenge(html: str) -> bool:
    lower = html.lower()
    markers = ("cf-chl-", "captcha", "just a moment", "attention required", "cloudflare ray id")
    return any(marker in lower for marker in markers)


def _slug_version(version: str) -> list[str]:
    value = version.lower().lstrip("v")
    return [value, value.replace(".", "-"), re.sub(r"[^a-z0-9]+", "-", value).strip("-")]


class ApkMirrorResolver:
    def __init__(
        self,
        http: HttpClient | None = None,
        min_request_interval: float = 1.0,
        max_release_attempts: int = 2,
        max_variant_attempts: int = 3,
    ) -> None:
        self.http = http or HttpClient(user_agent=_BROWSER_USER_AGENT)
        self.min_request_interval = max(0.0, min_request_interval)
        self.max_release_attempts = max(1, max_release_attempts)
        self.max_variant_attempts = max(1, max_variant_attempts)
        self._last_request_at: float | None = None
        self.http.session.headers.update(
            {
                "User-Agent": _BROWSER_USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.8",
            }
        )

    def _pace_request(self) -> None:
        now = time.monotonic()
        last_request_at = getattr(self, "_last_request_at", None)
        min_request_interval = getattr(self, "min_request_interval", 1.0)
        if last_request_at is not None:
            remaining = min_request_interval - (now - last_request_at)
            if remaining > 0:
                time.sleep(remaining)
        self._last_request_at = time.monotonic()

    def _html(self, url: str, referer: str | None = None) -> tuple[str, str]:
        headers = {"Referer": referer} if referer else None
        self._pace_request()
        response = self.http.session.get(url, headers=headers, timeout=self.http.timeout)
        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            response.close()
            raise ApkMirrorRateLimited(retry_after)
        if response.status_code == 403:
            response.close()
            raise ApkMirrorError("APKMirror blocked the request with HTTP 403")
        response.raise_for_status()
        html = response.text
        response_url = response.url
        response.close()
        if _challenge(html):
            raise ApkMirrorError("APKMirror returned a Cloudflare/CAPTCHA challenge")
        return html, response_url

    @staticmethod
    def _links(html: str, base_url: str) -> list[Link]:
        parser = _LinkParser()
        parser.feed(html)
        result: list[Link] = []
        for link in parser.links:
            absolute = urljoin(base_url, link.href)
            host = (urlsplit(absolute).hostname or "").lower()
            if host.endswith("apkmirror.com"):
                result.append(Link(absolute, link.text, link.element_id, link.title))
        return result

    def iter_latest_candidates(self, app: AppConfig) -> Iterator[tuple[str, str, str]]:
        listing_html, listing_url = self._html(app.apkmirror_url)
        release_candidates: list[tuple[str, Link]] = []
        seen: set[str] = set()
        app_path = urlsplit(app.apkmirror_url).path.rstrip("/").lower()
        for link in self._links(listing_html, listing_url):
            link_path = urlsplit(link.href).path.lower()
            if (
                "/apk/" not in link_path
                or not link_path.startswith(app_path + "/")
                or "release" not in link_path
                or link.href in seen
            ):
                continue
            haystack = " ".join(filter(None, [link.text, link.title, link.href]))
            lower = haystack.lower()
            if any(marker in lower for marker in (" beta", " alpha", " rc", "-beta", "-alpha", "-rc")):
                continue
            match = re.search(r"(?<!\d)(\d+(?:\.\d+)+(?:[-._A-Za-z0-9]+)?)", haystack)
            if not match:
                continue
            seen.add(link.href)
            release_candidates.append((match.group(1).rstrip("-._"), link))

        release_candidates.sort(key=lambda item: version_key(item[0]), reverse=True)
        errors: list[str] = []
        yielded_any = False
        release_limit = getattr(self, "max_release_attempts", 2)
        for version, link in release_candidates[:release_limit]:
            try:
                for direct, page in self._iter_from_release(app, version, link, listing_url):
                    yielded_any = True
                    yield direct, page, version
            except ApkMirrorRateLimited:
                raise
            except ApkMirrorError as exc:
                errors.append(f"{version}: {exc}")
                continue
        if not yielded_any:
            detail = f" ({'; '.join(errors[:3])})" if errors else ""
            raise ApkMirrorError(f"Could not determine the latest APKMirror version for {app.name}{detail}")

    def resolve_latest_candidates(self, app: AppConfig) -> list[tuple[str, str, str]]:
        return list(self.iter_latest_candidates(app))

    def resolve_latest(self, app: AppConfig) -> tuple[str, str, str]:
        return next(self.iter_latest_candidates(app))

    def resolve(self, app: AppConfig, version: str) -> tuple[str, str]:
        return next(self.iter_candidates(app, version))

    def iter_candidates(self, app: AppConfig, version: str) -> Iterator[tuple[str, str]]:
        listing_html, listing_url = self._html(app.apkmirror_url)
        version_tokens = _slug_version(version)
        release_links = [
            link
            for link in self._links(listing_html, listing_url)
            if "/apk/" in link.href
            and any(token in (link.href + " " + link.text).lower() for token in version_tokens)
            and "release" in link.href.lower()
        ]
        if not release_links:
            yield from self._iter_constructed_candidates(app, version, listing_url)
            return
        release_link = max(release_links, key=lambda link: self._release_score(link, version_tokens))
        yield from self._iter_from_release(app, version, release_link, listing_url)

    def resolve_candidates(self, app: AppConfig, version: str) -> list[tuple[str, str]]:
        return list(self.iter_candidates(app, version))

    def _resolve_constructed_candidates(
        self,
        app: AppConfig,
        version: str,
        listing_url: str,
    ) -> list[tuple[str, str]]:
        return list(self._iter_constructed_candidates(app, version, listing_url))

    def _iter_constructed_candidates(
        self,
        app: AppConfig,
        version: str,
        listing_url: str,
    ) -> Iterator[tuple[str, str]]:
        errors: list[str] = []
        yielded_any = False
        for release_url in self._constructed_release_urls(app, version):
            release_link = Link(release_url, f"{app.name} {version}")
            try:
                for candidate in self._iter_from_release(app, version, release_link, listing_url):
                    yielded_any = True
                    yield candidate
                if yielded_any:
                    return
            except ApkMirrorRateLimited:
                raise
            except Exception as exc:
                errors.append(f"{release_url}: {exc}")
        if yielded_any:
            return
        detail = f": {'; '.join(errors[:3])}" if errors else ""
        raise ApkMirrorError(f"Could not resolve APKMirror release for {app.name} {version}{detail}")

    @classmethod
    def _constructed_release_urls(cls, app: AppConfig, version: str) -> list[str]:
        app_slug = urlsplit(app.apkmirror_url).path.rstrip("/").split("/")[-1]
        version_slug = re.sub(r"[^a-z0-9]+", "-", version.lower().lstrip("v")).strip("-")
        if app.key == "twitter":
            prefixes = ["x", app_slug]
            suffixes = ["release", "release-0-release"]
            return [
                urljoin(app.apkmirror_url, f"{prefix}-{version_slug}-{suffix}/")
                for prefix in prefixes
                for suffix in suffixes
            ]
        return [urljoin(app.apkmirror_url, f"{app_slug}-{version_slug}-release/")]

    @classmethod
    def _constructed_release_url(cls, app: AppConfig, version: str) -> str:
        return cls._constructed_release_urls(app, version)[-1]

    def _resolve_from_release(
        self,
        app: AppConfig,
        version: str,
        release_link: Link,
        listing_url: str,
    ) -> list[tuple[str, str]]:
        return list(self._iter_from_release(app, version, release_link, listing_url))

    def _iter_from_release(
        self,
        app: AppConfig,
        version: str,
        release_link: Link,
        listing_url: str,
    ) -> Iterator[tuple[str, str]]:
        release_html, release_url = self._html(release_link.href, listing_url)
        release_path = urlsplit(release_url).path.rstrip("/") + "/"
        pending: dict[str, Link] = {}

        def add_candidate(link: Link) -> None:
            link_path = urlsplit(link.href).path
            if "android-apk-download" not in link_path.lower() or not link_path.startswith(release_path):
                return
            key = self._variant_url_key(link.href)
            if key in visited:
                return
            existing = pending.get(key)
            pending[key] = self._merge_variant_links(existing, link) if existing else link

        visited: set[str] = set()
        for link in self._links(release_html, release_url):
            add_candidate(link)
        if not pending:
            raise ApkMirrorError(f"No downloadable variants found for {app.name} {version}")
        variant_limit = getattr(self, "max_variant_attempts", 3)
        errors: list[str] = []
        yielded_any = False
        while pending and len(visited) < variant_limit:
            key = max(pending, key=lambda item: self._variant_score(pending[item], app))
            variant = pending.pop(key)
            if self._variant_score(variant, app) < 0:
                continue
            visited.add(key)
            try:
                download_html, download_page_url = self._html(variant.href, release_url)
                download_links = self._links(download_html, download_page_url)
                current_variant = variant
                for alternate in download_links:
                    alternate_key = self._variant_url_key(alternate.href)
                    if alternate_key == key:
                        current_variant = self._merge_variant_links(current_variant, alternate)
                    add_candidate(alternate)
                if self._variant_score(current_variant, app) < 0:
                    errors.append(f"{current_variant.text or variant.href}: incompatible architecture")
                    continue
                direct = next((link for link in download_links if link.element_id == "download-link"), None)
                if direct is None:
                    direct = next(
                        (
                            link
                            for link in download_links
                            if "download.php" in link.href.lower() or "key=" in link.href.lower()
                        ),
                        None,
                    )
                if direct is None:
                    raise ApkMirrorError("download link missing")
                yielded_any = True
                yield direct.href, variant.href
            except ApkMirrorRateLimited:
                raise
            except ApkMirrorError as exc:
                errors.append(f"{variant.text or variant.href}: {exc}")
        if not yielded_any:
            detail = "; ".join(errors[:3])
            raise ApkMirrorError(f"No usable APKMirror variants: {detail}")

    @staticmethod
    def _variant_url_key(url: str) -> str:
        parts = urlsplit(url)
        return f"{parts.scheme.lower()}://{parts.netloc.lower()}{parts.path.rstrip('/')}"

    @staticmethod
    def _merge_variant_links(first: Link, second: Link) -> Link:
        texts = list(dict.fromkeys(value for value in (first.text, second.text) if value))
        titles = list(dict.fromkeys(value for value in (first.title, second.title) if value))
        return Link(
            href=first.href,
            text=" ".join(texts),
            element_id=first.element_id or second.element_id,
            title=" ".join(titles) or None,
        )

    def download_candidate(self, url: str, referer: str, destination: Path) -> DownloadResult:
        return self.http.download(
            url,
            destination,
            max_size=1_500_000_000,
            extra_headers={"Referer": referer},
            before_request=self._pace_request,
            html_link_resolver=self._next_download_link,
        )

    def _next_download_link(self, html: str, page_url: str) -> str | None:
        if _challenge(html):
            raise ApkMirrorError("APKMirror returned a Cloudflare/CAPTCHA challenge")
        links = self._links(html, page_url)
        next_link = next((link for link in links if link.element_id == "download-link"), None)
        if next_link is None:
            next_link = next(
                (
                    link
                    for link in links
                    if "download.php" in link.href.lower()
                    or "key=" in link.href.lower()
                    or link.text.strip().lower() in {"here", "click here", "download"}
                ),
                None,
            )
        return next_link.href if next_link else None

    @staticmethod
    def _release_score(link: Link, tokens: list[str]) -> int:
        haystack = (link.href + " " + link.text).lower()
        score = 0
        if tokens[0] in haystack:
            score += 20
        if tokens[1] in haystack:
            score += 10
        if "beta" not in haystack:
            score += 2
        return score

    @staticmethod
    def _variant_score(link: Link, app: AppConfig) -> int:
        haystack = " ".join(filter(None, [link.href, link.text, link.title])).lower().replace("_", "-")
        score = 0
        if "arm64-v8a" in haystack or "arm64" in haystack:
            score += 100
        if "universal" in haystack:
            score += 60
        if "bundle" in haystack or "apkm" in haystack:
            score += 15
        if "nodpi" in haystack:
            score += 5
        if any(abi in haystack for abi in ("x86", "arm-v7a", "armeabi-v7a", "armeabi")) and "arm64" not in haystack:
            score -= 200
        return score
