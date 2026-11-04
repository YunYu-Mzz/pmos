// PMOS direct browser transport: one persistent loopback CDP connection,
// one serialized readonly operation. No old queue or inbox is imported.
import fs from 'node:fs/promises';
import path from 'node:path';
import crypto from 'node:crypto';
import readline from 'node:readline';
import {fileURLToPath} from 'node:url';
import {setTimeout as delay} from 'node:timers/promises';
import {compile} from '../adapters/shaanxi_plan.mjs';
import {captureNativeChild} from '../adapters/shaanxi_child_capture.mjs';
import {planCandidates} from '../adapters/shaanxi_monthly_policy.mjs';
const ORIGIN='https://pmos.sn.sgcc.com.cn';
const hash=bytes=>crypto.createHash('sha256').update(bytes).digest('hex');
const now=()=>new Date().toISOString();
const stable=v=>Array.isArray(v)?'['+v.map(stable).join(',')+']':v&&typeof v==='object'?'{'+Object.keys(v).sort().map(k=>JSON.stringify(k)+':'+stable(v[k])).join(',')+'}':JSON.stringify(v);
const credentialKey=k=>/^(token|ticket|access_token|authorization|cookie|password|sessionid)$/i.test(k);
function credential(v,depth=0){
 if(depth>20)return true;
 if(Array.isArray(v))return v.some(x=>credential(x,depth+1));
 if(v&&typeof v==='object')return Object.entries(v).some(([k,x])=>credentialKey(k)||credential(x,depth+1));
 if(typeof v==='string'&&/^(https?:\/\/|\/)/.test(v))try{return [...new URL(v,ORIGIN).searchParams.keys()].some(credentialKey);}catch{}
 return false;
}
async function atomic(file,value){
 await fs.mkdir(path.dirname(file),{recursive:true,mode:0o700});const tmp=file+'.'+crypto.randomUUID()+'.tmp',f=await fs.open(tmp,'wx',0o600);
 try{await f.writeFile(JSON.stringify(value));await f.sync();}finally{await f.close();}
 await fs.rename(tmp,file);const d=await fs.open(path.dirname(file),'r');try{await d.sync();}finally{await d.close();}
}
async function bytesFile(file,bytes){
 await fs.mkdir(path.dirname(file),{recursive:true,mode:0o700});let f;
 try{f=await fs.open(file,'wx',0o600);}catch(e){if(e.code!=='EEXIST')throw e;if(hash(await fs.readFile(file))!==hash(bytes))throw Error('immutable_archive_conflict');return;}
 try{await f.writeFile(bytes);await f.sync();}finally{await f.close();}
 const d=await fs.open(path.dirname(file),'r');try{await d.sync();}finally{await d.close();}
}
function safePath(url){try{const u=new URL(url);return u.origin===ORIGIN?u.pathname:null;}catch{return null;}}
function frameUrl(frame){return frame.url.includes('#')?frame.url:frame.url+(frame.urlFragment||'');}
function authSignal(item,body){return item.status===401||[body?.status,body?.code,body?.errorCode].some(x=>Number(x)===401);}
function safeError(error){return /^(direct_|native_|monthly_|immutable_|cdp_|official_|download_)/.test(error?.message||'')?error.message:'direct_browser_operation_failed';}

