from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class AppConfig:
    key: str
    name: str
    package: str
    apkmirror_url: str
    formats: tuple[str, ...]
    prefer: tuple[str, ...]
    target_abi: str = "arm64-v8a"
    patched_packages: tuple[str, ...] = ()


@dataclass(frozen=True)
class SourceConfig:
    key: str
    name: str
    patches_repo: str
    cli_repo: str
    patches_asset_regex: str
    cli_asset_regex: str
    channel: str
    scheduled: bool
    apps: tuple[str, ...]
    compatibility: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolConfig:
    android_build_tools: str


@dataclass(frozen=True)
class ReleaseAsset:
    id: int
    name: str
    api_url: str
    browser_url: str
    size: int
    digest: str | None = None


@dataclass(frozen=True)
class ReleaseInfo:
    id: int
    repo: str
    tag: str
    name: str
    prerelease: bool
    draft: bool
    published_at: str | None
    assets: tuple[ReleaseAsset, ...]


@dataclass(frozen=True)
class DownloadResult:
    path: Path
    size: int
    sha256: str
    final_url: str


@dataclass(frozen=True)
class ApkInfo:
    path: Path
    package: str
    version_name: str
    version_code: str
    split: str | None = None
    native_code: tuple[str, ...] = ()


@dataclass
class AppBuildResult:
    app: str
    status: str
    version: str | None = None
    version_code: str | None = None
    input_sha256: str | None = None
    output_path: str | None = None
    output_sha256: str | None = None
    error: str | None = None
    details: dict[str, Any] = field(default_factory=dict)
