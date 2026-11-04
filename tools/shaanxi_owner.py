"""Explicit prepare/run/rollback for the independent PMOS Shaanxi owner.

Prepare is the only command that can gracefully stop the old worker. It
refuses existing protections first. No browser/session/network change exists.
"""
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import argparse,asyncio,hashlib,json,os,subprocess,time
from pmos.faults import Fault
from pmos.shaanxi_lifecycle import prepare_handoff,check_source_quiescent,process_identity,stop_original_worker,service_state
from pmos.shaanxi_runner import Runner
from pmos.shaanxi_transport import OriginalWorkerLock,atomic,load

PMOS_SERVICE='pmos-shaanxi-native.service'


def prepare(args):
    source=args.source_root.resolve()
    # Do not stop a healthy owner merely to discover an already known block.
    if (source/'state/session_block.json').exists():raise Fault('original_session_block_preserved','local')
    worker=process_identity(args.expected_pid)
    if service_state().get('MainPID')!=str(args.expected_pid):raise Fault('original_worker_owner_changed','local')
    stopped=stop_original_worker(worker['pid'],worker)
    try:
        config=prepare_handoff(source,args.runtime_root,args.instance,ROOT,approval=args.approval,operations_budget=args.operations_budget)
    except BaseException:
        # This only starts the original protocol: it observes its original
        # durable command if present and keeps every existing protection.
        subprocess.run(['systemctl','--user','start','shaanxi-queue-worker.service'],check=True)
        raise
    atomic(args.instance/'original_worker_stop.json',{**stopped,'original_start_identity':worker})
    return {'phase':'handoff_prepared_no_dispatch','generation':config['generation'],
            'config':str(args.instance/'handoff.json'),'snapshot_jobs':config['snapshot_jobs'],'code_sha':config['code_sha']}


def rollback(args):
    config=load(args.config)
    if not config or config.get('province')!='shaanxi':raise Fault('shaanxi_handoff_required','local')
    instance=Path(config['instance']);source=Path(config['source_root'])
    stopped=subprocess.run(['systemctl','--user','stop',PMOS_SERVICE],check=True,timeout=620)
    lock=OriginalWorkerLock(source);lock.acquire()
    try:
        check_source_quiescent(source)
        if config.get('direct',{}).get('mode')=='sqlite_native':
            from pmos.shaanxi_direct_rollback import export_direct
            export_direct(config)
        for p in (instance/'native_commands').glob('[0-9]*.json'):
            if '.' in p.stem:continue
            record=load(p)
            if not (source/'state/browser_session/results'/(record['command_id']+'.json')).is_file():
                raise Fault('pmos_original_command_still_unknown','local')
        for key,ref in [('queue','collection_queue/control.json'),('keeper','keeper/control.json')]:
            if load(source/'state'/ref)!=config['original_controls'][key]:
                raise Fault('human_control_changed_since_handoff','local')
        raw=(source/'state/collection_queue/journal.json').read_bytes()
        atomic(instance/'rollback.json',{'phase':'pmos_stopped_original_facts_preserved',
            'generation':config['generation'],'at':time.time(),'current_journal_sha256':hashlib.sha256(raw).hexdigest(),
            'original_snapshot_restored':False,'unknown_requests_replayed':False})
    finally:lock.close()
    subprocess.run(['systemctl','--user','start','shaanxi-queue-worker.service'],check=True)
    return {'phase':'original_worker_restored','state':service_state(),'original_snapshot_restored':False}


def main():
    ap=argparse.ArgumentParser();sub=ap.add_subparsers(dest='action',required=True)
    p=sub.add_parser('prepare');p.add_argument('--source-root',type=Path,required=True);p.add_argument('--runtime-root',type=Path,required=True);p.add_argument('--instance',type=Path,required=True);p.add_argument('--expected-pid',type=int,required=True);p.add_argument('--approval',required=True);p.add_argument('--operations-budget',type=int,default=5)
    p=sub.add_parser('run');p.add_argument('--config',type=Path,required=True)
    p=sub.add_parser('rollback');p.add_argument('--config',type=Path,required=True)
    args=ap.parse_args()
    try:
        if args.action=='prepare':result=prepare(args)
        elif args.action=='rollback':result=rollback(args)
        else:
            config=load(args.config)
            if not isinstance(config,dict):raise Fault('shaanxi_handoff_required','local')
            if config.get('direct',{}).get('mode')=='sqlite_native':
                from pmos.shaanxi_direct_runner import DirectRunner
                asyncio.run(DirectRunner(config,ROOT).run())
            else:asyncio.run(Runner(config,ROOT).run())
            result={'phase':'pmos_worker_exited'}
        print(json.dumps(result,ensure_ascii=False))
    except Fault as fault:
        print(json.dumps({'phase':'protected_no_bypass','reason':fault.code},ensure_ascii=False));raise SystemExit(2)

if __name__=='__main__':main()
