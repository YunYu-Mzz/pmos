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
    instance = province.instance()
    state = StateStore(instance / "state")
    ledger = Ledger(instance / "ledger.sqlite3")
    try:
        _print({"province": province.id, "instance": str(instance), "control": state.control(),
                "block": state.blocked(), "hold": state.read("hold"), "dataset_holds": state.read("dataset_holds", {}),
                "status": state.read("status"), "unsettled_attempts": [dict(r) for r in ledger.unsettled()],
                "tasks": ledger.summary(), "requests_last_24h": ledger.counts(time.time() - 86400)})
    finally:
        ledger.close()


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
        if args.target == "provinces":
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
    except (SpecError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
