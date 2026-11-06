"""JSON-compatible snapshots of local runs and model diagnostics."""
from __future__ import annotations

import json
import math
from collections.abc import Iterator
from pathlib import Path
from typing import Any


def json_safe(value: Any) -> Any:
    """Copy JSON-shaped data, retaining non-finite diagnostic numbers as text."""
    if isinstance(value, dict):
        return {key: json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        if math.isnan(value):
            return "NaN"
        return "Infinity" if value > 0 else "-Infinity"
    return value


def recent_reports(root: Path) -> Iterator[dict]:
    """Read newest valid run journals, tolerating damaged or concurrently removed files.

    Listing and reading are synchronous; HTTP handlers run this work in a thread.
    """
    indexed = []
    for path in root.glob("*/report.json"):
        identifier = path.parent.name
        if len(identifier) != 32 or any(char not in "0123456789abcdef" for char in identifier):
            continue
        try:
            indexed.append((path.stat().st_mtime_ns, path))
        except OSError:
            continue
    for _, path in sorted(indexed, key=lambda entry: entry[0], reverse=True):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("id") == path.parent.name:
            yield data
