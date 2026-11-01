"""One status mail for all five provinces (user 2026-11-01: 统一所有省份邮件格式，都统一披露相同的
信息和内容……按照安徽的模板来吧).

Canonical copy: pmos/pmos/status_mail.py. Each province's reporting directory carries a byte-identical
copy named status_mail.py, as it carries the shared bridge's protocol subset; tests/test_status_mail.py
checks every copy in the checkout. Standard library only, nothing from PMOS.

What every province sends, the way Anhui's PMOS bridge did on 2026-11-01:
- the 20 standard metrics (FIELDS) and nothing else. ordinary_* count all the province's originals,
  legacy and PMOS, de-duplicated by path;
- collector_state "active" while the collector runs, "inactive" when it does not, None when unknown,
  so the Feishu runtime line can say 运行 / 已停止 / 待核验;
- counts_note: one sentence with the same parts in the same order (counts_note());
- the mail body of render().
"""
from __future__ import annotations

from datetime import datetime

FIELDS = ("ordinary_originals", "ordinary_bytes", "last_hour_files_mtime", "missing_originals",
          "disk_free_bytes", "disk_total_bytes", "memory_available_bytes", "current_dataset",
          "collector_state", "collector_pid", "browser_state", "browser_pid", "collection_paused",
          "keeper_enabled", "keeper_last_success_at", "business_last_success_at", "blocked",
          "phase", "adaptation_enabled", "counts_note")

# The shared protocol rejects a metric text over 500 characters or with a control character.
MAX_TEXT = 500

PMOS_COUNTS = ("originals", "bytes", "last_hour_files_mtime", "missing_originals", "requests_last_hour")

# A PMOS province's per-indicator table still comes from the frozen legacy archive.
LEGACY_TABLE = "逐指标表仍为旧系统冻结统计。"


def _value(value) -> str:
    return "未知" if value is None else str(value)


def _text(value: str) -> str:
    return "".join(c for c in value if ord(c) >= 32)[:MAX_TEXT]


def counts_note(*, label: str, owner: str, since: str | None, headline: str | None,
                legacy_originals: int | None, pmos_originals: int | None, last_hour_files: int | None,
                requests_last_hour: int | None, saved: int | None, empty: int | None,
                pending: int | None, waiting: int | None, held: int | None, blocked: bool | None,
                block_reason: str | None, stalled_minutes: int | None, dedupe: str = "文件路径",
                tail: str = "") -> str:
    """The one sentence. ``owner`` is "PMOS" or "旧系统"; ``since`` is when it took over (China time);
    ``dedupe`` is what the province counts an original by (Shaanxi and Jibei: 原件SHA)."""
    total = None if legacy_originals is None or pmos_originals is None else legacy_originals + pmos_originals
    legacy = "旧系统冻结" if owner == "PMOS" else "旧系统"
    block = block_reason or ("原因未知" if blocked else "无" if blocked is False else "未知")
    return _text(
        f"{since + '起' if since else ''}{label}由{owner}采集（{headline or '状态未知'}）。"
        f"原件合计{_value(total)}份（{legacy}{_value(legacy_originals)}份、PMOS{_value(pmos_originals)}份；"
        f"近1小时{_value(last_hour_files)}份）；近1小时请求{_value(requests_last_hour)}次；"
        f"任务完成{_value(saved)}、空{_value(empty)}、待采{_value(pending)}、等待发布{_value(waiting)}；"
        f"暂停栏目{_value(held)}个；阻断：{block}；停滞{_value(stalled_minutes)}分钟。"
        f"按{dedupe}去重；近1小时按文件mtime估算。{tail}")


def headline(metrics: dict) -> str:
    """A legacy collector's headline from its metrics, in PMOS's words."""
    if metrics.get("blocked"):
        return "已暂停：阻断"
    if metrics.get("collection_paused"):
        return "已暂停"
    state = metrics.get("collector_state")
    if state == "active":
        return "正常"
    if state in ("inactive", "failed"):
        return "已停止"
    return "状态未知"


def legacy_note(metrics: dict, *, label: str, saved=None, empty=None, pending=None, waiting=None,
                held=None, requests_last_hour=None, block_reason=None, stalled_minutes=None,
                dedupe="文件路径") -> str:
    """The note of a province its legacy collector still collects: what it cannot tell stays 未知."""
    return counts_note(
        label=label, owner="旧系统", since=None, headline=headline(metrics),
        legacy_originals=metrics.get("ordinary_originals"), pmos_originals=0,
        last_hour_files=metrics.get("last_hour_files_mtime"), requests_last_hour=requests_last_hour,
        saved=saved, empty=empty, pending=pending, waiting=waiting, held=held,
        blocked=metrics.get("blocked"), block_reason=block_reason, stalled_minutes=stalled_minutes,
        dedupe=dedupe)


