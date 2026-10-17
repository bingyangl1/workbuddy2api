"""文档链接必须指向真实文件。

## 这条测试的由来

`docs-dev/README.md` 的索引里长期列着 4 份文档：

    realm.md / security.md / panel.md / usage.md

状态栏写着「⏳ 待补」，但它们**从来没被创建过** ——
所有读者点进去都是 404，而索引本身看起来"很完整"。

这类问题正向检查发现不了（没人会逐个点），但它让文档的可信度归零：
**读者一旦遇到一次失效链接，就不会再相信这个索引。**

## 本测试检查什么

1. `docs-dev/*.md` 里的相对链接都指向真实文件
2. 主 `README.md` 指向 `docs-dev/` 的链接都真实存在
3. 索引里提到 `.md` 的名字必须真实存在（防「文件名写错」）
"""
import os
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = Path(__file__).resolve().parent.parent
DOCS_DEV = ROOT / "docs-dev"


def _md_files(base: Path) -> list[Path]:
    return sorted(p for p in base.rglob("*.md") if p.is_file())


#: 匹配 markdown 链接里的目标（排除 http/https/mailto/锚点）
_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")


def _relative_targets(text: str) -> list[str]:
    """抽出所有「相对路径」形式的链接目标。"""
    out = []
    for m in _LINK_RE.finditer(text):
        target = m.group(1).strip()
        # 去掉标题 `path "title"` 形式
        target = target.split('"')[0].strip()
        if not target:
            continue
        if target.startswith(("http://", "https://", "mailto:", "#")):
            continue
        # 去掉锚点
        target = target.split("#")[0].strip()
        if not target:
            continue          # 纯锚点链接
        out.append(target)
    return out


class DocsDevLinksCase(unittest.TestCase):
    def test_docs_dev_dir_exists(self):
        self.assertTrue(DOCS_DEV.is_dir(), "docs-dev/ 目录不存在")
        self.assertTrue(_md_files(DOCS_DEV), "docs-dev/ 下没有任何 .md")

    def test_all_links_resolve(self):
        """每份文档里的相对链接都要指向真实文件。"""
        broken: list[str] = []
        for doc in _md_files(DOCS_DEV):
            text = doc.read_text(encoding="utf-8")
            for target in _relative_targets(text):
                # 目标相对「当前文档所在目录」解析
                resolved = (doc.parent / target).resolve()
                if not resolved.exists():
                    rel_doc = doc.relative_to(ROOT).as_posix()
                    broken.append(f"{rel_doc} → {target}")
        self.assertEqual(
            broken, [],
            "**文档里有失效链接**（读者点进去 404）：\n  " + "\n  ".join(broken))

    def test_index_lists_only_existing_files(self):
        """索引里出现的 `xxx.md` 名字必须真实存在。

        这条专门防「索引写了但文件没建」—— 正是 realm.md 等 4 份文档
        曾经的问题。

        ## 只检查「裸文件名」

        `../CHANGELOG.md`、`../PLAN-fork.md` 这类**带路径**的引用指向
        docs-dev 之外，由 `test_all_links_resolve` 负责验证。
        这里只抓「光写了个 `xxx.md`，看起来像本目录文件」的情况。
        """
        index = DOCS_DEV / "README.md"
        self.assertTrue(index.is_file(), "docs-dev/README.md 不存在")
        text = index.read_text(encoding="utf-8")

        # 抽「看起来像本目录文件」的引用：
        #   * markdown 链接 [x](name.md) 且 name 不含 `/`
        #   * 反引号包裹的 `name.md` 且不含 `/`
        mentioned: set[str] = set()
        for m in _LINK_RE.finditer(text):
            t = m.group(1).split("#")[0].strip()
            if t and "/" not in t and t.endswith(".md"):
                mentioned.add(t)
        for m in re.finditer(r"`([\w\-]+\.md)`", text):
            mentioned.add(m.group(1))

        missing = [n for n in sorted(mentioned)
                   if not (DOCS_DEV / n).is_file()]
        self.assertEqual(
            missing, [],
            "**索引里提到了这些文件但它们不存在**："
            f"{missing}。要么创建它，要么从索引里删掉"
            "（引用 docs-dev 之外的文件请写成带路径的形式，如 `../XXX.md`）。")

    def test_main_readme_docs_links(self):
        """主 README 指向 docs-dev/ 的链接都要真实。"""
        readme = ROOT / "README.md"
        if not readme.is_file():
            self.skipTest("没有主 README")
        text = readme.read_text(encoding="utf-8")
        broken = []
        for target in _relative_targets(text):
            if "docs-dev/" not in target and not target.endswith(".md"):
                continue
            resolved = (ROOT / target).resolve()
            if not resolved.exists():
                broken.append(target)
        self.assertEqual(broken, [],
                         f"主 README 有失效链接：{broken}")


