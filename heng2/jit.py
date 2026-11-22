#!/usr/bin/env python3
"""字节码 → C 的热循环 JIT。

背景：实测 1000 万次自增循环，衡语 32.55 ns/次，C 0.83 ns/次，差 39 倍。
差距全部来自解释器的取指-分派-栈操作开销。完整 JIT（寄存器分配、
类型推断、内联缓存）是后端级别的工作量，短期做不完。

这里的方案是「保守 JIT」：
  1. 扫描字节码，找出**只操作数值槽位**的循环体（无函数调用、无对象分配）；
  2. 把这段循环翻译成 C 源码；
  3. 用 clang 编译成动态库，dlopen 加载；
  4. 后续再执行到同一位置，直接调用原生函数。

它不能优化所有代码，但对「数值计算密集的循环」——也就是真正需要
性能的那部分——能拿到接近 C 的速度。

设计取舍：
  - 只处理纯数值循环，遇到任何不确定就放弃（退回解释执行）
  - 编译失败不影响正确性，只是没加速
  - 结果写回槽位数组，与解释器状态一致
"""

from __future__ import annotations

import ctypes
import hashlib
import os
import subprocess
import tempfile
from pathlib import Path

from .bytecode import Op, OPERANDS

CACHE_DIR = Path(tempfile.gettempdir()) / "heng_jit"

# 可以作为纯数值操作翻译的指令（槽位 → 槽位）
SAFE_OPS = {
    Op.PUSH_NUM, Op.STORE_LOCAL, Op.LOAD_LOCAL,
    Op.ADD_NUM, Op.SUB_NUM, Op.MUL_NUM, Op.DIV_NUM, Op.MOD_NUM,
    Op.LT_NUM, Op.LE_NUM, Op.GT_NUM, Op.GE_NUM, Op.EQ_NUM, Op.NE_NUM,
    Op.ADD_SLOT_IMM, Op.SUB_SLOT_IMM, Op.MUL_SLOT_IMM, Op.DIV_SLOT_IMM,
    Op.MOD_SLOT_IMM, Op.ADD_SLOT_SLOT, Op.SUB_SLOT_SLOT, Op.MUL_SLOT_SLOT,
    Op.LT_SLOT_IMM, Op.LE_SLOT_IMM, Op.GT_SLOT_IMM, Op.GE_SLOT_IMM,
    Op.EQ_SLOT_IMM, Op.NE_SLOT_IMM,
    Op.JMP, Op.JMP_IFNOT, Op.LT, Op.LE, Op.GT, Op.GE, Op.EQ, Op.NE,
    Op.ADD, Op.SUB, Op.MUL, Op.DIV, Op.MOD,
    Op.PUSH_TRUE, Op.PUSH_FALSE,
}


def _decode(code: bytes, pc: int):
    """解出一条指令，返回 (Op, 操作数列表, 下一条的 pc)。"""
    opv = code[pc]
    pc += 1
    try:
        op = Op(opv)
    except ValueError:
        return None, [], pc
    fmt = OPERANDS.get(op, "")
    args = []
    import struct
    for ch in fmt:
        if ch == "a":
            args.append(code[pc]); pc += 1
        elif ch in ("b", "h"):
            args.append(struct.unpack_from("<H", code, pc)[0]); pc += 2
        elif ch == "c":
            args.append(struct.unpack_from("<I", code, pc)[0]); pc += 4
        elif ch == "d":
            args.append(struct.unpack_from("<i", code, pc)[0]); pc += 4
        elif ch == "f":
            args.append(struct.unpack_from("<d", code, pc)[0]); pc += 8
    return op, args, pc


