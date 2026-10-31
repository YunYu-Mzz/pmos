"""Unique preimages of frozen Liaoning legacy identity commitments.

All candidate dates and typed choices come from pinned local writer rules and
saved queue/manifest domains. No legacy crawler is imported or executed. This
recovers identity commitments, never original native payload/response bytes,
permission, or dispatch authority. Native values are retained only in RAM.
"""
from __future__ import annotations

import ast
from collections import Counter
from copy import deepcopy
from dataclasses import replace
from datetime import date, timedelta
import io
import json
from pathlib import Path
import re
import tarfile

from ..contracts import TaskIdentity, canonical
from .liaoning import (_Reader, _sha, baseline_queue_records as _phase2_records,
                       QUEUE, MANIFEST)

PHASE4 = "pmos/evidence/liaoning_phase4_remaining.json"
PHASE4_SHA256 = "61473f8a198eba8fc87bf54268a3688c657ce97983ec8bac73680196bea61983"
RELEASE = "state/aliyun_deployment_20261023/release.tar.gz"
RELEASE_SHA256 = "363e77e4682617f30f82a6ac7379570a0a42a048ed38a578434ad63cf0452fcf"
HISTORICAL_WRITER = "reporting/diagnostics_20261024/memory_candidate/before/lncrawler/server_verified_sources.py"
HISTORICAL_WRITER_SHA256 = "8c45d159078e21b2595b618246184ab0e23152377d958399303501e87f8289b4"
FRESH_SNAPSHOT = ("reporting/diagnostics_20261028/resource_wait/checkpoints_before/"
                  "state/server_verified_expansion_20261023/queue.json")
MAX_CANDIDATES = 2072
PREFIX = "/px-dif-settlement-infpubquery/pdblnReportController/"
HEX = re.compile(r"[0-9a-f]{64}\Z")

# Whole normalized function ASTs are pinned; comments/formatting do not matter.
# Registry literals and the actual source/archive bytes are also recorded.
PINS = {
    "lncrawler/expanded_adapters.py": {
        "ReportAdapter.endpoint": "438bcbc7d8d6545012ade4776b8eac1e5cd6c139f9df4860c5a1a21b92f230e6",
        "ReportAdapter.validate_period": "296979459dd52216f1dc0930e89d10f13d7efd6aeb7ca3b059eda6aa29c64eea",
        "ReportAdapter.params": "37069334d4adb1d89379f143a92706f7d26c84ae69e756e314e175f453a7eef0",
        "ReportAdapter.identity": "a8f43e3a75db2ce1f7047c419a232e492ffc3f8ec524d02ae9e55e013e7f04b5",
        "ReportAdapter.selections": "dba18a46b47c44a0ab10113981d4d131259187f5f105f4e0ff02edf923648c77",
        "ReportAdapter.validate_response": "d445a076f42f8808fee99e97a29f684d7bc1f00b5b36ed59dbc5f1eca8127c18",
        "periods": "53a341f90081cc8926bc3bba910606bd89d4321d181b7e08b05baa1ef7789c7f",
        "task_key": "267e24ecf6b1ce3fe98cb47ad02bf463e04283c820dc6f3eb9eaed41db877a8e",
        "adapter": "01e0cda210226a6d97c34fea5e19edefec7bfcfb3b8d5a47c98a0807538e7c14"},
    "lncrawler/sitewide_specs.py": {
        "adapter": "d98e01a23dfc0de8c393f1861be288b0468ca2e7581d074e93cc507c38277a68"},
    "lncrawler/power_reports.py": {
        "valid_day": "99444dd62ba5200c77682e4d858419c47cd03491323e610b33a0544e4e203ff5"},
    "lncrawler/advanced_tasks.py": {
        "AdvancedTasks.configure": "d08595d8a61b53355f4af7d782b6d4d7fc1d8a2e712c488e04613ccd4e0ba329",
        "AdvancedTasks._task": "3f50e6ce42499084b42190166f9382c188871a1ea043b154f0b761ff55d45c68",
        "AdvancedArchive.save": "e19b1d34c134ead5e40ed386d46ec0c56ed0f0e1cc90aab68c56a67bd58c1075"},
    "lncrawler/server_verified_sources.py": {
        "make_manifest": "ad622072d090336bffd2b31254c09a8b318e6fee70d970cc5ec29d58a9597d81",
        "source_jobs": "9263bada86171c16a890e0f438c7d976bd85344a1f3e51fcb240a37fa6d30bc4",
        "VerifiedSourceRunner.seed_new_dates": "9d9b702feecaf150d20e4a5f0d50a3891b563424562b0de59993eaf2e5e7d85d",
        "VerifiedSourceRunner.dispatch": "ebea9ff0168c840111b7c1e5f25c96205213a463b8e30f8d470922e709dbd67a"},
}


