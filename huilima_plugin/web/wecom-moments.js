/* Local-owner manual Moments; no startup requests, background poll, or auto-submit. */
(function () {
'use strict';
const ROOT='/api/integrations/wecom/moments', PENDING='creatorhub-wecom-moments-receipt';
const statuses={draft:'草稿',approved:'已审核，待提交',submitting:'正在提交，请勿重复操作',creating:'已提交，待查询发表进度',awaiting_member:'待成员发表',published:'成员已发表（不代表客户已阅读）',scope_rejected:'发表范围未通过，请核对',rejected:'提交未通过',unknown:'结果暂未确认，请先到企业微信查看，不要再次提交',failed_before_task:'发表任务未创建（部分素材可能已上传）',canceled:'已在本系统取消（不撤回企业微信中的任务）'};
const progress=status=>statuses[status]||'进度待确认';
function details(title,content){const d=document.createElement('details'),summary=document.createElement('summary'),text=document.createElement('pre');summary.textContent=title;text.textContent=content;text.style.whiteSpace='pre-wrap';text.style.overflowWrap='anywhere';d.append(summary,text);return d;}
function diagnostic(area,content=''){const d=el('wcm-'+area+'-details'),t=el('wcm-'+area+'-technical');if(d){d.hidden=!content;d.open=false;}if(t)t.textContent=content;}
function readable(error){const text=String(error?.message||'');return /[\u4e00-\u9fff]/.test(text)?text:'连接暂时中断，请先查看进度，确认结果前不要重复提交。';}
function technical(error){return error?.technicalDetail||(/[\u4e00-\u9fff]/.test(String(error?.message||''))?'':String(error?.message||''));}

const ID=/^wecom_moment_[a-f0-9]{32}$/;
let busy=false;
const el=id=>document.getElementById(id);
const put=(t,info='')=>{if(el('wcm-status'))el('wcm-status').textContent=t;diagnostic('status',info);};
const fail=error=>put(readable(error),technical(error));
function boundary(){
 if(document.documentElement.dataset.preview==='true')throw Error('这是演示页面，不会读取或发送企业微信数据。请到正式工作台操作。');
 if(typeof OPS_TOKEN!=='undefined'&&OPS_TOKEN)throw Error('当前团队身份暂不支持提交朋友圈，请让管理员在安装 Hub 的电脑上操作。系统不会自动切换身份。');
}
async function request(path='',body,key){
 boundary();
 const headers=body===undefined?{}:{'Content-Type':'application/json','Idempotency-Key':key};
 const r=await fetch(ROOT+path,{method:body===undefined?'GET':'POST',headers,body:body===undefined?undefined:JSON.stringify(body),credentials:'omit',redirect:'error'});
 if(!r.ok){let d;try{d=await r.json();}catch{}const messages={401:'登录身份已失效，请联系管理员重新登录。',403:'当前身份没有操作权限，请联系管理员。',422:'填写的信息不完整或格式不正确，请检查后再操作。',429:'操作太频繁，请稍后查看进度，不要反复提交。'};const error=Error(messages[r.status]||'暂时未能完成操作。请先查看进度，确认结果前不要重复提交。');error.technicalDetail='HTTP '+r.status+' · '+(typeof d?.detail==='string'?d.detail:Array.isArray(d?.detail)?'字段检查：'+d.detail.map(x=>(x.loc||[]).slice(1).join('.')).join('、'):'服务暂未返回可确认的结果');throw error;}

 return r.json();
}
function remember(operation,key){try{sessionStorage.setItem(PENDING,JSON.stringify({operation,key}));}catch{}}
function pending(){try{return JSON.parse(sessionStorage.getItem(PENDING)||'null');}catch{return null;}}
function clear(){try{sessionStorage.removeItem(PENDING);}catch{}}
function form(title,fields,button,submit,detail){
 boundary();const d=el('wcm-dialog'), f=el('wcm-form');
 el('wcm-title').textContent=title;el('wcm-error').textContent='';diagnostic('error');el('wcm-preview').replaceChildren();f.replaceChildren();
 fields.forEach(x=>{const l=document.createElement('label');l.htmlFor='wcmf-'+x.name;l.textContent=x.label;
  const e=document.createElement(x.options?'select':x.type==='textarea'?'textarea':'input');e.id=l.htmlFor;e.name=x.name;
  if(x.options)x.options.forEach(o=>{const n=document.createElement('option');n.value=o.value??o;n.textContent=o.label??o;e.append(n);});
  else if(x.type!=='textarea')e.type=x.type||'text';
  if(x.type==='checkbox')e.checked=false;else if(!x.options||x.value!==undefined)e.value=x.value??'';
  e.required=!!x.required;e.readOnly=!!x.readonly;if(x.type==='textarea')e.rows=5;f.append(l,e);if(x.help){const h=document.createElement('p');h.className='hint';h.textContent=x.help;f.append(h);}
 });
 if(detail)preview(detail);
 const key='wcm-ui-'+crypto.randomUUID(), b=el('wcm-save');b.textContent=button;b.hidden=!submit;b.disabled=false;
 f.onsubmit=async event=>{event.preventDefault();if(b.disabled||!submit)return;b.disabled=true;el('wcm-error').textContent='';
  try{boundary();const values={};fields.forEach(x=>values[x.name]=x.type==='checkbox'?f.elements[x.name].checked:f.elements[x.name].value);
   await submit(values,key);d.close();await load();
  }catch(error){el('wcm-error').textContent=readable(error);diagnostic('error',(technical(error)?technical(error)+'\n':'')+'操作编号：'+key+'。请保留此编号，供管理员检查；未确认结果前不要重复提交。');}
  finally{b.disabled=false;}
 };d.showModal();
}
function preview(row){
 const p=el('wcm-preview'), text=document.createElement('p');
 text.textContent=`${row.label}\n进度：${progress(row.status)}\n发表成员（账号编号）：${row.sender_user_id}\n可见客户的标签编号：${(row.customer_tag_ids||[]).join('、')}\n客户范围说明：${row.customer_scope_note}\n朋友圈内容：\n${row.text||'（没有文字，仅图片或视频）'}\n审核时间：${row.approved_at||'尚未审核'}\n成员是否已发表：${row.member_published===true?'已发表':row.member_published===false?'尚未发表':'暂未确认'}`;
 p.append(text);
 p.append(details('查看处理记录（供管理员排查）',`任务编号：${row.job_id||'暂无'}\n朋友圈编号：${row.moment_id||'暂无'}\n内容校验码：${row.digest}\n系统状态：${row.status}`));

 for(const [i,m]of(row.media||[]).entries()){
  const caption=document.createElement('p');caption.textContent='图片或视频 '+(i+1);p.append(caption);p.append(details('查看文件校验信息（供管理员排查）',`大小：${m.size} 字节\nSHA256：${m.sha256}`));
  if(ID.test(row.id)){
   const media=document.createElement(row.media_type==='video'?'video':'img');media.src=ROOT+'/'+row.id+'/media/'+i;
   if(row.media_type==='video'){media.controls=true;media.preload='metadata';}else media.alt='待审核的图片 '+(i+1);
   media.style.maxWidth='100%';media.style.maxHeight='280px';p.append(media);
  }
 }
}
async function load(){
 if(busy)return;busy=true;
 try{const data=await request(), bound=await request('/binding');
  put(`发表成员：${bound.binding_available?(bound.binding_fresh?'已确认':'需要重新确认，请点“确认发表成员”'):'尚未确认，请先点“确认发表成员”'}。以下是已保存的进度；需要最新结果时，请在对应内容下点“查询发表进度”。`);
  const list=el('wcm-list');list.replaceChildren();
  for(const row of data.items||[]){if(!ID.test(row.id))continue;
   const card=document.createElement('article');card.className='card';card.style.cssText='padding:14px;margin:10px 0;overflow-wrap:anywhere';
   const title=document.createElement('h4');title.textContent=row.label+' · '+progress(row.status);card.append(title);
   const note=document.createElement('p');note.textContent=`${row.media_count?'已添加 '+row.media_count+' 份图片或视频':'未添加图片或视频'} · 使用 ${row.customer_tag_count} 个客户标签。${row.demo_only?'仅供演示，不能提交给成员。':''}`;card.append(note);
   card.append(details('查看处理记录',`记录编号：${row.id}\n向企业微信提交的次数：${row.task_creation_attempts}\n系统状态：${row.status}\n问题记录：${row.last_error_code||row.last_query_error||'暂无'}\n企业微信返回码：${row.last_api_code||row.last_query_api_code||'暂无'}`));
   const actions=[['查看完整内容',()=>show(row.id)]];
   if(row.status==='draft'&&!row.demo_only)actions.push(['审核内容',()=>review(row.id)]);
   if(row.status==='approved')actions.push(['提交给成员',()=>submit(row.id)]);
   if(['creating','awaiting_member','published','scope_rejected'].includes(row.status))actions.push(['查询发表进度',()=>refresh(row.id)]);
   if(['draft','approved','failed_before_task','rejected'].includes(row.status))actions.push(['取消草稿',()=>cancel(row.id)]);
   for(const [label,fn]of actions){const b=document.createElement('button');b.type='button';b.className='ghost sm';b.textContent=label;b.onclick=fn;card.append(b);}
   list.append(card);
  }
  if(!(data.items||[]).length)list.textContent='还没有朋友圈草稿，点击“新建朋友圈”开始准备内容。';
 }catch(error){fail(error);}finally{busy=false;}
}
async function preflight(){
 try{form('确认谁来发表',[{name:'ack',label:'向企业微信确认当前应用对应的发表成员；不会添加权限、读取客户详情或发送朋友圈。',type:'checkbox'}],'确认成员',async(v,k)=>{
  if(!v.ack)throw Error('请先勾选并确认本次操作范围');remember('preflight',k);const r=await request('/preflight',{},k);
  if(r.status==='running')throw Error('上次操作仍在处理中，请点“找回未完成操作”，不要重复提交');clear();
  if(r.status!=='ready'){const error=Error('暂时无法确认发表成员，请联系管理员。');error.technicalDetail=(r.error_code||'未确认')+(r.api_code?' · 企业微信返回码 '+r.api_code:'');throw error;}
 });}catch(error){fail(error);}
}
async function draft(){
 try{const bound=await request('/binding');if(!bound.binding_available||!bound.binding_fresh)throw Error('请先点“确认发表成员”。确认结果1小时内有效，需要重新确认后才能新建朋友圈。');
  form('新建朋友圈 · 先保存，不发送',[{name:'label',label:'内容名称',required:true},{name:'sender',label:'发表成员（已确认，不可修改）',readonly:true,value:bound.sender_user_id},{name:'text',label:'朋友圈文字',help:'中文建议不超过1300字；表情和链接也会占用长度。系统会在保存时检查。',type:'textarea'},
   {name:'media_type',label:'内容形式（图片和视频不能混用）',options:[{value:'text',label:'纯文字'},{value:'images',label:'最多9张JPG/PNG'},{value:'video',label:'一个≤10MB且≤30秒的MP4'}]},
   {name:'assets',label:'素材编号（由管理员提供；多个编号用逗号分隔；纯文字留空）'},
   {name:'tags',label:'可见客户的标签编号（由管理员提供；多个编号用逗号分隔）',required:true},
   {name:'scope',label:'哪些客户可以看到（请说明已核对的客户范围，不默认发给所有客户）',type:'textarea',required:true},
   {name:'demo',label:'仅用于演示（勾选后，这条内容将不能提交给成员）',type:'checkbox'}],'保存草稿',async(v,k)=>{
    remember('draft',k);await request('/drafts',{label:v.label,binding_id:bound.binding_id,text:v.text,media_type:v.media_type,asset_ids:v.assets.split(/[,，\s]+/).filter(Boolean),customer_tag_ids:v.tags.split(/[,，\s]+/).filter(Boolean),customer_scope_note:v.scope,demo_only:v.demo},k);clear();
   });
 }catch(error){fail(error);}
}
async function show(id){try{const row=await request('/'+id);form('朋友圈内容与发表进度',[],null,null,row);}catch(e){fail(e);}}
async function review(id){
 try{const row=await request('/'+id);form('审核朋友圈 · 审核通过后仍需提交',[{name:'note',label:'审核说明',type:'textarea',required:true},
  {name:'content',label:'我已查看完整文案、图片或视频，确认内容无误。',type:'checkbox'},
  {name:'tags',label:'我已核对客户标签对应的客户，确认没有发给范围以外的人。',type:'checkbox'},
  {name:'member',label:'我知道提交后会通知指定成员，仍需成员在企业微信手机端确认发表。',type:'checkbox'}],'确认审核通过（30分钟内可提交）',async(v,k)=>{
   await request('/'+id+'/review',{digest:row.digest,review_note:v.note,acknowledge_content:v.content,acknowledge_customer_tags:v.tags,acknowledge_member_notification:v.member},k);
  },row);
 }catch(e){fail(e);}
}
async function submit(id){
 try{const row=await request('/'+id);form('提交给成员 · 仍需在手机上发表',[{name:'ack',label:'我确认以上内容和可见客户准确，同意将图片或视频上传至企业微信，并向指定成员发送发表任务。成员仍需在手机上确认发表。结果暂未确认时，先核对，不重复提交。',type:'checkbox'}],'确认提交给成员',async(v,k)=>{
   if(!v.ack)throw Error('请先确认内容、可见客户和提交后的操作');const r=await request('/'+id+'/submit',{digest:row.digest,acknowledge_external_writes:true},k);
   if(['unknown','submitting','failed_before_task','rejected'].includes(r.status))throw Error(progress(r.status)+'；请关闭窗口查看进度，不要再次提交');
  },row);
 }catch(e){fail(e);}
}
async function refresh(id){
 try{form('查询发表进度',[{name:'ack',label:'向企业微信查询这条内容的最新进度，不重新提交，也不读取聊天或客户详情。两次查询至少间隔1分钟。',type:'checkbox'}],'查询最新进度',async(v,k)=>{
  if(!v.ack)throw Error('请确认');const r=await request('/'+id+'/refresh',{acknowledge_platform_read:true},k);
  if(r.last_query_error){const error=Error('暂时无法取得最新进度，仍显示上次保存的进度。');error.technicalDetail=r.last_query_error;throw error;}
 });}catch(e){fail(e);}
}
async function cancel(id){try{form('取消本系统中的草稿 · 不会撤回企业微信中的任务',[{name:'reason',label:'原因',type:'textarea',required:true}],'确认取消草稿',(v,k)=>request('/'+id+'/cancel',{reason:v.reason},k));}catch(e){fail(e);}}
async function receipt(){
 try{boundary();const r=pending();if(!r||!['draft','preflight'].includes(r.operation)||!/^[A-Za-z0-9._:-]{16,128}$/.test(r.key))throw Error('没有需要找回的操作。已提交的朋友圈，请先在列表中查看进度，结果未确认前不要再次提交。');
  const result=await request('/receipts/'+r.operation+'/'+encodeURIComponent(r.key));
  const labels={ready:'发表成员已确认',draft:'草稿已保存',running:'仍在处理中，请稍后再查看',interrupted:'上次操作中断，请联系管理员确认',failed:'操作未完成',done:'已完成'};
  put('上次操作的结果：'+(labels[result.status]||statuses[result.status]||'暂未确认')+'。本次只查看已保存的结果，没有再次提交。','操作编号：'+r.key+'\n系统状态：'+result.status);
  if(result.status!=='running')clear();
 }catch(e){fail(e);}
}
Object.assign(window,{wcmLoad:load,wcmPreflight:preflight,wcmDraft:draft,wcmShow:show,wcmReview:review,wcmSubmit:submit,wcmRefresh:refresh,wcmCancel:cancel,wcmReceipt:receipt});
})();
