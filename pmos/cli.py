"""Command line: ``python -m pmos <province> <command>`` (see README.md)."""
from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys
import time
from pathlib import Path

from . import adapt
from .engine import Engine, enqueue
from .faults import Fault
from .ledger import Ledger
from .province import Province, province_ids
from .province import REPO
from .spec import SpecError
from .state import StateStore


def _print(value) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def cmd_provinces(_args) -> None:
    rows = []
    for pid in province_ids():
        province = Province.load(pid)
        statuses: dict[str, int] = {}
        for spec in province.datasets().values():
            statuses[spec.status] = statuses.get(spec.status, 0) + 1
        rows.append({"id": pid, "name": province.name, "origin": province.origin, "datasets": statuses})
    _print(rows)


def cmd_check(province: Province, _args) -> None:
    specs = province.datasets()
    keepalive = province.keepalive_spec()
    _print({"province": province.id, "datasets": len(specs), "enabled": len(province.enabled()),
            "keepalive_dataset": keepalive.id if keepalive else None, "ok": True})


def cmd_datasets(province: Province, args) -> None:
    rows = []
    for s in province.datasets().values():
        if args.status not in (None, s.status):
            continue
        row = {"id": s.id, "name": s.name, "status": s.status, "period": s.period["kind"],
               "transport": s.request["transport"], "path": s.request.get("path"), "scopes": len(s.scopes)}
        if args.scopes:
            row["scope_keys"] = {key: values for key, values in s.scope_items() if key}
        rows.append(row)
    _print(rows)


def cmd_status(province: Province, args) -> None:
    from .reporting import ledger_snapshot, status_metrics
    if getattr(args, "mail", False):
        # For a status mail bridge: counts and timestamps only, read-only.
        _print({"province": province.id, **status_metrics(province.instance())})
        return
    _print({"province": province.id, "release": "offline_only", **ledger_snapshot(province.instance() / "ledger.sqlite3")})


def cmd_control(province: Province, args) -> None:
    state = StateStore(province.instance() / "state")
    changes = {"start": {"collect": True, "paused": False}, "stop": {"collect": False},
               "pause": {"paused": True}, "resume": {"paused": False}}[args.command]
    _print(state.set_control(**changes))


def cmd_ack(province: Province, args) -> None:
    """After a human re-login / review: clear exactly the named block."""
    if not args.approval.strip():
        raise ValueError("approval_required")
    state = StateStore(province.instance() / "state")
    cleared = state.clear_block(args.reason)
    state.event("block_acknowledged", reason=args.reason, approval=args.approval)
    _print({"cleared": cleared})


def cmd_release_hold(province: Province, args) -> None:
    """After review: let one held dataset continue, or with --all every dataset held for exactly that reason."""
    if not args.approval.strip():
        raise ValueError("approval_required")
    state = StateStore(province.instance() / "state")
    holds = state.read("dataset_holds", {}) or {}
    if getattr(args, "all", False):
        if args.dataset:
            raise ValueError("dataset_or_all")
        names = [name for name, hold in holds.items() if hold["reason"] == args.reason]
        if not names:
            raise ValueError("no_dataset_hold")
    else:
        current = holds.get(args.dataset)
        if current is None:
            raise ValueError("no_dataset_hold")
        if current["reason"] != args.reason:
            raise ValueError("hold_reason_mismatch")
        names = [args.dataset]
    for name in names:
        del holds[name]
    state.write("dataset_holds", holds)
    for name in names:
        state.event("dataset_released", dataset=name, reason=args.reason, approval=args.approval)
    _print({"released": names, "reason": args.reason})


def cmd_skip_lapse(province: Province, args) -> None:
    """A person's decision: the suspected query lapse is a real denial (e.g. no data by hand either)."""
    from .engine import settle_query_lapse
    if not args.approval.strip():
        raise ValueError("approval_required")
    state = StateStore(province.instance() / "state")
    if state.read("query_lapse") is None:
        raise ValueError("no_query_lapse")
    held = settle_query_lapse(state, "query_lapse_skipped", approval=args.approval)
    _print({"province": province.id, "held": held})


def cmd_settle(province: Province, args) -> None:
    ledger = Ledger(province.instance() / "ledger.sqlite3")
    try:
        ledger.settle_unknown(args.attempt)
        _print({"attempt": args.attempt, "outcome": "unknown"})
    finally:
        ledger.close()


# Errors the PMOS page scripts throw before they call any page function (see
# _NATIVE_JS / _COMPONENT_JS): such an attempt cannot have sent anything.
PRE_CALL_CODES = frozenset({
    "webpack_runtime_missing", "webpack_require_unavailable", "native_module_missing", "native_export_missing",
    "component_missing", "component_ambiguous", "component_param_missing", "component_method_missing"})


