"""Synthetic shapes and exact legacy keys; no live responses or browsers."""
import copy
import hashlib
import json
import unittest
from pathlib import Path
from pmos.adapters.shaanxi import decode
from pmos.adapters.shaanxi_legacy.spot_tables import ValidationError
from pmos.contracts import canonical
from pmos.faults import Fault
from pmos.shaanxi_lab import ROOT, identity_bridge, inventory, fixture_demo

FIXTURE = json.loads((ROOT/'tests/fixtures/shaanxi_grid_fixture.json').read_text())


def run_decode(body, contract):
    raw = json.dumps(body, ensure_ascii=False).encode()
    c = copy.deepcopy(contract); c['response_sha256'] = hashlib.sha256(raw).hexdigest()
    return decode(raw, c)


def base(family, query=None):
    query = query or {'fixture':'query'}
    return {'dataset':{'id':'fixture_source','family':family}, 'params':{},
        'turn':{'phase':'captured_requires_adapter_validation','pending':[],'failures':[],
                'invokeResult':{'nativeQuery':query}},
        'query_binding':{'expected':query}}


class Grid(unittest.TestCase):
    def setUp(self):
        self.b, self.c = copy.deepcopy(FIXTURE['body']), copy.deepcopy(FIXTURE['contract'])

    def test_grid_preserves_zero_negative_null_empty_and_all_96_fields(self):
        x = run_decode(self.b,self.c)
        self.assertFalse(x.empty)
        for key,val in self.b['data']['list'][0].items(): self.assertEqual(x.rows[0][key],val)
        self.assertIsNone(x.unit)
        self.assertFalse(x.summary()['full_dataset_coverage_verified'])

    def test_selected_page_is_not_all_pages(self):
        self.b['data']['total']=2;self.c['params']['size']=1
        x=run_decode(self.b,self.c);self.assertFalse(x.pagination['all_pages_verified'])

    def test_missing_point_does_not_become_zero(self):
        self.b['data']['list'][0].pop('00:15')
        with self.assertRaises(Fault):run_decode(self.b,self.c)

    def test_date_and_dimensions_do_not_cross(self):
        for field,val in [('date','2026-01-02'),('UNIT_ID','another-unit'),('MEAS_TYPE','another-type')]:
            b=copy.deepcopy(self.b);b['data']['list'][0][field]=val
            with self.subTest(field=field), self.assertRaises(Fault):run_decode(b,self.c)

    def test_missing_response_date_is_unverified(self):
        self.b['data']['list'][0].pop('date')
        with self.assertRaises(Fault):run_decode(self.b,self.c)

    def test_page_out_of_bounds_or_short_is_not_success(self):
        for page,total,size in [(2,1,50),(1,3,2)]:
            self.c['params'].update(page=page,size=size);self.b['data']['total']=total
            with self.assertRaises(Fault):run_decode(self.b,self.c)

    def test_pending_and_failures_cannot_validate(self):
        for field,val in [('pending',[{'requestId':'fixture'}]),('failures',[{'error':'unknown'}])]:
            c=copy.deepcopy(self.c);c['turn'][field]=val
            with self.assertRaises(Fault):run_decode(self.b,c)

    def test_query_drift_is_rejected(self):
        self.c['turn']['invokeResult']['form']['date']='2026-01-02'
        with self.assertRaises(Fault):run_decode(self.b,self.c)

    def test_hash_drift_and_secret_fields_are_rejected(self):
        raw=json.dumps(self.b,ensure_ascii=False).encode();self.c['response_sha256']='0'*64
        with self.assertRaises(Fault):decode(raw,self.c)
        self.b['cookie']='fixture-not-a-secret'
        with self.assertRaises(Fault):run_decode(self.b,self.c)

    def test_http200_business_auth_and_denial_are_not_data(self):
        for code,category in [(401,'auth'),(40302,'permission'),(429,'rate_limit')]:
            self.b['code']=code
            with self.subTest(code=code),self.assertRaises(Fault) as c:run_decode(self.b,self.c)
            self.assertEqual(c.exception.category,category)

    def test_preview_status0_shape(self):
        self.c['dataset']['family']='preview';self.b.pop('code');self.b.update(status=0,message='Success')
        self.assertEqual(len(run_decode(self.b,self.c).rows),1)

    def test_unknown_family_and_absent_mapping_fail_closed(self):
        for family,columns in [('unobserved',self.c['dataset']['validation']['native_columns']),('grid',[])]:
            c=copy.deepcopy(self.c);c['dataset']['family']=family;c['dataset']['validation']['native_columns']=columns
            with self.assertRaises(Fault):run_decode(self.b,c)

    def test_boolean_success_code_is_not_numeric_success(self):
        self.c['dataset']['family']='preview';self.b.pop('code');self.b.update(status=False,message='Success')
        with self.assertRaises(Fault):run_decode(self.b,self.c)

    def test_weekly_open_end_not_replaced_with_seven_days(self):
        self.c['dataset']['family']='weekly';self.c['params']={'startDate':'2026-01-01','endDate':None,'page':1,'size':50}
        self.b['data']['list'][0]['date']='2026-01-20'
        self.c['response_scope'].pop('date',None)
        self.assertFalse(run_decode(self.b,self.c).empty)
        self.c['params']['endDate']='2026-01-07'
        with self.assertRaises(Fault):run_decode(self.b,self.c)

    def test_weighted_unpaged_array_keeps_native_code_and_display_proof(self):
        self.c['dataset']['family']='weighted_price';self.c['dataset']['nativeWeighted']={'tableName':'fixture_weighted','form':{'tableName':'fixture_weighted','data_type':'1','dataSource':'fixture'}}
        methods={'search':'fixture-method-not-executed'};form={**self.c['dataset']['nativeWeighted']['form'],'date':'2026-01-01'}
        row=self.b['data']['list'][0];row.update(area_id='fixture-area',data_type='1')
        self.c['dataset']['validation']['source_identity_fields']=['date','area_id','data_type']
        self.c['dataset']['sampleContract']={'components':[{'methods':methods}],'columns':['fixture-columns']}
        self.c['turn']['postRead']={'matched':True,'components':[{'state':{'form':form,'tableData':[{**row,'data_type':'日前'}]},'methods':methods}],'columns':['fixture-columns']}
        self.c['turn']['invokeResult']={'nativeQuery':form};self.c['query_binding']={'expected':form}
        self.b['data']=[row]
        x=run_decode(self.b,self.c);self.assertEqual(x.rows[0]['data_type'],'1');self.assertIsNone(x.pagination)


