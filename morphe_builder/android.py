from __future__ import annotations

import os
import re
import shutil
import subprocess
import zipfile
from pathlib import Path

from .models import ApkInfo


class AndroidToolError(RuntimeError):
    pass


_PACKAGE_LINE_RE = re.compile(r"^package:\s+(.+)$", re.MULTILINE)
_ATTRIBUTE_RE = re.compile(r"([A-Za-z0-9_]+)='([^']*)'")
_NATIVE_RE = re.compile(r"native-code:\s+(.+)")
_QUOTED_RE = re.compile(r"'([^']+)'" )


class AndroidTools:
    def __init__(self, build_tools_version: str | None = None) -> None:
        self.build_tools_version = build_tools_version
        self.aapt = self._find("aapt")
        self.apksigner = self._find("apksigner")
        self.zipalign = self._find("zipalign")

    def _find(self, name: str) -> Path:
        suffix = ".bat" if os.name == "nt" and name == "apksigner" else (".exe" if os.name == "nt" else "")
        executable = name + suffix
        android_home = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT")
        candidates: list[Path] = []
        if android_home:
            build_tools = Path(android_home) / "build-tools"
            if self.build_tools_version:
                candidates.append(build_tools / self.build_tools_version / executable)
            if build_tools.exists():
                versions = sorted(
                    (path for path in build_tools.iterdir() if path.is_dir()),
                    key=lambda path: self._version_key(path.name),
                    reverse=True,
                )
                candidates.extend(path / executable for path in versions)
        discovered = shutil.which(name)
        if discovered:
            candidates.append(Path(discovered))
        for candidate in candidates:
            if candidate.exists():
                return candidate
        raise AndroidToolError(f"Android build tool not found: {name}")

    @staticmethod
    def _version_key(value: str) -> tuple[int | str, ...]:
        return tuple(int(part) if part.isdigit() else part for part in re.split(r"(\d+)", value))

    def inspect_apk(self, path: Path) -> ApkInfo:
        if not path.is_file() or not zipfile.is_zipfile(path):
            raise AndroidToolError(f"Not a readable APK ZIP: {path}")
        result = subprocess.run(
            [str(self.aapt), "dump", "badging", str(path)],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise AndroidToolError(f"aapt failed for {path.name}: {result.stderr.strip()}")
        package_match = _PACKAGE_LINE_RE.search(result.stdout)
        if not package_match:
            raise AndroidToolError(f"aapt returned no package metadata for {path.name}")
        attributes = dict(_ATTRIBUTE_RE.findall(package_match.group(1)))
        if not attributes.get("name"):
            raise AndroidToolError(f"aapt returned no package name for {path.name}")
        native_code: tuple[str, ...] = ()
        native_match = _NATIVE_RE.search(result.stdout)
        if native_match:
            native_code = tuple(_QUOTED_RE.findall(native_match.group(1)))
        return ApkInfo(
            path=path,
            package=attributes["name"],
            version_code=attributes.get("versionCode", ""),
            version_name=attributes.get("versionName", ""),
            split=attributes.get("split") or None,
            native_code=native_code,
        )

    def validate_apk(
        self,
        path: Path,
        *,
        expected_packages: set[str] | None,
        expected_version: str | None = None,
        require_arm64: bool = True,
    ) -> ApkInfo:
        with zipfile.ZipFile(path) as archive:
            bad_member = archive.testzip()
            if bad_member:
                raise AndroidToolError(f"Corrupt APK member: {bad_member}")
        info = self.inspect_apk(path)
        if expected_packages is not None and info.package not in expected_packages:
            raise AndroidToolError(
                f"Package mismatch for {path.name}: expected {sorted(expected_packages)}, got {info.package}"
            )
        if expected_version and info.version_name.lstrip("v") != expected_version.lstrip("v"):
            raise AndroidToolError(
                f"Version mismatch for {path.name}: expected {expected_version}, got {info.version_name}"
            )
        if require_arm64 and info.native_code and "arm64-v8a" not in info.native_code:
            raise AndroidToolError(f"APK has native code but no arm64-v8a support: {info.native_code}")
        return info

    def verify_signature(self, path: Path, expected_fingerprint: str | None = None) -> str:
        result = subprocess.run(
            [str(self.apksigner), "verify", "--verbose", "--print-certs", str(path)],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise AndroidToolError(f"Signature verification failed: {result.stdout}\n{result.stderr}")
        match = re.search(r"Signer #1 certificate SHA-256 digest:\s*([0-9a-fA-F]+)", result.stdout)
        fingerprint = match.group(1).lower() if match else ""
        if expected_fingerprint:
            normalized = re.sub(r"[^0-9a-fA-F]", "", expected_fingerprint).lower()
            if not fingerprint or fingerprint != normalized:
                raise AndroidToolError(
                    f"Signing certificate mismatch: expected {normalized}, got {fingerprint or 'unknown'}"
                )
        return fingerprint

    def align_and_sign(
        self,
        unsigned_path: Path,
        output_path: Path,
        *,
        keystore_path: Path,
        alias: str,
        keystore_type: str,
        keystore_password: str,
        key_password: str,
    ) -> None:
        aligned_path = output_path.with_name(f".{output_path.name}.aligned.apk")
        aligned_path.unlink(missing_ok=True)
        output_path.unlink(missing_ok=True)
        try:
            align = subprocess.run(
                [str(self.zipalign), "-f", "-P", "16", "4", str(unsigned_path), str(aligned_path)],
                check=False,
                capture_output=True,
                text=True,
            )
            if align.returncode != 0:
                raise AndroidToolError(f"zipalign failed: {align.stdout}\n{align.stderr}")
            signing_env = os.environ.copy()
            signing_env["MORPHE_APKSIGNER_STORE_PASS"] = keystore_password
            signing_env["MORPHE_APKSIGNER_KEY_PASS"] = key_password
            sign = subprocess.run(
                [
                    str(self.apksigner),
                    "sign",
                    "--ks",
                    str(keystore_path),
                    "--ks-key-alias",
                    alias,
                    "--ks-type",
                    keystore_type,
                    "--ks-pass",
                    "env:MORPHE_APKSIGNER_STORE_PASS",
                    "--key-pass",
                    "env:MORPHE_APKSIGNER_KEY_PASS",
                    "--v4-signing-enabled",
                    "false",
                    "--out",
                    str(output_path),
                    str(aligned_path),
                ],
                check=False,
                capture_output=True,
                text=True,
                env=signing_env,
            )
            if sign.returncode != 0 or not output_path.is_file():
                raise AndroidToolError(f"apksigner failed: {sign.stdout}\n{sign.stderr}")
        finally:
            aligned_path.unlink(missing_ok=True)

    def check_alignment(self, path: Path) -> None:
        result = subprocess.run(
            [str(self.zipalign), "-c", "-P", "16", "4", str(path)],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise AndroidToolError(f"zipalign verification failed: {result.stdout}\n{result.stderr}")