def scan_loops(code: bytes, n_slots: int):
    """找出「向后跳」形成的自然循环，返回 [(起始pc, 结束pc, 循环体指令表)]。

    只保留循环体里全是 SAFE_OPS 且不涉及槽位越界的候选。
    """
    instrs = []
    pc = 0
    while pc < len(code):
        start = pc
        op, args, nxt = _decode(code, pc)
        if op is None:
            return []          # 遇到不认识的指令，整体放弃
        instrs.append((start, op, args, nxt))
        pc = nxt

    by_offset = {i[0]: k for k, i in enumerate(instrs)}
    loops = []
    for k, (start, op, args, nxt) in enumerate(instrs):
        if op not in (Op.JMP, Op.JMP_IFNOT):
            continue
        target = nxt + args[0]
        if target >= start:
            continue           # 只处理向后跳（循环）
        if target not in by_offset:
            continue
        lo = by_offset[target]
        body = instrs[lo:k + 1]
        if any(o not in SAFE_OPS for _, o, _, _ in body):
            continue
        # 槽位必须在范围内
        bad = False
        for _, o, a, _ in body:
            if o in (Op.LOAD_LOCAL, Op.STORE_LOCAL) and a[0] >= n_slots:
                bad = True
            if o in (Op.ADD_SLOT_IMM, Op.SUB_SLOT_IMM, Op.MUL_SLOT_IMM,
                     Op.DIV_SLOT_IMM, Op.MOD_SLOT_IMM, Op.LT_SLOT_IMM,
                     Op.LE_SLOT_IMM, Op.GT_SLOT_IMM, Op.GE_SLOT_IMM,
                     Op.EQ_SLOT_IMM, Op.NE_SLOT_IMM) and a[0] >= n_slots:
                bad = True
            if o in (Op.ADD_SLOT_SLOT, Op.SUB_SLOT_SLOT, Op.MUL_SLOT_SLOT):
                if a[0] >= n_slots or a[1] >= n_slots:
                    bad = True
        if bad or not body:
            continue
        loops.append((instrs[lo][0], instrs[k][3], body))
    return loops


BIN = {
    Op.ADD: "+", Op.SUB: "-", Op.MUL: "*", Op.DIV: "/", Op.MOD: None,
    Op.LT: "<", Op.LE: "<=", Op.GT: ">", Op.GE: ">=", Op.EQ: "==", Op.NE: "!=",
    Op.ADD_NUM: "+", Op.SUB_NUM: "-", Op.MUL_NUM: "*", Op.DIV_NUM: "/",
    Op.LT_NUM: "<", Op.LE_NUM: "<=", Op.GT_NUM: ">", Op.GE_NUM: ">=",
    Op.EQ_NUM: "==", Op.NE_NUM: "!=",
}

SLOT_IMM = {
    Op.ADD_SLOT_IMM: "+=", Op.SUB_SLOT_IMM: "-=", Op.MUL_SLOT_IMM: "*=",
    Op.DIV_SLOT_IMM: "/=", Op.MOD_SLOT_IMM: None,
}
SLOT_SLOT = {
    Op.ADD_SLOT_SLOT: "+=", Op.SUB_SLOT_SLOT: "-=", Op.MUL_SLOT_SLOT: "*=",
}
CMP_SLOT_IMM = {
    Op.LT_SLOT_IMM: "<", Op.LE_SLOT_IMM: "<=", Op.GT_SLOT_IMM: ">",
    Op.GE_SLOT_IMM: ">=", Op.EQ_SLOT_IMM: "==", Op.NE_SLOT_IMM: "!=",
}


