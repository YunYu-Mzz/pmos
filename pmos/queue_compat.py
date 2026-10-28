"""Read-only legacy queue decoders and an offline, non-dispatchable index.

No Crawlee/legacy module is imported. Old state is never opened for writing.
Every old ID is retained, including orphan outcomes and unresolved frontiers.
An online ownership handoff is deliberately outside this module's authority.
"""
from __future__ import annotations

from collections import Counter
from contextlib import closing
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import sqlite3

from .contracts import TaskIdentity, canonical, reject_secrets

PROVINCES = {"liaoning", "anhui", "shanxi"}
TERMINAL = {"saved", "empty", "failed", "denied", "skipped"}
UNKNOWN = {"sent", "leased", "reserved", "interrupted", "unknown", "attempting", "running", "pending_review",
           "prior_attempt_requires_review", "download_saved_pending_validation"}


@dataclass(frozen=True)
class LegacyTask:
    legacy_key: str
    dataset: str
    legacy_state: str
    state: str
    consumed: bool
    identity: TaskIdentity | None = None
    blockers: tuple[str, ...] = ()
    facts_sha256: str = ""

    def summary(self) -> dict:
        return {"legacy_key_sha256": hashlib.sha256(self.legacy_key.encode()).hexdigest(),
                "dataset": self.dataset, "legacy_state": self.legacy_state, "state": self.state,
                "consumed": self.consumed, "identity_key": self.identity.key if self.identity else None,
                "blockers": list(self.blockers), "facts_sha256": self.facts_sha256,
                "dispatch_allowed": False}


@dataclass(frozen=True)
class QueueContext:
    province: str
    account_alias: str
    dataset: str = ""
    period_kind: str | None = None
    operation: str = "query"
    dataset_aliases: dict[str, str] = field(default_factory=dict)
    snapshot_anchor: str | None = None

    def check(self):
        if self.province not in PROVINCES or not self.account_alias:
            raise ValueError("explicit_province_account_required")
        if self.period_kind not in (None, "day", "week", "month", "year", "snapshot", "range", "week_start_monday"):
            raise ValueError("explicit_period_kind_required")


def read_json(path: Path, *, expected_sha256: str | None = None) -> tuple[dict, str]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 64 * 1024 * 1024:
        raise ValueError("bounded_regular_queue_snapshot_required")
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if expected_sha256 is not None and digest != expected_sha256:
        raise ValueError("queue_snapshot_sha256_changed")
    value = json.loads(raw)
    reject_secrets(value)
    if not isinstance(value, dict):
        raise ValueError("legacy_queue_object_required")
    return value, digest


def normalize_state(value: str, *, attempted=False, active=False) -> tuple[str, bool]:
    if value in {"saved", "captured", "complete", "completed", "succeeded", "download_values_validated"}:
        return "saved", True
    if value in {"empty", "success_empty_collection"}:
        return "empty", True
    if value in {"denied", "denied_scope", "permission_denied", "permission_skipped"}:
        return "denied", True
    if value in {"failed", "failure", "error"}:
        return "failed", True
    if active or attempted or value in UNKNOWN:
        return "unknown", True
    if value in {"pending", "queued", "new"}:
        return "pending", False
    if value in {"skipped", "retired", "superseded"}:
        return "skipped", True
    return "held", True


def _identity(context: QueueContext, dataset: str, period, dimensions=None, page=None,
              *, period_kind=None, operation=None) -> TaskIdentity | None:
    kind = period_kind or context.period_kind
    if not dataset or kind is None or period in (None, ""):
        return None
    # Query dates / snapshot anchors are preserved verbatim. Never infer a
    # statistical month from the publication date or use date as a member ID.
    task = TaskIdentity(context.province, context.account_alias,
        context.dataset_aliases.get(dataset, dataset), {"kind": kind, "label": str(period)},
        dimensions or {}, page=page, operation=operation or context.operation)
    try:
        task.document()
    except (ValueError, TypeError):
        return None  # retain the old key as an unmapped, non-dispatchable fact
    return task


