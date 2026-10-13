"""账号池分组（Phase 2 T8）：命名分组 CRUD + 账号划组。

与模型分组（routers/groups.py）正交：那个管「密钥能用哪些模型」，
这个管「密钥能用哪些账号」（经 ApiKey.account_group_id 绑定，
选号侧见 proxy._select_account 的 group_id 过滤）。

删除守卫（manager upstreamsvc 同口径，409）：组内还有账号、或还有密钥
绑定时拒绝删除——删除那一刻没人用不代表下一秒没有（并发请求刚选好号
正要绑），静默删掉会让在途请求突然跨组串号。
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from admin.audit import current_actor, log_request
from admin.db import get_db
from admin.models import Account, AccountGroup, ApiKey
from admin.security import require_admin

router = APIRouter(prefix="/api/account-groups", tags=["account-groups"])


class GroupIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    note: str = ""


@router.get("")
def list_groups(_: bool = Depends(require_admin), db: Session = Depends(get_db)):
    rows = db.query(AccountGroup).order_by(AccountGroup.id).all()
    items = []
    for g in rows:
        acc_count = db.query(Account).filter(Account.group_id == g.id).count()
        key_count = db.query(ApiKey).filter(ApiKey.account_group_id == g.id).count()
        items.append({
            "id": g.id, "name": g.name, "note": g.note,
            "accounts": acc_count, "bound_keys": key_count,
            "created_at": g.created_at.isoformat() if g.created_at else None,
        })
    return {"items": items}


@router.post("")
def create_group(body: GroupIn, request: Request,
                 _: bool = Depends(require_admin), db: Session = Depends(get_db)):
    name = body.name.strip()
    if db.query(AccountGroup).filter(AccountGroup.name == name).first():
        raise HTTPException(status_code=409, detail=f"分组 '{name}' 已存在")
    g = AccountGroup(name=name, note=body.note)
    db.add(g)
    db.commit()
    db.refresh(g)
    log_request(current_actor(), "create_account_group", target=f"{g.id}:{g.name}",
                request=request)
    return {"id": g.id, "name": g.name, "note": g.note}


@router.patch("/{group_id}")
def patch_group(group_id: int, body: dict, request: Request,
                _: bool = Depends(require_admin), db: Session = Depends(get_db)):
    g = db.query(AccountGroup).filter(AccountGroup.id == group_id).first()
    if not g:
        raise HTTPException(status_code=404, detail="分组不存在")
    if "name" in body:
        name = str(body["name"]).strip()
        if not name:
            raise HTTPException(status_code=400, detail="名称不能为空")
        clash = (db.query(AccountGroup)
                 .filter(AccountGroup.name == name, AccountGroup.id != group_id).first())
        if clash:
            raise HTTPException(status_code=409, detail=f"分组 '{name}' 已存在")
        g.name = name
    if "note" in body:
        g.note = str(body["note"])
    db.commit()
    log_request(current_actor(), "update_account_group", target=f"{g.id}:{g.name}",
                detail=",".join(sorted(body.keys())), request=request)
    return {"id": g.id, "ok": True}


@router.delete("/{group_id}")
def delete_group(group_id: int, request: Request,
                 _: bool = Depends(require_admin), db: Session = Depends(get_db)):
    g = db.query(AccountGroup).filter(AccountGroup.id == group_id).first()
    if not g:
        raise HTTPException(status_code=404, detail="分组不存在")
    # 删除守卫：409（请求本身没错，是**当前状态**不允许——把账号/密钥先挪走
    # 就能删，可重试性正是 409 该表达的，与 manager upstreamsvc 同口径）
    acc_count = db.query(Account).filter(Account.group_id == group_id).count()
    if acc_count:
        raise HTTPException(
            status_code=409,
            detail=f"分组内还有 {acc_count} 个账号，请先把它们移出该分组再删除")
    key_count = db.query(ApiKey).filter(ApiKey.account_group_id == group_id).count()
    if key_count:
        raise HTTPException(
            status_code=409,
            detail=f"还有 {key_count} 把密钥绑定该分组，请先解绑再删除")
    log_request(current_actor(), "delete_account_group", target=f"{g.id}:{g.name}",
                request=request)
    db.delete(g)
    db.commit()
    return {"id": group_id, "ok": True}


@router.post("/{group_id}/accounts/{account_id}")
def assign_account(group_id: int, account_id: int, request: Request,
                   _: bool = Depends(require_admin), db: Session = Depends(get_db)):
    """把账号划入分组；group_id=0 走 /unassign 语义（这里不允许，语义要显式）。"""
    g = db.query(AccountGroup).filter(AccountGroup.id == group_id).first()
    if not g:
        raise HTTPException(status_code=404, detail="分组不存在")
    acc = db.query(Account).filter(Account.id == account_id).first()
    if not acc:
        raise HTTPException(status_code=404, detail="账号不存在")
    acc.group_id = group_id
    db.commit()
    log_request(current_actor(), "assign_account_group",
                target=f"acc:{account_id}", detail=f"-> group:{g.name}",
                request=request)
    return {"account_id": account_id, "group_id": group_id, "ok": True}


@router.post("/accounts/{account_id}/unassign")
def unassign_account(account_id: int, request: Request,
                     _: bool = Depends(require_admin), db: Session = Depends(get_db)):
    acc = db.query(Account).filter(Account.id == account_id).first()
    if not acc:
        raise HTTPException(status_code=404, detail="账号不存在")
    old = acc.group_id
    acc.group_id = 0
    db.commit()
    log_request(current_actor(), "unassign_account_group",
                target=f"acc:{account_id}", detail=f"was group:{old}",
                request=request)
    return {"account_id": account_id, "group_id": 0, "ok": True}
