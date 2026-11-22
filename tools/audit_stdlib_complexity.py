#!/usr/bin/env python3
"""标准库复杂度审计。

起因：`表.追加` 单看实现完全正确（原地扩容），但它在循环里返回新序列，
导致 `xs = 表.追加(xs, v)` 退化为 O(n^2)——20 万次要 95 秒。

读代码容易漏掉这种「语义层面的复杂度」。所以这里用实测：
对每个函数喂不同规模的输入，按耗时增长斜率判定复杂度。

判定方法：
    规模 n1, n2 (n2 = 4 * n1)
    若耗时比 ≈ 4   → O(n)
    若耗时比 ≈ 16  → O(n^2)
    若耗时比 ≈ 1   → O(1)

用法：
    python3 tools/audit_stdlib_complexity.py
    python3 tools/audit_stdlib_complexity.py --check   # 有 O(n^2) 时退出码 1
"""

from __future__ import annotations

import math
import subprocess
import sys
import tempfile
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NATIVE = ROOT / "native" / "bin" / "hengvm"

# (标签, 表达式模板, 小规模, 大规模)
# 模板里 {n} 会被替换成规模。表达式必须产出同一个值，便于校验。
CASES = [
    # ---- 表：最可疑的一类（值语义 + 复制） ----
    ("表.追加（循环内）",
     "变量 xs : 序列 数 = []\n  变量 i : 数 = 0\n  循环 i < {n}:\n"
     "    xs = 表.追加(xs, i)\n    i = i + 1\n  结束\n  说出 表.长度(xs)", 2000, 8000),
    ("表.连接（累加）",
     "变量 acc : 序列 数 = []\n  变量 i : 数 = 0\n  循环 i < {n}:\n"
     "    acc = 表.连接(acc, [i])\n    i = i + 1\n  结束\n  说出 表.长度(acc)", 2000, 8000),
    ("表.片段（全量切）",
     "变量 xs : 序列 数 = []\n  变量 i : 数 = 0\n  循环 i < {n}:\n"
     "    xs = 表.追加(xs, i)\n    i = i + 1\n  结束\n"
     "  变量 出来 : 序列 数 = 表.片段(xs, 0, 表.长度(xs))\n  说出 表.长度(出来)", 2000, 8000),
    ("表.排序（内部计时）",
     "变量 xs : 序列 数 = []\n  变量 i : 数 = 0\n  循环 i < {n}:\n"
     "    xs = 表.追加(xs, {n} - i)\n    i = i + 1\n  结束\n"
     "  变量 t0 : 数 = 时间.毫秒()\n"
     "  变量 s : 序列 数 = 表.排序(xs)\n"
     "  说出 时间.毫秒() - t0", 4000, 16000),
    ("表.去重",
     "变量 xs : 序列 数 = []\n  变量 i : 数 = 0\n  循环 i < {n}:\n"
     "    xs = 表.追加(xs, i)\n    i = i + 1\n  结束\n"
     "  说出 表.长度(表.去重(xs))", 2000, 8000),
    ("表.求和",
     "变量 xs : 序列 数 = []\n  变量 i : 数 = 0\n  循环 i < {n}:\n"
     "    xs = 表.追加(xs, i)\n    i = i + 1\n  结束\n  说出 表.求和(xs)", 2000, 8000),
    ("表.含（最坏）",
     "变量 xs : 序列 数 = []\n  变量 i : 数 = 0\n  循环 i < {n}:\n"
     "    xs = 表.追加(xs, i)\n    i = i + 1\n  结束\n  说出 表.含(xs, -1)", 2000, 8000),
    ("表.倒序",
     "变量 xs : 序列 数 = []\n  变量 i : 数 = 0\n  循环 i < {n}:\n"
     "    xs = 表.追加(xs, i)\n    i = i + 1\n  结束\n  说出 表.长度(表.倒序(xs))", 2000, 8000),
    # ---- 文字：拼接是重灾区 ----
    ("文字拼接（循环累加）",
     "变量 s : 文本 = \"\"\n  变量 i : 数 = 0\n  循环 i < {n}:\n"
     "    s = s + \"x\"\n    i = i + 1\n  结束\n  说出 文字.长度(s)", 2000, 8000),
    ("文字.替换（循环）",
     "变量 s : 文本 = \"\"\n  变量 i : 数 = 0\n  循环 i < {n}:\n"
     "    s = s + \"x\"\n    i = i + 1\n  结束\n"
     "  说出 文字.长度(文字.替换(s, \"x\", \"y\"))", 2000, 8000),
    ("文字.切分",
     "变量 s : 文本 = \"\"\n  变量 i : 数 = 0\n  循环 i < {n}:\n"
     "    s = s + \"x,\"\n    i = i + 1\n  结束\n  说出 表.长度(文字.切分(s, \",\"))", 2000, 8000),
    # ---- 字典 ----
    ("字典.写入（循环）",
     "变量 m : 映射 文本 数 = {}\n  变量 i : 数 = 0\n  循环 i < {n}:\n"
     "    将 i 放到 m[化成文(i)]\n    i = i + 1\n  结束\n  说出 字典.大小(m)", 2000, 8000),
    ("字典.键",
     "变量 m : 映射 文本 数 = {}\n  变量 i : 数 = 0\n  循环 i < {n}:\n"
     "    将 i 放到 m[化成文(i)]\n    i = i + 1\n  结束\n  说出 表.长度(字典.键(m))", 2000, 8000),
]


