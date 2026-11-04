"""Upgrade this PMOS owner without resetting any attempts or original facts."""
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import argparse,hashlib,json,subprocess,time
from pmos.faults import Fault
from pmos.ledger import Ledger
from pmos.shaanxi_transport import load,atomic,OriginalWorkerLock
from pmos.shaanxi_lifecycle import validate_handoff,code_fingerprint,verify_process,process_identity


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--config',type=Path,required=True);ap.add_argument('--previous-release',type=Path,required=True);ap.add_argument('--expected-pid',type=int,required=True);ap.add_argument('--approval',required=True);a=ap.parse_args()
    if not a.approval.strip():raise Fault('code_upgrade_approval_required','local')
    old_bytes=a.config.read_bytes();config=json.loads(old_bytes);source,instance=validate_handoff(config,a.previous_release)
    owner=load(instance/'state/native_owner.json',{});verify_process(owner['start_identity'])
    if owner.get('pid')!=a.expected_pid:raise Fault('pmos_owner_changed','local')
    current=subprocess.check_output(['systemctl','--user','show','pmos-shaanxi-native.service','-p','MainPID','--value'],text=True).strip()
    if current!=str(a.expected_pid):raise Fault('pmos_service_owner_changed','local')
    subprocess.run(['systemctl','--user','stop','pmos-shaanxi-native.service'],check=True,timeout=620)
    lock=OriginalWorkerLock(source);lock.acquire()
    try:
        if (source/'state/turn.lock').exists() or (source/'state/collection_queue/pending_command.json').exists():raise Fault('native_turn_pending','local')
        if any(not (source/'state/browser_session/results'/p.name).is_file() for p in (source/'state/browser_session/inbox').glob('*.json')):raise Fault('native_command_pending','local')
        db=Ledger(instance/'ledger.sqlite3')
        try:
            if db.unsettled():raise Fault('pmos_attempt_unsettled','local')
            receipt=db.handoff();new_sha=code_fingerprint(ROOT)
            if new_sha==config['code_sha']:raise Fault('code_upgrade_has_no_change','local')
            db.db.execute('CREATE TABLE IF NOT EXISTS native_release_upgrades(id INTEGER PRIMARY KEY,previous_code_sha TEXT NOT NULL,new_code_sha TEXT NOT NULL,handoff_receipt_sha TEXT NOT NULL,approval TEXT NOT NULL,at REAL NOT NULL)')
            prior=db.db.execute('SELECT new_code_sha FROM native_release_upgrades ORDER BY id DESC LIMIT 1').fetchone();expected=prior[0] if prior else receipt['code_sha']
            if expected!=config['code_sha']:raise Fault('previous_code_upgrade_requires_recovery','local')
            proof={'previous_release':str(a.previous_release),'new_release':str(ROOT),'previous_code_sha':config['code_sha'],'new_code_sha':new_sha,'original_handoff_sha':receipt['receipt_sha256'],'prior_config_sha256':hashlib.sha256(old_bytes).hexdigest(),'approval':a.approval,'at':time.time(),'generation_preserved':config['generation'],'attempts_preserved':db.db.execute('select count(*) from attempts').fetchone()[0],'legacy_facts_preserved':db.db.execute('select count(*) from legacy_records').fetchone()[0]}
            directory=instance/'code_upgrades'/new_sha;atomic(directory/'before_config.json',config,exclusive=True);atomic(directory/'approval.json',proof,exclusive=True)
            db.db.execute('INSERT INTO native_release_upgrades(previous_code_sha,new_code_sha,handoff_receipt_sha,approval,at) VALUES(?,?,?,?,?)',(config['code_sha'],new_sha,receipt['receipt_sha256'],a.approval,proof['at']))
            config={**config,'code_sha':new_sha,'phase':'approved_code_upgrade_no_dispatch','latest_upgrade':str(directory/'approval.json')};atomic(a.config,config)
        finally:db.close()
    finally:lock.close()
    print(json.dumps({'phase':'pmos_code_upgrade_prepared_no_dispatch','new_release':str(ROOT),'code_sha':new_sha,'original_attempts_preserved':proof['attempts_preserved'],'legacy_facts_preserved':proof['legacy_facts_preserved']}))

if __name__=='__main__':main()
