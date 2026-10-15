"""Read-only release information for the browser UI; installing is native-only."""
from functools import lru_cache
import tempfile
import os
import secrets
from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import FileResponse
from admin.security import require_admin
from desktop.common import ROOT, VERSION, RELEASES_URL
from desktop.updates import UpdateService

router = APIRouter(prefix="/api/app", tags=["releases"])


@router.get("/version")
def version():
    return {"version": VERSION, "releases_url": RELEASES_URL}


@router.get("/icon")
def icon():
    return FileResponse(ROOT / "desktop/assets/app.svg", media_type="image/svg+xml")


@router.get("/desktop-ready")
def desktop_ready(x_desktop_boot: str = Header(default="")):
    expected = os.getenv("WB_DESKTOP_BOOT", "")
    if not expected or not secrets.compare_digest(expected, x_desktop_boot):
        raise HTTPException(status_code=404)
    return {"ready": True}


@lru_cache(maxsize=1)
def service():
    # The server version never downloads or installs executables.
    base = Path(tempfile.gettempdir()) / "workbuddy2api-releases"
    base.mkdir(exist_ok=True)
    return UpdateService(base)


@router.get("/releases", dependencies=[Depends(require_admin)])
def releases():
    return service().check()


# ── Changelog 查看器（P5）────────────────────────────────────────────
# 读部署目录 CHANGELOG.md（Keep a Changelog 格式），离线可用，结构化返回
# 供前端按版本折叠渲染、标记当前版本。只读；文件缺失返回空段落而非 500。

@lru_cache(maxsize=1)
def _changelog_raw() -> str:
    """读 CHANGELOG.md 全文（缓存；部署期内不变）。找不到返回空串。"""
    path = ROOT / "CHANGELOG.md"
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _parse_changelog(text: str) -> list[dict]:
    """把 Keep a Changelog 文本解析成 [{version, date, sections:[{title, items[]}]}]。

    段落头形如 `## [0.1.0] - 2026-10-15` 或 `## [Unreleased]`；
    小节头 `### 新增/修复/改进/安全`；条目 `- ...`（多行条目以两空格缩进续行）。
    """
    versions: list[dict] = []
    cur: dict | None = None
    cur_section: dict | None = None
    for line in text.splitlines():
        if line.startswith("## "):
            head = line[3:].strip()
            # [0.1.0] - 2026-10-15  /  [Unreleased]  /  [0.1.0] - 基线
            version = head
            date = ""
            if head.startswith("[") and "]" in head:
                version = head[1:head.index("]")]
                rest = head[head.index("]") + 1:].lstrip(" -")
                date = rest
            cur = {"version": version, "date": date, "sections": []}
            versions.append(cur)
            cur_section = None
        elif line.startswith("### ") and cur is not None:
            cur_section = {"title": line[4:].strip(), "items": []}
            cur["sections"].append(cur_section)
        elif line.startswith("- ") and cur_section is not None:
            cur_section["items"].append(line[2:].strip())
        elif cur_section is not None and cur_section["items"] and line.startswith("  "):
            # 续行（缩进）并入上一条
            cur_section["items"][-1] += " " + line.strip()
    return versions


@router.get("/changelog", dependencies=[Depends(require_admin)])
def changelog():
    """部署目录 CHANGELOG.md 的结构化版本段（离线可用）。"""
    versions = _parse_changelog(_changelog_raw())
    return {"current_version": VERSION, "versions": versions}

