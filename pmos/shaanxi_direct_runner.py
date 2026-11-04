"""One PMOS owner, SQLite frontier, direct native browser operations."""
from __future__ import annotations
import asyncio,hashlib,json,os,signal,time
from dataclasses import replace
from pathlib import Path
from .contracts import canonical
from .engine import Engine
from .faults import Fault
from .migration.shaanxi import task_identity
from .province import Province
from .shaanxi_direct_store import DirectStore,encode,stamp
from .shaanxi_direct_transport import DirectTransport,BrowserRPC,MONTHLY
from .shaanxi_direct_pdf import DeferredPDFs
from .shaanxi_lifecycle import validate_handoff,verify_process,process_identity
from .shaanxi_transport import OriginalWorkerLock,atomic,load,inside


class DirectEngine(Engine):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        from .shaanxi_unissued_review import UnissuedFileLedger
        self.ledger.close()
        self.ledger=UnissuedFileLedger(self.instance/'ledger.sqlite3',self.clock)

    async def collect_direct(self,job):
        identity=task_identity(job,self.account_alias);self.ledger.seed(identity.dataset,[job['key']])
        self.decode_response=lambda raw:self.transport.value['parsed'];self.responses.link_from=self.transport
        result=await self.request('collect',{'transport':'native','job_key':job['key']},{'kind':'document' if job['kind'] in {'download','monthly_attachment'} else 'json'},dataset=identity.dataset,period=job['key'],identity=identity)
        self.archive.link_from=self.responses
        result.record=self.archive.store(identity.dataset,job['key'],result.response.body,result.outcome.extension,legacy_source=job['datasetId'],http_status=result.response.status,collector='pmos_direct')
        return result


