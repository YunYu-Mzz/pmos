"""Durable task queue and request ledger (SQLite, one file per province).

Every site request is recorded as an attempt *before* it is sent. An attempt
left ``pending`` (crash, kill, power loss) means the outcome is unknown; the
worker refuses to send anything until a human settles it. Tasks are keyed by
(dataset, period, scope) so re-seeding never duplicates work.
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
import time
from pathlib import Path
from typing import Iterable

# deferred: answered "not yet published"; pending again on the next day.
TASK_STATUSES = ("pending", "saved", "empty", "skipped", "failed", "deferred")
ATTEMPT_KINDS = ("collect", "sample", "keepalive", "probe")
# ``not_sent``: provably never reached the network; it consumes no identity.
OUTCOMES = ("pending", "success", "empty", "failed", "unknown", "not_sent")
# Kinds that the site counts as user activity (they renew the login).
BUSINESS_KINDS = ("collect", "sample", "keepalive")
# Kinds charged to the collection budget (hour/day caps).
BUDGET_KINDS = ("collect", "sample")
# Page-menu failures raised before any report frame opened (ui.select_menu, ui.select_options).
NAVIGATION_CODES = frozenset({"menu_navigation_timeout", "menu_label_not_unique", "menu_control_changed",
                              "menu_label_not_visible", "menu_path_ambiguous", "select_options_missing"})

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
CREATE INDEX IF NOT EXISTS attempts_identity ON attempts(dataset, period, scope, kind);
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
CREATE INDEX IF NOT EXISTS legacy_dataset_period ON legacy_records(
  json_extract(identity_json,'$.dataset'), json_extract(identity_json,'$.period.label'));
CREATE TABLE IF NOT EXISTS file_tombstones(
  file_key TEXT PRIMARY KEY,identity_key TEXT NOT NULL,source_ref TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS handoff_extensions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,added_budget INTEGER NOT NULL,approval TEXT NOT NULL,
  receipt_sha256 TEXT NOT NULL,granted_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS owner_handoff(
  singleton INTEGER PRIMARY KEY CHECK(singleton=1),receipt_json TEXT NOT NULL,
  receipt_sha256 TEXT NOT NULL,granted_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS handoff_scope_extensions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,order_json TEXT NOT NULL,seeded_json TEXT NOT NULL,
  approval TEXT NOT NULL,receipt_sha256 TEXT NOT NULL,granted_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS released_attempts(
  attempt_id INTEGER PRIMARY KEY,approval TEXT NOT NULL,released_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS late_legacy_imports(
  source_id TEXT NOT NULL,snapshot_sha256 TEXT NOT NULL,rows INTEGER NOT NULL,approval TEXT NOT NULL,
  imported_at REAL NOT NULL,PRIMARY KEY(source_id,snapshot_sha256));
"""

# Every site request after a handoff counts against its budget, keepalive too.
HANDOFF_KEYS = {"version", "province", "account_alias", "legacy_owner", "snapshots", "scope",
                "request_budget", "code_sha", "approval", "session", "seeding"}
