import hashlib,json,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
from pmos import status_mail
from pmos.ledger import Ledger
from pmos.shaanxi_direct_store import DirectStore
from pmos.shaanxi_direct_reporting import snapshot,EXTRA_SCHEMA
from pmos.shaanxi_direct_rollback import export_direct
from pmos.shaanxi_transport import atomic,load


class DirectReportingTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name);self.source=self.root/'source';self.instance=self.root/'instance'
        self.source.mkdir();self.instance.mkdir();self.db=Ledger(self.instance/'ledger.sqlite3');self.addCleanup(self.db.close);self.store=DirectStore(self.db,'test')
        self.store.import_frozen({'jobs':{}},{'province':'shaanxi','keeperTarget':'keeper','collectorTarget':'data','datasets':[{'id':'grid','family':'grid','enabled':True}]},sha256='a'*64,source_ref='frozen');self.db.db.executescript(EXTRA_SCHEMA)
        self.config={'source_root':str(self.source),'instance':str(self.instance),'account_alias':'test'};atomic(self.instance/'handoff.json',self.config);atomic(self.instance/'state/control.json',{'collect':True});atomic(self.instance/'state/status.json',{'phase':'completed','pid':123})
        self.raw=self.instance/'native_turns/fixture/fixture.response';self.raw.parent.mkdir(parents=True);self.raw.write_bytes(b'{"code":200,"data":[0,-1,null]}');self.digest=hashlib.sha256(self.raw.read_bytes()).hexdigest()
        self.key=self.store.seed('grid','table',{'date':'2026-01-01','page':1,'size':1});self.job=self.store.job(self.key)
        self.validation=self.instance/'direct_parsed'/self.key/'final/validated.json';self.value={'outcome':'saved','sha256':self.digest,'responseFile':str(self.raw),'rows':[{'date':'2026-01-01'}],'keys':['one'],'server_total_rows':1};atomic(self.validation,self.value)
        self.store.settle(self.job,self.value,str(self.validation),self.digest,self.raw,self.raw.stat().st_size)
        self.db.db.execute('INSERT INTO sn_source_artifacts VALUES(?,?)',('grid',self.digest));self.db.db.execute('INSERT INTO sn_artifact_metadata VALUES(?,?,?)',(self.digest,time.time(),0))
        metrics={k:None for k in status_mail.FIELDS};metrics.update(missing_originals=0,ordinary_originals=0)
        atomic(self.instance/'direct_config/report_baseline.json',{'schema':2,'province':'shaanxi','report_id':'baseline','generated_at':'2026-11-02T00:00:00Z','metrics':metrics,'coverage':{'indicators':[{'ids':['grid'],'file_count':None,'bytes':None,'periods':[],'scopes':[],'note':'fixture','queue_status':{}}],'notes':[]}})

    def test_report_twenty_fields_actual_sha_and_current_sql_tasks_without_journal(self):
        self.assertFalse((self.source/'state/collection_queue/journal.json').exists())
        with patch('pmos.shaanxi_direct_reporting.owner_evidence',return_value={'owned_live':True}):report=snapshot(self.instance,status_mail)
        self.assertEqual(set(report['metrics']),set(status_mail.FIELDS));self.assertEqual(report['metrics']['ordinary_originals'],1);self.assertEqual(report['metrics']['collector_state'],'active');self.assertFalse(report['metrics']['collection_paused'])
        self.assertEqual(report['coverage']['indicators'][0]['queue_status'],{'saved':1});self.assertEqual(report['coverage']['indicators'][0]['file_count'],1)

    def test_changed_new_raw_is_missing_not_counted_as_successful_original(self):
        self.raw.write_bytes(b'changed')
        with patch('pmos.shaanxi_direct_reporting.owner_evidence',return_value={'owned_live':True}):report=snapshot(self.instance,status_mail)
        self.assertEqual(report['metrics']['ordinary_originals'],0);self.assertEqual(report['metrics']['missing_originals'],1)

    def prepare_rollback(self):
        journal=self.source/'state/collection_queue/journal.json';atomic(journal,{'jobs':{}});self.store.set_setting('migration_proof',{'source_journal':{'sha256':hashlib.sha256(journal.read_bytes()).hexdigest()}});return journal

    def test_reverse_export_preserves_new_task_and_raw_hash_instead_of_old_snapshot(self):
        journal=self.prepare_rollback();export_direct(self.config);out=load(journal);job=out['jobs'][self.key];self.assertEqual(job['status'],'saved');validation=load(job['validation']);raw=Path(validation['responseFile']);self.assertEqual(hashlib.sha256(raw.read_bytes()).hexdigest(),self.digest);self.assertTrue(raw.is_relative_to(self.source/'state/pmos_direct_export'));self.assertEqual(len(out['jobs']),1)

    def test_unknown_target_prevents_unsafe_legacy_restore(self):
        journal=self.prepare_rollback();before=journal.read_bytes();self.store.hold('target','data','',{'reason':'unknown'})
        with self.assertRaisesRegex(ValueError,'unknown_direct'):export_direct(self.config)
        self.assertEqual(journal.read_bytes(),before)

if __name__=='__main__':unittest.main()
