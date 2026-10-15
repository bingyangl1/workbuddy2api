"""本地快照备份 / 恢复路由（P5）。

安全要点：
- **全部端点 require_admin**（非 require_admin_read）——快照含账号凭据，
  只读 token 绝不能拿到它。
- 导出/导入写 audit_logs（backup.export / backup.restore）。
- 恢复前自动留底（pre-restore-*.json，含真实凭据，可完整还原）。
- 文件名白名单校验（admin/backup.py 里 _SAFE_NAME）防路径穿越。
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from admin import backup as backup_mod
from admin.audit import current_actor, log_request
from admin.config import settings
from admin.db import get_db
from admin.security import require_admin

router = APIRouter(prefix="/api/backup", tags=["backup"])


def _dir() -> Path:
    """备份目录（settings.BACKUP_DIR，默认 <部署根>/backups）。"""
    return backup_mod.backup_dir(Path(settings.BACKUP_DIR))


class ExportIn(BaseModel):
    name: str = ""
    include_secrets: bool = True


class RestoreIn(BaseModel):
    name: str


@router.get("/list")
def list_backups(_: bool = Depends(require_admin)):
    """备份文件列表（名字/大小/时间；不含内容）。"""
    return {"items": backup_mod.list_snapshots(_dir())}


@router.post("/export")
def export_backup(body: ExportIn, request: Request,
                  _: bool = Depends(require_admin), db: Session = Depends(get_db)):
    """导出当前库快照到备份目录。include_secrets=False 时敏感列脱敏。"""
    payload = backup_mod.export_snapshot(db, include_secrets=body.include_secrets)
    name = body.name.strip() or None
    if not name:
        from datetime import datetime
        stamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
        name = f"snapshot-{stamp}.json"
    if not name.endswith(".json"):
        name += ".json"
    try:
        path = backup_mod.write_snapshot(_dir(), payload, name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    stats = backup_mod.snapshot_stats(payload)
    log_request(current_actor(), "backup.export", target=path.name,
                detail=f"rows={sum(stats.values())} secrets={body.include_secrets}",
                request=request)
    return {"ok": True, "name": path.name, "size": path.stat().st_size,
            "tables": stats, "include_secrets": body.include_secrets}


@router.get("/download/{name}")
def download_backup(name: str, _: bool = Depends(require_admin)):
    """下载一份备份文件（原始 JSON）。"""
    try:
        path = _dir() / name
        if not backup_mod._SAFE_NAME.fullmatch(name):
            raise ValueError("非法备份文件名")
        data = path.read_bytes()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except OSError:
        raise HTTPException(status_code=404, detail="备份文件不存在")
    return Response(content=data, media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.post("/restore")
def restore_backup(body: RestoreIn, request: Request,
                   _: bool = Depends(require_admin), db: Session = Depends(get_db)):
    """从备份文件恢复（覆盖式）。**恢复前自动留底**。

    整个导入在单事务内完成，失败整体回滚（不留半份数据）。
    """
    directory = _dir()
    try:
        payload = backup_mod.read_snapshot(directory, body.name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="备份文件不存在")
    except Exception as e:  # noqa: BLE001  损坏的 JSON
        raise HTTPException(status_code=400, detail=f"备份文件无法解析: {e}")

    # 恢复前留底：没有退路不能做覆盖式恢复
    try:
        safeguard = backup_mod.pre_restore_backup(db, directory)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"恢复前留底失败，已中止: {e}")

    try:
        result = backup_mod.restore_snapshot(db, payload)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"恢复失败，已回滚: {e}")

    log_request(current_actor(), "backup.restore", target=body.name,
                detail=f"restored={sum(result['restored'].values())} safeguard={safeguard.name}",
                request=request)
    return {"ok": True, "restored": result["restored"], "skipped": result["skipped"],
            "safeguard": safeguard.name}


@router.get("/download/{name}/preview")
def preview_backup(name: str, _: bool = Depends(require_admin)):
    """预览一份备份的元信息与每表行数（不返回行内容）。"""
    try:
        payload = backup_mod.read_snapshot(_dir(), name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="备份文件不存在")
    return JSONResponse({
        "name": name,
        "format": payload.get("format"),
        "generated_at": payload.get("generated_at"),
        "include_secrets": payload.get("include_secrets"),
        "tables": backup_mod.snapshot_stats(payload),
    })
