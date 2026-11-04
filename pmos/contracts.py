"""Versioned, credential-free identities and offline adapter contracts.

Source/session/template revisions belong in receipts, never in a business key.
No browser, worker or legacy module is imported here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import re
from decimal import Decimal
from datetime import date, datetime, timezone


def canonical(value) -> bytes:
    def encode_decimal(item):
        if isinstance(item, Decimal) and item.is_finite():
            return str(item)
        raise TypeError("unsupported_canonical_type")
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False, default=encode_decimal).encode("utf-8")


def unit_from_response(raw: bytes, contract: dict) -> str | None:
    """Only an explicitly bound inline unit field is currently resolved.

    A plausible path, boolean or 64-hex string is not source verification.
    External UI/header unit proofs need a future reviewed evidence resolver.
    """
    proof, unit = contract.get("unit_evidence"), contract.get("unit")
    if not isinstance(proof, dict) or not isinstance(unit, str) or not unit:
        return None
    path = proof.get("response_path")
    if (not isinstance(path, list) or not 1 <= len(path) <= 12
            or any(type(key) not in (str, int) for key in path)
            or path[-1] not in {"unit", "单位", "计量单位", "measurementUnit"}
            or proof.get("source_sha256") != hashlib.sha256(raw).hexdigest()):
        return None
    try:
        value = json.loads(raw)
        for key in path:
            if type(key) not in (str, int):
                return None
            value = value[key]
    except (ValueError, KeyError, IndexError, TypeError):
        return None
    return unit if value == unit else None


def reject_secrets(value) -> None:
    """Fail closed on credential-shaped fields; never include their values."""
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise ValueError("string_field_names_required")
            if re.search(r"(?i)password|passwd|cookie|token|ticket|authorization|secret|authcode", key):
                raise ValueError("credential_field_forbidden")
            reject_secrets(child)
    elif isinstance(value, list):
        for child in value:
            reject_secrets(child)


def normalized_period(value: dict) -> dict:
    """Canonical calendar keys; equivalent spellings cannot evade dedupe.

    Snapshot anchors remain capture anchors, never statistical periods. Native
    Monday queries retain one query anchor, without inferred week boundaries.
    Weeks and true ranges require their actual explicit boundaries.
    """
    kind = value["kind"]
    if set(value) - {"kind", "label", "start", "end", "anchor"}:
        raise ValueError("period_fields_unreviewed")
    label = value.get("label", value.get("start", value.get("anchor")))
    if kind == 'native_open_range':
        if set(value) != {'kind', 'start', 'end'} or value['end'] is not None:
            raise ValueError('explicit_native_open_range_required')
        if date.fromisoformat(value['start']).isoformat() != value['start']:
            raise ValueError('canonical_native_start_required')
        return {'kind': kind, 'start': value['start'], 'end': None}
    if kind == 'request_anchor':
        if set(value) != {'kind', 'anchor'} or not isinstance(value['anchor'], str) or not value['anchor']:
            raise ValueError('explicit_request_anchor_required')
        return {'kind': kind, 'anchor': value['anchor']}
    if kind == "week_start_monday":
        if (set(value) - {"kind", "label", "anchor"} or not isinstance(label, str)
                or any(v != label for k, v in value.items() if k != "kind")):
            raise ValueError("explicit_monday_query_anchor_required")
        try:
            observed = date.fromisoformat(label)
            if observed.isoformat() != label or observed.weekday() != 0:
                raise ValueError()
        except ValueError:
            raise ValueError("canonical_monday_anchor_required") from None
        return {"kind": kind, "anchor": label}
    if kind == "snapshot":
        if not isinstance(label, str) or not label or value.get("end", label) != label:
            raise ValueError("explicit_snapshot_anchor_required")
        def anchor(text):
            if re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d)", text):
                return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc).isoformat(timespec="microseconds")
            return text
        label = anchor(label)
        if any(not isinstance(v, str) or anchor(v) != label for k, v in value.items() if k != "kind"):
            raise ValueError("snapshot_anchor_mismatch")
        return {"kind": kind, "anchor": label}
    if kind in {"day", "month", "year"}:
        if not isinstance(label, str) or value.get("start", label) != label or value.get("end", label) != label:
            raise ValueError("calendar_period_mismatch")
        if kind == "day":
            if date.fromisoformat(label).isoformat() != label:
                raise ValueError("canonical_day_required")
        elif kind == "month":
            if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", label):
                raise ValueError("canonical_month_required")
        elif not re.fullmatch(r"\d{4}", label):
            raise ValueError("canonical_year_required")
        return {"kind": kind, "label": label}
    if kind in {"week", "range"}:
        if "start" not in value or "end" not in value or "label" in value:
            raise ValueError("explicit_period_boundaries_required")
        start, end = value["start"], value["end"]
        if any(date.fromisoformat(v).isoformat() != v for v in (start, end)) or start > end:
            raise ValueError("invalid_period_boundaries")
        return {"kind": kind, "start": start, "end": end}
    raise ValueError("explicit_period_kind_required")


@dataclass(frozen=True)
class TaskIdentity:
    province: str
    account_alias: str
    dataset: str
    period: dict
    dimensions: dict = field(default_factory=dict)
    page: int | None = None
    operation: str = "query"

    def document(self) -> dict:
        # New native open ranges/request anchors are explicitly versioned;
        # existing calendar identities retain their original v1 keys.
        version = 2 if self.period.get('kind') in {'native_open_range','request_anchor'} else 1
        value = {"version": version, **self.__dict__}
        if any(not isinstance(value[k], str) or not value[k] for k in
               ("province", "account_alias", "dataset", "operation")):
            raise ValueError("identity_fields_required")
        if not isinstance(self.period, dict) or self.period.get("kind") not in {
                "day", "week", "month", "year", "snapshot", "range", "week_start_monday",
                "native_open_range", "request_anchor"}:
            raise ValueError("explicit_period_kind_required")
        if self.page is not None and (type(self.page) is not int or self.page < 1):
            raise ValueError("page_invalid")
        if not isinstance(self.dimensions, dict):
            raise ValueError("explicit_dimensions_object_required")
        reject_secrets(value)
        value["period"] = normalized_period(self.period)
        def json_types(item):
            if isinstance(item, dict):
                return all(isinstance(key, str) and json_types(child) for key, child in item.items())
            if isinstance(item, list):
                return all(json_types(child) for child in item)
            return type(item) in (str, int, float, bool, type(None))
        if not json_types(value):
            raise ValueError("identity_requires_json_native_types")
        return value

    @property
    def key(self) -> str:
        return hashlib.sha256(canonical(self.document())).hexdigest()

    @property
    def file_key(self) -> str | None:
        if self.operation != "attachment" or not self.dimensions.get("file_id"):
            return None
        if not isinstance(self.dimensions["file_id"], str) or not self.dimensions["file_id"].strip():
            raise ValueError("stable_file_id_string_required")
        return hashlib.sha256(canonical({"province": self.province, "account_alias": self.account_alias,
            "operation": "attachment", "file_id": self.dimensions["file_id"]})).hexdigest()


@dataclass(frozen=True)
class ParsedArtifact:
    rows: list[dict]
    empty: bool
    pagination: dict | None = None
    unit: str | None = None
    semantic_state: str = "source_fields_only"

    def summary(self) -> dict:
        return {"rows": len(self.rows), "empty": self.empty, "pagination": self.pagination,
                "unit": self.unit, "semantic_state": self.semantic_state,
                "full_dataset_coverage_verified": False}
