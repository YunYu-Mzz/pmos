"""Read-only throughput/accounting snapshot; never issues website requests."""
import argparse,collections,datetime,hashlib,json,pathlib,sqlite3,time

ap=argparse.ArgumentParser();ap.add_argument('--instance',required=True);ap.add_argument('--since',type=float);ap.add_argument('--window',type=float,default=600);ap.add_argument('--audit',action='store_true');args=ap.parse_args()
root=pathlib.Path(args.instance);now=time.time();since=args.since if args.since is not None else now-args.window
db=sqlite3.connect('file:'+str(root/'ledger.sqlite3')+'?mode=ro',uri=True);db.row_factory=sqlite3.Row;db.execute('BEGIN')
rows=list(db.execute('SELECT a.*,j.kind AS task_kind,j.source_id FROM attempts a JOIN sn_jobs j ON j.key=a.period WHERE a.kind=? AND a.finished_at>=? AND a.finished_at<=? ORDER BY a.id',('collect',since,now)))
counts=collections.Counter(r['outcome'] for r in rows);elapsed=now-since;completed=[r for r in rows if r['outcome'] in {'success','empty'}];saved=counts['success']
opens={r[0] for r in db.execute("SELECT DISTINCT attempt FROM sn_commands WHERE stage IN ('open','monthly_menu') AND prepared_at>=?",(since,))}
per_kind={}
for kind in sorted({r['task_kind'] for r in rows}):
    group=[r for r in rows if r['task_kind']==kind];done=[r for r in group if r['outcome'] in {'success','empty'}]
    per_kind[kind]={'outcomes':dict(collections.Counter(r['outcome'] for r in group)),'completed':len(done),'opened':sum(r['id'] in opens for r in done),'mean_task_seconds':round(sum(r['finished_at']-r['started_at'] for r in done)/len(done),3) if done else None}
pdf_exists=db.execute("SELECT 1 FROM sqlite_master WHERE name='sn_pdf_jobs'").fetchone()
out={'at':now,'at_local':datetime.datetime.fromtimestamp(now).isoformat(),'since':since,'elapsed_seconds':elapsed,'outcomes':dict(counts),'saved_per_hour':round(saved*3600/elapsed,2),'completed_per_hour':round(len(completed)*3600/elapsed,2),'unique_sources':len({r['source_id'] for r in completed}),'source_switches':sum(a['source_id']!=b['source_id'] for a,b in zip(completed,completed[1:])),'per_kind':per_kind,'pdf_queue':dict(db.execute('SELECT status,count(*) FROM sn_pdf_jobs GROUP BY status')) if pdf_exists else {},'frontier':dict(db.execute('SELECT status,count(*) FROM sn_jobs GROUP BY status')),'status':json.loads((root/'state/status.json').read_text()),'last_attempt_id':db.execute('SELECT max(id) FROM attempts').fetchone()[0],'unsettled':db.execute('SELECT count(*) FROM attempts WHERE outcome=?',('pending',)).fetchone()[0]}
if args.audit:
    checked=0;errors=[]
    for r in db.execute('SELECT a.*,j.params FROM sn_job_artifacts a JOIN sn_jobs j ON j.key=a.job_key WHERE a.finished_at>=?',(since,)):
        try:
            v=json.loads(pathlib.Path(r['validation']).read_text());raw=pathlib.Path(v['responseFile']).read_bytes()
            if hashlib.sha256(raw).hexdigest()!=r['sha256']:raise ValueError('raw_sha_mismatch')
            common=db.execute('SELECT status,artifact_sha256 FROM tasks WHERE period=?',(r['job_key'],)).fetchall()
            if len(common)!=1 or common[0]['status']!=r['outcome'] or common[0]['artifact_sha256']!=r['sha256']:raise ValueError('common_ledger_mismatch')
            if v.get('kind')=='pdf_original' and (not raw.startswith(b'%PDF-') or b'%%EOF' not in raw[-2048:]):raise ValueError('pdf_format_changed')
            p=json.loads(r['params']);day=p.get('date')
            if isinstance(v.get('rows'),list) and day:
                for row in v['rows']:
                    if isinstance(row,dict) and row.get('date') and row['date']!=day:raise ValueError('source_date_mismatch')
            checked+=1
        except Exception as exc:errors.append({'job_key':r['job_key'],'error':str(exc)})
    out['audit']={'checked':checked,'errors':errors}
print(json.dumps(out,ensure_ascii=False,indent=2));db.close()
