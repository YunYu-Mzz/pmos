"""Small durable JSON state for one province instance.

Files live in ``<instance>/state``. Every write is atomic (temp file, fsync,
rename). Only fixed reason codes are stored; never headers, cookies, tokens,
response bodies or raw exception text.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from .periods import CHINA

CODE = re.compile(r"[a-z][a-z0-9_]{0,79}")


class StateError(ValueError):
    pass


def now_iso() -> str:
    return datetime.now(CHINA).isoformat(timespec="seconds")


def code(value: Any, field: str = "reason") -> str:
    if not isinstance(value, str) or not CODE.fullmatch(value):
        raise StateError(f"{field} must be a fixed lowercase code")
    return value


class StateStore:
    def __init__(self, root: Path | str):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def path(self, name: str) -> Path:
        code(name, "state name")
        return self.root / f"{name}.json"

    def read(self, name: str, default: Any = None) -> Any:
        path = self.path(name)
        if not path.exists():
            return default
        return json.loads(path.read_text(encoding="utf-8"))

    def write(self, name: str, value: Any) -> Any:
        payload = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")
        descriptor, temporary = tempfile.mkstemp(dir=self.root, prefix=f".{name}.")
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path(name))
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return value

    def remove(self, name: str) -> None:
        self.path(name).unlink(missing_ok=True)

    def event(self, phase: str, **fields: Any) -> dict:
        record = {"at": now_iso(), "phase": code(phase, "phase"), **fields}
        line = (json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
        descriptor = os.open(self.root / "events.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(descriptor, line)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return record

    # -- control: what the operator wants ---------------------------------
    def control(self) -> dict:
        return self.read("control", {"collect": False, "paused": False})

    def set_control(self, **changes: Any) -> dict:
        value = {**self.control(), **changes, "updated_at": now_iso()}
        self.write("control", value)
        self.event("control", **{k: v for k, v in changes.items() if isinstance(v, (bool, str, int))})
        return value

    # -- block: a protection that only a human may clear -------------------
    def block(self, reason: str, **fields: Any) -> dict:
        """Keep the first unreviewed protection; later ones are only logged."""
        code(reason)
        current = self.read("block")
        if current is not None:
            self.event("block_ignored", reason=reason, active=current["reason"])
            return current
        value = {"reason": reason, "at": now_iso(), **fields}
        self.write("block", value)
        self.event("blocked", reason=reason)
        return value

    def blocked(self) -> dict | None:
        return self.read("block")

    def clear_block(self, expected_reason: str) -> dict:
        """Explicit human acknowledgement for exactly the reviewed block."""
        current = self.read("block")
        if current is None:
            raise StateError("no_block")
        if current["reason"] != expected_reason:
            raise StateError("block_reason_mismatch")
        self.write("block_history_last", current)
        self.remove("block")
        self.event("block_cleared", reason=expected_reason)
        return current

    def status(self, **fields: Any) -> dict:
        value = {**self.read("status", {}), **fields, "updated_at": now_iso(), "pid": os.getpid()}
        return self.write("status", value)
