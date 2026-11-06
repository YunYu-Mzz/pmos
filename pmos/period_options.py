"""Date-specific selector inventories in the existing worker request lane.

Unlisted combinations remain pending for review, never empty or skipped.
"""
from __future__ import annotations

import hashlib
import json

from .faults import Fault
from .spec import scope_key

SCOPE = "period_options"


def enabled(spec) -> bool:
    return bool(spec.source.get("period_options"))


def listed_scopes(found, label: str) -> list[dict]:
    if not isinstance(found, list) or not found:
        raise ValueError("date_options_empty")
    names = set()
    for option in found:
        if (not isinstance(option, dict) or option.get("value_type") != "string"
                or not isinstance(option.get("label"), str) or not option["label"].strip()
                or option.get("value") != option["label"]):
            raise ValueError("date_options_not_text_valued")
        names.add(option["label"])
    return [{label: name} for name in sorted(names)]


async def prepare(engine, spec, period: str) -> float:
    """Inventory once; resume from saved evidence after interruption, never re-send."""
    label = spec.source["period_options"]["label"]
    page = spec.source["ui_request"]
    if spec.period["kind"] != "day" or any(set(s) != {label} for s in spec.scopes):
        raise Fault("date_options_spec_invalid", "local")

    def last_attempt():
        return engine.ledger.db.execute("SELECT * FROM attempts WHERE kind='sample' AND dataset=? AND period=?"
                                        " AND scope=? ORDER BY id DESC LIMIT 1", (spec.id, period, SCOPE)).fetchone()

    attempt, raw, reason = last_attempt(), None, None
    if attempt is not None:
        if attempt["outcome"] in ("success", "empty"):
            records = [r for r in engine.responses.records(spec.id) if r["scope"] == f"attempt_{attempt['id']}"]
            if records and engine.responses.verify(records[-1]):
                raw = (engine.responses.root / records[-1]["path"]).read_bytes()
            else:
                reason = "date_options_evidence_missing"
        else:
            reason = "date_options_previous_attempt_unresolved"
    else:
        fields = [{**f, "value": period} for f in page.get("fields", []) if f.get("kind") == "date"]
        if len(fields) != 1:
            raise Fault("date_options_date_field_invalid", "local")
        request = {"transport": "ui", "action": "options", "frame": page["frame"],
                   "verify_date": True,
                   **({"menu": page["menu"]} if page.get("menu") else {}),
                   "fields": fields, "select": {"kind": "select", "label": label}}
        try:
            result = await engine.request("sample", request, {"kind": "json"}, dataset=spec.id, period=period,
                                          scope=SCOPE, lane=spec.source.get("lane"))
            raw = result.response.body
        except Fault as fault:
            if fault.code == "spacing_not_elapsed":
                return 1
            if fault.category != "local" or fault.code in {
                    "stop_all_requested", "unsettled_attempt", "handoff_request_budget_exhausted",
                    "province_blocked", "dataset_outside_handoff_scope"}:
                raise
            reason = "date_options_" + fault.code
        attempt = last_attempt()
    digest = hashlib.sha256(raw).hexdigest() if raw is not None else None
    scopes = []
    if reason is None:
        try:
            value = json.loads(raw)
            if value.get("observed_date") != period:
                raise ValueError("date_options_date_unverified")
            scopes = listed_scopes(value.get("data"), label)
        except (ValueError, AttributeError, TypeError) as exc:
            reason = (str(exc) if isinstance(exc, ValueError) and str(exc).startswith("date_options_")
                      else "date_options_response_invalid")
    if reason is None:
        known = {s.get(label) for s in spec.scopes}
        new = [s for s in scopes if s[label] not in known]
        if new:
            discovered = engine.state.read("discovered_options", {}) or {}
            discovered[spec.id] = (discovered.get(spec.id) or []) + new
            engine.state.write("discovered_options", discovered)
            engine.province.extra_scopes = discovered
    engine.ledger.record_period_options(spec.id, period, [scope_key(s) for s in scopes], response_sha256=digest,
                                        attempt_id=attempt["id"] if attempt else None, reason=reason)
    engine.state.event("date_options_review" if reason else "date_options_verified", dataset=spec.id,
                        period=period, listed=len(scopes), response_sha256=digest, reason=reason)
    engine._status("date_options_review" if reason else "date_options_ready", last_dataset=spec.id,
                   last_period=period, last_scope="", listed_options=len(scopes))
    return 0.2