def _functions(tree, prefix=""):
    result = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            result[prefix + node.name] = node
        elif isinstance(node, ast.ClassDef):
            result.update(_functions(node, prefix + node.name + "."))
    return result


def _normalized_ast(node):
    """Use the pre-3.12 representation only for empty generic type params.

    Work on AST nodes, never on dump strings: source string literals containing
    ``type_params=[]`` remain evidence and must not be rewritten.
    """
    node = deepcopy(node)
    for child in ast.walk(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and "type_params" in child._fields:
            if getattr(child, "type_params", []) != []:
                raise ValueError("generic_legacy_writer_type_parameters_unreviewed")
            child._fields = tuple(field for field in child._fields if field != "type_params")
    # Python 3.13+ omits empty fields unless asked; keep the pinned (3.9-3.12) form.
    import inspect
    extra = {"show_empty": True} if "show_empty" in inspect.signature(ast.dump).parameters else {}
    return ast.dump(node, annotate_fields=True, include_attributes=False, **extra)


def _pin_source(raw, pins):
    nodes = _functions(ast.parse(raw))
    result = []
    for name, expected in pins.items():
        node = nodes.get(name)
        if node is None:
            raise ValueError("legacy_identity_writer_function_missing")
        digest = _sha(_normalized_ast(node).encode())
        if digest != expected:
            raise ValueError("legacy_identity_writer_ast_changed")
        result.append({"function": name, "ast_sha256": digest, "source_line": node.lineno})
    return result


def _assert_prefix(raw):
    assignments = [node for node in ast.parse(raw).body if isinstance(node, ast.Assign)
                   and any(isinstance(target, ast.Name) and target.id == "PREFIX" for target in node.targets)]
    if len(assignments) != 1:
        raise ValueError("literal_native_endpoint_prefix_required")
    try:
        value = ast.literal_eval(assignments[0].value)
    except (ValueError, TypeError):
        raise ValueError("literal_native_endpoint_prefix_required") from None
    if value != PREFIX or type(value) is not str:
        raise ValueError("native_endpoint_prefix_changed")
    return {"constant": "PREFIX", "literal_sha256": _sha(canonical(value))}


def _specs(raws, datasets):
    result = {}
    for raw in raws:
        for node in ast.walk(ast.parse(raw)):
            if (not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name)
                    or node.func.id != "ReportAdapter" or len(node.args) < 4):
                continue
            try:
                dataset = ast.literal_eval(node.args[0])
                if dataset not in datasets:
                    continue
                number, field = ast.literal_eval(node.args[2]), ast.literal_eval(node.args[3])
                kwargs = {kw.arg: ast.literal_eval(kw.value) for kw in node.keywords}
            except (ValueError, TypeError):
                raise ValueError("literal_legacy_report_spec_required") from None
            spec = {"dataset": dataset, "number": number, "date_field": field,
                    "selection_field": kwargs.get("selection_field"), "selected_label": kwargs.get("selected_label"),
                    "period": kwargs.get("period", "day"), "kind": kwargs.get("kind", "report")}
            if dataset in result or spec["kind"] != "report" or spec["period"] != "day":
                raise ValueError("unique_daily_legacy_report_spec_required")
            if not isinstance(number, str) or not re.fullmatch(r"\d{3}", number) or not isinstance(field, str) or not field:
                raise ValueError("literal_legacy_report_binding_required")
            selection_field, selected_label = spec["selection_field"], spec["selected_label"]
            if (selection_field is not None and (not isinstance(selection_field, str) or not selection_field or selection_field == field)
                    or selected_label is not None and (selection_field is None or not _typed_choice(selected_label))):
                raise ValueError("literal_legacy_selection_schema_invalid")
            result[dataset] = spec
    if set(result) != set(datasets):
        raise ValueError("legacy_report_spec_not_retained")
    return result