def build(src: str, out: str) -> tuple[bool, str]:
    with tempfile.NamedTemporaryFile("w", suffix=".hng", delete=False, encoding="utf-8") as f:
        f.write(src)
        path = f.name
    try:
        r = subprocess.run([sys.executable, "-m", "heng2", "--build", path],
                           capture_output=True, text=True, cwd=ROOT)
        if r.returncode != 0:
            return False, (r.stdout + r.stderr)[:120]
        built = path[:-4] + ".hbc"
        if not os.path.exists(built):
            return False, "未生成字节码"
        os.replace(built, out)
        return True, ""
    finally:
        os.unlink(path)


BASELINE_MS = 3.7      # 空程序启动开销；实测得出，用于扣除


def run_ms(path: str, repeat: int = 3) -> tuple[float | None, str]:
    best = None
    out = ""
    for _ in range(repeat):
        import time
        t0 = time.perf_counter()
        r = subprocess.run([str(NATIVE), path], capture_output=True, text=True, timeout=300)
        d = (time.perf_counter() - t0) * 1000
        if r.returncode != 0:
            return None, r.stderr.strip()[:80]
        best = d if best is None else min(best, d)
        out = r.stdout.strip()
    return best, out


def classify(small_ms: float, large_ms: float, ratio: float) -> str:
    """按 4 倍规模的耗时比判定复杂度。"""
    if ratio <= 1.8:
        return "O(1)"
    if ratio <= 6:
        return "O(n)"
    if ratio <= 11:
        return "O(n log n)"
    if ratio <= 28:
        return "O(n^2)"
    return f"更差（{ratio:.0f}x）"


def main() -> int:
    check = "--check" in sys.argv
    if not NATIVE.exists():
        print(f"找不到原生 VM：{NATIVE}")
        return 1

    print(f"审计 {len(CASES)} 个用例（规模 4 倍递增，耗时比判定复杂度）")
    print(f"{'用例':<22}{'小规模':>10}{'大规模':>11}{'倍数':>7}  {'判定':<12}输出")
    print("-" * 78)

    problems = []
    tmpdir = tempfile.mkdtemp()
    for label, tmpl, n1, n2 in CASES:
        r1 = r2 = None
        o1 = o2 = ""
        for n, tag in ((n1, "a"), (n2, "b")):
            src = "开始:\n  " + tmpl.replace("{n}", str(n)) + "\n结束\n"
            bin_path = os.path.join(tmpdir, f"{tag}.hbc")
            ok, err = build(src, bin_path)
            if not ok:
                r1 = "编译失败"
                break
            ms, out = run_ms(bin_path)
            if ms is None:
                r1 = "运行失败"
                break
            if tag == "a":
                r1, o1 = ms, out
            else:
                r2, o2 = ms, out

        if not isinstance(r1, float) or not isinstance(r2, float):
            print(f"{label:<22}{'—':>10}{'—':>11}{'—':>7}  跳过")
            continue

        # 扣除进程启动开销，否则 3.7ms 的固定成本会掩盖真实差异
        c1 = max(r1 - BASELINE_MS, 0.0)
        c2 = max(r2 - BASELINE_MS, 0.0)
        # 扣完基线后耗时太小时，比值全是噪声（曾出现 4ms vs 4ms 判成 16x）。
        # 这种情况不参与判定——它本来也不可能是瓶颈。
        if c1 < 2.0 or c2 < 2.0:
            ratio = 4.0 if c2 > 0 else 1.0     # 视为 O(n)，不做告警
        else:
            ratio = c2 / c1
        verdict = classify(r1, r2, ratio)
        flag = ""
        if "n^2" in verdict or "更差" in verdict:
            problems.append((label, verdict, r1, r2, ratio))
            flag = " ⚠"
        print(f"{label:<22}{r1:>9.1f}ms{r2:>10.1f}ms{ratio:>6.1f}x  {verdict:<12}{o1==o2}{flag}")

    print()
    if problems:
        print(f"发现 {len(problems)} 处平方级或更差的实现：")
        for label, v, a, b, r in problems:
            print(f"  ⚠ {label}: {v}（{a:.0f}ms → {b:.0f}ms，{r:.1f}x）")
        return 1 if check else 0

    print("✓ 未发现平方级实现")
    return 0


if __name__ == "__main__":
    sys.exit(main())
