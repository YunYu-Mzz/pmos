"""Pure admission at an idle boundary; never restarts a browser.

Driver-private Route/Future/buffer disposal is version-specific and stays in
the legacy owners until independently verified. This module only decides.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MemoryObservation:
    charged_bytes: int | None
    limit_bytes: int
    driver_bytes: int | None
    driver_limit_bytes: int
    in_flight: bool = False
    keeper_pending: bool = False


def admission(observation: MemoryObservation) -> str:
    if observation.in_flight or observation.keeper_pending:
        return "wait_owned_operation"
    if observation.charged_bytes is None or observation.driver_bytes is None:
        return "wait_measurement_unknown"
    if observation.charged_bytes >= observation.limit_bytes or observation.driver_bytes >= observation.driver_limit_bytes:
        return "resource_hold_retain_session"
    return "admit"
