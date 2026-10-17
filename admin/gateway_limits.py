"""Per-key sliding window and renewable Redis concurrency leases."""
from collections import deque
import asyncio
import logging
import math
import uuid
import threading
import time

from admin.config import settings

_lock = threading.Lock()
_windows = {}
_active = {}
_redis = None
_redis_initialized = False
_redis_lock = threading.Lock()
_logger = logging.getLogger(__name__)
_ACQUIRE = """
local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2])/1000000
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now-60)
redis.call('ZREMRANGEBYSCORE', KEYS[2], '-inf', now)
if tonumber(ARGV[1]) > 0 and redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[1]) then
 local first = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')
 return math.max(1, math.ceil(60-now+tonumber(first[2])))
end
if tonumber(ARGV[2]) > 0 and redis.call('ZCARD', KEYS[2]) >= tonumber(ARGV[2]) then return 1 end
if tonumber(ARGV[1]) > 0 then
 redis.call('ZADD', KEYS[1], now, ARGV[3]); redis.call('EXPIRE', KEYS[1], 61)
end
redis.call('ZADD', KEYS[2], now+120, ARGV[3]); redis.call('EXPIRE', KEYS[2], 121)
return 0
"""
_RENEW = """
if not redis.call('ZSCORE', KEYS[1], ARGV[1]) then return 0 end
local t = redis.call('TIME')
redis.call('ZADD', KEYS[1], tonumber(t[1])+tonumber(t[2])/1000000+120, ARGV[1])
redis.call('EXPIRE', KEYS[1], 121)
return 1
"""


def _redis_client():
    global _redis, _redis_initialized
    with _redis_lock:
        if not _redis_initialized:
            backend = settings.GATEWAY_LIMIT_BACKEND
            if backend not in ('auto', 'redis', 'memory'):
                raise ValueError('invalid gateway limit backend')
            if backend != 'memory':
                try:
                    import redis
                    client = redis.Redis.from_url(settings.REDIS_URL, socket_connect_timeout=1, socket_timeout=1)
                    client.ping()
                    _redis = client
                except Exception:
                    if backend == 'redis':
                        raise
                    _logger.warning('Gateway admission falling back to process-local memory')
            _redis_initialized = True
        return _redis


def _keys(key):
    return (f'wb:gateway:{{{key}}}:rate', f'wb:gateway:{{{key}}}:active')


class Admission:
    def __init__(self, key, retry_after=0):
        self.key = key
        self.retry_after = retry_after
        self.released = False
        self.client = None
        self.token = uuid.uuid4().hex


def _limits():
    return max(0, int(getattr(settings, "GATEWAY_RATE_PER_MINUTE", 0))), max(0, int(getattr(settings, "GATEWAY_MAX_CONCURRENT", 0)))


def resolve_concurrent(global_limit: int, per_key) -> int:
    """把「全局上限」与「每密钥覆盖」合成该密钥的并发上限（纯函数，便于测试）。

    | `per_key` | 含义 | 结果 |
    |---|---|---|
    | `None`  | 未设置（**存量密钥都是这个**） | `global_limit`（跟随全局，零回归） |
    | `0`     | 显式不限 | `0`（不限制该密钥） |
    | `N > 0` | 最多 N 路 | `N` |

    负数按非法处理（回落到全局），避免脏数据把闸门算成负数。
    """
    if per_key is None:
        return global_limit
    try:
        n = int(per_key)
    except (TypeError, ValueError):
        return global_limit
    if n < 0:
        return global_limit
    return n


def allow(key, per_key_concurrent=None):
    """申请一路准入。

    `per_key_concurrent` 是该密钥的并发覆盖值（`api_keys.max_concurrent`）：
    None=跟随全局 / 0=不限 / N=上限。见 `resolve_concurrent`。

    **它只影响并发**，不影响速率 —— `GATEWAY_RATE_PER_MINUTE` 仍是全局口径
    （速率是「对外配额」，与「单密钥能开几路」是两件事）。
    """
    rate, concurrent = _limits()
    concurrent = resolve_concurrent(concurrent, per_key_concurrent)
    if not key or (not rate and not concurrent):
        return Admission(None)
    client = _redis_client()
    if client is not None:
        lease = Admission(key)
        lease.client = client
        wait = int(client.eval(_ACQUIRE, 2, *_keys(key), rate, concurrent, lease.token))
        return (None, wait) if wait else lease
    now = time.monotonic()
    with _lock:
        for stale in list(_windows):
            values = _windows[stale]
            while values and now - values[0] >= 60:
                values.popleft()
            if not values and not _active.get(stale):
                _windows.pop(stale)
        q = _windows.setdefault(key, deque())
        if rate:
            while q and now - q[0] >= 60:
                q.popleft()
            if len(q) >= rate:
                return None, max(1, math.ceil(60 - (now - q[0])))
            q.append(now)
        current = _active.get(key, 0)
        if concurrent and current >= concurrent:
            if rate and q and q[-1] == now:
                q.pop()
            return None, 1
        _active[key] = current + 1
    return Admission(key)


