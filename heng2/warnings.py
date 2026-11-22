"""警告检查：能跑但可能有隐患的代码。

主流编译器（rustc / clang）都有分级的警告系统。衡语之前只有错误，
「变量声明了没用」「返回后面还有语句」这类问题完全不提示。

这里只做三条最常见的，宁少勿滥——警告太多会被忽略。
"""

from __future__ import annotations

from dataclasses import dataclass

from .ast2 import (
    Program, Func, Let, SetVar, SetIndex, SetField, If, Loop, Repeat, Each,
    Return, Assert, ExprStmt, VarRef, Lambda, BinOp, UnOp, Call, Index, Field,
    SeqLit, MapLit, Await, Text, Num, Bool, Nil,
)


@dataclass
class Warning_:
    code: str
    message: str
    line: int
    col: int
    help: str = ""


def _walk_expr(e, out: list) -> None:
    """收集表达式里所有 VarRef 的名字（含嵌套）。"""
    if e is None:
        return
    if isinstance(e, VarRef):
        out.append(e)
    elif isinstance(e, Lambda):
        _walk_expr(e.body, out)
    elif isinstance(e, BinOp):
        _walk_expr(e.left, out)
        _walk_expr(e.right, out)
    elif isinstance(e, UnOp):
        _walk_expr(e.operand, out)
    elif isinstance(e, Call):
        _walk_expr(e.callee, out)
        for a in e.args:
            _walk_expr(a, out)
        for _k, v in (e.kwargs or []):
            _walk_expr(v, out)
    elif isinstance(e, Index):
        _walk_expr(e.base, out)
        _walk_expr(e.index, out)
    elif isinstance(e, Field):
        _walk_expr(e.obj, out)
    elif isinstance(e, SeqLit):
        for it in e.items:
            _walk_expr(it, out)
    elif isinstance(e, MapLit):
        for k, v in e.pairs:
            _walk_expr(k, out)
            _walk_expr(v, out)
    elif isinstance(e, Await):
        _walk_expr(e.call, out)


def _walk_stmts(stmts: list, used: list, lets: list,
                unreachable: list, exprs: list) -> None:
    dead = False
    for s in stmts:
        if dead:
            unreachable.append(s)
            continue
        if isinstance(s, Let):
            lets.append(s)
            _walk_expr(s.init, used)
        elif isinstance(s, SetVar):
            _walk_expr(s.value, used)
            exprs.append(s)
        elif isinstance(s, SetIndex):
            _walk_expr(s.base, used)
            _walk_expr(s.index, used)
            _walk_expr(s.value, used)
        elif isinstance(s, SetField):
            _walk_expr(s.base, used)
            _walk_expr(s.value, used)
        elif isinstance(s, If):
            _walk_expr(s.cond, used)
            _walk_stmts(s.then_body, used, lets, unreachable, exprs)
            _walk_stmts(s.else_body or [], used, lets, unreachable, exprs)
        elif isinstance(s, Loop):
            _walk_expr(s.cond, used)
            _walk_stmts(s.body, used, lets, unreachable, exprs)
        elif isinstance(s, Repeat):
            _walk_expr(s.count, used)
            _walk_stmts(s.body, used, lets, unreachable, exprs)
        elif isinstance(s, Each):
            _walk_expr(s.iterable, used)
            _walk_stmts(s.body, used, lets, unreachable, exprs)
        elif isinstance(s, Return):
            _walk_expr(s.value, used)
            dead = True
        elif isinstance(s, Assert):
            _walk_expr(s.cond, used)
            _walk_expr(getattr(s, "message", None), used)
        elif isinstance(s, ExprStmt):
            _walk_expr(s.expr, used)
        else:
            for attr in ("value", "cond", "count", "iterable"):
                v = getattr(s, attr, None)
                if isinstance(v, object) and v is not None:
                    _walk_expr(v, used)


def check(prog: Program) -> list[Warning_]:
    warns: list[Warning_] = []

    blocks = []
    if prog.entry:
        blocks.append(("入口", prog.entry))
    for fn in prog.funcs:
        blocks.append((fn.name, fn.body))

    for name, body in blocks:
        used: list = []
        lets: list = []
        unreachable: list = []
        exprs: list = []
        _walk_stmts(body, used, lets, unreachable, exprs)

        used_names = {v.name for v in used}
        # 声明后从未被读的变量（名字以下划线开头视为有意忽略）
        for let in lets:
            if let.name.startswith("_"):
                continue
            if let.name not in used_names:
                warns.append(Warning_(
                    "W01", f"变量 {let.name} 声明后未使用",
                    let.line, let.col,
                    "删掉这个变量，或在名字前加下划线表示有意保留"))

        for s in unreachable:
            warns.append(Warning_(
                "W02", "该语句永远不会执行",
                getattr(s, "line", 0), getattr(s, "col", 0),
                "它在「返回」之后；检查是否应该提前返回"))

    # 重复的映射键字面量
    for name, body in blocks:
        _check_dup_map_keys(body, warns)
    return warns


def _check_dup_map_keys(stmts: list, warns: list) -> None:
    """映射字面量里重复的常量键：后一个会覆盖前一个。"""
    for s in stmts:
        for attr in ("init", "value", "cond", "count", "iterable", "expr"):
            e = getattr(s, attr, None)
            if isinstance(e, MapLit):
                seen: dict = {}
                for k, _v in e.pairs:
                    if isinstance(k, Text) or isinstance(k, Num):
                        key = k.value
                        if key in seen:
                            warns.append(Warning_(
                                "W03", f"映射键 {key!r} 重复，后一个会覆盖前一个",
                                getattr(k, "line", 0), getattr(k, "col", 0),
                                "删掉重复的键，或确认这是有意覆盖"))
                        seen[key] = True
        for attr in ("then_body", "else_body", "body"):
            sub = getattr(s, attr, None)
            if isinstance(sub, list):
                _check_dup_map_keys(sub, warns)
