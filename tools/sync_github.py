#!/usr/bin/env python3
"""衡语项目自动同步到 GitHub。

用法：
    python3 tools/sync_github.py                 # 有改动就提交并推送
    python3 tools/sync_github.py --message "..."  # 自定义提交信息
    python3 tools/sync_github.py --dry-run        # 只看会提交什么

设计要点：
  1 提交信息用「按改动类型生成」，不用「auto commit」这种废话
  2 推送前先拉取，避免非快进冲突
  3 失败原因必须说清楚（凭据失效 / 网络 / 冲突），不静默吞掉
  4 不提交构建产物（.gitignore 已排除）
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def run(cmd: list[str], check: bool = True, capture: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(cmd, cwd=ROOT, capture_output=capture, text=True)
    if check and r.returncode != 0:
        msg = (r.stderr or r.stdout or "").strip()
        raise RuntimeError(f"命令失败：{' '.join(cmd)}\n{msg[:600]}")
    return r


def has_remote() -> bool:
    r = run(["git", "remote"], check=False)
    return "origin" in (r.stdout or "")


def changed_files() -> list[str]:
    r = run(["git", "status", "--porcelain"], check=False)
    out = []
    for line in (r.stdout or "").splitlines():
        if len(line) > 3:
            out.append(line[3:].strip())
    return out


def classify(files: list[str]) -> str:
    """根据改动文件推断提交类型与范围。"""
    kinds = set()
    scopes = set()
    for f in files:
        p = f.lower()
        if p.startswith("heng2/"):
            kinds.add("编译器"); scopes.add("compiler")
        elif p.startswith("native/"):
            kinds.add("虚拟机"); scopes.add("vm")
        elif p.startswith("heng-ide/"):
            kinds.add("IDE"); scopes.add("ide")
        elif p.startswith("heng-os/"):
            kinds.add("OS 原型"); scopes.add("os")
        elif p.startswith("heng-tools/"):
            kinds.add("工具"); scopes.add("tools")
        elif p.startswith("docs/"):
            kinds.add("文档"); scopes.add("docs")
        elif p.startswith("tests/") or p.startswith("validation/"):
            kinds.add("测试"); scopes.add("test")
        elif p.startswith("skill/"):
            kinds.add("Skill"); scopes.add("skill")
        elif p in ("readme.md", "license"):
            kinds.add("文档"); scopes.add("docs")
        else:
            kinds.add("杂项")
    if not kinds:
        kinds = {"更新"}
    return "、".join(sorted(kinds)), ",".join(sorted(scopes)) or "misc"


def build_message(files: list[str], custom: str | None) -> str:
    if custom:
        return custom
    kinds, scopes = classify(files)
    n = len(files)
    head = f"{kinds}更新（{n} 个文件）"
    body_lines = [f"范围: {scopes}", "", "改动文件（最多列 20 个）:"]
    for f in files[:20]:
        body_lines.append(f"- {f}")
    if n > 20:
        body_lines.append(f"- …另有 {n - 20} 个")
    return head + "\n\n" + "\n".join(body_lines)


def ensure_identity() -> None:
    """没有配置 git 身份时补一个，否则 commit 会失败。"""
    for key, val in (("user.name", "shh6666"),
                     ("user.email", "shh6666@users.noreply.github.com")):
        r = run(["git", "config", key], check=False)
        if not (r.stdout or "").strip():
            run(["git", "config", key, val])


def main() -> int:
    ap = argparse.ArgumentParser(description="同步衡语项目到 GitHub")
    ap.add_argument("--message", "-m", help="自定义提交信息")
    ap.add_argument("--dry-run", action="store_true", help="只看会提交什么")
    args = ap.parse_args()

    ensure_identity()

    if not has_remote():
        print("✗ 没有配置远程仓库 origin")
        print("  先运行：git remote add origin git@github.com:shh6666/heng-lang.git")
        return 1

    files = changed_files()
    if not files:
        print("✓ 没有需要同步的改动")
        return 0

    print(f"发现 {len(files)} 个改动：")
    for f in files[:15]:
        print(f"  {f}")
    if len(files) > 15:
        print(f"  …另有 {len(files) - 15} 个")

    msg = build_message(files, args.message)
    print(f"\n提交信息：{msg.splitlines()[0]}")

    if args.dry_run:
        print("\n（dry-run，不实际提交）")
        return 0

    run(["git", "add", "-A"])
    r = run(["git", "commit", "-m", msg], check=False)
    if r.returncode != 0:
        print(f"✗ 提交失败：{(r.stdout or r.stderr)[:400]}")
        return 1
    print("✓ 已提交")

    # 先拉后推，避免非快进
    r = run(["git", "pull", "--rebase", "--autostash", "origin", "main"], check=False)
    if r.returncode != 0:
        stderr = (r.stderr or r.stdout or "")
        if "couldn't find remote ref" not in stderr and "no tracking" not in stderr:
            print(f"! 拉取有警告（继续推送）：{stderr.strip()[:200]}")

    r = run(["git", "push", "-u", "origin", "main"], check=False)
    if r.returncode == 0:
        print("✓ 已推送到 GitHub")
        return 0

    err = (r.stderr or r.stdout or "").strip()
    print("✗ 推送失败")
    if "Authentication failed" in err or "could not read Username" in err:
        print("  原因：凭据失效")
        print("  修复：git remote set-url origin https://<PAT>@github.com/shh6666/heng-lang.git")
    elif "rejected" in err:
        print("  原因：远端有你不存在的提交")
        print("  修复：git pull --rebase origin main 后重试")
    else:
        print(f"  {err[:400]}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
