from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
API = "https://api.github.com"


def request_json(url: str, token: str) -> object:
    request = Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urlopen(request, timeout=30) as response:
        return json.load(response)


def download_json(url: str, token: str) -> dict:
    request = Request(
        url,
        headers={
            "Accept": "application/octet-stream",
            "Authorization": f"Bearer {token}",
        },
    )
    with urlopen(request, timeout=30) as response:
        return json.load(response)


def main() -> int:
    token = os.environ.get("GITHUB_TOKEN")
    repository = os.environ.get("GITHUB_REPOSITORY")
    if not token or not repository:
        print("GITHUB_TOKEN and GITHUB_REPOSITORY are required", file=sys.stderr)
        return 1

    apps = json.loads((ROOT / "config" / "apps.json").read_text(encoding="utf-8"))["apps"]
    catalog = json.loads((ROOT / "config" / "catalog.json").read_text(encoding="utf-8"))
    visible = {key: value for key, value in catalog["apps"].items() if value.get("visible")}
    releases = request_json(f"{API}/repos/{repository}/releases?per_page=100", token)
    candidates_by_app: dict[str, list[dict]] = {}

    for release in releases if isinstance(releases, list) else []:
        if release.get("draft") or not release.get("published_at"):
            continue
        assets = {asset["name"]: asset for asset in release.get("assets", [])}
        manifest_asset = assets.get("build-manifest.json")
        if not manifest_asset:
            continue
        try:
            manifest = download_json(manifest_asset["browser_download_url"], token)
        except Exception as exc:
            print(f"Skipping {release.get('tag_name')}: {exc}", file=sys.stderr)
            continue
        for result in manifest.get("results", []):
            app_key = result.get("app")
            if app_key not in visible or result.get("status", "success") not in {"success", "ok"}:
                continue
            filename = result.get("details", {}).get("final_filename")
            if not filename and result.get("output_path"):
                filename = Path(result["output_path"]).name
            asset = assets.get(filename)
            if not asset:
                continue
            candidate = {
                "app": app_key,
                "name": apps[app_key]["name"],
                "package": apps[app_key]["package"],
                "tagline": visible[app_key].get("tagline", "Patched Android app"),
                "description": visible[app_key].get("description", ""),
                "version": result.get("version"),
                "version_code": result.get("version_code"),
                "source": manifest.get("source"),
                "release_name": release.get("name") or release.get("tag_name"),
                "release_url": release.get("html_url"),
                "updated_at": release.get("published_at"),
                "filename": asset["name"],
                "size": asset.get("size", 0),
                "sha256": result.get("output_sha256") or asset.get("digest", "").removeprefix("sha256:"),
                "download_url": asset["browser_download_url"],
            }
            candidates_by_app.setdefault(app_key, []).append(candidate)

    latest: dict[str, dict] = {}
    for app_key, candidates in candidates_by_app.items():
        preferred_sources = visible[app_key].get("preferred_sources", [])
        best_rank = min(
            preferred_sources.index(item["source"])
            if item["source"] in preferred_sources
            else len(preferred_sources)
            for item in candidates
        )
        latest[app_key] = max(
            (
                item
                for item in candidates
                if (
                    preferred_sources.index(item["source"])
                    if item["source"] in preferred_sources
                    else len(preferred_sources)
                )
                == best_rank
            ),
            key=lambda item: item["updated_at"] or "",
        )

    ordered = []
    for app_key in catalog["featured"]:
        if app_key in latest:
            ordered.append(latest.pop(app_key))
    ordered.extend(latest[key] for key in sorted(latest))
    output = {
        "schema_version": 1,
        "generated_at": os.environ.get("GITHUB_RUN_ID", "local"),
        "featured": [item["app"] for item in ordered if item["app"] in catalog["featured"]],
        "apps": ordered,
    }
    site = ROOT / "site"
    site.mkdir(exist_ok=True)
    (site / "catalog.json").write_text(json.dumps(output, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