def cmd_review_attempt(province: Province, args) -> None:
    """After review: settle the local page failure that blocked the province, then clear the block.

    Only the latest attempt, failed locally with no HTTP status, and only while
    it is the reviewed block. ``not_sent`` (only for an error raised before the
    page call): the attempt consumes nothing and the item stays pending.
    ``isolated``: the task settles ``failed``, never retried, identity and file
    stay consumed. Nothing is sent either way.
    """
    if not args.approval.strip():
        raise ValueError("approval_required")
    state = StateStore(province.instance() / "state")
    ledger = Ledger(province.instance() / "ledger.sqlite3")
    try:
        block = state.blocked()
        attempt = ledger.db.execute("SELECT * FROM attempts WHERE id=?", (args.attempt,)).fetchone()
        latest = ledger.db.execute("SELECT MAX(id) FROM attempts").fetchone()[0]
        if attempt is None or attempt["id"] != latest or ledger.unsettled():
            raise ValueError("latest_settled_attempt_required")
        if (attempt["outcome"], attempt["http_status"], attempt["business_status"]) != ("failed", None, None):
            raise ValueError("unsent_local_failure_required")
        if (block is None or block.get("category") != "local" or block.get("reason") != attempt["reason"]
                or block.get("dataset") != attempt["dataset"] or block.get("http_status") is not None):
            raise ValueError("matching_local_block_required")
        task = ledger.task(attempt["dataset"], attempt["period"], attempt["scope"])
        if task is None or task["status"] != "pending":
            raise ValueError("pending_task_required")
        entry = {"dataset": attempt["dataset"], "period": attempt["period"], "scope": attempt["scope"],
                 "attempt": attempt["id"], "decision": args.decision, "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        if args.decision == "not_sent":
            if attempt["reason"] not in PRE_CALL_CODES:
                raise ValueError("not_a_pre_call_failure")
            ledger.not_sent(attempt["id"], attempt["reason"], reviewed=True)
        else:
            reason = "isolated:" + attempt["reason"]
            ledger.settle_task(attempt["dataset"], attempt["period"], "failed", scope=attempt["scope"], reason=reason)
            entry.update(reason=reason, category="local")
            state.write("isolated_items", (state.read("isolated_items", []) or [])[-999:] + [entry])
        state.event("attempt_reviewed", approval=args.approval, **entry)
        state.clear_block(attempt["reason"])
        _print({"reviewed": entry, "block_cleared": attempt["reason"]})
    finally:
        ledger.close()


async def _browser(province: Province, *, record: bool = False):
    from playwright.async_api import async_playwright
    from .browser.attach import attach
    from .browser.session import Recorder, visible_logout
    from .browser.transport import PageTransport, Templates
    from . import memory
    # Before the driver starts (NODE_OPTIONS) and before any CDP connection.
    memory.prepare_driver(province, province.instance() / "state")
    memory.install_asyncio_wait_fix()
    playwright = await async_playwright().start()
    browser, page = await attach(playwright, province.origin, province.browser_config())
    tasks: set = set()

    def spawn(coroutine):
        task = asyncio.ensure_future(coroutine)
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    templates = Templates(province.origin, ttl=float(province.browser_config().get("template_ttl_seconds", 900)))
    # Listen to page requests only during PMOS's own operations unless the
    # province asks otherwise (driver memory: see Templates).
    templates.observe(page.context, spawn, always=province.browser_config().get("observe_templates") == "always")
    transport = PageTransport(page, province.origin, templates, province.policy.request_timeout_seconds,
                              province.scripts(), gate=bool(province.browser_config().get("request_gate")),
                              main_frame_fallback=province.browser_config().get("template_frame_fallback") == "main_frame")
    # Frames a previous run opened may carry a session from before a re-login.
    await transport.reset_frames()

    async def reopen(old):
        """A fresh page of the crashed one's role: the work page from the portal, else the same address."""
        from .browser.attach import work_page
        config = province.browser_config()
        if config.get("work_page"):
            return await work_page(browser, province.origin, config["work_page"], exclude=(old,))
        fresh = await old.context.new_page()
        await fresh.goto(old.url, wait_until="load", timeout=province.policy.request_timeout_seconds * 1000)
        return fresh
    transport.reopen = reopen
    recorder = None
    if record:
        recorder = Recorder(province.origin, province.instance() / "captures" / f"{time.strftime('%Y%m%d-%H%M%S')}.jsonl")
        recorder.attach(page.context, spawn)

    async def check_session():
        page = transport.page             # a crash recovery replaces it
        if not browser.is_connected() or page.is_closed():
            return Fault("browser_disconnected", "unknown")
        code = await visible_logout(page)
        return Fault(code, "auth") if code else None

    return playwright, transport, check_session, recorder


def _maintenance(engine: Engine, transport) -> list:
    """Memory upkeep between ticks, and (opt-in per province) the memory admission."""
    from .memory import Admission, BrowserReclaim, Maintenance, PageHeapRefresh
    page = getattr(transport, "page", None)
    upkeep = [Maintenance(engine.state, getattr(getattr(page, "context", None), "browser", None))]
    # Shanxi (merged from shanxi/pmos-live 693e3c9): an in-place reload of the site tab past a JS heap limit.
    refresh = engine.province.browser_config().get("page_heap_refresh_mib")
    if page is not None and refresh:
        upkeep.append(PageHeapRefresh(engine.state, page, transport, limit_mib=float(refresh)))
    alert = engine.province.browser_config().get("renderer_alert")
    profile = engine.province.browser_config().get("profile_dir")
    if alert and profile:
        from .memory import RendererWatch
        upkeep.append(RendererWatch(engine.state, str(Path(profile).expanduser()), alert))
    guard = engine.province.browser_config().get("memory_guard")
    if guard:
        engine.admission = Admission(engine.state, guard)
        if page is not None:
            upkeep.append(BrowserReclaim(engine.state, page, guard))
    return upkeep


def _stop_event() -> asyncio.Event:
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stopped.set)
    return stopped


