"""Liaoning: the legacy production manifest as PMOS specs, proven identity-equal.

Migration is safe only if nothing the legacy collector already consumed can be
sent again. The engine refuses a consumed identity, so every PMOS spec must
compute exactly the identity ``queue_compat`` derives from the legacy record of
the same task. This module

* turns legacy ``report`` sources (pdbln report components; the production
  manifest gives number, route, endpoint, period kind and verified
  selections; ``ReportAdapter`` gives the parameter names) into specs that send
  what the legacy browser code sends: ``queryParam[date_field]`` and
  ``queryParam[selection_field]`` on ``pdblnReport<number>``, then
  ``initData()``;
* checks parity: for every legacy record of those sources, the engine identity
  of the corresponding task equals the legacy identity. A source is eligible
  for cutover only when all its records map and match.

Read-only on the legacy root (declared files, SHA recorded); writes nothing
unless :func:`write_specs` is called with an explicit PMOS directory.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from pathlib import Path

from ..contracts import canonical
from ..engine import document_identity, task_identity
from ..faults import Fault
from ..queue_audit import _source_rule_refs
from ..queue_compat import QueueContext, decode_liaoning, read_json
from ..spec import DatasetSpec, SpecError, scope_key

MANIFEST = "state/server_verified_expansion_20261023/active_manifest.json"
QUEUES = ("state/server_verified_expansion_20261023/queue.json",
          "state/server_advanced_tasks_20261016.json", "state/server_sitewide_20261016_tasks.json")
APP = "/pxf-dif-settlement-outnetpub"
PERIOD_TOKEN = {"day": "{date}", "month": "{month}"}
PERIOD_START = {"day": "2026-01-01", "month": "2026-01"}


def _declared(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise FileNotFoundError(relative)
    return path


def read_manifest(root: Path) -> tuple[dict, str]:
    manifest, digest = read_json(_declared(root, MANIFEST))
    if not isinstance(manifest.get("sources"), dict):
        raise ValueError("liaoning_manifest_sources_required")
    return manifest, digest


def publish_lag(title: str) -> int:
    """Days a report's day is held back before it is asked for (PMOS saves an empty or partial answer for good).

    As Anhui (2026-11-01) and Liaoning's lesson 22: an actual report is published the next day (非市场机组总出力
    10-29/10-30 were empty the same day, 96 points later). What looks ahead (预测, 日前, a 计划 that is not its
    执行情况) waits 1 day, never the running one; everything else waits 2.
    """
    ahead = "预测" in title or "日前" in title or ("计划" in title and "执行" not in title)
    return 1 if ahead else 2


def report_spec(source: dict, rule: dict, manifest_sha256: str) -> dict:
    """One legacy report source as a PMOS spec (never enabled here)."""
    kind, number, dataset = source.get("period"), source.get("number"), source["dataset"]
    if source.get("kind") != "report" or kind not in PERIOD_TOKEN:
        raise ValueError("report_day_or_month_source_required")
    if not (isinstance(number, str) and number.isdigit() and len(number) == 3):
        raise ValueError("report_number_required")
    endpoint, route = source.get("endpoint"), source.get("route")
    if endpoint != f"/px-dif-settlement-infpubquery/pdblnReportController/pdblnReport{number}" \
            or route != f"{APP}/pdbln/report/PdblnReport{number}":
        raise ValueError("report_endpoint_or_route_unexpected")
    date_field, selection_field = rule["period_field"], rule.get("selection_field")
    set_values = {"tableData": [], f"queryParam.{date_field}": PERIOD_TOKEN[kind]}
    contract = {"request_data_path": ["set"], "date_fields": [f"queryParam.{date_field}"], "dimension_fields": {}}
    scopes = []
    if selection_field:
        selections = source.get("selections")
        if not isinstance(selections, list) or not selections or not all(isinstance(s, str) and s for s in selections):
            raise ValueError("verified_selections_required")
        set_values[f"queryParam.{selection_field}"] = "{scope[" + selection_field + "]}"
        contract["dimension_fields"] = {f"queryParam.{selection_field}": selection_field}
        scopes = [{selection_field: value} for value in selections]
    elif source.get("selections") not in (None, [""], []):
        raise ValueError("selections_without_selection_field")
    proofs = [p["sha256"] for p in source.get("proofs", []) if isinstance(p, dict) and isinstance(p.get("sha256"), str)]
    return {
        "id": dataset, "name": source.get("title") or dataset,
        "status": "verified" if proofs else "candidate",
        "period": {"kind": kind, "start": PERIOD_START[kind], "end": "today",
                   **({"lag_days": publish_lag(source.get("title") or dataset)} if kind == "day" else {})},
        "request": {"transport": "component", "method": source.get("method", "POST"), "path": endpoint,
                    "frame": f"/pdbln/report/PdblnReport{number}", "open_url": f"{APP}/#{route}",
                    **({"site_menu": {"source": source["source"], "route": route}} if source.get("source") else {}),
                    "component": f"pdblnReport{number}", "call": "initData", "set": set_values},
        "response": {"kind": "json"},
        **({"scopes": scopes} if scopes else {}),
        "source": {"legacy": "liaoning production manifest", "manifest": MANIFEST,
                   "manifest_sha256": manifest_sha256, "legacy_kind": "report", "number": number,
                   "legacy_source": source.get("source"), "legacy_scope": source.get("scope"),
                   "proof_sha256": proofs, "identity_contract": contract,
                   "note": "same component call as the legacy advanced/sitewide browser code"},
    }


DIMENSION = "state/dimension_coverage_20261024"
DIMENSION_RUNTIME = DIMENSION + "/runtime.json"
DIMENSION_OPERATIONS = DIMENSION + "/operations"
# The dimension lane asks each option of a day with exactly these two parameters (its issued jobs).
DIMENSION_RULE = {"period_field": "mdate", "selection_field": "name"}


def dimension_jobs(root: Path) -> tuple[list[tuple[dict, dict]], dict]:
    """(job, outcome) of every business request of the dimension lane, read-only.

    The lane (LNCrawler dimension_coverage) asks every option of a day for ten
    sources, the node prices among them; each request left
    ``operations/<key>/issued.json`` (dataset, period, stage, selection, key,
    params) and its ``receipt.json``. Dictionary reads (stage ``dictionary``)
    list a day's options and are no business data.
    """
    root = Path(root).resolve()
    directory = root / DIMENSION_OPERATIONS
    found, files = [], []
    for path in sorted(directory.glob("*/issued.json")) if directory.is_dir() else []:
        raw = path.read_bytes()
        files.append(hashlib.sha256(raw).hexdigest())
        issued = json.loads(raw)
        if issued.get("stage") != "data" or not isinstance(issued.get("key"), str) or not isinstance(issued.get("params"), dict):
            continue
        receipt_path = path.parent / "receipt.json"
        receipt = json.loads(receipt_path.read_bytes()) if receipt_path.is_file() else {}
        outcome = receipt.get("outcome") if isinstance(receipt.get("outcome"), dict) else {}
        # Sent (an issued job exists): without a settled outcome it is unknown, never asked again.
        state = outcome.get("outcome") or ("unknown" if receipt.get("phase") != "completed" else "held")
        # A parameter the lane left null (as the page's own init does, e.g. 1c01's ``name``) asks nothing:
        # the lane's own identity is the day and its one selection.
        params = {k: v for k, v in issued["params"].items() if v is not None}
        job = {"key": issued["key"], "dataset": issued["dataset"], "params": params,
               "period": issued.get("period"), "page": 1, "sent": True}
        found.append((job, {**outcome, "outcome": state}))
    ref = {"path": DIMENSION_OPERATIONS + "/*/issued.json", "files": len(files),
           "sha256": hashlib.sha256("".join(files).encode()).hexdigest()}
    return found, ref


def dimension_options(root: Path, jobs: list[tuple[dict, dict]], rules: dict | None = None) -> dict[str, list[str]]:
    """Every option the dimension lane asked or listed for a source (its scopes in PMOS).

    The option is the source's own selection field (``name`` for most, ``type`` for 1c01).
    """
    root = Path(root).resolve()
    options: dict[str, set] = {}
    for job, _ in jobs:
        field = ((rules or {}).get(job["dataset"]) or DIMENSION_RULE).get("selection_field")
        name = job["params"].get(field)
        if isinstance(name, str) and name:
            options.setdefault(job["dataset"], set()).add(name)
    try:
        runtime, _ = read_json(_declared(root, DIMENSION_RUNTIME))
    except FileNotFoundError:
        runtime = {}
    for dataset, source in (runtime.get("sources") or {}).items():
        current = (source or {}).get("current") or {}
        for name in current.get("options") or []:
            if isinstance(name, str) and name:
                options.setdefault(dataset, set()).add(name)
    return {dataset: sorted(names) for dataset, names in options.items()}


def build_specs(root: Path) -> dict:
    """Specs for the uniform report sources; every other source with its reason."""
    root = Path(root).resolve()
    manifest, manifest_sha = read_manifest(root)
    rules, code_refs = _source_rule_refs(root)
    jobs, _ = dimension_jobs(root)
    lane_params = {}
    for job, _ in jobs:
        lane_params.setdefault(job["dataset"], set()).add(tuple(sorted(job["params"])))
    rules = dict(rules)
    for name, shapes in lane_params.items():
        # The lane's own two parameters where the code yields no rule (sitewide_0400 日前节点价格).
        if name not in rules and shapes == {tuple(sorted(DIMENSION_RULE.values()))}:
            rules[name] = dict(DIMENSION_RULE)
    widened = dimension_options(root, jobs, rules)
    specs, deferred = {}, {}
    for name, source in manifest["sources"].items():
        if name in widened and rules.get(name, {}).get("selection_field"):
            # Every option the lane asked or listed, not only the one the manifest verified.
            source = {**source, "selections": sorted(set(source.get("selections") or []) - {""} | set(widened[name]))}
        kind = source.get("kind")
        if name in manifest.get("excluded", {}):
            deferred[name] = "excluded_by_legacy_manifest"
        elif kind != "report":
            deferred[name] = f"legacy_kind_{kind}_not_in_first_scope"
        elif name not in rules:
            deferred[name] = "no_legacy_parameter_rule"
        elif source.get("period") not in PERIOD_TOKEN:
            deferred[name] = f"period_{source.get('period')}_not_in_first_scope"
        else:
            try:
                value = report_spec(source, rules[name], manifest_sha)
                spec = DatasetSpec(**value)
                spec.check()
                specs[name] = value
            except (ValueError, KeyError, SpecError) as exc:
                deferred[name] = f"spec_rejected:{exc}"[:160]
    return {"manifest_sha256": manifest_sha, "code_refs": code_refs, "rules": rules,
            "specs": specs, "deferred": deferred, "dimension_sources": sorted(widened)}


def legacy_rows(root: Path, account_alias: str, built: dict) -> tuple[list, list]:
    """Decoded legacy records of the declared production queue files (read-only)."""
    root = Path(root).resolve()
    manifest, manifest_sha = read_manifest(root)
    kinds = {name: spec.get("period", "snapshot") for name, spec in manifest["sources"].items()}
    context = QueueContext("liaoning", account_alias)
    rows, refs = [], [{"path": MANIFEST, "sha256": manifest_sha}]
    for relative in QUEUES:
        try:
            value, digest = read_json(_declared(root, relative))
        except FileNotFoundError:
            refs.append({"path": relative, "missing": True})
            continue
        refs.append({"path": relative, "sha256": digest})
        rows += [(relative, row) for row in decode_liaoning(value, context, period_kinds=kinds,
                                                             source_contracts=built["rules"])]
    prior = {"sources": {name: {"tasks": {}, "outcomes": spec.get("prior_outcomes", {})}
                         for name, spec in manifest["sources"].items() if isinstance(spec.get("prior_outcomes"), dict)}}
    rows += [(MANIFEST + "#prior_outcomes", row) for row in decode_liaoning(prior, context, period_kinds=kinds,
                                                                            source_contracts=built["rules"])]
    jobs, dimension_ref = dimension_jobs(root)
    refs.append(dimension_ref)
    for job, outcome in jobs:
        lane = {"sources": {job["dataset"]: {"pending": [job], "outcomes": {job["key"]: outcome}}}}
        rows += [(DIMENSION_OPERATIONS, row) for row in decode_liaoning(lane, context, period_kinds=kinds,
                                                                         source_contracts=built["rules"])]
    return rows, refs


OPERATIONS = "state/server_verified_expansion_20261023/operations"


def issued_identities(root: Path, account_alias: str, built: dict) -> tuple[dict, dict]:
    """legacy_key -> identity from the legacy worker's own ``operations/<id>/issued.json`` (read-only).

    The verified-source queue keeps no query for a consumed task, but the
    worker wrote each issued job (key, dataset, params, period, selection,
    page) before sending it. The job decodes exactly like a pending task of
    that queue. A key issued with two different jobs is left unresolved.
    """
    root = Path(root).resolve()
    directory = root / OPERATIONS
    manifest, _ = read_manifest(root)
    kinds = {name: spec.get("period", "snapshot") for name, spec in manifest["sources"].items()}
    context = QueueContext("liaoning", account_alias)
    found, conflicting, files = {}, set(), []
    for path in sorted(directory.glob("*/issued.json")) if directory.is_dir() else []:
        raw = path.read_bytes()
        files.append(hashlib.sha256(raw).hexdigest())
        job = json.loads(raw).get("job")
        if not isinstance(job, dict) or not isinstance(job.get("key"), str) or not isinstance(job.get("dataset"), str):
            continue
        rows = decode_liaoning({"sources": {job["dataset"]: {"pending": [job], "outcomes": {}}}}, context,
                               period_kinds=kinds, source_contracts=built["rules"])
        identity = rows[0].identity if len(rows) == 1 and rows[0].legacy_key == job["key"] else None
        if identity is None:
            continue
        if job["key"] in found and found[job["key"]].key != identity.key:
            conflicting.add(job["key"])
        found.setdefault(job["key"], identity)
    for key in conflicting:
        del found[key]
    ref = {"path": OPERATIONS + "/*/issued.json", "files": len(files),
           "sha256": hashlib.sha256("".join(files).encode()).hexdigest(), "conflicting_keys": len(conflicting)}
    return found, ref


# An empty answer asked for less than this many days after its period ended is no tombstone: the
# site had not published it yet (Liaoning 2026-11-01: of 54 daily empties 49 were asked the same
# day and 5 the next, none later; e38351fe's 10-29/10-30 had 96 points when asked again). User,
# via the Liaoning session: "10-31、11-01 这类当天或第二天采到的空结果，当作未采处理".
EARLY_EMPTY_DAYS = 2


def early_empty_keys(root: Path) -> set[str]:
    """Legacy keys of the verified queue whose empty outcome came before its period was published."""
    from datetime import datetime, timedelta, timezone
    from ..periods import last_day
    root = Path(root).resolve()
    queue, _ = read_json(_declared(root, QUEUES[0]))
    jobs = {}
    directory = root / OPERATIONS
    for path in sorted(directory.glob("*/issued.json")) if directory.is_dir() else []:
        job = json.loads(path.read_bytes()).get("job")
        if isinstance(job, dict) and isinstance(job.get("key"), str):
            jobs.setdefault(job["key"], job)
    china = timezone(timedelta(hours=8))
    early = set()
    for group in (queue.get("sources") or queue.get("datasets") or {}).values():
        for key, outcome in (group.get("outcomes") or {}).items():
            if not isinstance(outcome, dict) or outcome.get("outcome") != "empty" or key not in jobs:
                continue
            period = jobs[key].get("period")
            kind = "day" if isinstance(period, str) and len(period) == 10 else "month" if isinstance(period, str) and len(period) == 7 else None
            try:
                asked = datetime.fromisoformat(str(outcome["finished_at"]).replace("Z", "+00:00")).astimezone(china).date()
                published = last_day(kind, period) + timedelta(days=EARLY_EMPTY_DAYS) if kind else None
            except (KeyError, ValueError, TypeError):
                continue
            if published is not None and asked < published:
                early.add(key)
    return early


def _early_empty(row, early: set):
    """A tombstone of an empty answer asked too early is imported as an unconsumed fact: PMOS asks again."""
    from dataclasses import replace
    if row.legacy_key in early and row.state == "empty" and row.consumed:
        return replace(row, state="pending", consumed=False)
    return row


# The daily reports the legacy worker ran on Crawlee queues, not in the verified queue the cutover read.
# system_load first: it is the keepalive. Their originals are the proof; PMOS's identity is one report and one day.
# Not nonmarket_generation_forecast (033): the site moved it to FineReport on 2026-10-23 (route spotgoodsSum), so
# the legacy worker excluded it from this lane; its originals here end on 10-16. It belongs to the engine lane.
DAILY_LANE = ("system_load", "centralized_solar_daily", "centralized_wind_daily", "distributed_solar",
              "distributed_solar_actual", "distributed_wind", "distributed_wind_actual", "generation_total_forecast",
              "generator_maintenance_capacity", "hydro_daily", "local_coal_forecast", "local_hydro_actual",
              "local_hydro_plan", "must_run_stop_units", "nuclear_total", "tie_line_dayahead", "tie_line_schedule")
# The FineReport reports (routes moved 2026-10-23): their originals are in report_engine, 033's older ones in power_reports.
ENGINE_LANE = ("sitewide_e38351fe661a034a", "sitewide_0353290a57044c3e", "nonmarket_generation_forecast")
# The two weekly reports the verified queue held without a parameter rule: their originals are in advanced_reports.
WEEKLY_LANE = ("sitewide_d86eb498e4cf0eca", "sitewide_f5d806b38bb8a248")
DAILY_QUEUES = "state/crawlee_daily_verified_20261014/request_queues"
DAILY_ORIGINALS = "data/2026/original/power_reports"
DAILY_ENGINE = "data/2026/original/report_engine"
DAILY_ADVANCED = "data/2026/original/advanced_reports"      # the two sitewide engine reports, before their route moved
DAILY_LOAD = "data/2026/raw/system_load"


def _numbers(value) -> int:
    """How many numbers an answer holds: 0 means the report had nothing for that day yet."""
    if isinstance(value, dict):
        return sum(_numbers(item) for item in value.values())
    if isinstance(value, list):
        return sum(_numbers(item) for item in value)
    if isinstance(value, str):
        try:
            float(value)
            return 1
        except ValueError:
            return 0
    return int(isinstance(value, (int, float)) and not isinstance(value, bool))


def _power_original(day_dir: Path, day: str) -> str | None:
    """"saved" or "early_empty" for one day's legacy originals, None if it holds none."""
    from datetime import date, timedelta
    verdict = None
    for version in sorted(day_dir.iterdir()) if day_dir.is_dir() else []:
        try:
            meta, body = json.loads((version / "metadata.json").read_bytes()), json.loads((version / "response.json").read_bytes())
        except (FileNotFoundError, NotADirectoryError):
            continue
        if _numbers(body.get("data") if isinstance(body, dict) else body):
            return "saved"
        # User, via the Liaoning session: "10-31、11-01 这类当天或第二天采到的空结果，当作未采处理".
        captured = str(meta.get("captured_at", ""))[:10]
        early = not captured or date.fromisoformat(captured) < date.fromisoformat(day) + timedelta(days=EARLY_EMPTY_DAYS)
        verdict = verdict if verdict == "saved" else "early_empty" if early else "saved"
    return verdict


