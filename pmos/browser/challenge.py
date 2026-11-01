"""A page replacement that met the site's security challenge.

Anhui 2026-11-01 06:59: a planned recycle's new page got 412 on its first
document; the browser then solved the site's challenge by itself and
reloaded (no slider, no captcha), but the reload landed on 信息披露's 首页
column without the 现货市场 tree. The user decided (11-01 10:5x, 允许自动判断后继续):
only for a planned recycle and only that one document 412, if within 45 s the
page has loaded on the 综合查询 site with no login/security/rate-limit text,
the usual 96-point keepalive check decides; at most once in 2 hours.
Everything else stops the province for a person, as before.
"""
from __future__ import annotations

import asyncio
import time
from urllib.parse import urlsplit

from ..faults import Fault

# As the Anhui legacy visible_reason: login, security, rate-limit and permission notices.
STOP_TEXT = ("访问过于频繁", "请求过于频繁", "访问频繁", "限流",
             "登录失效", "登录已过期", "请重新登录", "未登录", "会话已注销", "会话已过期",
             "网络环境发生变化", "安全验证", "滑动验证", "拖动滑块",
             "您当前没有该菜单访问权限", "权限不足", "没有访问权限", "无权访问", "操作异常")


class DocumentWatch:
    """Every top-level document of the site that came back >= 400 while a replacement page opens."""

    def __init__(self, context, origin: str, *, ignore=()):
        self.context, self.origin, self.ignore = context, origin, tuple(ignore)
        self.failures: list[dict] = []

    def _seen(self, response) -> None:
        try:
            request = response.request
            if response.status < 400 or request.resource_type != "document":
                return
            parsed = urlsplit(response.url)
            if f"{parsed.scheme}://{parsed.netloc}" != self.origin:
                return
            try:
                frame = request.frame
            except Exception:
                # A popup's first navigation is issued before its frame exists: the new page's own document.
                if not request.is_navigation_request():
                    return
                frame = None
            if frame is not None and (frame.parent_frame is not None or frame.page in self.ignore):
                return
            self.failures.append({"status": response.status, "path": parsed.path,
                                  "page": frame.page if frame is not None else None})
        except Exception:
            pass  # a response of a closing page decides nothing

    def __enter__(self):
        self.context.on("response", self._seen)
        return self

    def __exit__(self, *exc):
        try:
            self.context.remove_listener("response", self._seen)
        except Exception:
            pass
        return False

    def only(self, status: int) -> bool:
        return len(self.failures) == 1 and self.failures[0]["status"] == status

    def fault(self) -> Fault:
        first = self.failures[0]
        code = "security_verification" if first["status"] == 412 else "page_document_failed"
        return Fault(code, "auth", http_status=first["status"])


async def settle(page, watch: DocumentWatch, rule: dict, *, clock=time.monotonic, sleep=asyncio.sleep) -> dict | None:
    """Up to ``settle_seconds``: the page has loaded on ``rule["path"]`` with ``rule["text"]`` and no stop text,
    and still only the one challenge was seen. None otherwise."""
    start = clock()
    end = start + rule.get("settle_seconds", 45)
    while clock() < end:
        await sleep(1)
        if not watch.only(rule.get("status", 412)) or page.is_closed():
            return None
        try:
            ready, text = await page.evaluate("() => [document.readyState, document.body ? document.body.innerText : '']")
        except Exception:
            continue                      # the challenge's reload replaced the document
        if any(needle in text for needle in STOP_TEXT):
            return None
        parsed = urlsplit(page.url)
        if (ready == "complete" and f"{parsed.scheme}://{parsed.netloc}" == watch.origin
                and parsed.path == rule["path"] and rule["text"] in text):
            return {"status": watch.failures[0]["status"], "settled_after_seconds": round(clock() - start, 1)}
    return None


def numeric_series(value) -> list:
    """Every number of a JSON answer, in order (numbers and numeric strings): the 96 points a check compares."""
    found = []

    def walk(node):
        if isinstance(node, dict):
            for item in node.values():
                walk(item)
        elif isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, bool) or node is None:
            return
        elif isinstance(node, (int, float)):
            found.append(float(node))
        elif isinstance(node, str):
            try:
                found.append(float(node))
            except ValueError:
                pass
    walk(value)
    return found
