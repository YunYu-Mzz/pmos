"""Attach to an already running, already logged-in Chrome over loopback CDP."""
from __future__ import annotations

import re
import stat
from pathlib import Path

from ..fsretry import eperm_retry


class AttachError(RuntimeError):
    pass


def cdp_endpoint(browser: dict) -> str:
    """``browser.cdp_url`` wins; otherwise the port of ``profile_dir``'s DevToolsActivePort, asked for the
    running browser's own socket (``/json/version``).

    Anhui 2026-11-01: the file still held the browser GUID of the launch that
    wrote it (10-23); the Edge running since 10-31 never rewrote it, so that
    path answered 404 while the port was right. ``profile_process_check``:
    the process listening on the port must run with this ``--user-data-dir``.
    """
    url = browser.get("cdp_url")
    if url:
        if not re.fullmatch(r"(http|ws)://127\.0\.0\.1:\d{2,5}(/.*)?", url):
            raise AttachError("cdp_url_must_be_loopback")
        return url
    profile = browser.get("profile_dir")
    if not profile:
        raise AttachError("browser.cdp_url or browser.profile_dir required")
    profile = Path(profile).expanduser()
    # Anhui's profile is under ~/Desktop, where macOS briefly refuses launchd processes (EPERM).
    lines = eperm_retry(_active_port, profile / "DevToolsActivePort")
    if len(lines) != 2 or not lines[0].isdigit() or not re.fullmatch(r"/devtools/browser/[A-Za-z0-9-]+", lines[1]):
        raise AttachError("devtools_active_port_invalid")
    port = int(lines[0])
    if browser.get("profile_process_check"):
        commands = _listener_commands(port)
        wanted = f" --user-data-dir={profile.resolve()} "
        if not commands or not all(wanted in f" {command.strip()} " for command in commands):
            raise AttachError("devtools_port_not_this_profile")
    socket = _browser_socket(port)
    if not re.fullmatch(rf"ws://127\.0\.0\.1:{port}/devtools/browser/[A-Za-z0-9-]+", socket or ""):
        raise AttachError("devtools_version_invalid")
    return socket


def _browser_socket(port: int) -> str | None:
    """The browser's current ``webSocketDebuggerUrl`` (loopback only; nothing of the site is asked)."""
    import json
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=5) as answer:
            return json.loads(answer.read()).get("webSocketDebuggerUrl")
    except (OSError, ValueError):
        raise AttachError("devtools_version_unreachable") from None


def _listener_commands(port: int) -> list[str]:
    """Command lines of the processes listening on the loopback port (macOS lsof and ps)."""
    import subprocess
    try:
        pids = subprocess.run(["lsof", "-nP", "-a", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
                              capture_output=True, text=True, timeout=10).stdout.split()
        return [subprocess.run(["ps", "-o", "command=", "-p", pid], capture_output=True, text=True,
                               timeout=10).stdout for pid in sorted(set(pids))]
    except (OSError, subprocess.SubprocessError):
        raise AttachError("devtools_port_owner_unreadable") from None


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
                # Re-login can leave our old named tab on the site's home
                # column. Its name alone does not make it 综合查询.
                if (config.get("initial_section") and config.get("url_contains")
                        and config["url_contains"] not in page.url):
                    await page.evaluate("() => { window.name = ''; }")
                else:
                    named.append(page)
        except Exception:
            continue                      # a crashed or closing page plays no role
    if len(named) == 1:
        return await _prepare_work_page(named[0], config, timeout)
    if named:
        raise AttachError("work_page_not_unique")
    if config.get("adopt_url_contains"):
        kind = [page for page in pages if config["adopt_url_contains"] in page.url]
        if len(kind) > 1:
            raise AttachError("work_page_candidates_not_unique")
        if kind:
            await kind[0].evaluate("name => { window.name = name; }", config["name"])
            return await _prepare_work_page(kind[0], config, timeout)
    # Anhui 2026-11-01 12:39: the portal is /?ACYxOEl1=…#/dashboard (the site's dynamic parameter
    # sits before the hash), so its address is matched without the query.
    portals = [page for page in pages if config["open_from"] in _without_query(page.url)]
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
    return await _prepare_work_page(page, config, timeout)


async def _prepare_work_page(page, config: dict, timeout: float):
    """Anhui's login handoff ends at a ready market tree, not just a tab."""
    if config.get("initial_section"):
        from . import ui
        await ui.bring_forward(page)
        await ui.select_section(page, config["initial_section"], timeout)
        await ui._leave_hover_menu(page)
    return page


def _without_query(url: str) -> str:
    from urllib.parse import urlsplit, urlunsplit
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", parts.fragment))


async def _open_by_menu(portal, labels: list, timeout: float):
    """Click the portal's menu as a person does (Anhui open_disclosure): expand the parents if the
    leaf is not visible, then the leaf opens a new tab through the site's own script."""
    import asyncio
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    async def visible_label(label, *, wait=True):
        while True:
            locator = portal.get_by_text(label, exact=True)
            matches = [locator.nth(i) for i in range(await locator.count())
                       if await locator.nth(i).is_visible()]
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                raise AttachError("portal_menu_leaf_not_unique")
            if not wait:
                return None
            if loop.time() >= deadline:
                raise AttachError("portal_menu_not_ready")
            await asyncio.sleep(0.2)

    leaf = await visible_label(labels[-1], wait=False)
    if leaf is None:
        for label in labels[:-1]:
            parent = await visible_label(label)
            await parent.click(timeout=max(1, (deadline - loop.time()) * 1000), no_wait_after=True)
        leaf = await visible_label(labels[-1])
    async with portal.context.expect_page(timeout=max(1, (deadline - loop.time()) * 1000)) as opened:
        await leaf.click(timeout=max(1, (deadline - loop.time()) * 1000), no_wait_after=True)
    return await opened.value


async def reenter_work_page(browser, origin: str, config: dict):
    """A new login gets a fresh query app from the portal's own menu.

    The old app may retain an expired session even with a valid role URL.
    Only this worker's named query tab is closed, after its replacement is
    ready. The portal and any user-owned tabs stay.
    """
    old = []
    exclude = []
    for context in browser.contexts:
        for page in context.pages:
            if not page.url.startswith(origin + "/") or config["open_from"] in _without_query(page.url):
                continue
            owned = await page.evaluate("() => window.name") == config["name"]
            if owned:
                old.append(page)
            if owned or config.get("adopt_url_contains", config["url_contains"]) in page.url:
                exclude.append(page)
    fresh = await work_page(browser, origin, config, exclude=exclude)
    for page in old:
        if not page.is_closed():
            await page.close()
    return fresh


async def attach(playwright, origin: str, browser_config: dict):
    browser = await playwright.chromium.connect_over_cdp(cdp_endpoint(browser_config))
    if browser_config.get("work_page"):
        config = browser_config["work_page"]
        page = await (reenter_work_page(browser, origin, config) if config.get("reenter_on_attach")
                      else work_page(browser, origin, config))
        return browser, page
    return browser, select_page(browser, origin, browser_config.get("page_url_contains"))
