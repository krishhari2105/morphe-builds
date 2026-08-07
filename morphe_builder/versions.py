from __future__ import annotations

import re


_PACKAGE_RE = re.compile(r"Package name:\s*([A-Za-z0-9_.]+)")
_VERSION_RE = re.compile(r"^(v?[0-9][A-Za-z0-9._+-]*(?:-[A-Za-z0-9._+-]+)?)")


class VersionParseError(ValueError):
    pass


def version_key(value: str) -> tuple[tuple[int, object], ...]:
    normalized = value[1:] if value.startswith("v") else value
    pieces = re.findall(r"\d+|[A-Za-z]+", normalized)
    result: list[tuple[int, object]] = []
    for piece in pieces:
        if piece.isdigit():
            result.append((1, int(piece)))
        else:
            result.append((0, piece.lower()))
    return tuple(result)


def parse_compatible_versions(output: str) -> dict[str, list[str]]:
    versions: dict[str, list[str]] = {}
    current_package: str | None = None
    saw_package = False

    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        package_match = _PACKAGE_RE.search(line)
        if package_match:
            current_package = package_match.group(1)
            saw_package = True
            versions.setdefault(current_package, [])
            continue
        if current_package is None:
            continue
        if re.search(r"\bAny\b", line, re.IGNORECASE):
            if "Any" not in versions[current_package]:
                versions[current_package].append("Any")
            continue
        match = _VERSION_RE.match(line)
        if not match:
            continue
        value = match.group(1).rstrip(".,;:")
        if value not in versions[current_package]:
            versions[current_package].append(value)

    if not saw_package:
        raise VersionParseError("Morphe CLI output contained no package sections")

    for package, values in versions.items():
        numeric = [value for value in values if value != "Any"]
        numeric.sort(key=version_key, reverse=True)
        versions[package] = (["Any"] if "Any" in values else []) + numeric
    return versions


def normalize_version(value: str) -> str:
    return value[1:] if value.startswith("v") else value
