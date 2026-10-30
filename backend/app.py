# language: Python, file: backend/app.py
# FastAPI 入口：管理 API + OpenAI 兼容代理 + 静态前端
from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import config, db, pool, proxy
from .crypto import decrypt, encrypt, mask

ROOT = config.FRONTEND_DIR


# ------------------------------------------------------------------ 鉴权
def check_admin(token: Optional[str]) -> None:
    if not config.ADMIN_TOKEN:
        return
    if token == config.ADMIN_TOKEN:
        return
    raise HTTPException(status_code=401, detail="管理口令错误")


def admin(x_admin_token: Optional[str] = Header(default=None)) -> None:
    check_admin(x_admin_token)


def check_proxy(request: Request) -> None:
    if not config.PROXY_TOKEN:
        return
    given = request.headers.get("x-proxy-token") or request.query_params.get("proxy_token")
    if given != config.PROXY_TOKEN:
        raise HTTPException(status_code=401, detail="proxy token 错误")


# ------------------------------------------------------------------ 模型
class EndpointIn(BaseModel):
    name: str = Field(default="", max_length=120)
    base_url: str
    api_key: str = ""
    auth_header: str = "Authorization"
    auth_prefix: str = "Bearer "
    model: str = ""
    weight: int = 1
    priority: int = 0
    enabled: bool = True
    notes: str = ""


class EndpointPatch(BaseModel):
    name: Optional[str] = None
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    auth_header: Optional[str] = None
    auth_prefix: Optional[str] = None
    model: Optional[str] = None
    weight: Optional[int] = None
    priority: Optional[int] = None
    enabled: Optional[bool] = None
    notes: Optional[str] = None


class SettingsPatch(BaseModel):
    strategy: Optional[str] = None
    max_attempts: Optional[int] = None
    request_timeout: Optional[int] = None
    cooldown_quota: Optional[int] = None
    cooldown_auth: Optional[int] = None
    cooldown_rate: Optional[int] = None
    cooldown_server: Optional[int] = None
    probe_path: Optional[str] = None
    probe_interval: Optional[int] = None
    probe_timeout: Optional[int] = None
    quota_keywords: Optional[str] = None
    default_api_key: Optional[str] = None
    clear_default_api_key: Optional[bool] = None
    default_auth_header: Optional[str] = None
    default_auth_prefix: Optional[str] = None
    sticky_ttl: Optional[int] = None
    sticky_header: Optional[str] = None
    sticky_spread: Optional[str] = None
    log_keep: Optional[int] = None
    log_keep_hours: Optional[int] = None
    affinity_max: Optional[int] = None
    disable_on_quota: Optional[bool] = None
    quota_revive_days: Optional[int] = None


# ------------------------------------------------------------------ 序列化
def public_settings(settings: dict[str, Any]) -> dict[str, Any]:
    """设置回前端时抹掉默认密钥本体，只给掩码。"""
    out = {k: v for k, v in settings.items() if k != "default_api_key_enc"}
    raw = str(settings.get("default_api_key_enc") or "")
    key = decrypt(raw) if raw else ""
    out["has_default_api_key"] = bool(key)
    out["default_api_key_masked"] = mask(key) if key else ""
    return out


def serialize(ep: dict[str, Any], now: Optional[float] = None) -> dict[str, Any]:
    now = now if now is not None else time.time()
    own = pool.own_secret(ep)
    plain = own or pool.default_secret()
    status = pool.effective_status(ep, now)
    cooldown_left = max(0, int((ep.get("cooldown_until") or 0) - now))
    total = ep.get("total_requests") or 0
    success = ep.get("total_success") or 0
    return {
        "id": ep["id"],
        "name": ep["name"],
        "base_url": ep["base_url"],
        "api_key_masked": mask(plain),
        "has_key": bool(own),
        "uses_default_key": (not own) and bool(plain),
        "auth_header": ep["auth_header"],
        "auth_prefix": ep["auth_prefix"],
        "model": ep["model"],
        "weight": ep["weight"],
        "priority": ep["priority"],
        "enabled": bool(ep["enabled"]),
        "auto_disabled": bool(ep.get("auto_disabled") or 0),
        "disabled_at": ep.get("disabled_at") or 0,
        "auto_enable_at": ep.get("auto_enable_at") or 0,
        "revive_left": (
            max(0, int((ep.get("auto_enable_at") or 0) - now))
            if (not ep["enabled"]) and (ep.get("auto_enable_at") or 0) > 0 else 0
        ),
        "notes": ep["notes"],
        "status": status,
        "cooldown_left": cooldown_left,
        "consecutive_failures": ep.get("consecutive_failures") or 0,
        "total_requests": total,
        "total_success": success,
        "total_fail": ep.get("total_fail") or 0,
        "success_rate": round(success / total * 100, 1) if total else 0.0,
        "last_latency_ms": round(ep.get("last_latency_ms") or 0, 1),
        "last_error": ep.get("last_error") or "",
        "last_error_at": ep.get("last_error_at") or 0,
        "last_used_at": ep.get("last_used_at") or 0,
        "last_probe_at": ep.get("last_probe_at") or 0,
    }


