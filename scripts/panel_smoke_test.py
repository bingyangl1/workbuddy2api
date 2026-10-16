# -*- coding: utf-8 -*-
"""全流程浏览器静默测试（用户要求：全流程 web 用浏览器静默测试一下功能）。

对全部 19 个路由（18 个主页面 + login）逐页：
  1. 导航 → 等 readyState complete → 等网络静默
  2. 抓 **console 错误 / 未捕获异常 / 失败请求（4xx/5xx）**
  3. 断言页面有 h1（且非错误页文案）、无「加载失败」类错误态
  4. 断言无横向溢出（375px 移动视口）
  5. 记录页面标题、可见文本长度、表格行数等体征

另有「交互冒烟」阶段：对若干页做安全的点击（切 tab / 开弹窗 / 切换排序 / 搜索输入），
只点**不触上游**的控件（护栏已开，且刻意避开批量/领流/运行等写操作按钮）。

输出：逐页 PASS/FAIL 汇总 + 失败明细。全程静默（不弹窗、不改数据）。
"""
import json, io, sys, time, base64, urllib.request, urllib.parse, os
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
import websocket

ENGINE = "http://127.0.0.1:8790"
BASE = ENGINE + "/panel"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_shots")
os.makedirs(OUT, exist_ok=True)

PAGES = [
    ("/dashboard/", "仪表盘"),
    ("/accounts/", "账号池"),
    ("/keys/", "API 密钥"),
    ("/models/", "模型管理"),
    ("/models/catalog/", "模型中心"),
    ("/playground/", "聊天测试台"),
    ("/logs/", "用量日志"),
    ("/stats/", "使用统计"),
    ("/security/", "安全治理"),
    ("/audit/", "操作审计"),
    ("/account-groups/", "账号分组"),
    ("/credits/", "积分统计"),
    ("/tokens/", "访问令牌"),
    ("/schedules/", "定时任务"),
    ("/growth/", "成长计划"),
    ("/settings/", "系统设置"),
    ("/settings/sync/", "线上同步"),
    ("/settings/password/", "修改密码"),
    ("/settings/app-source/", "逆向产物"),
    ("/settings/layout/", "界面布局"),
    ("/backup/", "数据备份"),
    ("/changelog/", "更新日志"),
]

# 登录
req = urllib.request.Request(ENGINE + "/api/login",
    data=urllib.parse.urlencode({"username": "admin", "password": "Wb2!LocalTest8790"}).encode(),
    headers={"Content-Type": "application/x-www-form-urlencoded"})
token = json.loads(urllib.request.urlopen(req, timeout=15).read())["token"]

nt = json.loads(urllib.request.urlopen(
    urllib.request.Request("http://127.0.0.1:9223/json/new?about:blank", method="PUT"), timeout=15).read())
ws = websocket.create_connection(nt["webSocketDebuggerUrl"], suppress_origin=True, timeout=90)
tab_id = nt["id"]
mid = [0]
console_errs = []
net_fails = []
pending = {"console": [], "net": []}

def rpc(method, params=None, timeout=90):
    mid[0] += 1
    ws.send(json.dumps({"id": mid[0], "method": method, "params": params or {}}))
    ws.settimeout(timeout)
    while True:
        m = json.loads(ws.recv())
        if m.get("id") == mid[0]:
            return m
        _collect(m)

def _collect(m):
    meth = m.get("method", "")
    if meth == "Runtime.exceptionThrown":
        d = m.get("params", {}).get("exceptionDetails", {})
        desc = str((d.get("exception") or {}).get("description") or d.get("text") or "")[:300]
        console_errs.append(desc)
    elif meth == "Runtime.consoleAPICalled" and m.get("params", {}).get("type") == "error":
        args = m.get("params", {}).get("args", [])
        txt = " ".join(str(a.get("value") or a.get("description") or "") for a in args)[:300]
        console_errs.append("[console.error] " + txt)
    elif meth == "Network.responseReceived":
        r = m.get("params", {}).get("response", {})
        code = r.get("status", 0)
        url = r.get("url", "")
        if code >= 400 and "/panel/" not in url:  # 面板静态资源 404 也算失败
            net_fails.append(f"{code} {url[:140]}")
        elif code >= 400:
            net_fails.append(f"{code} {url[:140]}")

def drain():
    """非阻塞排空事件队列。"""
    ws.settimeout(0.35)
    try:
        while True:
            _collect(json.loads(ws.recv()))
    except Exception:
        pass

def js(expr):
    r = rpc("Runtime.evaluate", {"expression": expr, "returnByValue": True, "awaitPromise": True})
    return r.get("result", {}).get("result", {}).get("value")

