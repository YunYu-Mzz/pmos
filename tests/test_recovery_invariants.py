from dataclasses import replace
import unittest

from pmos.contracts import TaskIdentity
from pmos.queue_compat import LegacyTask
from pmos.recovery.invariants import validate_queue_recovery


class RecoveryConservationTests(unittest.TestCase):
    def setUp(self):
        self.old = LegacyTask("old-key", "technical-source", "leased", "unknown", True,
                              blockers=("identity_mapping_required", "legacy_active_owner_unsettled"),
                              facts_sha256="a" * 64)
        self.identity = TaskIdentity("liaoning", "offline-shadow-review", "technical-source",
                                     {"kind": "day", "label": "2026-10-01"}, {})
        self.new = replace(self.old, identity=self.identity, blockers=("legacy_active_owner_unsettled",))

    def check_change_rejected(self, row, message):
        with self.assertRaisesRegex(ValueError, message):
            validate_queue_recovery({"retained-source": [self.old]}, {"retained-source": [row]})

    def test_identity_can_be_filled_but_inflight_stays_consumed_unknown(self):
        result = validate_queue_recovery({"retained-source": [self.old]}, {"retained-source": [self.new]})
        self.assertEqual((result["identities_recovered"], result["consumed"], result["states"]),
                         (1, 1, {"unknown": 1}))
        self.assertFalse(result["dispatch_allowed"])
        self.assertNotIn("old-key", str(result))

    def test_state_consumption_facts_and_dataset_cannot_change(self):
        for fields in ({"state": "pending"}, {"consumed": False}, {"legacy_state": "pending"},
                       {"facts_sha256": "b" * 64}, {"dataset": "other-source"}):
            with self.subTest(fields=fields):
                self.check_change_rejected(replace(self.new, **fields), "legacy_state_or_facts_changed")

    def test_hold_cannot_be_removed_or_mapping_cleared_without_identity(self):
        self.check_change_rejected(replace(self.new, blockers=()), "legacy_protection_changed")
        self.check_change_rejected(replace(self.old, blockers=("legacy_active_owner_unsettled",)),
                                   "legacy_protection_changed")

    def test_existing_identity_source_and_key_multiplicity_cannot_change(self):
        with self.assertRaisesRegex(ValueError, "existing_identity_changed"):
            validate_queue_recovery({"s": [self.new]}, {"s": [replace(self.new, identity=None)]})
        with self.assertRaisesRegex(ValueError, "legacy_source_views_changed"):
            validate_queue_recovery({"s": [self.old]}, {"other": [self.new]})
        with self.assertRaisesRegex(ValueError, "legacy_keys_or_multiplicity_changed"):
            validate_queue_recovery({"s": [self.old]}, {"s": [self.new, self.new]})


if __name__ == "__main__":
    unittest.main()
