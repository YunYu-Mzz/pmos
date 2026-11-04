"""Explicit reviewed repairs of stored originals and proven pre-request guards."""
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import argparse,hashlib,json,time,subprocess,uuid
from pmos.contracts import canonical
from pmos.migration.shaanxi import task_identity
from pmos.province import Province
from pmos.shaanxi_direct_runner import DirectEngine,DirectRunner
from pmos.shaanxi_direct_store import DirectStore,encode
from pmos.shaanxi_direct_transport import DirectTransport
from pmos.shaanxi_exception_review import completed_receipt,BROWSE_HISTORY
from pmos.shaanxi_lifecycle import validate_handoff
from pmos.shaanxi_transport import OriginalWorkerLock,atomic,load,inside

ap=argparse.ArgumentParser();ap.add_argument('--config',type=Path,required=True);ap.add_argument('--previous-release',type=Path,required=True);ap.add_argument('--approval',required=True);a=ap.parse_args()
assert a.approval.strip()
config=load(a.config);source,instance=validate_handoff(config,a.previous_release)
review=instance/'reviews/exception_review_20261103';assert not (review/'applied.json').exists(),'review_already_applied'
for service in ['pmos-shaanxi-native.service','shaanxi-queue-worker.service']:
    assert subprocess.check_output(['systemctl','--user','show',service,'-p','MainPID','--value'],text=True).strip()=='0'
