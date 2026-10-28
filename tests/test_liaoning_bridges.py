"""Synthetic tests for bounded legacy commitment preimages; no live data."""
import ast
from collections import Counter
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pmos.contracts import TaskIdentity, canonical
from pmos.queue_compat import LegacyTask
from pmos.recovery import liaoning_bridges as bridges
from pmos.recovery.liaoning import _Reader, _sha


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve() / "liaoning"
        self.root.mkdir()
        self.reader = _Reader(self.root)
        self.spec = {"dataset": "example", "number": "005", "date_field": "mdate",
            "selection_field": "name", "selected_label": None, "period": "day", "kind": "report"}
        self.periods = ["2026-01-01", "2026-01-02", "2026-01-03"]

    def tearDown(self):
        self.temp.cleanup()

    def write(self, relative, value):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(canonical(value))
        return path

    def domain(self):
        jobs = [bridges._job(self.spec, period, "synthetic-choice")[0] for period in self.periods]
        prior = {jobs[0]["key"]: {"outcome": "saved", "raw_sha256": "b" * 64}}
        source = {"dataset": "example", "kind": "report", "period": "day", "number": "005",
            "method": "POST", "endpoint": bridges.PREFIX + "pdblnReport005",
            "route": "/pxf-dif-settlement-outnetpub/pdbln/report/PdblnReport005",
            "original_journal": "server_advanced_tasks_20261016.json", "prior_outcomes": prior,
            "source_evidence_sha256": "c" * 64, "selections": ["synthetic-choice"],
            "proofs": [{"selection": "synthetic-choice", "sha256": "b" * 64}]}
        manifest = self.write(bridges.MANIFEST, {"sources": {"example": source}})
        self.reader.raw(manifest)
        journal = {"selections": ["synthetic-choice"], "verified_selections": ["synthetic-choice"],
            "source_evidence_sha256": "c" * 64, "outcomes": prior, "tasks": {jobs[0]["key"]: jobs[0]}}
        path = self.write("state/server_verified_expansion_20261023/server_advanced_tasks_20261016.json",
                          {"datasets": {"example": journal}})
        anchors = {str(path.relative_to(self.root.parent)): _sha(path.read_bytes())}
        group = {"pending": [jobs[1]], "outcomes": {jobs[2]["key"]: {"outcome": "empty", "sha256": "a" * 64}}}
        return source, journal, group, jobs[2]["key"], anchors

    def test_complete_controlled_domain_has_one_old_preimage(self):
        source, _, group, key, anchors = self.domain()
        hits, report = bridges._validate_domain(self.reader, self.spec, source, group, self.periods, {key}, anchors)
        job, preimage = hits[key]
        self.assertEqual(preimage, {"period": "2026-01-03", "selection": "synthetic-choice"})
        self.assertEqual(job["params"], {"mdate": "2026-01-03", "name": "synthetic-choice"})
        self.assertEqual(report["candidate_count"], 3)
        self.assertEqual((report["frozen_pending_count"], report["frozen_prior_outcome_count"],
                          report["frozen_current_outcome_count"]), (1, 1, 1))

    def test_pending_job_extra_even_empty_parameter_is_rejected(self):
        source, _, group, key, anchors = self.domain()
        group["pending"][0]["params"]["unknown_empty_filter"] = ""
        with self.assertRaisesRegex(ValueError, "unmapped_or_changed_native_parameters"):
            bridges._validate_domain(self.reader, self.spec, source, group, self.periods, {key}, anchors)

    def test_frontier_must_equal_full_closed_writer_domain(self):
        source, _, group, key, anchors = self.domain()
        group["pending"] = []
        with self.assertRaisesRegex(ValueError, "not_equal_frozen"):
            bridges._validate_domain(self.reader, self.spec, source, group, self.periods, {key}, anchors)

    def test_endpoint_method_and_number_cannot_be_guessed(self):
        source, _, group, key, anchors = self.domain()
        for field, value in (("method", "GET"), ("number", "006"), ("endpoint", bridges.PREFIX + "pdblnReport006")):
            changed = {**source, field: value}
            with self.assertRaisesRegex(ValueError, "endpoint_method_or_report_rule_changed"):
                bridges._validate_domain(self.reader, self.spec, changed, group, self.periods, {key}, anchors)

    def test_integer_and_string_choices_are_distinct_preimages(self):
        choices = [1, "1"]
        journal = {"selections": choices, "verified_selections": choices}
        source = {"selections": choices}
        self.assertEqual(bridges._choices(self.spec, source, journal), choices)
        keys, _ = bridges._enumerate(self.spec, self.periods[:1], choices, set())
        self.assertEqual(len(keys), 2)
        self.assertNotEqual(bridges._job(self.spec, self.periods[0], 1)[0]["key"],
                            bridges._job(self.spec, self.periods[0], "1")[0]["key"])
        with self.assertRaisesRegex(ValueError, "must_be_disclosed"):
            bridges._choices(self.spec, {"selections": [True]}, {"selections": [True], "verified_selections": [True]})

    def test_verified_choices_must_be_typed_subset_of_disclosed_domain(self):
        with self.assertRaisesRegex(ValueError, "must_be_disclosed"):
            bridges._choices(self.spec, {"selections": [1]}, {"selections": ["1"], "verified_selections": [1]})
        with self.assertRaisesRegex(ValueError, "typed_verified_selection_domain_not_retained"):
            bridges._choices(self.spec, {"selections": ["1"]}, {"selections": [1], "verified_selections": [1]})
        no_dimension = {**self.spec, "selection_field": None}
        with self.assertRaisesRegex(ValueError, "unexpected_selection"):
            bridges._choices(no_dimension, {"selections": ["synthetic"]},
                             {"selections": ["synthetic"], "verified_selections": ["synthetic"]})

    def test_zero_match_never_produces_identity(self):
        with self.assertRaisesRegex(ValueError, "not_unique_or_missing"):
            bridges._enumerate(self.spec, self.periods, ["synthetic-choice"], {"missing-old-key"})

    def test_multiple_or_colliding_candidates_are_rejected(self):
        with patch.object(bridges, "_legacy_key", return_value="same-old-key"):
            with self.assertRaisesRegex(ValueError, "duplicate_or_colliding"):
                bridges._enumerate(self.spec, self.periods, ["synthetic-choice"], {"same-old-key"})
        with self.assertRaisesRegex(ValueError, "bounded_identity_preimage"):
            bridges._enumerate(self.spec, self.periods * bridges.MAX_CANDIDATES, ["synthetic-choice"], set())

    def test_calendar_uses_saved_canonical_seed_not_current_date(self):
        self.assertEqual(bridges._calendar("2026-01-02"), self.periods[:2])
        for seed in (None, "2027-01-01", "2026-1-2", "2026-02-30"):
            with self.assertRaises(ValueError):
                bridges._calendar(seed)

    def test_whole_writer_ast_is_pinned_without_importing_it(self):
        raw = b"def task_key(dataset, identity):\n    return dataset\n"
        node = ast.parse(raw).body[0]
        digest = _sha(bridges._normalized_ast(node).encode())
        self.assertEqual(bridges._pin_source(raw, {"task_key": digest})[0]["ast_sha256"], digest)
        with self.assertRaisesRegex(ValueError, "writer_ast_changed"):
            bridges._pin_source(raw.replace(b"return dataset", b"return identity"), {"task_key": digest})
        with self.assertRaisesRegex(ValueError, "function_missing"):
            bridges._pin_source(raw, {"params": digest})

    def test_literal_selection_schema_rejects_bool_or_empty_field(self):
        for keyword in ("selection_field=True", "selection_field=''", "selection_field='mdate'",
                        "selection_field='name',selected_label=True", "selection_field='name',selected_label=''",
                        "selected_label='synthetic'"):
            raw = ("ADAPTERS=(ReportAdapter('example','synthetic','005','mdate'," + keyword + "),)").encode()
            with self.assertRaisesRegex(ValueError, "selection_schema_invalid"):
                bridges._specs([raw], {"example"})

    def test_indirect_period_validator_and_adapter_semantic_drift_are_rejected(self):
        raw = b"class ReportAdapter:\n    def validate_period(self, value, today):\n        return value\ndef adapter(dataset):\n    return dataset\n"
        nodes = bridges._functions(ast.parse(raw))
        pins = {name: _sha(bridges._normalized_ast(nodes[name]).encode())
                for name in ("ReportAdapter.validate_period", "adapter")}
        bridges._pin_source(raw, pins)
        with self.assertRaisesRegex(ValueError, "writer_ast_changed"):
            bridges._pin_source(raw.replace(b"return value", b"return '2026-01-01'"), pins)
        with self.assertRaisesRegex(ValueError, "writer_ast_changed"):
            bridges._pin_source(raw.replace(b"return dataset", b"return 'another-adapter'"), pins)

    def test_endpoint_prefix_constant_cannot_drift_behind_same_function_ast(self):
        raw = ("PREFIX=" + repr(bridges.PREFIX) + "\ndef endpoint():\n    return PREFIX\n").encode()
        bridges._assert_prefix(raw)
        changed = raw.replace(bridges.PREFIX.encode(), b"/changed/")
        self.assertEqual(ast.dump(ast.parse(raw).body[1], include_attributes=False),
                         ast.dump(ast.parse(changed).body[1], include_attributes=False))
        with self.assertRaisesRegex(ValueError, "endpoint_prefix_changed"):
            bridges._assert_prefix(changed)

    def test_empty_type_params_are_version_neutral_without_rewriting_literals(self):
        raw = b"def f(x):\n    return 'literal type_params=[]'\n"
        node = ast.parse(raw).body[0]
        # This fixed text is the Python3.9 AST dump, not a runtime-computed pin.
        expected = "FunctionDef(name='f', args=arguments(posonlyargs=[], args=[arg(arg='x')], kwonlyargs=[], kw_defaults=[], defaults=[]), body=[Return(value=Constant(value='literal type_params=[]'))], decorator_list=[])"
        self.assertEqual(bridges._normalized_ast(node), expected)
        if "type_params" not in node._fields:
            node._fields = (*node._fields, "type_params")
        node.type_params = []
        self.assertEqual(bridges._normalized_ast(node), expected)
        self.assertIn("type_params", node._fields)  # The caller's AST is unchanged.
        node.type_params = [ast.Name(id="T", ctx=ast.Load())]
        with self.assertRaisesRegex(ValueError, "type_parameters_unreviewed"):
            bridges._normalized_ast(node)

    def test_resolver_preserves_consumption_state_and_non_identity_blockers(self):
        identity = TaskIdentity("liaoning", "offline-shadow-review", "example", {"kind": "day", "label": "2026-01-03"})
        blockers = ("identity_mapping_required", "source_parameter_identity_mapping_required",
                    "legacy_source_hold", "legacy_stage_requires_settlement", "legacy_active_owner_unsettled")
        for state in ("saved", "empty", "unknown", "failed", "denied"):
            before = LegacyTask("old-key", "example", state, state, True, blockers=blockers, facts_sha256="a" * 64)
            after = bridges._resolved_task(before, identity)
            self.assertEqual((after.legacy_key, after.dataset, after.legacy_state, after.state, after.consumed, after.facts_sha256),
                             (before.legacy_key, before.dataset, before.legacy_state, before.state, before.consumed, before.facts_sha256))
            self.assertEqual(set(after.blockers), set(blockers) - {"identity_mapping_required", "source_parameter_identity_mapping_required"})

    def test_same_old_key_from_other_snapshot_is_not_a_cohort_member(self):
        path = self.write("state/expected.json", {})
        before = LegacyTask("old-key", "example", "saved", "saved", True,
                            blockers=("identity_mapping_required",), facts_sha256="a" * 64)
        source = str(path)
        expected = {("liaoning/state/expected.json", _sha(before.legacy_key.encode())): {
            "dataset": before.dataset, "state": before.state, "consumed": before.consumed,
            "facts_sha256": before.facts_sha256, "remaining_blockers": list(before.blockers),
            "source_snapshot_ref": {"sha256": _sha(path.read_bytes())}}}
        with patch.object(bridges, "_phase2_records", return_value={
                str(self.root / "state/other.json"): [before], source: [before]}):
            result = bridges._baseline(self.reader, expected)
        self.assertEqual(result, {source: [before]})

    def test_saved_domain_input_sha_is_not_replaced_by_new_metadata(self):
        path = self.write("state/known.json", {"version": 2})
        self.reader.raw(path)
        with self.assertRaisesRegex(ValueError, "domain_input_sha256_changed"):
            bridges._require_saved_anchor(self.reader, "state/known.json", {"liaoning/state/known.json": "a" * 64})

    def test_prior_choice_requires_saved_sample_body_commitment(self):
        source, _, group, key, anchors = self.domain()
        source["proofs"][0]["sha256"] = "d" * 64
        with self.assertRaisesRegex(ValueError, "prior_sample_commitment_not_retained"):
            bridges._validate_domain(self.reader, self.spec, source, group, self.periods, {key}, anchors)

    def test_missing_sha_commitments_never_match_each_other(self):
        for invalid in (None, "", "abc", "A" * 64, 123, []):
            for variant in ("source_sha", "journal_sha", "prior_and_proof_sha", "proof_sha"):
                with self.subTest(invalid_type=type(invalid).__name__, variant=variant):
                    source, journal, group, key, _ = self.domain()
                    expected_error = "prior_sample_commitment_not_retained"
                    if variant == "source_sha":
                        source["source_evidence_sha256"] = invalid
                        expected_error = "source_evidence_sha256_required"
                    elif variant == "journal_sha":
                        journal["source_evidence_sha256"] = invalid
                        expected_error = "source_evidence_sha256_required"
                    elif variant == "prior_and_proof_sha":
                        next(iter(source["prior_outcomes"].values()))["raw_sha256"] = invalid
                        expected_error = "prior_outcome_body_sha256_required"
                    source["proofs"][0]["sha256"] = invalid
                    path = self.write("state/server_verified_expansion_20261023/server_advanced_tasks_20261016.json",
                                      {"datasets": {"example": journal}})
                    anchors = {str(path.relative_to(self.root.parent)): _sha(path.read_bytes())}
                    with self.assertRaisesRegex(ValueError, expected_error):
                        bridges._validate_domain(_Reader(self.root), self.spec, source, group, self.periods, {key}, anchors)


if __name__ == "__main__":
    unittest.main()