def _task(context, key, dataset, raw, *, status=None, period=None, dimensions=None, page=None,
          active=False, extra_blockers=(), period_kind=None, operation=None):
    status = str(status if status is not None else raw.get("status", raw.get("phase", "pending")))
    evidence_markers = ("attempted", "attempt", "issued", "sent", "attempted_at", "reason",
                        "native_receipt", "receipt", "original", "source_path", "parsed_path", "response_sha256")
    containers = [raw] + [raw[key] for key in ("job", "attempt_record") if isinstance(raw.get(key), dict)]
    attempted = any(item.get(key) for item in containers for key in evidence_markers)
    state, consumed = normalize_state(status, attempted=attempted, active=active)
    blockers = list(extra_blockers)
    if active:
        blockers.append("legacy_active_owner_unsettled")
    identity = _identity(context, dataset, period, dimensions, page, period_kind=period_kind, operation=operation)
    if identity is None:
        blockers.append("identity_mapping_required")
    reject_secrets(raw)
    return LegacyTask(str(key), dataset, status, state, consumed, identity, tuple(sorted(set(blockers))),
                      hashlib.sha256(canonical(raw)).hexdigest())


def decode_journal(value: dict, context: QueueContext) -> list[LegacyTask]:
    """Crawlee batch_journal.json v1, including day/month/year subclasses."""
    context.check()
    if value.get("schema_version") != 1 or not isinstance(value.get("dates"), list) or not isinstance(value.get("outcomes"), dict):
        raise ValueError("legacy_batch_journal_v1_required")
    dataset = value.get("dataset")
    if not isinstance(dataset, str) or context.dataset and dataset != context.dataset:
        raise ValueError("legacy_dataset_mismatch")
    kind = value.get("granularity", context.period_kind)
    if kind is None:
        raise ValueError("journal_granularity_must_be_explicit")
    batch = value.get("batch") or {}
    confirmed = batch.get("confirmed", {})
    if not isinstance(confirmed, dict) or not isinstance(batch.get("dates", []), list):
        raise ValueError("legacy_batch_shape_changed")
    dates = list(dict.fromkeys([*value["dates"], *value["outcomes"], *batch.get("dates", []), *confirmed]))
    rows = []
    for period in dates:
        outcomes = {v for v in (value["outcomes"].get(period), confirmed.get(period)) if v is not None}
        status = next(iter(outcomes)) if len(outcomes) == 1 else "pending"
        blockers = []
        if len(outcomes) > 1:
            status = "unknown"
            blockers.append("conflicting_legacy_outcomes")
        reserved = period in batch.get("dates", []) and not outcomes
        if reserved:
            status = "interrupted"  # acknowledged alone does not prove not sent
        rows.append(_task(context, f"{dataset}:{kind}:{period}", dataset,
            {"period": period, "outcomes": sorted(outcomes), "batch_status": batch.get("status")},
            status=status, period=period, period_kind=kind, active=reserved, extra_blockers=blockers))
    return rows