def valid_pmos(value) -> bool:
    """A ``pmos <id> status --mail`` answer a bridge may merge."""
    return isinstance(value, dict) and all(type(value.get(k)) is int and value[k] >= 0 for k in PMOS_COUNTS)


def _later(a, b):
    def parsed(v):
        try:
            return datetime.fromisoformat(v).timestamp()
        except (TypeError, ValueError):
            return None
    return max((v for v in (a, b) if parsed(v) is not None), key=parsed, default=a)


def apply_pmos(metrics: dict, pmos: dict, *, label: str, running: bool, tail: str = LEGACY_TABLE) -> None:
    """Merge ``pmos <id> status --mail`` into the legacy metrics, Anhui's way.

    The PMOS archive is a separate directory, so its counts add to the frozen legacy ones without
    double counting. PMOS describes the collector; browser, memory, disk and adaptation stay the
    legacy readings. ``running``: the bridge saw a fresh PMOS status and its worker process.
    """
    legacy = metrics["ordinary_originals"]
    for field, key in (("ordinary_originals", "originals"), ("ordinary_bytes", "bytes"),
                       ("last_hour_files_mtime", "last_hour_files_mtime"), ("missing_originals", "missing_originals")):
        metrics[field] = (metrics[field] or 0) + pmos[key]
    blocked = bool(pmos.get("blocked"))
    metrics.update(collector_state="active" if running else "inactive",
                   collector_pid=pmos.get("worker_pid") if running else None,
                   current_dataset=pmos.get("current_dataset"), phase=pmos.get("phase"),
                   collection_paused=not running or pmos.get("phase") in ("paused", "stopped_all"),
                   blocked=blocked,
                   # PMOS keeps the login itself while it runs and is not blocked.
                   keeper_enabled=running and not blocked)
    for key in ("business_last_success_at", "keeper_last_success_at"):
        metrics[key] = _later(metrics[key], pmos.get(key))
    tasks = pmos.get("tasks") or {}
    metrics["counts_note"] = counts_note(
        label=label, owner="PMOS", since=pmos.get("since"),
        headline=pmos.get("headline") if running else "已停止",
        legacy_originals=legacy, pmos_originals=pmos["originals"],
        last_hour_files=pmos["last_hour_files_mtime"], requests_last_hour=pmos["requests_last_hour"],
        saved=tasks.get("saved", 0), empty=tasks.get("empty", 0), pending=tasks.get("pending", 0),
        waiting=tasks.get("deferred", 0), held=len(pmos.get("held_datasets") or []),
        blocked=blocked, block_reason=pmos.get("block_reason"), stalled_minutes=pmos.get("stalled_minutes"),
        tail=tail)


def pmos_unreadable(metrics: dict, *, label: str, tail: str = LEGACY_TABLE) -> None:
    """A PMOS province whose ``status --mail`` failed: say so, and claim nothing about the collector."""
    metrics.update(collector_state=None, collector_pid=None, current_dataset=None, phase=None,
                   collection_paused=None, blocked=None, keeper_enabled=None)
    metrics["counts_note"] = _text("PMOS状态读取失败。" + counts_note(
        label=label, owner="PMOS", since=None, headline=None, legacy_originals=metrics["ordinary_originals"],
        pmos_originals=None, last_hour_files=None, requests_last_hour=None, saved=None, empty=None,
        pending=None, waiting=None, held=None, blocked=None, block_reason=None, stalled_minutes=None, tail=tail))


def standard(metrics: dict) -> dict:
    """Only the 20 fields, in protocol order (drops a province's extra counts)."""
    return {k: metrics[k] for k in FIELDS}


def render(report: dict, label: str) -> str:
    """The mail body: Anhui's."""
    m = report["metrics"]
    return (f"{label}采集进度\n核验时间：{report['generated_at']}\n"
            f"原始载体/源回执：{_value(m['ordinary_originals'])}份；{_value(m['ordinary_bytes'])}字节\n"
            f"{m['counts_note']}\n逐指标统计通过签名schema2协议交由唯一汇总端写入原飞书表格。")
