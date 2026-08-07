from __future__ import annotations

import base64
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .android import AndroidTools
from .config import Config, resolve_patch_selection
from .github import GitHubClient
from .manifest import sha256_file
from .models import ApkInfo, ReleaseAsset, ReleaseInfo, SourceConfig
from .versions import parse_compatible_versions


class PatchingError(RuntimeError):
    pass


@dataclass(frozen=True)
class ResolvedTools:
    patches_release: ReleaseInfo
    patches_asset: ReleaseAsset
    patches_path: Path
    patches_sha256: str
    cli_release: ReleaseInfo
    cli_asset: ReleaseAsset
    cli_path: Path
    cli_sha256: str


def resolve_tools(github: GitHubClient, source: SourceConfig, tools_dir: Path) -> ResolvedTools:
    patches_release, patches_asset, cli_release, cli_asset = github.resolve_source(source)
    tools_dir.mkdir(parents=True, exist_ok=True)
    patches_path = tools_dir / patches_asset.name
    cli_path = tools_dir / cli_asset.name
    if not patches_path.exists() or sha256_file(patches_path) != (patches_asset.digest or "").removeprefix("sha256:"):
        github.download_asset(patches_asset, patches_path)
    if not cli_path.exists() or sha256_file(cli_path) != (cli_asset.digest or "").removeprefix("sha256:"):
        github.download_asset(cli_asset, cli_path)
    return ResolvedTools(
        patches_release=patches_release,
        patches_asset=patches_asset,
        patches_path=patches_path,
        patches_sha256=sha256_file(patches_path),
        cli_release=cli_release,
        cli_asset=cli_asset,
        cli_path=cli_path,
        cli_sha256=sha256_file(cli_path),
    )


def list_versions(tools: ResolvedTools) -> dict[str, list[str]]:
    result = subprocess.run(
        ["java", "-jar", str(tools.cli_path), "list-versions", f"--patches={tools.patches_path}"],
        check=False,
        capture_output=True,
        text=True,
        env=_sanitized_morphe_env(),
    )
    if result.returncode != 0:
        raise PatchingError(f"Morphe list-versions failed:\n{result.stdout}\n{result.stderr}")
    output = "\n".join(part for part in (result.stdout, result.stderr) if part)
    return parse_compatible_versions(output)


def prepare_keystore(work_dir: Path) -> Path:
    configured_path = os.environ.get("SIGNING_KEYSTORE_PATH")
    if configured_path:
        path = Path(configured_path)
        if not path.is_file():
            raise PatchingError(f"SIGNING_KEYSTORE_PATH does not exist: {path}")
        return path

    encoded = os.environ.get("SIGNING_KEYSTORE_B64")
    if encoded:
        path = work_dir / "signing-keystore.bks"
        try:
            path.write_bytes(base64.b64decode(encoded, validate=True))
        except ValueError as exc:
            raise PatchingError("SIGNING_KEYSTORE_B64 is not valid base64") from exc
        return path

    raise PatchingError("No signing keystore configured; set SIGNING_KEYSTORE_B64 or SIGNING_KEYSTORE_PATH")


def _sanitized_morphe_env() -> dict[str, str]:
    blocked = ("TOKEN", "SECRET", "PASSWORD", "KEYSTORE", "ACTIONS_RUNTIME", "ACTIONS_ID_TOKEN")
    return {
        key: value
        for key, value in os.environ.items()
        if not any(marker in key.upper() for marker in blocked)
    }


def patch_unsigned(
    config: Config,
    source: SourceConfig,
    app_key: str,
    source_path: Path,
    output_path: Path,
    result_path: Path,
    tools: ResolvedTools,
    android: AndroidTools,
    *,
    version_name: str,
    allowed_output_packages: set[str],
    strip_to_arm64: bool,
) -> tuple[ApkInfo, str]:
    enable, disable = resolve_patch_selection(config, source.key, app_key)
    command = [
        "java",
        "-jar",
        str(tools.cli_path),
        "patch",
        "-o",
        str(output_path),
        "--result-file",
        str(result_path),
    ]
    for patch in enable:
        command.extend(["-e", patch])
    for patch in disable:
        command.extend(["-d", patch])
    command.append(f"--patches={tools.patches_path}")
    command.append("--unsigned")
    if strip_to_arm64:
        command.append("--striplibs=arm64-v8a")
    command.append(str(source_path))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.unlink(missing_ok=True)
    input_sha256 = sha256_file(source_path)
    result = subprocess.run(command, check=False, text=True, env=_sanitized_morphe_env())
    if sha256_file(source_path) != input_sha256:
        raise PatchingError(f"Morphe modified the source input for {app_key}; refusing the output")
    if result.returncode != 0 or not output_path.is_file():
        raise PatchingError(f"Morphe patch command failed for {app_key} with exit code {result.returncode}")

    info = android.validate_apk(
        output_path,
        expected_packages=allowed_output_packages,
        expected_version=version_name,
        require_arm64=True,
    )
    return info, sha256_file(output_path)
