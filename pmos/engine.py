"""The province worker: one serialized request at a time, durable accounting.

Each ``tick`` decides exactly one thing:

1. a visible logout notice  -> block (auth), stop all requests
2. a block awaiting review   -> hold; keep the login alive only if the fault
                                allows it (faults.decide), first failure ends it
3. an unsettled attempt      -> block; a human settles it (``pmos ack``)
4. collection off / paused / complete / waiting for spacing
                              -> session keeping (renewing request when the
                                 site would otherwise count us idle)
5. otherwise                 -> collect the next pending task

The engine is transport-agnostic: ``transport.send(request) -> Response`` and
``check_session() -> Fault | None`` are injected (browser in production, fakes
in tests). Adaptation commands (``pmos <id> sample``) are queued in state and
run by the same worker, so they share its pacing and its browser connection.
"""
from __future__ import annotations

import asyncio
import fcntl
import inspect
import os
import time
from dataclasses import dataclass

from .archive import Archive
from .contracts import TaskIdentity
from .faults import Decision, DriverLost, Fault, decide, driver_gone
from .ledger import BUDGET_KINDS, Ledger, validate_ledger_path
from .pacing import DAY, wait_seconds
from .periods import last_day, today
from .province import Province
from .spec import DatasetSpec
from .state import StateStore
from .validate import Outcome, Response, evaluate

PROBE_RESPONSE = {"kind": "json", "empty_when_no_data": False}
# Local faults that describe the worker or province, never one item.
GLOBAL_LOCAL_FAULTS = frozenset({
    "spacing_not_elapsed", "stop_all_requested", "handoff_request_budget_exhausted", "province_blocked",
    "unsettled_attempt", "legacy_shadow_requires_owner_handoff", "dataset_outside_handoff_scope",
    "request_template_missing", "request_template_stale", "report_frame_missing", "internal_error",
    "business_identity_context_mismatch"})
# This many isolated failures in a row in one dataset pause that dataset: the
# fault is then the dataset's, and each further item would only be consumed.
ISOLATION_STREAK = 3
# Likewise for HTTP 200 + 40302 denials (production records them and goes on;
# 2026-10-31 one date's sections were denied one after another).
DENIAL_STREAK = 5
# Shanxi 2026-10-31 22:03: one disclosure page query after another answered
# HTTP 200 + 40302 while the same service's exports kept working. The queries'
# permission had lapsed (entering 信息披露 from the portal restores it); it
# was no denial. 40302 on queries of this many datasets in a row, with no
# answered query of the service in between, is that lapse: the items stay
# pending, the service's queries wait, its exports go on, and PMOS enters the
# report from the portal menu and verifies with one query.
QUERY_LAPSE_DATASETS = 2
QUERY_TRANSPORTS = ("component", "native")
QUERY_LAPSE_RECOVERIES_PER_HOUR = 3
PAGE_RECOVERIES_PER_HOUR = 3
DRIVER_LOSSES_TO_STOP = 3
# A prime re-reads an already collected record so the page shows its own request (as the Anhui
# legacy re-captures one after every restart); its answer is only a sample, never a task's result,
# so it is not an identity that may be asked for once (Anhui 2026-11-01 13:23: a restart refused it).
PRIME_SCOPE = "template_prime"
# Page operations (menu, form, dropdown) failing in a row, across reports, stop the province.
PAGE_FAILURES_TO_STOP = 3
# Network faults in a row (any reports) before the province stops instead of isolating the item.
NETWORK_FAULTS_TO_STOP = 3
# The page was driven and answered; only a read's own check failed (Anhui 2026-11-01 12:32:
# three dropdown reads in a row stopped a working page). Not counted as a failing page.
PAGE_ANSWERED_CODES = frozenset({"select_value_changed_by_reading", "select_text_not_restored", "select_value_unsettled"})
UNSENT_COOLDOWN_SECONDS = 600
UNSENT_HOLDS_TO_STOP = 3
LAPSE_RULE = ("用户原话：等它改完自动化再恢复（2026-10-31）：同一服务的查询在两个数据集上连续 HTTP 200 + 40302 "
              "视为查询权限失效，不记为拒绝；自动从门户菜单重新进入该报表，用一个查询验证")
# Memory upkeep runs in waits of a second or more, and at least this often: at
# a 1 s pace (user, 2026-10-31) no wait between requests reaches a second.
UPKEEP_EVERY_SECONDS = 30
UNPUBLISHED_RULE = ("用户原话：近 7 天的 40302 第二天再试（2026-10-31）：窗口内的 HTTP 200 + 40302 视为尚未发布，"
                    "第二天再试一次，超出窗口才算拒绝")


def store_document(archive: Archive, spec: DatasetSpec, item: dict, result: "Result", **receipt) -> list[dict]:
    """Archive a downloaded document (or each part of an article) with its listing metadata."""
    response, outcome = result.response, result.outcome
    pieces = list(zip(response.parts, outcome.detail["parts"])) if response.parts else [(response, outcome)]
    meta = {"record_id": item["record_id"], "title": item["title"], "published": item.get("published"),
            "period_basis": item["period_basis"], "attachment_id": item["attachment"]["id"], **receipt}
    records = []
    for index, (part, part_outcome) in enumerate(pieces, 1):
        scope = f"{item['scope']}.{index:02d}" if response.parts else item["scope"]
        records.append(archive.store(
            spec.id, item["period"], part.body, part_outcome.extension, scope=scope, http_status=part.status,
            attachment_name=part.name or item["attachment"]["name"], **meta,
            **({"container": part_outcome.detail} if part_outcome.detail else {})))
    return records


def enqueue(state: StateStore, command: dict) -> str:
    """Queue an adaptation command for the running worker (CLI side)."""
    import uuid
    command = {**command, "id": uuid.uuid4().hex[:12]}
    state.write("commands", state.read("commands", []) + [command])
    return command["id"]


@dataclass
class Result:
    outcome: Outcome
    response: Response
    record: dict | None = None
    parsed: object | None = None


