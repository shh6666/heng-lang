"""衡语内核 v2 —— 代码生成：AST → .hbc 字节码。

关键点：
  1. 变量引用已由检查层解析成槽位下标，这里直接发 LOAD_LOCAL/STORE_LOCAL；
  2. 量纲已消除，数值运算只发 ADD/SUB/MUL/DIV；
  3. 单位只作为「输出显示标签」记录，不参与运算；
  4. 控制流全部编译成跳转，不使用宿主递归。
"""

from __future__ import annotations

import math
import os

from typing import Optional

from .ast2 import (
    Expr, Num, Text, Bool, Nil, VarRef, Field, BinOp, UnOp, Call, Await,
    Index, SetIndex, SetField, Lambda, SeqLit, MapLit, Stmt, Let, SetVar, If, Loop, Repeat, Each, Return, Assert,
    Raise, Try, Say, Exec, Observe, Sandbox, Authorize, Parallel, Spawn,
    Receive, ExprStmt, Func, Program,
)
import struct

from .bytecode import Op, C, CodeBuilder, Module, FuncCode, RecordLayout
from .check2 import BUILTINS
from .types2 import Type, KNOWN_CAPABILITIES, T_ANY

# 超指令开关：设 HENG_NO_SUPER=1 可关闭融合，用于对照测量与排查。
# 便于回答「这条优化到底带来多少收益」，而不必维护两份代码。
_NO_SUPER = os.environ.get("HENG_NO_SUPER") == "1"


class CompileError(Exception):
    pass


