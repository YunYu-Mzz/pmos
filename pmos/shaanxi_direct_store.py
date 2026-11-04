"""PMOS's incremental Shaanxi frontier. No live legacy JSON queue access.

The source journal is accepted only as an explicit, frozen migration input.
Runtime operations update bounded SQLite rows; child creation and page checks
are committed with the parent's confirmation.
"""
from __future__ import annotations
from contextlib import contextmanager
from datetime import date,datetime,timedelta,timezone
import hashlib,json,time
from .contracts import canonical
from .migration.shaanxi import task_identity,dataset_id,verified_key
from .faults import Fault

TZ=timezone(timedelta(hours=8))
ATTACHMENTS={'download','monthly_attachment','monthly_context'}
SNAPSHOTS={'catalogue','listing','file_listing'}
BATCH_LIMIT=64
ATTACHMENT_BATCH_LIMIT=8
SCHEMA='''
CREATE TABLE IF NOT EXISTS sn_sources(id TEXT PRIMARY KEY,body TEXT NOT NULL,sha256 TEXT NOT NULL,family TEXT,enabled INTEGER NOT NULL,target_id TEXT);
CREATE TABLE IF NOT EXISTS sn_jobs(key TEXT PRIMARY KEY,source_id TEXT NOT NULL,kind TEXT NOT NULL,params TEXT NOT NULL,body TEXT NOT NULL,identity_key TEXT,file_key TEXT,status TEXT NOT NULL,consumed INTEGER NOT NULL,priority INTEGER NOT NULL,created_at TEXT NOT NULL,finished_at TEXT,task_date TEXT,snapshot_date TEXT,page INTEGER,validation TEXT,artifact_sha TEXT,reason TEXT);
CREATE INDEX IF NOT EXISTS sn_jobs_pending ON sn_jobs(status,consumed,priority,created_at,key);
CREATE INDEX IF NOT EXISTS sn_jobs_source ON sn_jobs(source_id,status,kind);
CREATE INDEX IF NOT EXISTS sn_jobs_snapshot ON sn_jobs(status,snapshot_date,kind,page);
CREATE INDEX IF NOT EXISTS sn_jobs_identity ON sn_jobs(identity_key);
CREATE TABLE IF NOT EXISTS sn_holds(scope TEXT NOT NULL,source_id TEXT NOT NULL,anchor TEXT NOT NULL DEFAULT '',body TEXT NOT NULL,PRIMARY KEY(scope,source_id,anchor));
CREATE TABLE IF NOT EXISTS sn_settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sn_imports(sha256 TEXT PRIMARY KEY,record_count INTEGER NOT NULL,source_ref TEXT NOT NULL,at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS sn_page_groups(key TEXT PRIMARY KEY,total INTEGER NOT NULL,legacy_key_count INTEGER NOT NULL DEFAULT 0,complete INTEGER NOT NULL DEFAULT 0,body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sn_page_keys(group_key TEXT NOT NULL,record_key TEXT NOT NULL,job_key TEXT,PRIMARY KEY(group_key,record_key));
CREATE TABLE IF NOT EXISTS sn_pages(group_key TEXT NOT NULL,page INTEGER NOT NULL,job_key TEXT,finished_at REAL,PRIMARY KEY(group_key,page));
CREATE TABLE IF NOT EXISTS sn_unit_keys(group_key TEXT NOT NULL,natural_key TEXT NOT NULL,job_key TEXT,PRIMARY KEY(group_key,natural_key));
CREATE TABLE IF NOT EXISTS sn_relations(parent TEXT NOT NULL,child TEXT NOT NULL,kind TEXT NOT NULL,PRIMARY KEY(parent,child));
CREATE TABLE IF NOT EXISTS sn_file_claims(file_id TEXT PRIMARY KEY,job_key TEXT NOT NULL,state TEXT NOT NULL,proof TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sn_turns(key TEXT PRIMARY KEY,job_key TEXT NOT NULL,attempt INTEGER,stage TEXT NOT NULL,state TEXT NOT NULL,plan_ref TEXT,receipt_ref TEXT,receipt_sha TEXT);
CREATE INDEX IF NOT EXISTS sn_turns_job ON sn_turns(job_key,stage);
CREATE TABLE IF NOT EXISTS sn_legacy_turns(key TEXT PRIMARY KEY,job_key TEXT,stage TEXT,identity TEXT NOT NULL,source_ref TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS sn_legacy_turn_job ON sn_legacy_turns(job_key,stage);
CREATE TABLE IF NOT EXISTS sn_commands(id TEXT PRIMARY KEY,job_key TEXT NOT NULL,attempt INTEGER,stage TEXT NOT NULL,payload TEXT NOT NULL,phase TEXT NOT NULL,result_ref TEXT,prepared_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS sn_artifacts(sha256 TEXT PRIMARY KEY,path TEXT NOT NULL,bytes INTEGER NOT NULL,first_seen REAL NOT NULL,origin TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sn_job_artifacts(job_key TEXT PRIMARY KEY,sha256 TEXT NOT NULL,source_id TEXT NOT NULL,outcome TEXT NOT NULL,validation TEXT NOT NULL,finished_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS sn_records(source_id TEXT NOT NULL,record_id TEXT NOT NULL,body TEXT NOT NULL,parent_job TEXT NOT NULL,PRIMARY KEY(source_id,record_id,parent_job));
CREATE TABLE IF NOT EXISTS sn_requests(session_id TEXT NOT NULL,request_id TEXT NOT NULL,turn_key TEXT NOT NULL,started_at REAL NOT NULL,path TEXT NOT NULL,PRIMARY KEY(session_id,request_id));
CREATE TABLE IF NOT EXISTS sn_pdf_jobs(job_key TEXT PRIMARY KEY,source_id TEXT NOT NULL,sha256 TEXT NOT NULL,validation TEXT NOT NULL,status TEXT NOT NULL,created_at REAL NOT NULL,started_at REAL,finished_at REAL,result_ref TEXT,error TEXT);
CREATE INDEX IF NOT EXISTS sn_pdf_pending ON sn_pdf_jobs(status,created_at);
CREATE TABLE IF NOT EXISTS sn_exception_reviews(review_id TEXT PRIMARY KEY,job_key TEXT UNIQUE NOT NULL,approval TEXT NOT NULL,proof TEXT NOT NULL,created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS sn_cached_recoveries(job_key TEXT PRIMARY KEY,approval TEXT NOT NULL,proof TEXT NOT NULL,recovered_at REAL NOT NULL);
'''