assert not (source/'state/session_block.json').exists() and not (source/'state/turn.lock').exists()
keeper=load(source/'state/keeper/status.json');proof=load(source/'state/keeper/last_success.json')
from datetime import datetime
assert proof['passed'] and proof['fullSampleMatches'] and time.time()-datetime.fromisoformat(proof['finishedAt'].replace('Z','+00:00')).timestamp()<600
assert not keeper.get('pending') and not keeper.get('requiresReview')
lock=OriginalWorkerLock(source);lock.acquire();engine=DirectEngine(Province.load('shaanxi',ROOT/'provinces'),None,instance=instance,account_alias=config['account_alias']);engine.acquire()
try:
    store=DirectStore(engine.ledger,config['account_alias']);assert not engine.ledger.unsettled()
    transport=DirectTransport(config,ROOT,store,lock);runner=DirectRunner.__new__(DirectRunner);runner.store=store;runner.engine=engine;runner.config=config
    initial=[dict(r) for r in store.db.execute("SELECT * FROM sn_jobs WHERE status LIKE 'held%'")]
    atomic(review/'jobs_before_apply.json',initial,exclusive=True);atomic(review/'config_before_apply.json',config,exclusive=True)
    journal=source/'state/collection_queue/journal.json';journal_sha=hashlib.sha256(journal.read_bytes()).hexdigest()
    recovered=[];rejected=[]
    for row in initial:
        if row['status']!='held_direct_exception':continue
        job=json.loads(row['body']);d=store.source(job['datasetId']);candidates=[]
        if d.get('family')=='curve':
            for command in store.db.execute("SELECT * FROM sn_commands WHERE job_key=? AND stage='query' AND result_ref IS NOT NULL",(job['key'],)):
                candidates.append((load(command['result_ref']),command['result_ref']))
        elif job['kind'] in {'listing','file_listing'} and job['params'].get('page')==1:
            for f in (instance/'direct_parsed'/job['key']).glob('*/validation_input.json'):
                value=load(f)
                if value['dataset']['id']==d['id'] and all(value['params'].get(k)==job['params'].get(k) for k in ['snapshotDate','page','size']):candidates.append((value['turn'],str(f)))
        for original,ref in candidates:
            try:
                derived=completed_receipt(original)
                value=transport.validate_receipt(d,job,derived)
                value['captured_at']=datetime.fromisoformat(derived['finishedAt'].replace('Z','+00:00')).timestamp()
                identity=task_identity(job,config['account_alias']);engine.ledger.seed(identity.dataset,[job['key']])
                transport.latest=(value['source_sha256'],value['source_path']);engine.responses.link_from=transport
                engine.responses.store(identity.dataset,job['key'],value['body'],'.json',scope='review_cached',reviewed=True)
                engine.archive.link_from=engine.responses;engine.archive.store(identity.dataset,job['key'],value['body'],'.json',legacy_source=job['datasetId'],collector='pmos_direct_offline_review')
                assert runner.confirm(job,value)
                fact={'job_key':job['key'],'source_id':job['datasetId'],'outcome':value['validation']['outcome'],'original_receipt_ref':ref,'original_receipt_sha256':hashlib.sha256(canonical(original)).hexdigest(),'raw_sha256':value['source_sha256'],'website_requests_issued':0}
                store.db.execute('INSERT INTO sn_cached_recoveries VALUES(?,?,?,?)',(job['key'],a.approval,encode(fact),time.time()));atomic(Path(value['validation_path']).parent/'offline_review.json',fact);recovered.append(fact);break
            except Exception as exc:rejected.append({'job_key':job['key'],'error':str(exc)[:240]})
    menu=load(review/'native_menu.json');assert menu['ok'] and menu['result']['matched']
    labels={x['guid']:x for x in menu['result']['menu'] if x.get('guid')}
    ids=['479262fa-64dc-4141-97ab-567e54b87e0f','a6cd751e-573b-400f-be29-7227602cfa8f','072ba033-a18a-4783-9700-9e98ecf0185f','74f02ee8-2610-4f26-81a8-16824bfc2692'];repaired={}
    for guid in ids:
        sid='disclosure:'+guid;d=store.source(sid);native=labels[guid]
        old='x.label==='+json.dumps(d['name'],ensure_ascii=False)
        assert native['viewname']==d['url'] and native['openWay']=='1' and old in d['openExpression']
        before=d.copy();d={**d,'nativeMenuLabel':native['label'],'openExpression':d['openExpression'].replace(old,'x.label==='+json.dumps(native['label'],ensure_ascii=False),1)}
        atomic(review/('source_before_'+guid+'.json'),before,exclusive=True)
        store.db.execute('UPDATE sn_sources SET body=?,sha256=? WHERE id=?',(encode(d),hashlib.sha256(canonical(d)).hexdigest(),sid));repaired[sid]=d
    requeued=[]
    for row in initial:
        if row['status']!='held_direct_exception' or row['source_id'] not in repaired or row['kind']!='table':continue
        job=json.loads(row['body']);e=job.get('exception',{})
        if e.get('stage')!='open' or not e.get('invokeError') or e.get('observedRequests') or e.get('responses') or e.get('pending') or e.get('failures'):continue
        commands=store.db.execute('SELECT * FROM sn_commands WHERE job_key=?',(job['key'],)).fetchall()
        if len(commands)!=1 or commands[0]['stage']!='open':continue
        command=commands[0];old=store.db.execute('SELECT * FROM attempts WHERE id=?',(command['attempt'],)).fetchone()
        active=store.db.execute("SELECT id FROM attempts WHERE period=? AND kind='collect' AND outcome!='not_sent' AND id NOT IN (SELECT attempt_id FROM released_attempts)",(job['key'],)).fetchall()
        if old['outcome'] not in {'failed','unknown'} or len(active)!=1 or active[0]['id']!=old['id']:continue
        identity=task_identity(job,config['account_alias'])
        if store.db.execute('SELECT 1 FROM legacy_records WHERE identity_key=? AND consumed=1',(identity.key,)).fetchone():continue
        path=inside(instance,command['result_ref']);receipt=load(path)
        assert receipt['key']==e['key'] and receipt.get('invokeError') and not receipt.get('responses') and not receipt.get('observedRequests')
        token='review_20261103_'+job['key'];fact={'kind':'menu_label_renamed_before_native_handler','original_attempt':old['id'],'original_outcome':old['outcome'],'original_receipt':str(path),'original_receipt_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'repaired_source_sha256':hashlib.sha256(canonical(repaired[job['datasetId']])).hexdigest(),'observed_http_requests':0,'approval':a.approval}
        with store.transaction():
            store.db.execute('INSERT INTO sn_exception_reviews VALUES(?,?,?,?,?)',(token,job['key'],a.approval,encode(fact),time.time()))
            store.db.execute('INSERT INTO released_attempts VALUES(?,?,?)',(old['id'],a.approval,time.time()))
            updated={k:v for k,v in job.items() if k not in {'error','exception','finishedAt','reservedAt','pmosAttempt'}};updated['manualReview']=token
            store.update(updated,state='pending',consumed=False)
            store.db.execute("UPDATE tasks SET status='pending',reason=?,updated_at=? WHERE dataset=? AND period=?",('explicit_review_menu_binding_fixed',time.time(),identity.dataset,job['key']))
        requeued.append({'job_key':job['key'],'review_id':token,**fact})
    # Exact native browse-history write observed successful alongside business
    # data. It is auxiliary; primary/auth endpoints remain outside this list.
    paths=config['direct'].setdefault('non_business_paths',[])
    if BROWSE_HISTORY not in paths:paths.append(BROWSE_HISTORY)
    atomic(a.config,config)
    assert hashlib.sha256(journal.read_bytes()).hexdigest()==journal_sha
    result={'at':time.time(),'approval':a.approval,'cached_recovered':recovered,'requeued_proven_unsent':requeued,'cached_rejections':rejected,'repaired_source_ids':list(repaired),'source_journal_unchanged':True,'website_requests_issued_by_review':0}
    atomic(review/'applied.json',result,exclusive=True)
    print(json.dumps({'cached_recovered':len(recovered),'cached_outcomes':{s:sum(x['outcome']==s for x in recovered) for s in ['saved','empty']},'requeued_proven_unsent':len(requeued),'cached_rejections':len(rejected),'source_journal_unchanged':True,'website_requests_issued':0}))
finally:engine.release();lock.close()
