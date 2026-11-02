/* Offline DOM tests, no real browser, platform, credentials or timer. */
const test=require('node:test'), assert=require('node:assert/strict'), vm=require('node:vm'), fs=require('node:fs'), path=require('node:path');
const source=fs.readFileSync(path.join(__dirname,'../huilima_plugin/web/wecom-moments.js'),'utf8');
function fixture({token='',preview=false,handler=async()=>({items:[]})}={}){
 const calls=[],store=new Map(),nodes={};
 class Node{
  constructor(tag='div'){this.tag=tag;this.children=[];this.elements={};this.dataset={};this.style={};this.disabled=false;this.checked=false;this.value='';}
  append(...items){for(const n of items){this.children.push(n);if(n.name)this.elements[n.name]=n;if(this.tag==='select'&&this.children.length===1)this.value=n.value;}}
  replaceChildren(){this.children=[];this.elements={};} showModal(){this.open=true;} close(){this.open=false;}
 }
 for(const id of ['wcm-status','wcm-list','wcm-dialog','wcm-form','wcm-preview','wcm-title','wcm-error','wcm-save'])nodes[id]=new Node();
 const context={OPS_TOKEN:token,document:{documentElement:{dataset:{preview:String(preview)}},getElementById:id=>nodes[id],createElement:tag=>new Node(tag)},
  fetch:async(url,opts)=>{calls.push([url,opts]);const value=await handler(url,opts);return {ok:true,json:async()=>value};},
  sessionStorage:{setItem:(k,v)=>store.set(k,v),getItem:k=>store.get(k),removeItem:k=>store.delete(k)},
  crypto:{randomUUID:()=> '00000000-0000-4000-8000-000000000001'},window:{},JSON,Error,encodeURIComponent};
 vm.runInNewContext(source,context);return {...context.window,nodes,calls,store};
}
const id='wecom_moment_'+'a'.repeat(32);
const binding={binding_available:true,binding_fresh:true,sender_user_id:'fixture-member',binding_id:'b'.repeat(64)};
const row={id,label:'fixture',status:'draft',media_count:0,customer_tag_count:1,task_creation_attempts:0,text:'fixture text',sender_user_id:'fixture-member',customer_tag_ids:['etFIXTURE'],customer_scope_note:'controlled',digest:'c'.repeat(64),media:[]};
const submit=f=>f.nodes['wcm-form'].onsubmit({preventDefault(){}});
test('initial load makes no requests or timers; view only cached GETs',async()=>{
 const f=fixture({handler:async u=>u.endsWith('/binding')?binding:{items:[row]}});assert.equal(f.calls.length,0);
 await f.wcmLoad();assert.equal(f.calls.length,2);assert.ok(f.calls.every(c=>c[1].method==='GET'));
 assert.equal(f.nodes['wcm-list'].children[0].children[0].textContent,'fixture · 本地草稿');
});
test('team and static preview never fall back to local owner',async()=>{
 for(const opts of [{token:'cht_fixture'},{preview:true}]){const f=fixture(opts);
  await f.wcmLoad();await f.wcmDraft();await f.wcmPreflight();await f.wcmSubmit(id);await f.wcmRefresh(id);await f.wcmReceipt();
  assert.equal(f.calls.length,0);
 }
});
test('preflight requires deliberate check; error retains original stable request',async()=>{
 let fail=true;const f=fixture({handler:async u=>{if(u.endsWith('/preflight')&&fail)throw Error('fixture timeout');return u.endsWith('/preflight')?{status:'ready'}:u.endsWith('/binding')?binding:{items:[]};}});
 await f.wcmPreflight();assert.equal(f.calls.length,0);await submit(f);assert.equal(f.calls.length,0);
 f.nodes['wcm-form'].elements.ack.checked=true;await submit(f);const key=f.calls[0][1].headers['Idempotency-Key'];assert.equal(f.store.size,1);
 fail=false;await submit(f);assert.equal(f.calls[1][1].headers['Idempotency-Key'],key);assert.equal(f.store.size,0);
});
test('review does not submit and submit is separate, checked action',async()=>{
 const f=fixture({handler:async(u,o)=>o.method==='GET'?(u.endsWith('/binding')?binding:u.endsWith(id)?row:{items:[]}):{status:u.endsWith('/review')?'approved':'creating'}});
 await f.wcmReview(id);assert.ok(f.nodes['wcm-preview'].children[0].textContent.includes('etFIXTURE'));
 const e=f.nodes['wcm-form'].elements;e.note.value='owner fixture review';e.content.checked=e.tags.checked=e.member.checked=true;
 await submit(f);assert.equal(f.calls.filter(c=>c[0].endsWith('/review')).length,1);assert.equal(f.calls.filter(c=>c[0].endsWith('/submit')).length,0);
 await f.wcmSubmit(id);await submit(f);assert.equal(f.calls.filter(c=>c[0].endsWith('/submit')).length,0);
 f.nodes['wcm-form'].elements.ack.checked=true;await submit(f);
 assert.equal(f.calls.filter(c=>c[0].endsWith('/submit')).length,1);
});
test('creation unknown is not auto retried or re-approved',async()=>{
 const f=fixture({handler:async(u,o)=>o.method==='GET'?row:{status:'unknown'}});await f.wcmSubmit(id);
 f.nodes['wcm-form'].elements.ack.checked=true;await submit(f);
 assert.equal(f.calls.filter(c=>c[0].endsWith('/submit')).length,1);
 assert.match(f.nodes['wcm-error'].textContent,/禁止重发/);assert.equal(f.nodes['wcm-dialog'].open,true);
});
test('receipt recovery is local GET not a repeated POST',async()=>{
 const f=fixture({handler:async()=>({status:'ready'})});
 f.store.set('creatorhub-wecom-moments-receipt',JSON.stringify({operation:'preflight',key:'wcm-ui-00000000-0000-4000-8000-000000000001'}));
 await f.wcmReceipt();assert.equal(f.calls.length,1);assert.match(f.calls[0][0],/\/receipts\/preflight\//);assert.equal(f.calls[0][1].method,'GET');
});

test('new draft retains first select option instead of sending an empty media type',async()=>{
 const f=fixture({handler:async(u,o)=>o.method==='GET'?(u.endsWith('/binding')?binding:{items:[]}):{status:'draft'}});
 await f.wcmDraft();const e=f.nodes['wcm-form'].elements;assert.equal(e.media_type.value,'text');
 e.label.value='fixture';e.text.value='fixture text';e.tags.value='etFIXTURE';e.scope.value='controlled fixture';
 await submit(f);const post=f.calls.find(c=>c[0].endsWith('/drafts'));assert.ok(post);
 assert.equal(JSON.parse(post[1].body).media_type,'text');
 assert.equal(f.calls.filter(c=>c[0].endsWith('/submit')).length,0);
});