def decode_jobs(value: dict, context: QueueContext, *, attempts: dict | None = None) -> list[LegacyTask]:
    """Anhui jobs dict plus its separate native_attempts dict (both required)."""
    context.check()
    if not isinstance(value.get("jobs"), dict):
        raise ValueError("legacy_jobs_mapping_required")
    attempts_supplied = attempts is not None
    attempts = attempts if attempts is not None else {}
    result = []
    for key in dict.fromkeys([*value["jobs"], *attempts]):
        job = value["jobs"].get(key, {})
        attempt = attempts.get(key, {})
        if not isinstance(job, dict) or not isinstance(attempt, dict):
            raise ValueError("legacy_job_or_attempt_shape_changed")
        raw = {"job": job, "attempt_record": attempt}
        state = attempt.get("phase", attempt.get("status")) or job.get("status", job.get("phase", "pending"))
        blockers = []
        if not attempts_supplied:
            blockers.append("attempt_index_required")
        if key not in value["jobs"]:
            blockers.append("orphan_attempt_preserved")
        # Attempt flags outrank an innocent-looking pending queue entry.
        effective = {**job, "attempted": bool(attempt) or bool(job.get("attempted") or job.get("attempt"))}
        raw.update(effective)
        period = attempt.get("period", job.get("anchor_date", job.get("period", job.get("date", job.get("month")))))
        dimensions = attempt.get("selectors", job.get("selectors", value.get("selectors", {})))
        kind = context.period_kind
        if kind is None:
            # These are actual declared controls, not publication/mtime
            # inference. A label with no corresponding control stays unmapped.
            observed = [k for k, field in (("day", "日期"), ("month", "月份"), ("year", "年份"))
                        if isinstance(dimensions, dict) and dimensions.get(field) == period]
            if len(observed) == 1:
                kind = observed[0]
            elif "month" in job and job["month"] == period:
                kind = "month"
        if "generation_type" in job:
            dimensions = {**dimensions, "generation_type": job["generation_type"]}
        if "page" in job and period in (None, ""):
            period = context.snapshot_anchor
        if value.get("version") is not None:
            dimensions = {**dimensions, "source_version": value["version"]}
        result.append(_task(context, key, job.get("dataset", context.dataset), raw, status=state,
            period=period, dimensions=dimensions, page=job.get("page"), extra_blockers=blockers, period_kind=kind))
    return result


def decode_liaoning(value: dict, context: QueueContext, *, period_kinds: dict | None = None,
                     source_contracts: dict | None = None) -> list[LegacyTask]:
    """Advanced tasks and verified-source pending/outcomes, without reseeding."""
    context.check()
    if context.province != "liaoning":
        raise ValueError("liaoning_context_required")
    groups = value.get("datasets", value.get("sources"))
    if not isinstance(groups, dict):
        raise ValueError("liaoning_task_groups_required")
    active = value.get("active") or {}
    if isinstance(active.get("job"), dict):
        active = active["job"]
    groups = dict(groups)
    orphan_dataset = active.get("dataset", "") if active else None
    if active and orphan_dataset not in groups:
        groups[orphan_dataset] = {"pending": [active], "outcomes": {}, "orphan_active": True}
    rows = []
    for dataset, group in groups.items():
        tasks = group.get("tasks")
        if tasks is None:
            tasks = {task["key"]: task for task in group.get("pending", [])}
        outcomes = group.get("outcomes", {})
        if not isinstance(tasks, dict) or not isinstance(outcomes, dict):
            raise ValueError("liaoning_task_or_outcome_shape_changed")
        keys = list(dict.fromkeys([*tasks, *outcomes] + ([active["key"]] if active.get("dataset") == dataset and active.get("key") else [])))
        for key in keys:
            job = tasks.get(key, active if active.get("key") == key else {})
            outcome = outcomes.get(key, {})
            if isinstance(outcome, str):
                outcome = {"outcome": outcome}
            identity = outcome.get("identity", {})
            period = job.get("period", job.get("snapshot_date", identity.get("period")))
            params = job.get("params", identity)
            dimensions = {k: v for k, v in params.items() if k not in {"period", "date", "month", "year", "page", "pageNum", "pageSize"}}
            page = job.get("page", identity.get("page"))
            blockers = ["legacy_source_hold"] if group.get("hold") or group.get("suspended") else []
            if value.get("helper_hold") or value.get("hold"):
                blockers.append("legacy_global_hold")
            if group.get("orphan_active"):
                blockers.append("orphan_active_preserved")
            if group.get("_frontier_stage"):
                blockers.append("legacy_stage_requires_settlement")
            status = outcome.get("outcome") or job.get("status", job.get("phase", "pending"))
            if outcome.get("outcome") and job.get("status") in {"failed", "denied", "unknown"}:
                blockers.append("conflicting_legacy_outcomes")
            flags = bool(job.get("attempted") or job.get("attempt") or job.get("issued") or job.get("sent") or job.get("attempted_at"))
            mapping_missing = False
            rule = (source_contracts or {}).get(dataset)
            if job.get("params"):
                if "snapshot_date" in job and params == {"page": page, "snapshot_date": job["snapshot_date"]}:
                    dimensions = {}
                elif rule and isinstance(rule.get("period_field"), str):
                    date_field, selection_field = rule["period_field"], rule.get("selection_field")
                    permitted = {date_field} | ({selection_field} if selection_field else set())
                    if set(params) != permitted or params.get(date_field) != period:
                        mapping_missing = True
                    else:
                        dimensions = {selection_field: params[selection_field]} if selection_field else {}
                        if identity and (identity.get("period") != period or
                                identity.get("selection") != params.get(selection_field)):
                            mapping_missing = True
                else:
                    mapping_missing = True
            elif rule and identity:
                selection_field = rule.get("selection_field")
                dimensions = {selection_field: identity["selection"]} if selection_field and "selection" in identity else {}
            if rule and rule.get("paginated") is not True:
                page = None
            if mapping_missing:
                blockers.append("source_parameter_identity_mapping_required")
            task = _task(context, key, dataset, {"job": job, "outcome": outcome, "attempted": flags,
                "frontier_stage": group.get("_frontier_stage")},
                status=status, period=period, dimensions=dimensions, page=page,
                active=active.get("key") == key, extra_blockers=blockers,
                period_kind="snapshot" if "snapshot_date" in job else (period_kinds or {}).get(dataset))
            if mapping_missing:
                task = LegacyTask(task.legacy_key, task.dataset, task.legacy_state, task.state, task.consumed,
                                  None, tuple(sorted(set(task.blockers + ("identity_mapping_required",)))), task.facts_sha256)
            rows.append(task)
    return rows


