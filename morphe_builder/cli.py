from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .config import Config, ConfigError, load_config
from .github import GitHubClient
from .manifest import app_set_id, slug
from .models import ReleaseInfo
from .patching import list_versions, resolve_tools
from .pipeline import Builder, PipelineError


def _json_object(value: str, name: str) -> dict[str, str]:
    try:
        parsed = json.loads(value or "{}")
    except json.JSONDecodeError as exc:
        raise argparse.ArgumentTypeError(f"{name} must be valid JSON: {exc}") from exc
    if not isinstance(parsed, dict) or any(
        not isinstance(key, str) or not isinstance(item, str) for key, item in parsed.items()
    ):
        raise argparse.ArgumentTypeError(f"{name} must be a JSON object of string keys and values")
    return parsed


def _csv(value: str) -> list[str]:
    items = [item.strip() for item in value.split(",") if item.strip()]
    return items or ["all"]


def _write_output(name: str, value: str) -> None:
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with Path(output).open("a", encoding="utf-8") as handle:
            handle.write(f"{name}={value}\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="morphe-builder")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("validate-config", help="Validate repository configuration")

    matrix = subparsers.add_parser("matrix", help="Create a source matrix")
    matrix.add_argument("--sources", default="morphe")

    versions = subparsers.add_parser("list-versions", help="Print compatible app versions")
    versions.add_argument("--source", required=True)

    upstreams = subparsers.add_parser("check-upstreams", help="Find scheduled sources with new releases")
    upstreams.add_argument("--sources", default="scheduled")

    prepare = subparsers.add_parser("prepare", help="Patch unsigned APKs without signing secrets")
    prepare.add_argument("--source", required=True)
    prepare.add_argument("--apps", default="all")
    prepare.add_argument("--version-overrides", default="{}")
    prepare.add_argument("--base-urls", default="{}")
    prepare.add_argument("--base-url", default="")

    finalize = subparsers.add_parser("finalize", help="Sign, verify, and optionally publish prepared APKs")
    finalize.add_argument("--source", required=True)
    finalize.add_argument("--publish", action=argparse.BooleanOptionalAction, default=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = load_config()
        if args.command == "validate-config":
            print(f"Configuration valid: {len(config.apps)} apps, {len(config.sources)} sources")
            return 0

        if args.command == "matrix":
            sources = _resolve_sources(config, args.sources)
            value = json.dumps(sources, separators=(",", ":"))
            print(value)
            _write_output("sources", value)
            return 0

        github = GitHubClient()
        if args.command == "list-versions":
            source = config.sources.get(args.source)
            if not source:
                raise PipelineError(f"Unknown source: {args.source}")
            tools = resolve_tools(github, source, Path(".cache/tools"))
            print(json.dumps(list_versions(tools), indent=2, sort_keys=True))
            return 0

        if args.command == "check-upstreams":
            candidates = _resolve_sources(config, args.sources)
            current_releases = github.list_releases(github.repository, max_pages=10) if github.repository else []
            changed: list[str] = []
            for source_key in candidates:
                source = config.sources[source_key]
                try:
                    patches_release, _, cli_release, _ = github.resolve_source(source)
                except Exception as exc:
                    print(f"Skipping {source_key}: {exc}", file=sys.stderr)
                    continue
                scheduled_apps = list(config.apps) if source.apps == ("*",) else list(source.apps)
                prefix = (
                    f"build-{slug(source.key)}-{slug(patches_release.tag)}-"
                    f"{slug(cli_release.tag)}-apps-{app_set_id(scheduled_apps)}-"
                )
                if not any(
                    release.tag.startswith(prefix) and _release_is_complete(release) for release in current_releases
                ):
                    changed.append(source_key)
            value = json.dumps(changed, separators=(",", ":"))
            print(value)
            _write_output("sources", value)
            _write_output("sources_csv", ",".join(changed))
            _write_output("has_changes", "true" if changed else "false")
            return 0

        if args.command == "prepare":
            versions = _json_object(args.version_overrides, "version-overrides")
            base_urls = _json_object(args.base_urls, "base-urls")
            state = Builder(config, github).prepare(
                args.source,
                _csv(args.apps),
                versions,
                base_urls,
                args.base_url or None,
            )
            print(json.dumps({"source": state["source"], "apps": state["apps"]}, indent=2))
            return 0

        if args.command == "finalize":
            manifest = Builder(config, github).finalize(args.source, publish=args.publish)
            print(json.dumps({"tag": manifest["tag"], "source": manifest["source"]}, indent=2))
            return 0
    except (ConfigError, PipelineError, ValueError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 2


def _resolve_sources(config: Config, value: str) -> list[str]:
    if value == "scheduled":
        return [key for key, source in config.sources.items() if source.scheduled]
    if value == "all":
        return list(config.sources)
    sources = _csv(value)
    unknown = set(sources) - set(config.sources)
    if unknown:
        raise PipelineError(f"Unknown sources: {sorted(unknown)}")
    return sources


def _release_is_complete(release: ReleaseInfo) -> bool:
    names = {asset.name for asset in release.assets}
    return "build-manifest.json" in names and "SHA256SUMS" in names and any(name.endswith(".apk") for name in names)
