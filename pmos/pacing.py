"""Request pacing: one shared spacing after every response, optional caps.

All request kinds (collection, samples, keepalive, TIME probes) wait the same
``response_interval_seconds`` after the latest response *end*. Only
collection and samples are charged to the hourly/daily caps.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, fields

# Validation lower bound, not authorization. Each province keeps its policy.
MIN_RESPONSE_INTERVAL_SECONDS = 1
HOUR = 3600
DAY = 86400


class PolicyError(ValueError):
    pass


@dataclass(frozen=True)
class Policy:
    response_interval_seconds: float = 60
    max_per_hour: int | None = None
    max_per_day: int | None = None
    request_timeout_seconds: float = 45
    max_consecutive_empty: int = 2
    # Session keeping (see session.py): renew after this much business idleness.
    renew_after_idle_seconds: float = 600
    probe_after_idle_seconds: float = 300
    extra: dict = field(default_factory=dict, compare=False)

    @classmethod
    def from_dict(cls, value: dict) -> "Policy":
        known = {f.name for f in fields(cls)} - {"extra"}
        policy = cls(**{k: v for k, v in value.items() if k in known},
                     extra={k: v for k, v in value.items() if k not in known})
        policy.check()
        return policy

    def check(self) -> None:
        def number(v):
            return type(v) in (int, float) and math.isfinite(v)
        if not number(self.response_interval_seconds) or self.response_interval_seconds < MIN_RESPONSE_INTERVAL_SECONDS:
            raise PolicyError(f"response_interval_seconds must be >= {MIN_RESPONSE_INTERVAL_SECONDS}")
        for name in ("max_per_hour", "max_per_day"):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or value < 1):
                raise PolicyError(f"{name} must be null or a positive integer")
        if not number(self.request_timeout_seconds) or not 10 <= self.request_timeout_seconds <= 600:
            raise PolicyError("request_timeout_seconds must be 10..600")
        if type(self.max_consecutive_empty) is not int or self.max_consecutive_empty < 1:
            raise PolicyError("max_consecutive_empty must be a positive integer")
        if not number(self.renew_after_idle_seconds) or not 120 <= self.renew_after_idle_seconds <= 1500:
            # The PMOS sites log out after ~30 idle minutes; renew well before.
            raise PolicyError("renew_after_idle_seconds must be 120..1500")
        if not number(self.probe_after_idle_seconds) or self.probe_after_idle_seconds < 60:
            raise PolicyError("probe_after_idle_seconds must be >= 60")


def wait_seconds(policy: Policy, now: float, last_response_end: float,
                 budget_starts: list[float] | None = None, *, charged: bool = True) -> float:
    """Seconds until the next request may be sent (0 means now)."""
    ready = last_response_end + policy.response_interval_seconds
    if charged and budget_starts:
        for limit, window in ((policy.max_per_hour, HOUR), (policy.max_per_day, DAY)):
            if limit is None:
                continue
            recent = [t for t in budget_starts if now - t < window]
            if len(recent) >= limit:
                ready = max(ready, recent[-limit] + window + 1)
    return max(0.0, ready - now)