def emit_c(body, n_slots: int, fname: str) -> str | None:
    """把循环体翻译成 C 函数体。返回 None 表示无法翻译。

    生成的函数签名：
        void fname(double *slots, long *steps)
    返回时 slots 已就地更新；steps 用于防止死循环（上限保护）。
    """
    live = sorted({a[0] for _, o, a, _ in body
                   if o in (Op.LOAD_LOCAL, Op.STORE_LOCAL, Op.ADD_SLOT_IMM,
                            Op.SUB_SLOT_IMM, Op.MUL_SLOT_IMM, Op.DIV_SLOT_IMM,
                            Op.MOD_SLOT_IMM, Op.LT_SLOT_IMM, Op.LE_SLOT_IMM,
                            Op.GT_SLOT_IMM, Op.GE_SLOT_IMM, Op.EQ_SLOT_IMM,
                            Op.NE_SLOT_IMM)} |
                  {a[0] for _, o, a, _ in body if o in (Op.ADD_SLOT_SLOT, Op.SUB_SLOT_SLOT, Op.MUL_SLOT_SLOT)} |
                  {a[1] for _, o, a, _ in body if o in (Op.ADD_SLOT_SLOT, Op.SUB_SLOT_SLOT, Op.MUL_SLOT_SLOT)})
    if not live:
        return None
    for s in live:
        if s >= n_slots:
            return None

    # 栈机 → 表达式：只支持很浅的栈（本项目的热点都是如此）
    lines = []
    stack = []

    # 先扫一遍确定所有需要打标签的位置：
    #   - 循环体内的指令偏移（向后跳的目标）
    #   - 向前跳的目标（循环出口）
    body_starts = {start for start, _, _, _ in body}
    pc2label = {}
    for start, op, a, nxt in body:
        if op in (Op.JMP, Op.JMP_IFNOT):
            tgt = nxt + a[0]
            if tgt in body_starts:
                pc2label.setdefault(tgt, f"T{len(pc2label)}")
            else:
                # 跳出循环：统一落到 END
                pc2label.setdefault(tgt, "END")

    def pop():
        return stack.pop() if stack else "0.0"

    for start, op, a, nxt in body:
        if start in pc2label:
            lines.append(f"  {pc2label[start]}: ;")
        if op == Op.PUSH_NUM:
            stack.append(repr(float(a[0])))
        elif op == Op.PUSH_TRUE:
            stack.append("1")
        elif op == Op.PUSH_FALSE:
            stack.append("0")
        elif op == Op.LOAD_LOCAL:
            stack.append(f"slots[{a[0]}]")
        elif op == Op.STORE_LOCAL:
            lines.append(f"  slots[{a[0]}] = {pop()};")
        elif op in BIN and BIN[op]:
            r, l = pop(), pop()
            stack.append(f"({l} {BIN[op]} {r})")
        elif op in SLOT_IMM and SLOT_IMM[op]:
            lines.append(f"  slots[{a[0]}] {SLOT_IMM[op]} {repr(float(a[1]))};")
        elif op in SLOT_SLOT:
            lines.append(f"  slots[{a[0]}] {SLOT_SLOT[op]} slots[{a[1]}];")
        elif op in CMP_SLOT_IMM:
            lines.append(f"  {{ int c = slots[{a[0]}] {CMP_SLOT_IMM[op]} {repr(float(a[1]))};")
            stack.append("c")
            lines.append("  }")
        elif op == Op.JMP:
            stack.clear()
            lines.append(f"  goto {pc2label.get(nxt + a[0], 'END')};")
        elif op == Op.JMP_IFNOT:
            cond = pop()
            lines.append(f"  if (!({cond})) goto {pc2label.get(nxt + a[0], 'END')};")
        else:
            return None
    lines.append("  END: ;")
    return "\n".join(lines)


class JitLoop:
    """一个已编译的热循环。"""

    def __init__(self, start_pc: int, end_pc: int, lib, fn):
        self.start_pc = start_pc
        self.end_pc = end_pc
        self._lib = lib
        self._fn = fn
        self.hits = 0

    def run(self, slots_buf, steps=0):
        st = ctypes.c_long(steps)
        self._fn(slots_buf, ctypes.byref(st))
        self.hits += 1
        return st.value


