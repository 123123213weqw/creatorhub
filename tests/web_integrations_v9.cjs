const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync('huilima_plugin/web/integrations-v9.js','utf8');
function fixture(){
 const calls=[],data=new Map(),element={textContent:''};let accepted=false,fail=false,sequence=0,form;
 const c={document:{documentElement:{dataset:{}},getElementById:()=>element},OPS_TOKEN:'',window:{},
  sessionStorage:{getItem:k=>data.get(k)||null,setItem:(k,v)=>data.set(k,v),removeItem:k=>data.delete(k)},
  crypto:{randomUUID:()=>('uuid-'+(++sequence))},Date,JSON,Number,Object,Error,encodeURIComponent,
  confirm:()=>accepted,opsForm:(...x)=>form=x,
  opsAPI:async(p,b,k)=>{calls.push({p,b,k});return {employee_mobile_acceptance:'excluded_by_owner',facts:{},pending:{real_crm_table_schema:true}}},
  api:async(p,o)=>{calls.push({p,o});if(fail)throw Error('fixture network unavailable');return {cache_present:false,refresh:{status:'completed',finished:1800000000,summary:{sections:{groups:{status:'ok',observed_count:0,complete:true,positive_sample_verified:false}}}}};}
 };
 vm.runInNewContext(source,c);
 return {c,calls,data,element,setAccept:x=>accepted=x,setFail:x=>fail=x,getForm:()=>form};
}
(async()=>{
 let f=fixture();assert.equal(f.calls.length,0);
 await f.c.window.v9LoadHandoff();assert.match(f.element.textContent,/按要求排除/);assert.match(f.element.textContent,/正式飞书客户表/);
 f=fixture();await f.c.window.v9BusinessRefresh();assert.equal(f.calls.length,0);
 f.c.document.documentElement.dataset.preview='true';f.setAccept(true);await f.c.window.v9BusinessRefresh();assert.equal(f.calls.length,0);
 f=fixture();f.c.OPS_TOKEN='fixture-team';f.setAccept(true);await f.c.window.v9BusinessRefresh();assert.equal(f.calls.length,0);
 f=fixture();await f.c.window.v9BusinessStatus();assert.equal(f.calls[0].p,'/api/integrations/wecom/business/status');assert.match(f.element.textContent,/未知不表示零/);
 f=fixture();f.setAccept(true);f.setFail(true);await f.c.window.v9BusinessRefresh();const first=f.calls[0];assert.equal(f.data.size,1);
 f.setFail(false);await f.c.window.v9BusinessRefresh();assert.deepEqual(f.calls[1].o,first.o);assert.equal(f.data.size,0);assert.match(f.element.textContent,/未验证正样本/);
 f=fixture();f.c.window.v9ObservationImport();const form=f.getForm();assert.throws(()=>form[2]({ack:false},'fixture'),/明确确认/);
 assert.throws(()=>form[2]({ack:true,document:'{}'},'fixture'),/acknowledge_authorized_source/);
 console.log('8 v9 UI state scenarios passed (offline VM; no browser or platform I/O)');
})().catch(e=>{console.error(e);process.exit(1)});
