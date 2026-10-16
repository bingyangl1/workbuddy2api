"""main.py 子进程日志转发的健壮性测试。

## 背景（这是真实事故）

用户在中文 Windows 上跑 `python main.py`，控制台出现：

    Exception in thread Thread-1 (_pump):
    UnicodeDecodeError: 'gbk' codec can't decode byte 0x9a in position 60

根因：`subprocess.Popen(..., text=True)` **没指定 encoding** → 用系统默认编码
（中文 Windows = GBK），而 uvicorn 输出 UTF-8 → `stream.readline()` 抛
UnicodeDecodeError，转发线程当场死掉。

危害远不止「少几行日志」：`_pump` 的死会让**管道再没人排空**，uvicorn 在
access log 上阻塞 → 事件循环冻住 → 端口还在监听但任何请求都不返回
（main.py 的 docstring 里记录过这个「僵死」现象）。

## 本测试覆盖三层防线

1. `Popen` 显式传 `encoding="utf-8", errors="replace"`
2. `_pump` 的读循环能扛住 readline 抛 UnicodeDecodeError（丢弃坏行继续读）
3. 坏行连续抛异常时不会变成 CPU 空转死循环（有上限）
"""
import io
import os
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class _FakeStream:
    """模拟 Popen 的文本流，支持「抛出解码错误」与「正常行」交替。"""

    def __init__(self, items):
        # items: list of ("line", str) 或 ("err", Exception) 或 ("eof", None)
        self._items = list(items)
        self._i = 0

    def readline(self):
        if self._i >= len(self._items):
            return ""          # EOF
        kind, payload = self._items[self._i]
        self._i += 1
        if kind == "err":
            raise payload
        if kind == "eof":
            return ""
        return payload


class PumpRobustnessCase(unittest.TestCase):
    def _run_pump(self, stream, log_path: Path):
        import main as m
        t = threading.Thread(target=m._pump, args=(stream, log_path, "t"), daemon=True)
        t.start()
        t.join(timeout=5)
        return t.is_alive()

    def test_readline_decode_error_does_not_kill_pump(self):
        """readline 抛 UnicodeDecodeError 时必须继续读，不能死线程。"""
        log = Path(self._tmp("a.log"))
        stream = _FakeStream([
            ("line", "正常第一行\n"),
            ("err", UnicodeDecodeError("gbk", b"\x9a", 0, 1, "illegal multibyte sequence")),
            ("line", "坏行之后仍要能读到这一行\n"),
            ("eof", None),
        ])
        alive = self._run_pump(stream, log)
        self.assertFalse(alive, "_pump 没能在读完后退出（可能死循环）")
        text = log.read_text(encoding="utf-8", errors="replace")
        self.assertIn("正常第一行", text)
        self.assertIn("坏行之后仍要能读到这一行", text,
                      "解码错误之后必须继续读取后续行（否则管道积压 → 子进程僵死）")

    def test_bounded_on_persistent_decode_errors(self):
        """持续解码失败时必须有上限，不能 CPU 空转死循环。"""
        log = Path(self._tmp("b.log"))
        # 2000 次连续错误：上限 50，应很快收手并进 finally
        stream = _FakeStream([
            ("err", UnicodeDecodeError("gbk", b"\x9a", 0, 1, "x")) for _ in range(2000)
        ])
        t0 = time.time()
        alive = self._run_pump(stream, log)
        elapsed = time.time() - t0
        self.assertFalse(alive, "_pump 卡住了")
        self.assertLess(elapsed, 4, f"_pump 耗时过长（{elapsed:.1f}s），可能没在限额后收手")

    def test_drain_stops_at_eof(self):
        """EOF 时必须立刻结束（不能无限等）。"""
        log = Path(self._tmp("c.log"))
        stream = _FakeStream([("eof", None)])
        t0 = time.time()
        alive = self._run_pump(stream, log)
        self.assertFalse(alive)
        self.assertLess(time.time() - t0, 3)

    def test_log_write_failure_does_not_kill_pump(self):
        """日志文件写不进去（如目录变成文件）也不能死线程。"""
        import main as m
        bad = Path(self._tmp("d")) / "not-a-dir" / "x.log"   # 父目录不存在 → open 失败
        stream = _FakeStream([("line", "hello\n"), ("eof", None)])
        alive = self._run_pump(stream, bad)
        self.assertFalse(alive, "写盘失败时 _pump 也应当能读完退出")

    @staticmethod
    def _tmp(name: str) -> str:
        d = Path(os.environ.get("TEMP", ".")) / "wb-pump-test"
        d.mkdir(parents=True, exist_ok=True)
        return str(d / name)


class PopenEncodingCase(unittest.TestCase):
    """静态检查：源码里不得再出现「text=True 但不给 encoding」的 Popen。"""

    def test_all_popen_have_explicit_encoding(self):
        import re
        src = (Path(__file__).resolve().parent.parent / "main.py").read_text(encoding="utf-8")
        # 找出所有 Popen(...) 调用块
        blocks = re.findall(r"subprocess\.Popen\((.*?)\n\s*\)", src, re.DOTALL)
        self.assertTrue(blocks, "没解析到 Popen 调用（源码结构变了？）")
        for b in blocks:
            has_text = "text=True" in b or "universal_newlines" in b
            if has_text:
                self.assertIn(
                    "encoding=", b,
                    "Popen(text=True) 必须显式指定 encoding —— 否则中文 Windows 用 GBK，"
                    "而 uvicorn 输出 UTF-8，readline() 会抛 UnicodeDecodeError 打死转发线程。\n"
                    f"问题代码块：\n{b}",
                )


if __name__ == "__main__":
    unittest.main()
