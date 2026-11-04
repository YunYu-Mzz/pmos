"""Explicit, offline reverse export; never restore an obsolete queue snapshot."""
import hashlib,json,os,time
from pathlib import Path
from .ledger import Ledger
from .shaanxi_direct_store import DirectStore,key_for,stamp
from .shaanxi_transport import atomic,load


def export_direct(config):
    source=Path(config['source_root']);instance=Path(config['instance']);target=source/'state/pmos_direct_export'
    db=Ledger(instance/'ledger.sqlite3')
    try:
        store=DirectStore(db,config['account_alias'])
        if db.unsettled() or store.db.execute("SELECT 1 FROM sn_holds WHERE scope='target' LIMIT 1").fetchone():raise ValueError('unknown_direct_operation_requires_review_before_legacy_restore')
        original=source/'state/collection_queue/journal.json';raw=original.read_bytes();proof=store.setting('migration_proof')['source_journal']
        if hashlib.sha256(raw).hexdigest()!=proof['sha256']:raise ValueError('frozen_legacy_queue_changed_requires_merge_review')
        def rewrite(v):
            if isinstance(v,str) and v.startswith(str(instance)+'/'):return str(target/v[len(str(instance))+1:])
            if isinstance(v,list):return [rewrite(x) for x in v]
            if isinstance(v,dict):return {k:rewrite(x) for k,x in v.items()}
            return v
        for directory in ('direct_parsed','native_turns','direct_commands','direct_completed'):
            base=instance/directory
            if not base.exists():continue
            for p in base.rglob('*'):
                if not p.is_file():continue
                if p.is_symlink():raise ValueError('export_symlink_not_allowed')
                dest=target/p.relative_to(instance);dest.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
                if p.suffix=='.json':atomic(dest,rewrite(load(p)))
                elif not dest.exists():os.link(p,dest)
                elif hashlib.sha256(p.read_bytes()).hexdigest()!=hashlib.sha256(dest.read_bytes()).hexdigest():raise ValueError('immutable_export_conflict')
        journal=store.setting('legacy_non_job_metadata',{});journal['jobs']={row['key']:rewrite(json.loads(row['body'])) for row in store.db.execute('SELECT key,body FROM sn_jobs')}
        for row in store.db.execute('SELECT * FROM sn_file_claims'):
            job=store.job(row['job_key'])
            if not job:continue
            params=job['params'];key=key_for(job['datasetId'],'download',params);existing=journal['jobs'].get(key)
            if existing and existing['status']!='pending':continue
            journal['jobs'][key]={**(existing or {}),'key':key,'datasetId':job['datasetId'],'kind':'download','params':params,'status':'held_pmos_consumed_file','createdAt':job.get('createdAt',stamp()),'finishedAt':stamp(),'error':'PMOS file consumption preserved; no replay','consumptionProof':json.loads(row['proof']),'originalPmosJob':row['job_key']}
        # Preserve all non-job historical metadata and every imported hold.
        journal['pageGroups']={}
        for row in store.db.execute('SELECT * FROM sn_page_groups'):
            body=json.loads(row['body']);body.update(total=row['total'],complete=bool(row['complete']),keys=[r[0] for r in store.db.execute('SELECT record_key FROM sn_page_keys WHERE group_key=?',(row['key'],))],pages=[r[0] for r in store.db.execute('SELECT page FROM sn_pages WHERE group_key=?',(row['key'],))]);journal['pageGroups'][row['key']]=body
        journal.update(datasetBlocks={},gridDateHolds={},listingSnapshotHolds={},lastSeedDay=store.setting('last_seed_day'))
        for row in store.db.execute('SELECT * FROM sn_holds'):
            body=json.loads(row['body'])
            if row['scope']=='source':journal['datasetBlocks'][row['source_id']]=body
            elif row['scope'] in {'date','snapshot'}:journal['gridDateHolds' if row['scope']=='date' else 'listingSnapshotHolds'].setdefault(row['source_id'],{})[row['anchor']]=body
        atomic(instance/'direct_migration/rollback_export_before.json',{'sha256':hashlib.sha256(raw).hexdigest(),'at':time.time(),'new_tasks':len(journal['jobs'])})
        atomic(original,journal)
        atomic(instance/'direct_migration/rollback_export_complete.json',{'at':time.time(),'sha256':hashlib.sha256(original.read_bytes()).hexdigest(),'tasks':len(journal['jobs']),'original_snapshot_restored':False,'unknown_or_failed_replayed':False})
    finally:db.close()
