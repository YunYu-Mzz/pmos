"""Direct PMOS transport. The legacy queue is not a runtime dependency.

Every issued browser stage has a durable command, identity and immutable
receipt. Restart recovery observes those receipts; it never resends a stage.
The only imported Shaanxi functions are private, pure validation/exports.
"""
from __future__ import annotations
import asyncio,hashlib,json,os,time,re
from datetime import datetime,timezone,timedelta
from pathlib import Path
from .contracts import ParsedArtifact,canonical
from .faults import Fault
from .shaanxi_transport import atomic,inside,load
from .shaanxi_direct_store import encode,stamp
from .adapters.shaanxi_native.queue_validate import validate,export
from .validate import Response

MONTHLY='disclosure:8c15a8c7-3888-4e14-b50b-9b141891e56b'
DOWNLOAD='/px-settlement-infpubmeex/fileService/download'


class BrowserRPC:
    def __init__(self,code_root,instance):
        self.code=Path(code_root);self.instance=Path(instance);self.process=None;self.sequence=0;self.lock=asyncio.Lock();self.initialized=False;self.needs_reconnect=False

    async def start(self,config):
        self.error=(self.instance/'state/direct_browser.stderr').open('ab',buffering=0)
        self.process=await asyncio.create_subprocess_exec(config.get('node_binary','/usr/bin/node'),str(self.code/'pmos/browser/shaanxi_direct.mjs'),stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=self.error,limit=16*1024*1024)
        # Chrome disables its HTTP discovery routes on this host. Read only
        # the two-line debugging endpoint metadata used by the existing
        # authorized observer; no cookies, preferences or login values.
        def endpoint():
            p=Path(config['debug_endpoint_file'])
            if p!=Path('/home/mz/.local/share/sd_crawler/shaanxi/chrome-profile/DevToolsActivePort') or p.stat().st_size>2048:raise Fault('dedicated_browser_debug_metadata_required','local')
            lines=p.read_text().strip().splitlines()
            if len(lines)!=2 or lines[0]!='9222' or not re.fullmatch(r'/devtools/browser/[a-zA-Z0-9-]+',lines[1]):raise Fault('dedicated_browser_debug_endpoint_changed','local')
            return 'ws://127.0.0.1:'+lines[0]+lines[1]
        result=await self.call('initialize',config=config,endpoint=await asyncio.to_thread(endpoint));self.initialized=True;return result

    async def call(self,op,**params):
        async with self.lock:
            if self.process.returncode is not None:self.needs_reconnect=True;raise Fault('direct_browser_process_ended','network')
            self.sequence+=1;cid='rpc_'+str(self.sequence)
            self.process.stdin.write((encode({'id':cid,'op':op,**params})+'\n').encode());await self.process.stdin.drain()
            try:line=await asyncio.wait_for(self.process.stdout.readline(),timeout=420)
            except asyncio.TimeoutError:raise Fault('direct_browser_reply_unknown','network') from None
            if not line:self.needs_reconnect=True;raise Fault('direct_browser_reply_missing','network')
            reply=json.loads(line)
            if reply.get('id')!=cid:raise Fault('direct_browser_reply_identity_changed','network')
            if not reply.get('ok'):
                code=reply.get('error','direct_browser_failed')
                if code in {'cdp_timeout','cdp_connection_closed','cdp_connection_failed','direct_browser_process_ended','direct_browser_reply_missing'}:self.needs_reconnect=True
                raise Fault(code,'network' if self.needs_reconnect else 'local')
            result=reply['result'];receipt=result.get('parentReceipt',result) if isinstance(result,dict) else {}
            if receipt.get('error') in {'cdp_timeout','cdp_connection_closed','cdp_connection_failed'}:self.needs_reconnect=True
            return result

    async def close(self):
        if self.process is None:return
        if self.process.returncode is None:
            try:
                if self.initialized:await self.call('close')
            finally:self.process.stdin.close()
            try:await asyncio.wait_for(self.process.wait(),10)
            except asyncio.TimeoutError:self.process.terminate();await self.process.wait()
        self.error.close()


