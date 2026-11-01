"""Element UI driving shared by the ``component`` and ``ui`` transports.

* ``ensure_frame`` finds the report frame. When it is not open it either
  loads ``request.open_url`` (the report's own SPA route) in a hidden
  same-origin iframe, as the Liaoning collector does, or clicks through the
  site's left menu tree (``request.menu`` = list of labels), as Anhui does.
* ``select_menu`` opens a menu leaf by its full label path (not by a label
  alone, which repeats across sections); a leaf already current is not
  clicked again.
* ``fill`` sets one form field (date input or select) like a human would.
* ``settled`` waits until loading masks are gone.
* ``list_rows`` / ``goto_page`` / ``open_record`` / ``record_files`` read an
  article listing (table + el-pagination) and an article dialog.

Ported from the Anhui collector (adapters/daily.py, adapters/site_navigation.py,
adapters/publication_listing.py, adapters/publications.py).
"""
from __future__ import annotations

import asyncio
import json
import re

from ..faults import Fault

_BUSY_JS = """() => ![...document.querySelectorAll('[aria-busy="true"], .el-loading-mask')]
  .some(e => e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden')"""


# One hidden iframe per front-end app: a later route of the same app changes
# that iframe's hash instead of starting another app instance (each instance
# polls the site in the background).
_OPEN_FRAME_JS = """args => {
  const target = new URL(args.src, location.origin);
  if (target.origin !== location.origin) throw new Error('open_url_cross_origin');
  for (const existing of document.querySelectorAll('iframe[id^="pmos-"]')) {
    let current;
    try { current = new URL(existing.contentWindow.location.href); } catch (e) { continue; }
    if (current.origin === target.origin && current.pathname === target.pathname && target.hash) {
      existing.contentWindow.location.hash = target.hash;
      return 'reused';
    }
  }
  const frame = document.createElement('iframe');
  frame.id = args.id.replace(/[^A-Za-z0-9_-]/g, '_');
  frame.setAttribute('aria-hidden', 'true');
  frame.style.cssText = 'position:fixed;left:-10000px;top:0;width:1280px;height:800px;border:0;opacity:.01;pointer-events:none';
  frame.src = new URL(args.src, location.origin).href;
  if (new URL(frame.src).origin !== location.origin) throw new Error('open_url_cross_origin');
  document.body.append(frame);
  return true;
}"""


# Only the app frames PMOS opened itself (ids "pmos-..."), never the site's own.
_CLOSE_FRAMES_JS = """() => {
  const frames = [...document.querySelectorAll('iframe[id^="pmos-"]')];
  frames.forEach(frame => frame.remove());
  return frames.length;
}"""


async def close_owned_frames(page) -> int:
    """Remove PMOS's own app frames so the next call opens them with the current login.

    An app keeps the session it loaded with: after a human re-login, a reused
    frame of it was answered 40302 on every query (Shanxi, 2026-10-31 19:25).
    """
    closed = await page.evaluate(_CLOSE_FRAMES_JS)
    if closed:
        # Let Playwright see the removed frames detach before the next ensure_frame
        # (right after start, a removed frame was still matched: Shanxi r26/r27,
        # 2026-11-01 00:32 and 00:37, preflight component_missing within 2 s).
        await asyncio.sleep(1)
    return closed


# Each tree node with its full label path, from the root section to itself.
_MENU_NODES_JS = """nodes => nodes.map((node, index) => {
  const path = []; let current = node;
  while (current) {
    const label = current.querySelector(':scope > .el-tree-node__content .asideTreeSpan, :scope > .el-tree-node__content .el-tree-node__label, :scope > .el-tree-node__content > .span-ellipsis');
    if (label) path.unshift(label.textContent.trim());
    current = current.parentElement && current.parentElement.closest('.el-tree-node');
  }
  const content = node.querySelector(':scope > .el-tree-node__content');
  const rect = content && content.getBoundingClientRect();
  const visible = !!rect && rect.width > 0 && rect.height > 0 && getComputedStyle(content).visibility !== 'hidden';
  return {index, path, visible, current: node.classList.contains('is-current'),
          expanded: node.classList.contains('is-expanded') || node.getAttribute('aria-expanded') === 'true'};
})"""

