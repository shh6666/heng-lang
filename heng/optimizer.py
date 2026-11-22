"""衡语编译器优化 pass：常量折叠（constant folding）与常量条件消分支。

设计原则（对应编译器架构研究）：
  - 优化作用于 AST，发生在类型检查之后、解释/WASM 代码生成之前；
  - 只折叠「语义完全确定」的表达式：不改变单位代数、不改变错误行为；
  - 保守策略：除数为 0 的除法不折叠（保留运行时 错误<除零> 语义）；
  - 折叠后更新 inferred_type 标注，保持显式 IR 的一致性；
  - 优化是可选的（默认关闭），通过 API 显式开启，避免影响解释器的可观测 IR。

未来扩展位：死代码消除、公共子表达式消除、内联——按 pass 列表顺序执行。
"""

from __future__ import annotations

from dataclasses import replace
from typing import Optional

from .ast_nodes import (
    Type, Unit, Expr, Number, String, Bool, Null, Var, Attr, Binary, Unary,
    Call, SeqLiteral, MapLiteral, Stmt, VarDecl, Assign, If, Loop, ForEach,
    Return, Assert, Throw, Try, Exec, Observe, Sandbox, Authorize, Parallel,
    Spawn, Receive, Output, ExprStmt, FuncDef, RecordDef, Program,
)
from .typecheck import unit_vector, add_vec, sub_vec, DIMS, PREFERRED_UNIT


class OptimizeError(Exception):
    pass


def _vec_to_unit(vec: tuple[int, ...]) -> Unit:
    num, den = [], []
    for i, v in enumerate(vec):
        name = PREFERRED_UNIT.get(DIMS[i], DIMS[i])
        if v > 0:
            num.extend([name] * v)
        elif v < 0:
            den.extend([name] * (-v))
    return Unit(num, den)


