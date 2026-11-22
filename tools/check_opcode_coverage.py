#!/usr/bin/env python3
"""指令覆盖自检。

问题背景：100 个项目验证时发现「VM 早就实现了 INDEX_SET，
但解析器不支持下标语法」——指令与语法两侧不同步，静态检查发现不了，
只能等真实程序撞上去才暴露。

本脚本做三件事：
  1. 从 native/hengvm.c 抽出 VM 实现的指令集合；
  2. 扫 heng2/compiler2.py，抽出编译器实际会生成的指令集合；
  3. 报告「VM 实现了但编译器从不生成」的孤儿指令——它们意味着
     某个语言特性有后端却没前端，属于必须排查的不同步。

用法：
  python3 tools/check_opcode_coverage.py          # 报告
  python3 tools/check_opcode_coverage.py --strict # 有孤儿时退出码 1
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BYTECODE = ROOT / "heng2" / "bytecode.py"
VM = ROOT / "native" / "hengvm.c"
COMPILER = ROOT / "heng2" / "compiler2.py"

# 这些指令由编译器之外的途径使用，不要求 compiler2.py 直接生成
ALLOWED_ORPHANS = {
    "HALT",        # 入口末尾由 CodeBuilder 收尾（在 bytecode.py 里）
    "NOP",         # 调试/占位
    "YIELD",       # 协作式调度的预留点，当前由调度器内部使用
    "CALL_HOST",   # 旧宿主调用，已被 CALL_FFI 取代，保留兼容
    # 以下三条是「表达式求值优化」的预留指令：VM 已实现，
    # 等编译器接入超指令（superinstruction）融合后再生成。
    # 保留它们是为了让 VM 的指令空间先稳定下来。
    "DUP",
    "SWAP",
    "SELECT",
}


def op_names_from_bytecode() -> list[str]:
    text = BYTECODE.read_text(encoding="utf-8")
    block = re.search(r"class Op\(.*?\n(?=\nclass |\ndef )", text, re.S)
    if not block:
        raise SystemExit("无法在 bytecode.py 中定位 Op 枚举")
    return re.findall(r"^\s{4}([A-Z_0-9]+)\s*=\s*0x", block.group(0), re.M)


def ops_implemented_in_vm() -> set[str]:
    text = VM.read_text(encoding="utf-8")
    return set(re.findall(r"case\s+OP_([A-Z_0-9]+)\s*:", text))


def ops_emitted_by_compiler() -> set[str]:
    found: set[str] = set()
    for path in list(ROOT.glob("heng2/*.py")):
        text = path.read_text(encoding="utf-8")
        found |= set(re.findall(r"\bOp\.([A-Z_0-9]+)\b", text))
    return found


def main() -> int:
    strict = "--strict" in sys.argv
    declared = op_names_from_bytecode()
    in_vm = ops_implemented_in_vm()
    emitted = ops_emitted_by_compiler()

    declared_set = set(declared)
    # Op 里成组声明（如 case OP_RET: case OP_RET_NIL）也要算上
    missing_in_vm = sorted(declared_set - in_vm - {"EOF", "MAX"})
    orphans = sorted(in_vm - emitted - ALLOWED_ORPHANS)
    unknown = sorted(in_vm - declared_set)

    print(f"字节码指令声明：{len(declared)} 条")
    print(f"VM 已实现      ：{len(in_vm)} 条")
    print(f"编译器会生成   ：{len(emitted)} 条")
    print()

    if unknown:
        print("⚠ VM 中出现了未声明的指令（拼写或遗漏）:")
        for n in unknown:
            print(f"    {n}")
        print()

    if missing_in_vm:
        print(f"⚠ 已声明但 VM 未实现（{len(missing_in_vm)} 条）——运行到会报「尚未实现」:")
        for n in missing_in_vm:
            print(f"    {n}")
        print()

    if orphans:
        print(f"⚠ 孤儿指令（VM 已实现，但编译器从不生成）（{len(orphans)} 条）:")
        print("   这意味着某个语言特性有后端却没有前端——下标访问就是这样漏掉的。")
        for n in orphans:
            print(f"    {n}")
        print()

    # 指令编号冲突检查：新增指令时最容易犯的错（0x40 段曾被 EQ/NE 占用）
    import collections
    text_bc = BYTECODE.read_text(encoding="utf-8")
    block = re.search(r"class Op\(.*?\n(?=\nclass |\ndef )", text_bc, re.S)
    pairs = re.findall(r"^\s{4}([A-Z_0-9]+)\s*=\s*(0x[0-9A-Fa-f]+)", block.group(0), re.M)
    byval = collections.defaultdict(list)
    for nm, v in pairs:
        byval[int(v, 16)].append(nm)
    dups = {v: ns for v, ns in byval.items() if len(ns) > 1}
    if dups:
        print(f"⚠ 指令编号冲突（{len(dups)} 处）——会导致解码到错误指令:")
        for v, ns in sorted(dups.items()):
            print(f"    {hex(v)}: {' / '.join(ns)}")
        print()

    ok = not (unknown or missing_in_vm or orphans or dups)
    if ok:
        print("✓ 指令覆盖一致：所有声明、实现、生成三方对齐")
    return 0 if (ok or not strict) else 1


if __name__ == "__main__":
    sys.exit(main())