# Every entry of the site's left menu tree with its label path (root section
# first) and route, from the tree's own data, as the legacy disclosure-menu
# reader read it. Nothing is clicked or sent.
_MENU_ROUTES_JS = """() => {
  const out = [];
  const walk = (node, parents) => {
    const current = node.currentNode || {};
    if (typeof current.name === 'string' && typeof current.url === 'string')
      out.push({path: [...parents, current.name], url: current.url});
    for (const child of node.nextNode || []) walk(child, [...parents, node.name || current.name || '']);
  };
  for (const root of document.querySelectorAll('.el-tree > .el-tree-node')) {
    const data = root.__vue__ && root.__vue__.node && root.__vue__.node.data;
    if (data) walk(data, []);
  }
  return out;
}"""


def route_of(location: str) -> str:
    """``/pxf-<app>/<page>`` of a frame needle, an app URL or a menu route."""
    tail = location.split("#")[-1].split("?")[0]
    parts = [part for part in tail.split("/") if part]
    return "/" + "/".join(parts[-2:])


async def portal_menu_path(page, route: str) -> list:
    """The label path of the portal menu entry that opens ``route``."""
    entries = [entry for entry in await page.evaluate(_MENU_ROUTES_JS) if route_of(entry["url"]) == route_of(route)]
    if not entries:
        raise Fault("portal_menu_route_missing", "local")
    return entries[0]["path"]