class DirectTransport:
    def __init__(self,config,code_root,store,owner_lock,*,response_interval_seconds=1):
        self.config=config;self.instance=Path(config['instance']);self.source=Path(config['source_root']);self.store=store;self.db=store.db;self.owner_lock=owner_lock
        self.rpc=BrowserRPC(code_root,self.instance);self.latest=None;self.value=None;self.job=None;self.last_failure=None
        self.response_interval_seconds=response_interval_seconds
        self.registry={**store.setting('registry_metadata'),'datasets':[json.loads(r[0]) for r in self.db.execute('SELECT body FROM sn_sources')]}
        self.contract=load(self.instance/'direct_config/monthly_contract.json',{})

    async def start(self):
        targets=[r[0] for r in self.db.execute('SELECT DISTINCT target_id FROM sn_sources WHERE enabled=1')]
        holds=[r[0] for r in self.db.execute("SELECT source_id FROM sn_holds WHERE scope='target'")]
        return await self.rpc.start({'instance':str(self.instance),'source_root':str(self.source),'collection_targets':targets,'keeper_target':self.registry['keeperTarget'],'held_targets':holds,'registry':self.registry,'response_interval_ms':1000*self.response_interval_seconds,'debug_endpoint_file':'/home/mz/.local/share/sd_crawler/shaanxi/chrome-profile/DevToolsActivePort','non_business_paths':self.config['direct'].get('non_business_paths',[]),'node_binary':self.config['direct'].get('node_binary','/usr/bin/node')})

    def target(self,d):return d.get('collectorTarget') or self.registry['collectorTarget']

    async def compile(self,op,d,job,contract=None,**extra):
        return await self.rpc.call('compile',input={'op':op,'dataset':d,'job':job,'contract':contract,**extra})

    async def inspect(self,d,job,*,op='snapshot',target=None,contract=None):
        plan=await self.compile(op,d,job,contract)
        return await self.rpc.call('inspect',targetId=target or self.target(d),expression=plan['expression'])

    async def preflight(self,request):
        self.owner_lock.check()
        if isinstance(self.rpc,BrowserRPC) and (self.rpc.process is None or self.rpc.process.returncode is not None or self.rpc.needs_reconnect):raise Fault('direct_browser_unavailable_before_issue','local',unsent=True)
        if (self.source/'state/turn.lock').exists():raise Fault('shared_native_turn_busy','local',unsent=True)
        if (self.source/'state/session_block.json').exists():
            block=load(self.source/'state/session_block.json',{})
            if block.get('reason')=='auth_expired':raise Fault('original_session_auth_expired','auth',unsent=True)
        k=load(self.source/'state/keeper/status.json',{});p=load(self.source/'state/keeper/last_success.json',{})
        try:age=time.time()-datetime.fromisoformat(p['finishedAt'].replace('Z','+00:00')).timestamp();fresh=time.time()-datetime.fromisoformat(k['at'].replace('Z','+00:00')).timestamp()
        except (ValueError,KeyError,TypeError):raise Fault('keeper_evidence_missing','local',unsent=True) from None
        if not p.get('passed') or not p.get('fullSampleMatches') or not k.get('controlEnabled') or not 0<=age<=600 or not 0<=fresh<=15:raise Fault('keeper_not_fresh','local',unsent=True)
        deadline=k.get('nextAttemptAt') or k.get('nextDueAt')
        if k.get('pending') or k.get('requiresReview') or k.get('due') or (deadline and time.time()>=datetime.fromisoformat(deadline.replace('Z','+00:00')).timestamp()):raise Fault('keeper_has_priority','local',unsent=True)

    async def native(self,d,job,attempt,stage,plan,*,target=None,identity=None,monthly=False,inspect_expression=None):
        if identity is None:
            identity={'purpose':'pmos_direct_native','jobKey':job['key'],'action':stage}
            reviewed=self.store.reviewed_identity(job)
            if reviewed:identity['explicitUnsentReview']=reviewed
        turn_key=hashlib.sha256(canonical(identity)).hexdigest();command='sn_'+turn_key+'_'+str(attempt)
        # Opening the native menu first loads its columns and then its default
        # business data. Observed successful initialization can exceed 45s;
        # give that one original invocation time to finish, never reissue it.
        wait_ms=90000 if plan.get('download') or stage in {'open','monthly_menu'} else 45000
        full={**plan,'identity':identity,'targetId':target or self.target(d),'readOnly':True,'timeoutMs':wait_ms}
        folder=self.instance/'direct_commands';record=folder/(command+'.json');result_file=folder/(command+'.result.json')
        prior=self.db.execute('SELECT * FROM sn_commands WHERE id=?',(command,)).fetchone()
        if prior:
            if prior['job_key']!=job['key'] or prior['attempt']!=attempt or prior['payload']!=encode(full):raise Fault('direct_stage_identity_changed','local')
            if result_file.exists():return load(result_file)
            raise Fault('direct_prior_stage_result_unknown_no_reissue','network')
        # A prior legacy stage of this identity is never granted a new name.
        if self.db.execute('SELECT 1 FROM sn_legacy_turns WHERE key=?',(turn_key,)).fetchone():raise Fault('direct_legacy_stage_consumed','local')
        previous_turn=self.db.execute('SELECT state FROM sn_turns WHERE key=?',(turn_key,)).fetchone()
        if previous_turn and previous_turn['state']!='not_sent':raise Fault('direct_previous_stage_consumed_no_reissue','local')
        atomic(record,{'job_key':job['key'],'attempt':attempt,'stage':stage,'plan':full,'prepared_at':time.time()},exclusive=True)
        with self.store.transaction():
            self.db.execute('INSERT INTO sn_commands VALUES(?,?,?,?,?,?,NULL,?)',(command,job['key'],attempt,stage,encode(full),'prepared',time.time()))
            self.db.execute('INSERT OR REPLACE INTO sn_turns VALUES(?,?,?,?,?,?,NULL,NULL)',(turn_key,job['key'],attempt,stage,'prepared',str(record)))
        self.db.execute("UPDATE sn_commands SET phase='published' WHERE id=?",(command,))
        try:
            result=await self.rpc.call('monthly_child',plan=full,inspectExpression=inspect_expression) if monthly else await self.rpc.call('native',plan=full)
            # The keeper can acquire the shared lease between two stages.
            # Only a proven ZERO-send local mutex result may be waited out;
            # no issued stage, denial or unknown response is invoked twice.
            prior_sent=self.db.execute("SELECT count(*) FROM sn_turns WHERE job_key=? AND key!=? AND state!='not_sent'",(job['key'],turn_key)).fetchone()[0]
            for wait_index in range(15):
                if monthly or not prior_sent or result.get('phase')!='not_sent' or result.get('reason')!='native_turn_busy':break
                atomic(folder/(command+'.not_sent.'+str(wait_index+1)+'.json'),result,exclusive=True)
                await asyncio.sleep(1);result=await self.rpc.call('native',plan=full)
        except Fault:
            self.last_failure={'targetUncertain':True,'targetId':full['targetId'],'turnKey':turn_key,'stage':stage};raise
        atomic(result_file,result,exclusive=True);raw=result_file.read_bytes()
        with self.store.transaction():
            self.db.execute("UPDATE sn_commands SET phase='answered',result_ref=? WHERE id=?",(str(result_file),command))
            self.db.execute('UPDATE sn_turns SET state=?,receipt_ref=?,receipt_sha=? WHERE key=?',(result.get('phase','monthly_child_captured'),str(result_file),hashlib.sha256(raw).hexdigest(),turn_key))
            rec=result.get('parentReceipt',result)
            for item in rec.get('observedRequests',[])+result.get('events',[]):
                if item.get('requestId'):
                    self.db.execute('INSERT OR IGNORE INTO sn_requests VALUES(?,?,?,?,?)',(item.get('sessionId',rec.get('observedSessionId','')),item['requestId'],turn_key,datetime.fromisoformat(item['startedAt'].replace('Z','+00:00')).timestamp(),item['path']))
        receipt=result.get('parentReceipt',result)
        if receipt.get('phase')=='not_sent' or receipt.get('provenUnsent'):
            # If an earlier stage already issued, the job as a whole is still consumed.
            prior_sent=self.db.execute("SELECT count(*) FROM sn_turns WHERE job_key=? AND key!=? AND state!='not_sent'",(job['key'],turn_key)).fetchone()[0]
            self.db.execute("UPDATE sn_turns SET state='not_sent' WHERE key=?",(turn_key,))
            raise Fault(receipt.get('reason',receipt.get('error','direct_not_sent')),'local',unsent=not prior_sent)
        if receipt.get('phase')=='held':
            self.last_failure={**receipt,'stage':stage}
            block=load(self.source/'state/session_block.json',{})
            raise Fault('direct_native_stage_held','auth' if block.get('reason')=='auth_expired' else 'network' if receipt.get('targetUncertain') else 'site_data')
        return result

    def validate_receipt(self,d,job,receipt,*,params=None,download=False,endpoint=None,final=True):
        request={'dataset':d,'turn':receipt,'params':{**(params or job['params']),'nativeFilters':job.get('nativeFilters',{})},'download':download,**({'endpoint':endpoint} if endpoint else {})}
        for item in receipt.get('responses',[]):
            if item.get('responseFile'):inside(self.instance,item['responseFile'])
        try:result=validate(request)
        except (ValueError,KeyError,TypeError,AttributeError) as error:raise Fault('direct_validation:'+str(error)[:140],'site_data') from None
        if final:
            mismatch=self.store.precheck_page(job,result)
            if mismatch:
                self.last_failure={'validation_mismatch':mismatch};raise Fault(mismatch['code'],'site_data')
        digest=result.get('sha256') or result.get('response_sha256');matches=[x for x in receipt['responses'] if x.get('sha256')==digest and x.get('responseFile') and x.get('kind')=='data']
        if len(matches)!=1:raise Fault('direct_primary_ambiguous','site_data')
        original=inside(self.instance,matches[0]['responseFile']);raw=original.read_bytes()
        if hashlib.sha256(raw).hexdigest()!=digest:raise Fault('direct_raw_hash_changed','local')
        result['responseFile']=str(original)
        dest=self.instance/'direct_parsed'/job['key']/('final' if final else receipt['key']);dest.mkdir(parents=True,exist_ok=True,mode=0o700)
        if download and not (dest/'original.pdf').exists():os.link(original,dest/'original.pdf')
        deferred=result.get('kind')=='pdf_original'
        if deferred:result['pdfParse']={'status':'pending','sourceSha256':digest,'originalVerified':True}
        # Archive/format verification is synchronous. Expensive PDF semantic
        # exports run only after the PMOS archive and confirmation commit.
        export(result,dest,None if deferred else d)
        atomic(dest/'validation_input.json',request);atomic(dest/'validated.json',result)
        rows=result.get('rows');rows=rows if isinstance(rows,list) else result.get('records',[])
        value={'parsed':ParsedArtifact(rows,result['outcome']=='empty',semantic_state='native_pdf_original_verified_parse_pending' if deferred else 'native_validated_fields_and_semantic_sidecars'),'validation':result,'validation_path':str(dest/'validated.json'),'body':raw,'source_sha256':digest,'source_path':original,'status':matches[0]['status']}
        if final:atomic(self.instance/'direct_completed'/(job['key']+'.json'),{'job':job,'validation':value['validation_path'],'raw_sha256':digest,'raw_path':str(original),'http_status':value['status'],'stage_receipt':str(self.instance/'native_turns'/receipt['key']/'receipt.json')})
        return value

    async def regular(self,d,job,attempt):
        snapshot=await self.inspect(d,job);bound=None
        try:bound=(await self.compile('bind',d,job,snapshot))['component']
        except Fault:pass
        if bound is not None and job['kind']=='download':
            check=await self.compile('has_file',d,job,snapshot)
            if not await self.rpc.call('inspect',targetId=self.target(d),expression=check['expression']):bound=None
        if bound is None:
            if job.get('unissuedRecovery'):
                # This permission covers a first file download in an already
                # open directory. It never repeats the rejected menu action.
                raise Fault('reviewed_unissued_file_not_in_open_directory','local',unsent=True)
            opened=await self.native(d,job,attempt,'open',await self.compile('open',d,job))
            # No second menu action: poll only the current DOM after Vue settles.
            for _ in range(30):
                snapshot=await self.inspect(d,job)
                try:bound=(await self.compile('bind',d,job,snapshot))['component'];break
                except Fault:await asyncio.sleep(.1)
            if bound is None:raise Fault('direct_component_not_bound_after_open','site_data')
            if d['family'] in {'disclosure_list','weighted_price','file_list'}:opened['postRead']=snapshot
            nav=(await self.compile('navigation_params',d,job,snapshot,day=datetime.now(timezone(timedelta(hours=8))).date().isoformat()))['params']
            reuse=job['kind']=='catalogue' or job['kind'] in {'listing','file_listing'} and job['params'].get('page')==nav.get('page')==1 and job['params'].get('size')==nav.get('size')
            preliminary=self.validate_receipt(d,job,opened,params=job['params'] if reuse else nav,final=reuse)
            if reuse:return preliminary
        plan=await self.compile('query',d,job,snapshot)
        job=self.store.filters(job,plan.pop('nativeFilters',{}));self.job=job
        if job['kind']=='download':plan.update(download=True,expectedFilename=job['params']['missionName']+'.pdf' if job['params'].get('missionName') else 'file.pdf')
        receipt=await self.native(d,job,attempt,'query',plan)
        return self.validate_receipt(d,job,receipt,download=job['kind']=='download',endpoint=plan['expectedPath'])

    async def monthly(self,d,job,attempt):
        p=job['params'];parent=self.store.job(p['parentJob'])
        today=datetime.now(timezone(timedelta(hours=8))).date().isoformat()
        if not parent or parent['status']!='saved' or parent['kind']!='listing' or parent['params']['snapshotDate']!=today or p['snapshotDate']!=today:raise Fault('monthly_parent_not_current_verified','local',unsent=True)
        validation=load(inside(self.source,parent['validation']) if not Path(parent['validation']).is_relative_to(self.instance) else inside(self.instance,parent['validation']))
        raw_path=Path(validation['responseFile']);inside(self.instance if raw_path.is_relative_to(self.instance) else self.source,raw_path);raw=raw_path.read_bytes()
        if hashlib.sha256(raw).hexdigest()!=p['parentSha'] or validation['sha256']!=p['parentSha']:raise Fault('monthly_parent_hash_changed','local',unsent=True)
        view=await self.inspect(d,job,op='monthly_parent',contract=self.contract)
        if not view.get('matched') or view.get('page')!=p['sourcePage']:
            # A new context operation has its own identity; the original list
            # is not relabeled or repeated as a collected page.
            snapshot=await self.inspect(d,job)
            try:await self.compile('bind',d,job,snapshot)
            except Fault:
                opened=await self.native(d,job,attempt,'monthly_menu',await self.compile('open',d,job))
                snapshot=await self.inspect(d,job)
                await self.compile('bind',d,job,snapshot)
                nav=(await self.compile('navigation_params',d,job,snapshot,day=today))['params'];opened['postRead']=snapshot
                self.validate_receipt(d,job,opened,params=nav,final=False)
            context_plan=await self.compile('monthly_context_query',d,job,self.contract,page=p['sourcePage'])
            context_plan.update(expectedPath=d['endpoint'],context={'kind':'root'},postReadExpression=(await self.compile('snapshot',d,job))['expression'])
            receipt=await self.native(d,job,attempt,'monthly_context',context_plan)
            self.validate_receipt(d,job,receipt,params={'snapshotDate':today,'page':p['sourcePage'],'size':d['nativeListing']['pageSize']},final=False)
            view=await self.inspect(d,job,op='monthly_parent',contract=self.contract)
        matches=[r for r in view.get('rows',[]) if r.get('guid')==p['recordGuid'] and str(r.get('viewWayCode'))=='1002' and sum(a.get('id')==p['fileId'] and a.get('name')==p['fileName'] for a in r.get('attachment',[]))==1]
        if len(matches)!=1:raise Fault('monthly_native_record_changed','site_data')
        plan=await self.compile('monthly_open',d,job,self.contract);plan.update(discovery=True,childNavigation=True,nativeUserGesture=True,expectedPath='/__monthly_settlement_detail__',context={'kind':'root'})
        inspection=(await self.compile('monthly_detail_inspect',d,job,self.contract))['expression']
        identity={'purpose':'first_monthly_settlement_detail_capture','source':MONTHLY,'recordGuid':p['recordGuid'],'parentJob':p['parentJob']}
        captured=await self.native(d,job,attempt,'monthly_detail',plan,identity=identity,monthly=True,inspect_expression=inspection)
        if not captured.get('completeObservation') or not captured.get('childInspection',{}).get('matched') or len(captured.get('children',[]))!=1:raise Fault('monthly_initial_detail_not_complete','site_data')
        target=captured['children'][0]['targetId'];download=await self.compile('monthly_download',d,job,self.contract)
        download.update(expectedPath=DOWNLOAD,download=True,expectedFilename=p['fileName'],context={'kind':'root'})
        identity={'purpose':'first_monthly_settlement_attachment_download','source':MONTHLY,'recordGuid':p['recordGuid'],'params':{'fileId':p['fileId'],'missionName':p['fileName']},'queueJob':job['key']}
        receipt=await self.native(d,job,attempt,'monthly_download',download,target=target,identity=identity)
        return self.validate_receipt(d,job,receipt,download=True,endpoint=DOWNLOAD)

    async def send(self,request):
        await self.preflight(request);job={**self.store.job(request['job_key']),'status':'running','reservedAt':stamp(),'pmosAttempt':request['_pmos_attempt']};self.job=job;self.last_failure=None
        with self.store.transaction():
            self.store.update(job,consumed=True)
            if job['kind'] in {'download','monthly_attachment'}:
                reviewed=getattr(self.store.ledger,'reviewed_unissued_file',lambda j:False)(job)
                if reviewed:
                    self.db.execute('UPDATE sn_file_claims SET job_key=?,state=?,proof=? WHERE file_id=?',(job['key'],'issued',encode({'attempt':request['_pmos_attempt'],'first_unissued_review':job.get('unissuedRecovery',job.get('legacyUnsentReview'))}),job['params']['fileId']))
                else:self.db.execute('INSERT INTO sn_file_claims VALUES(?,?,?,?)',(job['params']['fileId'],job['key'],'issued',encode({'attempt':request['_pmos_attempt']})))
        d=self.store.source(job['datasetId'])
        try:self.value=await (self.monthly(d,job,request['_pmos_attempt']) if job['kind']=='monthly_attachment' else self.regular(d,job,request['_pmos_attempt']))
        except Fault as fault:
            if not self.db.execute('SELECT 1 FROM sn_commands WHERE attempt=?',(request['_pmos_attempt'],)).fetchone():raise Fault(fault.code,'local',unsent=True) from None
            raise
        self.latest=(self.value['source_sha256'],self.value['source_path'])
        return Response(self.value['status'],'application/pdf' if self.value['body'].startswith(b'%PDF-') else 'application/json',self.value['body'],operation={'owner':'pmos_direct','job_key':job['key'],'old_queue_used':False})
