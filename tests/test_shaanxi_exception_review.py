import copy,hashlib,json,tempfile,unittest
from pathlib import Path
from pmos.shaanxi_exception_review import completed_receipt,BROWSE_HISTORY
from pmos.contracts import canonical
from pmos.faults import Fault
from pmos.ledger import Ledger
from pmos.shaanxi_direct_store import DirectStore


class CompletedOriginalTests(unittest.TestCase):
    def receipt(self):return {'phase':'held','responses':[{'kind':'native_sdk_auth','status':200,'businessStatus':0},{'kind':'unreviewed','path':BROWSE_HISTORY,'status':200,'businessStatus':0},{'kind':'data','path':'/fixture','status':200,'responseFile':'original','sha256':'fixture'}],'pending':[],'failures':[]}
    def test_only_reviewed_auxiliary_changes_and_original_stays_immutable(self):
        original=self.receipt();before=copy.deepcopy(original);derived=completed_receipt(original)
        self.assertEqual(original,before);self.assertEqual(derived['responses'][-1],original['responses'][-1]);self.assertEqual(derived['responses'][1]['kind'],'native_auxiliary');self.assertEqual(derived['adapterReview']['websiteRequestsIssued'],0)
    def test_pending_failed_or_foreign_response_cannot_be_admitted(self):
        for key in ['pending','failures','blockingFailures','foreignFrameRequests']:
            r=self.receipt();r[key]=[{'path':'/fixture'}]
            with self.assertRaises(ValueError):completed_receipt(r)
    def test_unknown_endpoint_or_auxiliary_error_never_exempted(self):
        for change in [{'path':'/other'},{'status':403},{'businessStatus':401}]:
            r=self.receipt();r['responses'][1].update(change)
            with self.assertRaises(ValueError):completed_receipt(r)
    def test_success_business_cannot_mask_auth_denial(self):
        r=self.receipt();r['responses'][0]['businessStatus']=401
        with self.assertRaises(ValueError):completed_receipt(r)
    def test_verified_open_page_can_be_revalidated_without_another_request(self):
        r=self.receipt();r['phase']='captured_requires_adapter_validation';self.assertEqual(completed_receipt(r),r)


class ExplicitUnsentTests(unittest.TestCase):
    def test_only_matching_review_and_unchanged_zero_request_receipt_is_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);ledger=Ledger(p/'ledger.sqlite3');self.addCleanup(ledger.close);s=DirectStore(ledger,'test');d={'id':'source','family':'grid','enabled':True}
            s.import_frozen({'jobs':{}},{'province':'shaanxi','collectorTarget':'data','keeperTarget':'keeper','datasets':[d]},sha256='a'*64,source_ref='fixture')
            key=s.seed('source','table',{'date':'2026-01-01','page':1,'size':50});job=s.job(key);job['manualReview']='explicit'
            with self.assertRaises(Fault):s.reviewed_identity(job)
            original=p/'receipt.json';original.write_text(json.dumps({'invokeError':True,'phase':'held','observedRequests':[],'responses':[],'pending':[],'failures':[]}))
            proof={'kind':'menu_label_renamed_before_native_handler','repaired_source_sha256':hashlib.sha256(canonical(d)).hexdigest(),'original_receipt':str(original),'original_receipt_sha256':hashlib.sha256(original.read_bytes()).hexdigest()}
            s.db.execute('INSERT INTO sn_exception_reviews VALUES(?,?,?,?,?)',('explicit',key,'user requested review',json.dumps(proof),1))
            self.assertEqual(s.reviewed_identity(job),'explicit')
            original.write_text(json.dumps({'invokeError':True,'observedRequests':[{'path':'/fixture'}]}))
            with self.assertRaises(Fault):s.reviewed_identity(job)


if __name__=='__main__':unittest.main()
