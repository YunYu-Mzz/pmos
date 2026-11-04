import hashlib,json,tempfile,time,unittest
from datetime import datetime,timezone,timedelta
from pathlib import Path
from pmos.contracts import canonical
from pmos.faults import Fault
from pmos.province import Province
from pmos.shaanxi_direct_runner import DirectEngine,DirectRunner
from pmos.shaanxi_direct_store import DirectStore
from pmos.shaanxi_direct_transport import DirectTransport
from pmos.shaanxi_direct_reporting import EXTRA_SCHEMA
from pmos.shaanxi_transport import atomic,OriginalWorkerLock,load


class FakeBrowser:
    """A fixture transport, deliberately no browser or website access."""
    def __init__(self,root,fixture):self.root=root;self.fixture=fixture;self.calls=[];self.http_status=200;self.busy=False
    async def call(self,op,**kw):
        self.calls.append(op)
        if op=='compile':
            action=kw['input']['op']
            if action=='snapshot':return {'expression':'fixture_snapshot'}
            if action=='bind':return {'component':{'fixture':True}}
            if action=='query':return {'expression':'fixture_native_method','nativeFilters':{},'expectedPath':'/fixture'}
            raise AssertionError(action)
        if op=='inspect':return {'matched':True,'components':[]}
        if op=='native':
            if self.busy:self.busy=False;return {'phase':'not_sent','reason':'native_turn_busy','websiteRequestsIssued':0}
            plan=kw['plan'];key=hashlib.sha256(canonical(plan['identity'])).hexdigest();folder=self.root/'native_turns'/key
            raw=json.dumps(self.fixture['body']).encode();folder.mkdir(parents=True,exist_ok=True);p=folder/'fixture.response';p.write_bytes(raw)
            return {**self.fixture['contract']['turn'],'phase':'captured_requires_adapter_validation','key':key,'targetId':'data','directory':str(folder),'pending':[],'failures':[],'responses':[{'kind':'data','path':'/fixture','status':self.http_status,'sha256':hashlib.sha256(raw).hexdigest(),'responseFile':str(p)}],'observedSessionId':'fixture','observedRequests':[{'requestId':key,'path':'/fixture','startedAt':datetime.now(timezone.utc).isoformat()}]}
        raise AssertionError(op)


class DirectPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);root=Path(self.tmp.name);self.source=root/'source';self.instance=root/'instance';self.instance.mkdir()
        queue=self.source/'state/collection_queue';queue.mkdir(parents=True);(queue/'worker.lock').touch();self.journal=queue/'journal.json';self.journal.write_text('legacy must stay frozen')
        self.lock=OriginalWorkerLock(self.source);self.lock.acquire();self.addCleanup(self.lock.close)
        self.fixture=load(Path(__file__).parent/'fixtures/shaanxi_grid_fixture.json');self.d={**self.fixture['contract']['dataset'],'endpoint':'/fixture','enabled':True,'collectorTarget':'data'}
        province=root/'provinces/shaanxi';province.mkdir(parents=True);atomic(province/'province.json',{'id':'shaanxi','name':'fixture','origin':'https://offline.invalid','policy':{'response_interval_seconds':1}})
        self.engine=DirectEngine(Province.load('shaanxi',root/'provinces'),None,instance=self.instance,account_alias='test');self.engine.acquire();self.addCleanup(self.engine.release)
        self.store=DirectStore(self.engine.ledger,'test');self.store.import_frozen({'jobs':{}},{'province':'shaanxi','collectorTarget':'data','keeperTarget':'keeper','datasets':[self.d]},sha256='a'*64,source_ref='frozen')
        self.transport=DirectTransport({'source_root':str(self.source),'instance':str(self.instance),'account_alias':'test','direct':{}},root,self.store,self.lock);self.engine.transport=self.transport;self.fake=FakeBrowser(self.instance,self.fixture);self.transport.rpc=self.fake
        now=datetime.now(timezone.utc);atomic(self.source/'state/keeper/status.json',{'at':now.isoformat(),'controlEnabled':True,'nextAttemptAt':(now+timedelta(seconds=300)).isoformat()});atomic(self.source/'state/keeper/last_success.json',{'finishedAt':now.isoformat(),'passed':True,'fullSampleMatches':True})
    def job(self,day='2026-01-01'):
        key=self.store.seed(self.d['id'],'table',{'date':day,'page':1,'size':50});return self.store.job(key)

    async def test_direct_engine_archives_once_preserves_values_and_no_old_queue(self):
        j=self.job();before=self.journal.stat();result=await self.engine.collect_direct(j);after=self.journal.stat()
        self.assertEqual(before.st_mtime_ns,after.st_mtime_ns);self.assertEqual(self.journal.read_text(),'legacy must stay frozen')
        rows=self.transport.value['validation']['rows'];self.assertEqual([rows[0][k] for k in ['00:15','00:30','00:45','01:00']],[0,-1,None,''])
        self.assertTrue(self.engine.archive.verify(result.record));self.assertNotIn('queueStep',self.fake.calls)
        self.assertEqual(self.fake.calls.count('native'),1)
        with self.assertRaises(Fault):await self.engine.collect_direct(j)
        self.assertEqual(self.fake.calls.count('native'),1)
        self.assertEqual(self.store.db.execute('select count(*) from sn_requests').fetchone()[0],1)

    async def test_two_dates_only_open_first_page_then_reuse_native_component(self):
        original=self.fake.call;opened=False;actions=[]
        async def browser(op,**kw):
            nonlocal opened
            if op=='compile':
                action=kw['input']['op']
                if action=='bind' and not opened:raise Fault('fixture_unbound','local')
                if action=='open':return {'expression':'open','expectedPath':'/fixture'}
                if action=='navigation_params':return {'params':{'date':'2026-01-01','page':1,'size':50}}
            if op=='native':
                actions.append(kw['plan']['identity']['action'])
                if actions[-1]=='open':opened=True
            return await original(op,**kw)
        self.fake.call=browser
        await self.engine.collect_direct(self.job())
        self.engine.clock=lambda:time.time()+2;self.fixture['body']['data']['list'][0]['date']='2026-01-02'
        await self.engine.collect_direct(self.job('2026-01-02'))
        self.assertEqual(actions,['open','query','query'])

    async def test_http412_is_only_this_attempt_no_global_stop(self):
        self.fake.http_status=412
        with self.assertRaises(Fault):await self.engine.collect_direct(self.job())
        self.assertEqual(self.engine.ledger.db.execute('select outcome from attempts').fetchone()[0],'failed')
        self.assertFalse((self.source/'state/session_block.json').exists());self.assertIsNone(self.engine.state.blocked())
        self.fake.http_status=200
        self.engine.clock=lambda:time.time()+2
        # A separate source/day identity, using its own matching source rows.
        self.fixture['body']['data']['list'][0]['date']='2026-01-02'
        result=await self.engine.collect_direct(self.job('2026-01-02'));self.assertEqual(result.outcome.result,'saved')

    async def test_not_sent_mutex_race_releases_identity_without_resending_issued_stage(self):
        self.fake.busy=True;j=self.job()
        with self.assertRaises(Fault) as caught:await self.engine.collect_direct(j)
        self.assertTrue(caught.exception.unsent);self.assertEqual(self.engine.ledger.db.execute('select outcome from attempts').fetchone()[0],'not_sent')
        # Exercise the next attempt's same native stage key only after a
        # durable zero-send proof; the first attempt remains in history.
        self.store.update(j,state='pending',consumed=False)
        self.engine.clock=lambda:time.time()+2
        result=await self.engine.collect_direct(j);self.assertEqual(result.outcome.result,'saved')
        self.assertEqual([r[0] for r in self.store.db.execute('select outcome from attempts order by id')],['not_sent','success'])
        self.assertEqual(self.store.db.execute('select count(*) from sn_commands').fetchone()[0],2)

    async def test_due_keeper_yields_before_durable_attempt(self):
        now=datetime.now(timezone.utc);atomic(self.source/'state/keeper/status.json',{'at':now.isoformat(),'controlEnabled':True,'nextAttemptAt':(now-timedelta(seconds=1)).isoformat()})
        with self.assertRaises(Fault):await self.engine.collect_direct(self.job())
        self.assertEqual(self.store.db.execute('select count(*) from attempts').fetchone()[0],0);self.assertNotIn('native',self.fake.calls)

    async def test_retired_registry_five_second_value_cannot_override_pmos_one_second_policy(self):
        self.transport.registry['responseEndIntervalSeconds']=5;seen=[]
        async def start(config):seen.append(config);return {'phase':'initialized'}
        self.fake.start=start;await self.transport.start();self.assertEqual(seen[0]['response_interval_ms'],1000)

    async def test_keeper_mutex_between_stages_does_not_repeat_the_completed_stage(self):
        job=self.job();plan={'expression':'fixture','expectedPath':'/fixture'}
        await self.transport.native(self.d,job,1,'open',plan);self.fake.busy=True
        result=await self.transport.native(self.d,job,1,'query',plan)
        self.assertEqual(result['phase'],'captured_requires_adapter_validation');self.assertEqual(self.fake.calls.count('native'),3)
        self.assertEqual(self.store.db.execute('select count(*) from sn_commands').fetchone()[0],2)
        self.assertEqual(len(list((self.instance/'direct_commands').glob('*.not_sent.1.json'))),1)

    async def test_slow_native_menu_initialization_has_own_budget_without_extending_regular_queries(self):
        job=self.job();plan={'expression':'fixture','expectedPath':'/fixture'};budgets=[];original=self.fake.call
        async def browser(op,**kw):
            if op=='native':budgets.append((kw['plan']['identity']['action'],kw['plan']['timeoutMs']))
            return await original(op,**kw)
        self.fake.call=browser
        await self.transport.native(self.d,job,1,'open',plan)
        await self.transport.native(self.d,job,1,'query',plan)
        self.assertEqual(budgets,[('open',90000),('query',45000)])

    async def test_frontier_and_common_task_confirm_in_one_transaction_idempotently(self):
        self.store.db.executescript(EXTRA_SCHEMA);job=self.job();await self.engine.collect_direct(job)
        runner=DirectRunner.__new__(DirectRunner);runner.store=self.store;runner.engine=self.engine;runner.config={'account_alias':'test'}
        self.assertTrue(runner.confirm(self.transport.job,self.transport.value));self.assertTrue(runner.confirm(self.transport.job,self.transport.value))
        self.assertEqual(self.store.job(job['key'])['status'],'saved');self.assertFalse(self.store.db.in_transaction)
        common=self.store.db.execute('select status,attempts from tasks').fetchone();self.assertEqual(tuple(common),('saved',1));self.assertEqual(self.store.db.execute('select count(*) from sn_job_artifacts').fetchone()[0],1)

    async def test_crash_after_response_reconciles_cached_original_without_another_call(self):
        self.store.db.executescript(EXTRA_SCHEMA);job=self.job();await self.engine.collect_direct(job)
        # v9 lost the running marker while persisting discovered filters.
        # Its immutable command and completed response still prove the exact
        # attempt and must be recoverable without reissuing the query.
        self.store.update(job,state='pending',consumed=True)
        runner=DirectRunner.__new__(DirectRunner);runner.store=self.store;runner.engine=self.engine;runner.transport=self.transport;runner.instance=self.instance;runner.config={'account_alias':'test'}
        runner.reconcile();self.assertEqual(self.store.job(job['key'])['status'],'saved');self.assertEqual(self.fake.calls.count('native'),1);self.assertEqual(self.store.db.execute('select count(*) from attempts').fetchone()[0],1)

    async def test_filtered_old_reply_releases_target_only_with_unique_terminal_timing(self):
        key='f'*64;job=self.job();self.store.quarantine(job,'original_unknown_kept')
        self.store.hold('target','data','',{'turnKey':key,'jobKey':job['key']})
        atomic(self.instance/'native_turns'/key/'receipt.json',{'invokeResult':{},'pending':[],'failures':[],'expectedPath':'/fixture','foreignFrameRequests':[{'requestId':'old','path':'/fixture','startedAt':'2026-11-02T00:00:00Z'}]})
        complete=False;calls=[]
        async def passive(op,**kwargs):
            calls.append(op)
            if op=='inspect':return [{'requestId':'old','path':'/fixture','uniqueMatch':complete,'evidence':{'start':1,'end':2}}]
            if op=='acknowledge_settled':return {'phase':'released'}
            if op=='cancel_isolated_target':return {'phase':'waiting_turn_lock'}
            raise AssertionError('A passive release must not invoke the website')
        self.fake.call=passive;r=DirectRunner.__new__(DirectRunner);r.store=self.store;r.transport=self.transport;r.instance=self.instance
        await r.release_late_targets();self.assertEqual(self.store.db.execute("select count(*) from sn_holds where scope='target'").fetchone()[0],1)
        complete=True;await r.release_late_targets();self.assertEqual(self.store.db.execute("select count(*) from sn_holds where scope='target'").fetchone()[0],0)
        self.assertEqual(self.store.job(job['key'])['error'],'original_unknown_kept');self.assertEqual(calls,['inspect','cancel_isolated_target','inspect','acknowledge_settled'])

if __name__=='__main__':unittest.main()
