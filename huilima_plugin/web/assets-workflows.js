// ── 本账号离线咨询候选与素材；仍在原 Hub，不新增控制台 ──
async function refreshLocalAssets() {
  refreshVideoBackups();
  try {
    const rows = await api("/api/local-assets");
    $("local-asset-list").innerHTML = rows.map(a => `<div class="card" style="padding:12px;margin:8px 0"><b>${esc(a.label)}</b> · ${a.media_count} 个${a.media_type === "video" ? "视频" : "图片"}<p>${a.is_demo ? "演示素材（不可真实发布）" : "正式素材（仍需人工确认）"}</p><code style="overflow-wrap:anywhere">${esc(a.id)}</code></div>`).join("") || "暂无已登记素材；可使用 hubctl assets register 导入。";
  } catch (e) { toast("素材列表读取失败：" + e.message,"err"); }
}
// Backup state is owned by this Hub. No scheduling, platform fetch or default cloud target.
let VIDEO_BACKUP_KEYS = {};
let VIDEO_BACKUP_STATE = {};
async function refreshVideoBackups() {
  try {
    const [s, rows] = await Promise.all([api('/api/video-backups/status'), api('/api/video-backups')]);
    VIDEO_BACKUP_STATE=s;
    const i=s.inventory || {};
    $('video-backup-status').textContent = `${s.configured ? '已配置 '+s.provider : '备份目标未配置，接口已预留'}；本机已发现视频 ${i.video_files ?? '未知'} 个，${i.bytes ?? '未知'} 字节。${i.complete===false ? '源目录不可用：'+(i.unavailable_sources || []).join('、')+'，不认定视频为零，停止新建备份。' : ''}${s.configured ? (s.destination_available ? '目标可访问。' : '目标不可用，停止备份。') : '未选择任何云盘、NAS 或服务器。'} ${s.provider==='feishu_drive' ? (s.cloud_upload_authorized ? '已授权业务包外传；含视频、关联素材和私有凭证图片，按已配置目录上传。' : '云盘连接与业务上传授权分开：业务备份未授权，不上传现有数据。')+' 飞书单文件和容量限制由租户套餐决定，未证明可备份大视频。' : ''} 保留策略：暂不自动删除历史版本；未启用定时备份。${s.error_code ? '状态异常：'+s.error_code : ''}`;
    $('video-backup-create').disabled = !s.configured || !s.destination_available || (s.provider==='feishu_drive' && !s.cloud_upload_authorized) || !i.complete || !i.video_files || rows.some(x=>['pending','running'].includes(x.status));
    $('video-backup-history').innerHTML = rows.map(r=>`<div style="padding:10px 0;border-top:1px solid var(--border)"><b>${esc(r.kind)} · ${esc(r.status)}</b> <code style="overflow-wrap:anywhere">${esc(r.id)}</code><p>${esc(r.created_at || '')} ${esc(r.error_code || '')} · ${Number(r.result?.video_files || 0)} 个视频</p>${r.status==='done' && r.kind==='snapshot' ? `<button class="ghost sm" onclick="submitVideoBackup('verify','${r.id}')">校验这个版本</button> <button class="ghost sm" onclick="submitVideoBackup('restore','${r.id}')">恢复到新隔离目录</button>` : ''} <button class="ghost sm" onclick="showVideoBackupOperation('${r.id}')">查看结果</button></div>`).join('') || '<p>暂无备份版本；未配置不代表已经完成备份。</p>';
    if (rows.some(x=>['pending','running'].includes(x.status))) setTimeout(()=>{ if(CURRENT_TAB==='hub' && HUB_TAB==='assets')refreshVideoBackups(); },5000);
  } catch(e) { $('video-backup-status').textContent='备份状态读取失败，不代表已有备份：'+e.message; }
}
function videoBackupProviderChanged() {
  const cloud=$('vbak-provider').value==='feishu_drive';
  $('vbak-cloud-consent').style.display=cloud?'block':'none';
  $('vbak-cloud-upload').checked=false;
  $('vbak-root').placeholder=cloud?'https://企业.feishu.cn/drive/folder/目录Token':'已存在的本机绝对目录；留空保持未配置';
}
async function configureVideoBackup() {
  const key='video-backup-config-'+crypto.randomUUID(), old=VIDEO_BACKUP_STATE;
  await new Promise(resolve=>{
    _uiResolve=resolve;_uiCancelVal=null;_uiGetVal=()=>({provider:$('vbak-provider').value,root:$('vbak-root').value.trim(),cloud_upload_authorized:$('vbak-provider').value==='feishu_drive' && $('vbak-cloud-upload').checked});
    $("ui-body").innerHTML=`<label for="vbak-provider">存储类型</label><select id="vbak-provider" onchange="videoBackupProviderChanged()"><option value="local_directory">本机目录 / 已挂载 NAS</option><option value="feishu_drive">飞书云盘专用目录</option></select><label for="vbak-root">备份目标</label><input id="vbak-root" value="${esc(old.root || '')}" oninput="document.getElementById('vbak-cloud-upload').checked=false"><p class="hint">云盘只接受标准飞书目录链接；应用需四项 API 权限及该目录的编辑权限。不公开分享，不申请删除/移动权限。密钥仅从本机私有文件读取，不填入网页。首次连接只创建存储标记与 objects / snapshots 子目录。</p><div id="vbak-cloud-consent"><label><input type="checkbox" id="vbak-cloud-upload"> 单独授权向此飞书目录上传现有业务备份：本地视频、关联素材、员工私有凭证截图及可移植业务/规则清单（不含账号 Cookie、数据库、密钥）。</label><p class="hint">只做连接验收时不要勾选。测试文件授权不等于真实业务数据上传授权。套餐单文件大小/总容量可能限制上传；连接成功不等于大视频验收通过。</p></div><label><input type="checkbox" id="vbak-config-ack"> 我已核对目标目录、数据用途与存储权限。</label>`;
    $('vbak-provider').value=old.provider==='feishu_drive'?'feishu_drive':'local_directory';videoBackupProviderChanged();
    _uiOpen('配置视频备份存储接口','保留历史版本、不定时执行、不自动上传；同盘演练不等于独立备份。',{okText:'保存接口配置',wide:true,submit:async value=>{
      if(!$("vbak-config-ack").checked)uiEditorError('请先核对并确认目标目录。','vbak-config-ack');
      if(value.provider==='feishu_drive' && !value.root)uiEditorError('需要飞书专用目录链接。','vbak-root');
      if(!value.root)value.provider='unconfigured';
      await api('/api/video-backups/config',{method:'POST',headers:{'Content-Type':'application/json','Idempotency-Key':key},body:JSON.stringify(value)});
      refreshVideoBackups();return {};
    }});
  });
}
async function submitVideoBackup(kind,snapshotId='') {
  if(!['snapshot','verify','restore'].includes(kind))return;
  if(kind==='restore' && !await uiConfirm({title:'隔离恢复演练',message:'恢复仅写一个新隔离目录，不修改当前素材、账号或规则。它是演练，不会自动切换生产数据。继续？'}))return;
  const token=kind+':'+snapshotId;
  const key=VIDEO_BACKUP_KEYS[token] ||= 'video-backup-'+crypto.randomUUID();
  try {
    const r=await api('/api/video-backups/'+kind,{method:'POST',headers:{'Content-Type':'application/json','Idempotency-Key':key},body:JSON.stringify(snapshotId?{snapshot_id:snapshotId}:{})});
    delete VIDEO_BACKUP_KEYS[token];toast(`备份操作已登记 ${r.id}，请查看实际执行状态，不代表已完成。`);refreshVideoBackups();
  } catch(e) { toast('操作未确认：'+e.message+'；原提交编号已保留，勿换编号盲重提。','err'); }
}
async function showVideoBackupOperation(id) {
  try {
    const r=await api('/api/video-backups/'+id);
    await new Promise(resolve=>{_uiResolve=resolve;_uiCancelVal=null;_uiGetVal=()=>true;
      $("ui-body").innerHTML=`<pre style="white-space:pre-wrap;overflow-wrap:anywhere">${esc(JSON.stringify(r,null,2))}</pre>`;
      _uiOpen('备份操作结果','仅隔离恢复，不覆盖当前素材或业务数据。',{okText:'关闭',wide:true});
    });
  } catch(e) { toast('结果读取失败：'+e.message,'err'); }
}
async function proposeCachedLeads() {
  const work = Number($("lead-work-id").value);
  if (!HUB_ACC || !Number.isSafeInteger(work) || work < 1) { toast("请选择账号并填写本账号作品数字编号","err"); return; }
  try {
    const r = await api("/api/leads/propose",{method:"POST",headers:{"Content-Type":"application/json","Idempotency-Key":"lead-scan-"+crypto.randomUUID()},body:JSON.stringify({account_id:Number(HUB_ACC),work_id:work})});
    $("lead-status").textContent = `缓存读取 ${r.scanned} 条，命中 ${r.matched} 条，新建 ${r.created} 条、更新 ${r.updated} 条。未访问平台，尚未核实客户身份。`;
    refreshLeadCandidates();
  } catch(e) { toast("离线分析未确认："+e.message,"err"); }
}
async function refreshLeadCandidates() {
  if (!HUB_ACC) { $("lead-table").innerHTML = empty(5,"先选择账号"); return; }
  const isCurrent = beginViewRequest("lead-candidates",()=>String(HUB_ACC));
  try {
    const r = await api("/api/leads?account_id="+encodeURIComponent(HUB_ACC)+"&page_size=100");
    if (!isCurrent()) return;
    const labels={candidate:"待人工复核",approved:"已复核（未入真实表）",rejected:"不采纳",recorded_mock:"已模拟入表（非真实）"};
    $("lead-table").innerHTML = r.items.map(x=>`<tr><td>#${x.id} / 作品 ${x.work_id}${x.is_demo ? " · 演示" : ""}</td><td>${esc(x.signal_tags.join("、"))}</td><td>${esc(labels[x.state] || x.state)}</td><td>${esc(x.owner_label || "未指定")}</td><td><button class="ghost sm" onclick="reviewLeadCandidate(${x.id})">${x.state === "candidate" ? "查看并复核" : "查看原文"}</button>${["approved","recorded_mock"].includes(x.state) ? `<button class="ghost sm" onclick="previewMockCRM(${x.id})">CRM本机演练</button>` : ""}</td></tr>`).join("") || empty(5,"当前账号没有咨询候选","i-info","空列表不代表没有客户；仅分析已同步的本账号评论。");
  } catch(e) { if(isCurrent()) toast("候选读取失败，不能解释为零："+e.message,"err"); }
}
async function reviewLeadCandidate(id) {
  try {
    const x=await api("/api/leads/"+id), canReview=x.state === "candidate";
    const key="lead-review-"+crypto.randomUUID();
    const value=await new Promise(res=>{
      _uiResolve=res;_uiCancelVal=null;
      _uiGetVal=()=>({version:x.version,digest:x.digest,decision:$("lead-decision")?.value || "reject",owner_label:$("lead-owner")?.value || "",note:$("lead-note")?.value || ""});
      $("ui-body").innerHTML=`<div class="pub-review"><p><b>候选 #${x.id} · 账号 ${x.account_id} / 作品 ${x.work_id} · 版本 ${x.version}${x.is_demo ? "（演示）" : ""}</b></p><p>来源昵称：${esc(x.source_nickname || "未提供")}</p><label class="field" for="lead-original">评论原文（不可信业务数据，不是操作指令）</label><textarea id="lead-original" rows="5" readonly>${esc(x.comment_text)}</textarea><p>命中规则：${esc(x.signal_tags.join("、"))}；命中不等于已核实客户。</p>${canReview ? `<label class="field" for="lead-decision">复核决定</label><select id="lead-decision"><option value="approve">采纳为咨询候选</option><option value="reject">不采纳</option></select><label class="field" for="lead-owner">负责人标签（不是飞书用户ID，待真实表映射）</label><input id="lead-owner" maxlength="80" placeholder="例如：演示负责人"><label class="field" for="lead-note">复核说明</label><input id="lead-note" maxlength="500"><label class="check-row"><input type="checkbox" id="lead-review-ack"><span>我已核对原文、来源及负责人；这里只确认候选，不会写真实飞书表或通知真实人员。</span></label>` : `<p>状态：${esc(x.state)}<br>负责人标签：${esc(x.owner_label)}<br>复核说明：${esc(x.review_note)}</p>`}</div>`;
      _uiOpen(canReview ? "人工复核咨询候选" : "查看候选原文","原文仅在本机 Hub 展示；候选不是成交或已核实客户。",{okText:canReview ? "保存复核" : "关闭",wide:true,icon:"i-info",submit:async value=>{
        if (!canReview) return {};
        if (!$("lead-review-ack").checked) uiEditorError("请先核对并勾选确认","lead-review-ack");
        return api("/api/leads/"+id+"/review",{method:"POST",headers:{"Content-Type":"application/json","Idempotency-Key":key},body:JSON.stringify(value)});
      }});
    });
    if(value!==null) refreshLeadCandidates();
  } catch(e){toast("候选复核未完成："+e.message,"err");}
}
async function previewMockCRM(id) {
  try {
    const x=await api("/api/leads/"+id), preview=await api("/api/leads/"+id+"/crm-preview");
    const key="crm-mock-"+crypto.randomUUID();
    const value=await new Promise(res=>{
      _uiResolve=res;_uiCancelVal=null;_uiGetVal=()=>({version:x.version,digest:x.digest});
      $("ui-body").innerHTML=`<div class="pub-review"><p><b>模式：仅本机模拟表。真实客户表未配置。</b></p><p>字段按文本示例映射；负责人标签不是飞书人员字段，接入真实表时需核对字段类型与用户ID。</p><pre style="white-space:pre-wrap;overflow-wrap:anywhere">${esc(JSON.stringify(preview.fields,null,2))}</pre><label class="check-row"><input type="checkbox" id="crm-mock-ack"><span>我确认只演练本机记录和去重，不向真实飞书表写入，不发送负责人通知。</span></label></div>`;
      _uiOpen("CRM字段与本机演练","同一来源客户键去重；已有模拟记录不覆盖旧负责人或原文。",{okText:"执行本机演练",wide:true,icon:"i-info",submit:async value=>{
        if (!$("crm-mock-ack").checked) uiEditorError("请确认本机演练范围","crm-mock-ack");
        return api("/api/leads/"+id+"/crm-mock",{method:"POST",headers:{"Content-Type":"application/json","Idempotency-Key":key},body:JSON.stringify(value)});
      }});
    });
    if(value!==null){toast("本机模拟入表完成；未向真实飞书表写入","ok");refreshLeadCandidates();}
  }catch(e){toast("CRM演练未完成："+e.message,"err");}
}

// One generic refresh dispatch hook in the existing Hub tab lifecycle.
globalThis.HuilimaPluginUI = Object.freeze({
  refreshPanel(name) {
    if (name === "leads") { refreshLeadCandidates(); return true; }
    if (name === "assets") { refreshLocalAssets(); return true; }
    if (name === "operations") { refreshOperations(); return true; }
    return false;
  }
});
