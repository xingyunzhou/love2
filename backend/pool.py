# language: Python, file: backend/pool.py
# 号池核心：选号策略、故障分类、冷却状态机、探活、日志广播
from __future__ import annotations

import asyncio
import random
import time
from typing import Any, Optional

import httpx

from . import db
from .crypto import decrypt

_cursor = 0
_subs: set[asyncio.Queue] = set()


# ------------------------------------------------------------------ 日志
def log(level: str, kind: str, message: str, ep: Optional[dict[str, Any]] = None,
        latency_ms: float = 0.0) -> None:
    endpoint_id = ep["id"] if ep else None
    name = ep["name"] if ep else ""
    db.add_log(level, kind, message, endpoint_id, name, latency_ms)
    event = {
        "ts": time.time(),
        "level": level,
        "kind": kind,
        "endpoint_id": endpoint_id,
        "endpoint_name": name,
        "message": message,
        "latency_ms": round(latency_ms, 1),
    }
    for q in list(_subs):
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            pass


def subscribe() -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=500)
    _subs.add(q)
    return q


def unsubscribe(q: asyncio.Queue) -> None:
    _subs.discard(q)


# ------------------------------------------------------------------ 状态
def effective_status(ep: dict[str, Any], now: Optional[float] = None) -> str:
    now = now if now is not None else time.time()
    if not ep.get("enabled", 1):
        return "disabled"
    status = ep.get("status") or "ok"
    if status == "cooling":
        return "ok" if (ep.get("cooldown_until") or 0) <= now else "cooling"
    return status


def is_healthy(ep: dict[str, Any], now: Optional[float] = None) -> bool:
    return effective_status(ep, now) == "ok"


def _cooldown_for(kind: str, settings: dict[str, Any]) -> float:
    table = {
        "quota": "cooldown_quota",
        "auth": "cooldown_auth",
        "rate_limit": "cooldown_rate",
        "server": "cooldown_server",
        "network": "cooldown_server",
        "client": "cooldown_server",
    }
    return float(settings.get(table.get(kind, "cooldown_server"), 60))


def classify(status: int, body: bytes, settings: dict[str, Any]) -> tuple[str, bool]:
    """把上游响应分类：返回 (kind, retryable)。retryable=False 表示是调用方的问题，直接透传。"""
    raw = (settings.get("quota_keywords") or "")
    keywords = [k.strip().lower() for k in str(raw).split(",") if k.strip()]
    text = body[:8192].decode("utf-8", "ignore").lower()
    hit_quota = any(k in text for k in keywords)

    if status == 402:
        return "quota", True
    if status == 429:
        return ("quota", True) if hit_quota else ("rate_limit", True)
    if status in (401, 403):
        return ("quota", True) if hit_quota else ("auth", True)
    if hit_quota:
        return "quota", True
    if status in (408, 425) or status >= 500:
        return "server", True
    return "client", False


def report_success(ep: dict[str, Any], latency_ms: float) -> None:
    db.run(
        "UPDATE endpoint_state SET status='ok', cooldown_until=0, consecutive_failures=0, "
        "total_requests=total_requests+1, total_success=total_success+1, "
        "last_latency_ms=?, last_used_at=? WHERE endpoint_id=?",
        (latency_ms, time.time(), ep["id"]),
    )


def report_failure(ep: dict[str, Any], kind: str, message: str, retryable: bool = True) -> None:
    settings = db.get_settings()
    cooldown = _cooldown_for(kind, settings) if retryable else 0
    status = "cooling" if cooldown > 0 else "ok"
    db.run(
        "UPDATE endpoint_state SET status=?, cooldown_until=?, "
        "consecutive_failures=consecutive_failures+1, total_requests=total_requests+1, "
        "total_fail=total_fail+1, last_error=?, last_error_at=?, last_used_at=? "
        "WHERE endpoint_id=?",
        (status, time.time() + cooldown, f"[{kind}] {message}"[:500], time.time(), time.time(), ep["id"]),
    )
    label = {
        "quota": "余额/额度不足，已冷却",
        "auth": "鉴权失败，已冷却",
        "rate_limit": "触发限流，短冷却",
        "server": "上游 5xx，短暂冷却",
        "network": "网络错误，短暂冷却",
        "client": "调用方错误，透传",
    }.get(kind, kind)
    level = "warn" if kind in ("quota", "auth", "rate_limit") else "error"
    cooldown_note = f"，冷却 {int(cooldown)}s" if cooldown > 0 else ""
    log(level, kind, f"{label}{cooldown_note} · {message[:200]}", ep)


