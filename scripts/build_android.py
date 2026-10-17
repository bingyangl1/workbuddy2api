"""Build an arm64 APK with an explicitly staged backend; no local credentials."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STAGING = ROOT / "android/app/build/generated/python"


def check_runtime() -> None:
    # Run the staged app against Android's pinned dependency versions. Gradle
    # compilation alone cannot catch import-time FastAPI/Pydantic errors.
    environment = ROOT / 'build/android-runtime-check'
    python = environment / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    if not python.is_file():
        venv.EnvBuilder(with_pip=True).create(environment)
    # ⚠️ pip 源必须**可配置**，不能写死国内镜像。
    #
    # 原来写死清华源（`pypi.tuna.tsinghua.edu.cn`）：在国内开发机上确实快，
    # 但 GitHub Actions 的 runner 在海外 —— 连国内镜像反而超时：
    #
    #     ReadTimeoutError: HTTPSConnectionPool(
    #         host='pypi.tuna.tsinghua.edu.cn', port=443): Read timed out.
    #     ...重试 5 次后失败
    #
    # CI 上应该用官方 PyPI（runner 到 pypi.org 是快的）。
    # 所以：优先读环境变量 `WB_PIP_INDEX_URL`，其次国内默认值（本地友好），
    # CI 里显式设成官方源即可。
    index_url = os.getenv('WB_PIP_INDEX_URL',
                          'https://pypi.tuna.tsinghua.edu.cn/simple')
    subprocess.run([str(python), '-m', 'pip', 'install', '--disable-pip-version-check',
                    '-r', str(ROOT / 'android/requirements.txt'),
                    '--index-url', index_url,
                    '--retries', '5', '--timeout', '60'], check=True)
    subprocess.run([str(python), str(ROOT / 'scripts/check_runtime.py'), '--android',
                    '--source-dir', str(STAGING)], cwd=ROOT, check=True)


def mobile_html(html: str) -> str:
    html = re.sub(r'<button\b[^>]*data-tab="client".*?</button>', '', html, flags=re.S)
    html = re.sub(r'<section id="panel-client".*?</section>', '', html, flags=re.S)
    html = re.sub(r'<section id="panel-settings".*?</section>', '', html, flags=re.S)
    settings = (ROOT / "android/web/settings.html").read_text(encoding="utf-8")
    html = re.sub(r'<section id="panel-sync".*?</section>', lambda _: settings, html, flags=re.S)
    html = html.replace('</svg> 同步设置</button>', '</svg> 设置</button>')
    html = re.sub(r'/\* ==================== 线上同步设置.*?(?=/\* ==================== 客户端参数)',
                  'function loadSyncConfig() {}\n\n', html, flags=re.S)
    html = re.sub(r'<button\b[^>]*onclick="scanLocal\(\)".*?</button>', '', html, flags=re.S)
    html = re.sub(r'^.*\[`injectAcc\(.*\n', '', html, flags=re.M)
    html = re.sub(r'async function injectAcc\(.*?(?=/\* 猫猫旅行)', '', html, flags=re.S)
    html = re.sub(r'/\* 扫描本机账号.*?(?=async function refreshAll\()', '', html, flags=re.S)
    html = re.sub(r'/\* ==================== 客户端参数.*?(?=/\* ==================== 定时任务)', '', html, flags=re.S)
    html = html.replace(',"client"', '').replace(',"settings"', '')
    html = re.sub(r'^.*if \(name === "(?:client|settings)"\).*\n', '', html, flags=re.M)
    html = re.sub(r'<button\b[^>]*data-tab="settings".*?</button>', '', html, flags=re.S)
    html = html.replace('登录新账号，或从本机与备份中恢复已有账号。', '手机号登录或浏览器授权，账号保存在这台手机。')
    html = html.replace('也可选择本地 .info 文件。', '也可选择本地 .info 文件。新版 Windows 加密凭证请先在电脑账号管理页导出“系统明文备份”后再导入，或使用手机号授权登录。')
    html = html.replace('<link rel="stylesheet" href="/static/desktop.css" />',
                        '<link rel="stylesheet" href="/static/desktop.css" /><link rel="stylesheet" href="/static/mobile.css" />')
    html = html.replace('</body>', '<script src="/static/mobile.js"></script><script src="/static/mobile_settings.js"></script>\n</body>')
    return html


def prepare() -> None:
    # Do not allow a symlink or changed working directory to redirect recursive cleanup.
    staging = STAGING.resolve()
    allowed = (ROOT / "android/app/build").resolve()
    if not staging.is_relative_to(allowed) or staging != allowed / "generated/python":
        raise RuntimeError("Unexpected Android staging path")
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    for path in (ROOT / "admin").rglob("*.py"):
        relative = path.relative_to(ROOT)
        # ⚠️ **不要在这里排除任何 admin 模块**。
        #
        # 上游那版排除了两个：
        #     app_source.py             —— 拆包浏览工具
        #     routers/client_profile.py —— 客户端参数页
        #
        # 那是**按上游自己的 server.py 写的**。而我们的 `admin/server.py`
        # 是**无条件 import 两者**的：
        #
        #     from admin.routers import (..., app_source, ...,
        #                                benefits, client_profile, ...)
        #     app.include_router(app_source.router)
        #     app.include_router(client_profile.router)
        #
        # 排掉之后 APK 里的运行时直接
        #     ImportError: cannot import name 'app_source' from 'admin.routers'
        # 起不来 —— CI 的 `check_runtime --android` 一跑就报出来了。
        #
        # 权衡：这两个都是**只读页面/工具**，打包进去只多几十 KB；
        # 而 import 失败是**应用起不来**。留着显然更划算。
        #
        # 真要不带这些功能，应该在 server.py 里做**条件挂载**
        #（如 `if not is_android():`），而不是在打包时偷偷删文件 ——
        # 那种「源码里有、产物里没有」的不一致最难查。
        target = staging / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    for name in ("upstream_compat.py", "anthropic_adapter.py", "responses_adapter.py",
                 "responses_projection.py", "desensitize.py", "wb_install.py", "converter.py"):
        shutil.copy2(ROOT / name, staging / name)
    (staging / "desktop/assets").mkdir(parents=True)
    for name in ("__init__.py", "common.py", "runtime.py", "updates.py"):
        shutil.copy2(ROOT / "desktop" / name, staging / "desktop" / name)
    shutil.copy2(ROOT / "desktop/assets/app.svg", staging / "desktop/assets/app.svg")
    metadata = json.loads((ROOT / "desktop/version.json").read_text(encoding="utf-8"))
    metadata["asset"] = "WorkBuddy2API-{version}-android-arm64.apk"
    (staging / "desktop/version.json").write_text(json.dumps(metadata), encoding="utf-8")
    shutil.copytree(ROOT / "admin/static", staging / "admin/static")
    shutil.copytree(ROOT / "admin/data", staging / "admin/data")
    index = staging / "admin/static/index.html"
    index.write_text(mobile_html(index.read_text(encoding="utf-8")), encoding="utf-8")
    for name in ("mobile.js", "mobile.css", "mobile_settings.js"):
        shutil.copy2(ROOT / "android/web" / name, staging / "admin/static" / name)

    # ── 新面板（P3 静态导出产物）──────────────────────────────────
    #
    # APK 里**两套面板都打包**：
    #
    #   旧面板  admin/static/index.html  → /admin（移动端适配版）
    #   新面板  web/out                  → /panel（Next.js 静态导出）
    #
    # 默认落点由 `ADMIN_DEFAULT_PANEL` 决定（`android_runtime.py` 里设为 next）。
    # 保留旧面板是为了「新面板出问题时能一键回退」，成本只是几 MB。
    #
    # ⚠️ 必须先 `cd web && NEXT_OUTPUT_EXPORT=1 NEXT_PUBLIC_BASE_PATH=/panel npm run build`。
    # 没构建就跳过（`--prepare-only` 在无 Node 环境下仍要能用），
    # 但**打 APK 时会显式报错**（见下方 `require_panel` 参数）。
    web_out = ROOT / "web" / "out"
    if web_out.is_dir() and (web_out / "index.html").is_file():
        shutil.copytree(web_out, staging / "web/out")
        print(f"  新面板已打包：{len(list((staging / 'web/out').rglob('*')))} 个条目")
    else:
        print("  ⚠️ 未找到 web/out（新面板未构建）—— APK 里将只有旧面板")
    for name in ("android_runtime.py", "mobile_http.py"):
        shutil.copy2(ROOT / "android/python" / name, staging / name)
    shutil.copy2(ROOT / "LICENSE", staging / "LICENSE")
    print("Prepared mobile backend and UI (no desktop parameter page or scan tools)")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--with-debug", action="store_true", help="Also build the isolated emulator debug APK")
    parser.add_argument("--gradle", help="Gradle 8.11.1 executable")
    parser.add_argument("--jdk", help="JDK 17 directory")
    parser.add_argument("--sdk", help="Android SDK directory")
    args = parser.parse_args()
    prepare()
    if args.prepare_only:
        return
    check_runtime()
    env = dict(os.environ, WB_ANDROID_BUILD_PYTHON=sys.executable)
    if args.jdk:
        env["JAVA_HOME"] = str(Path(args.jdk).resolve())
    if args.sdk:
        env["ANDROID_HOME"] = str(Path(args.sdk).resolve())
    if env.get("JAVA_HOME"):
        env["PATH"] = str(Path(env["JAVA_HOME"]) / "bin") + os.pathsep + env.get("PATH", "")
    gradle = args.gradle or shutil.which("gradle")
    if not gradle:
        raise SystemExit("Set --gradle to Gradle 8.11.1 and provide JDK 17 / Android SDK")
    # Keep one private signing identity for all upgrades. Never upload these files.
    signing = ROOT / "build/android-signing"
    signing.mkdir(parents=True, exist_ok=True)
    credentials = signing / "signing.json"
    keystore = signing / "release.jks"
    if credentials.is_file():
        config = json.loads(credentials.read_text(encoding="utf-8"))
    else:
        config = {"password": secrets.token_urlsafe(40)}
        credentials.write_text(json.dumps(config), encoding="utf-8")
    env.update(WB_ANDROID_KEYSTORE=str(keystore), WB_ANDROID_STORE_PASSWORD=config["password"],
               WB_ANDROID_KEY_PASSWORD=config["password"])
    if not keystore.is_file():
        keytool = str(Path(env["JAVA_HOME"]) / "bin/keytool") if env.get("JAVA_HOME") else "keytool"
        subprocess.run([keytool, "-genkeypair", "-keystore", str(keystore), "-storetype", "JKS",
                        "-storepass:env", "WB_ANDROID_STORE_PASSWORD", "-keypass:env", "WB_ANDROID_KEY_PASSWORD",
                        "-alias", "workbuddy2api", "-keyalg", "RSA", "-keysize", "3072",
                        "-validity", "10000", "-dname", "CN=WorkBuddy2API Android"], env=env, check=True)
    tasks = ["assembleRelease", "lintRelease"]
    if args.with_debug:
        tasks.append("assembleDebug")
    subprocess.run([gradle, "--no-daemon", *tasks], cwd=ROOT / "android", env=env, check=True)
    version = json.loads((ROOT / "desktop/version.json").read_text(encoding="utf-8"))["version"]
    output = ROOT / "dist/android"
    output.mkdir(parents=True, exist_ok=True)
    target = output / f"WorkBuddy2API-{version}-android-arm64.apk"
    shutil.copy2(ROOT / "android/app/build/outputs/apk/release/app-release.apk", target)
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    target.with_suffix(".apk.sha256").write_text(f"{digest}  {target.name}\n", encoding="ascii")
    print(f"Built {target.name} ({target.stat().st_size / 1048576:.1f} MB)")


if __name__ == "__main__":
    main()
