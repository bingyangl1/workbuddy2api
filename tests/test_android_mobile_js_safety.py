"""`mobile.js` 顶层不能访问不存在的 DOM 元素。

## 真机实测发现的真正根因（OnePlus 9）

界面永久停在「正在打开本地工作台…」，点「重新连接」报
`pywebview is not defined`，但**内嵌服务完全正常**（curl 200）。

### 根因链（三层，越往下越致命）

1. `admin/static/desktop.js` 没导出 `checkUpdatesAtStartup`
   → mobile.js 调它时 TypeError（**已修**）

2. 移除引导层的顺序排在可能失败的操作之后（**已修**）

3. **`mobile.js` 的 IIFE 顶层直接访问不存在的元素**（本次修）：

       document.querySelector('.api-share-card').append(diagnosticButton);

   而 `.api-share-card` 在 `index.html` **里根本不存在**
   （`grep -c 'api-share-card' admin/static/index.html` = 0）。

   → `null.append()` 抛 TypeError → **整个 IIFE 中途崩溃**
   → `window.WBMobile` 从来没被赋值
   → Java 端 `window.WBMobile && WBMobile.initialize(...)` **静默跳过**
   → 引导层永不消失

### 为什么这一层最难查

* `&&` 短路让注入**无声无息地不发生**，logcat 里一行错误都没有
* 服务是好的，所以看起来像网络问题
* 只有把 `mobile.js` 单独拿到 node 里跑（带最小 DOM 桩）才会看到
  `TypeError: Cannot set properties of null`

**教训**：IIFE 顶层的 DOM 访问必须全部判空。一处崩溃 = 整个模块不导出。
"""
import os
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = Path(__file__).resolve().parent.parent
MOBILE_JS = ROOT / "android" / "web" / "mobile.js"
INDEX_HTML = ROOT / "admin" / "static" / "index.html"


class TopLevelDomSafetyCase(unittest.TestCase):
    """检查 `mobile.js` 里每处 `querySelector(...)` 用法是否安全。"""

    @classmethod
    def setUpClass(cls):
        cls.src = MOBILE_JS.read_text(encoding="utf-8")
        cls.lines = cls.src.splitlines()

    def _is_guarded(self, idx: int, line: str) -> bool:
        """判断这一行的 querySelector 结果是否被安全使用。

        安全的三种写法：
          1. 可选链：`document.querySelector(x)?.foo`
          2. 赋值给变量后再判空（我们人工确认过的那种）
          3. 在 `if (...)` 条件里
        """
        s = line.strip()
        if "?." in s.split("document.querySelector", 1)[-1]:
            return True
        if re.match(r"^(const|let|var)\s+\w+\s*=", s):
            return True          # 赋给变量，后面应判空（由下条测试管）
        if s.startswith("if ") or " if (" in s:
            return True
        # 出现在 `querySelector(...)` 后直接 `.xxx =` / `.append` 的，不安全
        return False

    def test_no_unguarded_top_level_query(self):
        """顶层（缩进 2 空格）的 querySelector 必须安全。"""
        bad = []
        for n, line in enumerate(self.lines, 1):
            # IIFE 内的顶层 = 恰好 2 空格缩进
            if not re.match(r"^  \S", line):
                continue
            if "document.querySelector" not in line:
                continue
            s = line.strip()
            if s.startswith("//"):
                continue
            if not self._is_guarded(n, line):
                bad.append(f"L{n}: {s[:80]}")
        self.assertEqual(
            bad, [],
            "**IIFE 顶层的 querySelector 必须判空** —— 一处崩溃会让整个 "
            f"mobile.js 不导出 WBMobile，引导层永久卡死：{bad}")

    def test_known_missing_selectors_are_guarded(self):
        """`.api-share-card` 在 index.html 里不存在，必须显式判空。"""
        self.assertIn(
            "const shareCard = document.querySelector('.api-share-card')",
            self.src,
            "`.api-share-card` 必须先赋给变量再判空 —— "
            "它在当前 index.html 里不存在，直接 .append 会崩")
        self.assertNotRegex(
            self.src,
            r"document\.querySelector\('\.api-share-card'\)\.append",
            "**禁止直接对 querySelector 结果调用 .append**")

    def test_arrange_header_guards(self):
        """`arrangeHeader()` 会立刻执行，必须内部判空。"""
        m = re.search(r"function arrangeHeader\(\)\s*\{(.*?)\n  \}",
                      self.src, re.S)
        self.assertIsNotNone(m, "找不到 arrangeHeader()")
        body = m.group(1)
        self.assertRegex(
            body, r"if\s*\(target\s*&&\s*actions\)|if\s*\(!?\s*target",
            "arrangeHeader() 内部必须判空（它在 IIFE 里立刻执行，"
            "任一元素缺失都会让整个脚本崩溃）")