async def _run(province: Province) -> None:
    playwright, transport, check_session, _ = await _browser(province)
    try:
        engine = Engine(province, transport, check_session)
        await engine.run(_stop_event(), between=_maintenance(engine, transport))
    finally:
        await playwright.stop()  # disconnect only; the browser keeps running


def acceptance_gate(province: Province) -> dict:
    """Acceptance runs need this instance's reviewed handoff; checked before any browser attach."""
    from .ledger import Ledger
    path = province.instance() / "ledger.sqlite3"
    if not path.is_file():
        raise ValueError("acceptance_requires_a_handoff_ledger; see docs/SHANXI_MIGRATION.md")
    ledger = Ledger(path)
    try:
        handoff = ledger.handoff()
        if handoff is None:
            raise ValueError("acceptance_requires_owner_handoff_receipt")
        if handoff["province"] != province.id:
            raise ValueError("acceptance_receipt_province_mismatch")
        blocker = ledger.dispatch_blocker()
        if blocker:
            raise ValueError(f"acceptance_not_dispatchable:{blocker}")
        if StateStore(province.instance() / "state").blocked():
            raise ValueError("acceptance_instance_blocked_review_first")
        return {**handoff, "used": ledger.attempts_since_handoff(), "request_budget": ledger.request_budget()}
    finally:
        ledger.close()


async def _accept(province: Province, handoff: dict) -> None:
    """Work the receipt's scope until its budget is used, anything blocks, or the scope is done."""
    print(json.dumps({"acceptance": province.id, "scope": len(handoff["scope"]), "budget": handoff["request_budget"],
                      "used": handoff["used"], "receipt_sha256": handoff["receipt_sha256"]}, ensure_ascii=False),
          file=sys.stderr)
    playwright, transport, check_session, _ = await _browser(province)
    try:
        engine = Engine(province, transport, check_session, account_alias=handoff["account_alias"])
        engine.state.set_control(collect=True, stop_all=False)

        def finished() -> bool:
            phase = (engine.state.read("status", {}) or {}).get("phase")
            return bool(engine.ledger.dispatch_blocker() or not _held(engine.state)
                        or engine.state.control().get("stop_all") or phase == "complete")
        await engine.run(_stop_event(), stop_when=finished,  # closes the engine's ledger
                         between=_maintenance(engine, transport))
        ledger = Ledger(province.instance() / "ledger.sqlite3")
        try:
            attempts = ledger.attempts_since_handoff()
        finally:
            ledger.close()
        _print({"acceptance_finished": province.id, "status": engine.state.read("status"),
                "block": engine.state.blocked(), "attempts": attempts})
    finally:
        await playwright.stop()  # disconnect only; the browser keeps running


def _held(state: StateStore) -> bool:
    """No block, or one the worker holds while renewing the login (it stays for review).

    Like Shanxi's session hold: only a block that forbids keepalive, or a
    renewal that failed during the hold, ends the process.
    """
    block = state.blocked()
    if block is None:
        return True
    hold = state.read("hold", {}) or {}
    renewal_stopped = hold.get("block_at") == block["at"] and hold.get("keepalive") is False
    return bool(block.get("keepalive_allowed")) and not renewal_stopped


def cmd_probe_blank(province: Province, args) -> None:
    """Queue one blank-option probe for the running worker (it keeps the single request sequence) and wait."""
    spec = province.datasets().get(args.dataset)
    if spec is None or not spec.scopes:
        raise ValueError("probe_needs_a_per_option_dataset")
    state = StateStore(province.instance() / "state")
    command_id = enqueue(state, {"action": "probe_blank", "dataset": args.dataset, "period": args.period,
                                 **({"timeout": args.timeout} if args.timeout else {}),
                                 **({"option": args.option} if args.option else {})})
    deadline = time.time() + args.wait
    while time.time() < deadline:
        result = state.read("command_results", {}).get(command_id)
        if result is not None:
            _print(result)
            return
        time.sleep(2)
    _print({"queued": command_id, "note": "the worker has not run it yet"})


def cmd_queue_and_wait(province: Province, args) -> None:
    """Queue collect-one / probe-api-blank for the running worker (one request sequence) and wait for it."""
    spec = province.datasets().get(args.dataset)
    if spec is None or not spec.scopes:
        raise ValueError("needs_a_per_option_dataset")
    command = {"action": args.command.replace("-", "_"), "dataset": args.dataset}
    if getattr(args, "period", None):
        command["period"] = args.period
    if getattr(args, "option", None):
        command["option"] = args.option
    if getattr(args, "export_path", None):
        command.update(export_path=args.export_path, date_field=args.date_field)
    state = StateStore(province.instance() / "state")
    command_id = enqueue(state, command)
    deadline = time.time() + args.wait
    while time.time() < deadline:
        result = state.read("command_results", {}).get(command_id)
        if result is not None:
            _print(result)
            return
        time.sleep(2)
    _print({"queued": command_id, "note": "the worker has not run it yet"})


def cmd_release_denied(province: Province, args) -> None:
    ids = []
    for part in args.attempts.split(","):
        low, _, high = part.partition("-")
        ids += list(range(int(low), int(high or low) + 1))
    if not ids or len(ids) > 1000:
        raise ValueError("attempt_range_invalid")
    ledger = Ledger(province.instance() / "ledger.sqlite3")
    try:
        if ledger.unsettled():
            raise ValueError("unsettled_attempt")
        reopened = ledger.release_denied(ids, args.approval, defer=getattr(args, "defer", False))
    finally:
        ledger.close()
    StateStore(province.instance() / "state").event("denied_released", attempts=f"{ids[0]}-{ids[-1]}",
                                                    count=len(reopened), approval=args.approval)
    _print({"province": province.id, "released": len(reopened), "datasets": sorted({r[1] for r in reopened})})