def prior_originals(root: Path, name: str) -> list[str]:
    """Days of a report whose only original the legacy adaptation kept: the manifest's prior_outcomes, proved by its file.

    sitewide_f5d806b38bb8a248's 2026-10-20 was asked on 10-21 and saved under state/local_adaptation_20261020/responses;
    the queue started after it and the originals tree does not hold it.
    """
    root = Path(root).resolve()
    if not (root / MANIFEST).is_file():
        return []
    source = read_manifest(root)[0]["sources"].get(name) or {}
    saved = {item.get("raw_sha256") for item in (source.get("prior_outcomes") or {}).values() if item.get("outcome") == "saved"}
    days = []
    for proof in source.get("proofs") or []:
        path = root / str(proof.get("response_file") or "")
        if proof.get("sha256") in saved and proof.get("rows") and path.is_file() and path.resolve().is_relative_to(root):
            if hashlib.sha256(path.read_bytes()).hexdigest() == proof["sha256"]:
                days += [v for v in (proof.get("params") or {}).values() if isinstance(v, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", v)]
    return sorted(set(days))


def daily_lane(root: Path, specs: dict, account_alias: str = "liaoning-production", names=DAILY_LANE,
               *, early_empty_consumed: bool = False) -> dict:
    """What the legacy worker completed of the daily reports, as tombstones in PMOS's own identities. Read-only.

    A day is complete with an original holding data, or a later empty answer. A day whose only answer came
    before it was published stays open, and so does one the queue never handled. A day the queue handled
    without any original is a legacy failure: never re-sent, and listed.

    ``early_empty_consumed``: those early answers count too, recorded as empty (user 2026-11-01, of Liaoning's
    15 such days: "空结果不重采"). PMOS's release-early does not reach them: it releases PMOS's own attempts.
    """
    root = Path(root).resolve()
    rows, report, states = [], {}, {}
    for name in names:
        spec = specs[name]
        days = {}
        queue = root / DAILY_QUEUES / ("liaoning-2026-daily-verified-20261014-" + name.replace("_", "-"))
        for path in sorted(queue.glob("*.json")) if queue.is_dir() else []:
            if path.name != "__metadata__.json":
                record = json.loads(path.read_bytes())
                days[record["user_data"]["date"]] = "handled" if record.get("handled_at") else "open"
        known = set(days)
        originals = root / DAILY_ORIGINALS / name
        known |= {p.name for p in originals.iterdir()} if originals.is_dir() else set()
        engine = root / DAILY_ENGINE / name
        known |= {p.name for p in engine.iterdir()} if engine.is_dir() else set()
        advanced = root / DAILY_ADVANCED / name
        known |= {p.name for p in advanced.iterdir()} if advanced.is_dir() else set()
        load = root / DAILY_LOAD
        if name == "system_load":
            known |= {p.stem for p in load.glob("*.json")}
        prior = prior_originals(root, name)
        known |= set(prior)
        consumed, early, opened, failed = [], [], [], []
        for day in sorted(known):
            verdict = _power_original(originals / day, day)
            if verdict != "saved" and engine.is_dir():
                for version in sorted((engine / day).glob("*/metadata.json")):
                    if json.loads(version.read_bytes()).get("nonnull_points"):
                        verdict = "saved"
            if verdict != "saved" and advanced.is_dir():
                for version in sorted((advanced / day).glob("*/*/metadata.json")):
                    if json.loads(version.read_bytes()).get("row_count"):
                        verdict = "saved"
            if name == "system_load" and (load / f"{day}.json").is_file():
                record = json.loads((load / f"{day}.json").read_bytes()).get("record") or {}
                verdict = "saved" if _numbers(record.get("columns")) else "early_empty"
            if verdict == "saved" or day in prior:
                consumed.append(day)
            elif verdict == "early_empty":
                early.append(day)
            elif days.get(day) == "handled":
                failed.append(day)
                consumed.append(day)
            else:
                opened.append(day)
        if early_empty_consumed:
            states.update({f"daily:{name}:{day}": "empty" for day in early})
            consumed = sorted(consumed + early)
        rows += [(f"daily:{name}:{day}", task_identity("liaoning", account_alias, spec, day, "")) for day in consumed]
        # No original and no daily queue entry. It may still have been asked and interrupted: the engine queues'
        # isolated_active_reviews say so of 09-10 and 09-13, which this report does not read.
        never = [day for day in spec.periods_to_collect() if day not in known]
        report[name] = {"consumed": len(consumed), "early_empty": early, "open": opened, "handled_without_original": failed,
                        "no_original": never, **({"original_in_adaptation": prior} if prior else {})}
    return {"rows": rows, "states": states, "per_dataset": report, "datasets": list(names),
            "early_empty_consumed": early_empty_consumed,
            "will_be_asked": sum(len(item["open"]) + len(item["no_original"]) + (0 if early_empty_consumed else len(item["early_empty"]))
                                 for item in report.values()),
            "snapshot_sha256": digest([[key, identity.key, states.get(key, "saved")] for key, identity in rows])}


ATTACHMENT_QUEUE = "state/attachment_coverage_20261029/queue.json"
ATTACHMENT_OPERATIONS = "state/attachment_coverage_20261029/operations"
NATIVE_LISTS = "data/2026/original/native_lists"
MEMBER_LANE = ("sitewide_8090c94f9cf7b256", "sitewide_b03896324973acd7", "sitewide_f1051ebc83754fde")


def list_lane(root: Path, specs: dict, account_alias: str = "liaoning-production", *, only: list[str] | None = None,
              resend_empty: bool = False) -> dict:
    """The disclosure columns (``pub_*``) and the three member lists: what the legacy attachment queue holds, as tombstones in PMOS's own identities. Read-only.

    A saved file is never asked again. A file the site answered with nothing (HTTP 200, 0 bytes; the legacy review
    kept it "no retry") is recorded as empty and not asked either, unless ``resend_empty``: tombstones cannot be taken
    back, so that is decided before the import. A file sent and never settled in any other way is recorded as unknown.
    A file the legacy queue held for review (its title names another year) was never asked and PMOS's ``business_year``
    leaves it out too; they are listed. The legacy list pages are not imported: a listing is of its own day, and a first
    PMOS listing reads to the real end. ``only``: join just these columns now (a pilot); the tombstones cover every
    column either way.
    """
    root = Path(root).resolve()
    names = sorted(name for name, spec in specs.items() if name.startswith("pub_") and spec.documents)
    by_legacy = {specs[name].source["legacy_dataset"]: name for name in names}
    queue = json.loads(_declared(root, ATTACHMENT_QUEUE).read_bytes())
    if queue.get("active") or queue.get("global_hold") or queue.get("source_holds"):
        raise ValueError("legacy_attachment_queue_not_settled")
    rows, states, report = [], {}, {name: Counter() for name in names}
    answered, withheld, left = [], [], Counter()

    def identity(job):
        name = by_legacy[job["dataset"]]
        return name, document_identity("liaoning", account_alias, specs[name], {"attachment": {"id": job["attachment_id"]}})

    for key, job in sorted(queue["outcomes"].items()):
        if job.get("outcome") != "saved":
            raise ValueError("legacy_attachment_outcome_unknown:" + key)
        name, value = identity(job)
        rows.append((key, value))
        report[name]["saved"] += 1
    for key, job in sorted(queue["pending"].items()):
        name, value = identity(job)
        listed = {"dataset": name, "filename": job["filename"], "file_id": job["attachment_id"]}
        if key in queue.get("scope_reviews", {}):
            withheld.append({**listed, "title_years": queue["scope_reviews"][key].get("title_years")})
            report[name]["withheld"] += 1
        elif (root / ATTACHMENT_OPERATIONS / key / "issued.json").is_file():
            review = queue.get("isolated_job_reviews", {}).get(key, {})
            state = "empty" if str(review.get("reason", "")).startswith("settled_empty") else "unknown"
            answered.append({**listed, "state": state})
            report[name][state] += 1
            if resend_empty and state == "empty":
                left[name] += 1
            else:
                rows.append((key, value))
                states[key] = state
        else:
            left[name] += 1
            report[name]["queued"] += 1
    pages = {}
    names += [name for name in MEMBER_LANE if name in specs]
    for name in names:
        newest = sorted((root / NATIVE_LISTS / specs[name].source["legacy_dataset"]).glob("*/*/*/metadata.json"))
        total = json.loads(newest[-1].read_bytes()).get("total") if newest else None
        size = specs[name].request["pagination"]["page_size"]
        pages[name] = max(1, -(-int(total or 0) // size))
    datasets = names if only is None else [name for name in names if name in set(only)]
    if only is not None and len(datasets) != len(set(only)):
        raise ValueError("list_lane_only_unknown:" + ",".join(sorted(set(only) - set(names))))
    asked_pages, asked_files = sum(pages[name] for name in datasets), sum(left[name] for name in datasets)
    return {"rows": rows, "states": states, "per_dataset": {name: dict(tally) for name, tally in report.items() if tally},
            "datasets": datasets, "columns": len(names), "answered_without_a_file": answered, "resend_empty": resend_empty,
            "withheld_year_conflict": withheld, "first_listing_pages": asked_pages, "files_left_to_download": asked_files,
            "first_listing_pages_all_columns": sum(pages.values()), "files_left_to_download_all_columns": sum(left.values()),
            "one_snapshot": True, "will_be_asked": asked_pages + asked_files,
            "snapshot_sha256": digest([[key, value.key, states.get(key, "saved")] for key, value in rows])}


def apply_daily_lane(ledger, lane: dict, approval: str, *, source_id: str = "liaoning-daily-lane") -> dict:
    """Offline, on the handoff's ledger: the tombstones, then these reports first in the collection order."""
    handoff = ledger.handoff()
    if handoff is None:
        raise ValueError("daily_lane_requires_a_handoff")
    if lane.get("one_snapshot"):
        # A pilot and the full lane import one snapshot. Another one means other flags (--resend-empty): tombstones are never taken back.
        earlier = {row[0] for row in ledger.db.execute("SELECT snapshot_sha256 FROM late_legacy_imports WHERE source_id=?", (source_id,))}
        if earlier and lane["snapshot_sha256"] not in earlier:
            raise ValueError("lane_already_imported_with_another_snapshot")
    try:
        imported = ledger.import_late_legacy(source_id, lane["snapshot_sha256"], lane["rows"], approval,
                                             states=lane["states"])
    except ValueError as exc:
        if str(exc) != "late_import_already_done":
            raise
        imported = {"source_id": source_id, "added": 0, "already_done": True}
    names = lane["datasets"]
    order = names + [name for name in handoff["scope"] if name not in names]
    seeded = handoff["seeded"] + [name for name in names if name not in handoff["seeded"]]
    return {"imported": imported, **ledger.extend_scope(order, seeded, approval)}


TASK_KEY_PREFIX = "liaoning:2026:expanded:"
MANY_OPTIONS = 100


def task_key_identities(rows, built: dict, account_alias: str) -> dict:
    """legacy_key -> identity of records whose key alone names them (read-only, exact).

    LNCrawler's ``expanded_adapters.task_key`` is ``liaoning:2026:expanded:<dataset>:`` plus the
    sha256 of the semantic identity ``{"period", "selection"}`` (json, ensure_ascii=False,
    sort_keys, compact separators; Liaoning session 11-01). For a record that kept neither its
    query nor an issued job (e6a2's 10-16 prior outcome), every 2026 period of its kind and every
    selection of its spec is hashed; exactly one preimage names it, else it stays unresolved.
    """
    from datetime import date, timedelta
    context = QueueContext("liaoning", account_alias)
    found = {}
    for _, row in rows:
        if row.identity is not None or not row.legacy_key.startswith(TASK_KEY_PREFIX + row.dataset + ":"):
            continue
        spec, rule = built["specs"].get(row.dataset), built["rules"].get(row.dataset) or {}
        if spec is None or not rule.get("period_field"):
            continue
        digest_hex = row.legacy_key.rsplit(":", 1)[1]
        field = rule.get("selection_field")
        selections = [scope[field] for scope in spec.get("scopes", [])] if field else [""]
        kind = spec["period"]["kind"]
        periods = ([(date(2026, 1, 1) + timedelta(days=n)).isoformat() for n in range(365)] if kind == "day"
                   else [f"2026-{m:02d}" for m in range(1, 13)])
        matches = [(period, selection) for period in periods for selection in selections
                   if hashlib.sha256(json.dumps({"period": period, "selection": selection}, ensure_ascii=False,
                                                sort_keys=True, separators=(",", ":")).encode()).hexdigest() == digest_hex]
        if len(matches) != 1:
            continue
        period, selection = matches[0]
        job = {"key": row.legacy_key, "dataset": row.dataset, "period": period, "page": 1,
               "params": {rule["period_field"]: period, **({field: selection} if field else {})}}
        decoded = decode_liaoning({"sources": {row.dataset: {"pending": [job], "outcomes": {}}}}, context,
                                  period_kinds={row.dataset: kind}, source_contracts=built["rules"])
        if len(decoded) == 1 and decoded[0].identity is not None:
            found[row.legacy_key] = decoded[0].identity
    return found


def bridged_identities(root: Path, account_alias: str) -> dict:
    """legacy_key -> identity recovered by the phase 5 exact identity bridges.

    The bridges rebuild the identity commitment of consumed legacy records
    whose queue entry no longer carries its query (5 empty, 6 saved). They are
    re-keyed to the cutover account alias; nothing else changes.
    """
    from dataclasses import replace
    from ..recovery.liaoning_bridges import resolve_queue_records
    return {task.legacy_key: replace(task.identity, account_alias=account_alias)
            for task in resolve_queue_records(root) if task.identity is not None}


def parity(root: Path, *, account_alias: str = "liaoning-production", bridges: bool = True) -> dict:
    """Engine identity == legacy identity for every legacy record of a generated spec."""
    built = build_specs(root)
    specs = {name: DatasetSpec(**value) for name, value in built["specs"].items()}
    rows, refs = legacy_rows(root, account_alias, built)
    issued, issued_ref = issued_identities(root, account_alias, built)
    refs = refs + [issued_ref]
    bridged, bridge_status = {}, "not_requested"
    if bridges:
        try:
            bridged, bridge_status = bridged_identities(root, account_alias), "applied"
        except ValueError as exc:
            # The bridges pin the legacy writer code and inputs; drift makes
            # them refuse, and those records then stay unresolved here too.
            bridge_status = f"refused:{exc}"
    # The worker's own issued jobs first; a phase 5 bridge only where none was written; the
    # task key's own preimage for what neither resolves.
    bridged = {**bridged, **issued}
    bridged = {**task_key_identities([(ref, r) for ref, r in rows if r.legacy_key not in bridged], built, account_alias),
               **bridged}
    per = {name: Counter() for name in specs}
    out_of_scope = Counter()
    examples = {}
    early = early_empty_keys(root)
    for source_ref, row in rows:
        row = _early_empty(row, early)
        spec = specs.get(row.dataset)
        if spec is None:
            out_of_scope["consumed" if row.consumed else "unconsumed"] += 1
            continue
        tally = per[row.dataset]
        tally["records"] += 1
        tally["consumed"] += int(row.consumed)
        if row.identity is None and row.legacy_key in bridged:
            from dataclasses import replace
            row = replace(row, identity=bridged[row.legacy_key])
            tally["bridged"] += 1
        if row.identity is None:
            tally["unmapped_consumed" if row.consumed else "unmapped"] += 1
            examples.setdefault(row.dataset, ("unmapped", source_ref, list(row.blockers)))
            continue
        legacy = row.identity
        scope = scope_key(legacy.dimensions) if spec.scopes else ("" if not legacy.dimensions else None)
        if scope is None or (spec.scopes and scope not in {key for key, _ in spec.scope_items()}):
            tally["scope_not_in_spec_consumed" if row.consumed else "scope_not_in_spec"] += 1
            examples.setdefault(row.dataset, ("scope_not_in_spec", source_ref, sorted(legacy.dimensions)))
            continue
        try:
            mine = task_identity("liaoning", account_alias, spec, legacy.period["label"], scope, page=legacy.page)
        except Fault as fault:
            tally["engine_identity_error"] += 1
            examples.setdefault(row.dataset, ("engine_identity_error", source_ref, fault.code))
            continue
        if mine.key == legacy.key:
            tally["matched"] += 1
        else:
            tally["mismatched_consumed" if row.consumed else "mismatched"] += 1
            examples.setdefault(row.dataset, ("mismatched", source_ref, _difference(mine.document(), legacy.document())))
    eligible = sorted(name for name, tally in per.items()
                      if tally["records"] and tally["matched"] == tally["records"])
    report = {
        "province": "liaoning", "account_alias": account_alias, "dispatch_allowed": False,
        "identity_bridges": bridge_status,
        "inputs": refs, "manifest_sha256": built["manifest_sha256"], "code_refs": built["code_refs"],
        "generated_specs": len(specs), "deferred_sources": len(built["deferred"]),
        "deferred_reasons": dict(Counter(reason.split(":")[0] for reason in built["deferred"].values())),
        "legacy_records_out_of_scope": dict(out_of_scope),
        "eligible": eligible,
        "not_eligible": {name: {k: v for k, v in per[name].items()} for name in sorted(per) if name not in eligible},
        "per_source": {name: dict(tally) for name, tally in sorted(per.items())},
        "first_problem": {name: list(example) for name, example in sorted(examples.items())},
    }
    report["summary"] = {
        "generated_specs": len(specs), "eligible_sources": len(eligible),
        "records": sum(t["records"] for t in per.values()), "matched": sum(t["matched"] for t in per.values()),
        "bridged": sum(t["bridged"] for t in per.values()),
        "consumed": sum(t["consumed"] for t in per.values()),
        "unresolved_consumed": sum(t[k] for t in per.values() for k in
                                   ("unmapped_consumed", "mismatched_consumed", "scope_not_in_spec_consumed")),
    }
    return report


def _difference(mine: dict, legacy: dict) -> list[str]:
    return sorted(key for key in set(mine) | set(legacy) if mine.get(key) != legacy.get(key))


def write_specs(built: dict, directory: Path, names: list[str] | None = None) -> list[str]:
    """Write generated specs into a PMOS datasets directory (never a legacy directory)."""
    directory = Path(directory).resolve()
    if directory.name != "datasets" or directory.parent.name != "liaoning" or directory.parent.parent.name != "provinces":
        raise ValueError("pmos_liaoning_datasets_directory_required")
    written = []
    for name in names or sorted(built["specs"]):
        value = built["specs"][name]
        spec = DatasetSpec(**value)
        spec.note("generated_from_legacy_manifest", manifest_sha256=built["manifest_sha256"])
        spec.save(directory / f"{name}.json")
        written.append(name)
    return written


def digest(value) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


# -- cutover --------------------------------------------------------------
GLOBAL_BLOCKERS = {"legacy_active_owner_unsettled", "orphan_active_preserved", "legacy_stage_requires_settlement"}
SOURCE_BLOCKERS = {"legacy_source_hold", "conflicting_legacy_outcomes"}


def plan_cutover(root: Path, *, account_alias: str = "liaoning-production", requested: list[str] | None = None,
                 bridges: bool = True) -> dict:
    """Which sources may move to PMOS from this (frozen) legacy root, and why not the others.

    Eligible: every legacy record maps to an engine-identical identity and the
    legacy owner holds nothing about the source. A legacy task still in
    flight, or a global legacy hold, blocks the whole cutover: freeze at a
    settled boundary first. Nothing is written.
    """
    report = parity(root, account_alias=account_alias, bridges=bridges)
    built = build_specs(root)
    rows, _ = legacy_rows(root, account_alias, built)
    blockers, source_blocked = Counter(), {}
    for _, row in rows:
        for blocker in row.blockers:
            if blocker in GLOBAL_BLOCKERS or blocker == "legacy_global_hold":
                blockers[blocker] += 1
            if blocker in SOURCE_BLOCKERS and row.dataset in report["per_source"]:
                source_blocked.setdefault(row.dataset, set()).add(blocker)
    eligible = [name for name in report["eligible"] if name not in source_blocked]
    # The scope order is the collection order: a source of hundreds of options a day (the node prices,
    # ~1,324 a day) goes last, so every other report's new days come first.
    many = {name for name, spec in built["specs"].items() if len(spec.get("scopes", [])) >= MANY_OPTIONS}
    eligible = sorted(eligible, key=lambda name: (name in many, name))
    if requested is not None:
        unknown = sorted(set(requested) - set(eligible))
        if unknown:
            raise ValueError("requested_sources_not_eligible:" + ",".join(unknown))
        eligible = [name for name in eligible if name in requested]
    plan = {
        "province": "liaoning", "account_alias": account_alias, "inputs": report["inputs"],
        "manifest_sha256": report["manifest_sha256"], "identity_bridges": report["identity_bridges"],
        "global_blockers": dict(blockers), "scope": eligible,
        "excluded": {**{name: "identity_parity_incomplete" for name in report["not_eligible"]},
                     **{name: sorted(reasons) for name, reasons in source_blocked.items()}},
        "deferred_sources": report["deferred_sources"], "deferred_reasons": report["deferred_reasons"],
        "per_source": {name: report["per_source"][name] for name in eligible},
        "ready": bool(eligible) and not blockers,
    }
    plan["plan_sha256"] = digest({k: v for k, v in plan.items()})
    return plan


def apply_cutover(root: Path, instance: Path, plan: dict, *, legacy_owner: dict, code_sha: str, approval: str,
                  request_budget: int, keepalive_dataset: str | None = None) -> dict:
    """Build a NEW PMOS ledger from the frozen legacy queue and grant the handoff.

    Every decoded legacy record of every declared queue file is imported as a
    non-dispatchable fact (all datasets, so later scope extensions stay
    protected); the handoff receipt then lets the engine send only the planned
    scope, at most ``request_budget`` site requests. Sends nothing.
    """
    from ..ledger import Ledger
    from ..queue_compat import import_shadow
    if not plan.get("ready"):
        raise ValueError("cutover_plan_not_ready")
    # Asked for the bridges as the plan did: a refused request is still a request (Liaoning 2026-11-01
    # 14:4x: "refused:…" recomputed as "not_requested" changed only that field and refused the apply).
    fresh = plan_cutover(root, account_alias=plan["account_alias"], requested=plan["scope"],
                         bridges=plan["identity_bridges"] != "not_requested")
    if fresh["plan_sha256"] != plan["plan_sha256"]:
        raise ValueError("legacy_root_changed_since_plan")
    instance = Path(instance)
    if instance.exists():
        raise ValueError("new_instance_directory_required")
    built = build_specs(root)
    rows, refs = legacy_rows(root, plan["account_alias"], built)
    bridged = bridged_identities(root, plan["account_alias"]) if plan["identity_bridges"] == "applied" else {}
    bridged = {**bridged, **issued_identities(root, plan["account_alias"], built)[0]}
    bridged = {**task_key_identities([(ref, r) for ref, r in rows if r.legacy_key not in bridged], built,
                                     plan["account_alias"]), **bridged}
    from dataclasses import replace
    early = early_empty_keys(root)
    by_source: dict[str, list] = {}
    for source_ref, row in rows:
        if row.identity is None and row.legacy_key in bridged:
            row = replace(row, identity=bridged[row.legacy_key])
        by_source.setdefault(source_ref, []).append(_early_empty(row, early))
    digests = {ref["path"]: ref["sha256"] for ref in refs if "sha256" in ref}
    digests.setdefault(DIMENSION_OPERATIONS, next((ref["sha256"] for ref in refs
                                                   if ref["path"] == DIMENSION_OPERATIONS + "/*/issued.json"), None))
    instance.mkdir(parents=True, mode=0o700)
    ledger = Ledger(instance / "ledger.sqlite3")
    snapshots = []
    try:
        context = QueueContext("liaoning", plan["account_alias"])
        for source_ref, source_rows in sorted(by_source.items()):
            snapshot = digests.get(source_ref) or digest([row.summary() for row in source_rows])
            import_shadow(ledger, source_rows, context, source_id=source_ref, snapshot_sha256=snapshot)
            snapshots.append({"source_id": source_ref, "snapshot_sha256": snapshot})
        receipt = ledger.grant_handoff({
            "version": 1, "province": "liaoning", "account_alias": plan["account_alias"],
            "legacy_owner": legacy_owner, "snapshots": snapshots, "scope": plan["scope"],
            "request_budget": request_budget, "code_sha": code_sha, "seeding": "spec_periods",
            # Liaoning production renews with the mounted 022 report after 300 s idle (system_load).
            "session": {"keepalive_dataset": keepalive_dataset} if keepalive_dataset else {},
            "approval": approval + f" (plan {plan['plan_sha256']})"})
        summary = ledger.legacy_summary()
    finally:
        ledger.close()
    return {"receipt": receipt, "ledger": summary}
