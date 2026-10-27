"""Attach to an already running, already logged-in Chrome over loopback CDP."""
from __future__ import annotations

import re
from pathlib import Path


class AttachError(RuntimeError):
    pass


def cdp_endpoint(browser: dict) -> str:
    """``browser.cdp_url`` wins; otherwise read DevToolsActivePort of ``profile_dir``."""
    url = browser.get("cdp_url")
    if url:
        if not re.fullmatch(r"(http|ws)://127\.0\.0\.1:\d{2,5}(/.*)?", url):
            raise AttachError("cdp_url_must_be_loopback")
        return url
    profile = browser.get("profile_dir")
    if not profile:
        raise AttachError("browser.cdp_url or browser.profile_dir required")
    active = Path(profile).expanduser() / "DevToolsActivePort"
    if active.is_symlink() or not active.exists():
        raise AttachError("devtools_active_port_missing")
    lines = active.read_text(encoding="ascii").splitlines()
    if len(lines) != 2 or not lines[0].isdigit() or not re.fullmatch(r"/devtools/browser/[A-Za-z0-9-]+", lines[1]):
        raise AttachError("devtools_active_port_invalid")
    return f"ws://127.0.0.1:{int(lines[0])}{lines[1]}"


def select_page(browser, origin: str, contains: str | None = None):
    """Exactly one top-level page of the province site must be open."""
    pages = [page for context in browser.contexts for page in context.pages
             if page.url.startswith(origin + "/") and (contains is None or contains in page.url)]
    if len(pages) != 1:
        raise AttachError("site_page_not_unique" if pages else "site_page_missing")
    return pages[0]


async def attach(playwright, origin: str, browser_config: dict):
    browser = await playwright.chromium.connect_over_cdp(cdp_endpoint(browser_config))
    return browser, select_page(browser, origin, browser_config.get("page_url_contains"))