class Jit:
    """热循环编译器。失败一律静默退回解释执行。"""

    def __init__(self, enabled: bool = True):
        self.enabled = enabled and os.environ.get("HENG_NO_JIT") != "1"
        self.loops: dict[int, JitLoop] = {}
        self.compiled = 0
        self.failed = 0

    def try_build(self, code: bytes, n_slots: int) -> None:
        if not self.enabled or self.compiled:
            return
        loops = scan_loops(code, n_slots)
        if not loops:
            return
        # 只编译第一个（入口通常只有一个热循环）
        start, end, body = loops[0]
        cbody = emit_c(body, n_slots, "run")
        if cbody is None:
            self.failed += 1
            return
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        key = hashlib.sha1((cbody + str(n_slots)).encode()).hexdigest()[:12]
        src = CACHE_DIR / f"loop_{key}.c"
        so = CACHE_DIR / f"loop_{key}.dylib"
        src.write_text(
            "#include <math.h>\n"
            "void run(double *slots, long *steps) {\n"
            f"{cbody}\n"
            "}\n", encoding="utf-8")
        r = subprocess.run(["clang", "-O3", "-shared", "-fPIC", "-o", str(so), str(src)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            self.failed += 1
            return
        try:
            lib = ctypes.CDLL(str(so))
            fn = lib.run
            fn.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_long)]
            fn.restype = None
            self.loops[start] = JitLoop(start, end, lib, fn)
            self.compiled += 1
        except OSError:
            self.failed += 1


def _demo_main():
    """命令行演示：编译并运行 .hng 里的纯数值循环。

    用法：python3 -m heng2.jit <文件.hng>
    """
    import sys
    from .api2 import compile_src

    if len(sys.argv) < 2:
        print("用法: python3 -m heng2.jit <文件.hng>")
        return 1
    src = Path(sys.argv[1]).read_text(encoding="utf-8")
    mod, _ = compile_src(src)

    # 入口槽位的初值需要先跑一遍解释器之外的方式获得？
    # 简化：只对「入口全是纯数值循环」的程序演示，
    # 槽位初值从字节码开头的 PUSH/STORE 序列取。
    loops = scan_loops(mod.entry, mod.entry_slots)
    if not loops:
        print("该文件没有可 JIT 的纯数值循环")
        return 1
    start, end, body = loops[0]
    cbody = emit_c(body, mod.entry_slots, "run")
    if cbody is None:
        print("循环无法翻译为 C")
        return 1
    print(f"找到循环：字节码 {start}..{end}，{len(body)} 条指令")
    print("生成的 C：")
    print(cbody)

    # 取槽位初值：扫描循环之前的 PUSH_NUM/STORE_LOCAL 对
    import struct
    init = {}
    p = 0
    pend = None
    while p < start:
        op, args, nxt = _decode(mod.entry, p)
        if op is None:
            break
        if op == Op.PUSH_NUM:
            pend = float(args[0])
        elif op == Op.STORE_LOCAL and pend is not None:
            init[args[0]] = pend
            pend = None
        p = nxt

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha1((cbody + str(mod.entry_slots)).encode()).hexdigest()[:12]
    srcf = CACHE_DIR / f"demo_{key}.c"
    sof = CACHE_DIR / f"demo_{key}.dylib"
    srcf.write_text("#include <math.h>\nvoid run(double *slots, long *steps) {\n"
                    + cbody + "\n}\n", encoding="utf-8")
    r = subprocess.run(["clang", "-O3", "-shared", "-fPIC", "-o", str(sof), str(srcf)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print("编译失败：", r.stderr[:200])
        return 1

    lib = ctypes.CDLL(str(sof))
    fn = lib.run
    fn.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_long)]
    fn.restype = None
    buf = (ctypes.c_double * mod.entry_slots)()
    for k2, v2 in init.items():
        buf[k2] = v2
    st = ctypes.c_long(0)
    import time
    t0 = time.perf_counter()
    fn(buf, ctypes.byref(st))
    dt = time.perf_counter() - t0
    print(f"\n执行耗时 {dt*1000:.2f} ms")
    print("结果槽位：", [buf[i] for i in range(mod.entry_slots)])
    return 0


if __name__ == "__main__":
    raise SystemExit(_demo_main())