def decode_native_request(row: dict, context: QueueContext) -> LegacyTask:
    """Shanxi native requests schema; contract declares the actual date fields."""
    context.check()
    body = json.loads(row["body"]) if isinstance(row["body"], str) else row["body"]
    contract = json.loads(row["contract"]) if isinstance(row["contract"], str) else row["contract"]
    if not isinstance(body, dict) or not isinstance(contract, dict):
        raise ValueError("native_queue_body_contract_required")
    data = body if contract.get("request_envelope") == "flat" else body.get("data")
    if not isinstance(data, dict):
        raise ValueError("native_business_parameters_required")
    fields = contract.get("date_fields")
    if not isinstance(fields, list):
        raise ValueError("native_reviewed_date_fields_required")
    for key in fields:
        if data.get(key) != row["period"]:
            raise ValueError("native_period_identity_mismatch")
    dimensions = {k: v for k, v in data.items() if k not in fields}
    kind = context.period_kind if fields else "snapshot"
    blockers = []
    if contract.get("request_envelope") != "flat":
        page_info = body.get("pageInfo")
        if not isinstance(page_info, dict) or page_info.get("pageNum") != row["page"] or type(row["page"]) is not int:
            raise ValueError("native_page_identity_mismatch")
        size = page_info.get("pageSize")
        if type(size) is not int or size < 1:
            raise ValueError("native_page_size_invalid")
        reviewed = contract.get("reviewed_page_size", contract.get("observed_request", {}).get("body", {}).get("pageInfo", {}).get("pageSize"))
        if reviewed is None:
            blockers.append("native_page_size_contract_required")
        elif reviewed != size:
            raise ValueError("native_page_size_contract_mismatch")
        dimensions = {**dimensions, "_page_size": size}
    task = _task(context, row["identity"], row["dataset"],
        {"status": row["status"], "path": row["path"], "body": body, "contract": contract},
        status=row["status"], period=row["period"], dimensions=dimensions, page=row["page"], period_kind=kind,
        extra_blockers=blockers)
    if blockers:
        return LegacyTask(task.legacy_key, task.dataset, task.legacy_state, task.state, task.consumed,
                          None, task.blockers + ("identity_mapping_required",), task.facts_sha256)
    return task


