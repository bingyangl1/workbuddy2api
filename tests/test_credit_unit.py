"""积分单位的单复数容忍 —— 真实事故的回归测试。

## 事故（用户实测发现：国际版「可用积分 350 只显示 100」）

上游同一批积分数据里，`CapacityUnit` 有**两种写法**：

    Free Plan Subscription → `CapacityUnit = "credits"`（复数）
    Bonus Pack（gift 包）  → `CapacityUnit = "credit"` （**单数**）

原实现用严格相等过滤：

    if row.get("CapacityUnit", "credits") != "credits":
        continue                      # ← gift 包被整包丢掉

后果（两层，第二层更严重）：
  1. 界面：国际版账号有 100（Free Plan）+ 250（Bonus Pack）= 350 可用积分，
     但只显示 100 —— 用户直接看到的现象。
  2. 调度：那 250 被完全忽略 → 「快到期优先用」策略不会去消耗它
     → 上游给的 `CycleEndTime = 2026-10-26`（**会过期**）→ 到期作废。

## 实测证据（单次只读请求，1 个国际版账号）

    POST /billing/meter/get-user-resource-free-packages
    → 200，Accounts 里两个包：
        TCACA_code_035_ArVxJcGDsm  CapacityUnit="credits"  CycleCapacityRemain=100
        TCACA_code_006_DbXS0lrypC  CapacityUnit="credit"   CycleCapacityRemain=250
                                   CycleEndTime="2026-10-26 00:18:17"

参数与客户端**逐项一致**（PageNumber/PageSize/Status/PackageCodes/SlicePeriod*），
所以客户端拿到的也是这份数据 —— 差别只在我们这边把单数单位过滤掉了。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from admin.billing import _is_credit_unit


class CreditUnitCase(unittest.TestCase):
    def test_plural_and_singular_both_accepted(self):
        """复数（credits）与单数（credit）都必须接受。"""
        self.assertTrue(_is_credit_unit("credits"), "复数应接受")
        self.assertTrue(_is_credit_unit("credit"), "**单数必须接受**（gift 包就是这样）")

    def test_case_insensitive(self):
        for v in ("Credits", "CREDIT", "CrEdItS", " credit ", "credit "):
            self.assertTrue(_is_credit_unit(v), f"{v!r} 应接受")

    def test_missing_or_empty_treated_as_credit(self):
        """缺省或空值视为积分（老数据/部分接口不带该字段）。"""
        for v in (None, "", "  "):
            self.assertTrue(_is_credit_unit(v), f"{v!r} 应视为积分")

    def test_other_units_rejected(self):
        """非积分单位必须拒绝（否则会把 token 计数的行混进积分汇总）。"""
        for v in ("tokens", "token", "requests", "usd", "cny", "credits_usd"):
            self.assertFalse(_is_credit_unit(v), f"{v!r} 不该算作积分单位")


class FilterBehaviorCase(unittest.TestCase):
    """直接测合并逻辑：单数单位的行不得被丢弃。"""

    def test_merge_keeps_singular_unit_row(self):
        from admin.billing import _identity
        # 模拟上游两行（复数 + 单数）
        rows = [
            {"ResourceId": "r1", "PackageCode": "TCACA_code_035_ArVxJcGDsm",
             "CapacityUnit": "credits", "CycleCapacityRemain": 100},
            {"ResourceId": "r2", "PackageCode": "TCACA_code_006_DbXS0lrypC",
             "CapacityUnit": "credit", "CycleCapacityRemain": 250,
             "CycleEndTime": "2026-10-26 00:18:17"},
        ]
        merged = {}
        for row in rows:
            if not _is_credit_unit(row.get("CapacityUnit")):
                continue
            merged[_identity(row)] = {**merged.get(_identity(row), {}), **row}
        self.assertEqual(len(merged), 2, "两个包都该保留（修复前只剩 1 个）")
        codes = {r["PackageCode"] for r in merged.values()}
        self.assertIn("TCACA_code_006_DbXS0lrypC", codes,
                      "**gift 包（单数单位）必须保留** —— 它带着 250 积分和到期时间")

    def test_credits_precise_fields_used_when_present(self):
        """上游给的是 `CycleCapacityRemain`（非 Precise 版）时也要能读出来。

        实测：gift 包的 `CapacityRemain` 是 0，真实剩余在 `CycleCapacityRemain`；
        `*Precise` 字段是字符串（"250"）。
        """
        from admin.credits import resource_summary
        row = {
            "PackageCode": "TCACA_code_006_DbXS0lrypC",
            "CapacityUnit": "credit",
            "CapacityType": 1,
            "CapacityRemain": 0, "CapacitySize": 0, "CapacityUsed": 0,
            "CycleCapacityRemain": 250, "CycleCapacitySize": 250,
            "CycleCapacityUsedPrecise": "0",
            "CycleCapacitySizePrecise": "250",
            "CycleCapacityRemainPrecise": "250",
            "CycleEndTime": "2026-10-26 00:18:17",
            "DeductionEndTime": 1792945097000,
        }
        s = resource_summary(row)
        self.assertEqual(s["total"], 250.0, f"总量应为 250，实际 {s['total']}")
        self.assertEqual(s["remaining"], 250.0, f"剩余应为 250，实际 {s['remaining']}")
        self.assertIsNotNone(s["expire_at"], "必须有到期时间（上游 CycleEndTime 给了 10/26）")


if __name__ == "__main__":
    unittest.main()
