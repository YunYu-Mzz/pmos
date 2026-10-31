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
# Item isolation is a per-province choice; a lost login, rate limiting or an
# unknown network outcome always stops the province.
ISOLATABLE = frozenset({"site_data", "unknown", "permission", "local"})
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
    # Lane -> seconds after the latest response end before a request of that
    # lane (spec ``source.lane``); one shared serialized sequence. Shanxi
    # production: native 5 s, everything else 15 s.
    lane_intervals: dict = field(default_factory=dict)
    # Fault categories settled per item (task failed/denied, worker continues)
    # instead of blocking the province. Never auth, rate_limit or network.
    isolate_items: tuple = ()
    # Days within which an HTTP 200 + 40302 on a day period means "not yet
    # published" (Shanxi answers so for its newest days): the item is deferred
    # to the next day instead of denied. None: every 40302 is a denial.
    unpublished_window_days: int | None = None
    # Province defaults for the streaks that pause a dataset (a spec's own
    # ``source`` value wins). None: the engine's ISOLATION_STREAK / DENIAL_STREAK.
    # Liaoning production pauses a source at its first denial or unknown result.
    isolation_streak: int | None = None
    denial_streak: int | None = None
    # Business pauses (the login is still renewed) while the instance's disk
    # has less than this free; it resumes by itself (Liaoning 2026-10-31: 5.6 GB
    # left, the node prices alone would need ~84 GB).
    min_free_disk_mb: int | None = None
    extra: dict = field(default_factory=dict, compare=False)

    @classmethod
    def from_dict(cls, value: dict) -> "Policy":
        known = {f.name for f in fields(cls)} - {"extra"}
        known_values = {k: v for k, v in value.items() if k in known}
        if "isolate_items" in known_values:
            known_values["isolate_items"] = tuple(known_values["isolate_items"])
        policy = cls(**known_values, extra={k: v for k, v in value.items() if k not in known})
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
        if not isinstance(self.lane_intervals, dict) or any(
                not isinstance(k, str) or not number(v) or v < MIN_RESPONSE_INTERVAL_SECONDS
                for k, v in self.lane_intervals.items()):
            raise PolicyError(f"lane_intervals maps lane names to seconds >= {MIN_RESPONSE_INTERVAL_SECONDS}")
        if self.unpublished_window_days is not None and (type(self.unpublished_window_days) is not int
                                                         or not 1 <= self.unpublished_window_days <= 31):
            raise PolicyError("unpublished_window_days must be null or 1..31")
        if self.min_free_disk_mb is not None and (type(self.min_free_disk_mb) is not int or self.min_free_disk_mb < 1):
            raise PolicyError("min_free_disk_mb must be null or a positive integer")
        for name in ("isolation_streak", "denial_streak"):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or not 1 <= value <= 100):
                raise PolicyError(f"{name} must be null or 1..100")
        if not set(self.isolate_items) <= ISOLATABLE:
            raise PolicyError(f"isolate_items may only name {sorted(ISOLATABLE)}")

    def interval(self, lane: str | None = None) -> float:
        return self.lane_intervals.get(lane, self.response_interval_seconds) if lane else self.response_interval_seconds


def wait_seconds(policy: Policy, now: float, last_response_end: float,
                 budget_starts: list[float] | None = None, *, charged: bool = True, lane: str | None = None) -> float:
    """Seconds until the next request may be sent (0 means now)."""
    ready = last_response_end + policy.interval(lane)
    if charged and budget_starts:
        for limit, window in ((policy.max_per_hour, HOUR), (policy.max_per_day, DAY)):
            if limit is None:
                continue
            recent = [t for t in budget_starts if now - t < window]
            if len(recent) >= limit:
                ready = max(ready, recent[-limit] + window + 1)
    return max(0.0, ready - now)