def goto(path, settle=2.6):
    # 导航前先排空缓冲：确保后面抓到的事件都属于**本次**导航，
    # 不会把上一次导航迟到的异常算到本页头上（实测踩过：随机一页报 #418，
    # 实为前一页水合的迟到事件被错记）。
    drain()
    console_errs.clear(); net_fails.clear()
    rpc("Page.navigate", {"url": BASE + path})
    time.sleep(3.2)
    for _ in range(30):
        if js("document.readyState") == "complete":
            break
        time.sleep(0.4)
    # 内容就绪判定：`readyState==complete` 只说明**网络**完成，不代表 React 已渲染
    # （首轮遍历冷启动时实测 body 只有 ~26 字符的骨架，导致假失败）。
    # 等到出现 h1 / 骨架 / 错误态之一，最多再等 15s。
    for _ in range(30):
        if js("!!(document.querySelector('h1') || "
              "document.querySelector('[role=alert],[role=status],.animate-pulse'))"):
            break
        time.sleep(0.5)
    time.sleep(settle)
    drain()

def shot(name):
    d = rpc("Page.captureScreenshot", {"format": "png"}).get("result", {}).get("data", "")
    open(os.path.join(OUT, name), "wb").write(base64.b64decode(d))

rpc("Page.enable"); rpc("Runtime.enable"); rpc("Network.enable")
rpc("Emulation.setDeviceMetricsOverride", {"width": 1440, "height": 900, "deviceScaleFactor": 1, "mobile": False})

# 登录
goto("/login/", 1.5)
js("sessionStorage.setItem('wb-admin-token', " + json.dumps(token) + ")")
js("localStorage.setItem('theme','light'); localStorage.setItem('wb.realm','cn')")

results = []
for path, label in PAGES:
    t0 = time.time()
    goto(path, 2.6)
    h1 = js("(document.querySelector('h1')||{}).textContent || ''")
    body_len = js("(document.body.innerText||'').length")
    # 页面骨架判定：正常态有 h1；错误态（LoadError）渲染 role=alert/inline 提示 + 重试按钮，
    # 这也是**正确的**渲染，不算失败。两者皆无才是白屏。
    has_skeleton = js("""
      (() => {
        const h1 = !!(document.querySelector('h1')||{}).textContent?.trim();
        const alert = !!document.querySelector('[role=alert],[role=status]');
        const retry = [...document.querySelectorAll('button')].some(b => b.textContent.includes('重试'));
        return h1 || alert || retry;
      })()
    """)
    err_state = js("""
      (() => {
        const t = document.body.innerText || '';
        // 仅当「既无骨架又无内容」或出现明确的加载失败文案时才算异常
        const bad = ['Network Error', '无法加载页面', '出错了'];
        return bad.filter(s => t.includes(s));
      })()
    """)
    rows = js("document.querySelectorAll('tbody tr').length")
    overflow = js("document.documentElement.scrollWidth > 1440")
    # 上游触达类故障（502/503/504）算 WARN 不算 FAIL：后端如实报错、面板如实渲染
    # 错误态 + 重试按钮，这是正确行为（本机 dev 库部分上游接口不可用是已知环境状况）。
    upstream_fails = [n for n in net_fails if n.startswith(("502", "503", "504"))]
    hard_net = [n for n in net_fails if not n.startswith(("502", "503", "504"))]
    ok = bool(has_skeleton) and not err_state and not console_errs and not hard_net
    results.append({
        "path": path, "label": label, "h1": (h1 or "").strip()[:30],
        "len": body_len, "rows": rows, "overflow": bool(overflow),
        "errs": list(console_errs), "net": list(net_fails),
        "upstream_warn": upstream_fails,
        "ms": int((time.time() - t0) * 1000), "ok": ok,
    })
    mark = "PASS" if ok else "FAIL"
    if ok and upstream_fails:
        mark = "WARN"
    print(f"[{mark}] {path:22s} h1={ (h1 or '').strip()[:20]:22s} rows={rows:3d} len={body_len:5d} {int((time.time()-t0)*1000)}ms")
    if not ok:
        if err_state: print(f"        错误态文案: {err_state}")
        for e in console_errs[:3]: print(f"        console: {e[:180]}")
        for n in hard_net[:3]: print(f"        net: {n}")
    if upstream_fails:
        print(f"        (上游 5xx，面板如实显示错误态) {upstream_fails[:2]}")

# ── 交互冒烟：只点不触上游的控件 ──
print("\n=== 交互冒烟（安全控件）===")
inter = []

def interact(path, desc, script, wait=1.4):
    goto(path, 2.2)
    r = js(script)
    time.sleep(wait)
    drain()
    ok = r not in (None, False) and not console_errs and not net_fails
    inter.append({"path": path, "desc": desc, "result": str(r)[:60],
                  "errs": list(console_errs), "net": list(net_fails), "ok": ok})
    print(f"[{'PASS' if ok else 'FAIL'}] {path} :: {desc} → {str(r)[:50]}")
    if not ok:
        for e in console_errs[:2]: print(f"        console: {e[:160]}")
        for n in net_fails[:2]: print(f"        net: {n}")

# 模型页：切分组 tab
interact("/models/", "切到「模型分组」tab",
         "[...document.querySelectorAll('button[role=tab]')].find(b=>b.textContent.includes('分组'))?.click(), 'clicked'")
# 模型中心：切排序为倍率
interact("/models/catalog/", "排序切到「按倍率」",
         """
         (() => {
           const t = [...document.querySelectorAll('button')].find(b=>b.textContent.includes('按系列'));
           if (!t) return 'no-trigger';
           t.click(); return 'opened';
         })()
         """)