def cmd_release_early(province: Province, args) -> None:
    """Offline, user-approved: an answer saved before its day was published waits for its day again."""
    ledger = Ledger(province.instance() / "ledger.sqlite3")
    try:
        if ledger.unsettled():
            raise ValueError("unsettled_attempt")
        attempt, dataset, period, scope = ledger.release_early(args.attempt, args.approval)
    finally:
        ledger.close()
    StateStore(province.instance() / "state").event("early_answer_released", attempt=attempt, dataset=dataset,
                                                    period=period, approval=args.approval)
    _print({"province": province.id, "released": attempt, "dataset": dataset, "period": period, "scope": scope})


def cmd_release_discovery(province: Province, args) -> None:
    """After review: a report whose dropdown discovery was held reads its options again (next day's roll-forward)."""
    if not args.approval.strip():
        raise ValueError("approval_required")
    state = StateStore(province.instance() / "state")
    holds = state.read("discovery_holds", {}) or {}
    names = sorted(holds) if getattr(args, "all", False) else [args.dataset]
    if (getattr(args, "all", False) and args.dataset) or not names or any(n not in holds for n in names):
        raise ValueError("no_discovery_hold")
    released = {name: holds.pop(name)["reason"] for name in names}
    state.write("discovery_holds", holds)
    for name, reason in released.items():
        state.event("options_discovery_released", dataset=name, reason=reason, approval=args.approval)
    _print({"province": province.id, "released": released})


def cmd_review_unsent(province: Province, args) -> None:
    """Offline, user-approved: reviewed page-menu failures (and one stopped mid-navigation) consume nothing."""
    ids = []
    for part in args.attempts.split(","):
        low, _, high = part.partition("-")
        ids += list(range(int(low), int(high or low) + 1))
    if not ids or len(ids) > 1000:
        raise ValueError("attempt_range_invalid")
    ledger = Ledger(province.instance() / "ledger.sqlite3")
    try:
        reopened = ledger.review_unsent(ids, args.approval)
    finally:
        ledger.close()
    StateStore(province.instance() / "state").event("navigation_reviewed_unsent", attempts=",".join(map(str, ids)),
                                                    count=len(reopened), approval=args.approval)
    _print({"province": province.id, "not_sent": [r[0] for r in reopened],
            "tasks": [{"dataset": r[1], "period": r[2], "scope": r[3]} for r in reopened]})


def cmd_prioritize(province: Province, args) -> None:
    """These datasets are collected first (normal collection otherwise); --clear restores the handoff order."""
    state = StateStore(province.instance() / "state")
    names = [] if args.clear else [n for n in (args.datasets or "").split(",") if n]
    unknown = [n for n in names if n not in province.datasets()]
    if unknown or (not names and not args.clear):
        raise ValueError("prioritize_datasets_unknown:" + ",".join(unknown) if unknown else "prioritize_datasets_required")
    state.write("priority_datasets", names)
    state.event("datasets_prioritized", datasets=",".join(names) or "-")
    _print({"province": province.id, "priority_datasets": names})


def cmd_extend_scope(province: Province, args) -> None:
    """Offline: the approved order (a superset of the scope) and the datasets seeded daily from their spec periods."""
    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    specs = province.datasets()
    unknown = [name for name in plan["order"] if name not in specs]
    if unknown:
        raise ValueError("scope_dataset_without_spec:" + ",".join(unknown))
    weak = [name for name in plan["seeded"] if specs[name].status not in ("sampled", "verified", "enabled")]
    if weak:
        raise ValueError("seeded_dataset_not_proven:" + ",".join(weak))
    ledger = Ledger(province.instance() / "ledger.sqlite3")
    try:
        _print({"province": province.id, **ledger.extend_scope(plan["order"], plan["seeded"], args.approval)})
    finally:
        ledger.close()


def cmd_import_manifest(province: Province, args) -> None:
    """Offline: the legacy main archive manifest as consumed export tombstones, so nothing it saved is sent again."""
    if province.id != "shanxi":
        raise ValueError("legacy_manifest_import_is_shanxi_only")
    from .migration.shanxi import manifest_tombstones
    ledger = Ledger(province.instance() / "ledger.sqlite3")
    try:
        handoff = ledger.handoff()
        if handoff is None:
            raise ValueError("late_import_requires_a_handoff")
        found = manifest_tombstones(args.manifest, province.datasets(), handoff["account_alias"])
        result = ledger.import_late_legacy(f"manifest:{args.manifest.name}", found["snapshot_sha256"], found["rows"],
                                           args.approval)
        _print({"province": province.id, **result, "skipped": found["skipped"], "datasets": found["datasets"],
                "snapshot_sha256": found["snapshot_sha256"]})
    finally:
        ledger.close()


