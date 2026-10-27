"""Failure taxonomy and the single place that decides what a failure stops.

Every failure carries a fixed ``code`` and a ``category``:

==========  ==========================================  ======================
category    meaning                                     effect
==========  ==========================================  ======================
auth        login lost (401, logout notice)             block province, no keepalive
permission  site refused this endpoint (40302, 403)     sample: dataset denied;
                                                        collect: block province
rate_limit  429 / "访问频繁"                            block province, no keepalive
network     no or incomplete response                   block province, no keepalive
unknown     unexpected business code / HTML page        block province, no keepalive
site_data   HTTP 200 + business success, but the        quarantine dataset,
            payload failed local validation             others continue
local       our bug, config or disk problem             block province, keepalive
==========  ==========================================  ======================

"keepalive" means the session keeper may keep renewing the login while a
human reviews; it is only allowed when the login itself is not in question.
"""
from __future__ import annotations

from dataclasses import dataclass

CATEGORIES = ("auth", "permission", "rate_limit", "network", "unknown", "site_data", "local")


class Fault(Exception):
    def __init__(self, code: str, category: str, *, http_status: int | None = None,
                 business_status: str | None = None):
        if category not in CATEGORIES:
            raise ValueError("fault_category_invalid")
        super().__init__(code)
        self.code = code
        self.category = category
        self.http_status = http_status
        self.business_status = business_status


@dataclass(frozen=True)
class Decision:
    scope: str            # "dataset" or "province"
    keepalive: bool       # may the session keeper keep the login alive meanwhile
    dataset_status: str | None = None   # new spec status for dataset-scoped faults


def decide(fault: Fault, phase: str, *, permission_scope: str = "province") -> Decision:
    """``phase`` is "collect" (enabled datasets) or "sample" (adaptation)."""
    if fault.category == "site_data":
        return Decision("dataset", True, "quarantined")
    if fault.category == "permission" and (phase == "sample" or (
            permission_scope == "dataset" and fault.http_status == 200 and fault.business_status == "40302")):
        return Decision("dataset", True, "denied")
    if fault.category == "local" and fault.code in {
            "request_template_missing", "request_template_stale", "report_frame_missing", "spacing_not_elapsed"}:
        return Decision("province", True)
    return Decision("province", False)