def release(admission):
    if not admission or not admission.key:
        return
    if admission.client is not None:
        admission.client.zrem(_keys(admission.key)[1], admission.token)
        return
    with _lock:
        if admission.released:
            return
        admission.released = True
        current = _active.get(admission.key, 0)
        if current <= 1:
            _active.pop(admission.key, None)
        else:
            _active[admission.key] = current - 1
        if admission.key in _windows and not _windows[admission.key]:
            _windows.pop(admission.key, None)


def snapshot():
    with _lock:
        return {"active": dict(_active), "window_sizes": {k: len(v) for k, v in _windows.items()},
                "scope": "process_local"}


def _valid_key_id(token):
    """校验密钥并返回 `(key_id, max_concurrent)`；无效返回 None。

    为什么一次返回两个值：中间件本来就要查一次库做鉴权，顺手把
    `max_concurrent` 带回来，避免为了拿这个字段再查一次（网关是热路径）。
    """
    from admin.db import SessionLocal
    from admin.security import get_key_row
    with SessionLocal() as db:
        row = get_key_row(db, token)
        if not row or row.status != "active":
            return None
        return str(row.id), getattr(row, "max_concurrent", None)


async def _finish_thread(fn, *args):
    task = asyncio.create_task(asyncio.to_thread(fn, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


class GatewayAdmissionMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        from starlette.responses import JSONResponse
        # 全局两个闸都为 0 时直接放行 —— 但**不能就此短路**：
        # 某个密钥可能单独设了 `max_concurrent`（全局未开、per-key 开了的生僻配置）。
        # 所以这里只做「路径都不对」的快速跳出，per-key 值在拿到 key 后再判。
        if scope['type'] != 'http' or not scope.get('path', '').startswith('/v1/'):
            return await self.app(scope, receive, send)
        headers = dict(scope.get('headers', []))
        token = headers.get(b'x-api-key', b'').decode('latin1')
        if not token:
            auth = headers.get(b'authorization', b'').decode('latin1')
            token = auth[7:].strip() if auth.startswith('Bearer ') else ''
        # Only authenticated IDs enter the bounded state, never raw client tokens.
        # 顺带取回该密钥的并发覆盖值，避免为了这个字段再查一次库。
        ident = await _finish_thread(_valid_key_id, token) if token else None
        if ident is None:
            return await self.app(scope, receive, send)
        key, per_key = ident
        pending = asyncio.create_task(asyncio.to_thread(allow, key, per_key))
        try:
            result = await asyncio.shield(pending)
        except asyncio.CancelledError:
            result = await pending
            if not isinstance(result, tuple):
                await _finish_thread(release, result)
            raise
        except Exception:
            return await JSONResponse({'error': {'message': '流量控制服务暂不可用', 'type': 'gateway_unavailable'}},
                                      status_code=503, headers={'Retry-After': '5'})(scope, receive, send)
        if isinstance(result, tuple):
            _, retry = result
            return await JSONResponse({'error': {'message': 'API Key 请求过于频繁，请稍后重试', 'type': 'rate_limit_exceeded'}},
                                      status_code=429, headers={'Retry-After': str(retry)})(scope, receive, send)
        owner = asyncio.current_task()
        async def renew():
            while True:
                await asyncio.sleep(30)
                try:
                    if await _finish_thread(result.client.eval, _RENEW, 1, _keys(result.key)[1], result.token):
                        continue
                except Exception:
                    pass
                owner.cancel()
                return
        heartbeat = asyncio.create_task(renew()) if result.client is not None else None
        try:
            await self.app(scope, receive, send)
        finally:
            if heartbeat:
                heartbeat.cancel()
                try:
                    await heartbeat
                except asyncio.CancelledError:
                    pass
            try:
                await _finish_thread(release, result)
            except Exception:
                _logger.warning('Gateway lease release failed; Redis lease will expire')
