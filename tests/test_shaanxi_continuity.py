from pathlib import Path
from datetime import datetime,timezone
import hashlib,json,sqlite3,tempfile,unittest
from pmos.shaanxi_continuity import quarantine_terminal_network,terminal_network_receipt
from pmos.shaanxi_transport import atomic

class Continuity(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.base=Path(self.temp.name);self.source=self.base/'source';self.instance=self.base/'pmos';self.s=self.source/'state';self.key='a'*64;self.job='b'*64;self.sid='fixture-source'
        self.db=sqlite3.connect(':memory:');self.db.row_factory=sqlite3.Row;self.addCleanup(self.db.close);self.db.execute('create table attempts(id integer,period text,outcome text)');self.db.execute('insert into attempts values(1,?,?)',(self.job,'failed'));self.ledger=type('Ledger',(),{'db':self.db})()
        self.receipt={'phase':'held','reason':'unknown_request_result','expectedPath':'/fixture/data','identity':{'queueJob':self.job,'action':'query'},'pending':[],'responses':[],
            'failures':[{'requestId':'fixture.1','path':'/px-common-authcenter/sso/token','error':'net::ERR_ABORTED','finishedAt':'2026-01-01T00:00:00Z'}, {'requestId':'fixture.2','path':'/fixture/data','error':'net::ERR_NETWORK_CHANGED','finishedAt':'2026-01-01T00:00:01Z'}]}
        atomic(self.s/'session_block.json',{'reason':'unknown_request_result','key':self.key});atomic(self.s/'native_turns'/self.key/'receipt.json',self.receipt)
        atomic(self.s/'native_turns'/self.key/'issued.json',{'readOnly':True,'expressionSha256':hashlib.sha256(b'fixture').hexdigest()})
        atomic(self.s/'collection_queue/tasks'/self.job/'query.plan.json',{'readOnly':True,'expectedPath':'/fixture/data','identity':self.receipt['identity'],'expression':'fixture'})
        atomic(self.s/'collection_queue/journal.json',{'jobs':{self.job:{'key':self.job,'datasetId':self.sid,'status':'held','params':{'date':'2026-01-01','page':9,'size':50}},'future':{'status':'pending'}},'datasetBlocks':{self.sid:{'reason':'Error: Native request held: unknown_request_result','jobKey':self.job}}})
        atomic(self.instance/'native_commands/1.json',{'job_key':self.job,'command_id':'fixture_command','command':{'op':'queueStep','preferredTaskKey':self.job}})
        atomic(self.s/'browser_session/results/fixture_command.json',{'ok':True,'result':{'job':{'key':self.job,'status':'held'}}})
        atomic(self.s/'keeper/last_success.json',{'passed':True,'fullSampleMatches':True,'finishedAt':datetime.now(timezone.utc).isoformat()})
    def apply(self):return quarantine_terminal_network(self.source,self.instance,self.ledger)
    def test_completed_network_failure_skips_only_original_identity_and_restores_future_source(self):
        before=json.loads((self.s/'collection_queue/journal.json').read_text())['jobs'];self.assertTrue(self.apply());self.assertFalse(self.apply())
        after=json.loads((self.s/'collection_queue/journal.json').read_text());self.assertEqual(before,after['jobs']);self.assertFalse(after['datasetBlocks']);self.assertFalse((self.s/'session_block.json').exists());self.assertTrue((self.instance/'state/exception_reviews'/(self.key+'.json')).exists());self.assertFalse((self.s/'browser_session/inbox').exists())
    def test_auth_or_pending_request_are_never_released(self):
        for change in ({'responses':[{'path':'/fixture/data','status':200,'businessStatus':401}]},{'pending':[{'requestId':'fixture.pending'}]}):
            receipt={**self.receipt,**change};self.assertFalse(terminal_network_receipt(receipt));atomic(self.s/'native_turns'/self.key/'receipt.json',receipt);self.assertFalse(self.apply());self.assertTrue((self.s/'session_block.json').exists())
    def test_missing_healthy_keeper_does_not_infer_login(self):
        atomic(self.s/'keeper/last_success.json',{'passed':False});self.assertFalse(self.apply());self.assertTrue((self.s/'session_block.json').exists())
    def test_unowned_old_request_does_not_gain_automatic_release(self):
        self.db.execute('delete from attempts');self.assertFalse(self.apply());self.assertTrue((self.s/'session_block.json').exists())