def _source_evidence(reader, datasets):
    raws, proofs = {}, []
    for relative, pins in PINS.items():
        raw = reader.raw(relative)
        raws[relative] = raw
        proof = {"source_ref": reader.ref(relative), "algorithms": _pin_source(raw, pins)}
        if relative == "lncrawler/expanded_adapters.py":
            proof["endpoint_prefix"] = _assert_prefix(raw)
        proofs.append(proof)
    historical = reader.raw(HISTORICAL_WRITER)
    if _sha(historical) != HISTORICAL_WRITER_SHA256:
        raise ValueError("historical_identity_writer_source_changed")
    historical_pins = {**PINS["lncrawler/server_verified_sources.py"],
        "VerifiedSourceRunner.dispatch": "8c40b7acc2dbac763a78db2e1c0884977c80c3dd3d09c974197b7f0ed57c5d7e"}
    proofs.append({"source_ref": reader.ref(HISTORICAL_WRITER), "algorithms": _pin_source(historical, historical_pins)})
    archive = reader.raw(RELEASE)
    if _sha(archive) != RELEASE_SHA256:
        raise ValueError("historical_release_source_archive_changed")
    wanted = ("lncrawler/expanded_adapters.py", "lncrawler/power_reports.py",
              "lncrawler/advanced_tasks.py", "lncrawler/sitewide_specs.py")
    archived, archive_refs = {}, []
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as source:
        # Inventory all bounded headers before reading exactly four pure source
        # members. Never extract or inspect any private/profile/config member.
        members = source.getmembers()
        if len(members) > 4096:
            raise ValueError("bounded_historical_source_archive_required")
        for relative in wanted:
            matches = [member for member in members if member.name == relative]
            if len(matches) != 1 or not matches[0].isfile() or matches[0].size > 128 * 1024:
                raise ValueError("unique_bounded_historical_source_member_required")
            member = matches[0]
            with source.extractfile(member) as opened:
                raw = opened.read(128 * 1024 + 1)
            if len(raw) != member.size:
                raise ValueError("historical_source_member_size_changed")
            archived[relative] = raw
            proof = {"archive_ref": reader.ref(RELEASE), "member_path": relative,
                     "member_sha256": _sha(raw), "algorithms": _pin_source(raw, PINS.get(relative, {}))}
            if relative == "lncrawler/expanded_adapters.py":
                proof["endpoint_prefix"] = _assert_prefix(raw)
            archive_refs.append(proof)
    live_specs = _specs([raws["lncrawler/expanded_adapters.py"], raws["lncrawler/sitewide_specs.py"]], datasets)
    old_specs = _specs([archived["lncrawler/expanded_adapters.py"], archived["lncrawler/sitewide_specs.py"]], datasets)
    if canonical(live_specs) != canonical(old_specs):
        raise ValueError("historical_and_current_report_registry_binding_changed")
    return live_specs, {"source_functions": proofs, "historical_archive_sources": archive_refs,
        "ast_normalization": "omit_only_empty_type_params_on_function_asyncfunction_class;reject_nonempty",
        "historical_archive_members_listed": len(members), "historical_source_members_read": len(wanted),
        "archive_members_extracted": 0, "legacy_modules_imported_or_executed": 0}


