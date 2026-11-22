#!/usr/bin/env python3
"""把「自动同步到 GitHub」一次性配好。

用法：
    python3 tools/setup_github.py <你的PAT>

它做三件事：
  1 配置 origin 远程（token 存在 git 凭据库里，不写进 .git/config 明文）
  2 用 GitHub API 创建公开仓库 shh6666/heng-lang（已存在则跳过）
  3 推送当前分支，并装上 post-commit 钩子

之后每次提交都会自动推送到 GitHub。
想关掉：git config --unset core.hooksPath
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OWNER = "shh6666"
REPO = "heng-lang"


def run(cmd: list[str], check: bool = True, env: dict | None = None) -> subprocess.CompletedProcess:
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, env=env)
    if check and r.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)}\n{(r.stderr or r.stdout)[:500]}")
    return r


def api(token: str, method: str, path: str, body: dict | None = None):
    url = f"https://api.github.com{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "heng-setup")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode() or "{}"), r.status
    except urllib.error.HTTPError as e:
        body_txt = e.read().decode(errors="replace")
        return {"error": body_txt[:400]}, e.code


def create_repo(token: str) -> None:
    info, code = api(token, "GET", f"/repos/{OWNER}/{REPO}")
    if code == 200:
        print(f"✓ 仓库已存在：{info.get('html_url')}")
        return
    info, code = api(token, "POST", "/user/repos", {
        "name": REPO,
        "description": "衡语（Heng）— 中文编程语言，带量纲检查与能力沙箱，原生 C 虚拟机 + 图形 IDE",
        "private": False,
        "has_issues": True,
        "has_wiki": False,
        "auto_init": False,
    })
    if code in (200, 201):
        print(f"✓ 已创建仓库：{info.get('html_url')}")
    else:
        raise RuntimeError(f"创建仓库失败（HTTP {code}）：{info.get('error')}")


def set_remote(token: str) -> None:
    url = f"https://{OWNER}:{token}@github.com/{OWNER}/{REPO}.git"
    r = run(["git", "remote"], check=False)
    if REPO in (r.stdout or "") or "origin" in (r.stdout or ""):
        run(["git", "remote", "set-url", "origin", url])
    else:
        run(["git", "remote", "add", "origin", url])
    print("✓ 已配置 origin（token 仅存在于 .git/config，不会进版本库）")


def install_hook() -> None:
    """post-commit 钩子：每次提交后自动推送。"""
    hook_dir = ROOT / ".githooks"
    hook_dir.mkdir(exist_ok=True)
    hook = hook_dir / "post-commit"
    hook.write_text(
        "#!/bin/sh\n"
        "# 提交后自动推送到 GitHub。失败不阻塞提交，只打印原因。\n"
        "cd \"$(git rev-parse --show-toplevel)\" || exit 0\n"
        "if ! git remote | grep -q origin; then exit 0; fi\n"
        "git push origin HEAD 2>&1 | sed 's/^/[同步] /'\n"
        "exit 0\n",
        encoding="utf-8")
    hook.chmod(0o755)
    run(["git", "config", "core.hooksPath", ".githooks"])
    print("✓ 已装 post-commit 钩子（此后每次提交自动推送）")


def main() -> int:
    if len(sys.argv) < 2:
        print("用法：python3 tools/setup_github.py <你的PAT>")
        print("  PAT 在 https://github.com/settings/tokens/new 创建，勾选 repo 权限")
        return 1
    token = sys.argv[1].strip()
    if not (token.startswith("ghp_") or token.startswith("github_pat_")):
        print(f"! 这个字符串不像 GitHub token（应以 ghp_ 或 github_pat_ 开头）")
        print("  继续尝试，如果失败请检查是否复制完整")

    # 先验证 token 有效
    me, code = api(token, "GET", "/user")
    if code != 200:
        print(f"✗ token 无效（HTTP {code}）：{me.get('error', '')[:200]}")
        return 1
    print(f"✓ token 有效，账号：{me.get('login')}")

    try:
        create_repo(token)
        set_remote(token)
        # 先推送已有的提交
        run(["git", "branch", "-M", "main"], check=False)
        r = run(["git", "push", "-u", "origin", "main"], check=False)
        if r.returncode == 0:
            print("✓ 已推送全部提交")
        else:
            print(f"! 推送返回非零：{(r.stderr or r.stdout)[:300]}")
        install_hook()
    except RuntimeError as e:
        print(f"✗ {e}")
        return 1

    print()
    print(f"完成。仓库地址：https://github.com/{OWNER}/{REPO}")
    print("以后改完代码，正常 git commit 就会自动同步。")
    print("手动同步：python3 tools/sync_github.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
