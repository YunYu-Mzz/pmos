"""Verified-empty source policy; no live browser or network."""
import unittest
from pmos.engine import Engine
from pmos.faults import Fault
from pmos.validate import Response
from helpers import Clock, FakeTransport, TempDir, dataset, envelope, make_province


class EmptyResponsePolicyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = TempDir()
        self.addCleanup(self.tmp.cleanup)
        self.clock = Clock()

    def engine(self, continue_empty=True):
        spec = dataset('maintenance', response='json', keepalive=True)
        if continue_empty:
            spec['source'] = {'empty_response_policy': 'continue'}
        self.province = make_province(self.tmp.path, [spec], policy={'response_interval_seconds': 0.5})
        self.transport = FakeTransport(self.clock, default=envelope(0, data=[]))
        e = Engine(self.province, self.transport, instance=self.tmp.path / 'var', clock=self.clock)
        self.addCleanup(e.ledger.close)
        e.state.set_control(collect=True)
        return e, self.province.datasets()['maintenance']

    async def collect(self, e, s, day):
        e.ledger.seed(s.id, [day])
        self.clock.advance(1)
        return await e.collect(s, day, '')

    async def test_old_empty_dates_preserved_and_two_new_empty_dates_complete(self):
        e, s = self.engine()
        for day in ['2026-11-01', '2026-11-02']:
            await self.collect(e, s, day)
        before = [dict(e.ledger.task(s.id, day, '')) for day in ['2026-11-01', '2026-11-02']]
        for day in ['2026-11-04', '2026-11-03']:
            await self.collect(e, s, day)
        self.assertEqual(before, [dict(e.ledger.task(s.id, day, '')) for day in ['2026-11-01', '2026-11-02']])
        self.assertEqual(e.ledger.summary()[s.id]['empty'], 4)
        self.assertEqual(len(self.transport.sent), 4)
        self.assertEqual(e.dataset_holds(), {})
        self.assertIsNone(e.state.blocked())
        self.assertEqual(e.state.read('empty_streaks')[s.id], 4)
        self.assertEqual(len(list(e.responses.records(s.id))), 4)

    async def test_other_sources_keep_their_original_empty_threshold(self):
        e, s = self.engine(False)
        await self.collect(e, s, '2026-11-01')
        await self.collect(e, s, '2026-11-02')
        self.assertEqual(e.dataset_holds()[s.id]['reason'], 'consecutive_empty_periods')

    async def test_nonempty_still_saves_and_resets_empty_streak(self):
        e, s = self.engine()
        await self.collect(e, s, '2026-11-01')
        self.transport.queue.append(envelope(0, data=[{'value': 0}]))
        result = await self.collect(e, s, '2026-11-02')
        self.assertEqual(result.outcome.result, 'saved')
        self.assertEqual(e.state.read('empty_streaks')[s.id], 0)
        self.assertEqual(e.ledger.task(s.id, '2026-11-02', '')['status'], 'saved')

    async def test_unauthorized_is_not_relabelled_empty(self):
        e, s = self.engine()
        self.transport.queue.append(Response(401, 'application/json', b'{"status":401,"data":[]}'))
        with self.assertRaises(Fault):
            await self.collect(e, s, '2026-11-03')
        self.assertEqual(e.ledger.last_attempt('collect')['outcome'], 'failed')
        self.assertNotEqual(e.ledger.task(s.id, '2026-11-03', '')['status'], 'empty')

    async def test_network_unknown_is_preserved(self):
        e, s = self.engine()
        self.transport.queue.append(Fault('network_error', 'network'))
        with self.assertRaises(Fault):
            await self.collect(e, s, '2026-11-03')
        self.assertEqual(e.ledger.last_attempt('collect')['outcome'], 'unknown')
        self.assertEqual(e.ledger.task(s.id, '2026-11-03', '')['status'], 'pending')

    async def test_rate_limit_is_not_relabelled_empty(self):
        e, s = self.engine()
        self.transport.queue.append(Response(429, 'application/json', b'{"status":429,"data":[]}'))
        with self.assertRaises(Fault):
            await self.collect(e, s, '2026-11-03')
        self.assertNotEqual(e.ledger.task(s.id, '2026-11-03', '')['status'], 'empty')

    async def test_unknown_business_response_is_not_relabelled_empty(self):
        e, s = self.engine()
        self.transport.queue.append(envelope(500, data=[]))
        with self.assertRaises(Fault):
            await self.collect(e, s, '2026-11-03')
        self.assertNotEqual(e.ledger.task(s.id, '2026-11-03', '')['status'], 'empty')


if __name__ == '__main__':
    unittest.main()
