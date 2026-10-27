"""Durable task queue and request ledger (SQLite, one file per province).

Every site request is recorded as an attempt *before* it is sent. An attempt
left ``pending`` (crash, kill, power loss) means the outcome is unknown; the
worker refuses to send anything until a human settles it. Tasks are keyed by
(dataset, period, scope) so re-seeding never duplicates work.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Iterable

TASK_STATUSES = ("pending", "saved", "empty", "skipped", "failed")
ATTEMPT_KINDS = ("collect", "sample", "keepalive", "probe")
OUTCOMES = ("pending", "success", "empty", "failed", "unknown")
# Kinds that the site counts as user activity (they renew the login).
BUSINESS_KINDS = ("collect", "sample", "keepalive")
# Kinds charged to the collection budget (hour/day caps).
BUDGET_KINDS = ("collect", "sample")

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks(
  dataset TEXT NOT NULL, period TEXT NOT NULL, scope TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
  artifact_sha256 TEXT, reason TEXT,
  created_at REAL NOT NULL, updated_at REAL NOT NULL,
  PRIMARY KEY(dataset, period, scope));
CREATE TABLE IF NOT EXISTS attempts(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL, dataset TEXT, period TEXT, scope TEXT NOT NULL DEFAULT '',
  started_at REAL NOT NULL, finished_at REAL,
  outcome TEXT NOT NULL DEFAULT 'pending',
  http_status INTEGER, business_status TEXT, reason TEXT);
CREATE TABLE IF NOT EXISTS items(
  dataset TEXT NOT NULL, period TEXT NOT NULL, scope TEXT NOT NULL,
  payload TEXT NOT NULL, created_at REAL NOT NULL,
  PRIMARY KEY(dataset, period, scope));
CREATE TABLE IF NOT EXISTS records(
  dataset TEXT NOT NULL, record_id TEXT NOT NULL, first_seen REAL NOT NULL,
  PRIMARY KEY(dataset, record_id));
CREATE INDEX IF NOT EXISTS attempts_finished ON attempts(finished_at);
CREATE INDEX IF NOT EXISTS attempts_outcome ON attempts(outcome);
CREATE TABLE IF NOT EXISTS attempt_identities(
  attempt_id INTEGER PRIMARY KEY, identity_key TEXT NOT NULL, identity_json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS attempt_identity_key ON attempt_identities(identity_key);
CREATE TABLE IF NOT EXISTS migration_owner(
  singleton INTEGER PRIMARY KEY CHECK(singleton=1),province TEXT NOT NULL,account_alias TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS migration_sources(
  source_id TEXT NOT NULL,snapshot_sha256 TEXT NOT NULL,rows_sha256 TEXT NOT NULL,
  PRIMARY KEY(source_id,snapshot_sha256));
CREATE TABLE IF NOT EXISTS legacy_records(
  source_id TEXT NOT NULL,snapshot_sha256 TEXT NOT NULL,legacy_key TEXT NOT NULL,
  identity_key TEXT,identity_json TEXT,state TEXT NOT NULL,consumed INTEGER NOT NULL,
  blockers_json TEXT NOT NULL,facts_sha256 TEXT NOT NULL,
  PRIMARY KEY(source_id,snapshot_sha256,legacy_key));
CREATE INDEX IF NOT EXISTS legacy_identity_key ON legacy_records(identity_key);
CREATE TABLE IF NOT EXISTS file_tombstones(
  file_key TEXT PRIMARY KEY,identity_key TEXT NOT NULL,source_ref TEXT NOT NULL);
"""


def validate_ledger_path(path: Path | str) -> Path:
    """Reject every legacy province target BEFORE mkdir/SQLite side effects."""
    target = Path(path).resolve()
    workspace = Path(__file__).resolve().parents[2]
    if any(target.is_relative_to(workspace / province) for province in ("liaoning", "anhui", "shanxi")):
        raise ValueError("legacy_directory_never_a_unified_ledger_target")
    return target