def decode_frontier(value: dict, context: QueueContext) -> list[LegacyTask]:
    """Liaoning native engine's independent single-source stage frontier."""
    dataset = value.get("dataset", context.dataset)
    if not dataset or context.dataset and context.dataset != dataset:
        raise ValueError("explicit_frontier_dataset_required")
    if not isinstance(value.get("pending"), list) or not isinstance(value.get("outcomes"), dict):
        raise ValueError("native_frontier_shape_required")
    active = value.get("active") or {}
    job = active.get("job", active)
    if job:
        job = {**job, "dataset": dataset}
    group = {**value, "_frontier_stage": active.get("stage")}
    return decode_liaoning({"sources": {dataset: group}, "active": job}, context)


def decode_publication(row: dict, context: QueueContext) -> LegacyTask:
    """Shanxi publication/attachment aliases retain an undated query anchor.

    File ID belongs to dimensions. A publication date is never silently used
    as its statistical period; without an explicit snapshot anchor it is held.
    """
    context.check()
    command = json.loads(row["command"]) if isinstance(row["command"], str) else row["command"]
    kind = row["kind"]
    if kind == "publication":
        params = command["queryParam"]
        dimensions = params["data"]
        page = params["pageInfo"]["pageNum"]
        operation = "listing"
    elif kind == "attachment":
        dimensions = command["query"]
        if not isinstance(dimensions.get("fileId"), str) or not dimensions["fileId"].strip():
            raise ValueError("native_attachment_id_required")
        dimensions = {"file_id": dimensions["fileId"]}
        page, operation = None, "attachment"
    else:
        raise ValueError("native_publication_kind_changed")
    return _task(context, row["identity"], context.dataset, {"command": command, "status": row["status"]},
                 status=row["status"], period="undated" if kind == "attachment" else context.snapshot_anchor, dimensions=dimensions, page=page,
                 period_kind="snapshot", operation=operation)


def read_native_sqlite(path: Path, context: QueueContext, *, batch_size=64, table="requests") -> list[LegacyTask]:
    """Short readonly SELECT batches, closing before JSON parsing or file I/O.

    A live rolling snapshot is not an atomic cutover export. No journal mode,
    immutable flag, checkpoint or backup command touches the source DB.
    """
    if not path.is_file() or path.is_symlink() or not 1 <= batch_size <= 128 or table not in {"requests", "work"}:
        raise ValueError("bounded_readonly_queue_required")
    with path.open("rb") as source:
        header = source.read(20)
    if len(header) < 20 or header[:16] != b"SQLite format 3\x00" or header[18:20] != b"\x01\x01":
        # A readonly WAL connection may create/update shared-memory sidecars.
        # Require a separately frozen rollback-format export for such stores.
        raise ValueError("wal_or_unknown_source_requires_frozen_export")
    uri = path.resolve().as_uri() + "?mode=ro"
    def fetch(sql, params=()):
        with closing(sqlite3.connect(uri, uri=True, timeout=0.1)) as db:
            db.execute("PRAGMA query_only=ON")
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute(sql, params).fetchall()]
    ceiling = fetch(f"SELECT max(rowid) AS n FROM {table}")[0]["n"]
    after, result = 0, []
    while ceiling is not None:
        columns = ("identity,dataset,period,path,body,contract,page,status" if table == "requests" else
                   "identity,kind,scope,command,binding,status")
        rows = fetch(f"SELECT rowid AS _rowid,{columns} FROM {table}"
                     " WHERE rowid>? AND rowid<=? ORDER BY rowid LIMIT ?", (after, ceiling, batch_size))
        if not rows:
            break
        after = rows[-1]["_rowid"]
        decoder = decode_native_request if table == "requests" else decode_publication
        result.extend(decoder(row, context) for row in rows)
    return result


