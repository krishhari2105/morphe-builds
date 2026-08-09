from __future__ import annotations

import argparse
import json
import os
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

API = "https://api.github.com"


def request_json(url: str, token: str) -> dict:
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


def delete_cache(repository: str, cache_id: int, token: str) -> None:
    request = Request(
        f"{API}/repos/{repository}/actions/caches/{cache_id}",
        method="DELETE",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urlopen(request, timeout=30):
        return


def list_caches(repository: str, token: str) -> list[dict]:
    caches: list[dict] = []
    page = 1
    while True:
        payload = request_json(
            f"{API}/repos/{repository}/actions/caches?per_page=100&page={page}", token
        )
        batch = payload.get("actions_caches", [])
        if not isinstance(batch, list):
            raise RuntimeError("Unexpected Actions caches response")
        caches.extend(item for item in batch if isinstance(item, dict))
        if len(batch) < 100:
            return caches
        page += 1


def caches_to_delete(caches: list[dict], prefix: str, keep: int) -> list[dict]:
    matching = [item for item in caches if str(item.get("key", "")).startswith(prefix)]
    matching.sort(
        key=lambda item: (str(item.get("created_at", "")), int(item.get("id", 0))),
        reverse=True,
    )
    return matching[max(0, keep) :]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prune old source-scoped GitHub Actions caches")
    parser.add_argument("--source", required=True)
    parser.add_argument("--keep", type=int, default=2)
    args = parser.parse_args(argv)

    token = os.environ.get("GITHUB_TOKEN")
    repository = os.environ.get("GITHUB_REPOSITORY")
    if not token or not repository:
        print("::warning::GITHUB_TOKEN and GITHUB_REPOSITORY are required for cache cleanup")
        return 0

    try:
        caches = list_caches(repository, token)
        deleted = 0
        for cache_type in ("tools", "bases"):
            prefix = f"morphe-{cache_type}-Linux-{args.source}--"
            for cache in caches_to_delete(caches, prefix, args.keep):
                delete_cache(repository, int(cache["id"]), token)
                deleted += 1
                print(f"Deleted cache {cache['key']} ({cache['id']})")
        print(f"Cache retention complete for {args.source}: deleted {deleted}")
    except (HTTPError, URLError, OSError, ValueError, RuntimeError, KeyError) as exc:
        print(f"::warning::Cache retention failed for {args.source}: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
