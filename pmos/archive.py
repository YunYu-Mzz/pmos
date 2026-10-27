"""Content-addressed original archive.

Layout under ``<instance>/original``::

    <dataset>/<period>[/<scope>]/<sha256><ext>
    manifest.jsonl     one line per (dataset, period, scope, sha256)

The original bytes are written and fsynced before the manifest line, and the
manifest line before the task is confirmed. Bytes are never parsed here.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path

from .state import now_iso

EXTENSIONS = {".xlsx", ".xls", ".json", ".pdf", ".csv", ".html", ".txt", ".zip", ".bin",
              ".docx", ".doc", ".png", ".jpg"}
SCOPE = re.compile(r"[A-Za-z0-9_.-]{1,64}")


class ArchiveError(ValueError):
    pass


class Archive:
    def __init__(self, root: Path | str, province: str):
        self.root = Path(root)
        self.province = province
        self.manifest = self.root / "manifest.jsonl"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def directory(self, dataset: str, period: str, scope: str = "") -> Path:
        if not dataset.isidentifier():
            raise ArchiveError("dataset_id_invalid")
        if scope and not SCOPE.fullmatch(scope):
            raise ArchiveError("scope_invalid")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", period) or ".." in period:
            raise ArchiveError("period_invalid")
        path = self.root / dataset / period
        return path / scope if scope else path

    def store(self, dataset: str, period: str, payload: bytes, extension: str, *, scope: str = "",
              acquisition: str = "browser_request", **receipt) -> dict:
        if extension not in EXTENSIONS:
            raise ArchiveError("extension_unsupported")
        if not payload:
            raise ArchiveError("empty_original")
        digest = hashlib.sha256(payload).hexdigest()
        directory = self.directory(dataset, period, scope)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = directory / (digest + extension)
        with (self.root / ".archive.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if target.exists():
                if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                    raise ArchiveError("existing_original_hash_mismatch")
            else:
                descriptor, temporary = tempfile.mkstemp(dir=directory, prefix=".incoming.")
                try:
                    with os.fdopen(descriptor, "wb") as stream:
                        stream.write(payload)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temporary, target)
                    directory_fd = os.open(directory, os.O_RDONLY)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)
            record = {"province": self.province, "dataset": dataset, "period": period, "scope": scope,
                      "sha256": digest, "size": len(payload), "path": target.relative_to(self.root).as_posix(),
                      "acquisition": acquisition, "archived_at": now_iso(), **receipt}
            if not self._known(dataset, period, scope, digest):
                with self.manifest.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(record, ensure_ascii=False) + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
            return record

    def _known(self, dataset: str, period: str, scope: str, digest: str) -> bool:
        return any(r["dataset"] == dataset and r["period"] == period and r.get("scope", "") == scope
                   and r["sha256"] == digest for r in self.records())

    def records(self, dataset: str | None = None) -> list[dict]:
        if not self.manifest.exists():
            return []
        rows = [json.loads(line) for line in self.manifest.read_text(encoding="utf-8").splitlines() if line.strip()]
        return [r for r in rows if dataset is None or r["dataset"] == dataset]

    def verify(self, record: dict) -> bool:
        path = (self.root / record["path"]).resolve()
        if not path.is_relative_to(self.root.resolve()):
            raise ArchiveError("original_path_outside_archive")
        return path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == record["sha256"]

    def latest_period(self, dataset: str) -> str | None:
        periods = [r["period"] for r in self.records(dataset)]
        return max(periods) if periods else None