def endpoint_or_404(endpoint_id: int) -> dict[str, Any]:
    ep = db.get_endpoint(endpoint_id)
    if not ep:
        raise HTTPException(status_code=404, detail="接口不存在")
    return ep


def normalize_url(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="base_url 必须以 http:// 或 https:// 开头")
    return url


# ------------------------------------------------------------------ 生命周期
@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    stop = asyncio.Event()
    task = asyncio.create_task(pool.auto_probe_loop(stop))
    house = asyncio.create_task(housekeeping_loop(stop))
    pool.log("info", "system", "号池服务启动")
    if not config.ADMIN_TOKEN:
        pool.log("warn", "system", "ADMIN_TOKEN 未设置，管理面板无口令保护")
    try:
        yield
    finally:
        stop.set()
        task.cancel()
        house.cancel()


async def housekeeping_loop(stop: asyncio.Event) -> None:
    """每 5 分钟：裁日志、裁会话表、截断 WAL、处理到期复活的号；一天最多 VACUUM 一次。"""
    last_vacuum = time.time()
    while not stop.is_set():
        try:
            settings = db.get_settings()
            removed = db.trim_logs(int(settings.get("log_keep") or 0),
                                   int(settings.get("log_keep_hours") or 0))
            db.trim_affinity(int(settings.get("affinity_max") or 0))
            db.checkpoint()
            if removed:
                pool.log("info", "housekeeping", f"按保留策略清理日志 {removed} 条")
            pool.revive_expired()  # 欠费停用满期的号自动回池
            if time.time() - last_vacuum > 86400:
                if db.vacuum_if_bloated():
                    pool.log("info", "housekeeping", "空页占比偏高，已 VACUUM 收缩数据库")
                last_vacuum = time.time()
        except Exception as exc:  # noqa: BLE001 - 清理任务不能把进程带走
            pool.log("error", "housekeeping", f"清理任务异常: {type(exc).__name__}: {exc}")
        try:
            await asyncio.wait_for(stop.wait(), timeout=300)
        except asyncio.TimeoutError:
            continue


app = FastAPI(title="Endpoint Pool", version="1.0.0", lifespan=lifespan)


# ------------------------------------------------------------------ 管理 API
@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "ts": time.time()}


@app.get("/api/overview", dependencies=[Depends(admin)])
async def overview() -> dict[str, Any]:
    now = time.time()
    items = [serialize(ep, now) for ep in db.list_endpoints()]
    ok = sum(1 for e in items if e["status"] == "ok")
    cooling = sum(1 for e in items if e["status"] == "cooling")
    disabled = sum(1 for e in items if not e["enabled"])
    disk = db.stats()
    return {
        "endpoints": items,
        "stats": {
            "total": len(items),
            "healthy": ok,
            "cooling": cooling,
            "disabled": disabled,
            "requests": sum(e["total_requests"] for e in items),
            "success": sum(e["total_success"] for e in items),
            "fail": sum(e["total_fail"] for e in items),
            "avg_latency_ms": round(
                sum(e["last_latency_ms"] for e in items if e["last_latency_ms"]) /
                max(1, sum(1 for e in items if e["last_latency_ms"])), 1
            ),
            "sticky_sessions": db.count_affinity(),
            "logs": disk["logs"],
            "db_mb": round((disk["db_bytes"] + disk["wal_bytes"]) / 1048576, 2),
        },
        "disk": disk,
        "settings": public_settings(db.get_settings()),
        "admin_token_required": bool(config.ADMIN_TOKEN),
        "proxy_token_required": bool(config.PROXY_TOKEN),
    }