class DocumentedNumbersCase(unittest.TestCase):
    """文档里写的数字要与仓库实际一致。

    索引的「约定」里写了：**文档里的数字要能复现**。
    这条测试把它变成可执行的检查 —— 否则数字会悄悄过期。
    """

    def test_mutation_count_matches_docs(self):
        """ENGINEERING-BASELINE 说的 mutation 数量要与实际一致。"""
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "verify_tests_doc_probe", ROOT / "scripts" / "verify_tests.py")
        mod = importlib.util.module_from_spec(spec)
        # ⚠️ **必须先注册到 sys.modules**。
        #
        # `verify_tests.py` 用了 `@dataclass`，而 dataclass 在解析注解时
        # 会去 `sys.modules[cls.__module__]` 找模块 —— 没注册就会拿到 None，
        # 抛 `AttributeError: 'NoneType' object has no attribute '__dict__'`。
        # 这是 `importlib.util` 动态加载的经典坑，不是脚本本身的问题。
        sys.modules[spec.name] = mod
        try:
            spec.loader.exec_module(mod)          # type: ignore[union-attr]
        except Exception as e:      # noqa: BLE001
            sys.modules.pop(spec.name, None)
            self.skipTest(f"无法加载 verify_tests：{e}")
        actual = len(mod.MUTATIONS)

        doc = DOCS_DEV / "ENGINEERING-BASELINE.md"
        self.assertTrue(doc.is_file(), "工程基线文档不存在")
        text = doc.read_text(encoding="utf-8")
        # 兼容两种写法：
        #   表格行：  | 反向验证 mutation | **34 条**（全部有效） |
        #   正文：    跑全部 34 条
        m = re.search(r"mutation[^|\n]*\|\s*\**(\d+)\s*条", text) or \
            re.search(r"(\d+)\s*条[^\n]{0,12}mutation", text)
        self.assertIsNotNone(
            m, "工程基线文档里应写明 mutation 数量"
               "（形如「反向验证 mutation | 34 条」）")
        documented = int(m.group(1))
        self.assertEqual(
            documented, actual,
            f"文档写 {documented} 条 mutation，实际有 {actual} 条 —— "
            "改了清单请同步文档（这条测试就是为了防止数字过期）")

    def test_page_count_matches_docs(self):
        """panel.md 说的页面数量要与实际一致。"""
        pages = list((ROOT / "web" / "app").rglob("page.tsx"))
        if not pages:
            self.skipTest("没有 web/app 页面")
        actual = len(pages)

        doc = DOCS_DEV / "panel.md"
        self.assertTrue(doc.is_file())
        text = doc.read_text(encoding="utf-8")
        m = re.search(r"(\d+)\s*个页面", text)
        self.assertIsNotNone(m, "panel.md 里应写明页面数量")
        documented = int(m.group(1))
        self.assertEqual(
            documented, actual,
            f"panel.md 写 {documented} 个页面，实际 {actual} 个")


if __name__ == "__main__":
    unittest.main()
