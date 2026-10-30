# language: Python, file: backend/proxy.py
# 统一入口：把 /v1/*、/proxy/* 的请求投给号池里选中的接口，失败自动换号重试
from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Optional
from urllib.parse import quote

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.background import BackgroundTask

from . import db, pool

HOP_BY_HOP = {
    "host", "content-length", "connection", "keep-alive", "proxy-authenticate",
    "proxy-authorization", "te", "trailer", "transfer-encoding", "upgrade",
    "accept-encoding", "x-admin-token", "x-proxy-token", "x-session-id",
}

DROP_FROM_RESPONSE = {
    "content-length", "transfer-encoding", "connection", "keep-alive",
    "content-encoding", "set-cookie",
}


def join_url(base: str, path: str) -> str:
    """拼上游地址；base_url 带不带 /v1 都能用，不会拼出 /v1/v1。"""
    b = base.rstrip("/")
    p = path.lstrip("/")
    if b.endswith("/v1") and p.startswith("v1/"):
        p = p[3:]
    return f"{b}/{p}"


def build_headers(request: Request, ep: dict[str, Any], sticky_header: str = "") -> dict[str, str]:
    headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_BY_HOP}
    auth_name = (ep.get("auth_header") or "Authorization")
    drop = {"authorization", auth_name.lower()}
    if sticky_header:
        drop.add(sticky_header.lower())
    for key in list(headers):
        if key.lower() in drop:
            headers.pop(key)
    headers.update(pool.auth_headers(ep))
    return headers


def _content_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):  # 多段 content
        return "\n".join(
            str(p.get("text", "")) for p in value if isinstance(p, dict)
        )
    return str(value or "")


def derive_session_key(request: Request, body: bytes, header_name: str) -> str:
    """决定「同一个会话」是谁：请求头 > user 字段 > metadata > 请求前缀哈希 > IP+UA。

    前缀哈希只取 system/developer 段 + 第一条用户消息，所以对话往下长、key 不变——
    这正好对应上游 prompt 缓存的匹配方式（前缀一致即命中）。
    """
    if header_name:
        explicit = request.headers.get(header_name)
        if explicit and explicit.strip():
            return "sid:" + explicit.strip()[:200]

    payload: Any = None
    if body:
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            payload = None

    if isinstance(payload, dict):
        for field in ("user", "session_id"):
            value = payload.get(field)
            if isinstance(value, (str, int)) and str(value).strip():
                return f"{field}:" + str(value).strip()[:200]
        meta = payload.get("metadata")
        if isinstance(meta, dict):
            for field in ("user_id", "session_id", "conversation_id"):
                value = meta.get(field)
                if isinstance(value, (str, int)) and str(value).strip():
                    return f"meta:{field}:" + str(value).strip()[:200]

        messages = payload.get("messages") or payload.get("input")
        if isinstance(messages, list) and messages:
            head: list[str] = []
            for msg in messages:
                if not isinstance(msg, dict):
                    continue
                head.append(f"{msg.get('role', '')}:{_content_text(msg.get('content'))[:4000]}")
                if msg.get("role") not in ("system", "developer"):
                    break
            if head:
                material = f"{payload.get('model', '')}\n" + "\n".join(head)
                return "prefix:" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]

        if body:
            return "body:" + hashlib.sha256(body[:8000]).hexdigest()[:32]

    client = request.client.host if request.client else ""
    agent = request.headers.get("user-agent", "")
    return "ip:" + hashlib.sha256(f"{client}|{agent}".encode()).hexdigest()[:32]


def apply_model_override(body: bytes, ep: dict[str, Any]) -> bytes:
    """接口配了 model 字段就覆盖请求体里的 model，留空则原样透传。"""
    forced = (ep.get("model") or "").strip()
    if not forced or not body:
        return body
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return body
    if not isinstance(payload, dict):
        return body
    if payload.get("model") == forced:
        return body
    payload["model"] = forced
    return json.dumps(payload, ensure_ascii=False).encode()