def cmd_cutover(province: Province, args) -> None:
    """Offline cutover planning and ledger building (no browser, no site request)."""
    import importlib
    try:
        module = importlib.import_module(f".migration.{province.id}", __package__)
    except ModuleNotFoundError:
        raise ValueError("no_cutover_module_for_province") from None
    directory = province.datasets_dir
    if args.command == "cutover-plan":
        plan = (module.plan_cutover(args.snapshot) if province.id == "liaoning"
                else module.plan_cutover(args.snapshot, directory))
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            with args.report.open("x", encoding="utf-8") as stream:
                json.dump(plan, stream, ensure_ascii=False, indent=1)
        _print({k: plan[k] for k in ("ready", "global_blockers", "scope", "pending_in_scope", "pending_total",
                                      "excluded_pending", "plan_sha256") if k in plan})
        return
    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    cutover = json.loads((province.directory / "cutover.json").read_text(encoding="utf-8"))
    if province.id != "shanxi":
        raise ValueError("cutover_apply_cli_is_shanxi_only_for_now")
    result = module.apply_cutover(args.snapshot, directory, province.instance(), plan,
                                  legacy_owner={"stopped_evidence": args.owner_evidence}, code_sha=args.code_sha,
                                  approval=args.approval, request_budget=args.budget, cutover=cutover)
    _print({"receipt_sha256": result["receipt"]["receipt_sha256"], "scope": len(result["receipt"]["scope"]),
            "budget": result["receipt"]["request_budget"], "seeded": sum(result["seeded"].values()),
            "ledger": result["ledger"]})


async def _capture(province: Province, minutes: float) -> None:
    playwright, _, _, recorder = await _browser(province, record=True)
    try:
        print(f"Recording {province.name} API calls to {recorder.path} for {minutes} min; "
              "open the report and click query/export in the browser. Ctrl-C to finish.", file=sys.stderr)
        stopped = _stop_event()
        try:
            await asyncio.wait_for(stopped.wait(), timeout=minutes * 60)
        except asyncio.TimeoutError:
            pass
        _print({"captures": recorder.count, "file": str(recorder.path)})
    finally:
        await playwright.stop()


def cmd_candidate(province: Province, args) -> None:
    captures = sorted((province.instance() / "captures").glob("*.jsonl"))
    if not captures:
        raise SpecError("no capture files; run `capture` first")
    path = captures[-1] if args.file is None else args.file
    entry = adapt.read_capture(path, args.index)
    spec = adapt.candidate_from_capture(province, entry, args.id, args.name, args.period, args.start,
                                        args.date_field, args.transport)
    _print(spec.to_json())


def cmd_sample(province: Province, args) -> None:
    """Queue a sample for the running worker and wait for its result."""
    state = StateStore(province.instance() / "state")
    if args.dataset not in province.datasets():
        raise SpecError(f"unknown dataset {args.dataset}")
    command_id = enqueue(state, {"action": "sample", "dataset": args.dataset, "period": args.period,
                                 "scope": args.scope})
    deadline = time.time() + args.wait
    while time.time() < deadline:
        result = state.read("command_results", {}).get(command_id)
        if result is not None:
            _print(result)
            return
        time.sleep(2)
    _print({"queued": command_id, "note": "worker has not run it yet; is `pmos <id> run` active?"})


