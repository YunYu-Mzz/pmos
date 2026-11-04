import copy,hashlib,tempfile,time,unittest
from pathlib import Path
from pmos.contracts import canonical
from pmos.migration.shaanxi import task_identity,verified_key
from pmos.shaanxi_direct_store import DirectStore,encode
from pmos.shaanxi_unissued_review import SOURCE,DOWNLOAD,UnissuedFileLedger,digest,prove_open_only

class FirstDownloadReviewTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);root=Path(self.tmp.name)
  self.db=UnissuedFileLedger(root/'ledger.sqlite3');self.addCleanup(self.db.close);self.store=DirectStore(self.db,'test')
  self.params={'fileId':'file','date':'20260101','fileName':'x.PDF','missionName':'x'}
  oldkey=hashlib.sha256(canonical({'kind':'download','fileId':'file'})).hexdigest()
  self.old={'key':oldkey,'datasetId':SOURCE,'kind':'download','params':self.params,'status':'held_request_failed_412'}
  source={'id':SOURCE,'family':'directory','enabled':True,'openExpression':'native_menu_only'}
  self.store.import_frozen({'jobs':{oldkey:self.old}},{'province':'shaanxi','collectorTarget':'data','keeperTarget':'keeper','datasets':[source]},sha256='a'*64,source_ref='frozen')
  identity=task_identity(self.old,'test');self.identity=identity
  self.db.db.execute('INSERT INTO legacy_records VALUES(?,?,?,?,?,?,?,?,?)',('legacy','a'*64,oldkey,identity.key,canonical(identity.document()).decode(),'held',1,'[]','fact'))
  self.db.db.execute('INSERT INTO file_tombstones VALUES(?,?,?)',(identity.file_key,identity.key,'legacy:a'))
  self.plan={'op':'nativeTurn','discovery':True,'expectedPath':'/__native_menu_discovery__','expression':'native_menu_only','identity':{'queueJob':oldkey,'action':'open','source':SOURCE,'kind':'download','params':self.params,'planRevision':0,'nativeFilters':{}}}
  self.receipt={'identity':self.plan['identity'],'phase':'held','reason':'request_failed_412','responses':[{'kind':'native_document','path':'/menu','status':412}],'pending':[]}
  folder=root/oldkey;folder.mkdir();self.planfile=folder/'open.plan.json';self.receiptfile=folder/'open.receipt.json';self.planfile.write_text(encode(self.plan));self.receiptfile.write_text(encode(self.receipt))
  self.new={**self.old,'status':'pending','unissuedRecovery':{'skippedJob':oldkey}};self.new['key']=hashlib.sha256(canonical({'kind':'first_unissued_download_after_open_412','skippedJob':oldkey,'fileId':'file'})).hexdigest();verified_key(self.new);self.store._insert(self.new)
  self.proof={'skippedJob':oldkey,'original_job_sha256':digest(self.old),'source_sha256':digest(source),'plan_ref':str(self.planfile),'receipt_ref':str(self.receiptfile),'plan_sha256':hashlib.sha256(self.planfile.read_bytes()).hexdigest(),'receipt_sha256':hashlib.sha256(self.receiptfile.read_bytes()).hexdigest(),'legacy_sha256':digest([dict(x) for x in self.db.db.execute('SELECT * FROM legacy_records')]),'legacy_file_sha256':digest([dict(x) for x in self.db.db.execute('SELECT * FROM file_tombstones')])}
 def admit(self):
  self.db.db.execute('INSERT INTO sn_unissued_file_reviews VALUES(?,?,?,?,?,?,?)',('review',self.new['key'],self.identity.key,self.identity.file_key,'explicit user review',encode(self.proof),time.time()))
 def test_old_claim_protected_without_review(self):
  self.assertTrue(self.db.consumed_identity(self.identity.key));self.assertTrue(self.db.consumed_file(self.identity.file_key));self.assertFalse(self.db.reviewed_unissued_file(self.new))
 def test_first_attempt_consumes_identity_and_preserves_import(self):
  self.admit();self.assertFalse(self.db.consumed_identity(self.identity.key));self.assertTrue(self.db.reviewed_unissued_file(self.new));before=[tuple(x) for x in self.db.db.execute('SELECT * FROM file_tombstones')]
  attempt=self.db.begin('collect',dataset=self.identity.dataset,period=self.new['key'],identity=self.identity);self.db.finish(attempt,'unknown')
  self.assertTrue(self.db.consumed_identity(self.identity.key));self.assertTrue(self.db.consumed_file(self.identity.file_key));self.assertEqual(before,[tuple(x) for x in self.db.db.execute('SELECT * FROM file_tombstones')])
  with self.assertRaises(ValueError):self.db.begin('collect',dataset=self.identity.dataset,period=self.new['key'],identity=self.identity)
 def test_changed_receipt_or_extra_download_plan_fails_closed(self):
  self.admit();self.receiptfile.write_text('{}');self.assertTrue(self.db.consumed_identity(self.identity.key));self.receiptfile.write_text(encode(self.receipt));(self.planfile.parent/'download.plan.json').write_text('{}');self.assertTrue(self.db.consumed_identity(self.identity.key))
 def test_business_download_or_auth_is_never_unissued(self):
  for response in [{'kind':'data','path':DOWNLOAD,'status':412},{'kind':'native_sdk_auth','path':'/token','status':401}]:
   receipt=copy.deepcopy(self.receipt);receipt['responses'].append(response);self.assertFalse(prove_open_only(self.plan,receipt,self.old))
  receipt=copy.deepcopy(self.receipt);receipt['observedRequests']=[{'path':DOWNLOAD}];self.assertFalse(prove_open_only(self.plan,receipt,self.old))
 def test_new_foreign_consumed_record_blocks_review(self):
  self.admit();self.db.db.execute("UPDATE legacy_records SET state='saved'");self.assertTrue(self.db.consumed_identity(self.identity.key))
 def test_no_review_for_another_file_or_job(self):
  self.admit();other={**self.new,'params':{**self.params,'fileId':'other'}};self.assertFalse(self.db.reviewed_unissued_file(other))
  with self.assertRaises(ValueError):self.db.begin('collect',dataset=self.identity.dataset,period='another-job',identity=self.identity)
 def test_missing_target_preflight_requires_exact_original_and_no_native_plan(self):
  original={**self.old,'status':'held','error':'Error: No target with given id found'}
  root=Path(self.tmp.name);result=root/'preflight.json';result.write_text(encode({'result':{'job':original}}));directory=root/'empty'/original['key']
  proof={'original_job':original,'result_ref':str(result),'result_sha256':hashlib.sha256(result.read_bytes()).hexdigest(),'original_task_directory':str(directory),'source_sha256':self.proof['source_sha256'],'legacy_sha256':self.proof['legacy_sha256'],'legacy_file_sha256':self.proof['legacy_file_sha256']}
  self.db.db.execute('UPDATE sn_file_claims SET state=?',('held',));self.db.db.execute('INSERT INTO sn_unsent_preflight_reviews VALUES(?,?,?,?,?,?,?)',('preflight',original['key'],self.identity.key,self.identity.file_key,'explicit user review',encode(proof),time.time()))
  self.assertFalse(self.db.consumed_identity(self.identity.key));job={**original,'legacyUnsentReview':'preflight'};self.assertTrue(self.db.reviewed_unissued_file(job))
  directory.mkdir(parents=True);(directory/'open.plan.json').write_text('{}');self.assertTrue(self.db.consumed_identity(self.identity.key))

if __name__=='__main__':unittest.main()
