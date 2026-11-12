"""Version-first acquisition, validation, authentication, and provider failover."""

from __future__ import annotations

import re
import tempfile
from collections.abc import Iterator
from itertools import islice
from pathlib import Path
from typing import Any

from .acquisition import AcquisitionError, BaseCache, download_manual, rename_detected
from .android import AndroidTools
from .apkmirror import ApkMirrorRateLimited, ApkMirrorResolver
from .bundles import inspect_source, verify_source_signatures
from .http import HttpClient
from .models import ApkInfo, AppConfig
from .stock_sources import PROVIDER_ORDER, StockCandidate, StockSources, is_stable_version
from .versions import normalize_version, version_key

BaseResult = tuple[Path, ApkInfo, tuple[str, ...], bool, dict[str, Any]]


class BaseAcquirer:
    def __init__(
        self,
        http: HttpClient,
        apkmirror: ApkMirrorResolver,
        android: AndroidTools,
        cache: BaseCache,
        sources: StockSources,
    ) -> None:
        self.http = http
        self.apkmirror = apkmirror
        self.android = android
        self.cache = cache
        self.sources = sources
        self.blocked: dict[str, str] = {}

    def _failure(self, provider: str, exc: Exception) -> str:
        message = str(exc)
        if isinstance(exc, ApkMirrorRateLimited) or re.search(
            r"\b(?:403|429)\b|captcha|challenge|turnstile", message, re.IGNORECASE
        ):
            self.blocked[provider] = message
        return f"{provider}: {message}"

    def _candidates(self, provider: str, app: AppConfig, version: str) -> Iterator[StockCandidate]:
        if provider in self.blocked:
            return
        if provider == "apkmirror":
            for url, page in self.apkmirror.iter_candidates(app, version):
                yield StockCandidate(provider, url, page, version)
        else:
            yield from self.sources.iter_candidates(provider, app, version)

    def _latest_versions(self, app: AppConfig, errors: list[str]) -> list[str]:
        # Prefer an official release authority, then store listings. Mirrors/archives
        # can satisfy a known version but cannot tell us what upstream's latest is.
        for provider in ("github", "apkmirror", "uptodown", "apkpure", "apkcombo"):
            versions: set[str] = set()
            try:
                if provider in self.blocked:
                    continue
                discovered = (
                    self.apkmirror.discover_versions(app)
                    if provider == "apkmirror"
                    else self.sources.discover_versions(provider, app)
                )
                versions.update(normalize_version(value) for value in discovered if is_stable_version(value))
            except Exception as exc:
                errors.append(self._failure(provider, exc))
            if versions:
                return sorted(versions, key=version_key, reverse=True)
        return []

    def _accept(
        self,
        path: Path,
        app: AppConfig,
        expected_version: str | None,
        provider: str,
        source_page: str | None,
        final_url: str | None,
        allowed_versions: set[str] | None = None,
        latest: bool = False,
    ) -> BaseResult:
        info, abis, strip = inspect_source(
            path,
            self.android,
            expected_package=app.package,
            expected_version=expected_version,
            target_abi=app.target_abi,
        )
        if not is_stable_version(info.version_name):
            raise AcquisitionError(f"Not a stable input version: {info.version_name}")
        if allowed_versions and normalize_version(info.version_name) not in allowed_versions:
            raise AcquisitionError(
                f"Manual source version {info.version_name} is not compatible; expected {sorted(allowed_versions)}"
            )
        signer = verify_source_signatures(path, self.android, app.signing_cert_sha256)
        cached = self.cache.store(
            app,
            path,
            version_name=info.version_name,
            version_code=info.version_code,
            source_page=source_page,
            final_url=final_url,
            provider=provider,
            signing_certificate_sha256=signer,
            requested_version="Any" if latest else info.version_name,
            resolution_mode="latest" if latest else "exact",
        )
        return (
            cached.path,
            info,
            abis,
            strip,
            {
                "acquisition": provider,
                "provider": provider,
                "source_page": source_page,
                "final_url": final_url,
                "requested_version": "Any" if latest else info.version_name,
                "resolution_mode": "latest" if latest else "exact",
                "resolved_version": info.version_name,
                "input_sha256": cached.sha256,
                "input_signing_certificate_sha256": signer,
            },
        )

    def acquire(
        self,
        app: AppConfig,
        candidate_versions: list[str | None],
        manual_url: str | None,
        app_work_dir: Path,
    ) -> BaseResult:
        app_work_dir.mkdir(parents=True, exist_ok=True)
        errors: list[str] = []
        concrete = {normalize_version(value) for value in candidate_versions if value}
        latest = not concrete and None in candidate_versions
        if manual_url:
            try:
                with tempfile.TemporaryDirectory(prefix="manual-", dir=app_work_dir) as temp:
                    result = download_manual(self.http, manual_url, Path(temp) / "base.bin")
                    return self._accept(
                        rename_detected(result.path),
                        app,
                        None,
                        "manual-url",
                        manual_url,
                        result.final_url,
                        allowed_versions=concrete,
                        latest=latest,
                    )
            except Exception as exc:
                errors.append(f"manual-url: {exc}")

        versions = sorted(concrete, key=version_key, reverse=True)
        if not versions and None in candidate_versions:
            versions = self._latest_versions(app, errors)
        for version in versions:
            if not is_stable_version(version):
                errors.append(f"Skipped non-stable version {version}")
                continue
            cached = self.cache.find(app, version)
            if cached:
                try:
                    info, abis, strip = inspect_source(
                        cached.path,
                        self.android,
                        expected_package=app.package,
                        expected_version=version,
                        target_abi=app.target_abi,
                    )
                    signer = verify_source_signatures(cached.path, self.android, app.signing_cert_sha256)
                    return (
                        cached.path,
                        info,
                        abis,
                        strip,
                        {
                            "acquisition": "actions-cache",
                            "provider": cached.provider or "legacy-cache",
                            "source_page": cached.source_page,
                            "final_url": cached.final_url,
                            "requested_version": "Any" if latest else version,
                            "resolved_version": info.version_name,
                            "resolution_mode": "latest" if latest else "exact",
                            "input_sha256": cached.sha256,
                            "input_signing_certificate_sha256": signer,
                        },
                    )
                except Exception as exc:
                    errors.append(f"cache {version}: {exc}")
            for provider in PROVIDER_ORDER:
                if provider in self.blocked:
                    continue
                try:
                    count = 0
                    for candidate in islice(self._candidates(provider, app, version), 8):
                        count += 1
                        if normalize_version(candidate.version) != version:
                            errors.append(f"{provider} returned a different version than {version}")
                            continue
                        try:
                            with tempfile.TemporaryDirectory(prefix=f"{provider}-", dir=app_work_dir) as temp:
                                destination = Path(temp) / "base.bin"
                                if provider == "apkmirror":
                                    result = self.apkmirror.download_candidate(
                                        candidate.url, candidate.source_page, destination
                                    )
                                else:
                                    result = self.http.download(
                                        candidate.url,
                                        destination,
                                        expected_sha256=candidate.sha256,
                                        extra_headers={
                                            "Referer": candidate.source_page,
                                            "Accept": "application/octet-stream,*/*",
                                        },
                                    )
                                return self._accept(
                                    rename_detected(result.path),
                                    app,
                                    version,
                                    provider,
                                    candidate.source_page,
                                    result.final_url,
                                    latest=latest,
                                )
                        except Exception as exc:
                            errors.append(f"{version} {self._failure(provider, exc)}")
                            if provider in self.blocked:
                                break
                    if not count:
                        errors.append(f"{provider} {version}: no matching stock artifact")
                except Exception as exc:
                    errors.append(f"{version} {self._failure(provider, exc)}")
        blocked = "; ".join(f"{name} unavailable: {reason}" for name, reason in self.blocked.items())
        detail = "; ".join(errors[-24:])
        raise AcquisitionError(
            f"Could not acquire an authenticated stable base for {app.key}. {detail}. {blocked}. "
            f'Rerun with base_urls JSON such as {{"{app.key}": "https://..."}}'
        )
