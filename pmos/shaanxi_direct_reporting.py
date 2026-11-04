"""Same twenty-field sender protocol, frozen baseline + incremental SQLite."""
from __future__ import annotations
import copy,hashlib,json,shutil,sqlite3,time,uuid
from collections import Counter
from datetime import datetime,timezone
from pathlib import Path
from .shaanxi_transport import atomic,load,inside
from .shaanxi_reporting import owner_evidence

EXTRA_SCHEMA='''
CREATE TABLE IF NOT EXISTS sn_source_artifacts(source_id TEXT NOT NULL,sha256 TEXT NOT NULL,PRIMARY KEY(source_id,sha256));
CREATE TABLE IF NOT EXISTS sn_artifact_metadata(sha256 TEXT PRIMARY KEY,mtime REAL NOT NULL,pdf INTEGER NOT NULL);
'''


def freeze_baseline(module,source,instance,store,journal,registry):
    """One explicit migration read, never called by the online worker."""
    source=Path(source);instance=Path(instance);store.db.executescript(EXTRA_SCHEMA);mapping={}
    for job in journal['jobs'].values():
        if job.get('validation'):mapping.setdefault(str(inside(source,job['validation'])),set()).add(job['datasetId'])
    for d in registry['datasets']:
        if d.get('sampleEvidence'):mapping.setdefault(str(inside(source,d['sampleEvidence'])/'validated.json'),set()).add(d['id'])
    evidence=module.evidence;captured={};sources=set()
    def record(path,sid,digest,size,pdf):
        p=Path(path);captured.setdefault(digest,(str(p),size,p.stat().st_mtime,int(pdf)))
        if sid:sources.add((sid,digest))
    def observed(root,path,*args,**kwargs):
        found=evidence(root,path,*args,**kwargs)
        if found:
            _,p,digest,size,pdf=found
            for sid in mapping.get(str(Path(path)),{None}):record(p,sid,digest,size,pdf)
        return found
    module.evidence=observed
    try:baseline=module.snapshot(source)
    finally:module.evidence=evidence
    for job in journal['jobs'].values():
        imported=job.get('importedEvidence')
        if job['status']=='saved' and imported:
            p=inside(source,imported['path']);raw=p.read_bytes();digest=hashlib.sha256(raw).hexdigest()
            if digest==imported['sha256'] and raw.startswith(b'%PDF-') and b'%%EOF' in raw[-2048:]:record(p,job['datasetId'],digest,len(raw),True)
    if len(captured)!=baseline['metrics']['ordinary_originals']:raise ValueError('baseline_verified_sha_inventory_mismatch')
    owned_before=set();manifest=instance/'original/manifest.jsonl'
    if manifest.exists():
        for line in manifest.read_text().splitlines():
            record=json.loads(line)
            if record['sha256'] in captured:owned_before.add(record['sha256'])
    with store.transaction():
        for digest,(path,size,mtime,pdf) in captured.items():
            store.db.execute('INSERT OR IGNORE INTO sn_artifacts VALUES(?,?,?,?,?)',(digest,path,size,time.time(),'pmos_compatibility_baseline' if digest in owned_before else 'frozen_baseline'))
            store.db.execute('INSERT OR IGNORE INTO sn_artifact_metadata VALUES(?,?,?)',(digest,mtime,pdf))
        store.db.executemany('INSERT OR IGNORE INTO sn_source_artifacts VALUES(?,?)',sorted(sources))
        store.set_setting('report_baseline_at',baseline['generated_at'])
    atomic(instance/'direct_config/report_baseline.json',baseline,exclusive=True)
    return {'verified_originals':len(captured),'source_sha_mappings':len(sources),'coverage_rows':len(baseline['coverage']['indicators'])}


