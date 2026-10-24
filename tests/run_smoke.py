# language: Python, file: tests/run_smoke.py
"""端到端冒烟（进程内）：起假上游线程 + FastAPI TestClient，验证轮询/换号/冷却/探活/日志。

用法：python tests/run_smoke.py   （数据落在 ./data，跑完自动清掉测试库）
"""
from __future__ import annotations

import os
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)
os.environ["DATA_DIR"] = str(DATA_DIR)
os.environ["ADMIN_TOKEN"] = "smoke-token"

for leftover in ("pool.db", "pool.db-wal", "pool.db-shm"):
    try:
        (DATA_DIR / leftover).unlink()
    except FileNotFoundError:
        pass

from fastapi.testclient import TestClient  # noqa: E402

from backend.app import app  # noqa: E402
from tests.mock_upstream import Handler  # noqa: E402

H = {"x-admin-token": "smoke-token"}
PORTS = (8899, 8900, 8901)
SERVERS = []


def start_upstreams() -> None:
    for port in PORTS:
        srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        SERVERS.append(srv)


def stop_upstreams() -> None:
    for srv in SERVERS:
        srv.shutdown()
        srv.server_close()


def main() -> int:
    start_upstreams()
    try:
        with TestClient(app) as client:
            # 1) 健康检查 / 鉴权
            assert client.get("/api/health").json()["ok"] is True
            assert client.get("/api/overview").status_code == 401, "缺少口令应 401"
            assert client.get("/api/pool/status").status_code == 200

            # 2) 建池：余额不足 / 鉴权失败 / 正常
            add = [
                {"name": "dead-quota", "base_url": f"http://127.0.0.1:{PORTS[0]}", "api_key": "sk-quota"},
                {"name": "dead-auth", "base_url": f"http://127.0.0.1:{PORTS[1]}", "api_key": "sk-auth"},
                {"name": "alive", "base_url": f"http://127.0.0.1:{PORTS[2]}", "api_key": "sk-alive"},
            ]
            ids = []
            for item in add:
                r = client.post("/api/endpoints", headers=H, json=item)
                assert r.status_code == 200, r.text
                body = r.json()
                ids.append(body["id"])
                assert body["has_key"] is True
                assert item["api_key"] not in body["api_key_masked"], body["api_key_masked"]
            assert len(client.get("/api/overview", headers=H).json()["endpoints"]) == 3

            # 3) 首次调用：坏号被冷却，最后由 alive 返回（自动换号）
            payload = {"model": "mock-model", "messages": [{"role": "user", "content": "hi"}]}
            r = client.post("/v1/chat/completions", json=payload)
            assert r.status_code == 200, r.text
            assert r.json()["served_by_key"] == "sk-alive", r.text
            print(f"  首次调用 → {r.json()['served_by_key']} "
                  f"(x-pool-endpoint={r.headers['x-pool-endpoint']}, attempt={r.headers['x-pool-attempt']})")

            ov = client.get("/api/overview", headers=H).json()
            by_name = {e["name"]: e for e in ov["endpoints"]}
            # 402 不是冷却，是直接停用
            assert by_name["dead-quota"]["status"] == "disabled", by_name["dead-quota"]
            assert by_name["dead-quota"]["enabled"] is False, by_name["dead-quota"]
            assert by_name["dead-quota"]["auto_disabled"] is True, by_name["dead-quota"]
            assert by_name["dead-auth"]["status"] == "cooling", by_name["dead-auth"]
            assert by_name["dead-auth"]["enabled"] is True, by_name["dead-auth"]
            assert by_name["alive"]["status"] == "ok", by_name["alive"]
            print(f"  402 → 自动停用: dead-quota enabled={by_name['dead-quota']['enabled']} "
                  f"auto={by_name['dead-quota']['auto_disabled']} ← "
                  f"{by_name['dead-quota']['last_error'][:50]}")
            print(f"  401 → 冷却: dead-auth {by_name['dead-auth']['cooldown_left']}s")

            # 3.5) 停用的号：不参与路由、不参与批量探活、探活成功也不自己启用
            r = client.put(f"/api/endpoints/{ids[0]}", headers=H, json={"api_key": "sk-recovered"})
            assert r.json()["enabled"] is False, "改 key 不该顺手启用"

            r = client.post("/api/endpoints/%d/probe" % ids[0], headers=H)
            assert r.json()["ok"] is True, r.text
            after_probe = next(e for e in client.get("/api/overview", headers=H).json()["endpoints"]
                               if e["id"] == ids[0])
            assert after_probe["enabled"] is False, "停用的号探活成功也不能自动启用"
            assert after_probe["auto_disabled"] is True, after_probe

            probed = {x["id"] for x in client.post("/api/probe-all", headers=H).json()["results"]}
            assert ids[0] not in probed, "全部探活不该碰停用的号"
            print("  停用号不路由 / 不批量探活 / 探活不自动启用: ok")

            # 还原成坏 key 并保持停用，让后面的轮询断言保持确定
            client.put(f"/api/endpoints/{ids[0]}", headers=H,
                       json={"api_key": "sk-quota", "enabled": False})

            # 4) 后续请求不再碰冷却中的接口
            for _ in range(3):
                r = client.post("/v1/chat/completions", json=payload)
                assert r.status_code == 200 and r.json()["served_by_key"] == "sk-alive", r.text
            print("  轮询自动跳过冷却接口: ok")

            # 4.5) base_url 自带 /v1 时不应拼成 /v1/v1（先停掉其它活号，让它必须被选中）
            r = client.post("/api/endpoints", headers=H,
                            json={"name": "v1-suffix", "base_url": f"http://127.0.0.1:{PORTS[2]}/v1", "api_key": "sk-v1suffix"})
            assert r.status_code == 200, r.text
            v1_id = r.json()["id"]
            client.post(f"/api/endpoints/{ids[2]}/toggle", headers=H)  # 停用 alive
            r = client.post("/v1/chat/completions", json=payload)
            assert r.status_code == 200, r.text
            assert r.json()["served_by_key"] == "sk-v1suffix", r.text
            client.post(f"/api/endpoints/{ids[2]}/toggle", headers=H)  # 恢复 alive
            client.delete(f"/api/endpoints/{v1_id}", headers=H)
            print("  base_url 带 /v1 不重复拼接: ok")

            # 5) 上游 500 / 429 也走换号
            for name, key in (("boom", "sk-500"), ("limited", "sk-429")):
                client.post("/api/endpoints", headers=H,
                            json={"name": name, "base_url": f"http://127.0.0.1:{PORTS[0]}", "api_key": key})
            ov = client.get("/api/overview", headers=H).json()
            assert ov["stats"]["total"] == 5
            for _ in range(2):
                r = client.post("/v1/chat/completions", json=payload)
                assert r.status_code == 200 and r.json()["served_by_key"] == "sk-alive", r.text
            by_name = {e["name"]: e for e in client.get("/api/overview", headers=H).json()["endpoints"]}
            assert by_name["boom"]["status"] == "cooling"
            assert by_name["limited"]["status"] == "cooling"
            print("  5xx / 429 冷却: ok")

            # 6) 探活 + 重置
            r = client.post(f"/api/endpoints/{ids[2]}/probe", headers=H)
            assert r.status_code == 200 and r.json()["ok"] is True, r.text
            r = client.post(f"/api/endpoints/{ids[0]}/reset?counters=true", headers=H)
            assert r.json()["status"] == "ok" and r.json()["total_requests"] == 0, r.text
            assert r.json()["enabled"] is True and r.json()["auto_disabled"] is False, r.json()
            r = client.post(f"/api/endpoints/{ids[1]}/toggle", headers=H)
            assert r.json()["enabled"] is False
            r = client.post(f"/api/endpoints/{ids[1]}/toggle", headers=H)
            assert r.json()["enabled"] is True
            print("  探活 / 重置 / 启停: ok")

            # 7) 设置校验
            s = client.put("/api/settings", headers=H,
                           json={"strategy": "least_used", "max_attempts": 3, "cooldown_quota": 600}).json()
            assert s["strategy"] == "least_used" and s["max_attempts"] == 3 and s["cooldown_quota"] == 600
            assert client.put("/api/settings", headers=H, json={"strategy": "nope"}).status_code == 400
            print("  设置校验: ok")

            # 8) 全局默认密钥：设置一次，加号时留空即可
            r = client.put("/api/settings", headers=H, json={"default_api_key": "sk-global-default"})
            assert r.status_code == 200, r.text
            assert r.json()["has_default_api_key"] is True
            assert "sk-global" not in r.text, "设置接口不应回传默认密钥本体"
            assert "default_api_key_enc" not in r.text

            r = client.post("/api/endpoints", headers=H,
                            json={"name": "no-key", "base_url": f"http://127.0.0.1:{PORTS[2]}"})
            assert r.status_code == 200, r.text
            nokey = r.json()
            assert nokey["has_key"] is False and nokey["uses_default_key"] is True, nokey

            client.post(f"/api/endpoints/{ids[2]}/toggle", headers=H)  # 停掉 alive，只留 no-key
            r = client.post("/v1/chat/completions", json=payload)
            assert r.status_code == 200, r.text
            assert r.json()["served_by_key"] == "sk-global-default", r.text
            print("  全局默认密钥生效: ok")

            r = client.post(f"/api/endpoints/{nokey['id']}/probe", headers=H)
            assert r.status_code == 200 and r.json()["ok"] is True, r.text
            client.post(f"/api/endpoints/{ids[2]}/toggle", headers=H)  # 恢复 alive
            client.delete(f"/api/endpoints/{nokey['id']}", headers=H)

            r = client.put("/api/settings", headers=H, json={"clear_default_api_key": True}).json()
            assert r["has_default_api_key"] is False, r
            print("  默认密钥可清空: ok")

            # 9) 日志已经记下换号过程
            logs = client.get("/api/logs?limit=80", headers=H).json()["logs"]
            kinds = {l["kind"] for l in logs}
            assert {"quota", "auth"} <= kinds, kinds
            assert any(l["kind"] == "route" for l in logs)
            print(f"  日志: {len(logs)} 条，种类 {sorted(k for k in kinds if k)}")

            # 9) 前端静态资源
            r = client.get("/")
            assert r.status_code == 200 and "号池控制台" in r.text
            assert client.get("/app.js").status_code == 200
            assert client.get("/style.css").status_code == 200
            print("  静态前端: ok")

            # 9.5) 粘性路由：同一会话固定命中同一接口（上游 prompt 缓存才可能命中）
            client.put(f"/api/endpoints/{ids[0]}", headers=H, json={"enabled": False})  # 幂等停用，池里只留活号
            r = client.post("/api/endpoints", headers=H,
                            json={"name": "sticky-b", "base_url": f"http://127.0.0.1:{PORTS[1]}",
                                  "api_key": "sk-sticky-b"})
            sticky_b = r.json()["id"]
            client.put("/api/settings", headers=H, json={"strategy": "sticky", "sticky_ttl": 600})

            def call(sid=None, body=None):
                headers = {"x-session-id": sid} if sid else {}
                res = client.post("/v1/chat/completions", json=body or payload, headers=headers)
                assert res.status_code == 200, res.text
                return res.json()["served_by_key"]

            first = call("conv-1")
            for _ in range(4):
                assert call("conv-1") == first, "同一会话被拆到了不同接口"
            heads = {call(f"conv-{i}") for i in range(6)}
            assert len(heads) <= 2, heads

            # 不带 session 头时用「system + 首条用户消息」的前缀哈希，同一个前缀也要粘住
            body_a = {"model": "m", "messages": [
                {"role": "system", "content": "你是运维助手，回答保持简短。"},
                {"role": "user", "content": "第一问"},
            ]}
            body_b = {"model": "m", "messages": [
                {"role": "system", "content": "你是运维助手，回答保持简短。"},
                {"role": "user", "content": "第一问"},
                {"role": "assistant", "content": "答"},
                {"role": "user", "content": "第二问"},
            ]}
            a1 = call(body=body_a)
            assert call(body=body_a) == a1
            assert call(body=body_b) == a1, "同一前缀追加消息后应继续命中同一接口"
            assert client.get("/api/overview", headers=H).json()["stats"]["sticky_sessions"] >= 2
            print(f"  粘性路由: 会话固定命中 {first}，前缀粘性 ok")

            # 新会话挑号方式可切
            s = client.put("/api/settings", headers=H, json={"sticky_spread": "least_used"}).json()
            assert s["sticky_spread"] == "least_used", s
            assert call("conv-least-used") in ("sk-alive", "sk-sticky-b")
            assert client.put("/api/settings", headers=H, json={"sticky_spread": "nope"}).status_code == 400
            client.put("/api/settings", headers=H, json={"sticky_spread": "hash"})
            print("  新会话挑号方式可切: ok")

            client.delete(f"/api/endpoints/{sticky_b}", headers=H)
            client.put("/api/settings", headers=H, json={"strategy": "round_robin"})

            # 10) 删除
            assert client.delete(f"/api/endpoints/{ids[0]}", headers=H).status_code == 200
            assert client.get("/api/overview", headers=H).json()["stats"]["total"] == 4
            print("  删除: ok")

            # 11) 日志自动清理：条数上限 + 时间上限 + WAL 截断
            client.put("/api/settings", headers=H, json={"log_keep": 5, "log_keep_hours": 168})
            before = client.get("/api/overview", headers=H).json()["disk"]
            assert before["logs"] > 5, before
            r = client.post("/api/maintenance", headers=H).json()
            assert r["removed_logs"] > 0, r
            assert r["stats"]["logs"] <= 10, r["stats"]  # 清理后自己又写了一条
            assert r["stats"]["db_bytes"] > 0
            ov = client.get("/api/overview", headers=H).json()
            assert client.get("/api/settings", headers=H).json()["log_keep"] == 5
            assert "disk" in ov and "db_bytes" in ov["disk"], ov.get("disk")
            print(f"  日志清理: {before['logs']} → {r['stats']['logs']} 条，"
                  f"库 {ov['stats']['db_mb']} MB")

        print("\nALL SMOKE CHECKS PASSED")
        return 0
    finally:
        stop_upstreams()
        from backend import db as _db
        _db.close()
        for leftover in ("pool.db", "pool.db-wal", "pool.db-shm"):
            try:
                (DATA_DIR / leftover).unlink()
            except (FileNotFoundError, PermissionError):
                pass


if __name__ == "__main__":
    raise SystemExit(main())
