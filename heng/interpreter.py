"""衡语解释器与运行时。

实现：
  - 值模型：带单位的数值、记录、序列、映射、错误
  - 作用域与块级作用域
  - 全部控制流语句
  - 错误类型 / 尝试-捕获-终结
  - 沙箱能力模型（默认拒绝 + 显式授权）
  - 执行/观察 工具闭环
  - 结构化并发 + 信箱
  - IR 输出（显式 IR 供验证）
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional, Callable

from .ast_nodes import (
    Type, Unit, Expr, Number, String, Bool, Null, Var, Attr, Binary, Unary,
    Call, SeqLiteral, MapLiteral, Stmt, VarDecl, Assign, If, Loop, ForEach,
    Return, Assert, Throw, Try, Exec, Observe, Sandbox, Authorize, Parallel, Repeat,
    Spawn, Receive, Output, ExprStmt, FuncDef, RecordDef, Program,
)
from .typecheck import unit_vector, add_vec, sub_vec, DIMS, BASE_UNITS, PREFERRED_UNIT

# 共享不可变类型对象：解释执行时避免为每个字面量新建 Type。
# Type 本身是可变 dataclass，但这里的常量实例只被读取、从不被写入。
_TYPE_NUM = Type("数")
_TYPE_TEXT = Type("文本")
_TYPE_BOOL = Type("真假")
_TYPE_NULL = Type("空")
_TYPE_ANY = Type("任意")
_TYPE_CAP = Type("能力")
_TYPE_RECTYPE = Type("记录类型")
_EMPTY_CAPS: set[str] = set()


def _is_bare_num(t: Type) -> bool:
    """是否为无量纲数值类型（快路判定）。"""
    u = t.unit
    return t.name == "数" and (u is None or u.is_dimensionless())


# ---------------- 运行时值 ----------------

@dataclass
class HValue:
    type: Type
    value: Any


@dataclass
class HError(Exception):
    category: str
    message: str

    def __str__(self) -> str:
        return f"错误<{self.category}>: {self.message}"


@dataclass
class HRecord:
    type_name: str
    fields: dict[str, HValue]


@dataclass
class HFunction:
    defn: FuncDef


@dataclass
class HCapability:
    name: str


@dataclass
class Mailbox:
    messages: list[Any] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def send(self, msg: Any) -> None:
        with self.lock:
            self.messages.append(msg)

    def recv(self) -> Any:
        with self.lock:
            return self.messages.pop(0) if self.messages else None


@dataclass
class Signal:
    """控制流信号：返回 / 中断。"""
    kind: str
    value: Any = None


class Env:
    """词法作用域环境。

    性能要点（对应报告「解释器性能优化」）：
      - 函数调用是递归场景的主要开销来源，因此提供 `child()` 原地复用：
        顺序调用复用同一个 Env 对象，只有嵌套调用（递归）才新建，显著减少分配；
      - `get` 改为迭代实现，避免递归函数的额外栈帧。
    """

    __slots__ = ("vars", "parent")

    def __init__(self, parent: Optional["Env"] = None):
        self.vars: dict[str, HValue] = {}
        self.parent = parent

    def get(self, name: str) -> HValue:
        env: Optional[Env] = self
        while env is not None:
            v = env.vars.get(name)
            if v is not None:
                return v
            env = env.parent
        raise HError("未定义", f"未定义变量 {name}")

    def set(self, name: str, val: HValue) -> None:
        """就近赋值：找到定义该变量的作用域并更新。"""
        env: Optional[Env] = self
        while env is not None:
            if name in env.vars:
                env.vars[name] = val
                return
            env = env.parent
        raise HError("未定义", f"未定义变量 {name}")

    def declare(self, name: str, val: HValue) -> None:
        self.vars[name] = val

    def child(self) -> "Env":
        """返回可复用的子作用域（清空旧绑定，保留 parent 关系）。"""
        self.vars.clear()
        return self


# ---------------- 解释器 ----------------

class Runtime:
    def __init__(self, program: Program, tools: Optional[dict[str, Callable]] = None,
                 trace_ir: bool = False,
                 host_modules: Optional[dict[str, Any]] = None):
        self.prog = program
        self.global_env = Env()
        self.tools: dict[str, Callable] = tools or {}
        # trace_ir 默认关闭：每条语句无条件写 IR trace 是纯开销（实测占解释耗时约 46%）。
        # 仅在需要展示显式 IR 时开启。
        self.trace_ir = trace_ir
        self.ir: list[str] = []
        self.output: list[str] = []
        # 并行块挂起线程：发起 只登记不阻塞，块结束统一会合（C11 真并发语义）
        self._pending_threads: list[tuple[threading.Thread, Mailbox]] = []
        # 函数/记录表的快速索引（解释期不再线性扫描 prog.records）
        self._record_index = {r.name: r for r in program.records}
        # 变量查找缓存：Var 节点 → 绑定所在层深（0=当前层，1=父层…）。
        # 相对深度对同一次调用的所有递归帧都成立，因此缓存安全且收益最大。
        self._lookup_depth: dict[int, int] = {}
        self.host_modules: dict[str, Any] = host_modules or {}
        self._build_dispatch()
        self._register_tools()
        self._init_globals()

    # ---- 内置工具 ----
    def _default_tools(self) -> dict[str, Callable]:
        return {
            "输出": lambda *a: " ".join(self._fmt(x) for x in a),
            "工具.查询": lambda target: {
                "状态": "完成", "内容": f"查询({target})结果", "长度": len(str(target))},
            "工具.清理": lambda: {"状态": "已清理"},
            "文件.读取": lambda path: f"读取:{path}",
            "文件.写入": lambda path, content: f"写入:{path}",
            "网络.请求": lambda url: f"响应:{url}",
        }

    def _register_tools(self) -> None:
        defaults = self._default_tools()
        for k, v in defaults.items():
            self.tools.setdefault(k, v)

    def _init_globals(self) -> None:
        # 记录类型
        for rec in self.prog.records:
            self.global_env.declare(rec.name, HValue(Type("记录类型"), RecordDefRef(rec)))
        # 函数
        for fn in self.prog.functions:
            self.global_env.declare(fn.name, HValue(Type("函数"), HFunction(fn)))
        # 常量
        for name, typ, val in self.prog.constants:
            self.global_env.declare(name, self.eval(val, self.global_env))

    def run(self) -> list[str]:
        if self.prog.entry:
            env = Env(self.global_env)
            self.exec_block(self.prog.entry, env, set())
        return self.output

    # ---- 分派表 ----
    # 报告「解释器性能优化」的落地：用「AST 节点类型 → 处理函数」表取代
    # 逐层 isinstance 长链。语料实测 isinstance 调用占解释耗时约 19%，
    # 每个表达式节点平均要经历 4~6 次类型判断；查表只需一次 dict 命中。
    def _build_dispatch(self) -> None:
        self._stmt_dispatch: dict[type, Callable] = {
            VarDecl: self._exec_vardecl,
            Assign: self._exec_assign,
            If: self._exec_if,
            Repeat: self._exec_repeat,
            Loop: self._exec_loop,
            ForEach: self._exec_foreach,
            Return: self._exec_return,
            Assert: self._exec_assert,
            Throw: self._exec_throw,
            Try: self._exec_try,
            Exec: self._exec_exec,
            Observe: self._exec_observe,
            Sandbox: self._exec_sandbox,
            Authorize: self._exec_authorize,
            Parallel: self._exec_parallel,
            Spawn: self._exec_spawn,
            Receive: self._exec_receive,
            Output: self._exec_output,
            ExprStmt: self._exec_exprstmt,
        }
        self._expr_dispatch: dict[type, Callable] = {
            Number: self._eval_number,
            String: self._eval_string,
            Bool: self._eval_bool,
            Null: self._eval_null,
            Var: self._eval_var,
            Attr: self._eval_attr,
            Binary: self._eval_binary,
            Unary: self._eval_unary,
            Call: self._eval_call,
            SeqLiteral: self._eval_seq,
            MapLiteral: self._eval_map,
        }
        # 内置函数快速名集合：避免每次调用都走字符串比较链
        self._builtin_names = frozenset({"化成文", "化为数", "长度"})
        # 纯数值快捷二元运算（两侧均无量纲数时走此快路，跳过量纲向量计算）
        self._fast_binop = {
            "+": lambda a, b: a + b,
            "-": lambda a, b: a - b,
            "*": lambda a, b: a * b,
            "<": lambda a, b: a < b,
            ">": lambda a, b: a > b,
            "==": lambda a, b: a == b,
            "!=": lambda a, b: a != b,
            "<=": lambda a, b: a <= b,
            ">=": lambda a, b: a >= b,
        }

    # ---- 语句执行 ----
    def exec_block(self, stmts: list[Stmt], env: Env, caps: set[str]) -> Optional[Signal]:
        run = self.exec_stmt
        for s in stmts:
            sig = run(s, env, caps)
            if sig is not None:
                return sig
        return None

    def exec_stmt(self, s: Stmt, env: Env, caps: set[str]) -> Optional[Signal]:
        if self.trace_ir:
            self.ir.append(f"[{s.line}:{s.col}] {type(s).__name__}")
        handler = self._stmt_dispatch.get(type(s))
        if handler is None:
            raise HError("未定义", f"未知语句 {type(s).__name__}")
        return handler(s, env, caps)

    # ---- 各语句处理函数 ----
    def _exec_vardecl(self, s: VarDecl, env: Env, caps: set[str]) -> None:
        val = self.eval(s.init, env, caps) if s.init else self._default_value(s.declared_type)
        env.declare(s.name, val)
        return None

    def _exec_assign(self, s: Assign, env: Env, caps: set[str]) -> None:
        val = self.eval(s.value, env, caps)
        env.set(s.target, val)
        return None

    def _exec_if(self, s: If, env: Env, caps: set[str]) -> Optional[Signal]:
        cond = self.eval(s.cond, env, caps)
        if cond.value:
            return self.exec_block(s.then_body, Env(env), caps)
        elif s.else_body:
            return self.exec_block(s.else_body, Env(env), caps)
        return None

    def _exec_repeat(self, s: Repeat, env: Env, caps: set[str]) -> Optional[Signal]:
        n = self.eval(s.times, env, caps).value
        try:
            n = int(n)
        except (TypeError, ValueError):
            raise HError("类型", "重复次数必须为整数")
        for _ in range(max(n, 0)):
            sig = self.exec_block(s.body, Env(env), caps)
            if sig and sig.kind == "return":
                return sig
        return None

    def _exec_loop(self, s: Loop, env: Env, caps: set[str]) -> Optional[Signal]:
        guard = 0
        while self.eval(s.cond, env, caps).value:
            guard += 1
            if guard > 100000:
                raise HError("越界", "循环次数超过 100000，疑似死循环")
            sig = self.exec_block(s.body, Env(env), caps)
            if sig and sig.kind == "return":
                return sig
        return None

    def _exec_foreach(self, s: ForEach, env: Env, caps: set[str]) -> Optional[Signal]:
        iterable = self.eval(s.iterable, env, caps)
        # 序列：元素本身即 HValue；映射：按类型系统语义遍历「键」，键为文本 HValue。
        # 旧实现返回 (key, HValue) 原生元组，导致输出时暴露 Python repr——已修复。
        if isinstance(iterable.value, list):
            items = list(iterable.value)
        elif isinstance(iterable.value, dict):
            items = [HValue(Type("文本"), k) for k in iterable.value.keys()]
        else:
            items = []
        for item in items:
            if not isinstance(item, HValue):
                item = HValue(Type("任意"), item)
            child = Env(env)
            child.declare(s.var, item)
            sig = self.exec_block(s.body, child, caps)
            if sig and sig.kind == "return":
                return sig
        return None

    def _exec_return(self, s: Return, env: Env, caps: set[str]) -> Signal:
        return Signal("return", self.eval(s.value, env, caps))

    def _exec_assert(self, s: Assert, env: Env, caps: set[str]) -> None:
        if not self.eval(s.cond, env, caps).value:
            raise HError("断言", "断言失败")
        return None

    def _exec_throw(self, s: Throw, env: Env, caps: set[str]) -> None:
        raise HError(s.error_type, self._fmt(self.eval(s.message, env, caps)))

    def _exec_try(self, s: Try, env: Env, caps: set[str]) -> Optional[Signal]:
        try:
            sig = self.exec_block(s.body, Env(env), caps)
        except HError as err:
            sig = None
            if s.catch_body and (s.catch_type is None or s.catch_type == err.category):
                child = Env(env)
                if s.catch_var:
                    child.declare(s.catch_var, HValue(Type("文本"), err.message))
                # 捕获块内的 返回 必须向外传播（旧实现丢弃信号，导致返回 None）
                sig = self.exec_block(s.catch_body, child, caps)
            else:
                raise
        finally:
            if s.finally_body:
                fsig = self.exec_block(s.finally_body, Env(env), caps)
                if sig is None and fsig is not None and fsig.kind == "return":
                    sig = fsig
        return sig

    def _exec_exec(self, s: Exec, env: Env, caps: set[str]) -> None:
        # 运行时能力校验（P0 安全修复）：
        # 旧实现用 _expr_name(s.call) 对 Call 节点恒返回 None，检查永不触发——
        # 沙箱可绕过。改为从 Call 的 callee 提取能力名。
        # caps 语义：set() 空 = 不受限上下文；非空（沙箱 {"输出",...}）= 受限。
        capname = self._capname_for_check(s.call)
        if capname and capname != "输出" and caps and capname not in caps:
            raise HError("未授权", f"执行未授权能力 {capname}")
        val = self.eval(s.call, env, caps)
        if s.name != "_":
            env.declare(s.name, val)
        return None

    def _capname_for_check(self, call: Expr) -> Optional[str]:
        """执行语句需要校验的能力名。

        标准库函数（如 网络.取）声明的能力名是 网络.请求，与静态检查保持一致；
        其余能力表达式（工具.查询）能力名即函数全名。
        """
        if not isinstance(call, Call):
            return None
        name = self._expr_name(call.callee)
        if name and "." in name:
            from .stdlib import resolve
            entry = resolve(name)
            if entry is not None and entry.needs_cap is not None:
                return entry.needs_cap
            return name
        return name

    def _exec_observe(self, s: Observe, env: Env, caps: set[str]) -> Optional[Signal]:
        # 观察块：基于已执行结果分支（条件由块内 如果 语句表达）
        return self.exec_block(s.body, Env(env), caps)

    def _exec_sandbox(self, s: Sandbox, env: Env, caps: set[str]) -> Optional[Signal]:
        return self.exec_block(s.body, Env(env), {"输出"})

    def _exec_authorize(self, s: Authorize, env: Env, caps: set[str]) -> None:
        caps.update(s.capabilities)
        return None

    def _exec_parallel(self, s: Parallel, env: Env, caps: set[str]) -> Optional[Signal]:
        # 并行块与外层共享环境，使 发起 声明的信箱变量对 接收 可见。
        # 关键：块内多个 发起 只登记线程不阻塞，块结束时统一会合（C11「块结束自动会合」）。
        # 旧实现在每个 Spawn 后立即 join，导致多个任务退化为串行，属真实语义 bug。
        saved = self._pending_threads
        self._pending_threads = []
        try:
            sig = self.exec_block(s.body, env, caps)
            # 块结束：会合所有由本块 发起 的任务
            for t, _mb in self._pending_threads:
                t.join()
            return sig
        finally:
            self._pending_threads = saved

    def _exec_spawn(self, s: Spawn, env: Env, caps: set[str]) -> None:
        fn = self.global_env.get(s.func_name).value
        args = [self.eval(a, env, caps) for a in s.args]
        mailbox = Mailbox()

        def worker() -> None:
            try:
                call_env = Env(self.global_env)
                for p, arg in zip(fn.defn.params, args):
                    call_env.declare(p.name, arg)
                sig = self.exec_block(fn.defn.body, call_env, caps)
                if sig and sig.kind == "return":
                    mailbox.send(sig.value)
            except HError as err:
                mailbox.send(err)

        t = threading.Thread(target=worker)
        t.start()
        # 只登记，不在此 join——真正的并发由 Parallel 块结束时会合
        self._pending_threads.append((t, mailbox))
        env.declare(f"{s.func_name}_信箱", HValue(Type("信箱"), mailbox))
        return None

    def _exec_receive(self, s: Receive, env: Env, caps: set[str]) -> None:
        box = self.eval(s.mailbox, env, caps).value
        msg = None
        if isinstance(box, Mailbox):
            msg = box.recv()
            # 若尚未有消息且该信箱对应挂起任务，等待其完成后重试（块内 接收 也安全）
            if msg is None:
                for t, mb in self._pending_threads:
                    if mb is box:
                        t.join()
                        msg = box.recv()
                        break
        if s.target and msg is not None:
            env.declare(s.target, msg)
        return None

    def _exec_output(self, s: Output, env: Env, caps: set[str]) -> None:
        val = self.eval(s.value, env, caps)
        self.output.append(self._fmt(val))
        return None

    def _exec_exprstmt(self, s: ExprStmt, env: Env, caps: set[str]) -> None:
        self.eval(s.expr, env, caps)
        return None

    # ---- 表达式求值 ----
    def eval(self, e: Expr, env: Env, caps: Optional[set[str]] = None) -> HValue:
        handler = self._expr_dispatch.get(type(e))
        if handler is None:
            raise HError("未定义", f"未知表达式 {type(e).__name__}")
        return handler(e, env, caps if caps is not None else _EMPTY_CAPS)

    def _eval_number(self, e: Number, env: Env, caps: set[str]) -> HValue:
        return HValue(Type("数", unit=e.unit), e.value)

    def _eval_string(self, e: String, env: Env, caps: set[str]) -> HValue:
        return HValue(_TYPE_TEXT, e.value)

    def _eval_bool(self, e: Bool, env: Env, caps: set[str]) -> HValue:
        return HValue(_TYPE_BOOL, e.value)

    def _eval_null(self, e: Null, env: Env, caps: set[str]) -> HValue:
        return HValue(_TYPE_NULL, None)

    def _eval_var(self, e: Var, env: Env, caps: set[str]) -> HValue:
        # 变量查找缓存：记录该名字的绑定位于第几层，命中后直接按层深取，
        # 省掉逐层 dict 查找（递归场景下每帧少 1~3 次 dict 命中）。
        depth = self._lookup_depth.get(id(e))
        if depth is not None:
            cur = env
            for _ in range(depth):
                cur = cur.parent  # type: ignore[assignment]
                if cur is None:
                    depth = None
                    break
            if depth is not None:
                v = cur.vars.get(e.name)
                if v is not None:
                    return v
        # 未命中：逐层查找并记录深度
        cur = env
        d = 0
        while cur is not None:
            v = cur.vars.get(e.name)
            if v is not None:
                self._lookup_depth[id(e)] = d
                return v
            cur = cur.parent
            d += 1
        raise HError("未定义", f"未定义变量 {e.name}")

    def _eval_attr(self, e: Attr, env: Env, caps: set[str]) -> HValue:
        base = self.eval(e.obj, env, caps)
        v = base.value
        if isinstance(v, HRecord):
            return v.fields.get(e.name, HValue(_TYPE_ANY, None))
        if isinstance(v, dict):
            return HValue(_TYPE_ANY, v.get(e.name))
        if isinstance(v, HCapability):
            return HValue(_TYPE_CAP, HCapability(f"{v.name}.{e.name}"))
        if isinstance(v, RecordDefRef):
            return HValue(_TYPE_RECTYPE, v)
        return HValue(_TYPE_ANY, None)

    def _eval_unary(self, e: Unary, env: Env, caps: set[str]) -> HValue:
        v = self.eval(e.operand, env, caps)
        if e.op == "-":
            return HValue(v.type, -v.value)
        if e.op == "非":
            return HValue(_TYPE_BOOL, not v.value)
        raise HError("未定义", f"未知一元运算 {e.op}")

    def _eval_binary(self, e: Binary, env: Env, caps: set[str]) -> HValue:
        op = e.op
        # 短路求值（G1）：并且/或者不预先求值右侧，避免多余副作用与错误
        if op == "并且":
            l = self.eval(e.left, env, caps)
            if not l.value:
                return HValue(_TYPE_BOOL, False)
            r = self.eval(e.right, env, caps)
            return HValue(_TYPE_BOOL, bool(r.value))
        if op == "或者":
            l = self.eval(e.left, env, caps)
            if l.value:
                return HValue(_TYPE_BOOL, True)
            r = self.eval(e.right, env, caps)
            return HValue(_TYPE_BOOL, bool(r.value))
        l = self.eval(e.left, env, caps)
        r = self.eval(e.right, env, caps)
        # 快路：两侧都是无量纲数（最常见情形），直接做浮点运算，
        # 跳过单位向量计算与字典派发。语义与慢路完全一致。
        if type(l.value) is float and type(r.value) is float and _is_bare_num(l.type) and _is_bare_num(r.type):
            fn = self._fast_binop.get(op)
            if fn is not None:
                res = fn(l.value, r.value)
                return HValue(_TYPE_BOOL, res) if not isinstance(res, float) else HValue(_TYPE_NUM, res)
        return self._binary(l, r, op, e)

    def _binary(self, l: HValue, r: HValue, op: str, e: Binary) -> HValue:
        a, b = l.value, r.value

        if op == "+":
            if l.type.name == "文本" and r.type.name == "文本":
                return HValue(_TYPE_TEXT, str(a) + str(b))
            lv, rv = unit_vector(l.type.unit), unit_vector(r.type.unit)
            if lv != rv:
                raise HError("量纲", f"{l.type} 与 {r.type} 不可相加")
            return HValue(l.type, a + b)
        if op == "-":
            lv, rv = unit_vector(l.type.unit), unit_vector(r.type.unit)
            if lv != rv:
                raise HError("量纲", f"{l.type} 与 {r.type} 不可相减")
            return HValue(l.type, a - b)
        if op == "*":
            lv, rv = unit_vector(l.type.unit), unit_vector(r.type.unit)
            return HValue(Type("数", unit=self._vec_to_unit(add_vec(lv, rv))), a * b)
        if op == "/":
            if b == 0:
                raise HError("除零", "除数不能为零")
            lv, rv = unit_vector(l.type.unit), unit_vector(r.type.unit)
            return HValue(Type("数", unit=self._vec_to_unit(sub_vec(lv, rv))), a / b)
        if op == "==":
            return HValue(_TYPE_BOOL, a == b)
        if op == "!=":
            return HValue(_TYPE_BOOL, a != b)
        if op == "<":
            return HValue(_TYPE_BOOL, a < b)
        if op == "<=":
            return HValue(_TYPE_BOOL, a <= b)
        if op == ">":
            return HValue(_TYPE_BOOL, a > b)
        if op == ">=":
            return HValue(_TYPE_BOOL, a >= b)
        raise HError("未定义", f"未知运算符 {op}")

    def _eval_call(self, e: Call, env: Env, caps: set[str]) -> HValue:
        return self._call(e, env, caps)

    def _eval_seq(self, e: SeqLiteral, env: Env, caps: set[str]) -> HValue:
        items = [self.eval(x, env, caps) for x in e.items]
        elem_type = items[0].type if items else _TYPE_ANY
        return HValue(Type("序列", elem=elem_type), items)

    def _eval_map(self, e: MapLiteral, env: Env, caps: set[str]) -> HValue:
        pairs = [(self.eval(k, env, caps), self.eval(v, env, caps)) for k, v in e.pairs]
        if pairs:
            return HValue(Type("映射", key=pairs[0][0].type, val=pairs[0][1].type),
                          {self._fmt(k): v for k, v in pairs})
        return HValue(Type("映射", key=_TYPE_ANY, val=_TYPE_ANY), {})

    def _call(self, e: Call, env: Env, caps: set[str]) -> HValue:
        args = [self.eval(a, env, caps) for a in e.args]
        callee = e.callee
        fname = callee.name if type(callee) is Var else self._expr_name(callee)

        if fname is not None:
            # 用户定义函数优先（与类型检查器一致）。旧实现把内置函数放在最前，
            # 导致用户定义了同名做法后被静默遮蔽，返回内置结果——真实 bug。
            fv = self.global_env.vars.get(fname)
            if fv is not None and isinstance(fv.value, HFunction):
                return self._call_user(fv.value.defn, args, caps)
            # 内置函数：仅当名字未被局部变量占用时才生效，
            # 保证「变量 长度 : 数」不会让 长度(...) 突然调不到实现。
            if fname in self._builtin_names and env.vars.get(fname) is None:
                return self._builtin(fname, args)
            # 记录构造器
            rec = self._record_index.get(fname)
            if rec is not None:
                return HValue(Type(fname), HRecord(fname, {
                    f.name: arg for f, arg in zip(rec.fields, args)}))

        # 标准库（heng.stdlib）
        if fname is not None and "." in fname:
            from .stdlib import call_stdlib
            res = call_stdlib(fname, args, caps, self._fmt)
            if res is not None:
                return res

            # FFI：导入 外部 的宿主模块，按 `别名.函数(...)` 调用
            alias, _, fname2 = fname.partition(".")
            if alias in self.host_modules:
                from .modules import call_host
                from .stdlib import _wrap_inferred
                raw = [self._unwrap(a) for a in args]
                kwargs = {k: self._unwrap(v) for k, v in (e.kwargs or [])}
                result = call_host(self.host_modules[alias], fname2, raw, caps, kwargs)
                return _wrap_inferred(result)

        # 工具调用
        capname = self._capname_of_expr(callee)
        tool_name = capname or fname
        if tool_name in self.tools:
            raw_args = [self._unwrap(a) for a in args]
            result = self.tools[tool_name](*raw_args)
            if tool_name == "输出":
                return HValue(_TYPE_NULL, None)
            if isinstance(result, dict):
                return HValue(Type("映射"), result)
            return HValue(_TYPE_TEXT, result)

        raise HError("未定义", f"未定义函数或能力 {tool_name}")

    def _builtin(self, fname: str, args: list[HValue]) -> HValue:
        """内置转换函数：把任意值格式化为文本 / 解析为数值 / 求长度。"""
        if fname == "化成文":
            if len(args) != 1:
                raise HError("类型", "化成文 需要恰好一个参数")
            return HValue(_TYPE_TEXT, self._fmt(args[0]))
        if fname == "化为数":
            if len(args) != 1:
                raise HError("类型", "化为数 需要恰好一个参数")
            v = args[0].value
            try:
                return HValue(_TYPE_NUM, float(v))
            except (TypeError, ValueError):
                raise HError("类型", f"无法把 {v!r} 化为数")
        if fname == "长度":
            if len(args) != 1:
                raise HError("类型", "长度 需要恰好一个参数")
            # 修复：原实现返回 数<个>，导致「说出 长度(s)」打印出「3个」——
            # 长度是无量纲计数，不应带单位。
            return HValue(_TYPE_NUM, float(self._len_of(args[0].value)))
        raise HError("未定义", f"未定义内置函数 {fname}")

    @staticmethod
    def _len_of(v: Any) -> int:
        """文本/序列/映射取长度；数值等无长度的值给出衡语错误而不是 Python 栈。"""
        if isinstance(v, str):
            return len(v)
        if isinstance(v, (list, dict)):
            return len(v)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            raise HError("类型", f"数 没有长度，长度 只能用于文本、序列、映射")
        raise HError("类型", f"{type(v).__name__} 类型没有长度")

    def _call_user(self, fn: FuncDef, args: list[HValue], caps: set[str]) -> HValue:
        call_env = Env(self.global_env)
        for p, arg in zip(fn.params, args):
            call_env.declare(p.name, arg)
        # 规约前置：绑定实参后求值，为假则拒绝执行（C5 冗余检错）
        if fn.contract is not None and fn.contract.pre is not None:
            if not self.eval(fn.contract.pre, call_env, caps).value:
                raise HError("断言", f"前置条件不满足：函数 {fn.name}")
        sig = self.exec_block(fn.body, call_env, caps)
        ret = sig.value if (sig is not None and sig.kind == "return") else HValue(_TYPE_NULL, None)
        # 规约后置：把返回值绑定为「结果」后求值
        if fn.contract is not None and fn.contract.post is not None:
            call_env.declare("结果", ret)
            if not self.eval(fn.contract.post, call_env, caps).value:
                raise HError("断言", f"后置条件不满足：函数 {fn.name}")
        # 运行时单位标注回落到声明类型，保证静态类型与显示一致
        rt = fn.ret_type
        if ret.type.name == rt.name and rt.unit is not None:
            ret = HValue(rt, ret.value)
        return ret

    # ---- 辅助 ----
    def _expr_name(self, e: Expr) -> Optional[str]:
        if isinstance(e, Var):
            return e.name
        if isinstance(e, Attr):
            base = self._expr_name(e.obj)
            return f"{base}.{e.name}" if base else None
        return None

    def _capname_of_expr(self, e: Expr) -> Optional[str]:
        name = self._expr_name(e)
        if name and ("." in name):
            return name
        return None

    def _unwrap(self, v: HValue) -> Any:
        return v.value

    def _fmt(self, v: Any) -> str:
        if isinstance(v, HValue):
            # 只有「真实的非无量纲单位」才拼接。Unit([],[]) 的 __str__ 为 "1"，
            # 旧实现因字符串 "1" 为真而拼上，导致纯数 5 显示成 "51"——这是显示 bug。
            if v.type.name == "数" and v.type.unit is not None and not v.type.unit.is_dimensionless():
                return f"{self._num(v.value)}{v.type.unit}"
            return self._fmt(v.value)
        if isinstance(v, bool):
            return "真" if v else "假"
        if isinstance(v, float):
            return self._num(v)
        if isinstance(v, dict):
            return "{" + ", ".join(f"{k}: {self._fmt(x)}" for k, x in v.items()) + "}"
        if isinstance(v, list):
            return "[" + ", ".join(self._fmt(x) for x in v) + "]"
        if isinstance(v, HRecord):
            return f"{v.type_name}(" + ", ".join(f"{k}={self._fmt(x)}" for k, x in v.fields.items()) + ")"
        return str(v)

    @staticmethod
    def _num(x: float) -> str:
        if isinstance(x, float) and x.is_integer():
            return str(int(x))
        return str(round(x, 6))

    def _default_value(self, typ: Type) -> HValue:
        if typ.name == "数":
            return HValue(typ, 0)
        if typ.name == "文本":
            return HValue(_TYPE_TEXT, "")
        if typ.name == "真假":
            return HValue(_TYPE_BOOL, False)
        if typ.name == "序列":
            return HValue(typ, [])
        if typ.name == "映射":
            return HValue(typ, {})
        return HValue(typ, None)

    def _vec_to_unit(self, vec: tuple[int, ...]) -> Unit:
        num, den = [], []
        for i, val in enumerate(vec):
            name = PREFERRED_UNIT.get(DIMS[i], DIMS[i])
            if val > 0:
                num.extend([name] * val)
            elif val < 0:
                den.extend([name] * (-val))
        return Unit(num, den)

    def ir_dump(self) -> str:
        return "\n".join(self.ir)


@dataclass
class RecordDefRef:
    defn: RecordDef
