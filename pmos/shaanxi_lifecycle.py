"""Guarded owner handoff/rollback primitives for the isolated Shaanxi release.

This module never refreshes, logs in, clears a block or dispatches a website
request. Source code and the native observer/keeper remain untouched.
"""
from __future__ import annotations
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
import uuid
from .contracts import canonical
from .faults import Fault
from .shaanxi_transport import OriginalWorkerLock, atomic, load

WORKER_SERVICE='shaanxi-queue-worker.service'
SOURCE_ID='shaanxi-native-journal'


def process_identity(pid,proc_root=Path('/proc')):
    if type(pid) is not int or pid<=0:raise Fault('process_identity_required','local')
    try:
        stat=(proc_root/str(pid)/'stat').read_text();tail=stat[stat.rfind(') ')+2:].split()
        return {'pid':pid,'start_ticks':tail[19],
                'boot_id':(proc_root/'sys/kernel/random/boot_id').read_text().strip()}
    except (OSError,IndexError):raise Fault('original_process_not_live','local') from None


def verify_process(expected):
    if process_identity(expected['pid'])!=expected:
        raise Fault('original_process_identity_changed','local')


def code_fingerprint(code_root):
    root=Path(code_root).resolve()
    paths=sorted(p for directory in ('pmos','provinces','tools','deploy')
                 for p in (root/directory).rglob('*') if p.is_file() and
                 p.suffix in {'.py','.js','.cjs','.mjs','.json','.service','.conf'} and '__pycache__' not in p.parts)
    if not paths or any(p.is_symlink() for p in paths):raise Fault('regular_code_snapshot_required','local')
    values={str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    return hashlib.sha256(canonical(values)).hexdigest()


def collect_pins(source_root,runtime_root):
    source=Path(source_root).resolve();runtime=Path(runtime_root).resolve();pins={}
    for p in sorted((source/'shaanxi_crawler').glob('*.py')):
        if p.is_symlink():raise Fault('source_code_symlink','local')
        pins[str(p.relative_to(source))]=hashlib.sha256(p.read_bytes()).hexdigest()
    for p in sorted(runtime.glob('*.mjs')):
        if p.is_symlink():raise Fault('runtime_code_symlink','local')
        pins['@runtime/'+p.name]=hashlib.sha256(p.read_bytes()).hexdigest()
    if not pins or '@runtime/queue_operations.mjs' not in pins:raise Fault('native_runtime_pin_missing','local')
    return pins


def check_source_quiescent(source_root,*,require_healthy=True,clock=time.time):
    root=Path(source_root).resolve();s=root/'state'
    if (s/'session_block.json').exists():raise Fault('original_session_block_preserved','local')
    if (s/'turn.lock').exists():raise Fault('original_turn_inflight','local')
    if (s/'collection_queue/pending_command.json').exists():raise Fault('original_command_pending','local')
    k=load(s/'keeper/status.json',{});r=load(s/'keeper/runtime.json',{})
    if k.get('pending') or r.get('pending') or k.get('requiresReview') or r.get('reviewRequired'):
        raise Fault('original_keeper_pending_review','local')
    if require_healthy:
        p=load(s/'keeper/last_success.json',{})
        try:
            age=clock()-datetime.fromisoformat(p['finishedAt'].replace('Z','+00:00')).timestamp()
            status_age=clock()-datetime.fromisoformat(k['at'].replace('Z','+00:00')).timestamp()
        except (ValueError,KeyError,TypeError):raise Fault('original_keeper_evidence_missing','local') from None
        if not (0<=age<=600 and 0<=status_age<=10 and p.get('passed') and p.get('fullSampleMatches') and k.get('controlEnabled')):
            raise Fault('original_keeper_not_verified','local')
        verify_process(process_identity(k['pid']))
    # Original observer retains inbox files. Anything without its result has
    # unknown completion, regardless of an old manifest/status saying idle.
    inbox=s/'browser_session/inbox';results=s/'browser_session/results'
    if any(not (results/p.name).is_file() for p in inbox.glob('*.json')):
        raise Fault('original_observer_command_unanswered','local')
    return k


def frozen_snapshot(source_root,instance):
    source=Path(source_root)/'state/collection_queue/journal.json';base=Path(instance)/'source_snapshots';base.mkdir(parents=True,exist_ok=True,mode=0o700)
    raw=source.read_bytes();digest=hashlib.sha256(raw).hexdigest();dest=base/(digest+'.json')
    if not dest.exists():
        os.link(source,dest)
        fd=os.open(base,os.O_RDONLY)
        try:os.fsync(fd)
        finally:os.close(fd)
    if hashlib.sha256(dest.read_bytes()).hexdigest()!=digest:raise Fault('frozen_source_snapshot_changed','local')
    return dest,digest,json.loads(raw)


def prepare_handoff(source_root,runtime_root,instance,code_root,*,approval,operations_budget=5):
    """Only called after the operator has gracefully stopped the original worker.

    Acquires the SAME kernel lock, checks all original pending evidence, then
    freezes the journal and records an explicit new PMOS generation. It cannot
    itself stop a service or grant the ledger any dispatch rights.
    """
    if not isinstance(approval,str) or not approval.strip():raise Fault('user_goal_approval_required','local')
    if type(operations_budget) is not int or not 1<=operations_budget<=1000:raise Fault('operation_budget_invalid','local')
    source=Path(source_root).resolve();instance=Path(instance).resolve()
    if instance==source or instance.is_relative_to(source):raise Fault('independent_instance_required','local')
    control=load(source/'state/collection_queue/control.json',{})
    if not control.get('enabled'):raise Fault('original_collection_control_not_enabled','local')
    lock=OriginalWorkerLock(source);lock.acquire()
    try:
        keeper=check_source_quiescent(source)
        owner=load(source/'state/collection_queue/worker_owner.json',{})
        if type(owner.get('pid')) is not int:raise Fault('original_worker_owner_record_missing','local')
        try:os.kill(owner['pid'],0)
        except ProcessLookupError:pass
        else:raise Fault('original_worker_still_live','local')
        snapshot,digest,journal=frozen_snapshot(source,instance)
        ready=load(source/'state/browser_session/ready.json',{})
        proof=load(source/'state/keeper/last_success.json',{})
        config={'version':1,'province':'shaanxi','account_alias':'shaanxi-production',
            'generation':'pmos-shaanxi-'+uuid.uuid4().hex,'source_root':str(source),
            'runtime_root':str(Path(runtime_root).resolve()),'instance':str(instance),
            'code_sha':code_fingerprint(code_root),'approval':approval,'operations_budget':operations_budget,
            'budget_unit':'native_queue_operations_not_http_requests','runtime_pins':collect_pins(source,runtime_root),
            'snapshot_ref':str(snapshot),'snapshot_sha256':digest,
            'original_worker':{'pid':owner['pid'],'started_at':owner.get('startedAt'),
                'stopped_evidence':'original_pid_absent_and_original_worker_kernel_lock_acquired'},
            'observer':process_identity(ready['pid']),'keeper':process_identity(keeper['pid']),
            'browser':process_identity(proof['browserPid']),
            'original_controls':{'queue':control,'keeper':load(source/'state/keeper/control.json',{})},
            'prepared_at':datetime.now(timezone.utc).isoformat(),'phase':'prepared_no_dispatch',
            'snapshot_jobs':len(journal['jobs'])}
        path=instance/'handoff.json'
        if path.exists():raise Fault('existing_handoff_preserved','local')
        atomic(path,config,exclusive=True)
        return config
    finally:lock.close()


def validate_handoff(config,code_root):
    if config.get('version')!=1 or config.get('province')!='shaanxi' or not config.get('approval'):
        raise Fault('shaanxi_handoff_not_authorized','local')
    if code_fingerprint(code_root)!=config.get('code_sha'):raise Fault('pmos_release_code_changed','local')
    source=Path(config['source_root']).resolve();instance=Path(config['instance']).resolve()
    if instance.is_relative_to(source):raise Fault('independent_instance_required','local')
    for field in ('observer','keeper','browser'):verify_process(config[field])
    raw=Path(config['snapshot_ref']).read_bytes()
    if hashlib.sha256(raw).hexdigest()!=config['snapshot_sha256']:raise Fault('handoff_snapshot_changed','local')
    return source,instance


def service_state():
    result=subprocess.run(['systemctl','--user','show',WORKER_SERVICE,'-p','MainPID','-p','ActiveState','-p','SubState'],check=True,capture_output=True,text=True)
    return dict(line.split('=',1) for line in result.stdout.splitlines() if '=' in line)


def stop_original_worker(expected_pid,expected_identity=None):
    # Refuse a new maintenance owner rather than stopping whatever happens to
    # occupy a service name. The caller must also pin the original start ticks.
    before=service_state()
    if before.get('MainPID')!=str(expected_pid):raise Fault('original_worker_owner_changed','local')
    verify_process(expected_identity or process_identity(expected_pid))
    subprocess.run(['systemctl','--user','stop',WORKER_SERVICE],check=True,timeout=620)
    after=service_state()
    if after.get('MainPID')!='0':raise Fault('original_worker_not_stopped','local')
    return {'before':before,'after':after,'stopped_at':datetime.now(timezone.utc).isoformat()}
