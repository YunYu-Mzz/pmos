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


def status_metrics(instance: Path, *, now: float | None = None) -> dict:
    """What a status mail needs from one PMOS instance: counts and timestamps only, read-only.

    Originals come from the archive manifest (file mtime for "last hour", as
    the legacy Shanxi bridge counts); phase, block and holds from state; the
    latest business response and renewal from the ledger. Never request
    bodies, page values or credentials.
    """
    import json
    import time
    from datetime import datetime, timedelta, timezone
    now = time.time() if now is None else now
    instance = Path(instance)
    root = instance / "original"
    count = size = recent = missing = 0
    seen = set()
    manifest = root / "manifest.jsonl"
    if manifest.exists():
        with manifest.open("rb") as stream:
            for line in stream:
                if not line.endswith(b"\n"):
                    break  # a line being appended right now
                row = json.loads(line)
                path = root / row["path"]
                if row["path"] in seen or not path.resolve().is_relative_to(root.resolve()):
                    continue
                seen.add(row["path"])
                try:
                    stat = path.stat()
                except FileNotFoundError:
                    missing += 1
                    continue
                count += 1
                size += stat.st_size
                recent += int(now - 3600 <= stat.st_mtime <= now)

    def state(name):
        try:
            return json.loads((instance / "state" / f"{name}.json").read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return None

    def iso(seconds):
        return datetime.fromtimestamp(seconds, timezone.utc).isoformat(timespec="seconds") if seconds else None
    status, block, holds = state("status") or {}, state("block"), state("dataset_holds") or {}
    ledger = ledger_snapshot(instance / "ledger.sqlite3")
    business = renewal = since = None
    option_inventory = []
    scope_review_pending = 0
    last_hour = 0
    if ledger["exists"]:
        with closing(sqlite3.connect((instance / "ledger.sqlite3").resolve().as_uri() + "?mode=ro", uri=True,
                                     timeout=0.1)) as db:
            db.execute("PRAGMA query_only=ON")
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='period_options'").fetchone():
                option_inventory = [dict(dataset=r[0],state=r[1],dates=r[2]) for r in db.execute(
                    "SELECT dataset,state,COUNT(*) FROM period_options GROUP BY dataset,state")]
                scope_review_pending = db.execute(
                    "SELECT COUNT(*) FROM tasks t JOIN period_options p ON t.dataset=p.dataset AND t.period=p.period"
                    " WHERE t.status='pending' AND (p.state='review' OR (p.state='ready' AND NOT EXISTS("
                    " SELECT 1 FROM json_each(p.scopes_json) j WHERE j.value=t.scope)))").fetchone()[0]
                if status.get("period_option_mode") == "cartesian":
                    scope_review_pending = 0  # historical inventories no longer filter this queue
            business = db.execute("SELECT MAX(finished_at) FROM attempts WHERE kind IN ('collect','sample')"
                                  " AND outcome IN ('success','empty')").fetchone()[0]
            renewal = db.execute("SELECT MAX(finished_at) FROM attempts WHERE kind='keepalive'"
                                 " AND outcome IN ('success','empty')").fetchone()[0]
            last_hour = db.execute("SELECT COUNT(*) FROM attempts WHERE kind IN ('collect','sample')"
                                   " AND started_at>=?", (now - 3600,)).fetchone()[0]
            try:
                granted = db.execute("SELECT granted_at FROM owner_handoff").fetchone()
            except sqlite3.OperationalError:
                granted = None
            if granted and granted[0]:
                # From when PMOS collects this province (the handoff), China time, for the mail's note.
                since = datetime.fromtimestamp(granted[0], timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
    tasks = {}
    for counts in ledger["tasks"].values():
        for key, value in counts.items():
            tasks[key] = tasks.get(key, 0) + value
    return {"originals": count, "bytes": size, "last_hour_files_mtime": recent, "missing_originals": missing,
            "period_option_inventory":option_inventory,"scope_review_pending":scope_review_pending,
            "period_option_mode":status.get("period_option_mode"),
            "requests_last_hour": last_hour, "tasks": tasks, "attempts": ledger["attempts"],
            "phase": status.get("phase"), "current_dataset": status.get("last_dataset"),
            "status_updated_at": status.get("updated_at"), "worker_pid": status.get("pid"),
            "blocked": block is not None, "block_reason": (block or {}).get("reason"),
            "held_datasets": sorted(holds), "business_last_success_at": iso(business),
            "keeper_last_success_at": iso(renewal), "generated_at": iso(now), "since": since,
            **headline(status.get("phase"), block, business, now)}


# A collection without a business answer for this long is called stalled in the headline.
STALLED_MINUTES = 30


def headline(phase, block, business, now) -> dict:
    """For a mail subject (Liaoning 2026-11-01: the user found a one-hour stop only by a missing mail)."""
    stalled = round((now - business) / 60) if business else None
    if block is not None:
        line = f"已暂停：{block.get('reason')}"
    elif phase in ("paused", "stopped_all", "idle"):
        line = f"已暂停（{phase}）"
    elif phase in ("caught_up", "waiting_publication"):
        line = "正常（已追平，等待新日期）"
    elif phase == "waiting_scope_review":
        line = "当天可选组合已处理，历史维度待核"
    elif phase not in ("complete", "quiet_window") and stalled is not None and stalled >= STALLED_MINUTES:
        line = f"已停滞 {stalled} 分钟"
    else:
        line = "正常"
    return {"stalled_minutes": stalled, "headline": line}
