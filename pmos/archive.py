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
from .fsretry import eperm_retry

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
        self.link_from: "Archive | None" = None   # the archive whose copy of the same bytes this one may share
        self.latest: tuple[str, Path] | None = None
        self._known_size = 0
        self._known_keys: set[tuple] = set()

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
        # The answer exists only in memory here (its identity is already consumed): a transient
        # EPERM is retried. Content-addressed, so a repeat writes the same file.
        return eperm_retry(self._store, dataset, period, payload, extension, scope=scope, acquisition=acquisition,
                           **receipt)

    def _store(self, dataset: str, period: str, payload: bytes, extension: str, *, scope: str = "",
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
                self._write(directory, target, digest, payload)
            self.latest = (digest, target)
            record = {"province": self.province, "dataset": dataset, "period": period, "scope": scope,
                      "sha256": digest, "size": len(payload), "path": target.relative_to(self.root).as_posix(),
                      "acquisition": acquisition, "archived_at": now_iso(), **receipt}
            if not self._known(dataset, period, scope, digest):
                with self.manifest.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(record, ensure_ascii=False) + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
            return record

    def _write(self, directory: Path, target: Path, digest: str, payload: bytes) -> None:
        """The file, durably: a hard link when ``link_from`` has just stored these very bytes, else a new copy."""
        peer = self.link_from.latest if self.link_from is not None else None
        linked = False
        if peer is not None and peer[0] == digest:
            try:
                if peer[1].stat().st_size == len(payload):
                    os.link(peer[1], target)
                    linked = True
            except OSError:
                pass                      # another file system, or the copy is gone: write our own
        descriptor = temporary = None
        if not linked:
            descriptor, temporary = tempfile.mkstemp(dir=directory, prefix=".incoming.")
        try:
            if not linked:
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
            if temporary is not None and os.path.exists(temporary):
                os.unlink(temporary)

    def _known(self, dataset: str, period: str, scope: str, digest: str) -> bool:
        """Already in the manifest? Reads only what was appended since the last call (a year's manifest is large)."""
        size = self.manifest.stat().st_size if self.manifest.exists() else 0
        if size != self._known_size:
            with self.manifest.open("rb") as stream:
                if size < self._known_size:
                    self._known_size, self._known_keys = 0, set()
                stream.seek(self._known_size)
                # Only whole lines: a line another process is still appending is read next time.
                chunk = stream.read(size - self._known_size)
            complete = chunk[:chunk.rfind(b"\n") + 1]
            for line in complete.decode("utf-8").splitlines():
                if line.strip():
                    row = json.loads(line)
                    self._known_keys.add((row["dataset"], row["period"], row.get("scope", ""), row["sha256"]))
            self._known_size += len(complete)
        return (dataset, period, scope, digest) in self._known_keys

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