def summarize(rows: list[LegacyTask]) -> dict:
    return {"records": len(rows), "states": dict(Counter(row.state for row in rows)),
            "mapped": sum(row.identity is not None for row in rows),
            "unmapped": sum(row.identity is None for row in rows),
            "consumed": sum(row.consumed for row in rows),
            "blocked": sum(bool(row.blockers) for row in rows), "dispatch_allowed": False,
            "source_atomically_frozen": False, "website_requests": 0}


def import_shadow(ledger, rows: list[LegacyTask], context: QueueContext, *, source_id: str, snapshot_sha256: str) -> dict:
    """Index into a NEW offline Ledger; never grant any task dispatch rights.

    Snapshots accumulate monotonically. A later pending/saved row cannot erase
    earlier unknown, denied or failed facts. All aliases and contradictory
    outcomes are retained instead of overwriting the more inconvenient one.
    """
    context.check()
    root = Path(__file__).resolve().parents[2]
    if any(ledger.path.resolve().is_relative_to(root / province) for province in PROVINCES):
        raise ValueError("legacy_directory_never_a_shadow_target")
    if not source_id or len(snapshot_sha256) != 64 or any(c not in "0123456789abcdef" for c in snapshot_sha256):
        raise ValueError("explicit_snapshot_reference_required")
    keys = [row.legacy_key for row in rows]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate_legacy_keys_in_snapshot")
    for row in rows:
        if row.identity and (row.identity.province != context.province or row.identity.account_alias != context.account_alias):
            raise ValueError("cross_province_or_account_mapping_forbidden")
    ledger.db.execute("BEGIN IMMEDIATE")
    try:
        if ledger.db.execute("SELECT 1 FROM attempts LIMIT 1").fetchone() or ledger.db.execute("SELECT 1 FROM tasks LIMIT 1").fetchone():
            # If dispatch won the transaction race, importing cannot turn its
            # in-flight ledger into a shadow. If import wins, begin's fence
            # rejects dispatch. Compatibility targets are always fresh indexes.
            raise ValueError("fresh_shadow_ledger_required")
        owner = ledger.db.execute("SELECT province,account_alias FROM migration_owner WHERE singleton=1").fetchone()
        if owner and tuple(owner) != (context.province, context.account_alias):
            raise ValueError("ledger_province_account_mismatch")
        ledger.db.execute("INSERT OR IGNORE INTO migration_owner VALUES(1,?,?)", (context.province, context.account_alias))
        existing = ledger.db.execute("SELECT rows_sha256 FROM migration_sources WHERE source_id=? AND snapshot_sha256=?",
                                     (source_id, snapshot_sha256)).fetchone()
        digest = hashlib.sha256(canonical([row.summary() for row in rows])).hexdigest()
        if existing and existing["rows_sha256"] != digest:
            raise ValueError("snapshot_mapping_changed_without_new_review")
        ledger.db.execute("INSERT OR IGNORE INTO migration_sources(source_id,snapshot_sha256,rows_sha256) VALUES(?,?,?)",
                          (source_id, snapshot_sha256, digest))
        for row in rows:
            ledger.db.execute("INSERT OR IGNORE INTO legacy_records"
                "(source_id,snapshot_sha256,legacy_key,identity_key,identity_json,state,consumed,blockers_json,facts_sha256)"
                " VALUES(?,?,?,?,?,?,?,?,?)", (source_id, snapshot_sha256, row.legacy_key,
                    row.identity.key if row.identity else None,
                    canonical(row.identity.document()).decode() if row.identity else None,
                    row.state, int(row.consumed), json.dumps(row.blockers), row.facts_sha256))
            if row.identity and row.consumed and row.identity.file_key:
                ledger.db.execute("INSERT OR IGNORE INTO file_tombstones VALUES(?,?,?)",
                    (row.identity.file_key, row.identity.key, source_id + ":" + snapshot_sha256))
        ledger.db.execute("COMMIT")
    except BaseException:
        ledger.db.execute("ROLLBACK")
        raise
    return {**summarize(rows), "shadow_imported": True, "live_handoff_completed": False}
