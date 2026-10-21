# language: Python, file: tests/check_frontend.py
"""前端静态检查：主题变量成对、设置项有控件、无残留域名/文案。

用法：python tests/check_frontend.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HTML = (ROOT / "frontend/index.html").read_text(encoding="utf-8")
CSS = (ROOT / "frontend/style.css").read_text(encoding="utf-8")
JS = (ROOT / "frontend/app.js").read_text(encoding="utf-8")
BACKEND_SETTINGS = (ROOT / "backend/db.py").read_text(encoding="utf-8")

problems: list[str] = []


def css_block(selector: str) -> str:
    m = re.search(re.escape(selector) + r"\s*\{(.*?)\}", CSS, re.S)
    return m.group(1) if m else ""


root_vars = set(re.findall(r"(--[a-z0-9-]+)\s*:", css_block(":root")))
light_vars = set(re.findall(r"(--[a-z0-9-]+)\s*:", css_block('[data-theme="light"]')))
used_vars = set(re.findall(r"var\((--[a-z0-9-]+)\)", CSS))

SHARED_VARS = {"--mono"}  # 字体栈等与主题无关，只在 :root 定义
missing_light = sorted(used_vars - light_vars - SHARED_VARS)
if missing_light:
    problems.append(f"亮色主题缺这些变量: {missing_light}")
undefined = sorted(used_vars - root_vars - light_vars)
if undefined:
    problems.append(f"用了未定义的变量: {undefined}")

# 设置项都有对应控件
settings_keys = set(re.findall(r'^    "([a-z_]+)":', BACKEND_SETTINGS, re.M))
form_names = set(re.findall(r'name="([^"]+)"', HTML))
no_control = sorted(k for k in settings_keys if k not in form_names and k != "default_api_key_enc")
if no_control:
    problems.append(f"设置项没有表单控件: {no_control}")

# app.js 引用的 id 必须在 html 里（动态创建的除外）
ids = set(re.findall(r'id="([^"]+)"', HTML))
dynamic = {"adminBanner", "tab"}
missing_ids = sorted(set(re.findall(r'\$\("#([A-Za-z0-9_-]+)"\)', JS)) - ids - dynamic)
if missing_ids:
    problems.append(f"app.js 引用了不存在的 id: {missing_ids}")

# 主题切换按钮
if 'id="themeBtn"' not in HTML or "applyTheme" not in JS:
    problems.append("缺少主题切换")

# 不要出现具体第三方域名，避免被照着脚本化滥用
banned = ["lovable.app", "launchpad.lovable", "proxy-key-launchpad"]
hits = [b for b in banned if b in HTML or b in CSS or b in JS
        or b in (ROOT / "README.md").read_text(encoding="utf-8")]
if hits:
    problems.append(f"文案里仍有具体域名: {hits}")

if problems:
    print(f"前端检查: {len(problems)} 处问题")
    for p in problems:
        print("  -", p)
    sys.exit(1)

print("前端检查: ok（主题变量成对、设置项有控件、无残留域名）")