def _read_phase4(reader):
    value, digest = reader.json(reader.workspace / PHASE4)
    if digest != PHASE4_SHA256:
        raise ValueError("immutable_phase4_cohort_sha256_changed")
    rows = value["queue_evidence"]["records"]
    if (len(rows) != 11 or Counter(row["state"] for row in rows) != {"empty": 5, "saved": 6}
            or any(row["identity_key_sha256"] is not None or row["consumed"] is not True for row in rows)):
        raise ValueError("immutable_phase4_old_identity_cohort_changed")
    expected = {(row["source_snapshot_ref"]["path"], row["legacy_key_sha256"]): row for row in rows}
    if len(expected) != 11:
        raise ValueError("unique_phase4_source_and_old_key_required")
    return value, expected


def _saved_input_anchors(reader, phase4):
    reference = phase4["phase3_ref"]
    if reference["path"] != "pmos/evidence/liaoning_phase3_recovery.json":
        raise ValueError("declared_phase3_domain_anchor_report_required")
    phase3, digest = reader.json(reader.workspace / reference["path"])
    if digest != reference["sha256"]:
        raise ValueError("saved_phase3_domain_anchor_report_changed")
    anchors = {ref["path"]: ref["sha256"] for ref in phase3["evidence_refs"].values()}
    needed = {"liaoning/" + QUEUE, "liaoning/" + MANIFEST,
        "liaoning/state/server_verified_expansion_20261023/server_advanced_tasks_20261016.json",
        "liaoning/state/server_verified_expansion_20261023/server_sitewide_20261016_tasks.json"}
    for ref in phase4["input_refs"]:
        if ref["path"] in needed:
            if anchors.get(ref["path"]) != ref["sha256"]:
                raise ValueError("saved_phase3_phase4_domain_anchor_conflict")
    if not needed <= anchors.keys():
        raise ValueError("saved_domain_input_anchor_missing")
    return anchors


def _require_saved_anchor(reader, relative, anchors):
    ref = reader.ref(relative)
    if anchors.get(ref["path"]) != ref["sha256"]:
        raise ValueError("saved_identity_domain_input_sha256_changed")


def _baseline(reader, expected):
    result = {}
    for source, rows in _phase2_records(reader.root).items():
        relative = str(Path(source).relative_to(reader.workspace))
        selected = []
        for task in rows:
            record = expected.get((relative, _sha(task.legacy_key.encode())))
            if record is None:
                continue
            if (task.dataset, task.state, task.consumed, task.facts_sha256, list(task.blockers)) != (
                    record["dataset"], record["state"], record["consumed"], record["facts_sha256"], record["remaining_blockers"]):
                raise ValueError("phase4_old_task_facts_or_blockers_changed")
            if task.identity is not None:
                raise ValueError("phase4_old_task_expected_unmapped")
            raw = reader.raw(reader.workspace / relative)
            if _sha(raw) != record["source_snapshot_ref"]["sha256"]:
                raise ValueError("phase4_old_task_source_sha256_changed")
            selected.append(task)
        if selected:
            result[source] = selected
    if sum(map(len, result.values())) != len(expected):
        raise ValueError("phase4_old_task_source_and_key_not_retained")
    return result


def _legacy_key(dataset, identity):
    payload = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "liaoning:2026:expanded:" + dataset + ":" + _sha(payload.encode())


def _typed_choice(value):
    return ((type(value) is str and bool(value) and len(value) <= 200)
            or (type(value) is int and abs(value) <= 9007199254740991))


