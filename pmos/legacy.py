"""Read legacy manifests with explicit field mapping; never open legacy queues.

Importing queue ownership requires a frozen export of consumed/unknown states
and a reviewed identity map. A manifest alone is deliberately insufficient.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


def review_manifest(path: Path, original_root: Path, mapping: dict[str, str]) -> list[dict]:
    if set(mapping) != {"dataset", "period", "path", "sha256"}:
        raise ValueError("explicit_legacy_field_map_required")
    rows = []
    # The caller selects only a public manifest, never a browser/config file.
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            value = json.loads(line)
            row = {key: value[field] for key, field in mapping.items()}
            source = (original_root / row["path"]).resolve()
            if not source.is_relative_to(original_root.resolve()) or not source.is_file():
                raise ValueError("legacy_original_outside_root_or_missing")
            with source.open("rb") as original:
                digest = hashlib.file_digest(original, "sha256").hexdigest()
            if digest != row["sha256"]:
                raise ValueError("legacy_original_sha256_mismatch")
            rows.append({**row, "size": source.stat().st_size, "sha256_verified": True,
                         "queue_import_ready": False, "reason": "consumed_identity_export_required"})
    return rows
