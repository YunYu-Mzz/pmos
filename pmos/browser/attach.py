"""Attach to an already running, already logged-in Chrome over loopback CDP."""
from __future__ import annotations

import re
import stat
from pathlib import Path

from ..fsretry import eperm_retry


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
    # Anhui's profile is under ~/Desktop, where macOS briefly refuses launchd processes (EPERM).
    lines = eperm_retry(_active_port, Path(profile).expanduser() / "DevToolsActivePort")
    if len(lines) != 2 or not lines[0].isdigit() or not re.fullmatch(r"/devtools/browser/[A-Za-z0-9-]+", lines[1]):
        raise AttachError("devtools_active_port_invalid")
    return f"ws://127.0.0.1:{int(lines[0])}{lines[1]}"


def _active_port(active: Path) -> list[str]:
    # lstat, not exists()/is_file(): those can turn an EPERM into "missing" instead of raising it.
    try:
        if not stat.S_ISREG(active.lstat().st_mode):
            raise AttachError("devtools_active_port_missing")
        return active.read_text(encoding="ascii").splitlines()
    except (FileNotFoundError, NotADirectoryError):
        raise AttachError("devtools_active_port_missing") from None


def select_page(browser, origin: str, contains: str | None = None):
    """Exactly one top-level page of the province site must be open."""
    pages = [page for context in browser.contexts for page in context.pages
             if page.url.startswith(origin + "/") and (contains is None or contains in page.url)]
    if len(pages) != 1:
        raise AttachError("site_page_not_unique" if pages else "site_page_missing")
    return pages[0]


async def work_page(browser, origin: str, config: dict, timeout: float = 30, *, exclude=()):
    """PMOS's own work page (window name ``config["name"]``); adopted or opened from the portal if missing.

    - ``adopt_url_contains``: the one existing page of that kind becomes the
      work page (Anhui: the 综合查询 page the legacy worker left; one tab only).
    - ``menu``: opened by the site's own menu click on the portal (Anhui: the
      site's script adds a dynamic, verified parameter to the address; a bare
      window.open of the plain address is unproven), waiting for
      ``url_contains``.
    - ``url``: opened with window.open from the portal (Liaoning: proven by its
      legacy cold starts; inherits sessionStorage natively, nothing is copied).
    """
    pages = [page for context in browser.contexts for page in context.pages
             if page.url.startswith(origin + "/") and page not in exclude]
    named = []
    for page in pages:
        try:
            if await page.evaluate("() => window.name") == config["name"]:
                named.append(page)
        except Exception:
            continue                      # a crashed or closing page plays no role
    if len(named) == 1:
        return named[0]
    if named:
        raise AttachError("work_page_not_unique")
    if config.get("adopt_url_contains"):
        kind = [page for page in pages if config["adopt_url_contains"] in page.url]
        if len(kind) > 1:
            raise AttachError("work_page_candidates_not_unique")
        if kind:
            await kind[0].evaluate("name => { window.name = name; }", config["name"])
            return kind[0]
    portals = [page for page in pages if config["open_from"] in page.url]
    if len(portals) != 1:
        raise AttachError("portal_page_not_unique" if portals else "portal_page_missing")
    portal = portals[0]
    if config.get("menu"):
        page = await _open_by_menu(portal, config["menu"], timeout)
        await page.wait_for_url(lambda url: config["url_contains"] in url, timeout=timeout * 1000)
        await page.wait_for_load_state("load", timeout=timeout * 1000)
        await page.evaluate("name => { window.name = name; }", config["name"])
    else:
        import json
        session = await portal.context.new_cdp_session(portal)
        try:
            async with portal.context.expect_page(timeout=timeout * 1000) as opened:
                # As a person's click: with a user gesture, so the popup is never blocked.
                await session.send("Runtime.evaluate", {
                    "expression": f"window.open({json.dumps(origin + config['url'])}, {json.dumps(config['name'])}); true",
                    "userGesture": True})
            page = await opened.value
        finally:
            await session.detach()
        await page.wait_for_load_state("load", timeout=timeout * 1000)
    if await page.evaluate("() => window.name") != config["name"]:
        raise AttachError("work_page_name_lost")
    return page


async def _open_by_menu(portal, labels: list, timeout: float):
    """Click the portal's menu as a person does (Anhui open_disclosure): expand the parents if the
    leaf is not visible, then the leaf opens a new tab through the site's own script."""
    leaf = portal.get_by_text(labels[-1], exact=True)
    if not await leaf.is_visible():
        for label in labels[:-1]:
            await portal.get_by_text(label, exact=True).first.click(timeout=timeout * 1000)
    if await leaf.count() != 1:
        raise AttachError("portal_menu_leaf_not_unique")
    async with portal.context.expect_page(timeout=timeout * 1000) as opened:
        await leaf.click(timeout=timeout * 1000, no_wait_after=True)
    return await opened.value


async def attach(playwright, origin: str, browser_config: dict):
    browser = await playwright.chromium.connect_over_cdp(cdp_endpoint(browser_config))
    if browser_config.get("work_page"):
        return browser, await work_page(browser, origin, browser_config["work_page"])
    return browser, select_page(browser, origin, browser_config.get("page_url_contains"))
