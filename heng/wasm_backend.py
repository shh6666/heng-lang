"""衡语 WASM 后端：把 AST 编译为真实可运行的 WebAssembly 二进制模块。

零外部依赖——手写 LEB128 与 wasm 二进制 section 编码。

支持子集（衡语数值/控制核心）：
  - 数值字面量（f64，丢弃单位标注——单位在编译期已检查完毕）
  - 二元运算 + - * / 与比较 = != < <= > >=（返回 i32 布尔）
  - 函数定义、调用、返回
  - 变量声明（局部变量）、赋值
  - 倘若/否则 条件
  - 循环 + 断言
  - 输出写入线性内存（供宿主读取）

设计：三层管线的「底层」。衡语源码 → AST → 类型检查 → 本后端 → .wasm 二进制。
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Optional

from .ast_nodes import (
    Expr, Number, String, Bool, Null, Var, Attr, Binary, Unary, Call,
    SeqLiteral, MapLiteral, Stmt, VarDecl, Assign, If, Loop, Repeat, ForEach,
    Return, Assert, Output, ExprStmt, FuncDef, Program,
)


# ---------------- WASM 基础编码 ----------------

def uleb(n: int) -> bytes:
    """无符号 LEB128。"""
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def sleb(n: int) -> bytes:
    """有符号 LEB128。"""
    out = bytearray()
    more = True
    while more:
        b = n & 0x7F
        n >>= 7
        if (n == 0 and (b & 0x40) == 0) or (n == -1 and (b & 0x40) != 0):
            out.append(b)
            more = False
        else:
            out.append(b | 0x80)
    return bytes(out)


def vec(items: list[bytes]) -> bytes:
    return uleb(len(items)) + b"".join(items)


def vt_vec(types: list[int]) -> bytes:
    """值类型向量：元素本身是单字节值类型码。"""
    return uleb(len(types)) + b"".join(bytes([t]) for t in types)


def name(s: str) -> bytes:
    b = s.encode("utf-8")
    return uleb(len(b)) + b


def section(sid: int, payload: bytes) -> bytes:
    return bytes([sid]) + uleb(len(payload)) + payload


# 值类型
I32 = 0x7F
F64 = 0x7C

# 指令操作码
I32_CONST = 0x41
F64_CONST = 0x44
I64_CONST = 0x42
LOCAL_GET = 0x20
LOCAL_SET = 0x21
LOCAL_TEE = 0x22
F64_ADD = 0xA0
F64_SUB = 0xA1
F64_MUL = 0xA2
F64_DIV = 0xA3
F64_NEG = 0x91
F64_EQ = 0x61
F64_NE = 0x62
F64_LT = 0x63
F64_GT = 0x64
F64_LE = 0x65
F64_GE = 0x66
I32_EQZ = 0x45
I32_EQ = 0x46
I32_NE = 0x47
SELECT = 0x1B
CALL = 0x10
IF = 0x04
ELSE = 0x05
END = 0x0B
BLOCK = 0x02
BR = 0x0C
BR_IF = 0x0D
LOOP = 0x03
DROP = 0x1A
RETURN = 0x0F
UNREACHABLE = 0x00
I32_TRUNC_F64_S = 0xAE
F64_CONVERT_I32_S = 0xB7
I32_AND = 0x71
I32_OR = 0x72


class WasmCompileError(Exception):
    pass


def _has_return(stmts: list[Stmt]) -> bool:
    """递归判断语句序列中是否存在 返回（含嵌套块）。

    用于决定函数末尾是否需要补默认返回值。只看第一层会漏判
    「如果…返回…结束」，导致多余栈值（真实的 WASM 验证失败来源）。
    """
    for s in stmts:
        if isinstance(s, Return):
            return True
        for attr in ("body", "then_body", "else_body", "catch_body", "finally_body"):
            sub = getattr(s, attr, None)
            if isinstance(sub, list) and _has_return(sub):
                return True
    return False


def _always_returns(stmts: list[Stmt]) -> bool:
    """判断语句序列是否必定以 返回 结束（所有可达路径都返回）。

    用例：函数体是 `如果 c: 返回 a 否则: 返回 b 结束` 时，两个分支都以
    明确返回收尾，执行到 `end` 之后不可达，末尾不能再补默认返回值——
    否则会造成「fallthru 缺少栈值」的 WASM 验证失败。
    """
    for s in stmts:
        if isinstance(s, Return):
            return True
        if isinstance(s, If):
            if s.else_body and _always_returns(s.then_body) and _always_returns(s.else_body):
                # 两分支都必然返回，但后续语句仍可能不可达；按「必返回」处理
                return True
        if isinstance(s, (Loop, Repeat)):
            # 循环体即使必返回，也可能执行 0 次；不能视为必返回
            continue
    return False


@dataclass
class FuncCtx:
    """单个函数的编译上下文。"""
    locals: dict[str, int] = field(default_factory=dict)   # 变量名 → local index
    local_types: list[int] = field(default_factory=list)
    code: bytearray = field(default_factory=bytearray)

    def declare_local(self, name: str, ty: int) -> int:
        idx = len(self.local_types)
        self.locals[name] = idx
        self.local_types.append(ty)
        return idx


@dataclass
class WasmModule:
    """产出的 wasm 模块信息。"""
    binary: bytes
    exports: list[str]
    func_names: list[str]


class WasmCompiler:
    def __init__(self, program: Program):
        self.prog = program
        # 函数签名：name → (param_types, ret_type or None, func_index)
        self.func_sigs: dict[str, tuple[list[int], Optional[int], int]] = {}
        self._assign_indices()

    def _wasm_type(self, t) -> int:
        """衡语类型 → wasm 值类型：真假用 i32，其余（数）用 f64。"""
        return I32 if getattr(t, "name", "") == "真假" else F64

    def _assign_indices(self) -> None:
        # imported 输出函数占 index 0
        idx = 1
        for fn in self.prog.functions:
            params = [self._wasm_type(p.type) for p in fn.params]
            ret = F64 if fn.ret_type.name == "数" else (I32 if fn.ret_type.name == "真假" else None)
            self.func_sigs[fn.name] = (params, ret, idx)
            idx += 1
        # 入口函数分配最后一个 index
        self.entry_index = idx

    def compile(self) -> WasmModule:
        # -------- Type section (1) --------
        # type 0: (f64)->i32  [host output: fd? 简化为 (f64)->() 用 type1]
        types: list[bytes] = []
        # type 0: () -> ()   (host 输出函数)
        types.append(b"\x60" + vt_vec([]) + vt_vec([]))
        # type 1: (f64) -> ()  备用
        types.append(b"\x60" + vt_vec([F64]) + vt_vec([]))
        # 每个衡语函数一个 type
        type_of_func: dict[str, int] = {}
        for fn in self.prog.functions:
            params = [self._wasm_type(p.type) for p in fn.params]
            ret = F64 if fn.ret_type.name == "数" else (I32 if fn.ret_type.name == "真假" else None)
            rets = [] if ret is None else [ret]
            type_of_func[fn.name] = len(types)
            types.append(b"\x60" + vt_vec(params) + vt_vec(rets))
        # 入口 type: () -> f64
        entry_type = len(types)
        types.append(b"\x60" + vt_vec([]) + vt_vec([F64]))
        type_sec = section(1, vec(types))

        # -------- Import section (2): 宿主输出函数 --------
        imports = [name("env") + name("输出") + b"\x00" + uleb(0)]
        import_sec = section(2, vec(imports))

        # -------- Function section (3) --------
        func_type_indices: list[int] = []
        for fn in self.prog.functions:
            func_type_indices.append(type_of_func[fn.name])
        func_type_indices.append(entry_type)
        func_sec = section(3, vec([uleb(t) for t in func_type_indices]))

        # -------- Memory section (5): 1 页 = 64KB，用于输出缓冲 --------
        mem_sec = section(5, vec([b"\x00" + uleb(1)]))

        # -------- Export section (7) --------
        exports: list[tuple[str, int]] = [("运行", self.entry_index)]
        for fn in self.prog.functions:
            exports.append((fn.name, self.func_sigs[fn.name][2]))
        export_entries = []
        for nm, idx in exports:
            export_entries.append(name(nm) + b"\x00" + uleb(idx))
        export_sec = section(7, vec(export_entries))

        # -------- Code section (10) --------
        bodies: list[bytes] = []
        for fn in self.prog.functions:
            bodies.append(self._compile_function(fn, is_entry=False))
        bodies.append(self._compile_entry())
        code_sec = section(10, vec(bodies))

        binary = (
            b"\x00asm" + struct.pack("<I", 1)
            + type_sec + import_sec + func_sec + mem_sec + export_sec + code_sec
        )
        return WasmModule(binary=binary, exports=[e[0] for e in exports],
                          func_names=[f.name for f in self.prog.functions])

    # -------- 函数体编译 --------
    def _compile_function(self, fn: FuncDef, is_entry: bool) -> bytes:
        ctx = FuncCtx()
        # 参数占 local 0..n-1，类型按声明（真假=i32，其余=f64）
        for p in fn.params:
            ctx.declare_local(p.name, self._wasm_type(p.type))
        self._compile_block(fn.body, ctx)
        # 若函数体「所有路径都返回」，则末尾不可达，必须补 unreachable
        # 让验证器知道这里不会有 fallthrough；否则若从未返回则补默认值。
        # 旧实现只看第一层有没有 返回，漏判「如果…返回…否则…返回…结束」，
        # 造成 fallthru 缺少栈值——真实的 WASM 验证失败来源。
        if _always_returns(fn.body):
            ctx.code += bytes([UNREACHABLE])
        elif not _has_return(fn.body):
            if self._wasm_type(fn.ret_type) == I32:
                ctx.code += bytes([I32_CONST]) + uleb(0)
            else:
                ctx.code += bytes([F64_CONST]) + struct.pack("<d", 0.0)
        ctx.code += bytes([END])
        return self._finish_body(ctx, param_count=len(fn.params))

    def _compile_entry(self) -> bytes:
        ctx = FuncCtx()
        if self.prog.entry:
            self._compile_block(self.prog.entry, ctx)
        ctx.code += bytes([F64_CONST]) + struct.pack("<d", 0.0)
        ctx.code += bytes([END])
        return self._finish_body(ctx, param_count=0)

    def _finish_body(self, ctx: FuncCtx, param_count: int = 0) -> bytes:
        """组装函数体。WASM 规范：local 段只声明「参数之外」的局部变量，
        参数已由函数签名声明并占据索引 0..param_count-1。
        旧实现把参数也写进 local 段，导致索引错位——本修复的关键。
        """
        extra = ctx.local_types[param_count:]
        if extra:
            # 按类型连续分组（同类型合并为一条 count+type），保持声明索引顺序
            groups = []
            i = 0
            while i < len(extra):
                t = extra[i]
                n = 1
                while i + n < len(extra) and extra[i + n] == t:
                    n += 1
                groups.append(uleb(n) + bytes([t]))
                i += n
            locals_decl = vec(groups)
        else:
            locals_decl = vec([])
        body = locals_decl + bytes(ctx.code)
        return uleb(len(body)) + body

    # -------- 语句编译 --------
    def _compile_block(self, stmts: list[Stmt], ctx: FuncCtx) -> None:
        for s in stmts:
            self._compile_stmt(s, ctx)

    def _compile_stmt(self, s: Stmt, ctx: FuncCtx) -> None:
        if isinstance(s, VarDecl):
            # 布尔变量用 i32 local，数值用 f64 local
            val_ty = self._wasm_type(s.declared_type)
            if s.init is not None:
                init = s.init
                self._compile_expr(init, ctx)  # 值在栈上
            else:
                ctx.code += bytes([I32_CONST]) + uleb(0) if val_ty == I32 else bytes([F64_CONST]) + struct.pack("<d", 0.0)
            idx = ctx.declare_local(s.name, val_ty)
            ctx.code += bytes([LOCAL_SET]) + uleb(idx)
            return

        if isinstance(s, Assign):
            self._compile_expr(s.value, ctx)
            idx = ctx.locals.get(s.target)
            if idx is None:
                raise WasmCompileError(f"{s.line}:{s.col} 未定义局部变量 {s.target}")
            ctx.code += bytes([LOCAL_SET]) + uleb(idx)
            return

        if isinstance(s, If):
            self._compile_expr(s.cond, ctx)   # i32 条件
            ctx.code += bytes([IF]) + bytes([0x40])  # void if：返回值由分支内 RETURN 处理
            self._compile_block(s.then_body, ctx)
            if s.else_body:
                ctx.code += bytes([ELSE])
                self._compile_block(s.else_body, ctx)
            ctx.code += bytes([END])
            return

        if isinstance(s, Loop):
            # block + loop 结构
            ctx.code += bytes([BLOCK]) + bytes([0x40])  # void block
            ctx.code += bytes([LOOP]) + bytes([0x40])
            self._compile_expr(s.cond, ctx)
            ctx.code += bytes([I32_EQZ]) + bytes([BR_IF]) + uleb(1)  # 条件为假则跳出 loop
            self._compile_block(s.body, ctx)
            ctx.code += bytes([BR]) + uleb(0)  # 继续 loop
            ctx.code += bytes([END])  # loop end
            ctx.code += bytes([END])  # block end
            return

        if isinstance(s, Repeat):
            # 重复 N 次 → 计数器循环，语义与解释器一致（执行恰好 N 次）。
            #   上界 = N                        存为 f64 局部变量
            #   计数 = 0                        存为 f64 局部变量
            #   block
            #     loop
            #       计数 >= 上界 ? br 1（跳出）
            #       <body>
            #       计数 = 计数 + 1
            #       br 0
            #     end
            #   end
            self._compile_expr(s.times, ctx)
            upper = ctx.declare_local(f"$上界{len(ctx.local_types)}", F64)
            ctx.code += bytes([LOCAL_SET]) + uleb(upper)
            counter = ctx.declare_local(f"$计数{len(ctx.local_types)}", F64)
            ctx.code += bytes([F64_CONST]) + struct.pack("<d", 0.0)
            ctx.code += bytes([LOCAL_SET]) + uleb(counter)

            ctx.code += bytes([BLOCK]) + bytes([0x40])
            ctx.code += bytes([LOOP]) + bytes([0x40])
            ctx.code += bytes([LOCAL_GET]) + uleb(counter)
            ctx.code += bytes([LOCAL_GET]) + uleb(upper)
            ctx.code += bytes([F64_GE])
            ctx.code += bytes([BR_IF]) + uleb(1)
            self._compile_block(s.body, ctx)
            ctx.code += bytes([LOCAL_GET]) + uleb(counter)
            ctx.code += bytes([F64_CONST]) + struct.pack("<d", 1.0)
            ctx.code += bytes([F64_ADD])
            ctx.code += bytes([LOCAL_SET]) + uleb(counter)
            ctx.code += bytes([BR]) + uleb(0)
            ctx.code += bytes([END])
            ctx.code += bytes([END])
            return

        if isinstance(s, ForEach):
            # 每个 <序列字面量> 为 x —— 序列迭代在 WASM 里没有堆对象，
            # 因此只支持「编译期可知元素」的序列字面量：展开为逐元素赋值 + 执行块体。
            # 其他可迭代值（变量、映射）必须显式报错，不能静默跳过——
            # 嵌入式场景下「编译成功但逻辑被删掉」比编译失败危险得多。
            if not isinstance(s.iterable, SeqLiteral):
                raise WasmCompileError(
                    f"{s.line}:{s.col} WASM 后端的 每个 只支持序列字面量；"
                    f"请改用索引循环（循环 + 变量），或改用解释器运行")
            if not s.iterable.items:
                return
            # 复用同一个局部变量承载当前元素，逐项赋值后执行块体
            idx = ctx.locals.get(s.var)
            if idx is None:
                idx = ctx.declare_local(s.var, F64)
            for item in s.iterable.items:
                self._compile_expr(item, ctx)
                ctx.code += bytes([LOCAL_SET]) + uleb(idx)
                self._compile_block(s.body, ctx)
            return

        if isinstance(s, Return):
            self._compile_expr(s.value, ctx)
            ctx.code += bytes([RETURN])
            return

        if isinstance(s, Assert):
            self._compile_expr(s.cond, ctx)
            ctx.code += bytes([I32_EQZ]) + bytes([IF]) + bytes([0x40]) + bytes([UNREACHABLE]) + bytes([END])
            return

        if isinstance(s, Output):
            # 输出：把 f64 传给宿主 输出 函数
            self._compile_expr(s.value, ctx)
            ctx.code += bytes([CALL]) + uleb(0)
            return

        if isinstance(s, ExprStmt):
            self._compile_expr(s.expr, ctx)
            ctx.code += bytes([DROP])
            return

        # 其余语句（错误处理/沙箱/并发/观察/执行等）属运行时语义，WASM 后端未实现。
        # 安全要求：必须显式报编译错误，绝不能静默跳过——
        # 在嵌入式/机器人场景下，"编译成功但逻辑被静默删除"比"编译失败"危险得多。
        raise WasmCompileError(
            f"{s.line}:{s.col} WASM 后端不支持语句 {type(s).__name__}；"
            f"请仅用数值子集，或改用解释器运行（错误/沙箱/并发/观察等需解释器语义）")

    # -------- 表达式编译 --------
    def _compile_expr(self, e: Expr, ctx: FuncCtx) -> None:
        if isinstance(e, Number):
            ctx.code += bytes([F64_CONST]) + struct.pack("<d", e.value)
            return
        if isinstance(e, Bool):
            ctx.code += bytes([I32_CONST]) + uleb(1 if e.value else 0)
            return
        if isinstance(e, Null):
            ctx.code += bytes([F64_CONST]) + struct.pack("<d", 0.0)
            return
        if isinstance(e, Var):
            idx = ctx.locals.get(e.name)
            if idx is None:
                raise WasmCompileError(f"{e.line}:{e.col} 未定义局部变量 {e.name}")
            ctx.code += bytes([LOCAL_GET]) + uleb(idx)
            return
        if isinstance(e, Unary):
            self._compile_expr(e.operand, ctx)
            if e.op == "-":
                ctx.code += bytes([F64_NEG])
            elif e.op == "非":
                ctx.code += bytes([I32_EQZ])
            return
        if isinstance(e, Binary):
            # 逻辑与/或：两侧为 i32 布尔，结果 i32（非短路——wasm 表达式层无短路语义，
            # 但布尔表达式无副作用时等价；含副作用的短路由解释器保证）
            if e.op in ("并且", "或者"):
                self._compile_expr(e.left, ctx)
                self._compile_expr(e.right, ctx)
                ctx.code += bytes([I32_AND if e.op == "并且" else I32_OR])
                return
            self._compile_expr(e.left, ctx)
            self._compile_expr(e.right, ctx)
            op = {
                "+": F64_ADD, "-": F64_SUB, "*": F64_MUL, "/": F64_DIV,
                "==": F64_EQ, "!=": F64_NE, "<": F64_LT, ">": F64_GT,
                "<=": F64_LE, ">=": F64_GE,
            }.get(e.op)
            if op is None:
                raise WasmCompileError(f"{e.line}:{e.col} wasm 不支持运算符 {e.op}")
            ctx.code += bytes([op])
            return
        if isinstance(e, Call):
            # 函数调用
            if isinstance(e.callee, Var) and e.callee.name in self.func_sigs:
                for arg in e.args:
                    self._compile_expr(arg, ctx)
                _, ret, idx = self.func_sigs[e.callee.name]
                ctx.code += bytes([CALL]) + uleb(idx)
                if ret is None:
                    ctx.code += bytes([F64_CONST]) + struct.pack("<d", 0.0)
                return
            # 输出() 调用
            if isinstance(e.callee, Var) and e.callee.name == "输出":
                for arg in e.args:
                    self._compile_expr(arg, ctx)
                ctx.code += bytes([CALL]) + uleb(0)
                ctx.code += bytes([F64_CONST]) + struct.pack("<d", 0.0)
                return
            raise WasmCompileError(f"{e.line}:{e.col} wasm 暂不支持该调用")
        # 字符串/属性/序列/映射等：WASM 后端未实现。
        # 安全要求：显式报错，绝不压 0 占位（否则产生"静默错误结果"的程序）。
        raise WasmCompileError(
            f"{e.line}:{e.col} WASM 后端不支持表达式 {type(e).__name__}；"
            f"WASM 目标仅支持数值/布尔子集")


def compile_to_wasm(program: Program) -> WasmModule:
    return WasmCompiler(program).compile()


def write_wasm(module: WasmModule, path: str) -> None:
    with open(path, "wb") as f:
        f.write(module.binary)
