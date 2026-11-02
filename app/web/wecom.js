/* Manual, count-only WeCom integration in the existing Hub. No polling. */
(() => {
  let key = '', busy = false;
  const storage = 'creatorhub-wecom-pending-request';
  try { key = sessionStorage.getItem(storage) || ''; } catch (_) {}
  if (!/^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$/.test(key)) key = '';
  function owner() {
    if (document.documentElement.dataset.preview === 'true') throw Error('静态预览不连接企微。');
    if (typeof OPS_TOKEN !== 'undefined' && OPS_TOKEN) throw Error('企微摘要仅限本机管理员；不回退团队凭据。');
  }
  function show(data) {
    const labels = {running:'进行中',interrupted:'中断，需核对原请求',completed:'完成',failed:'失败'};
    const stamp = value => { const d=new Date(value); return value && Number.isFinite(d.valueOf()) ? d.toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false})+'（北京时间）' : '未知'; };
    const count = value => Number.isInteger(value) && value>=0 ? value : '未知';
    if (typeof data === 'string') document.getElementById('wecom-summary').textContent = data;
    else {
      const saved=data.last_success, last=data.last_attempt, receipt=data.refresh;
      const lines = saved ? [
        '测试成员：'+count(saved.counts?.enabled_member_count)+' 人 · 客户数量：'+count(saved.counts?.customer_count),
        '数据采集时间：'+stamp(saved.finished_at),
        '缓存：'+(data.cache_fresh?'1小时内的快照，不代表实时数据':'已过期或时钟异常；不自动补抓'),
      ] : ['尚无成功摘要；客户数量未知，不表示零。'];
      if(last) lines.push('最近尝试：'+(labels[last.status]||'未知')+(Number.isInteger(last.api_code)?' · 接口码 '+last.api_code:''));
      if(last?.status==='failed' && saved) lines.push('最近刷新失败；上方保留的是此前成功快照。');
      if(receipt) lines.push('本请求回执：'+(labels[receipt.status]||'未知')+(data.replayed?'（复用原回执，未重复调用）':''));
      if(data.next_allowed_at) lines.push('下次最早可刷新：'+stamp(data.next_allowed_at));
      lines.push('仅手动刷新；不读取详情或聊天、不发送消息。');
      document.getElementById('wecom-summary').textContent = lines.join('\n');
    }
    if (data && ['completed', 'failed', 'interrupted'].includes(data.refresh?.status)) {
      key = ''; try { sessionStorage.removeItem(storage); } catch (_) {}
    }
  }
  window.wecomLoadStatus = async () => {
    try {
      owner();
      const path = key ? 'requests/' + encodeURIComponent(key) : 'status';
      show(await api('/api/integrations/wecom/' + path));
    } catch (e) { show('读取未确认，不表示零。' + (key ? ' 保留原请求编号：' + key : '') + '\n' + e.message); }
  };
  window.wecomRefreshCounts = async () => {
    if (busy) return;
    try {
      owner();
      if (!window.confirm('仅读取这1名测试成员的客户ID列表并保存数量，至少间隔15分钟。不取详情或聊天、不发消息。确认继续？')) return;
      busy = true; document.getElementById('wecom-refresh-button').disabled = true;
      key = key || 'wecom-ui-' + crypto.randomUUID();
      try { sessionStorage.setItem(storage, key); } catch (_) {}
      show('正在刷新；不自动重试。原请求编号：'+key);
      show(await api('/api/integrations/wecom/refresh', {method:'POST',
        headers:{'Content-Type':'application/json','Idempotency-Key':key},
        body:JSON.stringify({acknowledge_customer_ids:true})}));
    } catch (e) { show('刷新未确认；不自动重试。保留原请求编号：' + key + '\n' + e.message); }
    finally { busy = false; document.getElementById('wecom-refresh-button').disabled = false; }
  };
})();
