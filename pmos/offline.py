"""Read only originals, replay through the existing Engine, never attach CDP."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile

from .adapters import decode
from .contracts import TaskIdentity, canonical
from .engine import Engine
from .province import PROVINCES, Province
from .validate import Response


def load_contract(province: str) -> dict:
    if province not in {"liaoning", "anhui", "shanxi"}:
        raise ValueError("province_not_in_phase_one")
    return json.loads((PROVINCES / province / "offline.json").read_text())


def replay(province: str, path: Path, expected_sha256: str) -> dict:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 16 * 1024 * 1024:
        raise ValueError("bounded_regular_original_required")
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != expected_sha256:
        raise ValueError("source_sha256_mismatch")
    parsed = decode(raw, load_contract(province))
    return {"province": province, "source_sha256": digest, "size": len(raw),
            "parsed_sha256": hashlib.sha256(canonical(parsed.rows)).hexdigest(),
            **parsed.summary(), "website_requests": 0, "validation": "sample_only"}


async def demo(province: str) -> dict:
    """Three adapters exercise exactly the same durable engine and archive."""
    contract = load_contract(province)
    fixture = PROVINCES.parent / "tests" / "fixtures" / (province + "_native.json")
    raw = fixture.read_bytes()
    parsed = decode(raw, contract)
    now = [1_792_000_000.0]

    class FixtureTransport:
        calls = 0

        async def send(self, request):
            self.calls += 1
            now[0] += 3
            return Response(200, "application/json", raw)

    identity = TaskIdentity(province, "offline-fixture", contract["dataset"],
                            {"kind": "day", "start": "2026-10-18", "end": "2026-10-18"})
    with tempfile.TemporaryDirectory(prefix="pmos-offline-") as temporary:
        root = Path(temporary)
        directory = root / province
        directory.mkdir()
        (directory / "datasets").mkdir()
        (directory / "province.json").write_bytes(canonical({"id": province, "name": province,
            "origin": "https://offline.invalid", "policy": {"response_interval_seconds": contract["interval_reference_seconds"]}}))
        # This fixture specification cannot reach a real endpoint.
        spec = {"id": contract["dataset"], "name": "脱敏固定夹具", "status": "enabled",
                "period": {"kind": "day", "start": "2026-10-18", "end": "2026-10-18"},
                "request": {"transport": "fetch", "method": "POST", "path": "/offline/fixture"},
                "response": {"kind": "json"}}
        (directory / "datasets" / (contract["dataset"] + ".json")).write_bytes(canonical(spec))
        transport = FixtureTransport()
        engine = Engine(Province.load(province, root), transport, instance=root / "instance", clock=lambda: now[0],
                        decode_response=lambda body: decode(body, contract))
        engine.acquire()
        try:
            engine.ledger.seed(contract["dataset"], ["2026-10-18"])
            result = await engine.collect(engine.province.datasets()[contract["dataset"]], "2026-10-18")
            if result.parsed != parsed:
                raise ValueError("engine_decoder_result_mismatch")
            # Re-seeding an already archived identity does not create new work.
            added = engine.ledger.seed(contract["dataset"], ["2026-10-18"])
            return {"province": province, "task_key": identity.key, "fixture": True,
                    "website_requests": 0, "fixture_dispatches": transport.calls,
                    "task_status": engine.ledger.task(contract["dataset"], "2026-10-18")["status"],
                    "archive_verified": engine.archive.verify(result.record), "reseed_added": added,
                    "spacing_remaining": engine.spacing_wait("collect"), **parsed.summary()}
        finally:
            engine.release()
