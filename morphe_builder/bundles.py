from __future__ import annotations

import shutil
import stat
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .android import AndroidTools
from .models import ApkInfo


class BundleError(RuntimeError):
    pass


@dataclass(frozen=True)
class BundleInfo:
    base: ApkInfo
    apks: tuple[ApkInfo, ...]
    native_abis: tuple[str, ...]
    needs_arm64_strip: bool


MAX_ENTRIES = 10_000
MAX_MEMBER_SIZE = 1_000_000_000
MAX_TOTAL_SIZE = 2_000_000_000
MAX_COMPRESSION_RATIO = 200


def _validate_member(info: zipfile.ZipInfo, seen: set[str]) -> None:
    path = PurePosixPath(info.filename.replace("\\", "/"))
    normalized = str(path)
    if path.is_absolute() or ".." in path.parts or not normalized or normalized == ".":
        raise BundleError(f"Unsafe archive member: {info.filename}")
    key = normalized.casefold()
    if key in seen:
        raise BundleError(f"Duplicate archive path: {info.filename}")
    seen.add(key)
    mode = info.external_attr >> 16
    if stat.S_ISLNK(mode):
        raise BundleError(f"Archive symlink is not allowed: {info.filename}")
    if info.file_size > MAX_MEMBER_SIZE:
        raise BundleError(f"Archive member is too large: {info.filename}")
    if info.compress_size and info.file_size / info.compress_size > MAX_COMPRESSION_RATIO:
        raise BundleError(f"Suspicious compression ratio: {info.filename}")


def _extract_apks_for_inspection(bundle_path: Path, destination: Path) -> list[Path]:
    if not zipfile.is_zipfile(bundle_path):
        raise BundleError(f"Not a ZIP-based bundle: {bundle_path}")
    extracted: list[Path] = []
    seen: set[str] = set()
    total_size = 0
    with zipfile.ZipFile(bundle_path) as archive:
        infos = archive.infolist()
        if len(infos) > MAX_ENTRIES:
            raise BundleError(f"Archive has too many entries: {len(infos)}")
        for info in infos:
            _validate_member(info, seen)
            total_size += info.file_size
            if total_size > MAX_TOTAL_SIZE:
                raise BundleError("Archive uncompressed size exceeds safety limit")
            if info.is_dir() or not info.filename.lower().endswith(".apk"):
                continue
            output = destination / f"{len(extracted):04d}-{PurePosixPath(info.filename).name}"
            with archive.open(info) as source, output.open("wb") as target:
                shutil.copyfileobj(source, target, length=1024 * 1024)
            extracted.append(output)
    if not extracted:
        raise BundleError("Bundle contains no APK files")
    return extracted


def inspect_bundle(
    bundle_path: Path,
    android: AndroidTools,
    *,
    expected_package: str,
    expected_version: str | None = None,
    target_abi: str = "arm64-v8a",
) -> BundleInfo:
    """Inspect a bundle without modifying it; Morphe CLI performs its own temporary merge."""
    with tempfile.TemporaryDirectory(prefix="morphe-inspect-") as temp:
        apk_paths = _extract_apks_for_inspection(bundle_path, Path(temp))
        apks = tuple(android.inspect_apk(path) for path in apk_paths)

    packages = {apk.package for apk in apks}
    bases = [apk for apk in apks if not apk.split]
    if packages != {expected_package}:
        raise BundleError(f"Bundle package mismatch: expected {expected_package}, found {sorted(packages)}")
    if len(bases) != 1:
        raise BundleError(f"Bundle must contain exactly one base APK, found {len(bases)}")
    base = bases[0]
    version_codes = {apk.version_code for apk in apks}
    if version_codes != {base.version_code}:
        raise BundleError(f"Bundle contains multiple version codes: {sorted(version_codes)}")
    conflicting_names = {
        apk.version_name for apk in apks if apk.version_name and apk.version_name != base.version_name
    }
    if conflicting_names:
        raise BundleError(
            f"Bundle contains version names that conflict with base {base.version_name}: {sorted(conflicting_names)}"
        )
    if expected_version and base.version_name.lstrip("v") != expected_version.lstrip("v"):
        raise BundleError(
            f"Bundle version mismatch: expected {expected_version}, found {base.version_name}"
        )

    native_abis = tuple(sorted({abi for apk in apks for abi in apk.native_code}))
    if native_abis and target_abi not in native_abis:
        raise BundleError(f"Bundle has native code but no {target_abi} support: {native_abis}")
    return BundleInfo(
        base=base,
        apks=apks,
        native_abis=native_abis,
        needs_arm64_strip=bool(native_abis and set(native_abis) != {target_abi}),
    )


def inspect_source(
    source_path: Path,
    android: AndroidTools,
    *,
    expected_package: str,
    expected_version: str | None = None,
    target_abi: str = "arm64-v8a",
) -> tuple[ApkInfo, tuple[str, ...], bool]:
    if source_path.suffix.lower() in {".apkm", ".apks", ".xapk"}:
        bundle = inspect_bundle(
            source_path,
            android,
            expected_package=expected_package,
            expected_version=expected_version,
            target_abi=target_abi,
        )
        return bundle.base, bundle.native_abis, bundle.needs_arm64_strip

    info = android.validate_apk(
        source_path,
        expected_packages={expected_package},
        expected_version=expected_version,
        require_arm64=True,
    )
    native_abis = tuple(sorted(info.native_code))
    needs_strip = bool(native_abis and set(native_abis) != {target_abi})
    return info, native_abis, needs_strip