# ------------------------------------------------------------------ 选号
async def pick(exclude: Optional[set[int]] = None) -> Optional[dict[str, Any]]:
    global _cursor
    exclude = exclude or set()
    now = time.time()
    eps = [e for e in db.list_endpoints() if e["id"] not in exclude and is_healthy(e, now)]
    if not eps:
        return None

    settings = db.get_settings()
    strategy = settings.get("strategy", "round_robin")

    if strategy == "random":
        return random.choice(eps)
    if strategy == "least_used":
        return min(eps, key=lambda e: (e["total_requests"], e["id"]))
    if strategy == "priority":
        top = max(e["priority"] for e in eps)
        eps = [e for e in eps if e["priority"] == top]
    if strategy == "weighted":
        bag: list[dict[str, Any]] = []
        for e in eps:
            bag.extend([e] * max(1, min(int(e["weight"] or 1), 50)))
        _cursor = (_cursor + 1) % len(bag)
        return bag[_cursor]

    # round_robin / priority 内部轮询
    _cursor = (_cursor + 1) % len(eps)
    return eps[_cursor]


def secret(ep: dict[str, Any]) -> str:
    return decrypt(ep.get("api_key_enc", ""))


def auth_headers(ep: dict[str, Any]) -> dict[str, str]:
    key = secret(ep)
    if not key:
        return {}
    header = ep.get("auth_header") or "Authorization"
    prefix = ep.get("auth_prefix") or ""
    return {header: f"{prefix}{key}".strip()}


# ------------------------------------------------------------------ 探活
async def probe(ep: dict[str, Any]) -> dict[str, Any]:
    settings = db.get_settings()
    path = settings.get("probe_path") or "/v1/models"
    url = ep["base_url"].rstrip("/") + "/" + path.lstrip("/")
    timeout = float(settings.get("probe_timeout") or 20)
    headers = {"accept": "application/json", **auth_headers(ep)}
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            resp = await client.get(url, headers=headers)
        latency = (time.perf_counter() - started) * 1000
    except httpx.HTTPError as exc:
        report_failure(ep, "network", f"探活失败 {type(exc).__name__}: {exc}")
        db.run("UPDATE endpoint_state SET last_probe_at=?, last_probe_ms=0 WHERE endpoint_id=?",
               (time.time(), ep["id"]))
        return {"ok": False, "status": 0, "latency_ms": 0, "error": str(exc)}

    db.run("UPDATE endpoint_state SET last_probe_at=?, last_probe_ms=? WHERE endpoint_id=?",
           (time.time(), latency, ep["id"]))

    if resp.status_code < 400:
        report_success(ep, latency)
        log("info", "probe", f"探活成功 {resp.status_code} · {latency:.0f}ms", ep, latency)
        return {"ok": True, "status": resp.status_code, "latency_ms": round(latency, 1), "error": ""}

    kind, retryable = classify(resp.status_code, resp.content, settings)
    if retryable:
        report_failure(ep, kind, f"探活 {resp.status_code}: {resp.text[:200]}", retryable)
    else:
        log("warn", "probe", f"探活返回 {resp.status_code}（调用方问题，不改状态）", ep)
    return {
        "ok": False,
        "status": resp.status_code,
        "latency_ms": round(latency, 1),
        "error": resp.text[:300],
    }


async def probe_all() -> list[dict[str, Any]]:
    out = []
    for ep in db.list_endpoints():
        if not ep["enabled"]:
            continue
        result = await probe(ep)
        out.append({"id": ep["id"], "name": ep["name"], **result})
    return out


async def auto_probe_loop(stop: asyncio.Event) -> None:
    """周期性探活处于冷却状态的接口，余额恢复了就自动放回池子。"""
    while not stop.is_set():
        settings = db.get_settings()
        interval = int(settings.get("probe_interval") or 0)
        if interval <= 0:
            sleep_for = 30
        else:
            now = time.time()
            for ep in db.list_endpoints():
                if not ep["enabled"] or effective_status(ep, now) != "cooling":
                    continue
                await probe(ep)
            sleep_for = max(interval, 15)
        try:
            await asyncio.wait_for(stop.wait(), timeout=sleep_for)
        except asyncio.TimeoutError:
            continue
