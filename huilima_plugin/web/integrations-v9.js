/* Existing Hub only. No startup requests, automatic refresh or saved credentials. */
(() => {
  let pending=null,busy=false;
  const storage='creatorhub-wecom-business-pending-v1';
  try {pending=JSON.parse(sessionStorage.getItem(storage)||'null');}catch(_){}
  if(!pending || !/^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$/.test(pending.key||'') || !Number.isInteger(pending.start_time) || !Number.isInteger(pending.end_time)) pending=null;
  const show=value=>{document.getElementById('v9-summary').textContent=typeof value==='string'?value:JSON.stringify(value,null,2);};
  function owner(){
    if(document.documentElement.dataset.preview==='true')throw Error('静态预览不连接业务接口。');
    if(typeof OPS_TOKEN!=='undefined' && OPS_TOKEN)throw Error('此连接器操作仅限本机管理员，不回退团队凭据。');
  }
  const attempt=async fn=>{try{await fn();}catch(e){show('操作未确认；不自动重试，不表示没有数据。\n'+e.message);}};
  window.v9LoadHandoff=()=>attempt(async()=>{
    const r=await opsAPI('handoff'), f=r.facts||{};
    const labels={wecom_business_positive_samples:'企微客户群 / 朋友圈 / 群发记录的真实正样本',
      real_crm_table_schema:'正式飞书客户表、字段与资源权限',real_crm_write_and_owner_notice_acceptance:'真实线索入表＋负责人通知验收',
      independent_backup_destination_and_recovery_acceptance:'独立备份位置与实际恢复验收',backup_targets:'备份保留期及恢复目标',
      required_platforms_confirmation:'必选平台清单',metrics_cadence_confirmation:'指标刷新周期',existing_sop_persona_assets:'既有SOP / 人设 / 技能资产',
      designated_maintainer:'指定维护人',long_running_and_customer_signoff:'持续运行与客户签认'};
    const lines=[r.employee_mobile_acceptance==='excluded_by_owner'?'员工手机分发验收：已按要求排除':'员工手机分发验收：仍在范围内',
      '真实指标/评论离线快照：'+(f.real_observation_snapshots??'未知')+'；人设配置：'+(f.persona_count??'未知'),
      '客户表字段预检：'+(f.crm_schema_preflight_ready?'已通过（不代表可写入）':'尚未通过'),
      '真实CRM投递：'+(f.crm_real_delivery_enabled?'已人工启用':'未启用'),
      '企微完整业务摘要：'+(f.wecom_business_complete_snapshot_available?'已有缓存，需核对时间与正样本':'尚无完整缓存'),
      '', '待补项（不把模拟数据算成验收）：'];
    for(const [k,v] of Object.entries(r.pending||{}))if(v)lines.push('· '+(labels[k]||k));
    show(lines.join('\n'));
  });
  window.v9Observations=()=>attempt(async()=>show(await opsAPI('observations')));
  window.v10BackupPolicy=()=>attempt(async()=>{owner();show(await opsAPI('backup-policy'));});
  window.v9BusinessStatus=()=>attempt(async()=>{
    owner();showBusiness(await api('/api/integrations/wecom/business/'+(pending?'requests/'+encodeURIComponent(pending.key):'status')));
  });
  function showBusiness(r){
    const attempt=r.refresh||r.last_attempt, saved=r.last_complete;
    const status={completed:'完成',partial:'部分可用 / 不完整',running:'进行中',interrupted:'中断，需核对原请求',failed:'失败'};
    const lines=[saved?'完整缓存：'+(r.cache_fresh?'1小时内快照，不代表实时':'已过期；不会自动补抓'):'尚无完整缓存；未知不表示零'];
    if(attempt){
      lines.push('最近尝试：'+(status[attempt.status]||'未知'));
      if(attempt.finished)lines.push('检查时间：'+new Date(attempt.finished*1000).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false})+'（北京时间）');
      const labels={groups:'客户群',customer_messages:'客户群发',group_messages:'客户群群发',moments:'客户朋友圈'};
      for(const [name,s] of Object.entries(attempt.summary?.sections||{}))
        lines.push((labels[name]||name)+'：'+(s.status==='ok'?'观察到 '+s.observed_count+' 条'+(s.complete?'':'（未拉全）')+(s.positive_sample_verified?'':'；未验证正样本'):'不可用，接口码 '+(s.api_code??'未知')));
      if(attempt.error_code)lines.push('错误：'+attempt.error_code);
    }
    if(pending)lines.push('待核对请求：'+pending.key);
    lines.push('记录可证明执行状态，不代表客户已读、回复或成交。');show(lines.join('\n'));
  }
  window.v9CRMPreflight=()=>attempt(async()=>{
    owner();if(!confirm('仅检查已配置客户表的字段名称与类型，使用该连接器选定的凭据。不读表内记录，不写入、不通知、不自动启用。继续？'))return;
    show(await opsAPI('delivery/preflight',{},'crm-preflight-ui-'+crypto.randomUUID()));
  });
  window.v9BusinessRefresh=()=>attempt(async()=>{
    owner();if(busy)return;
    if(!confirm('仅读取当前1名测试成员的客户群列表、近7天朋友圈与群发执行记录；仅保存数量及时间。不读取聊天/详情、不创建或发送任务。与客户数量刷新共用15分钟间隔。继续？'))return;
    busy=true;
    try {
      const end=Math.floor(Date.now()/1000);
      pending=pending||{key:'wecom-business-ui-'+crypto.randomUUID(),start_time:end-7*86400,end_time:end};
      try{sessionStorage.setItem(storage,JSON.stringify(pending));}catch(_){}
      const result=await api('/api/integrations/wecom/business/refresh',{method:'POST',headers:{'Content-Type':'application/json','Idempotency-Key':pending.key},
        body:JSON.stringify({start_time:pending.start_time,end_time:pending.end_time,acknowledge_business_records:true})});
      showBusiness(result);
      if(['completed','partial','failed','interrupted'].includes(result.refresh?.status)){
        pending=null;try{sessionStorage.removeItem(storage);}catch(_){}
      }
    } finally {busy=false;}
  });
  window.v9ObservationImport=()=>{
    opsForm('导入已授权离线来源；只创建待人工复核线索',[
      {name:'document',label:'JSON：账号/作品、来源链接、采集时间、五项指标（未知用null+reason）及评论。格式见CLI AI_USAGE。不要放Cookie或密钥。',type:'textarea',required:true},
      {name:'ack',label:'确认来源已获授权；演练资料已标记 is_demo:true；不会发布、写CRM或通知',type:'checkbox'}],
      (v,k)=>{if(!v.ack)throw Error('需要明确确认来源');const body=JSON.parse(v.document);if(body.acknowledge_authorized_source!==true)throw Error('JSON须明确 acknowledge_authorized_source:true');return opsAPI('observations',body,k);});
  };
  window.v9Profile=()=>attempt(async()=>{
    owner();const p=(await opsAPI('delivery-profile')).profile;
    opsForm('交付目标；保存不会启用采集、备份定时或自动删除',[
      {name:'profile',label:'完整配置JSON；只填写已确认的周期/负责人，不确定的目标保持null。员工手机验收可按当前范围排除并填写原因。',type:'textarea',value:JSON.stringify(p,null,2),required:true}],
      (v,k)=>opsAPI('delivery-profile',JSON.parse(v.profile),k));
  });
})();
