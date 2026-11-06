import unittest
from dataclasses import replace

from helpers import Clock, FakeTransport, TempDir, dataset, envelope, make_province
from pmos.engine import Engine
from pmos.faults import Fault


class MissingHourlyTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_hours_are_validated_archived_and_cannot_be_queried_twice(self):
        tmp = TempDir()
        self.addCleanup(tmp.cleanup)
        clock = Clock()
        item = dataset('real_time_uniform_settlement_price', response='json')
        item['request'] = {'transport':'ui','action':'export','frame':'hourly', 'menu':['市场','电价'],
                           'fields':[{'kind':'date','placeholder':'选择日期','value':'{date}'}]}
        province = replace(make_province(tmp.path, [item]), id='anhui')
        transport = FakeTransport(clock, default=envelope(0, {f'{h:02d}:00': '-0.1' for h in range(1,25)}))
        engine = Engine(province, transport, instance=tmp.path/'var', clock=clock)
        self.addCleanup(engine.ledger.close)
        spec = province.datasets()[item['id']]
        command = {'period':'2026-10-22','approval':'user'}
        result = await engine._collect_missing_hourly(spec, command)
        self.assertEqual((result['result'],result['points']), ('saved',24))
        self.assertEqual(engine.ledger.task(spec.id, command['period'])['status'], 'saved')
        self.assertTrue(engine.archive.verify(engine.archive.records(spec.id)[-1]))
        with self.assertRaisesRegex(Fault, 'hourly_task_already_present'):
            await engine._collect_missing_hourly(spec, command)
        self.assertEqual(len(transport.sent), 1)

    async def test_23_points_kept_as_response_but_not_confirmed_as_complete(self):
        tmp = TempDir()
        self.addCleanup(tmp.cleanup)
        item = dataset('real_time_uniform_settlement_price', response='json')
        item['request'] = {'transport':'ui','action':'export','frame':'hourly', 'menu':['市场','电价'],
                           'fields':[{'kind':'date','placeholder':'选择日期','value':'{date}'}]}
        province = replace(make_province(tmp.path, [item]), id='anhui')
        engine = Engine(province, FakeTransport(default=envelope(0,{f'{h:02d}:00':'1' for h in range(1,24)})), instance=tmp.path/'var')
        self.addCleanup(engine.ledger.close)
        with self.assertRaisesRegex(Fault, 'hourly_values_incomplete'):
            await engine._collect_missing_hourly(province.datasets()[item['id']], {'period':'2026-10-22','approval':'user'})
        self.assertEqual(len(engine.samples.records(item['id'])), 1)
        self.assertEqual(len(engine.archive.records(item['id'])), 0)
        self.assertIsNone(engine.ledger.task(item['id'], '2026-10-22'))
