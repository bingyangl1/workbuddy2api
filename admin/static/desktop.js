/* Release text is always inserted as textContent, never interpreted as HTML. */
window.WBDesktop = (() => {
  let native = false, initialized = false, checking = false, releases = null, dialog, poll;
  // 允许跳转的 Releases 前缀：由 /api/app/version 下发（权威来源 = desktop/version.json），
  // 不再硬编码 owner/repo —— fork 换仓库后旧硬编码会让「打开 Releases」按钮消失。
  let allowedReleasePrefix = '';
  const versionButton = () => document.getElementById('appVersion');
  const element = (tag, text, className) => {
    const el = document.createElement(tag);
    if (text !== undefined) el.textContent = text;
    if (className) el.className = className;
    return el;
  };
  const button = (text, action, primary = false) => {
    const el = element('button', text, primary ? 'primary' : '');
    el.type = 'button'; el.addEventListener('click', action); return el;
  };
  const link = (text, url) => {
    const el = element('a', text);
    if (allowedReleasePrefix && url.startsWith(allowedReleasePrefix)) {
      el.href = url; el.target = '_blank'; el.rel = 'noopener noreferrer';
    }
    return el;
  };
  function label() {
    if (!releases) return;
    versionButton().textContent = `v${releases.version} · ${releases.available ? '发现新版本' : '检查更新'}`;
    versionButton().classList.toggle('has-update', !!releases.available);
  }
  async function check(auto = false) {
    if (checking) return;
    checking = true;
    try {
      if (native) releases = await pywebview.api.check_updates();
      else {
        const response = await api('/api/app/releases');
        if (!response.ok) throw new Error(`检查失败（HTTP ${response.status}）`);
        releases = await response.json();
      }
      label();
      if (dialog?.open) render();
      if (auto && releases.available) {
        // A notice does not open a modal or require a decision at startup.
        const notice = element('div', `发现新版本 ${releases.latest.tag}，点击左下角版本号查看。`, 'card');
        Object.assign(notice.style, {position:'fixed',bottom:'24px',right:'24px',padding:'16px',zIndex:70,cursor:'pointer'});
        notice.addEventListener('click', () => { notice.remove(); openUpdates(); });
        document.body.append(notice); setTimeout(() => notice.remove(), 9000);
      }
    } catch (error) {
      if (releases) releases.error = error.message;
      else releases = {version:'—', releases:[], error:error.message};
      if (dialog?.open) render();
    } finally { checking = false; }
  }
  function render() {
    dialog.replaceChildren();
    const header = element('div', undefined, 'wb-update-header');
    header.append(element('h2', '版本与更新'), button('关闭', () => dialog.close()));
    dialog.append(header, element('p', `当前版本 v${releases?.version || '…'} · 更新由你决定`, 'wb-update-meta'));
    if (releases?.error) dialog.append(element('p', releases.error, 'wb-update-meta'));
    const actions = element('div', undefined, 'wb-update-actions');
    const releasesUrl = releases?.releases_url || '';
    if (releasesUrl) allowedReleasePrefix = releasesUrl;
    actions.append(button('重新检查', () => check()));
    if (releasesUrl) actions.append(link('打开 Releases', releasesUrl));
    const latest = releases?.latest;
    if (native && latest?.installable) {
      const download = button('下载更新', async () => {
        download.disabled = true;
        try {
          const result = await pywebview.api.download_update(latest.id);
          if (result.error) status.textContent = result.error;
          else watchDownload();
        } catch (error) { status.textContent = error.message; }
        finally { download.disabled = false; }
      }, true);
      actions.append(download);
    } else if (latest && !latest.installable) {
      dialog.append(element('p', '该版本尚未提供 Windows EXE 或校验文件，可在 Releases 查看。', 'wb-update-meta'));
    }
    dialog.append(actions);
    const status = element('p', releases?.update?.message || '', 'wb-update-meta');
    status.id = 'wbUpdateStatus'; dialog.append(status);
    const install = button('重启并安装更新', async () => {
      install.disabled = true;
      try {
        const result = await pywebview.api.apply_update();
        if (result.error) status.textContent = result.error;
      } catch (error) { status.textContent = error.message; }
      finally { install.disabled = false; }
    }, true);
    install.id = 'wbUpdateInstall'; install.hidden = !releases?.update?.ready; dialog.append(install);
    const history = element('div', undefined, 'wb-update-history');
    history.append(element('h3', '历史版本'));
    for (const row of releases?.releases || []) {
      const item = element('section', undefined, 'wb-update-release');
      item.append(element('h3', row.name), element('p', `${row.tag} · ${row.date ? new Date(row.date).toLocaleString() : ''}`, 'wb-update-meta'),
        element('pre', row.body), link('查看此版本', row.url));
      history.append(item);
    }
    if (!releases?.releases?.length) history.append(element('p', '暂无已发布版本。', 'wb-update-meta'));
    dialog.append(history);
    if (native) {
      const runtime = element('p', releases?.runtime || '运行环境正在准备…', 'wb-update-meta');
      runtime.id = 'wbRuntimeStatus'; dialog.append(runtime);
      dialog.append(button('重试准备运行环境', () => pywebview.api.retry_runtime()));
      watchDownload();
    }
  }
  function watchDownload() {
    clearInterval(poll);
    let polling = false;
    poll = setInterval(async () => {
      if (!dialog?.open) { clearInterval(poll); return; }
      if (polling) return;
      polling = true;
      try {
        const result = await pywebview.api.update_status();
        document.getElementById('wbUpdateStatus').textContent = result.message;
        document.getElementById('wbRuntimeStatus').textContent = result.runtime;
        document.getElementById('wbUpdateInstall').hidden = !result.ready;
        if (releases) releases.update = result;
      } finally { polling = false; }
    }, 1000);
  }
  async function openUpdates() {
    if (!dialog) {
      dialog = element('dialog', undefined, 'wb-update-dialog');
      dialog.setAttribute('aria-label', '版本与更新'); document.body.append(dialog);
      dialog.addEventListener('click', e => { if (e.target === dialog) dialog.close(); });
      dialog.addEventListener('close', () => clearInterval(poll));
    }
    render(); if (!dialog.open) dialog.showModal(); await check();
  }
  async function initializeNative() {
    if (initialized) return;
    // ⚠️ **必须先判断 pywebview 是否存在**。
    //
    // 这个函数被绑在页面上的「重新连接」按钮
    //（`index.html`: `onclick="WBDesktop.retryLocal()"`）。
    // 而在**安卓 WebView / 普通浏览器**里根本没有 `pywebview` 这个全局对象 ——
    // 直接 `await pywebview.api.desktop_login()` 会抛
    // `ReferenceError: pywebview is not defined`，
    // 被 catch 后显示成「本地连接失败：pywebview is not defined」。
    //
    // 用户看到的是「连不上本地服务」，实际服务完全正常（curl 200）。
    // 这个误导性提示在真机排查时浪费了大量时间。
    if (!window.pywebview?.api) {
      // 非桌面端：不标记 initialized（下次仍可重试），
      // 并给出**准确的**说明而不是抛异常。
      const status = document.getElementById('localBootStatus');
      if (status) {
        status.textContent = window.WBMobile
          ? '手机端请稍候，正在准备本地服务…'
          : '当前不是桌面客户端环境，请在桌面版中使用此功能';
      }
      return;
    }
    initialized = true; native = true;
    try {
      const result = await pywebview.api.desktop_login();
      TOKEN = result.token; localStorage.setItem(TOKEN_KEY, TOKEN);
      // Existing persisted token may already have entered the application.
      if (document.getElementById('app').classList.contains('hidden')) enterApp();
      document.getElementById('localBoot').remove();
      document.getElementById('topbarMode').textContent = '本地运行';
    } catch (error) {
      document.getElementById('localBootStatus').textContent = `本地连接失败：${error.message}`;
      initialized = false;
    }
    check(true);
  }
  fetch('/api/app/version').then(r => r.json()).then(data => {
    if (!releases) { releases = {...data, releases:[]}; label(); }
  }).catch(() => {});
  window.addEventListener('pywebviewready', initializeNative);
  document.getElementById('sideNav').addEventListener('click', event => {
    const item = event.target.closest('[data-tab]');
    if (item) document.getElementById('topbarTitle').textContent = [...item.childNodes]
      .filter(node => node.nodeType === Node.TEXT_NODE).map(node => node.textContent).join('').trim();
  });
  if (window.pywebview?.api) initializeNative();
  return {
    openUpdates,
    retryLocal: initializeNative,
    // ── 供 `android/web/mobile.js` 调用 ──────────────────────────────
    //
    // 安卓端在 `WBMobile.initialize()` 里调了 `WBDesktop.checkUpdatesAtStartup()`，
    // 但这里**从来没导出过这个名字** → 抛 TypeError。
    //
    // 后果连锁：`WBMobile.initialize()` 在「移除 localBoot 引导层」**之前**
    // 就抛了异常 → 用户界面永远停在「正在打开本地工作台…」。
    // 而 WebView 默认不转发 console 日志，所以看起来像「服务连不上」。
    //
    // 修法：把它导出成一个**实际启动更新检查**的函数（与桌面端启动时
    // 的行为一致：`check(true)` 表示「自动检查」——不弹窗，只更新角标）。
    checkUpdatesAtStartup: () => {
      try { check(true); } catch (_) { /* 更新检查失败不该影响进主界面 */ }
    },
  };
})();
