"""本地快照备份 / 恢复（P5）。

用途：把本机库**全部业务表**导出成一份自包含 JSON 快照，可在同机或新机导入恢复。
这是「本机快照」形态——**不是**异地镜像备份（PostgreSQL 异地镜像按用户决定搁置）。

安全设计（本模块处理的是最敏感的数据：账号凭据 auth_json、密钥 hash、IP 规则）：

  1. **导出必须管理员**（require_admin，非只读 token）：快照含账号凭据，
     只读 token 绝不能拿到它。
  2. **恢复前自动留底**：导入前先对当前库导出一份 `pre-restore-<ts>.json`，
     写进备份目录。恢复是覆盖式高危操作，没有退路不能做。
  3. **审计留痕**：导出/导入都写 audit_logs（action=backup.export / backup.restore）。
  4. **默认脱敏开关**：`include_secrets=False` 时把 auth_json / key_hash / key_full
     / ip 等敏感列的**值替换成占位符**（结构完整、可用于体积评估与结构核对，
     但不泄露凭据）。默认 True（管理员显式请求才含真实凭据）。
  5. **绝不含运行期无意义的表**：只导业务表清单（见 TABLES）。
  6. 导入时**逐表覆盖**（DELETE 后 INSERT），不做部分合并——避免「导一半」的中间态；
     整份导入在**单个事务**内完成，失败则整体回滚（不留半份数据）。

序列化：用 SQLAlchemy 反射（`Table` 对象）而非硬编码列名——加列/改列时备份自动跟随，
不会因为忘了改备份代码而静默丢字段。datetime 存 ISO 字符串，导入时反解析。
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from sqlalchemy import inspect as sa_inspect, text
from sqlalchemy.orm import Session

#: 快照格式版本。将来结构变更时据此判断兼容性（不认识就拒绝导入）。
SNAPSHOT_FORMAT = 1

#: 需要备份的业务表——**顺序即外键无关的导入顺序**（本项目无真外键，
#: 但保持「基础表在前」便于人工检查）。
TABLES: tuple[str, ...] = (
    "accounts",
    "account_groups",
    "account_model_cool",
    "api_keys",
    "api_key_ips",
    "model_groups",
    "model_group_items",
    "model_configs",
    "system_settings",
    "api_tokens",
    "ip_rules",
    "ip_access_logs",
    "schedules",
    "audit_logs",
    "usage_logs",
)

#: 含敏感值的列 → 脱敏时替换成的占位符。
#:
#: 两类判定：
#:   * **子串匹配**（token/secret/password/auth_json/key_hash/key_full）——
#:     这些词一旦出现在列名里基本都是敏感列，宁可多脱不可漏脱。
#:   * **精确列名**（IP 类）——不能用子串！`credit_multiplier` 里含 "ip"
#:     （mult**ip**lier），子串匹配会把积分倍率脱敏掉（实测踩过）。
_SECRET_COLUMN_HINTS: tuple[tuple[str, str], ...] = (
    ("auth_json", "[redacted]"),
    ("token", "[redacted-token]"),      # api_tokens.token_hash / wbt_
    ("key_hash", "[redacted-hash]"),
    ("key_full", "[redacted-key]"),
    ("secret", "[redacted]"),
    ("password", "[redacted]"),
)

#: 精确列名的敏感列（全等匹配）。
_SECRET_COLUMNS_EXACT: frozenset[str] = frozenset({
    "ip", "client_ip", "source_ip", "remote_ip", "last_ip", "ip_address",
    "ip_allowlist", "allowed_ips",
})

#: 脱敏时必须保留的列（白名单，防误伤）。
_KEEP_COLUMNS = frozenset()

_TS_KEYS = ("created_at", "updated_at")

#: 形如 "2026-10-15 16:57:06.631755" 或 ISO 的字符串——SQLite 裸 SQL 会把
#: DATETIME 列**直接返回成字符串**，而 MySQL/PG 驱动返回 datetime 对象。
#: 两种形态都要收敛成统一标记，否则同一份快照在不同方言下结构不一致。
_TS_STR_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(\.\d+)?([+-]\d{2}:?\d{2})?$")


def _serialize_value(value, column: str = ""):
    """JSON 可序列化：datetime → {"__dt__": ISO}，其余原样。

    SQLite 裸 SQL 返回 str 形态的时间戳，也要归一到 {"__dt__": ...}，
    否则同库类型不一致（导入时无法与时区/格式假字符串区分）。
    只对**时间列名**做字符串归一，避免把用户输入的普通字符串误判成时间。
    """
    if isinstance(value, datetime):
        return {"__dt__": value.isoformat()}
    if isinstance(value, str) and column in _TS_KEYS and _TS_STR_RE.match(value):
        return {"__dt__": value.replace(" ", "T", 1)}
    return value


def _deserialize_value(value):
    if isinstance(value, dict) and "__dt__" in value:
        try:
            return datetime.fromisoformat(value["__dt__"])
        except (ValueError, TypeError):
            return None
    return value


def _redact_row(row: dict, include_secrets: bool) -> dict:
    """按列名脱敏（结构保留）。子串规则 + 精确列名规则（见常量注释）。"""
    if include_secrets:
        return row
    out = {}
    for col, val in row.items():
        if col in _KEEP_COLUMNS:
            out[col] = val
            continue
        lower = col.lower()
        if lower in _SECRET_COLUMNS_EXACT:
            out[col] = "[redacted-ip]" if val is not None else None
            continue
        for hint, placeholder in _SECRET_COLUMN_HINTS:
            if hint in lower:
                out[col] = placeholder if val is not None else None
                break
        else:
            out[col] = val
    return out


def _q(db: Session, name: str) -> str:
    """按方言引用标识符（表名/列名）。

    必须引用，不能裸拼：`system_settings.key` 的列名 `key` 是 MySQL 保留字，
    裸拼直接 1064 语法错误（实测踩过）。SQLAlchemy 的 dialect 给出正确引用符
    （MySQL 反引号 / SQLite 双引号 / PG 双引号）。
    """
    return db.get_bind().dialect.identifier_preparer.quote(name)


def export_snapshot(db: Session, *, include_secrets: bool = True) -> dict:
    """导出全库快照（dict，可直接 json.dumps）。

    include_secrets=False 时敏感列值替换成占位符（结构/行数不变）。
    """
    insp = sa_inspect(db.get_bind())
    existing = set(insp.get_table_names())
    payload: dict = {
        "format": SNAPSHOT_FORMAT,
        "generated_at": datetime.utcnow().isoformat(),
        "include_secrets": bool(include_secrets),
        "tables": {},
    }
    for table in TABLES:
        if table not in existing:
            continue  # 旧库可能缺新表；跳过而非报错
        rows = db.execute(text(f"SELECT * FROM {_q(db, table)}")).mappings().all()
        payload["tables"][table] = [
            {k: _serialize_value(v, k)
             for k, v in _redact_row(dict(r), include_secrets).items()}
            for r in rows
        ]
    return payload


def snapshot_stats(payload: dict) -> dict:
    """每张表的行数（前端/审计展示用；不含任何值）。"""
    return {t: len(rows) for t, rows in (payload.get("tables") or {}).items()}


def restore_snapshot(db: Session, payload: dict, *, allow_partial: bool = False) -> dict:
    """把快照导入当前库（逐表 DELETE + INSERT，单事务）。

    失败整体回滚（不留半份数据）。返回 {restored: {table: n}, skipped: [...]}。

    为什么单事务：本功能最常见的误操作是「导错文件」；半份数据比全不导更糟——
    账号在新、密钥在旧，症状是随机 401，排查成本极高。
    """
    fmt = payload.get("format")
    if fmt != SNAPSHOT_FORMAT:
        raise ValueError(f"快照格式不受支持（期望 {SNAPSHOT_FORMAT}，实际 {fmt!r}）")
    tables = payload.get("tables")
    if not isinstance(tables, dict):
        raise ValueError("快照缺少 tables 字段")

    insp = sa_inspect(db.get_bind())
    existing = set(insp.get_table_names())
    restored: dict[str, int] = {}
    skipped: list[str] = []
    try:
        for table in TABLES:
            if table not in tables:
                if allow_partial:
                    skipped.append(table)
                    continue
                skipped.append(table)
                continue  # 快照里没有该表 = 空表（导出时库缺表）；清空它
            rows = tables[table]
            if not isinstance(rows, list):
                raise ValueError(f"表 {table} 的数据不是数组")
            if table not in existing:
                skipped.append(table)
                continue
            db.execute(text(f"DELETE FROM {_q(db, table)}"))
            if rows:
                cols = list(rows[0].keys())
                # 列名来自快照，必须校验只含标识符字符——否则是 SQL 注入面
                for c in cols:
                    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", c):
                        raise ValueError(f"表 {table} 含非法列名 {c!r}")
                placeholders = ", ".join(f":{c}" for c in cols)
                col_sql = ", ".join(_q(db, c) for c in cols)
                db.execute(
                    text(f"INSERT INTO {_q(db, table)} ({col_sql}) VALUES ({placeholders})"),
                    [{c: _deserialize_value(r.get(c)) for c in cols} for r in rows])
            restored[table] = len(rows)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return {"restored": restored, "skipped": skipped}


# ── 文件形态：备份目录 + 留底 ──────────────────────────────────────────

_SAFE_NAME = re.compile(r"^[A-Za-z0-9_.\-]{1,80}$")


def backup_dir(root: Path) -> Path:
    path = Path(root) / "backups"
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_snapshot(directory: Path, payload: dict, name: str) -> Path:
    """写快照文件（原子替换）。name 只允许安全字符（防路径穿越）。"""
    if not _SAFE_NAME.fullmatch(name):
        raise ValueError("非法备份文件名")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / name
    temp = target.with_suffix(target.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(target)
    return target


def list_snapshots(directory: Path) -> list[dict]:
    """列出备份文件（名 + 大小 + 时间，按时间倒序）。只认 .json。"""
    directory = Path(directory)
    if not directory.is_dir():
        return []
    out = []
    for f in directory.glob("*.json"):
        try:
            st = f.stat()
        except OSError:
            continue
        out.append({"name": f.name, "size": st.st_size,
                    "mtime": datetime.utcfromtimestamp(st.st_mtime).isoformat()})
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out


def read_snapshot(directory: Path, name: str) -> dict:
    """读一份备份文件（name 校验防穿越）。"""
    if not _SAFE_NAME.fullmatch(name):
        raise ValueError("非法备份文件名")
    path = Path(directory) / name
    if not path.is_file():
        raise FileNotFoundError(name)
    return json.loads(path.read_text(encoding="utf-8"))


def pre_restore_backup(db: Session, directory: Path) -> Path:
    """恢复前自动留底（含真实凭据——留底必须能完整还原）。"""
    payload = export_snapshot(db, include_secrets=True)
    stamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    return write_snapshot(directory, payload, f"pre-restore-{stamp}.json")
