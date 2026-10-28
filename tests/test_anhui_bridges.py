"""Phase-five bridges preserve fixed old facts while reading newer originals."""
from copy import deepcopy
from datetime import datetime
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from pmos.contracts import canonical
from pmos.faults import Fault
from pmos.queue_compat import QueueContext, decode_jobs
from pmos.recovery import anhui_bridges as bridges
from pmos.recovery.invariants import validate_queue_recovery


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


class BridgeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve() / "anhui"
        self.root.mkdir()
        self.phase4 = {"remaining_article_records": [], "references": [], "baseline_refs": [],
            "bindings": {"remaining_captures": 3}, "candidates": {"remaining_candidates": 52}}
        self.phase3 = {"queue_views": {}}
        rules = {}
        for reference in bridges.RULES:
            rules[reference] = self.write(reference, b"# Synthetic reviewed rule, never executed.\n")
        patcher = patch.object(bridges, "RULES", rules)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, relative, raw):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        return sha(raw)

    def save(self, relative, value):
        return self.write(relative, json.dumps(value, ensure_ascii=False).encode())

    def declare(self, relative, digest):
        self.phase4["references"].append({"path": "anhui/" + relative, "sha256": digest})

    def group(self, dataset="notice", statuses=("pending",), snapshot_statuses=None):
        menu = ["synthetic", dataset]
        jobs = {}
        for index, status in enumerate(statuses):
            title, date = f"Synthetic article {index}", f"2026-09-{index + 1:02d}"
            key = bridges._article_key(title, date)
            jobs[key] = {"title": title, "publication_date": date, "period": "2026-08", "status": status}
        queue = {"dataset": dataset, "menu_path": menu, "jobs": jobs}
        reference = f"state/server_publication_queue_{dataset}.json"
        digest = self.save(reference, queue)
        self.declare(reference, digest)
        snapshot_ref = bridges.prior.SNAPSHOTS[0] + "/" + Path(reference).name
        snapshot = deepcopy(queue)
        if snapshot_statuses is not None:
            for job, status in zip(snapshot["jobs"].values(), snapshot_statuses):
                job["status"] = status
        self.declare(snapshot_ref, self.save(snapshot_ref, snapshot))
        sid = "q" + str(len(self.phase3["queue_views"]))
        namespace = str(self.root / reference)
        self.phase3["queue_views"][sid] = {"source_ref": namespace}
        rows = decode_jobs(queue, QueueContext("anhui", "offline-shadow-review", dataset), attempts=None)
        for task in rows:
            self.phase4["remaining_article_records"].append({"source_view_id": sid,
                "source_ref_sha256": sha(namespace.encode()), "legacy_key_sha256": sha(task.legacy_key.encode()),
                "dataset": dataset, "legacy_state": task.legacy_state, "state": task.state,
                "consumed": task.consumed, "facts_sha256": task.facts_sha256,
                "protective_blockers": sorted(set(task.blockers) - {"identity_mapping_required"}),
                "menu_scope_sha256": sha(canonical(menu))})
        return queue, reference, snapshot_ref

    def listing(self, queue, *, guid_mode="unique", menu=None):
        dataset = queue["dataset"]
        rows = []
        for index, job in enumerate(queue["jobs"].values()):
            millis = int(datetime.fromisoformat(job["publication_date"]).replace(tzinfo=ZoneInfo("Asia/Shanghai")).timestamp() * 1000)
            row = {"title": job["title"], "validDate": millis,
                "guid": f"synthetic-guid-{dataset}-{index}" if guid_mode != "empty" else "",
                "informationId": f"synthetic-information-{dataset}-{index}"}
            rows.append(row)
            if guid_mode == "conflict":
                rows.append({**row, "guid": "another-synthetic-guid"})
        source = f"data/2026/native_listing_sources/{dataset}/page-1/source.json"
        source_sha = self.save(source, {"status": 0, "data": {"pageNum": 1, "pageSize": 20, "total": len(rows), "list": rows}})
        parsed = f"data/2026/native_listing_sources/{dataset}/page-1/parsed.json"
        parsed_sha = self.save(parsed, {"dataset": dataset, "menu_path": menu if menu is not None else queue["menu_path"],
            "source_path": source, "source_sha256": source_sha, "source_rows": len(rows), "page": "1",
            "all_ui_listing_rows_matched": True, "publication_date_is_not_statistical_period": True,
            "publication_date_field": "validDate", "articles": [{"title": row["title"],
                "publication_date": queue["jobs"][bridges._article_key(row["title"],
                    datetime.fromtimestamp(row["validDate"] / 1000, ZoneInfo("Asia/Shanghai")).date().isoformat())]["publication_date"]}
                for row in rows]})
        marker = f"state/server_native_listing_sources_{dataset}.json"
        self.save(marker, {"1": {"source_path": source, "source_sha256": source_sha,
            "parsed_path": parsed, "parsed_sha256": parsed_sha, "source_rows": len(rows), "page": "1"}})
        return source, parsed, marker

    def report(self):
        return bridges.recover(self.root, phase4=self.phase4, phase3=self.phase3)

    def records(self, before=False):
        function = bridges.baseline_queue_records if before else bridges.resolve_queue_records
        return function(self.root, phase4=self.phase4, phase3=self.phase3)

    def test_nine_snapshot_records_and_one_exact_record_in_changed_queue_keep_old_states(self):
        groups = [self.group("alpha", ("captured",) * 3 + ("pending",) * 4),
                  self.group("beta", ("saved", "saved"), snapshot_statuses=("saved", "pending")),
                  self.group("gamma", ("pending",))]
        for queue, reference, _ in groups:
            self.listing(queue)
            changed = deepcopy(queue)
            for job in changed["jobs"].values():
                if job["status"] == "pending":
                    job["status"] = "saved"
            changed["jobs"]["new-collected-key"] = {"title": "New collector row", "publication_date": "2026-10-01", "status": "saved"}
            self.save(reference, changed)
        before, after = self.records(True), self.records()
        check = validate_queue_recovery(before, after)
        self.assertEqual((check["record_views"], check["identities_recovered"], check["unmapped_after"]), (10, 10, 0))
        self.assertEqual(check["states"], {"pending": 5, "saved": 5})
        self.assertEqual(check["consumed"], 5)
        report = self.report()
        modes = [row["cohort_proof"] for row in report["queue_identity_bridges"]]
        self.assertEqual(modes.count("phase4_committed_snapshot_exact_record"), 9)
        self.assertEqual(modes.count("exact_phase4_record_in_changed_queue"), 1)
        self.assertEqual(len(report["drift_groups"]["queue_documents_vs_phase4"]), 3)

    def test_valid_identity_is_undated_article_and_report_contains_no_real_title_or_id(self):
        queue, _, _ = self.group()
        self.listing(queue)
        rows = next(iter(self.records().values()))
        self.assertEqual(rows[0].identity.document()["period"], {"kind": "snapshot", "anchor": "undated"})
        self.assertEqual(rows[0].identity.operation, "article")
        self.assertIn("attempt_index_required", rows[0].blockers)
        encoded = json.dumps(self.report())
        for text in ["Synthetic article 0", "synthetic-guid-notice-0", "synthetic-information-notice-0"]:
            self.assertNotIn(text, encoded)

    def test_changed_current_scope_cannot_replace_missing_committed_old_scope(self):
        queue, reference, snapshot = self.group()
        self.listing(queue, menu=["foreign", "menu"])
        queue["menu_path"] = ["foreign", "menu"]
        self.save(reference, queue)
        self.save(snapshot, queue)
        with self.assertRaisesRegex(ValueError, "committed_old_menu"):
            self.report()

    def test_wrong_committed_snapshot_sha_is_not_a_scope_anchor(self):
        queue, reference, snapshot = self.group()
        self.listing(queue)
        for row in self.phase4["references"]:
            if row["path"].endswith(snapshot):
                row["sha256"] = "f" * 64
        queue["jobs"][next(iter(queue["jobs"]))]["status"] = "saved"
        self.save(reference, queue)
        with self.assertRaisesRegex(ValueError, "committed_old_menu"):
            self.report()

    def test_cross_namespace_commitment_is_rejected(self):
        queue, _, _ = self.group()
        self.listing(queue)
        self.phase4["remaining_article_records"][0]["source_ref_sha256"] = "f" * 64
        with self.assertRaisesRegex(ValueError, "namespace_or_scope"):
            self.report()

    def test_wrong_listing_menu_never_supplies_identity(self):
        queue, _, _ = self.group()
        self.listing(queue, menu=["foreign", "menu"])
        with self.assertRaisesRegex(ValueError, "listing_chain_invalid"):
            self.report()

    def test_source_sha_mismatch_blocks_bridge(self):
        queue, _, _ = self.group()
        source, _, _ = self.listing(queue)
        self.write(source, b"{}")
        with self.assertRaisesRegex(ValueError, "sha256_changed"):
            self.report()

    def test_missing_guid_stays_unmapped(self):
        queue, _, _ = self.group()
        self.listing(queue, guid_mode="empty")
        report = self.report()
        self.assertEqual(report["summary"]["article_identities_recovered"], 0)
        self.assertEqual(report["summary"]["article_identities_remaining"], 1)
        validate_queue_recovery(self.records(True), self.records())

    def test_conflicting_guids_stay_unmapped(self):
        queue, _, _ = self.group()
        self.listing(queue, guid_mode="conflict")
        report = self.report()
        self.assertEqual(report["summary"]["article_identities_recovered"], 0)
        self.assertEqual(report["remaining_article_records"][0]["candidate_identity_count"], 2)

    def test_current_success_without_an_exact_old_record_cannot_reconstruct_pending(self):
        queue, reference, snapshot = self.group()
        self.listing(queue)
        saved = deepcopy(queue)
        saved["jobs"][next(iter(saved["jobs"]))]["status"] = "saved"
        self.save(reference, saved)
        self.save(snapshot, saved)
        # This is a committed same-menu archive, but its old fact does not
        # match the fixed pending commitment. Do not synthesize a downgrade.
        for row in self.phase4["references"]:
            if row["path"].endswith(snapshot):
                row["sha256"] = sha((self.root / snapshot).read_bytes())
        with self.assertRaisesRegex(ValueError, "record_facts_not_reconstructed"):
            self.report()

    def test_business_denial_with_rows_is_not_an_identity_source(self):
        queue, _, _ = self.group()
        source, parsed, marker = self.listing(queue)
        payload = json.loads((self.root / source).read_text())
        payload["status"] = 40302
        source_sha = self.save(source, payload)
        parsed_value = json.loads((self.root / parsed).read_text())
        parsed_value["source_sha256"] = source_sha
        parsed_sha = self.save(parsed, parsed_value)
        index = json.loads((self.root / marker).read_text())
        index["1"].update(source_sha256=source_sha, parsed_sha256=parsed_sha)
        self.save(marker, index)
        with self.assertRaises(Fault):
            self.report()

    def test_duplicate_old_key_commitment_fails_closed(self):
        queue, _, _ = self.group()
        self.listing(queue)
        self.phase4["remaining_article_records"].append(deepcopy(self.phase4["remaining_article_records"][0]))
        with self.assertRaisesRegex(ValueError, "duplicate_phase4"):
            self.report()

    def test_baseline_end_recheck_rejects_phase4_changed_during_bridge(self):
        queue, _, _ = self.group()
        self.listing(queue)
        folder = self.root.parent / "pmos/evidence"
        folder.mkdir(parents=True)
        phase3_raw = json.dumps(self.phase3).encode()
        (folder / "anhui_phase3_recovery.json").write_bytes(phase3_raw)
        phase4 = deepcopy(self.phase4)
        phase4["baseline_refs"] = [{"path": "pmos/evidence/anhui_phase3_recovery.json", "sha256": sha(phase3_raw)}]
        phase4_raw = json.dumps(phase4).encode()
        (folder / "anhui_phase4_remaining.json").write_bytes(phase4_raw)
        original = bridges._bridges
        def mutate(*args):
            result = original(*args)
            (folder / "anhui_phase4_remaining.json").write_bytes(phase4_raw + b" ")
            return result
        with patch.object(bridges, "PHASE4_SHA256", sha(phase4_raw)), patch.object(bridges, "_bridges", mutate):
            with self.assertRaisesRegex(ValueError, "baseline_sha256_changed"):
                bridges.recover(self.root)


if __name__ == "__main__":
    unittest.main()