async def portal_frame(page, route: str, timeout: float):
    """The site's own (not PMOS's) app frame once it shows ``route``."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        for frame in page.frames:
            if frame.parent_frame is None or route_of(frame.url) != route_of(route):
                continue
            element = await frame.frame_element()
            if not (await element.get_attribute("id") or "").startswith("pmos-"):
                await settled(frame, timeout)
                return frame
        await asyncio.sleep(0.2)
    raise Fault("portal_frame_open_timeout", "local")


async def reload_frame(frame, timeout: float, settle: float = 3) -> None:
    """Load the app document anew, as after a person's entry from the portal.

    Shanxi 2026-10-31 22:46:57: the person's entry reloaded the 信息披露 app
    document, which exchanged its SSO token three times; its own query 10 s
    later was answered. A hash-only route change (PMOS's first re-entry) kept
    the old document and its queries stayed refused.
    """
    await frame.evaluate("() => { window.__pmosBeforeReload = true; setTimeout(() => location.reload(), 0); return true; }")
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        await asyncio.sleep(0.2)
        try:
            if await frame.evaluate("() => window.__pmosBeforeReload !== true && document.readyState === 'complete'"):
                break
        except Exception:
            continue                      # the document is being replaced
    else:
        raise Fault("portal_frame_reload_timeout", "local")
    await settled(frame, timeout)
    await asyncio.sleep(settle)           # its SSO token exchange (about 1 s on Shanxi)


# A point inside the visible part of a (possibly clipped) long menu label.
_VISIBLE_POINT_JS = """element => {
  const limits = {left: 0, top: 0, right: innerWidth, bottom: innerHeight};
  for (let p = element.parentElement; p; p = p.parentElement) {
    const style = getComputedStyle(p), rect = p.getBoundingClientRect();
    if (/hidden|clip|scroll|auto/.test(style.overflowX)) { limits.left = Math.max(limits.left, rect.left); limits.right = Math.min(limits.right, rect.right); }
    if (/hidden|clip|scroll|auto/.test(style.overflowY)) { limits.top = Math.max(limits.top, rect.top); limits.bottom = Math.min(limits.bottom, rect.bottom); }
  }
  const box = element.getBoundingClientRect();
  for (const rect of element.getClientRects()) {
    const left = Math.max(rect.left, limits.left), right = Math.min(rect.right, limits.right);
    const top = Math.max(rect.top, limits.top), bottom = Math.min(rect.bottom, limits.bottom);
    if (right - left >= 2 && bottom - top >= 2) return {x: (left + right) / 2 - box.left, y: (top + bottom) / 2 - box.top};
  }
  return null;
}"""


def frames_matching(page, needle: str) -> list:
    # A frame just removed (close_owned_frames) can still be listed until its detach event arrives.
    return [frame for frame in page.frames if needle in frame.url and not getattr(frame, "is_detached", lambda: False)()]


# Liaoning: a report opens from the column page's own menu component, as the
# legacy worker's OPEN_REPORT did (chAside.handleNodeClick on the decrypted
# menu entry with this route, and infoCode when given); the page creates the frame itself.
_SITE_MENU_JS = """x => {
  const seen = new Set(), vms = [], queue = [...document.querySelectorAll('*')].map(e => e.__vue__).filter(Boolean);
  while (queue.length) { const v = queue.shift(); if (seen.has(v)) continue; seen.add(v); vms.push(v); queue.push(...(v.$children || [])); }
  const aside = vms.find(v => v.$options && v.$options.name === 'chAside');
  const header = vms.find(v => v.$options && v.$options.name === 'chHeader');
  if (!aside || !header || !header.menuBoxObj || typeof aside.handleNodeClick !== 'function') throw new Error('site_menu_missing');
  const rows = [], walk = list => { for (const r of list || []) { rows.push(r); walk(r.children || r.childList || []); } };
  walk(aside.decryData(JSON.parse(JSON.stringify(Object.values(header.menuBoxObj).flat()))));
  const found = rows.filter(r => r.viewname === x.route && (!x.source || r.infoCode === x.source));
  if (found.length !== 1) throw new Error('site_menu_entry_missing');
  aside.handleNodeClick(found[0], aside.$refs.tree && aside.$refs.tree.getNode(found[0].guid));
  return true;
}"""


async def bring_forward(page) -> None:
    """A hidden tab first comes to the front (as the Anhui legacy ensure_visible).

    Anhui 2026-11-01 12:07: in a background Edge tab the tree's expand and the
    dropdown's transitions never finished; every menu step waited out its
    timeout (menu_navigation_timeout, select_options_missing).
    """
    try:
        state = await page.evaluate("document.visibilityState")
    except Exception:
        state = None                      # bring_to_front reports the real page error
    if state != "visible":
        await page.bring_to_front()


async def ensure_frame(page, request: dict, timeout: float):
    needle = request["frame"]
    found = frames_matching(page, needle)
    if len(found) == 1:
        return found[0]
    if len(found) > 1:
        raise Fault("report_frame_ambiguous", "local")
    await bring_forward(page)
    loop = asyncio.get_running_loop()
    if request.get("site_menu"):
        try:
            await page.evaluate(_SITE_MENU_JS, request["site_menu"])
        except Exception as exc:
            raise Fault("site_menu_entry_missing" if "site_menu_entry_missing" in str(exc) else "site_menu_missing",
                        "local") from None
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            found = frames_matching(page, needle)
            if len(found) == 1:
                await settled(found[0], timeout)
                return found[0]
            await asyncio.sleep(0.2)
        raise Fault("report_frame_open_timeout", "local")
    if request.get("open_url"):
        await page.evaluate(_OPEN_FRAME_JS, {"src": request["open_url"], "id": "pmos-" + request["frame"][-40:]})
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            found = frames_matching(page, needle)
            if len(found) == 1:
                await settled(found[0], timeout)
                return found[0]
            await asyncio.sleep(0.2)
        raise Fault("report_frame_open_timeout", "local")
    labels = request.get("menu")
    if not labels:
        raise Fault("report_frame_missing", "local")
    await select_menu(page, labels, timeout, hover_exit=bool(request.get("menu_hover_exit")))
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        found = frames_matching(page, needle)
        if len(found) == 1:
            await settled(found[0], timeout)
            return found[0]
        await asyncio.sleep(0.2)
    raise Fault("report_frame_missing", "local")


async def _menu_node(page, path: list, timeout: float) -> dict:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        nodes = await page.locator(".el-tree-node").evaluate_all(_MENU_NODES_JS)
        matches = [n for n in nodes if n["path"] == list(path) and n["visible"]]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise Fault("menu_path_ambiguous", "local")
        if loop.time() > deadline:
            raise Fault("menu_navigation_timeout", "local")
        await asyncio.sleep(0.2)


async def select_menu(page, labels: list, timeout: float, *, hover_exit: bool = False, force: bool = False) -> None:
    """Open the menu leaf ``labels`` (root section first) in the site's left tree.

    ``force``: click the leaf even if it is the current one (a re-entry).
    A failure before the leaf is clicked opened no report, so nothing of it was
    sent (``unsent``); after that click the report's own requests may have run.
    """
    await bring_forward(page)
    state = {"leaf_clicked": False}
    try:
        await _select_menu(page, labels, timeout, hover_exit, force, state)
    except Fault as fault:
        if state["leaf_clicked"] or fault.unsent:
            raise
        raise Fault(fault.code, fault.category, http_status=fault.http_status, unsent=True) from None


async def _select_menu(page, labels: list, timeout: float, hover_exit: bool, force: bool, state: dict) -> None:
    nodes = await page.locator(".el-tree-node").evaluate_all(_MENU_NODES_JS)
    if not any(n["path"] == labels[:1] and n["visible"] for n in nodes):
        # The root section is chosen in the horizontal top menu first.
        top = page.locator(".el-menu-item:visible, .el-submenu__title:visible").filter(
            has_text=re.compile(r"^\s*" + re.escape(labels[0]) + r"\s*$"))
        if await top.count() != 1:
            raise Fault("menu_label_not_unique", "local")
        await top.click(timeout=timeout * 1000)
        await asyncio.sleep(0.3)
        # The pointer now rests on the top bar, whose hover menu may cover the tree: as the Anhui
        # legacy (clear_hover_menu) it is moved off first (月度输变电设备检修计划, 中长期市场, 11-01).
        hover_exit = True
    if hover_exit:
        await _leave_hover_menu(page)
    for depth in range(1, len(labels) + 1):
        leaf = depth == len(labels)
        node = await _menu_node(page, labels[:depth], timeout)
        if (node["current"] and not force) if leaf else node["expanded"]:
            continue
        item = page.locator(".el-tree-node").nth(node["index"])
        # Labels: .asideTreeSpan / .el-tree-node__label (Liaoning, Anhui), .span-ellipsis (Shanxi portal).
        target = item.locator(":scope > .el-tree-node__content .asideTreeSpan, :scope > .el-tree-node__content .el-tree-node__label,"
                              " :scope > .el-tree-node__content > .span-ellipsis"
                              if leaf else ":scope > .el-tree-node__content > .el-tree-node__expand-icon")
        if await target.count() != 1:
            raise Fault("menu_control_changed", "local")
        await target.scroll_into_view_if_needed(timeout=timeout * 1000)
        point = await target.evaluate(_VISIBLE_POINT_JS)
        if point is None:
            raise Fault("menu_label_not_visible", "local")
        state["leaf_clicked"] = state["leaf_clicked"] or leaf
        await target.click(position=point, timeout=timeout * 1000)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            node = await _menu_node(page, labels[:depth], timeout)
            if node["current"] if leaf else node["expanded"]:
                break
            if loop.time() > deadline:
                raise Fault("menu_navigation_timeout", "local")
            await asyncio.sleep(0.1)


async def _leave_hover_menu(page) -> None:
    """Move the pointer off an expanded hover menu that covers the tree (never hide its DOM)."""
    menu = page.locator(".menuBox.menuBoxExpanded:visible")
    if await menu.count() == 0:
        return
    point = await menu.first.evaluate("""e => { const r = e.getBoundingClientRect();
      return [[innerWidth - 4, innerHeight - 4], [4, innerHeight - 4], [4, 4], [innerWidth - 4, 4]]
        .find(([x, y]) => x < r.left || x > r.right || y < r.top || y > r.bottom) || null; }""")
    if point is None:
        raise Fault("hover_menu_has_no_outside_point", "local")
    await page.mouse.move(*point)
    await menu.first.wait_for(state="hidden", timeout=3000)


async def settled(frame, timeout: float) -> None:
    try:
        handle = await frame.wait_for_function(_BUSY_JS, timeout=timeout * 1000)
    except Exception:
        raise Fault("page_busy_timeout", "network") from None
    try:
        await handle.dispose()  # else its JSHandle lives as long as the page
    except Exception:
        pass


async def _field(frame, field: dict):
    if field.get("placeholder"):
        # A list gives accepted alternatives, e.g. ["请选择日期", "选择日期"].
        options = field["placeholder"] if isinstance(field["placeholder"], list) else [field["placeholder"]]
        locator = frame.get_by_placeholder(re.compile("^(" + "|".join(map(re.escape, options)) + ")$"))
    else:
        item = frame.locator(".el-form-item").filter(has=frame.get_by_text(field["label"], exact=True))
        locator = item.locator("input")
    # A report's form may still be rendering just after its frame opened (Anhui 2026-11-01 12:5x,
    # queryRqscjjcqfslj): wait for the field; two of them is a changed form at once.
    deadline = asyncio.get_running_loop().time() + 10
    while (count := await locator.count()) == 0 and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.2)
    if count != 1:
        raise Fault("form_field_changed", "local")
    return locator


async def fill(frame, field: dict, timeout: float) -> None:
    locator = await _field(frame, field)
    value = field["value"]
    if await locator.input_value() == value:
        return
    if field.get("kind", "date") == "date":
        await locator.fill(value)
        await locator.press("Enter")
        await locator.press("Tab")
    elif field["kind"] == "select":
        await locator.click()
        option = frame.locator(".el-select-dropdown__item:visible").filter(
            has_text=re.compile(r"^\s*" + re.escape(value) + r"\s*$"))
        deadline = asyncio.get_running_loop().time() + min(timeout, 10)
        while await option.count() != 1:
            if asyncio.get_running_loop().time() > deadline:
                raise Fault("select_option_missing", "local")
            await asyncio.sleep(0.1)
        await option.click()
    else:
        raise Fault("form_field_kind_unknown", "local")
    await settled(frame, timeout)
    if await locator.input_value() != value:
        raise Fault("form_value_not_committed", "local")


_OPTION_ITEMS_JS = """items => items.map(e => {
  const vm = e.__vue__, value = vm ? vm.value : undefined;
  return {label: e.textContent.trim(), value_type: typeof value,
          value: (typeof value === 'string' || (typeof value === 'number' && isFinite(value))) ? value : null};
})"""


# The select's model value (ElSelect's own, where there is one) and the input's text: both must be back.
_SELECT_STATE_JS = """el => {
  for (let n = el; n; n = n.parentElement) {
    const vm = n.__vue__;
    if (vm && vm.$options && vm.$options.name === 'ElSelect')
      return JSON.stringify([vm.value === undefined ? null : vm.value, el.value]);
  }
  return JSON.stringify([null, el.value]);
}"""


async def select_options(frame, field: dict, timeout: float) -> list[dict]:
    """The options of one labelled select as the page lists them (text, value), read without choosing one.

    As the Anhui legacy single-selector discovery: open the dropdown, read
    every item's text and its ElOption value, close it with Escape. Its value
    may end as one of the listed options; anything else fails the read.
    """
    locator = await _field(frame, field)
    # The date just set may make the page reload this select's options and reset its default
    # (Anhui 2026-11-01 12:31, five reports): read only once its state has held for a second.
    before = await _stable_state(locator, hold=1.0, limit=5.0)
    await locator.click()
    items = frame.locator(".el-select-dropdown__item:visible")
    deadline = asyncio.get_running_loop().time() + min(timeout, 10)
    while await items.count() == 0:
        if asyncio.get_running_loop().time() > deadline:
            raise Fault("select_options_missing", "local")
        await asyncio.sleep(0.1)
    found = await items.evaluate_all(_OPTION_ITEMS_JS)
    await locator.press("Escape")
    # A filterable ElSelect shows nothing while open and puts its label back a tick after
    # closing (Anhui 2026-11-01 11:31): read again until value and text are back (3 s).
    settle = asyncio.get_running_loop().time() + 3
    while await items.count() or (after := await locator.evaluate(_SELECT_STATE_JS)) != before:
        if asyncio.get_running_loop().time() > settle:
            break
        await asyncio.sleep(0.1)
    else:
        return found
    if await items.count():
        raise Fault("select_dropdown_not_closed", "local")
    # As the legacy single-selector discovery (selector_inventory, allow_single_root_model_change):
    # the text alone may differ, and the opened select may have taken exactly one of the options
    # it lists (Anhui 12:31: five reports pick one when opened). Nothing is queried either way.
    old_model, new_model = json.loads(before)[0], json.loads(after)[0]
    if old_model == new_model or sum(o.get("value") == new_model for o in found) == 1:
        return found
    raise Fault("select_value_changed_by_reading", "local")


async def _stable_state(locator, *, hold: float, limit: float) -> str:
    loop = asyncio.get_running_loop()
    start = loop.time()
    state = await locator.evaluate(_SELECT_STATE_JS)
    since = start
    while loop.time() - since < hold:
        if loop.time() - start > limit:
            raise Fault("select_value_unsettled", "local")
        await asyncio.sleep(0.25)
        current = await locator.evaluate(_SELECT_STATE_JS)
        if current != state:
            state, since = current, loop.time()
    return state


async def button(frame, text: str):
    spaced = r"\s*".join(map(re.escape, text))
    locator = frame.get_by_role("button", name=re.compile(rf"^\s*{spaced}\s*$"))
    if await locator.count() != 1:
        raise Fault("form_button_changed", "local")
    return locator


# -- article listings (table rows + el-pagination) and article dialogs ------
_CELLS_JS = "rows => rows.map(r => [...r.querySelectorAll('td')].map(c => c.innerText.trim()))"
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


async def list_rows(frame, columns: dict | None = None) -> list[dict]:
    """Visible article rows as {"title", "published"} (published must be a date)."""
    columns = columns or {}
    title, published = columns.get("title", 0), columns.get("published", 1)
    rows = await frame.locator("table:visible tr").evaluate_all(_CELLS_JS)
    return [{"title": r[title], "published": r[published]} for r in rows
            if len(r) > max(title, published) and r[title] and DATE.fullmatch(r[published])]


async def pager(frame) -> dict | None:
    roots = frame.locator(".el-pagination:visible")
    count = await roots.count()
    if count == 0:
        return None
    if count != 1:
        raise Fault("pagination_ambiguous", "local")
    previous, following = roots.locator("button.btn-prev"), roots.locator("button.btn-next")
    if await previous.count() != 1 or await following.count() != 1:
        raise Fault("pagination_controls_changed", "local")
    editor, active = roots.locator(".el-pagination__editor input"), roots.locator(".el-pager .active")
    if await editor.count() == 1:
        number = await editor.input_value()
    else:
        number = (await active.inner_text()).strip() if await active.count() == 1 else ""
    total = re.search(r"共\s*(\d+)\s*条", await roots.inner_text())
    return {"page": int(number) if number.isdecimal() else None, "total": int(total[1]) if total else None,
            "next_disabled": await following.is_disabled(), "previous_disabled": await previous.is_disabled()}


async def goto_page(frame, target: int, timeout: float) -> None:
    """Show listing page ``target`` with the page-number box, or one next/previous click."""
    state = await pager(frame)
    if state is None or state["page"] == target:
        if state is None and target != 1:
            raise Fault("listing_page_unreachable", "local")
        return
    roots = frame.locator(".el-pagination:visible")
    editor = roots.locator(".el-pagination__editor input")
    if await editor.count() == 1 and await editor.is_editable():
        await editor.fill(str(target))
        await editor.press("Enter")
    elif state["page"] is not None and target == state["page"] + 1 and not state["next_disabled"]:
        await roots.locator("button.btn-next").click(timeout=timeout * 1000)
    elif state["page"] is not None and target == state["page"] - 1 and not state["previous_disabled"]:
        await roots.locator("button.btn-prev").click(timeout=timeout * 1000)
    else:
        raise Fault("listing_page_unreachable", "local")
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while ((await pager(frame)) or {}).get("page") != target:
        if loop.time() > deadline:
            raise Fault("listing_page_not_confirmed", "network")
        await asyncio.sleep(0.1)
    await settled(frame, timeout)


async def close_dialog(frame, selector: str, timeout: float) -> None:
    dialogs = frame.locator(selector)
    if await dialogs.count() == 0:
        return
    if await dialogs.count() != 1:
        raise Fault("dialog_ambiguous", "local")
    # Element's header button has a 0x0 box; its close icon is clickable.
    await dialogs.locator(".el-dialog__headerbtn .el-dialog__close").click(timeout=timeout * 1000)
    await dialogs.wait_for(state="hidden", timeout=timeout * 1000)


async def open_record(frame, record: dict, timeout: float, *, columns: dict | None = None,
                      dialog: str = ".el-dialog:visible", max_scan: int = 5):
    """Open one article (title + date) in its dialog; returns the dialog locator.

    New publications push articles down, so a record not on its recorded page
    is looked for on the first ``max_scan`` pages.
    """
    await close_dialog(frame, dialog, timeout)
    wanted = {"title": record["title"], "published": record["published"]}
    pages = [record.get("page") or 1] + [n for n in range(1, max_scan + 1) if n != record.get("page")]
    for number in pages:
        try:
            await goto_page(frame, number, timeout)
        except Fault as fault:
            if fault.code != "listing_page_unreachable":
                raise
            continue
        if wanted in await list_rows(frame, columns):
            break
    else:
        raise Fault("record_not_listed", "site_data")
    row = frame.locator("table:visible tr").filter(has=frame.get_by_text(record["title"], exact=True)).filter(
        has=frame.get_by_text(record["published"], exact=True))
    if await row.count() != 1:
        raise Fault("record_row_ambiguous", "local")
    await row.get_by_text(record["title"], exact=True).click(timeout=timeout * 1000)
    box = frame.locator(dialog)
    try:
        await box.wait_for(timeout=timeout * 1000)
    except Exception:
        raise Fault("record_dialog_missing", "network") from None
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        text = await box.inner_text()
        if record["title"] in text and record["published"] in text:
            return box
        if loop.time() > deadline:
            # The site shows an unavailable article as its bare title.
            bare = re.sub(r"[\s\ue000-\uf8ff]", "", text) == re.sub(r"\s", "", record["title"])
            raise Fault("record_detail_unavailable" if bare else "record_detail_changed", "site_data")
        await asyncio.sleep(0.1)
