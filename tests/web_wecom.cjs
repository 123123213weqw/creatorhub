/* Offline UI-state tests. No browser or real network. */
const test=require('node:test'), assert=require('node:assert/strict');
const vm=require('node:vm'), fs=require('node:fs'), path=require('node:path');
const source=fs.readFileSync(path.join(__dirname,'../huilima_plugin/web/wecom.js'),'utf8');
function fixture({token='',preview=false,key='',confirm=true,api=async()=>({})}={}){
  const calls=[],store=new Map(key?[['creatorhub-wecom-pending-request',key]]:[]);
  const elements={'wecom-summary':{textContent:''},'wecom-refresh-button':{disabled:false}};
  const c={OPS_TOKEN:token,document:{documentElement:{dataset:{preview:String(preview)}},getElementById:id=>elements[id]},
    sessionStorage:{getItem:k=>store.get(k),setItem:(k,v)=>store.set(k,v),removeItem:k=>store.delete(k)},
    crypto:{randomUUID:()=> '00000000-0000-4000-8000-000000000001'},
    api:async(...args)=>{calls.push(args);return api(...args);},
    window:{confirm:()=>confirm},JSON,encodeURIComponent,Error};
  vm.runInNewContext(source,c);
  return {...c.window,calls,store,elements};
}
test('load performs no I/O; view is a cached GET',async()=>{
  const f=fixture();assert.equal(f.calls.length,0);
  await f.wecomLoadStatus();assert.deepEqual(f.calls,[['/api/integrations/wecom/status']]);
});
test('preview and team context never fall back to local owner',async()=>{
  for(const opts of [{token:'cht_fixture'},{preview:true}]){
    const f=fixture(opts);await f.wecomLoadStatus();await f.wecomRefreshCounts();
    assert.equal(f.calls.length,0);
  }
});
test('cancel before network; no automatic refresh',async()=>{
  const f=fixture({confirm:false});await f.wecomRefreshCounts();
  assert.equal(f.calls.length,0);assert.equal(f.store.size,0);
});
test('ambiguous failure retains same key and query is receipt-only',async()=>{
  let fail=true;
  const f=fixture({api:async()=>{if(fail)throw Error('fixture timeout');return {refresh:{status:'completed'}};}});
  await f.wecomRefreshCounts();const k=f.store.get('creatorhub-wecom-pending-request');
  assert.ok(k);await f.wecomLoadStatus();
  assert.equal(f.calls[1][0],'/api/integrations/wecom/requests/'+k);
  fail=false;await f.wecomRefreshCounts();
  assert.equal(f.calls[0][1].headers['Idempotency-Key'],f.calls[2][1].headers['Idempotency-Key']);
  assert.equal(f.store.size,0);
});
test('double click is coalesced, failure receipt preserves displayed old-cache context',async()=>{
  let release;const p=new Promise(r=>release=r);
  const f=fixture({api:()=>p});const first=f.wecomRefreshCounts();
  await f.wecomRefreshCounts();assert.equal(f.calls.length,1);
  release({refresh:{status:'failed',api_code:60020},last_success:{counts:{customer_count:1}}});
  await first;assert.equal(f.store.size,0);
  assert.match(f.elements['wecom-summary'].textContent,/失败/);
  assert.equal(f.elements['wecom-refresh-button'].disabled,false);
});
