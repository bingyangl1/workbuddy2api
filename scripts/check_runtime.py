"""启动自检：确认应用能在**全新的空数据库**上起来并响应核心请求。

# 为什么需要

打包（尤其是 Android APK）时最容易犯的错是**漏打包某个模块**：
本地跑得好好的，装到手机上却 `ModuleNotFoundError` 起不来。
而 APK 构建要二十分钟 —— 等到那时才发现很浪费。

这个脚本在一份**全新的临时 SQLite 库**上把应用真的拉起来，
依次请求几个核心端点。任何 import 链断了 / 建表失败 / 路由缺失都会立刻暴露。

# 与其他测试的区别

* 不读开发者的 `.env`（显式清掉 `ADMIN_*` 等环境变量）
* 不碰开发者的数据库（临时目录 + SQLite）
* **不访问外网**（只请求本机 TestClient）
* 与 `verify_tests.py` 互补：那个测「测试灵不灵」，这个测「应用起不起来」

# 用法

    python scripts/check_runtime.py            # 桌面/服务端形态
    python scripts/check_runtime.py --android  # 安卓形态（设 WORKBUDDY_ANDROID=1）
"""
from __future__ import annotations

import argparse
import os
import secrets
import sys
import tempfile
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--android", action="store_true",
                        help="模拟安卓形态（WORKBUDDY_ANDROID=1）")
    parser.add_argument("--source-dir", type=Path,
                        default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()

    sys.path.insert(0, str(args.source_dir.resolve()))
    # 绝不读开发者的 .env / 不碰他们的账号库
    for name in list(os.environ):
        if name.startswith(("ADMIN_", "CONVERTER_", "WORKBUDDY_", "WB_DESKTOP_")):
            os.environ.pop(name)

    # ⚠️ 用 `mkdtemp` + 自己清理，而不是 `TemporaryDirectory`。
    #
    # Windows 上 SQLAlchemy 的 SQLite 连接池在进程内持有文件句柄，
    # `TemporaryDirectory.__exit__` 立刻 rmtree 会撞上
    # `PermissionError: [WinError 32] 另一个程序正在使用此文件`。
    # 手动清理时可以先 `engine.dispose()` 放掉连接。
    work = tempfile.mkdtemp(prefix="wb-runtime-check-")
    try:
        return _run(args, work)
    finally:
        try:
            from admin.db import engine
            engine.dispose()
        except Exception:      # noqa: BLE001
            pass
        import gc
        import shutil
        gc.collect()
        shutil.rmtree(work, ignore_errors=True)


def _run(args, work: str) -> int:
    os.environ.update({
        "WORKBUDDY_ANDROID" if args.android else "WORKBUDDY_DESKTOP": "1",
        "ADMIN_DATABASE_URL":
            "sqlite:///" + (Path(work) / "check.db").as_posix(),
        "ADMIN_GATEWAY_LIMIT_BACKEND": "memory",
        "ADMIN_REDIS_URL": "",
        "ADMIN_USERNAME": "runtime-check",
        "ADMIN_PASSWORD": secrets.token_urlsafe(32),
        "ADMIN_JWT_SECRET": secrets.token_urlsafe(48),
        # 打包形态默认关闭逆向工具（APK 里不该带拆包器）
        "ADMIN_DEV_TOOLS": "0",
        # 上游护栏：本脚本不主动打上游，但设上更保险
        "ADMIN_UPSTREAM_READONLY_ALL": "1",
        # 面板静态托管：APK 里要把面板打进去
        "ADMIN_PANEL_STATIC": "1",
        "ADMIN_PANEL_PATH": "/panel",
    })

    print(f"  形态     : {'android' if args.android else 'desktop/server'}")
    print(f"  临时库   : {work}/check.db")
    print()

    # ① 核心 import 链（漏打包最先在这里炸）
    try:
        import fastapi  # noqa: F401
        import pydantic  # noqa: F401
        from fastapi.testclient import TestClient

        from admin.server import app
        from admin.db import engine
        from admin.security import create_admin_token
    except Exception as e:      # noqa: BLE001
        print(f"   ❌ import 失败：{type(e).__name__}: {e}")
        return 1
    print("   ✅ import 链完整")

    if engine.dialect.name != "sqlite":
        print(f"   ❌ 期望 sqlite，实际 {engine.dialect.name}")
        return 1
    print("   ✅ 数据库方言 = sqlite")

    # ② 真的起服务并请求
    failures: list[str] = []
    # 面板检查项**按形态**决定：
    #
    #   * 服务端 / 桌面端：新旧两个面板都要在（`ADMIN_DEFAULT_PANEL` 可切）
    #   * **安卓端：只有旧面板** —— `build_android.py` 只打包
    #     `admin/static`（旧面板）+ 移动版 CSS/JS，**不复制 `web/out`**。
    #
    # 早先这里无条件检查 `/panel/`，于是 APK 构建时必然 404
    #（CI 实测踩过：`Preflight check` 步骤直接失败）。
    panel_checks = [("/api/app/version", "版本"), ("/admin", "旧面板")]
    if not args.android:
        panel_checks.append(("/panel/", "新面板"))
    try:
        with TestClient(app) as client:
            for path, label in panel_checks:
                r = client.get(path)
                ok = r.status_code == 200
                print(f"   {'✅' if ok else '❌'} GET {path:<22} "
                      f"{r.status_code}  {label}")
                if not ok:
                    failures.append(f"{path} -> {r.status_code}")

            # 鉴权：未带 token 必须被拒
            r = client.get("/api/accounts")
            ok = r.status_code in (401, 403)
            print(f"   {'✅' if ok else '❌'} GET /api/accounts 未登录 "
                  f"{r.status_code}（期望 401/403）")
            if not ok:
                failures.append("/api/accounts 未鉴权")

            headers = {"X-Admin-Token": create_admin_token()}
            for path, label in [("/api/accounts", "账号列表"),
                                ("/api/keys", "密钥列表"),
                                ("/api/schedules", "定时任务"),
                                ("/api/benefits/campaign", "限时权益"),
                                ("/api/growth/records", "成长审计")]:
                r = client.get(path, headers=headers)
                ok = r.status_code == 200
                print(f"   {'✅' if ok else '❌'} GET {path:<26} "
                      f"{r.status_code}  {label}")
                if not ok:
                    failures.append(f"{path} -> {r.status_code}")
    except Exception as e:      # noqa: BLE001
        print(f"   ❌ 启动/请求失败：{type(e).__name__}: {e}")
        return 1

    print()
    if failures:
        print(f"   ❌ {len(failures)} 项失败：{failures}")
        return 1
    print("   ✅ 启动自检全部通过（应用能起来、核心端点正常）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
