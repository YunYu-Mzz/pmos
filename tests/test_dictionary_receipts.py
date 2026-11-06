import json
import unittest
from pmos.engine import Engine
from pmos.faults import Fault
from pmos.validate import Response
from helpers import Clock, FakeTransport, TempDir, dataset, envelope, make_province


class DictionaryReceiptTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=TempDir();self.addCleanup(self.tmp.cleanup);self.clock=Clock()
        value=dataset('report',response='json')
        value['request']={'transport':'component','method':'POST','path':'/px-test/report','frame':'/report','component':'report','call':'initData','set':{'queryParam.date':'{date}','queryParam.type':'{scope[type]}'}}
        value['scopes']=[{'type':'old'}]
        value['source']={'dictionary':{'path':'/px-test/dictionary','call':'initDic','field':'type','set':{'queryParam.date':'{date}','queryParam.type':None}}}
        self.province=make_province(self.tmp.path,[value]);self.transport=FakeTransport(self.clock)
        self.engine=Engine(self.province,self.transport,instance=self.tmp.path/'var',clock=self.clock)
        self.addCleanup(self.engine.ledger.close);self.spec=self.province.datasets()['report']

    async def test_empty_dictionary_raw_is_durable_and_no_data_task_created(self):
        response=envelope(0,data=[]);self.transport.queue.append(response)
        result=await self.engine._read_dictionary(self.spec,'2026-10-29')
        self.assertEqual(result['listed'],0)
        records=self.engine.samples.records('report');self.assertEqual(len(records),1)
        r=records[0];self.assertEqual(r['acquisition'],'dictionary_audit_native')
        self.assertEqual(r['native_set'],{'queryParam.date':'2026-10-29','queryParam.type':None})
        self.assertEqual((self.engine.samples.root/r['path']).read_bytes(),response.body)
        self.assertEqual(self.engine.ledger.summary(),{})

    async def test_nonempty_dictionary_preserves_legacy_sample_and_new_option_notice(self):
        self.transport.queue.append(envelope(0,data=[{'type':'old'},{'type':'new'}]))
        result=await self.engine._read_dictionary(self.spec,'2026-10-29')
        self.assertEqual(result['new'],['new'])
        records=self.engine.samples.records('report')
        self.assertEqual({r['scope'] for r in records},{'dictionary','dictionary_audit_1'})
        self.assertEqual(self.engine.state.read('new_options')['report']['new'],['new'])
        self.assertEqual(self.engine.ledger.summary(),{})

    async def test_identical_empty_bytes_retain_distinct_business_date_receipts(self):
        self.transport.default=envelope(0,data=[])
        await self.engine._read_dictionary(self.spec,'2026-10-28')
        self.clock.advance(31)
        await self.engine._read_dictionary(self.spec,'2026-10-29')
        records=self.engine.samples.records('report')
        self.assertEqual(len(records),2)
        self.assertEqual({r['intended_period'] for r in records},{'2026-10-28','2026-10-29'})

    async def test_auth_error_cannot_be_recorded_as_successful_dictionary(self):
        self.transport.queue.append(Response(401,'application/json',b'{"status":401,"data":[]}'))
        with self.assertRaises(Fault):
            await self.engine._read_dictionary(self.spec,'2026-10-29')
        self.assertEqual(self.engine.samples.records('report'),[])


if __name__=='__main__':unittest.main()