class ListingsAndCurves(unittest.TestCase):
    def test_curve_null_is_not_96_zeroes_and_wrong_context_rejected(self):
        query={'dateTime':'2026-01-01','infoCode':'fixture','marketId':'fixture-market'};c=base('curve',query)
        c['dataset'].update(nativeCurve={'infoCode':'fixture','marketId':'fixture-market','guid':'fixture-menu'},validation={'native_columns':[{'prop':f'v{i:04}','label':str(i)} for i in range(96)]});c['params']={'date':'2026-01-01'}
        self.assertTrue(run_decode({'status':0,'message':'查询无数据！','data':None},c).empty)
        self.assertFalse(run_decode({'status':0,'message':'Success','data':{f'v{i:04}':i for i in range(96)}},c).empty)
        c['params']['date']='2026-01-02'
        with self.assertRaises(Fault):run_decode({'status':0,'message':'Success','data':None},c)

    def test_directory_is_metadata_without_any_file_download(self):
        c=base('directory');c['dataset']['fileName']='fixture-directory'
        b={'code':200,'errorCode':None,'data':[{'name':'fixture-directory','date':'20260101','fileId':'fixture-file','fileName':'fixture-directory_20260101.pdf','missionName':'合成标题'}]}
        x=run_decode(b,c);self.assertEqual(x.semantic_state,'attachment_directory_only_zero_downloads')
        b['data']*=2
        with self.assertRaises(Fault):run_decode(b,c)

    def test_disclosure_null_zero_and_wrong_pages_value(self):
        q={'data':{},'pageInfo':{'pageNum':1,'pageSize':20}};c=base('disclosure_list',q)
        cols=[{'prop':'title','label':'标题'}]
        c['dataset'].update(nativeListing={'infoCode':'fixture','guid':'menu','marketId':'market','filters':{}},validation={'native_columns':cols})
        c['params']={'snapshotDate':'2026-01-01','page':1,'size':20}
        state={'queryParam':q,'infoCode':'fixture','marketInfoTwo':'market','viewId':'menu','resultValue':[],'total':0}
        c['turn']['postRead']={'components':[{'state':state}],'columns':[cols]}
        x=run_decode({'status':0,'message':'Success','data':{'list':None,'total':0,'pageNum':1,'pageSize':20,'pages':999}},c)
        self.assertTrue(x.empty);self.assertEqual(x.pagination['expected_pages'],1)
        self.assertEqual(x.semantic_state,'publication_listing_only_zero_downloads')

    def test_file_listing_native_ui_details_are_not_fabricated_fields(self):
        q={'fileName':'route','createdTime':'','pageNum':1,'pageSize':20};c=base('file_list',q)
        cols=[{'prop':'fileName','label':'文件名称'}];methods={'onSearch':'fixture-search','assemableDate':'fixture-transform'}
        record={'id':'fixture-record','fileName':'fixture.pdf','filePath':'fixture-storage-id','createdTime':'2026-01-01T00:00:00+08:00'}
        c['dataset'].update(nativeFileList={'routeName':'route','createdTime':''},sampleContract={'components':[{'methods':methods}],'columns':[cols]},validation={'native_columns':cols})
        c['params']={'page':1,'size':20,'snapshotDate':'2026-01-01'}
        c['turn']['postRead']={'matched':True,'columns':[cols],'components':[{'routeName':'route','methods':methods,'state':{'offlineInformation':{'createdTime':'','pageNum':1,'pageSize':20},'resultTotal':1,'tableData':[{**record,'details':'详情'}]}}]}
        x=run_decode({'status':0,'message':'Success','data':{'list':[record],'total':1,'pageNum':1,'pageSize':20}},c)
        self.assertNotIn('details',x.rows[0]);self.assertEqual(x.semantic_state,'file_listing_only_zero_downloads')


