import unittest
from dataclasses import replace
from helpers import FakeTransport, TempDir, dataset, envelope, make_province
from pmos.engine import Engine
from pmos.faults import Fault
from pmos.spec import scope_key

class DayaheadTests(unittest.IsolatedAsyncioTestCase):
    def setup_engine(self, data):
        tmp=TempDir(); self.addCleanup(tmp.cleanup)
        item=dataset('day_ahead_uniform_settlement_price', response='json')
        item['request']={'transport':'ui','action':'export','frame':'f','menu':['市场']}
        province=replace(make_province(tmp.path,[item]),id='anhui')
        transport=FakeTransport(default=envelope(0,data))
        engine=Engine(province,transport,instance=tmp.path/'var');self.addCleanup(engine.ledger.close)
        engine.spacing_wait=lambda *args: 0  # pacing is tested separately
        return engine,province.datasets()[item['id']],transport
    async def test_each_type_is_separate_and_all_24_are_archived(self):
        engine,spec,transport=self.setup_engine({f'{h:02}:00':h for h in range(1,25)})
        for t in ('电价','电量'):
            result=await engine._collect_missing_dayahead(spec,{'period':'2026-10-29','price_type':t,'approval':'user'})
            self.assertEqual(result['points'],24)
            self.assertEqual(engine.ledger.task(spec.id,'2026-10-29',result['scope'])['status'],'saved')
            self.assertEqual(transport.sent[-1]['match_selected_models'],{'type':'数据类型','genName':'调度单元名称'})
        self.assertNotEqual(transport.sent[0]['fields'][1]['value'],transport.sent[1]['fields'][1]['value'])
        with self.assertRaisesRegex(Fault,'dayahead_task_already_present'):
            await engine._collect_missing_dayahead(spec,{'period':'2026-10-29','price_type':'电价','approval':'user'})
    async def test_incomplete_receipt_is_kept_but_not_archived(self):
        engine,spec,_=self.setup_engine({f'{h:02}:00':h for h in range(1,24)})
        with self.assertRaisesRegex(Fault,'dayahead_values_incomplete'):
            await engine._collect_missing_dayahead(spec,{'period':'2026-10-29','price_type':'电价','approval':'user'})
        self.assertEqual(len(engine.samples.records(spec.id)),1)
        self.assertEqual(len(engine.archive.records(spec.id)),0)
