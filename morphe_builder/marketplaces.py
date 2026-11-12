"""Bounded, public-page marketplace adapters; deliberately no challenge solvers.

Uptodown exposes versionURL records in its public versions API. APKPure's
/downloading/<version> pages expose per-ABI /b/APK and /b/XAPK links. APKCombo
uses /download/phone-<version>-<format> and signed /d?u= or /r2?u= wrappers.
Only links actually published in those responses are returned. In particular,
Uptodown's newer Turnstile-gated download-url API is not called.
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import parse_qs, quote, unquote, urljoin, urlsplit

import requests

from .http import HttpClient
from .models import AppConfig
from .versions import version_key

_MAX_REQUESTS = 16
_MAX_BYTES = 2_000_000
_MAX_VERSIONS = 5
_MAX_HISTORY_PAGES = 3
_PAGE_HOSTS = {"apkpure": {"apkpure.com", "www.apkpure.com"}, "apkcombo": {"apkcombo.com", "www.apkcombo.com"}}
_DOWNLOAD_HOSTS = {
    "uptodown": {"dw.uptodown.com"},
    "apkpure": {"d.apkpure.com", "download.apkpure.com", "download.pureapk.com", "data.winudf.com"},
    "apkcombo": {"download.apkcombo.com", "download.pureapk.com", "d.apkpure.com", "data.winudf.com"},
}
_PRERELEASE = re.compile(
    r"beta|alpha|preview|canary|nightly|snapshot|secondary|(?:^|[\d._ -])(?:rc|dev)(?:\d|$|[._ -])", re.I
)
_ABIS = re.compile(r"arm64[-_]v8a|armeabi[-_]v7a|x86_64|x86|universal|noarch", re.I)
_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}


def _safe_url(url: str, hosts: set[str]) -> bool:
    try:
        parts = urlsplit(url)
        return (
            parts.scheme == "https"
            and parts.hostname in hosts
            and parts.port in (None, 443)
            and not parts.username
            and not parts.password
            and not parts.fragment
            and not re.search(r"[\s\\\x00-\x1f]", url)
        )
    except ValueError:
        return False


def _stable(version: str) -> bool:
    return bool(re.fullmatch(r"[0-9][A-Za-z0-9._+ -]{0,99}", version)) and not _PRERELEASE.search(version)


@dataclass
class _Node:
    tag: str
    attrs: dict[str, str]
    parent: _Node | None = None
    children: list[_Node | str] = field(default_factory=list)

    def text(self) -> str:
        return "".join(c.text() if isinstance(c, _Node) else c for c in self.children).strip()


class _Page(HTMLParser):
    def __init__(self, html: str):
        super().__init__(convert_charrefs=True)
        self.root = _Node("root", {})
        self.stack = [self.root]
        self.nodes: list[_Node] = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        node = _Node(tag, {k: v or "" for k, v in attrs}, self.stack[-1])
        self.stack[-1].children.append(node)
        self.nodes.append(node)
        if tag not in _VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.stack.pop()

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)

    def applications(self) -> Iterator[dict]:
        def walk(value):
            if isinstance(value, list):
                for child in value:
                    yield from walk(child)
            elif isinstance(value, dict):
                types = value.get("@type", [])
                if isinstance(types, str):
                    types = [types]
                if any(t in types for t in ("SoftwareApplication", "MobileApplication")):
                    yield value
                yield from walk(value.get("@graph", []))

        for node in self.nodes:
            if node.tag == "script" and node.attrs.get("type") == "application/ld+json":
                try:
                    yield from walk(json.loads(node.text()))
                except (ValueError, TypeError):
                    continue

    def version(self) -> str | None:
        for app in self.applications():
            value = app.get("softwareVersion")
            if isinstance(value, str) and value.strip():
                return value.strip()
        for node in self.nodes:
            if {"version", "vername"} & set(node.attrs.get("class", "").split()):
                return node.text()
        for node in self.nodes:
            if node.tag == "h2" and "download" in node.text().lower():
                match = re.search(r"\b[0-9]+(?:\.[0-9]+)+(?:[-._+][A-Za-z0-9]+)*", node.text())
                if match:
                    value = match[0]
                    if _PRERELEASE.search(node.text()):
                        value += "-beta"
                    return value
        return None

    def links(self, base: str) -> Iterator[tuple[str, str]]:
        """Retain each variant's local ABI label, not every ABI on the page."""
        code_abi = ""
        for node in self.nodes:
            if node.tag == "code":
                code_abi = node.text()
            href = node.attrs.get("href") if node.tag == "a" else None
            if node.tag == "iframe" and node.attrs.get("id") == "iframe_download":
                href = node.attrs.get("src")
            if href:
                context = node.text() + " " + code_abi
                parent = None if _ABIS.search(code_abi) else node.parent
                for _ in range(3):
                    if parent is None or parent.tag in ("root", "body", "html"):
                        break
                    text = parent.text()
                    if len(text) < 500:
                        context += " " + text
                        if _ABIS.search(text):
                            break
                    parent = parent.parent
                yield urljoin(base, href), context


