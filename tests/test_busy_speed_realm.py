"""B：繁忙度 / 测速端点必须按 realm 选号。

## 为什么（两版是两个上游域）

`/api/models/busy` 与 `/api/models/speed-test` 都要**真实调用上游**，
而两版的上游是**两个不同的域**：

    cn     → copilot.tencent.com / codebuddy.cn
    global → www.workbuddy.ai

所以：
* **繁忙度**是各自独立的事实 —— 拿国内版账号问出来的数值去标注国际版模型，
  数字本身就是错的（而且用户会据此做调度决策）。
* **测速**更严重：原实现把出站 URL 写死成 `settings.BACKEND`
  （国内版基址），于是「用国际版账号测速」会变成
  **国际版凭证打国内版端点** → 上游直接判非法，而且错误/冷却会记到那个
  无辜的国际版账号上（明明是我们发错了地方）。

## 本测试覆盖

* `/busy` 接受 realm 并在挑号时按 realm 过滤（不同版本各用各的账号）
* `/speed-test` 接受 realm，且**出站基址来自账号 realm 而非全局 settings**
* 无账号时的报错**指明是哪一版**没有账号
* 缓存键隐含 realm（按账号分键，不同版本账号必然不同）
"""
import inspect
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


def _db():
    from admin.db import Base
    from admin import models as _m  # noqa: F401
    engine = create_engine("sqlite://", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine, tables=[
        Base.metadata.tables["accounts"],
        Base.metadata.tables["model_configs"],
    ])
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()


def _mk(db, uid, realm, balance=100, status="active"):
    from admin.models import Account
    a = Account(uid=uid, realm=realm, status=status, balance_remain=balance,
                auth_json='{"auth":{"accessToken":"x"}}')
    db.add(a)
    db.commit()
    return a


class ModelsForRealmCase(unittest.TestCase):
    """`_models_for_realm()`：模型清单必须按 realm 过滤，**且剥掉本地前缀**。

    ## 这个 bug 的实测证据

    两版模型清单交集**只有 5 个**：

        cn     32 个：auto / hy3 / hy4-preview / …
        global 18 个：default-model / fast-model / balanced-model / deep-model / …

    而 `/busy` 原实现取 `ModelConfig` **全表** → 把国内版 27 个模型 id
    一并发给国际版账号 → 上游根本不认识 → 请求失败 → 返回 `stale=True`
    （前端显示一屏 unknown，看起来像「上游不可用」）。

    ## 为什么必须剥前缀

    `global:` / `cn:` 是**本地**的 realm 隔离标记；上游只认**裸模型名**。
    带着前缀发过去，上游同样不认识。
    """

    def setUp(self):
        self.db = _db()
        from admin.models import ModelConfig
        for mid in ("hy3", "auto", "cn:hy4-preview",
                    "global:default-model", "global:balanced-model"):
            self.db.add(ModelConfig(level="system", model_id=mid, enabled=1))
        self.db.commit()
        from admin.routers.models import _models_for_realm
        self.fn = _models_for_realm

    def tearDown(self):
        self.db.close()

    def test_cn_excludes_global_models(self):
        got = self.fn(self.db, "cn")
        self.assertNotIn("default-model", got,
                         "cn 的清单里不该出现国际版模型")
        self.assertIn("hy3", got)
        self.assertIn("auto", got)

    def test_global_excludes_cn_models(self):
        """**核心**：global 只能拿到带 global: 前缀的那些（并剥掉前缀）。"""
        got = self.fn(self.db, "global")
        self.assertEqual(sorted(got), ["balanced-model", "default-model"],
                         "global 应只含国际版模型且已剥掉前缀")
        self.assertNotIn("hy3", got, "国内版模型不能混进国际版清单")

    def test_prefix_stripped(self):
        """上游只认裸名 —— 前缀是本地标记，必须剥掉。"""
        for r in ("cn", "global"):
            for mid in self.fn(self.db, r):
                self.assertNotIn(":", mid,
                                 f"{r} 的模型名仍带本地前缀：{mid!r}")

    def test_empty_realm_returns_all_unchanged(self):
        """realm='' → 全表且**不剥前缀**（既有行为，零回归）。"""
        got = self.fn(self.db, "")
        self.assertIn("global:default-model", got,
                      "不传 realm 时应保持原样（旧的调用方依赖这个形态）")
        self.assertEqual(len(got), 5)

    def test_realm_with_no_models_returns_empty(self):
        """该版没有模型 → 空列表（而不是拿另一版的顶上）。"""
        from admin.models import ModelConfig
        self.db.query(ModelConfig).filter(
            ModelConfig.model_id.like("global:%")).delete(synchronize_session=False)
        self.db.commit()
        self.assertEqual(self.fn(self.db, "global"), [])


