"""A stopped collection is told within minutes (Liaoning 2026-11-01: liaoning-monitor.timer has no successor; a one-hour
stop was found only by a missing hourly mail). Read-only on the instance; never touches the website, the browser or a block.

conditions() names what is wrong now; reconcile() turns its changes into mails, once per condition, again every
REMIND_SECONDS while it lasts, and once when it clears. A mail that could not be sent is tried again on the next check.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime
from email.message import EmailMessage
from email.utils import formatdate
from pathlib import Path

from .reporting import STALLED_MINUTES

REMIND_SECONDS = 6 * 3600
HUNG_SECONDS = 300
# Phases in which no new answer is expected.
QUIET = ("complete", "quiet_window", "caught_up", "waiting_publication", "paused", "stopped_all", "idle")


def _state(instance: Path, name: str):
    try:
        return json.loads((instance / "state" / f"{name}.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return None


def last_answer(instance: Path) -> float | None:
    """When a query last had an answer: the attempts index, not the archive (a million files, every 5 minutes)."""
    path = instance / "ledger.sqlite3"
    if not path.exists():
        return None
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)) as db:
        return db.execute("SELECT MAX(finished_at) FROM attempts WHERE kind IN ('collect','sample')"
                          " AND outcome IN ('success','empty')").fetchone()[0]


def conditions(instance: Path, unit_state: str, now: float) -> dict[str, str]:
    """code -> what the mail says. Stopped on purpose (stop-all, pause) is no condition."""
    found = {}
    control = _state(instance, "control") or {}
    status = _state(instance, "status") or {}
    block = _state(instance, "block")
    phase = status.get("phase")
    if unit_state != "active" and not control.get("stop_all"):
        found["worker_down"] = f"采集服务不在运行（{unit_state or '未知'}），也不是人为 stop-all。"
    if block:
        found["blocked"] = f"全省已封锁：{block.get('reason')}（{block.get('at')}），保活{'继续' if block.get('keepalive_allowed') else '也停了'}。"
    if phase == "disk_paused":
        found["disk_paused"] = "磁盘空间不足，采集已暂停（保活继续）。"
    for name, hold in (_state(instance, "dataset_holds") or {}).items():
        if not hold.get("until"):
            found["held:" + name] = f"报表 {name} 已暂停，要人工放开：{hold.get('reason')}（{hold.get('at')}）。"
    if unit_state == "active" and not block and not control.get("stop_all") and not control.get("paused"):
        try:
            age = now - datetime.fromisoformat(status["updated_at"]).timestamp()
        except (KeyError, TypeError, ValueError):
            age = None
        if age is not None and age > HUNG_SECONDS:
            found["worker_hung"] = f"采集进程在，但 {round(age / 60)} 分钟没有更新状态。"
        answered = last_answer(instance)
        if phase not in QUIET and answered is not None and now - answered >= STALLED_MINUTES * 60:
            found["stalled"] = f"已 {round((now - answered) / 60)} 分钟没有拿到答复（状态 {phase}）。"
    return found


def reconcile(state: dict, found: dict[str, str], now: float) -> tuple[dict, list[dict]]:
    """(new state, mails to send). ``state``: code -> {since, sent, text}; {cleared: [...]} waits for its mail."""
    active, mails = dict(state.get("active") or {}), []
    for code, text in found.items():
        entry = active.get(code)
        if entry is None:
            entry = active[code] = {"since": now, "sent": None, "text": text}
        entry["text"] = text
        if entry["sent"] is None or now - entry["sent"] >= REMIND_SECONDS:
            mails.append({"kind": "alert", "code": code, "text": text, "since": entry["since"]})
    cleared = list(state.get("cleared") or [])
    for code in [code for code in active if code not in found]:
        entry = active.pop(code)
        if entry["sent"] is not None:         # nothing to take back if it was never told
            cleared.append({"code": code, "text": entry["text"], "since": entry["since"]})
    mails += [{"kind": "cleared", **item} for item in cleared]
    return {"active": active, "cleared": cleared}, mails


def mark_sent(state: dict, mail: dict, now: float) -> None:
    if mail["kind"] == "alert":
        state["active"][mail["code"]]["sent"] = now
    else:
        state["cleared"] = [item for item in state["cleared"] if item["code"] != mail["code"]]


def message(mail: dict, label: str, address: str, recipient: str) -> EmailMessage:
    """Not the [CRAWLER-STATUS-V1] protocol: nothing but a person reads these."""
    since = datetime.fromtimestamp(mail["since"]).strftime("%m-%d %H:%M")
    item = EmailMessage()
    item["From"], item["To"], item["Date"] = address, recipient, formatdate(localtime=True)
    if mail["kind"] == "alert":
        item["Subject"] = f"[PMOS告警] {label}：{mail['code']}"
        item.set_content(f"{label}：{mail['text']}\n开始时间：{since}\n\n这封告警不会自动处理任何事，请到{label}会话查看。")
    else:
        item["Subject"] = f"[PMOS恢复] {label}：{mail['code']}"
        item.set_content(f"{label}：已恢复。此前（{since} 起）：{mail['text']}")
    return item
