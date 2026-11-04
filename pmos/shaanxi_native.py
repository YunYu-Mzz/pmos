"""PMOS Engine integration with an exact native task transport.

The old queue is a compatibility facts/frontier store. PMOS selects a precise
identity, starts its own attempt durably, then asks the native adapter for ONLY
that key. No old queue worker is started or imported.
"""
from __future__ import annotations
from pathlib import Path
from datetime import datetime, timezone
import hashlib
import json
from .engine import Engine
from .faults import Fault
from .migration.shaanxi import task_identity
from .shaanxi_replay import replay
from .shaanxi_transport import CommandClient, inside, load, verify_readonly_preflight, atomic
from .validate import Response

UNSENT_PHASES=frozenset({'waiting_turn_lock','waiting_session_review','waiting_healthy_keeper',
    'paused','idle','preferred_task_no_longer_pending'})
TRANSITION_STATUSES=frozenset({'expanded','assembled','admitted','empty_sample_not_admitted'})


class NativeTaskTransport:
    def __init__(self,source_root,instance,owner_lock,*,generation,runtime_pins,runtime_root=None):
        self.source=Path(source_root).resolve();self.instance=Path(instance).resolve()
        self.client=CommandClient(self.source,self.instance,owner_lock,generation=generation)
        self.pins=runtime_pins;self.lock=owner_lock;self.last_replay=None
        self.runtime_root=runtime_root
        self.latest=None

    async def preflight(self,request):
        verify_readonly_preflight(self.source,request['legacy_key'],runtime_pins=self.pins,owner_lock=self.lock,runtime_root=self.runtime_root)

    async def send(self,request):
        # Recheck immediately before command publication, not merely before
        # PMOS's durable attempt transaction.
        await self.preflight(request)
        prepared=self.client.prepare(request['_pmos_attempt'],request['legacy_key'])
        self.client.publish(prepared)
        result=await self.client.wait(prepared,timeout=request.get('timeout',600))
        return self.interpret(prepared,result,request['legacy_key'])

    def operation_trace(self,prepared,job_key):
        folder=self.source/'state/collection_queue/tasks'/job_key
        refs=[];requests=set();pending=set()
        for p in sorted(folder.rglob('*.receipt.json')) if folder.is_dir() else []:
            raw=p.read_bytes();receipt=json.loads(raw)
            try:started=datetime.fromisoformat(receipt['startedAt'].replace('Z','+00:00')).timestamp()
            except (KeyError,ValueError,TypeError):continue
            if started < prepared['prepared_at']-1:continue
            sid=receipt.get('observedSessionId','')
            for x in receipt.get('responses',[])+receipt.get('failures',[])+receipt.get('pending',[]):
                if x.get('requestId'):requests.add((sid,x['requestId']))
            for x in receipt.get('pending',[]):
                if x.get('requestId'):pending.add((sid,x['requestId']))
            refs.append({'path':str(p),'sha256':hashlib.sha256(raw).hexdigest(),'turn_key':receipt.get('key'),
                         'phase':receipt.get('phase')})
        trace={'native_job_key':job_key,'original_result_ref':str(self.client.observer/'results'/(prepared['command_id']+'.json')),
               'native_turn_receipts':refs,'observed_native_requests':len(requests),'observed_pending_requests':len(pending),
               'request_count_basis':'observed_original_turn_receipts_not_one_operation_equals_one_request'}
        atomic(self.client.commands/(str(prepared['attempt'])+'.trace.json'),trace)
        return trace

    def interpret(self,prepared,result,job_key):
        job=result.get('job');phase=result.get('phase')
        if job is None:
            if phase in UNSENT_PHASES:raise Fault('native_wait:'+phase,'local',unsent=True)
            raise Fault('native_completion_missing_exact_job','network')
        if job['key']!=job_key:raise Fault('native_wrong_job','network')
        trace=self.operation_trace(prepared,job_key)
        if job.get('status') in TRANSITION_STATUSES:
            return Response(0,'',b'',operation={**trace,'transition_only':True,'phase':phase,
                'source_result':str(self.client.observer/'results'/(prepared['command_id']+'.json'))})
        if job.get('status') not in {'saved','empty'}:
            protection=load(self.source/'state/session_block.json',{})
            category={'auth_expired':'auth','security_verification':'permission','rate_limited':'rate_limit'}.get(protection.get('reason'),'site_data')
            raise Fault('native_task_'+str(job.get('status','unreviewed')),category)
        validation=inside(self.source,job.get('validation',''))
        # Native tasks keep their original validation input beside their
        # output, including monthly/detail variations and admission samples.
        input_file=validation.with_name('validation_input.json')
        if not input_file.is_file():
            input_file=self.source/'state/collection_queue/tasks'/job['key']/'validation_input.json'
        value=replay(input_file,self.source,destination=self.instance/'parsed'/job['key'],expected_validation=validation)
        self.last_replay=value
        self.latest=(value['source_sha256'],value['source_path'])
        return Response(value['response_status'],'application/pdf' if value['body'].startswith(b'%PDF-') else 'application/json',
            value['body'],operation=trace)


class ShaanxiEngine(Engine):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.responses.link_from=self.transport

    async def collect_native_job(self,job):
        identity=task_identity(job,self.account_alias);dataset=identity.dataset;period=job['key']
        self.ledger.seed(dataset,[period])
        self.decode_response=lambda raw:self.transport.last_replay['parsed']
        result=await self.request('collect',{'transport':'native','legacy_key':job['key']},
            {'kind':'document' if job['kind'] in {'download','monthly_attachment'} else 'json'},
            dataset=dataset,period=period,identity=identity)
        if result.response.operation and result.response.operation.get('transition_only'):
            self.ledger.settle_task(dataset,period,'skipped',reason='native_transition_only')
        elif result.outcome.result=='saved':
            # Original response bytes and semantic sidecars are independently
            # verified. The existing Archive links responses/ -> original/.
            result.record=self.archive.store(dataset,period,result.response.body,result.outcome.extension,
                legacy_job_key=job['key'],legacy_source=job['datasetId'],http_status=result.response.status)
            self.ledger.settle_task(dataset,period,'saved',sha256=result.record['sha256'])
        else:
            self.ledger.settle_task(dataset,period,'empty',reason='native_verified_empty')
        return result