# legacy_pending_only: work exactly the legacy queue's unconsumed identities
# (seeded at apply time); spec_periods: also expand spec periods daily.
SEEDING = ("legacy_pending_only", "spec_periods")


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

    def next_task(self, datasets: list[str], *, newest: str | None = None, oldest: str | None = None) -> sqlite3.Row | None:
        """First pending task of the first dataset that has one; newest period first (none after ``newest`` or before ``oldest``)."""
        for dataset in datasets:
            row = self.db.execute("SELECT * FROM tasks WHERE dataset=? AND status='pending'"
                                  + (" AND period<=?" if newest else "") + (" AND period>=?" if oldest else "")
                                  + " ORDER BY period DESC, scope LIMIT 1",
                                  (dataset, *([newest] if newest else []), *([oldest] if oldest else []))).fetchone()
            if row is not None:
                return row
        return None

    def pending_after(self, dataset: str, newest: str) -> int:
        """Pending tasks of days not yet due (they wait, nothing is consumed)."""
        return self.db.execute("SELECT COUNT(*) FROM tasks WHERE dataset=? AND status='pending' AND period>?",
                               (dataset, newest)).fetchone()[0]

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

    def isolation_streak(self, dataset: str, limit: int, prefix: str = "isolated:") -> int:
        """How many of the dataset's latest settled tasks (up to ``limit``) failed with ``prefix`` in a row."""
        rows = self.db.execute("SELECT status, reason FROM tasks WHERE dataset=? AND status!='pending'"
                               " ORDER BY updated_at DESC, rowid DESC LIMIT ?", (dataset, limit))
        streak = 0
        for row in rows:
            if row["status"] != "failed" or not (row["reason"] or "").startswith(prefix):
                break
            streak += 1
        return streak

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
            blocker = self.dispatch_blocker()
            if blocker:
                # Same transaction as the attempt: no race past the gate or budget.
                raise ValueError(blocker)
            handoff = self.handoff()
            if handoff is not None and dataset is not None and kind in BUDGET_KINDS and dataset not in handoff["scope"]:
                raise ValueError("dataset_outside_handoff_scope")
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
            " WHERE i.identity_key=? AND a.kind IN ('collect','sample') AND a.outcome!='not_sent'"
            " AND a.id NOT IN (SELECT attempt_id FROM released_attempts) LIMIT 1",
            (identity_key,)).fetchone()
            or self.db.execute("SELECT 1 FROM legacy_records WHERE identity_key=? AND consumed=1 LIMIT 1", (identity_key,)).fetchone())

    def has_legacy_shadow(self) -> bool:
        return self.db.execute("SELECT 1 FROM migration_sources LIMIT 1").fetchone() is not None

    # -- owner handoff ----------------------------------------------------
    def handoff(self) -> dict | None:
        """The receipt, with the latest approved scope extension applied.

        ``scope`` is then the extension's collection order (a superset of the
        receipt's); ``seeded`` lists the datasets whose spec periods are seeded
        daily even under ``legacy_pending_only``.
        """
        import json
        row = self.db.execute("SELECT receipt_json, receipt_sha256, granted_at FROM owner_handoff").fetchone()
        if not row:
            return None
        value = {**json.loads(row["receipt_json"]), "receipt_sha256": row["receipt_sha256"],
                 "granted_at": row["granted_at"]}
        value["seeded"] = list(value["scope"]) if value["seeding"] == "spec_periods" else []
        extension = self.db.execute("SELECT order_json, seeded_json FROM handoff_scope_extensions"
                                    " ORDER BY id DESC LIMIT 1").fetchone()
        if extension:
            value.update(scope=json.loads(extension["order_json"]), seeded=json.loads(extension["seeded_json"]))
        return value

    def extend_scope(self, order: list[str], seeded: list[str], approval: str) -> dict:
        """Record a user-approved scope extension: the full collection order and the daily-seeded datasets.

        Nothing ever leaves the scope or the seeded set: each extension must
        contain the previous one. The receipt itself never changes.
        """
        import json
        if (not isinstance(order, list) or not order or len(set(order)) != len(order)
                or not all(isinstance(x, str) and x for x in order)):
            raise ValueError("scope_order_invalid")
        if not isinstance(seeded, list) or len(set(seeded)) != len(seeded) or not set(seeded) <= set(order):
            raise ValueError("seeded_must_be_within_scope")
        if not isinstance(approval, str) or not approval.strip():
            raise ValueError("handoff_extension_approval_required")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            current = self.handoff()
            if current is None:
                raise ValueError("handoff_extension_requires_a_handoff")
            if not set(current["scope"]) <= set(order):
                raise ValueError("scope_extension_cannot_remove_datasets")
            if not set(current["seeded"]) <= set(seeded):
                raise ValueError("scope_extension_cannot_unseed_datasets")
            self.db.execute("INSERT INTO handoff_scope_extensions(order_json,seeded_json,approval,receipt_sha256,granted_at)"
                            " VALUES(?,?,?,?,?)", (json.dumps(order), json.dumps(seeded), approval,
                                                   current["receipt_sha256"], self.clock()))
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        return {"scope": len(order), "seeded": len(seeded), "added": sorted(set(order) - set(current["scope"]))}

    def import_late_legacy(self, source_id: str, snapshot_sha256: str, rows: list[tuple], approval: str) -> dict:
        """After the handoff, add legacy facts that were not imported at cutover: consumed tombstones only.

        ``rows``: (legacy_key, identity) of records the legacy owner completed
        (e.g. its main archive manifest). A tombstone can only stop a send,
        never allow one; each snapshot is imported once.
        """
        from .contracts import canonical
        if not source_id or len(snapshot_sha256) != 64 or any(c not in "0123456789abcdef" for c in snapshot_sha256):
            raise ValueError("explicit_snapshot_reference_required")
        if not isinstance(approval, str) or not approval.strip():
            raise ValueError("late_import_approval_required")
        keys = [key for key, _ in rows]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate_legacy_keys_in_snapshot")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            if self.db.execute("SELECT 1 FROM owner_handoff").fetchone() is None:
                raise ValueError("late_import_requires_a_handoff")
            if self.db.execute("SELECT 1 FROM late_legacy_imports WHERE source_id=? AND snapshot_sha256=?",
                               (source_id, snapshot_sha256)).fetchone():
                raise ValueError("late_import_already_done")
            owner = self.db.execute("SELECT province,account_alias FROM migration_owner WHERE singleton=1").fetchone()
            digest = hashlib.sha256(canonical([[key, identity.key] for key, identity in rows])).hexdigest()
            added = 0
            for key, identity in rows:
                if owner and tuple(owner) != (identity.province, identity.account_alias):
                    raise ValueError("ledger_province_account_mismatch")
                cursor = self.db.execute(
                    "INSERT OR IGNORE INTO legacy_records(source_id,snapshot_sha256,legacy_key,identity_key,identity_json,"
                    "state,consumed,blockers_json,facts_sha256) VALUES(?,?,?,?,?,'saved',1,'[]',?)",
                    (source_id, snapshot_sha256, key, identity.key, canonical(identity.document()).decode(), digest))
                added += cursor.rowcount
            self.db.execute("INSERT INTO migration_sources(source_id,snapshot_sha256,rows_sha256) VALUES(?,?,?)",
                            (source_id, snapshot_sha256, digest))
            self.db.execute("INSERT INTO late_legacy_imports VALUES(?,?,?,?,?)",
                            (source_id, snapshot_sha256, added, approval, self.clock()))
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        return {"source_id": source_id, "rows": len(rows), "added": added}

    def legacy_periods(self) -> dict[str, dict[str, tuple[bool, bool]]]:
        """dataset -> period label -> (any consumed legacy record, any unconsumed one), any operation."""
        result: dict[str, dict[str, tuple[bool, bool]]] = {}
        for dataset, label, consumed, pending in self.db.execute(
                "SELECT json_extract(identity_json,'$.dataset'), json_extract(identity_json,'$.period.label'),"
                " MAX(consumed), MIN(consumed)=0 FROM legacy_records WHERE identity_json IS NOT NULL GROUP BY 1, 2"):
            if dataset is not None and label is not None:
                result.setdefault(dataset, {})[label] = (bool(consumed), bool(pending))
        return result

    def page_sizes(self, dataset: str, period: str) -> list[int]:
        """Page sizes any page of this period was requested with, by the legacy owner or here."""
        sizes = {row[0] for row in self.db.execute(
            "SELECT DISTINCT json_extract(identity_json,'$.dimensions._page_size') FROM legacy_records"
            " WHERE json_extract(identity_json,'$.dataset')=? AND json_extract(identity_json,'$.period.label')=?",
            (dataset, period))}
        sizes |= {row[0] for row in self.db.execute(
            "SELECT DISTINCT json_extract(i.identity_json,'$.dimensions._page_size') FROM attempt_identities i"
            " JOIN attempts a ON a.id=i.attempt_id WHERE a.dataset=? AND a.period=? AND a.outcome!='not_sent'",
            (dataset, period))}
        return sorted(size for size in sizes if isinstance(size, int))

    def newest_task_period(self, dataset: str) -> str | None:
        row = self.db.execute("SELECT MAX(period) FROM tasks WHERE dataset=?", (dataset,)).fetchone()
        return row[0] if row else None

    def oldest_task_period(self, dataset: str) -> str | None:
        row = self.db.execute("SELECT MIN(period) FROM tasks WHERE dataset=?", (dataset,)).fetchone()
        return row[0] if row else None

    def has_tasks(self, dataset: str) -> bool:
        return self.db.execute("SELECT 1 FROM tasks WHERE dataset=? LIMIT 1", (dataset,)).fetchone() is not None

    def grant_handoff(self, receipt: dict) -> dict:
        """Record the reviewed owner handoff that lets this shadow ledger dispatch.

        The receipt names exactly the legacy snapshots imported here (their
        tombstones stay authoritative), the datasets that may be sent, a hard
        request budget, the code SHA and the user's approval. It is written
        once; a different handoff needs a new ledger.
        """
        import hashlib
        import json
        from .contracts import canonical, reject_secrets
        if set(receipt) != HANDOFF_KEYS or receipt["version"] != 1:
            raise ValueError("handoff_receipt_fields_invalid")
        reject_secrets(receipt)
        budget = receipt["request_budget"]
        if type(budget) is not int or not 1 <= budget <= 1000:
            raise ValueError("handoff_request_budget_invalid")
        if not isinstance(receipt["scope"], list) or not receipt["scope"] or \
                not all(isinstance(d, str) and d for d in receipt["scope"]) or len(set(receipt["scope"])) != len(receipt["scope"]):
            raise ValueError("handoff_scope_invalid")
        for key in ("code_sha", "approval"):
            if not isinstance(receipt[key], str) or not receipt[key].strip():
                raise ValueError(f"handoff_{key}_required")
        if not isinstance(receipt["legacy_owner"], dict) or not receipt["legacy_owner"].get("stopped_evidence"):
            raise ValueError("handoff_legacy_owner_stop_evidence_required")
        if receipt["seeding"] not in SEEDING:
            raise ValueError("handoff_seeding_invalid")
        session = receipt["session"]
        if not isinstance(session, dict) or set(session) - {"keepalive_dataset", "keepalive_period", "probe"}:
            raise ValueError("handoff_session_invalid")
        snapshots = sorted((s["source_id"], s["snapshot_sha256"]) for s in receipt["snapshots"])
        self.db.execute("BEGIN IMMEDIATE")
        try:
            owner = self.db.execute("SELECT province,account_alias FROM migration_owner WHERE singleton=1").fetchone()
            if owner is None or tuple(owner) != (receipt["province"], receipt["account_alias"]):
                raise ValueError("handoff_requires_imported_legacy_tombstones_for_this_account")
            imported = sorted(tuple(r) for r in self.db.execute("SELECT source_id,snapshot_sha256 FROM migration_sources"))
            if imported != snapshots:
                raise ValueError("handoff_snapshots_differ_from_imported_tombstones")
            if self.db.execute("SELECT 1 FROM attempts LIMIT 1").fetchone():
                raise ValueError("handoff_requires_a_fresh_ledger")
            if self.db.execute("SELECT 1 FROM owner_handoff").fetchone():
                raise ValueError("handoff_already_granted")
            document = canonical(receipt)
            digest = hashlib.sha256(document).hexdigest()
            self.db.execute("INSERT INTO owner_handoff VALUES(1,?,?,?)", (document.decode(), digest, self.clock()))
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        return {**receipt, "receipt_sha256": digest}

    def dispatch_blocker(self) -> str | None:
        """Why nothing may be sent from this ledger now, or None."""
        handoff = self.handoff()
        if self.has_legacy_shadow() and handoff is None:
            return "legacy_shadow_requires_owner_handoff"
        if handoff is not None and self.attempts_since_handoff() >= self.request_budget():
            return "handoff_request_budget_exhausted"
        return None

    def request_budget(self) -> int:
        """The handoff's budget plus every approved extension."""
        handoff = self.db.execute("SELECT receipt_json FROM owner_handoff").fetchone()
        if handoff is None:
            return 0
        import json
        added = self.db.execute("SELECT COALESCE(SUM(added_budget), 0) FROM handoff_extensions").fetchone()[0]
        return json.loads(handoff["receipt_json"])["request_budget"] + added

    def extend_handoff(self, added_budget: int, approval: str) -> dict:
        """Record a user-approved budget extension; the receipt itself never changes."""
        if type(added_budget) is not int or not 1 <= added_budget <= 100000:
            raise ValueError("handoff_extension_budget_invalid")
        if not isinstance(approval, str) or not approval.strip():
            raise ValueError("handoff_extension_approval_required")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.db.execute("SELECT receipt_sha256 FROM owner_handoff").fetchone()
            if row is None:
                raise ValueError("handoff_extension_requires_a_handoff")
            self.db.execute("INSERT INTO handoff_extensions(added_budget,approval,receipt_sha256,granted_at)"
                            " VALUES(?,?,?,?)", (added_budget, approval, row["receipt_sha256"], self.clock()))
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        return {"request_budget": self.request_budget(), "used": self.attempts_since_handoff(),
                "extensions": self.db.execute("SELECT COUNT(*) FROM handoff_extensions").fetchone()[0]}

    def attempts_since_handoff(self) -> int:
        # A handoff is granted on an attempt-free ledger, so every attempt in it
        # counts against the budget (no clock comparison to get wrong), except one
        # proven never sent (not_sent): it reached no website.
        if self.db.execute("SELECT 1 FROM owner_handoff").fetchone() is None:
            return 0
        return self.db.execute("SELECT COUNT(*) FROM attempts WHERE outcome IS NULL OR outcome!='not_sent'").fetchone()[0]

    def legacy_file_tombstone(self, file_key: str) -> bool:
        return self.db.execute("SELECT 1 FROM file_tombstones WHERE file_key=? AND source_ref NOT LIKE 'attempt:%'",
                               (file_key,)).fetchone() is not None

    def legacy_states(self, identity_key: str) -> list[str]:
        return sorted({row["state"] for row in self.db.execute(
            "SELECT state FROM legacy_records WHERE identity_key=? AND consumed=1", (identity_key,))})

    def consumed_file(self, file_key: str) -> bool:
        return self.db.execute("SELECT 1 FROM file_tombstones WHERE file_key=?", (file_key,)).fetchone() is not None

    def legacy_summary(self) -> dict:
        return {"snapshots": self.db.execute("SELECT COUNT(*) FROM migration_sources").fetchone()[0],
                "states": {r[0]: r[1] for r in self.db.execute("SELECT state,COUNT(*) FROM legacy_records GROUP BY state")},
                "unmapped": self.db.execute("SELECT COUNT(*) FROM legacy_records WHERE identity_key IS NULL").fetchone()[0],
                "dispatch_allowed": self.dispatch_blocker() is None,
                "handoff_receipt_sha256": (self.handoff() or {}).get("receipt_sha256")}

    def finish(self, attempt: int, outcome: str, *, http_status: int | None = None,
               business_status: str | None = None, reason: str | None = None) -> None:
        if outcome not in OUTCOMES or outcome == "pending":
            raise ValueError("attempt_outcome_invalid")
        cursor = self._write("UPDATE attempts SET finished_at=?, outcome=?, http_status=?, business_status=?, reason=?"
                             " WHERE id=? AND outcome='pending'",
                             (self.clock(), outcome, http_status, business_status, reason, attempt))
        if cursor.rowcount != 1:
            raise ValueError("attempt_not_pending")

    def not_sent(self, attempt: int, reason: str, *, reviewed: bool = False) -> None:
        """The attempt provably never reached the network, so it consumes nothing.

        Live: a pending attempt whose page step failed before the request gate
        let any business request out. ``reviewed``: an operator releases a
        failed one whose error the page script raises before the business call.
        Its file tombstone goes with it; the task keeps its status.
        """
        expected = "failed" if reviewed else "pending"
        self.db.execute("BEGIN IMMEDIATE")
        try:
            cursor = self.db.execute("UPDATE attempts SET outcome='not_sent', finished_at=COALESCE(finished_at, ?),"
                                     " reason=? WHERE id=? AND outcome=?", (self.clock(), reason, attempt, expected))
            if cursor.rowcount != 1:
                raise ValueError(f"attempt_not_{expected}")
            self.db.execute("DELETE FROM file_tombstones WHERE source_ref=?", (f"attempt:{attempt}",))
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def review_unsent(self, attempts: list[int], approval: str) -> list[tuple]:
        """User-approved, after review: page operations that never opened a report consume nothing.

        Anhui 2026-11-01 12:07: in a hidden tab the site's menu never reached the
        report (``NAVIGATION_CODES``, no HTTP status); one more was still
        navigating when its worker was stopped (``pending``). Each becomes
        ``not_sent``; a task isolated for it is pending again.
        """
        if not isinstance(approval, str) or not approval.strip():
            raise ValueError("review_approval_required")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            reopened = []
            for attempt in attempts:
                row = self.db.execute("SELECT * FROM attempts WHERE id=?", (attempt,)).fetchone()
                # pending: stopped mid-navigation; unknown/driver_lost: a replay on a closed page's frame
                # (Anhui 2026-11-01 13:42, "Target page … closed": the script never ran).
                if (row is None or row["kind"] not in ("collect", "sample") or row["http_status"] is not None
                        or row["business_status"] is not None
                        or not (row["outcome"] == "pending" or (row["outcome"] == "failed" and row["reason"] in NAVIGATION_CODES)
                                or (row["outcome"] == "unknown" and row["reason"] == "driver_lost"))):
                    raise ValueError(f"attempt_{attempt}_is_not_an_unsent_navigation")
                reason = "reviewed_not_sent:" + (row["reason"] or "stopped_while_navigating")
                self.db.execute("UPDATE attempts SET outcome='not_sent', finished_at=COALESCE(finished_at, ?), reason=?"
                                " WHERE id=?", (self.clock(), reason, attempt))
                self.db.execute("DELETE FROM file_tombstones WHERE source_ref=?", (f"attempt:{attempt}",))
                self.db.execute("INSERT INTO released_attempts VALUES(?,?,?)", (attempt, approval, self.clock()))
                scope = re.sub(r"(^|\.)p\d{4}$", "", row["scope"])
                task = self.db.execute("SELECT status, reason FROM tasks WHERE dataset=? AND period=? AND scope=?",
                                       (row["dataset"], row["period"], scope)).fetchone()
                if task is not None and task["status"] == "failed" and (task["reason"] or "").startswith("isolated:"):
                    self.db.execute("UPDATE tasks SET status='pending', reason=NULL, updated_at=? WHERE dataset=? AND"
                                    " period=? AND scope=?", (self.clock(), row["dataset"], row["period"], scope))
                reopened.append((attempt, row["dataset"], row["period"], scope))
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        return reopened

    def release_denied(self, attempts: list[int], approval: str, *, defer: bool = False) -> list[tuple]:
        """User-approved: denied attempts (HTTP 200 + 40302) stop consuming their items, which return to pending.

        For a denial the user attributes to a broken session or to data not yet
        published (``defer``: the item waits for the next day). The attempts
        stay in the history as they were.
        """
        if not isinstance(approval, str) or not approval.strip():
            raise ValueError("release_approval_required")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            reopened = []
            for attempt in attempts:
                row = self.db.execute("SELECT * FROM attempts WHERE id=?", (attempt,)).fetchone()
                if row is None or (row["kind"], row["outcome"], row["http_status"], row["business_status"]) != (
                        "collect", "failed", 200, "40302"):
                    raise ValueError(f"attempt_{attempt}_is_not_a_denial")
                # A page's attempt carries its page in the scope ("<scope>.p0001", unscoped "p0001"); the task does not.
                scope = re.sub(r"(^|\.)p\d{4}$", "", row["scope"])
                task = self.db.execute("SELECT status, reason FROM tasks WHERE dataset=? AND period=? AND scope=?",
                                       (row["dataset"], row["period"], scope)).fetchone()
                if task is None or (task["status"], task["reason"]) != ("failed", "denied:40302"):
                    raise ValueError(f"attempt_{attempt}_task_is_not_denied")
                self.db.execute("INSERT INTO released_attempts VALUES(?,?,?)", (attempt, approval, self.clock()))
                self.db.execute("UPDATE tasks SET status=?, reason=?, updated_at=? WHERE dataset=? AND period=? AND scope=?",
                                ("deferred" if defer else "pending", "unpublished:40302" if defer else None,
                                 self.clock(), row["dataset"], row["period"], scope))
                reopened.append((attempt, row["dataset"], row["period"], scope))
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        return reopened

    def release_early(self, attempt: int, approval: str) -> tuple:
        """User-approved: an answer saved before its day was published (a partial or empty day) stops
        consuming its item, which is pending again and waits for its day (``lag_days``).

        Anhui 2026-11-01 11:31: 实际负荷 of the running day was saved with 46
        real points and 50 zeros. The early original stays in the archive and
        the attempt in the history.
        """
        if not isinstance(approval, str) or not approval.strip():
            raise ValueError("release_approval_required")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.db.execute("SELECT * FROM attempts WHERE id=?", (attempt,)).fetchone()
            if row is None or (row["kind"], row["outcome"]) not in (("collect", "success"), ("collect", "empty")):
                raise ValueError(f"attempt_{attempt}_is_not_a_saved_answer")
            task = self.db.execute("SELECT status FROM tasks WHERE dataset=? AND period=? AND scope=?",
                                   (row["dataset"], row["period"], row["scope"])).fetchone()
            if task is None or task["status"] not in ("saved", "empty"):
                raise ValueError(f"attempt_{attempt}_task_is_not_settled")
            if self.db.execute("SELECT 1 FROM released_attempts WHERE attempt_id=?", (attempt,)).fetchone():
                raise ValueError(f"attempt_{attempt}_already_released")
            self.db.execute("INSERT INTO released_attempts VALUES(?,?,?)", (attempt, approval, self.clock()))
            self.db.execute("UPDATE tasks SET status='pending', reason=?, updated_at=? WHERE dataset=? AND period=? AND scope=?",
                            (f"released_early:{attempt}", self.clock(), row["dataset"], row["period"], row["scope"]))
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        return (attempt, row["dataset"], row["period"], row["scope"])

    def latest_denial(self, dataset: str, period: str) -> int | None:
        """The newest unreleased HTTP 200 + 40302 attempt of a period (any page)."""
        row = self.db.execute("SELECT id FROM attempts WHERE dataset=? AND period=? AND http_status=200"
                              " AND business_status='40302' AND outcome='failed'"
                              " AND id NOT IN (SELECT attempt_id FROM released_attempts) ORDER BY id DESC LIMIT 1",
                              (dataset, period)).fetchone()
        return row["id"] if row else None

    def reopen_deferred(self, before: float) -> int:
        """Deferred items settled before ``before`` (the day's start) are pending again: one try a day."""
        cursor = self._write("UPDATE tasks SET status='pending', reason=NULL, updated_at=? WHERE status='deferred'"
                             " AND updated_at<?", (self.clock(), before))
        return cursor.rowcount

    def unsettled(self) -> list[sqlite3.Row]:
        return list(self.db.execute("SELECT * FROM attempts WHERE outcome='pending' ORDER BY id"))

    def consumed(self, dataset: str, period: str, scope: str = "") -> bool:
        """Any dispatched collect/sample consumes the identity, even after ack.

        A successful response before an archive/checkpoint crash must never be
        sent again just because the task still says pending. Keepalive/probes
        are separate explicitly approved operations. A ``not_sent`` attempt was
        never dispatched.
        """
        return self.db.execute(
            "SELECT 1 FROM attempts WHERE dataset=? AND period=? AND scope=?"
            " AND kind IN ('collect','sample') AND outcome!='not_sent'"
            " AND id NOT IN (SELECT attempt_id FROM released_attempts) LIMIT 1",
            (dataset, period, scope)).fetchone() is not None

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

    def last_business_end(self, datasets: list[str] | None = None) -> float:
        """Latest business response: what the site counts as activity (not TIME probes).

        ``datasets``: only those (e.g. one subsystem's, whose session idles on its own).
        """
        marks = ",".join("?" * len(BUSINESS_KINDS))
        where, args = "", list(BUSINESS_KINDS)
        if datasets is not None:
            where = f" AND dataset IN ({','.join('?' * len(datasets))})"
            args += list(datasets)
        row = self.db.execute(f"SELECT MAX(finished_at) t FROM attempts WHERE kind IN ({marks})"
                              f" AND outcome IN ('success', 'empty'){where}", args).fetchone()
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
