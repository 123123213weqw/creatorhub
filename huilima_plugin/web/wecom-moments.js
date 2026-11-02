/* Local-owner manual Moments; no startup requests, background poll, or auto-submit. */
(function () {
'use strict';
const ROOT='/api/integrations/wecom/moments', PENDING='creatorhub-wecom-moments-receipt';
const statuses={draft:'本地草稿',approved:'已审核，未提交',submitting:'提交中，勿重复',creating:'已取得创建回执，尚未发表',awaiting_member:'任务已创建，等待成员发表',published:'成员发表已核验（非客户已读）',scope_rejected:'执行范围被拒，须人工核对',rejected:'平台明确拒绝',unknown:'结果未知，须到企微核对，禁止重发',failed_before_task:'未尝试创建任务（素材可能已上传）',canceled:'仅本地取消，非远端撤回'};
const ID=/^wecom_moment_[a-f0-9]{32}$/;
let busy=false;
const el=id=>document.getElementById(id);
const put=t=>{if(el('wcm-status'))el('wcm-status').textContent=t;};
function boundary(){
 if(document.documentElement.dataset.preview==='true')throw Error('静态预览不连接业务数据。请打开本机 Hub。');
 if(typeof OPS_TOKEN!=='undefined'&&OPS_TOKEN)throw Error('朋友圈创建仅限本机管理员；请先退出团队凭据，不会自动降权回退。');
}
async function request(path='',body,key){
 boundary();
 const headers=body===undefined?{}:{'Content-Type':'application/json','Idempotency-Key':key};
 const r=await fetch(ROOT+path,{method:body===undefined?'GET':'POST',headers,body:body===undefined?undefined:JSON.stringify(body),credentials:'omit',redirect:'error'});
 if(!r.ok){let d;try{d=await r.json();}catch{}throw Error(typeof d?.detail==='string'?d.detail:Array.isArray(d?.detail)?'字段校验失败：'+d.detail.map(x=>(x.loc||[]).slice(1).join('.')).join('、'):'操作未确认，HTTP '+r.status+'；请核对原请求回执，不要盲目重发。');}
 return r.json();
}
function remember(operation,key){try{sessionStorage.setItem(PENDING,JSON.stringify({operation,key}));}catch{}}
function pending(){try{return JSON.parse(sessionStorage.getItem(PENDING)||'null');}catch{return null;}}
function clear(){try{sessionStorage.removeItem(PENDING);}catch{}}
function form(title,fields,button,submit,detail){
 boundary();const d=el('wcm-dialog'), f=el('wcm-form');
 el('wcm-title').textContent=title;el('wcm-error').textContent='';el('wcm-preview').replaceChildren();f.replaceChildren();
 fields.forEach(x=>{const l=document.createElement('label');l.htmlFor='wcmf-'+x.name;l.textContent=x.label;
  const e=document.createElement(x.options?'select':x.type==='textarea'?'textarea':'input');e.id=l.htmlFor;e.name=x.name;
  if(x.options)x.options.forEach(o=>{const n=document.createElement('option');n.value=o.value??o;n.textContent=o.label??o;e.append(n);});
  else if(x.type!=='textarea')e.type=x.type||'text';
  if(x.type==='checkbox')e.checked=false;else if(!x.options||x.value!==undefined)e.value=x.value??'';
  e.required=!!x.required;e.readOnly=!!x.readonly;if(x.type==='textarea')e.rows=5;f.append(l,e);
 });
 if(detail)preview(detail);
 const key='wcm-ui-'+crypto.randomUUID(), b=el('wcm-save');b.textContent=button;b.hidden=!submit;b.disabled=false;
 f.onsubmit=async event=>{event.preventDefault();if(b.disabled||!submit)return;b.disabled=true;el('wcm-error').textContent='';
  try{boundary();const values={};fields.forEach(x=>values[x.name]=x.type==='checkbox'?f.elements[x.name].checked:f.elements[x.name].value);
   await submit(values,key);d.close();await load();
  }catch(error){el('wcm-error').textContent=error.message+' 原操作编号：'+key+'。超时后留在本页核对状态。';}
  finally{b.disabled=false;}
 };d.showModal();
}
function preview(row){
 const p=el('wcm-preview'), text=document.createElement('p');
 text.textContent=`${row.label}\n状态：${statuses[row.status]||row.status}\n执行成员：${row.sender_user_id}\n企业客户标签：${(row.customer_tag_ids||[]).join('、')}\n范围说明：${row.customer_scope_note}\n完整正文：\n${row.text||'（无文字，仅素材）'}\n摘要：${row.digest}\n审核时间：${row.approved_at||'尚未审核'}\n任务编号：${row.job_id||'无'}；朋友圈编号：${row.moment_id||'无'}\n成员已发表：${row.member_published===true?'是':row.member_published===false?'否':'未知'}`;
 p.append(text);
 for(const [i,m]of(row.media||[]).entries()){
  const caption=document.createElement('p');caption.textContent=`素材 ${i+1} · ${m.size} 字节 · SHA256 ${m.sha256}`;p.append(caption);
  if(ID.test(row.id)){
   const media=document.createElement(row.media_type==='video'?'video':'img');media.src=ROOT+'/'+row.id+'/media/'+i;
   if(row.media_type==='video'){media.controls=true;media.preload='metadata';}else media.alt='人工审核的冻结图片 '+(i+1);
   media.style.maxWidth='100%';media.style.maxHeight='280px';p.append(media);
  }
 }
}
async function load(){
 if(busy)return;busy=true;
 try{const data=await request(), bound=await request('/binding');
  put(`仅本地状态。成员范围：${bound.binding_available?(bound.binding_fresh?'已核对（1小时内）':'已过期，请手动核对'):'尚未核对'}；平台写权限仍须真实验收。创建任务与成员发表分开显示。`);
  const list=el('wcm-list');list.replaceChildren();
  for(const row of data.items||[]){if(!ID.test(row.id))continue;
   const card=document.createElement('article');card.className='card';card.style.cssText='padding:14px;margin:10px 0;overflow-wrap:anywhere';
   const title=document.createElement('h4');title.textContent=row.label+' · '+(statuses[row.status]||row.status);card.append(title);
   const note=document.createElement('p');note.textContent=`${row.id}\n素材 ${row.media_count}；企业客户标签 ${row.customer_tag_count}；创建尝试 ${row.task_creation_attempts} 次；${row.demo_only?'演示数据不可外发':'未标记为演示'}。\n${row.last_error_code||row.last_query_error||''}${row.last_api_code||row.last_query_api_code?' · 平台码 '+(row.last_api_code||row.last_query_api_code):''}`;note.style.whiteSpace='pre-wrap';card.append(note);
   const actions=[['核对完整内容',()=>show(row.id)]];
   if(row.status==='draft'&&!row.demo_only)actions.push(['人工审核',()=>review(row.id)]);
   if(row.status==='approved')actions.push(['确认创建一次',()=>submit(row.id)]);
   if(['creating','awaiting_member','published','scope_rejected'].includes(row.status))actions.push(['手动查询结果',()=>refresh(row.id)]);
   if(['draft','approved','failed_before_task','rejected'].includes(row.status))actions.push(['仅取消本地任务',()=>cancel(row.id)]);
   for(const [label,fn]of actions){const b=document.createElement('button');b.type='button';b.className='ghost sm';b.textContent=label;b.onclick=fn;card.append(b);}
   list.append(card);
  }
  if(!(data.items||[]).length)list.textContent='暂无朋友圈草稿。查看状态不会访问企微。';
 }catch(error){put(error.message);}finally{busy=false;}
}
async function preflight(){
 try{form('手动只读核对当前应用的唯一成员',[{name:'ack',label:'只核对现有应用可见范围，不新增权限，不读取客户详情，不创建发表任务。',type:'checkbox'}],'确认只读核对',async(v,k)=>{
  if(!v.ack)throw Error('请确认只读范围');remember('preflight',k);const r=await request('/preflight',{},k);
  if(r.status==='running')throw Error('原请求仍在运行；请查询回执');clear();
  if(r.status!=='ready')throw Error('成员核对失败：'+(r.error_code||'未确认')+(r.api_code?' · '+r.api_code:''));
 });}catch(error){put(error.message);}
}
async function draft(){
 try{const bound=await request('/binding');if(!bound.binding_available||!bound.binding_fresh)throw Error('请先手动核对唯一成员（仅1小时内有效），不会自动访问企微。');
  form('创建本地朋友圈草稿 · 不外发',[{name:'label',label:'草稿名称',required:true},{name:'sender',label:'执行成员（只读）',readonly:true,value:bound.sender_user_id},{name:'text',label:'完整正文（最多2000字且4000字节）',type:'textarea'},
   {name:'media_type',label:'素材类型，不混合',options:[{value:'text',label:'纯文字'},{value:'images',label:'最多9张JPG/PNG'},{value:'video',label:'一个≤10MB且≤30秒的MP4'}]},
   {name:'assets',label:'已登记素材编号 asset_…，逗号分隔；纯文字留空'},
   {name:'tags',label:'明确的企业客户标签ID et…，逗号分隔；必须已核对标签内的客户',required:true},
   {name:'scope',label:'客户范围核对说明；不得使用“全体客户”作为默认',type:'textarea',required:true},
   {name:'demo',label:'仅隔离演示，永久禁止此草稿外发',type:'checkbox'}],'保存本地草稿',async(v,k)=>{
    remember('draft',k);await request('/drafts',{label:v.label,binding_id:bound.binding_id,text:v.text,media_type:v.media_type,asset_ids:v.assets.split(/[,，\s]+/).filter(Boolean),customer_tag_ids:v.tags.split(/[,，\s]+/).filter(Boolean),customer_scope_note:v.scope,demo_only:v.demo},k);clear();
   });
 }catch(error){put(error.message);}
}
async function show(id){try{const row=await request('/'+id);form('冻结内容与真实回执',[],null,null,row);}catch(e){put(e.message);}}
async function review(id){
 try{const row=await request('/'+id);form('人工审核 · 只审核，不提交',[{name:'note',label:'人工复核依据',type:'textarea',required:true},
  {name:'content',label:'已核对上方完整文案、所有图片/视频和SHA256。',type:'checkbox'},
  {name:'tags',label:'已核对明确企业标签内的客户，确认没有扩大客户范围。',type:'checkbox'},
  {name:'member',label:'知晓后续创建会通知这一名成员；成员须在企微确认发表。',type:'checkbox'}],'保存审核（30分钟内有效）',async(v,k)=>{
   await request('/'+id+'/review',{digest:row.digest,review_note:v.note,acknowledge_content:v.content,acknowledge_customer_tags:v.tags,acknowledge_member_notification:v.member},k);
  },row);
 }catch(e){put(e.message);}
}
async function submit(id){
 try{const row=await request('/'+id);form('创建一次企微发表任务 · 不是立即发表',[{name:'ack',label:'明确确认：按上述完整内容和客户标签创建一次；素材上传至腾讯，会通知这名成员。未知结果不自动重发；这不代表成员已发表或客户已读。',type:'checkbox'}],'确认上传素材并创建一次',async(v,k)=>{
   if(!v.ack)throw Error('请明确确认外发影响');const r=await request('/'+id+'/submit',{digest:row.digest,acknowledge_external_writes:true},k);
   if(['unknown','submitting','failed_before_task','rejected'].includes(r.status))throw Error(statuses[r.status]+'；请关闭窗口查看本地状态，不要重新创建');
  },row);
 }catch(e){put(e.message);}
}
async function refresh(id){
 try{form('手动查询任务创建和成员发表状态',[{name:'ack',label:'只读查询此任务；至少间隔60秒，不查普通聊天、回复或客户详情，不重新创建任务。',type:'checkbox'}],'确认只读查询一次',async(v,k)=>{
  if(!v.ack)throw Error('请确认');const r=await request('/'+id+'/refresh',{acknowledge_platform_read:true},k);
  if(r.last_query_error)throw Error('查询未完成：'+r.last_query_error+'；原状态不变');
 });}catch(e){put(e.message);}
}
async function cancel(id){try{form('仅取消本地记录 · 不撤回远端任务',[{name:'reason',label:'原因',type:'textarea',required:true}],'确认仅本地取消',(v,k)=>request('/'+id+'/cancel',{reason:v.reason},k));}catch(e){put(e.message);}}
async function receipt(){
 try{boundary();const r=pending();if(!r||!['draft','preflight'].includes(r.operation)||!/^[A-Za-z0-9._:-]{16,128}$/.test(r.key))throw Error('没有待核对的创建草稿或成员核对请求。提交任务请查看本地任务状态。');
  const result=await request('/receipts/'+r.operation+'/'+encodeURIComponent(r.key));
  put('原请求 '+r.key+'：'+(statuses[result.status]||result.status)+'。仅查询本地回执，未重新提交。');
  if(result.status!=='running')clear();
 }catch(e){put(e.message);}
}
Object.assign(window,{wcmLoad:load,wcmPreflight:preflight,wcmDraft:draft,wcmShow:show,wcmReview:review,wcmSubmit:submit,wcmRefresh:refresh,wcmCancel:cancel,wcmReceipt:receipt});
})();