class DirectRunner:
    def __init__(self,config,code_root):
        self.config=config;self.code=Path(code_root).resolve();self.source,self.instance=validate_handoff(config,self.code)
        if config.get('direct',{}).get('mode')!='sqlite_native':raise Fault('direct_migration_not_prepared','local')
        self.lock=OriginalWorkerLock(self.source);self.stopping=False;self.engine=None;self.transport=None
        self.counts={'saved':0,'empty':0,'failed':0,'expanded':0};self.owner_path=self.instance/'state/native_owner.json'

    def acquire(self):
        self.lock.acquire()
        try:
            province=Province.load('shaanxi',self.code/'provinces');province.policy=replace(province.policy,request_timeout_seconds=400)
            self.engine=DirectEngine(province,None,instance=self.instance,account_alias=self.config['account_alias']);self.engine.acquire()
            self.store=DirectStore(self.engine.ledger,self.config['account_alias'])
            imported=self.store.db.execute('SELECT sha256 FROM sn_imports').fetchall()
            if len(imported)!=1 or imported[0][0]!=self.config['direct']['snapshot_sha256']:raise Fault('direct_frozen_import_missing','local')
            receipt=self.engine.ledger.handoff();current=receipt['code_sha']
            for upgrade in self.store.db.execute('SELECT * FROM native_release_upgrades ORDER BY id'):
                if upgrade['previous_code_sha']!=current or upgrade['handoff_receipt_sha']!=receipt['receipt_sha256'] or not upgrade['approval']:raise Fault('native_release_upgrade_chain_invalid','local')
                current=upgrade['new_code_sha']
            if current!=self.config['code_sha']:raise Fault('direct_release_upgrade_missing','local')
            self.transport=DirectTransport(self.config,self.code,self.store,self.lock,response_interval_seconds=self.engine.policy.response_interval_seconds);self.engine.transport=self.transport
            self.reconcile()
            self.pdfs=DeferredPDFs(self.store,self.instance)
            atomic(self.owner_path,{'generation':self.config['generation'],'pid':os.getpid(),'start_identity':process_identity(os.getpid()),'code_sha':self.config['code_sha'],'phase':'owned','mode':'pmos_direct','at':time.time()})
        except BaseException:
            if self.engine:self.engine.release()
            self.lock.close();raise

    def status(self,phase,**extra):
        self.engine.state.status(collector='pmos',mode='pmos_direct',phase=phase,generation=self.config['generation'],native_operations_used=self.engine.ledger.attempts_since_handoff(),operations_budget=self.engine.ledger.request_budget(),budget_unit='native_task_operations_http_stages_recorded_separately',new_tasks=self.counts,**extra)

    def check_owner(self):
        self.lock.check();owner=load(self.owner_path,{})
        if owner.get('pid')!=os.getpid() or owner.get('generation')!=self.config['generation']:raise Fault('direct_owner_changed','local')
        for field in ('browser','observer','keeper'):verify_process(self.config[field])

    def confirm(self,job,value):
        identity=task_identity(job,self.config['account_alias'])
        with self.store.transaction():
            okay=self.store.settle(job,value['validation'],value['validation_path'],value['source_sha256'],value['source_path'],len(value['body']),captured_at=value.get('captured_at'))
            state=value['validation']['outcome'] if okay else 'failed';digest=value['source_sha256'] if okay else None
            # Same connection/transaction as frontier, pages and children.
            # Ledger.settle_task owns its transaction and cannot nest here.
            prior=self.engine.ledger.task(identity.dataset,job['key'])
            if prior is None:raise Fault('direct_common_task_missing','local')
            if prior['status']!=state or digest and prior['artifact_sha256']!=digest:
                self.store.db.execute('UPDATE tasks SET status=?,artifact_sha256=COALESCE(?,artifact_sha256),reason=?,attempts=attempts+1,updated_at=? WHERE dataset=? AND period=? AND scope=?',(state,digest,None if okay else 'cross_page_validation_failed',time.time(),identity.dataset,job['key'],''))
            if okay:
                self.store.db.execute('INSERT OR IGNORE INTO sn_source_artifacts VALUES(?,?)',(job['datasetId'],value['source_sha256']))
                self.store.db.execute('INSERT OR IGNORE INTO sn_artifact_metadata VALUES(?,?,?)',(value['source_sha256'],Path(value['source_path']).stat().st_mtime,int(value['body'].startswith(b'%PDF-'))))
        return okay

    def recover_value(self,job,completed):
        validation_path=inside(self.instance,completed['validation']);v=load(validation_path);p=inside(self.instance,completed['raw_path']);body=p.read_bytes()
        if hashlib.sha256(body).hexdigest()!=completed['raw_sha256'] or (v.get('sha256') or v.get('response_sha256'))!=completed['raw_sha256']:raise Fault('direct_recovery_original_changed','local')
        return {'validation':v,'validation_path':str(validation_path),'body':body,'source_path':p,'source_sha256':completed['raw_sha256'],'status':completed['http_status']}

    def reconcile(self):
        # Only rows from this direct owner. Compatibility history is retained
        # as-is and must be settled before the explicit migration gate.
        rows=self.store.db.execute("SELECT a.* FROM attempts a JOIN sn_jobs j ON j.key=a.period WHERE a.outcome='pending' OR (j.status IN ('running','pending') AND j.consumed=1 AND a.id=(SELECT max(c.attempt) FROM sn_commands c WHERE c.job_key=j.key)) ORDER BY a.id").fetchall()
        for attempt in rows:
            job=self.store.job(attempt['period']);completed=load(self.instance/'direct_completed'/(job['key']+'.json'))
            if completed:
                value=self.recover_value(job,completed);identity=task_identity(job,self.config['account_alias'])
                self.engine.responses.link_from=self.transport;self.transport.latest=(value['source_sha256'],value['source_path'])
                extension='.pdf' if value['body'].startswith(b'%PDF-') else '.json'
                self.engine.responses.store(identity.dataset,job['key'],value['body'],extension,scope='attempt_'+str(attempt['id']),recovered=True)
                self.engine.archive.link_from=self.engine.responses;self.engine.archive.store(identity.dataset,job['key'],value['body'],extension,recovered=True,legacy_source=job['datasetId'])
                if attempt['outcome']=='pending':self.engine.ledger.finish(attempt['id'],'empty' if value['validation']['outcome']=='empty' else 'success',http_status=value['status'])
                self.confirm(job,value);continue
            commands=self.store.db.execute('SELECT * FROM sn_commands WHERE attempt=?',(attempt['id'],)).fetchall();unknown=False
            for command in commands:
                plan=json.loads(command['payload']);key=hashlib.sha256(canonical(plan['identity'])).hexdigest();receipt=load(self.instance/'native_turns'/key/'receipt.json')
                # Cached receipts are observations only. A partially completed
                # operation is never resumed by another invocation.
                if not receipt or receipt.get('targetUncertain'):
                    unknown=True;self.store.hold('target',plan['targetId'],'',{'reason':'interrupted_native_stage_unknown','turnKey':key,'automaticReplayAllowed':False})
            if attempt['outcome']=='pending':
                if not commands:self.engine.ledger.not_sent(attempt['id'],'crash_before_direct_preparation')
                else:self.engine.ledger.finish(attempt['id'],'unknown' if unknown else 'failed',reason='interrupted_direct_stage_preserved_no_replay')
            self.store.quarantine(job,'interrupted_direct_stage_preserved_no_replay',consumed=bool(commands));identity=task_identity(job,self.config['account_alias'])
            self.engine.ledger.settle_task(identity.dataset,job['key'],'failed',reason='interrupted_direct_stage_preserved_no_replay')

    async def monthly_children(self,job,value):
        if job['kind']!='listing' or job['datasetId']!=MONTHLY:return
        excluded=[r[0] for r in self.store.db.execute('SELECT file_id FROM sn_file_claims')]
        existing=[{'params':{'fileId':json.loads(r[0]).get('fileId')}} for r in self.store.db.execute("SELECT params FROM sn_jobs WHERE kind='monthly_attachment' AND (consumed=1 OR (status='pending' AND snapshot_date=?))",(job['params']['snapshotDate'],))]
        children=await self.transport.rpc.call('monthly_candidates',input={'rows':value['validation']['rows'],'parentJob':job['key'],'parentSha':value['source_sha256'],'sourcePage':job['params']['page'],'snapshotDate':job['params']['snapshotDate'],'existingJobs':existing,'excludedFileIds':excluded})
        with self.store.transaction():
            for child in children:
                self.store.seed(child['datasetId'],child['kind'],child['params'],priority=105)
            self.store.set_setting('monthly_planned:'+job['key'],True)

    async def release_late_targets(self):
        for row in self.store.db.execute("SELECT source_id,body FROM sn_holds WHERE scope='target'").fetchall():
            hold=json.loads(row['body']);key=hold.get('turnKey')
            if not isinstance(key,str) or len(key)!=64:continue
            file=self.instance/'state/direct_target_settled'/(key+'.json');proof=load(file)
            if not proof:
                receipt=load(self.instance/'native_turns'/key/'receipt.json',{})
                foreign=receipt.get('foreignFrameRequests',[])
                # A completed API in a newly hash-navigated iframe was once
                # filtered before the frame metadata caught up. Passive
                # resource timing proves its original transport ended; the
                # result remains held and is never admitted or requested again.
                if receipt.get('invokeResult') is not None and not receipt.get('pending') and not receipt.get('bodyReadsPending') and not receipt.get('error') and not receipt.get('failures') and foreign and any(x['path']==receipt.get('expectedPath') for x in foreign):
                    expected=[{k:x[k] for k in ('requestId','path','startedAt')} for x in foreign]
                    expression="""(()=>{const wanted=EXPECTED,items=[],seen=new Set();function visit(w,depth){if(depth>8||seen.has(w))return;seen.add(w);try{if(w.location.origin!=='https://pmos.sn.sgcc.com.cn')return;for(const x of w.performance.getEntriesByType('resource')){const u=new URL(x.name);if(u.origin===w.location.origin)items.push({path:u.pathname,start:w.performance.timeOrigin+x.startTime,end:w.performance.timeOrigin+x.responseEnd,complete:x.responseEnd>0});}for(const f of w.document.querySelectorAll('iframe'))visit(f.contentWindow,depth+1);}catch{}}visit(window,0);return wanted.map(x=>{const found=items.filter(y=>y.path===x.path&&Math.abs(y.start-Date.parse(x.startedAt))<2000&&y.complete&&y.end>=y.start);return{requestId:x.requestId,path:x.path,uniqueMatch:found.length===1,evidence:found.length===1?found[0]:null};});})()""".replace('EXPECTED',encode(expected))
                    evidence=await self.transport.rpc.call('inspect',targetId=row['source_id'],expression=expression)
                    if len(evidence)==len(expected) and all(x.get('uniqueMatch') for x in evidence):
                        proof={'turnKey':key,'targetId':row['source_id'],'allObservedRequestsTerminal':True,'terminalTimingEvidence':evidence,'oldResultNotAdmitted':True,'replayed':False,'at':stamp()};atomic(file,proof)
            if proof and proof.get('turnKey')==key and proof.get('targetId')==row['source_id'] and proof.get('allObservedRequestsTerminal'):
                await self.transport.rpc.call('acknowledge_settled',proof=proof)
                with self.store.transaction():
                    self.store.set_setting('late_target_release:'+key,proof)
                    self.store.db.execute("DELETE FROM sn_holds WHERE scope='target' AND source_id=?",(row['source_id'],))
            elif (self.instance/'native_turns'/key/'receipt.json').is_file():
                # User policy: isolate non-auth errors and continue. Cancel
                # remaining old page loads before admitting a NEW identity;
                # preserve the old held task and never refresh/replay it.
                self.store.set_setting('target_cancel_prepared:'+key,{'targetId':row['source_id'],'at':stamp(),'oldIdentityReplayed':False})
                try:cancelled=await self.transport.rpc.call('cancel_isolated_target',turnKey=key)
                except Fault as fault:
                    if fault.code in {'direct_shared_session_protected','direct_authentication_requires_manual_login'}:return
                    raise
                if cancelled.get('phase')=='isolated_request_cancelled_allow_new' and cancelled.get('targetId')==row['source_id']:
                    with self.store.transaction():
                        self.store.set_setting('target_cancel_completed:'+key,cancelled);self.store.db.execute("DELETE FROM sn_holds WHERE scope='target' AND source_id=?",(row['source_id'],))

    async def tick(self):
        self.check_owner();control=self.engine.state.control();legacy_control=load(self.source/'state/collection_queue/control.json',{})
        await self.pdfs.tick()
        if not control.get('collect') or control.get('paused') or control.get('stop_all') or not legacy_control.get('enabled'):
            self.status('paused');return 2
        block=load(self.source/'state/session_block.json',{})
        if block.get('reason')=='auth_expired':self.status('waiting_auth_or_access_review',reason='real_auth_expired');return 2
        await self.reconnect_driver()
        await self.release_late_targets()
        if self.engine.ledger.dispatch_blocker():self.status('operation_budget_exhausted');self.stopping=True;return 0
        if legacy_control.get('watchDaily'):self.store.seed_daily()
        # Recover local child planning after a checkpoint crash without a
        # website operation or changing the parent's successful outcome.
        for row in self.store.db.execute("SELECT j.body,a.validation FROM sn_jobs j JOIN sn_job_artifacts a ON a.job_key=j.key WHERE j.source_id=? AND j.kind='listing' AND j.status='saved' AND NOT EXISTS(SELECT 1 FROM sn_settings s WHERE s.key='monthly_planned:'||j.key) LIMIT 1",(MONTHLY,)):
            parent=json.loads(row['body']);v=load(row['validation']);await self.monthly_children(parent,{'validation':v,'source_sha256':v['sha256']});return 0
        job=self.store.next_job(download_attachments=bool(legacy_control.get('downloadAttachments')))
        if not job:self.status('idle');return 2
        d=self.store.source(job['datasetId'])
        if job['kind']=='table' and d.get('family')=='preview' and len(d.get('sampleValueTypes',[]))>1 and not job['params'].get('valueType'):
            self.store.expand(job,d['sampleValueTypes']);identity=task_identity(job,self.config['account_alias']);self.engine.ledger.seed(identity.dataset,[job['key']]);self.engine.ledger.settle_task(identity.dataset,job['key'],'skipped',reason='all_verified_value_types_expanded_locally');self.counts['expanded']+=1;return 0
        if job['kind'] not in {'table','catalogue','listing','file_listing','download','monthly_attachment'}:
            self.store.quarantine(job,'direct_historical_transition_not_reissued',consumed=False);return 0
        wait=self.engine.spacing_wait('collect')
        if wait>0:self.status('waiting',reason='response_end_spacing',wait_seconds=wait);return min(1,wait)
        self.status('dispatching_native_task',job_key=job['key'],legacy_source=job['datasetId'])
        try:
            result=await self.engine.collect_direct(job);job=self.transport.job;value=self.transport.value
            self.confirm(job,value);self.counts[value['validation']['outcome']]+=1
            self.status('completed',last_job_key=job['key'],last_outcome=value['validation']['outcome'])
        except Fault as fault:
            if fault.unsent or fault.code.startswith('preflight_deferred:') or fault.code=='spacing_not_elapsed':
                current=self.store.job(job['key'])
                if current['status']=='running':
                    current={k:v for k,v in current.items() if k not in {'reservedAt','pmosAttempt'}};self.store.update(current,state='pending',consumed=False)
                    if job['kind'] in {'download','monthly_attachment'}:self.store.db.execute("DELETE FROM sn_file_claims WHERE job_key=? AND state='issued'",(job['key'],))
                transient=fault.category=='auth' or fault.code.startswith('preflight_deferred:') or any(x in fault.code for x in ('spacing','turn_busy','keeper_','cdp_')) or fault.code in {'direct_browser_process_ended','direct_browser_reply_missing','direct_browser_unavailable_before_issue'}
                if not transient:
                    current=self.store.job(job['key']);self.store.quarantine(current,fault.code,consumed=False,proof={'websiteRequestsIssued':0});self.status('task_isolated',reason=fault.code);return 0
                self.status('waiting',reason=fault.code);return 1
            failure=self.transport.last_failure or {};mismatch=failure.get('validation_mismatch',{})
            self.store.quarantine(self.transport.job or job,fault.code,scope=mismatch.get('scope') if mismatch.get('scope') in {'date','snapshot'} else None,source_hold=mismatch.get('scope')=='source',proof=failure)
            if failure.get('targetUncertain'):self.store.hold('target',failure['targetId'],'',{'jobKey':job['key'],'turnKey':failure.get('key',failure.get('turnKey')),'reason':fault.code,'automaticReplayAllowed':False})
            identity=task_identity(job,self.config['account_alias']);self.engine.ledger.settle_task(identity.dataset,job['key'],'failed',reason=fault.code);self.counts['failed']+=1
            self.status('waiting_auth_or_access_review' if fault.category=='auth' else 'task_isolated',reason=fault.code,last_job_key=job['key'])
        return 0

    async def reconnect_driver(self):
        if self.transport.rpc.process.returncode is not None or self.transport.rpc.needs_reconnect:
            await self.transport.rpc.close();self.transport.rpc=BrowserRPC(self.code,self.instance)
            await self.transport.start();self.engine.state.event('direct_broker_reconnected_existing_browser',website_requests=0)

    async def run(self):
        self.acquire();loop=asyncio.get_running_loop()
        for sig in (signal.SIGTERM,signal.SIGINT):loop.add_signal_handler(sig,lambda:setattr(self,'stopping',True))
        try:
            await self.transport.start()
            while not self.stopping:await asyncio.sleep(await self.tick())
        finally:
            try:
                await self.pdfs.close()
                await self.transport.rpc.close()
            finally:
                self.status('stopped',graceful=True);atomic(self.owner_path,{'generation':self.config['generation'],'pid':os.getpid(),'phase':'released','at':time.time()});self.engine.release();self.lock.close()
