"""Single PMOS owner for the existing Shaanxi native frontier.

Only an explicit, code-pinned handoff may start it. Original artifacts,
unknowns and native protection remain authoritative; no login/retry handler.
"""
from __future__ import annotations
import asyncio
from dataclasses import replace
from datetime import date,datetime,timezone,timedelta
import hashlib
import json
import os
from pathlib import Path
import signal
import time
from .contracts import canonical
from .faults import Fault
from .migration.shaanxi import import_journal,task_identity,dataset_id,KINDS
from .province import Province
from .shaanxi_lifecycle import validate_handoff,check_source_quiescent,SOURCE_ID,verify_process,process_identity
from .shaanxi_native import NativeTaskTransport,ShaanxiEngine,UNSENT_PHASES
from .shaanxi_transport import OriginalWorkerLock,atomic,load,verify_pins
from .validate import evaluate
from .shaanxi_continuity import quarantine_terminal_network


def candidates(journal,registry,ledger,alias,*,now=None,deferred=None,control=None,limit=None):
    now=time.time() if now is None else now;deferred=deferred or {};sources={d['id']:d for d in registry['datasets']};eligible=[];counts={}
    today=datetime.fromtimestamp(now,timezone(timedelta(hours=8))).date().isoformat()
    statuses={(r['dataset'],r['period']):r['status'] for r in ledger.db.execute('SELECT dataset,period,status FROM tasks')}
    for key,job in journal['jobs'].items():
        if job.get('status')!='pending':continue
        reason=None;d=sources.get(job['datasetId'])
        if job['datasetId'] in journal.get('datasetBlocks',{}):reason='source_protected'
        elif not d or (not d.get('enabled') and job.get('kind') not in {'disclosure_admission','listing_admission','directory_admission'}):reason='source_not_enabled'
        elif control is not None and job.get('kind') in {'download','monthly_attachment','monthly_context'} and not control.get('downloadAttachments'):reason='attachments_disabled'
        elif job['params'].get('date') in journal.get('gridDateHolds',{}).get(job['datasetId'],{}):reason='date_protected'
        elif job['params'].get('snapshotDate') in journal.get('listingSnapshotHolds',{}).get(job['datasetId'],{}):reason='snapshot_protected'
        elif job.get('reservedAt') or job.get('finishedAt') or job.get('validation'):reason='prior_attempt_fact'
        elif job.get('kind') not in KINDS:reason='kind_unreviewed'
        elif deferred.get(key,0)>now:reason='local_deferred'
        if reason:counts[reason]=counts.get(reason,0)+1;continue
        family=d.get('family');kind=job['kind'];p=job['params']
        # Finish current listing page groups before moving their mutable
        # snapshot. Historical pages must never be silently treated as fresh.
        continuation=kind in {'listing','file_listing'} and p.get('page',1)>1
        lane=2 if kind in {'download','monthly_attachment','monthly_context'} else 1 if job['datasetId'].startswith('disclosure:') else 0
        current_snapshot=family in {'directory','disclosure_list','file_list'} and p.get('snapshotDate')==today
        eligible.append((0 if continuation else 1,lane,0 if current_snapshot else 1,
                         job.get('priority',100),job.get('createdAt',''),key,job))
    eligible.sort(key=lambda x:x[:-1])
    selected=[]
    for item in eligible:
        job=item[-1];key=job['key'];reason=None
        try:identity=task_identity(job,alias)
        except (ValueError,KeyError,TypeError):reason='identity_unmapped'
        else:
            if statuses.get((identity.dataset,key)) in {'saved','empty','failed','skipped'}:reason='pmos_settled'
            elif ledger.consumed_identity(identity.key) or identity.file_key and ledger.consumed_file(identity.file_key):reason='consumed_identity'
        if reason:counts[reason]=counts.get(reason,0)+1;continue
        selected.append(job)
        if limit is not None and len(selected)>=limit:break
    return selected,counts


