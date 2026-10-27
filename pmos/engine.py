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
import os
import time
from dataclasses import dataclass

from .archive import Archive
from .faults import Decision, Fault, decide
from .ledger import BUDGET_KINDS, Ledger
from .pacing import DAY, wait_seconds
from .periods import today
from .province import Province
from .spec import DatasetSpec
from .state import StateStore
from .validate import Outcome, Response, evaluate

PROBE_RESPONSE = {"kind": "json", "empty_when_no_data": False}


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
                 clock=time.time, decode_response=None):
        self.province = province
        self.policy = province.policy
        self.transport = transport
        self.check_session = check_session
        self.clock = clock
        self.decode_response = decode_response
        self.instance = instance or province.instance()
        self.state = StateStore(self.instance / "state")
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
    def spacing_wait(self, kind: str) -> float:
        charged = kind in BUDGET_KINDS
        starts = self.ledger.budget_starts(self.clock() - DAY) if charged else None
        return wait_seconds(self.policy, self.clock(), self.ledger.last_response_end(), starts, charged=charged)

    async def request(self, kind: str, request: dict, response_spec: dict, *,
                      dataset: str | None = None, period: str | None = None, scope: str = "") -> Result:
        """Send one request if spacing allows; the attempt is durable before sending."""
        async with self.serial:
            block = self.state.blocked()
            if block and (kind not in ("keepalive", "probe") or not block.get("keepalive_allowed")):
                raise Fault("province_blocked", "local")
            if self.ledger.unsettled():
                raise Fault("unsettled_attempt", "local")
            if kind in BUDGET_KINDS and dataset is not None and period is not None:
                if self.ledger.consumed(dataset, period, scope):
                    raise Fault("consumed_identity_requires_review", "unknown")
            if self.spacing_wait(kind) > 0:
                raise Fault("spacing_not_elapsed", "local")
            if request.get("transport") == "fetch" and not self._ready(request):
                # Proven local, before durable dispatch. Never create a fake
                # sent attempt for the historical missing-template failure.
                raise Fault("request_template_missing", "local")
            attempt = self.ledger.begin(kind, dataset, period, scope)
            try:
                response = await self.transport.send({"timeout": self.policy.request_timeout_seconds, **request})
            except Fault as fault:
                self.ledger.finish(attempt, "unknown" if fault.category == "network" else "failed", reason=fault.code)
                raise
            except Exception:
                self.ledger.finish(attempt, "unknown", reason="transport_exception")
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
            return Result(outcome, response, parsed=parsed)

    # -- collection -------------------------------------------------------
    def seed(self) -> None:
        day = today()
        if self._seeded_on == day:
            return
        for spec in self.province.enabled():
            due = spec.periods_to_collect(on=day)
            for key, _ in spec.scope_items():
                self.ledger.seed(spec.id, due, key)
        self._seeded_on = day

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
            task = self.ledger.next_task([spec.id])
            if task is None:
                continue
            if spec.request["transport"] == "fetch" and not task["scope"].startswith("f"):
                built = spec.build(task["period"], spec.scope_values(task["scope"]))
                if self._sendable(built) is None:
                    missing.add(built.get("template", built["path"]))
                    continue
            return task, missing
        return None, missing

    def dataset_holds(self) -> dict:
        return self.state.read("dataset_holds", {})

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
        request = self._sendable(spec.build(period, values))
        if request is None:
            raise Fault("request_template_missing", "local")
        try:
            result = await self.request("collect", request, spec.response, dataset=spec.id, period=period, scope=scope)
        except Fault as fault:
            if request.get("bootstrapping") and fault.category == "local":
                # The page could not be driven: that report waits, the others go on.
                raise Fault("bootstrap_" + fault.code, "site_data") from None
            raise
        if result.outcome.result == "saved":
            result.record = self.archive.store(spec.id, period, result.response.body, result.outcome.extension,
                                               scope=scope, http_status=result.response.status,
                                               business_status=result.outcome.business_status,
                                               **({"scope_values": values} if values else {}))
            self.ledger.settle_task(spec.id, period, "saved", scope=scope, sha256=result.record["sha256"])
            self.state.write("empty_streaks", {**self.state.read("empty_streaks", {}), spec.id: 0})
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

    async def _collect_page(self, spec: DatasetSpec, period: str, scope: str) -> Result:
        """One page of a paginated task; the task settles after its last page."""
        from . import paging
        pagination = spec.request["pagination"]
        cursor = self.state.read("page_cursor")
        if not cursor or (cursor["dataset"], cursor["period"], cursor["scope"]) != (spec.id, period, scope):
            cursor = {"dataset": spec.id, "period": period, "scope": scope,
                      "page": paging.first_page(pagination), "pages": [], "records": 0, "new_items": 0}
        values = spec.scope_values(scope)
        request = paging.apply(spec.build(period, values), pagination, cursor["page"])
        if not self._ready(request):
            raise Fault("request_template_missing", "local")
        attempt_scope = f"{scope}.p{cursor['page']:04d}" if scope else f"p{cursor['page']:04d}"
        result = await self.request("collect", request, spec.response, dataset=spec.id, period=period, scope=attempt_scope)
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
        result = await self.request("collect", spec.build_download(item), response_spec,
                                    dataset=spec.id, period=period, scope=scope)
        if result.outcome.result != "saved":
            self.ledger.settle_task(spec.id, period, "empty", scope=scope, reason="verified_empty_response")
            return result
        records = store_document(self.archive, spec, item, result)
        result.record = records[0]
        self.ledger.settle_task(spec.id, period, "saved", scope=scope, sha256=records[0]["sha256"],
                                reason=f"files={len(records)}" if len(records) > 1 else None)
        return result

    # -- session keeping --------------------------------------------------
    async def keep_session(self) -> str | None:
        """Renew with a business request when idle; else a TIME probe if configured.

        Returns what was sent ("renewal"/"probe") or None. Raises Fault on failure.
        """
        idle = self.clock() - self.ledger.last_business_end()
        spec = self.province.keepalive_spec()
        waiting = []
        if spec is not None and idle >= self.policy.renew_after_idle_seconds and self.spacing_wait("keepalive") == 0:
            period = self.archive.latest_period(spec.id)
            built = spec.build(period) if period is not None else None
            request = self._sendable(built) if built is not None else None
            failed = self.state.read("renewal_bootstrap_failed")
            if built is None:
                waiting.append("renewal_needs_one_saved_period")
            elif request is None or (request.get("bootstrapping") and failed):
                waiting.append("renewal_template:" + built["path"])
            else:
                try:
                    await self.request("keepalive", request, spec.response, dataset=spec.id, period=period)
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
        probe = self.province.session.get("probe")
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
        if self.check_session is not None:
            fault = await self.check_session()
            if fault is not None:
                self.block(fault, decide(fault, "collect"))
        block = self.state.blocked()
        if block is not None:
            return await self._hold(block)
        if self.ledger.unsettled():
            self.state.block("unsettled_attempt", category="local", keepalive_allowed=False)
            return 5
        if await self.run_command():
            return 1
        control = self.state.control()
        try:
            if not control.get("collect") or control.get("paused"):
                await self.keep_session()
                self._status("idle" if not control.get("collect") else "paused")
                return 5
            self.seed()
            holds = self.dataset_holds()
            enabled = [s for s in self.province.enabled() if s.id not in holds]
            task, missing = self._next_sendable(enabled)
            if task is None:
                await self.keep_session()
                if missing:
                    # Reports whose fetch template the page has not shown yet
                    # (and that have no bootstrap) wait; nothing is blocked.
                    self._status("waiting_templates", templates=sorted(missing))
                else:
                    self._status("complete")
                return 10
            wait = self.spacing_wait("collect")
            if wait > 0:
                await self.keep_session()
                self._status("waiting", wait_seconds=round(wait, 1))
                return min(wait, 5)
            spec = next(s for s in enabled if s.id == task["dataset"])
            await self.collect(spec, task["period"], task["scope"])
            self._status("collecting", last_dataset=spec.id, last_period=task["period"], last_scope=task["scope"])
            return 0.2
        except Fault as fault:
            if fault.code == "spacing_not_elapsed":
                return 1
            decision = decide(fault, "collect", permission_scope=self.province.notices.get("permission_scope", "province"))
            dataset = task["dataset"] if "task" in locals() and task is not None else None
            if decision.scope == "dataset" and dataset:
                self.hold_dataset(dataset, fault.code, decision.dataset_status or "paused")
            else:
                self.block(fault, decision, dataset)
            return 1
        except Exception:
            # A bug of ours after any request was settled in the ledger.
            self.block(Fault("internal_error", "local"), Decision("province", False))
            raise

    # -- queued adaptation commands ---------------------------------------
    def enqueue(self, command: dict) -> str:
        return enqueue(self.state, command)

    async def run_command(self) -> bool:
        commands = self.state.read("commands", [])
        if not commands or self.spacing_wait("sample") > 0:
            return False
        command, rest = commands[0], commands[1:]
        self.state.write("commands", rest)
        results = self.state.read("command_results", {})
        try:
            if command.get("action") != "sample":
                raise ValueError("command_unknown")
            from .adapt import sample
            spec = self.province.datasets()[command["dataset"]]
            results[command["id"]] = {"ok": True, **await sample(self, spec, command["period"], command.get("scope"))}
        except Fault as fault:
            results[command["id"]] = {"ok": False, "reason": fault.code, "category": fault.category}
            decision = decide(fault, "sample")
            if decision.scope == "province":
                self.block(fault, decision, command.get("dataset"))
        except Exception as exc:
            results[command["id"]] = {"ok": False, "reason": type(exc).__name__}
        self.state.write("command_results", results)
        return True

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
        self.state.status(phase=phase, province=self.province.id, **fields)

    async def run(self, stopped: asyncio.Event, sleep=asyncio.sleep) -> None:
        self.acquire()
        try:
            while not stopped.is_set():
                delay = await self.tick()
                try:
                    await asyncio.wait_for(stopped.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
        finally:
            self.release()
