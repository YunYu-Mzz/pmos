import hashlib,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from pmos.ledger import Ledger
from pmos.shaanxi_direct_store import DirectStore
from pmos.shaanxi_direct_pdf import parse_one,DeferredPDFs
from pmos.shaanxi_transport import atomic,load


class OfflinePDFTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.db=Ledger(self.root/'ledger.sqlite3');self.addCleanup(self.db.close);self.store=DirectStore(self.db,'test')
        self.store.import_frozen({'jobs':{}},{'province':'shaanxi','collectorTarget':'data','keeperTarget':'keeper','datasets':[{'id':'pdf','family':'directory','enabled':True}]},sha256='a'*64,source_ref='fixture')
        self.key=self.store.seed('pdf','download',{'fileId':'f','date':'20260101'});self.raw=self.root/'original.pdf';self.raw.write_bytes(b'%PDF-1.7\nfixture\n%%EOF')
        self.sha=hashlib.sha256(self.raw.read_bytes()).hexdigest();self.validation=self.root/'direct_parsed'/self.key/'final/validated.json';self.validation.parent.mkdir(parents=True)
        self.value={'outcome':'saved','kind':'pdf_original','sha256':self.sha,'responseFile':str(self.raw),'source':{'date':'20260101'},'pdfParse':{'status':'pending'}};atomic(self.validation,self.value)
        self.store.settle(self.store.job(self.key),self.value,str(self.validation),self.sha,str(self.raw),self.raw.stat().st_size)

    def test_offline_export_runs_once_preserves_original_and_pending_is_not_parsed(self):
        self.assertEqual(load(self.validation)['pdfParse']['status'],'pending')
        def exported(v,dest,d):v['settlementParse']={'status':'parsed_tables','classification':{'status':'parsed'}}
        with patch('pmos.adapters.shaanxi_native.queue_validate.export',side_effect=exported) as export:
            self.assertEqual(parse_one(self.root,self.key),0);self.assertEqual(parse_one(self.root,self.key),0);self.assertEqual(export.call_count,1)
        self.assertEqual(load(self.validation)['pdfParse']['status'],'parsed')
        self.assertEqual(hashlib.sha256(self.raw.read_bytes()).hexdigest(),self.sha)

    def test_nested_parser_review_is_not_claimed_parsed(self):
        def exported(v,dest,d):v['settlementParse']={'status':'parsed_tables','classification':{'status':'requires_review'}}
        with patch('pmos.adapters.shaanxi_native.queue_validate.export',side_effect=exported):parse_one(self.root,self.key)
        self.assertEqual(load(self.validation)['pdfParse']['status'],'requires_review')

    def test_no_registered_parser_is_explicit_original_only(self):
        with patch('pmos.adapters.shaanxi_native.queue_validate.export'):parse_one(self.root,self.key)
        self.assertEqual(load(self.validation)['pdfParse']['status'],'original_only')

    def test_changed_raw_is_rejected_without_export(self):
        self.raw.write_bytes(b'changed')
        with patch('pmos.adapters.shaanxi_native.queue_validate.export') as export:
            with self.assertRaises(ValueError):parse_one(self.root,self.key)
            export.assert_not_called()


class ManagerTests(OfflinePDFTests,unittest.IsolatedAsyncioTestCase):
    async def test_background_process_does_not_wait_for_parser_and_crash_is_isolated(self):
        import asyncio
        gate=asyncio.Event()
        class Process:
            returncode=None
            pid=999999999
            async def wait(self):await gate.wait();return self.returncode
        proc=Process();manager=DeferredPDFs(self.store,self.root)
        with patch('pmos.shaanxi_direct_pdf.asyncio.create_subprocess_exec',return_value=proc):await manager.tick()
        self.assertIs(manager.process,proc);self.assertEqual(self.store.db.execute('select status from sn_pdf_jobs').fetchone()[0],'running')
        proc.returncode=1;gate.set();await manager.tick()
        self.assertIsNone(manager.process);self.assertEqual(self.store.db.execute('select status from sn_pdf_jobs').fetchone()[0],'requires_review')
        self.assertEqual(self.store.job(self.key)['status'],'saved')


if __name__=='__main__':unittest.main()
