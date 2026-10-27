"""Exact-ID crosswalk from the existing three-province field catalog.

This is a discovery index, NOT a new sample/semantic acceptance. Same titles
are deliberately ignored. Files are hashed, never imported or executed.
"""
from __future__ import annotations

from collections import Counter
import hashlib
import json
from pathlib import Path
import re

PROVINCES = ("liaoning", "anhui", "shanxi")
MAX_BYTES = 32 * 1024 * 1024
PRIVATE = re.compile(r"(?i)private|profile|browser|cookie|credential|password|token|secret|authorization|session|config|(?:^|[_-])auth(?:[_\.-]|$)|\.har$")


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def safe_reference(workspace: Path, reference: str) -> Path | None:
    workspace = workspace.resolve()
    path = workspace / reference
    parts = Path(reference).parts
    if (Path(reference).is_absolute() or ".." in parts or not parts or parts[0] not in PROVINCES
            or any(PRIVATE.search(part) for part in parts)):
        return None
    if any(workspace.joinpath(*parts[:i]).is_symlink() for i in range(1, len(parts) + 1)):
        return None
    resolved = path.resolve()
    if (not resolved.is_relative_to(workspace / parts[0])
            or any(PRIVATE.search(part) for part in resolved.relative_to(workspace).parts)):
        return None
    if path.suffix not in {".json", ".jsonl", ".csv", ".md"}:
        return None
    return path


def crosswalk(workspace: Path, baseline_ids: dict[str, set[str]], *, catalog_path: Path | None = None) -> dict:
    original_root = Path(workspace).absolute()
    relative = (catalog_path or original_root / "artifacts/three_province_fields_20261026/catalog.json").absolute().relative_to(original_root)
    workspace = original_root.resolve()
    path = workspace / relative
    if (".." in relative.parts or any(workspace.joinpath(*relative.parts[:i]).is_symlink()
                                     for i in range(1, len(relative.parts) + 1))
            or not path.is_file() or path.stat().st_size > MAX_BYTES):
        raise ValueError("bounded_public_field_catalog_required")
    before = digest(path)
    value = json.loads(path.read_bytes())
    if not isinstance(value.get("datasets"), list) or not isinstance(value.get("evidence"), list):
        raise ValueError("field_catalog_schema_changed")
    evidence = {row["id"]: row for row in value["evidence"]}
    files = {}
    rows = []
    for record in value["datasets"]:
        province, dataset = record["province"], record["dataset"]
        if province not in baseline_ids or dataset not in baseline_ids[province]:
            continue
        refs = []
        for key in record.get("evidence_ids", []):
            if key not in evidence:
                continue
            entry = evidence[key]
            reference = entry["path"]
            source = safe_reference(workspace, reference)
            if source is None:
                refs.append({"reference": reference, "status": "private_or_unreviewed_path_excluded"})
                continue
            if reference not in files:
                if not source.is_file():
                    facts = {"path": reference, "status": "missing"}
                elif source.stat().st_size > MAX_BYTES:
                    facts = {"path": reference, "status": "size_limit_excluded"}
                else:
                    first = digest(source)
                    facts = {"path": reference, "size": source.stat().st_size, "sha256": first,
                             "unchanged_during_read": digest(source) == first}
                files[reference] = facts
            facts = files[reference]
            expected = entry.get("sha256")
            status = facts.get("status")
            if status is None:
                status = ("changed_during_read" if not facts["unchanged_during_read"] else
                          "catalog_sha_verified" if expected == facts["sha256"] else "catalog_reference_sha_changed")
            refs.append({"path": reference, "kind": entry.get("kind"), "pointer": entry.get("pointer"),
                         "catalog_sha256": expected, "actual_sha256": facts.get("sha256"), "status": status})
        rows.append({"province": province, "dataset": dataset, "exact_id_match": True,
                     "evidence_refs": refs, "qualification": "discovery_only_requires_identity_and_original_chain"})
    after = digest(path)
    return {"mode": "read_only_cross_catalog_discovery", "catalog_ref": str(path),
            "catalog_sha256": before, "catalog_unchanged_during_read": before == after,
            "datasets_matched": dict(Counter(row["province"] for row in rows)),
            "unique_referenced_files": len(files), "files": list(files.values()), "entries": rows,
            "acceptance_upgrades": 0, "production_writes": 0, "website_requests": 0,
            "note": "Exact IDs only. A matching SHA in a derived/report artifact is not proof of original sample or consumed request."}
