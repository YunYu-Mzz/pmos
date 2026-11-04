import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from pmos.shaanxi_lab import ROOT
from pmos.shaanxi_replay import replay
from pmos.shaanxi_transport import atomic
from pmos.faults import Fault
from pmos.adapters.shaanxi_native.settlement_classification import normalize


class Replay(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name);self.source=self.base/'legacy';self.source.mkdir()
        f=json.loads((ROOT/'tests/fixtures/shaanxi_grid_fixture.json').read_text())
        self.raw=json.dumps(f['body'],ensure_ascii=False).encode();self.payload=self.source/'fixture.response';self.payload.write_bytes(self.raw)
        self.request={'dataset':{**f['contract']['dataset'],'endpoint':'/fixture/data'},'params':f['contract']['params'],
            'turn':{**f['contract']['turn'],'responses':[{'kind':'data','path':'/fixture/data','status':200,
                'responseFile':str(self.payload),'sha256':hashlib.sha256(self.raw).hexdigest()}]}}
        self.input=self.source/'validation_input.json';atomic(self.input,self.request)

    def test_original_hash_and_native_values_match(self):
        x=replay(self.input,self.source);self.assertEqual(x['parsed'].rows,json.loads(self.raw)['data']['list'])
        self.assertEqual(x['website_requests'],0)

    def test_sidecar_export_writes_only_independent_output(self):
        out=self.base/'pmos/output';replay(self.input,self.source,destination=out)
        self.assertTrue((out/'validated.json').exists());self.assertTrue((out/'native_rows.csv').exists())
        self.assertFalse((self.source/'native_rows.csv').exists())
        with self.assertRaises(Fault):replay(self.input,self.source,destination=self.source/'forbidden')

    def test_changed_original_is_rejected(self):
        self.payload.write_bytes(b'changed')
        with self.assertRaises(Fault):replay(self.input,self.source)

    def test_replay_must_match_the_original_validation(self):
        validation=replay(self.input,self.source)['validation']
        expected=self.source/'validated.json';atomic(expected,validation)
        replay(self.input,self.source,expected_validation=expected)
        validation['response_sha256']='0'*64;validation['sha256']='0'*64;atomic(expected,validation)
        with self.assertRaisesRegex(Fault,'native_validation_primary_mismatch'):
            replay(self.input,self.source,expected_validation=expected)

    def test_unreviewed_external_path_is_rejected(self):
        self.request['turn']['responses'][0]['responseFile']=str(self.base/'outside.response');atomic(self.input,self.request)
        with self.assertRaises(Fault):replay(self.input,self.source)

    def test_pdf_original_is_linked_without_repeated_download_or_parser_claim(self):
        self.payload.write_bytes(b'%PDF-1.4\nsynthetic fixture only\n%%EOF')
        self.request['download']=True;self.request['turn']['responses'][0]['sha256']=hashlib.sha256(self.payload.read_bytes()).hexdigest();atomic(self.input,self.request)
        out=self.base/'pmos/pdf';x=replay(self.input,self.source,destination=out)
        self.assertEqual((out/'original.pdf').stat().st_ino,self.payload.stat().st_ino)
        self.assertEqual(x['validation']['kind'],'pdf_original')
        self.assertEqual(x['parsed'].rows,[])

    def test_settlement_zero_price_and_quantity_survive_ported_semantic_parser(self):
        rows=normalize([{'date':'2026-01-01','table':1,'side':'合成发电侧','category':'合成分类',
            'quantity':'0','quantityUnit':'亿千瓦时','price':'0','priceUnit':'元/兆瓦时','sourceMissingMarker':None}],
            {'status':'observed_section_boundaries','scopes':{'table:1':['合成说明']}},'0'*64)
        self.assertEqual([r['metric'] for r in rows],['结算电量','结算均价']);self.assertTrue(all(r['value']=='0' for r in rows))
