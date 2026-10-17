"""账号级执行租约（跨进程互斥）。

# 解决什么问题

我们的成长任务有两个执行入口：

    POST /api/growth/run          同步（面板上「做任务」按钮）
    POST /api/growth/run-async    异步队列
    定时任务 run_growth_tasks     已在 schedules 里，可被启用

这三条路径**原本没有互斥**。用户手动点一次、正好定时也触发，
同一账号同一任务就会发两遍请求 —— 这正是「多遍跑会检测」的场景。

`/run-async` 有一道 `jobrunner.RUNNER.get(JOB_KEY)` 的判断，但那是
**进程内内存状态**：

  * 多 uvicorn worker（`--workers 4`）各有一份，互相看不见
  * 进程重启后状态清零，正在跑的会被当成没跑
  * 它只挡 `run-async` 自己，挡不住同步 `/run` 与定时任务

# 怎么解决

用 `SystemSetting` 表做 **CAS（compare-and-swap）锁**：

    1. 读到当前 value
    2. 若未过期 → 抢不到，返回 False
    3. 否则写新 value（新 owner + 新过期时间），条件是
       `WHERE value = <刚读到的旧值>` —— 只有这一条 UPDATE 成功才算抢到

第 3 步是关键：数据库保证「比较 + 交换」的原子性。两个进程同时抢，
只有一个 UPDATE 的行数会是 1。

**为什么不新建一张表**：`SystemSetting` 是现成的 KV 表（我们已经在用），
复用它零迁移成本。租约只是临时状态，不值得独立建表。

# 租约的粒度

**按账号**（`growth:lease:<account_id>`），不是全局。

理由：全局锁会让「A 账号在跑」挡住「B 账号手动执行」，用户会以为按钮坏了。
按账号锁能精确到「同一个号不被两个入口同时跑」，互不影响。

# 过期时间

默认 600 秒（10 分钟）。一个账号跑完所有任务约 27 秒，600 秒是很宽的余量。
**过期是必须的**：进程被 kill 时不会执行释放逻辑，没有过期时间就会永久锁死。
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from contextlib import contextmanager

from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from admin.models import SystemSetting

_log = logging.getLogger("admin.growth_lease")

#: 租约 key 前缀。按账号区分，见模块文档「租约的粒度」。
LEASE_PREFIX = "growth:lease:"

#: 默认租约时长（秒）。
DEFAULT_TTL = 600


def lease_key(account_id: int) -> str:
    return f"{LEASE_PREFIX}{account_id}"


def _read_state(db, key: str) -> dict:
    """读当前租约状态；读不到/坏数据一律当「空闲」。"""
    try:
        row = db.get(SystemSetting, key)
    except SQLAlchemyError:
        return {}
    if row is None:
        return {}
    try:
        data = json.loads(row.value or "{}")
        return data if isinstance(data, dict) else {}
    except (TypeError, ValueError):
        return {}


def is_locked(db, account_id: int) -> bool:
    """该账号当前是否被别的执行占用（只读判断，不改状态）。"""
    st = _read_state(db, lease_key(account_id))
    try:
        return float(st.get("expires") or 0) > time.time()
    except (TypeError, ValueError):
        return False


def acquire(db, account_id: int, ttl: int = DEFAULT_TTL) -> str | None:
    """抢租约。成功返回 owner 字符串，失败返回 None。

    失败有三种可能，**都不该当作错误**：
      * 别的进程正在跑这个账号
      * 同一进程的上一个请求还没释放
      * 用户连点了两下
    """
    key = lease_key(account_id)
    ttl = max(30, int(ttl))

    # 确保 key 存在（并发首次创建时只有一个能成功，其余吞掉 IntegrityError）
    try:
        if db.get(SystemSetting, key) is None:
            db.add(SystemSetting(key=key, value="{}"))
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
    except SQLAlchemyError as e:
        _log.warning("租约初始化失败 key=%s: %s", key, e)
        db.rollback()
        return None

    row = db.get(SystemSetting, key)
    if row is None:
        return None

    previous = row.value or "{}"
    try:
        expiry = float(json.loads(previous).get("expires") or 0)
    except (TypeError, ValueError, AttributeError):
        expiry = 0.0

    if expiry > time.time():
        return None          # 还在有效期内，别人占着

    owner = uuid.uuid4().hex
    value = json.dumps({"owner": owner, "expires": time.time() + ttl,
                        "account_id": int(account_id)})
    try:
        changed = (db.query(SystemSetting)
                   .filter(SystemSetting.key == key,
                           SystemSetting.value == previous)
                   .update({SystemSetting.value: value},
                           synchronize_session=False))
        db.commit()
    except SQLAlchemyError as e:
        _log.warning("租约获取失败 key=%s: %s", key, e)
        db.rollback()
        return None

    # CAS：只有「读到的旧值仍是当前值」才能更新成功
    return owner if changed else None


def release(db, account_id: int, owner: str | None) -> bool:
    """释放租约（只释放自己持有的那把）。

    校验 owner 是必要的：A 的租约过期后 B 抢到了，A 迟到的 release
    不能把 B 的锁解掉。
    """
    if not owner:
        return False
    key = lease_key(account_id)
    try:
        row = db.get(SystemSetting, key)
        if row is None:
            return False
        current = json.loads(row.value or "{}")
        if str(current.get("owner") or "") != str(owner):
            return False          # 不是自己的锁，不动
        (db.query(SystemSetting)
         .filter(SystemSetting.key == key,
                 SystemSetting.value == (row.value or "{}"))
         .update({SystemSetting.value: json.dumps({"owner": "", "expires": 0})},
                 synchronize_session=False))
        db.commit()
        return True
    except (SQLAlchemyError, TypeError, ValueError) as e:
        _log.warning("租约释放失败 key=%s: %s", key, e)
        db.rollback()
        return False


def renew(db, account_id: int, owner: str | None, ttl: int = DEFAULT_TTL) -> bool:
    """续期（长任务用）。同样校验 owner。"""
    if not owner:
        return False
    key = lease_key(account_id)
    ttl = max(30, int(ttl))
    try:
        row = db.get(SystemSetting, key)
        if row is None:
            return False
        previous = row.value or "{}"
        current = json.loads(previous)
        if str(current.get("owner") or "") != str(owner):
            return False
        current["expires"] = time.time() + ttl
        changed = (db.query(SystemSetting)
                   .filter(SystemSetting.key == key,
                           SystemSetting.value == previous)
                   .update({SystemSetting.value: json.dumps(current)},
                           synchronize_session=False))
        db.commit()
        return bool(changed)
    except (SQLAlchemyError, TypeError, ValueError) as e:
        _log.warning("租约续期失败 key=%s: %s", key, e)
        db.rollback()
        return False


@contextmanager
def hold(db, account_id: int, ttl: int = DEFAULT_TTL):
    """`with` 用法：拿到 yield owner，拿不到 yield None。

        with hold(db, acc.id) as owner:
            if owner is None:
                continue        # 这个号正被别的入口跑，跳过
            ... 干活 ...

    **即使 `yield` 返回 None 也一定会执行释放逻辑**（虽然此时是空操作），
    这样调用方不必自己分支处理 try/finally。
    """
    owner = acquire(db, account_id, ttl)
    try:
        yield owner
    finally:
        if owner:
            release(db, account_id, owner)