def snapshot(instance,status_mail,now=None):
    now=time.time() if now is None else now;root=Path(instance);config=load(root/'handoff.json');source=Path(config['source_root'])
    baseline=load(root/'direct_config/report_baseline.json');report=copy.deepcopy(baseline)
    db=sqlite3.connect('file:'+str(root/'ledger.sqlite3')+'?mode=ro',uri=True);db.row_factory=sqlite3.Row
    try:
        db.execute('BEGIN');status=load(root/'state/status.json',{});owner=owner_evidence(root,now);control=load(root/'state/control.json',{})
        artifacts={r['sha256']:dict(r) for r in db.execute('SELECT a.*,m.mtime,m.pdf FROM sn_artifacts a JOIN sn_artifact_metadata m USING(sha256)')}
        missing=set();direct_rows=list(db.execute('SELECT a.*,j.params FROM sn_job_artifacts a JOIN sn_jobs j ON j.key=a.job_key'))
        validations={}
        for row in direct_rows:
            v=load(inside(root,row['validation']));p=inside(root,v.get('responseFile',''))
            if not p.is_file() or hashlib.sha256(p.read_bytes()).hexdigest()!=row['sha256']:missing.add(row['sha256']);continue
            validations[row['job_key']]={'outcome':v['outcome'],'nonempty_rows':isinstance(v.get('rows'),list) and bool(v['rows']),'native_query':v.get('native_query') or {}}
        total=sum(a['bytes'] for k,a in artifacts.items() if k not in missing);metrics=report['metrics'];disk=shutil.disk_usage(root)
        counts=dict(db.execute('SELECT status,count(*) FROM sn_jobs GROUP BY status'));holds=db.execute("SELECT count(*) FROM sn_holds WHERE scope='source'").fetchone()[0]
        block=load(source/'state/session_block.json',{});auth=block.get('reason')=='auth_expired';phase=status.get('phase','unknown');keeper=load(source/'state/keeper/status.json',{});success=load(source/'state/keeper/last_success.json',{})
        mem=None
        if Path('/proc/meminfo').exists():mem=int(next(x.split()[1] for x in Path('/proc/meminfo').read_text().splitlines() if x.startswith('MemAvailable:')))*1024
        last=db.execute('SELECT max(finished_at) FROM sn_job_artifacts').fetchone()[0]
        recent=sum(now-3600<=a['mtime']<=now for k,a in artifacts.items() if k not in missing)
        metrics.update(ordinary_originals=len(artifacts)-len(missing),ordinary_bytes=total,last_hour_files_mtime=recent,missing_originals=baseline['metrics'].get('missing_originals',0)+len(missing),disk_free_bytes=disk.free,disk_total_bytes=disk.total,memory_available_bytes=mem,collector_state='active' if owner['owned_live'] else 'inactive',collector_pid=status.get('pid') if owner['owned_live'] else 0,current_dataset=None,collection_paused=auth or not owner['owned_live'] or not control.get('collect') or bool(control.get('paused') or control.get('stop_all')),blocked=auth,phase=phase,keeper_enabled=keeper.get('controlEnabled'),keeper_last_success_at=success.get('finishedAt') if success.get('passed') and success.get('fullSampleMatches') else None,business_last_success_at=datetime.fromtimestamp(last,timezone.utc).isoformat() if last else baseline['metrics'].get('business_last_success_at'))
        source_id=status.get('legacy_source');source_row=db.execute('SELECT body FROM sn_sources WHERE id=?',(source_id,)).fetchone()
        if source_row and phase=='dispatching_native_task':metrics['current_dataset']=json.loads(source_row[0]).get('name')
        valid_sha=set(artifacts)-missing
        pmos_sha=({r[0] for r in db.execute("SELECT sha256 FROM sn_artifacts WHERE origin IN ('pmos_direct','pmos_compatibility_baseline')")} | {row['sha256'] for row in direct_rows}) & valid_sha
        # The previous PMOS compatibility originals are already included in
        # the verified baseline. Keep that partition explicit and disjoint.
        requests=db.execute('SELECT count(*) FROM sn_requests WHERE started_at>=?',(now-3600,)).fetchone()[0]
        metrics['counts_note']=status_mail.counts_note(label='陕西',owner='PMOS',since=None,headline=status_mail.headline(metrics),legacy_originals=len(valid_sha)-len(pmos_sha),pmos_originals=len(pmos_sha),last_hour_files=recent,requests_last_hour=requests,saved=counts.get('saved',0)+counts.get('admitted',0),empty=counts.get('empty',0)+counts.get('empty_sample_not_admitted',0),pending=counts.get('pending',0),waiting=0,held=holds,blocked=auth,block_reason='登录失效，等待人工登录' if auth else None,stalled_minutes=max(0,int((now-last)/60)) if last else None,dedupe='原件SHA',tail='旧原库按切换时核验基线保留；新任务、分页及原件来自PMOS独立任务库。')
        for item in report['coverage']['indicators']:
            ids=item['ids'];marks=','.join('?'*len(ids));shas={r[0] for r in db.execute('SELECT DISTINCT sha256 FROM sn_source_artifacts WHERE source_id IN ('+marks+')',ids)};shas-=missing
            item['file_count']=len(shas) if shas else item['file_count'];item['bytes']=sum(artifacts[k]['bytes'] for k in shas) if shas else item['bytes']
            item['queue_status']=dict(db.execute('SELECT status,count(*) FROM sn_jobs WHERE source_id IN ('+marks+') GROUP BY status',ids))
            periods=set(item.get('periods',[]));scopes=set(item.get('scopes',[]))
            new=[r for r in direct_rows if r['source_id'] in ids and r['job_key'] in validations]
            for row in new:
                v=validations[row['job_key']];p=json.loads(row['params'])
                if v['outcome']=='saved' and v['nonempty_rows'] and p.get('date'):periods.add(p['date'])
                for field in ('valueType','name','unitName'):
                    value=(v.get('native_query') or {}).get('nativeFilters',{}).get(field)
                    if value:scopes.add('查询筛选 '+field+'='+str(value))
            item.update(periods=sorted(periods),unique_periods=len(periods),date_start=min(periods) if periods else None,date_end=max(periods) if periods else None,scopes=sorted(scopes))
            if new:item['note']+=' 已合并PMOS直接采集的原件与任务事实；分页或维度存在异常时不声明全量完整。'
        report.update(report_id=str(uuid.uuid4()),generated_at=datetime.fromtimestamp(now,timezone.utc).isoformat(timespec='seconds'))
        report['coverage']['notes']=['旧原库为切换时逐原件核验SHA的冻结基线；运行中只合并PMOS SQLite及新原件，不读旧队列。','各指标共享原件按SHA去重；目录、样本、任务和完整历史范围分别保留，不推定全部采齐。']
        return report
    finally:db.close()
