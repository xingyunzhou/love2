# language: Python, file: backend/config.py
# 运行期配置：全部来自环境变量，容器部署时用 -e / env_file 注入
from __future__ import annotations

import os
from pathlib import Path

DATA_DIR = Path(os.getenv("DATA_DIR", "data")).expanduser().resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "pool.db"

# 管理面板口令；留空 = 面板不设防（本机调试用）
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "").strip()
# 代理转发口令；留空 = /v1/* 无需口令
PROXY_TOKEN = os.getenv("PROXY_TOKEN", "").strip()
# 加密号池密钥的主口令；留空则自动生成并落地到 DATA_DIR/.secret
SECRET_KEY = os.getenv("SECRET_KEY", "").strip()

HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8000"))
LOG_KEEP = int(os.getenv("LOG_KEEP", "3000"))

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