class StagedMobileJsCase(unittest.TestCase):
    """staging 里的 mobile.js 必须与源码一致。"""

    STAGED = (ROOT / "android" / "app" / "build" / "generated"
              / "python" / "admin" / "static" / "mobile.js")

    def test_staging_in_sync(self):
        if not self.STAGED.is_file():
            self.skipTest("staging 未生成")
        self.assertEqual(
            MOBILE_JS.read_text(encoding="utf-8"),
            self.STAGED.read_text(encoding="utf-8"),
            "**staging 与源码不一致** —— 改了 mobile.js 必须重跑 "
            "`build_android.py --prepare-only`，否则 APK 打进旧版本")


class NodeSmokeCase(unittest.TestCase):
    """用 node + 最小 DOM 桩真跑一遍 mobile.js（防再次崩溃）。"""

    def test_loads_without_crashing(self):
        import shutil
        import subprocess
        import tempfile

        node = shutil.which("node")
        if not node:
            self.skipTest("没有 node")

        src = MOBILE_JS.read_text(encoding="utf-8")
        stub = r"""
const store = {};
global.localStorage = {
  getItem: k => (k in store ? store[k] : null),
  setItem: (k, v) => { store[k] = String(v); },
  removeItem: k => { delete store[k]; },
};
// **故意不提供 .api-share-card / .api-share-help**：
// 真实 index.html 里就没有这两个元素，模拟那种情况
global.document = {
  documentElement: { classList: { add: () => {} } },
  getElementById: () => null,
  querySelector: () => null,
  createElement: () => ({ style: {}, addEventListener: () => {}, append: () => {} }),
  addEventListener: () => {},
  execCommand: () => true,
  body: { append: () => {} },
  activeElement: null,
};
global.window = global;
global.navigator = { clipboard: {} };
global.Event = class { constructor(t) { this.type = t; } };
window.addEventListener = () => {};
window.dispatchEvent = () => {};
window.matchMedia = () => ({ matches: false, addEventListener: () => {} });

// index.html 的内联脚本定义了这些（浏览器里是「脚本作用域」的
// let/const，对后续 <script> 可见）。node 里没有那种跨脚本作用域，
// 所以显式声明成全局来模拟同样的可见性。
global.TOKEN_KEY = 'wb_admin_token';
global.TOKEN = '';
// index.html 内联脚本提供的其它工具函数
global.enterApp = () => {};
global.copyText = () => {};
global.toast = () => {};
global.openModal = () => {};
global.api = async () => ({});
global.safeJson = async x => ({ ok: true, data: {} });
global.esc = s => String(s);

__SCRIPT__

if (typeof window.WBMobile === 'undefined') {
  console.log('FAIL: WBMobile 未导出');
  process.exit(3);
}
if (typeof window.WBMobile.initialize !== 'function') {
  console.log('FAIL: initialize 不是函数');
  process.exit(4);
}
window.WBMobile.initialize('tok');
if (localStorage.getItem('wb_admin_token') !== 'tok') {
  console.log('FAIL: token 未写入');
  process.exit(5);
}
console.log('OK');
"""
        stub = stub.replace("__SCRIPT__", src)
        fd, path = tempfile.mkstemp(suffix=".js", prefix="wb_mobile_smoke_")
        os.close(fd)
        Path(path).write_text(stub, encoding="utf-8")
        try:
            proc = subprocess.run([node, path], capture_output=True, timeout=60)
        finally:
            os.unlink(path)

        out = proc.stdout.decode("utf-8", "ignore").strip()
        err = proc.stderr.decode("utf-8", "ignore").strip()
        self.assertEqual(
            proc.returncode, 0,
            f"**mobile.js 在缺元素的环境下崩溃了** —— 这正是真机上的状态。\n"
            f"stdout: {out[:200]}\nstderr: {err[:600]}")
        self.assertIn("OK", out)


if __name__ == "__main__":
    unittest.main()