export class DirectBrowser {
 constructor(config){
  this.config=config;this.root=path.resolve(config.instance);this.sharedRoot=path.resolve(config.source_root);this.seq=0;this.pendingMethods=new Map();this.sessions=new Map();this.targets=new Set(config.collection_targets);this.active=null;this.late=new Map();this.closed=false;this.downloads=new Map();this.frameMaps=new Map();this.heldTargets=new Set(config.held_targets||[]);this.unresolved=new Map();
  if(this.targets.has(config.keeper_target)||!this.targets.size)throw Error('direct_collection_targets_invalid');
  this.nonBusiness=new Set(config.non_business_paths||[]);
  this.intervalMs=Math.max(1000,Number(config.response_interval_ms||1000));this.lastNativeEnd=0;
  if(!Number.isFinite(this.intervalMs)||this.intervalMs>60000)throw Error('direct_response_interval_invalid');
  if([...this.nonBusiness].some(p=>typeof p!=='string'||!p.startsWith('/')||/auth|sso|token|password/i.test(p)))throw Error('direct_auxiliary_policy_invalid');
 }
 async connect(endpoint){
  if(!/^ws:\/\/127\.0\.0\.1:\d{2,5}\/devtools\/browser\/[a-zA-Z0-9-]+$/.test(endpoint))throw Error('direct_loopback_endpoint_required');
  this.ws=new WebSocket(endpoint);
  this.ws.addEventListener('message',event=>this.message(JSON.parse(event.data)));
  this.ws.addEventListener('close',()=>{this.closed=true;for(const p of this.pendingMethods.values()){clearTimeout(p.timer);p.reject(Error('cdp_connection_closed'));}this.pendingMethods.clear();});
  await new Promise((resolve,reject)=>{this.ws.addEventListener('open',resolve,{once:true});this.ws.addEventListener('error',()=>reject(Error('cdp_connection_failed')),{once:true});});
  return{phase:'direct_browser_connected',version:await this.send('Browser.getVersion'),websiteRequestsIssued:0};
 }
 send=(method,params={},sessionId,timeoutMs=30000)=>new Promise((resolve,reject)=>{
  if(this.closed||this.ws.readyState!==WebSocket.OPEN){reject(Error('cdp_connection_closed'));return;}
  const id=++this.seq,started=Date.now(),timer=setTimeout(()=>{
   this.pendingMethods.delete(id);
   const targetId=[...this.sessions].find(([,sid])=>sid===sessionId)?.[0];
   // Keep bounded transport metadata only: expressions, URLs, response bodies
   // and authentication values never enter this diagnostic record.
   void atomic(path.join(this.root,'state/direct_cdp_timeouts',hash(process.pid+':'+id+':'+started)+'.json'),{
    at:now(),method,targetId,timeoutMs,elapsedMs:Date.now()-started,
    turnKey:this.active?.key,oldOperationNotReplayed:true,
   }).catch(()=>{});
   const error=Error('cdp_timeout');error.cdpMethod=method;reject(error);
  },timeoutMs);
  this.pendingMethods.set(id,{resolve,reject,timer});this.ws.send(JSON.stringify({id,method,params,...(sessionId?{sessionId}:{})}));
 });
 async session(target){
  if(!this.targets.has(target)||target===this.config.keeper_target)throw Error('direct_target_not_admitted');
  const {targetInfo:t}=await this.send('Target.getTargetInfo',{targetId:target});
  if(t.type!=='page'||new URL(t.url).origin!==ORIGIN)throw Error('official_collection_page_required');
  // Chrome can suspend the background renderer while the separate keeper
  // remains healthy. Wake only the admitted collection page, without loading
  // a URL or invoking any old business handler, before touching its renderer.
  await this.send('Target.activateTarget',{targetId:target});
  let sid=this.sessions.get(target);
  if(!sid){sid=(await this.send('Target.attachToTarget',{targetId:target,flatten:true})).sessionId;}
  await this.send('Page.setWebLifecycleState',{state:'active'},sid);
  await this.send('Emulation.setFocusEmulationEnabled',{enabled:true},sid);
  if(!this.sessions.has(target)){
   await this.send('Runtime.enable',{},sid);await this.send('Network.enable',{},sid);await this.send('Page.enable',{},sid);this.sessions.set(target,sid);
  }
  return sid;
 }
 async evaluate(target,expression){
  // This entry point is reserved for the pure compiled DOM inspections.
  // A stuck read is known unsent and reconnects the channel; issued native
  // handlers retain the separate durable receipt/no-replay path below.
  const sid=await this.session(target),r=await this.send('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true,timeout:5000},sid,7000);
  if(r.exceptionDetails)throw Error('native_readonly_context_unavailable');return r.result.value;
 }
 async signalAuth(item,body){
  const signal={reason:'auth_expired',at:now(),key:item.turnKey,scope:'pmos_direct',requestId:item.requestId,path:item.path,httpStatus:item.status,businessStatus:body?.status,businessCode:body?.code};
  await atomic(path.join(this.root,'state/direct_auth_signal.json'),signal);
  // The existing keeper honors this small shared authentication marker. It
  // has no dependence on the old task frontier and performs no re-login.
  const file=path.join(this.sharedRoot,'state/session_block.json');let prior;try{prior=JSON.parse(await fs.readFile(file,'utf8'));}catch(e){if(e.code!=='ENOENT')throw e;}
  await atomic(file,prior?.reason==='auth_expired'?prior:{...signal,...(prior?{previous:prior}:{})});
 }
 lateFinished(sid,item){
  const origin=this.unresolved.get(sid);if(!origin||origin.key!==item.turnKey)return;
  origin.remaining.delete(item.requestId);origin.terminal.add(item.requestId);const revision=++origin.revision;
  if(!origin.remaining.size&&origin.expectedSeen)setTimeout(async()=>{
   if(origin.revision!==revision||origin.remaining.size)return;
   const proof={turnKey:origin.key,targetId:origin.targetId,allObservedRequestsTerminal:true,terminalRequestIds:[...origin.terminal],quietMs:500,at:now(),oldResultNotAdmitted:true,replayed:false};
   await atomic(path.join(this.root,'state/direct_target_settled',origin.key+'.json'),proof).catch(()=>{});
  },500);
 }
 async acknowledgeSettled(proof){
  const file=path.join(this.root,'state/direct_target_settled',proof.turnKey+'.json'),stored=JSON.parse(await fs.readFile(file,'utf8'));
  const origins=[...this.unresolved.values()].filter(x=>x.targetId===proof.targetId);
  if(stable(stored)!==stable(proof)||!stored.allObservedRequestsTerminal||origins.some(x=>x.remaining.size||x.key!==proof.turnKey))throw Error('direct_late_terminal_proof_mismatch');
  this.heldTargets.delete(proof.targetId);for(const [sid,o]of this.unresolved)if(o.key===proof.turnKey)this.unresolved.delete(sid);return{phase:'target_released_after_original_terminal_observation',replayed:false};
 }
 message(v){
  if(v.id){const p=this.pendingMethods.get(v.id);if(!p)return;clearTimeout(p.timer);this.pendingMethods.delete(v.id);v.error?p.reject(Error('cdp_method_failed')):p.resolve(v.result);return;}
  const p=v.params||{},a=this.active,sid=v.sessionId;
  if(v.method==='Page.frameNavigated'){
   const map=this.frameMaps.get(sid)||new Map();map.set(p.frame.id,{url:frameUrl(p.frame),root:!p.frame.parentId,loaderId:p.frame.loaderId});this.frameMaps.set(sid,map);return;
  }
  if(v.method==='Page.navigatedWithinDocument'){
   const frame=this.frameMaps.get(sid)?.get(p.frameId);if(frame)frame.url=p.url;return;
  }
  if(v.method==='Browser.downloadWillBegin'&&a?.download&&a.download.frames.has(p.frameId)){
   if(a.download.guid&&a.download.guid!==p.guid){a.download.ambiguous=true;return;}
   a.download.guid=p.guid;a.download.filename=p.suggestedFilename;a.download.officialBlob=typeof p.url==='string'&&p.url.startsWith('blob:'+ORIGIN+'/');this.downloads.set(p.guid,a.download);return;
  }
  if(v.method==='Browser.downloadProgress'){
   const d=this.downloads.get(p.guid);if(d)Object.assign(d,{state:p.state,totalBytes:p.totalBytes,receivedBytes:p.receivedBytes});return;
  }
  if(v.method==='Network.requestWillBeSent'&&this.unresolved.has(sid)&&(!a||a.sid!==sid)&&['XHR','Fetch','Document'].includes(p.type)){
   const origin=this.unresolved.get(sid),pathname=safePath(p.request.url);if(!pathname)return;
   const item={requestId:p.requestId,path:pathname,method:p.request.method,requestType:p.type,frameId:p.frameId,startedAt:now(),turnKey:origin.key,kind:pathname===origin.expectedPath?'data':/auth|sso|token|ticket/i.test(pathname)?'native_sdk_auth':'unreviewed'};
   this.late.set(sid+':'+p.requestId,item);origin.remaining.add(p.requestId);origin.expectedSeen||=pathname===origin.expectedPath;origin.revision++;return;
  }
  if(v.method==='Network.requestWillBeSent'&&a&&sid===a.sid&&['XHR','Fetch','Document'].includes(p.type)){
   const pathname=safePath(p.request.url);if(!pathname)return;
   const item={requestId:p.requestId,path:pathname,method:p.request.method,requestType:p.type,frameId:p.frameId,startedAt:now(),turnKey:a.key};
   const sdk=/auth|sso|ticket|gettoken|\/token(?:\/|$)/i.test(pathname),primary=pathname===a.plan.expectedPath;
   item.kind=primary?'data':sdk?'native_sdk_auth':this.nonBusiness.has(pathname)?'native_auxiliary':p.type==='Document'?'native_document':a.plan.discovery?'data':'unreviewed';
   if(['data','unreviewed'].includes(item.kind)&&a.plan.context){
    const frame=this.frameMaps.get(sid)?.get(p.frameId);let matches=false;
    if(frame){try{matches=a.plan.context.kind==='root'?frame.root:[frame.url,p.documentURL].filter(Boolean).some(url=>{const u=new URL(url);return u.origin===ORIGIN&&u.hash.slice(1).split('?')[0]===a.plan.context.route;});}catch{}}
    if(!matches)item.verifyFrameAtCompletion=true;
   }
   a.requests.push(item);a.pending.set(p.requestId,item);a.last=Date.now();return;
  }
  let item=a&&a.sid===sid?a.pending.get(p.requestId):null,late=false;
  if(!item){item=this.late.get(sid+':'+p.requestId);late=!!item;}
  if(!item)return;
  if(v.method==='Network.responseReceived'){
   Object.assign(item,{status:p.response.status,mimeType:p.response.mimeType});
   if(item.status===401)void this.signalAuth(item).catch(()=>{});
   if(a&&a.sid===sid)a.last=Date.now();return;
  }
  if(v.method==='Network.loadingFailed'){
   const record={...item,error:p.errorText,finishedAt:now()};
   if(late){this.late.delete(sid+':'+p.requestId);void atomic(path.join(this.root,'state/direct_late',hash(sid+':'+p.requestId)+'.json'),{...record,transportCompleted:true,lateResultNotAdmitted:true}).then(()=>this.lateFinished(sid,item)).catch(()=>{});}
   else{a.pending.delete(p.requestId);(item.kind==='native_auxiliary'?a.warnings:a.failures).push(record);a.last=Date.now();}
   return;
  }
  if(v.method!=='Network.loadingFinished')return;
  item.finishedAt=now();
  if(late){this.late.delete(sid+':'+p.requestId);void this.readBody(item,sid,null,true).catch(()=>atomic(path.join(this.root,'state/direct_late',hash(sid+':'+p.requestId)+'.json'),{...item,transportCompleted:true,bodyUnavailable:true,lateResultNotAdmitted:true})).then(()=>this.lateFinished(sid,item)).catch(()=>{});return;}
  a.pending.delete(p.requestId);a.last=Date.now();const task=this.readBody(item,sid,a,false).catch(()=>{a.failures.push({...item,error:'direct_response_body_unavailable',transportCompleted:true});}).finally(()=>{a.bodyJobs.delete(task);a.last=Date.now();});a.bodyJobs.add(task);
 }
 async readBody(item,sid,a,late){
  if(item.verifyFrameAtCompletion&&a&&!late){
   const {frameTree}=await this.send('Page.getFrameTree',{},sid);let actual;
   const visit=n=>{if(n.frame.id===item.frameId)actual={url:frameUrl(n.frame),root:!n.frame.parentId};for(const c of n.childFrames||[])visit(c);};visit(frameTree);
   let matches=false;try{const u=new URL(actual?.url);matches=a.plan.context.kind==='root'?actual.root:u.origin===ORIGIN&&u.hash.slice(1).split('?')[0]===a.plan.context.route;}catch{}
   if(!matches){a.foreignRequests.push({...item,transportCompleted:true,notAttributedToThisTask:true});return;}
   item.frameVerifiedAtCompletion=true;
  }
  if(item.kind==='native_auxiliary'||item.kind==='native_document'){
   const record={...item,bodyRetained:false};if(a)(item.kind==='native_auxiliary'?a.warnings:a.responses).push(record);return;
  }
  let raw;
  const binary=item.kind==='data'&&item.status===200&&a?.download;
  if(binary){
   // Reuse the native browser download instead of copying its large body
   // through CDP a second time merely to probe for a JSON error envelope.
   // If no official blob appears, retain the existing error/auth inspection.
   const blobUntil=Date.now()+250;
   while(a.download.state==='not_started'&&Date.now()<blobUntil)await delay(25);
   // A business error may use the nominal binary content type. Inspect JSON
   // error envelopes before waiting for a download the page will never start.
   if(!a.download.officialBlob)try{const b=await this.send('Network.getResponseBody',{requestId:item.requestId},sid);const candidate=b.base64Encoded?Buffer.from(b.body,'base64'):Buffer.from(b.body);if(!candidate.length)raw=candidate;else{JSON.parse(candidate);raw=candidate;}}catch{}
   if(!raw){
   const until=Date.now()+30000;while(Date.now()<until&&a.download.state!=='completed'&&!a.download.ambiguous)await delay(100);
   const d=a.download;
   if(d.ambiguous||d.state!=='completed'||!d.officialBlob||!d.guid||d.receivedBytes!==d.totalBytes||d.totalBytes<=0||d.totalBytes>256*1024*1024)throw Error('download_completion_not_verified');
   raw=await fs.readFile(path.join(this.root,'downloads',d.guid));if(raw.length!==d.totalBytes)throw Error('download_size_mismatch');
   }
  }else{
   const body=await this.send('Network.getResponseBody',{requestId:item.requestId},sid);raw=body.base64Encoded?Buffer.from(body.body,'base64'):Buffer.from(body.body);
  }
  if(a?.sealed){late=true;a=null;}
  const record={...item,bytes:raw.length,sha256:hash(raw)};let body;try{body=JSON.parse(raw);}catch{}
  if(authSignal(item,body))await this.signalAuth(item,body);
  record.businessStatus=body?.status;record.businessCode=body?.code;record.errorCode=body?.errorCode;record.businessSuccess=body?.success;
  if(['Success','操作成功','查询成功'].includes(body?.message))record.businessMessage=body.message;
  if(item.kind==='native_sdk_auth'||credential(body))record.bodyRedacted=true;
  else if(item.kind==='data'&&!late){
   if(/text\/html/i.test(item.mimeType||'')||/^\s*<!doctype html|^\s*<html/i.test(raw.toString('utf8',0,100))){record.bodyRedacted=true;record.securityDocument=true;}
   else{
    const target=path.join(a.folder,record.sha256+'.response');await bytesFile(target,raw);record.responseFile=target;
   if(binary){if(a.plan.expectedFilename&&a.download.filename!==a.plan.expectedFilename)throw Error('download_filename_mismatch');record.nativeDownload={guid:a.download.guid,bytes:raw.length,officialBlobOriginVerified:true,profileHistoryRead:false};}
   }
  }else record.bodyRetained=false;
  if(late){await atomic(path.join(this.root,'state/direct_late',hash(sid+':'+item.requestId)+'.json'),{...record,transportCompleted:true,lateResultNotAdmitted:true});return;}
  a.responses.push(record);
 }
 async acquireLease(key){
  const dir=path.join(this.sharedRoot,'state/turn.lock');await fs.mkdir(dir);const lease={owner:'pmos_direct',pid:process.pid,nonce:crypto.randomUUID(),key,at:now()};
  try{await atomic(path.join(dir,'lease.json'),lease);}catch(error){await fs.rm(dir,{recursive:true});throw error;}
  const writes=new Set(),timer=setInterval(()=>{const p=atomic(path.join(dir,'lease.json'),{...lease,at:now()}).catch(()=>{});writes.add(p);p.finally(()=>writes.delete(p));},2000);
  return{dir,lease,timer,writes};
 }
 async releaseLease(owned){clearInterval(owned.timer);await Promise.allSettled([...owned.writes]);let current;try{current=JSON.parse(await fs.readFile(path.join(owned.dir,'lease.json'),'utf8'));}catch{return;}if(current.nonce!==owned.lease.nonce)throw Error('direct_native_lease_changed');await fs.rm(owned.dir,{recursive:true});}
 async runNative(plan,existingLease=null){
  if(plan.readOnly!==true||!this.targets.has(plan.targetId)||plan.targetId===this.config.keeper_target)throw Error('direct_readonly_plan_required');
  if(this.heldTargets.has(plan.targetId))return{phase:'not_sent',reason:'direct_target_awaiting_terminal_evidence',websiteRequestsIssued:0};
  if(this.active)throw Error('direct_operation_already_active');
  const key=hash(stable(plan.identity)),folder=path.join(this.root,'native_turns',key),issued=path.join(folder,'issued.json'),receiptFile=path.join(folder,'receipt.json');
  try{await fs.access(issued);throw Error('direct_identity_already_issued');}catch(e){if(e.code!=='ENOENT')throw e;}
  let keeperEnd=0;try{const keeper=JSON.parse(await fs.readFile(path.join(this.sharedRoot,'state/keeper/last_success.json'),'utf8'));keeperEnd=Date.parse(keeper.finishedAt)||0;}catch(e){if(e.code!=='ENOENT')throw e;}
  const wait=Math.max(this.lastNativeEnd,keeperEnd)+this.intervalMs-Date.now();if(wait>0)await delay(wait);
  let owned=existingLease;try{if(!owned)owned=await this.acquireLease(key);}catch(e){if(e.code==='EEXIST')return{phase:'not_sent',reason:'native_turn_busy',websiteRequestsIssued:0};throw e;}
  let a,receipt,downloadConfigured=false;
  try{
   let block;try{block=JSON.parse(await fs.readFile(path.join(this.sharedRoot,'state/session_block.json'),'utf8'));}catch(e){if(e.code!=='ENOENT')throw e;}
   if(['auth_expired','security_verification','rate_limited'].includes(block?.reason))throw Error('direct_shared_session_protected');
   if(block?.reason==='unknown_request_result'&&/^[a-f0-9]{64}$/.test(block.key||'')){
    const old=JSON.parse(await fs.readFile(path.join(this.sharedRoot,'state/native_turns',block.key,'receipt.json'),'utf8'));
    if(old.targetId===plan.targetId&&old.pending?.length)throw Error('direct_target_has_legacy_pending_request');
   }
   const sid=await this.session(plan.targetId);
   a={key,folder,sid,plan,pending:new Map(),responses:[],failures:[],warnings:[],requests:[],foreignRequests:[],bodyJobs:new Set(),last:Date.now(),frames:new Set(),download:null};
   const {frameTree}=await this.send('Page.getFrameTree',{},sid),frameMap=new Map();const walk=n=>{a.frames.add(n.frame.id);frameMap.set(n.frame.id,{url:frameUrl(n.frame),root:!n.frame.parentId,loaderId:n.frame.loaderId});for(const c of n.childFrames||[])walk(c);};walk(frameTree);this.frameMaps.set(sid,frameMap);
   if(plan.download){
    const frames=new Set([...frameMap].filter(([,f])=>!plan.context||plan.context.kind==='root'?f.root:new URL(f.url).hash.slice(1).split('?')[0]===plan.context.route).map(([id])=>id));
    if(!frames.size)throw Error('download_frame_context_missing');
    await fs.mkdir(path.join(this.root,'downloads'),{recursive:true,mode:0o700});await this.send('Browser.setDownloadBehavior',{behavior:'allowAndName',downloadPath:path.join(this.root,'downloads'),eventsEnabled:true});downloadConfigured=true;a.download={state:'not_started',frames};
   }
   receipt={identity:plan.identity,key,targetId:plan.targetId,expectedPath:plan.expectedPath,observedSessionId:sid,startedAt:now(),phase:'issued',pending:[],responses:[],failures:[],readOnly:true,directPMOS:true};
   await atomic(issued,{...receipt,expressionSha256:hash(plan.expression)});a.issued=true;this.active=a;
   const invoked=await this.send('Runtime.evaluate',{expression:plan.expression,returnByValue:true,awaitPromise:true,...(plan.nativeUserGesture?{userGesture:true}:{})},sid);a.invokeReturned=true;
   if(invoked.exceptionDetails){
    receipt.invokeError=true;
    const detail=String(invoked.exceptionDetails.exception?.description||'').split('\n')[0];
    receipt.invokeException=/^Error: (?:Native|Observed|Actual|Pinned|Exact|Unreviewed|monthly_)/.test(detail)?detail.slice(0,240):'native_handler_exception_details_not_retained';
    throw Error('native_component_invocation_failed');
   }
   receipt.invokeResult=invoked.result.value;
   const started=Date.now(),limit=plan.timeoutMs||300000,quiet=plan.discovery?300:100;
   while(Date.now()-started<limit){
    const primary=a.responses.some(x=>x.kind==='data'&&x.path===plan.expectedPath),outcome=plan.discovery&&!plan.requirePrimary?a.responses.some(x=>x.kind==='data'||x.kind==='native_document'):primary;
    const blocking=[...a.pending.values()].filter(x=>x.kind!=='native_auxiliary').length;
    const primaryFailed=a.failures.some(x=>x.path===plan.expectedPath);
    if((outcome||primaryFailed||plan.childNavigation)&&!blocking&&!a.bodyJobs.size&&Date.now()-a.last>=quiet){a.observedTerminal=true;break;}
    await delay(25);
   }
   if(plan.postReadExpression){
    const post=await this.send('Runtime.evaluate',{expression:plan.postReadExpression,returnByValue:true,awaitPromise:true},sid);if(post.exceptionDetails)throw Error('native_post_read_unavailable');receipt.postRead=post.result.value;
   }
   receipt.phase='captured_requires_adapter_validation';
  }catch(error){
   receipt=receipt||{key,targetId:plan.targetId,identity:plan.identity,expectedPath:plan.expectedPath,startedAt:now(),readOnly:true,directPMOS:true};receipt.phase='held';receipt.error=safeError(error);
   if(error.cdpMethod)receipt.cdpTimeoutMethod=error.cdpMethod;
   if(!a?.issued)receipt.provenUnsent=true;
  }finally{
   if(a){
    a.sealed=true;receipt.responses=[...a.responses];receipt.failures=[...a.failures];receipt.auxiliaryWarnings=[...a.warnings];
    receipt.pending=[...a.pending.values()].filter(x=>x.kind!=='native_auxiliary');receipt.auxiliaryPending=[...a.pending.values()].filter(x=>x.kind==='native_auxiliary');
    receipt.observedRequests=a.requests;receipt.foreignFrameRequests=a.foreignRequests;receipt.finishedAt=now();
    if(receipt.pending.length||a.bodyJobs.size||a.failures.length||a.responses.some(x=>x.kind==='unreviewed'||x.securityDocument))receipt.phase='held';
    receipt.targetUncertain=!!(a.issued&&(receipt.pending.length||a.bodyJobs.size||!a.invokeReturned||receipt.invokeError||!a.observedTerminal));
    if(receipt.targetUncertain)receipt.phase='held';
    receipt.bodyReadsPending=a.bodyJobs.size;
    if(receipt.targetUncertain)this.heldTargets.add(plan.targetId);
    if(receipt.targetUncertain&&a.invokeReturned&&!receipt.invokeError&&!a.bodyJobs.size){this.unresolved.set(a.sid,{key,targetId:plan.targetId,expectedPath:plan.expectedPath,expectedSeen:a.requests.some(x=>x.path===plan.expectedPath),remaining:new Set([...a.pending.keys()]),terminal:new Set(a.responses.concat(a.failures).map(x=>x.requestId)),revision:0});}
    // Native SDK aborts are advisory only when the exact business body did
    // complete; the pure validator verifies the linked evidence again.
    const primary=a.responses.filter(x=>x.kind==='data'&&x.path===plan.expectedPath&&x.status===200&&x.responseFile);
    if(receipt.phase==='held'&&!receipt.pending.length&&primary.length===1&&a.failures.length&&a.failures.every(x=>x.path==='/px-common-authcenter/sso/token'&&['net::ERR_ABORTED','net::ERR_NETWORK_CHANGED'].includes(x.error))){
     receipt.phase='captured_requires_adapter_validation';receipt.blockingFailures=[];receipt.warningPolicy={continueWhenSdkAbortedAndBusinessCompleted:true,continueWhenSdkNetworkChangedAndBusinessCompleted:true};receipt.warnings=a.failures.map(x=>({code:x.error==='net::ERR_NETWORK_CHANGED'?'native_sdk_network_changed_business_completed':'native_sdk_auth_aborted_business_completed',requestId:x.requestId,error:x.error,businessEvidence:[{path:primary[0].path,sha256:primary[0].sha256}]}));
    }
    for(const item of a.pending.values())this.late.set(a.sid+':'+item.requestId,{...item,turnKey:key});
   }
   this.active=null;
   if(downloadConfigured)await this.send('Browser.setDownloadBehavior',{behavior:'default',eventsEnabled:false}).catch(()=>{});
   if(!existingLease&&owned)await this.releaseLease(owned);
   if(receipt){receipt.directory=folder;receipt.finishedAt??=now();await atomic(receiptFile,receipt);if(a?.issued)this.lastNativeEnd=Date.now();}
  }
  return receipt;
 }
 async monthlyChild(plan,inspectExpression){
  const owned=await this.acquireLease(hash(stable(plan.identity)));
  try{
   const parentSession=await this.session(plan.targetId),folder=path.join(this.root,'monthly_details',plan.identity.recordGuid);
   const result=await captureNativeChild({api:{send:this.send,ws:this.ws},parentTarget:plan.targetId,parentSession,folder,runParent:()=>this.runNative(plan,owned),inspectChild:async child=>{
    const {targetInfo}=await this.send('Target.getTargetInfo',{targetId:child.targetId});if(new URL(targetInfo.url).origin!==ORIGIN)throw Error('official_monthly_child_required');this.targets.add(child.targetId);this.sessions.set(child.targetId,child.sessionId);
    const r=await this.send('Runtime.evaluate',{expression:inspectExpression,returnByValue:true,awaitPromise:true},child.sessionId);if(r.exceptionDetails)throw Error('monthly_child_context_invalid');return r.result.value;
   }});
   for(const child of result.children||[])this.sessions.delete(child.targetId); // auto-attach cleanup detached these sessions
   for(const item of result.events||[])if(authSignal(item,{status:item.businessStatus,code:item.businessCode}))await this.signalAuth({...item,turnKey:result.parentReceipt?.key},{status:item.businessStatus,code:item.businessCode});
   await atomic(path.join(folder,'capture.json'),result);return result;
  }finally{await this.releaseLease(owned);}
 }
 async cancelIsolatedTarget(key){
  if(!/^[a-f0-9]{64}$/.test(key)||this.active)throw Error('direct_cancel_isolation_invalid');
  const receipt=JSON.parse(await fs.readFile(path.join(this.root,'native_turns',key,'receipt.json'),'utf8')),target=receipt.targetId;
  if(receipt.phase!=='held'||!this.targets.has(target)||target===this.config.keeper_target||!this.heldTargets.has(target))throw Error('direct_cancel_exact_held_target_required');
  let block;try{block=JSON.parse(await fs.readFile(path.join(this.sharedRoot,'state/session_block.json'),'utf8'));}catch(e){if(e.code!=='ENOENT')throw e;}
  if(block?.reason==='auth_expired')throw Error('direct_authentication_requires_manual_login');
  let owned;try{owned=await this.acquireLease('cancel_'+key);}catch(error){if(error.code==='EEXIST')return{phase:'waiting_turn_lock',websiteRequestsIssued:0};throw error;}
  try{
   const sid=await this.session(target),file=path.join(this.root,'state/direct_target_cancelled',key+'.json');
   await atomic(file,{phase:'prepared',turnKey:key,targetId:target,reason:'skip_isolated_nonauth_operation_without_replay',at:now()});
   // Page.stopLoading cancels existing navigations/resource fetches. It
   // neither refreshes the page nor resends an old business request.
   await this.send('Page.stopLoading',{},sid);await delay(1000);
   const proof={phase:'isolated_request_cancelled_allow_new',turnKey:key,targetId:target,method:'Page.stopLoading',websiteRequestsIssued:0,originalResultNotAdmitted:true,oldIdentityReplayed:false,at:now()};
   await atomic(file,proof);this.heldTargets.delete(target);for(const [id,o]of this.unresolved)if(o.targetId===target)this.unresolved.delete(id);return proof;
  }finally{await this.releaseLease(owned);}
 }
 async close(){if(this.active)throw Error('direct_operation_inflight');this.ws.close();}
}

async function main(){
 let browser;const input=readline.createInterface({input:process.stdin,crlfDelay:Infinity});
 for await(const line of input){
  let command;
  try{
   command=JSON.parse(line);if(!/^[a-zA-Z0-9_-]{1,120}$/.test(command.id||''))throw Error('direct_command_id_invalid');
   let result;
   if(command.op==='initialize'){if(browser)throw Error('direct_already_initialized');browser=new DirectBrowser(command.config);result=await browser.connect(command.endpoint);}
   else if(command.op==='compile')result=compile({...command.input,registry:command.input.registry||browser?.config.registry});
   else if(command.op==='monthly_candidates')result=planCandidates(command.input);
   else if(!browser)throw Error('direct_not_initialized');
   else if(command.op==='inspect')result=await browser.evaluate(command.targetId,command.expression);
   else if(command.op==='acknowledge_settled')result=await browser.acknowledgeSettled(command.proof);
   else if(command.op==='cancel_isolated_target')result=await browser.cancelIsolatedTarget(command.turnKey);
   else if(command.op==='native')result=await browser.runNative(command.plan);
   else if(command.op==='monthly_child')result=await browser.monthlyChild(command.plan,command.inspectExpression);
   else if(command.op==='close'){await browser.close();result={phase:'closed'};}
   else throw Error('direct_unknown_operation');
   process.stdout.write(JSON.stringify({id:command.id,ok:true,result})+'\n');
  }catch(error){process.stdout.write(JSON.stringify({id:command?.id,ok:false,error:command?.op==='compile'?'native_contract_plan_rejected':safeError(error)})+'\n');}
 }
 if(browser&&!browser.active)await browser.close().catch(()=>{});
}
if(process.argv[1]&&path.resolve(process.argv[1])===fileURLToPath(import.meta.url))await main();
