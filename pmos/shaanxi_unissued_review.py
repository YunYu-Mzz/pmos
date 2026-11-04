"""Append-only, individually reviewed FIRST downloads, never a retry policy."""
import hashlib
import json
from pathlib import Path

from .contracts import canonical
from .ledger import Ledger

SOURCE = 'disclosure:388bd8af-e533-404e-bb95-16304fde2378'
DOWNLOAD = '/sn-trade-out/SignController/xxplDownload'
SCHEMA = '''CREATE TABLE IF NOT EXISTS sn_unissued_file_reviews(
 review_id TEXT PRIMARY KEY, job_key TEXT UNIQUE NOT NULL,
 identity_key TEXT UNIQUE NOT NULL, file_key TEXT UNIQUE NOT NULL,
 approval TEXT NOT NULL, proof TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS sn_unsent_preflight_reviews(
 review_id TEXT PRIMARY KEY, job_key TEXT UNIQUE NOT NULL,
 identity_key TEXT UNIQUE NOT NULL, file_key TEXT,
 approval TEXT NOT NULL, proof TEXT NOT NULL, created_at REAL NOT NULL);'''


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def prove_open_only(plan, receipt, original):
    """The rejected menu operation is NOT the file's business download."""
    revision = original.get('planRevision', 0)
    identity = {'queueJob': original['key'], 'action': 'open' + ('_r' + str(revision) if revision else ''),
                'source': SOURCE, 'kind': 'download', 'params': original['params'],
                'planRevision': revision, 'nativeFilters': original.get('nativeFilters', {})}
    if original['datasetId'] != SOURCE or original['kind'] != 'download' or original['status'] != 'held_request_failed_412':
        return False
    if plan.get('op') != 'nativeTurn' or plan.get('discovery') is not True or plan.get('expectedPath') != '/__native_menu_discovery__':
        return False
    if plan.get('identity') != identity or receipt.get('identity') != identity:
        return False
    if any(word in plan.get('expression', '').lower() for word in ('donwload', 'xxpldownload')):
        return False
    if receipt.get('phase') != 'held' or receipt.get('reason') != 'request_failed_412' or any(receipt.get(k) for k in ('pending', 'failures', 'blockingFailures', 'bodyReadsPending')):
        return False
    responses = receipt.get('responses', [])
    if not any(x.get('kind') == 'native_document' and x.get('status') == 412 for x in responses):
        return False
    if any(x.get('path') == DOWNLOAD for x in responses + receipt.get('observedRequests', [])):
        return False
    denied = {401, 403, 423, 429, 500, 502, 503, 504}
    return not any(str(x.get(k)) in {str(n) for n in denied}
                   for x in responses for k in ('status', 'businessStatus', 'businessCode', 'errorCode'))


