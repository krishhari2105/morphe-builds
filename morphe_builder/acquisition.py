from __future__ import annotations

import json
import shutil
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from .apkmirror import ApkMirrorRateLimited, ApkMirrorResolver
from .http import DownloadError, HttpClient
from .manifest import sha256_file, slug
from .models import AppConfig, DownloadResult
from .versions import version_key


class AcquisitionError(RuntimeError):
    pass


@dataclass(frozen=True)
class CachedBase:
    path: Path
    app: str
    package: str
    version_name: str
    version_code: str
    sha256: str
    source_page: str | None
    final_url: str | None = None
    requested_version: str | None = None
    resolution_mode: str = "exact"


class BaseCache:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def _read(self, manifest_path: Path, app: AppConfig, version: str | None) -> CachedBase | None:
        try:
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
            if raw.get("package") != app.package:
                return None
            if version is not None and str(raw.get("version_name", "")).lstrip("v") != version.lstrip("v"):
                return None
            path = self.root / raw["file"]
            if not path.is_file() or sha256_file(path) != raw.get("sha256"):
                return None
            return CachedBase(
                path=path,
                app=app.key,
                package=app.package,
                version_name=str(raw["version_name"]),
                version_code=str(raw["version_code"]),
                sha256=str(raw["sha256"]),
                source_page=raw.get("source_page"),
                final_url=raw.get("final_url"),
                requested_version=raw.get("requested_version"),
                resolution_mode=str(raw.get("resolution_mode", "exact")),
            )
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def find(self, app: AppConfig, version: str) -> CachedBase | None:
        for manifest_path in sorted(self.root.glob(f"{app.key}-*.json")):
            cached = self._read(manifest_path, app, version)
            if cached:
                return cached
        return None

    def find_latest(self, app: AppConfig) -> CachedBase | None:
        candidates: list[CachedBase] = []
        for manifest_path in self.root.glob(f"{app.key}-*.json"):
            cached = self._read(manifest_path, app, None)
            if cached:
                candidates.append(cached)
        candidates.sort(
            key=lambda item: (
                int(item.version_code) if item.version_code.isdigit() else -1,
                version_key(item.version_name),
                item.sha256,
            ),
            reverse=True,
        )
        return candidates[0] if candidates else None

    def store(
        self,
        app: AppConfig,
        source_path: Path,
        *,
        version_name: str,
        version_code: str,
        source_page: str | None,
        final_url: str | None = None,
        requested_version: str | None = None,
        resolution_mode: str = "exact",
    ) -> CachedBase:
        digest = sha256_file(source_path)
        extension = detect_format(source_path)
        filename = f"{app.key}-{slug(version_name)}-{slug(version_code)}-{digest[:12]}.{extension}"
        cached_path = self.root / filename
        if not cached_path.exists():
            shutil.copy2(source_path, cached_path)
        manifest = {
            "schema_version": 1,
            "app": app.key,
            "package": app.package,
            "version_name": version_name,
            "version_code": version_code,
            "sha256": digest,
            "file": filename,
            "source_page": source_page,
            "final_url": final_url,
            "requested_version": requested_version or version_name,
            "resolution_mode": resolution_mode,
        }
        manifest_path = self.root / f"{app.key}-{slug(version_name)}-{digest[:12]}.json"
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return CachedBase(
            path=cached_path,
            app=app.key,
            package=app.package,
            version_name=version_name,
            version_code=version_code,
            sha256=digest,
            source_page=source_page,
            final_url=final_url,
            requested_version=requested_version or version_name,
            resolution_mode=resolution_mode,
        )


def detect_format(path: Path) -> str:
    if not zipfile.is_zipfile(path):
        raise AcquisitionError(f"Downloaded file is not an APK-compatible ZIP: {path}")
    with zipfile.ZipFile(path) as archive:
        nested_apks = [name for name in archive.namelist() if name.lower().endswith(".apk")]
    if nested_apks:
        suffix = path.suffix.lower().lstrip(".")
        return suffix if suffix in {"apkm", "apks", "xapk"} else "apkm"
    return "apk"


def rename_detected(path: Path) -> Path:
    extension = detect_format(path)
    destination = path.with_suffix(f".{extension}")
    if destination != path:
        destination.unlink(missing_ok=True)
        path.replace(destination)
    return destination


def download_manual(http: HttpClient, url: str, destination: Path) -> DownloadResult:
    if not url.lower().startswith("https://"):
        raise AcquisitionError("Manual base URL must use HTTPS")
    return http.download(url, destination, max_size=1_500_000_000)


def download_apkmirror_latest_candidates(
    resolver: ApkMirrorResolver,
    app: AppConfig,
    destination_dir: Path,
) -> Iterator[tuple[DownloadResult, str, str]]:
    destination_dir.mkdir(parents=True, exist_ok=True)
    yielded_any = False
    for index, (direct_url, source_page, version) in enumerate(resolver.iter_latest_candidates(app)):
        destination = destination_dir / f"latest-candidate-{index}.bin"
        try:
            result = resolver.download_candidate(direct_url, source_page, destination)
            yielded_any = True
            yield result, source_page, version
        except ApkMirrorRateLimited:
            destination.unlink(missing_ok=True)
            raise
        except DownloadError as exc:
            destination.unlink(missing_ok=True)
            if "429" in str(exc):
                raise ApkMirrorRateLimited() from exc
        except Exception:
            destination.unlink(missing_ok=True)
    if not yielded_any:
        raise AcquisitionError(f"All latest APKMirror variants failed for {app.key}")


def download_apkmirror_candidates(
    resolver: ApkMirrorResolver,
    app: AppConfig,
    version: str,
    destination_dir: Path,
) -> Iterator[tuple[DownloadResult, str, str]]:
    destination_dir.mkdir(parents=True, exist_ok=True)
    yielded_any = False
    for index, (direct_url, source_page) in enumerate(resolver.iter_candidates(app, version)):
        destination = destination_dir / f"candidate-{index}.bin"
        try:
            result = resolver.download_candidate(direct_url, source_page, destination)
            yielded_any = True
            yield result, source_page, version
        except ApkMirrorRateLimited:
            destination.unlink(missing_ok=True)
            raise
        except DownloadError as exc:
            destination.unlink(missing_ok=True)
            if "429" in str(exc):
                raise ApkMirrorRateLimited() from exc
        except Exception:
            destination.unlink(missing_ok=True)
    if not yielded_any:
        raise AcquisitionError(f"All APKMirror variants failed to download for {app.key} {version}")


def download_apkmirror(
    resolver: ApkMirrorResolver,
    app: AppConfig,
    version: str | None,
    destination: Path,
) -> tuple[DownloadResult, str, str]:
    if version:
        candidates = download_apkmirror_candidates(resolver, app, version, destination.parent)
    else:
        candidates = download_apkmirror_latest_candidates(resolver, app, destination.parent)
    result, source_page, resolved_version = next(candidates)
    if result.path != destination:
        result.path.replace(destination)
        result = DownloadResult(destination, result.size, result.sha256, result.final_url)
    return result, source_page, resolved_version
