"""Explicit one-time migration from the compatibility owner. No website calls."""
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import argparse,hashlib,importlib,json,sqlite3,subprocess,time,shutil
from datetime import datetime
from pmos.contracts import canonical
from pmos.ledger import Ledger
from pmos.migration.shaanxi import dataset_id
from pmos.shaanxi_direct_store import DirectStore,encode,group_for
from pmos.shaanxi_direct_reporting import freeze_baseline
from pmos.shaanxi_lifecycle import validate_handoff,verify_process,code_fingerprint,frozen_snapshot
from pmos.shaanxi_transport import OriginalWorkerLock,atomic,load,inside


def legacy_facts(store,source,registry):
    turns=0;files=0;unit_pages=0
    # Once only: importing existing issued identities is not dispatch.
    for p in (source/'state/native_turns').glob('*/issued.json'):
        issued=load(p,{});identity=issued.get('identity')
        if not isinstance(identity,dict):raise ValueError('issued_identity_missing')
        key=hashlib.sha256(canonical(identity)).hexdigest()
        store.db.execute('INSERT OR IGNORE INTO sn_legacy_turns VALUES(?,?,?,?,?)',(key,identity.get('jobKey',identity.get('queueJob')),identity.get('action',identity.get('purpose')),encode(identity),str(p)));turns+=1
        fid=identity.get('fileId') or (identity.get('params') or {}).get('fileId')
        if fid:store.db.execute('INSERT OR IGNORE INTO sn_file_claims VALUES(?,?,?,?)',(fid,identity.get('jobKey',identity.get('queueJob',key)),'legacy_issued',encode({'issued':str(p),'automaticReplayAllowed':False})));files+=1
        receipt=load(p.with_name('receipt.json'),{})
        for item in receipt.get('observedRequests',[]):
            if item.get('requestId') and item.get('startedAt'):
                store.db.execute('INSERT OR IGNORE INTO sn_requests VALUES(?,?,?,?,?)',(receipt.get('observedSessionId',''),item['requestId'],key,datetime.fromisoformat(item['startedAt'].replace('Z','+00:00')).timestamp(),item['path']))
    for row in store.db.execute("SELECT body FROM sn_jobs WHERE source_id='1643899919922237440' AND status IN ('saved','empty')").fetchall():
        job=json.loads(row[0])
        if not job.get('validation'):continue
        validation_path=inside(source,job['validation']);v=load(validation_path);digest=v.get('sha256') or v.get('response_sha256')
        raw_ref=v.get('responseFile') or str(Path(v['receipt_directory'])/(digest+'.response')) if v.get('receipt_directory') else v.get('responseFile')
        if not raw_ref:
            request=load(validation_path.with_name('validation_input.json'),{})
            raw_ref=next((x['responseFile'] for x in request.get('turn',{}).get('responses',[]) if x.get('sha256')==digest and x.get('responseFile')),None)
        raw_path=inside(source,raw_ref);raw=raw_path.read_bytes()
        if hashlib.sha256(raw).hexdigest()!=digest:raise ValueError('legacy_unit_validation_hash_changed')
        group=group_for(job)
        for record in v.get('rows',[]):
            natural=encode([record.get('date'),record.get('MEAS_TYPE'),record.get('UNIT_ID')])
            old=store.db.execute('SELECT job_key FROM sn_unit_keys WHERE group_key=? AND natural_key=?',(group,natural)).fetchone()
            if old and old[0]!=job['key']:store.hold('date',job['datasetId'],job['params']['date'],{'reason':'legacy_cross_page_unit_duplicate','jobs':[old[0],job['key']]})
            store.db.execute('INSERT OR IGNORE INTO sn_unit_keys VALUES(?,?,?)',(group,natural,job['key']))
        unit_pages+=1
    return {'issued_turns_imported':turns,'issued_file_facts':files,'unit_pages_verified':unit_pages}


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--config',type=Path,required=True);ap.add_argument('--previous-release',type=Path,required=True);ap.add_argument('--expected-pid',type=int,required=True);ap.add_argument('--approval',required=True);a=ap.parse_args()
    if not a.approval.strip():raise ValueError('explicit_migration_authorization_required')
    prior=a.config.read_bytes();config=json.loads(prior);source,instance=validate_handoff(config,a.previous_release)
    if config.get('direct'):raise ValueError('direct_migration_already_prepared')
    owner=load(instance/'state/native_owner.json',{});verify_process(owner['start_identity'])
    if owner.get('pid')!=a.expected_pid:raise ValueError('previous_owner_changed')
    active=subprocess.check_output(['systemctl','--user','show','pmos-shaanxi-native.service','-p','MainPID','--value'],text=True).strip()
    if active!=str(a.expected_pid):raise ValueError('previous_service_owner_changed')
    subprocess.run(['systemctl','--user','stop','pmos-shaanxi-native.service'],check=True,timeout=620)
    lock=OriginalWorkerLock(source);db=None;committed=False
    try:
        lock.acquire()
        # Wait only for the unchanged keeper's current shared turn to end.
        until=time.time()+45
        while (source/'state/turn.lock').exists() and time.time()<until:time.sleep(.25)
        if (source/'state/turn.lock').exists() or (source/'state/collection_queue/pending_command.json').exists():raise ValueError('native_turn_pending')
        if (source/'state/session_block.json').exists():raise ValueError('existing_session_protection_preserved')
        db=Ledger(instance/'ledger.sqlite3')
        if db.unsettled():raise ValueError('previous_attempt_unsettled')
        rawfile,digest,journal=frozen_snapshot(source,instance);registry=load(source/'config/spot_registry.json')
        # Full database backup, including every old attempt and tombstone.
        directory=instance/'direct_migration';directory.mkdir(parents=True,exist_ok=True)
        backup=sqlite3.connect(directory/'before.sqlite3');db.db.backup(backup);backup.close()
        store=DirectStore(db,config['account_alias'])
        if store.db.execute('SELECT 1 FROM sn_imports').fetchone():raise ValueError('existing_staging_import_requires_review')
        scope=db.handoff()['scope']
        if any(dataset_id(d['id']) not in scope for d in registry['datasets'] if d.get('enabled')):raise ValueError('enabled_source_outside_original_handoff')
        store.import_frozen(journal,registry,sha256=digest,source_ref=str(rawfile))
        with store.transaction():facts=legacy_facts(store,source,registry)
        atomic(instance/'direct_config/registry.json',registry,exclusive=True)
        monthly=load(source/'config/monthly_attachment_native_contract.json');atomic(instance/'direct_config/monthly_contract.json',monthly,exclusive=True)
        sys.path.insert(0,str(source/'reporting'));module=importlib.import_module('snapshot')
        baseline=freeze_baseline(module,source,instance,store,journal,registry)
        for command in (instance/'native_commands').glob('*.trace.json'):
            trace=load(command)
            for ref in trace.get('native_turn_receipts',[]):
                receipt=load(inside(source,ref['path']),{})
                for item in receipt.get('responses',[])+receipt.get('failures',[])+receipt.get('pending',[]):
                    if item.get('requestId') and item.get('startedAt'):store.db.execute('INSERT OR IGNORE INTO sn_requests VALUES(?,?,?,?,?)',(receipt.get('observedSessionId',''),item['requestId'],receipt['key'],datetime.fromisoformat(item['startedAt'].replace('Z','+00:00')).timestamp(),item['path']))
        new_sha=code_fingerprint(ROOT);receipt=db.handoff();prev=db.db.execute('SELECT new_code_sha FROM native_release_upgrades ORDER BY id DESC LIMIT 1').fetchone()
        if (prev[0] if prev else receipt['code_sha'])!=config['code_sha']:raise ValueError('previous_upgrade_chain_changed')
        stat=(source/'state/collection_queue/journal.json').stat();proof={'source_journal':{'sha256':digest,'inode':stat.st_ino,'size':stat.st_size,'mtime_ns':stat.st_mtime_ns},'jobs':len(journal['jobs']),'baseline':baseline,'facts':facts,'previous_code_sha':config['code_sha'],'new_code_sha':new_sha,'previous_release':str(a.previous_release),'approval':a.approval,'at':time.time(),'attempts_preserved':db.db.execute('select count(*) from attempts').fetchone()[0],'legacy_records_preserved':db.db.execute('select count(*) from legacy_records').fetchone()[0]}
        policy=load(source/'config/session_policy.json',{})
        atomic(directory/'before_config.json',config,exclusive=True);atomic(directory/'acceptance_prepare.json',proof,exclusive=True)
        with store.transaction():
            db.db.execute('INSERT INTO native_release_upgrades(previous_code_sha,new_code_sha,handoff_receipt_sha,approval,at) VALUES(?,?,?,?,?)',(config['code_sha'],new_sha,receipt['receipt_sha256'],a.approval,time.time()))
            store.set_setting('migration_proof',proof)
        config={**config,'code_sha':new_sha,'phase':'direct_migration_prepared_no_dispatch','direct':{'mode':'sqlite_native','snapshot_sha256':digest,'snapshot_ref':str(rawfile),'proof':str(directory/'acceptance_prepare.json'),'non_business_paths':policy.get('nonBusinessPaths',[]),'node_binary':shutil.which('node')}}
        # Non-business additions in the original policy are objects; the
        # standard observed, readonly paths are copied explicitly as well.
        config['direct']['non_business_paths']=sorted(set(['/px-gateway/exception/sendError','/px-settlement-infpubquery/ipCommitmentApi/queryIsCommited']+[x if isinstance(x,str) else x['path'] for x in config['direct']['non_business_paths']]))
        atomic(a.config,config);committed=True
        print(json.dumps({'phase':'direct_migration_prepared_no_dispatch','new_code_sha':new_sha,'proof':proof},ensure_ascii=False))
    finally:
        if db:db.close()
        lock.close()
        if not committed:subprocess.run(['systemctl','--user','start','pmos-shaanxi-native.service'],check=True)

if __name__=='__main__':main()
