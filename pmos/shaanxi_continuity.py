"""Quarantine a completed, non-auth native network failure; never replay it.

Only the current PMOS attempt's exact receipt and original result qualify.
In-flight/unknown transport, refusals, changed code and other owners do not.
"""
from pathlib import Path
from datetime import datetime,timezone
import hashlib,json,os,re,time,uuid
from .shaanxi_transport import load,atomic

NETWORK=re.compile(r'^net::ERR_(?:NETWORK_CHANGED|INTERNET_DISCONNECTED|CONNECTION_[A-Z_]+|TIMED_OUT|NAME_NOT_RESOLVED|SOCKS_CONNECTION_FAILED|PROXY_CONNECTION_FAILED|TUNNEL_CONNECTION_FAILED)$')
SDK='/px-common-authcenter/sso/token'
REFUSALS={401,403,40302,412,423,429,'401','403','40302','412','423','429'}


def terminal_network_receipt(receipt):
    if receipt.get('phase')!='held' or receipt.get('reason')!='unknown_request_result' or receipt.get('pending'):
        return False
    expected=receipt.get('expectedPath');failures=receipt.get('failures',[])
    if not expected or not failures:return False
    primary=False
    for item in receipt.get('responses',[]):
        if item.get('path') not in {expected,SDK}:return False
        if any(item.get(field) in REFUSALS for field in ('status','businessStatus','businessCode','errorCode')):return False
        if item.get('status')!=200:return False
    for item in failures:
        if not item.get('finishedAt'):return False
        error=item.get('error','')
        if item.get('path')==SDK and error=='net::ERR_ABORTED':continue
        if item.get('path') not in {expected,SDK} or not NETWORK.fullmatch(error):return False
        primary|=item.get('path')==expected
    return primary


def legacy_atomic(path,value):
    # Preserve native JS parameter insertion order. Reordering an old params
    # object is not a task migration and must not change its historical key.
    p=Path(path);tmp=p.with_name('.'+p.name+'.'+uuid.uuid4().hex+'.tmp');raw=json.dumps(value,ensure_ascii=False,separators=(',',':')).encode()
    fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    try:
        with os.fdopen(fd,'wb') as f:f.write(raw);f.flush();os.fsync(f.fileno())
        os.replace(tmp,p);d=os.open(p.parent,os.O_RDONLY)
        try:os.fsync(d)
        finally:os.close(d)
    finally:
        if tmp.exists():tmp.unlink()


def quarantine_terminal_network(source,instance,ledger,*,clock=time.time):
    """Caller owns the original worker kernel lock. This sends no command."""
    source=Path(source);instance=Path(instance);s=source/'state';block=load(s/'session_block.json',{})
    key=block.get('key','')
    if block.get('reason')!='unknown_request_result' or not re.fullmatch('[a-f0-9]{64}',key) or block.get('previous') or block.get('later'):
        return False
    receipt_path=s/'native_turns'/key/'receipt.json';receipt=load(receipt_path,{})
    if not terminal_network_receipt(receipt):return False
    jobkey=receipt.get('identity',{}).get('queueJob')
    if not isinstance(jobkey,str) or not re.fullmatch('[a-f0-9]{64}',jobkey):return False
    attempt=ledger.db.execute("SELECT id FROM attempts WHERE period=? AND outcome IN ('failed','unknown') ORDER BY id DESC LIMIT 1",(jobkey,)).fetchone()
    if attempt is None:return False
    prepared=load(instance/'native_commands'/(str(attempt['id'])+'.json'),{})
    if prepared.get('job_key')!=jobkey or prepared.get('command')!={'op':'queueStep','preferredTaskKey':jobkey}:return False
    result_path=s/'browser_session/results'/(prepared['command_id']+'.json');result=load(result_path,{})
    job=result.get('result',{}).get('job',{})
    if result.get('ok') is not True or job.get('key')!=jobkey or job.get('status')!='held':return False
    action=receipt.get('identity',{}).get('action')
    if not isinstance(action,str) or not re.fullmatch(r'(?:query|open|download)(?:_r\d+)?',action):return False
    plan_path=s/'collection_queue/tasks'/jobkey/(action+'.plan.json');plan=load(plan_path,{})
    if plan.get('readOnly') is not True or plan.get('identity')!=receipt.get('identity') or plan.get('expectedPath')!=receipt.get('expectedPath'):return False
    issued=load(receipt_path.with_name('issued.json'),{})
    if issued.get('readOnly') is not True or issued.get('expressionSha256')!=hashlib.sha256(plan.get('expression','').encode()).hexdigest():return False
    proof=load(s/'keeper/last_success.json',{})
    try:age=clock()-datetime.fromisoformat(proof['finishedAt'].replace('Z','+00:00')).timestamp()
    except (ValueError,KeyError,TypeError):return False
    if not (0<=age<=600 and proof.get('passed') and proof.get('fullSampleMatches')):return False
    lease=s/'turn.lock'
    try:lease.mkdir()
    except FileExistsError:return False
    try:
        atomic(lease/'lease.json',{'owner':'pmos_terminal_network_isolation','pid':os.getpid(),'nonce':uuid.uuid4().hex,'at':datetime.now(timezone.utc).isoformat()})
        if load(s/'session_block.json')!=block or (s/'collection_queue/pending_command.json').exists():return False
        if any(not (s/'browser_session/results'/p.name).is_file() for p in (s/'browser_session/inbox').glob('*.json')):return False
        journal_path=s/'collection_queue/journal.json';journal=load(journal_path,{});original=journal.get('jobs',{}).get(jobkey,{})
        sid=original.get('datasetId');source_block=journal.get('datasetBlocks',{}).get(sid,{})
        if original.get('status')!='held' or source_block.get('jobKey')!=jobkey or 'unknown_request_result' not in source_block.get('reason',''):return False
        review={'version':1,'phase':'completed_network_failure_isolated','at':datetime.now(timezone.utc).isoformat(),
            'approval':'user_instruction_non_auth_exception_skip_current_task_no_replay','block':block,'receipt_sha256':hashlib.sha256(receipt_path.read_bytes()).hexdigest(),
            'job_key':jobkey,'source_id':sid,'original_source_block':source_block,'pmos_attempt':attempt['id'],
            'original_result_sha256':hashlib.sha256(result_path.read_bytes()).hexdigest(),'request_ids':[x['requestId'] for x in receipt['failures']],
            'all_original_requests_terminal':True,'original_task_stays_held':True,'original_attempt_replayed':False,'website_requests_issued':0,'keeper_verified_at':proof['finishedAt']}
        review_path=instance/'state/exception_reviews'/(key+'.json');previous=load(review_path)
        if previous is not None and previous.get('receipt_sha256')!=review['receipt_sha256']:return False
        if previous is not None:review=previous
        else:atomic(review_path,review,exclusive=True)
        history=s/'session_block_history'/('pmos-terminal-network-'+key+'.json')
        previous=load(history)
        if previous is not None and previous!=review:return False
        if previous is None:atomic(history,review,exclusive=True)
        # If interrupted after unlink, the old source stays held (safe); the
        # province can continue. Never clear a different/later auth block.
        if load(s/'session_block.json')!=block:return False
        (s/'session_block.json').unlink();fd=os.open(s,os.O_RDONLY)
        try:os.fsync(fd)
        finally:os.close(fd)
        del journal['datasetBlocks'][sid];journal['updatedAt']=datetime.now(timezone.utc).isoformat();legacy_atomic(journal_path,journal)
        return True
    finally:
        (lease/'lease.json').unlink(missing_ok=True);lease.rmdir()
