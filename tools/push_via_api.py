# language: Python, file: tools/push_via_api.py
"""沙箱里 git 连不上 github 时的兜底推送：走 GitHub REST API 造 tree/commit 再移动 ref。

用法：
    python tools/push_via_api.py            # 预览要推什么
    python tools/push_via_api.py --apply    # 真的推

前提：gh 已登录（gh auth status 正常），本地工作区干净、HEAD 有未推送的提交。
原理：用本地 HEAD 的 tree 和改动文件内容建远端 tree → 建 commit（父 = 远端 head）→
PATCH refs/heads/<branch>。远端 commit sha 会与本地不同（GitHub 会把 message 结尾换行
归一化），内容完全一致；本机网络正常时用 `git fetch && git reset --hard origin/<branch>`
对齐即可。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def sh(*args: str) -> str:
    out = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    if out.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} 失败: {out.stderr.strip()}")
    return out.stdout.strip()


def gh(*args: str) -> str:
    out = subprocess.run(["gh", *args], capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    if out.returncode != 0:
        raise SystemExit(f"gh {' '.join(args)} 失败: {out.stderr.strip()}")
    return out.stdout.strip()


def api_json(path: str, method: str, payload: dict) -> dict:
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False)
        tmp = fh.name
    try:
        return json.loads(gh("api", path, "-X", method, "--input", tmp))
    finally:
        Path(tmp).unlink(missing_ok=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真的推送，缺省只预览")
    ap.add_argument("--branch", default=None)
    ap.add_argument("--repo", default=None, help="owner/repo，缺省取 origin")
    args = ap.parse_args()

    branch = args.branch or sh("rev-parse", "--abbrev-ref", "HEAD")
    repo = args.repo
    if not repo:
        url = sh("remote", "get-url", "origin")
        repo = url.split("github.com")[-1].strip(":/").removesuffix(".git")

    head = sh("rev-parse", "HEAD")
    remote_head = gh("api", f"repos/{repo}/git/ref/heads/{branch}", "--jq", ".object.sha")
    if head == remote_head:
        print("远端已是最新，无需推送")
        return 0

    remote_tree = gh("api", f"repos/{repo}/git/commits/{remote_head}", "--jq", ".tree.sha")
    # 本地 origin/<branch> 可能是旧的（这里推不上去就没 fetch 过），所以不按 diff 取文件，
    # 直接把 HEAD 里所有文件整体铺一遍：结果 tree 一定等于本地 HEAD 的 tree。
    # 已知局限：本地删掉的文件不会从远端移除（本仓库没有这种情况）。
    files = [p for p in sh("ls-tree", "-r", "--name-only", head).split("\n") if p]
    print(f"repo={repo} branch={branch} remote={remote_head[:8]} -> local={head[:8]}，全量同步 {len(files)} 个文件")

    tree = json.loads(api_json(f"repos/{repo}/git/trees", "POST", {
        "base_tree": remote_tree,
        "tree": [
            {"path": rel, "mode": "100644", "type": "blob",
             "content": (ROOT / rel).read_text(encoding="utf-8").replace("\r\n", "\n")}
            for rel in files
        ],
    }))["sha"]

    local_tree = sh("log", "-1", "--format=%T", head)
    print("tree", tree, "本地 tree", local_tree, "一致" if tree == local_tree else "不一致（有非文本改动？）")

    if not args.apply:
        print("预览模式，加 --apply 才会真正创建 commit 并移动 ref")
        return 0

    commit = api_json(f"repos/{repo}/git/commits", "POST", {
        "message": sh("log", "-1", "--format=%B", head),
        "tree": tree,
        "parents": [remote_head],
        "author": {"name": sh("log", "-1", "--format=%an", head),
                   "email": sh("log", "-1", "--format=%ae", head),
                   "date": sh("log", "-1", "--format=%aI", head)},
    })["sha"]
    gh("api", f"repos/{repo}/git/refs/heads/{branch}", "-X", "PATCH",
       "-f", f"sha={commit}", "-F", "force=false")
    print(f"已推送 commit {commit}")
    print(f"本机网络恢复后执行：git fetch origin && git reset --hard origin/{branch}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