class Ledger:
    def __init__(self, path: Path | str, clock=time.time):
        self.path = validate_ledger_path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.clock = clock
        self.db = sqlite3.connect(self.path, isolation_level=None, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    def _write(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            cursor = self.db.execute(sql, args)
            self.db.execute("COMMIT")
            return cursor
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    # -- tasks ------------------------------------------------------------
    def seed(self, dataset: str, periods: Iterable[str], scope: str = "") -> int:
        now = self.clock()
        rows = [(dataset, period, scope, now, now) for period in periods]
        self.db.execute("BEGIN IMMEDIATE")
        try:
            before = self.db.total_changes
            self.db.executemany(
                "INSERT OR IGNORE INTO tasks(dataset, period, scope, status, created_at, updated_at)"
                " VALUES(?, ?, ?, 'pending', ?, ?)", rows)
            added = self.db.total_changes - before
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        return added

    def put_listing(self, dataset: str, record_ids: list[str], items: list[dict]) -> tuple[set[str], int]:
        """One listing page, atomically: which records were known, and the new download tasks.

        Each item needs ``period`` and ``scope``; its payload rebuilds the
        download request after a restart. Records are marked seen in the same
        transaction that queues their files, so a crash cannot lose them.
        """
        import json
        ids = sorted(set(record_ids))
        now = self.clock()
        added = 0
        self.db.execute("BEGIN IMMEDIATE")
        try:
            marks = ",".join("?" * len(ids))
            known = {row["record_id"] for row in self.db.execute(
                f"SELECT record_id FROM records WHERE dataset=? AND record_id IN ({marks})", (dataset, *ids))} if ids else set()
            for item in items:
                key = (dataset, item["period"], item["scope"])
                self.db.execute("INSERT OR IGNORE INTO items(dataset, period, scope, payload, created_at)"
                                " VALUES(?, ?, ?, ?, ?)", (*key, json.dumps(item, ensure_ascii=False), now))
                added += self.db.execute(
                    "INSERT OR IGNORE INTO tasks(dataset, period, scope, status, created_at, updated_at)"
                    " VALUES(?, ?, ?, 'pending', ?, ?)", (*key, now, now)).rowcount
            self.db.executemany("INSERT OR IGNORE INTO records(dataset, record_id, first_seen) VALUES(?, ?, ?)",
                                [(dataset, record_id, now) for record_id in ids])
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        return known, added

    def item(self, dataset: str, period: str, scope: str) -> dict | None:
        import json
        row = self.db.execute("SELECT payload FROM items WHERE dataset=? AND period=? AND scope=?",
                              (dataset, period, scope)).fetchone()
        return json.loads(row["payload"]) if row else None

    def supersede(self, dataset: str, scope: str, before_period: str, reason: str) -> int:
        """Skip older pending tasks that a newer one made redundant (daily listings)."""
        return self._write("UPDATE tasks SET status='skipped', reason=?, updated_at=? WHERE dataset=? AND scope=?"
                           " AND status='pending' AND period<?",
                           (reason, self.clock(), dataset, scope, before_period)).rowcount

    def mark_existing(self, dataset: str, period: str, sha256: str, scope: str = "") -> None:
        """Record an artifact that already exists (import, earlier run) as saved."""
        now = self.clock()
        self._write("INSERT INTO tasks(dataset, period, scope, status, artifact_sha256, created_at, updated_at)"
                    " VALUES(?, ?, ?, 'saved', ?, ?, ?) ON CONFLICT(dataset, period, scope) DO UPDATE SET"
                    " status='saved', artifact_sha256=excluded.artifact_sha256, updated_at=excluded.updated_at",
                    (dataset, period, scope, sha256, now, now))

    def next_task(self, datasets: list[str]) -> sqlite3.Row | None:
        """First pending task of the first dataset that has one; newest period first."""
        for dataset in datasets:
            row = self.db.execute("SELECT * FROM tasks WHERE dataset=? AND status='pending'"
                                  " ORDER BY period DESC, scope LIMIT 1", (dataset,)).fetchone()
            if row is not None:
                return row
        return None

    def settle_task(self, dataset: str, period: str, status: str, *, scope: str = "",
                    sha256: str | None = None, reason: str | None = None) -> None:
        if status not in TASK_STATUSES or status == "pending":
            raise ValueError("task_status_invalid")
        cursor = self._write("UPDATE tasks SET status=?, artifact_sha256=COALESCE(?, artifact_sha256), reason=?,"
                             " attempts=attempts+1, updated_at=? WHERE dataset=? AND period=? AND scope=?",
                             (status, sha256, reason, self.clock(), dataset, period, scope))
        if cursor.rowcount != 1:
            raise ValueError("task_missing")

    def reopen(self, dataset: str, period: str, scope: str = "") -> None:
        self._write("UPDATE tasks SET status='pending', updated_at=? WHERE dataset=? AND period=? AND scope=?",
                    (self.clock(), dataset, period, scope))

    def task(self, dataset: str, period: str, scope: str = "") -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM tasks WHERE dataset=? AND period=? AND scope=?",
                               (dataset, period, scope)).fetchone()

    def summary(self) -> dict[str, dict[str, int]]:
        result: dict[str, dict[str, int]] = {}
        for row in self.db.execute("SELECT dataset, status, COUNT(*) n FROM tasks GROUP BY dataset, status"):
            result.setdefault(row["dataset"], {})[row["status"]] = row["n"]
        return result

    # -- attempts ---------------------------------------------------------
    def begin(self, kind: str, dataset: str | None = None, period: str | None = None, scope: str = "", *, identity=None) -> int:
        if kind not in ATTEMPT_KINDS:
            raise ValueError("attempt_kind_invalid")
        from .contracts import canonical
        document = canonical(identity.document()).decode() if identity else None
        self.db.execute("BEGIN IMMEDIATE")
        try:
            if self.has_legacy_shadow():
                raise ValueError("legacy_shadow_requires_owner_handoff")
            if identity:
                owner = self.db.execute("SELECT province,account_alias FROM migration_owner WHERE singleton=1").fetchone()
                if owner and tuple(owner) != (identity.province, identity.account_alias):
                    raise ValueError("ledger_province_account_mismatch")
                self.db.execute("INSERT OR IGNORE INTO migration_owner VALUES(1,?,?)", (identity.province, identity.account_alias))
            if identity and kind in BUDGET_KINDS and self.consumed_identity(identity.key):
                raise ValueError("business_identity_already_consumed")
            if identity and kind in BUDGET_KINDS and identity.file_key and self.consumed_file(identity.file_key):
                raise ValueError("business_file_already_consumed")
            cursor = self.db.execute("INSERT INTO attempts(kind,dataset,period,scope,started_at) VALUES(?,?,?,?,?)",
                                     (kind, dataset, period, scope, self.clock()))
            attempt = int(cursor.lastrowid)
            if identity:
                self.db.execute("INSERT INTO attempt_identities VALUES(?,?,?)", (attempt, identity.key, document))
                if kind in BUDGET_KINDS and identity.file_key:
                    self.db.execute("INSERT INTO file_tombstones VALUES(?,?,?)", (identity.file_key, identity.key, f"attempt:{attempt}"))
            self.db.execute("COMMIT")
            return attempt
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def consumed_identity(self, identity_key: str) -> bool:
        return bool(self.db.execute("SELECT 1 FROM attempt_identities i JOIN attempts a ON a.id=i.attempt_id"
            " WHERE i.identity_key=? AND a.kind IN ('collect','sample') LIMIT 1", (identity_key,)).fetchone()
            or self.db.execute("SELECT 1 FROM legacy_records WHERE identity_key=? AND consumed=1 LIMIT 1", (identity_key,)).fetchone())

    def has_legacy_shadow(self) -> bool:
        return self.db.execute("SELECT 1 FROM migration_sources LIMIT 1").fetchone() is not None

    def consumed_file(self, file_key: str) -> bool:
        return self.db.execute("SELECT 1 FROM file_tombstones WHERE file_key=?", (file_key,)).fetchone() is not None

    def legacy_summary(self) -> dict:
        return {"snapshots": self.db.execute("SELECT COUNT(*) FROM migration_sources").fetchone()[0],
                "states": {r[0]: r[1] for r in self.db.execute("SELECT state,COUNT(*) FROM legacy_records GROUP BY state")},
                "unmapped": self.db.execute("SELECT COUNT(*) FROM legacy_records WHERE identity_key IS NULL").fetchone()[0],
                "dispatch_allowed": False}

    def finish(self, attempt: int, outcome: str, *, http_status: int | None = None,
               business_status: str | None = None, reason: str | None = None) -> None:
        if outcome not in OUTCOMES or outcome == "pending":
            raise ValueError("attempt_outcome_invalid")
        cursor = self._write("UPDATE attempts SET finished_at=?, outcome=?, http_status=?, business_status=?, reason=?"
                             " WHERE id=? AND outcome='pending'",
                             (self.clock(), outcome, http_status, business_status, reason, attempt))
        if cursor.rowcount != 1:
            raise ValueError("attempt_not_pending")

    def unsettled(self) -> list[sqlite3.Row]:
        return list(self.db.execute("SELECT * FROM attempts WHERE outcome='pending' ORDER BY id"))

    def consumed(self, dataset: str, period: str, scope: str = "") -> bool:
        """Any dispatched collect/sample consumes the identity, even after ack.

        A successful response before an archive/checkpoint crash must never be
        sent again just because the task still says pending. Keepalive/probes
        are separate explicitly approved operations.
        """
        return self.db.execute(
            "SELECT 1 FROM attempts WHERE dataset=? AND period=? AND scope=?"
            " AND kind IN ('collect','sample') LIMIT 1", (dataset, period, scope)).fetchone() is not None

    def settle_unknown(self, attempt: int) -> None:
        """Operator review: the outcome of an interrupted attempt stays unknown.
        Its start time still counts as a response end for spacing."""
        row = self.db.execute("SELECT started_at FROM attempts WHERE id=? AND outcome='pending'", (attempt,)).fetchone()
        if row is None:
            raise ValueError("attempt_not_pending")
        self._write("UPDATE attempts SET outcome='unknown', finished_at=?, reason='interrupted' WHERE id=?",
                    (max(row["started_at"], self.clock()), attempt))

    def last_response_end(self) -> float:
        """Latest moment any request finished (or started, if still open): shared spacing."""
        row = self.db.execute("SELECT MAX(COALESCE(finished_at, started_at)) t FROM attempts").fetchone()
        return row["t"] or 0.0

    def last_business_end(self) -> float:
        """Latest business response: what the site counts as activity (not TIME probes)."""
        marks = ",".join("?" * len(BUSINESS_KINDS))
        row = self.db.execute(f"SELECT MAX(finished_at) t FROM attempts WHERE kind IN ({marks})"
                              " AND outcome IN ('success', 'empty')", BUSINESS_KINDS).fetchone()
        return row["t"] or 0.0

    def last_attempt(self, kind: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM attempts WHERE kind=? ORDER BY id DESC LIMIT 1", (kind,)).fetchone()

    def budget_starts(self, since: float) -> list[float]:
        marks = ",".join("?" * len(BUDGET_KINDS))
        return [row["started_at"] for row in self.db.execute(
            f"SELECT started_at FROM attempts WHERE kind IN ({marks}) AND started_at>=? ORDER BY started_at",
            (*BUDGET_KINDS, since))]

    def counts(self, since: float) -> dict[str, int]:
        return {row["kind"]: row["n"] for row in self.db.execute(
            "SELECT kind, COUNT(*) n FROM attempts WHERE started_at>=? GROUP BY kind", (since,))}
