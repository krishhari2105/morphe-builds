from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import AppConfig, SourceConfig, ToolConfig


ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = ROOT / "config"


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Config:
    apps: dict[str, AppConfig]
    sources: dict[str, SourceConfig]
    patches: dict[str, Any]
    tools: ToolConfig


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"Missing configuration file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"Configuration root must be an object: {path}")
    return data


def _required(data: dict[str, Any], key: str, context: str) -> Any:
    value = data.get(key)
    if value is None or value == "":
        raise ConfigError(f"Missing {context}.{key}")
    return value


def load_config(config_dir: Path = CONFIG_DIR) -> Config:
    apps_data = _read_json(config_dir / "apps.json")
    sources_data = _read_json(config_dir / "patch-sources.json")
    patches = _read_json(config_dir / "patches.json")
    tools_data = _read_json(config_dir / "tools.json")

    apps: dict[str, AppConfig] = {}
    packages: set[str] = set()
    for key, raw in apps_data.get("apps", {}).items():
        package = str(_required(raw, "package", f"apps.{key}"))
        if package in packages:
            raise ConfigError(f"Duplicate package: {package}")
        packages.add(package)
        formats = tuple(str(item).lower() for item in raw.get("formats", []))
        if not formats or any(item not in {"apk", "apkm", "apks", "xapk"} for item in formats):
            raise ConfigError(f"Invalid formats for apps.{key}")
        target_abi = str(raw.get("target_abi", "arm64-v8a"))
        if target_abi != "arm64-v8a":
            raise ConfigError(f"Unsupported target ABI for apps.{key}: {target_abi}")
        apps[key] = AppConfig(
            key=key,
            name=str(_required(raw, "name", f"apps.{key}")),
            package=package,
            apkmirror_url=str(_required(raw, "apkmirror_url", f"apps.{key}")),
            formats=formats,
            prefer=tuple(str(item) for item in raw.get("prefer", ["arm64-v8a", "universal"])),
            target_abi=target_abi,
            patched_packages=tuple(str(item) for item in raw.get("patched_packages", [])),
        )

    if not apps:
        raise ConfigError("No apps configured")

    sources: dict[str, SourceConfig] = {}
    for key, raw in sources_data.get("sources", {}).items():
        channel = str(raw.get("channel", "stable"))
        if channel not in {"stable", "prerelease", "latest"}:
            raise ConfigError(f"Invalid channel for sources.{key}: {channel}")
        for regex_key in ("patches_asset_regex", "cli_asset_regex"):
            try:
                re.compile(str(_required(raw, regex_key, f"sources.{key}")))
            except re.error as exc:
                raise ConfigError(f"Invalid regex sources.{key}.{regex_key}: {exc}") from exc
        source_apps = tuple(str(item) for item in raw.get("apps", ["*"]))
        unknown = set(source_apps) - set(apps) - {"*"}
        if unknown:
            raise ConfigError(f"Unknown apps in sources.{key}: {sorted(unknown)}")
        compatibility = raw.get("compatibility", {})
        if not isinstance(compatibility, dict):
            raise ConfigError(f"sources.{key}.compatibility must be an object")
        sources[key] = SourceConfig(
            key=key,
            name=str(_required(raw, "name", f"sources.{key}")),
            patches_repo=str(_required(raw, "patches_repo", f"sources.{key}")),
            cli_repo=str(_required(raw, "cli_repo", f"sources.{key}")),
            patches_asset_regex=str(raw["patches_asset_regex"]),
            cli_asset_regex=str(raw["cli_asset_regex"]),
            channel=channel,
            scheduled=bool(raw.get("scheduled", False)),
            apps=source_apps,
            compatibility=compatibility,
        )

    if not sources:
        raise ConfigError("No patch sources configured")

    tools = ToolConfig(
        android_build_tools=str(_required(tools_data, "android_build_tools", "tools")),
    )

    _validate_patch_config(patches, apps, sources)
    return Config(apps=apps, sources=sources, patches=patches, tools=tools)


def _validate_patch_list(value: Any, context: str) -> None:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise ConfigError(f"{context} must be a list of non-empty strings")


def _validate_patch_config(
    patches: dict[str, Any], apps: dict[str, AppConfig], sources: dict[str, SourceConfig]
) -> None:
    defaults = patches.get("defaults", {})
    _validate_patch_list(defaults.get("enable", []), "patches.defaults.enable")
    _validate_patch_list(defaults.get("disable", []), "patches.defaults.disable")
    for app_key, value in patches.get("apps", {}).items():
        if app_key not in apps:
            raise ConfigError(f"Unknown app in patches.apps: {app_key}")
        _validate_patch_list(value.get("enable", []), f"patches.apps.{app_key}.enable")
        _validate_patch_list(value.get("disable", []), f"patches.apps.{app_key}.disable")
    for source_key, source_value in patches.get("sources", {}).items():
        if source_key not in sources:
            raise ConfigError(f"Unknown source in patches.sources: {source_key}")
        if not isinstance(source_value, dict):
            raise ConfigError(f"patches.sources.{source_key} must be an object")
        source_defaults = source_value.get("defaults", {})
        if not isinstance(source_defaults, dict):
            raise ConfigError(f"patches.sources.{source_key}.defaults must be an object")
        _validate_patch_list(
            source_defaults.get("enable", []), f"patches.sources.{source_key}.defaults.enable"
        )
        _validate_patch_list(
            source_defaults.get("disable", []), f"patches.sources.{source_key}.defaults.disable"
        )
        source_apps = source_value.get("apps", {})
        if not isinstance(source_apps, dict):
            raise ConfigError(f"patches.sources.{source_key}.apps must be an object")
        for app_key, app_value in source_apps.items():
            if app_key not in apps:
                raise ConfigError(f"Unknown app in patches.sources.{source_key}.apps: {app_key}")
            if not isinstance(app_value, dict):
                raise ConfigError(f"patches.sources.{source_key}.apps.{app_key} must be an object")
            _validate_patch_list(
                app_value.get("enable", []),
                f"patches.sources.{source_key}.apps.{app_key}.enable",
            )
            _validate_patch_list(
                app_value.get("disable", []),
                f"patches.sources.{source_key}.apps.{app_key}.disable",
            )


def resolve_patch_selection(config: Config, source_key: str, app_key: str) -> tuple[list[str], list[str]]:
    enable: list[str] = []
    disable: list[str] = []

    def merge(raw: dict[str, Any]) -> None:
        for name in raw.get("enable", []):
            if name not in enable:
                enable.append(name)
            if name in disable:
                disable.remove(name)
        for name in raw.get("disable", []):
            if name not in disable:
                disable.append(name)
            if name in enable:
                enable.remove(name)

    merge(config.patches.get("defaults", {}))
    merge(config.patches.get("apps", {}).get(app_key, {}))
    source = config.patches.get("sources", {}).get(source_key, {})
    merge(source.get("defaults", {}))
    merge(source.get("apps", {}).get(app_key, {}))
    return enable, disable
