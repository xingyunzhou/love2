# language: Python, file: backend/crypto.py
# 号池里的 api_key 落库前用 Fernet(AES-128-CBC + HMAC) 加密，前端只拿得到掩码
from __future__ import annotations

import base64
import hashlib
import os

from cryptography.fernet import Fernet, InvalidToken

from .config import DATA_DIR, SECRET_KEY

_SECRET_FILE = DATA_DIR / ".secret"


def _material() -> bytes:
    if SECRET_KEY:
        return SECRET_KEY.encode()
    if _SECRET_FILE.exists():
        return _SECRET_FILE.read_bytes().strip()
    raw = base64.urlsafe_b64encode(os.urandom(32))
    try:
        _SECRET_FILE.write_bytes(raw)
        try:
            os.chmod(_SECRET_FILE, 0o600)
        except OSError:
            pass
    except OSError as exc:  # 只读文件系统：内存密钥，重启后旧密文解不开
        print(f"[crypto] 无法写入 {_SECRET_FILE} ({exc})，本次使用临时密钥；请设置 SECRET_KEY", flush=True)
    return raw


_fernet = Fernet(base64.urlsafe_b64encode(hashlib.sha256(_material()).digest()))


def encrypt(plain: str) -> str:
    if not plain:
        return ""
    return _fernet.encrypt(plain.encode()).decode()


def decrypt(token: str) -> str:
    if not token:
        return ""
    try:
        return _fernet.decrypt(token.encode()).decode()
    except (InvalidToken, ValueError):
        return ""


def mask(plain: str) -> str:
    if not plain:
        return ""
    if len(plain) <= 8:
        return plain[:2] + "***"
    return f"{plain[:4]}...{plain[-4:]}"
