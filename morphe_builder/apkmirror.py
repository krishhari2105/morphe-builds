from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from .http import HttpClient
from .models import AppConfig
from .versions import version_key


class ApkMirrorError(RuntimeError):
    pass


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
    def __init__(self, http: HttpClient | None = None) -> None:
        self.http = http or HttpClient(user_agent=(
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        ))
        self.http.session.headers.update(
            {
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.8",
            }
        )

    def _html(self, url: str, referer: str | None = None) -> tuple[str, str]:
        headers = {"Referer": referer} if referer else None
        response = self.http.session.get(url, headers=headers, timeout=self.http.timeout)
        if response.status_code in {403, 429}:
            raise ApkMirrorError(f"APKMirror blocked the request with HTTP {response.status_code}")
        response.raise_for_status()
        html = response.text
        if _challenge(html):
            raise ApkMirrorError("APKMirror returned a Cloudflare/CAPTCHA challenge")
        return html, response.url

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

    def resolve_latest_candidates(self, app: AppConfig) -> list[tuple[str, str, str]]:
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
        all_candidates: list[tuple[str, str, str]] = []
        for version, link in release_candidates:
            try:
                resolved = self._resolve_from_release(app, version, link, listing_url)
            except ApkMirrorError as exc:
                errors.append(f"{version}: {exc}")
                continue
            all_candidates.extend((direct, page, version) for direct, page in resolved)
        if all_candidates:
            return all_candidates
        detail = f" ({'; '.join(errors[:3])})" if errors else ""
        raise ApkMirrorError(f"Could not determine the latest APKMirror version for {app.name}{detail}")

    def resolve_latest(self, app: AppConfig) -> tuple[str, str, str]:
        candidates = self.resolve_latest_candidates(app)
        return candidates[0]

    def resolve(self, app: AppConfig, version: str) -> tuple[str, str]:
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
            candidates = self._resolve_constructed_candidates(app, version, listing_url)
        else:
            release_link = max(release_links, key=lambda link: self._release_score(link, version_tokens))
            candidates = self._resolve_from_release(app, version, release_link, listing_url)
        if not candidates:
            raise ApkMirrorError(f"No downloadable variants found for {app.name} {version}")
        return candidates[0]

    def resolve_candidates(self, app: AppConfig, version: str) -> list[tuple[str, str]]:
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
            return self._resolve_constructed_candidates(app, version, listing_url)
        release_link = max(release_links, key=lambda link: self._release_score(link, version_tokens))
        return self._resolve_from_release(app, version, release_link, listing_url)

    def _resolve_constructed_candidates(
        self,
        app: AppConfig,
        version: str,
        listing_url: str,
    ) -> list[tuple[str, str]]:
        errors: list[str] = []
        for release_url in self._constructed_release_urls(app, version):
            release_link = Link(release_url, f"{app.name} {version}")
            try:
                return self._resolve_from_release(app, version, release_link, listing_url)
            except Exception as exc:
                errors.append(f"{release_url}: {exc}")
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
        release_html, release_url = self._html(release_link.href, listing_url)
        candidates = [
            link
            for link in self._links(release_html, release_url)
            if "android-apk-download" in link.href.lower()
        ]
        if not candidates:
            raise ApkMirrorError(f"No downloadable variants found for {app.name} {version}")
        candidates.sort(key=lambda link: self._variant_score(link, app), reverse=True)
        resolved: list[tuple[str, str]] = []
        errors: list[str] = []
        for variant in candidates:
            try:
                download_html, download_page_url = self._html(variant.href, release_url)
                download_links = self._links(download_html, download_page_url)
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
                final_url = self._follow_download_pages(direct.href, download_page_url)
                if final_url not in {item[0] for item in resolved}:
                    resolved.append((final_url, variant.href))
            except ApkMirrorError as exc:
                errors.append(f"{variant.text or variant.href}: {exc}")
        if not resolved:
            detail = "; ".join(errors[:3])
            raise ApkMirrorError(f"No usable APKMirror variants: {detail}")
        return resolved

    def _follow_download_pages(self, url: str, referer: str) -> str:
        current_url = url
        current_referer = referer
        seen: set[str] = set()
        for _ in range(5):
            if current_url in seen:
                raise ApkMirrorError("APKMirror download flow entered a redirect/page loop")
            seen.add(current_url)
            response = self.http.session.get(
                current_url,
                headers={"Referer": current_referer},
                stream=True,
                allow_redirects=True,
                timeout=self.http.timeout,
            )
            if response.status_code in {403, 429}:
                response.close()
                raise ApkMirrorError(f"APKMirror blocked the download flow with HTTP {response.status_code}")
            response.raise_for_status()
            content_type = response.headers.get("Content-Type", "").lower()
            if "html" not in content_type:
                final_url = response.url
                response.close()
                return final_url
            html = response.text
            page_url = response.url
            response.close()
            if _challenge(html):
                raise ApkMirrorError("APKMirror returned a Cloudflare/CAPTCHA challenge")
            links = self._links(html, page_url)
            next_link = next((link for link in links if link.element_id == "download-link"), None)
            if next_link is None:
                next_link = next(
                    (
                        link
                        for link in links
                        if (
                            "download.php" in link.href.lower()
                            or "key=" in link.href.lower()
                            or link.text.strip().lower() in {"here", "click here", "download"}
                        )
                        and link.href not in seen
                    ),
                    None,
                )
            if next_link is None:
                raise ApkMirrorError("APKMirror intermediate page contained no next download link")
            current_referer = page_url
            current_url = next_link.href
        raise ApkMirrorError("APKMirror download flow exceeded the page limit")

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
        if any(abi in haystack for abi in ("x86", "armeabi-v7a", "armeabi")) and "arm64" not in haystack:
            score -= 200
        return score
