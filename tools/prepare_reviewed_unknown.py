"""One reviewed non-auth exception may be quarantined before owner handoff.

This does not assert that a pending request ended, admit its late body, reopen
its task/source, or change the general native policy. All exact facts and the
user's skip-exception decision remain in a durable review receipt.
"""
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import argparse,hashlib,json,os,time,uuid,re
from datetime import datetime,timezone
from pmos.faults import Fault
from pmos.shaanxi_transport import load,atomic,OriginalWorkerLock
from pmos.shaanxi_lifecycle import process_identity,verify_process,stop_original_worker,prepare_handoff


def inspect(args):
    source=args.source_root.resolve();s=source/'state';block=load(s/'session_block.json',{})
    if (block.get('reason')!='unknown_request_result' or block.get('key')!=args.turn_key
        or block.get('previous') or block.get('later')):raise Fault('reviewed_unknown_block_changed','local')
    receipt=s/'native_turns'/args.turn_key/'receipt.json';raw=receipt.read_bytes();turn=json.loads(raw)
    if hashlib.sha256(raw).hexdigest()!=args.receipt_sha:raise Fault('reviewed_unknown_receipt_changed','local')
    if turn.get('phase')!='held' or turn.get('reason')!='unknown_request_result' or not turn.get('pending'):
        raise Fault('exact_original_unknown_required','local')
    signals={401,403,40302,412,423,429,'401','403','40302','412','423','429'}
    if any(any(x.get(k) in signals for k in ('status','businessStatus','businessCode','errorCode'))
           for x in turn.get('responses',[])+turn.get('failures',[])):
        raise Fault('refusal_must_not_be_quarantined','local')
    j=load(s/'collection_queue/journal.json',{});job=j.get('jobs',{}).get(args.job_key,{})
    if job.get('status')!='held' or j.get('datasetBlocks',{}).get(job.get('datasetId'),{}).get('jobKey')!=args.job_key:
        raise Fault('original_task_and_source_must_stay_held','local')
    if turn.get('identity',{}).get('queueJob')!=args.job_key:raise Fault('original_turn_job_binding_changed','local')
    if (s/'collection_queue/pending_command.json').exists():raise Fault('original_command_pending','local')
    if any(not (s/'browser_session/results'/p.name).is_file() for p in (s/'browser_session/inbox').glob('*.json')):
        raise Fault('original_observer_command_unanswered','local')
    p=load(s/'keeper/last_success.json',{});k=load(s/'keeper/status.json',{})
    try:age=time.time()-datetime.fromisoformat(p['finishedAt'].replace('Z','+00:00')).timestamp()
    except (KeyError,ValueError,TypeError):raise Fault('fresh_keeper_proof_required','local') from None
    if not (0<=age<=600 and p.get('passed') and p.get('fullSampleMatches') and k.get('controlEnabled') and not k.get('pending') and not k.get('requiresReview')):
        raise Fault('fresh_keeper_proof_required','local')
    verify_process(process_identity(p['browserPid']));verify_process(process_identity(k['pid']))
    viewed=load(s/'browser_session/results'/(args.collector_read_command+'.json'),{})
    try:view=json.loads(viewed.get('result','null'))
    except (ValueError,TypeError):view=None
    if viewed.get('ok') is not True or not isinstance(view,dict) or view.get('readyState')!='complete':
        raise Fault('reviewed_collector_read_missing','local')
    return block,turn,job,p


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--source-root',type=Path,required=True);ap.add_argument('--runtime-root',type=Path,required=True);ap.add_argument('--instance',type=Path,required=True)
    ap.add_argument('--expected-pid',type=int,required=True);ap.add_argument('--turn-key',required=True);ap.add_argument('--receipt-sha',required=True);ap.add_argument('--job-key',required=True);ap.add_argument('--collector-read-command',required=True);ap.add_argument('--approval',required=True)
    args=ap.parse_args()
    if not args.approval.strip():raise Fault('specific_skip_and_handoff_approval_required','local')
    if any(not re.fullmatch(r'[a-f0-9]{64}',v) for v in (args.turn_key,args.receipt_sha,args.job_key)):
        raise Fault('exact_original_hashes_required','local')
    if not re.fullmatch(r'[a-zA-Z0-9_-]+',args.collector_read_command):raise Fault('exact_collector_read_required','local')
    args.source_root=args.source_root.resolve();args.instance=args.instance.resolve()
    if args.instance.is_relative_to(args.source_root):raise Fault('independent_instance_required','local')
    if (args.instance/'handoff.json').exists():raise Fault('existing_handoff_preserved','local')
    block,turn,job,proof=inspect(args);worker=process_identity(args.expected_pid)
    stopped=stop_original_worker(args.expected_pid,worker)
    atomic(args.instance/'original_worker_stop.json',{**stopped,'original_start_identity':worker})
    lock=OriginalWorkerLock(args.source_root);lock.acquire();s=args.source_root/'state';lease=s/'turn.lock';owned=False
    try:
        lease.mkdir();owned=True
        atomic(lease/'lease.json',{'owner':'pmos_reviewed_unknown_handoff','pid':os.getpid(),'nonce':uuid.uuid4().hex,'at':datetime.now(timezone.utc).isoformat()})
        block,turn,job,proof=inspect(args)
        review={'version':1,'phase':'reviewed_unknown_quarantined_for_new_owner','at':datetime.now(timezone.utc).isoformat(),
            'approval':args.approval,'original_block':block,'original_turn':args.turn_key,'original_receipt_sha256':args.receipt_sha,
            'original_job_key':args.job_key,'original_source_id':job['datasetId'],'pending_request_ids':[x['requestId'] for x in turn['pending']],
            'original_result':'unknown_not_changed_to_terminal','original_task_and_source_stay_held':True,
            'original_request_replayed':False,'late_bodies_admitted':False,'website_requests_issued':0,
            'keeper_proof':proof,'collector_read_command':args.collector_read_command,'original_worker_stopped':stopped}
        base=args.instance/'reviews'/args.turn_key;atomic(base/'review.json',review,exclusive=True)
        original=s/'session_block.json';raw=original.read_bytes();atomic(base/'original_block.json',json.loads(raw),exclusive=True)
        if json.loads(raw)!=block:raise Fault('original_block_changed_during_review','local')
        history=s/'session_block_history'/('pmos-reviewed-'+args.turn_key+'.json')
        atomic(history,review,exclusive=True)
        original.unlink();fd=os.open(s,os.O_RDONLY)
        try:os.fsync(fd)
        finally:os.close(fd)
    finally:
        if owned:
            (lease/'lease.json').unlink();lease.rmdir()
        lock.close()
    config=prepare_handoff(args.source_root,args.runtime_root,args.instance,ROOT,approval=args.approval,operations_budget=5)
    print(json.dumps({'phase':'reviewed_handoff_prepared_no_dispatch','generation':config['generation'],'snapshot_jobs':config['snapshot_jobs'],'config':str(args.instance/'handoff.json'),'original_job_stays_held':True,'website_requests_issued':0}))

if __name__=='__main__':main()