def cmd_enable(province: Province, args) -> None:
    spec = province.datasets()[args.dataset]
    _print(adapt.enable(spec, force_reason=args.force_reason).to_json())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pmos", description="Unified PMOS province collector")
    sub = parser.add_subparsers(dest="target", required=True)
    sub.add_parser("provinces", help="list provinces and dataset counts")
    offline_demo = sub.add_parser("offline-demo", help="fixed sanitized fixtures through the shared engine; no browser")
    offline_demo.add_argument("province", choices=["liaoning", "anhui", "shanxi", "all"])
    replay_parser = sub.add_parser("replay", help="read one controlled original; print aggregates only")
    replay_parser.add_argument("province", choices=["liaoning", "anhui", "shanxi"])
    replay_parser.add_argument("input", type=lambda v: __import__("pathlib").Path(v))
    replay_parser.add_argument("--sha256", required=True)
    replay_parser.add_argument("--contract", type=lambda v: __import__("pathlib").Path(v),
                               help="explicit phase-two bound contract; never use a credential config")
    replay_parser.add_argument("--contract-sha256")
    audit = sub.add_parser("offline-audit", help="rebuild local retained-source inventory; no network or queue writes")
    audit.add_argument("province", choices=["liaoning", "anhui", "shanxi"])
    audit.add_argument("--legacy-root", type=lambda v: __import__("pathlib").Path(v))
    audit.add_argument("--report", type=lambda v: __import__("pathlib").Path(v), help="optional output under pmos/var only")
    legacy = sub.add_parser("legacy-review", help="read a declared old queue and optionally index a NEW offline shadow")
    legacy.add_argument("province", choices=["liaoning", "anhui", "shanxi"])
    legacy.add_argument("format", choices=["journal", "jobs", "liaoning", "frontier", "sqlite-requests", "sqlite-publication"])
    legacy.add_argument("input", type=lambda v: __import__("pathlib").Path(v))
    legacy.add_argument("--sha256", required=True)
    legacy.add_argument("--account-alias", required=True)
    legacy.add_argument("--dataset", default="")
    legacy.add_argument("--period-kind", choices=["day", "week", "month", "year", "snapshot", "range", "week_start_monday"])
    legacy.add_argument("--operation", choices=["query", "export", "listing", "attachment", "article"],
                        help="reviewed operation; do not infer export/query from the queue filename")
    legacy.add_argument("--snapshot-anchor")
    legacy.add_argument("--attempts", type=lambda v: __import__("pathlib").Path(v))
    legacy.add_argument("--attempts-sha256")
    legacy.add_argument("--mapping", type=lambda v: __import__("pathlib").Path(v))
    legacy.add_argument("--mapping-sha256")
    legacy.add_argument("--shadow-dir", type=lambda v: __import__("pathlib").Path(v))
    queue_audit = sub.add_parser("legacy-audit", help="audit declared local legacy journals into a temporary fenced index")
    queue_audit.add_argument("province", choices=["liaoning", "anhui", "shanxi"])
    queue_audit.add_argument("--legacy-root", type=lambda v: __import__("pathlib").Path(v))
    queue_audit.add_argument("--report", type=lambda v: __import__("pathlib").Path(v))
    new = sub.add_parser("new-province", help="scaffold provinces/<id>")
    new.add_argument("id")
    new.add_argument("--name", required=True)
    new.add_argument("--origin", required=True)
    for pid in province_ids():
        p = sub.add_parser(pid, help=f"{pid} commands")
        commands = p.add_subparsers(dest="command", required=True)
        commands.add_parser("check", help="validate province.json and every dataset spec")
        d = commands.add_parser("datasets", help="list dataset specs")
        d.add_argument("--status")
        d.add_argument("--scopes", action="store_true", help="list scope keys and values")
        st_parser = commands.add_parser("status", help="instance status (no browser, no requests)")
        st_parser.add_argument("--mail", action="store_true", help="the metrics a status mail needs, as JSON")
        for name in ("start", "stop", "pause", "resume"):
            commands.add_parser(name, help=f"{name} collection (read by the running worker)")
        a = commands.add_parser("ack", help="clear a reviewed block, e.g. after re-login")
        a.add_argument("reason")
        a.add_argument("--approval", required=True, help="the user's approval, verbatim")
        rh = commands.add_parser("release-hold", help="let one reviewed held dataset continue")
        rh.add_argument("dataset", nargs="?")
        rh.add_argument("--all", action="store_true", help="every dataset held for exactly --reason")
        rh.add_argument("--reason", required=True, help="the hold's reason, as status shows it")
        rh.add_argument("--approval", required=True, help="the user's approval, verbatim")
        sl = commands.add_parser("skip-lapse", help="a person's decision: the suspected query lapse is a real denial")
        sl.add_argument("--approval", required=True, help="the user's words, verbatim")
        st = commands.add_parser("settle-unknown", help="mark an interrupted attempt as unknown after review")
        st.add_argument("attempt", type=int)
        ra = commands.add_parser("review-attempt",
                                 help="offline: after review, settle the failed attempt of a local-fault block")
        ra.add_argument("attempt", type=int)
        ra.add_argument("--decision", required=True, choices=("not_sent", "isolated"))
        ra.add_argument("--approval", required=True, help="the user's approval, verbatim")
        commands.add_parser("run", help="attach to the logged-in browser and run the worker")
        commands.add_parser("accept", help="acceptance run: only with a handoff receipt, stops at its budget")
        commands.add_parser("stop-all", help="no request of any kind, keepalive included (read by the worker)")
        eb = commands.add_parser("extend-budget", help="offline: add a user-approved request budget to the handoff")
        eb.add_argument("--add", required=True, type=int)
        eb.add_argument("--approval", required=True, help="the user's approval, verbatim")
        cp = commands.add_parser("cutover-plan", help="offline: which datasets move from a frozen legacy snapshot")
        cp.add_argument("--snapshot", required=True, type=lambda v: __import__("pathlib").Path(v))
        cp.add_argument("--report", type=lambda v: __import__("pathlib").Path(v), help="write the full plan here")
        ca = commands.add_parser("cutover-apply", help="offline: build a NEW instance ledger with tombstones and the handoff")
        ca.add_argument("--snapshot", required=True, type=lambda v: __import__("pathlib").Path(v))
        ca.add_argument("--plan", required=True, type=lambda v: __import__("pathlib").Path(v))
        ca.add_argument("--budget", required=True, type=int)
        ca.add_argument("--approval", required=True, help="the user's approval, verbatim")
        ca.add_argument("--owner-evidence", required=True, help="how the legacy owner was proven stopped")
        ca.add_argument("--code-sha", required=True)
        pb = commands.add_parser("probe-blank", help="queue one blank-option request (1000 per page) for the running worker")
        pb.add_argument("dataset")
        pb.add_argument("--period", required=True)
        pb.add_argument("--wait", type=float, default=180)
        pb.add_argument("--timeout", type=float, help="page-call timeout for this probe (at least the policy's)")
        pb.add_argument("--option", default="", help="an option value instead of blank (e.g. the page's default)")
        co = commands.add_parser("collect-one", help="queue: collect the dataset's next pending item now (normal collection)")
        co.add_argument("dataset")
        co.add_argument("--period", help="with --option: that day and option instead of the next pending item")
        co.add_argument("--option")
        co.add_argument("--wait", type=float, default=180)
        pa = commands.add_parser("probe-api-blank",
                                 help="queue: replay the page's query once with every option blank (after collect-one)")
        pa.add_argument("dataset")
        pa.add_argument("--period", required=True)
        pa.add_argument("--wait", type=float, default=180)
        pe = commands.add_parser("probe-export-blank", help="queue: the export endpoint once with every option blank")
        pe.add_argument("dataset")
        pe.add_argument("--period", required=True)
        pe.add_argument("--export-path", required=True)
        pe.add_argument("--date-field", default="tdate")
        pe.add_argument("--option", default="", help="an option value instead of blank (e.g. the page's default)")
        pe.add_argument("--wait", type=float, default=180)
        rd = commands.add_parser("release-denied", help="offline: user-approved, denied (40302) attempts back to pending")
        rd.add_argument("attempts", help="attempt ids, e.g. 622-637 or 622,623")
        rd.add_argument("--defer", action="store_true", help="not yet published: the items wait for the next day")
        rd.add_argument("--approval", required=True, help="the user's approval, verbatim")
        re_ = commands.add_parser("release-early",
                                  help="offline: user-approved, an answer saved before its day was published waits again")
        re_.add_argument("attempt", type=int)
        re_.add_argument("--approval", required=True, help="the user's approval, verbatim")
        pr = commands.add_parser("prioritize", help="these datasets are collected first (read by the running worker)")
        pr.add_argument("datasets", nargs="?", help="comma-separated dataset ids, in order")
        pr.add_argument("--clear", action="store_true", help="back to the handoff order")
        ru = commands.add_parser("review-unsent",
                                 help="offline: user-approved, reviewed page-menu failures (or one stopped mid-navigation) consume nothing")
        ru.add_argument("attempts", help="attempt ids, e.g. 3-16 or 3,4,17")
        ru.add_argument("--approval", required=True, help="the user's approval, verbatim")
        rdisc = commands.add_parser("release-discovery", help="after review: a held dropdown discovery runs again")
        rdisc.add_argument("dataset", nargs="?")
        rdisc.add_argument("--all", action="store_true", help="every held discovery")
        rdisc.add_argument("--approval", required=True, help="the user's approval, verbatim")
        es = commands.add_parser("extend-scope", help="offline: add a user-approved collection order and daily seeding")
        es.add_argument("--plan", required=True, type=lambda v: __import__("pathlib").Path(v),
                        help="JSON with order and seeded, e.g. provinces/<id>/full_year_scope.json")
        es.add_argument("--approval", required=True, help="the user's approval, verbatim")
        im = commands.add_parser("import-legacy-manifest",
                                 help="offline: the legacy main archive manifest as consumed tombstones (after handoff)")
        im.add_argument("--manifest", required=True, type=lambda v: __import__("pathlib").Path(v))
        im.add_argument("--approval", required=True, help="the user's approval, verbatim")
        c = commands.add_parser("capture", help="record the page's own API calls while a human browses")
        c.add_argument("--minutes", type=float, default=10)
        cand = commands.add_parser("candidate", help="create a candidate spec from a captured call")
        cand.add_argument("index", type=int)
        cand.add_argument("--id", required=True)
        cand.add_argument("--name", required=True)
        cand.add_argument("--period", choices=["day", "month", "year", "snapshot"], required=True)
        cand.add_argument("--start", required=True)
        cand.add_argument("--date-field", action="append")
        cand.add_argument("--transport", choices=["fetch", "component", "ui"], default="fetch")
        cand.add_argument("--file", type=lambda v: __import__("pathlib").Path(v))
        s = commands.add_parser("sample", help="one adaptation sample through the running worker")
        s.add_argument("dataset")
        s.add_argument("period")
        s.add_argument("--scope", help="scope key (see `datasets --scopes`); default: the first scope")
        s.add_argument("--wait", type=float, default=300)
        e = commands.add_parser("enable", help="enable a verified dataset")
        e.add_argument("dataset")
        e.add_argument("--force-reason")
    args = parser.parse_args(argv)
    try:
        if args.target == "offline-demo":
            from .offline import demo
            ids = ["liaoning", "anhui", "shanxi"] if args.province == "all" else [args.province]
            _print([asyncio.run(demo(pid)) for pid in ids])
        elif args.target == "replay":
            from .offline import replay
            contract = None
            if args.contract is not None:
                from .queue_compat import read_json
                if not args.contract_sha256:
                    raise ValueError("contract_sha256_required")
                contract, _ = read_json(args.contract, expected_sha256=args.contract_sha256)
            _print(replay(args.province, args.input, args.sha256, contract=contract))
        elif args.target == "offline-audit":
            from .offline import audit_inventory
            from .contracts import canonical
            report = audit_inventory(args.province, args.legacy_root or REPO.parent / args.province)
            if args.report:
                path = offline_output(args.report)
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("xb") as stream:
                    stream.write(canonical(report) + b"\n")
            _print({k: report[k] for k in report if k not in {
                "entries", "units", "excluded_units", "sources", "indicator_rows", "input_refs", "source_references",
                "catalog_references", "public_input_hashes"}})
        elif args.target == "legacy-review":
            cmd_legacy_review(args)
        elif args.target == "legacy-audit":
            from .queue_audit import audit_queues
            from .contracts import canonical
            report = audit_queues(args.province, args.legacy_root or REPO.parent / args.province)
            if args.report:
                path = offline_output(args.report)
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("xb") as stream:
                    stream.write(canonical(report) + b"\n")
            _print({k: report[k] for k in report if k not in {"source_views", "source_refs"}})
        elif args.target == "new-province" or getattr(args, "command", None) not in (
                None, "check", "datasets", "status", "accept", "stop-all", "cutover-plan", "cutover-apply",
                "extend-budget", "review-attempt", "ack", "release-hold", "extend-scope", "pause", "resume", "skip-lapse",
                "import-legacy-manifest", "release-denied", "release-early", "release-discovery", "review-unsent", "prioritize", "probe-blank", "collect-one", "probe-api-blank", "probe-export-blank"):
            # This release cannot attach outside acceptance, queue a sample or
            # enable a dataset; clearing a block or a hold needs the user's
            # approval text. pause/resume only toggle collection of a running
            # worker (it keeps renewing the login). No environment variable bypass.
            raise ValueError("offline_release_live_actions_disabled; see docs/ACCEPTANCE.md")
        elif args.target == "provinces":
            cmd_provinces(args)
        elif args.target == "new-province":
            _print({"created": str(adapt.scaffold(args.id, args.name, args.origin))})
        else:
            province = Province.load(args.target)
            handlers = {"check": cmd_check, "datasets": cmd_datasets, "status": cmd_status,
                        "start": cmd_control, "stop": cmd_control, "pause": cmd_control, "resume": cmd_control,
                        "ack": cmd_ack, "settle-unknown": cmd_settle, "review-attempt": cmd_review_attempt,
                        "release-hold": cmd_release_hold, "skip-lapse": cmd_skip_lapse,
                        "candidate": cmd_candidate,
                        "sample": cmd_sample, "enable": cmd_enable}
            if args.command == "run":
                asyncio.run(_run(province))
            elif args.command == "accept":
                handoff = acceptance_gate(province)
                asyncio.run(_accept(province, handoff))
            elif args.command == "stop-all":
                StateStore(province.instance() / "state").set_control(stop_all=True)
                _print({"province": province.id, "stop_all": True})
            elif args.command in ("cutover-plan", "cutover-apply"):
                cmd_cutover(province, args)
            elif args.command == "probe-blank":
                cmd_probe_blank(province, args)
            elif args.command in ("collect-one", "probe-api-blank", "probe-export-blank"):
                cmd_queue_and_wait(province, args)
            elif args.command == "release-denied":
                cmd_release_denied(province, args)
            elif args.command == "release-early":
                cmd_release_early(province, args)
            elif args.command == "release-discovery":
                cmd_release_discovery(province, args)
            elif args.command == "review-unsent":
                cmd_review_unsent(province, args)
            elif args.command == "prioritize":
                cmd_prioritize(province, args)
            elif args.command == "extend-scope":
                cmd_extend_scope(province, args)
            elif args.command == "import-legacy-manifest":
                cmd_import_manifest(province, args)
            elif args.command == "extend-budget":
                ledger = Ledger(province.instance() / "ledger.sqlite3")
                try:
                    _print({"province": province.id, **ledger.extend_handoff(args.add, args.approval)})
                finally:
                    ledger.close()
            elif args.command == "capture":
                asyncio.run(_capture(province, args.minutes))
            else:
                handlers[args.command](province, args)
        return 0
    except (SpecError, ValueError, Fault) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def offline_output(path):
    result = path.resolve()
    if not result.is_relative_to((REPO / "var").resolve()):
        raise ValueError("offline_output_must_be_under_pmos_var")
    return result


