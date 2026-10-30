# language: Python, file: tests/mock_upstream.py
"""本地联调用的假上游：按 API Key 决定返回什么，用来验证换号逻辑。"""
from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BEHAVIOR = {
    "sk-quota": (402, {"error": {"message": "insufficient balance: 余额不足"}}),
    "sk-auth": (401, {"error": {"message": "invalid api key"}}),
    "sk-500": (500, {"error": {"message": "upstream boom"}}),
    "sk-429": (429, {"error": {"message": "rate limit exceeded"}}),
}


class Handler(BaseHTTPRequestHandler):
    server_version = "MockUpstream/1.0"
    protocol_version = "HTTP/1.1"  # 保持长连接，避免连接复用撞到已关闭的 socket

    def _key(self) -> str:
        auth = self.headers.get("authorization", "")
        return auth.replace("Bearer ", "").strip()

    def _send(self, status: int, payload: dict) -> None:
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _handle(self) -> None:
        key = self._key()
        if key in BEHAVIOR:
            status, payload = BEHAVIOR[key]
            self._send(status, payload)
            return
        length = int(self.headers.get("content-length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}") if length else {}
        port = self.server.server_address[1]
        self._send(200, {
            "id": f"mock-{port}",
            "object": "chat.completion",
            "served_by_port": port,
            "served_by_key": key,
            "model": body.get("model", "mock-model"),
            "choices": [{"index": 0, "message": {"role": "assistant", "content": f"hello from {key}"}}],
        })

    do_GET = _handle
    do_POST = _handle

    def log_message(self, *args):  # 静音
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8899
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
