import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {DirectBrowser} from '../pmos/browser/shaanxi_direct.mjs';

async function fixture(t,{body={code:200,data:[0,-1,null]},failure=null,frame='root',status=200,late=false}={}){
 const temp=await fs.mkdtemp(path.join(os.tmpdir(),'sn_direct_'));t.after(()=>fs.rm(temp,{recursive:true,force:true}));const source=path.join(temp,'source'),instance=path.join(temp,'instance');await fs.mkdir(path.join(source,'state'),{recursive:true});
 const browser=new DirectBrowser({instance,source_root:source,keeper_target:'keeper',collection_targets:['data']});
 browser.session=async()=> 'sid';browser.send=async(method,p)=>{
  if(method==='Page.getFrameTree')return{frameTree:{frame:{id:'root',url:'https://pmos.sn.sgcc.com.cn/'}}};
  if(method==='Runtime.evaluate'){
   const emit=(method,params)=>browser.message({method,params,sessionId:'sid'});
   emit('Network.requestWillBeSent',{requestId:'1',frameId:frame,type:'XHR',request:{url:'https://pmos.sn.sgcc.com.cn/fixture',method:'POST'}});
   if(!late){emit('Network.responseReceived',{requestId:'1',response:{status,mimeType:'application/json'}});emit(failure?'Network.loadingFailed':'Network.loadingFinished',{requestId:'1',errorText:failure});}
   return{result:{value:{nativeQuery:{fixture:true}}}};
  }
  if(method==='Network.getResponseBody')return{body:JSON.stringify(body),base64Encoded:false};
  throw Error('unexpected_fake_method');
 };
 return {browser,source,instance,plan:{identity:{jobKey:'fixture',action:'query'},targetId:'data',readOnly:true,expectedPath:'/fixture',context:{kind:'root'},expression:'fixture_only',timeoutMs:400}};
}

