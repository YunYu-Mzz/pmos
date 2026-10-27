"""Assert that RAM-only identity enrichment preserves every old queue fact."""
from __future__ import annotations

from collections import Counter
import hashlib

from ..contracts import canonical
from ..queue_compat import LegacyTask


MAPPING_BLOCKERS = {"identity_mapping_required", "source_parameter_identity_mapping_required"}


def validate_queue_recovery(before: dict[str, list[LegacyTask]],
                            after: dict[str, list[LegacyTask]]) -> dict:
    """Validate the same source cohort; return only aggregate counts and hashes.

    This does not import a ledger, write files, or confer scheduling permission.
    Real old keys and selectors remain in the caller's RAM.
    """
    if set(before) != set(after):
        raise ValueError("legacy_source_views_changed")
    states, facts = Counter(), []
    total = unmapped_before = unmapped_after = recovered = consumed = 0
    for source in sorted(before):
        old = {row.legacy_key: row for row in before[source]}
        new = {row.legacy_key: row for row in after[source]}
        if (len(old) != len(before[source]) or len(new) != len(after[source])
                or set(old) != set(new)):
            raise ValueError("legacy_keys_or_multiplicity_changed")
        for key in sorted(old):
            first, last = old[key], new[key]
            fields = ("dataset", "legacy_state", "state", "consumed", "facts_sha256")
            if any(getattr(first, field) != getattr(last, field) for field in fields):
                raise ValueError("legacy_state_or_facts_changed")
            if first.identity is not None and first.identity != last.identity:
                raise ValueError("existing_identity_changed")
            filled = first.identity is None and last.identity is not None
            expected = set(first.blockers) - (MAPPING_BLOCKERS if filled else set())
            if set(last.blockers) != expected:
                raise ValueError("legacy_protection_changed")
            if last.identity is not None:
                last.identity.document()
            total += 1
            unmapped_before += first.identity is None
            unmapped_after += last.identity is None
            recovered += filled
            consumed += first.consumed
            states[first.state] += 1
            facts.append({"source_ref_sha256": hashlib.sha256(source.encode()).hexdigest(),
                          "legacy_key_sha256": hashlib.sha256(key.encode()).hexdigest(),
                          **{field: getattr(first, field) for field in fields},
                          "protective_blockers": sorted(set(first.blockers) - MAPPING_BLOCKERS)})
    return {"source_views": len(before), "record_views": total,
            "unmapped_before": unmapped_before, "identities_recovered": recovered,
            "unmapped_after": unmapped_after, "states": dict(sorted(states.items())),
            "consumed": consumed, "state_facts_protection_sha256": hashlib.sha256(canonical(facts)).hexdigest(),
            "states_and_facts_unchanged": True, "dispatch_allowed": False}
