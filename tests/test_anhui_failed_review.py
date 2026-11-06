import unittest
from dataclasses import replace
from helpers import Clock,FakeTransport,TempDir,dataset,envelope,make_province
from pmos.engine import Engine
from pmos.faults import Fault

class FailedReviewTests(unittest.IsolatedAsyncioTestCase):
    def create(self,data):
        tmp=TempDir();self.addCleanup(tmp.cleanup);clock=Clock()
        item=dataset('important_channel_transmission_actual',response='json')
        province=replace(make_province(tmp.path,[item]),id='anhui')
        transport=FakeTransport(clock,default=envelope(0,data))
        engine=Engine(province,transport,instance=tmp.path/'var',clock=clock);self.addCleanup(engine.ledger.close)
        spec=province.datasets()[item['id']];engine.ledger.seed(spec.id,['2026-10-31'])
        old=engine.ledger.begin('collect',spec.id,'2026-10-31');engine.ledger.finish(old,'unknown',reason='network_error')
        engine.ledger.settle_task(spec.id,'2026-10-31','failed',reason='isolated:network_error');clock.advance(31)
        return engine,spec,old
    async def test_valid_new_response_replaces_failed_task_but_preserves_old_attempt(self):
        clocks=[f'{h:02d}:{m:02d}' for h in range(24) for m in (0,15,30,45) if h or m]+['24:00']
        engine,spec,old=self.create({t:'-1.5' for t in clocks})
        r=await engine._review_failed(spec,{'period':'2026-10-31','approval':'user'})
        self.assertEqual(r['result'],'saved');self.assertEqual(engine.ledger.task(spec.id,'2026-10-31')['status'],'saved')
        self.assertEqual(engine.ledger.db.execute('select outcome from attempts where id=?',(old,)).fetchone()[0],'unknown')
        self.assertTrue(engine.archive.verify(engine.archive.records(spec.id)[-1]))
    async def test_incomplete_response_is_retained_failed_task_not_promoted(self):
        engine,spec,_=self.create({'00:15':'1'})
        with self.assertRaisesRegex(Fault,'failed_review_values_incomplete'):
            await engine._review_failed(spec,{'period':'2026-10-31','approval':'user'})
        self.assertEqual(engine.ledger.task(spec.id,'2026-10-31')['status'],'failed')
        self.assertEqual(len(engine.samples.records(spec.id)),1);self.assertEqual(len(engine.archive.records(spec.id)),0)

class HourlyFailedReviewTests(unittest.IsolatedAsyncioTestCase):
    async def test_hourly_list_requires_24_times_and_preserves_values(self):
        tmp=TempDir();self.addCleanup(tmp.cleanup);clock=Clock()
        item=dataset('publication_22c779b5bdc2edc8',response='json')
        province=replace(make_province(tmp.path,[item]),id='anhui')
        data=[{'dataTime':f'{h:02d}:00','record':'-2.5'} for h in range(1,25)]
        engine=Engine(province,FakeTransport(clock,default=envelope(0,data)),instance=tmp.path/'var',clock=clock);self.addCleanup(engine.ledger.close)
        spec=province.datasets()[item['id']];engine.ledger.seed(spec.id,['2026-10-31'])
        old=engine.ledger.begin('collect',spec.id,'2026-10-31');engine.ledger.finish(old,'unknown',reason='network_error')
        engine.ledger.settle_task(spec.id,'2026-10-31','failed',reason='isolated:network_error');clock.advance(31)
        r=await engine._review_failed(spec,{'period':'2026-10-31','approval':'user'})
        self.assertEqual(r['result'],'saved');self.assertTrue(engine.archive.verify(engine.archive.records(spec.id)[-1]))

class PrimeHistoricalOptionTests(unittest.IsolatedAsyncioTestCase):
    async def test_historical_option_is_sent_by_bound_template_after_priming(self):
        tmp=TempDir();self.addCleanup(tmp.cleanup);clock=Clock()
        item=dataset('important_channel_transmission_actual',response='json')
        item['scopes']=[{'通道':'历史通道'}]
        item['request']={'transport':'fetch','method':'POST','path':'/px-test/flow',
            'template_body_set':{'businessTime':'{date}','genName':'{scope[通道]}'},
            'bootstrap':{'transport':'ui','action':'query','frame':'flow','path':'/px-test/flow',
                'fields':[{'kind':'date','placeholder':'日期','value':'{date}'},{'kind':'select','label':'通道','value':'{scope[通道]}'}]}}
        class PrimingTransport(FakeTransport):
            async def send(self,request):
                if request['transport']=='ui':self.unready.clear()
                return await super().send(request)
        clocks=[f'{h:02d}:{m:02d}' for h in range(24) for m in (0,15,30,45) if h or m]+['24:00']
        transport=PrimingTransport(clock,default=envelope(0,{t:'1' for t in clocks}));transport.unready={'/px-test/flow'}
        province=replace(make_province(tmp.path,[item]),id='anhui')
        engine=Engine(province,transport,instance=tmp.path/'var',clock=clock);self.addCleanup(engine.ledger.close)
        spec=province.datasets()[item['id']];scope=spec.scope_items()[0][0]
        engine.ledger.seed(spec.id,['2026-10-31'],scope);old=engine.ledger.begin('collect',spec.id,'2026-10-31',scope)
        engine.ledger.finish(old,'unknown',reason='network_error');engine.ledger.settle_task(spec.id,'2026-10-31','failed',scope=scope);clock.advance(31)
        engine.spacing_wait=lambda *args: 0  # fake clock does not advance during asyncio.sleep
        result=await engine._review_failed(spec,{'period':'2026-10-31','scope':scope,'approval':'user'})
        self.assertEqual(result['result'],'saved');self.assertEqual(transport.sent[0]['fields'][1]['any_listed'],True)
        self.assertEqual(transport.sent[1]['template_body_set'],{'businessTime':'2026-10-31','genName':'历史通道'})
