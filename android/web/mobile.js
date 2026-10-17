window.WBMobile = (() => {
  let initialized = false;
  let saveCounter = 0;
  const saves = new Map();
  function saveText(filename, text, mime) {
    return new Promise((resolve, reject) => {
      if(!window.WBAndroid?.saveText) { reject(new Error('当前安装包不支持保存文件，请更新 APK')); return; }
      const id = String(++saveCounter);
      const timer = setTimeout(() => { saves.delete(id); reject(new Error('文件保存超时，请重试')); }, 300000);
      saves.set(id, {resolve, reject, timer});
      if (!WBAndroid.saveText(id, filename, text, mime)) saveResult(id, false, '无法开始保存，请等待其他导出完成');
    });
  }
  function saveResult(id, ok, message) {
    const pending = saves.get(String(id));
    if (!pending) return;
    saves.delete(String(id)); clearTimeout(pending.timer);
    if(ok) pending.resolve(); else pending.reject(new Error(message || '文件未保存'));
  }
  function initialize(token) {
    TOKEN = token;
    localStorage.setItem(TOKEN_KEY, token);
    if (!initialized) {
      initialized = true;
      // ── 顺序很关键：**先拆掉引导层，再做任何可能失败的事** ──────────
      //
      // 原来这里是 enterApp() → 移除 localBoot → topbarMode →
      // WBDesktop.checkUpdatesAtStartup()。中间任何一步抛异常，
      // 后面的「移除 localBoot」就永远不执行 —— 用户界面卡在
      // 「正在打开本地工作台…」，而服务其实是好的（curl 返回 200）。
      //
      // 而 WebView 默认不转发 console 错误，所以这种失败**完全没有
      // 可见线索**，只能靠猜。实测就是被这个坑住的。
      //
      // 现在：先无条件拆引导层（进入主界面），再把每个可选项单独 try。
      document.getElementById('localBoot')?.remove();
      try { enterApp(); } catch (e) { reportBootError('enterApp', e); }
      try {
        const modeEl = document.getElementById('topbarMode');
        if (modeEl) modeEl.textContent = '手机本地运行';
      } catch (e) { reportBootError('topbarMode', e); }
      // 更新检查是**纯附加**功能：失败绝不该阻断进入主界面，
      // 也不该在方法不存在时抛 TypeError（早期版本没导出这个方法）。
      try { window.WBDesktop?.checkUpdatesAtStartup?.(); }
      catch (e) { reportBootError('checkUpdatesAtStartup', e); }
      try { window.WBMobileSettings?.start(); }
      catch (e) { reportBootError('WBMobileSettings', e); }
    }
    try {
      const app = document.getElementById('app');
      if (app && app.classList.contains('hidden')) enterApp();
    } catch (e) { reportBootError('enterApp retry', e); }
    window.dispatchEvent(new Event('wb-shell-resume'));
  }

  /**
   * 把启动阶段的异常**显示到界面上**，而不是只写 console。
   *
   * 为什么需要：Android WebView 默认不转发 console 日志，`adb logcat`
   * 里一行 JS 错误都看不到。真机排查时「界面卡住 + 零日志」等于瞎猜。
   * 把错误贴到启动画面，至少能截图看到。
   */
  function reportBootError(where, err) {
    const msg = (err && (err.message || err.name)) || String(err);
    // eslint-disable-next-line no-console
    console.error('[boot]', where, err);
    try {
      const status = document.getElementById('localBootStatus');
      if (status) {
        status.textContent = `启动异常（${where}）：${msg}`;
      } else if (typeof toast === 'function') {
        // `toast` 是 index.html 内联脚本里的**全局函数**（不是 WBDesktop 的方法）。
        // 早先在 `WBDesktop.toast` 上找它 —— 那不存在，被导出测试抓到了。
        toast(`启动异常：${msg}`, 'err');
      }
    } catch (_) { /* 报告失败不该再抛 */ }
  }
  document.documentElement.classList.add('mobile-mode', 'desktop-mode');
  // Android WebView denies navigator.clipboard writes. Its selection copy
  // command uses the system clipboard and works from a user's tap.
  copyText = function(selector) {
    const source = document.querySelector(selector);
    if (!source) return;
    const text = source.value || source.textContent || '';
    const previous = document.activeElement;
    const field = document.createElement('textarea');
    field.value = text;
    field.readOnly = true;
    field.style.cssText = 'position:fixed;top:0;left:0;width:1px;height:1px;opacity:0';
    document.body.append(field);
    field.select();
    field.setSelectionRange(0, text.length);
    let copied = false;
    try { copied = document.execCommand('copy'); }
    finally { field.remove(); if (previous?.focus) previous.focus({preventScroll:true}); }
    toast(copied ? '已复制' : '复制失败，请长按文字复制', copied ? 'ok' : 'err');
  };
  // ⚠️ **必须判空**：`.topbar-endpoint` 在部分界面形态下不存在。
  // 这是 IIFE **顶层**代码 —— 一旦抛异常，整个 mobile.js 会中途退出，
  // `window.WBMobile` 就永远不会被赋值，Java 端的
  // `window.WBMobile && WBMobile.initialize(...)` 于是**静默跳过**，
  // 界面永久卡在「正在打开本地工作台…」，且没有任何错误日志。
  // 真机（OnePlus 9）实测就是被这个坑住的。
  const endpointEl = document.querySelector('.topbar-endpoint');
  if (endpointEl) endpointEl.onclick = () => copyText('#proxyUrl');
  const diagnosticButton = document.createElement('button');
  diagnosticButton.type = 'button';
  diagnosticButton.className = 'console-btn quiet mobile-diagnostics-button';
  diagnosticButton.textContent = '调用诊断';
  diagnosticButton.onclick = async () => {
    diagnosticButton.disabled = true;
    try {
      const result = await safeJson(await api('/api/app/request-timings'));
      if (!result.ok) { toast('读取调用记录失败', 'err'); return; }
      const records = [...(result.data.active || []), ...(result.data.recent || [])];
      const seconds = ms => ms == null ? '尚未返回' : (ms / 1000).toFixed(3) + ' 秒';
      const outcomes = {active:'处理中', completed:'响应已结束', disconnected:'客户端断开', interrupted:'请求中断', http_error:'接口报错'};
      const requestErrors = {client_disconnected:'请求正文尚未接收完整，连接已断开', invalid_json:'请求正文不是有效 JSON', invalid_encoding:'请求正文编码无法解析', body_read_failed:'读取请求正文失败'};
      const labels = {get_key_row:'Key 校验', _key_group_models:'模型权限', _pick_best_model:'模型选择',
        _select_account:'选号', _account_session_safe:'凭证准备', _headers_for_attempt:'请求头准备', metadata_credentials:'模型请求凭证准备',
        gateway_admission:'入口校验', read_request_body:'读取请求正文',
        _record_usage:'记账', models_catalog:'模型查询', _upstream_extra_headers:'附加请求头',
        _note_success:'更新账号状态', close:'关闭数据库会话',
        'connection.connect_tcp':'DNS / 建立连接', 'connection.start_tls':'TLS 握手',
        'http11.send_request_headers':'发送上游请求头', 'http11.send_request_body':'上传上游请求体',
        'http11.response_closed':'关闭上游响应',
        'http11.receive_response_headers':'等待上游响应头', 'http11.receive_response_body':'接收上游响应体',
        retry_backoff:'重试退避等待', client_upload_wait:'接收客户端请求', client_send_wait:'向客户端发送'};
      openModal(`<h3 class="font-semibold text-lg mb-3">调用诊断</h3>
        <p class="text-sm mb-3" style="color:var(--fg-muted)">处理耗时从请求进入 API 开始，另列出接入与服务调度等待。HTTP 200 和心跳不代表已有回答，请查看有效增量与请求状态。</p>
        <button id="refreshMobileDiagnostics" type="button" class="console-btn quiet mb-3">刷新记录</button>
        <button id="copyMobileDiagnostics" type="button" class="console-btn quiet mb-3">复制诊断记录</button>
        <textarea id="mobileDiagnosticsText" hidden></textarea>
        <div class="mobile-diagnostic-records">${records.map(row => `<details class="mobile-diagnostic-record">
          <summary><b>${esc(row.path)}</b><small>${esc(new Date(row.started_at).toLocaleTimeString())} · ${esc(outcomes[row.outcome] || '—')} · HTTP ${row.status || '—'}</small>
          <span>${row.streaming ? '有效增量' : '响应体'} ${seconds(row.first_delta_ms)} · 已耗时 ${seconds(row.elapsed_ms ?? row.total_ms)}</span></summary>
          <div><p>请求编号：${esc(row.id)}</p><p>响应头：${seconds(row.headers_ms)} · 传输首字节：${seconds(row.first_body_ms)}</p>
          ${row.ingress_ms != null ? `<p>首个请求字节到处理：${seconds(row.ingress_ms)} · 调度等待：${seconds(row.asgi_queue_ms)}</p><p>服务调度最大延迟：${seconds(row.event_loop_lag_ms)}</p>` : ''}
          ${row.upstream_attempts ? `<p>上游请求次数：${Number(row.upstream_attempts)}</p>` : ''}
          ${row.network_error ? `<p>网络错误类型：${esc(row.network_error)}</p>` : ''}
          ${row.upstream_error && row.upstream_error !== 'success' ? `<p>上游失败分类：${esc(row.upstream_error)}${row.upstream_status ? ` · HTTP ${Number(row.upstream_status)}` : ''}</p>` : ''}
          ${row.outcome === 'active' ? `<p>当前步骤：${esc(labels[row.phase] || row.phase || '等待处理')}</p>` : ''}
          ${row.request_error ? `<p>${esc(requestErrors[row.request_error] || '读取请求正文失败')}</p>` : ''}
          ${row.method && !['GET','HEAD','OPTIONS'].includes(row.method) ? `<p>请求正文：${row.upload_complete ? '接收完成' : '未接收完成'}</p>` : ''}
          <p>收到 ${Number(row.request_bytes || 0).toLocaleString()} 字节 · 发出 ${Number(row.response_bytes || 0).toLocaleString()} 字节</p>
          ${(row.stages || []).map(stage => `<p>${esc(labels[stage.name] || stage.name)}：${seconds(stage.ms)}</p>`).join('')}</div>
        </details>`).join('') || '<p>暂无外部调用记录，请先在聊天软件发起请求。</p>'}</div>`, 'max-w-2xl');
      document.getElementById('mobileDiagnosticsText').value = JSON.stringify(result.data, null, 2);
      document.getElementById('refreshMobileDiagnostics').onclick = () => diagnosticButton.onclick();
      document.getElementById('copyMobileDiagnostics').onclick = () => copyText('#mobileDiagnosticsText');
    } catch (error) { toast('读取调用记录失败', 'err'); }
    finally { diagnosticButton.disabled = false; }
  };
  // ⚠️ **必须判空**：`.api-share-card` 在当前 index.html 里**根本不存在**
  // （实测：`grep -c 'api-share-card' admin/static/index.html` = 0）。
  //
  // 原来这里直接 `.append(...)` → `null.append` 抛 TypeError →
  // 整个 mobile.js IIFE 中途崩溃 → `window.WBMobile` 从没被赋值 →
  // Java 端 `window.WBMobile && ...` 静默跳过 → 引导层永不消失。
  //
  // **这是真机上「界面卡在正在打开本地工作台」的真正根因。**
  const shareCard = document.querySelector('.api-share-card');
  if (shareCard) shareCard.append(diagnosticButton);
  const help = document.querySelector('.api-share-help');
  if (help) {
    const details = document.createElement('details');
    details.className = 'mobile-api-help';
    const summary = document.createElement('summary');
    summary.textContent = '接入说明（OpenAI / Claude）';
    help.replaceWith(details);
    details.append(summary, help);
  }
  const compact = window.matchMedia('(max-width:900px)');
  const actions = document.querySelector('.topbar-actions');
  const header = document.querySelector('.desktop-topbar');
  function arrangeHeader() {
    // ⚠️ 两个元素都可能不存在（不同面板形态），且本函数**立刻执行**（L176）。
    // 任一处为 null 都会让整个 IIFE 崩溃 → WBMobile 未导出 → 引导层卡死。
    const target = compact.matches ? document.getElementById('appSide') : header;
    if (target && actions) target.appendChild(actions);
  }
  compact.addEventListener('change', arrangeHeader);
  try { arrangeHeader(); } catch (e) { reportBootError('arrangeHeader', e); }
  return {initialize, saveText, saveResult};
})();