test('completed response has exact raw bytes and a second issue is refused',async t=>{
 const {browser,plan}=await fixture(t);const r=await browser.runNative(plan);assert.equal(r.phase,'captured_requires_adapter_validation');assert.deepEqual(JSON.parse(await fs.readFile(r.responses[0].responseFile)),{code:200,data:[0,-1,null]});assert.equal(r.pending.length,0);await assert.rejects(()=>browser.runNative(plan),/already_issued/);
});
test('completed network failure is terminal for only the current task',async t=>{
 const {browser,plan,source}=await fixture(t,{failure:'net::ERR_NETWORK_CHANGED'});const r=await browser.runNative(plan);assert.equal(r.phase,'held');assert.equal(r.targetUncertain,false);assert.equal(r.failures[0].error,'net::ERR_NETWORK_CHANGED');assert.equal(browser.heldTargets.size,0);await assert.rejects(fs.access(path.join(source,'state/session_block.json')));
});
test('pending original keeps the target quarantined and saves late failure without reissue',async t=>{
 const {browser,plan,instance}=await fixture(t,{late:true});const r=await browser.runNative({...plan,timeoutMs:30});assert.equal(r.targetUncertain,true);assert.equal(browser.heldTargets.has('data'),true);browser.message({method:'Network.loadingFailed',sessionId:'sid',params:{requestId:'1',errorText:'net::ERR_ABORTED'}});await new Promise(r=>setTimeout(r,30));assert.equal((await fs.readdir(path.join(instance,'state/direct_late'))).length,1);assert.equal((await browser.runNative({...plan,identity:{jobKey:'next'}})).phase,'not_sent');await new Promise(r=>setTimeout(r,550));const proof=JSON.parse(await fs.readFile(path.join(instance,'state/direct_target_settled',r.key+'.json')));await browser.acknowledgeSettled(proof);assert.equal(browser.heldTargets.has('data'),false);assert.equal(JSON.parse(await fs.readFile(path.join(r.directory,'receipt.json'))).phase,'held');
});
test('unrelated iframe response cannot be attributed to this task',async t=>{
 const {browser,plan}=await fixture(t,{frame:'foreign'});const r=await browser.runNative({...plan,timeoutMs:30});assert.equal(r.responses.length,0);assert.equal(r.foreignFrameRequests.length,1);assert.equal(r.targetUncertain,true);
});
test('real business401 writes the original keeper-compatible authentication marker',async t=>{
 const {browser,plan,source}=await fixture(t,{body:{code:401},status:200});await browser.runNative(plan);const block=JSON.parse(await fs.readFile(path.join(source,'state/session_block.json')));assert.equal(block.reason,'auth_expired');assert.equal(block.scope,'pmos_direct');
});
test('credential shaped response is never retained as a business artifact',async t=>{
 const {browser,plan}=await fixture(t,{body:{status:0,data:{token:'fixture_secret_not_written'}}});const r=await browser.runNative(plan);assert.equal(r.responses[0].bodyRedacted,true);assert.equal(r.responses[0].responseFile,undefined);assert.ok(!JSON.stringify(r).includes('fixture_secret'));
});
test('keeper mutex busy proves zero send without an issued marker',async t=>{
 const {browser,plan,source,instance}=await fixture(t);await fs.mkdir(path.join(source,'state/turn.lock'));const r=await browser.runNative(plan);assert.equal(r.phase,'not_sent');assert.equal(r.websiteRequestsIssued,0);await assert.rejects(fs.access(path.join(instance,'native_turns')));
});
test('root and idle target events do not become an active request or crash the broker',async t=>{
 const {browser}=await fixture(t);assert.doesNotThrow(()=>browser.message({method:'Target.attachedToTarget',params:{sessionId:'other',targetInfo:{targetId:'other'}}}));assert.doesNotThrow(()=>browser.message({method:'Network.loadingFinished',params:{requestId:'unrelated'},sessionId:'other'}));assert.equal(browser.active,null);assert.equal(browser.late.size,0);
});
test('menu discovery waits for the actual API after the document has loaded',async t=>{
 const {browser,plan}=await fixture(t);const originalSend=browser.send;
 browser.send=async(method,p,sid)=>{
  if(method!=='Runtime.evaluate')return originalSend(method,p,sid);
  const emit=(method,params)=>browser.message({method,params,sessionId:'sid'});
  emit('Network.requestWillBeSent',{requestId:'document',frameId:'root',type:'Document',request:{url:'https://pmos.sn.sgcc.com.cn/menu',method:'GET'}});emit('Network.responseReceived',{requestId:'document',response:{status:200,mimeType:'text/html'}});emit('Network.loadingFinished',{requestId:'document'});
  setTimeout(()=>{void originalSend(method,p,sid);},350);return{result:{value:{nativeMenu:'fixture'}}};
 };
 const r=await browser.runNative({...plan,discovery:true,requirePrimary:true,timeoutMs:1200});assert.equal(r.phase,'captured_requires_adapter_validation');assert.ok(r.responses.some(x=>x.kind==='data'&&x.path==='/fixture'));
});
test('iframe hash navigation updates the frame binding before the API is attributed',async t=>{
 const {browser,plan}=await fixture(t);const send=browser.send;
 browser.send=async(method,p,sid)=>{if(method==='Runtime.evaluate')browser.message({method:'Page.navigatedWithinDocument',sessionId:'sid',params:{frameId:'root',url:'https://pmos.sn.sgcc.com.cn/#/native-route'}});return send(method,p,sid);};
 const r=await browser.runNative({...plan,context:{kind:'route_hash',route:'/native-route'}});assert.equal(r.phase,'captured_requires_adapter_validation');assert.equal(r.foreignFrameRequests.length,0);assert.ok(r.responses.some(x=>x.path==='/fixture'));
});
test('Chrome separate urlFragment field participates in the exact route binding',async t=>{
 const {browser,plan}=await fixture(t);const send=browser.send;
 browser.send=async(method,p,sid)=>method==='Page.getFrameTree'?{frameTree:{frame:{id:'root',url:'https://pmos.sn.sgcc.com.cn/',urlFragment:'#/native-route'}}}:send(method,p,sid);
 const r=await browser.runNative({...plan,context:{kind:'route_hash',route:'/native-route'}});assert.equal(r.phase,'captured_requires_adapter_validation');assert.equal(r.foreignFrameRequests.length,0);
});
test('isolated page cancellation keeps the old result held and sends no new query',async t=>{
 const {browser,plan}=await fixture(t,{late:true});const r=await browser.runNative({...plan,timeoutMs:30});let cancelled=0;const send=browser.send;
 browser.send=async(method,p,sid)=>{if(method==='Page.stopLoading'){cancelled++;return{};}return send(method,p,sid);};
 const proof=await browser.cancelIsolatedTarget(r.key);assert.equal(cancelled,1);assert.equal(proof.websiteRequestsIssued,0);assert.equal(proof.oldIdentityReplayed,false);assert.equal(browser.heldTargets.has('data'),false);assert.equal(JSON.parse(await fs.readFile(path.join(r.directory,'receipt.json'))).phase,'held');
});
test('native metadata and collection stages also honor the one-second response gap',async t=>{
 const {browser,plan}=await fixture(t);await browser.runNative(plan);const ended=Date.now();let invoked=0;const send=browser.send;
 browser.send=async(method,p,sid)=>{if(method==='Runtime.evaluate')invoked=Date.now();return send(method,p,sid);};
 await browser.runNative({...plan,identity:{jobKey:'next_stage',action:'query'}});assert.ok(invoked-ended>=990);
});