@app.post("/api/endpoints", dependencies=[Depends(admin)])
async def create_endpoint(body: EndpointIn) -> dict[str, Any]:
    base = normalize_url(body.base_url)
    name = body.name.strip() or base.split("//")[-1][:60]
    eid = db.create_endpoint({
        "name": name,
        "base_url": base,
        "api_key_enc": encrypt(body.api_key.strip()),
        "auth_header": body.auth_header.strip() or "Authorization",
        "auth_prefix": body.auth_prefix,
        "model": body.model.strip(),
        "weight": max(1, body.weight),
        "priority": body.priority,
        "enabled": 1 if body.enabled else 0,
        "notes": body.notes.strip(),
    })
    pool.log("info", "admin", f"新增接口: {name}")
    return serialize(endpoint_or_404(eid))


@app.post("/api/endpoints/batch", dependencies=[Depends(admin)])
async def batch_create(items: list[EndpointIn]) -> dict[str, Any]:
    created = 0
    skipped = 0
    for item in items:
        try:
            base = normalize_url(item.base_url)
        except HTTPException:
            skipped += 1
            continue
        if not base:
            skipped += 1
            continue
        name = item.name.strip() or base.split("//")[-1][:60]
        db.create_endpoint({
            "name": name,
            "base_url": base,
            "api_key_enc": encrypt(item.api_key.strip()),
            "auth_header": item.auth_header.strip() or "Authorization",
            "auth_prefix": item.auth_prefix,
            "model": item.model.strip(),
            "weight": max(1, item.weight),
            "priority": item.priority,
            "enabled": 1 if item.enabled else 0,
            "notes": item.notes.strip(),
        })
        created += 1
    pool.log("info", "admin", f"批量导入 {created} 个接口，跳过 {skipped} 条")
    return {"created": created, "skipped": skipped}


@app.put("/api/endpoints/{endpoint_id}", dependencies=[Depends(admin)])
async def update_endpoint(endpoint_id: int, body: EndpointPatch) -> dict[str, Any]:
    ep = endpoint_or_404(endpoint_id)
    patch: dict[str, Any] = {}
    for field in ("name", "auth_header", "auth_prefix", "model", "notes"):
        value = getattr(body, field)
        if value is not None:
            patch[field] = value
    if body.base_url is not None:
        patch["base_url"] = normalize_url(body.base_url)
    if body.weight is not None:
        patch["weight"] = max(1, body.weight)
    if body.priority is not None:
        patch["priority"] = body.priority
    if body.enabled is not None:
        db.set_enabled(endpoint_id, body.enabled, 0)  # 人工开关，清掉自动停用标记
        if body.enabled:
            db.reset_state(endpoint_id)
    if body.api_key is not None and body.api_key.strip():
        patch["api_key_enc"] = encrypt(body.api_key.strip())
    db.update_endpoint(endpoint_id, patch)
    pool.log("info", "admin", f"更新接口: {ep['name']}")
    return serialize(endpoint_or_404(endpoint_id))


@app.delete("/api/endpoints/{endpoint_id}", dependencies=[Depends(admin)])
async def delete_endpoint(endpoint_id: int) -> dict[str, Any]:
    ep = endpoint_or_404(endpoint_id)
    db.delete_endpoint(endpoint_id)
    db.drop_affinity_for_endpoint(endpoint_id)
    pool.log("warn", "admin", f"删除接口: {ep['name']}")
    return {"deleted": endpoint_id}


@app.post("/api/endpoints/{endpoint_id}/toggle", dependencies=[Depends(admin)])
async def toggle_endpoint(endpoint_id: int) -> dict[str, Any]:
    ep = endpoint_or_404(endpoint_id)
    enable = not bool(ep["enabled"])
    db.set_enabled(endpoint_id, enable, 0)
    if enable:
        db.reset_state(endpoint_id)
    pool.log("info", "admin", f"{'启用' if enable else '停用'}接口: {ep['name']}")
    return serialize(endpoint_or_404(endpoint_id))


@app.post("/api/endpoints/{endpoint_id}/reset", dependencies=[Depends(admin)])
async def reset_endpoint(endpoint_id: int, counters: bool = Query(default=False)) -> dict[str, Any]:
    ep = endpoint_or_404(endpoint_id)
    db.reset_state(endpoint_id)
    db.set_enabled(endpoint_id, True, 0)  # 重置 = 人工确认复活，顺带解除自动停用
    if counters:
        db.reset_counters(endpoint_id)
    pool.log("info", "admin", f"重置状态: {ep['name']}")
    return serialize(endpoint_or_404(endpoint_id))


@app.post("/api/endpoints/{endpoint_id}/probe", dependencies=[Depends(admin)])
async def probe_endpoint(endpoint_id: int) -> dict[str, Any]:
    ep = endpoint_or_404(endpoint_id)
    return {"id": endpoint_id, "name": ep["name"], **await pool.probe(ep)}


