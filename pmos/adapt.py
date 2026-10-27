"""The standard adaptation workflow (docs/ADAPTING.md).

    1 scaffold   pmos new-province <id> ...     province.json + datasets/
    2 capture    pmos <id> capture              record the page's own API calls
                                                while a human opens a report
    3 candidate  pmos <id> candidate <n> ...    turn captured call <n> into a spec
    4 sample     pmos <id> sample <ds> <period> one real request, kept in samples/
                                                (twice, on two different periods)
    5 enable     pmos <id> enable <ds>          verified -> enabled, tasks seeded

Status changes are written to the spec file with a history entry, so the
repository shows who verified what and from which sample.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import periods
from .faults import Fault, decide
from .province import PROVINCES, Province
from .spec import DatasetSpec, SpecError

DATE_PATTERNS = (("day", re.compile(r"\d{4}-\d{2}-\d{2}"), "{date}"),
                 ("month", re.compile(r"\d{4}-\d{2}"), "{month}"),
                 ("day", re.compile(r"\d{8}"), "{date_compact}"))


def scaffold(province_id: str, name: str, origin: str, root: Path = PROVINCES) -> Path:
    directory = root / province_id
    if (directory / "province.json").exists():
        raise SpecError(f"province {province_id} already exists")
    (directory / "datasets").mkdir(parents=True)
    profile = {
        "id": province_id, "name": name, "origin": origin,
        "browser": {"profile_dir": f"~/pmos-browser/{province_id}", "page_url_contains": None},
        "policy": {"response_interval_seconds": 60, "max_per_hour": None, "max_per_day": None,
                   "request_timeout_seconds": 45, "max_consecutive_empty": 2,
                   "renew_after_idle_seconds": 600, "probe_after_idle_seconds": 300},
        "session": {"keepalive_dataset": None, "probe": None, "collection_order": []},
        "notices": {},
    }
    (directory / "province.json").write_text(json.dumps(profile, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    Province.load(province_id, root)
    return directory


def read_capture(path: Path, index: int) -> dict:
    for line in path.read_text(encoding="utf-8").splitlines():
        entry = json.loads(line)
        if entry.get("index") == index:
            return entry
    raise SpecError(f"capture #{index} not found in {path}")


def templatize(value, date_fields: set[str] | None, kind: str):
    """Replace the captured period value with placeholders, field by field."""
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if isinstance(item, str) and (date_fields is None or key in date_fields):
                out[key] = _placeholder(item, kind) or item
            else:
                out[key] = templatize(item, date_fields, kind)
        return out
    if isinstance(value, list):
        return [templatize(item, date_fields, kind) for item in value]
    return value


def _placeholder(text: str, kind: str) -> str | None:
    for pattern_kind, pattern, placeholder in DATE_PATTERNS:
        if pattern.fullmatch(text):
            if kind == "month" and pattern_kind == "day":
                return None
            return placeholder
    return None


def candidate_from_capture(province: Province, entry: dict, dataset_id: str, name: str, kind: str,
                           start: str, date_fields: list[str] | None = None, transport: str = "fetch") -> DatasetSpec:
    content = entry.get("content_type", "")
    response_kind = ("json" if "json" in content else "xlsx" if "sheet" in content
                     else "excel" if "excel" in content or "octet" in content else "pdf" if "pdf" in content else "binary")
    request = {"transport": transport, "method": entry["method"], "path": entry["path"]}
    if entry.get("body") is not None:
        request["body"] = templatize(entry["body"], set(date_fields) if date_fields else None, kind)
    if entry.get("query"):
        request["query"] = templatize(entry["query"], set(date_fields) if date_fields else None, kind)
    spec = DatasetSpec(id=dataset_id, name=name, status="candidate",
                       period={"kind": kind, "start": start, "end": "today"},
                       request=request, response={"kind": response_kind},
                       source={"capture_index": entry["index"], "frame_url": entry.get("frame_url")})
    spec.note("candidate_from_capture", capture_index=entry["index"])
    spec.check()
    target = province.datasets_dir / f"{dataset_id}.json"
    if target.exists():
        raise SpecError(f"{dataset_id} already exists")
    spec.save(target)
    return spec


async def sample(engine, spec: DatasetSpec, period: str, scope: str | None = None) -> dict:
    """One real request for adaptation; updates the spec lifecycle.

    A scoped dataset samples its first scope unless ``scope`` names another key.
    """
    kind = spec.period["kind"]
    periods.validate("day" if kind == "snapshot" else kind, period)
    if spec.status in ("denied", "retired"):
        raise SpecError(f"{spec.id} is {spec.status}; it is never sampled again automatically")
    if spec.documents is not None and scope == "file":
        return await _sample_document(engine, spec, period)
    key, values = (scope, spec.scope_values(scope)) if scope else spec.scope_items()[0]
    # A repeated proven sample is a local read, never another site request.
    previous = [r for r in engine.samples.records(spec.id) if r["period"] == period and r.get("scope", "") == key]
    if previous and any(h.get("event") == "sample_ok" and h.get("period") == period for h in spec.history):
        record = previous[-1]
        if not engine.samples.verify(record):
            raise Fault("sample_original_hash_mismatch", "local")
        return {"dataset": spec.id, "period": period, "result": "saved", "status": spec.status,
                "sha256": record["sha256"], "reused": True}
    request = spec.build(period, values)
    if "pagination" in spec.request:
        from .paging import apply, first_page
        request = apply(request, spec.request["pagination"], first_page(spec.request["pagination"]))
    try:
        result = await engine.request("sample", request, spec.response,
                                      dataset=spec.id, period=period, scope=key,
                                      identity=engine.task_identity(spec, period, key,
                                          page=first_page(spec.request["pagination"]) if "pagination" in spec.request else None))
    except Fault as fault:
        decision = decide(fault, "sample")
        if decision.scope == "dataset":
            spec.status = decision.dataset_status
            spec.note("sample_failed", period=period, reason=fault.code, business_status=fault.business_status)
            spec.save()
        raise
    record = None
    if result.outcome.result == "saved":
        record = engine.samples.store(spec.id, period, result.response.body, result.outcome.extension,
                                      scope=key, acquisition="adaptation_sample",
                                      **({"scope_values": values} if values else {}))
    sampled = {h["period"] for h in spec.history if h.get("event") == "sample_ok"}
    if spec.documents is not None:
        # A listing proves the list call and the record shape; ``verified``
        # additionally needs one downloaded attachment (sample --scope file).
        items = _listed_items(spec, result.response.body)
        spec.note("sample_ok", period=period, result=result.outcome.result, kind="listing",
                  sha256=record["sha256"] if record else None, files=len(items))
        if spec.status == "candidate" and items:
            spec.status = "sampled"
        spec.save()
        return {"dataset": spec.id, "period": period, "result": result.outcome.result, "files": len(items),
                "status": spec.status, "sha256": record["sha256"] if record else None}
    spec.note("sample_ok", period=period, result=result.outcome.result,
              sha256=record["sha256"] if record else None, **({"scope": values} if values else {}))
    if result.outcome.result == "saved":
        if spec.status == "candidate":
            spec.status = "sampled"
        elif spec.status == "sampled" and sampled - {period}:
            spec.status = "verified"
    spec.save()
    return {"dataset": spec.id, "period": period, "result": result.outcome.result,
            "status": spec.status, "sha256": record["sha256"] if record else None}


def _listed_items(spec: DatasetSpec, body: bytes) -> list[dict]:
    from .documents import DocumentError, extract
    from .paging import get_path
    try:
        return extract(get_path(json.loads(body), spec.request["pagination"]["records"]) or [], spec.documents)
    except (DocumentError, ValueError) as exc:
        raise Fault(str(exc), "site_data") from None


async def _sample_document(engine, spec: DatasetSpec, period: str) -> dict:
    """Download the newest attachment of the latest listing sample."""
    listings = [r for r in engine.samples.records(spec.id) if not r.get("scope", "").startswith("f")]
    items = []
    for record in reversed(listings):
        items = _listed_items(spec, (engine.samples.root / record["path"]).read_bytes())
        if items:
            break
    if not items:
        raise SpecError(f"{spec.id}: sample a listing with at least one attachment first")
    item = items[0]
    response_spec = spec.documents.get("download_response", {"kind": "document"})
    try:
        result = await engine.request("sample", spec.build_download(item), response_spec,
                                      dataset=spec.id, period=item["period"], scope=item["scope"],
                                      identity=engine.document_identity(spec, item))
    except Fault as fault:
        decision = decide(fault, "sample")
        if decision.scope == "dataset":
            spec.status = decision.dataset_status
            spec.note("sample_failed", kind="file", reason=fault.code, business_status=fault.business_status)
            spec.save()
        raise
    records = []
    if result.outcome.result == "saved":
        from .engine import store_document
        records = store_document(engine.samples, spec, item, result, acquisition="adaptation_sample")
        if spec.status == "sampled":
            spec.status = "verified"
    formats = [r["path"].rsplit(".", 1)[-1] for r in records]
    spec.note("sample_ok", period=item["period"], result=result.outcome.result, kind="file",
              sha256=records[0]["sha256"] if records else None, formats=formats)
    spec.save()
    return {"dataset": spec.id, "period": item["period"], "result": result.outcome.result,
            "status": spec.status, "sha256": records[0]["sha256"] if records else None,
            "format": "." + formats[0] if formats else None, "files": len(records)}


def enable(spec: DatasetSpec, *, force_reason: str | None = None) -> DatasetSpec:
    if spec.status != "verified" and not force_reason:
        raise SpecError(f"{spec.id} is {spec.status}; only verified datasets can be enabled "
                        "(pass --force-reason to record an explicit human decision)")
    spec.status = "enabled"
    spec.note("enabled", **({"force_reason": force_reason} if force_reason else {}))
    spec.save()
    return spec
