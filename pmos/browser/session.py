"""Read-only page observation: logout notices and adaptation capture.

``visible_logout`` scans every frame's Element UI notices; it never clicks
them. Clicking OK on the site's "会话已注销" dialog calls the server logout and
kills the session for every tab, so the collector leaves dialogs to humans.

``Recorder`` passively logs the page's own API calls while a human browses a
report (adaptation step 1). It keeps method, path, request body and response
metadata, never headers, cookies or tokens.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from .. import platform

_NOTICE_JS = """selectors => [...document.querySelectorAll(selectors)]
  .filter(e => e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden')
  .slice(0, 30).map(e => (e.innerText || '').slice(0, 300))"""


async def visible_logout(page, extra_patterns=()) -> str | None:
    for frame in page.frames:
        try:
            texts = await frame.evaluate(_NOTICE_JS, platform.NOTICE_SELECTORS)
        except Exception:
            continue  # frames navigate and detach during normal use
        for text in texts:
            if platform.NETWORK_CHANGED_TEXT.search(text):
                return "network_environment_changed"
            if platform.AUTH_TEXT.search(text) or any(p.search(text) for p in extra_patterns):
                return "auth_expired"
    return None


SENSITIVE_KEYS = ("token", "ticket", "password", "passwd", "pwd", "cookie", "secret", "authorization", "sign")


def _redact(value):
    if isinstance(value, dict):
        return {k: ("<redacted>" if any(s in k.lower() for s in SENSITIVE_KEYS) else _redact(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v) for v in value[:50]]
    return value


class Recorder:
    def __init__(self, origin: str, path: Path, clock=time.time):
        self.origin = origin
        self.path = path
        self.clock = clock
        self.count = 0
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    def attach(self, context, spawn) -> None:
        context.on("response", lambda response: spawn(self._record(response)))

    async def _record(self, response) -> None:
        request = response.request
        parsed = urlsplit(request.url)
        if (f"{parsed.scheme}://{parsed.netloc}" != self.origin or request.resource_type not in ("xhr", "fetch")
                or parsed.path in platform.NATIVE_BACKGROUND_PATHS):
            return
        body = request.post_data
        try:
            body = _redact(json.loads(body)) if body else None
        except ValueError:
            body = {"_non_json_length": len(body)}
        content_type = response.headers.get("content-type", "")
        entry = {"at": self.clock(), "method": request.method, "path": parsed.path,
                 "query": _redact(dict(parse_qsl(parsed.query))), "body": body,
                 "frame_url": request.frame.url.split("?")[0] if request.frame else None,
                 "http_status": response.status, "content_type": content_type.split(";")[0]}
        try:
            payload = await response.body()
            entry["size"] = len(payload)
            if "json" in content_type and len(payload) < 4_000_000:
                parsed_body = json.loads(payload)
                entry["business_status"] = platform.business_status(parsed_body)
                data = parsed_body.get("data") if isinstance(parsed_body, dict) else None
                entry["data_shape"] = type(data).__name__ + (f"[{len(data)}]" if isinstance(data, (list, dict)) else "")
        except Exception:
            entry["body_unavailable"] = True
        self.count += 1
        entry["index"] = self.count
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
