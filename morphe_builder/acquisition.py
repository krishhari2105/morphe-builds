from __future__ import annotations

import json
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path

from .apkmirror import ApkMirrorResolver
from .http import HttpClient
from .manifest import sha256_file, slug
from .models import AppConfig, DownloadResult


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


class BaseCache:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def find(self, app: AppConfig, version: str | None) -> CachedBase | None:
        for manifest_path in sorted(self.root.glob(f"{app.key}-*.json"), reverse=True):
            try:
                raw = json.loads(manifest_path.read_text(encoding="utf-8"))
                if raw.get("package") != app.package:
                    continue
                if version and str(raw.get("version_name", "")).lstrip("v") != version.lstrip("v"):
                    continue
                path = self.root / raw["file"]
                if not path.is_file() or sha256_file(path) != raw.get("sha256"):
                    continue
                return CachedBase(
                    path=path,
                    app=app.key,
                    package=app.package,
                    version_name=str(raw["version_name"]),
                    version_code=str(raw["version_code"]),
                    sha256=str(raw["sha256"]),
                    source_page=raw.get("source_page"),
                )
            except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue
        return None

    def store(
        self,
        app: AppConfig,
        source_path: Path,
        *,
        version_name: str,
        version_code: str,
        source_page: str | None,
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


def download_apkmirror_candidates(
    http: HttpClient,
    resolver: ApkMirrorResolver,
    app: AppConfig,
    version: str,
    destination_dir: Path,
) -> list[tuple[DownloadResult, str, str]]:
    destination_dir.mkdir(parents=True, exist_ok=True)
    candidates = resolver.resolve_candidates(app, version)
    results: list[tuple[DownloadResult, str, str]] = []
    for index, (direct_url, source_page) in enumerate(candidates):
        destination = destination_dir / f"candidate-{index}.bin"
        try:
            result = http.download(
                direct_url,
                destination,
                max_size=1_500_000_000,
                extra_headers={"Referer": source_page},
            )
            results.append((result, source_page, version))
        except Exception:
            destination.unlink(missing_ok=True)
    if not results:
        raise AcquisitionError(f"All APKMirror variants failed to download for {app.key} {version}")
    return results


def download_apkmirror(
    http: HttpClient,
    resolver: ApkMirrorResolver,
    app: AppConfig,
    version: str | None,
    destination: Path,
) -> tuple[DownloadResult, str, str]:
    if version:
        results = download_apkmirror_candidates(http, resolver, app, version, destination.parent)
        result, source_page, resolved_version = results[0]
        if result.path != destination:
            result.path.replace(destination)
            result = DownloadResult(destination, result.size, result.sha256, result.final_url)
        return result, source_page, resolved_version
    direct_url, source_page, resolved_version = resolver.resolve_latest(app)
    result = http.download(
        direct_url,
        destination,
        max_size=1_500_000_000,
        extra_headers={"Referer": source_page},
    )
    return result, source_page, resolved_version
