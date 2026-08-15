from __future__ import annotations

import hashlib
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .models import DownloadResult


class DownloadError(RuntimeError):
    pass


class HttpClient:
    def __init__(
        self,
        token: str | None = None,
        user_agent: str = "morphe-builds/2",
        connect_timeout: int = 20,
        read_timeout: int = 120,
    ) -> None:
        self.token = token
        self.timeout = (connect_timeout, read_timeout)
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": user_agent, "Accept": "application/vnd.github+json"})
        retry = Retry(
            total=3,
            connect=3,
            read=2,
            status=3,
            backoff_factor=1,
            status_forcelist=(500, 502, 503, 504),
            allowed_methods=frozenset({"GET", "HEAD"}),
            respect_retry_after_header=True,
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    def _headers(self, *, github: bool = False, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers: dict[str, str] = {}
        if github and self.token:
            headers["Authorization"] = f"Bearer {self.token}"
            headers["X-GitHub-Api-Version"] = "2022-11-28"
        if extra:
            headers.update(extra)
        return headers

    def get_json(self, url: str, *, github: bool = False, params: dict[str, Any] | None = None) -> Any:
        response = self.session.get(
            url,
            headers=self._headers(github=github),
            params=params,
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response.json()

    def request_json(
        self,
        method: str,
        url: str,
        *,
        github: bool = False,
        json_data: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        request_headers = self._headers(github=github, extra=headers)
        response = self.session.request(
            method,
            url,
            headers=request_headers,
            json=json_data,
            timeout=self.timeout,
        )
        response.raise_for_status()
        if not response.content:
            return None
        return response.json()

    def download(
        self,
        url: str,
        destination: Path,
        *,
        github: bool = False,
        expected_sha256: str | None = None,
        expected_size: int | None = None,
        max_size: int = 1_500_000_000,
        extra_headers: dict[str, str] | None = None,
        before_request: Callable[[], None] | None = None,
        html_link_resolver: Callable[[str, str], str | None] | None = None,
    ) -> DownloadResult:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temp_path = destination.with_name(f".{destination.name}.{os.getpid()}.part")
        temp_path.unlink(missing_ok=True)
        headers = self._headers(github=github, extra=extra_headers)
        current_url = url
        original_origin = self._origin(url)

        try:
            for _ in range(10):
                if before_request:
                    before_request()
                response = self.session.get(
                    current_url,
                    headers=headers,
                    stream=True,
                    allow_redirects=False,
                    timeout=self.timeout,
                )
                if response.is_redirect or response.is_permanent_redirect:
                    location = response.headers.get("Location")
                    response.close()
                    if not location:
                        raise DownloadError("Redirect response had no Location header")
                    previous_url = current_url
                    current_url = urljoin(current_url, location)
                    if urlsplit(current_url).scheme != "https":
                        raise DownloadError("Refusing a non-HTTPS redirect")
                    if self._origin(current_url) != original_origin:
                        headers.pop("Authorization", None)
                    headers["Referer"] = previous_url
                    continue
                try:
                    response.raise_for_status()
                except requests.RequestException:
                    response.close()
                    raise

                content_type = response.headers.get("Content-Type", "").lower()
                if html_link_resolver and "html" in content_type:
                    html = response.text
                    page_url = response.url
                    response.close()
                    next_url = html_link_resolver(html, page_url)
                    if not next_url:
                        raise DownloadError("HTML download page contained no next link")
                    current_url = urljoin(page_url, next_url)
                    if urlsplit(current_url).scheme != "https":
                        raise DownloadError("Refusing a non-HTTPS download-page link")
                    if self._origin(current_url) != original_origin:
                        headers.pop("Authorization", None)
                    headers["Referer"] = page_url
                    continue
                break
            else:
                raise DownloadError("Too many redirects or intermediate download pages")

            declared_size = response.headers.get("Content-Length")
            if declared_size and int(declared_size) > max_size:
                response.close()
                raise DownloadError(f"Download exceeds maximum size: {declared_size} bytes")

            digest = hashlib.sha256()
            size = 0
            prefix = bytearray()
            with response, temp_path.open("wb") as output:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    if len(prefix) < 1024:
                        prefix.extend(chunk[: 1024 - len(prefix)])
                    size += len(chunk)
                    if size > max_size:
                        raise DownloadError(f"Download exceeded maximum size: {max_size} bytes")
                    digest.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())

            if size == 0:
                raise DownloadError("Downloaded file is empty")
            if declared_size and size != int(declared_size):
                raise DownloadError(f"Truncated download: expected {declared_size} bytes, received {size}")
            if expected_size is not None and size != expected_size:
                raise DownloadError(f"Size mismatch: expected {expected_size}, received {size}")
            if self._looks_like_html(bytes(prefix)):
                raise DownloadError("Server returned an HTML/challenge page instead of an APK or tool")

            actual_sha256 = digest.hexdigest()
            if expected_sha256 and actual_sha256.lower() != expected_sha256.lower().removeprefix("sha256:"):
                raise DownloadError(f"SHA-256 mismatch: expected {expected_sha256}, received {actual_sha256}")
            temp_path.replace(destination)
            return DownloadResult(path=destination, size=size, sha256=actual_sha256, final_url=current_url)
        except (requests.RequestException, OSError, ValueError) as exc:
            raise DownloadError(str(exc)) from exc
        finally:
            temp_path.unlink(missing_ok=True)

    @staticmethod
    def _origin(url: str) -> tuple[str, str, int | None]:
        parsed = urlsplit(url)
        return parsed.scheme.lower(), (parsed.hostname or "").lower(), parsed.port

    @staticmethod
    def _looks_like_html(prefix: bytes) -> bool:
        sample = prefix.lstrip().lower()
        markers = (b"<!doctype html", b"<html", b"<head", b"cloudflare", b"captcha", b"just a moment")
        return any(marker in sample for marker in markers)