def _choices(spec, source, journal):
    verified, disclosed = journal.get("verified_selections"), journal.get("selections")
    if (not isinstance(verified, list) or not isinstance(disclosed, list) or not verified
            or canonical(verified) != canonical(source.get("selections"))):
        raise ValueError("typed_verified_selection_domain_not_retained")
    if spec["selection_field"]:
        if (any(not _typed_choice(value) for value in disclosed + verified)
                or len({canonical(value) for value in disclosed}) != len(disclosed)
                or len({canonical(value) for value in verified}) != len(verified)
                or not {canonical(value) for value in verified} <= {canonical(value) for value in disclosed}):
            raise ValueError("typed_choice_must_be_disclosed_and_verified")
        if spec["selected_label"] is not None and verified != [spec["selected_label"]]:
            raise ValueError("fixed_native_selection_rule_changed")
    elif verified != [""] or disclosed != [""]:
        raise ValueError("dimension_free_report_has_unexpected_selection")
    return list(verified)


def _calendar(seed):
    if not isinstance(seed, str):
        raise ValueError("frozen_seeded_date_required")
    try:
        end = date.fromisoformat(seed)
    except ValueError:
        raise ValueError("frozen_seeded_date_required") from None
    if end.year != 2026 or end.isoformat() != seed:
        raise ValueError("frozen_2026_calendar_required")
    start = date(2026, 1, 1)
    return [(start + timedelta(days=offset)).isoformat() for offset in range((end - start).days + 1)]


def _job(spec, period, selection):
    params = {spec["date_field"]: period}
    identity = {"period": period}
    if spec["selection_field"]:
        if not _typed_choice(selection):
            raise ValueError("typed_selection_required")
        params[spec["selection_field"]] = selection
        identity["selection"] = selection
    elif selection != "" or type(selection) is not str:
        raise ValueError("unexpected_native_selection_dimension")
    return {"key": _legacy_key(spec["dataset"], identity), "dataset": spec["dataset"],
            "period": period, "selection": selection, "page": 1, "params": params}, identity


def _assert_job(spec, job, periods, choices):
    if (job.get("dataset") != spec["dataset"] or job.get("period") not in periods
            or canonical(job.get("selection")) not in {canonical(value) for value in choices}
            or type(job.get("page")) is not int or job["page"] != 1):
        raise ValueError("observed_job_outside_controlled_domain")
    expected, _ = _job(spec, job["period"], job["selection"])
    if (job.get("key") != expected["key"] or not isinstance(job.get("params"), dict)
            or canonical(job["params"]) != canonical(expected["params"])):
        raise ValueError("observed_job_has_unmapped_or_changed_native_parameters")


def _enumerate(spec, periods, choices, old_keys):
    count = len(periods) * len(choices)
    if not 0 < count <= MAX_CANDIDATES:
        raise ValueError("bounded_identity_preimage_domain_required")
    keys, matches = set(), {key: [] for key in old_keys}
    for period in periods:
        for selection in choices:
            job, identity = _job(spec, period, selection)
            if job["key"] in keys:
                raise ValueError("duplicate_or_colliding_identity_candidates")
            keys.add(job["key"])
            if job["key"] in matches:
                matches[job["key"]].append((job, identity))
    if any(len(found) != 1 for found in matches.values()):
        raise ValueError("legacy_identity_preimage_not_unique_or_missing")
    return keys, {key: found[0] for key, found in matches.items()}


def _resolved_task(task, identity):
    identity.document()
    return replace(task, identity=identity, blockers=tuple(blocker for blocker in task.blockers
        if blocker not in {"identity_mapping_required", "source_parameter_identity_mapping_required"}))


