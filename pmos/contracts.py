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


def canonical(value) -> bytes:
    def encode_decimal(item):
        if isinstance(item, Decimal) and item.is_finite():
            return str(item)
        raise TypeError("unsupported_canonical_type")
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False, default=encode_decimal).encode("utf-8")


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
        value = {"version": 1, **self.__dict__}
        if any(not isinstance(value[k], str) or not value[k] for k in
               ("province", "account_alias", "dataset", "operation")):
            raise ValueError("identity_fields_required")
        if not isinstance(self.period, dict) or self.period.get("kind") not in {
                "day", "week", "month", "year", "snapshot", "range"}:
            raise ValueError("explicit_period_kind_required")
        if self.page is not None and (type(self.page) is not int or self.page < 1):
            raise ValueError("page_invalid")
        reject_secrets(value)
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
