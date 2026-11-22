"""衡语内核 v2 —— 指令集与 .hbc 字节码格式。

.hbc 是衡语自有的二进制格式，不包含任何 Python 对象引用。
结构：

    magic     "HENG"                4 字节
    version   u8 = 2
    flags     u8
    pool      常量池：u32 个数 + 每项 (u8 类型 + 负载)
    funcs     函数表：u32 个数 + 每项 (名字池索引 u32, 参数数 u16,
                                    局部槽位 u16, 返回类型描述, 代码长 u32, 代码)
    records   记录表：u32 个数 + 每项 (名字池索引, 字段数 u16, 字段名索引…)
    entry     入口代码块：u32 长度 + 代码（无则为 0）
    globals   全局槽位数 u16
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any


# ---------------- 常量池项类型 ----------------
class C(IntEnum):
    NUM = 0        # f64
    TEXT = 1       # utf-8 字符串
    BOOL = 2       # u8
    UNIT = 3       # 单位显示串（用于输出格式化）
    NIL = 4


# ---------------- 指令码 ----------------
class Op(IntEnum):
    NOP = 0x00
    HALT = 0x01

    # 常量与栈
    PUSH_NUM = 0x10        # 立即数 f64
    PUSH_CONST = 0x11      # 常量池索引 u32
    PUSH_NIL = 0x12
    PUSH_TRUE = 0x13
    PUSH_FALSE = 0x14
    POP = 0x15
    DUP = 0x16
    SWAP = 0x17
    SELECT = 0x1B     # 三元选择（预留：供后续条件表达式优化使用）

    # 变量
    LOAD_LOCAL = 0x20      # u16 槽位
    STORE_LOCAL = 0x21     # u16 槽位
    LOAD_GLOBAL = 0x22     # u16 槽位
    STORE_GLOBAL = 0x23    # u16 槽位

    # 算术
    ADD = 0x30
    SUB = 0x31
    MUL = 0x32
    DIV = 0x33
    MOD = 0x34
    NEG = 0x35
    # 字符串拼接（操作数是文本）
    CONCAT = 0x36
    # 数值特化：操作数已确定为「数」，省掉运行时的类型判断
    ADD_NUM = 0xC0
    SUB_NUM = 0xC1
    MUL_NUM = 0xC2
    DIV_NUM = 0xC3
    MOD_NUM = 0xC4
    LT_NUM  = 0xC5
    LE_NUM  = 0xC6
    GT_NUM  = 0xC7
    GE_NUM  = 0xC8
    EQ_NUM  = 0xC9
    NE_NUM  = 0xCA
    # 超指令：把「取槽 → 运算 → 写回同槽」合成一条，省掉栈往返
    ADD_SLOT_IMM   = 0xD0   # u16 槽, f64 立即数：x = x + 常量
    ADD_SLOT_SLOT  = 0xD1   # u16 目标槽, u16 源槽：x = x + y
    LT_SLOT_IMM    = 0xD2   # u16 槽, f64 立即数：压入 x < 常量
    MAKE_FUNC      = 0xD3   # u16 函数索引：把函数作为值压栈
    CALL_VALUE     = 0xD4   # u16 槽, u8 实参数：调用槽里存的函数
    HIGHER_ORDER   = 0xD5   # u8 种类, u8 参数：映射/过滤/折叠
    # 索引访问超指令：省掉 LOAD_LOCAL + 下标计算 + INDEX_GET 的组合开销
    SLOT_INDEX_SLOT = 0xD6  # u16 容器槽, u16 下标槽：压入 c[i]
    SLOT_INDEX_IMM  = 0xD7  # u16 容器槽, f64 下标：压入 c[常量]
    SLOT_INDEX_STORE = 0xD8 # u16 容器槽, u16 下标槽, u16 值槽：c[i] = v
    # 算术与比较的槽位超指令（按 101 项目的指令分布排序）
    SUB_SLOT_IMM     = 0xD9   # x = x - 常量
    MUL_SLOT_IMM     = 0xDA   # x = x * 常量
    DIV_SLOT_IMM     = 0xDB   # x = x / 常量
    MOD_SLOT_IMM     = 0xDC   # x = x % 常量
    SUB_SLOT_SLOT    = 0xDD   # x = x - y
    MUL_SLOT_SLOT    = 0xDE   # x = x * y
    GT_SLOT_IMM      = 0xDF   # 压入 x > 常量
    LE_SLOT_IMM      = 0xE0   # 压入 x <= 常量
    GE_SLOT_IMM      = 0xE1   # 压入 x >= 常量
    EQ_SLOT_IMM      = 0xE2   # 压入 x == 常量
    NE_SLOT_IMM      = 0xE3   # 压入 x != 常量
    # 追加到自身：xs = 表.追加(xs, v) —— 真实程序里最高频的模式。
    # 原地追加，避免整表复制导致的 O(n^2)。
    SLOT_APPEND_SLOT = 0xE4   # u16 目标槽, u16 值槽
    SLOT_APPEND_IMM  = 0xE5   # u16 目标槽, f64 值
    # 连接到自身：acc = 表.连接(acc, ys) —— 累加拼接的高频形态
    SLOT_CONCAT_SLOT = 0xE6   # u16 目标槽, u16 源槽
    SLOT_APPEND_STACK = 0xE7  # u16 目标槽：把栈顶值追加进去（值为任意表达式）
    MAKE_CLOSURE     = 0xE8   # u16 函数索引, u8 捕获数：取栈上 N 个值构成闭包
    LOAD_FREE        = 0xE9   # u16 捕获序号：读取闭包捕获的第 i 个值
    FIELD_SET        = 0xEA   # u32 字段名：把栈顶值写入次栈顶记录的该字段


    # 比较
    EQ = 0x40
    NE = 0x41
    LT = 0x42
    LE = 0x43
    GT = 0x44
    GE = 0x45
    NOT = 0x46

    # 控制流
    JMP = 0x50             # i32 相对偏移（相对下一条指令）
    JMP_IF = 0x51
    JMP_IFNOT = 0x52

    # 函数
    CALL = 0x60            # u16 函数索引, u8 实参数
    RET = 0x61
    RET_NIL = 0x62

    # 集合与字段
    MAKE_SEQ = 0x70        # u16 元素数
    MAKE_MAP = 0x71        # u16 键值对数
    MAKE_REC = 0x72        # u16 记录索引, u8 字段数
    FIELD_GET = 0x73       # u32 字段名池索引
    INDEX_GET = 0x74
    INDEX_SET = 0x75
    LEN = 0x76

    # 输出与错误
    PRINT = 0x80           # u32 单位串池索引（0 表示不加单位）
    THROW = 0x81           # u32 错误类别池索引

    # 能力
    CAP_CHECK = 0x90       # u32 能力名池索引
    SANDBOX_ENTER = 0x91
    SANDBOX_EXIT = 0x92
    AUTHORIZE = 0x93       # u32 能力名池索引（在沙箱内登记一条授权）

    # 异常处理（区间式）：TRY_BEGIN 记录处理器地址，TRY_END 撤销
    TRY_BEGIN = 0xA0       # i32 catch 块指令下标, i32 finally 块下标（-1 无）
    TRY_END = 0xA1         # 正常退出 try 区
    END_TRY = 0xA2         # catch 块结束（恢复外层）

    # 并发
    SPAWN = 0xB4     # u16 函数索引, u8 实参数, u16 信箱槽位 → 新任务，结果写信箱
    YIELD = 0xB5     # 无操作数（协作式让出；多线程下为调度点）
    RECV = 0xB6      # u16 信箱变量槽位                   → 阻塞取结果，压栈
    JOIN = 0xB7      # u8 任务组内任务数                   → 等待本组全部完成
    AWAIT = 0xB8     # u16 函数索引, u8 实参数, u16 信箱槽位 → 发起并立即等结果，压栈

    # 内置
    BUILTIN = 0xB0         # u8 内置编号, u8 实参数, u32 单位串池索引（化成文用）
    CALL_STD = 0xB1        # u32 函数名池索引, u8 实参数（标准库）
    CALL_HOST = 0xB2       # u32 点分名池索引, u8 实参数（宿主模块调用，保留）
    CALL_FFI = 0xB9        # u32 外部函数名池索引, u8 实参数（C 动态库调用）
    CALL_TOOL = 0xB3       # u32 能力名池索引, u8 实参数（内置工具）


# 指令操作数格式：名称 → 操作数数量与宽度（'a'=u8,'b'=u16,'c'=u32,'d'=i32,'f'=f64）
OPERANDS: dict[Op, str] = {
    Op.PUSH_NUM: "f",
    Op.PUSH_CONST: "c",
    Op.LOAD_LOCAL: "b",
    Op.STORE_LOCAL: "b",
    Op.LOAD_GLOBAL: "b",
    Op.STORE_GLOBAL: "b",
    Op.JMP: "d",
    Op.JMP_IF: "d",
    Op.JMP_IFNOT: "d",
    Op.CALL: "ba",
    Op.MAKE_SEQ: "b",
    Op.MAKE_MAP: "b",
    Op.MAKE_REC: "ba",
    Op.FIELD_GET: "c",
    Op.PRINT: "c",
    Op.THROW: "c",
    Op.CAP_CHECK: "c",
    Op.AUTHORIZE: "c",
    Op.SPAWN: "bab",
    Op.RECV: "b",
    Op.JOIN: "a",
    Op.AWAIT: "bab",
    Op.TRY_BEGIN: "dd",
    Op.TRY_END: "",
    Op.END_TRY: "",
    Op.BUILTIN: "aac",
    Op.CALL_STD: "ca",
    Op.CALL_HOST: "ca",
    Op.CALL_FFI: "ca",
    Op.ADD_SLOT_IMM: "hf",
    Op.ADD_SLOT_SLOT: "hh",
    Op.LT_SLOT_IMM: "hf",
    Op.MAKE_FUNC: "h",
    Op.CALL_VALUE: "hb",
    Op.HIGHER_ORDER: "ab",
    Op.SLOT_INDEX_SLOT: "hh",
    Op.SLOT_INDEX_IMM: "hf",
    Op.SLOT_INDEX_STORE: "hhh",
    Op.SUB_SLOT_IMM: "hf",
    Op.MUL_SLOT_IMM: "hf",
    Op.DIV_SLOT_IMM: "hf",
    Op.MOD_SLOT_IMM: "hf",
    Op.SUB_SLOT_SLOT: "hh",
    Op.MUL_SLOT_SLOT: "hh",
    Op.GT_SLOT_IMM: "hf",
    Op.LE_SLOT_IMM: "hf",
    Op.GE_SLOT_IMM: "hf",
    Op.EQ_SLOT_IMM: "hf",
    Op.NE_SLOT_IMM: "hf",
    Op.SLOT_APPEND_SLOT: "hh",
    Op.SLOT_APPEND_IMM: "hf",
    Op.SLOT_CONCAT_SLOT: "hh",
    Op.SLOT_APPEND_STACK: "h",
    Op.MAKE_CLOSURE: "hb",
    Op.LOAD_FREE: "h",
    Op.FIELD_SET: "c",
    Op.ADD_NUM: "", Op.SUB_NUM: "", Op.MUL_NUM: "", Op.DIV_NUM: "", Op.MOD_NUM: "",
    Op.LT_NUM: "", Op.LE_NUM: "", Op.GT_NUM: "", Op.GE_NUM: "",
    Op.EQ_NUM: "", Op.NE_NUM: "",
    Op.CALL_TOOL: "ca",
    Op.PUSH_TRUE: "",
    Op.PUSH_FALSE: "",
}

OPNAME = {op: op.name for op in Op}


# ---------------- 编码器 ----------------

class CodeBuilder:
    """字节码构造器。用「占位 + 回填」处理前向跳转。"""

    __slots__ = ("buf", "pool", "pool_index", "cur_line", "pc_line")

    def __init__(self):
        self.buf = bytearray()
        self.pool: list[tuple[C, Any]] = []
        self.pool_index: dict[tuple[C, Any], int] = {}
        # PC → 源码行号。运行时出错时 VM 给出 pc，上层据此定位源码行。
        self.cur_line: int = 0
        self.pc_line: list[int] = []

    def at_line(self, line: int) -> None:
        """标记后续指令来自某源码行。"""
        if line:
            self.cur_line = line

    # ---- 常量池 ----
    def const(self, kind: C, value: Any) -> int:
        key = (kind, value)
        idx = self.pool_index.get(key)
        if idx is None:
            idx = len(self.pool)
            self.pool.append(key)
            self.pool_index[key] = idx
        return idx

    def text(self, s: str) -> int:
        return self.const(C.TEXT, s)

    # ---- 发指令 ----
    def emit(self, op: Op, *operands) -> int:
        """写一条指令，返回其起始偏移。"""
        pos = len(self.buf)
        if len(self.pc_line) <= pos:
            self.pc_line.extend([0] * (pos - len(self.pc_line) + 1))
        self.pc_line[pos] = self.cur_line
        self.buf.append(int(op))
        fmt = OPERANDS.get(op, "")
        for ch, val in zip(fmt, operands):
            if ch == "a":
                self.buf.append(val & 0xFF)
            elif ch in ("b", "h"):      # h = u16（槽位号），与 b 同宽
                self.buf += struct.pack("<H", int(val) & 0xFFFF)
            elif ch == "c":
                self.buf += struct.pack("<I", val & 0xFFFFFFFF)
            elif ch == "d":
                self.buf += struct.pack("<i", val)
            elif ch == "f":
                self.buf += struct.pack("<d", float(val))
        return pos

    def emit_num(self, v: float) -> None:
        self.emit(Op.PUSH_NUM, v)

    def patch_jump(self, pos: int) -> None:
        """把 pos 处的跳转目标改为「当前位置」（相对下一条指令）。"""
        # 操作数位于 op 码之后
        target = len(self.buf) - (pos + 5)
        struct.pack_into("<i", self.buf, pos + 1, target)

    def patch_jump_to(self, pos: int, dest: int) -> None:
        """把 pos 处的跳转目标改为字节偏移 dest（相对下一条指令）。"""
        struct.pack_into("<i", self.buf, pos + 1, dest - (pos + 5))

    def patch_operand_at(self, pos: int, value: int) -> None:
        """直接改写 pos 处的 i32 操作数（用于双操作数指令的第 2 个字段）。"""
        struct.pack_into("<i", self.buf, pos + 5, value)

    def here(self) -> int:
        return len(self.buf)


# ---------------- 模块容器 ----------------

@dataclass
class FuncCode:
    name: str
    n_params: int
    n_slots: int
    ret_kind: str          # "数"/"文本"/"真假"/"空"
    ret_unit: str
    code: bytes = b""


@dataclass
class RecordLayout:
    name: str
    fields: list[str] = field(default_factory=list)


@dataclass
class Module:
    pool: list[tuple[C, Any]] = field(default_factory=list)
    entry_slots: int = 0
    say_units: list[str] = field(default_factory=list)
    # FFI：别名 → 动态库名/路径（如 {"数学": "libm", "压缩": "libz"}）
    ffi_libs: dict[str, str] = field(default_factory=dict)
    funcs: list[FuncCode] = field(default_factory=list)
    records: list[RecordLayout] = field(default_factory=list)
    entry: bytes = b""
    n_globals: int = 0
    # 入口输出单位映射（全局槽位 → 单位串），供输出格式化
    global_units: dict[int, str] = field(default_factory=dict)
    # 入口 PC → 源码行号（仅内存使用，不序列化）
    entry_lines: list[int] = field(default_factory=list)

    # ---- 序列化 ----
    def to_bytes(self) -> bytes:
        out = bytearray()
        out += b"HENG" + bytes([2, 0])

        # 常量池
        out += struct.pack("<I", len(self.pool))
        for kind, value in self.pool:
            out.append(int(kind))
            if kind == C.NUM:
                out += struct.pack("<d", float(value))
            elif kind == C.BOOL:
                out.append(1 if value else 0)
            elif kind in (C.TEXT, C.UNIT):
                raw = str(value).encode("utf-8")
                out += struct.pack("<I", len(raw)) + raw
            # NIL 无负载

        # 函数表
        out += struct.pack("<I", len(self.funcs))
        for f in self.funcs:
            name_idx = self._find_text(f.name)
            ret_idx = self._find_text(f.ret_kind)
            unit_idx = self._find_text(f.ret_unit)
            out += struct.pack("<IHHHH", name_idx, f.n_params, f.n_slots,
                               ret_idx, unit_idx)
            out += struct.pack("<I", len(f.code)) + f.code

        # 记录表
        out += struct.pack("<I", len(self.records))
        for r in self.records:
            out += struct.pack("<IH", self._find_text(r.name), len(r.fields))
            for fld in r.fields:
                out += struct.pack("<I", self._find_text(fld))

        # 入口
        out += struct.pack("<I", len(self.entry)) + self.entry

        # 槽位计数
        out += struct.pack("<H", self.n_globals)
        out += struct.pack("<H", self.entry_slots)

        # 输出单位标签表（量纲在编译期消除，单位只用于显示）。
        # 直接内嵌 UTF-8 字符串而非常量池索引：空串与「第一个常量」无法区分，
        # 索引方式会在原生 VM 侧产生歧义（真实错的来源）。
        out += struct.pack("<I", len(self.say_units))
        for u in self.say_units:
            raw = u.encode("utf-8")
            out += struct.pack("<I", len(raw)) + raw

        # FFI 库表（可选段）：老版本字节码没有这一段，读取方需容忍缺失
        out += struct.pack("<I", len(self.ffi_libs))
        for alias, lib in self.ffi_libs.items():
            for txt in (str(alias), str(lib)):
                raw = txt.encode("utf-8")
                out += struct.pack("<I", len(raw)) + raw
        return bytes(out)

    def _find_text(self, s: str) -> int:
        for i, (k, v) in enumerate(self.pool):
            if k == C.TEXT and v == s:
                return i
        raise ValueError(f"常量池缺少字符串 {s!r}")

    @classmethod
    def from_bytes(cls, data: bytes) -> "Module":
        if data[:4] != b"HENG":
            raise ValueError("不是衡语字节码文件（magic 不匹配）")
        if data[4] != 2:
            raise ValueError(f"字节码版本不支持：{data[4]}")
        p = 6
        (n_pool,) = struct.unpack_from("<I", data, p)
        p += 4
        pool: list[tuple[C, Any]] = []
        for _ in range(n_pool):
            kind = C(data[p])
            p += 1
            if kind == C.NUM:
                (v,) = struct.unpack_from("<d", data, p)
                p += 8
            elif kind == C.BOOL:
                v = bool(data[p])
                p += 1
            elif kind in (C.TEXT, C.UNIT):
                (ln,) = struct.unpack_from("<I", data, p)
                p += 4
                v = data[p:p + ln].decode("utf-8")
                p += ln
            else:
                v = None
            pool.append((kind, v))

        def s(idx: int) -> str:
            v = pool[idx][1]
            return str(v) if v is not None else ""

        (n_funcs,) = struct.unpack_from("<I", data, p)
        p += 4
        funcs: list[FuncCode] = []
        for _ in range(n_funcs):
            name_i, n_params, n_slots, ret_i, unit_i = struct.unpack_from("<IHHHH", data, p)
            p += 12
            (clen,) = struct.unpack_from("<I", data, p)
            p += 4
            code = data[p:p + clen]
            p += clen
            funcs.append(FuncCode(s(name_i), n_params, n_slots, s(ret_i), s(unit_i), code))

        (n_recs,) = struct.unpack_from("<I", data, p)
        p += 4
        recs: list[RecordLayout] = []
        for _ in range(n_recs):
            name_i, n_f = struct.unpack_from("<IH", data, p)
            p += 6
            fields = []
            for _ in range(n_f):
                (fi,) = struct.unpack_from("<I", data, p)
                p += 4
                fields.append(s(fi))
            recs.append(RecordLayout(s(name_i), fields))

        (elen,) = struct.unpack_from("<I", data, p)
        p += 4
        entry = data[p:p + elen]
        p += elen
        (n_glob,) = struct.unpack_from("<H", data, p)
        p += 2
        (entry_slots,) = struct.unpack_from("<H", data, p)
        p += 2
        (n_units,) = struct.unpack_from("<I", data, p)
        p += 4
        units: list[str] = []
        for _ in range(n_units):
            (ul,) = struct.unpack_from("<I", data, p)
            p += 4
            units.append(data[p:p + ul].decode("utf-8"))
            p += ul

        return cls(pool=pool, funcs=funcs, records=recs, entry=entry,
                   n_globals=n_glob, entry_slots=entry_slots, say_units=units)