class Compiler:
    def __init__(self, prog: Program, host_aliases: set[str] | None = None,
                 global_slots: dict[str, int] | None = None):
        self.prog = prog
        self._current_fn = None
        self.global_slots = global_slots or {}
        self.host_aliases = host_aliases or set()
        self._tmp_base = 0
        self._tmp_used = 0
        self._entry_slots = 0
        # 编译期能力作用域：记录当前是否在沙箱内及已授权能力
        self._cap_scopes: list[set[str] | None] = [None]
        # 变量名 → 槽位（编译期镜像，供信箱等按名查找）
        self._named_slots: dict[str, int] = {}
        # 并发编排：当前所在的并发块 id 栈，以及每组已发起的任务数
        self._group_stack: list[int] = []
        self._group_marks: dict[int, int] = {}
        self.rec_index = {r.name: i for i, r in enumerate(prog.records)}
        # 记录字段名 → 常量池（所有函数共享同一个池，见 _shared_pool）
        self.b = CodeBuilder()
        self.pool = self.b.pool
        self.pool_index = self.b.pool_index
        # 输出单位标签序列（按 Say 出现顺序）
        self.say_units: list[str] = []
        # 当前函数发射器
        self.cur: Optional[CodeBuilder] = None

    # ---- 共享常量池：函数各自构建代码，但 pool 合并到模块级 ----
    def _make(self) -> CodeBuilder:
        b = CodeBuilder()
        b.pool = self.pool
        b.pool_index = self.pool_index
        return b

    # ============================================================
    # 入口
    # ============================================================
    def compile(self) -> Module:
        mod = Module()
        mod.pool = self.pool

        # 函数：先编译全部函数体，再编译入口（入口依赖函数表齐备）
        # 用索引遍历：编译函数体时可能派生新的匿名函数，列表会变长
        func_codes: list[FuncCode] = []
        idx = 0
        while idx < len(self.prog.funcs):
            func_codes.append(self.compile_func(self.prog.funcs[idx]))
            idx += 1

        entry_code = b""
        if self.prog.entry is not None:
            b = self._make()
            self.cur = b
            # 临时槽位从入口已分配的槽位之后开始，避免与用户变量冲突
            self._named_slots = dict(getattr(self.prog, "entry_name_slots", {}))
            self._tmp_base = self.prog.entry_slots
            self._tmp_used = 0
            # 全局常量必须在入口最前面求值并写入全局槽位，
            # 否则函数体里的 LOAD_GLOBAL 会读到空值（真实缺陷）。
            for name, _typ, val in self.prog.consts:
                self.emit_expr(val)
                b.emit(Op.STORE_GLOBAL, self.global_slots[name])
            self.emit_stmts(self.prog.entry)
            b.emit(Op.PUSH_NIL)
            b.emit(Op.HALT)
            entry_code = bytes(b.buf)
            mod.entry_lines = list(b.pc_line)
            self._entry_slots = self.prog.entry_slots + self._tmp_used

        # 入口编译期间可能派生匿名函数（lambda 在表达式位置被提升），
        # 这里把函数表补齐——顺序必须保持与 MAKE_FUNC 记录的索引一致。
        while idx < len(self.prog.funcs):
            func_codes.append(self.compile_func(self.prog.funcs[idx]))
            idx += 1

        # 序列化需要函数名/返回类型名字符串存在于池中，先登记
        self.b.text("")
        for fc in func_codes:
            self.b.text(fc.name)
            self.b.text(fc.ret_kind)
            if fc.ret_unit:
                self.b.text(fc.ret_unit)
        for r in self.prog.records:
            self.b.text(r.name)
            for f in r.fields:
                self.b.text(f.name)
        # 输出单位串也要进池（.hbc 运行时按索引取）
        for u in self.say_units:
            if u:
                self.b.text(u)

        mod.funcs = func_codes
        mod.records = [RecordLayout(r.name, [f.name for f in r.fields])
                       for r in self.prog.records]
        mod.entry = entry_code
        mod.n_globals = self.prog.n_globals
        mod.entry_slots = self._entry_slots
        mod.say_units = self.say_units
        return mod

    def compile_func(self, fn: Func) -> FuncCode:
        b = self._make()
        self.cur = b
        self._named_slots = dict(fn.name_slots)
        self._tmp_base = fn.n_slots
        self._tmp_used = 0

        # 契约前置条件：进入函数先检查，不满足即 错误<断言>
        if fn.contract is not None and fn.contract.require is not None:
            self.emit_expr(fn.contract.require)
            ok = b.emit(Op.JMP_IF, 0)
            b.emit(Op.PUSH_CONST, b.text(f"前置条件不满足：{fn.name}"))
            b.emit(Op.THROW, b.text("断言"))
            b.patch_jump(ok)

        self.emit_stmts(fn.body)
        b.emit(Op.PUSH_NIL)
        b.emit(Op.RET)

        ret_kind = fn.ret.name
        ret_unit = str(fn.ret.unit) if ret_kind == "数" else ""
        # 槽位总数 = 声明槽位 + 生成的临时槽位
        n_slots = fn.n_slots + self._tmp_used
        return FuncCode(fn.name, len(fn.params), n_slots, ret_kind,
                        ret_unit, bytes(b.buf))

    # ============================================================
    # 语句
    # ============================================================
    def emit_stmts(self, stmts: list[Stmt]) -> None:
        for s in stmts:
            self.emit_stmt(s)

    def emit_stmt(self, s: Stmt) -> None:
        b = self.cur
        if getattr(s, "line", 0):
            b.at_line(s.line)      # 记录 PC→行号，供运行时错误定位

        if isinstance(s, Let):
            if s.init is not None:
                self.emit_expr(s.init)
            else:
                b.emit(Op.PUSH_NIL)
            b.emit(Op.STORE_LOCAL, s.slot)
            return

        if isinstance(s, SetField):
            self.emit_expr(s.base)
            self.emit_expr(s.value)
            b.emit(Op.FIELD_SET, b.text(s.name))
            return

        if isinstance(s, SetIndex):
            # c[i] = v：只要容器是局部变量就走 SLOT_INDEX_STORE。
            # 这条指令能拿到目标槽位，因此可以正确做写时复制——
            # 通用 INDEX_SET 拿不到槽位，会把复制品丢掉，导致别名污染。
            # 只在「当前帧」的局部变量上启用：跨帧槽位号不能直接给 VM 用。
            # 临时槽不回收（槽位浪费几个，换来绝对安全——回收曾导致段错误）。
            if (not _NO_SUPER and s.base.ty is not None and s.base.ty.name == "序列"
                    and isinstance(s.base, VarRef) and s.base.slot >= 0
                    and s.base.depth == 0):
                islot = self._local_slot_of(s.index)
                if islot is None:
                    islot = self._tmp_slot()
                    self.emit_expr(s.index)
                    b.emit(Op.STORE_LOCAL, islot)
                vslot = self._local_slot_of(s.value)
                if vslot is None:
                    vslot = self._tmp_slot()
                    self.emit_expr(s.value)
                    b.emit(Op.STORE_LOCAL, vslot)
                b.emit(Op.SLOT_INDEX_STORE, s.base.slot, islot, vslot)
                return
            # 栈顺序：容器、下标、值 —— 与 INDEX_SET 的取值约定一致
            self.emit_expr(s.base)
            self.emit_expr(s.index)
            self.emit_expr(s.value)
            b.emit(Op.INDEX_SET)
            return

        if isinstance(s, SetVar):
            # xs = 表.追加(xs, v)：原地追加，避免整表复制（O(n^2) → O(n)）
            if (not _NO_SUPER and s.depth != -2 and s.slot >= 0
                    and isinstance(s.value, Call)
                    and self._dotted(s.value.callee) == "表.追加"
                    and len(s.value.args) == 2):
                target, val = s.value.args
                if (isinstance(target, VarRef) and target.name == s.target
                        and target.ty is not None and target.ty.name == "序列"):
                    islot = self._local_slot_of(val)
                    if islot is not None:
                        b.emit(Op.SLOT_APPEND_SLOT, s.slot, islot)
                        return
                    if isinstance(val, Num):
                        b.emit(Op.SLOT_APPEND_IMM, s.slot, float(val.value))
                        return
                    # 任意表达式：先求值压栈，再原地追加
                    self.emit_expr(val)
                    b.emit(Op.SLOT_APPEND_STACK, s.slot)
                    return

            # acc = 表.连接(acc, ys)：原地拼接，避免每次整表复制（O(n^2)）
            if (not _NO_SUPER and s.depth != -2 and s.slot >= 0
                    and isinstance(s.value, Call)
                    and self._dotted(s.value.callee) == "表.连接"
                    and len(s.value.args) == 2):
                left, right = s.value.args
                if (isinstance(left, VarRef) and left.name == s.target
                        and left.ty is not None and left.ty.name == "序列"):
                    rs = self._local_slot_of(right)
                    if rs is not None:
                        b.emit(Op.SLOT_CONCAT_SLOT, s.slot, rs)
                        return
                    # acc = 表.连接(acc, [x]) 单元素字面量：直接退化为追加
                    if isinstance(right, SeqLit) and len(right.items) == 1:
                        elem = right.items[0]
                        es = self._local_slot_of(elem)
                        if es is not None:
                            b.emit(Op.SLOT_APPEND_SLOT, s.slot, es)
                            return
                        if isinstance(elem, Num):
                            b.emit(Op.SLOT_APPEND_IMM, s.slot, float(elem.value))
                            return

            # 超指令融合：x = x OP 常量 / x = x OP y
            # 按 101 个项目的指令分布，+ 与 - 覆盖绝大多数自增/自减场景，
            # * 常见于累乘；比较类用于循环条件里的「与常量比」。
            if (not _NO_SUPER and s.depth != -2 and s.slot >= 0 and isinstance(s.value, BinOp)
                    and s.value.ty is not None and s.value.ty.name == "数"
                    and isinstance(s.value.left, VarRef) and s.value.left.name == s.target):
                r = s.value.right
                imm_ops = {"+": Op.ADD_SLOT_IMM, "-": Op.SUB_SLOT_IMM,
                           "*": Op.MUL_SLOT_IMM, "/": Op.DIV_SLOT_IMM,
                           "%": Op.MOD_SLOT_IMM}
                slot_ops = {"+": Op.ADD_SLOT_SLOT, "-": Op.SUB_SLOT_SLOT,
                            "*": Op.MUL_SLOT_SLOT}
                if isinstance(r, Num) and s.value.op in imm_ops:
                    b.emit(imm_ops[s.value.op], s.slot, float(r.value))
                    return
                if isinstance(r, VarRef) and r.slot >= 0 and r.depth >= 0 \
                        and s.value.op in slot_ops:
                    b.emit(slot_ops[s.value.op], s.slot, r.slot)
                    return
            self.emit_expr(s.value)
            if s.depth == -2:
                b.emit(Op.STORE_GLOBAL, s.slot)
            else:
                b.emit(Op.STORE_LOCAL, s.slot)
            return

        if isinstance(s, Say):
            self.emit_expr(s.value)
            # 单位直接编码在 PRINT 指令里，不再依赖「按顺序对位」——
            # 顺序对位在分支/循环下必然错乱（真实缺陷）。
            label = self._unit_label(s.value.ty)
            b.emit(Op.PRINT, b.text(label))
            return

        if isinstance(s, If):
            self.emit_expr(s.cond)
            j_false = b.emit(Op.JMP_IFNOT, 0)
            self.emit_stmts(s.then_body)
            if s.else_body is not None:
                j_end = b.emit(Op.JMP, 0)
                b.patch_jump(j_false)
                self.emit_stmts(s.else_body)
                b.patch_jump(j_end)
            else:
                b.patch_jump(j_false)
            return

        if isinstance(s, Loop):
            top = b.here()
            self.emit_expr(s.cond)
            j_out = b.emit(Op.JMP_IFNOT, 0)
            self.emit_stmts(s.body)
            b.emit(Op.JMP, top - (b.here() + 5))
            b.patch_jump(j_out)
            return

        if isinstance(s, Repeat):
            # 编译为计数器循环：
            #   n = 表达式；i = 0
            #   top: i >= n ? 跳出
            #        body; i = i + 1; jmp top
            # 使用两个隐藏槽位（编译期临时槽位，追加在函数槽位末尾）
            n_slot = self._tmp_slot()
            i_slot = self._tmp_slot()
            self.emit_expr(s.times)
            b.emit(Op.STORE_LOCAL, n_slot)
            b.emit(Op.PUSH_NUM, 0.0)
            b.emit(Op.STORE_LOCAL, i_slot)
            top = b.here()
            b.emit(Op.LOAD_LOCAL, i_slot)
            b.emit(Op.LOAD_LOCAL, n_slot)
            b.emit(Op.GE)
            j_out = b.emit(Op.JMP_IF, 0)
            self.emit_stmts(s.body)
            b.emit(Op.LOAD_LOCAL, i_slot)
            b.emit(Op.PUSH_NUM, 1.0)
            b.emit(Op.ADD)
            b.emit(Op.STORE_LOCAL, i_slot)
            b.emit(Op.JMP, top - (b.here() + 5))
            b.patch_jump(j_out)
            return

        if isinstance(s, Each):
            # 只支持序列字面量（编译期展开）与变量序列（运行期下标循环）
            if isinstance(s.iterable, SeqLit):
                for item in s.iterable.items:
                    self.emit_expr(item)
                    b.emit(Op.STORE_LOCAL, s.slot)
                    self.emit_stmts(s.body)
                return
            # 变量序列：i = 0; while i < len: x = seq[i]; body; i++
            seq_slot = self._tmp_slot()
            i_slot = self._tmp_slot()
            self.emit_expr(s.iterable)
            b.emit(Op.STORE_LOCAL, seq_slot)
            b.emit(Op.PUSH_NUM, 0.0)
            b.emit(Op.STORE_LOCAL, i_slot)
            top = b.here()
            b.emit(Op.LOAD_LOCAL, i_slot)
            b.emit(Op.LOAD_LOCAL, seq_slot)
            b.emit(Op.LEN)
            b.emit(Op.GE)
            j_out = b.emit(Op.JMP_IF, 0)
            b.emit(Op.LOAD_LOCAL, seq_slot)
            b.emit(Op.LOAD_LOCAL, i_slot)
            b.emit(Op.INDEX_GET)
            b.emit(Op.STORE_LOCAL, s.slot)
            self.emit_stmts(s.body)
            b.emit(Op.LOAD_LOCAL, i_slot)
            b.emit(Op.PUSH_NUM, 1.0)
            b.emit(Op.ADD)
            b.emit(Op.STORE_LOCAL, i_slot)
            b.emit(Op.JMP, top - (b.here() + 5))
            b.patch_jump(j_out)
            return

        if isinstance(s, Return):
            if s.value is None:
                b.emit(Op.RET_NIL)
            else:
                self.emit_expr(s.value)
                b.emit(Op.RET)
            return

        if isinstance(s, Assert):
            self.emit_expr(s.cond)
            pos = b.emit(Op.JMP_IF, 0)
            b.emit(Op.PUSH_CONST, b.text("断言失败"))
            b.emit(Op.THROW, b.text("断言"))
            b.patch_jump(pos)
            return

        if isinstance(s, Raise):
            self.emit_expr(s.message)
            b.emit(Op.THROW, b.text(s.error_type))
            return

        if isinstance(s, Try):
            # 布局（每一跳都用显式占位 + 回填，避免隐式落空）：
            #   TRY_BEGIN  <catch> <finally| -1>
            #   <try body>
            #   TRY_END
            #   JMP -> finally（若有）否则 -> after
            # catch:
            #   <bind msg>
            #   <catch body>
            #   END_TRY
            #   JMP -> finally（若有）否则 -> after
            # finally:
            #   <finally body>
            # after:
            begin = b.emit(Op.TRY_BEGIN, 0, -1)
            self.emit_stmts(s.body)
            b.emit(Op.TRY_END)
            j_norm = b.emit(Op.JMP, 0)

            catch_pos = b.here()
            if s.catch_body is not None:
                if s.catch_var:
                    b.emit(Op.STORE_LOCAL, s.catch_slot)
                else:
                    b.emit(Op.POP)
                self.emit_stmts(s.catch_body)
            else:
                b.emit(Op.POP)
            b.emit(Op.END_TRY)
            j_catch = b.emit(Op.JMP, 0)

            fin_pos = -1
            if s.finally_body is not None:
                fin_pos = b.here()
                self.emit_stmts(s.finally_body)

            after = b.here()
            b.patch_jump_to(j_norm, fin_pos if fin_pos >= 0 else after)
            b.patch_jump_to(j_catch, fin_pos if fin_pos >= 0 else after)

            # TRY_BEGIN 双操作数都是「相对下一条指令的字节偏移」（与 'd' 字段一致）
            struct.pack_into("<i", b.buf, begin + 1, catch_pos - (begin + 9))
            struct.pack_into("<i", b.buf, begin + 5,
                             (fin_pos - (begin + 9)) if fin_pos >= 0 else -1)
            return

        if isinstance(s, Sandbox):
            # 进入沙箱：能力清空，块内只有显式授权的能力可用
            b.emit(Op.SANDBOX_ENTER)
            self._push_caps()
            self.emit_stmts(s.body)
            self._pop_caps()
            b.emit(Op.SANDBOX_EXIT)
            return

        if isinstance(s, Authorize):
            for cap in s.capabilities:
                b.emit(Op.AUTHORIZE, b.text(cap))
            return

        if isinstance(s, Observe):
            self.emit_stmts(s.body)
            return

        if isinstance(s, Parallel):
            # 并发块：块内 发起 创建真任务；块尾 JOIN 等待本组全部完成。
            # JOIN 保证不泄漏线程，也让「块后一定能取到结果」成立。
            mark = self._group_marks.setdefault(s.group_id, 0)
            self._group_stack.append(s.group_id)
            try:
                self.emit_stmts(s.body)
            finally:
                self._group_stack.pop()
            started = self._group_marks.get(s.group_id, 0) - mark
            s.n_tasks = started
            b.emit(Op.JOIN, started if started < 256 else 255)
            return

        if isinstance(s, Exec):
            self.emit_expr(s.call)
            if s.name != "_":
                b.emit(Op.STORE_LOCAL, s.slot)
            else:
                b.emit(Op.POP)
            return

        if isinstance(s, Spawn):
            # 真并发：实参在父任务求值，深拷贝给子任务；立即返回不等待。
            # 子任务结束时把返回值写入信箱 <做法名>_信箱。
            for a in s.args:
                self.emit_expr(a)
            b.emit(Op.SPAWN, s.target, len(s.args),
                   self._mailbox_slot(s.func_name))
            # 记录本组任务数量，供块尾 JOIN 使用
            gid = self._group_stack[-1] if self._group_stack else -1
            self._group_marks[gid] = self._group_marks.get(gid, 0) + 1
            return

        if isinstance(s, Receive):
            # 从信箱阻塞取结果：信箱为空时让出，直到对应任务完成
            src = self._dotted(s.mailbox)
            if src:
                b.emit(Op.RECV, self._mailbox_slot(src))
            else:
                b.emit(Op.PUSH_NIL)
            if s.target and s.target_slot >= 0:
                b.emit(Op.STORE_LOCAL, s.target_slot)
            else:
                b.emit(Op.POP)
            return

        if isinstance(s, ExprStmt):
            self.emit_expr(s.expr)
            b.emit(Op.POP)
            return

        raise CompileError(f"{s.line}:{s.col} 不支持的语句 {type(s).__name__}")

    # ============================================================
    # 表达式
    # ============================================================
    def _fold_const(self, e: Expr):
        """常量折叠：纯字面量的算术表达式在编译期算出结果。

        只折叠「两个数叶子」这一层以及可递归折叠的子树——
        保守策略：不认识的一律返回 None，交给正常编译路径。
        收益：`81000 / 1800`、`2 * 3 + 1` 这类不再每次运行都算一遍。
        """
        if not isinstance(e, BinOp):
            return None
        if e.op not in ("+", "-", "*", "/", "%"):
            return None

        def lit(x: Expr):
            """递归求常量值：既认字面量，也认可折叠的子树。"""
            if isinstance(x, Num):
                return x.value
            if isinstance(x, UnOp) and x.op == "-":
                inner = lit(x.operand)
                return None if inner is None else -inner
            if isinstance(x, BinOp) and x.op in ("+", "-", "*", "/", "%"):
                return self._fold_const(x)      # 递归折叠子树
            return None

        lv, rv = lit(e.left), lit(e.right)
        if lv is None or rv is None:
            return None
        try:
            if e.op == "+": v = lv + rv
            elif e.op == "-": v = lv - rv
            elif e.op == "*": v = lv * rv
            elif e.op == "/":
                if rv == 0:
                    return None     # 除零留给运行时，保持错误行为一致
                v = lv / rv
            else:
                if rv == 0:
                    return None
                v = math.fmod(lv, rv)
        except Exception:
            return None
        return v

    def emit_expr(self, e: Expr) -> None:
        b = self.cur

        # 常量折叠：先于所有分支尝试（折叠成功只需一条 PUSH_NUM）
        folded = self._fold_const(e)
        if folded is not None:
            b.emit(Op.PUSH_NUM, folded)
            return

        if isinstance(e, Num):
            b.emit(Op.PUSH_NUM, e.value)
            return

        if isinstance(e, Text):
            b.emit(Op.PUSH_CONST, b.text(e.value))
            return

        if isinstance(e, Bool):
            b.emit(Op.PUSH_TRUE if e.value else Op.PUSH_FALSE)
            return

        if isinstance(e, Nil):
            b.emit(Op.PUSH_NIL)
            return

        if isinstance(e, VarRef):
            if e.depth == -3:          # 闭包捕获的自由变量
                b.emit(Op.LOAD_FREE, e.slot)
                return
            if e.depth == -2:
                b.emit(Op.LOAD_GLOBAL, e.slot)
            elif e.slot < 0:
                raise CompileError(
                    f"{e.line}:{e.col} 内部错误：变量 {e.name} 未分配槽位"
                    f"（作用域解析与代码生成不一致）")
            else:
                b.emit(Op.LOAD_LOCAL, e.slot)
            return

        if isinstance(e, Lambda):
            fi = self._lift_lambda(e)
            frees = getattr(e, "free_vars", None) or []
            if frees:
                # 按序把外层值压栈，再构造闭包（按值捕获）
                for nm, _ty, slot in frees:
                    b.emit(Op.LOAD_LOCAL, slot)
                b.emit(Op.MAKE_CLOSURE, fi, len(frees))
            else:
                b.emit(Op.MAKE_FUNC, fi)
            return

        if isinstance(e, Index):
            # 超指令：c[i] 直接读槽取元素。
            # 只在「容器静态类型是序列」时启用——文本与映射的取下标语义
            # 与序列不同，不能合并。
            is_seq = (not _NO_SUPER) and (e.base.ty is not None and e.base.ty.name == "序列")
            if is_seq and isinstance(e.base, VarRef) and e.base.slot >= 0 \
                    and e.base.depth >= 0:
                islot = self._local_slot_of(e.index)
                if islot is not None:
                    b.emit(Op.SLOT_INDEX_SLOT, e.base.slot, islot)
                    return
                # 常量下标：编译期取首元素等常见写法
                if isinstance(e.index, Num) and e.index.value == int(e.index.value) \
                        and 0 <= e.index.value < 1024:
                    b.emit(Op.SLOT_INDEX_IMM, e.base.slot, float(e.index.value))
                    return
            self.emit_expr(e.base)
            self.emit_expr(e.index)
            b.emit(Op.INDEX_GET)
            return

        if isinstance(e, Field):
            # `模块.常量`：拼出带点的名字直接读全局槽。
            # 不这样处理会先 emit_expr(模块别名)，而别名不是变量，报未分配槽位。
            dotted = self._dotted(e)
            if dotted is not None and dotted in self.global_slots:
                b.emit(Op.LOAD_GLOBAL, self.global_slots[dotted])
                return
            self.emit_expr(e.obj)
            b.emit(Op.FIELD_GET, b.text(e.name))
            return

        if isinstance(e, BinOp):
            # 短路求值：并且/或者 必须编译成跳转，而不是两侧都求值
            if e.op == "并且":
                self.emit_expr(e.left)
                j_false = b.emit(Op.JMP_IFNOT, 0)
                self.emit_expr(e.right)
                j_end = b.emit(Op.JMP, 0)
                b.patch_jump(j_false)
                b.emit(Op.PUSH_FALSE)
                b.patch_jump(j_end)
                return
            if e.op == "或者":
                self.emit_expr(e.left)
                j_true = b.emit(Op.JMP_IF, 0)
                self.emit_expr(e.right)
                j_end = b.emit(Op.JMP, 0)
                b.patch_jump(j_true)
                b.emit(Op.PUSH_TRUE)
                b.patch_jump(j_end)
                return
            self.emit_expr(e.left)
            self.emit_expr(e.right)
            # 文本参与 + → CONCAT（VM 侧会做格式化）
            if e.op == "+" and e.ty is not None and e.ty.name == "文本":
                b.emit(Op.CONCAT)
                return
            if e.op == "+" and e.left.ty.name == "序列":
                b.emit(Op.ADD)
                return
            # 数值特化：两侧静态类型都是「数」时，用免判型的专用指令。
            # 这是纯解释器最划算的优化——热路径省掉两次 tag 判断。
            both_num = (e.left.ty is not None and e.right.ty is not None
                        and e.left.ty.name == "数" and e.right.ty.name == "数")
            if both_num:
                num_op = {"+": Op.ADD_NUM, "-": Op.SUB_NUM, "*": Op.MUL_NUM,
                          "/": Op.DIV_NUM, "%": Op.MOD_NUM,
                          "==": Op.EQ_NUM, "!=": Op.NE_NUM,
                          "<": Op.LT_NUM, "<=": Op.LE_NUM,
                          ">": Op.GT_NUM, ">=": Op.GE_NUM}.get(e.op)
                if num_op is not None:
                    b.emit(num_op)
                    return

            # 比较超指令：x OP 常量（循环条件的高频形态）
            if (not _NO_SUPER) and isinstance(e.right, Num) and isinstance(e.left, VarRef) \
                    and e.left.slot >= 0 and e.left.depth >= 0 \
                    and e.left.ty is not None and e.left.ty.name == "数":
                cmp_ops = {">": Op.GT_SLOT_IMM, "<=": Op.LE_SLOT_IMM,
                           ">=": Op.GE_SLOT_IMM, "==": Op.EQ_SLOT_IMM,
                           "!=": Op.NE_SLOT_IMM}
                if e.op in cmp_ops:
                    b.emit(cmp_ops[e.op], e.left.slot, float(e.right.value))
                    return

            op = {"+": Op.ADD, "-": Op.SUB, "*": Op.MUL, "/": Op.DIV, "%": Op.MOD,
                  "==": Op.EQ, "!=": Op.NE, "<": Op.LT, "<=": Op.LE,
                  ">": Op.GT, ">=": Op.GE}.get(e.op)
            if op is None:
                raise CompileError(f"{e.line}:{e.col} 不支持运算符 {e.op}")
            b.emit(op)
            return

        if isinstance(e, UnOp):
            self.emit_expr(e.operand)
            b.emit(Op.NEG if e.op == "-" else Op.NOT)
            return

        if isinstance(e, Call):
            self.emit_call(e)
            return

        if isinstance(e, Await):
            inner = e.call
            for a in inner.args:
                self.emit_expr(a)
            # 等待完成 的结果直接进栈，不需要信箱槽位；-1 表示无信箱
            b.emit(Op.AWAIT, inner.target, len(inner.args), 0xFFFF)
            return

        if isinstance(e, SeqLit):
            for it in e.items:
                self.emit_expr(it)
            b.emit(Op.MAKE_SEQ, len(e.items))
            return

        if isinstance(e, MapLit):
            for k, v in e.pairs:
                self.emit_expr(k)
                self.emit_expr(v)
            b.emit(Op.MAKE_MAP, len(e.pairs))
            return

        raise CompileError(f"{e.line}:{e.col} 不支持的表达式 {type(e).__name__}")

    def _local_slot_of(self, e: Expr):
        """若 e 是当前帧内的局部变量引用，返回其槽位；否则 None。"""
        if isinstance(e, VarRef) and e.slot >= 0 and e.depth >= 0:
            if e.ty is not None and e.ty.name == "数":
                return e.slot
        return None

    def _lift_lambda(self, e: Lambda) -> int:
        """把匿名函数提升为独立函数，返回其函数表下标。"""
        if e.fn_index >= 0:
            return e.fn_index
        name = f"<匿名#{len(self.prog.funcs)}>"
        fn = Func(name=name,
                  params=list(e.params),
                  ret=e.ret_ty if e.ret_ty is not None else T_ANY,
                  errors=[],
                  contract=None,
                  body=[Return(e.body.line, e.body.col, e.body)],
                  line=e.line, col=e.col)
        fn.n_slots = max(getattr(e, "n_slots", 0), len(e.params))
        # 只登记；函数体由 compile() 的统一循环编译（可能继续派生新 lambda）
        e.fn_index = len(self.prog.funcs)
        self.prog.funcs.append(fn)
        return e.fn_index

    def emit_call(self, e: Call) -> None:
        b = self.cur

        # 高阶函数：VM 内部循环调用回调（避免在 C 标准库层回调衡语函数）
        HO = {"表.映射": 0, "表.过滤": 1, "表.折叠": 2}
        ho_name = self._dotted(e.callee)
        if ho_name in HO and len(e.args) >= 2:
            self.emit_expr(e.args[0])
            self.emit_expr(e.args[1])
            if ho_name == "表.折叠" and len(e.args) >= 3:
                self.emit_expr(e.args[2])
                b.emit(Op.HIGHER_ORDER, HO[ho_name], 3)
            else:
                b.emit(Op.HIGHER_ORDER, HO[ho_name], 2)
            return

        # 调用变量里存的匿名函数：f(2)
        if (getattr(e, "call_value", False) and isinstance(e.callee, VarRef)
                and e.callee.slot >= 0 and e.callee.depth >= 0):
            for a in e.args:
                self.emit_expr(a)
            b.emit(Op.CALL_VALUE, e.callee.slot, len(e.args))
            return

        # 内置函数
        if isinstance(e.callee, VarRef) and e.callee.name in BUILTINS:
            code = {"化成文": 0, "化为数": 1, "长度": 2}[e.callee.name]
            for a in e.args:
                self.emit_expr(a)
            # 化成文 需要单位：量纲在编译期已消除，运行期只能靠这里带过去
            unit_idx = 0
            if code == 0 and e.args and e.args[0].ty is not None:
                unit_idx = b.text(self._unit_label(e.args[0].ty))
            b.emit(Op.BUILTIN, code, len(e.args), unit_idx)
            return

        # 用户函数
        if e.target >= 0 and isinstance(e.callee, VarRef) and e.callee.name in \
                {f.name for f in self.prog.funcs}:
            for a in e.args:
                self.emit_expr(a)
            b.emit(Op.CALL, e.target, len(e.args))
            return

        # 记录构造 / 模块函数
        name = self._dotted(e.callee)
        # 跨模块调用：`模.翻倍(...)` 会被解析成 Field，检查器已按
        # 「模.翻倍」查到函数，但 e.target 只在 VarRef 情形下被设置，
        # 所以这里按名字再查一次函数表。
        if name is not None and isinstance(e.callee, Field):
            for i, fn in enumerate(self.prog.funcs):
                if fn.name == name:
                    for a in e.args:
                        self.emit_expr(a)
                    b.emit(Op.CALL, i, len(e.args))
                    return
        if name in self.rec_index:
            for a in e.args:
                self.emit_expr(a)
            b.emit(Op.MAKE_REC, self.rec_index[name], len(e.args))
            return

        # FFI 外部模块：别名.函数
        if name is not None:
            mod = name.split(".", 1)[0]
            if mod in self.host_aliases:
                for a in e.args:
                    self.emit_expr(a)
                b.emit(Op.CAP_CHECK, b.text(f"外部.调用{mod}"))
                # FFI：直接调用 C 动态库符号（dlopen/dlsym），不依赖任何宿主语言
                self._register_ffi(mod, name)
                b.emit(Op.CALL_FFI, b.text(name), len(e.args))
                return

        # 标准库
        if name is not None:
            from .stdlib2 import lookup
            entry = lookup(name)
            if entry is not None:
                for a in e.args:
                    self.emit_expr(a)
                if entry.cap:
                    b.emit(Op.CAP_CHECK, b.text(entry.cap))
                b.emit(Op.CALL_STD, b.text(name), len(e.args))
                return

        # 能力调用（工具.查询 / 文件.读取 / 网络.请求 …）
        if name is not None and name in KNOWN_CAPABILITIES:
            for a in e.args:
                self.emit_expr(a)
            b.emit(Op.CAP_CHECK, b.text(name))
            b.emit(Op.CALL_TOOL, b.text(name), len(e.args))
            return

        raise CompileError(f"{e.line}:{e.col} 未知调用：{name}")

    # ============================================================
    # 辅助
    # ============================================================
    def _register_ffi(self, alias: str, full_name: str) -> None:
        """FFI 库表由 api2 统一写入模块（别名 → 动态库名），这里无需额外处理。"""

    def _mailbox_slot(self, func_name: str) -> int:
        """信箱变量的槽位：编译期按 <做法名>_信箱 查已分配槽位。"""
        name = func_name if func_name.endswith("_信箱") else f"{func_name}_信箱"
        slot = self._named_slots.get(name)
        if slot is None:
            raise CompileError(f"内部错误：信箱变量 {name} 未分配槽位")
        return slot

    def _push_caps(self) -> None:
        self._cap_scopes.append(set())

    def _pop_caps(self) -> None:
        self._cap_scopes.pop()

    def _tmp_slot(self) -> int:
        """分配一个编译期临时槽位（重复/每个 的隐藏计数器等）。"""
        s = self._tmp_base + self._tmp_used
        self._tmp_used += 1
        return s

    def _unit_label(self, t: Type) -> str:
        if t is not None and t.name == "数" and not t.unit.is_none:
            return str(t.unit)
        return ""

    @staticmethod
    def _dotted(e: Expr) -> Optional[str]:
        parts: list[str] = []
        cur = e
        while isinstance(cur, Field):
            parts.append(cur.name)
            cur = cur.obj
        if not isinstance(cur, VarRef):
            return None
        parts.append(cur.name)
        return ".".join(reversed(parts))
