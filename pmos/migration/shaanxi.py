"""Read-only full-journal migration, preserving exact native queue identities.

All 13 task kinds are retained, including local parent/admission/context jobs.
Mapping is never dispatch authorization. Open native date ranges stay open;
undated operation identities are explicit request anchors, not guessed dates.
"""
from __future__ import annotations

from collections import Counter
from datetime import date
import hashlib
import json
from pathlib import Path

from ..contracts import TaskIdentity, canonical
from ..queue_compat import LegacyTask, QueueContext, import_shadow, summarize
from ..shaanxi_lab import legacy_key

KINDS = frozenset({'table','catalogue','admission','download','disclosure_admission',
    'listing','listing_admission','directory_admission','file_listing','dimension_parent',
    'listing_empty_review','monthly_attachment','monthly_context'})
UNFINISHED = frozenset({'running','reserved','unknown','sent','pending_review'})


def dataset_id(legacy_id):
    if not isinstance(legacy_id, str) or not legacy_id:
        raise ValueError('legacy_source_id_required')
    return 'sn_' + hashlib.sha256(legacy_id.encode()).hexdigest()[:24]


def verified_key(job):
    try:
        return legacy_key(job)
    except ValueError as error:
        if str(error) != 'legacy_exact_key_mismatch':
            raise
    p,kind=job['params'],job['kind']
    variants=[]
    # These insertion-ordered JSON.stringify identities are explicitly in the
    # legacy source, distinct from the sorted ordinary queue key algorithm.
    if kind=='admission':
        variants.append({'purpose':'native_alias_admission','rootMenuId':job['datasetId'],'nativeGuid':p.get('nativeGuid')})
    elif kind=='disclosure_admission':
        variants.append({'purpose':'disclosure_grid_admission','nativeRoute':p.get('nativeRoute')})
    elif kind=='directory_admission':
        variants.append({'purpose':'disclosure_directory_admission','route':p.get('nativeRoute')})
    elif kind=='listing_admission':
        variants.append({'purpose':'disclosure_listing_admission','guid':p.get('nativeGuid'),
                         'source':p.get('nativeInfoCode'),'component':p.get('nativeComponent')})
    elif kind=='download' and job.get('unissuedRecovery'):
        doc={'kind':'first_unissued_download_after_open_412',
             'skippedJob':job['unissuedRecovery'].get('skippedJob'),'fileId':p.get('fileId')}
        if hashlib.sha256(canonical(doc)).hexdigest()==job['key']:
            return job['key']
    for doc in variants:
        raw=json.dumps(doc,ensure_ascii=False,separators=(',',':')).encode()
        if hashlib.sha256(raw).hexdigest()==job['key']:
            return job['key']
    raise ValueError('legacy_exact_key_mismatch')


def task_identity(job, account_alias):
    key = verified_key(job)
    params, kind = job['params'], job['kind']
    if kind not in KINDS:
        raise ValueError('shaanxi_task_kind_unreviewed')
    if kind == 'download':
        return TaskIdentity('shaanxi', account_alias, 'sn_attachment',
            {'kind':'request_anchor','anchor':'legacy-file-id'},
            {'file_id':params['fileId']}, operation='attachment')
    if params.get('date') and not params.get('currentSample') and not params.get('nativeDefaultSnapshot'):
        label=params['date']
        if date.fromisoformat(label).isoformat() != label:
            raise ValueError('legacy_date_not_canonical')
        period={'kind':'day','label':label}
    elif params.get('snapshotDate'):
        label=params['snapshotDate']
        if date.fromisoformat(label).isoformat() != label:
            raise ValueError('legacy_snapshot_not_canonical')
        period={'kind':'snapshot','anchor':label}
    elif params.get('startDate'):
        period={'kind':'range' if params.get('endDate') is not None else 'native_open_range',
                'start':params['startDate'],'end':params.get('endDate')}
    else:
        period={'kind':'request_anchor','anchor':'legacy-job:'+key}
    identity=TaskIdentity('shaanxi',account_alias,dataset_id(job['datasetId']),period,
        {'legacy_kind':kind,'legacy_params':params},page=params.get('page'),operation=kind)
    identity.document()
    return identity


def decode_journal(journal, account_alias):
    jobs=journal.get('jobs')
    if not isinstance(jobs,dict):
        raise ValueError('shaanxi_jobs_object_required')
    rows=[]
    for key,job in jobs.items():
        errors=[];identity=None
        if not isinstance(job,dict) or job.get('key') != key:
            raise ValueError('shaanxi_journal_key_mismatch')
        try:
            identity=task_identity(job,account_alias)
        except (ValueError,TypeError,KeyError):
            errors.append('identity_mapping_requires_review')
        status=job.get('status')
        if not isinstance(status,str) or not status:
            errors.append('status_missing');status='unreviewed'
        if status=='pending':
            state,consumed='pending',False
            if job.get('reservedAt') or job.get('finishedAt') or job.get('validation'):
                state,consumed='unknown',True;errors.append('pending_with_prior_attempt_evidence')
        elif status in {'saved','empty'}:
            state,consumed=status,True
        elif status in UNFINISHED or status.startswith('held_interrupted'):
            state,consumed='unknown',True;errors.append('unfinished_or_unknown_original')
        elif status.startswith('held'):
            state,consumed='held',True
        elif status in {'expanded','assembled','admitted','empty_sample_not_admitted','skipped'}:
            state,consumed='skipped',True
        else:
            state,consumed='held',True;errors.append('status_unreviewed')
        if job['datasetId'] in journal.get('datasetBlocks',{}):
            errors.append('original_source_protected')
        if job.get('params',{}).get('date') in journal.get('gridDateHolds',{}).get(job['datasetId'],{}):
            errors.append('original_grid_date_protected')
        if job.get('params',{}).get('snapshotDate') in journal.get('listingSnapshotHolds',{}).get(job['datasetId'],{}):
            errors.append('original_listing_snapshot_protected')
        rows.append(LegacyTask(key,identity.dataset if identity else dataset_id(job['datasetId']),
            status,state,consumed,identity,tuple(errors),hashlib.sha256(canonical(job)).hexdigest()))
    return rows


def audit(journal, account_alias):
    rows=decode_journal(journal,account_alias)
    identities={};duplicate_keys=[]
    for row in rows:
        if row.identity:
            prior=identities.get(row.identity.key)
            if prior and prior!=row.legacy_key:
                duplicate_keys.append([prior,row.legacy_key])
            identities[row.identity.key]=row.legacy_key
    return {**summarize(rows),'mode':'shaanxi_full_journal_readonly','website_requests':0,
        'original_counts':dict(Counter(x.legacy_state for x in rows)),
        'kind_counts':dict(Counter(j['kind'] for j in journal['jobs'].values())),
        'mapping_errors':[row.legacy_key for row in rows if row.identity is None],
        'duplicate_pmos_identities':duplicate_keys,
        'protected_source_count':len(journal.get('datasetBlocks',{})),
        'pending_eligible_before_owner_review':sum(row.state=='pending' and not row.blockers for row in rows),
        'online_dispatch_authorized':False}


def import_journal(ledger,journal,account_alias,*,snapshot_sha256):
    return import_shadow(ledger,decode_journal(journal,account_alias),QueueContext('shaanxi',account_alias),
                         source_id='shaanxi-native-journal',snapshot_sha256=snapshot_sha256)
