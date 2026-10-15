"""聊天测试台（P4-3）：管理员免密钥直调，走同一号池状态机。

与 /v1/chat/completions 的关系：复用同一套**原子**状态机函数——选号
`_select_account`（粘性/健康/到期紧迫度/防撞号）、在途租约 `POOL.acquire/release`、
错误分类 `_classify_error` + 账号处置 `_apply_account_policy`、记账 `_record_usage`
（key_id=0 + use_case='playground' 标记，与密钥调用同一张 usage_logs 表）。

刻意**不做**多账号轮换/多模型切换：测试台是「诊断一个模型在一个号上的表现」的
工具，轮换会把单号问题掩盖掉（轮换是生产网关的弹性，不是测试台的目标）。
失败的错误分类与处置照常落库（cooling/熔断/降权），与 /v1 口径一致。

安全：require_admin（JWT）；不对外（无 API Key 语义）；真实消耗积分——
usage.credit 通过 SSE 尾帧回传给前端实时显示；流式可被客户端中断（断开即关上游）。
"""
from __future__ import annotations

import json
import time

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from admin import backend
from admin.config import settings
from admin.db import get_db
from admin.security import require_admin

# 复用 proxy 的号池状态机原子函数（同一实现，非复制）
from admin.pool import POOL
from admin.routers.proxy import (
    _ChatStreamCM, _account_session_async, _apply_account_policy,
    _classify_error, _error_reset_at, _global_ua_override, _maybe_degrade,
    _parse_usage, _record_usage, _select_account, _session_headers,
    _stream_timeout, _upstream_extra_headers,
)
from admin import realm as realm_mod

router = APIRouter(prefix="/api/playground", tags=["playground"])


class ChatIn(BaseModel):
    model: str
    messages: list[dict] = []
    reasoning_effort: str | None = None   # 该模型支持的档位（前端只列支持项）
    stream: bool = True
    max_tokens: int | None = None
    temperature: float | None = None