class Runner:
    def __init__(self,config,code_root):
        self.config=config;self.code_root=Path(code_root).resolve();self.source,self.instance=validate_handoff(config,code_root)
        self.lock=OriginalWorkerLock(self.source);self.stopping=False;self.engine=None;self.transport=None
        self.deferred={};self.owner_path=self.instance/'state/native_owner.json';self.counts={'saved':0,'empty':0,'failed':0,'transitions':0}

    def acquire(self):
        self.lock.acquire()
        try:
            # A restart may observe its own existing command, but may not
            # silently grant a first handoff in an uncertain source state.
            fresh=not (self.instance/'ledger.sqlite3').exists()
            if fresh:check_source_quiescent(self.source)
            province=Province.load('shaanxi',self.code_root/'provinces')
            province.policy=replace(province.policy,request_timeout_seconds=600)
            self.transport=NativeTaskTransport(self.source,self.instance,self.lock,generation=self.config['generation'],
                runtime_pins=self.config['runtime_pins'],runtime_root=self.config['runtime_root'])
            self.engine=ShaanxiEngine(province,self.transport,instance=self.instance,account_alias=self.config['account_alias']);self.engine.acquire()
            if self.engine.ledger.handoff() is None:
                if self.engine.ledger.db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0]:raise Fault('existing_attempts_without_handoff','local')
                raw=Path(self.config['snapshot_ref']).read_bytes();journal=json.loads(raw)
                import_journal(self.engine.ledger,journal,self.config['account_alias'],snapshot_sha256=self.config['snapshot_sha256'])
                scope=sorted({dataset_id(j['datasetId']) for j in journal['jobs'].values()}|{'sn_attachment'})
                self.engine.ledger.grant_handoff({'version':1,'province':'shaanxi','account_alias':self.config['account_alias'],
                    'legacy_owner':{'stopped_evidence':self.config['original_worker']['stopped_evidence'],'pid':self.config['original_worker']['pid']},
                    'snapshots':[{'source_id':SOURCE_ID,'snapshot_sha256':self.config['snapshot_sha256']}],
                    'scope':scope,'request_budget':self.config['operations_budget'],'code_sha':self.config['code_sha'],
                    'approval':self.config['approval']+'; budget unit=native operations; actual HTTP requests audited separately',
                    'session':{'keepalive_dataset':None,'keepalive_period':None,'probe':None},'seeding':'legacy_pending_only'})
                self.engine.state.set_control(collect=True,paused=False,stop_all=False)
            receipt=self.engine.ledger.handoff()
            current=receipt['code_sha']
            self.engine.ledger.db.execute('CREATE TABLE IF NOT EXISTS native_release_upgrades(id INTEGER PRIMARY KEY,previous_code_sha TEXT NOT NULL,new_code_sha TEXT NOT NULL,handoff_receipt_sha TEXT NOT NULL,approval TEXT NOT NULL,at REAL NOT NULL)')
            for upgrade in self.engine.ledger.db.execute('SELECT * FROM native_release_upgrades ORDER BY id'):
                if upgrade['previous_code_sha']!=current or upgrade['handoff_receipt_sha']!=receipt['receipt_sha256'] or not upgrade['approval']:
                    raise Fault('native_release_upgrade_chain_invalid','local')
                current=upgrade['new_code_sha']
            if current!=self.config['code_sha']:raise Fault('native_release_upgrade_not_recorded','local')
            self.engine.ledger.db.execute('CREATE TABLE IF NOT EXISTS native_reconciliations(attempt_id INTEGER PRIMARY KEY,result_ref TEXT,disposition TEXT NOT NULL,artifact_sha TEXT,reconciled_at REAL NOT NULL)')
            atomic(self.owner_path,{'generation':self.config['generation'],'pid':os.getpid(),'code_sha':self.config['code_sha'],
                'phase':'owned','start_identity':process_identity(os.getpid()),'original_worker_lock':str(self.lock.path),'at':time.time()})
        except BaseException:
            if self.engine is not None:self.engine.release();self.engine=None
            self.lock.close();raise

    def check_owner(self):
        self.lock.check();owner=load(self.owner_path,{})
        if owner.get('generation')!=self.config['generation'] or owner.get('pid')!=os.getpid():raise Fault('pmos_native_owner_changed','local')
        for key in ('browser','observer','keeper'):verify_process(self.config[key])
        verify_pins(self.source,self.config['runtime_pins'],runtime_root=self.config['runtime_root'])

    async def seed_daily(self,journal):
        if not load(self.source/'state/collection_queue/control.json',{}).get('watchDaily'):
            return True
        today=datetime.now(timezone(timedelta(hours=8))).date().isoformat()
        if journal.get('lastSeedDay')==today:return True
        state=self.engine.state.read('daily_seed',{})
        if state.get('day')!=today:state={'day':today,'sequence':1}
        if state.get('phase')=='review_required':return True
        # Native queueSeed is pinned, file-only and shares turn.lock. Wait for
        # the healthy keeper rather than racing it for a local seed operation.
        try:check_source_quiescent(self.source)
        except Fault:return False
        prepared=self.transport.client.prepare_seed(today,state['sequence'])
        self.transport.client.publish(prepared)
        try:result=await self.transport.client.wait(prepared,timeout=60)
        except Fault:
            self.engine.state.write('daily_seed',{**state,'phase':'review_required','command_id':prepared['command_id']})
            return True
        if result.get('phase')=='waiting_turn_lock':
            self.engine.state.write('daily_seed',{**state,'sequence':state['sequence']+1,'phase':'waiting_turn_lock'});return False
        if result.get('phase')!='seeded_without_website_requests':
            self.engine.state.write('daily_seed',{**state,'phase':'review_required','command_id':prepared['command_id']});return True
        self.engine.state.write('daily_seed',{**state,'phase':'seeded_without_website_requests','command_id':prepared['command_id']})
        self.engine.state.event('daily_seeded',source_command=prepared['command_id'],website_requests=0)
        return False

    def status(self,phase,**kwargs):
        self.engine.state.status(collector='pmos',phase=phase,generation=self.config['generation'],
            native_operations_used=self.engine.ledger.attempts_since_handoff(),operations_budget=self.engine.ledger.request_budget(),
            budget_unit='native_queue_operations_not_http_requests',new_tasks=self.counts,**kwargs)

    def reconcile(self):
        """Checkpoint ORIGINAL results after a crash/late reply; never send."""
        rows=list(self.engine.ledger.db.execute("SELECT a.* FROM attempts a LEFT JOIN tasks t ON t.dataset=a.dataset AND t.period=a.period AND t.scope=a.scope LEFT JOIN native_reconciliations r ON r.attempt_id=a.id WHERE a.outcome!='not_sent' AND r.attempt_id IS NULL AND (a.outcome IN ('pending','unknown') OR t.status='pending') ORDER BY a.id"))
        for attempt in rows:
            p=load(self.transport.client.commands/(str(attempt['id'])+'.json'))
            if p is None:
                # No durable preparation exists and the observer keeps every
                # inbox. The earlier attempt remains consumed if unknown.
                foreign=list((self.source/'state/browser_session/inbox').glob('pmos_sn_'+str(attempt['id'])+'_*.json'))
                if foreign:raise Fault('native_command_record_missing','local')
                if attempt['outcome']=='pending':self.engine.ledger.not_sent(attempt['id'],'crash_before_durable_native_preparation')
                else:
                    self.engine.ledger.settle_task(attempt['dataset'],attempt['period'],'failed',reason='unknown_without_preparation_preserved')
                    self.engine.ledger.db.execute('INSERT INTO native_reconciliations VALUES(?,?,?,?,?)',(attempt['id'],None,'unknown_preserved',None,time.time()))
                continue
            try:result=self.transport.client.result(p)
            except Fault as fault:
                # The observer answered, but did not attest whether the
                # operation sent anything. Isolate that identity permanently;
                # a local error result must not restart the owner forever.
                if attempt['outcome']=='pending':
                    self.engine.ledger.finish(attempt['id'],'unknown',reason=fault.code)
                self.engine.ledger.settle_task(attempt['dataset'],attempt['period'],'failed',reason=fault.code)
                self.engine.ledger.db.execute('INSERT INTO native_reconciliations VALUES(?,?,?,?,?)',
                    (attempt['id'],p['command_id'],'observer_error_unknown_preserved',None,time.time()))
                continue
            if result is None:return False
            try:response=self.transport.interpret(p,result,p['job_key'])
            except Fault as fault:
                if attempt['outcome']=='pending':
                    if fault.unsent:self.engine.ledger.not_sent(attempt['id'],fault.code)
                    else:self.engine.ledger.finish(attempt['id'],'failed',reason=fault.code)
                elif fault.unsent:
                    # Retain the original unknown history, and record the
                    # delayed zero-send proof. Do not reopen/replay it.
                    self.engine.ledger.settle_task(attempt['dataset'],attempt['period'],'skipped',reason=fault.code)
                    self.engine.ledger.db.execute('INSERT INTO native_reconciliations VALUES(?,?,?,?,?)',
                        (attempt['id'],p['command_id'],'late_unsent_identity_skipped',None,time.time()))
                if not fault.unsent:self.engine.ledger.settle_task(attempt['dataset'],attempt['period'],'failed',reason=fault.code)
                if not fault.unsent:self.engine.ledger.db.execute('INSERT INTO native_reconciliations VALUES(?,?,?,?,?)',(attempt['id'],p['command_id'],'original_terminal_failure_preserved',None,time.time()))
                continue
            if response.operation and response.operation.get('transition_only'):
                disposition='local_transition';artifact_sha=None;status='skipped'
            else:
                outcome=evaluate({'kind':'document' if response.body.startswith(b'%PDF-') else 'json'},response)
                disposition=outcome.result;artifact_sha=None;status=outcome.result
                if status=='saved':
                    self.engine.responses.store(attempt['dataset'],attempt['period'],response.body,outcome.extension,scope='attempt_'+str(attempt['id']),outcome=outcome.result)
                    record=self.engine.archive.store(attempt['dataset'],attempt['period'],response.body,outcome.extension,source_command=p['command_id'],reconciled_original=True)
                    artifact_sha=record['sha256']
            self.engine.ledger.settle_task(attempt['dataset'],attempt['period'],status,sha256=artifact_sha,reason='original_command_reconciled_without_reissue')
            if attempt['outcome']=='pending':self.engine.ledger.finish(attempt['id'],'local_transition' if disposition=='local_transition' else 'empty' if status=='empty' else 'success')
            self.engine.ledger.db.execute('INSERT INTO native_reconciliations VALUES(?,?,?,?,?)',(attempt['id'],p['command_id'],disposition,artifact_sha,time.time()))
        return True

    async def tick(self):
        self.check_owner()
        if not self.reconcile():self.status('waiting_original_native_result');return 'waiting'
        control=self.engine.state.control()
        if not control.get('collect') or control.get('paused') or control.get('stop_all'):
            self.status('paused');return 'waiting'
        if self.engine.ledger.dispatch_blocker():self.status('operation_budget_exhausted');return 'budget'
        if (self.source/'state/session_block.json').exists():
            try:isolated=quarantine_terminal_network(self.source,self.instance,self.engine.ledger)
            except (OSError,ValueError,KeyError,TypeError):isolated=False
            if isolated:
                self.engine.state.event('terminal_native_network_task_isolated',replayed=False)
                self.status('task_isolated',reason='completed_native_network_failure_skipped')
                return 'worked'
            self.status('waiting_original_protection');return 'waiting'
        journal=load(self.source/'state/collection_queue/journal.json');registry=load(self.source/'config/spot_registry.json')
        if not await self.seed_daily(journal):self.status('seeding_next_day_locally');return 'waiting'
        jobs,excluded=candidates(journal,registry,self.engine.ledger,self.config['account_alias'],deferred=self.deferred,
            control=load(self.source/'state/collection_queue/control.json',{}),limit=1)
        if not jobs:self.status('idle',excluded=excluded);return 'waiting'
        job=jobs[0];self.status('dispatching_native_task',job_key=job['key'],legacy_source=job['datasetId'])
        try:
            result=await self.engine.collect_native_job(job)
            phase='transitions' if result.response.operation and result.response.operation.get('transition_only') else result.outcome.result
            self.counts[phase]+=1;self.status('completed',last_job_key=job['key'],last_outcome=phase)
        except Fault as fault:
            self.deferred[job['key']]=time.time()+30
            if fault.category in {'auth','rate_limit','permission'}:
                self.status('waiting_auth_or_access_review',reason=fault.code);return 'waiting'
            attempt=self.engine.ledger.db.execute('SELECT * FROM attempts ORDER BY id DESC LIMIT 1').fetchone()
            if attempt and attempt['period']==job['key'] and attempt['outcome']=='failed':
                identity=task_identity(job,self.config['account_alias']);self.engine.ledger.settle_task(identity.dataset,job['key'],'failed',reason=fault.code);self.counts['failed']+=1
            self.status('waiting' if fault.unsent or fault.category=='local' else 'task_isolated',reason=fault.code)
        return 'worked'

    async def run(self):
        self.acquire()
        loop=asyncio.get_running_loop()
        for sig in (signal.SIGTERM,signal.SIGINT):loop.add_signal_handler(sig,lambda:setattr(self,'stopping',True))
        try:
            while not self.stopping:
                phase=await self.tick()
                if phase=='budget':break
                await asyncio.sleep(1 if phase=='worked' else 2)
        finally:
            self.status('stopped',graceful=True)
            atomic(self.owner_path,{'generation':self.config['generation'],'pid':os.getpid(),'phase':'released','at':time.time()})
            self.engine.release();self.lock.close()
