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


# Each tree node with its full label path, from the root section to itself.
_MENU_NODES_JS = """nodes => nodes.map((node, index) => {
  const path = []; let current = node;
  while (current) {
    const label = current.querySelector(':scope > .el-tree-node__content .asideTreeSpan, :scope > .el-tree-node__content .el-tree-node__label');
    if (label) path.unshift(label.textContent.trim());
    current = current.parentElement && current.parentElement.closest('.el-tree-node');
  }
  const content = node.querySelector(':scope > .el-tree-node__content');
  const rect = content && content.getBoundingClientRect();
  const visible = !!rect && rect.width > 0 && rect.height > 0 && getComputedStyle(content).visibility !== 'hidden';
  return {index, path, visible, current: node.classList.contains('is-current'),
          expanded: node.classList.contains('is-expanded') || node.getAttribute('aria-expanded') === 'true'};
})"""

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
    return [frame for frame in page.frames if needle in frame.url]


async def ensure_frame(page, request: dict, timeout: float):
    needle = request["frame"]
    found = frames_matching(page, needle)
    if len(found) == 1:
        return found[0]
    if len(found) > 1:
        raise Fault("report_frame_ambiguous", "local")
    loop = asyncio.get_running_loop()
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


async def select_menu(page, labels: list, timeout: float, *, hover_exit: bool = False) -> None:
    """Open the menu leaf ``labels`` (root section first) in the site's left tree."""
    nodes = await page.locator(".el-tree-node").evaluate_all(_MENU_NODES_JS)
    if not any(n["path"] == labels[:1] and n["visible"] for n in nodes):
        # The root section is chosen in the horizontal top menu first.
        top = page.locator(".el-menu-item:visible, .el-submenu__title:visible").filter(
            has_text=re.compile(r"^\s*" + re.escape(labels[0]) + r"\s*$"))
        if await top.count() != 1:
            raise Fault("menu_label_not_unique", "local")
        await top.click(timeout=timeout * 1000)
        await asyncio.sleep(0.3)
    if hover_exit:
        await _leave_hover_menu(page)
    for depth in range(1, len(labels) + 1):
        leaf = depth == len(labels)
        node = await _menu_node(page, labels[:depth], timeout)
        if node["current"] if leaf else node["expanded"]:
            continue
        item = page.locator(".el-tree-node").nth(node["index"])
        target = item.locator(":scope > .el-tree-node__content .asideTreeSpan, :scope > .el-tree-node__content .el-tree-node__label"
                              if leaf else ":scope > .el-tree-node__content > .el-tree-node__expand-icon")
        if await target.count() != 1:
            raise Fault("menu_control_changed", "local")
        await target.scroll_into_view_if_needed(timeout=timeout * 1000)
        point = await target.evaluate(_VISIBLE_POINT_JS)
        if point is None:
            raise Fault("menu_label_not_visible", "local")
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
        await frame.wait_for_function(_BUSY_JS, timeout=timeout * 1000)
    except Exception:
        raise Fault("page_busy_timeout", "network") from None


async def _field(frame, field: dict):
    if field.get("placeholder"):
        # A list gives accepted alternatives, e.g. ["请选择日期", "选择日期"].
        options = field["placeholder"] if isinstance(field["placeholder"], list) else [field["placeholder"]]
        locator = frame.get_by_placeholder(re.compile("^(" + "|".join(map(re.escape, options)) + ")$"))
    else:
        item = frame.locator(".el-form-item").filter(has=frame.get_by_text(field["label"], exact=True))
        locator = item.locator("input")
    if await locator.count() != 1:
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