@app.post("/api/probe-all", dependencies=[Depends(admin)])
async def probe_all() -> dict[str, Any]:
    return {"results": await pool.probe_all()}


@app.get("/api/settings", dependencies=[Depends(admin)])
async def get_settings() -> dict[str, Any]:
    return public_settings(db.get_settings())


@app.put("/api/settings", dependencies=[Depends(admin)])
async def put_settings(body: SettingsPatch) -> dict[str, Any]:
    data = body.model_dump(exclude_none=True)
    patch = {k: v for k, v in data.items() if k not in ("default_api_key", "clear_default_api_key")}
    if "strategy" in patch and patch["strategy"] not in (
        "round_robin", "weighted", "least_used", "priority", "random", "sticky"
    ):
        raise HTTPException(status_code=400, detail="未知策略")
    if "sticky_spread" in patch and patch["sticky_spread"] not in ("hash", "least_used"):
        raise HTTPException(status_code=400, detail="sticky_spread 只能是 hash 或 least_used")
    if data.pop("clear_default_api_key", False):
        patch["default_api_key_enc"] = ""
        pool.log("warn", "admin", "已清空全局默认密钥")
    new_key = (data.get("default_api_key") or "").strip()
    if new_key:
        patch["default_api_key_enc"] = encrypt(new_key)
        pool.log("info", "admin", f"更新全局默认密钥 {mask(new_key)}")
    settings = db.set_settings(patch)
    pool.log("info", "admin", "更新设置")
    return public_settings(settings)


@app.get("/api/logs", dependencies=[Depends(admin)])
async def get_logs(limit: int = Query(default=200, ge=1, le=2000)) -> dict[str, Any]:
    return {"logs": list(reversed(db.list_logs(limit)))}


@app.delete("/api/logs", dependencies=[Depends(admin)])
async def delete_logs() -> dict[str, Any]:
    db.clear_logs()
    return {"ok": True}


@app.post("/api/maintenance", dependencies=[Depends(admin)])
async def maintenance() -> dict[str, Any]:
    """立刻清一次日志/会话、截断 WAL、处理到期复活的号；空页多的话顺手 VACUUM。"""
    settings = db.get_settings()
    removed = db.trim_logs(int(settings.get("log_keep") or 0),
                           int(settings.get("log_keep_hours") or 0))
    sessions = db.trim_affinity(int(settings.get("affinity_max") or 0))
    db.checkpoint()
    revived = pool.revive_expired()
    vacuumed = db.vacuum_if_bloated()
    pool.log("info", "admin", f"手动维护：清日志 {removed} 条，清会话 {sessions} 条，"
                             f"到期复活 {len(revived)} 个")
    return {
        "removed_logs": removed,
        "removed_sessions": sessions,
        "revived": [{"id": r["id"], "name": r["name"]} for r in revived],
        "vacuumed": vacuumed,
        "stats": db.stats(),
    }


@app.get("/api/logs/stream")
async def log_stream(request: Request, token: Optional[str] = Query(default=None)) -> StreamingResponse:
    check_admin(token)

    async def gen():
        q = pool.subscribe()
        try:
            yield "retry: 3000\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    item = await asyncio.wait_for(q.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
        finally:
            pool.unsubscribe(q)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"cache-control": "no-cache", "x-accel-buffering": "no"},
    )


# ------------------------------------------------------------------ 代理
@app.api_route("/v1/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
async def proxy_v1(request: Request, path: str):
    check_proxy(request)
    return await proxy.handle(request, f"v1/{path}")


@app.api_route("/proxy/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
async def proxy_any(request: Request, path: str):
    check_proxy(request)
    return await proxy.handle(request, path)


@app.get("/api/pool/status")
async def pool_status() -> dict[str, Any]:
    """无需口令的轻量状态，给负载均衡探针/监控用，不泄露任何密钥。"""
    now = time.time()
    items = db.list_endpoints()
    return {
        "total": len(items),
        "healthy": sum(1 for e in items if pool.is_healthy(e, now)),
        "cooling": sum(1 for e in items if pool.effective_status(e, now) == "cooling"),
        "disabled": sum(1 for e in items if not e["enabled"]),
    }


# ------------------------------------------------------------------ 前端
if ROOT.exists():
    app.mount("/", StaticFiles(directory=str(ROOT), html=True), name="frontend")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("backend.app:app", host=config.HOST, port=config.PORT, reload=False)
