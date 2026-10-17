"""#5/#2/#4/#6 需求的防回归测试。

## 每个测试对应的用户需求

1. UsageLog.realm ——「模型日志和用量要求模型区分国际版和国内版，
   同模型不同版本不能混计」
2. GroupEditDialog 显示倍率 ——「按价格建立分组不好判断模型倍率」
3. playground account_id ——「按国际版、国内版选择 key 或账号去测试」
4. app_source realms ——「客户端目录需要可以配置国际版目录与国内版目录」
"""
import os
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = Path(__file__).resolve().parent.parent
PROXY = ROOT / "admin" / "routers" / "proxy.py"
LOGS = ROOT / "admin" / "routers" / "logs.py"
STATS = ROOT / "admin" / "routers" / "stats.py"
PG = ROOT / "admin" / "routers" / "playground.py"
APPSRC = ROOT / "admin" / "routers" / "app_source.py"
MODELS_PAGE = ROOT / "web" / "app" / "(main)" / "models" / "page.tsx"
GROUPS_PAGE = ROOT / "web" / "app" / "(main)" / "models" / "page.tsx"
PG_PAGE = ROOT / "web" / "app" / "(main)" / "playground" / "page.tsx"
APPSRC_PAGE = ROOT / "web" / "app" / "(main)" / "settings" / "app-source" / "page.tsx"


class UsageLogRealmCase(unittest.TestCase):
    """#5：用量日志必须记版本。"""

    @classmethod
    def setUpClass(cls):
        from admin.models import UsageLog
        cls.src = (ROOT / "admin" / "models.py").read_text(encoding="utf-8")
        i = cls.src.find("class UsageLog(Base)")
        j = cls.src.find("class ", i + 10)
        cls.seg = cls.src[i:j]
        cls.UsageLog = UsageLog

    def test_model_has_realm_column(self):
        self.assertIn("realm", self.UsageLog.__table__.columns,
                      "**UsageLog 必须有 realm 列** —— 同模型两版计费不同，"
                      "不记版本就无法分开统计")

    def test_record_usage_writes_realm(self):
        src = PROXY.read_text(encoding="utf-8")
        m = re.search(r"def _record_usage\(.*?(?=\ndef |\nclass )", src, re.S)
        self.assertIsNotNone(m, "找不到 _record_usage")
        body = m.group(0)
        # 必须**真的赋值**给 UsageLog(realm=...)，不能只查 billing_realm 出现
        #（反向验证抓到过假绿：mutation 删掉赋值但注释里仍有那个词）。
        self.assertRegex(
            body, r"realm\s*=\s*billing_realm",
            "**写入 UsageLog 时必须带 realm=billing_realm** —— "
            "漏了的话每行日志都是默认 cn，global 的用量被算进 cn")
        # billing_realm 的解析必须考虑账号自身 realm（裸名请求场景）
        self.assertIn("account_id", body,
                      "解析 realm 时要查账号自身 realm（裸名请求时 "
                      "requested_model 没有前缀可用）")

    def test_logs_filter_supports_realm(self):
        src = LOGS.read_text(encoding="utf-8")
        self.assertIn("realm", src,
                      "logs 路由应支持 realm 过滤参数")
        # _build_filters 里要有 realm 条件
        m = re.search(r"def _build_filters.*?(?=\n@|\ndef )", src, re.S)
        self.assertIsNotNone(m, "找不到 _build_filters")
        self.assertIn("UsageLog.realm", m.group(0),
                      "**_build_filters 必须过滤 UsageLog.realm** —— "
                      "列表/导出与统计口径不一致会让人误判")

    def test_stats_filter_supports_realm(self):
        src = STATS.read_text(encoding="utf-8")
        self.assertRegex(src, r"UsageLog\.realm\s*==",
                         "stats 路由应支持按 realm 过滤统计")

    def test_export_csv_has_realm_column(self):
        src = LOGS.read_text(encoding="utf-8")
        m = re.search(r'w\.writerow\(\["ID".*?\]\)', src, re.S)
        self.assertIsNotNone(m, "找不到 CSV 表头")
        self.assertIn("版本", m.group(0),
                      "**导出 CSV 必须带版本列** —— 否则对账时两版仍混在一起")


