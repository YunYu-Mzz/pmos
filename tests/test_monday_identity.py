"""Native Monday query anchors are neither inferred weeks nor daily jobs."""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

from pmos.contracts import TaskIdentity, normalized_period
from pmos.ledger import Ledger
from pmos.queue_compat import QueueContext, decode_liaoning, import_shadow
from pmos.recovery.liaoning import _identity


class MondayIdentityTests(unittest.TestCase):
    def identity(self, period=None, **changes):
        return replace(TaskIdentity("liaoning", "offline-fixture", "weekly-fixture",
            period or {"kind": "week_start_monday", "anchor": "2026-10-05"}, {"type": 1}), **changes)

    def test_label_and_anchor_roundtrip_share_one_query_identity(self):
        first = self.identity()
        second = self.identity({"kind": "week_start_monday", "label": "2026-10-05"})
        third = self.identity({"kind": "week_start_monday", "label": "2026-10-05", "anchor": "2026-10-05"})
        self.assertEqual(first.key, second.key)
        self.assertEqual(first.key, third.key)
        document = json.loads(json.dumps(first.document()))
        self.assertEqual(document["period"], {"kind": "week_start_monday", "anchor": "2026-10-05"})
        document.pop("version")
        self.assertEqual(TaskIdentity(**document).key, first.key)

    def test_non_monday_and_noncanonical_dates_are_rejected(self):
        for anchor in ("2026-10-06", "2026-10-04", "2026-02-30", "20261005", "2026-W41-1",
                       "2026-10-05T00:00:00+08:00", "2026-1-05", "", None, True):
            with self.subTest(anchor=anchor), self.assertRaises(ValueError):
                self.identity({"kind": "week_start_monday", "anchor": anchor}).key

    def test_explicit_boundaries_or_conflicting_aliases_cannot_enter_anchor_key(self):
        for extra in ({"start": "2026-10-05"}, {"end": "2026-10-11"}, {"label": "2026-10-12"},
                      {"timezone": "Asia/Shanghai"}, {"iso_week": "2026-W41"}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                self.identity({"kind": "week_start_monday", "anchor": "2026-10-05", **extra}).key

    def test_year_boundary_is_preserved_without_deriving_iso_week(self):
        period = normalized_period({"kind": "week_start_monday", "anchor": "2025-12-29"})
        self.assertEqual(period, {"kind": "week_start_monday", "anchor": "2025-12-29"})
        self.assertEqual(set(period), {"kind", "anchor"})

    def test_old_identity_hashes_are_unchanged_and_query_kinds_are_distinct(self):
        periods = [({"kind": "day", "label": "2026-10-05"}, "7511c359f631c877ea1a65243c8e7ad3e523886c018c24b87ac295ed9da28bf7"),
            ({"kind": "month", "label": "2026-10"}, "e4a46f35893768f2ccd87d946ddf8b1d923defb70936575f3cb779946fb1476e"),
            ({"kind": "year", "label": "2026"}, "bfd72f8bb09b5c30f92e1b916cb536f347fbc0e75985a7e2c987a731501e7687"),
            ({"kind": "week", "start": "2026-10-05", "end": "2026-10-11"}, "9082ac2fb2736b54bc897ac5676e725871e328722b1669228d7d4213e6b74991"),
            ({"kind": "range", "start": "2026-10-05", "end": "2026-10-11"}, "3d23c78cef475edbea0091820a3f98d23e9080d98f71ac9e9ca6aae53a470364"),
            ({"kind": "snapshot", "anchor": "retained-version"}, "be85a38c6a6e0b820762c257218cf9c9a79de95bcc5e701f75ffe8ee70016dc5")]
        for period, expected in periods:
            self.assertEqual(self.identity(period).key, expected)
        day = self.identity({"kind": "day", "label": "2026-10-05"})
        snapshot = self.identity({"kind": "snapshot", "anchor": "2026-10-05"})
        self.assertEqual(len({self.identity().key, day.key, snapshot.key, *(sha for _, sha in periods)}), 8)

    def test_monday_namespaces_retain_typed_scope_account_page_and_operation(self):
        first = self.identity()
        others = [replace(first, account_alias="other-account"), replace(first, province="shanxi"),
                  replace(first, dataset="other-source"), replace(first, page=1), replace(first, operation="export"),
                  replace(first, dimensions={"type": "1"}),
                  replace(first, period={"kind": "week_start_monday", "anchor": "2026-10-12"})]
        self.assertEqual(len({first.key, *(row.key for row in others)}), len(others) + 1)

    def test_recovery_requires_declared_native_kind_and_exact_parameters(self):
        job = {"period": "2026-10-05", "params": {"mdate": "2026-10-05", "type": 1}}
        rule = {"period_field": "mdate", "selection_field": "type", "period_kind": "week_start_monday"}
        row, reason = _identity("fixture", job, rule, "week_start_monday")
        self.assertIsNone(reason)
        self.assertEqual(row.dimensions, {"type": 1})
        first_page, _ = _identity("fixture", {**job, "page": 1}, rule, "week_start_monday")
        self.assertEqual(row.key, first_page.key)
        cases = [(job, None), (job, {**rule, "period_kind": "day"}),
                 ({**job, "params": {"mdate": "2026-10-12", "type": 1}}, rule),
                 ({**job, "params": {"mdate": "2026-10-05"}}, rule),
                 ({**job, "params": {**job["params"], "extra": True}}, rule),
                 ({**job, "params": None}, rule),
                 ({**job, "page": 2}, rule), ({**job, "page": True}, rule), ({**job, "page": 0}, rule),
                 ({**job, "snapshot_date": "2026-10-05"}, rule),
                 ({**job, "_explicit_engine_outcome_date": True}, rule)]
        for candidate, contract in cases:
            with self.subTest(job=candidate, rule=contract):
                identity, _ = _identity("fixture", candidate, contract, "week_start_monday")
                self.assertIsNone(identity)

    def test_old_queue_unknown_and_hold_survive_monday_mapping_and_shadow(self):
        context = QueueContext("liaoning", "offline-fixture", period_kind="week_start_monday")
        queue = {"hold": "old-global-hold", "sources": {"fixture": {"hold": "old-source-hold",
            "pending": [{"key": "old-key", "dataset": "fixture", "period": "2026-10-05",
                         "params": {"mdate": "2026-10-05"}}], "outcomes": {}}},
            "active": {"key": "old-key", "dataset": "fixture"}}
        rule = {"fixture": {"period_field": "mdate", "paginated": False}}
        row = decode_liaoning(queue, context, source_contracts=rule)[0]
        self.assertEqual((row.state, row.consumed), ("unknown", True))
        self.assertEqual(row.identity.document()["period"], {"kind": "week_start_monday", "anchor": "2026-10-05"})
        self.assertTrue({"legacy_global_hold", "legacy_source_hold", "legacy_active_owner_unsettled"}.issubset(row.blockers))
        with tempfile.TemporaryDirectory() as folder:
            ledger = Ledger(Path(folder) / "new-shadow.sqlite3")
            try:
                result = import_shadow(ledger, [row], context, source_id="old-snapshot", snapshot_sha256="a" * 64)
                self.assertFalse(result["dispatch_allowed"])
                self.assertEqual(ledger.summary(), {})
                with self.assertRaisesRegex(ValueError, "legacy_shadow_requires_owner_handoff"):
                    ledger.begin("collect", "fixture", "2026-10-05", identity=row.identity)
            finally:
                ledger.close()


if __name__ == "__main__":
    unittest.main()
