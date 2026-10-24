# language: Python, file: backend/db.py
# SQLite 持久层：endpoints / endpoint_state / settings / logs，单文件库挂在 /data 卷上
from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Any, Iterable, Optional

from .config import DB_PATH, LOG_KEEP

_lock = threading.RLock()
_conn: Optional[sqlite3.Connection] = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS endpoints (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT    NOT NULL,
    base_url     TEXT    NOT NULL,
    api_key_enc  TEXT    NOT NULL DEFAULT '',
    auth_header  TEXT    NOT NULL DEFAULT 'Authorization',
    auth_prefix  TEXT    NOT NULL DEFAULT 'Bearer ',
    model        TEXT    NOT NULL DEFAULT '',
    weight       INTEGER NOT NULL DEFAULT 1,
    priority     INTEGER NOT NULL DEFAULT 0,
    enabled      INTEGER NOT NULL DEFAULT 1,
    notes        TEXT    NOT NULL DEFAULT '',
    created_at   REAL    NOT NULL,
    updated_at   REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS endpoint_state (
    endpoint_id          INTEGER PRIMARY KEY,
    status               TEXT    NOT NULL DEFAULT 'ok',
    cooldown_until       REAL    NOT NULL DEFAULT 0,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    total_requests       INTEGER NOT NULL DEFAULT 0,
    total_success        INTEGER NOT NULL DEFAULT 0,
    total_fail           INTEGER NOT NULL DEFAULT 0,
    last_latency_ms      REAL    NOT NULL DEFAULT 0,
    last_error           TEXT    NOT NULL DEFAULT '',
    last_error_at        REAL    NOT NULL DEFAULT 0,
    last_used_at         REAL    NOT NULL DEFAULT 0,
    last_probe_at        REAL    NOT NULL DEFAULT 0,
    last_probe_ms        REAL    NOT NULL DEFAULT 0,
    auto_disabled        INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS logs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            REAL,
    level         TEXT,
    kind          TEXT,
    endpoint_id   INTEGER,
    endpoint_name TEXT,
    message       TEXT,
    latency_ms    REAL
);

-- 会话粘性：同一个请求前缀永远落到同一个接口，命中的是上游的 prompt 缓存
CREATE TABLE IF NOT EXISTS affinity (
    session_key TEXT PRIMARY KEY,
    endpoint_id INTEGER NOT NULL,
    updated_at  REAL    NOT NULL,
    hits        INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_affinity_updated ON affinity(updated_at);
CREATE INDEX IF NOT EXISTS idx_logs_ts ON logs(ts);
"""

SETTINGS_DEFAULTS: dict[str, Any] = {
    "strategy": "round_robin",      # round_robin | weighted | least_used | priority | random
    "max_attempts": 5,              # 单次请求最多尝试几个接口
    "request_timeout": 300,         # 秒，含流式
    "cooldown_quota": 1800,         # 余额/额度不足 → 冷多久
    "cooldown_auth": 1800,          # 401/403 → 冷多久
    "cooldown_rate": 30,            # 429 限流 → 冷多久
    "cooldown_server": 60,          # 5xx / 网络错误 → 冷多久
    "probe_path": "/v1/models",     # 探活请求路径（拼在 base_url 后）
    "probe_interval": 120,          # 秒，0 = 关闭自动探活
    "probe_timeout": 20,
    "quota_keywords": (
        "insufficient_quota,insufficient balance,insufficient_balance,quota exceeded,"
        "exceeded your current quota,no credit,credit balance,payment required,"
        "billing,余额不足,额度不足,余额为0,欠费,账户异常,已用完"
    ),
    # 全局默认密钥：很多池子所有接口共用同一个 key，加号时不用重复填
    "default_api_key_enc": "",
    "default_auth_header": "Authorization",
    "default_auth_prefix": "Bearer ",
    # 粘性路由：同一会话/同一前缀固定走同一个接口，吃满上游 prompt 缓存
    "sticky_ttl": 1800,
    "sticky_header": "x-session-id",
    # 新会话怎么挑号：hash = 一致性哈希（稳定），least_used = 按当前请求量挑最闲的
    "sticky_spread": "hash",
    # 日志保留：条数上限 + 时间上限，双阈值谁先到砍谁
    "log_keep": int(LOG_KEEP),
    "log_keep_hours": 168,
    "affinity_max": 20000,
    # 402 / 余额不足 → 直接停用该接口（不是冷却），并允许探活确认恢复后自动启用
    "disable_on_quota": True,
    "auto_revive": True,
}

_ENDPOINT_COLUMNS = (
    "id, name, base_url, api_key_enc, auth_header, auth_prefix, model, weight, "
    "priority, enabled, notes, created_at, updated_at"
)

_LIST_SQL = f"""
SELECT e.id, e.name, e.base_url, e.api_key_enc, e.auth_header, e.auth_prefix, e.model,
       e.weight, e.priority, e.enabled, e.notes, e.created_at, e.updated_at,
       COALESCE(s.status, 'ok')               AS status,
       COALESCE(s.cooldown_until, 0)          AS cooldown_until,
       COALESCE(s.consecutive_failures, 0)    AS consecutive_failures,
       COALESCE(s.total_requests, 0)          AS total_requests,
       COALESCE(s.total_success, 0)           AS total_success,
       COALESCE(s.total_fail, 0)              AS total_fail,
       COALESCE(s.last_latency_ms, 0)         AS last_latency_ms,
       COALESCE(s.last_error, '')             AS last_error,
       COALESCE(s.last_error_at, 0)           AS last_error_at,
       COALESCE(s.last_used_at, 0)            AS last_used_at,
       COALESCE(s.last_probe_at, 0)           AS last_probe_at,
       COALESCE(s.last_probe_ms, 0)           AS last_probe_ms,
       COALESCE(s.auto_disabled, 0)           AS auto_disabled
FROM endpoints e LEFT JOIN endpoint_state s ON s.endpoint_id = e.id
"""


def conn() -> sqlite3.Connection:
    global _conn
    with _lock:
        if _conn is None:
            _conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
            _conn.row_factory = sqlite3.Row
            _conn.execute("PRAGMA journal_mode=WAL")
            _conn.execute("PRAGMA synchronous=NORMAL")
            _conn.execute("PRAGMA busy_timeout=5000")
            _conn.execute("PRAGMA foreign_keys=ON")
        return _conn


def _migrate(c: sqlite3.Connection) -> None:
    """老库补列：SQLite 没有 ADD COLUMN IF NOT EXISTS，只能先查后加。"""
    cols = {row[1] for row in c.execute("PRAGMA table_info(endpoint_state)").fetchall()}
    if "auto_disabled" not in cols:
        c.execute("ALTER TABLE endpoint_state ADD COLUMN auto_disabled INTEGER NOT NULL DEFAULT 0")
        c.commit()


def init() -> None:
    with _lock:
        c = conn()
        c.executescript(SCHEMA)
        _migrate(c)
        for key, value in SETTINGS_DEFAULTS.items():
            c.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO NOTHING",
                (key, json.dumps(value)),
            )
        c.commit()
        # 清理历史日志，只留 LOG_KEEP 条
        c.execute(
            "DELETE FROM logs WHERE id NOT IN (SELECT id FROM logs ORDER BY id DESC LIMIT ?)",
            (LOG_KEEP,),
        )
        c.commit()


def close() -> None:
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
            _conn = None


def _params(params: Any) -> Any:
    """支持 (?, ?) 元组和 {name: value} 命名参数，别把 dict 转成 tuple。"""
    if isinstance(params, dict):
        return params
    return tuple(params)


def rows(sql: str, params: Any = ()) -> list[dict[str, Any]]:
    with _lock:
        cur = conn().execute(sql, _params(params))
        return [dict(r) for r in cur.fetchall()]


def one(sql: str, params: Any = ()) -> Optional[dict[str, Any]]:
    with _lock:
        cur = conn().execute(sql, _params(params))
        row = cur.fetchone()
        return dict(row) if row else None


def run(sql: str, params: Any = ()) -> int:
    with _lock:
        c = conn()
        cur = c.execute(sql, _params(params))
        c.commit()
        return cur.lastrowid or cur.rowcount


# ---------------------------------------------------------------- settings
def get_settings() -> dict[str, Any]:
    out = dict(SETTINGS_DEFAULTS)
    for r in rows("SELECT key, value FROM settings"):
        try:
            out[r["key"]] = json.loads(r["value"])
        except json.JSONDecodeError:
            out[r["key"]] = r["value"]
    # 数值型统一成 int/float，避免前端传来字符串
    for key, default in SETTINGS_DEFAULTS.items():
        if isinstance(default, int) and not isinstance(default, bool):
            try:
                out[key] = int(float(out[key]))
            except (TypeError, ValueError):
                out[key] = default
    if not isinstance(out.get("quota_keywords"), str):
        out["quota_keywords"] = SETTINGS_DEFAULTS["quota_keywords"]
    return out


def set_settings(patch: dict[str, Any]) -> dict[str, Any]:
    with _lock:
        c = conn()
        for key, value in patch.items():
            if key not in SETTINGS_DEFAULTS:
                continue
            c.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, json.dumps(value)),
            )
        c.commit()
    return get_settings()


# --------------------------------------------------------------- endpoints
def list_endpoints() -> list[dict[str, Any]]:
    return rows(_LIST_SQL + " ORDER BY e.id ASC")


def get_endpoint(endpoint_id: int) -> Optional[dict[str, Any]]:
    got = rows(_LIST_SQL + " WHERE e.id = ?", (endpoint_id,))
    return got[0] if got else None


def create_endpoint(payload: dict[str, Any]) -> int:
    now = time.time()
    eid = run(
        f"INSERT INTO endpoints ({_ENDPOINT_COLUMNS}) "
        "VALUES (NULL, :name, :base_url, :api_key_enc, :auth_header, :auth_prefix, :model, "
        ":weight, :priority, :enabled, :notes, :created_at, :updated_at)",
        {**payload, "created_at": now, "updated_at": now},
    )
    run("INSERT INTO endpoint_state(endpoint_id) VALUES(?) ON CONFLICT(endpoint_id) DO NOTHING", (eid,))
    return eid


_UPDATABLE = {
    "name", "base_url", "api_key_enc", "auth_header", "auth_prefix", "model",
    "weight", "priority", "enabled", "notes",
}


def update_endpoint(endpoint_id: int, payload: dict[str, Any]) -> None:
    sets, params = [], []
    for key, value in payload.items():
        if key in _UPDATABLE:
            sets.append(f"{key} = ?")
            params.append(value)
    if not sets:
        return
    sets.append("updated_at = ?")
    params.extend([time.time(), endpoint_id])
    run(f"UPDATE endpoints SET {', '.join(sets)} WHERE id = ?", params)


def delete_endpoint(endpoint_id: int) -> None:
    run("DELETE FROM endpoint_state WHERE endpoint_id = ?", (endpoint_id,))
    run("DELETE FROM endpoints WHERE id = ?", (endpoint_id,))


def set_enabled(endpoint_id: int, enabled: bool, auto_disabled: int = 0) -> None:
    """启停开关。auto_disabled 标记这次停用是系统自动判的，不是人手点的。"""
    run("UPDATE endpoints SET enabled = ?, updated_at = ? WHERE id = ?",
        (1 if enabled else 0, time.time(), endpoint_id))
    run("UPDATE endpoint_state SET auto_disabled = ? WHERE endpoint_id = ?",
        (1 if auto_disabled else 0, endpoint_id))


def reset_state(endpoint_id: int) -> None:
    run(
        "UPDATE endpoint_state SET status='ok', cooldown_until=0, consecutive_failures=0, "
        "last_error='', last_error_at=0 WHERE endpoint_id = ?",
        (endpoint_id,),
    )


def reset_counters(endpoint_id: int) -> None:
    run(
        "UPDATE endpoint_state SET total_requests=0, total_success=0, total_fail=0, "
        "last_latency_ms=0 WHERE endpoint_id = ?",
        (endpoint_id,),
    )


# ---------------------------------------------------------------- 会话粘性
def get_affinity(session_key: str) -> Optional[dict[str, Any]]:
    return one("SELECT session_key, endpoint_id, updated_at, hits FROM affinity WHERE session_key = ?",
               (session_key,))


def touch_affinity(session_key: str, endpoint_id: int) -> None:
    now = time.time()
    row = one("SELECT endpoint_id, hits FROM affinity WHERE session_key = ?", (session_key,))
    if row is None:
        run("INSERT INTO affinity(session_key, endpoint_id, updated_at, hits) VALUES(?,?,?,1)",
            (session_key, endpoint_id, now))
        return
    hits = (row["hits"] + 1) if row["endpoint_id"] == endpoint_id else 1
    run("UPDATE affinity SET endpoint_id=?, updated_at=?, hits=? WHERE session_key=?",
        (endpoint_id, now, hits, session_key))


def prune_affinity(ttl: float) -> None:
    run("DELETE FROM affinity WHERE updated_at < ?", (time.time() - ttl,))


def drop_affinity_for_endpoint(endpoint_id: int) -> None:
    run("DELETE FROM affinity WHERE endpoint_id = ?", (endpoint_id,))


def count_affinity() -> int:
    row = one("SELECT COUNT(*) AS c FROM affinity")
    return int(row["c"]) if row else 0


# -------------------------------------------------------------------- logs
def add_log(level: str, kind: str, message: str,
            endpoint_id: Optional[int] = None, endpoint_name: str = "",
            latency_ms: float = 0.0) -> None:
    run(
        "INSERT INTO logs(ts, level, kind, endpoint_id, endpoint_name, message, latency_ms) "
        "VALUES(?,?,?,?,?,?,?)",
        (time.time(), level, kind, endpoint_id, endpoint_name, message, latency_ms),
    )


def list_logs(limit: int = 200) -> list[dict[str, Any]]:
    return rows("SELECT * FROM logs ORDER BY id DESC LIMIT ?", (limit,))


def clear_logs() -> None:
    run("DELETE FROM logs")


# ---------------------------------------------------------------- 清理与体积
def trim_logs(keep: int = 0, hours: int = 0) -> int:
    """双阈值裁剪：先按时间砍，再按条数砍；返回删掉多少条。"""
    removed = 0
    with _lock:
        c = conn()
        if hours > 0:
            cur = c.execute("DELETE FROM logs WHERE ts < ?", (time.time() - hours * 3600,))
            removed += max(cur.rowcount, 0)
        if keep > 0:
            cur = c.execute(
                "DELETE FROM logs WHERE id NOT IN "
                "(SELECT id FROM logs ORDER BY id DESC LIMIT ?)",
                (keep,),
            )
            removed += max(cur.rowcount, 0)
        c.commit()
    return removed


def trim_affinity(max_rows: int) -> int:
    if max_rows <= 0:
        return 0
    with _lock:
        c = conn()
        cur = c.execute(
            "DELETE FROM affinity WHERE session_key NOT IN "
            "(SELECT session_key FROM affinity ORDER BY updated_at DESC LIMIT ?)",
            (max_rows,),
        )
        c.commit()
        return max(cur.rowcount, 0)


def checkpoint() -> None:
    """把 WAL 合回主库并截断，否则 -wal 文件会一直涨。"""
    with _lock:
        try:
            conn().execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass


def vacuum_if_bloated(ratio: float = 0.3, min_pages: int = 5000) -> bool:
    """空页占比过高才 VACUUM，删行后文件不会自己缩。"""
    with _lock:
        c = conn()
        try:
            page_count = c.execute("PRAGMA page_count").fetchone()[0] or 0
            free_pages = c.execute("PRAGMA freelist_count").fetchone()[0] or 0
        except sqlite3.Error:
            return False
        if page_count < min_pages or free_pages / page_count < ratio:
            return False
        try:
            c.execute("VACUUM")
            return True
        except sqlite3.Error:
            return False


def stats() -> dict[str, int]:
    db_bytes = DB_PATH.stat().st_size if DB_PATH.exists() else 0
    wal_path = DB_PATH.with_name(DB_PATH.name + "-wal")
    wal_bytes = wal_path.stat().st_size if wal_path.exists() else 0
    log_row = one("SELECT COUNT(*) AS c FROM logs") or {"c": 0}
    return {
        "logs": int(log_row["c"]),
        "affinity": count_affinity(),
        "db_bytes": int(db_bytes),
        "wal_bytes": int(wal_bytes),
    }
