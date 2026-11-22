"""衡语内核 v2 —— AST 定义。

设计要点（与旧实现的关键差异）：
  1. 变量引用 Var 携带 `depth`：编译期由作用域分析填入「第几层作用域」，
     代码生成据此直接算出槽位下标，运行期不做名字查找。
  2. 每个表达式带 `ty`：检查层回填类型（含量纲），代码生成据此决定指令。
  3. 不存在「运行期值携带单位」这类设计——单位只在 Type 里。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .types2 import Type, Unit, EMPTY_UNIT, T_UNKNOWN


# ============================================================
# 表达式
# ============================================================

@dataclass(slots=True)
class Expr:
    line: int = 0
    col: int = 0
    ty: Type = field(default=T_UNKNOWN, kw_only=True)


@dataclass(slots=True)
class Num(Expr):
    value: float = 0.0
    unit: Unit = EMPTY_UNIT


@dataclass(slots=True)
class Text(Expr):
    value: str = ""


@dataclass(slots=True)
class Bool(Expr):
    value: bool = False


@dataclass(slots=True)
class Nil(Expr):
    pass


@dataclass(slots=True)
class VarRef(Expr):
    name: str = ""
    depth: int = -1        # 编译期回填：0=当前作用域，1=外层…
    slot: int = -1         # 编译期回填：局部槽位下标（-1 表示全局）


@dataclass(slots=True)
class Field(Expr):
    obj: Expr = None
    name: str = ""


@dataclass(slots=True)
class BinOp(Expr):
    op: str = ""
    left: Expr = None
    right: Expr = None


@dataclass(slots=True)
class UnOp(Expr):
    op: str = ""
    operand: Expr = None


@dataclass(slots=True)
class Call(Expr):
    callee: Expr = None
    args: list[Expr] = field(default_factory=list)
    kwargs: list[tuple[str, Expr]] = field(default_factory=list)
    # 编译期回填：直接调用某函数的索引（-1 = 需动态派发，如 FFI / 标准库）
    target: int = -1
    # 是否为「调用变量里存的函数」（f(2) 其中 f 是匿名函数）
    call_value: bool = False


@dataclass(slots=True)
class Await(Expr):
    """等待完成 做法(参数…) —— 发起并立即等待，返回值即表达式值。"""
    call: Expr = None
    target: int = -1          # 编译期回填：函数索引


@dataclass(slots=True)
class Lambda(Expr):
    """匿名函数：做法(x : 数) -> 数 => 表达式。

    编译期提升为独立函数，`Lambda.fn_index` 记录其函数表下标。
    """
    params: list = None
    body: Expr = None
    ret_ty: object = None
    fn_index: int = -1
    n_slots: int = 0
    # 捕获的自由变量：(名字, 外层槽位)。创建时按值复制进闭包数组。
    free_vars: list = None


@dataclass(slots=True)
class Index(Expr):
    """下标访问：seq[i] / map[k]。"""
    base: Expr = None
    index: Expr = None


@dataclass(slots=True)
class SeqLit(Expr):
    items: list[Expr] = field(default_factory=list)


@dataclass(slots=True)
class MapLit(Expr):
    pairs: list[tuple[Expr, Expr]] = field(default_factory=list)


# ============================================================
# 语句
# ============================================================

@dataclass(slots=True)
class Stmt:
    line: int = 0
    col: int = 0


@dataclass(slots=True)
class Let(Stmt):
    name: str = ""
    declared: Type = None
    init: Optional[Expr] = None
    is_const: bool = False
    slot: int = -1          # 编译期回填：局部槽位
    ty: Type = None         # 编译期回填：变量最终类型


@dataclass(slots=True)
class SetField(Stmt):
    """记录字段赋值：将 v 放到 r.字段。"""
    base: Expr = None
    name: str = ""
    value: Expr = None
    ty: object = None


@dataclass(slots=True)
class SetIndex(Stmt):
    """将 值 放到 容器[下标]。"""
    base: Expr = None
    index: Expr = None
    value: Expr = None


@dataclass(slots=True)
class SetVar(Stmt):
    target: str = ""
    value: Expr = None
    depth: int = -1
    slot: int = -1
    ty: Type = None


@dataclass(slots=True)
class If(Stmt):
    cond: Expr = None
    then_body: list[Stmt] = field(default_factory=list)
    else_body: Optional[list[Stmt]] = None


@dataclass(slots=True)
class Loop(Stmt):
    cond: Expr = None
    body: list[Stmt] = field(default_factory=list)


@dataclass(slots=True)
class Repeat(Stmt):
    times: Expr = None
    body: list[Stmt] = field(default_factory=list)


@dataclass(slots=True)
class Each(Stmt):
    iterable: Expr = None
    var: str = ""
    body: list[Stmt] = field(default_factory=list)
    # 编译期回填：迭代变量的槽位与元素类型
    slot: int = -1
    elem_ty: Type = None


@dataclass(slots=True)
class Return(Stmt):
    value: Optional[Expr] = None


@dataclass(slots=True)
class Assert(Stmt):
    cond: Expr = None


@dataclass(slots=True)
class Raise(Stmt):
    error_type: str = ""
    message: Expr = None


@dataclass(slots=True)
class Try(Stmt):
    body: list[Stmt] = field(default_factory=list)
    catch_type: Optional[str] = None
    catch_var: Optional[str] = None
    catch_body: Optional[list[Stmt]] = None
    finally_body: Optional[list[Stmt]] = None
    catch_slot: int = -1


@dataclass(slots=True)
class Say(Stmt):
    value: Expr = None


@dataclass(slots=True)
class Exec(Stmt):
    name: str = ""
    call: Expr = None
    slot: int = -1          # 编译期回填：结果变量槽位
    ty: Type = None


@dataclass(slots=True)
class Observe(Stmt):
    name: str = ""
    body: list[Stmt] = field(default_factory=list)
    slot: int = -1


@dataclass(slots=True)
class Sandbox(Stmt):
    body: list[Stmt] = field(default_factory=list)


@dataclass(slots=True)
class Authorize(Stmt):
    capabilities: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Parallel(Stmt):
    body: list[Stmt] = field(default_factory=list)
    group_id: int = -1
    n_tasks: int = 0          # 编译期回填：块内 发起 的数量


@dataclass(slots=True)
class Spawn(Stmt):
    func_name: str = ""
    args: list[Expr] = field(default_factory=list)
    target: int = -1
    group_id: int = -1        # 编译期回填：所属并发块 id（JOIN 用）


@dataclass(slots=True)
class Receive(Stmt):
    mailbox: Expr = None
    target: Optional[str] = None
    target_slot: int = -1


@dataclass(slots=True)
class ExprStmt(Stmt):
    expr: Expr = None


# ============================================================
# 顶层
# ============================================================

@dataclass(slots=True)
class Contract:
    require: Optional[Expr] = None
    ensure: Optional[Expr] = None


@dataclass(slots=True)
class Param:
    name: str
    type: Type


@dataclass(slots=True)
class Func:
    name: str
    params: list[Param]
    ret: Type
    errors: list[str]
    contract: Optional[Contract]
    body: list[Stmt]
    line: int = 0
    col: int = 0
    # 编译期回填
    index: int = -1
    n_slots: int = 0      # 含参数的局部槽位总数
    name_slots: dict = field(default_factory=dict)  # 名字 → 槽位快照
    type_params: list = field(default_factory=list)  # 泛型参数名，如 ["T"]


@dataclass(slots=True)
class Record:
    name: str
    fields: list[Param]
    line: int = 0
    col: int = 0
    index: int = -1


@dataclass(slots=True)
class Import:
    kind: str            # "模块" / "外部"
    path: str
    alias: str
    line: int = 0
    col: int = 0


@dataclass(slots=True)
class Program:
    imports: list[Import] = field(default_factory=list)
    consts: list[tuple[str, Type, Expr]] = field(default_factory=list)
    records: list[Record] = field(default_factory=list)
    funcs: list[Func] = field(default_factory=list)
    entry: Optional[list[Stmt]] = None
    # 编译期回填：入口局部槽位数、全局槽位数
    entry_slots: int = 0
    n_globals: int = 0
    entry_name_slots: dict = field(default_factory=dict)
