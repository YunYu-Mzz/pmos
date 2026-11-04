// Capture a native child target before navigation. No URLs are constructed and
// all unrelated paused targets are resumed. Business bodies are retained only
// after recursively checking for credential-shaped fields.
import fs from 'node:fs/promises';
import path from 'node:path';
import crypto from 'node:crypto';
import {setTimeout as delay} from 'node:timers/promises';
const hash=b=>crypto.createHash('sha256').update(b).digest('hex');
const secretKey=k=>/^(?:token|ticket|access_token|authorization|cookie|password|sessionid)$/i.test(k);
function secret(value,depth=0){if(depth>15)return true;if(Array.isArray(value))return value.some(x=>secret(x,depth+1));if(value&&typeof value==='object')return Object.entries(value).some(([k,v])=>secretKey(k)||secret(v,depth+1));if(typeof value==='string'&&/^(?:https?:\/\/|\/)/i.test(value)){try{const u=new URL(value,'https://pmos.sn.sgcc.com.cn');return [...u.searchParams.keys()].some(secretKey);}catch{return false;}}return false;}
export function safeChildPath(url){try{const u=new URL(url);if(u.origin!=='https://pmos.sn.sgcc.com.cn')return null;return{path:u.pathname,queryKeys:[...u.searchParams.keys()]};}catch{return null;}}
export function isExpectedChild(parentTarget,info,preexisting=new Set()){return info?.type==='page'&&info.openerId===parentTarget&&!preexisting.has(info.targetId);}
export async function initializeAttachedChild(send,parentTarget,info,childSession){
 const expected=isExpectedChild(parentTarget,info);
 try{if(expected){await send('Runtime.enable',{},childSession);await send('Network.enable',{},childSession);}}
 finally{await send('Runtime.runIfWaitingForDebugger',{},childSession);}
 return expected;
}
export async function captureNativeChild({api,parentTarget,parentSession,runParent,folder,inspectChild}){
 const children=[],pending=new Map(),events=[],errors=[],tasks=new Set(),knownSessions=new Set(),pausedSessions=new Set();let last=Date.now(),enabled=false;
 const targets=await api.send('Target.getTargets');const preexisting=new Set((targets.targetInfos||[]).map(x=>x.targetId));
 const track=p=>{tasks.add(p);p.finally(()=>tasks.delete(p));};
 const listener=e=>{
  let m;try{m=JSON.parse(e.data);}catch{return;}const p=m.params||{};
  if(m.method==='Target.attachedToTarget'&&!m.sessionId){
   const sid=p.sessionId,info=p.targetInfo;pausedSessions.add(sid);const expected=isExpectedChild(parentTarget,info,preexisting);
   if(expected){knownSessions.add(sid);children.push({targetId:info.targetId,sessionId:sid,openerId:info.openerId,initialType:info.type});}
   track((async()=>{try{await initializeAttachedChild(api.send,parentTarget,expected?info:{...info,openerId:null},sid);pausedSessions.delete(sid);}catch(error){errors.push({stage:'attach',error:String(error),sessionId:sid});}})());return;
  }
  if(!knownSessions.has(m.sessionId))return;
  if(m.method==='Network.requestWillBeSent'&&['Document','XHR','Fetch'].includes(p.type)){
   const safe=safeChildPath(p.request.url);if(!safe)return;const item={sessionId:m.sessionId,requestId:p.requestId,...safe,method:p.request.method,requestType:p.type,startedAt:new Date().toISOString(),requestBodySha256:p.request.postData?hash(p.request.postData):null};pending.set(m.sessionId+':'+p.requestId,item);last=Date.now();
  }else if(m.method==='Network.responseReceived'){
   const item=pending.get(m.sessionId+':'+p.requestId);if(item){item.status=p.response.status;item.mimeType=p.response.mimeType;last=Date.now();}
  }else if(m.method==='Network.loadingFailed'){
   const key=m.sessionId+':'+p.requestId,item=pending.get(key);if(item){pending.delete(key);events.push({...item,transportError:p.errorText,finishedAt:new Date().toISOString()});last=Date.now();}
  }else if(m.method==='Network.loadingFinished'){
   const key=m.sessionId+':'+p.requestId,item=pending.get(key);if(!item)return;pending.delete(key);last=Date.now();
   track((async()=>{try{
    const credential=/auth|sso|token|ticket|login|password/i.test(item.path);
    if(credential||!/json/i.test(item.mimeType||'')){events.push({...item,bodyRetained:false,credentialRedacted:credential,finishedAt:new Date().toISOString()});return;}
    const body=await api.send('Network.getResponseBody',{requestId:item.requestId},item.sessionId),raw=body.base64Encoded?Buffer.from(body.body,'base64'):Buffer.from(body.body);let parsed;try{parsed=JSON.parse(raw);}catch{throw Error('child_json_body_invalid');}
    if(secret(parsed)){events.push({...item,bodyRetained:false,credentialRedacted:true,businessStatus:parsed?.status,businessCode:parsed?.code,finishedAt:new Date().toISOString()});return;}
    const digest=hash(raw);await fs.mkdir(folder,{recursive:true,mode:0o700});const dest=path.join(folder,digest+'.response');try{await fs.writeFile(dest,raw,{flag:'wx',mode:0o600});}catch(e){if(e.code!=='EEXIST')throw e;if(hash(await fs.readFile(dest))!==digest)throw Error('child_archive_hash_conflict');}
    events.push({...item,bodyRetained:true,responseFile:dest,sha256:digest,bytes:raw.length,businessStatus:parsed?.status,businessCode:parsed?.code,businessMessage:parsed?.message,finishedAt:new Date().toISOString()});
   }catch(error){errors.push({...item,stage:'archive',error:String(error)});}finally{last=Date.now();}})());
  }
 };
 api.ws.addEventListener('message',listener);
 let result,thrown,autoAttachDisabled=false;
 try{
  await api.send('Target.setAutoAttach',{autoAttach:true,waitForDebuggerOnStart:true,flatten:true,filter:[{type:'page',exclude:false},{exclude:true}]});enabled=true;
  // Root auto-attach includes existing tabs. Release those before invoking the
  // native action so the parent and unrelated tabs do not remain paused.
  await delay(250);if(tasks.size)await Promise.allSettled([...tasks]);
  if(errors.length||pausedSessions.size)throw Error('Existing targets could not be released');
  const parentReceipt=await runParent();
  const until=Date.now()+30000;
  while(Date.now()<until&&(!children.length||pending.size||tasks.size||Date.now()-last<2000))await delay(250);
  if(tasks.size)await Promise.allSettled([...tasks]);
  let childInspection=null;if(inspectChild&&children.length===1&&!pending.size&&!errors.length){try{childInspection=await inspectChild(children[0]);}catch(error){errors.push({stage:'passive_child_inspection',error:String(error)});}}
  result={parentReceipt,children,events,pending:[...pending.values()],errors,childInspection,initialChildCaptureObserved:children.length===1&&events.some(x=>x.requestType==='Document')};
 }catch(error){thrown=error;
 }finally{
  if(tasks.size)await Promise.allSettled([...tasks]);
  for(const sid of [...pausedSessions])try{await api.send('Runtime.runIfWaitingForDebugger',{},sid);pausedSessions.delete(sid);}catch(error){errors.push({stage:'cleanup_resume',sessionId:sid,error:String(error)});}
  if(enabled)try{await api.send('Target.setAutoAttach',{autoAttach:false,waitForDebuggerOnStart:false,flatten:true});autoAttachDisabled=true;}catch(error){errors.push({stage:'cleanup_disable',error:String(error)});}
  if(tasks.size)await Promise.allSettled([...tasks]);
  for(const sid of [...pausedSessions])try{await api.send('Runtime.runIfWaitingForDebugger',{},sid);pausedSessions.delete(sid);}catch(error){errors.push({stage:'cleanup_resume',sessionId:sid,error:String(error)});}
  api.ws.removeEventListener('message',listener);
 }
 if(thrown)throw thrown;
 result.cleanup={autoAttachDisabled,remainingPausedSessions:[...pausedSessions]};
 result.completeObservation=children.length===1&&!pending.size&&!errors.length&&autoAttachDisabled&&!pausedSessions.size;
 return result;
}
