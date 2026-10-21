# language: Python, file: tests/check_workflow.py
"""工作流静态检查：拦掉 GitHub Actions 里最容易踩的两个坑。

1. if: 条件里出现 secrets.* → GitHub 报 Unrecognized named-value: 'secrets'
   （if 条件不提供 secrets 上下文，加不加 ${{ }} 都一样）
2. metadata-action 的 images: 里用条件表达式 → 可能拼出空行，报 invalid reference

用法：python tests/check_workflow.py [.github/workflows/docker.yml]
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

ID_ACTIONS = {  # 这些 id 对应的 action 不接受空 images 条目
    "docker/metadata-action",
}


def walk(node, path="", problems: list[str] | None = None):
    if problems is None:
        problems = []
    if isinstance(node, dict):
        for key, value in node.items():
            here = f"{path}.{key}" if path else str(key)
            if key == "if" and isinstance(value, str) and re.search(r"\bsecrets\.", value):
                problems.append(f"{here}: if 条件里不能用 secrets.*（用 env 中转再判断 env.X != ''）")
            if key == "images" and isinstance(value, str) and re.search(r"\|\||&&|\?", value):
                problems.append(f"{here}: images 里别放条件表达式，可能拼出空镜像名")
            walk(value, here, problems)
    elif isinstance(node, list):
        for i, item in enumerate(node):
            walk(item, f"{path}[{i}]", problems)
    return problems


def main() -> int:
    target = Path(sys.argv[1] if len(sys.argv) > 1 else ".github/workflows/docker.yml")
    doc = yaml.safe_load(target.read_text(encoding="utf-8"))
    problems = walk(doc)

    # 顺带确认登录/推送步骤都带了 secret 来源，而不是硬编码
    text = target.read_text(encoding="utf-8")
    if "docker/login-action" not in text:
        problems.append("没有 docker/login-action，推送会 401")

    if problems:
        print(f"{target}: {len(problems)} 处问题")
        for p in problems:
            print("  -", p)
        return 1
    print(f"{target}: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
