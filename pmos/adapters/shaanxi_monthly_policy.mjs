// Pure policy for monthly detail/download queue planning. It never touches the
// website, the browser, or the queue; callers must verify the saved parent.
import crypto from 'node:crypto';

export const SOURCE='disclosure:8c15a8c7-3888-4e14-b50b-9b141891e56b';
export const DOWNLOAD_PATH='/px-settlement-infpubmeex/fileService/download';
const guid=/^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/;
const fileId=/^[nw][a-f0-9]{32}$/;
const digest=/^[a-f0-9]{64}$/;
const stable=x=>Array.isArray(x)?'['+x.map(stable).join(',')+']':x&&typeof x==='object'?'{'+Object.keys(x).sort().map(k=>JSON.stringify(k)+':'+stable(x[k])).join(',')+'}':JSON.stringify(x);
export const keyFor=x=>crypto.createHash('sha256').update(stable(x)).digest('hex');

function attachments(row){
 let items=row.attachment;
 if(typeof items==='string')try{items=JSON.parse(items||'[]');}catch{throw Error('invalid_native_attachments');}
 if(!Array.isArray(items))throw Error('invalid_native_attachments');
 return items;
}
function eligibleAttachment(a){return a&&typeof a.name==='string'&&a.name.length>4&&/\.pdf$/i.test(a.name)&&!/[\\/]/.test(a.name)&&fileId.test(a.id||'');}

// Snapshot and file identities are verified by the calling archive code.
// Only the source's own 1002 detail branch is eligible; 1001 remains held.
export function planCandidates({rows,parentJob,parentSha,sourcePage,snapshotDate,existingJobs,excludedFileIds=[]}){
 if(!Array.isArray(rows)||!digest.test(parentJob||'')||!digest.test(parentSha||'')||!Number.isInteger(sourcePage)||sourcePage<1||!/^\d{4}-\d{2}-\d{2}$/.test(snapshotDate||'')||!Array.isArray(existingJobs))throw Error('verified_parent_required');
 const used=new Set(existingJobs.map(x=>x?.params?.fileId).filter(Boolean));
 const excluded=new Set(excludedFileIds),seen=new Set(),jobs=[];
 for(const row of rows){
  if(String(row.viewWayCode)!=='1002'||!guid.test(row.guid||''))continue;
  for(const a of attachments(row)){
   if(!eligibleAttachment(a))continue;
   if(seen.has(a.id))throw Error('duplicate_native_file_identity');
   seen.add(a.id);
   if(used.has(a.id)||excluded.has(a.id))continue;
   const params={recordGuid:row.guid,fileId:a.id,fileName:a.name,nativeYear:String(row.fyear||''),nativeMonth:String(row.fmonth||''),parentJob,parentSha,sourcePage,snapshotDate};
   const identity={datasetId:SOURCE,kind:'monthly_attachment',params},key=keyFor(identity);
   jobs.push({key,...identity,priority:300,status:'pending',nativeSource:'verified_same_day_listing',createdAt:null});
  }
 }
 return jobs;
}

// The current native component and exact returned row must still agree. No
// inferred source fields and no direct/constructed URLs are accepted.
export function matchingNativeRow(row,job){
 if(!row||job?.datasetId!==SOURCE||job.kind!=='monthly_attachment'||String(row.viewWayCode)!=='1002'||row.guid!==job.params?.recordGuid)return false;
 let items;try{items=attachments(row);}catch{return false;}
 return items.filter(a=>eligibleAttachment(a)&&a.id===job.params.fileId&&a.name===job.params.fileName).length===1;
}

export function detailIdentity(job){
 if(job?.datasetId!==SOURCE||job.kind!=='monthly_attachment'||!guid.test(job.params?.recordGuid||'')||!fileId.test(job.params?.fileId||''))throw Error('monthly_job_identity_invalid');
 return {purpose:'first_monthly_settlement_detail_capture',source:SOURCE,recordGuid:job.params.recordGuid,parentJob:job.params.parentJob};
}

export function downloadIdentity(job){
 detailIdentity(job);
 if(!eligibleAttachment({id:job.params.fileId,name:job.params.fileName}))throw Error('monthly_filename_invalid');
 return {purpose:'first_monthly_settlement_attachment_download',source:SOURCE,recordGuid:job.params.recordGuid,
  params:{fileId:job.params.fileId,missionName:job.params.fileName.slice(0,-4)},queueJob:job.key};
}
