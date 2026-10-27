"""Shared offline fixtures: a temporary province, a fake transport and clock."""
from __future__ import annotations

import io
import json
import tempfile
import zipfile
from pathlib import Path

from pmos.province import Province
from pmos.validate import Response


def xlsx(marker: bytes = b"1") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as book:
        book.writestr("[Content_Types].xml", b"types")
        book.writestr("xl/workbook.xml", b"<workbook/>" + marker)
    return buffer.getvalue()


def excel(marker: bytes = b"1") -> Response:
    return Response(200, "application/vnd.ms-excel", xlsx(marker))


def envelope(status=0, data=None, message="") -> Response:
    body = {"status": status, "message": message, "data": data if data is not None else [1]}
    return Response(200, "application/json", json.dumps(body).encode())


class Clock:
    def __init__(self, start: float = 1_792_000_000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeTransport:
    """Records every request; answers with queued responses or a default."""

    def __init__(self, clock: Clock | None = None, default: Response | None = None, duration: float = 2.0):
        self.sent: list[dict] = []
        self.queue: list = []
        self.default = default or excel()
        self.clock = clock
        self.duration = duration
        self.unready: set[str] = set()   # paths whose template is not observed yet

    def ready(self, request: dict) -> bool:
        return request.get("path") not in self.unready

    async def send(self, request: dict) -> Response:
        self.sent.append(request)
        if self.clock is not None:
            self.clock.advance(self.duration)
        item = self.queue.pop(0) if self.queue else self.default
        if isinstance(item, Exception):
            raise item
        return item


def dataset(dataset_id: str, *, status="enabled", keepalive=False, kind="day", start="2026-10-01",
            response="xlsx", path=None) -> dict:
    value = {"id": dataset_id, "name": dataset_id, "status": status,
             "period": {"kind": kind, "start": start, "end": "today"},
             "request": {"transport": "fetch", "method": "POST", "path": path or f"/px-test/{dataset_id}",
                         "body": {"date": "{date}" if kind == "day" else "{month}"}},
             "response": {"kind": response}}
    if keepalive:
        value["keepalive"] = True
    return value


def make_province(root: Path, datasets: list[dict], **overrides) -> Province:
    directory = root / "provinces" / "testland"
    (directory / "datasets").mkdir(parents=True, exist_ok=True)
    profile = {"id": "testland", "name": "测试省", "origin": "https://pmos.test.sgcc.com.cn",
               "browser": {"cdp_url": "http://127.0.0.1:9222"},
               "policy": {"response_interval_seconds": 30, "renew_after_idle_seconds": 600,
                          "probe_after_idle_seconds": 300},
               "session": {"keepalive_dataset": next((d["id"] for d in datasets if d.get("keepalive")), None),
                           "probe": None}}
    for key, value in overrides.items():
        profile[key] = {**profile.get(key, {}), **value} if isinstance(value, dict) else value
    (directory / "province.json").write_text(json.dumps(profile, ensure_ascii=False))
    for item in datasets:
        (directory / "datasets" / f"{item['id']}.json").write_text(json.dumps(item, ensure_ascii=False))
    return Province.load("testland", root / "provinces")


class TempDir:
    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name)

    def cleanup(self):
        self._tmp.cleanup()
