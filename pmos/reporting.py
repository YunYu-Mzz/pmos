"""Short SQLite read batches and a schema2 coverage handoff, with no sender.

The existing signed mail/unique Feishu writer remains the integration owner.
File hashing and rendering MUST happen after read_batch has closed its DB.
"""
from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3


def ledger_snapshot(path: Path) -> dict:
    if not path.exists():
        return {"exists": False, "tasks": {}, "attempts": {}}
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.1)) as db:
        db.execute("PRAGMA query_only=ON")
        tasks = db.execute("SELECT dataset,status,COUNT(*) FROM tasks GROUP BY dataset,status").fetchall()
        attempts = db.execute("SELECT outcome,COUNT(*) FROM attempts GROUP BY outcome").fetchall()
    result = {}
    for dataset, status, count in tasks:
        result.setdefault(dataset, {})[status] = count
    return {"exists": True, "tasks": result, "attempts": dict(attempts)}


def read_batch(path: Path, *, after: int = 0, limit: int = 100) -> list[dict]:
    if type(limit) is not int or not 1 <= limit <= 500:
        raise ValueError("bounded_batch_required")
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.1)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        rows = db.execute("SELECT * FROM attempts WHERE id>? ORDER BY id LIMIT ?", (after, limit)).fetchall()
        return [dict(row) for row in rows]


def coverage_item(title: str, ids: list[str], records: list[dict]) -> dict:
    """Existing schema2-compatible indicator; callers supply verified receipts.

    Count physical source paths separately from unique business identities;
    never count parsed sidecars, dictionary requests or list rows as files.
    """
    paths = {}
    identities = set()
    periods = set()
    empty = set()
    for row in records:
        if row.get("sha256_verified") is not True:
            raise ValueError("unverified_receipt")
        if row.get("role") != "source":
            continue
        path = row["path"]
        fact = (row["sha256"], row["size"])
        if path in paths and paths[path] != fact:
            raise ValueError("source_receipt_conflict")
        paths[path] = fact
        identities.add(row["task_key"])
        if row.get("empty"):
            empty.add(row["task_key"])
        if row.get("statistical_period"):
            periods.add(row["statistical_period"])
    return {"title": title, "ids": ids, "file_count": len(paths),
            "bytes": sum(size for _, size in paths.values()), "unique_periods": len(periods),
            "periods": sorted(periods), "evidence_state": "offline_reviewed",
            "note": f"唯一任务{len(identities)}；空结果身份{len(empty)}；列表/时点不计为文件；完整覆盖未验收"}
