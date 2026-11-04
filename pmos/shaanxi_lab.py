"""Offline metadata, exact legacy keys and fake-transport Engine integration."""
from __future__ import annotations
from collections import Counter
import hashlib
import json
from pathlib import Path
from .adapters.shaanxi import FAMILIES
from .contracts import TaskIdentity, canonical, normalized_period, reject_secrets

ROOT = Path(__file__).resolve().parents[1]


def inventory(registry: dict, source_sha256: str) -> dict:
    rows, seen = [], set()
    for source in registry['datasets']:
        source_id = source['id']
        if not isinstance(source_id, str) or not source_id or source_id in seen:
            raise ValueError('source_id_missing_or_duplicate')
        seen.add(source_id)
        family = source.get('family')
        validation = source.get('validation') or {}
        rows.append({'legacy_id': source_id, 'name': source['name'], 'family': family,
            'legacy_enabled': source.get('enabled') is True, 'online_enabled': False,
            'qualification': 'shape_bridge_only_unverified' if family in FAMILIES else 'family_unassigned',
            'sample_only_in_legacy': source.get('sampleOnly') is True,
            'column_count': len(validation.get('native_columns') or []),
            'native_columns': validation.get('native_columns'),
            'source_identity_fields': validation.get('source_identity_fields'),
            'source_date_field': validation.get('source_date_field'),
            'points_per_row': validation.get('points_per_row'),
            'missing': ['real_response_replay', 'all_dimensions_coverage', 'owner_handoff',
                        'native_transport_audit', 'online_acceptance'] +
                       (['pdf_bytes_and_semantic_parser'] if family in {'directory','file_list','disclosure_list'} else [])})
    return {'province': 'shaanxi', 'mode': 'offline_metadata_inventory',
        'registry_sha256': source_sha256, 'directory_entries': len(rows),
        'family_counts': dict(Counter(row['family'] or 'unassigned' for row in rows)),
        'shape_bridge_entries': sum(row['family'] in FAMILIES for row in rows),
        'sample_verified_entries': 0, 'online_enabled_entries': 0,
        'website_requests': 0, 'entries': rows}


def _js_safe(value):
    # No float re-encoding: JS and Python differ for 1.0, exponent notation etc.
    if isinstance(value, dict):
        return all(isinstance(k, str) and _js_safe(v) for k, v in value.items())
    if isinstance(value, list):
        return all(_js_safe(v) for v in value)
    return type(value) in (str, bool, type(None)) or type(value) is int and abs(value) <= 2**53-1


def legacy_key(job: dict) -> str:
    kind, dataset, params = job.get('kind'), job.get('datasetId'), job.get('params')
    if not isinstance(kind, str) or not isinstance(dataset, str) or not isinstance(params, dict):
        raise ValueError('explicit_legacy_job_required')
    reject_secrets(params)
    if not _js_safe(params):
        raise ValueError('legacy_number_or_type_needs_review')
    if kind == 'download':
        file_id = params.get('fileId')
        if not isinstance(file_id, str) or not file_id:
            raise ValueError('legacy_stable_file_id_required')
        document = {'kind': kind, 'fileId': file_id}
    else:
        document = {'datasetId': dataset, 'kind': kind, 'params': params}
    digest = hashlib.sha256(canonical(document)).hexdigest()
    if job.get('key') != digest:
        raise ValueError('legacy_exact_key_mismatch')
    return digest


def identity_bridge(job: dict, *, account_alias: str, period: dict) -> dict:
    digest = legacy_key(job)
    params, kind = job['params'], job['kind']
    normalized = normalized_period(period)
    if kind == 'download':
        # Stable file identity never changes with listing or snapshot date.
        if normalized != {'kind': 'snapshot', 'anchor': 'legacy-file-id'}:
            raise ValueError('attachment_stable_anchor_required')
        identity = TaskIdentity('shaanxi', account_alias, 'legacy_attachment', normalized,
                                {'file_id': params['fileId']}, operation='attachment')
    else:
        if normalized.get('kind') == 'day':
            if normalized['label'] != params.get('date') or params.get('currentSample'):
                raise ValueError('legacy_day_binding_mismatch')
        elif normalized.get('kind') == 'snapshot':
            if normalized['anchor'] != params.get('snapshotDate'):
                raise ValueError('legacy_snapshot_binding_mismatch')
        elif normalized.get('kind') == 'range':
            if normalized['start'] != params.get('startDate') or normalized['end'] != params.get('endDate'):
                raise ValueError('legacy_native_range_mismatch')
        else:
            raise ValueError('legacy_period_mapping_not_reviewed')
        identity = TaskIdentity('shaanxi', account_alias, job['datasetId'], normalized,
            {'legacy_kind': kind, 'legacy_params': params}, page=params.get('page'), operation=kind)
    status = job.get('status')
    if not isinstance(status, str) or not status:
        raise ValueError('legacy_status_required')
    return {'legacy_key': digest, 'pmos_key': identity.key, 'identity': identity.document(),
            'legacy_status': status, 'legacy_dataset_ref': job['datasetId'],
            'may_dispatch': False, 'requires_original_owner_handoff': True,
            'consumption_state': 'not_yet_proven_unsent' if status == 'pending' else 'preserve_as_tombstone'}


def fixture_demo():
    """Run a synthetic Shaanxi response through unchanged PMOS Engine."""
    import tempfile
    import asyncio
    from .adapters.shaanxi import decode
    from .engine import Engine
    from .province import Province
    from .validate import Response
    fixture = json.loads((ROOT/'tests/fixtures/shaanxi_grid_fixture.json').read_text())
    body = json.dumps(fixture['body'], ensure_ascii=False).encode()
    contract = fixture['contract']
    calls = []

    class Transport:
        async def send(self, request):
            calls.append(request)
            return Response(200, 'application/json', body)

    async def run():
        with tempfile.TemporaryDirectory(prefix='pmos-shaanxi-fixture-') as tmp:
            base = Path(tmp); province = base/'provinces/shaanxi'; (province/'datasets').mkdir(parents=True)
            (province/'province.json').write_bytes(canonical({'id':'shaanxi','name':'陕西合成夹具',
                'origin':'https://offline.invalid','policy':{'response_interval_seconds':1}}))
            spec = {'id':'fixture_grid','name':'合成机组状态','status':'enabled',
                'period':{'kind':'day','start':'2026-01-01','end':'2026-01-01'},
                'request':{'transport':'fetch','method':'POST','path':'/offline/fixture'},
                'response':{'kind':'json'}}
            (province/'datasets/fixture_grid.json').write_bytes(canonical(spec))
            engine = Engine(Province.load('shaanxi', base/'provinces'), Transport(),
                instance=base/'instance', decode_response=lambda raw: decode(raw, contract))
            engine.acquire()
            try:
                engine.ledger.seed('fixture_grid',['2026-01-01'])
                result = await engine.collect(engine.province.datasets()['fixture_grid'],'2026-01-01')
                reseed = engine.ledger.seed('fixture_grid',['2026-01-01'])
                return {'fixture_only':True,'website_requests':0,'fixture_dispatches':len(calls),
                    'reseed_added':reseed,
                    'archive_verified':engine.archive.verify(result.record),
                    'task_status':engine.ledger.task('fixture_grid','2026-01-01')['status'],
                    **result.parsed.summary()}
            finally:
                engine.release()
    return asyncio.run(run())