class GroupDialogMultiplierCase(unittest.TestCase):
    """#2：分组编辑器显示积分倍率。"""

    @classmethod
    def setUpClass(cls):
        cls.src = MODELS_PAGE.read_text(encoding="utf-8")

    def test_dialog_receives_full_configs(self):
        self.assertIn(
            "allModels={configs}",
            self.src,
            "**分组编辑器必须收到完整 ModelConfig（含倍率）** —— "
            "只传 model_id 字符串数组时倍率信息被丢弃，"
            "按价格建分组无从判断")

    def test_dialog_shows_multiplier(self):
        self.assertIn("credit_multiplier", self.src,
                      "分组编辑器应渲染倍率（免费/x 倍角标）")

    def test_dialog_sorted_by_price(self):
        self.assertRegex(
            self.src, r"sort.*credit_multiplier",
            "分组编辑器列表应按倍率排序（免费在前），便于按价格勾选")


class PlaygroundAccountCase(unittest.TestCase):
    """#4：测试台可按版本选具体账号。"""

    @classmethod
    def setUpClass(cls):
        cls.backend = PG.read_text(encoding="utf-8")
        cls.frontend = PG_PAGE.read_text(encoding="utf-8")

    def test_backend_accepts_account_id(self):
        self.assertIn("account_id", self.backend,
                      "playground 后端应接受 account_id（手动选号）")

    def test_backend_realm_guard(self):
        """**行为级**：不一致分支必须真的 return 400。

        只查 `"realm_mismatch" in src` 会假绿 —— mutation 把条件改成
        `if False and ...` 时那个字符串还在，断言照样通过。
        所以必须断言「不一致时 return JSONResponse(400)」这条路径存在。
        """
        src = self.backend
        m = re.search(
            r"if\s+want_realm\s+and\s+acc_realm\s+and\s+acc_realm\s*!=\s*want_realm:"
            r"\s*\n\s*return\s+JSONResponse\(",
            src)
        self.assertIsNotNone(
            m, "**realm 不一致时必须直接 return 400** —— "
               "条件被弱化（如 `if False and ...`）时拿 cn 账号测 global 模型，"
               "混用身份出站（风控特征）")
        self.assertIn("realm_mismatch", src)

    def test_backend_key_id_recorded(self):
        m = re.search(r"_record_usage\(\s*int\(body\.key_id or 0\)", self.backend)
        self.assertIsNotNone(m, "指定 Key 时记账要记到那把 Key 上")

    def test_frontend_has_account_selector(self):
        self.assertIn("accountAuto", self.frontend,
                      "前端应有账号选择器（含「自动选号」选项）")

    def test_frontend_filters_by_realm(self):
        self.assertRegex(
            self.frontend, r"a\.realm === realm",
            "**账号列表要按当前版本过滤** —— 否则切到国际版还能选到国内账号")


class AppSourceRealmCase(unittest.TestCase):
    """#6：逆向产物识别两版目录。"""

    @classmethod
    def setUpClass(cls):
        cls.backend = APPSRC.read_text(encoding="utf-8")
        cls.frontend = APPSRC_PAGE.read_text(encoding="utf-8")

    def test_status_exposes_realms(self):
        """**行为级**：返回 dict 里必须真的包含 realms 键。

        只查 `"realms" in src` 会假绿 —— mutation 把 `\"realms\": realms_out,`
        换成注释时源码里仍出现 realms（变量名 realms_out 也在）。
        """
        m = re.search(r'return\s*\{[^}]*"realms"\s*:\s*realms_out', self.backend, re.S)
        self.assertIsNotNone(
            m, "**status 返回值必须包含 realms 键** —— "
               "主探测只报一个版本目录，另一版装了也看不见（用户实测反馈）")

    def test_detect_supports_realm_param(self):
        self.assertRegex(
            cls_src(self.backend), r"def detect\(realm",
            "detect 应支持 realm 参数（分版本扫描）")

    def test_frontend_shows_both_realms(self):
        self.assertIn("realms", self.frontend,
                      "前端应展示两版安装目录区块")


def cls_src(s: str) -> str:
    return s


if __name__ == "__main__":
    unittest.main()