def _validate_domain(reader, spec, source, group, periods, old_keys, anchors):
    dataset = spec["dataset"]
    if (source.get("dataset") != dataset or source.get("kind") != "report" or source.get("period") != "day"
            or source.get("number") != spec["number"] or source.get("method") != "POST"
            or source.get("endpoint") != PREFIX + "pdblnReport" + spec["number"]
            or source.get("route") != "/pxf-dif-settlement-outnetpub/pdbln/report/PdblnReport" + spec["number"]):
        raise ValueError("manifest_endpoint_method_or_report_rule_changed")
    original = source.get("original_journal")
    if original not in {"server_advanced_tasks_20261016.json", "server_sitewide_20261016_tasks.json"}:
        raise ValueError("declared_historical_business_journal_required")
    relative = "state/server_verified_expansion_20261023/" + original
    document, _ = reader.json(relative)
    _require_saved_anchor(reader, relative, anchors)
    journal = document["datasets"][dataset]
    source_sha = source.get("source_evidence_sha256")
    journal_sha = journal.get("source_evidence_sha256")
    if any(not isinstance(value, str) or not HEX.fullmatch(value) for value in (source_sha, journal_sha)):
        raise ValueError("historical_source_evidence_sha256_required")
    if (canonical(journal.get("outcomes")) != canonical(source.get("prior_outcomes"))
            or journal.get("source_evidence_sha256") != source.get("source_evidence_sha256")):
        raise ValueError("historical_prior_outcomes_or_source_commitment_changed")
    choices = _choices(spec, source, journal)
    candidates, hits = _enumerate(spec, periods, choices, old_keys)
    pending = group.get("pending")
    outcomes, prior = group.get("outcomes"), source.get("prior_outcomes")
    if not isinstance(pending, list) or not isinstance(outcomes, dict) or not isinstance(prior, dict):
        raise ValueError("frozen_report_frontier_shape_required")
    for job in pending:
        _assert_job(spec, job, periods, choices)
    for key in prior:
        prior_sha = prior[key].get("raw_sha256") if isinstance(prior[key], dict) else None
        if not isinstance(prior_sha, str) or not HEX.fullmatch(prior_sha):
            raise ValueError("prior_outcome_body_sha256_required")
        job = journal.get("tasks", {}).get(key)
        if not isinstance(job, dict):
            raise ValueError("prior_verified_job_not_retained")
        _assert_job(spec, job, periods, choices)
        if job["key"] != key:
            raise ValueError("prior_verified_old_key_changed")
    pending_keys = {job["key"] for job in pending}
    if (len(pending_keys) != len(pending) or pending_keys & set(outcomes) or pending_keys & set(prior)
            or set(outcomes) & set(prior) or pending_keys | set(outcomes) | set(prior) != candidates):
        raise ValueError("controlled_domain_not_equal_frozen_pending_prior_outcomes_union")
    # The saved proofs bind each admitted typed choice to a retained prior task
    # and its recorded body SHA. No dictionary or sample is queried again.
    for selection in choices:
        proofs = [proof for proof in source.get("proofs", [])
                  if isinstance(proof, dict) and isinstance(proof.get("sha256"), str) and HEX.fullmatch(proof["sha256"])
                  and canonical(proof.get("selection")) == canonical(selection)]
        verified = [(key, prior[key]) for key in prior
                    if canonical(journal["tasks"][key]["selection"]) == canonical(selection)
                    and prior[key].get("outcome") == "saved"]
        if not proofs or not any(proof.get("sha256") == outcome.get("raw_sha256")
                                 for proof in proofs for _, outcome in verified):
            raise ValueError("typed_choice_prior_sample_commitment_not_retained")
    report = {"dataset": dataset, "period_count": len(periods), "verified_choice_count": len(choices),
        "disclosed_choice_count": len(journal["selections"]),
        "typed_verified_choice_counts": dict(Counter(type(value).__name__ for value in choices)),
        "selection_dimension_present": spec["selection_field"] is not None,
        "period_domain_sha256": _sha(canonical(periods)), "verified_choice_domain_sha256": _sha(canonical(choices)),
        "disclosed_choice_domain_sha256": _sha(canonical(journal["selections"])),
        "candidate_count": len(candidates), "candidate_key_set_sha256": _sha(canonical(sorted(candidates))),
        "frozen_pending_count": len(pending), "frozen_prior_outcome_count": len(prior),
        "frozen_current_outcome_count": len(outcomes), "closed_domain_matches_frozen_union": True,
        "manifest_ref": reader.ref(MANIFEST), "manifest_source_pointer": "#/sources/" + dataset,
        "historical_journal_ref": reader.ref(relative), "historical_dataset_pointer": "#/datasets/" + dataset,
        "native_rule_sha256": _sha(canonical(spec)), "endpoint_sha256": _sha(source["endpoint"].encode()),
        "source_evidence_sha256": source["source_evidence_sha256"]}
    return hits, report


