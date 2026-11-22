"""衡语内核 v2 —— 反汇编器：把 .hbc 还原成可读指令流。

用途：
  1. 调试代码生成；
  2. 作为「显式 IR」展示给用户（比旧实现的字符串 trace 更真实）；
  3. 验证 .hbc 自足性（只看字节码就能理解程序）。
"""

from __future__ import annotations

import struct

from .bytecode import C, Op, Module, OPERANDS


def disasm(module: Module, entry_label: str = "<入口>") -> str:
    lines: list[str] = []
    lines.append(f"; 常量池（{len(module.pool)} 项）")
    for i, (kind, v) in enumerate(module.pool):
        lines.append(f"  [{i:3d}] {kind.name:5s} {v!r}")
    lines.append("")

    for idx, fn in enumerate(module.funcs):
        lines.append(f"做法 #{idx} {fn.name}(参数 {fn.n_params}, 槽位 {fn.n_slots}) "
                     f"-> {fn.ret_kind}{fn.ret_unit}")
        lines.extend(_dis_block(fn.code, module))
        lines.append(f"  结束 做法 {fn.name}")
        lines.append("")

    if module.entry:
        lines.append(f"{entry_label}（槽位 {module.entry_slots}）")
        lines.extend(_dis_block(module.entry, module))
        lines.append("  结束 入口")
    return "\n".join(lines)


def _dis_block(code: bytes, module: Module) -> list[str]:
    out: list[str] = []
    pc = 0
    n = len(code)
    while pc < n:
        start = pc
        opv = code[pc]
        pc += 1
        try:
            op = Op(opv)
        except ValueError:
            out.append(f"  {start:04d}  <未知指令 0x{opv:02X}>")
            break
        fmt = OPERANDS.get(op, "")
        args = []
        for ch in fmt:
            if ch == "a":
                args.append(code[pc]); pc += 1
            elif ch in ("b", "h"):
                args.append(struct.unpack_from("<H", code, pc)[0]); pc += 2
            elif ch == "c":
                idx = struct.unpack_from("<I", code, pc)[0]; pc += 4
                if op in (Op.PUSH_CONST, Op.FIELD_GET, Op.THROW, Op.CAP_CHECK,
                          Op.CALL_STD, Op.CALL_HOST, Op.CALL_TOOL, Op.PRINT):
                    val = module.pool[idx][1] if idx < len(module.pool) else "?"
                    args.append(f"#{idx}({val!r})")
                else:
                    args.append(idx)
            elif ch == "d":
                args.append(struct.unpack_from("<i", code, pc)[0]); pc += 4
            elif ch == "f":
                args.append(struct.unpack_from("<d", code, pc)[0]); pc += 8
        text = " ".join(str(a) for a in args)
        out.append(f"  {start:04d}  {op.name:14s} {text}".rstrip())
    return out