class IdentityAndIsolation(unittest.TestCase):
    def jobs(self):return json.loads((ROOT/'tests/fixtures/legacy_keys_from_node.json').read_text())['jobs']
    def bridge(self,j,period=None):return identity_bridge(j,account_alias='fixture-account',period=period or {'kind':'day','label':'2026-01-01'})

    def test_node_generated_keys_and_all_dimensions_preserved(self):
        for j in self.jobs()[:2]:
            x=self.bridge(j);self.assertEqual(x['identity']['dimensions']['legacy_params'],j['params']);self.assertFalse(x['may_dispatch'])
        self.assertNotEqual(self.bridge(self.jobs()[0])['pmos_key'],self.bridge(self.jobs()[1])['pmos_key'])

    def test_page_size_changes_are_distinct(self):
        j=self.jobs()[0];x=self.bridge(j);j=copy.deepcopy(j);j['params']['size']=21
        with self.assertRaises(ValueError):self.bridge(j)
        j['key']=hashlib.sha256(canonical({'datasetId':j['datasetId'],'kind':j['kind'],'params':j['params']})).hexdigest()
        self.assertNotEqual(x['pmos_key'],self.bridge(j)['pmos_key'])

    def test_consumed_unknown_and_denied_are_never_dispatchable(self):
        for status in ['saved','held','held_request_failed_412','unknown','denied','running','pending']:
            j=copy.deepcopy(self.jobs()[0]);j['status']=status;x=self.bridge(j);self.assertFalse(x['may_dispatch'])

    def test_global_attachment_identity_shared_across_source_and_date(self):
        jobs=self.jobs()[2:];p={'kind':'snapshot','anchor':'legacy-file-id'}
        a,b=[self.bridge(j,p) for j in jobs];self.assertEqual(a['pmos_key'],b['pmos_key'])
        with self.assertRaises(ValueError):self.bridge(jobs[0],{'kind':'snapshot','anchor':'2026-01-01'})

    def test_float_and_large_js_numbers_and_period_guessing_are_rejected(self):
        for value in [1.0,2**54]:
            j=copy.deepcopy(self.jobs()[0]);j['params']['size']=value
            with self.assertRaises(ValueError):self.bridge(j)
        with self.assertRaises(ValueError):self.bridge(self.jobs()[0],{'kind':'day','label':'2026-01-02'})

    def test_inventory_never_promotes_legacy_enabled(self):
        x=inventory({'datasets':[{'id':'fixture','name':'合成','family':'grid','enabled':True}]},'0'*64)
        self.assertEqual(x['online_enabled_entries'],0);self.assertEqual(x['sample_verified_entries'],0)
        self.assertEqual(x['entries'][0]['qualification'],'shape_bridge_only_unverified')

    def test_unchanged_engine_archives_fake_response(self):
        x=fixture_demo();self.assertTrue(x['archive_verified']);self.assertEqual(x['task_status'],'saved');self.assertEqual(x['website_requests'],0)
