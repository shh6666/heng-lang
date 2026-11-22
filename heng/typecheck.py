"""衡语类型检查器：静态类型 + 单位量纲代数 + 错误签名校验 + 能力检查。

约束落实：
  C7  量词单位类型系统 —— 维度向量加减乘除规则
  C8  错误签名显式化 —— 抛出必须属于签名集合
  C9  默认沙箱 + 显式授权 —— 能力白名单检查
  C3  标识符不得与关键字同形 —— 在命名检查中执行
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .ast_nodes import (
    Type, Unit, Expr, Number, String, Bool, Null, Var, Attr, Binary, Unary,
    Call, SeqLiteral, MapLiteral, Stmt, VarDecl, Assign, If, Loop, ForEach,
    Return, Assert, Throw, Try, Exec, Observe, Sandbox, Authorize, Parallel, Repeat,
    Spawn, Receive, Output, ExprStmt, Contract, FuncDef, RecordDef, Program,
)
from .lexer import KEYWORDS

# 维度向量：长度, 时间, 质量, 金额, 计数, 温度, 无量纲占位
DIMS = ("长度", "时间", "质量", "金额", "计数", "温度")
DIM_INDEX = {d: i for i, d in enumerate(DIMS)}

BASE_UNITS = {
    "米": "长度", "秒": "时间", "千克": "质量", "元": "金额",
    "个": "计数", "次": "计数", "摄氏度": "温度", "开尔文": "温度",
}

# 维度回推单位时的首选表示：同维度多个单位（如 个/次）取业务最常用者，
# 避免向量往返后 元/个 被显示成 元/次。
PREFERRED_UNIT = {
    "长度": "米", "时间": "秒", "质量": "千克", "金额": "元",
    "计数": "个", "温度": "摄氏度",
}

# 预定义错误类别（EBNF §5）
KNOWN_ERRORS = {"超时", "拒绝", "除零", "类型", "量纲", "未授权", "越界", "未定义", "断言"}

# 能力（EBNF §4）
KNOWN_CAPABILITIES = {
    "工具.查询", "工具.清理", "文件.读取", "文件.写入", "网络.请求", "输出",
}


class TypeError_(Exception):
    """衡语编译期类型错误（区别于 Python 内建 TypeError）。"""
    def __init__(self, msg: str, line: int = 0, col: int = 0):
        super().__init__(f"{line}:{col} 类型错误：{msg}" if line else f"类型错误：{msg}")
        self.line, self.col = line, col


def unit_vector(unit: Optional[Unit]) -> tuple[int, ...]:
    """把单位单项式化为维度指数向量。"""
    vec = [0] * len(DIMS)
    if unit is None:
        return tuple(vec)
    for u in unit.numerator:
        if u in BASE_UNITS:
            vec[DIM_INDEX[BASE_UNITS[u]]] += 1
        # 未知单位（如自定义）视为无量纲占位，不报错
    for u in unit.denominator:
        if u in BASE_UNITS:
            vec[DIM_INDEX[BASE_UNITS[u]]] -= 1
    return tuple(vec)


def add_vec(a: tuple[int, ...], b: tuple[int, ...]) -> tuple[int, ...]:
    return tuple(x + y for x, y in zip(a, b))


def sub_vec(a: tuple[int, ...], b: tuple[int, ...]) -> tuple[int, ...]:
    return tuple(x - y for x, y in zip(a, b))


def type_equal(a: Type, b: Type) -> bool:
    if a.name != b.name:
        return False
    if a.name == "数":
        return unit_vector(a.unit) == unit_vector(b.unit)
    if a.name == "序列":
        return type_equal(a.elem, b.elem) if a.elem and b.elem else False
    if a.name == "映射":
        return (type_equal(a.key, b.key) and type_equal(a.val, b.val)) if a.key and b.key and a.val and b.val else False
    return True


# ---- 标准库桥接（延迟导入 stdlib，避免解释器↔标准库循环依赖）----

def _stdlib_entry(name: str):
    from .stdlib import resolve
    return resolve(name)


def _stdlib_ret_type(kind: str, elem: str | None = None) -> Type:
    if kind == "数":
        return Type("数")
    if kind == "文本":
        return Type("文本")
    if kind == "真假":
        return Type("真假")
    if kind == "空":
        return Type("空")
    if kind == "序列":
        return Type("序列", elem=Type(elem) if elem else Type("任意"))
    if kind == "映射":
        return Type("映射", key=Type("文本"), val=Type("任意"))
    return Type("任意")


@dataclass
class Scope:
    vars: dict[str, Type]
    funcs: dict[str, FuncDef]
    records: dict[str, RecordDef]
    parent: Optional["Scope"] = None

    def lookup_var(self, name: str) -> Optional[Type]:
        if name in self.vars:
            return self.vars[name]
        return self.parent.lookup_var(name) if self.parent else None

    def lookup_func(self, name: str) -> Optional[FuncDef]:
        if name in self.funcs:
            return self.funcs[name]
        return self.parent.lookup_func(name) if self.parent else None

    def lookup_record(self, name: str) -> Optional[RecordDef]:
        if name in self.records:
            return self.records[name]
        return self.parent.lookup_record(name) if self.parent else None


class TypeChecker:
    def __init__(self, program: Program, host_aliases: set[str] | None = None):
        self.prog = program
        self.global_scope = Scope({}, {}, {})
        # FFI 导入的模块别名：这些名字下的调用返回「任意」，由宿主决定真实类型
        self.host_aliases = host_aliases or set()

    def collect_errors(self) -> "DiagnosticBag":
        """多错误收集模式：逐个函数/入口独立检查，遇错记录并继续。

        单遍 `check()` 遇第一个错误即抛，适合编译流水线；
        本方法供 LSP 与诊断报告使用，一次返回全部可发现问题。
        """
        from .diagnostics import DiagnosticBag
        bag = DiagnosticBag()

        # 命名冲突（C3）——可继续收集
        try:
            self._check_names()
        except TypeError_ as e:
            bag.error(str(e).split("类型错误：")[-1], e.line or 1, e.col or 1, "C3")

        # 注册符号（即使有错也尽量建立符号表，以便后续检查有依据）
        for name, typ, val in self.prog.constants:
            self.global_scope.vars[name] = typ
        for rec in self.prog.records:
            self.global_scope.records[rec.name] = rec
        for fn in self.prog.functions:
            self.global_scope.funcs[fn.name] = fn

        for name, typ, val in self.prog.constants:
            try:
                vt = self.infer(val, self.global_scope)
                if not self._assignable(typ, vt, val):
                    bag.error(f"常量 {name} 初值类型 {vt} 与声明 {typ} 不符", val.line, val.col, "常量类型")
            except TypeError_ as e:
                bag.error(str(e).split("类型错误：")[-1], e.line or 1, e.col or 1, "常量")

        for fn in self.prog.functions:
            try:
                self._check_function(fn)
            except TypeError_ as e:
                bag.error(str(e).split("类型错误：")[-1], e.line or 1, e.col or 1, "函数")

        if self.prog.entry:
            try:
                scope = Scope({}, {}, {}, self.global_scope)
                self.check_block(self.prog.entry, scope, None, set(), None)
            except TypeError_ as e:
                bag.error(str(e).split("类型错误：")[-1], e.line or 1, e.col or 1, "入口")

        return bag

    def check(self) -> None:
        # 标识符不得与关键字同形（C3）
        self._check_names()
        # 注册常量、记录、函数签名
        for name, typ, val in self.prog.constants:
            self.global_scope.vars[name] = typ
        for rec in self.prog.records:
            self.global_scope.records[rec.name] = rec
        for fn in self.prog.functions:
            self.global_scope.funcs[fn.name] = fn

        # 检查常量初值
        for name, typ, val in self.prog.constants:
            vt = self.infer(val, self.global_scope)
            if not self._assignable(typ, vt, val):
                raise TypeError_(f"常量 {name} 初值类型 {vt} 与声明 {typ} 不符", val.line, val.col)

        # 检查函数体
        for fn in self.prog.functions:
            self._check_function(fn)

        # 检查入口
        if self.prog.entry:
            scope = Scope({}, {}, {}, self.global_scope)
            self.check_block(self.prog.entry, scope, None, set(), None)

    # ---- 命名检查 ----
    def _check_names(self) -> None:
        names: list[tuple[str, int, int]] = []
        for c in self.prog.constants:
            names.append((c[0], 0, 0))
        for r in self.prog.records:
            names.append((r.name, r.line, r.col))
            for f in r.fields:
                names.append((f.name, r.line, r.col))
        for fn in self.prog.functions:
            names.append((fn.name, fn.line, fn.col))
            for p in fn.params:
                names.append((p.name, fn.line, fn.col))
        for name, line, col in names:
            if name in KEYWORDS:
                raise TypeError_(f"标识符 {name!r} 与关键字同形（C3 禁止）", line, col)

    def _assignable(self, declared: Type, actual: Type, node: Expr | None = None) -> bool:
        if actual.name == "任意":
            return True  # 动态值（工具结果属性）允许赋给任意声明类型
        # 空字面量多态：[] 与 {} 的元素类型未知，应允许赋给任何具体序列/映射类型，
        # 否则「变量 s : 序列<数> = []」这类正常写法被误拒。
        if node is not None:
            from .ast_nodes import SeqLiteral, MapLiteral
            if isinstance(node, SeqLiteral) and not node.items and declared.name == "序列" and actual.name == "序列":
                return True
            if isinstance(node, MapLiteral) and not node.pairs and declared.name == "映射" and actual.name == "映射":
                return True
        if not type_equal(declared, actual):
            return False
        return True

    # ---- 函数 ----
    def _check_function(self, fn: FuncDef) -> None:
        scope = Scope({}, {}, {}, self.global_scope)
        for p in fn.params:
            scope.vars[p.name] = p.type

        # 错误类别合法性
        for e in fn.errors:
            if e not in KNOWN_ERRORS:
                raise TypeError_(f"未定义的错误类别 {e!r}", fn.line, fn.col)

        # 规约前置/后置需为布尔
        if fn.contract and fn.contract.pre:
            pt = self.infer(fn.contract.pre, scope)
            if pt.name != "真假":
                raise TypeError_("前置条件必须为真假类型", fn.contract.pre.line, fn.contract.pre.col)
        if fn.contract and fn.contract.post:
            # 后置条件中的「结果」代表函数返回值
            scope.vars["结果"] = fn.ret_type
            pt = self.infer(fn.contract.post, scope)
            if pt.name != "真假":
                raise TypeError_("后置条件必须为真假类型", fn.contract.post.line, fn.contract.post.col)
            del scope.vars["结果"]

        self.check_block(fn.body, scope, fn, set(fn.errors))

    # ---- 语句块 ----
    def check_block(self, stmts: list[Stmt], scope: Scope,
                    fn: Optional[FuncDef], allowed_errors: set[str],
                    capabilities: Optional[set[str]] = None) -> None:
        caps = capabilities
        for s in stmts:
            self.check_stmt(s, scope, fn, allowed_errors, caps)

    def check_stmt(self, s: Stmt, scope: Scope, fn: Optional[FuncDef],
                   allowed_errors: set[str], caps: Optional[set[str]]) -> None:
        if isinstance(s, VarDecl):
            if s.name in scope.vars:
                raise TypeError_(f"禁止同名遮蔽：变量 {s.name} 已定义（C7 冗余检错）", s.line, s.col)
            if s.init is not None:
                it = self.infer(s.init, scope)
                if not self._assignable(s.declared_type, it, s.init):
                    raise TypeError_(
                        f"变量 {s.name} 声明类型 {s.declared_type} 与初值 {it} 不符",
                        s.line, s.col)
            scope.vars[s.name] = s.declared_type
            return

        if isinstance(s, Assign):
            vt = scope.lookup_var(s.target)
            if vt is None:
                raise TypeError_(f"未定义变量 {s.target}", s.line, s.col)
            et = self.infer(s.value, scope)
            if not self._assignable(vt, et, s.value):
                raise TypeError_(
                    f"赋值量纲/类型不匹配：目标 {vt}，值 {et}", s.line, s.col)
            return

        if isinstance(s, If):
            ct = self.infer(s.cond, scope)
            if ct.name != "真假":
                raise TypeError_("倘若条件必须为真假类型", s.line, s.col)
            self.check_block(s.then_body, Scope({}, {}, {}, scope), fn, allowed_errors, caps)
            if s.else_body:
                self.check_block(s.else_body, Scope({}, {}, {}, scope), fn, allowed_errors, caps)
            return

        if isinstance(s, Loop):
            ct = self.infer(s.cond, scope)
            if ct.name != "真假":
                raise TypeError_("循环条件必须为真假类型", s.line, s.col)
            self.check_block(s.body, Scope({}, {}, {}, scope), fn, allowed_errors, caps)
            return

        if isinstance(s, Repeat):
            tt = self.infer(s.times, scope)
            if tt.name != "数":
                raise TypeError_("重复次数必须为数", s.line, s.col)
            self.check_block(s.body, Scope({}, {}, {}, scope), fn, allowed_errors, caps)
            return

        if isinstance(s, ForEach):
            it = self.infer(s.iterable, scope)
            if it.name not in ("序列", "映射"):
                raise TypeError_("遍历对象必须为序列或映射", s.line, s.col)
            child = Scope({}, {}, {}, scope)
            elem = it.elem if it.name == "序列" else it.key
            if elem:
                child.vars[s.var] = elem
            self.check_block(s.body, child, fn, allowed_errors, caps)
            return

        if isinstance(s, Return):
            if fn is None:
                raise TypeError_("入口块中不能使用返回", s.line, s.col)
            rt = self.infer(s.value, scope)
            if not self._assignable(fn.ret_type, rt, s.value):
                raise TypeError_(f"返回类型 {rt} 与声明 {fn.ret_type} 不符", s.line, s.col)
            return

        if isinstance(s, Assert):
            ct = self.infer(s.cond, scope)
            if ct.name != "真假":
                raise TypeError_("断言条件必须为真假类型", s.line, s.col)
            return

        if isinstance(s, Throw):
            if s.error_type not in allowed_errors:
                raise TypeError_(
                    f"抛出未声明的错误类别 {s.error_type}；函数签名可能生：{sorted(allowed_errors)}",
                    s.line, s.col)
            return

        if isinstance(s, Try):
            self.check_block(s.body, Scope({}, {}, {}, scope), fn, allowed_errors, caps)
            if s.catch_body:
                child = Scope({}, {}, {}, scope)
                if s.catch_var:
                    child.vars[s.catch_var] = Type("文本")
                self.check_block(s.catch_body, child, fn, allowed_errors, caps)
            if s.finally_body:
                self.check_block(s.finally_body, Scope({}, {}, {}, scope), fn, allowed_errors, caps)
            return

        if isinstance(s, Exec):
            t = self.infer(s.call, scope, capabilities=caps)
            if s.name != "_":
                scope.vars[s.name] = t
            return

        if isinstance(s, Observe):
            if scope.lookup_var(s.name) is None:
                raise TypeError_(f"观察的变量 {s.name} 未定义", s.line, s.col)
            self.check_block(s.body, Scope({}, {}, {}, scope), fn, allowed_errors, caps)
            return

        if isinstance(s, Sandbox):
            # 沙箱块内能力默认清空，仅保留 输出（C9）；授权语句会扩充它
            self.check_block(s.body, Scope({}, {}, {}, scope), fn, allowed_errors, {"输出"})
            return

        if isinstance(s, Authorize):
            for cap in s.capabilities:
                if cap not in KNOWN_CAPABILITIES:
                    raise TypeError_(f"未知能力 {cap!r}", s.line, s.col)
            if caps is not None:
                caps.update(s.capabilities)
            return

        if isinstance(s, Parallel):
            # 并行块与外层共享作用域，使 启 声明的信箱变量对 收 可见
            self.check_block(s.body, scope, fn, allowed_errors, caps)
            return

        if isinstance(s, Spawn):
            target = scope.lookup_func(s.func_name)
            if target is None:
                raise TypeError_(f"未定义任务函数 {s.func_name}", s.line, s.col)
            if len(s.args) != len(target.params):
                raise TypeError_(f"启 参数数量不符：期望 {len(target.params)}，得到 {len(s.args)}", s.line, s.col)
            # 启 会声明 <函数名>_信箱 变量（运行时同款）
            scope.vars[f"{s.func_name}_信箱"] = Type("信箱")
            return

        if isinstance(s, Receive):
            self.infer(s.mailbox, scope)
            if s.target:
                scope.vars[s.target] = Type("文本")
            return

        if isinstance(s, Output):
            self.infer(s.value, scope)
            return

        if isinstance(s, ExprStmt):
            self.infer(s.expr, scope)
            return

        raise TypeError_(f"未知语句类型 {type(s).__name__}", s.line, s.col)

    # ---- 表达式类型推断（含量纲代数）----
    # capabilities is None  → 不受限上下文（普通函数体），不检查能力授权
    # capabilities = set(...) → 受限上下文（沙箱块），按白名单检查
    def infer(self, e: Expr, scope: Scope, capabilities: Optional[set[str]] = None) -> Type:
        caps = capabilities

        # 能力是否可用：None 表示不受限；否则按白名单
        def cap_ok(name: str) -> bool:
            return caps is None or name in caps

        if isinstance(e, Number):
            t = Type("数", unit=e.unit)
        elif isinstance(e, String):
            t = Type("文本")
        elif isinstance(e, Bool):
            t = Type("真假")
        elif isinstance(e, Null):
            t = Type("空")
        elif isinstance(e, Var):
            t = scope.lookup_var(e.name)
            if t is None:
                # 允许引用函数名（作为可调用值）与能力名
                if scope.lookup_func(e.name) is not None:
                    t = Type("函数")
                elif e.name in KNOWN_CAPABILITIES:
                    t = Type("能力")
                else:
                    raise TypeError_(f"未定义变量 {e.name}", e.line, e.col)
        elif isinstance(e, Attr):
            base = self.infer(e.obj, scope, caps)
            if base.name in KNOWN_CAPABILITIES or self._is_capability_expr(e):
                capname = self._capname(e)
                if not cap_ok(capname):
                    raise TypeError_(
                        f"未授权能力 {capname}：沙箱内必须显式授权（C9）", e.line, e.col)
                t = Type("能力")
            elif base.name in self.global_scope.records:
                rec = self.global_scope.records[base.name]
                f = next((x for x in rec.fields if x.name == e.name), None)
                if not f:
                    raise TypeError_(f"记录 {base.name} 无字段 {e.name}", e.line, e.col)
                t = f.type
            elif base.name == "函数":
                t = Type("能力")
            else:
                # 运行时对象属性，类型宽松处理
                t = Type("任意")
        elif isinstance(e, Binary):
            lt = self.infer(e.left, scope, caps)
            rt = self.infer(e.right, scope, caps)
            # 逻辑与/或：两侧必须为真假（G1），结果为真假
            if e.op in ("并且", "或者"):
                if lt.name != "真假" or rt.name != "真假":
                    raise TypeError_(
                        f"{e.op} 两侧必须为真假类型，得到 {lt} 与 {rt}", e.line, e.col)
                t = Type("真假")
            elif e.op in ("+", "-"):
                if lt.name != "数" or rt.name != "数":
                    if lt.name == "文本" and e.op == "+" and rt.name == "文本":
                        t = Type("文本")
                    else:
                        raise TypeError_(f"{e.op} 运算要求两侧为数（或文本拼接）", e.line, e.col)
                else:
                    lv, rv = unit_vector(lt.unit), unit_vector(rt.unit)
                    if lv != rv:
                        raise TypeError_(
                            f"量纲不匹配：{lt} 与 {rt} 不可 {e.op}（C7）", e.line, e.col)
                    t = Type("数", unit=lt.unit)
            elif e.op in ("*", "/"):
                if lt.name != "数" or rt.name != "数":
                    raise TypeError_(f"{e.op} 运算要求两侧为数", e.line, e.col)
                lv, rv = unit_vector(lt.unit), unit_vector(rt.unit)
                vec = add_vec(lv, rv) if e.op == "*" else sub_vec(lv, rv)
                t = Type("数", unit=self._vec_to_unit(vec))
            elif e.op in ("==", "!=", "<", "<=", ">", ">="):
                if lt.name == "任意" or rt.name == "任意":
                    t = Type("真假")  # 动态值允许比较，运行时决定
                elif lt.name != rt.name:
                    raise TypeError_(f"比较运算两侧类型不同：{lt} 与 {rt}", e.line, e.col)
                elif lt.name == "数" and unit_vector(lt.unit) != unit_vector(rt.unit):
                    raise TypeError_(f"比较量纲不匹配：{lt} 与 {rt}", e.line, e.col)
                else:
                    t = Type("真假")
            else:
                raise TypeError_(f"未知运算符 {e.op}", e.line, e.col)
        elif isinstance(e, Unary):
            ut = self.infer(e.operand, scope, caps)
            if e.op == "-":
                if ut.name != "数":
                    raise TypeError_("取负要求数类型", e.line, e.col)
                t = ut
            elif e.op == "非":
                if ut.name != "真假":
                    raise TypeError_("非 要求真假类型", e.line, e.col)
                t = Type("真假")
            else:
                raise TypeError_(f"未知一元运算符 {e.op}", e.line, e.col)
        elif isinstance(e, Call):
            callee = e.callee
            fname = self._expr_name(callee)
            # 用户定义的函数优先于同名的内置函数/标准库——
            # 修复原实现的遮蔽 bug：定义 做法 长度(...) 后调用却命中内置 长度，
            # 返回「3个」而不是用户函数的结果，静默给出错误答案。
            user_fn = scope.lookup_func(fname) if fname else None
            if fname and user_fn is not None:
                fn = user_fn
                if len(e.args) != len(fn.params):
                    raise TypeError_(f"调用 {fname} 参数数量不符", e.line, e.col)
                for arg, p in zip(e.args, fn.params):
                    at = self.infer(arg, scope, caps)
                    if not self._assignable(p.type, at, arg):
                        raise TypeError_(
                            f"调用 {fname} 参数 {p.name} 类型不符：期望 {p.type}，得到 {at}",
                            e.line, e.col)
                t = fn.ret_type
            elif fname in ("化成文", "化为数", "长度"):
                # 内置函数：化成文/化为数/长度
                ats = [self.infer(arg, scope, caps) for arg in e.args]
                if fname == "长度" and ats:
                    # 长度 只对文本/序列/映射有意义，静态拦住「长度(数值)」
                    if ats[0].name not in ("文本", "序列", "映射", "任意"):
                        raise TypeError_(
                            f"长度 只能用于文本、序列、映射，收到 {ats[0]}", e.line, e.col)
                if fname == "化成文":
                    t = Type("文本")
                elif fname == "化为数":
                    t = Type("数")
                else:
                    # 长度是无量纲计数（原实现返回 数<个>，导致显示成「3个」）
                    t = Type("数")
                e.inferred_type = t
                return t
            elif fname and _stdlib_entry(fname) is not None:
                # 标准库：静态校验参数个数与沙箱能力，并按声明返回类型参与后续检查，
                # 避免把一切标准库调用降级成「任意」而丢失类型信息。
                entry = _stdlib_entry(fname)
                n = len(e.args)
                arity = entry.arity
                ok = (n in arity) if isinstance(arity, tuple) else (n == arity)
                if not ok:
                    raise TypeError_(
                        f"标准库 {fname} 参数数量不符：需要 {arity} 个，收到 {n} 个",
                        e.line, e.col)
                if entry.needs_cap is not None and caps is not None \
                        and entry.needs_cap not in caps:
                    raise TypeError_(
                        f"未授权能力 {entry.needs_cap}：标准库 {fname} 在沙箱内需显式授权（C9）",
                        e.line, e.col)
                for arg in e.args:
                    self.infer(arg, scope, caps)
                t = _stdlib_ret_type(entry.ret, entry.ret_elem)
                e.inferred_type = t
                return t
            elif fname and "." in fname and fname.split(".", 1)[0] in self.host_aliases:
                # FFI：外部模块调用。宿主函数的真实类型不可静态获知，标为「任意」，
                # 但必须校验沙箱能力——外部调用在沙箱内需 授权 外部.调用<模块名>。
                host = fname.split(".", 1)[0]
                cap = f"外部.调用{host}"
                if caps is not None and cap not in caps:
                    raise TypeError_(
                        f"未授权能力 {cap}：沙箱内调用外部模块 {host} 需显式授权（C9）",
                        e.line, e.col)
                for arg in e.args:
                    self.infer(arg, scope, caps)
                t = Type("任意")
            elif self._is_capability_expr(callee):
                capname = self._capname(callee)
                if not cap_ok(capname):
                    raise TypeError_(f"未授权能力 {capname}（C9）", e.line, e.col)
                t = Type("映射")  # 工具调用返回结构化结果
            else:
                # 记录构造器等
                if fname in self.global_scope.records:
                    rec = self.global_scope.records[fname]
                    if len(e.args) != len(rec.fields):
                        raise TypeError_(f"记录 {fname} 构造参数数量不符", e.line, e.col)
                    for arg, f in zip(e.args, rec.fields):
                        at = self.infer(arg, scope, caps)
                        if not self._assignable(f.type, at, arg):
                            raise TypeError_(f"记录 {fname} 字段 {f.name} 类型不符", e.line, e.col)
                    t = Type(fname)
                else:
                    for arg in e.args:
                        self.infer(arg, scope, caps)
                    t = Type("任意")
        elif isinstance(e, SeqLiteral):
            ets = [self.infer(x, scope, caps) for x in e.items]
            if ets and all(type_equal(ets[0], x) for x in ets[1:]):
                t = Type("序列", elem=ets[0])
            elif not ets:
                t = Type("序列", elem=Type("任意"))
            else:
                raise TypeError_("序列元素类型不一致", e.line, e.col)
        elif isinstance(e, MapLiteral):
            kts = [self.infer(k, scope, caps) for k, _ in e.pairs]
            vts = [self.infer(v, scope, caps) for _, v in e.pairs]
            if kts and all(type_equal(kts[0], x) for x in kts[1:]) and all(type_equal(vts[0], x) for x in vts[1:]):
                t = Type("映射", key=kts[0], val=vts[0])
            elif not kts:
                t = Type("映射", key=Type("任意"), val=Type("任意"))
            else:
                raise TypeError_("映射键或值类型不一致", e.line, e.col)
        else:
            raise TypeError_(f"未知表达式 {type(e).__name__}", e.line, e.col)

        e.inferred_type = t  # 填充 IR 标注
        return t

    def _expr_name(self, e: Expr) -> Optional[str]:
        if isinstance(e, Var):
            return e.name
        if isinstance(e, Attr):
            base = self._expr_name(e.obj)
            return f"{base}.{e.name}" if base else None
        return None

    def _is_capability_expr(self, e: Expr) -> bool:
        name = self._expr_name(e)
        if name in KNOWN_CAPABILITIES:
            return True
        if isinstance(e, Attr) and isinstance(e.obj, Var):
            return f"{e.obj.name}.{e.name}" in KNOWN_CAPABILITIES
        return False

    def _capname(self, e: Expr) -> str:
        return self._expr_name(e) or ""

    def _vec_to_unit(self, vec: tuple[int, ...]) -> Unit:
        num, den = [], []
        for i, v in enumerate(vec):
            name = DIMS[i]
            uname = PREFERRED_UNIT.get(name, name)
            if v > 0:
                num.extend([uname] * v)
            elif v < 0:
                den.extend([uname] * (-v))
        return Unit(num, den)
