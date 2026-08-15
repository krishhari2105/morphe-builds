from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .acquisition import (
    AcquisitionError,
    BaseCache,
    download_apkmirror_candidates,
    download_apkmirror_latest_candidates,
    download_manual,
    rename_detected,
)
from .android import AndroidTools
from .apkmirror import ApkMirrorRateLimited, ApkMirrorResolver
from .bundles import inspect_source
from .config import Config
from .github import GitHubClient
from .http import HttpClient
from .manifest import build_tag, sha256_file, slug, write_checksums, write_manifest
from .models import ApkInfo, AppBuildResult, AppConfig, ReleaseInfo, SourceConfig
from .patching import list_versions, patch_unsigned, prepare_keystore, resolve_tools
from .versions import normalize_version, version_key


class PipelineError(RuntimeError):
    pass


class Builder:
    def __init__(self, config: Config, github: GitHubClient | None = None) -> None:
        self.config = config
        self.github = github or GitHubClient()
        self.http = HttpClient()
        self.apkmirror = ApkMirrorResolver()
        self.android = AndroidTools(config.tools.android_build_tools)
        self.cache = BaseCache(Path(os.environ.get("BASE_CACHE_DIR", ".cache/bases")))

    def prepare(
        self,
        source_key: str,
        apps: list[str],
        version_overrides: dict[str, str],
        base_urls: dict[str, str],
        base_url: str | None = None,
    ) -> dict[str, Any]:
        if source_key not in self.config.sources:
            raise PipelineError(f"Unknown patch source: {source_key}")
        source = self.config.sources[source_key]
        selected_apps = self._select_apps(source, apps)
        base_urls = self._merge_base_url(selected_apps, base_urls, base_url)

        work_dir = Path(".work") / source_key
        staging_dir = Path("staging") / source_key
        artifact_dir = Path("artifacts") / source_key
        shutil.rmtree(work_dir, ignore_errors=True)
        shutil.rmtree(staging_dir, ignore_errors=True)
        shutil.rmtree(artifact_dir, ignore_errors=True)
        work_dir.mkdir(parents=True)
        staging_dir.mkdir(parents=True)
        artifact_dir.mkdir(parents=True)

        tools = resolve_tools(self.github, source, Path(".cache/tools"))
        compatible = list_versions(tools)
        results: list[AppBuildResult] = []
        base_plan: list[dict[str, Any]] = []

        for app_key in selected_apps:
            app = self.config.apps[app_key]
            try:
                candidate_versions = self._candidate_versions(
                    app,
                    compatible.get(app.package, []),
                    version_overrides.get(app_key),
                )
                source_path, base_info, native_abis, strip_to_arm64, base_meta = self._acquire_base(
                    app,
                    candidate_versions,
                    base_urls.get(app_key),
                    work_dir / app_key,
                )
                arm64_suffix = "-arm64" if native_abis and (strip_to_arm64 or set(native_abis) == {"arm64-v8a"}) else ""
                patch_input = work_dir / app_key / f"patch-input{source_path.suffix.lower()}"
                shutil.copy2(source_path, patch_input)
                if sha256_file(patch_input) != sha256_file(source_path):
                    raise PipelineError(f"Failed to create an exact disposable source copy for {app_key}")

                unsigned_path = staging_dir / f"{app.key}-unsigned.apk"
                final_filename = f"{app.key}-{source.key}-v{slug(base_info.version_name)}{arm64_suffix}.apk"
                result_path = artifact_dir / f"{app.key}-patch-result.json"
                output_info, output_sha256 = patch_unsigned(
                    self.config,
                    source,
                    app_key,
                    patch_input,
                    unsigned_path,
                    result_path,
                    tools,
                    self.android,
                    version_name=base_info.version_name,
                    allowed_output_packages={app.package, *app.patched_packages},
                    strip_to_arm64=strip_to_arm64,
                )
                base_sha256 = sha256_file(source_path)
                base_entry: dict[str, Any] = {
                    "app": app_key,
                    "package": base_info.package,
                    "version_name": base_info.version_name,
                    "version_code": base_info.version_code,
                    "sha256": base_sha256,
                    "format": source_path.suffix.lower().lstrip("."),
                    "native_abis": list(native_abis),
                    "striplibs": ["arm64-v8a"] if strip_to_arm64 else [],
                    "source_page": base_meta.get("source_page"),
                }
                base_plan.append(base_entry)
                results.append(
                    AppBuildResult(
                        app=app_key,
                        status="success",
                        version=base_info.version_name,
                        version_code=base_info.version_code,
                        input_sha256=base_sha256,
                        output_path=str(unsigned_path),
                        output_sha256=output_sha256,
                        details={
                            "output_package": output_info.package,
                            "native_abis": list(output_info.native_code),
                            "patch_result": str(result_path),
                            "final_filename": final_filename,
                        },
                    )
                )
            except Exception as exc:
                print(f"ERROR [{app_key}]: {exc}", file=sys.stderr, flush=True)
                results.append(AppBuildResult(app=app_key, status="failed", error=str(exc)))

        state = {
            "schema_version": 1,
            "source": source.key,
            "apps": selected_apps,
            "patch_release": self._release_manifest(
                tools.patches_release, tools.patches_asset.name, tools.patches_sha256
            ),
            "cli_release": self._release_manifest(tools.cli_release, tools.cli_asset.name, tools.cli_sha256),
            "target_abi": "arm64-v8a",
            "bases": sorted(base_plan, key=lambda item: item["app"]),
            "results": [asdict(result) for result in results],
        }
        state_path = staging_dir / "build-state.json"
        write_manifest(state_path, state)
        self._write_step_summary(source, results, "unsigned preparation", base_urls)

        failures = [result for result in results if result.status != "success"]
        if failures:
            failed_names = ", ".join(result.app for result in failures)
            raise PipelineError(f"Preparation failed for {failed_names}; signing was not started")
        return state

    def finalize(self, source_key: str, *, publish: bool) -> dict[str, Any]:
        if source_key not in self.config.sources:
            raise PipelineError(f"Unknown patch source: {source_key}")
        source = self.config.sources[source_key]
        staging_dir = Path("staging") / source_key
        state_path = staging_dir / "build-state.json"
        if not state_path.is_file():
            raise PipelineError(f"Prepared build state not found: {state_path}")
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if state.get("source") != source_key:
            raise PipelineError(f"Prepared source mismatch: expected {source_key}, got {state.get('source')}")
        declared_apps = state.get("apps", [])
        raw_results = state.get("results", [])
        if not isinstance(declared_apps, list) or len(declared_apps) != len(set(declared_apps)):
            raise PipelineError("Prepared app set is invalid or contains duplicates")
        result_apps = [raw.get("app") for raw in raw_results if isinstance(raw, dict)]
        if len(result_apps) != len(raw_results) or len(result_apps) != len(set(result_apps)):
            raise PipelineError("Prepared results are invalid or contain duplicate apps")
        if set(result_apps) != set(declared_apps):
            raise PipelineError(
                f"Prepared results do not exactly match declared apps: {declared_apps} vs {result_apps}"
            )
        raw_bases = state.get("bases", [])
        base_apps = [raw.get("app") for raw in raw_bases if isinstance(raw, dict)]
        if len(base_apps) != len(raw_bases) or len(base_apps) != len(set(base_apps)):
            raise PipelineError("Prepared base metadata is invalid or contains duplicate apps")
        if set(base_apps) != set(declared_apps):
            raise PipelineError(f"Prepared bases do not exactly match declared apps: {declared_apps} vs {base_apps}")
        expected_apps = self._select_apps(source, list(declared_apps))
        if expected_apps != declared_apps:
            raise PipelineError(f"Prepared app order or source scope is invalid: {declared_apps}")
        self._verify_prepared_tools(state)

        work_dir = Path(".work") / f"sign-{source_key}"
        dist_dir = Path("dist") / source_key
        artifact_dir = Path("artifacts") / source_key
        shutil.rmtree(work_dir, ignore_errors=True)
        shutil.rmtree(dist_dir, ignore_errors=True)
        work_dir.mkdir(parents=True)
        dist_dir.mkdir(parents=True)
        artifact_dir.mkdir(parents=True, exist_ok=True)

        keystore_path = prepare_keystore(work_dir)
        decoded_keystore = bool(os.environ.get("SIGNING_KEYSTORE_B64")) and not os.environ.get("SIGNING_KEYSTORE_PATH")
        finalized_results: list[AppBuildResult] = []
        try:
            for raw in state.get("results", []):
                result = AppBuildResult(**raw)
                if result.status != "success" or not result.output_path:
                    raise PipelineError(f"Prepared result is not signable: {result.app}")
                if result.app not in state.get("apps", []) or result.app not in self.config.apps:
                    raise PipelineError(f"Prepared result contains an unexpected app: {result.app}")
                app = self.config.apps[result.app]
                unsigned_path = Path(result.output_path)
                try:
                    unsigned_path.resolve().relative_to(staging_dir.resolve())
                except ValueError as exc:
                    raise PipelineError(f"Prepared APK path escapes staging: {unsigned_path}") from exc
                if not unsigned_path.is_file():
                    raise PipelineError(f"Prepared unsigned APK is missing: {unsigned_path}")
                if sha256_file(unsigned_path) != result.output_sha256:
                    raise PipelineError(f"Prepared unsigned APK digest mismatch: {result.app}")

                final_filename = str(result.details["final_filename"])
                if Path(final_filename).name != final_filename:
                    raise PipelineError(f"Prepared output filename is unsafe: {final_filename}")
                output_path = dist_dir / final_filename
                self.android.align_and_sign(
                    unsigned_path,
                    output_path,
                    keystore_path=keystore_path,
                    alias=os.environ.get("SIGNING_KEY_ALIAS") or "Morphe",
                    keystore_type=os.environ.get("SIGNING_KEYSTORE_TYPE") or "BKS",
                    keystore_password=os.environ.get("SIGNING_KEYSTORE_PASSWORD", ""),
                    key_password=os.environ.get("SIGNING_KEY_PASSWORD") or "Morphe",
                )
                info = self.android.validate_apk(
                    output_path,
                    expected_packages={app.package, *app.patched_packages},
                    expected_version=result.version,
                    require_arm64=True,
                )
                self.android.check_alignment(output_path)
                page_aligned_16k = self.android.check_16k_page_alignment(output_path)
                fingerprint = self.android.verify_signature(output_path, os.environ.get("SIGNING_CERT_SHA256"))
                result.output_path = str(output_path)
                result.output_sha256 = sha256_file(output_path)
                result.details = {
                    **result.details,
                    "output_package": info.package,
                    "native_abis": list(info.native_code),
                    "signing_certificate_sha256": fingerprint,
                    "page_aligned_16k": page_aligned_16k,
                }
                finalized_results.append(result)
        finally:
            if decoded_keystore:
                keystore_path.unlink(missing_ok=True)

        plan = self._build_plan(state, finalized_results)
        tag = build_tag(
            source.key,
            str(state["patch_release"]["tag"]),
            str(state["cli_release"]["tag"]),
            plan,
        )
        manifest = {
            "schema_version": 1,
            "tag": tag,
            "source": source.key,
            "patch_release": state["patch_release"],
            "cli_release": state["cli_release"],
            "target_abi": "arm64-v8a",
            "plan": plan,
            "results": [asdict(result) for result in finalized_results],
        }
        manifest_path = artifact_dir / "build-manifest.json"
        write_manifest(manifest_path, manifest)
        outputs = [Path(result.output_path) for result in finalized_results if result.output_path]
        checksums_path = artifact_dir / "SHA256SUMS"
        write_checksums(checksums_path, [*outputs, manifest_path])
        self._write_step_summary(source, finalized_results, tag, {})

        if publish:
            release = self.github.publish_release(
                tag,
                f"{source.name}: {state['patch_release']['tag']}",
                self._release_body(source, state, finalized_results),
                [*outputs, manifest_path, checksums_path],
            )
            if source_key in {"morphe", "morphe-dev"}:
                self.github.prune_build_releases(source_key, release, self.config.sources)
        return manifest

    def _acquire_base(
        self,
        app: AppConfig,
        candidate_versions: list[str | None],
        manual_url: str | None,
        app_work_dir: Path,
    ) -> tuple[Path, ApkInfo, tuple[str, ...], bool, dict[str, Any]]:
        app_work_dir.mkdir(parents=True, exist_ok=True)
        errors: list[str] = []

        if manual_url:
            temp_path = app_work_dir / "manual-base-download.bin"
            try:
                download_manual(self.http, manual_url, temp_path)
                detected_path = rename_detected(temp_path)
                info, native_abis, strip = inspect_source(
                    detected_path,
                    self.android,
                    expected_package=app.package,
                    expected_version=None,
                    target_abi=app.target_abi,
                )
                allowed_versions = {normalize_version(value) for value in candidate_versions if value}
                if allowed_versions and normalize_version(info.version_name) not in allowed_versions:
                    raise AcquisitionError(
                        f"Manual source version {info.version_name} is not compatible; "
                        f"expected one of {sorted(allowed_versions)}"
                    )
                cached = self.cache.store(
                    app,
                    detected_path,
                    version_name=info.version_name,
                    version_code=info.version_code,
                    source_page=None,
                )
                return (
                    cached.path,
                    info,
                    native_abis,
                    strip,
                    {
                        "acquisition": "manual-url",
                        "source_page": None,
                    },
                )
            except Exception as exc:
                errors.append(f"manual-url: {exc}")
                temp_path.unlink(missing_ok=True)

        for candidate in candidate_versions:
            if candidate is None:
                rate_limit_error: ApkMirrorRateLimited | None = None
                try:
                    downloads = download_apkmirror_latest_candidates(
                        self.apkmirror, app, app_work_dir / "latest-variants"
                    )
                    latest_errors: list[str] = []
                    for result, source_page, resolved_version in downloads:
                        try:
                            detected_path = rename_detected(result.path)
                            info, native_abis, strip = inspect_source(
                                detected_path,
                                self.android,
                                expected_package=app.package,
                                expected_version=resolved_version,
                                target_abi=app.target_abi,
                            )
                            cached = self.cache.store(
                                app,
                                detected_path,
                                version_name=info.version_name,
                                version_code=info.version_code,
                                source_page=source_page,
                                final_url=result.final_url,
                                requested_version="Any",
                                resolution_mode="latest",
                            )
                            return (
                                cached.path,
                                info,
                                native_abis,
                                strip,
                                {
                                    "acquisition": "apkmirror-latest",
                                    "source_page": source_page,
                                    "final_url": result.final_url,
                                    "requested_version": "Any",
                                    "resolved_version": info.version_name,
                                },
                            )
                        except Exception as exc:
                            latest_errors.append(str(exc))
                            result.path.unlink(missing_ok=True)
                    errors.append(f"apkmirror latest: {'; '.join(latest_errors[:4])}")
                except ApkMirrorRateLimited as exc:
                    rate_limit_error = exc
                    errors.append(f"apkmirror latest: {exc}")
                except Exception as exc:
                    errors.append(f"apkmirror latest: {exc}")

                latest_cached = self.cache.find_latest(app)
                if latest_cached:
                    try:
                        info, native_abis, strip = inspect_source(
                            latest_cached.path,
                            self.android,
                            expected_package=app.package,
                            expected_version=latest_cached.version_name,
                            target_abi=app.target_abi,
                        )
                        return (
                            latest_cached.path,
                            info,
                            native_abis,
                            strip,
                            {
                                "acquisition": "actions-cache-latest-fallback",
                                "source_page": latest_cached.source_page,
                                "final_url": latest_cached.final_url,
                                "requested_version": "Any",
                                "resolved_version": info.version_name,
                            },
                        )
                    except Exception as exc:
                        errors.append(f"cache latest fallback: {exc}")
                if rate_limit_error:
                    raise AcquisitionError(str(rate_limit_error)) from rate_limit_error
                continue

            exact_cached = self.cache.find(app, candidate)
            if exact_cached:
                try:
                    info, native_abis, strip = inspect_source(
                        exact_cached.path,
                        self.android,
                        expected_package=app.package,
                        expected_version=candidate,
                        target_abi=app.target_abi,
                    )
                    return (
                        exact_cached.path,
                        info,
                        native_abis,
                        strip,
                        {
                            "acquisition": "actions-cache",
                            "source_page": exact_cached.source_page,
                        },
                    )
                except Exception as exc:
                    errors.append(f"cache {candidate or 'latest'}: {exc}")

            attempts: list[tuple[str, Any]] = [("apkmirror", None)]

            for method, _ in attempts:
                temp_path = app_work_dir / "base-download.bin"
                temp_path.unlink(missing_ok=True)
                try:
                    downloads = download_apkmirror_candidates(self.apkmirror, app, candidate, app_work_dir / "variants")
                    variant_errors: list[str] = []
                    for result, source_page, resolved_version in downloads:
                        try:
                            detected_path = rename_detected(result.path)
                            info, native_abis, strip = inspect_source(
                                detected_path,
                                self.android,
                                expected_package=app.package,
                                expected_version=resolved_version,
                                target_abi=app.target_abi,
                            )
                            cached = self.cache.store(
                                app,
                                detected_path,
                                version_name=info.version_name,
                                version_code=info.version_code,
                                source_page=source_page,
                            )
                            return (
                                cached.path,
                                info,
                                native_abis,
                                strip,
                                {
                                    "acquisition": method,
                                    "source_page": source_page,
                                },
                            )
                        except Exception as exc:
                            variant_errors.append(str(exc))
                            result.path.unlink(missing_ok=True)
                    errors.append(f"{method} {candidate or 'latest'}: {'; '.join(variant_errors[:4])}")
                except ApkMirrorRateLimited as exc:
                    raise AcquisitionError(str(exc)) from exc
                except Exception as exc:
                    errors.append(f"{method} {candidate or 'latest'}: {exc}")
                temp_path.unlink(missing_ok=True)

        detail = "; ".join(errors[-8:])
        raise AcquisitionError(
            f"Could not acquire a validated base for {app.key}. {detail}. "
            f'Rerun with base_urls JSON such as {{"{app.key}": "https://..."}}'
        )

    @staticmethod
    def _merge_base_url(
        selected_apps: list[str],
        base_urls: dict[str, str],
        base_url: str | None,
    ) -> dict[str, str]:
        if not base_url:
            return base_urls
        if len(selected_apps) != 1:
            raise PipelineError(
                "A single base URL requires exactly one selected app; choose one app or use base_urls JSON"
            )
        return {selected_apps[0]: base_url, **base_urls}

    @staticmethod
    def _candidate_versions(app: AppConfig, compatible: list[str], override: str | None) -> list[str | None]:
        if override and override.lower() != "auto":
            return [normalize_version(override)]
        numeric: set[str] = {normalize_version(value) for value in compatible if value.lower() != "any"}
        if numeric:
            sorted_numeric: list[str | None] = [*sorted(numeric, key=version_key, reverse=True)]
            return sorted_numeric
        if any(value.lower() == "any" for value in compatible):
            return [None]
        raise PipelineError(f"{app.name} is not compatible with the selected patch source")

    def _select_apps(self, source: SourceConfig, requested: list[str]) -> list[str]:
        allowed = set(self.config.apps) if source.apps == ("*",) else set(source.apps)
        if requested == ["all"]:
            selected = [app for app in self.config.apps if app in allowed]
        else:
            unknown = set(requested) - set(self.config.apps)
            if unknown:
                raise PipelineError(f"Unknown apps: {sorted(unknown)}")
            unsupported = set(requested) - allowed
            if unsupported:
                raise PipelineError(f"Apps not configured for {source.key}: {sorted(unsupported)}")
            selected = [app for app in self.config.apps if app in set(requested)]
        if not selected:
            raise PipelineError(f"No apps selected for {source.key}")
        return selected

    def _verify_prepared_tools(self, state: dict[str, Any]) -> None:
        for key in ("patch_release", "cli_release"):
            raw = state.get(key)
            if not isinstance(raw, dict):
                raise PipelineError(f"Prepared {key} metadata is missing")
            release = self.github.get_release_by_tag(str(raw.get("tag", "")), str(raw.get("repo", "")))
            if release is None or release.id != raw.get("id"):
                raise PipelineError(f"Prepared {key} no longer matches its GitHub release")
            matches = [asset for asset in release.assets if asset.name == raw.get("asset")]
            if len(matches) != 1:
                raise PipelineError(f"Prepared {key} asset is missing or ambiguous")
            github_digest = (matches[0].digest or "").removeprefix("sha256:").lower()
            if github_digest and github_digest != str(raw.get("sha256", "")).lower():
                raise PipelineError(f"Prepared {key} digest does not match GitHub")

    def _build_plan(self, state: dict[str, Any], results: list[AppBuildResult]) -> dict[str, Any]:
        fingerprints = sorted(
            {
                str(result.details.get("signing_certificate_sha256"))
                for result in results
                if result.details.get("signing_certificate_sha256")
            }
        )
        return {
            "source": state["source"],
            "patch_release": state["patch_release"]["tag"],
            "patch_sha256": state["patch_release"]["sha256"],
            "cli_release": state["cli_release"]["tag"],
            "cli_sha256": state["cli_release"]["sha256"],
            "apps": state["apps"],
            "bases": state["bases"],
            "patch_config": self.config.patches,
            "target_abi": "arm64-v8a",
            "signing_certificate_sha256": fingerprints,
            "builder_schema": 2,
        }

    @staticmethod
    def _release_manifest(release: ReleaseInfo, asset_name: str, digest: str) -> dict[str, Any]:
        return {
            "repo": release.repo,
            "id": release.id,
            "tag": release.tag,
            "asset": asset_name,
            "sha256": digest,
        }

    @staticmethod
    def _workflow_run_url() -> str | None:
        repository = os.environ.get("GITHUB_REPOSITORY")
        run_id = os.environ.get("GITHUB_RUN_ID")
        return f"https://github.com/{repository}/actions/runs/{run_id}" if repository and run_id else None

    @staticmethod
    def _release_body(source: SourceConfig, state: dict[str, Any], results: list[AppBuildResult]) -> str:
        lines = [
            f"**Patch source:** {source.name} (`{state['patch_release']['tag']}`)",
            f"**Morphe CLI:** `{state['cli_release']['tag']}`",
            "**Target:** arm64-v8a family devices",
            "",
            "| App | Version | SHA-256 |",
            "|---|---:|---|",
        ]
        for result in results:
            lines.append(f"| {result.app} | {result.version} | `{result.output_sha256}` |")
        workflow_run = Builder._workflow_run_url()
        if workflow_run:
            lines.append(f"\n**Workflow run:** {workflow_run}")
        lines.append("\nRaw source APK/APKM files are not published.")
        return "\n".join(lines)

    @staticmethod
    def _write_step_summary(
        source: SourceConfig,
        results: list[AppBuildResult],
        tag: str,
        base_urls: dict[str, str],
    ) -> None:
        summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
        if not summary_path:
            return
        lines = [
            f"## {source.name}",
            "",
            f"Build identity: `{tag}`",
            "",
            "| App | Status | Version | Details |",
            "|---|---|---|---|",
        ]
        for result in results:
            details = result.error or result.output_sha256 or ""
            lines.append(f"| {result.app} | {result.status} | {result.version or ''} | {details} |")
        failed = [result.app for result in results if result.status != "success"]
        if failed:
            example = dict(base_urls)
            for app in failed:
                example.setdefault(app, "https://APKMirror-direct-download-link")
            lines.extend(
                [
                    "",
                    "### Manual fallback",
                    "",
                    "Rerun the workflow with `base_urls`:",
                    "",
                    f"```json\n{json.dumps(example, indent=2)}\n```",
                ]
            )
        with Path(summary_path).open("a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
