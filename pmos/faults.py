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
local       our bug, config or disk problem             block province (keepalive only
                                                        for template waits); proven
                                                        unsent page step: hold dataset
==========  ==========================================  ======================

"keepalive" means the session keeper may keep renewing the login while a
human reviews; it is only allowed when the login itself is not in question.
"""
from __future__ import annotations

from dataclasses import dataclass

CATEGORIES = ("auth", "permission", "rate_limit", "network", "unknown", "site_data", "local")


class Fault(Exception):
    def __init__(self, code: str, category: str, *, http_status: int | None = None,
                 business_status: str | None = None, unsent: bool = False, detail: dict | None = None,
                 body: bytes | None = None):
        if category not in CATEGORIES:
            raise ValueError("fault_category_invalid")
        super().__init__(code)
        self.code = code
        self.category = category
        self.http_status = http_status
        self.business_status = business_status
        # Proven by the transport: no business request of this operation
        # reached the network (the request gate passed none). Consumes nothing.
        self.unsent = unsent
        # Page diagnostics only (a menu label, a depth, a box size): never values, addresses or tokens.
        self.detail = detail or {}
        # What the site answered, for the quarantine only (a report's stage refuses inside the transport): never logged.
        self.body = body


@dataclass(frozen=True)
class Decision:
    scope: str            # "dataset" or "province"
    keepalive: bool       # may the session keeper keep the login alive meanwhile
    dataset_status: str | None = None   # new spec status for dataset-scoped faults


class DriverLost(RuntimeError):
    """The browser driver or its connection is gone (Anhui 2026-10-31: a late CDP reply killed Playwright).

    Every further call would fail at once and settle item after item as
    unknown, so the worker records it and exits for its supervisor to restart
    it (no business block); the request in flight stays unknown, never re-sent.
    """


# Playwright's messages once its driver or the browser connection is gone.
DRIVER_GONE = ("Connection closed", "Target page, context or browser has been closed", "Browser has been closed",
               "browser has disconnected")


def driver_gone(exc: BaseException) -> bool:
    return any(marker in str(exc) for marker in DRIVER_GONE)


def decide(fault: Fault, phase: str, *, permission_scope: str = "province") -> Decision:
    """``phase`` is "collect" (enabled datasets) or "sample" (adaptation)."""
    if fault.category == "site_data":
        return Decision("dataset", True, "quarantined")
    if fault.category == "permission" and (phase == "sample" or (
            permission_scope == "dataset" and fault.http_status == 200 and fault.business_status == "40302")):
        return Decision("dataset", True, "denied")
    if fault.category == "local" and fault.unsent:
        # One dataset's page step failed before anything was sent (a missing
        # module/component, an unmet form check): that dataset waits for review.
        return Decision("dataset", True, "paused")
    if fault.category == "local" and fault.code in {
            "request_template_missing", "request_template_stale", "report_frame_missing", "spacing_not_elapsed"}:
        return Decision("province", True)
    return Decision("province", False)
