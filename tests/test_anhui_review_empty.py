import unittest
from dataclasses import replace

from helpers import Clock, FakeTransport, TempDir, dataset, envelope, make_province
from pmos.engine import Engine
from pmos.faults import Fault


class ReviewEmptyTests(unittest.IsolatedAsyncioTestCase):
    async def test_bootstrap_matches_both_date_and_channel_in_the_site_request(self):
        tmp = TempDir()
        self.addCleanup(tmp.cleanup)
        clock = Clock()
        item = dataset('flow', response='json')
        item['scopes'] = [{'通道': '湖泉'}]
        item['request'] = {'transport': 'fetch', 'method': 'POST', 'path': '/px-test/flow',
            'template_body_set': {'businessTime': '{date}', 'genName': '{scope[通道]}'},
            'bootstrap': {'transport': 'ui', 'action': 'query', 'frame': 'flow', 'path': '/px-test/flow',
                          'fields': [{'kind': 'date', 'placeholder': '日期', 'value': '{date}'},
                                     {'kind': 'select', 'label': '通道', 'value': '{scope[通道]}'}]}}
        province = replace(make_province(tmp.path, [item]), id='anhui')
        transport = FakeTransport(clock)
        transport.unready = {'/px-test/flow'}
        transport.default = envelope(0, {})
        engine = Engine(province, transport, instance=tmp.path / 'var', clock=clock)
        self.addCleanup(engine.ledger.close)
        spec = province.datasets()['flow']
        scope = spec.scope_items()[0][0]
        engine.ledger.seed('flow', ['2026-10-31'], scope)
        old = engine.ledger.begin('collect', 'flow', '2026-10-31', scope)
        engine.ledger.finish(old, 'empty', http_status=200, business_status='0')
        engine.ledger.settle_task('flow', '2026-10-31', 'empty', scope=scope)
        clock.advance(31)
        result = await engine._review_empty(spec, {'period':'2026-10-31', 'scope':scope, 'approval':'review'})
        self.assertEqual(result['result'], 'empty')
        self.assertEqual(transport.sent[0]['match_request_body'], {'businessTime':'2026-10-31', 'genName':'湖泉'})
        self.assertEqual(engine.ledger.task('flow', '2026-10-31', scope)['status'], 'empty')

    async def test_review_keeps_old_empty_and_archives_new_96_point_source_once(self):
        tmp = TempDir()
        self.addCleanup(tmp.cleanup)
        clock = Clock()
        province = replace(make_province(tmp.path, [dataset('load', response='json')]), id='anhui')
        transport = FakeTransport(clock)
        engine = Engine(province, transport, instance=tmp.path / 'var', clock=clock)
        self.addCleanup(engine.ledger.close)
        spec = province.datasets()['load']
        engine.ledger.seed('load', ['2026-10-31'])
        old = engine.ledger.begin('collect', 'load', '2026-10-31')
        engine.ledger.finish(old, 'empty', http_status=200, business_status='0')
        engine.ledger.settle_task('load', '2026-10-31', 'empty')
        clock.advance(31)
        clocks = [f'{h:02d}:{m:02d}' for h in range(24) for m in (0, 15, 30, 45) if h or m] + ['24:00']
        transport.default = envelope(0, {t: '1.5' for t in clocks})
        result = await engine._review_empty(spec, {'period': '2026-10-31', 'approval': 'review'})
        self.assertEqual((result['result'], result['complete']), ('saved', True))
        self.assertTrue(engine.samples.verify(engine.samples.records('load')[-1]))
        self.assertEqual(engine.ledger.task('load', '2026-10-31')['status'], 'empty')
        self.assertEqual(engine.ledger.db.execute('select outcome from attempts where id=?', (old,)).fetchone()[0], 'empty')
        with self.assertRaisesRegex(Fault, 'review_already_attempted'):
            await engine._review_empty(spec, {'period': '2026-10-31', 'approval': 'review'})
        self.assertEqual(len(transport.sent), 1)

    async def test_requires_recorded_empty_and_respects_excluded_unit(self):
        tmp = TempDir()
        self.addCleanup(tmp.cleanup)
        province = replace(make_province(tmp.path, [dataset('load', response='json')]), id='anhui')
        engine = Engine(province, FakeTransport(), instance=tmp.path / 'var')
        self.addCleanup(engine.ledger.close)
        spec = province.datasets()['load']
        engine.ledger.seed('load', ['2026-10-31'])
        with self.assertRaisesRegex(Fault, 'review_requires_recorded_empty'):
            await engine._review_empty(spec, {'period': '2026-10-31', 'approval': 'review'})
        spec.id = 'publication_6d80c3d4514ccfc2'
        with self.assertRaisesRegex(Fault, 'review_source_not_authorized'):
            await engine._review_empty(spec, {'period': '2026-10-31', 'approval': 'review'})