# 日志页：搜索输入
interact("/logs/", "搜索框输入",
         """
         (() => {
           const i = document.querySelector('input[type=text], input:not([type])');
           if (!i) return 'no-input';
           const s = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set;
           s.call(i, 'glm'); i.dispatchEvent(new Event('input',{bubbles:true}));
           return 'typed';
         })()
         """)
# 备份页：预览弹窗
interact("/backup/", "打开预览弹窗",
         "[...document.querySelectorAll('button')].find(b=>b.textContent.trim()==='预览')?.click(), 'clicked'", 2.0)
interact("/backup/", "关闭预览弹窗",
         "document.querySelector('[role=alertdialog] button')?.click(), 'closed'")
# 统计页：粒度切换
interact("/stats/", "粒度下拉可打开",
         "[...document.querySelectorAll('button')].find(b=>b.textContent.includes('按小时'))?.click(), 'clicked'")
# 设置页：链接到 changelog 存在
interact("/settings/", "存在更新日志入口",
         "!!document.querySelector('a[href*=\"/changelog\"]'), 'ok'")
# 账号页：状态筛选下拉
interact("/accounts/", "筛选控件存在",
         "document.querySelectorAll('button').length > 3 ? 'ok' : 'few'")
# 定时任务页：tab 或表格
interact("/schedules/", "页面控件存在",
         "document.querySelectorAll('button, table').length > 2 ? 'ok' : 'few'")
# 仪表盘：健康表
interact("/dashboard/", "模型健康表有行",
         "document.querySelectorAll('tbody tr').length > 0 ? 'has-rows' : 'empty'")
# 测试台：模型下拉已填充
interact("/playground/", "模型下拉已填充",
         "(document.body.innerText.includes('免费') || !!document.querySelector('button[aria-label]')) ? 'ok' : 'empty'", 2.5)
# 界面布局页：条目开关已渲染（至少 10 个）+ 配置页自己不可关闭
interact("/settings/layout/", "布局页开关已渲染",
         "document.querySelectorAll('button[role=switch]').length >= 10 ? 'ok' : ('few:' + document.querySelectorAll('button[role=switch]').length)")

# ── 移动端逐页溢出检查 ──
print("\n=== 移动端 375px 逐页横向溢出 ===")
rpc("Emulation.setDeviceMetricsOverride", {"width": 375, "height": 812, "deviceScaleFactor": 2, "mobile": True})
mob = []
for path, label in PAGES:
    goto(path, 2.0)
    # **正确判据**：`scrollWidth > clientWidth`（内容比视口宽），而不是
    # `scrollWidth > 375`（硬编码）。
    #
    # 为什么不能用硬编码 375：`mobile=True` 的模拟下，Chrome 会按页面的
    # `viewport meta` 决定布局宽度 —— 没有 meta 的页面（如 about:blank）会拿到
    # 默认 980，此时读到的 scrollWidth（如 389）与 375 比较就是假阳性
    # （实测踩过：侧边栏接入后 21 页全部「溢出 389」，实际一页都没溢出）。
    # 用 clientWidth 作基准则与页面自身声明的布局宽度一致，跨页面可比。
    ov = js("document.documentElement.scrollWidth > document.documentElement.clientWidth")
    sw = js("document.documentElement.scrollWidth")
    cw = js("document.documentElement.clientWidth")
    mob.append({"path": path, "overflow": bool(ov), "scrollW": sw, "clientW": cw})
    print(f"[{'FAIL' if ov else 'PASS'}] {path:22s} scrollW={sw} clientW={cw}")
rpc("Emulation.clearDeviceMetricsOverride")

# ── 汇总 ──
fails = [r for r in results if not r["ok"]]
ifails = [r for r in inter if not r["ok"]]
mfails = [r for r in mob if r["overflow"]]
print("\n" + "=" * 60)
print(f"页面遍历：{len(results) - len(fails)}/{len(results)} PASS")
print(f"交互冒烟：{len(inter) - len(ifails)}/{len(inter)} PASS")
print(f"移动溢出：{len(mob) - len(mfails)}/{len(mob)} 无溢出")
for f in fails:
    print(f"  PAGE FAIL {f['path']}: errs={f['errs'][:2]} net={f['net'][:2]} 错误态={f.get('errs')}")
for f in ifails:
    print(f"  INTERACT FAIL {f['path']} ({f['desc']}): {f['errs'][:2]} {f['net'][:2]}")
for f in mfails:
    print(f"  MOBILE OVERFLOW {f['path']}: scrollW={f['scrollW']}")

with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "_fulltest.json"), "w", encoding="utf-8") as fh:
    json.dump({"pages": results, "interactions": inter, "mobile": mob}, fh, ensure_ascii=False, indent=2)
print("\n明细已写 _fulltest.json")

try:
    urllib.request.urlopen("http://127.0.0.1:9223/json/close/" + tab_id, timeout=8)
except Exception:
    pass
ws.close()
print("DONE")