async def _cleanup(resp: httpx.Response, client: httpx.AsyncClient) -> None:
    try:
        await resp.aclose()
    finally:
        await client.aclose()


async def handle(request: Request, path: str) -> Response:
    settings = db.get_settings()
    attempts = max(1, int(settings.get("max_attempts") or 1))
    timeout = float(settings.get("request_timeout") or 300)
    body = await request.body()
    params = list(request.query_params.multi_items())
    method = request.method
    sticky_header = str(settings.get("sticky_header") or "x-session-id")
    session_key = derive_session_key(request, body, sticky_header)

    tried: set[int] = set()
    trace: list[dict[str, Any]] = []
    client = pool.make_client(httpx.Timeout(timeout, connect=30.0))
    streaming = False

    try:
        for attempt in range(1, attempts + 1):
            ep = await pool.pick(exclude=tried, session_key=session_key)
            if ep is None:
                break
            tried.add(ep["id"])
            url = join_url(ep["base_url"], path)
            headers = build_headers(request, ep, sticky_header)
            payload = apply_model_override(body, ep)
            started = time.perf_counter()

            try:
                req = client.build_request(
                    method, url, headers=headers, content=payload or None, params=params
                )
                resp = await client.send(req, stream=True)
            except httpx.HTTPError as exc:
                message = f"{type(exc).__name__}: {exc}"
                pool.report_failure(ep, "network", message)
                trace.append({"endpoint": ep["name"], "attempt": attempt, "error": message})
                continue

            status = resp.status_code
            if status >= 400:
                try:
                    raw = await resp.aread()
                except httpx.HTTPError as exc:
                    # 读错误响应体就断了，当网络故障处理，继续换号
                    message = f"{type(exc).__name__}: {exc}"
                    pool.report_failure(ep, "network", message)
                    trace.append({"endpoint": ep["name"], "attempt": attempt, "error": message})
                    continue
                await resp.aclose()
                kind, retryable = pool.classify(status, raw, settings)
                if not retryable:
                    pool.report_failure(ep, kind, f"{status} 调用方错误", retryable=False)
                    return Response(
                        content=raw,
                        status_code=status,
                        media_type=resp.headers.get("content-type", "application/json"),
                        headers={"x-pool-endpoint": quote(ep["name"])},
                    )
                pool.report_failure(ep, kind, f"{status}: {raw[:200].decode('utf-8', 'ignore')}")
                trace.append({
                    "endpoint": ep["name"], "attempt": attempt,
                    "status": status, "kind": kind,
                })
                continue

            latency = (time.perf_counter() - started) * 1000
            pool.report_success(ep, latency)
            pool.log("info", "route", f"{method} {path} → {ep['name']} · {status} · {latency:.0f}ms",
                     ep, latency)

            out_headers = {
                k: v for k, v in resp.headers.items() if k.lower() not in DROP_FROM_RESPONSE
            }
            out_headers["x-pool-endpoint"] = quote(ep["name"])
            out_headers["x-pool-attempt"] = str(attempt)
            out_headers["cache-control"] = out_headers.get("cache-control", "no-cache")
            streaming = True
            return StreamingResponse(
                resp.aiter_bytes(),
                status_code=status,
                headers=out_headers,
                background=BackgroundTask(_cleanup, resp, client),
            )

        pool.log("error", "exhaust", f"{method} {path} 全部接口不可用或失败，已尝试 {len(tried)} 个")
        detail = {
            "error": {
                "type": "no_available_endpoint",
                "message": "号池里没有可用接口，或本次请求在所有接口上都失败了",
                "attempts": trace,
            }
        }
        return JSONResponse(detail, status_code=503)
    except Exception as exc:  # noqa: BLE001 - 兜底，避免半开连接泄漏
        pool.log("error", "proxy", f"{method} {path} 代理异常: {type(exc).__name__}: {exc}")
        return JSONResponse(
            {"error": {"type": "proxy_failure", "message": f"{type(exc).__name__}: {exc}"}},
            status_code=502,
        )
    finally:
        if not streaming:
            await client.aclose()
