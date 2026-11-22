"""衡语 AST 节点定义。

AST 同时充当「显式 IR」的载体：每个节点携带类型、单位、错误签名等标注，
供类型检查器填充、解释器消费（对应 EBNF §6 三层管线）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


# ---------------- 类型 ----------------

@dataclass
class Type:
    name: str                      # 数/文本/真假/空/序列/映射/记录名
    unit: Optional["Unit"] = None  # 仅「数」携带单位
    elem: Optional["Type"] = None  # 序列元素类型
    key: Optional["Type"] = None   # 映射键类型
    val: Optional["Type"] = None   # 映射值类型

    def __str__(self) -> str:
        if self.name == "数" and self.unit:
            return f"数<{self.unit}>"
        if self.name == "序列":
            return f"序列<{self.elem}>"
        if self.name == "映射":
            return f"映射<{self.key},{self.val}>"
        return self.name

    @property
    def is_numeric(self) -> bool:
        return self.name == "数"


@dataclass
class Unit:
    """单位单项式：分子/分母单位名列表。"""
    numerator: list[str] = field(default_factory=list)
    denominator: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        num = "*".join(self.numerator) if self.numerator else "1"
        if self.denominator:
            return f"{num}/{'/'.join(self.denominator)}"
        return num

    def is_dimensionless(self) -> bool:
        return not self.numerator and not self.denominator


# ---------------- 表达式 ----------------

@dataclass
class Expr:
    line: int
    col: int
    inferred_type: Optional["Type"] = field(default=None, kw_only=True)  # 类型检查器填充（IR 标注）


@dataclass
class Number(Expr):
    value: float
    unit: Optional[Unit] = None


@dataclass
class String(Expr):
    value: str


@dataclass
class Bool(Expr):
    value: bool


@dataclass
class Null(Expr):
    pass


@dataclass
class Var(Expr):
    name: str


@dataclass
class Attr(Expr):
    obj: Expr
    name: str


@dataclass
class Binary(Expr):
    op: str
    left: Expr
    right: Expr


@dataclass
class Unary(Expr):
    op: str
    operand: Expr


@dataclass
class Call(Expr):
    callee: Expr
    args: list[Expr]
    # 关键字实参（FFI 用）：[(参数名, 表达式)]，与 args 并列
    kwargs: list[tuple[str, Expr]] = field(default_factory=list)


@dataclass
class SeqLiteral(Expr):
    items: list[Expr]


@dataclass
class MapLiteral(Expr):
    pairs: list[tuple[Expr, Expr]]


# ---------------- 语句 ----------------

@dataclass
class Stmt:
    line: int
    col: int


@dataclass
class VarDecl(Stmt):
    name: str
    declared_type: Type
    init: Optional[Expr] = None


@dataclass
class Assign(Stmt):
    target: str
    value: Expr


@dataclass
class If(Stmt):
    cond: Expr
    then_body: list[Stmt]
    else_body: Optional[list[Stmt]] = None


@dataclass
class Loop(Stmt):
    cond: Expr
    body: list[Stmt]


@dataclass
class Repeat(Stmt):
    """重复 N 次：面向零基础的自然计数循环。"""
    times: Expr
    body: list[Stmt]


@dataclass
class ForEach(Stmt):
    iterable: Expr
    var: str
    body: list[Stmt]


@dataclass
class Return(Stmt):
    value: Expr


@dataclass
class Assert(Stmt):
    cond: Expr


@dataclass
class Throw(Stmt):
    error_type: str
    message: Expr


@dataclass
class Try(Stmt):
    body: list[Stmt]
    catch_type: Optional[str] = None
    catch_var: Optional[str] = None
    catch_body: Optional[list[Stmt]] = None
    finally_body: Optional[list[Stmt]] = None


@dataclass
class Exec(Stmt):
    name: str
    call: Expr


@dataclass
class Observe(Stmt):
    name: str
    body: list[Stmt]


@dataclass
class Sandbox(Stmt):
    body: list[Stmt]


@dataclass
class Authorize(Stmt):
    capabilities: list[str]


@dataclass
class Parallel(Stmt):
    body: list[Stmt]


@dataclass
class Spawn(Stmt):
    func_name: str
    args: list[Expr]


@dataclass
class Receive(Stmt):
    mailbox: Expr
    target: Optional[str] = None


@dataclass
class Output(Stmt):
    value: Expr


@dataclass
class ExprStmt(Stmt):
    expr: Expr


@dataclass
class Contract:
    pre: Optional[Expr] = None
    post: Optional[Expr] = None


# ---------------- 顶层定义 ----------------

@dataclass
class Param:
    name: str
    type: Type


@dataclass
class FuncDef:
    name: str
    params: list[Param]
    ret_type: Type
    errors: list[str]
    contract: Optional[Contract]
    body: list[Stmt]
    line: int
    col: int


@dataclass
class RecordDef:
    name: str
    fields: list[Param]
    line: int
    col: int


@dataclass
class ImportSpec:
    """一条导入声明。

    kind:
      "模块"  —— 导入衡语源文件（.hng），path 为相对路径
      "外部"  —— FFI 导入宿主 Python 模块，path 为模块名
    alias: 本地使用的命名空间前缀（`导入 「工具.hng」之 工具` 中的「工具」）
    """
    kind: str
    path: str
    alias: str
    line: int
    col: int


@dataclass
class Program:
    imports: list[str] = field(default_factory=list)
    import_specs: list[ImportSpec] = field(default_factory=list)
    constants: list[tuple[str, Type, Expr]] = field(default_factory=list)
    records: list[RecordDef] = field(default_factory=list)
    functions: list[FuncDef] = field(default_factory=list)
    entry: Optional[list[Stmt]] = None
