"""FineReport daily tables (Liaoning's report engine lane): the answer's checks, no browser and no request.

Ported from the legacy worker's server_engine_collection (2026-10-23 to 11-01, about 760 days saved). A day is three
requests to one path; the last answer holds a 12 x 16 table of 96 times and values in ``htmlTag``.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser

PATH = "/px-basesystem-reportform/decision/view/form"
MAPPING_PATH = "/px-settlement-infpubquery/spotgoods/queryIpSpotgoodsInfoBytableName"
# (cmd, op, method), in the order the page's own form asks them after parameterCommit.
STAGES = (("parameters_d", "fr_dialog", "POST"), ("load_content", "fr_form", "GET"), ("load_report_content", "fr_form", "POST"))
# FineReport's own error page, HTTP 200, when its session ended (idle 66 min and more; never within 11.5 min).
SESSION_END = b"Dec-Session_Lifecycle_End"


class _Cells(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows, self.row, self.cell, self.skip = [], None, None, 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
        if tag == "tr":
            self.row = []
        if tag in ("td", "th"):
            self.cell = []

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.skip = max(0, self.skip - 1)
        if tag in ("td", "th") and self.cell is not None:
            if self.row is not None:
                self.row.append("".join(self.cell).strip())
            self.cell = None
        if tag == "tr" and self.row is not None:
            self.rows.append(self.row)
            self.row = None

    def handle_data(self, data):
        if self.cell is not None and not self.skip:
            self.cell.append(data)


def daily_values(body) -> list[dict]:
    """The day's 96 {time, value} (value None when blank), [] for the site's blank report. ValueError: a changed table."""
    if (not isinstance(body, dict) or type(body.get("currentPage")) is not int or body["currentPage"] != 1
            or body.get("totalPage") != 1 or not isinstance(body.get("htmlTag"), str)):
        raise ValueError("finereport_table_pagination_changed")
    parser = _Cells()
    parser.feed(body["htmlTag"])
    rows = [row for row in parser.rows if any(row)]
    # The blank report the site answers for a day it has no data for yet (seen 2026-10-23).
    if parser.rows == [[""], [""]] and 'class="pageContentDIV"' in body["htmlTag"] and 'class="x-table ' in body["htmlTag"]:
        return []
    if len(rows) != 12 or any(len(row) != 16 for row in rows):
        raise ValueError("finereport_daily_grid_changed")
    pairs = []
    for index in range(0, 12, 2):
        pairs.extend(zip(rows[index], rows[index + 1]))
    if [time for time, _ in pairs] != [f"{i // 4:02d}:{i % 4 * 15:02d}" for i in range(1, 97)]:
        raise ValueError("finereport_daily_times_changed")
    values = []
    for time, value in pairs:
        if value == "":
            values.append({"time": time, "value": None})
            continue
        try:
            number = Decimal(value)
        except InvalidOperation:
            raise ValueError("finereport_value_not_numeric") from None
        if not number.is_finite():
            raise ValueError("finereport_value_not_finite")
        values.append({"time": time, "value": value})
    return values
