"""Command line: ``python -m pmos <province> <command>`` (see README.md)."""
from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys
import time

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


def cmd_status(province: Province, _args) -> None:
    from .reporting import ledger_snapshot
    _print({"province": province.id, "release": "offline_only", **ledger_snapshot(province.instance() / "ledger.sqlite3")})


def cmd_control(province: Province, args) -> None:
    state = StateStore(province.instance() / "state")
    changes = {"start": {"collect": True, "paused": False}, "stop": {"collect": False},
               "pause": {"paused": True}, "resume": {"paused": False}}[args.command]
    _print(state.set_control(**changes))


def cmd_ack(province: Province, args) -> None:
    """After a human re-login / review: clear exactly the named block."""
    state = StateStore(province.instance() / "state")
    _print({"cleared": state.clear_block(args.reason)})


def cmd_settle(province: Province, args) -> None:
    ledger = Ledger(province.instance() / "ledger.sqlite3")
    try:
        ledger.settle_unknown(args.attempt)
        _print({"attempt": args.attempt, "outcome": "unknown"})
    finally:
        ledger.close()


async def _browser(province: Province, *, record: bool = False):
    from playwright.async_api import async_playwright
    from .browser.attach import attach
    from .browser.session import Recorder, visible_logout
    from .browser.transport import PageTransport, Templates
    playwright = await async_playwright().start()
    browser, page = await attach(playwright, province.origin, province.browser_config())
    tasks: set = set()

    def spawn(coroutine):
        task = asyncio.ensure_future(coroutine)
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    templates = Templates(province.origin)
    templates.observe(page.context, spawn)
    transport = PageTransport(page, province.origin, templates, province.policy.request_timeout_seconds,
                              province.scripts())
    recorder = None
    if record:
        recorder = Recorder(province.origin, province.instance() / "captures" / f"{time.strftime('%Y%m%d-%H%M%S')}.jsonl")
        recorder.attach(page.context, spawn)

    async def check_session():
        if not browser.is_connected() or page.is_closed():
            return Fault("browser_disconnected", "unknown")
        code = await visible_logout(page)
        return Fault(code, "auth") if code else None

    return playwright, transport, check_session, recorder


def _stop_event() -> asyncio.Event:
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stopped.set)
    return stopped


async def _run(province: Province) -> None:
    playwright, transport, check_session, _ = await _browser(province)
    try:
        await Engine(province, transport, check_session).run(_stop_event())
    finally:
        await playwright.stop()  # disconnect only; the browser keeps running


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
    legacy.add_argument("--period-kind", choices=["day", "week", "month", "year", "snapshot", "range"])
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
        commands.add_parser("status", help="instance status (no browser, no requests)")
        for name in ("start", "stop", "pause", "resume"):
            commands.add_parser(name, help=f"{name} collection (read by the running worker)")
        a = commands.add_parser("ack", help="clear a reviewed block, e.g. after re-login")
        a.add_argument("reason")
        st = commands.add_parser("settle-unknown", help="mark an interrupted attempt as unknown after review")
        st.add_argument("attempt", type=int)
        commands.add_parser("run", help="attach to the logged-in browser and run the worker")
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
        elif args.target == "new-province" or getattr(args, "command", None) not in (None, "check", "datasets", "status"):
            # This release cannot attach, clear a block, queue a sample or
            # enable a dataset. No environment variable bypass is provided.
            raise ValueError("offline_release_live_actions_disabled; see docs/ACCEPTANCE.md")
        elif args.target == "provinces":
            cmd_provinces(args)
        elif args.target == "new-province":
            _print({"created": str(adapt.scaffold(args.id, args.name, args.origin))})
        else:
            province = Province.load(args.target)
            handlers = {"check": cmd_check, "datasets": cmd_datasets, "status": cmd_status,
                        "start": cmd_control, "stop": cmd_control, "pause": cmd_control, "resume": cmd_control,
                        "ack": cmd_ack, "settle-unknown": cmd_settle, "candidate": cmd_candidate,
                        "sample": cmd_sample, "enable": cmd_enable}
            if args.command == "run":
                asyncio.run(_run(province))
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