class _Fetch:
    def __init__(
        self,
        http: HttpClient,
        provider: str,
        page_hosts: set[str],
        failures: dict[str, tuple[int | None, str]],
        blocked_hosts: dict[str, str],
    ):
        self.http, self.provider, self.hosts = http, provider, page_hosts
        self.failures, self.blocked_hosts = failures, blocked_hosts
        self.requests = 0

    def fail(self, url: str, message: str, status: int | None = None, *, block: bool = False):
        self.failures[url] = (status, message)
        if block:
            self.blocked_hosts[urlsplit(url).hostname or ""] = message
        raise RuntimeError(message)

    def get(self, url: str, *, missing_ok: bool = False) -> str | None:
        for _ in range(4):
            if not _safe_url(url, self.hosts):
                raise RuntimeError(f"{self.provider}: unsafe or untrusted page/redirect URL: {url}")
            blocked = self.blocked_hosts.get(urlsplit(url).hostname or "")
            if blocked:
                raise RuntimeError(blocked)
            if url in self.failures:
                status, message = self.failures[url]
                if status == 404 and missing_ok:
                    return None
                raise RuntimeError(message)
            if self.requests >= _MAX_REQUESTS:
                raise RuntimeError(f"{self.provider}: page request limit reached")
            self.requests += 1
            try:
                response = self.http.session.get(
                    url,
                    headers={"Accept": "text/html,application/xhtml+xml,application/json;q=0.9", "Authorization": None},
                    timeout=(min(self.http.timeout[0], 15), min(self.http.timeout[1], 30)),
                    allow_redirects=False,
                    stream=True,
                )
                try:
                    if response.status_code in (301, 302, 303, 307, 308):
                        location = response.headers.get("Location")
                        if not location:
                            raise RuntimeError(f"{self.provider}: redirect without Location at {url}")
                        url = urljoin(url, location)
                        continue
                    if response.status_code >= 400:
                        status = response.status_code
                        wait = response.headers.get("Retry-After")
                        detail = f"; Retry-After: {wait}" if wait else ""
                        message = f"{self.provider}: HTTP {status} (missing/blocked source) at {url}{detail}"
                        self.failures[url] = (status, message)
                        if missing_ok and status == 404:
                            return None
                        self.fail(url, message, status, block=status in (403, 429))
                    body = bytearray()
                    for chunk in response.iter_content(65536):
                        body.extend(chunk)
                        if len(body) > _MAX_BYTES:
                            raise RuntimeError(f"{self.provider}: response size limit exceeded at {url}")
                    html = body.decode(response.encoding or "utf-8", errors="replace")
                finally:
                    response.close()
            except requests.RequestException as exc:
                self.fail(url, f"{self.provider}: request failed at {url}: {exc}")
            if re.search(
                r"<title>\s*(?:Just a moment|Attention Required)|cf-chl-|verify you are human|captcha-container",
                html,
                re.I,
            ):
                self.fail(url, f"{self.provider}: blocked by an anti-bot challenge at {url}", block=True)
            return html
        raise RuntimeError(f"{self.provider}: redirect limit exceeded at {url}")