class UnissuedFileLedger(Ledger):
    """Only this Shaanxi direct owner can review an imported navigation claim."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.db.executescript(SCHEMA)

    def preflight_review(self, *, identity_key=None, file_key=None, job=None):
        field, value = ('identity_key', identity_key) if identity_key else ('file_key', file_key)
        row = self.db.execute('SELECT * FROM sn_unsent_preflight_reviews WHERE ' + field + '=?', (value,)).fetchone()
        if not row or not row['approval']:
            return None
        try:
            proof = json.loads(row['proof']);original = proof['original_job']
            raw = Path(proof['result_ref']).read_bytes()
            if hashlib.sha256(raw).hexdigest() != proof['result_sha256'] or json.loads(raw).get('result', {}).get('job') != original:
                return None
            if original['error'] != 'Error: No target with given id found' or original['status'] != 'held' or original['kind'] not in {'table','download'}:
                return None
            directory = Path(proof['original_task_directory'])
            if directory.name != original['key'] or directory.exists() and any(directory.iterdir()):
                return None
            if self.db.execute('SELECT 1 FROM sn_legacy_turns WHERE job_key=?', (original['key'],)).fetchone():
                return None
            source = self.db.execute('SELECT sha256 FROM sn_sources WHERE id=?', (original['datasetId'],)).fetchone()
            if not source or source[0] != proof['source_sha256']:
                return None
            legacy = [dict(r) for r in self.db.execute('SELECT * FROM legacy_records WHERE identity_key=? ORDER BY source_id,snapshot_sha256,legacy_key', (row['identity_key'],))]
            if not legacy or any(r['legacy_key'] != original['key'] or r['state'] != 'held' for r in legacy) or digest(legacy) != proof['legacy_sha256']:
                return None
            if job is not None and (job['key'] != original['key'] or job['params'] != original['params'] or job.get('legacyUnsentReview') != row['review_id']):
                return None
            if row['file_key']:
                tombstones = [dict(r) for r in self.db.execute("SELECT * FROM file_tombstones WHERE file_key=? AND source_ref NOT LIKE 'attempt:%' ORDER BY source_ref", (row['file_key'],))]
                claim = self.db.execute('SELECT job_key,state FROM sn_file_claims WHERE file_id=?', (original['params']['fileId'],)).fetchone()
                if digest(tombstones) != proof['legacy_file_sha256'] or not claim or tuple(claim) != (original['key'],'held'):
                    return None
            return dict(row)
        except (KeyError, TypeError, ValueError, OSError):
            return None

    def file_review(self, *, identity_key=None, file_key=None, job=None):
        field, value = ('identity_key', identity_key) if identity_key else ('file_key', file_key)
        row = self.db.execute('SELECT * FROM sn_unissued_file_reviews WHERE ' + field + '=?', (value,)).fetchone()
        if not row or not row['approval']:
            return self.preflight_review(identity_key=identity_key,file_key=file_key,job=job)
        try:
            proof = json.loads(row['proof'])
            oldrow = self.db.execute('SELECT * FROM sn_jobs WHERE key=?', (proof['skippedJob'],)).fetchone()
            original = json.loads(oldrow['body'])
            if oldrow['source_id'] != SOURCE or digest(original) != proof['original_job_sha256']:
                return None
            source = self.db.execute('SELECT sha256,body FROM sn_sources WHERE id=?', (SOURCE,)).fetchone()
            if source['sha256'] != proof['source_sha256']:
                return None
            planfile, receiptfile = Path(proof['plan_ref']), Path(proof['receipt_ref'])
            if planfile.parent != receiptfile.parent or planfile.parent.name != original['key']:
                return None
            planraw, receiptraw = planfile.read_bytes(), receiptfile.read_bytes()
            if hashlib.sha256(planraw).hexdigest() != proof['plan_sha256'] or hashlib.sha256(receiptraw).hexdigest() != proof['receipt_sha256']:
                return None
            plan, receipt = json.loads(planraw), json.loads(receiptraw)
            if plan['expression'] != json.loads(source['body'])['openExpression'] or not prove_open_only(plan, receipt, original):
                return None
            if any(p.name.startswith('download') or p.name.startswith('query') for p in planfile.parent.iterdir()):
                return None
            legacy = [dict(r) for r in self.db.execute('SELECT * FROM legacy_records WHERE identity_key=? ORDER BY source_id,snapshot_sha256,legacy_key', (row['identity_key'],))]
            if not legacy or any(r['legacy_key'] != original['key'] or r['state'] != 'held' for r in legacy) or digest(legacy) != proof['legacy_sha256']:
                return None
            tombstones = [dict(r) for r in self.db.execute("SELECT * FROM file_tombstones WHERE file_key=? AND source_ref NOT LIKE 'attempt:%' ORDER BY source_ref", (row['file_key'],))]
            if digest(tombstones) != proof['legacy_file_sha256']:
                return None
            claim = self.db.execute('SELECT * FROM sn_file_claims WHERE file_id=?', (original['params']['fileId'],)).fetchone()
            if not claim or claim['job_key'] != original['key'] or claim['state'] != 'held_request_failed_412':
                return None
            if job is not None and (job['key'] != row['job_key'] or job.get('unissuedRecovery', {}).get('skippedJob') != original['key'] or job['params'] != original['params']):
                return None
            return dict(row)
        except (KeyError, TypeError, ValueError, OSError):
            return None

    def reviewed_unissued_file(self, job):
        row = self.db.execute('SELECT identity_key FROM sn_unissued_file_reviews WHERE job_key=?', (job['key'],)).fetchone()
        if not row:
            row = self.db.execute('SELECT identity_key FROM sn_unsent_preflight_reviews WHERE job_key=? AND file_key IS NOT NULL', (job['key'],)).fetchone()
        return bool(row and self.file_review(identity_key=row[0], job=job))

    def begin(self, kind, dataset=None, period=None, scope='', *, identity=None):
        if identity and kind in {'collect','sample'}:
            review = self.file_review(identity_key=identity.key)
            if review and period != review['job_key']:
                raise ValueError('review_is_bound_to_one_exact_job')
        return super().begin(kind,dataset,period,scope,identity=identity)

    def consumed_identity(self, identity_key):
        if not self.file_review(identity_key=identity_key):
            return super().consumed_identity(identity_key)
        # A new attempt always consumes the file, including failed/unknown.
        return bool(self.db.execute("SELECT 1 FROM attempt_identities i JOIN attempts a ON a.id=i.attempt_id WHERE i.identity_key=? AND a.kind IN ('collect','sample') AND a.outcome!='not_sent' LIMIT 1", (identity_key,)).fetchone())

    def consumed_file(self, file_key):
        review = self.file_review(file_key=file_key)
        if not review:
            return super().consumed_file(file_key)
        return bool(self.db.execute("SELECT 1 FROM attempt_identities i JOIN attempts a ON a.id=i.attempt_id WHERE i.identity_key=? AND a.kind IN ('collect','sample') AND a.outcome!='not_sent' LIMIT 1", (review['identity_key'],)).fetchone())

    def claim_file(self, file_key, identity_key, attempt):
        if self.file_review(file_key=file_key):
            # The original import tombstone is immutable. The newly inserted
            # attempt_identities row consumes this reviewed file from now on.
            return
        return super().claim_file(file_key, identity_key, attempt)
