import test from 'node:test';import assert from 'node:assert/strict';import vm from 'node:vm';
import {compile} from '../pmos/adapters/shaanxi_plan.mjs';
function fixture(){
 const queryData=function(){this.called=true;},getIpc=function(){};
 const filters={marketId:'PXBSX',menuId:'source',period:{b:2,a:1}};
 const methods={queryData:String(queryData),getIpc:String(getIpc)};
 const c={state:{infoCode:'fixture',viewId:'source',marketInfoTwo:'PXBSX',queryParam:{data:filters,pageInfo:{pageNum:1,pageSize:20}}},methods};
 const dataset={family:'disclosure_list',url:'fixture',nativeListing:{infoCode:'fixture',guid:'source',marketId:'PXBSX',filters,pageSize:20},sampleContract:{components:[c],columns:[]}};
 const plan=compile({op:'query',dataset,job:{kind:'listing',params:{page:2,size:20}},contract:{components:[c],columns:[]}});
 const v={infoCode:'fixture',viewId:'source',marketInfoTwo:'PXBSX',resultValue:[],queryParam:{data:{period:{a:1,b:2},menuId:'source',marketId:'PXBSX'},pageInfo:{pageNum:1,pageSize:20}},$options:{methods:{queryData,getIpc}},queryData};
 return{plan,v,run:()=>vm.runInNewContext(plan.expression,{document:{querySelectorAll:()=>[{__vue__:v}]}})};
}
test('native listing accepts equal filter values with reordered nested keys',()=>{const f=fixture();const r=f.run();assert.equal(f.v.called,true);assert.equal(r.nativeQuery.pageInfo.pageNum,2);assert.equal(r.nativeQuery.data.period.b,2);});
test('native listing still rejects an altered filter value before any native call',()=>{const f=fixture();f.v.queryParam.data.period.b=3;assert.throws(f.run,/source filters changed/);assert.equal(f.v.called,undefined);});
test('optional undefined keys follow the actual JSON request serialization',()=>{const f=fixture();f.v.queryParam.data.optional=undefined;f.v.queryParam.data.period.optional=undefined;f.run();assert.equal(f.v.called,true);});
test('a stale widget page size is restored to the registered native size',()=>{const f=fixture();f.v.queryParam.pageInfo.pageSize=10;const r=f.run();assert.equal(f.v.called,true);assert.equal(r.nativeQuery.pageInfo.pageSize,20);});