test('official completed blob is read once from disk without a giant CDP body probe',async t=>{
 const {browser,instance}=await fixture(t);await fs.mkdir(path.join(instance,'downloads'),{recursive:true});const raw=Buffer.from('%PDF-1.7\nfixture\n%%EOF');await fs.writeFile(path.join(instance,'downloads','fixture-guid'),raw);
 let probes=0;browser.send=async()=>{probes++;throw Error('unneeded_body_probe');};
 const a={download:{state:'completed',officialBlob:true,guid:'fixture-guid',receivedBytes:raw.length,totalBytes:raw.length,filename:'fixture.pdf'},plan:{expectedFilename:'fixture.pdf'},folder:path.join(instance,'response'),responses:[],sealed:false};
 await browser.readBody({requestId:'pdf',kind:'data',status:200,mimeType:'application/pdf'},'sid',a,false);
 assert.equal(probes,0);assert.equal(a.responses[0].bytes,raw.length);assert.equal(a.responses[0].nativeDownload.officialBlobOriginVerified,true);
});

test('nominal binary response containing a business401 is still inspected and blocks auth',async t=>{
 const {browser,source,instance}=await fixture(t);browser.send=async()=>({body:JSON.stringify({code:401}),base64Encoded:false});
 const a={download:{state:'not_started'},plan:{},folder:path.join(instance,'response'),responses:[],sealed:false};
 await browser.readBody({requestId:'denied',turnKey:'fixture',kind:'data',status:200,mimeType:'application/octet-stream'},'sid',a,false);
 assert.equal(JSON.parse(await fs.readFile(path.join(source,'state/session_block.json'))).reason,'auth_expired');
});

test('cached collection session is awakened before every renderer inspection without reload or query',async t=>{
 const {browser}=await fixture(t);delete browser.session;const calls=[];
 browser.send=async(method,params)=>{
  calls.push({method,params});
  if(method==='Target.getTargetInfo')return{targetInfo:{type:'page',url:'https://pmos.sn.sgcc.com.cn/'}};
  if(method==='Target.attachToTarget')return{sessionId:'cached'};
  if(method==='Runtime.evaluate')return{result:{value:{matched:true}}};
  return{};
 };
 await browser.evaluate('data','pure_inspection');await browser.evaluate('data','pure_inspection');
 assert.equal(calls.filter(x=>x.method==='Target.attachToTarget').length,1);
 assert.equal(calls.filter(x=>x.method==='Target.activateTarget').length,2);
 assert.equal(calls.filter(x=>x.method==='Page.setWebLifecycleState'&&x.params.state==='active').length,2);
 assert.equal(calls.filter(x=>x.method==='Emulation.setFocusEmulationEnabled'&&x.params.enabled).length,2);
 assert.equal(calls.filter(x=>x.method==='Runtime.evaluate').length,2);
 assert.ok(!calls.some(x=>/reload|navigate|fetch/i.test(x.method)));
 for(const x of calls.filter(x=>x.method==='Runtime.evaluate'))assert.equal(x.params.expression,'pure_inspection');
});

test('unadmitted and nonofficial targets cannot be activated by the wake path',async t=>{
 const {browser}=await fixture(t);delete browser.session;let activated=0;
 browser.send=async method=>{
  if(method==='Target.getTargetInfo')return{targetInfo:{type:'page',url:'https://unrelated.invalid/'}};
  if(method==='Target.activateTarget')activated++;
  return{};
 };
 await assert.rejects(()=>browser.session('keeper'),/not_admitted/);
 await assert.rejects(()=>browser.session('data'),/official_collection_page_required/);
 assert.equal(activated,0);
});

test('CDP timeout diagnostics contain the method without expression or parameter contents',async t=>{
 const {browser,instance}=await fixture(t);const direct=new DirectBrowser(browser.config);
 direct.ws={readyState:WebSocket.OPEN,send:()=>{}};
 await assert.rejects(()=>direct.send('Runtime.evaluate',{expression:'private_value_must_not_be_logged'},'sid',10),e=>e.message==='cdp_timeout'&&e.cdpMethod==='Runtime.evaluate');
 await new Promise(r=>setTimeout(r,60));
 const dir=path.join(instance,'state/direct_cdp_timeouts'),files=await fs.readdir(dir);assert.equal(files.length,1);
 const raw=await fs.readFile(path.join(dir,files[0]),'utf8'),proof=JSON.parse(raw);
 assert.equal(proof.method,'Runtime.evaluate');assert.equal(proof.timeoutMs,10);
 assert.equal(proof.oldOperationNotReplayed,true);assert.ok(!raw.includes('private_value'));
});
