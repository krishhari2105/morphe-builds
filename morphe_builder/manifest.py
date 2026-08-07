from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable


def canonical_json(data: Any) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def plan_digest(plan: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(plan).encode("utf-8")).hexdigest()


def slug(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-.").lower()
    return cleaned[:60] or "untagged"


def app_set_id(apps: Iterable[str]) -> str:
    return hashlib.sha256(canonical_json(sorted(apps)).encode("utf-8")).hexdigest()[:8]


def build_tag(source: str, patch_tag: str, cli_tag: str, plan: dict[str, Any]) -> str:
    normalized_plan = dict(plan)
    normalized_plan["apps"] = sorted(plan.get("apps", []))
    apps_id = app_set_id(normalized_plan["apps"])
    return (
        f"build-{slug(source)}-{slug(patch_tag)}-{slug(cli_tag)}-"
        f"apps-{apps_id}-{plan_digest(normalized_plan)[:12]}"
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_manifest(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_checksums(path: Path, files: Iterable[Path]) -> None:
    lines = [f"{sha256_file(file)}  {file.name}" for file in sorted(files, key=lambda item: item.name)]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