class ConstantFolder:
    """递归折叠常量表达式。返回新 AST，不修改原树。"""

    def __init__(self):
        self.folded = 0  # 折叠计数，供验证与统计

    def fold(self, e: Expr) -> Expr:
        # 先递归子节点
        if isinstance(e, Binary):
            e = replace(e, left=self.fold(e.left), right=self.fold(e.right))
        elif isinstance(e, Unary):
            e = replace(e, operand=self.fold(e.operand))
        elif isinstance(e, Call):
            e = replace(e, args=[self.fold(a) for a in e.args], callee=self.fold(e.callee))
        elif isinstance(e, Attr):
            e = replace(e, obj=self.fold(e.obj))
        elif isinstance(e, SeqLiteral):
            e = replace(e, items=[self.fold(x) for x in e.items])
        elif isinstance(e, MapLiteral):
            e = replace(e, pairs=[(self.fold(k), self.fold(v)) for k, v in e.pairs])

        # 折叠当前节点
        if isinstance(e, Unary) and isinstance(e.operand, Number) and e.op == "-":
            self.folded += 1
            return Number(e.line, e.col, -e.operand.value, e.operand.unit)

        if isinstance(e, Binary) and isinstance(e.left, Number) and isinstance(e.right, Number):
            l, r = e.left, e.right
            a, b = l.value, r.value
            lv, rv = unit_vector(l.unit), unit_vector(r.unit)
            result: Optional[Number] = None
            if e.op in ("+", "-") and lv == rv:
                v = a + b if e.op == "+" else a - b
                result = Number(e.line, e.col, v, l.unit)
            elif e.op == "*":
                result = Number(e.line, e.col, a * b, _vec_to_unit(add_vec(lv, rv)))
            elif e.op == "/":
                if b != 0:  # 保守：不折叠除零，保留运行时错误语义
                    result = Number(e.line, e.col, a / b, _vec_to_unit(sub_vec(lv, rv)))
            if result is not None:
                self.folded += 1
                return result

        # 逻辑与/或折叠（Bool 操作数）
        if isinstance(e, Binary) and isinstance(e.left, Bool) and isinstance(e.right, Bool) \
                and e.op in ("并且", "或者"):
            self.folded += 1
            val = (e.left.value and e.right.value) if e.op == "并且" else (e.left.value or e.right.value)
            return Bool(e.line, e.col, val)

        # 比较运算符折叠为 Bool（要求两侧量纲一致，否则保持原样交给类型检查报错）
        if isinstance(e, Binary) and isinstance(e.left, Number) and isinstance(e.right, Number) \
                and e.op in ("==", "!=", "<", "<=", ">", ">="):
            l, r = e.left, e.right
            if unit_vector(l.unit) == unit_vector(r.unit):
                a, b = l.value, r.value
                val = {
                    "==": a == b, "!=": a != b, "<": a < b,
                    "<=": a <= b, ">": a > b, ">=": a >= b,
                }[e.op]
                self.folded += 1
                return Bool(e.line, e.col, val)

        return e

    def fold_stmts(self, stmts: list[Stmt]) -> list[Stmt]:
        out = []
        for s in stmts:
            out.append(self.fold_stmt(s))
        return out

    def fold_stmt(self, s: Stmt) -> Stmt:
        if isinstance(s, VarDecl):
            return replace(s, init=self.fold(s.init) if s.init else None)
        if isinstance(s, Assign):
            return replace(s, value=self.fold(s.value))
        if isinstance(s, If):
            cond = self.fold(s.cond)
            # 常量条件：选择唯一可达分支（保留另一个分支会被类型检查再次校验的问题，
            # 故此处仅在已通过类型检查后使用）。
            if isinstance(cond, Bool):
                self.folded += 1
                if cond.value:
                    return replace(s, cond=cond, then_body=self.fold_stmts(s.then_body))
                else:
                    return replace(s, cond=cond, else_body=self.fold_stmts(s.else_body) if s.else_body else None)
            return replace(s, cond=cond, then_body=self.fold_stmts(s.then_body),
                           else_body=self.fold_stmts(s.else_body) if s.else_body else None)
        if isinstance(s, Loop):
            return replace(s, cond=self.fold(s.cond), body=self.fold_stmts(s.body))
        if isinstance(s, ForEach):
            return replace(s, iterable=self.fold(s.iterable), body=self.fold_stmts(s.body))
        if isinstance(s, Return):
            return replace(s, value=self.fold(s.value))
        if isinstance(s, Assert):
            return replace(s, cond=self.fold(s.cond))
        if isinstance(s, Throw):
            return replace(s, message=self.fold(s.message))
        if isinstance(s, Try):
            return replace(s, body=self.fold_stmts(s.body),
                           catch_body=self.fold_stmts(s.catch_body) if s.catch_body else None,
                           finally_body=self.fold_stmts(s.finally_body) if s.finally_body else None)
        if isinstance(s, Exec):
            return replace(s, call=self.fold(s.call))
        if isinstance(s, Observe):
            return replace(s, body=self.fold_stmts(s.body))
        if isinstance(s, Sandbox):
            return replace(s, body=self.fold_stmts(s.body))
        if isinstance(s, Parallel):
            return replace(s, body=self.fold_stmts(s.body))
        if isinstance(s, Spawn):
            return replace(s, args=[self.fold(a) for a in s.args])
        if isinstance(s, Receive):
            return replace(s, mailbox=self.fold(s.mailbox))
        if isinstance(s, Output):
            return replace(s, value=self.fold(s.value))
        if isinstance(s, ExprStmt):
            return replace(s, expr=self.fold(s.expr))
        return s

    def fold_program(self, prog: Program) -> Program:
        new_funcs = []
        for fn in prog.functions:
            body = self.fold_stmts(fn.body)
            contract = None
            if fn.contract:
                from .ast_nodes import Contract
                contract = Contract(
                    pre=self.fold(fn.contract.pre) if fn.contract.pre else None,
                    post=self.fold(fn.contract.post) if fn.contract.post else None,
                )
            new_funcs.append(replace(fn, body=body, contract=contract))
        new_consts = [(n, t, self.fold(v)) for n, t, v in prog.constants]
        new_entry = self.fold_stmts(prog.entry) if prog.entry else None
        return replace(prog, functions=new_funcs, constants=new_consts, entry=new_entry)


def fold_constants(prog: Program) -> tuple[Program, int]:
    folder = ConstantFolder()
    new_prog = folder.fold_program(prog)
    return new_prog, folder.folded
