"""Keep recovered capture times factual; seed a separately named current scan."""
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import argparse,json,time,subprocess,hashlib
from datetime import datetime,timezone,timedelta
from pmos.ledger import Ledger
from pmos.shaanxi_direct_store import DirectStore,encode
from pmos.shaanxi_lifecycle import validate_handoff
from pmos.shaanxi_transport import OriginalWorkerLock,load,atomic
ap=argparse.ArgumentParser();ap.add_argument('--config',type=Path,required=True);ap.add_argument('--previous-release',type=Path,required=True);ap.add_argument('--approval',required=True);ap.add_argument('--run',default='review_20261103_filter_order_fixed');ap.add_argument('--failed-only',action='store_true');a=ap.parse_args();assert a.approval.strip()
config=load(a.config);source,instance=validate_handoff(config,a.previous_release);dest=instance/'reviews/exception_review_20261103'/('snapshot_refresh_'+a.run+'.json');assert a.run.replace('_','').isalnum() and not dest.exists()
assert subprocess.check_output(['systemctl','--user','show','pmos-shaanxi-native.service','-p','MainPID','--value'],text=True).strip()=='0'
lock=OriginalWorkerLock(source);lock.acquire();db=Ledger(instance/'ledger.sqlite3');store=DirectStore(db,config['account_alias'])
try:
    assert not db.unsettled();corrected=[];sources=set();today=datetime.now(timezone(timedelta(hours=8))).date().isoformat();run=a.run;seeded=[]
    with store.transaction():
        for row in ([] if a.failed_only else store.db.execute('SELECT r.job_key,r.proof,j.body FROM sn_cached_recoveries r JOIN sn_jobs j ON j.key=r.job_key').fetchall()):
            fact=json.loads(row['proof']);original=load(fact['original_receipt_ref']);original=original.get('turn',original)
            # Command results wrap the native receipt in a direct result only
            # for monthly children; those are excluded from this recovery.
            raw=json.dumps(original,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
            assert hashlib.sha256(raw).hexdigest()==fact['original_receipt_sha256']
            captured=original['finishedAt'];ts=datetime.fromisoformat(captured.replace('Z','+00:00')).timestamp();job=json.loads(row['body']);before=job['finishedAt'];job['finishedAt']=captured;job['offlineRecoveredAt']=time.time();store.update(job,consumed=True)
            store.db.execute('UPDATE sn_pages SET finished_at=? WHERE job_key=?',(ts,job['key']))
            corrected.append({'job_key':job['key'],'previous_confirmation_time':before,'actual_capture_finished_at':captured})
            if job['kind'] in {'listing','file_listing'}:sources.add(job['datasetId'])
        # Also cover any listing guarded by the just-observed ordering error.
        for row in store.db.execute("SELECT body FROM sn_jobs WHERE status='held_direct_exception' AND kind='listing'"):
            j=json.loads(row[0]);e=j.get('exception',{})
            if e.get('invokeException')=='Error: Native listing source filters changed' and not e.get('observedRequests') and not e.get('pending'):sources.add(j['datasetId'])
        for sid in sorted(sources):
            d=store.source(sid)
            if not d.get('enabled'):continue
            if store.db.execute("SELECT 1 FROM sn_holds WHERE scope='source' AND source_id=?",(sid,)).fetchone():continue
            size=d['nativeListing']['pageSize'] if d['family']=='disclosure_list' else d['nativeFileList']['pageSize'];kind='listing' if d['family']=='disclosure_list' else 'file_listing'
            seeded.append(store.seed(sid,kind,{'snapshotDate':today,'snapshotRun':run,'page':1,'size':size},priority=95,snapshotReview=a.approval))
    atomic(dest,{'at':time.time(),'approval':a.approval,'capture_time_readbacks':corrected,'current_snapshot_run':run,'new_snapshot_jobs':seeded,'website_requests_issued':0,'old_failed_snapshot_identities_preserved':True},exclusive=True)
    print(json.dumps({'capture_times_preserved':len(corrected),'fresh_snapshot_sources':len(seeded),'website_requests_issued':0}))
finally:db.close();lock.close()
