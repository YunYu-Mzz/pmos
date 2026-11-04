"""Durable exact-task transport over the existing native Shaanxi observer.

No CDP URL, credentials, synthesized request or fallback task is accepted.
The PMOS worker must hold the ORIGINAL worker's kernel lock before using it.
A missing/late result observes the same command; it never republishes under a
new ID. This module alone cannot authorize an ownership handoff.
"""
from __future__ import annotations
import asyncio
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import time
import uuid
from .contracts import canonical
from .faults import Fault

KEY = re.compile(r'[a-f0-9]{64}')


def load(path, default=None):
    try:return json.loads(Path(path).read_text())
    except FileNotFoundError:return default


def atomic(path, value, *, exclusive=False):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    raw=canonical(value);temp=path.with_name('.'+path.name+'.'+uuid.uuid4().hex+'.tmp')
    fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    try:
        with os.fdopen(fd,'wb') as f:f.write(raw);f.flush();os.fsync(f.fileno())
        if exclusive:
            os.link(temp,path);temp.unlink()
        else:os.replace(temp,path)
        d=os.open(path.parent,os.O_RDONLY)
        try:os.fsync(d)
        finally:os.close(d)
    finally:
        if temp.exists():temp.unlink()


def inside(root,value):
    root=Path(root).resolve();p=Path(value)
    if not p.is_absolute():p=root/p
    p=p.resolve()
    if not p.is_relative_to(root) or p.is_relative_to(root/'reporting-private'):
        raise Fault('native_reference_outside_source','local')
    return p


class OriginalWorkerLock:
    def __init__(self,source_root):
        self.path=Path(source_root)/'state/collection_queue/worker.lock'
        self.fd=None

    def acquire(self):
        if self.fd is not None:return
        # The legacy worker already created this file. Never invent a lock in
        # an unrelated source directory and call it exclusion of production.
        if not self.path.is_file() or self.path.is_symlink():
            raise Fault('original_worker_lock_missing','local')
        fd=os.open(self.path,os.O_RDWR)
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd);raise Fault('original_worker_still_owns_account','local') from None
        self.fd=fd

    def check(self):
        if self.fd is None:raise Fault('original_worker_lock_not_owned','local')
        if os.fstat(self.fd).st_ino!=self.path.stat().st_ino:
            raise Fault('original_worker_lock_replaced','local')

    def close(self):
        if self.fd is not None:os.close(self.fd);self.fd=None


class CommandClient:
    """File command protocol only; it does not pick or dispatch arbitrary work."""
    def __init__(self,source_root,instance,owner_lock,*,generation):
        self.source=Path(source_root).resolve();self.instance=Path(instance).resolve()
        self.owner_lock=owner_lock;self.generation=generation
        self.observer=self.source/'state/browser_session'
        self.commands=self.instance/'native_commands'

    def record_path(self,prepared):
        if prepared.get('local_seed'):
            if not re.fullmatch(r'\d{4}-\d{2}-\d{2}_[1-9]\d*',prepared['local_seed']):
                raise Fault('invalid_local_seed_record','local')
            return self.commands/'local_seeds'/(prepared['local_seed']+'.json')
        return self.commands/(str(prepared['attempt'])+'.json')

    def prepare_seed(self,day,sequence):
        from datetime import date,timedelta
        self.owner_lock.check();today=date.fromisoformat(day)
        if today.isoformat()!=day or type(sequence) is not int or sequence<1:
            raise Fault('explicit_local_seed_day_required','local')
        yesterday=(today-timedelta(days=1)).isoformat();name=day+'_'+str(sequence)
        command={'op':'queueSeed','startDate':yesterday,'endDate':yesterday}
        path=self.commands/'local_seeds'/(name+'.json');previous=load(path)
        if previous:
            if previous.get('generation')!=self.generation or previous.get('command')!=command:
                raise Fault('local_seed_record_changed','local')
            return previous
        value={'generation':self.generation,'local_seed':name,'job_key':None,
               'command_id':'pmos_sn_seed_'+today.strftime('%Y%m%d')+'_'+uuid.uuid4().hex,
               'command':command,'prepared_at':time.time(),'phase':'prepared'}
        atomic(path,value,exclusive=True);return value

    def prepare(self,attempt,job_key):
        self.owner_lock.check()
        if type(attempt) is not int or attempt<1 or not isinstance(job_key,str) or not KEY.fullmatch(job_key):
            raise Fault('exact_native_job_and_pmos_attempt_required','local')
        record=self.commands/(str(attempt)+'.json')
        command={'op':'queueStep','preferredTaskKey':job_key}
        previous=load(record)
        if previous is not None:
            if (previous.get('generation')!=self.generation or previous.get('job_key')!=job_key
                or previous.get('command')!=command):
                raise Fault('native_attempt_record_changed','local')
            return previous
        value={'generation':self.generation,'attempt':attempt,'job_key':job_key,
            'command_id':'pmos_sn_'+str(attempt)+'_'+uuid.uuid4().hex,
            'command':command,'prepared_at':time.time(),'phase':'prepared'}
        atomic(record,value,exclusive=True)
        return value

    def publish(self,prepared):
        self.owner_lock.check()
        # Re-read durable preparation before publication, rejecting tampering.
        stored=load(self.record_path(prepared))
        if stored!=prepared:raise Fault('native_preparation_not_durable','local')
        command_id=prepared['command_id'];inbox=self.observer/'inbox'/(command_id+'.json')
        result=self.observer/'results'/(command_id+'.json')
        if result.exists():return command_id
        if inbox.exists():
            if load(inbox)!=prepared['command']:raise Fault('native_inbox_changed','local')
            return command_id
        # The same ID is published exactly once. A repeated publish after a
        # crash only locates that ID/result; it cannot create a second action.
        atomic(inbox,prepared['command'],exclusive=True)
        return command_id

    def result(self,prepared):
        self.owner_lock.check()
        path=self.observer/'results'/(prepared['command_id']+'.json')
        try:raw=path.read_bytes()
        except FileNotFoundError:return None
        try:result=json.loads(raw)
        except (ValueError,UnicodeError):result=None
        value=result.get('result') if isinstance(result,dict) else None
        job=value.get('job') if isinstance(value,dict) else None
        atomic(self.record_path(prepared).with_suffix('.result_ref.json'),{
            'generation':self.generation,'attempt':prepared.get('attempt'),'local_seed':prepared.get('local_seed'),'command_id':prepared['command_id'],
            'result_file':str(path),'result_sha256':hashlib.sha256(raw).hexdigest(),
            'job_key':prepared['job_key'],'phase':value.get('phase') if isinstance(value,dict) else None,
            'status':job.get('status') if isinstance(job,dict) else None,'observed_at':time.time()})
        if not isinstance(result,dict) or result.get('ok') is not True:
            raise Fault('native_observer_error_preserved','network')
        value=result.get('result')
        if not isinstance(value,dict):raise Fault('native_result_shape_changed','network')
        job=value.get('job')
        if job is not None and not isinstance(job,dict):raise Fault('native_result_job_shape_changed','network')
        if job is not None and job.get('key')!=prepared['job_key']:
            raise Fault('native_adapter_selected_wrong_job','network')
        return value

    async def wait(self,prepared,*,timeout=600,poll=1):
        deadline=time.monotonic()+timeout
        while True:
            value=self.result(prepared)
            if value is not None:return value
            if time.monotonic()>=deadline:
                raise Fault('original_native_command_result_pending','network')
            await asyncio.sleep(poll)


