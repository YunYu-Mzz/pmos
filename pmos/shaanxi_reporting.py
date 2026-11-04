"""Annotate the EXISTING sender's snapshot with current PMOS owner evidence.

No new sender, keys, mail state, HMAC, recipient or network transport exists.
The legacy original/coverage scan and its one atomic task-count view remain.
"""
from datetime import datetime
from pathlib import Path
import hashlib
import json
import time
from .shaanxi_transport import load
from .shaanxi_lifecycle import verify_process
from .faults import Fault

ACTIVE_PHASES=frozenset({'dispatching_native_task','completed','idle','seeding_next_day_locally','task_isolated','waiting'})


def augment_metrics(metrics,evidence):
    if not evidence.get('owned_live'):
        return False
    status=evidence['status'];phase=status.get('phase','unknown')
    metrics['collector_state']='active';metrics['collector_pid']=status['pid'];metrics['phase']=phase
    paused=phase not in ACTIVE_PHASES or not evidence['control'].get('collect') or evidence['control'].get('paused') or evidence['control'].get('stop_all')
    metrics['collection_paused']=bool(paused or metrics.get('blocked') or metrics.get('collection_paused') is True)
    if phase in {'waiting_auth_or_access_review','waiting_original_protection','waiting_original_native_result'}:
        metrics['blocked']=True
    return True


def owner_evidence(instance,now=None):
    now=time.time() if now is None else now;root=Path(instance);status=load(root/'state/status.json',{})
    owner=load(root/'state/native_owner.json',{});config=load(root/'handoff.json',{});control=load(root/'state/control.json',{})
    verified=False
    try:
        stamp=datetime.fromisoformat(status['updated_at'].replace('Z','+00:00')).timestamp()
        verify_process(owner['start_identity'])
        verified=(owner.get('phase')=='owned' and owner.get('generation')==config.get('generation')==status.get('generation')
                  and status.get('pid')==owner.get('pid') and 0<=now-stamp<=120)
    except (Fault,ValueError,KeyError,TypeError):pass
    # The native transport retains originals in the compatibility directory as
    # well as PMOS hard links. Count the same SHA once, never add two archives.
    digests=set();manifest=root/'original/manifest.jsonl';archive_ok=True
    try:
        raw=manifest.read_bytes() if manifest.exists() else b''
        for line in raw.splitlines(keepends=True):
            if not line.endswith(b'\n'):continue  # concurrently appended final record
            item=json.loads(line);p=(root/'original'/item['path']).resolve()
            if not p.is_relative_to((root/'original').resolve()):raise ValueError('archive_reference_outside_instance')
            if hashlib.sha256(p.read_bytes()).hexdigest()!=item['sha256']:raise ValueError('archive_hash_changed')
            digests.add(item['sha256'])
    except (OSError,ValueError,KeyError,TypeError):archive_ok=False
    return {'owned_live':verified,'status':status,'owner':owner,'control':control,'observed_at':now,
            'pmos_unique_originals':len(digests) if archive_ok else None}


def wrap_existing_snapshot(snapshot_module,instance):
    original_snapshot=snapshot_module.snapshot;original_note=snapshot_module.status_mail.legacy_note

    def snapshot(root):
        evidence=owner_evidence(instance)
        registry=load(Path(root)/'config/spot_registry.json',{})
        names={d['id']:d.get('name',d['id']) for d in registry.get('datasets',[])}
        def note(metrics,**kwargs):
            owns=augment_metrics(metrics,evidence)
            if not owns:return original_note(metrics,**kwargs)
            if 'current_dataset' in metrics:
                source=evidence['status'].get('legacy_source')
                metrics['current_dataset']=names.get(source) if evidence['status'].get('phase') not in {'idle','stopped','paused'} else None
            formatter=getattr(snapshot_module.status_mail,'counts_note',None)
            if formatter is None:return original_note(metrics,**kwargs).replace('由旧系统采集','由PMOS采集')
            total=metrics.get('ordinary_originals');pmos=evidence.get('pmos_unique_originals')
            remaining=total-pmos if type(total) is int and type(pmos) is int and 0<=pmos<=total else None
            if remaining is None:pmos=None
            text=formatter(label=kwargs.get('label','陕西'),owner='PMOS',since=None,
                headline=snapshot_module.status_mail.headline(metrics),legacy_originals=remaining,pmos_originals=pmos,
                last_hour_files=metrics.get('last_hour_files_mtime'),requests_last_hour=kwargs.get('requests_last_hour'),
                saved=kwargs.get('saved'),empty=kwargs.get('empty'),pending=kwargs.get('pending'),waiting=kwargs.get('waiting'),
                held=kwargs.get('held'),blocked=metrics.get('blocked'),block_reason=kwargs.get('block_reason'),
                stalled_minutes=kwargs.get('stalled_minutes'),dedupe=kwargs.get('dedupe','原件SHA'),
                tail='PMOS原件已包含在原目录总数中，按SHA划归，不重复相加。')
            return text.replace('旧系统冻结','原库其余')
        snapshot_module.status_mail.legacy_note=note
        try:return original_snapshot(root)
        finally:snapshot_module.status_mail.legacy_note=original_note
    return snapshot
