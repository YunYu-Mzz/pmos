"""Compare identity enrichment to immutable prior record facts, without raw data."""
from copy import deepcopy
from dataclasses import replace
import hashlib
import unittest

from pmos.contracts import TaskIdentity, canonical
from pmos.queue_compat import LegacyTask
from pmos.recovery.monday import verify_transition


class MondayTransitionTests(unittest.TestCase):
    def setUp(self):
        self.source = "liaoning/state/old_business_queue.json"
        self.monday = LegacyTask("monday-key", "weekly-fixture", "leased", "unknown", True,
            identity=TaskIdentity("liaoning", "offline-fixture", "weekly-fixture",
                {"kind": "week_start_monday", "anchor": "2026-10-05"}),
            blockers=("legacy_active_owner_unsettled",), facts_sha256="a" * 64)
        self.existing = LegacyTask("mapped-key", "daily-fixture", "saved", "saved", True,
            identity=TaskIdentity("liaoning", "offline-fixture", "daily-fixture", {"kind": "day", "label": "2026-10-05"}),
            facts_sha256="b" * 64)
        self.missing = LegacyTask("missing-key", "missing-fixture", "pending", "pending", False,
            blockers=("identity_mapping_required",), facts_sha256="c" * 64)
        records = []
        for row in (self.monday, self.existing, self.missing):
            item = {"legacy_key_sha256": hashlib.sha256(row.legacy_key.encode()).hexdigest(),
                "dataset": row.dataset, "state": row.state, "consumed": row.consumed, "facts_sha256": row.facts_sha256,
                "identity_key_sha256": row.identity.key if row is self.existing else None,
                "record_kind": row.legacy_key, "evidence_refs": ["old-proof"]}
            if row is self.monday:
                item.update(native_period_kind="week_start_monday", native_monday_confirmed=True,
                            native_query_period_sha256=hashlib.sha256(canonical("2026-10-05")).hexdigest())
            records.append(item)
        self.previous = {"queue_evidence": {"baseline_unmapped_record_views": 3,
            "source_views": [{"source_ref": self.source, "records": records}],
            "record_kinds": {
                "monday-key": {"status": "evidence_found_protocol_pending", "remaining_blockers": [
                    "identity_mapping_required", "source_parameter_identity_mapping_required", "legacy_active_owner_unsettled"]},
                "mapped-key": {"status": "mapped_from_retained_evidence", "remaining_blockers": []},
                "missing-key": {"status": "evidence_still_missing", "remaining_blockers": ["identity_mapping_required"]}}}}
        self.current = {self.source: [self.monday, self.existing, self.missing]}

    def test_transition_preserves_prior_mapping_and_reports_hashes_only(self):
        report = verify_transition(self.previous, self.current)
        self.assertEqual((report["phase_three_mappings_preserved"], report["native_monday_identity_views_recovered"],
                          report["remaining_unmapped_record_views"]), (1, 1, 1))
        self.assertEqual(report["native_monday_states_preserved"], {"unknown": 1})
        self.assertEqual(report["native_monday_consumed_preserved"], 1)
        self.assertFalse(report["dispatch_allowed"])
        self.assertNotIn("2026-10-05", str(report))

    def test_anchor_cannot_change_or_be_replaced_with_day(self):
        for period in ({"kind": "week_start_monday", "anchor": "2026-10-12"},
                       {"kind": "day", "label": "2026-10-05"}):
            changed = replace(self.monday, identity=replace(self.monday.identity, period=period))
            with self.subTest(period=period), self.assertRaisesRegex(ValueError, "anchor_or_protection_changed"):
                verify_transition(self.previous, {self.source: [changed, self.existing, self.missing]})

    def test_facts_consumption_or_protection_cannot_be_relaxed(self):
        for changes in ({"state": "pending"}, {"consumed": False}, {"facts_sha256": "d" * 64}, {"blockers": ()}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                verify_transition(self.previous, {self.source: [replace(self.monday, **changes), self.existing, self.missing]})

    def test_prior_mappings_and_unrelated_missing_records_cannot_change(self):
        with self.assertRaisesRegex(ValueError, "existing_mapping_changed"):
            verify_transition(self.previous, {self.source: [self.monday, replace(self.existing, identity=None), self.missing]})
        with self.assertRaisesRegex(ValueError, "unrelated_missing_mapping_changed"):
            verify_transition(self.previous, {self.source: [self.monday, self.existing,
                replace(self.missing, identity=self.existing.identity)]})

    def test_changed_source_or_missing_duplicated_record_cannot_enter_cohort(self):
        with self.assertRaisesRegex(ValueError, "source_views_changed"):
            verify_transition(self.previous, {"other-source": self.current[self.source]})
        with self.assertRaisesRegex(ValueError, "record_facts_changed"):
            verify_transition(self.previous, {self.source: [self.existing, self.missing]})
        previous = deepcopy(self.previous)
        previous["queue_evidence"]["source_views"][0]["records"].append(previous["queue_evidence"]["source_views"][0]["records"][0])
        with self.assertRaisesRegex(ValueError, "duplicate_phase_three_record"):
            verify_transition(previous, self.current)


if __name__ == "__main__":
    unittest.main()