class MarketplaceSources:
    def __init__(self, http: HttpClient):
        self.http = http
        # Run-local negative caching. Never hammer a blocked host for each app/version.
        self._failures: dict[str, tuple[int | None, str]] = {}
        self._blocked_hosts: dict[str, str] = {}

    def _releases(
        self, provider: str, url: str, app: AppConfig, version: str | None
    ) -> tuple[_Fetch, str, list[tuple[str, str]]]:
        if provider not in _DOWNLOAD_HOSTS:
            raise RuntimeError(f"Unknown marketplace provider: {provider}")
        parts = urlsplit(url)
        hosts = (
            {parts.hostname}
            if provider == "uptodown" and parts.hostname and parts.hostname.endswith(".uptodown.com")
            else _PAGE_HOSTS.get(provider, set())
        )
        if not _safe_url(url, hosts) or parts.query:
            raise RuntimeError(f"{provider}: unsafe canonical app URL: {url}")
        base = url.rstrip("/")
        if provider in ("apkpure", "apkcombo") and parts.path.rstrip("/").split("/")[-1] != app.package:
            raise RuntimeError(f"{provider}: canonical app URL does not match package {app.package}")
        if provider == "uptodown" and parts.path.rstrip("/") != "/android":
            raise RuntimeError("uptodown: expected a canonical /android app URL")
        if version is not None and not _stable(version):
            raise RuntimeError(f"{provider}: only stable versions are supported: {version!r}")
        fetch = _Fetch(self.http, provider, hosts, self._failures, self._blocked_hosts)
        if provider == "uptodown":
            releases = self._uptodown_releases(fetch, base, version)
        elif version is not None:
            path = (
                "/downloading/" + quote(version, safe="")
                if provider == "apkpure"
                else "/download/phone-" + quote(version, safe="") + "-" + app.formats[0]
            )
            releases = [(version, base + path)]
        else:
            releases = self._web_releases(fetch, provider, base)
        return fetch, base, releases[:_MAX_VERSIONS]

    def discover_versions(self, provider: str, url: str, app: AppConfig) -> list[str]:
        # Public version metadata can identify what to fetch from a different
        # source even when this marketplace gates its own binary download.
        _, _, releases = self._releases(provider, url, app, None)
        return sorted({version for version, _ in releases if _stable(version)}, key=version_key, reverse=True)

    def iter_candidates(
        self, provider: str, url: str, app: AppConfig, version: str | None
    ) -> Iterator[tuple[str, str, str]]:
        fetch, base, releases = self._releases(provider, url, app, version)
        found = False
        seen: set[tuple[str, str]] = set()
        for resolved, page_url in releases[:_MAX_VERSIONS]:
            release_found = False
            pages = [page_url]
            if provider == "apkcombo" and version is not None:
                prefix = base + "/download/phone-" + quote(version, safe="") + "-"
                pages = [prefix + ext for ext in app.formats if ext in ("apk", "xapk", "apks")]
            for source_page in pages:
                html = fetch.get(source_page, missing_ok=provider == "apkcombo")
                if html is None:
                    continue
                page = _Page(html)
                actual = page.version()
                if actual != resolved:
                    raise RuntimeError(
                        f"{provider}: version mismatch at {source_page}: expected {resolved}, found {actual!r}"
                    )
                if not _stable(actual):
                    raise RuntimeError(f"{provider}: non-stable version at {source_page}")
                download_pages = [(source_page, page)]
                if provider == "uptodown":
                    variant_urls = self._uptodown_variants(fetch, base, page, app)
                    if variant_urls:
                        download_pages = []
                        for variant_url in variant_urls:
                            variant = page if variant_url == source_page else _Page(fetch.get(variant_url) or "")
                            if variant.version() != resolved:
                                raise RuntimeError(f"uptodown: variant version mismatch at {variant_url}")
                            download_pages.append((variant_url, variant))
                candidates = [
                    (direct, origin)
                    for origin, variant in download_pages
                    for direct in self._downloads(provider, variant, origin, app)
                ]
                if not candidates:
                    reason = (
                        "blocked: Turnstile/challenge or no public download link"
                        if provider == "uptodown"
                        else "no trusted compatible download candidate"
                    )
                    # A newest release must not disappear silently in favour of an older one.
                    if provider != "apkcombo":
                        fetch.fail(
                            source_page,
                            f"{provider}: {reason} for {resolved} at {source_page}",
                            block=provider == "uptodown" and "download-turnstile-widget" in html,
                        )
                    continue
                release_found = True
                for direct, origin in candidates:
                    if (direct, actual) not in seen:
                        seen.add((direct, actual))
                        found = True
                        yield direct, origin, actual
                # Suffix pages duplicate APKCombo's variant list; do not fetch all of them.
                break
            if not release_found:
                raise RuntimeError(f"{provider}: no trusted compatible download candidate for {resolved}")
        if not found:
            raise RuntimeError(
                f"{provider}: no trusted compatible download candidate for {version or 'latest stable'} within bounded discovery"
            )

    @staticmethod
    def _uptodown_releases(fetch: _Fetch, base: str, version: str | None) -> list[tuple[str, str]]:
        root = _Page(fetch.get(base) or "")
        app_id = next((n.attrs.get("data-code") for n in root.nodes if n.attrs.get("id") == "detail-app-name"), None)
        if not app_id or not app_id.isdigit():
            raise RuntimeError("uptodown: missing app ID; app page is unavailable or blocked")
        releases = []
        for number in range(1, (_MAX_HISTORY_PAGES if version else 1) + 1):
            raw = fetch.get(f"{base}/apps/{app_id}/versions/{number}")
            try:
                items = json.loads(raw or "").get("data")
            except (ValueError, AttributeError) as exc:
                raise RuntimeError("uptodown: invalid public versions response") from exc
            if not isinstance(items, list):
                raise RuntimeError("uptodown: missing public versions data")
            for item in items:
                if not isinstance(item, dict):
                    continue
                value = str(item.get("version", "")).strip()
                if not _stable(value) or (version is not None and value != version):
                    continue
                target = item.get("versionURL")
                if isinstance(target, dict):
                    page = "/".join(str(target.get(k, "")).rstrip("/") for k in ("url", "extraURL", "versionID"))
                else:
                    file_id = str(item.get("fileID", ""))
                    page = f"{base}/download/{file_id}" if file_id.isdigit() else ""
                if page.startswith(base + "/download/") and _safe_url(page, fetch.hosts):
                    releases.append((value, page))
            if version is not None and releases:
                break
            if not items:
                break
        latest = root.version()
        if version is None and latest and _stable(latest) and not any(v == latest for v, _ in releases):
            releases.append((latest, base + "/download"))
        if not releases:
            raise RuntimeError(
                f"uptodown: version {version or 'latest stable'} not found within {_MAX_HISTORY_PAGES} history pages"
            )
        return sorted(set(releases), key=lambda item: version_key(item[0]), reverse=True)

    @staticmethod
    def _uptodown_variants(fetch: _Fetch, base: str, page: _Page, app: AppConfig) -> list[str]:
        app_id = next(
            (
                n.attrs.get("data-app-id") or n.attrs.get("data-code")
                for n in page.nodes
                if n.attrs.get("data-app-id") or n.attrs.get("id") == "detail-app-name"
            ),
            "",
        )
        version_id = next(
            (n.attrs["data-version"] for n in page.nodes if n.attrs.get("data-version", "").isdigit()), ""
        )
        if not app_id or not app_id.isdigit() or not version_id:
            return []
        url = f"{base.rsplit('/', 1)[0]}/app/{app_id}/version/{version_id}/files"
        try:
            payload = json.loads(fetch.get(url) or "")
            content = payload.get("content")
        except (ValueError, AttributeError) as exc:
            raise RuntimeError("uptodown: invalid public variants response") from exc
        if not isinstance(content, str):
            raise RuntimeError("uptodown: missing public variants markup")
        variants = _Page(content)
        abi = ""
        ranked = []
        for node in variants.nodes:
            if node.tag == "p":
                abi = node.text().lower()
            file_id = node.attrs.get("data-file-id", "")
            if not file_id.isdigit() or not abi:
                continue
            abis = set(_ABIS.findall(abi))
            if app.target_abi not in abis and not abis & {"universal", "noarch"}:
                continue
            selected_abi = app.target_abi if abis == {app.target_abi} else "universal"
            variant_html = node.parent.text() if node.parent else ""
            variant_attrs = " ".join(
                v for child in variants.nodes if child.parent is node.parent for v in child.attrs.values()
            )
            kind = "xapk" if "xapk" in (variant_html + variant_attrs).lower() else "apk"
            if kind not in app.formats:
                continue
            preference = app.prefer.index(selected_abi) if selected_abi in app.prefer else len(app.prefer)
            ranked.append((preference, app.formats.index(kind), f"{base}/download/{file_id}"))
        return list(dict.fromkeys(item[2] for item in sorted(ranked)))[:3]

    @staticmethod
    def _web_releases(fetch: _Fetch, provider: str, base: str) -> list[tuple[str, str]]:
        listing_url = base + ("/versions" if provider == "apkpure" else "/download/apk")
        page = _Page(fetch.get(listing_url) or "")
        releases = []
        for node in page.nodes:
            if (
                node.tag != "a"
                or not node.attrs.get("href")
                or _PRERELEASE.search(node.text() + " " + node.attrs.get("title", ""))
            ):
                continue
            href = urljoin(listing_url, node.attrs["href"])
            if not href.startswith(base + "/") or not _safe_url(href, fetch.hosts):
                continue
            suffix = href[len(base) :]
            pattern = (
                r"/(?:downloading|download)/([^/?#]+)/*"
                if provider == "apkpure"
                else r"/download/phone-(.+)-(?:apk|xapk|apks)/*"
            )
            match = re.fullmatch(pattern, suffix)
            value = unquote(match[1]) if match else ""
            if _stable(value):
                releases.append((value, href))
        latest = page.version()
        if latest and _stable(latest) and not any(v == latest for v, _ in releases):
            href = base + "/downloading/" + quote(latest, safe="") if provider == "apkpure" else listing_url
            releases.append((latest, href))
        if not releases:
            raise RuntimeError(f"{provider}: no stable versions on the public app listing (missing/blocked source)")
        return sorted(set(releases), key=lambda item: version_key(item[0]), reverse=True)

    @staticmethod
    def _downloads(provider: str, page: _Page, source: str, app: AppConfig) -> list[str]:
        links = list(page.links(source))
        if provider == "uptodown":
            packages = set(
                re.findall(
                    r"play\.google\.com/store/apps/details\?id=([A-Za-z0-9_.]+)",
                    " ".join(value for node in page.nodes for value in node.attrs.values()),
                )
            )
            for node in page.nodes:
                if node.tag == "tr" and re.search(r"package\s+name", node.text(), re.I):
                    packages.update(re.findall(r"\b[a-zA-Z]\w*(?:\.[a-zA-Z]\w*){2,}\b", node.text()))
            if packages and packages != {app.package}:
                raise RuntimeError(f"uptodown: download page package mismatch: {sorted(packages)}")
            architecture = ""
            for node in page.nodes:
                if node.tag == "tr" and re.search(r"architecture", node.text(), re.I):
                    architecture = node.text()
            for node in page.nodes:
                if node.attrs.get("id") not in ("detail-download-button", "detail-download-button-know-native"):
                    continue
                token = node.attrs.get("data-url", "")
                if token and not token.startswith(("http:", "//")) and ".." not in token:
                    direct = token if token.startswith("https:") else "https://dw.uptodown.com/dwn/" + token.lstrip("/")
                    kind = "xapk" if node.attrs.get("data-only-xapk") == "1" else "apk"
                    links.append((direct, architecture + " " + kind))
        ranked = []
        for direct, label in links:
            if provider == "apkcombo" and _safe_url(direct, _PAGE_HOSTS["apkcombo"]):
                parts = urlsplit(direct)
                token = parse_qs(parts.query).get("u", [""])[0]
                try:
                    if parts.path == "/d":
                        direct = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode("utf-8")
                    elif parts.path == "/r2":
                        direct = unquote(token)
                    else:
                        continue
                except (ValueError, UnicodeError):
                    continue
            if not _safe_url(direct, _DOWNLOAD_HOSTS[provider]):
                continue
            parts = urlsplit(direct)
            # A provider's file host is not enough: APKPure links name the package
            # and must pin a real version code, never the mutable latest endpoint.
            params = parse_qs(parts.query)
            if parts.hostname in {"d.apkpure.com", "download.pureapk.com", "download.apkpure.com"} and (
                not re.fullmatch(r"/b/(?:APK|XAPK)/" + re.escape(app.package), parts.path)
                or not params.get("versionCode", [""])[0].isdigit()
            ):
                continue
            abi_text = params.get("nc", [""])[0] or label
            abis = {
                x.lower().replace("arm64_v8a", "arm64-v8a").replace("armeabi_v7a", "armeabi-v7a")
                for x in _ABIS.findall(abi_text)
            }
            target = app.target_abi.lower()
            if abis and target not in abis and not abis & {"universal", "noarch"}:
                continue
            if target in abis:
                abi = target if len(abis) == 1 else "universal"
            elif abis & {"universal", "noarch"}:
                abi = "universal"
            else:
                abi = "unknown"  # Byte-level package/ABI validation remains the caller's job.
            kind_match = re.search(r"/b/(APK|XAPK)/|\.(apk|xapk|apks)(?:$|[?&])", unquote(direct), re.I)
            kind = next((g.lower() for g in kind_match.groups() if g), "") if kind_match else ""
            if not kind:
                kind = next((k for k in ("xapk", "apks", "apk") if re.search(r"\b" + k + r"\b", label, re.I)), "")
            if kind not in app.formats:
                continue
            preference = app.prefer.index(abi) if abi in app.prefer else len(app.prefer)
            ranked.append((preference, app.formats.index(kind), direct))
        return list(dict.fromkeys(item[2] for item in sorted(ranked)))
