"""管理面作用域 API Token（wbt_ 前缀）：给脚本 / 看板 / CI 的只读凭据（Phase 2 T7）。

实现见 admin/tokensvc.py（SHA-256 存储、prefix 定位、常量时间比较）。
明文仅创建时返回一次；库中只存哈希。

鉴权口径：本路由（token 管理本身）全部走 require_admin（JWT only）——
wbt_ 永远不能写，也不能管理自己（连列表都不给：那是管理员视图，
wbt_ 持有者只需要被授权的既有 GET 端点，见 T9 收尾）。
"""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from admin.db import get_db
from admin.security import require_admin
from admin import tokensvc

router = APIRouter(prefix="/api/tokens", tags=["tokens"])


class TokenIn(BaseModel):
    """创建请求体。expires_at 是 ISO 字符串或 null（视图层 parse 成 datetime）。"""
    name: str = ""
    expires_at: str | None = None


class TokenPatch(BaseModel):
    """PATCH 语义：只改真的提交了的字段（extra 字段忽略，None=未提交）。

    pydantic v1/v2 兼容：exclude_unset 语义在两种版本下行为一致。
    """
    name: str | None = None
    enabled: bool | None = None
    scope: str | None = None
    expires_at: str | None = None


def _parse_expires(raw: str) -> datetime:
    """ISO 字符串 → datetime。非法值 400（创建/更新的入参校验点）。"""
    try:
        # fromisoformat：Python 3.11+ 接受 'Z' 后缀；3.10 及以下需要手工替换。
        return datetime.fromisoformat(raw.replace("Z", "+00:00")) if raw.endswith("Z") \
            else datetime.fromisoformat(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail="expires_at 必须是合法的 ISO 时间字符串")


@router.get("")
def list_tokens(_: bool = Depends(require_admin), db: Session = Depends(get_db)):
    return {"items": tokensvc.list_tokens(db)}


@router.post("")
def create_token(body: TokenIn, _: bool = Depends(require_admin), db: Session = Depends(get_db)):
    # TODO(T2): 接入 admin/audit.py 审计（create_token 动作）；Lead 合并时挂上。
    expires = _parse_expires(body.expires_at) if body.expires_at else None
    out = tokensvc.create_token(db, body.name, expires_at=expires)
    return out


@router.patch("/{token_id}")
def update_token(token_id: int, body: TokenPatch,
                 _: bool = Depends(require_admin), db: Session = Depends(get_db)):
    # TODO(T2): 接入 admin/audit.py 审计（update_token 动作）；Lead 合并时挂上。
    patch: dict = {}
    # exclude_unset：只取客户端真的提交了的字段（PATCH 语义）。
    # pydantic v2 用 model_dump，v1 用 dict；两者都支持 exclude_unset。
    submitted = (body.model_dump(exclude_unset=True)
                 if hasattr(body, "model_dump")
                 else body.dict(exclude_unset=True))
    if "name" in submitted:
        patch["name"] = submitted["name"]
    if "enabled" in submitted:
        patch["enabled"] = bool(submitted["enabled"])
    if "scope" in submitted:
        # 非法值由 tokensvc.normalize_scope 降级 readonly，不报错。
        patch["scope"] = submitted["scope"]
    if "expires_at" in submitted:
        # 显式传 null（提交了但值为 None）= 改为永不过期；字符串则 parse。
        v = submitted["expires_at"]
        patch["expires_at"] = _parse_expires(v) if v is not None else None
    out = tokensvc.update_token(db, token_id, patch)
    if out is None:
        raise HTTPException(status_code=404, detail="令牌不存在")
    return out


@router.delete("/{token_id}")
def delete_token(token_id: int, _: bool = Depends(require_admin),
                 db: Session = Depends(get_db)):
    # TODO(T2): 接入 admin/audit.py 审计（delete_token 动作）；Lead 合并时挂上。
    if not tokensvc.delete_token(db, token_id):
        raise HTTPException(status_code=404, detail="令牌不存在")
    return {"ok": True}