def cmd_legacy_review(args):
    import hashlib
    from .queue_compat import (QueueContext, read_json, decode_journal, decode_jobs, decode_liaoning,
        decode_frontier, read_native_sqlite, summarize, import_shadow)
    mapping = {}
    if args.mapping:
        if not args.mapping_sha256:
            raise ValueError("mapping_sha256_required")
        mapping, _ = read_json(args.mapping, expected_sha256=args.mapping_sha256)
    context = QueueContext(args.province, args.account_alias, args.dataset, args.period_kind,
        operation=args.operation or mapping.get("operation", "query"),
        dataset_aliases=mapping.get("dataset_aliases", {}), snapshot_anchor=args.snapshot_anchor)
    context.check()
    if args.format.startswith("sqlite-"):
        if args.input.is_symlink():
            raise ValueError("regular_queue_snapshot_required")
        before = hashlib.sha256(args.input.read_bytes()).hexdigest()
        if before != args.sha256:
            raise ValueError("queue_snapshot_sha256_changed")
        rows = read_native_sqlite(args.input, context, table="work" if args.format.endswith("publication") else "requests")
    else:
        value, before = read_json(args.input, expected_sha256=args.sha256)
        if args.format == "journal":
            rows = decode_journal(value, context)
        elif args.format == "jobs":
            attempts = None
            if args.attempts:
                if not args.attempts_sha256:
                    raise ValueError("attempts_sha256_required")
                attempts, _ = read_json(args.attempts, expected_sha256=args.attempts_sha256)
            rows = decode_jobs(value, context, attempts=attempts)
        elif args.format == "liaoning":
            rows = decode_liaoning(value, context, period_kinds=mapping.get("period_kinds"),
                                    source_contracts=mapping.get("source_contracts"))
        else:
            rows = decode_frontier(value, context)
    if hashlib.sha256(args.input.read_bytes()).hexdigest() != before:
        raise ValueError("rolling_source_changed_requires_new_snapshot")
    result = {"province": args.province, "source_sha256": before, **summarize(rows)}
    if args.shadow_dir:
        directory = offline_output(args.shadow_dir)
        if directory.exists():
            raise ValueError("new_offline_shadow_directory_required")
        ledger = Ledger(directory / "ledger.sqlite3")
        try:
            result.update(import_shadow(ledger, rows, context, source_id=args.format, snapshot_sha256=before))
            result["index"] = ledger.legacy_summary()
        finally:
            ledger.close()
    _print(result)