def _run(root):
    reader = _Reader(root)
    phase4, expected = _read_phase4(reader)
    anchors = _saved_input_anchors(reader, phase4)
    baseline = _baseline(reader, expected)
    tasks = [task for rows in baseline.values() for task in rows]
    datasets = {task.dataset for task in tasks}
    specs, source_proof = _source_evidence(reader, datasets)
    manifest, _ = reader.json(MANIFEST)
    queue, _ = reader.json(QUEUE)
    _require_saved_anchor(reader, MANIFEST, anchors)
    _require_saved_anchor(reader, QUEUE, anchors)
    periods = _calendar(queue.get("seeded_date"))
    if len(datasets) != 6 or len(periods) != 296:
        raise ValueError("fixed_phase4_dataset_or_calendar_domain_changed")
    hits, domains = {}, []
    for dataset in sorted(datasets):
        old_keys = {task.legacy_key for task in tasks if task.dataset == dataset}
        found, report = _validate_domain(reader, specs[dataset], manifest["sources"][dataset],
                                        queue["sources"][dataset], periods, old_keys, anchors)
        hits.update(found)
        domains.append(report)
    if sum(domain["candidate_count"] for domain in domains) != MAX_CANDIDATES:
        raise ValueError("fixed_phase4_total_candidate_bound_changed")
    rows, resolved = [], {}
    for source, old_tasks in baseline.items():
        relative = str(Path(source).relative_to(reader.workspace))
        resolved[source] = []
        for task in old_tasks:
            record = expected[(relative, _sha(task.legacy_key.encode()))]
            job, preimage = hits[task.legacy_key]
            if (_legacy_key(task.dataset, preimage) != task.legacy_key
                    or _sha(canonical(preimage)) != task.legacy_key.rsplit(":", 1)[-1]):
                raise ValueError("old_identity_commitment_preimage_recheck_failed")
            outcome = queue["sources"][task.dataset]["outcomes"][task.legacy_key]
            body = outcome.get("sha256")
            if not isinstance(body, str) or not HEX.fullmatch(body) or body != record["old_outcome_body_sha256"]:
                raise ValueError("frozen_old_outcome_body_sha256_changed")
            identity = TaskIdentity("liaoning", "offline-shadow-review", task.dataset,
                {"kind": "day", "label": preimage["period"]},
                {specs[task.dataset]["selection_field"]: preimage["selection"]}
                    if specs[task.dataset]["selection_field"] else {})
            identity.document()
            after = _resolved_task(task, identity)
            # Only identity blockers may be discharged, never owner/stage/hold.
            if (after.state, after.consumed, after.facts_sha256) != (task.state, task.consumed, task.facts_sha256):
                raise ValueError("old_task_consumption_fact_changed")
            resolved[source].append(after)
            rows.append({"legacy_key_sha256": _sha(task.legacy_key.encode()), "dataset": task.dataset,
                "source_snapshot_ref": record["source_snapshot_ref"], "facts_sha256": task.facts_sha256,
                "state": task.state, "consumed": task.consumed, "unique_preimage_matches": 1,
                "old_key_commitment_matches_preimage": True, "old_outcome_sha_matches_phase4": True,
                "legacy_identity_preimage_sha256": _sha(canonical(preimage)),
                "constructed_writer_job_params_sha256": _sha(canonical(job["params"])),
                "old_outcome_body_sha256": body, "identity_key_sha256": identity.key,
                "remaining_blockers": list(after.blockers), "dispatch_allowed": False,
                "original_native_payload_bytes_recovered": False, "original_response_bytes_recovered": False,
                "status": "unique_preimage_of_old_identity_commitment_associated_with_old_outcome_sha"})
    fresh = {"locally_retained": False}
    if (reader.root / FRESH_SNAPSHOT).is_file():
        latest, _ = reader.json(FRESH_SNAPSHOT)
        count = sum(task.legacy_key in latest.get("sources", {}).get(task.dataset, {}).get("outcomes", {}) for task in tasks)
        fresh = {"snapshot_ref": reader.ref(FRESH_SNAPSHOT), "locally_retained": True,
                 "same_old_outcome_keys_seen": count, "new_original_or_identity_recovery_count": 0}
    if reader.changed():
        raise ValueError("business_inputs_changed_during_identity_preimage_recovery")
    report = {"schema": "pmos-liaoning-phase5-identity-preimages-v1", "province": "liaoning",
        "mode": "offline_unique_preimage_of_frozen_old_identity_commitment",
        "phase4_ref": reader.ref(reader.workspace / PHASE4),
        "summary": {"old_record_cohort": len(tasks), "unique_identity_preimages_recovered": len(rows),
            "remaining_identity_bridges": len(tasks) - len(rows), "dataset_domains": len(domains),
            "candidate_identity_upper_bound": MAX_CANDIDATES, "candidate_identities_checked": MAX_CANDIDATES,
            "old_states": dict(Counter(task.state for task in tasks)), "old_consumed_facts": sum(task.consumed for task in tasks),
            "new_originals_recovered": 0},
        "calendar_domain": {"calendar_year_from_pinned_rule": 2026, "period_count": len(periods),
            "lower_bound_rule": "calendar_year_start_from_pinned_periods",
            "upper_bound_source_ref": reader.ref(QUEUE), "upper_bound_pointer": "#/seeded_date",
            "first_period_sha256": _sha(canonical(periods[0])), "last_period_sha256": _sha(canonical(periods[-1])),
            "enumeration_order": "ascending_calendar_then_recorded_typed_verified_choices"},
        "domains": domains, "records": rows, "writer_chain_evidence": source_proof,
        "new_existing_snapshot_review": fresh,
        "input_refs": [{"path": path, "sha256": digest} for path, digest in sorted(reader.refs.items())],
        "website_requests": 0, "production_writes": 0, "active_sqlite_connections": 0,
        "archive_members_extracted": 0, "dispatch_allowed": False, "inputs_changed_during_scan": [],
        "limitations": ["uniqueness is within the complete declared local writer domain and assumes SHA256 collision resistance",
            "identity ignores unknown native payload fields; only the pinned params-to-identity-to-key writer chain is reconstructed",
            "no original native payload bytes, extra empty Vue fields, response bytes or new permission evidence were recovered",
            "old body SHA is associated with the frozen outcome and was not revalidated against a missing original",
            "saved/empty consumption, owner/stage/hold blockers and all production source bytes remain unchanged"]}
    return report, resolved


def baseline_queue_records(root):
    """Return only the frozen phase4 eleven before views, with real keys in RAM."""
    reader = _Reader(root)
    _, expected = _read_phase4(reader)
    result = _baseline(reader, expected)
    if reader.changed():
        raise ValueError("business_inputs_changed_during_identity_preimage_recovery")
    return result


def resolve_queue_records(root):
    """Return eleven identity-only changes; consumed states and holds survive."""
    return _run(root)[1]


def recover(root):
    """Return redacted hashes/bounds and evidence paths; never write anything."""
    return _run(root)[0]
