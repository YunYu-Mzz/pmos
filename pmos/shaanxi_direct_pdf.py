"""Durable, single-process offline PDF exports; no browser or network access."""
from __future__ import annotations
import asyncio,fcntl,hashlib,json,os,signal,sys,time
from pathlib import Path
from .shaanxi_transport import atomic,inside,load


def parse_one(instance,key):
    """Only an already confirmed original may enter this offline worker."""
    import sqlite3
    from .adapters.shaanxi_native.queue_validate import export
    root=Path(instance).resolve()
    if len(key)!=64 or any(c not in '0123456789abcdef' for c in key):raise ValueError('pdf_job_key_required')
    db=sqlite3.connect('file:'+str(root/'ledger.sqlite3')+'?mode=ro',uri=True);db.row_factory=sqlite3.Row
    try:
        row=db.execute('SELECT p.*,s.body FROM sn_pdf_jobs p JOIN sn_job_artifacts a ON a.job_key=p.job_key AND a.sha256=p.sha256 JOIN sn_sources s ON s.id=p.source_id WHERE p.job_key=?',(key,)).fetchone()
        if row is None:raise ValueError('pdf_not_confirmed_in_archive')
        source=json.loads(row['body']);validation=inside(root,row['validation']);v=load(validation)
    finally:db.close()
    dest=validation.parent;lock=(dest/'offline_parse.lock').open('a')
    try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:return 75
    try:
        raw=inside(root,v['responseFile']).read_bytes()
        if v.get('kind')!='pdf_original' or hashlib.sha256(raw).hexdigest()!=row['sha256'] or v.get('sha256')!=row['sha256']:raise ValueError('offline_pdf_original_changed')
        result_file=dest/'offline_parse_result.json';completed=load(result_file,{})
        if completed.get('sourceSha256')==row['sha256'] and completed.get('job_key')==key:return 0
        if v.get('pdfParse',{}).get('status')=='pending':
            started=time.time();export(v,dest,source)
            parsers={k:x.get('status','unknown') for k,x in v.items() if k.endswith('Parse') and k!='pdfParse' and isinstance(x,dict)}
            def review(x):
                if isinstance(x,dict):return x.get('status') in {'requires_review','parsed_with_review','failed','unsupported'} or any(review(y) for y in x.values())
                if isinstance(x,list):return any(review(y) for y in x)
                return False
            status='original_only' if not parsers else 'requires_review' if any(x not in {'parsed','parsed_tables','success','complete','passed','ok'} for x in parsers.values()) or any(review(x) for k,x in v.items() if k.endswith('Parse') and k!='pdfParse') else 'parsed'
            v['pdfParse']={'status':status,'sourceSha256':row['sha256'],'originalVerified':True,'parserStatuses':parsers,'started_at':started,'finished_at':time.time(),'offline':True}
            atomic(dest/'source_metadata.json',v);atomic(validation,v)
        atomic(result_file,{'job_key':key,'sourceSha256':row['sha256'],**v['pdfParse']})
        return 0
    finally:lock.close()


class DeferredPDFs:
    def __init__(self,store,instance):
        self.store=store;self.instance=Path(instance);self.process=None;self.job=None;self.started=None;self.error=None;self.retry_after=0

    async def stop_process(self):
        if self.process and self.process.returncode is None:
            try:os.killpg(self.process.pid,signal.SIGTERM)
            except ProcessLookupError:pass
            try:await asyncio.wait_for(self.process.wait(),3)
            except asyncio.TimeoutError:
                try:os.killpg(self.process.pid,signal.SIGKILL)
                except ProcessLookupError:pass
                await self.process.wait()

    def finish(self,status,error=None,result_ref=None):
        self.store.db.execute('UPDATE sn_pdf_jobs SET status=?,finished_at=?,error=?,result_ref=? WHERE job_key=?',(status,time.time(),error,str(result_ref) if result_ref else None,self.job['job_key']))
        if self.error:self.error.close()
        self.process=None;self.job=None;self.error=None

    async def tick(self):
        if self.process:
            if self.process.returncode is None and time.monotonic()-self.started<180:return
            if self.process.returncode is None:
                await self.stop_process();self.finish('requires_review','offline_parser_timeout_original_retained');return
            ref=Path(self.job['validation']).parent/'offline_parse_result.json';result=load(ref,{})
            if self.process.returncode==0 and result.get('job_key')==self.job['job_key'] and result.get('sourceSha256')==self.job['sha256']:
                self.finish(result['status'],result_ref=ref)
            elif self.process.returncode==75:
                self.finish('pending','offline_parser_lock_busy');self.retry_after=time.monotonic()+5;return
            else:self.finish('requires_review','offline_parser_exit_'+str(self.process.returncode))
        if time.monotonic()<self.retry_after:return
        row=self.store.db.execute("SELECT * FROM sn_pdf_jobs WHERE status IN ('pending','running') ORDER BY created_at LIMIT 1").fetchone()
        if not row:return
        self.job=dict(row);self.started=time.monotonic()
        folder=self.instance/'state/pdf_parser';folder.mkdir(exist_ok=True,parents=True,mode=0o700)
        self.error=(folder/(row['job_key']+'.stderr')).open('ab',buffering=0)
        try:
            self.process=await asyncio.create_subprocess_exec(sys.executable,'-B','-m','pmos.shaanxi_direct_pdf',str(self.instance),row['job_key'],stdout=asyncio.subprocess.DEVNULL,stderr=self.error,start_new_session=True)
            self.store.db.execute("UPDATE sn_pdf_jobs SET status='running',started_at=?,error=NULL WHERE job_key=?",(time.time(),row['job_key']))
        except Exception as exc:self.finish('requires_review','offline_parser_start_'+type(exc).__name__)

    async def close(self):
        if self.process:
            await self.stop_process();self.finish('pending','collector_graceful_shutdown_offline_resume_allowed')


if __name__=='__main__':
    try:os.nice(5)
    except OSError:pass
    sys.exit(parse_one(sys.argv[1],sys.argv[2]))