def stamp():return datetime.now(timezone.utc).isoformat().replace('+00:00','Z')
def encode(v):return json.dumps(v,ensure_ascii=False,separators=(',',':'))
def key_for(source,kind,params):
    return hashlib.sha256(canonical({'kind':'download','fileId':params['fileId']} if kind=='download' else {'datasetId':source,'kind':kind,'params':params})).hexdigest()
def group_for(job):
    scope={k:v for k,v in job['params'].items() if k!='page'}
    return hashlib.sha256(canonical({'datasetId':job['datasetId'],'params':scope,'nativeFilters':job.get('nativeFilters',{})})).hexdigest()
def unit_row_matches(row,day):
    return isinstance(row,dict) and row.get('date')==day and all(isinstance(row.get(k),str) and bool(row[k]) for k in ('MEAS_TYPE','UNIT_ID'))


class DirectStore:
    def __init__(self,ledger,account_alias):
        self.ledger=ledger;self.db=ledger.db;self.account=account_alias;self.db.executescript(SCHEMA)

    @contextmanager
    def transaction(self):
        own=not self.db.in_transaction
        if own:self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
            if own:self.db.execute('COMMIT')
        except BaseException:
            if own:self.db.execute('ROLLBACK')
            raise

    def setting(self,key,default=None):
        row=self.db.execute('SELECT value FROM sn_settings WHERE key=?',(key,)).fetchone()
        return json.loads(row[0]) if row else default
    def set_setting(self,key,value):self.db.execute('INSERT OR REPLACE INTO sn_settings VALUES(?,?)',(key,encode(value)))
    def source(self,sid):
        row=self.db.execute('SELECT body FROM sn_sources WHERE id=?',(sid,)).fetchone()
        if row is None:raise Fault('direct_source_not_registered','local',unsent=True)
        return json.loads(row[0])
    def job(self,key):
        row=self.db.execute('SELECT body FROM sn_jobs WHERE key=?',(key,)).fetchone()
        return json.loads(row[0]) if row else None
    def hold(self,scope,sid,anchor,body):self.db.execute('INSERT OR REPLACE INTO sn_holds VALUES(?,?,?,?)',(scope,sid,anchor,encode(body)))

    def _insert(self,job):
        # Preserve the exact native object order in the stored payload, while
        # the PMOS business identity remains canonical and versioned.
        key=job['key'];identity=None
        try:identity=task_identity(job,self.account)
        except (ValueError,KeyError,TypeError):pass
        state=job['status'];consumed=state!='pending' or bool(job.get('reservedAt') or job.get('finishedAt') or job.get('validation'))
        if identity is None and state=='pending':state='held_identity_unmapped';consumed=True
        if state!=job['status']:job={**job,'legacyStatus':job['status'],'status':state}
        p=job['params'];self.db.execute('INSERT OR IGNORE INTO sn_jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (key,job['datasetId'],job['kind'],encode(p),encode(job),identity.key if identity else None,identity.file_key if identity else None,state,int(consumed),job.get('priority',100),job.get('createdAt',''),job.get('finishedAt'),p.get('date'),p.get('snapshotDate'),p.get('page'),job.get('validation'),None,job.get('error')))
        for child in job.get('children',[]):self.db.execute('INSERT OR IGNORE INTO sn_relations VALUES(?,?,?)',(key,child,'dimension'))
        if job.get('parentJob'):self.db.execute('INSERT OR IGNORE INTO sn_relations VALUES(?,?,?)',(job['parentJob'],key,'dimension'))
        fid=p.get('fileId')
        if fid and job['kind'] in {'download','monthly_attachment'} and consumed:
            self.db.execute('INSERT OR IGNORE INTO sn_file_claims VALUES(?,?,?,?)',(fid,key,state,encode({'origin':'frozen_task','legacy_key':key,'automatic_replay_allowed':False})))

    def import_frozen(self,journal,registry,*,sha256,source_ref):
        if self.db.execute('SELECT 1 FROM sn_imports WHERE sha256=?',(sha256,)).fetchone():return
        if self.db.execute('SELECT 1 FROM sn_imports LIMIT 1').fetchone():raise Fault('direct_frontier_already_imported','local')
        if registry.get('province')!='shaanxi':raise Fault('shaanxi_registry_required','local')
        with self.transaction():
            for source in registry['datasets']:
                target=source.get('collectorTarget') or registry['collectorTarget']
                if target==registry['keeperTarget']:raise Fault('keeper_cannot_be_collection_target','local')
                self.db.execute('INSERT INTO sn_sources VALUES(?,?,?,?,?,?)',(source['id'],encode(source),hashlib.sha256(canonical(source)).hexdigest(),source.get('family'),int(bool(source.get('enabled'))),target))
            for key,job in journal['jobs'].items():
                if job.get('key')!=key:raise Fault('frozen_native_job_key_changed','local')
                self._insert(job)
            for sid,body in journal.get('datasetBlocks',{}).items():self.hold('source',sid,'',body)
            for field,scope in [('gridDateHolds','date'),('listingSnapshotHolds','snapshot')]:
                for sid,anchors in journal.get(field,{}).items():
                    for anchor,body in anchors.items():self.hold(scope,sid,anchor,body)
            for key,body in journal.get('pageGroups',{}).items():
                total=body.get('total')
                if type(total) is not int or total<0:raise Fault('legacy_page_total_unverified','local')
                self.db.execute('INSERT INTO sn_page_groups VALUES(?,?,?,?,?)',(key,total,len(body.get('keys',[])),int(bool(body.get('complete'))),encode(body)))
                self.db.executemany('INSERT OR IGNORE INTO sn_page_keys VALUES(?,?,NULL)',[(key,k) for k in body.get('keys',[])])
                self.db.executemany('INSERT OR IGNORE INTO sn_pages VALUES(?,?,NULL,NULL)',[(key,p) for p in body.get('pages',[])])
            self.set_setting('registry_metadata',{k:v for k,v in registry.items() if k!='datasets'})
            self.set_setting('last_seed_day',journal.get('lastSeedDay'))
            self.set_setting('legacy_non_job_metadata',{k:v for k,v in journal.items() if k not in {'jobs','pageGroups','datasetBlocks','gridDateHolds','listingSnapshotHolds'}})
            self.set_setting('completed_since_snapshot',0);self.set_setting('completed_since_attachment',0)
            self.db.execute('INSERT INTO sn_imports VALUES(?,?,?,?)',(sha256,len(journal['jobs']),source_ref,time.time()))

    def seed(self,sid,kind,params,priority=100,**extra):
        key=key_for(sid,kind,params)
        if self.db.execute('SELECT 1 FROM sn_jobs WHERE key=?',(key,)).fetchone():return key
        job={'key':key,'datasetId':sid,'kind':kind,'params':params,'status':'pending','priority':priority,'createdAt':stamp(),**extra}
        verified_key(job);self._insert(job);return key

    def update(self,job,*,state=None,consumed=None,validation=None,artifact_sha=None,reason=None):
        if state is not None:job={**job,'status':state}
        if reason is not None:job={**job,'error':reason}
        row=self.db.execute('SELECT consumed,validation,artifact_sha FROM sn_jobs WHERE key=?',(job['key'],)).fetchone()
        if row is None:raise Fault('direct_task_not_seeded','local')
        self.db.execute('UPDATE sn_jobs SET body=?,status=?,consumed=?,finished_at=?,validation=?,artifact_sha=?,reason=? WHERE key=?',
            (encode(job),job['status'],row['consumed'] if consumed is None else int(consumed),job.get('finishedAt'),validation or row['validation'],artifact_sha or row['artifact_sha'],reason,job['key']))

    def filters(self,job,values):
        job={**job,'nativeFilters':values};self.update(job);return job

    def reviewed_identity(self,job):
        token=job.get('manualReview')
        if not token:return None
        from pathlib import Path
        row=self.db.execute('SELECT * FROM sn_exception_reviews WHERE review_id=? AND job_key=?',(token,job['key'])).fetchone()
        if not row or not row['approval']:raise Fault('manual_exception_review_missing','local',unsent=True)
        proof=json.loads(row['proof']);source=self.source(job['datasetId'])
        if proof.get('kind')!='menu_label_renamed_before_native_handler' or job['kind']!='table' or hashlib.sha256(canonical(source)).hexdigest()!=proof['repaired_source_sha256']:
            raise Fault('manual_exception_review_binding_changed','local',unsent=True)
        raw=Path(proof['original_receipt']).read_bytes();receipt=json.loads(raw)
        if hashlib.sha256(raw).hexdigest()!=proof['original_receipt_sha256'] or not receipt.get('invokeError') or receipt.get('observedRequests') or receipt.get('responses') or receipt.get('pending') or receipt.get('failures'):
            raise Fault('manual_exception_review_not_proven_unsent','local',unsent=True)
        return token

    def quarantine(self,job,reason,*,consumed=True,source_hold=False,scope=None,proof=None):
        with self.transaction():
            result={**job,'status':'held_direct_exception','error':reason,'finishedAt':stamp(),'exception':proof or {}}
            self.update(result,consumed=consumed,reason=reason)
            if source_hold:self.hold('source',job['datasetId'],'',{'jobKey':job['key'],'reason':reason,'proof':proof})
            if scope:self.hold(scope,job['datasetId'],job['params'].get('date') if scope=='date' else job['params'].get('snapshotDate'),{'jobKey':job['key'],'reason':reason,'proof':proof})
        return result

    def next_job(self,*,today=None,download_attachments=True,now=None):
        today=today or datetime.now(TZ).date().isoformat();now=time.time() if now is None else now
        if date.fromisoformat(today).isoformat()!=today:raise ValueError('canonical_day_required')
        # Finite SQLite index reads. No decoding/hash/lookup for the whole queue.
        where="""j.status='pending' AND j.consumed=0 AND s.enabled=1
          AND NOT EXISTS(SELECT 1 FROM sn_holds h WHERE (h.scope='source' AND h.source_id=j.source_id) OR (h.scope='date' AND h.source_id=j.source_id AND h.anchor=j.task_date) OR (h.scope='snapshot' AND h.source_id=j.source_id AND h.anchor=j.snapshot_date) OR (h.scope='target' AND h.source_id=s.target_id))"""
        base='SELECT j.* FROM sn_jobs j JOIN sn_sources s ON s.id=j.source_id WHERE '+where
        if not download_attachments:base+=" AND j.kind NOT IN ('download','monthly_attachment','monthly_context')"
        lanes=[]
        if self.setting('completed_since_snapshot',0)>=BATCH_LIMIT:lanes.append("j.kind IN ('catalogue','listing','file_listing') AND j.snapshot_date=?")
        if self.setting('completed_since_attachment',0)>=BATCH_LIMIT and download_attachments:lanes.append("j.kind IN ('download','monthly_attachment','monthly_context')")
        lanes.append('1=1')
        batch=self.setting('source_batch',{})
        recent=0<=now-batch.get('at',0)<600
        preferred=batch.get('source_id') if recent else None
        limit=ATTACHMENT_BATCH_LIMIT if batch.get('kind') in ATTACHMENTS else BATCH_LIMIT
        continuing=bool(preferred and batch.get('count',0)<limit)
        for lane in lanes:
            params=[today] if '?' in lane else []
            # Current snapshot continuations keep their short freshness window.
            # Otherwise reuse one source for at most 64 confirmations, then
            # yield to another source. Every existing eligibility guard remains.
            affinity=''
            if preferred:
                affinity=',CASE WHEN j.source_id=? THEN '+('0 ELSE 1' if continuing else '1 ELSE 0')+' END'
                params.append(preferred)
            sql=base+' AND ('+lane+") ORDER BY CASE WHEN j.kind='monthly_attachment' AND j.snapshot_date='"+today+"' THEN 0 WHEN j.kind IN ('listing','file_listing') AND j.page>1 AND j.snapshot_date='"+today+"' THEN 1 ELSE 2 END"+affinity+',j.priority,COALESCE(j.task_date,json_extract(j.params,\'$.startDate\')),j.page,j.created_at,j.key LIMIT 128'
            while True:
                rows=self.db.execute(sql,params).fetchall()
                if not rows:break
                changed=False
                for row in rows:
                    job=json.loads(row['body']);kind=job['kind'];p=job['params']
                    if kind in SNAPSHOTS|{'monthly_attachment','monthly_context'} and p.get('snapshotDate')!=today:
                        self.quarantine(job,'capture_snapshot_date_expired',consumed=False);changed=True;continue
                    if self.ledger.consumed_identity(row['identity_key']) or row['file_key'] and self.ledger.consumed_file(row['file_key']):
                        self.quarantine(job,'previous_identity_preserved_no_replay');changed=True;continue
                    if kind in {'download','monthly_attachment'} and self.db.execute('SELECT 1 FROM sn_file_claims WHERE file_id=?',(p.get('fileId'),)).fetchone():
                        if not getattr(self.ledger,'reviewed_unissued_file',lambda j:False)(job):
                            self.quarantine(job,'previous_file_identity_preserved_no_replay');changed=True;continue
                    if kind in {'listing','file_listing'} and p.get('page',1)>1:
                        parent=self.db.execute('SELECT body,finished_at FROM sn_jobs WHERE source_id=? AND kind=? AND snapshot_date=? AND page=? AND status=\'saved\' ORDER BY finished_at DESC',(job['datasetId'],kind,today,p['page']-1)).fetchall()
                        scope={k:v for k,v in p.items() if k!='page'};matches=[r for r in parent if {k:v for k,v in json.loads(r['body'])['params'].items() if k!='page'}==scope]
                        try:fresh=len(matches)==1 and 0<=now-datetime.fromisoformat(matches[0]['finished_at'].replace('Z','+00:00')).timestamp()<=120
                        except (TypeError,ValueError):fresh=False
                        if not fresh:
                            self.quarantine(job,'prior_snapshot_page_missing_or_not_recent',consumed=False);changed=True;continue
                    return job
                if not changed:break
        return None

    def expand(self,job,values):
        if not values or len(values)!=len(set(values)):raise Fault('verified_dimension_values_required','site_data')
        with self.transaction():
            children=[self.seed(job['datasetId'],'table',{**job['params'],'valueType':v},job.get('priority',100),parentJob=job['key']) for v in values]
            result={**job,'status':'expanded','children':children,'finishedAt':stamp(),'expansion':{'valueTypes':values,'reason':'all_registered_native_value_types'}}
            for child in children:self.db.execute('INSERT OR IGNORE INTO sn_relations VALUES(?,?,?)',(job['key'],child,'dimension'))
            self.update(result,consumed=False)
        return result

    def precheck_page(self,job,result):
        family=self.source(job['datasetId']).get('family')
        if job['kind'] not in {'table','listing','file_listing'} or family=='weighted_price':return None
        total=result.get('server_total_rows');keys=result.get('keys',[]);group=group_for(job)
        if type(total) is not int or total<0:return {'code':'native_page_total_missing','scope':'source'}
        scope='date' if family=='grid' else 'snapshot' if job['params'].get('snapshotDate') else 'source'
        previous=self.db.execute('SELECT total FROM sn_page_groups WHERE key=?',(group,)).fetchone()
        if previous and previous['total']!=total:return {'code':'validated_pagination_total_changed','scope':scope,'prior':previous['total'],'observed':total}
        if len(keys)!=len(set(keys)) or any(self.db.execute('SELECT 1 FROM sn_page_keys WHERE group_key=? AND record_key=? AND (job_key IS NULL OR job_key!=?)',(group,k,job['key'])).fetchone() for k in keys):return {'code':'duplicate_source_identity_across_page','scope':scope}
        if job['datasetId']=='1643899919922237440':
            rows=result.get('rows',[])
            if not isinstance(rows,list) or any(not unit_row_matches(r,job['params'].get('date')) for r in rows):return {'code':'unit_date_type_identity_mismatch_or_duplicate','scope':scope}
            natural=[encode([r['date'],r['MEAS_TYPE'],r['UNIT_ID']]) for r in rows]
            if len(natural)!=len(set(natural)) or any(self.db.execute('SELECT 1 FROM sn_unit_keys WHERE group_key=? AND natural_key=? AND (job_key IS NULL OR job_key!=?)',(group,k,job['key'])).fetchone() for k in natural):return {'code':'unit_date_type_identity_mismatch_or_duplicate','scope':scope}
        return None

    def settle(self,job,result,validation,raw_sha,raw_path,raw_bytes,*,captured_at=None):
        """Validate page groups and atomically confirm parent/children/artifacts."""
        source=self.source(job['datasetId']);family=source.get('family');keys=result.get('keys',[]);p=job['params'];kind=job['kind'];now=time.time();finished=stamp()
        if captured_at is not None:
            if not isinstance(captured_at,(int,float)) or not 0<captured_at<=now:raise Fault('offline_capture_time_invalid','local')
            finished=datetime.fromtimestamp(captured_at,timezone.utc).isoformat().replace('+00:00','Z')
        with self.transaction():
            existing=self.db.execute('SELECT status,artifact_sha FROM sn_jobs WHERE key=?',(job['key'],)).fetchone()
            if existing and existing['status'] in {'saved','empty'} and existing['artifact_sha']==raw_sha:return True
            mismatch=self.precheck_page(job,result)
            if mismatch:
                scope=mismatch['scope'];self.quarantine(job,mismatch['code'],scope=scope if scope!='source' else None,source_hold=scope=='source',proof=mismatch);return False
            if kind in {'table','listing','file_listing'} and family!='weighted_price':
                group=group_for(job);total=result.get('server_total_rows')
                if type(total) is not int or total<0:raise Fault('native_page_total_missing','site_data')
                self.db.execute('INSERT OR IGNORE INTO sn_page_groups VALUES(?,?,0,0,?)',(group,total,encode({'source_id':job['datasetId'],'params':p})))
                self.db.executemany('INSERT INTO sn_page_keys VALUES(?,?,?)',[(group,k,job['key']) for k in keys]);self.db.execute('INSERT OR REPLACE INTO sn_pages VALUES(?,?,?,?)',(group,p['page'],job['key'],captured_at if captured_at is not None else now))
                count=self.db.execute('SELECT count(*) FROM sn_page_keys WHERE group_key=?',(group,)).fetchone()[0]
                self.db.execute('UPDATE sn_page_groups SET complete=? WHERE key=?',(int(count==total),group))
                if p['page']*p['size']<total:
                    next_key=self.seed(job['datasetId'],kind,{**p,'page':p['page']+1},job.get('priority',120),nativeFilters=job.get('nativeFilters',{}),**({'parentJob':job['parentJob']} if job.get('parentJob') else {}))
                    if job.get('parentJob'):self.db.execute('INSERT OR IGNORE INTO sn_relations VALUES(?,?,?)',(job['parentJob'],next_key,'dimension'))
                if job['datasetId']=='1643899919922237440':
                    natural=[encode([r.get('date'),r.get('MEAS_TYPE'),r.get('UNIT_ID')]) for r in result.get('rows',[])]
                    if any(not unit_row_matches(r,p.get('date')) for r in result.get('rows',[])) or len(natural)!=len(set(natural)) or any(self.db.execute('SELECT 1 FROM sn_unit_keys WHERE group_key=? AND natural_key=?',(group,k)).fetchone() for k in natural):raise Fault('unit_date_type_identity_mismatch_or_duplicate','site_data')
                    self.db.executemany('INSERT INTO sn_unit_keys VALUES(?,?,?)',[(group,k,job['key']) for k in natural])
            if kind=='catalogue':
                for record in result.get('files',[]):
                    if not self.db.execute('SELECT 1 FROM sn_file_claims WHERE file_id=?',(record['fileId'],)).fetchone():self.seed(job['datasetId'],'download',record,300)
            rows=result.get('rows',[]);count=len(rows) if isinstance(rows,list) else rows if type(rows) is int else 0
            final={**job,'status':result['outcome'],'finishedAt':finished,'validation':validation,'rows':result.get('saved_rows',count),'bytes':raw_bytes}
            self.update(final,consumed=True,validation=validation,artifact_sha=raw_sha)
            self.db.execute('INSERT OR IGNORE INTO sn_artifacts VALUES(?,?,?,?,?)',(raw_sha,str(raw_path),raw_bytes,now,'pmos_direct'))
            self.db.execute('INSERT OR REPLACE INTO sn_job_artifacts VALUES(?,?,?,?,?,?)',(job['key'],raw_sha,job['datasetId'],result['outcome'],validation,now))
            if kind in {'download','monthly_attachment'}:self.db.execute('INSERT OR REPLACE INTO sn_file_claims VALUES(?,?,?,?)',(p['fileId'],job['key'],result['outcome'],encode({'validation':validation,'sha256':raw_sha})))
            if kind=='listing':
                for row in result.get('rows',[]):self.db.execute('INSERT OR IGNORE INTO sn_records VALUES(?,?,?,?)',(job['datasetId'],row['guid'],encode(row),job['key']))
            parent=job.get('parentJob')
            if parent:
                states=[r[0] for r in self.db.execute('SELECT j.status FROM sn_relations r JOIN sn_jobs j ON j.key=r.child WHERE r.parent=?',(parent,))]
                if states and all(x in {'saved','empty'} for x in states):
                    body=self.job(parent);body.update(status='assembled',finishedAt=finished);self.update(body,consumed=True)
            self.set_setting('completed_since_snapshot',0 if kind in SNAPSHOTS else self.setting('completed_since_snapshot',0)+1)
            self.set_setting('completed_since_attachment',0 if kind in ATTACHMENTS else self.setting('completed_since_attachment',0)+1)
            batch=self.setting('source_batch',{})
            count=batch.get('count',0)+1 if batch.get('source_id')==job['datasetId'] else 1
            self.set_setting('source_batch',{'source_id':job['datasetId'],'kind':kind,'count':count,'at':now})
            if result.get('kind')=='pdf_original' and result.get('pdfParse',{}).get('status')=='pending':
                self.db.execute('INSERT OR IGNORE INTO sn_pdf_jobs(job_key,source_id,sha256,validation,status,created_at) VALUES(?,?,?,?,?,?)',(job['key'],job['datasetId'],raw_sha,validation,'pending',now))
        return True

    def seed_daily(self,today=None):
        today=today or datetime.now(TZ).date().isoformat();last=self.setting('last_seed_day')
        if last==today:return 0
        first=date.fromisoformat(last) if last else date.fromisoformat(today)-timedelta(days=1);end=date.fromisoformat(today)-timedelta(days=1)
        dates=[]
        while first<=end:dates.append(first.isoformat());first+=timedelta(days=1)
        if len(dates)>3660:raise Fault('daily_catchup_range_requires_review','local')
        before=self.db.total_changes
        with self.transaction():
            for row in self.db.execute('SELECT body FROM sn_sources WHERE enabled=1').fetchall():
                d=json.loads(row[0]);sid=d['id'];family=d.get('family')
                if d.get('sampleOnly'):self.seed(sid,'table',{'date':today,'page':1,'size':20,'currentSample':True,'snapshotDate':today},90)
                elif family=='directory':self.seed(sid,'catalogue',{'snapshotDate':today},110)
                elif family=='disclosure_list':self.seed(sid,'listing',{'snapshotDate':today,'page':1,'size':d['nativeListing']['pageSize']},110)
                elif family=='file_list':self.seed(sid,'file_listing',{'snapshotDate':today,'page':1,'size':d['nativeFileList']['pageSize']},110)
                else:
                    for day in dates:
                        params={'date':day} if family=='weighted_price' else {'startDate':day,'endDate':None,'page':1,'size':10} if family=='weekly' else {'date':day,'page':1,'size':50 if family=='grid' else 1 if family=='curve' else 20}
                        self.seed(sid,'table',params,100)
            self.set_setting('last_seed_day',today)
        return self.db.total_changes-before

    def stats(self):return dict(self.db.execute('SELECT status,count(*) FROM sn_jobs GROUP BY status'))