class Engine:
    def __init__(self, province: Province, transport, check_session=None, *, instance=None,
                 clock=time.time, decode_response=None, account_alias="offline-default"):
        self.province = province
        self.policy = province.policy
        self.transport = transport
        self.check_session = check_session
        # Optional memory admission (pmos.memory.Admission): () -> {"allowed", "reasons"}.
        self.admission = None
        self._held_block = None
        self._app_cache = None
        self.clock = clock
        self._recycle_checked = float("-inf")
        self._primed: dict[str, float] = {}
        self._network_streak = 0
        self.decode_response = decode_response
        self.account_alias = account_alias
        self.instance = instance or province.instance()
        validate_ledger_path(self.instance / "ledger.sqlite3")
        self.state = StateStore(self.instance / "state")
        # Options found on the page since the specs were written (_discover_options).
        self.province.extra_scopes = self.state.read("discovered_options", {}) or {}
        self.ledger = Ledger(self.instance / "ledger.sqlite3", clock)
        self.archive = Archive(self.instance / "original", province.id)
        self.samples = Archive(self.instance / "samples", province.id)
        self.responses = Archive(self.instance / "responses", province.id)
        self.quarantine = Archive(self.instance / "quarantine", province.id)
        self.serial = asyncio.Lock()
        self._lock_fd: int | None = None
        self._seeded_on = None

    # -- process ownership ------------------------------------------------
    def acquire(self) -> None:
        """One worker per province instance, across processes."""
        if self._lock_fd is not None:
            return
        descriptor = os.open(self.instance / "state" / ".worker.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(descriptor)
            raise RuntimeError("another worker owns this province instance") from None
        self._lock_fd = descriptor

    def release(self) -> None:
        if self._lock_fd is not None:
            os.close(self._lock_fd)
            self._lock_fd = None
        self.ledger.close()

    # -- one request ------------------------------------------------------
    def spacing_wait(self, kind: str, lane: str | None = None) -> float:
        charged = kind in BUDGET_KINDS
        starts = self.ledger.budget_starts(self.clock() - DAY) if charged else None
        return wait_seconds(self.policy, self.clock(), self.ledger.last_response_end(), starts, charged=charged,
                            lane=lane)

    async def request(self, kind: str, request: dict, response_spec: dict, *,
                      dataset: str | None = None, period: str | None = None, scope: str = "", identity=None,
                      lane: str | None = None) -> Result:
        """Send one request if spacing allows; the attempt is durable before sending."""
        async with self.serial:
            blocker = self.ledger.dispatch_blocker()
            if blocker == "legacy_shadow_requires_owner_handoff":
                # Offline compatibility imports never authorize ownership or
                # dispatch, including keepalive and a newly invented identity.
                raise Fault(blocker, "unknown")
            if blocker:
                raise Fault(blocker, "local")
            if self.state.control().get("stop_all"):
                raise Fault("stop_all_requested", "local")
            handoff = self.ledger.handoff()
            if handoff and kind in BUDGET_KINDS and dataset not in handoff["scope"]:
                raise Fault("dataset_outside_handoff_scope", "local")
            if identity is not None:
                if (identity.province != self.province.id or identity.dataset != dataset
                        or identity.account_alias != self.account_alias):
                    raise Fault("business_identity_context_mismatch", "local")
                if kind in BUDGET_KINDS and self.ledger.consumed_identity(identity.key):
                    raise Fault("consumed_identity_requires_review", "unknown")
                if kind in BUDGET_KINDS and identity.file_key and self.ledger.consumed_file(identity.file_key):
                    raise Fault("consumed_file_requires_review", "unknown")
            block = self.state.blocked()
            if block and (kind not in ("keepalive", "probe") or not block.get("keepalive_allowed")):
                raise Fault("province_blocked", "local")
            if self.ledger.unsettled():
                raise Fault("unsettled_attempt", "local")
            if kind in BUDGET_KINDS and dataset is not None and period is not None and scope != PRIME_SCOPE:
                if self.ledger.consumed(dataset, period, scope):
                    raise Fault("consumed_identity_requires_review", "unknown")
            if self.spacing_wait(kind, lane) > 0:
                raise Fault("spacing_not_elapsed", "local")
            if request.get("transport") == "fetch" and not self._ready(request):
                # Proven local, before durable dispatch. Never create a fake
                # sent attempt for the historical missing-template failure.
                raise Fault("request_template_missing", "local")
            preflight = getattr(self.transport, "preflight", None)
            if preflight is not None and request.get("transport") in ("component", "native"):
                try:
                    await preflight(request)
                except Fault as fault:
                    if fault.category not in ("local", "network"):
                        raise
                    # Not sent, nothing consumed: the dataset waits for review.
                    raise Fault("preflight_deferred:" + fault.code, "local") from None
                request = {**request, "_preflighted": True}
            attempt = self.ledger.begin(kind, dataset, period, scope, identity=identity)
            try:
                response = await self.transport.send({"timeout": self.policy.request_timeout_seconds, **request})
            except Fault as fault:
                if request.get("transport") == "ui" and fault.category == "local" and fault.code not in PAGE_ANSWERED_CODES:
                    self._page_operation_failed(fault, dataset)
                if fault.unsent:
                    # The request gate proves nothing got out: the identity
                    # stays unconsumed, like a deferred preflight.
                    self.ledger.not_sent(attempt, fault.code)
                else:
                    self.ledger.finish(attempt, "unknown" if fault.category == "network" else "failed",
                                       reason=fault.code)
                raise
            except DriverLost:
                self.ledger.finish(attempt, "unknown", reason="driver_lost")
                raise
            except Exception as exc:
                self.ledger.finish(attempt, "unknown", reason="driver_lost" if driver_gone(exc) else "transport_exception")
                if driver_gone(exc):
                    # Died outside a guarded call (e.g. while opening a frame): no business
                    # block, or the restarted worker would refuse to start (Anhui 02:08).
                    raise DriverLost(str(exc)[:200]) from None
                raise Fault("transport_exception", "network") from None
            try:
                outcome = evaluate(response_spec, response)
                parsed = self.decode_response(response.body) if self.decode_response else None
            except Fault as fault:
                if fault.category == "site_data" and response.body:
                    self.quarantine.store(dataset or "response", period or "undated", response.body,
                                          ".bin", scope=f"attempt_{attempt}", reason=fault.code)
                self.ledger.finish(attempt, "failed", http_status=response.status,
                                   business_status=fault.business_status, reason=fault.code)
                raise
            if kind in BUDGET_KINDS and response.body:
                # A crash before task confirmation can be reviewed offline.
                self.responses.store(dataset or "response", period or "undated", response.body,
                                     outcome.extension if outcome.extension != ".parts" else ".bin",
                                     scope=f"attempt_{attempt}", outcome=outcome.result)
            elif kind in BUDGET_KINDS and response.parts:
                for number, (part, part_outcome) in enumerate(zip(response.parts, outcome.detail["parts"]), 1):
                    self.responses.store(dataset or "response", period or "undated", part.body,
                                         part_outcome.extension, scope=f"attempt_{attempt}.{number}")
            self.ledger.finish(attempt, "empty" if outcome.result == "empty" else "success",
                               http_status=response.status, business_status=outcome.business_status)
            self._network_streak = 0
            if request.get("transport") == "ui" and self.state.read("page_operation_failures"):
                self.state.write("page_operation_failures", [])
            if response.operation:
                # Every request the page made during the call, as the gate decided it.
                self.state.event("operation", attempt=attempt, dataset=dataset, **{
                    k: v for k, v in response.operation.items() if k != "blocked_paths"},
                    **({"blocked_paths": ",".join(response.operation["blocked_paths"])}
                       if response.operation.get("blocked_paths") else {}))
            return Result(outcome, response, parsed=parsed)

    # -- collection -------------------------------------------------------
    def collection_specs(self) -> list[DatasetSpec]:
        """What may be collected: the handoff scope after a cutover, else enabled specs."""
        handoff = self.ledger.handoff()
        if handoff is None:
            return self.province.enabled()
        specs = self.province.datasets()
        order = {name: index for index, name in enumerate(handoff["scope"])}
        # The receipt is the authorization; a sampled spec (proven by the legacy
        # owner's own completed rows) may be in it, a candidate or denied one not.
        return sorted((specs[name] for name in handoff["scope"]
                       if name in specs and specs[name].status in ("sampled", "verified", "enabled")),
                      key=lambda s: order[s.id])

    def seed(self) -> None:
        if self.ledger.dispatch_blocker() == "legacy_shadow_requires_owner_handoff":
            raise Fault("legacy_shadow_requires_owner_handoff", "unknown")
        day = today()
        if self._seeded_on == day:
            return
        # Items deferred as not yet published get their one try of the day.
        self.ledger.reopen_deferred(time.mktime(day.timetuple()))
        handoff = self.ledger.handoff()
        if handoff is None:
            for spec in self.collection_specs():
                due = spec.periods_to_collect(on=day)
                for key, _ in spec.scope_items():
                    self.ledger.seed(spec.id, due, key)
            self._seeded_on = day
            return
        # After a handoff only the seeded datasets grow (legacy_pending_only:
        # those an approved scope extension names); the legacy queue's pending
        # tasks were seeded at handoff.
        seeded = set(handoff["seeded"])
        # Extensions of a legacy_pending_only handoff skip what the legacy owner
        # completed; a spec_periods handoff seeds every period and lets each
        # task record its legacy_consumed skip.
        extension = handoff["seeding"] == "legacy_pending_only"
        legacy = self.ledger.legacy_periods() if seeded and extension else {}
        for spec in self.collection_specs():
            if spec.id not in seeded:
                continue
            if spec.period["kind"] == "snapshot" and self.ledger.has_tasks(spec.id):
                continue  # a registry snapshot is taken once, not every day
            done = legacy.get(spec.id, {})
            strict = bool(spec.source.get("skip_legacy_periods"))
            # A period the legacy owner completed (in any operation: its export
            # covers a native query's day too) is not seeded again; one with
            # unconsumed legacy rows is, and its consumed pages are skipped.
            # ``skip_legacy_periods`` (a changed page size): any legacy record skips it.
            due = [p for p in spec.periods_to_collect(on=day)
                   if p not in done or not (strict or (done[p][0] and not done[p][1]))]
            for key, _ in spec.scope_items():
                self.ledger.seed(spec.id, due, key)
        self._roll_forward_options(seeded, day)
        self._seeded_on = day

    def _roll_forward_options(self, seeded: set, day) -> None:
        """Days after a per-option report's newest known one get its known options (Anhui, 2026-10-31).

        The legacy worker discovers each new day's options from the page; until
        PMOS does, the known set rolls forward (a vanished option answers
        empty; a new one stays missing until discovery).
        """
        legacy = None
        for spec in self.collection_specs():
            if spec.id in seeded or not spec.source.get("roll_forward_options"):
                continue
            if legacy is None:
                legacy = self.ledger.legacy_periods()
            known = [p for p in (self.ledger.newest_task_period(spec.id), max(legacy.get(spec.id, {}) or [""]) or None) if p]
            if not known:
                continue                      # nothing discovered yet: no options to roll
            newest = max(known)
            due = [p for p in spec.periods_to_collect(on=day) if p > newest]
            if due:
                for key, _ in spec.scope_items():
                    self.ledger.seed(spec.id, due, key)
                self.state.event("options_rolled_forward", dataset=spec.id, periods=len(due), options=len(spec.scopes))
            if (spec.source.get("discover_options") and spec.source.get("ui_request")
                    and spec.id not in (self.state.read("discovery_holds", {}) or {})):
                newest_due = (due or [newest])[-1]
                queued = {(c.get("action"), c.get("dataset"), c.get("period")) for c in self.state.read("commands", []) or []}
                if (("discover_options", spec.id, newest_due) not in queued
                        and not self.ledger.consumed(spec.id, newest_due, "discover_options")):
                    self.enqueue({"action": "discover_options", "dataset": spec.id, "period": newest_due})

    def _cursor_task(self, enabled: list[DatasetSpec]):
        """A paginated task in progress keeps priority until its last page."""
        cursor = self.state.read("page_cursor")
        if not cursor or cursor["dataset"] not in {s.id for s in enabled}:
            return None
        row = self.ledger.task(cursor["dataset"], cursor["period"], cursor["scope"])
        if row is None or row["status"] != "pending":
            self.state.remove("page_cursor")
            return None
        return row

    def _next_sendable(self, enabled: list[DatasetSpec]):
        """(task, missing templates): the first pending task that can be sent now."""
        cursor = self._cursor_task(enabled)
        if cursor is not None:
            return cursor, set()
        missing = set()
        for spec in enabled:
            task = self.ledger.next_task([spec.id], newest=spec.newest_due())
            if task is None:
                continue
            if spec.request["transport"] == "fetch" and not task["scope"].startswith("f"):
                built = spec.build(task["period"], spec.scope_values(task["scope"]))
                if self._sendable(built) is None:
                    if spec.request.get("prime") and self.clock() - self._primed.get(spec.id, float("-inf")) >= 600:
                        return {"dataset": spec.id, "prime": True}, missing
                    missing.add(built.get("template", built["path"]))
                    continue
            return task, missing
        return None, missing

    def _prioritized(self, specs: list[DatasetSpec]) -> list[DatasetSpec]:
        """``prioritize``: these datasets go first, in that order; the rest keeps the handoff order.

        Anhui 2026-11-01: an acceptance run reaches the whole-day and per-option
        reports only after ~150 date-only days; nothing else changes (lag,
        priming, bootstrap and every check run as in normal collection).
        """
        first = [entry["dataset"] for entry in self.state.read("priority_datasets", []) or []]
        if not first:
            return specs
        rank = {name: index for index, name in enumerate(first)}
        return sorted(specs, key=lambda spec: rank.get(spec.id, len(rank)))

    def _priority_taken(self, dataset: str) -> None:
        """One item of a prioritized dataset was tried: its quota (``items``) shrinks; at 0 it leaves the list."""
        entries = self.state.read("priority_datasets", []) or []
        if not any(entry["dataset"] == dataset and entry.get("items") is not None for entry in entries):
            return
        left = []
        for entry in entries:
            if entry["dataset"] == dataset and entry.get("items") is not None:
                entry = {**entry, "items": entry["items"] - 1}
                if entry["items"] <= 0:
                    self.state.event("priority_quota_done", dataset=dataset)
                    continue
            left.append(entry)
        self.state.write("priority_datasets", left)

    def _waiting_publication(self, enabled: list[DatasetSpec]) -> list[str]:
        return sorted(spec.id for spec in enabled
                      if (newest := spec.newest_due()) and self.ledger.pending_after(spec.id, newest))

    def _grows_daily(self, enabled: list[DatasetSpec]) -> bool:
        handoff = self.ledger.handoff()
        if handoff is None:
            return False
        seeded = set(handoff["seeded"])
        return any(spec.id in seeded or spec.source.get("roll_forward_options") for spec in enabled)

    def dataset_holds(self) -> dict:
        holds = self.state.read("dataset_holds", {}) or {}
        now = self.clock()
        expired = [name for name, hold in holds.items() if hold.get("until") is not None and hold["until"] <= now]
        if expired:
            # A preparation failure (nothing was sent) cools down and is tried again.
            holds = {name: hold for name, hold in holds.items() if name not in expired}
            self.state.write("dataset_holds", holds)
            for name in expired:
                self.state.event("dataset_hold_expired", dataset=name)
        return holds

    def _hold_unsent(self, dataset: str, code: str, detail: dict | None = None) -> None:
        """Nothing got out (the gate proves it): the item stays unconsumed and the dataset cools down.

        Liaoning 2026-11-01 00:03: a preparation that could not be confirmed
        paused a lane for an hour although no request was sent. A cool-down of
        UNSENT_COOLDOWN_SECONDS, then a fresh try; the UNSENT_HOLDS_TO_STOP-th
        in a row stays held for review. A sent but unknown request is never
        retried (that is not this path).
        """
        streaks = self.state.read("unsent_streaks", {}) or {}
        streak = streaks.get(dataset, 0) + 1
        self.state.write("unsent_streaks", {**streaks, dataset: streak})
        holds = self.state.read("dataset_holds", {}) or {}
        if dataset in holds:
            return
        until = None if streak >= UNSENT_HOLDS_TO_STOP else self.clock() + UNSENT_COOLDOWN_SECONDS
        holds[dataset] = {"reason": "unsent:" + code, "status": "paused", "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                          **({"until": until} if until else {}), "streak": streak, **(detail or {})}
        self.state.write("dataset_holds", holds)
        self.state.event("dataset_held", dataset=dataset, reason="unsent:" + code, status="paused",
                         cooldown=until is not None, streak=streak, **(detail or {}))

    def hold_dataset(self, dataset: str, reason: str, status: str) -> None:
        holds = self.dataset_holds()
        if dataset not in holds:
            holds[dataset] = {"reason": reason, "status": status, "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
            self.state.write("dataset_holds", holds)
            self.state.event("dataset_held", dataset=dataset, reason=reason, status=status)

    async def collect(self, spec: DatasetSpec, period: str, scope: str = "") -> Result:
        if spec.documents is not None and scope:
            return await self._collect_document(spec, period, scope)
        if "pagination" in spec.request:
            return await self._collect_page(spec, period, scope)
        values = spec.scope_values(scope)
        identity = self.task_identity(spec, period, scope)
        if self._skip_consumed(spec, period, scope, identity):
            return None
        request = self._sendable(spec.build(period, values))
        if request is None:
            raise Fault("request_template_missing", "local")
        try:
            result = await self.request("collect", request, spec.response, dataset=spec.id, period=period, scope=scope,
                                        identity=identity, lane=spec.source.get("lane"))
        except Fault as fault:
            if request.get("bootstrapping") and fault.category == "local" and not fault.unsent:
                # The page could not be driven: that report waits, the others go on.
                raise Fault("bootstrap_" + fault.code, "site_data") from None
            # Proven unsent (the menu never reached the report, Anhui 2026-11-01 12:07): no
            # data fault, nothing consumed; the dataset cools down (_hold_unsent).
            raise
        if result.outcome.result == "saved":
            result.record = self.archive.store(spec.id, period, result.response.body, result.outcome.extension,
                                               scope=scope, http_status=result.response.status,
                                               business_status=result.outcome.business_status,
                                               **({"scope_values": values} if values else {}))
            # Archived first; a record that does not echo this task's option is held for review.
            echo_mismatch(spec, values, result.response.body)
            whole_day_mismatch(spec, values, result.response.body)
            self.ledger.settle_task(spec.id, period, "saved", scope=scope, sha256=result.record["sha256"])
            self.state.write("empty_streaks", {**self.state.read("empty_streaks", {}), spec.id: 0})
            streaks = self.state.read("unsent_streaks", {}) or {}
            if streaks.get(spec.id):
                self.state.write("unsent_streaks", {k: v for k, v in streaks.items() if k != spec.id})
        else:
            self.ledger.settle_task(spec.id, period, "empty", scope=scope, reason="verified_empty_response")
            if spec.scopes:
                # A unit or node may legitimately have no rows; never pause the
                # whole dataset because one scope was empty twice.
                return result
            streaks = self.state.read("empty_streaks", {})
            streaks[spec.id] = streaks.get(spec.id, 0) + 1
            self.state.write("empty_streaks", streaks)
            if streaks[spec.id] >= self.policy.max_consecutive_empty:
                self.hold_dataset(spec.id, "consecutive_empty_periods", "paused")
        return result

    def _skip_consumed(self, spec: DatasetSpec, period: str, scope: str, identity) -> bool:
        """A task the legacy owner already consumed is never sent: it settles as
        skipped with the legacy states. Only legacy tombstones are expected
        here; an identity this ledger dispatched itself still reaches the
        dispatch guard as an anomaly for review.
        """
        states = self.ledger.legacy_states(identity.key)
        if not states and identity.file_key and self.ledger.legacy_file_tombstone(identity.file_key):
            states = ["file_tombstone"]
        if not states:
            return False
        self.ledger.settle_task(spec.id, period, "skipped", scope=scope,
                                reason="legacy_consumed:" + ",".join(states))
        return True

    async def _collect_page(self, spec: DatasetSpec, period: str, scope: str) -> Result:
        """One page of a paginated task; the task settles after its last page."""
        from . import paging
        cursor = self.state.read("page_cursor")
        if not cursor or (cursor["dataset"], cursor["period"], cursor["scope"]) != (spec.id, period, scope):
            cursor = {"dataset": spec.id, "period": period, "scope": scope, "page_size": self._period_page_size(spec, period),
                      "page": paging.first_page(spec.request["pagination"]), "pages": [], "records": 0, "new_items": 0}
            spec = self._sized(spec, cursor["page_size"])
            # Pages the legacy owner completed are never asked again: resume after them.
            while self._skip_page(spec, period, scope, cursor["page"]):
                cursor["page"] += 1
                cursor["skipped_legacy_pages"] = cursor.get("skipped_legacy_pages", 0) + 1
        else:
            spec = self._sized(spec, cursor.get("page_size") or self._period_page_size(spec, period))
        pagination = spec.request["pagination"]
        values = spec.scope_values(scope)
        request = paging.apply(spec.build(period, values), pagination, cursor["page"])
        if not self._ready(request):
            raise Fault("request_template_missing", "local")
        attempt_scope = f"{scope}.p{cursor['page']:04d}" if scope else f"p{cursor['page']:04d}"
        result = await self.request("collect", request, spec.response, dataset=spec.id, period=period, scope=attempt_scope,
                                    identity=self.task_identity(spec, period, scope, page=cursor["page"]),
                                    lane=spec.source.get("lane"))
        try:
            count, done = paging.page_result(result.response.body, pagination, cursor["page"])
        except paging.PagingError as exc:
            self.state.remove("page_cursor")
            raise Fault(str(exc), "site_data", http_status=result.response.status) from None
        if count:
            page_scope = f"{scope}.p{cursor['page']:04d}" if scope else f"p{cursor['page']:04d}"
            record = self.archive.store(spec.id, period, result.response.body, ".json", scope=page_scope,
                                        page=cursor["page"], records=count,
                                        **({"scope_values": values} if values else {}))
            cursor["pages"].append({"page": cursor["page"], "sha256": record["sha256"], "records": count})
            cursor["records"] += count
            result.record = record
            if spec.documents is not None and self._list_documents(spec, result.response.body, pagination, cursor):
                done = True
        if not done:
            cursor["page"] += 1
            self.state.write("page_cursor", cursor)
            return result
        self.state.remove("page_cursor")
        if spec.documents is not None:
            self.ledger.supersede(spec.id, scope, period, "superseded_by_newer_listing")
            complete = self.state.read("listing_complete", {})
            if spec.id not in complete:
                # the first listing to reach the real end (early stops need one)
                self.state.write("listing_complete", {**complete, spec.id: period})
        if cursor["records"]:
            reason = f"pages={len(cursor['pages'])},records={cursor['records']}"
            if spec.documents is not None:
                reason += f",new_files={cursor.get('new_items', 0)}"
            self.ledger.settle_task(spec.id, period, "saved", scope=scope, sha256=cursor["pages"][-1]["sha256"],
                                    reason=reason)
        else:
            self.ledger.settle_task(spec.id, period, "empty", scope=scope, reason="verified_empty_listing")
        return result

    def _period_page_size(self, spec: DatasetSpec, period: str) -> int | None:
        """The spec's page size, unless the period was begun (by legacy or here) at another one.

        Pages of one period at two sizes would overlap or leave gaps between
        their page numbers: such a period finishes at the size it began with.
        """
        sizes = self.ledger.page_sizes(spec.id, period)
        if len(sizes) > 1:
            raise Fault("period_page_sizes_mixed", "local")
        return sizes[0] if sizes else spec.request["pagination"].get("page_size")

    @staticmethod
    def _sized(spec: DatasetSpec, size: int | None) -> DatasetSpec:
        current = spec.request["pagination"].get("page_size")
        return spec if size is None or size == current else spec.with_page_size(size)

    def _skip_page(self, spec: DatasetSpec, period: str, scope: str, page: int) -> bool:
        if page - paging_first(spec) + 1 > spec.request["pagination"].get("max_pages", 500):
            return False
        return bool(self.ledger.legacy_states(self.task_identity(spec, period, scope, page=page).key))

    def _list_documents(self, spec: DatasetSpec, body: bytes, pagination: dict, cursor: dict) -> bool:
        """Queue one download task per new attachment; True when listing may stop.

        Listings are newest first: with ``stop_at_known`` a page of records
        that are all known ends the listing (an earlier run saw the rest), but
        only after one listing has reached the real end of the list once.
        """
        import json
        from . import documents, paging
        try:
            items = documents.extract(paging.get_path(json.loads(body), pagination["records"]), spec.documents)
        except documents.DocumentError as exc:
            self.state.remove("page_cursor")
            raise Fault(str(exc), "site_data") from None
        fields = spec.documents.get("fields", {})
        records = [r for r in paging.get_path(json.loads(body), pagination["records"]) or [] if isinstance(r, dict)]
        ids = [str(paging.get_path(r, fields["id"])) for r in records]
        known, added = self.ledger.put_listing(spec.id, ids, items)
        cursor["new_items"] = cursor.get("new_items", 0) + added
        complete = spec.id in self.state.read("listing_complete", {})
        return bool(spec.documents.get("stop_at_known") and complete and ids and set(ids) <= known)

    async def _collect_document(self, spec: DatasetSpec, period: str, scope: str) -> Result:
        """Download one listed attachment; its request is rebuilt from the ledger item."""
        item = self.ledger.item(spec.id, period, scope)
        if item is None:
            raise Fault("document_item_missing", "local")
        response_spec = spec.documents.get("download_response", {"kind": "document"})
        identity = self.document_identity(spec, item)
        if self._skip_consumed(spec, period, scope, identity):
            return None
        result = await self.request("collect", spec.build_download(item), response_spec,
                                    dataset=spec.id, period=period, scope=scope, identity=identity,
                                    lane=spec.source.get("lane"))
        if result.outcome.result != "saved":
            self.ledger.settle_task(spec.id, period, "empty", scope=scope, reason="verified_empty_response")
            return result
        records = store_document(self.archive, spec, item, result)
        result.record = records[0]
        self.ledger.settle_task(spec.id, period, "saved", scope=scope, sha256=records[0]["sha256"],
                                reason=f"files={len(records)}" if len(records) > 1 else None)
        return result

    def task_identity(self, spec: DatasetSpec, period: str, scope: str = "", *, page=None) -> TaskIdentity:
        return task_identity(self.province.id, self.account_alias, spec, period, scope, page=page)

    def document_identity(self, spec: DatasetSpec, item: dict) -> TaskIdentity:
        record_only = spec.documents.get("record_only") is True
        return TaskIdentity(self.province.id, self.account_alias, spec.id,
            {"kind": "snapshot", "anchor": "undated"},
            {"record_id": item["record_id"]} if record_only else {"file_id": item["attachment"]["id"]},
            operation="article" if record_only else "attachment")

    # -- session keeping --------------------------------------------------
    def _keepalive_app(self, spec: DatasetSpec) -> list[str] | None:
        """The datasets of the keepalive's own site service, or None if it has none.

        A subsystem's session idles on its own: two hours of announcement
        downloads kept the Shanxi login alive while its disclosure subsystem
        lapsed, and every disclosure query then returned 40302 (2026-10-31).
        """
        app = app_of(spec)
        if app is None:
            return None
        if self._app_cache is None or self._app_cache[0] != (spec.id, app):
            self._app_cache = ((spec.id, app), [s.id for s in self.province.datasets().values() if app_of(s) == app])
        return self._app_cache[1]

    async def keep_session(self, lane: str | None = None) -> str | None:
        """Renew with a business request when the keepalive's subsystem is idle; else a TIME probe if configured.

        ``lane``: the spacing the renewal may use (a collection slot it takes).
        Returns what was sent ("renewal"/"probe") or None. Raises Fault on failure.
        """
        spec = self._keepalive_spec()
        idle = self.clock() - self.ledger.last_business_end(self._keepalive_app(spec) if spec is not None else None)
        waiting = []
        if spec is not None and idle >= self.policy.renew_after_idle_seconds and self.spacing_wait("keepalive", lane) == 0:
            # A fixed, proven period (Shanxi production renews with the day its
            # start-up check archived) wins over the latest period archived here.
            period = self._session().get("keepalive_period") or self.archive.latest_period(spec.id)
            built = spec.build(period) if period is not None else None
            request = self._sendable(built) if built is not None else None
            failed = self.state.read("renewal_bootstrap_failed")
            if built is None:
                waiting.append("renewal_needs_one_saved_period")
            elif request is None or (request.get("bootstrapping") and failed):
                waiting.append("renewal_template:" + built["path"])
            else:
                try:
                    await self.request("keepalive", request, spec.response, dataset=spec.id, period=period, lane=lane)
                except Fault as fault:
                    if not (request.get("bootstrapping") and fault.category == "local"):
                        raise
                    # The page could not be driven: never retried; one 导出
                    # click in the page (the template) resumes renewals.
                    self.state.write("renewal_bootstrap_failed", {"code": fault.code, "path": built["path"]})
                    self.state.event("renewal_bootstrap_failed", reason=fault.code)
                    waiting.append("renewal_template:" + built["path"])
                else:
                    self._note_session_keeping(None)
                    return "renewal"
        probe = self._session().get("probe")
        if probe and self.spacing_wait("probe") == 0:
            last = self.ledger.last_attempt("probe")
            last_end = max(self.ledger.last_business_end(), (last["finished_at"] or last["started_at"]) if last else 0)
            if self.clock() - last_end >= self.policy.probe_after_idle_seconds:
                request = {"transport": "fetch", "method": "POST", **probe}
                request.setdefault("template", request["path"])
                if self._ready(request):
                    await self.request("probe", request, PROBE_RESPONSE)
                    self._note_session_keeping(None if not waiting else ", ".join(waiting))
                    return "probe"
                waiting.append("probe_template:" + request["path"])
        self._note_session_keeping(", ".join(waiting) if waiting else None)
        return None

    async def _renew(self, lane: str | None) -> str | bool | None:
        """keep_session within collection: what it sent, False if nothing, None if its failure blocked.

        A renewal failure is the session's, never the waiting task's: it must
        not be settled against an item that was not sent.
        """
        try:
            return await self.keep_session(lane=lane) or False
        except Fault as fault:
            if fault.code == "spacing_not_elapsed":
                return False
            self.block(fault, decide(fault, "collect"))
            self.state.event("renewal_failed", reason=fault.code)
            return None

    def _session(self) -> dict:
        """Session keeping: the handoff receipt's reviewed settings, else the province's."""
        handoff = self.ledger.handoff()
        return {**self.province.session, **handoff["session"]} if handoff else self.province.session

    def _keepalive_spec(self) -> DatasetSpec | None:
        handoff = self.ledger.handoff()
        name = (handoff or {}).get("session", {}).get("keepalive_dataset")
        if not name:
            return self.province.keepalive_spec()
        spec = self.province.datasets().get(name)
        if spec is None or spec.status not in ("verified", "enabled") or spec.scopes or spec.period["kind"] == "snapshot":
            raise Fault("handoff_keepalive_dataset_invalid", "local")
        return spec

    def _sendable(self, request: dict) -> dict | None:
        """The request itself when its template is observed, else its bootstrap, else None."""
        if self._ready(request):
            return {k: v for k, v in request.items() if k != "bootstrap"}
        if request.get("bootstrap"):
            return {**request["bootstrap"], "bootstrapping": True}
        return None

    def _ready(self, request: dict) -> bool:
        """Session keeping is optional: an unobserved template is a wait, not a fault.

        The page's own polling or one human click provides it; nothing is sent
        (and no attempt is charged) until then.
        """
        ready = getattr(self.transport, "ready", None)
        return True if ready is None else bool(ready(request))

    def _note_session_keeping(self, waiting: str | None) -> None:
        current = (self.state.read("status", {}) or {}).get("session_keeping_waiting")
        if current != waiting:
            self.state.status(session_keeping_waiting=waiting)

    # -- the loop ---------------------------------------------------------
    def block(self, fault: Fault, decision: Decision, dataset: str | None = None) -> None:
        self.state.block(fault.code, category=fault.category, dataset=dataset,
                         keepalive_allowed=decision.keepalive, http_status=fault.http_status,
                         business_status=fault.business_status)

    async def tick(self) -> float:
        """One decision; returns seconds to sleep before the next tick."""
        blocker = self.ledger.dispatch_blocker()
        if blocker:
            self._status("offline_shadow_no_dispatch" if blocker == "legacy_shadow_requires_owner_handoff" else blocker)
            return 5
        if self.state.control().get("stop_all"):
            # Stop-all: no request of any kind, keepalive included (unlike pause).
            self._status("stopped_all")
            return 5
        if getattr(self.transport, "crashed", False):
            return await self._recover_page()
        if await self._recycle_due():
            return await self._recover_page(planned=True)
        if self.check_session is not None:
            fault = await self.check_session()
            if fault is not None:
                self.block(fault, decide(fault, "collect"))
        block = self.state.blocked()
        if block is not None:
            self._held_block = block
            return await self._hold(block)
        if self._held_block is not None:
            # A reviewed login block was cleared (after a human re-login): app
            # frames opened with the old session must not be reused.
            if self._held_block.get("category") == "auth" and hasattr(self.transport, "reset_frames"):
                await self.transport.reset_frames()
                self.state.event("frames_reset_after_relogin", block=self._held_block["reason"])
            self._held_block = None
        if self.ledger.unsettled():
            self.state.block("unsettled_attempt", category="local", keepalive_allowed=False)
            return 5
        pending = self.state.read("recycle_challenge_pending")
        if pending is not None:
            return await self._verify_after_challenge(pending)
        quiet = self._quiet_left()
        if quiet > 0:
            try:
                await self.keep_session()
            except Fault as fault:
                if fault.code != "spacing_not_elapsed":
                    raise
            self._status("quiet_window", seconds_left=round(quiet))
            return min(quiet, 60)
        if await self.run_command():
            return 1
        control = self.state.control()
        try:
            if not control.get("collect") or control.get("paused"):
                await self.keep_session()
                self._status("idle" if not control.get("collect") else "paused")
                return 5
            lapse = self.state.read("query_lapse")
            if lapse is not None:
                return await self._recover_query_lapse(lapse)
            self.seed()
            holds = self.dataset_holds()
            enabled = self._prioritized([s for s in self.collection_specs() if s.id not in holds])
            task, missing = self._next_sendable(enabled)
            if task is None:
                await self.keep_session()
                if missing:
                    # Reports whose fetch template the page has not shown yet
                    # (and that have no bootstrap) wait; nothing is blocked.
                    self._status("waiting_templates", templates=sorted(missing))
                elif holds:
                    # Held datasets keep pending items for review: not complete,
                    # so an acceptance run stays and keeps the login renewed.
                    self._status("waiting_holds", held=sorted(holds))
                elif waiting := self._waiting_publication(enabled):
                    # Days not yet published wait in the ledger: not complete either.
                    self._status("waiting_publication", datasets=waiting)
                elif self._grows_daily(enabled):
                    # Caught up, but tomorrow brings new days: the worker stays (an
                    # acceptance run ending here would leave its launchd unit stopped).
                    self._status("caught_up")
                else:
                    self._status("complete")
                return 10
            spec = next(s for s in enabled if s.id == task["dataset"])
            if isinstance(task, dict) and task.get("prime"):
                # Anhui 2026-11-01 13:14: a prime tried before the spacing elapsed was refused unsent,
                # yet marked done, so that report was skipped for 10 minutes.
                wait = self.spacing_wait("sample", spec.source.get("lane"))
                if wait > 0:
                    return min(wait, 5)
                return await self._prime_template(spec)
            wait = self.spacing_wait("collect", spec.source.get("lane"))
            if wait > 0:
                if await self._renew(None) is None:
                    return 1
                self._status("waiting", wait_seconds=round(wait, 1))
                return min(wait, 5)
            # The keepalive's subsystem idles while another one is worked: a
            # due renewal takes this collection slot. (Work in that subsystem
            # renews it by itself; a one-subsystem province never gets here.)
            keeper = self._keepalive_spec()
            app = self._keepalive_app(keeper) if keeper is not None else None
            if app is not None and spec.id not in app:
                renewed = await self._renew(spec.source.get("lane"))
                if renewed is None:
                    return 1
                if renewed:
                    self._status("renewed", last_dataset=keeper.id)
                    return 0.2
            if self._defer_incomplete(spec, task):
                return 0.2
            if self._disk_low():
                await self.keep_session()
                self._status("disk_paused", min_free_disk_mb=self.policy.min_free_disk_mb)
                return 30
            admitted = self.admission() if self.admission is not None else None
            if admitted is not None and not admitted["allowed"]:
                # Like Shanxi's memory guard: business waits, the login is still renewed.
                await self.keep_session()
                self._status("memory_paused", reasons=admitted["reasons"])
                return 5
            try:
                if await self.collect(spec, task["period"], task["scope"]) is not None:
                    self._query_answered(spec)
            finally:
                self._priority_taken(spec.id)
            self._status("collecting", last_dataset=spec.id, last_period=task["period"], last_scope=task["scope"])
            return 0.2
        except Fault as fault:
            if fault.code == "spacing_not_elapsed":
                return 1
            current = task if "task" in locals() else None
            if isinstance(current, dict) and current.get("prime"):
                # A prime is no item (no period): never isolated; a site answer still decides for the
                # province, anything else holds only this report (Anhui 13:23: KeyError 'period').
                decision = decide(fault, "collect")
                if fault.category in ("auth", "permission", "rate_limit", "network"):
                    self.block(fault, decision, current["dataset"])
                else:
                    self.hold_dataset(current["dataset"], "prime_" + fault.code, "paused")
                return 1
            if current is not None and (fault.code.startswith("preflight_deferred:") or fault.unsent):
                # Like the Shanxi coordinator's deferred scope: no request was
                # sent, the item stays pending, its dataset pauses for review
                # and the rest (keepalive included) continues.
                if fault.unsent:
                    self._hold_unsent(current["dataset"], fault.code, fault.detail)
                else:
                    self.hold_dataset(current["dataset"], fault.code, "paused")
                return 1
            if current is not None and self._isolate_item(fault, current):
                return 1
            decision = decide(fault, "collect", permission_scope=self.province.notices.get("permission_scope", "province"))
            dataset = task["dataset"] if "task" in locals() and task is not None else None
            if decision.scope == "dataset" and dataset:
                self.hold_dataset(dataset, fault.code, decision.dataset_status or "paused")
            else:
                self.block(fault, decision, dataset)
            return 1
        except DriverLost as lost:
            # Not a business fault: no block; the supervisor restarts the worker.
            self._driver_lost(str(lost))
            raise
        except Exception as exc:
            if driver_gone(exc):
                self._driver_lost(str(exc))
                raise DriverLost(str(exc)[:200]) from None
            # A bug of ours after any request was settled in the ledger.
            self.block(Fault("internal_error", "local"), Decision("province", False))
            raise

    def _hold_discovery(self, dataset: str, code: str, detail: dict | None = None) -> None:
        holds = self.state.read("discovery_holds", {}) or {}
        holds[dataset] = {"reason": code, "at": time.strftime("%Y-%m-%dT%H:%M:%S"), **(detail or {})}
        self.state.write("discovery_holds", holds)
        self.state.event("options_discovery_held", dataset=dataset, reason=code, **(detail or {}))

    async def _discover_options(self, spec: DatasetSpec, period: str) -> dict:
        """The day's options of a one-dropdown report, read from the page (Anhui single-selector discovery).

        Only options whose request value is their own text join the report
        (its replay sends the display label); they get this day's task, and
        the daily roll-forward carries them on. Others are reported only.
        """
        config, page = spec.source["discover_options"], spec.source["ui_request"]
        label = config["label"]
        dates = [{**f, "value": period} for f in page.get("fields", []) if f.get("kind") == "date"]
        request = {"transport": "ui", "action": "options", "frame": page["frame"],
                   **({"menu": page["menu"]} if page.get("menu") else {}),
                   "fields": dates, "select": {"kind": "select", "label": label}}
        result = await self.request("sample", request, {"kind": "json"}, dataset=spec.id, period=period,
                                    scope="discover_options", lane=spec.source.get("lane"))
        import json
        found = json.loads(result.response.body).get("data") or []
        known = {item.get(label) for item in spec.scopes}
        usable = sorted({o["label"] for o in found if o.get("value_type") == "string" and o.get("value") == o.get("label")
                         and o.get("label")})
        new = [name for name in usable if name not in known]
        if new:
            discovered = self.state.read("discovered_options", {}) or {}
            discovered[spec.id] = (discovered.get(spec.id) or []) + [{label: name} for name in new]
            self.state.write("discovered_options", discovered)
            self.province.extra_scopes = discovered
            from .spec import scope_key
            for name in new:
                self.ledger.seed(spec.id, [period], scope_key({label: name}))
        self.state.event("options_discovered", dataset=spec.id, period=period, listed=len(found), new=len(new),
                         not_text_valued=len(found) - len(usable))
        return {"dataset": spec.id, "period": period, "listed": len(found), "new": new}

    async def _prime_template(self, spec: DatasetSpec) -> float:
        """``request.prime``: one page query of an already collected record shows the page's own request.

        A whole-day request (every option blank) has no UI of its own; as the
        Anhui legacy worker (_bootstrap_candidate / needs_template first), a
        covered record is fetched once through the page. Its answer is kept as
        a sample, never as a task's result; at most once per 10 minutes.
        """
        self._primed[spec.id] = self.clock()
        prime = spec.request["prime"]
        try:
            result = await self.request("sample", {**prime["request"], "bootstrapping": True}, {"kind": "json"},
                                        dataset=spec.id, period=prime["period"], scope=PRIME_SCOPE,
                                        lane=spec.source.get("lane"))
        except Fault as fault:
            if fault.code == "spacing_not_elapsed":
                self._primed.pop(spec.id, None)      # nothing was tried
            elif fault.category == "local":
                # The page could not be driven (Anhui 13:14: select_option_missing): only this
                # report cools down; the province goes on.
                self._hold_unsent(spec.id, "prime_" + fault.code)
                return 1
            raise
        record = self.samples.store(spec.id, prime["period"], result.response.body, ".json", scope=PRIME_SCOPE,
                                    acquisition="template_prime")
        self.state.event("template_primed", dataset=spec.id, period=prime["period"], sample_sha256=record["sha256"])
        return 0.2

    async def _recycle_due(self) -> bool:
        """``browser.page_recycle``: a page in use this long, or with this JS heap, is replaced between two actions.

        Anhui 2026-11-01 00:10: one 综合查询 page grew to 2.4 GB and 650% CPU
        and crashed by itself about every 2.5 hours; its legacy worker now
        replaces it after an hour or 768 MB of JS heap.
        """
        rule = self.province.browser_config().get("page_recycle")
        health = getattr(self.transport, "page_health", None)
        now = self.clock()
        if not rule or health is None or now - self._recycle_checked < 60:
            return False
        self._recycle_checked = now
        found = await health()
        due = (found["age_seconds"] >= rule.get("max_age_seconds", 3600)
               or (found["js_heap_mb"] is not None and found["js_heap_mb"] >= rule.get("max_js_heap_mb", 768)))
        if due:
            self._recycle_reason = found
        return due

    def _page_operation_failed(self, fault: Fault, dataset: str | None) -> None:
        """PAGE_FAILURES_TO_STOP page operations failing in a row (any reports) stop the province.

        Anhui 2026-11-01 12:07: one hidden tab failed 15 operations in a row,
        each after its 45 s timeout, and spent the whole acceptance budget. The
        login is still renewed meanwhile (keepalive replays need no page work).
        """
        failures = (self.state.read("page_operation_failures", []) or [])[-(PAGE_FAILURES_TO_STOP - 1):] + [
            {"dataset": dataset, "reason": fault.code, "at": time.strftime("%Y-%m-%dT%H:%M:%S")}]
        self.state.write("page_operation_failures", failures)
        if len(failures) >= PAGE_FAILURES_TO_STOP:
            self.state.event("page_operations_failing", reasons=",".join(sorted({f["reason"] for f in failures})),
                             datasets=",".join(sorted({f["dataset"] or "" for f in failures})))
            self.block(Fault("page_operations_failing", "local"), Decision("province", True))

    def _driver_lost(self, error: str) -> None:
        """Record it; the DRIVER_LOSSES_TO_STOP-th within 2 h is blocked for a person (no restart loop).

        As the Anhui worker resume budget (2026-11-01): at most 3 automatic
        restarts in 2 hours.
        """
        now = self.clock()
        losses = [t for t in self.state.read("driver_losses", []) or [] if now - t < 7200] + [now]
        self.state.write("driver_losses", losses)
        self.state.event("driver_lost", error=error[:120], in_two_hours=len(losses))
        if len(losses) >= DRIVER_LOSSES_TO_STOP:
            self.block(Fault("driver_lost_repeatedly", "local"), Decision("province", False))

    async def _recover_page(self, planned: bool = False) -> float:
        """A crashed page is replaced before anything else is sent; repeated crashes stop the province.

        A planned recycle is counted apart: an hourly recycle plus a rare crash
        must not look like a crash loop.
        """
        now = self.clock()
        if not planned:
            recent = [t for t in self.state.read("page_recoveries", []) or [] if now - t < 3600]
            if len(recent) >= PAGE_RECOVERIES_PER_HOUR:
                self.block(Fault("page_crashed_repeatedly", "local"), Decision("province", False))
                return 5
            self.state.write("page_recoveries", recent + [now])
        rule = (self.province.browser_config().get("page_recycle") or {}).get("challenge") if planned else None
        try:
            if rule:
                info = await self.transport.recover_page(rule=rule, admit=lambda: self._admit_challenge(rule))
            else:
                info = await self.transport.recover_page()
        except DriverLost:
            raise
        except Fault as fault:
            # The new page's document was refused (Anhui 412): as any security answer, a person decides.
            self.state.event("page_recovery_failed", error=fault.code, http_status=fault.http_status, planned=planned)
            self.block(fault, decide(fault, "collect"))
            return 5
        except Exception as exc:
            from .browser.attach import AttachError
            # An AttachError carries a fixed code (portal_page_missing …); other messages may hold addresses.
            self.state.event("page_recovery_failed", error=type(exc).__name__, planned=planned,
                             **({"code": str(exc)[:80]} if isinstance(exc, AttachError) else {}))
            # A planned recycle leaves the old page working: the login is still renewed meanwhile
            # (Anhui 2026-11-01 12:39). A crashed page cannot renew anything.
            self.block(Fault("page_recovery_failed", "local"), Decision("province", planned))
            return 5
        # The old page's request templates died with it: a whole-day lane may prime again at once
        # (Anhui 2026-11-01: a recycle about every 20 minutes while the whole-day lane runs).
        self._primed.clear()
        info = dict(info or {})
        settled = info.pop("challenge", None)
        if planned:
            self.state.event("page_recycled", **info, **getattr(self, "_recycle_reason", {}))
        else:
            self.state.event("page_recovered", **info)
        if settled is not None:
            # Nothing else is sent before the check (also after a restart: it is kept in state).
            self.state.write("recycle_challenge_pending", {**settled, "verify_points": rule.get("verify_points", 96)})
        return 1

    def _admit_challenge(self, rule: dict) -> bool:
        """At most one automatic judgement per ``window_seconds``; spent when the attempt starts."""
        now = self.clock()
        recent = [t for t in self.state.read("recycle_challenges", []) or [] if now - t < rule.get("window_seconds", 7200)]
        if recent:
            self.state.event("recycle_challenge_not_admitted", last_at=recent[-1])
            return False
        self.state.write("recycle_challenges", recent + [now])
        return True

    async def _verify_after_challenge(self, pending: dict) -> float:
        """Anhui's keepalive check after a settled challenge: the keepalive report's archived day is
        queried once more, and its numbers must equal the archived original (96 points), or nothing goes on."""
        import json
        from .browser.challenge import numeric_series
        wait = self.spacing_wait("keepalive")
        if wait > 0:
            return wait
        self.state.remove("recycle_challenge_pending")
        settled = {k: v for k, v in pending.items() if k != "verify_points"}
        spec = self._keepalive_spec()
        period = (self._session().get("keepalive_period") or self.archive.latest_period(spec.id)) if spec else None
        records = [r for r in self.archive.records(spec.id) if r["period"] == period] if period else []
        request = self._sendable(spec.build(period)) if records else None
        if request is None:
            self.state.event("recycle_challenge_unverified", **settled, period=period)
            self.block(Fault("recycle_challenge_unverifiable", "local"), Decision("province", False))
            return 5
        try:
            result = await self.request("keepalive", request, spec.response, dataset=spec.id, period=period)
        except Fault as fault:
            self.state.event("recycle_challenge_unverified", **settled, period=period, reason=fault.code)
            self.block(fault, decide(fault, "collect"))
            return 5
        archived = records[-1]
        try:
            expected = numeric_series(json.loads((self.archive.root / archived["path"]).read_bytes()))
            found = numeric_series(json.loads(result.response.body))
        except (OSError, ValueError):
            expected, found = None, []
        points = pending.get("verify_points", 96)
        if not self.archive.verify(archived) or found != expected or len(found) < points:
            self.state.event("recycle_challenge_unverified", **settled, period=period, points=len(found))
            self.block(Fault("recycle_challenge_check_mismatch", "local"), Decision("province", False))
            return 5
        self.state.event("recycle_challenge_settled", **settled, dataset=spec.id, period=period, points=len(found),
                         archived_sha256=archived["sha256"])
        return 1

    def _quiet_left(self) -> float:
        if not self.policy.quiet_windows:
            return 0.0
        from datetime import datetime
        from .pacing import quiet_seconds_left
        from .periods import CHINA
        now = datetime.fromtimestamp(self.clock(), CHINA)
        return quiet_seconds_left(self.policy.quiet_windows, now.hour * 60 + now.minute + now.second / 60)

    def _disk_low(self) -> bool:
        if self.policy.min_free_disk_mb is None:
            return False
        import shutil
        return shutil.disk_usage(self.instance).free < self.policy.min_free_disk_mb * 1024 * 1024

    def _isolate_item(self, fault: Fault, task) -> bool:
        """Settle one failed item and continue, where the province policy says so.

        Shanxi production isolates non-auth single-item failures (user
        decision 2026-10-30) instead of stopping the province; 40302 only as
        HTTP 200 + business 40302. The item is never retried automatically: it
        settles ``failed`` (reason ``isolated:<code>`` / ``denied:40302``) and
        is listed in ``isolated_items`` for review; any sent identity stays
        consumed in the ledger.
        """
        if fault.category not in self.policy.isolate_items or fault.code in GLOBAL_LOCAL_FAULTS:
            return False
        if fault.category == "network":
            # One network fault is that item's (Anhui 2026-11-01 14:00, the legacy isolates the date);
            # NETWORK_FAULTS_TO_STOP in a row, any reports, are the connection's: the province stops.
            self._network_streak += 1
            if self._network_streak >= NETWORK_FAULTS_TO_STOP:
                return False
        if fault.category == "permission" and not (fault.http_status == 200 and fault.business_status == "40302"):
            return False
        if fault.category == "permission" and self._query_lapse(task):
            return True
        if fault.category == "permission" and self._unpublished(task["dataset"], task["period"]):
            return self._defer_unpublished(task)
        row = self.ledger.task(task["dataset"], task["period"], task["scope"])
        if row is None or row["status"] != "pending":
            return False
        cursor = self.state.read("page_cursor")
        if cursor and (cursor["dataset"], cursor["period"], cursor["scope"]) == (task["dataset"], task["period"], task["scope"]):
            self.state.remove("page_cursor")
        reason = "denied:40302" if fault.category == "permission" else f"isolated:{fault.code}"
        self.ledger.settle_task(task["dataset"], task["period"], "failed", scope=task["scope"], reason=reason)
        entry = {"dataset": task["dataset"], "period": task["period"], "scope": task["scope"], "reason": reason,
                 "category": fault.category, "http_status": fault.http_status,
                 "business_status": fault.business_status, "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        self.state.write("isolated_items", (self.state.read("isolated_items", []) or [])[-999:] + [entry])
        self.state.event("item_isolated", **{k: v for k, v in entry.items() if v is not None})
        source = (self.province.datasets().get(task["dataset"]) or None)
        source = source.source if source else {}
        denied = fault.category == "permission"
        limit = int(source.get("denial_streak", self.policy.denial_streak or DENIAL_STREAK) if denied
                    else source.get("isolation_streak", self.policy.isolation_streak or ISOLATION_STREAK))
        if self.ledger.isolation_streak(task["dataset"], limit, "denied:" if denied else "isolated:") >= limit:
            # Stricter than production, which keeps going item by item.
            self.hold_dataset(task["dataset"], f"repeated_{'denial' if denied else 'isolation'}:{fault.code}", "paused")
        return True

    def _query_lapse(self, task) -> bool:
        """A query's 40302 joins its service's run; at QUERY_LAPSE_DATASETS datasets it is a lapse, not a denial.

        On a lapse this item and the run's denied ones return to pending (the
        run's deferred ones keep their next-day try), the service's queries
        are held and the recovery runs at the next send slot.
        """
        spec = self.province.datasets().get(task["dataset"])
        # Per-option reports are refused option by option for this account
        # (Shanxi 2026-10-31: every option, default and blank): no lapse sign.
        service = (app_of(spec) if spec is not None and spec.request["transport"] in QUERY_TRANSPORTS and not spec.scopes
                   else None)
        if service is None:
            return False
        runs = self.state.read("query_denials", {}) or {}
        entry = {"dataset": task["dataset"], "period": task["period"], "scope": task["scope"]}
        run = runs.get(service, []) + [entry]
        if len({item["dataset"] for item in run}) < QUERY_LAPSE_DATASETS:
            self.state.write("query_denials", {**runs, service: run})
            return False
        cursor = self.state.read("page_cursor")
        if cursor and (cursor["dataset"], cursor["period"], cursor["scope"]) == (task["dataset"], task["period"], task["scope"]):
            self.state.remove("page_cursor")
        self.ledger.settle_task(task["dataset"], task["period"], "failed", scope=task["scope"], reason="denied:40302")
        reopened = self._release_lapsed(run)
        reason = f"query_lapse_suspected:{service}"
        held = [s.id for s in self.collection_specs()
                if app_of(s) == service and s.request["transport"] in QUERY_TRANSPORTS]
        for name in held:
            self.hold_dataset(name, reason, "paused")
        self.state.write("query_lapse", {**entry, "service": service, "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                         "run": run, "reopened": len(reopened)})
        self.state.write("query_denials", {k: v for k, v in runs.items() if k != service})
        self.state.event("query_lapse_suspected", service=service, datasets=sorted({i["dataset"] for i in run}),
                         held=len(held), reopened=len(reopened))
        return True

    def _release_lapsed(self, items: list) -> list:
        """Denied items of a lapse back to pending under LAPSE_RULE; the attempts stay in the history."""
        reopened = []
        for item in items:
            row = self.ledger.task(item["dataset"], item["period"], item["scope"])
            attempt = self.ledger.latest_denial(item["dataset"], item["period"])
            if row is not None and (row["status"], row["reason"]) == ("failed", "denied:40302") and attempt is not None:
                reopened += self.ledger.release_denied([attempt], LAPSE_RULE)
        return reopened

    async def _recover_query_lapse(self, lapse: dict) -> float:
        """Enter the lapsed report from the portal menu, verify with its one item, then let the service go on.

        Without a portal re-entry, after QUERY_LAPSE_RECOVERIES_PER_HOUR, or
        if the verifying query is still refused: the province holds (the login
        is still renewed) and a person enters 信息披露.
        """
        spec = self.province.datasets()[lapse["dataset"]]
        wait = self.spacing_wait("collect", spec.source.get("lane"))
        if wait > 0:
            self._status("query_lapse_waiting", wait_seconds=round(wait, 1))
            return min(wait, 5)
        now = self.clock()
        recent = [t for t in self.state.read("query_lapse_recoveries", []) or [] if now - t < 3600]
        reenter = getattr(self.transport, "portal_reentry", None)
        if reenter is None:
            return self._lapse_needs_a_person("query_lapse_needs_portal_entry", spec)
        if len(recent) >= QUERY_LAPSE_RECOVERIES_PER_HOUR:
            return self._lapse_refused(lapse, "query_lapse_recoveries_exhausted")
        self.state.write("query_lapse_recoveries", recent + [now])
        try:
            entered = await reenter(spec.request["frame"])
        except Fault as fault:
            return self._lapse_needs_a_person("query_lapse_reentry_failed", spec, cause=fault.code)
        self.state.event("query_lapse_reentered", dataset=spec.id, **entered)
        task = self.ledger.task(spec.id, lapse["period"], lapse["scope"])
        if task is None or task["status"] != "pending":
            return self._lapse_needs_a_person("query_lapse_verify_item_missing", spec)
        try:
            await self.collect(spec, task["period"], task["scope"])
        except Fault as fault:
            if fault.category == "permission" and (fault.http_status, fault.business_status) == (200, "40302"):
                # Refused even right after a fresh entry, as a person would see it:
                # a real denial of these reports, not a lapse (Shanxi r25 22:47).
                self.ledger.settle_task(spec.id, task["period"], "failed", scope=task["scope"], reason="denied:40302")
                return self._lapse_refused(lapse, "query_denied_after_portal_entry")
            return self._lapse_needs_a_person("query_lapse_verify_failed", spec, cause=fault.code)
        self._query_answered(spec)
        reason = f"query_lapse_suspected:{lapse['service']}"
        holds = self.dataset_holds()
        released = [name for name, hold in holds.items() if hold["reason"] == reason]
        self.state.write("dataset_holds", {k: v for k, v in holds.items() if k not in released})
        self.state.remove("query_lapse")
        self.state.event("query_lapse_recovered", dataset=spec.id, period=task["period"], released=len(released))
        self._status("query_lapse_recovered", last_dataset=spec.id)
        return 0.2

    def _lapse_refused(self, lapse: dict, reason: str) -> float:
        mine = settle_query_lapse(self.state, reason)
        self._status("query_lapse_not_recovered", held=mine)
        return 0.2

    def _lapse_needs_a_person(self, code: str, spec: DatasetSpec, cause: str | None = None) -> float:
        # The lapse stays recorded: after the person's entry and ``ack`` the recovery verifies again.
        # A block reason is a fixed code; the underlying fault goes to the event.
        self.state.write("query_lapse_recoveries", [])
        self.state.event("query_lapse_needs_a_person", reason=code, dataset=spec.id, **({"cause": cause} if cause else {}))
        self.block(Fault(code, "auth"), Decision("province", True), spec.id)
        return 5

    def _query_answered(self, spec: DatasetSpec) -> None:
        runs = self.state.read("query_denials", {}) or {}
        if spec.request["transport"] in QUERY_TRANSPORTS and app_of(spec) in runs:
            self.state.write("query_denials", {k: v for k, v in runs.items() if k != app_of(spec)})

    def _unpublished(self, dataset: str, period: str) -> bool:
        """A period that ended inside the policy's window (or has not ended): its 40302 means "not yet published"."""
        window = self.policy.unpublished_window_days
        spec = self.province.datasets().get(dataset)
        if window is None or spec is None or spec.period["kind"] not in ("day", "month"):
            return False
        try:
            age = (today() - last_day(spec.period["kind"], period)).days
        except Exception:
            return False
        return age <= window

    def _defer_incomplete(self, spec: DatasetSpec, task) -> bool:
        """A month (or year) still running is not asked for: its export would be partial and never redone.

        The task waits as deferred and gets its daily try once the period has ended.
        """
        kind = spec.period["kind"]
        if kind not in ("month", "year") or last_day(kind, task["period"]) < today():
            return False
        self.ledger.settle_task(spec.id, task["period"], "deferred", scope=task["scope"], reason="period_not_complete")
        self.state.event("incomplete_period_deferred", dataset=spec.id, period=task["period"])
        return True

    def _defer_unpublished(self, task) -> bool:
        """Not a denial: the attempt stops consuming the item, which is tried again tomorrow (user, 2026-10-31)."""
        attempt = self.ledger.latest_denial(task["dataset"], task["period"])
        if attempt is None:
            return False
        cursor = self.state.read("page_cursor")
        if cursor and (cursor["dataset"], cursor["period"], cursor["scope"]) == (task["dataset"], task["period"], task["scope"]):
            self.state.remove("page_cursor")
        self.ledger.settle_task(task["dataset"], task["period"], "failed", scope=task["scope"], reason="denied:40302")
        self.ledger.release_denied([attempt], UNPUBLISHED_RULE, defer=True)
        self.state.event("unpublished_deferred", dataset=task["dataset"], period=task["period"], scope=task["scope"],
                         attempt=attempt)
        return True

    # -- queued adaptation commands ---------------------------------------
    def enqueue(self, command: dict) -> str:
        return enqueue(self.state, command)

    async def run_command(self) -> bool:
        commands = self.state.read("commands", [])
        if not commands:
            return False
        # A probe uses its dataset's lane: on a busy 5 s lane the default
        # interval never elapses and the command would wait forever.
        spec = self.province.datasets().get(commands[0].get("dataset"))
        lane = (spec.source.get("lane") if spec is not None
                and commands[0].get("action") in ("probe_blank", "collect_one", "probe_api_blank", "probe_export_blank",
                                                  "discover_options")
                else None)
        if self.spacing_wait("sample", lane) > 0:
            return False
        command, rest = commands[0], commands[1:]
        self.state.write("commands", rest)
        results = self.state.read("command_results", {})
        try:
            spec = self.province.datasets()[command["dataset"]]
            if command.get("action") == "sample":
                from .adapt import sample
                results[command["id"]] = {"ok": True, **await sample(self, spec, command["period"], command.get("scope"))}
            elif command.get("action") == "probe_blank":
                results[command["id"]] = {"ok": True, **await self._probe_blank(spec, command["period"],
                                                                                timeout=command.get("timeout"),
                                                                                option=command.get("option", ""))}
            elif command.get("action") == "collect_one":
                results[command["id"]] = {"ok": True, **await self._collect_one(spec, command.get("period"),
                                                                                command.get("option"))}
            elif command.get("action") == "probe_api_blank":
                results[command["id"]] = {"ok": True, **await self._probe_api_blank(spec, command["period"])}
            elif command.get("action") == "probe_export_blank":
                results[command["id"]] = {"ok": True, **await self._probe_export_blank(spec, command)}
            elif command.get("action") == "discover_options":
                results[command["id"]] = {"ok": True, **await self._discover_options(spec, command["period"])}
            else:
                raise ValueError("command_unknown")
        except Fault as fault:
            results[command["id"]] = {"ok": False, "reason": fault.code, "category": fault.category}
            if command.get("action") == "discover_options" and fault.category in ("local", "unknown"):
                # Reading a dropdown sends no business request (Anhui 2026-11-01 11:31:
                # select_value_changed_by_reading stopped the province): only that
                # report's discovery waits for review; its known options roll on.
                # A login, permission, rate-limit or network answer is the site's and still stops it.
                self._hold_discovery(command["dataset"], fault.code, fault.detail)
            else:
                decision = decide(fault, "sample")
                if decision.scope == "province":
                    self.block(fault, decision, command.get("dataset"))
        except Exception as exc:
            results[command["id"]] = {"ok": False, "reason": type(exc).__name__}
        self.state.write("command_results", results)
        return True

    async def _probe_blank(self, spec: DatasetSpec, period: str, timeout: float | None = None, option: str = "") -> dict:
        """One request with every option of a per-option dataset left blank, 1000 per page (user, 2026-10-31).

        Answers whether one query returns all options of a day. Its identity
        (the blank option) was never sent before; the page is kept as a sample.
        """
        import copy
        import json
        import math
        from . import paging
        if not spec.scopes:
            raise Fault("probe_needs_a_per_option_dataset", "local", unsent=True)
        # Blank by default; or one given option value (e.g. the page's own default).
        blank = {name: option for name in spec.scopes[0]}
        probe = spec.with_page_size(1000) if "pagination" in spec.request else copy.deepcopy(spec)
        probe.scopes = [blank]
        key = probe.scope_items()[0][0]
        request = probe.build(period, blank)
        pagination = probe.request.get("pagination") or {}
        page = None
        if pagination:
            page = paging.first_page(pagination)
            request = paging.apply(request, pagination, page)
        if timeout:
            request["timeout"] = max(float(timeout), self.policy.request_timeout_seconds)
        result = await self.request("sample", request, probe.response, dataset=spec.id, period=period,
                                    scope=f"blank.{key}", identity=self.task_identity(probe, period, key, page=page),
                                    lane=spec.source.get("lane"))
        body = json.loads(result.response.body)
        records = [r for r in (paging.get_path(body, pagination.get("records", "data.list")) or []) if isinstance(r, dict)]
        total = paging.get_path(body, pagination["total"]) if "total" in pagination else None
        record = self.samples.store(spec.id, period, result.response.body, ".json", scope=f"blank_{key}",
                                    acquisition="blank_option_probe")
        return {"dataset": spec.id, "period": period, "records_on_page": len(records), "total": total,
                "pages_at_1000": math.ceil(total / 1000) if isinstance(total, int) else None,
                "distinct_options_on_page": {name: len({r.get(name) for r in records if name in r}) for name in blank},
                "record_fields": sorted(records[0])[:40] if records else [], "sample_sha256": record["sha256"]}

    async def _collect_one(self, spec: DatasetSpec, period: str | None = None, option: str | None = None) -> dict:
        """One item collected as usual (its page call also shows the request PMOS may replay).

        Without ``period``/``option``: the dataset's next pending item. With
        them: that day and the scope whose single value is ``option``.
        """
        if period is None:
            task = self.ledger.next_task([spec.id], newest=spec.newest_due())
        else:
            keys = [key for key, values in spec.scope_items() if option is not None and list(values.values()) == [option]]
            if len(keys) != 1:
                raise Fault("collect_one_option_unknown", "local", unsent=True)
            self.ledger.seed(spec.id, [period], keys[0])
            task = self.ledger.task(spec.id, period, keys[0])
            if task is not None and task["status"] != "pending":
                raise Fault("collect_one_item_not_pending", "local", unsent=True)
        if task is None:
            raise Fault("no_pending_item", "local", unsent=True)
        try:
            await self.collect(spec, task["period"], task["scope"])
        except Fault as fault:
            # Settled exactly as in normal collection (e.g. a recent 40302 is
            # deferred as not yet published, an older one denied).
            if not (fault.code.startswith("preflight_deferred:") or fault.unsent):
                self._isolate_item(fault, task)
            raise
        row = self.ledger.task(spec.id, task["period"], task["scope"])
        return {"dataset": spec.id, "period": task["period"], "scope": task["scope"],
                "status": row["status"] if row else None, "reason": row["reason"] if row else None}

    async def _probe_api_blank(self, spec: DatasetSpec, period: str) -> dict:
        """User 2026-10-31: the page's own query, replayed once with every option blank, 1000 per page.

        The page form refuses a blank option, so the site API itself answers
        whether one request returns all options of a day. The body is the
        component's ``queryConfig`` as the page sends it; the headers are those
        of the page's own latest request on this path (collect_one first).
        """
        import copy
        import json
        import math
        from . import paging
        templates = getattr(self.transport, "templates", None)
        path = spec.request["path"]
        if not spec.scopes:
            raise Fault("probe_needs_a_per_option_dataset", "local", unsent=True)
        if templates is None or path not in templates.items:
            raise Fault("api_probe_needs_a_page_request_first", "local", unsent=True)
        blank = {name: "" for name in spec.scopes[0]}
        probe = spec.with_page_size(1000) if "pagination" in spec.request else copy.deepcopy(spec)
        probe.scopes = [blank]
        key = probe.scope_items()[0][0]
        built = probe.build(period, blank)
        body = copy.deepcopy((built.get("set") or {}).get("queryConfig"))
        if not isinstance(body, dict) or not isinstance(body.get("data"), dict) or "pageInfo" not in body:
            raise Fault("api_probe_body_unknown", "local", unsent=True)
        body["pageInfo"].update(pageNum=1, pageSize=1000)
        request = {"transport": "fetch", "method": spec.request.get("method", "POST"), "path": path,
                   "template": path, "body": body}
        result = await self.request("sample", request, probe.response, dataset=spec.id, period=period,
                                    scope=f"apiblank.{key}", identity=self.task_identity(probe, period, key, page=1),
                                    lane=spec.source.get("lane"))
        value = json.loads(result.response.body)
        pagination = probe.request.get("pagination") or {}
        records = [r for r in (paging.get_path(value, pagination.get("records", "data.list")) or []) if isinstance(r, dict)]
        total = paging.get_path(value, pagination["total"]) if "total" in pagination else None
        record = self.samples.store(spec.id, period, result.response.body, ".json", scope=f"apiblank_{key}",
                                    acquisition="api_blank_option_probe")
        options = {name: sorted({str(r.get(name)) for r in records if name in r})[:5] for name in blank}
        return {"dataset": spec.id, "period": period, "status": value.get("status"), "records_on_page": len(records),
                "total": total, "pages_at_1000": math.ceil(total / 1000) if isinstance(total, int) else None,
                "distinct_options_on_page": {name: len({r.get(name) for r in records if name in r}) for name in blank},
                "option_examples": options, "record_fields": sorted(records[0])[:30] if records else [],
                "sample_sha256": record["sha256"]}

    async def _probe_export_blank(self, spec: DatasetSpec, command: dict) -> dict:
        """User 2026-10-31: the page's export endpoint once, every option blank or ``option`` (flat body, as legacy exported).

        With the page's own latest query on this dataset's page observed, its
        headers are reused (same page, same session). Otherwise the page's own
        ``exportData`` sends it, as legacy's browser export did: one request,
        nothing replayed. The file is kept as a sample.
        """
        import re
        from .contracts import TaskIdentity
        templates = getattr(self.transport, "templates", None)
        query_path = spec.request["path"]
        export_path = str(command.get("export_path") or "")
        if not spec.scopes or not re.fullmatch(r"/px-[a-z-]+/[A-Za-z0-9_/]+", export_path):
            raise Fault("export_probe_needs_a_per_option_dataset_and_path", "local", unsent=True)
        period = command["period"]
        option = str(command.get("option") or "")
        values = {name: option for name in spec.scopes[0]}
        body = {str(command.get("date_field") or "tdate"): period, **values}
        if templates is not None and query_path in templates.items:
            request = {"transport": "fetch", "method": "POST", "path": export_path, "template": query_path, "body": body}
        elif spec.request["transport"] == "component":
            page = spec.request
            current = page.get("set") or {}
            form = {key: option for key in ("selectValue",) if key in current}
            form.update({key: period for key in ("date",) if key in current})
            request = {"transport": "component", "method": "POST", "path": export_path, "call": "exportData",
                       "capture": "blob", "component": page["component"],
                       "match": {**(page.get("match") or {}), "exportUrl": export_path},
                       "set": {**form, "queryConfig.data": body},
                       **{key: page[key] for key in ("frame", "open_url", "menu", "prepare") if key in page}}
        else:
            raise Fault("api_probe_needs_a_page_request_first", "local", unsent=True)
        label = "exportoption" if option else "exportblank"
        identity = TaskIdentity(self.province.id, self.account_alias, spec.id,
                                {"kind": spec.period["kind"], "label": period}, values, operation="export")
        result = await self.request("sample", request, {"kind": "xlsx"}, dataset=spec.id, period=period,
                                    scope=label, identity=identity, lane=spec.source.get("lane"))
        record = self.samples.store(spec.id, period, result.response.body, result.outcome.extension,
                                    scope=label, acquisition=f"export_{'default' if option else 'blank'}_option_probe")
        return {"dataset": spec.id, "period": period, "via": request["transport"], "body": body,
                "http_status": result.response.status, "content_type": result.response.content_type,
                "bytes": len(result.response.body), "sample_sha256": record["sha256"], "sample_path": record.get("path")}

    async def _hold(self, block: dict) -> float:
        hold = self.state.read("hold", {})
        if hold.get("block_at") != block["at"]:
            hold = {"block_at": block["at"], "keepalive": bool(block.get("keepalive_allowed"))}
            self.state.write("hold", hold)
        # Unfinished sends outrank a previously allowed local hold.
        if self.ledger.unsettled() and hold["keepalive"]:
            hold.update(keepalive=False, stopped_by="unsettled_attempt")
            self.state.write("hold", hold)
        if hold["keepalive"]:
            try:
                await self.keep_session()
            except Fault as fault:
                if fault.code != "spacing_not_elapsed":
                    hold.update(keepalive=False, stopped_by=fault.code)
                    self.state.write("hold", hold)
                    self.state.event("hold_keepalive_stopped", reason=fault.code)
        self._status("blocked", block=block["reason"], hold_keepalive=hold["keepalive"])
        return 5

    def _status(self, phase: str, **fields) -> None:
        if phase != "blocked":
            # status merges into the last one: a cleared block must not linger there.
            fields = {"block": None, "hold_keepalive": None, **fields}
        self.state.status(phase=phase, province=self.province.id, **fields)

    async def run(self, stopped: asyncio.Event, sleep=asyncio.sleep, stop_when=None, between=None) -> None:
        """``between``: callables (sync or async) run after a tick that leaves time to wait.

        Memory upkeep goes there: no request of this worker is open then. A
        fast pace never leaves a second, so they also run every
        ``UPKEEP_EVERY_SECONDS``.
        """
        self.acquire()
        last_upkeep = float("-inf")
        try:
            while not stopped.is_set():
                delay = await self.tick()
                if stop_when is not None and stop_when():
                    break
                if delay >= 1 or self.clock() - last_upkeep >= UPKEEP_EVERY_SECONDS:
                    last_upkeep = self.clock()
                    for upkeep in between or ():
                        result = upkeep()
                        if inspect.isawaitable(result):
                            await result
                try:
                    await asyncio.wait_for(stopped.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
        finally:
            self.release()


def paging_first(spec: DatasetSpec) -> int:
    return spec.request["pagination"].get("first_page", 1)


def echo_mismatch(spec: DatasetSpec, values: dict, body: bytes) -> None:
    """``response.echo`` (record field -> scope label): every returned record names this task's option.

    Anhui's replay rule: a sibling that echoed the option per record binds the
    new answer to echo it too (multidimensional_native_replay).
    """
    echo = spec.response.get("echo")
    if not echo:
        return
    import json
    try:
        records = json.loads(body).get("data")
    except (ValueError, AttributeError):
        raise Fault("native_option_echo_unreadable", "site_data") from None
    if not isinstance(records, list):
        return  # an empty object or no list: nothing to bind (kept as returned)
    for record in records:
        if not isinstance(record, dict) or any(record.get(field) != values.get(label) for field, label in echo.items()):
            raise Fault("native_option_echo_mismatch", "site_data")


def whole_day_mismatch(spec: DatasetSpec, values: dict, body: bytes) -> None:
    """``response.whole_day``: one blank-option answer holds every option of the day, each complete.

    Anhui node_price_whole_day: each option has exactly ``times`` distinct
    times; ``single`` repeats none, ``uniform`` repeats every time equally
    with identical records (merged then); a typed answer carries the asked type.
    """
    rule = spec.response.get("whole_day")
    if not rule:
        return
    import json
    try:
        records = json.loads(body).get("data")
    except (ValueError, AttributeError):
        raise Fault("whole_day_unreadable", "site_data") from None
    if records in ([], {}, None):
        return                          # the site's empty day: kept as returned, never zero-filled
    if not isinstance(records, list):
        raise Fault("whole_day_shape_changed", "site_data")
    groups: dict = {}
    for record in records:
        if not isinstance(record, dict) or record.get(rule["option_field"]) in (None, ""):
            raise Fault("whole_day_option_missing", "site_data")
        if rule.get("type_field") and record.get(rule["type_field"]) != values.get(rule["type_label"]):
            raise Fault("whole_day_type_mismatch", "site_data")
        groups.setdefault(record[rule["option_field"]], {}).setdefault(record.get(rule["time_field"]), []).append(record)
    for times in groups.values():
        if len(times) != rule["times"]:
            raise Fault("whole_day_times_changed", "site_data")
        counts = {len(rows) for rows in times.values()}
        if rule.get("repeat", "single") == "single" and counts != {1}:
            raise Fault("whole_day_time_repeated", "site_data")
        if len(counts) != 1 or any(any(row != rows[0] for row in rows) for rows in times.values()):
            raise Fault("whole_day_uneven_repeats", "site_data")


def settle_query_lapse(state: StateStore, reason: str, **event) -> list[str]:
    """A lapse that is a real denial: only its own reports wait for review, the service's other queries go on.

    Their items stay pending (never re-sent automatically) and the province is
    not blocked. Used by the engine after a refused verification and by the
    ``skip-lapse`` command (a person's decision).
    """
    lapse = state.read("query_lapse")
    if lapse is None:
        return []
    mine = sorted({item["dataset"] for item in lapse.get("run", [])} | {lapse["dataset"]})
    held = f"query_lapse_suspected:{lapse['service']}"
    holds = state.read("dataset_holds", {}) or {}
    released = [name for name, hold in holds.items() if hold["reason"] == held and name not in mine]
    holds = {k: v for k, v in holds.items() if v["reason"] != held}
    for name in mine:
        holds.setdefault(name, {"reason": reason, "status": "paused", "at": time.strftime("%Y-%m-%dT%H:%M:%S")})
    state.write("dataset_holds", holds)
    state.remove("query_lapse")
    state.event("query_lapse_not_recovered", reason=reason, held=mine, released=len(released), **event)
    return mine


def app_of(spec: DatasetSpec) -> str | None:
    """The site service a spec's requests go to: the first segment of its API path.

    Shanxi's disclosure pages, their exports and the renewal export all call
    ``/px-phbsx-retail-shop/...``; that service answered 40302 after idling
    while the announcement service kept the login alive.
    """
    segment = str(spec.request.get("path") or "").strip("/").split("/", 1)[0]
    return segment or None


def task_identity(province: str, account_alias: str, spec: DatasetSpec, period: str, scope: str = "", *,
                  page=None) -> TaskIdentity:
    """The business identity of one task, as the ledger and legacy tombstones key it.

    ``source.identity_contract`` binds it to the request actually sent:
    ``date_fields`` must carry the period, and ``dimension_fields`` (request
    path -> legacy name) names the only other parameters that identify the
    task, so UI resets such as ``tableData`` never become dimensions.
    """
    kind = spec.period["kind"]
    operation = "listing" if spec.documents is not None else (
        "export" if spec.response.get("kind") in {"xlsx", "xls", "excel", "pdf", "binary", "document"} else "query")
    dimensions = spec.scope_values(scope)
    rule = spec.source.get("identity_contract", {})
    if rule:
        if "body" in rule:
            # A page call has no request body of its own: the contract carries
            # the plaintext body the legacy queue keyed (Shanxi native lane).
            from .spec import render
            from . import periods as _periods
            values = {**_periods.variables("day" if kind == "snapshot" else kind, period), "scope": dimensions}
            body = render(rule["body"], values)
            data = body if rule.get("request_envelope") == "flat" else body["data"]
        else:
            data = spec.build(period, dimensions)
            for key in rule.get("request_data_path", ["body"]):
                data = data[key]
        fields = rule.get("date_fields")
        if not isinstance(fields, list) or any(data.get(field) != period for field in fields):
            raise Fault("identity_period_binding_changed", "local")
        scope_values = dimensions
        if "dimension_fields" in rule:
            names = rule["dimension_fields"]
            if not isinstance(names, dict) or any(path not in data for path in names):
                raise Fault("identity_dimension_binding_changed", "local")
            dimensions = {name: data[path] for path, name in names.items()}
        else:
            dimensions = {k: v for k, v in data.items() if k not in fields}
        # A dimension the request carries only as an internal value (Anhui 6d80:
        # the unit's display name keys the legacy identity, its unitId is sent).
        for label in rule.get("scope_dimensions", []):
            if label not in scope_values:
                raise Fault("identity_dimension_binding_changed", "local")
            dimensions[label] = scope_values[label]
    if page is None and rule.get("fixed_page") is not None:
        page = rule["fixed_page"]  # an unpaged legacy request still recorded its page
    elif page is not None:
        actual_size = spec.request.get("pagination", {}).get("page_size")
        if rule.get("page_size") is not None and actual_size is not None and rule["page_size"] != actual_size:
            raise Fault("identity_page_size_binding_changed", "local")
        size = rule.get("page_size", actual_size)
        if size is not None:
            if type(size) is not int or size < 1:
                raise Fault("identity_page_size_binding_changed", "local")
            dimensions = {**dimensions, "_page_size": size}
    return TaskIdentity(province, account_alias, spec.id, {"kind": kind, "label": period}, dimensions, page, operation)