class BusyRealmCase(unittest.TestCase):
    def setUp(self):
        self.db = _db()
        _mk(self.db, "c1", "cn")       # 先建 → id 更小（原实现必然选它）
        _mk(self.db, "g1", "global")

    def tearDown(self):
        self.db.close()

    def test_signature_has_realm(self):
        from admin.routers.models import model_busy
        params = inspect.signature(model_busy).parameters
        self.assertIn("realm", params)
        self.assertEqual(params["realm"].default, "", "默认空 = 不过滤（零回归）")

    def test_get_busy_levels_accepts_realm(self):
        from admin.model_busy import get_busy_levels
        params = inspect.signature(get_busy_levels).parameters
        self.assertIn("realm", params)

    def test_picks_realm_specific_account(self):
        """不同 realm 必须挑到各自的账号。

        用 mock 拦住真实上游调用，只观察「选中了哪个账号」——
        打进 `_cache` 的 key 首元素就是 account.id（见 get_busy_levels）。
        """
        from unittest import mock
        from admin import model_busy as mb
        picked = {}

        class _FakeSess:
            def __init__(self, auth):
                pass
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False
            def fetch_model_busy(self, batch):
                return {}
            def updated_json(self):
                return '{"auth":{"accessToken":"x"}}'

        with mock.patch.object(mb, "backend", mock.MagicMock(
                AccountSession=_FakeSess), create=True):
            for r in ("cn", "global"):
                mb._cache.clear()
                mb.get_busy_levels(self.db, ["m1"], realm=r)
                keys = list(mb._cache.keys())
                self.assertTrue(keys, f"realm={r} 未产生缓存条目（可能没选到账号）")
                picked[r] = keys[0][0]
        self.assertNotEqual(picked["cn"], picked["global"],
                            "两个 realm 挑到了同一个账号 —— realm 过滤没生效")

    def test_no_account_for_realm_returns_unknown(self):
        """该版没账号 → 返回 unknown 而不是拿另一版的数值冒充。"""
        from admin.models import Account
        self.db.query(Account).filter(Account.realm == "global").delete()
        self.db.commit()
        from admin import model_busy as mb
        mb._cache.clear()
        r = mb.get_busy_levels(self.db, ["m1"], realm="global")
        self.assertEqual(r["levels"], {"m1": "unknown"})
        self.assertTrue(r["stale"])
        self.assertEqual(r["realm"], "global")

    def test_realm_echoed(self):
        from admin import model_busy as mb
        mb._cache.clear()
        r = mb.get_busy_levels(self.db, ["m1"], realm="cn")
        self.assertEqual(r.get("realm"), "cn")


class SpeedTestRealmCase(unittest.TestCase):
    """测速端点：不仅挑号要按 realm，**出站基址**也必须按账号 realm。"""

    @classmethod
    def setUpClass(cls):
        cls.src = (Path(__file__).resolve().parent.parent
                   / "admin" / "routers" / "models.py").read_text(encoding="utf-8")

    def test_signature_reads_realm_from_body(self):
        from admin.routers.models import speed_test
        doc = inspect.getdoc(speed_test) or ""
        self.assertIn("realm", doc, "文档要说明 realm 的作用")

    def test_url_not_hardcoded_to_settings_backend(self):
        """**核心**：出站 URL 不能写死 settings.BACKEND。

        原实现 `url = f"{settings.BACKEND}/v2/chat/completions"` 导致
        「国际版账号测速 → 打国内版端点」。
        """
        i = self.src.find("async def speed_test")
        j = self.src.find("@router.", i + 10)
        body = self.src[i:j if j > 0 else len(self.src)]
        # 允许出现在注释里（说明历史），但不能出现在实际赋值语句里
        import re
        assigns = [ln for ln in body.splitlines()
                   if "settings.BACKEND" in ln and not ln.strip().startswith("#")]
        self.assertEqual(assigns, [],
                         f"测速的出站基址不能写死 settings.BACKEND 实际使用：{assigns}")

    def test_url_uses_realm_chat_base(self):
        i = self.src.find("async def speed_test")
        j = self.src.find("@router.", i + 10)
        body = self.src[i:j if j > 0 else len(self.src)]
        self.assertIn("realm_mod.chat_base", body,
                      "出站基址必须按账号 realm 取（国际版打国际版端点）")
        self.assertIn("realm_mod.chat_paths", body,
                      "路径候选也要按 realm（两版的路径回落方向相反）")

    def test_account_pick_filters_realm(self):
        i = self.src.find("async def speed_test")
        j = self.src.find("@router.", i + 10)
        body = self.src[i:j if j > 0 else len(self.src)]
        self.assertIn("Account.realm == realm", body,
                      "挑号必须按 realm 过滤")

    def test_error_names_the_realm(self):
        i = self.src.find("async def speed_test")
        j = self.src.find("@router.", i + 10)
        body = self.src[i:j if j > 0 else len(self.src)]
        self.assertIn("国内版", body, "报错要指明是哪一版没有账号")
        self.assertIn("国际版", body)


if __name__ == "__main__":
    unittest.main()