@router.post("/chat")
async def playground_chat(
    body: ChatIn,
    request: Request,
    _: bool = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """管理员免密钥直调一个真实模型（走号池状态机，单次转发不轮换）。"""
    model = (body.model or "").strip()
    if not model:
        return JSONResponse(status_code=400,
                            content={"error": {"message": "缺少 model", "type": "invalid_request"}})
    if not body.messages:
        return JSONResponse(status_code=400,
                            content={"error": {"message": "缺少 messages", "type": "invalid_request"}})

    # realm 前缀剥离（cn:/global:），限定选号 realm；与 /v1 同口径
    model_realm, model_bare = realm_mod.resolve_model_prefix(model)
    if model_realm:
        model = model_bare

    # 出站 body（上游只支持流式；非流式由前端聚合或本层后续扩展）
    out: dict = {"model": model, "messages": body.messages, "stream": True,
                 "stream_options": {"include_usage": True}}
    if body.max_tokens:
        out["max_tokens"] = body.max_tokens
    if body.temperature is not None:
        out["temperature"] = body.temperature
    if body.reasoning_effort:
        out["reasoning_effort"] = body.reasoning_effort

    # 选号（同一状态机：粘性/健康/紧迫度/防撞号/模型级冷却/在途过滤）
    sticky_key = f"playground:{request.client.host if request.client else ''}"
    acc = _select_account(db, min_balance=1, model=model, sticky_key=sticky_key,
                          require_realm=model_realm or "")
    if not acc:
        return JSONResponse(
            status_code=503,
            content={"error": {"message": "无可用账号（该 realm 无健康账号或全部冷却/熔断/限流）",
                               "type": "no_account"}})

    # 在途租约（与 /v1 同一 POOL）
    uid = acc.uid or ""
    if not POOL.acquire(uid):
        return JSONResponse(status_code=503,
                            content={"error": {"message": "选中账号在途已满", "type": "busy"}})

    sess_i = await _account_session_async(db, acc)
    if sess_i is None:
        POOL.release(uid)
        return JSONResponse(status_code=503,
                            content={"error": {"message": "账号会话建立失败（凭据可能已失效）",
                                               "type": "session_error"}})

    # 会话头族 + 设备标识（与 /v1 同一构造，避免「非官方客户端」特征）
    extra = _upstream_extra_headers(request, purpose="playground")
    extra.update(_session_headers(out, uid))
    if uid:
        extra["X-Machine-ID"] = backend.stable_device_id(uid, "machine")
        extra["X-Session-ID"] = backend.stable_device_id(uid, "session")
    headers_i = sess_i.get_headers(extra=extra)
    _global_ua_override(sess_i, headers_i)

    started = time.perf_counter()
    client_ip = request.client.host if request.client else ""

    async def gen():
        """转发上游 SSE，收尾记账（key_id=0 + use_case=playground）。

        真实消耗积分：usage.credit 收尾时从流里解析，作为 SSE 尾帧
        `data: {"playground_usage": {...}}` 发给前端实时显示；
        未返回积分则该字段缺省（前端显示「—」，与「扣了 0」区分）。
        """
        status = 503
        err_kind = ""
        err_msg = ""
        try:
            async with httpx.AsyncClient(timeout=_stream_timeout(),
                                         limits=backend.HTTP_LIMITS) as client:
                async with _ChatStreamCM(client, out, headers_i, sess_i) as r:
                    status = r.status_code
                    if status >= 400:
                        detail = await r.aread()
                        text = detail[:65536].decode(errors="ignore") if isinstance(detail, bytes) else str(detail)[:65536]
                        err_kind = _classify_error(status, text)
                        # 与 /v1 同一错误处置（cooling/熔断/降权落库）
                        _apply_account_policy(db, acc, err_kind, status, text,
                                              model=model, reset_at=_error_reset_at(getattr(r, "headers", None), text))
                        _maybe_degrade(db, acc)
                        err_msg = text[:500]
                        yield f"data: {json.dumps({'error': {'message': text[:2000], 'type': 'upstream_error', 'code': status}}, ensure_ascii=False)}\n\n"
                    else:
                        # 200 转发：逐 chunk 透传（aiter_text 增量解码不拆汉字），
                        # 同时累积原文供收尾解析 usage。
                        collected: list[str] = []
                        async for chunk in r.aiter_text():
                            collected.append(chunk)
                            yield chunk
                        full = "".join(collected)
                        usage = _parse_usage(full)
                        # 记账（key_id=0 区分测试台；与 /v1 同一 usage_logs）
                        _record_usage(
                            0, acc.id, model,
                            usage.get("credits"), sess_i.updated_json(),
                            client_ip=client_ip, use_case="playground",
                            prompt_tokens=usage.get("prompt_tokens"),
                            completion_tokens=usage.get("completion_tokens"),
                            total_tokens=usage.get("total_tokens"),
                            cached_tokens=usage.get("cached_tokens"),
                            ttfb_ms=None,
                            latency_ms=int((time.perf_counter() - started) * 1000),
                            error_kind="", requested_model=model)
                        # 尾帧：实时积分消耗给前端（usage.credit；缺省 = 上游未返回）
                        tail = {"playground_usage": {
                            "credits": usage.get("credits"),
                            "prompt_tokens": usage.get("prompt_tokens"),
                            "completion_tokens": usage.get("completion_tokens"),
                            "total_tokens": usage.get("total_tokens"),
                            "latency_ms": int((time.perf_counter() - started) * 1000),
                        }}
                        yield f"data: {json.dumps(tail, ensure_ascii=False)}\n\n"
        except (httpx.TransportError, httpx.TimeoutException) as e:
            err_kind = "transport"
            err_msg = str(e)[:200]
            _apply_account_policy(db, acc, err_kind, 0, err_msg, model=model)
            yield f"data: {json.dumps({'error': {'message': f'上游连接失败: {e}', 'type': 'transport'}}, ensure_ascii=False)}\n\n"
        except Exception as e:  # noqa: BLE001 — 客户端中断（GeneratorExit/anyio）也走这里收尾
            err_msg = str(e)[:200]
            yield f"data: {json.dumps({'error': {'message': f'流中断: {e}', 'type': 'stream_aborted'}}, ensure_ascii=False)}\n\n"
        finally:
            # 无论成功/失败/中断：释放租约 + 关会话（中断时 httpx 流随 client 关闭）
            sess_i.close()
            POOL.release(uid)
            # 失败兜底记账（非 200/传输错且无 usage 时，记一条 error 行供审计）
            if err_kind and status >= 400:
                _record_usage(0, acc.id, model, 0.0, sess_i.updated_json(),
                              client_ip=client_ip, use_case="playground",
                              latency_ms=int((time.perf_counter() - started) * 1000),
                              error_kind=err_kind, requested_model=model)

    return StreamingResponse(gen(), media_type="text/event-stream")
