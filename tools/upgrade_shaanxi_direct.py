"""Code-pinned upgrade while the direct owner is stopped, with optional zero-send review."""
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import argparse,json,re,subprocess,time
from pmos.ledger import Ledger
from pmos.shaanxi_transport import load,atomic,OriginalWorkerLock
from pmos.shaanxi_direct_store import DirectStore
from pmos.shaanxi_lifecycle import validate_handoff,code_fingerprint


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--config',type=Path,required=True);ap.add_argument('--previous-release',type=Path,required=True);ap.add_argument('--approval',required=True);ap.add_argument('--review-zero-send',action='store_true');a=ap.parse_args()
    if not a.approval.strip():raise ValueError('authorization_required')
    config=load(a.config);source,instance=validate_handoff(config,a.previous_release)
    assert config.get('direct',{}).get('mode')=='sqlite_native'
    assert subprocess.check_output(['systemctl','--user','show','pmos-shaanxi-native.service','-p','MainPID','--value'],text=True).strip()=='0'
    lock=OriginalWorkerLock(source);lock.acquire();db=Ledger(instance/'ledger.sqlite3');store=DirectStore(db,config['account_alias'])
    try:
        assert not db.unsettled()
        proof=store.setting('migration_proof');review=[]
        if a.review_zero_send:
            assert db.db.execute('SELECT count(*) FROM sn_commands').fetchone()[0]==0
            assert not list((instance/'native_turns').glob('*/issued.json'))
            frozen=load(config['direct']['snapshot_ref'])
            review=[dict(r) for r in db.db.execute('SELECT * FROM attempts WHERE id>? AND outcome!=?',(proof['attempts_preserved'],'not_sent'))]
            assert all(r['period'] in frozen['jobs'] and frozen['jobs'][r['period']]['status']=='pending' for r in review)
            audit={'attempts_before_review':review,'website_requests_issued':0,'basis':'Every browser website action requires sn_commands and issued.json before Runtime invocation; both are absent. Broker failed in readonly inspection.','at':time.time()}
            audit_path=instance/'direct_migration'/('zero_send_review_'+ROOT.name+'.json');atomic(audit_path,audit,exclusive=True)
        old=config['code_sha'];new=code_fingerprint(ROOT);receipt=db.handoff()
        current=db.db.execute('SELECT new_code_sha FROM native_release_upgrades ORDER BY id DESC LIMIT 1').fetchone()[0]
        assert current==old and new!=old
        with store.transaction():
            for row in review:
                db.db.execute('UPDATE attempts SET outcome=?,reason=? WHERE id=?',('not_sent','reviewed_zero_send_before_any_direct_preparation',row['id']))
                db.db.execute('DELETE FROM file_tombstones WHERE source_ref=?',('attempt:'+str(row['id']),))
                job=frozen['jobs'][row['period']];store.update(job,consumed=False)
                db.db.execute('DELETE FROM sn_file_claims WHERE job_key=? AND state=?',(job['key'],'issued'))
                db.db.execute('UPDATE tasks SET status=?,reason=?,updated_at=? WHERE dataset=? AND period=?',('pending','reviewed_zero_send_before_any_direct_preparation',time.time(),row['dataset'],row['period']))
            db.db.execute('INSERT INTO native_release_upgrades(previous_code_sha,new_code_sha,handoff_receipt_sha,approval,at) VALUES(?,?,?,?,?)',(old,new,receipt['receipt_sha256'],a.approval,time.time()))
        atomic(instance/'direct_migration'/('upgrade_'+ROOT.name+'.json'),{'previous_code_sha':old,'new_code_sha':new,'approval':a.approval,'zero_send_reviewed':len(review),'at':time.time()},exclusive=True)
        config['code_sha']=new;atomic(a.config,config)
        directory=Path.home()/'.config/systemd/user'
        (directory/'pmos-shaanxi-native.service').write_text((ROOT/'deploy/pmos-shaanxi-native.service').read_text().replace('releases/RELEASE','releases/'+ROOT.name))
        report=directory/'shaanxi-collection-report-send.service.d/40-pmos-owner.conf'
        report.write_text(re.sub(r'releases/[^/]+/tools/pmos_status_sender.py','releases/'+ROOT.name+'/tools/pmos_status_sender.py',report.read_text()))
        print(json.dumps({'code_sha':new,'zero_send_reviewed':len(review),'phase':'direct_upgrade_prepared'}))
    finally:db.close();lock.close()
    subprocess.run(['systemctl','--user','daemon-reload'],check=True)
    subprocess.run(['systemctl','--user','reset-failed','pmos-shaanxi-native.service'],check=False,capture_output=True)
    subprocess.run(['systemctl','--user','start','pmos-shaanxi-native.service'],check=True)

if __name__=='__main__':main()