def verify_pins(source_root,runtime_pins,*,runtime_root=None):
    root=Path(source_root)
    for relative,digest in runtime_pins.items():
        if relative.startswith('@runtime/'):
            if runtime_root is None:raise Fault('runtime_pin_root_missing','local',unsent=True)
            p=inside(runtime_root,relative[len('@runtime/'):])
            if p.suffix!='.mjs' or p.parent!=Path(runtime_root).resolve():
                raise Fault('runtime_pin_scope_invalid','local',unsent=True)
        else:p=inside(root,relative)
        if hashlib.sha256(p.read_bytes()).hexdigest()!=digest:
            raise Fault('native_source_code_changed','local',unsent=True)


def verify_readonly_preflight(source_root,job_key,*,runtime_pins,owner_lock,runtime_root=None,clock=time.time):
    """No browser calls, commands or websites. All waits are proven unsent."""
    owner_lock.check();root=Path(source_root);s=root/'state'
    verify_pins(source_root,runtime_pins,runtime_root=runtime_root)
    if (s/'session_block.json').exists():raise Fault('native_session_protected','local',unsent=True)
    if not load(s/'collection_queue/control.json',{}).get('enabled') or not load(s/'keeper/control.json',{}).get('enabled'):
        raise Fault('native_control_paused','local',unsent=True)
    if (s/'turn.lock').exists():raise Fault('native_turn_still_inflight','local',unsent=True)
    if (s/'collection_queue/pending_command.json').exists():raise Fault('legacy_command_still_pending','local',unsent=True)
    k=load(s/'keeper/status.json',{});proof=load(s/'keeper/last_success.json',{})
    from datetime import datetime
    try:
        observed=datetime.fromisoformat(k['at'].replace('Z','+00:00')).timestamp()
        verified=datetime.fromisoformat(proof['finishedAt'].replace('Z','+00:00')).timestamp()
        due=datetime.fromisoformat(k['nextAttemptAt'].replace('Z','+00:00')).timestamp()
    except (ValueError,KeyError,TypeError):raise Fault('native_keeper_evidence_missing','local',unsent=True) from None
    if (not 0<=clock()-observed<=10 or not 0<=clock()-verified<=600 or
        not proof.get('passed') or not proof.get('fullSampleMatches') or k.get('pending') or
        k.get('requiresReview') or not k.get('controlEnabled') or k.get('phase') not in {'idle','succeeded'} or clock()>=due):
        raise Fault('native_keeper_not_ready','local',unsent=True)
    journal=load(s/'collection_queue/journal.json',{});job=journal.get('jobs',{}).get(job_key)
    if not job or job.get('status')!='pending' or job.get('reservedAt'):
        raise Fault('native_job_no_longer_unconsumed','local',unsent=True)
    if job['datasetId'] in journal.get('datasetBlocks',{}):
        raise Fault('native_source_is_protected','local',unsent=True)
    return job
