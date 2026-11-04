import hashlib,json,tempfile,unittest
from pathlib import Path
from pmos.ledger import Ledger
from pmos.shaanxi_direct_store import DirectStore,key_for,group_for


class DirectFrontierTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.ledger=Ledger(Path(self.tmp.name)/'ledger.sqlite3');self.addCleanup(self.ledger.close)
        self.store=DirectStore(self.ledger,'test');self.sources=[{'id':'grid','family':'grid','enabled':True},{'id':'directory','family':'directory','enabled':True},{'id':'preview','family':'preview','enabled':True}]
        self.registry={'province':'shaanxi','collectorTarget':'data','keeperTarget':'keeper','datasets':self.sources}
        self.store.import_frozen({'jobs':{},'lastSeedDay':'2026-11-02'},self.registry,sha256='a'*64,source_ref='frozen')

    def job(self,source='grid',kind='table',**params):
        p={'date':'2026-01-01','page':1,'size':2,**params};key=self.store.seed(source,kind,p)
        return self.store.job(key)

    def result(self,keys,total=4):return {'outcome':'saved','rows':[{'id':x} for x in keys],'saved_rows':len(keys),'keys':keys,'server_total_rows':total}

    def settle(self,j,keys,total=4):return self.store.settle(j,self.result(keys,total),'v','b'*64,'/raw',30)

    def test_pages_increment_and_atomic_total_change_hold_only_date(self):
        j=self.job();self.assertTrue(self.settle(j,['a','b']))
        child=self.store.job(key_for('grid','table',{**j['params'],'page':2}));self.assertEqual(child['status'],'pending')
        self.assertFalse(self.settle(child,['c','d'],6));self.assertEqual(self.store.job(child['key'])['status'],'held_direct_exception')
        self.assertEqual(self.store.db.execute('select count(*) from sn_page_keys').fetchone()[0],2)
        self.assertEqual(self.store.db.execute('select scope from sn_holds').fetchone()[0],'date')

    def test_cross_page_duplicate_not_confirmed_or_recounted(self):
        j=self.job();self.settle(j,['a','b']);child=self.store.job(key_for('grid','table',{**j['params'],'page':2}))
        self.assertFalse(self.settle(child,['b','c']));self.assertEqual(self.store.db.execute('select count(*) from sn_job_artifacts').fetchone()[0],1)

    def test_idempotent_parent_settle_does_not_duplicate_child(self):
        j=self.job();self.settle(j,['a','b']);self.settle(j,['a','b'])
        self.assertEqual(self.store.db.execute('select count(*) from sn_jobs').fetchone()[0],2)

    def test_source_batch_dates_continue_then_yield_after_64(self):
        first=self.job(date='2026-01-01');self.settle(first,['a'],1)
        second=self.job(date='2026-01-02');third=self.job(date='2026-01-03')
        other=self.job('preview',date='2026-01-01')
        self.assertEqual(self.store.next_job(today='2026-11-02')['key'],second['key'])
        self.settle(second,['b'],1)
        self.assertEqual(self.store.next_job(today='2026-11-02')['key'],third['key'])
        batch=self.store.setting('source_batch');batch['count']=64;self.store.set_setting('source_batch',batch)
        self.assertEqual(self.store.next_job(today='2026-11-02')['key'],other['key'])

    def test_batch_never_bypasses_date_hold_and_keeps_snapshot_fairness(self):
        first=self.job();self.settle(first,['a'],1)
        blocked=self.job(date='2026-01-02');other=self.job('preview')
        self.store.hold('date','grid','2026-01-02',{'reason':'preserved'})
        self.assertEqual(self.store.next_job(today='2026-11-02')['key'],other['key'])
        snapshot=self.job('directory','catalogue',snapshotDate='2026-11-02')
        self.store.set_setting('completed_since_snapshot',64)
        self.assertEqual(self.store.next_job(today='2026-11-02')['key'],snapshot['key'])
        self.assertEqual(self.store.job(blocked['key'])['status'],'pending')

    def test_pdf_pending_enqueued_with_confirmation_once(self):
        job=self.job('directory','download',fileId='pdf')
        value={'outcome':'saved','kind':'pdf_original','keys':[],'pdfParse':{'status':'pending'}}
        for _ in range(2):self.store.settle(job,value,'v','b'*64,'/raw',30)
        self.assertEqual(self.store.db.execute('SELECT status,count(*) FROM sn_pdf_jobs').fetchone()[0],'pending')
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM sn_pdf_jobs').fetchone()[0],1)

    def test_attachment_batch_yields_after_eight_large_files(self):
        table=self.job();download=self.job('directory','download',fileId='f')
        self.store.set_setting('source_batch',{'source_id':'directory','kind':'download','count':8,'at':__import__('time').time()})
        self.assertEqual(self.store.next_job(today='2026-11-02')['key'],table['key'])

    def test_download_identity_is_global_and_consumed_failure_preserved(self):
        j=self.job('directory','download',fileId='file1');self.store.quarantine(j,'failed')
        self.store.db.execute('INSERT INTO sn_file_claims VALUES(?,?,?,?)',('file1',j['key'],'failed','{}'))
        self.assertEqual(self.store.seed('grid','download',j['params']),j['key'])
        self.assertIsNone(self.store.next_job(today='2026-11-02'))

    def test_stale_snapshot_is_held_without_relabel_or_request(self):
        j=self.job('directory','catalogue',snapshotDate='2026-11-01')
        self.assertIsNone(self.store.next_job(today='2026-11-02'))
        got=self.store.job(j['key']);self.assertEqual(got['params']['snapshotDate'],'2026-11-01');self.assertEqual(got['status'],'held_direct_exception')

    def test_classification_expansion_local_and_all_pages_related(self):
        j=self.job('preview');parent=self.store.expand(j,['price','quantity'])
        self.assertEqual(len(parent['children']),2);self.assertEqual(self.ledger.db.execute('select count(*) from attempts').fetchone()[0],0)
        price=self.store.job(parent['children'][0]);self.settle(price,['a','b'])
        self.assertEqual(self.store.db.execute('select count(*) from sn_relations where parent=?',(j['key'],)).fetchone()[0],3)
        self.assertEqual(self.store.job(j['key'])['status'],'expanded')

    def test_directory_count_integer_and_file_children(self):
        j=self.job('directory','catalogue',snapshotDate='2026-11-02')
        self.store.settle(j,{'outcome':'saved','rows':1,'keys':[],'files':[{'fileId':'f1','fileName':'a.pdf','missionName':'a','date':'20260101'}]},'v','c'*64,'/raw',20)
        self.assertEqual(self.store.job(j['key'])['rows'],1);self.assertEqual(self.store.db.execute("select count(*) from sn_jobs where kind='download'").fetchone()[0],1)

    def test_target_hold_allows_other_target(self):
        self.job();second=self.job('preview');self.store.db.execute("update sn_sources set target_id='other' where id='preview'")
        self.store.hold('target','data','',{'reason':'unknown'})
        self.assertEqual(self.store.next_job(today='2026-11-02')['key'],second['key'])

    def test_daily_catchup_is_idempotent_and_not_full_json(self):
        self.store.seed_daily('2026-11-04');count=self.store.db.execute('select count(*) from sn_jobs').fetchone()[0]
        self.assertEqual(count,5);self.store.seed_daily('2026-11-04');self.assertEqual(self.store.db.execute('select count(*) from sn_jobs').fetchone()[0],count)
        self.assertEqual(self.store.setting('last_seed_day'),'2026-11-04')

    def test_import_preserves_unknown_body_and_disallows_another_import(self):
        j=self.job();self.store.quarantine(j,'unknown')
        with self.assertRaises(Exception):self.store.import_frozen({'jobs':{}},self.registry,sha256='b'*64,source_ref='new')
        self.assertEqual(self.store.job(j['key'])['error'],'unknown')

    def unit_job(self):
        sid='1643899919922237440';body={'id':sid,'family':'grid','enabled':True}
        self.store.db.execute('INSERT INTO sn_sources VALUES(?,?,?,?,?,?)',(sid,json.dumps(body),'fixture','grid',1,'data'))
        return self.job(sid)

    def test_unit_dimension_types_match_original_string_only_rule(self):
        job=self.unit_job()
        for field in ('MEAS_TYPE','UNIT_ID'):
            for invalid in (1,True,{},[],None,''):
                row={'date':job['params']['date'],'MEAS_TYPE':'1','UNIT_ID':'0',field:invalid}
                result={'rows':[row],'keys':['raw'],'server_total_rows':1}
                self.assertEqual(self.store.precheck_page(job,result)['scope'],'date')

    def test_unit_string_zero_identity_is_preserved_not_coerced(self):
        job=self.unit_job();row={'date':job['params']['date'],'MEAS_TYPE':'1','UNIT_ID':'0'}
        result={'outcome':'saved','rows':[row],'keys':['raw'],'server_total_rows':1}
        self.assertTrue(self.store.settle(job,result,'v','d'*64,'/raw',30))
        self.assertEqual(json.loads(self.store.db.execute('SELECT natural_key FROM sn_unit_keys').fetchone()[0]),[job['params']['date'],'1','0'])

if __name__=='__main__':unittest.main()
