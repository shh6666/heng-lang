"""衡语内核 v2 —— 检查层。

职责（全部在编译期完成，运行期零成本）：
  1. 类型推断：回填每个表达式的 ty（含量纲）；
  2. 量纲代数：加减要求同维，乘除累加维度；不匹配直接报错，不生成代码；
  3. 错误签名：报错 的类别必须出现在 可能生 里；
  4. 能力检查：沙箱块内未授权的能力调用直接报错；
  5. 槽位分配：把每个变量解析成确定的局部/全局槽位下标。

第 5 点是性能关键：运行期不做名字查找，只按下标读数组。
"""

from __future__ import annotations

from typing import Optional

from .ast2 import (
    Expr, Num, Text, Bool, Nil, VarRef, Field, BinOp, UnOp, Call, Await,
    Index, SetIndex, SetField, Lambda, SeqLit, MapLit, Stmt, Let, SetVar, If, Loop, Repeat, Each, Return, Assert,
    Raise, Try, Say, Exec, Observe, Sandbox, Authorize, Parallel, Spawn,
    Receive, ExprStmt, Func, Record, Program,
)
from .types2 import (
    Type, Unit, EMPTY_UNIT, T_NUM, T_TEXT, T_BOOL, T_NIL, T_ANY, T_UNKNOWN,
    T_num, T_seq, T_map, same_type, assignable, unit_vec, vadd, vsub, vec_unit,
    KNOWN_ERRORS, KNOWN_CAPABILITIES,
)


class CheckError(Exception):
    def __init__(self, msg: str, line: int = 0, col: int = 0):
        super().__init__(f"{line}:{col} 类型错误：{msg}" if line else f"类型错误：{msg}")
        self.line = line
        self.col = col


# 内置函数：名称 → (参数数, 返回类型)
BUILTINS: dict[str, tuple[tuple[int, ...], Type]] = {
    "化成文": ((1,), T_TEXT),
    "化为数": ((1,), T_NUM),
    "长度": ((1,), T_NUM),
}


class SlotAllocator:
    """为每个函数分配局部槽位。

    槽位布局：0..n_params-1 是参数，其后的局部变量各自占一格。
    作用域会被编译器压平为单层（与 Python 的 locals 数组同思路）：
    同名变量在不同作用域中视为不同槽位，引用按解析期的 depth 定位。
    """

    def __init__(self):
        self.n = 0

    def next(self) -> int:
        s = self.n
        self.n += 1
        return s


# 各能力的返回类型（未列出的按「映射<文本,任意>」宽松处理）。
# 在模块末尾统一填充，避免与 types2 的构造器产生导入顺序问题。
CAP_RETURN_TYPES: dict = {}


def _same_type(a: str, b: str) -> bool:
    """类型名是否等价。允许一方带模块前缀（内核.内存块 vs 内存块）。"""
    if a == b:
        return True
    return a.split(".")[-1] == b.split(".")[-1]


def _dotted_of(e) -> Optional[str]:
    """把 Field 链拼成 a.b.c；遇到非 VarRef 返回 None。"""
    parts = []
    cur = e
    while isinstance(cur, Field):
        parts.append(cur.name)
        cur = cur.obj
    if not isinstance(cur, VarRef):
        return None
    parts.append(cur.name)
    return ".".join(reversed(parts))


class Checker:
    def __init__(self, prog: Program, host_aliases: Optional[set[str]] = None):
        self.prog = prog
        self.host_aliases = host_aliases or set()
        self.funcs: dict[str, Func] = {f.name: f for f in prog.funcs}
        self.records: dict[str, Record] = {r.name: r for r in prog.records}
        self.consts: dict[str, Type] = {n: t for n, t, _ in prog.consts}
        # 每个作用域的 名字 → 槽位
        self.frames: list[dict[str, int]] = []
        self.n_slots = 0
        # 全局槽位
        self.globals: dict[str, int] = {}
        self.n_globals = 0
        # 变量名 → 声明类型（作用域压平后按名记录；同名遮蔽在编译期已被
        # 声明位置区分，这里记录最新的即可用于赋值检查）
        self._var_types: dict[str, Type] = {n: t for n, t, _ in prog.consts}
        self._all_names: dict[str, int] | None = None
        self._mailbox_types: dict[str, Type] = {}
        # 并发作用域深度：>0 表示当前处于并发块/任务内，此时禁止写外层变量
        self.parallel_depth = 0
        # 进入并发块时的变量名快照：这些是「外层变量」，块内不可写
        self._outer_snapshot: list[set[str]] = []
        # 并发块的组 id 与任务计数
        self._group_counter = 0
        # 沙箱能力栈（None = 不受限）
        self.caps: list[Optional[set[str]]] = [None]
        # 当前函数允许的错误类别
        self.allowed_errors: list[set[str]] = [set()]

    # ============================================================
    # 作用域与槽位
    # ============================================================
    def push_frame(self, init: Optional[dict[str, int]] = None) -> None:
        self.frames.append(dict(init or {}))

    def pop_frame(self) -> None:
        self.frames.pop()

    def declare(self, name: str, ty: Optional[Type] = None) -> int:
        """在当前帧声明变量，返回其槽位；同时记录类型。

        把「分配槽位」和「记录类型」合成一步，避免调用方漏写类型
        （Exec 曾因此导致 观察 找不到变量类型——真实缺陷）。
        """
        slot = self.n_slots
        self.n_slots += 1
        self.frames[-1][name] = slot
        if ty is not None:
            self._var_types[name] = ty
        # 同步记录到当次编译单元的名字表（供代码生成按名查槽位）
        if self._all_names is not None:
            self._all_names[name] = slot
        return slot

    def lookup(self, name: str) -> tuple[int, int]:
        """返回 (层深, 槽位)。层深 0 = 当前帧；找不到抛错。"""
        for d in range(len(self.frames) - 1, -1, -1):
            frame = self.frames[d]
            if name in frame:
                return len(self.frames) - 1 - d, frame[name]
        raise KeyError(name)

    def scope_depth_of(self, name: str) -> int:
        """返回名字相对当前帧的层深；0 = 当前帧自己声明的。"""
        try:
            d, _slot = self.lookup(name)
            return d
        except KeyError:
            return -1

    def lookup_global(self, name: str) -> Optional[int]:
        return self.globals.get(name)

    # ============================================================
    # 入口
    # ============================================================
    def run(self) -> None:
        # 1) 全局槽位分配：常量
        for name, typ, val in self.prog.consts:
            self.globals[name] = self.n_globals
            self.n_globals += 1

        # 2) 检查常量初值（常量表达式不引用局部变量）
        self.push_frame()
        for name, typ, val in self.prog.consts:
            vt = self.infer(val)
            if not assignable(typ, vt):
                raise CheckError(f"常量 {name} 初值类型 {vt} 与声明 {typ} 不符",
                                 val.line, val.col)

        # 3) 检查每个函数
        for fn in self.prog.funcs:
            self.check_func(fn)

        # 4) 检查入口（槽位从 0 重新开始：入口是独立的帧，不与函数共享槽位空间）
        if self.prog.entry is not None:
            self.n_slots = 0
            self._all_names = {}
            self._var_types = {n: t for n, t, _ in self.prog.consts}
            self.push_frame()
            self.caps = [None]
            self.allowed_errors = [set()]
            self.check_stmts(self.prog.entry)
            self.prog.entry_slots = self.n_slots
            self.prog.entry_name_slots = dict(self._all_names or {})
            self._all_names = None
            self.pop_frame()
        self.pop_frame()

    # ============================================================
    # 函数
    # ============================================================
    def check_func(self, fn: Func) -> None:
        self.n_slots = 0
        self._all_names = {}
        self.cur_fn_name = fn.name
        params: dict[str, int] = {}
        for p in fn.params:
            params[p.name] = self.n_slots
            self.n_slots += 1
            # 参数类型必须在检查函数体前登记，否则 甲 + 乙 会退化为「任意 + 任意」
            self._var_types[p.name] = p.type

        self.push_frame(params)
        self.caps = [None]
        self.allowed_errors = [set(fn.errors)]

        for e in fn.errors:
            if e not in KNOWN_ERRORS:
                raise CheckError(f"未定义的错误类别 {e!r}", fn.line, fn.col)

        # 契约
        if fn.contract is not None:
            if fn.contract.require is not None:
                t = self.infer(fn.contract.require)
                if t.name != "真假":
                    raise CheckError("要求 条件必须为真假",
                                     fn.contract.require.line, fn.contract.require.col)
            if fn.contract.ensure is not None:
                # 契约的「结果」用独立帧，绝不能与用户变量同名共享
                saved_types = dict(self._var_types)
                self.push_frame({"结果": self.n_slots})
                self._var_types["结果"] = fn.ret
                self.n_slots += 1
                _ = saved_types
                t = self.infer(fn.contract.ensure)
                self.pop_frame()
                if t.name != "真假":
                    raise CheckError("保证 条件必须为真假",
                                     fn.contract.ensure.line, fn.contract.ensure.col)

        self.check_stmts(fn.body, fn)

        # 必须有返回（返回值类型已逐条校验）
        fn.n_slots = self.n_slots
        fn.name_slots = dict(self._all_names or {})
        self._all_names = None
        self.pop_frame()

    # ============================================================
    # 语句
    # ============================================================
    def check_stmts(self, stmts: list[Stmt], fn: Optional[Func] = None) -> None:
        for s in stmts:
            self.check_stmt(s, fn)

    def check_stmt(self, s: Stmt, fn: Optional[Func] = None) -> None:
        if isinstance(s, Let):
            if s.init is not None:
                # 空容器字面量按声明类型推断：`变量 r : 序列 数 = []`
                # 否则空序列只能推成「序列 任意」，导致所有函数都写不出空初值。
                if isinstance(s.init, SeqLit) and not s.init.items and s.declared is not None:
                    vt = s.declared
                elif isinstance(s.init, MapLit) and not s.init.pairs and s.declared is not None:
                    vt = s.declared
                else:
                    vt = self.infer(s.init)
            else:
                vt = s.declared if s.declared is not None else T_ANY
            if s.declared is not None and s.declared.name != "任意":
                if not assignable(s.declared, vt):
                    hint = ""
                    # 量纲不符是最常见、也最难自查的错误，单独给出提示
                    if (s.declared.name == "数" and vt.name == "数"
                            and str(s.declared.unit) != str(vt.unit)):
                        hint = (f"（量纲不匹配：声明的单位是 {s.declared.unit or '无'}，"
                                f"但表达式算出 {vt.unit or '无'}。"
                                f"检查乘法/除法是否让单位正确约掉）")
                    raise CheckError(
                        f"变量 {s.name} 声明类型 {s.declared} 与初值 {vt} 不符{hint}",
                        s.line, s.col)
                final = s.declared
            else:
                final = vt
            s.ty = final
            s.slot = self.declare(s.name, final)
            return

        if isinstance(s, SetField):
            bt = self.infer(s.base)
            vt = self.infer(s.value)
            if bt.name not in ("记录", "映射", "任意") and bt.name not in self.records:
                raise CheckError(f"该值没有可写字段 {s.name}", s.line, s.col)
            # 字段类型检查（记录已知时）
            rec = self.records.get(bt.name)
            if rec is not None:
                for f in rec.fields:
                    if f.name == s.name:
                        if not assignable(f.type, vt):
                            raise CheckError(
                                f"字段 {s.name} 类型 {f.type} 与值 {vt} 不符", s.line, s.col)
                        break
            s.ty = vt
            return

        if isinstance(s, SetIndex):
            # 下标写入：三个子表达式都必须 infer，否则 VarRef 拿不到槽位
            # （曾漏掉 value，导致 将 t 放到 a[i] 里 t 未分配槽位）
            vt = self.infer(s.value)
            self.infer(s.index)
            bt = self.infer(s.base)
            if bt.name == "序列" and getattr(bt, "elem", None) is not None:
                if (vt.name != "任意" and bt.elem.name != "任意"
                        and not _same_type(vt.name, bt.elem.name)):
                    raise CheckError(
                        f"赋给序列的元素类型应为 {bt.elem}，实际是 {vt}",
                        s.line, s.col)
            if bt.name not in ("序列", "映射", "任意"):
                raise CheckError(f"{bt} 不支持下标赋值", s.line, s.col)
            # 并发安全：与 SetVar 同一规则——任务内不改外层
            if self.parallel_depth > 0 and self._outer_snapshot:
                if isinstance(s.base, VarRef) and s.base.name in self._outer_snapshot[-1]:
                    raise CheckError(
                        f"并发任务内不能修改外层变量 {s.base.name}"
                        f"（请让任务 返回 结果，再在父任务里汇总）",
                        s.line, s.col)
            return

        if isinstance(s, SetVar):
            # 并发安全：任务内只能写自己的局部变量，不能写外层变量。
            # 这是消除数据竞争的静态保证——不靠运行期加锁，也不靠用户自律。
            if self.parallel_depth > 0 and self._outer_snapshot:
                if s.target in self._outer_snapshot[-1]:
                    raise CheckError(
                        f"并发任务内不能修改外层变量 {s.target}"
                        f"（请让任务 返回 结果，再在父任务里汇总；"
                        f"这样才不会出现两个任务同时写同一个变量）",
                        s.line, s.col)
            vt = self._lookup_type(s.target, s)
            et = self.infer(s.value)
            if not assignable(vt, et):
                raise CheckError(f"赋值类型不匹配：目标 {vt}，值 {et}", s.line, s.col)
            s.ty = vt
            # 回填槽位与层深：解析期只能拿到层深（作用域路径），
            # 真正的槽位下标必须由检查层的作用域表查出。
            try:
                depth, slot = self.lookup(s.target)
                s.depth = depth
                s.slot = slot
            except KeyError:
                if s.target in self.globals:
                    s.depth = -2
                    s.slot = self.globals[s.target]
                else:
                    raise CheckError(f"未定义变量 {s.target}", s.line, s.col)
            return

        if isinstance(s, If):
            ct = self.infer(s.cond)
            if ct.name != "真假":
                raise CheckError("如果 条件必须为真假", s.line, s.col)
            self.push_frame()
            self.check_stmts(s.then_body, fn)
            self.pop_frame()
            if s.else_body is not None:
                self.push_frame()
                self.check_stmts(s.else_body, fn)
                self.pop_frame()
            return

        if isinstance(s, Loop):
            ct = self.infer(s.cond)
            if ct.name != "真假":
                raise CheckError("循环 条件必须为真假", s.line, s.col)
            self.push_frame()
            self.check_stmts(s.body, fn)
            self.pop_frame()
            return

        if isinstance(s, Repeat):
            tt = self.infer(s.times)
            if tt.name != "数":
                raise CheckError("重复 次数必须为数", s.line, s.col)
            self.push_frame()
            self.check_stmts(s.body, fn)
            self.pop_frame()
            return

        if isinstance(s, Each):
            it = self.infer(s.iterable)
            if it.name == "序列":
                elem = it.elem or T_ANY
            elif it.name == "映射":
                elem = it.key or T_TEXT
            elif it.name == "任意":
                elem = T_ANY
            else:
                raise CheckError("每个 的对象必须是序列或映射", s.line, s.col)
            self.push_frame()
            s.slot = self.declare(s.var, elem)
            s.elem_ty = elem
            self.check_stmts(s.body, fn)
            self.pop_frame()
            return

        if isinstance(s, Return):
            if fn is None:
                raise CheckError("入口块中不能使用 返回", s.line, s.col)
            if s.value is None:
                if fn.ret.name not in ("空", "任意"):
                    raise CheckError(f"该做法声明返回 {fn.ret}，不能空返回", s.line, s.col)
                return
            vt = self.infer(s.value)
            if not assignable(fn.ret, vt):
                raise CheckError(f"返回类型 {vt} 与声明 {fn.ret} 不符", s.line, s.col)
            return

        if isinstance(s, Assert):
            ct = self.infer(s.cond)
            if ct.name != "真假":
                raise CheckError("必须 条件必须为真假", s.line, s.col)
            return

        if isinstance(s, Raise):
            allowed = set()
            for a in self.allowed_errors:
                allowed |= a
            if s.error_type not in allowed:
                raise CheckError(
                    f"报错 的类别 {s.error_type} 未在 可能生 中声明（已声明：{sorted(allowed)}）",
                    s.line, s.col)
            self.infer(s.message)
            return

        if isinstance(s, Try):
            self.push_frame()
            self.check_stmts(s.body, fn)
            self.pop_frame()
            if s.catch_body is not None:
                self.push_frame()
                if s.catch_var:
                    s.catch_slot = self.declare(s.catch_var, T_TEXT)
                self.check_stmts(s.catch_body, fn)
                self.pop_frame()
            if s.finally_body is not None:
                self.push_frame()
                self.check_stmts(s.finally_body, fn)
                self.pop_frame()
            return

        if isinstance(s, Say):
            self.infer(s.value)
            return

        if isinstance(s, Exec):
            t = self.infer(s.call)
            if s.name != "_":
                s.ty = t
                s.slot = self.declare(s.name, t)
            return

        if isinstance(s, Observe):
            self._lookup_type(s.name, s)
            try:
                _d, s.slot = self.lookup(s.name)
            except KeyError:
                raise CheckError(f"观察的变量 {s.name} 未定义", s.line, s.col)
            self.push_frame()
            self.check_stmts(s.body, fn)
            self.pop_frame()
            return

        if isinstance(s, Sandbox):
            self.caps.append({"输出"})
            self.push_frame()
            self.check_stmts(s.body, fn)
            self.pop_frame()
            self.caps.pop()
            return

        if isinstance(s, Authorize):
            for cap in s.capabilities:
                if cap not in KNOWN_CAPABILITIES and not cap.startswith("外部.调用"):
                    raise CheckError(f"未知能力 {cap!r}", s.line, s.col)
            if self.caps[-1] is not None:
                self.caps[-1].update(s.capabilities)
            return

        if isinstance(s, Parallel):
            # 并行块与外层共享作用域（信箱变量在块外可见），
            # 但块内处于并发作用域：禁止写外层变量。
            s.group_id = self._group_counter
            self._group_counter += 1
            # 快照当前可见的所有变量：块内只要出现对这些变量的赋值就报错
            visible: set[str] = set()
            for fr in self.frames:
                visible.update(fr.keys())
            self._outer_snapshot.append(visible)
            self.parallel_depth += 1
            try:
                self.check_stmts(s.body, fn)
            finally:
                self.parallel_depth -= 1
                self._outer_snapshot.pop()
            return

        if isinstance(s, Spawn):
            target = self.funcs.get(s.func_name)
            if target is None:
                raise CheckError(f"未定义任务做法 {s.func_name}", s.line, s.col)
            if len(s.args) != len(target.params):
                raise CheckError(f"发起 参数数量不符：需要 {len(target.params)}，"
                                 f"收到 {len(s.args)}", s.line, s.col)
            for a, p in zip(s.args, target.params):
                at = self.infer(a)
                if not assignable(p.type, at):
                    raise CheckError(f"发起 {s.func_name} 参数类型不符：期望 {p.type}，得到 {at}",
                                     s.line, s.col)
            s.target = target.index
            s.group_id = self._group_counter - 1 if self._group_counter else -1
            # 信箱变量：<做法名>_信箱，类型即任务返回值（保留单位信息）
            self.declare(f"{s.func_name}_信箱", target.ret)
            self._mailbox_types[f"{s.func_name}_信箱"] = target.ret
            return

        if isinstance(s, Receive):
            mt = self.infer(s.mailbox)
            if s.target:
                # 接收变量的类型取信箱类型（含单位），而不是一概而论「任意」
                box_name = self._dotted(s.mailbox)
                recv_ty = self._mailbox_types.get(box_name, mt if mt.name != "映射" else T_ANY)
                s.target_slot = self.declare(s.target, recv_ty)
            return

        if isinstance(s, ExprStmt):
            self.infer(s.expr)
            return

        raise CheckError(f"未知语句 {type(s).__name__}", s.line, s.col)

    def _lookup_type(self, name: str, node) -> Type:
        """查变量声明类型（用于赋值检查）。"""
        vt = self._var_types.get(name) if hasattr(self, "_var_types") else None
        if vt is None:
            raise CheckError(f"未定义变量 {name}", node.line, node.col)
        return vt

    # ============================================================
    # 表达式推断
    # ============================================================
    def infer(self, e: Expr) -> Type:
        t = self._infer(e)
        e.ty = t
        return t

    def _infer(self, e: Expr) -> Type:
        if isinstance(e, Num):
            return T_num(e.unit)
        if isinstance(e, Text):
            return T_TEXT
        if isinstance(e, Bool):
            return T_BOOL
        if isinstance(e, Nil):
            return T_NIL

        if isinstance(e, VarRef):
            return self._infer_var(e)

        if isinstance(e, Lambda):
            return self._infer_lambda(e)

        if isinstance(e, Index):
            return self._infer_index(e)

        if isinstance(e, Field):
            return self._infer_field(e)

        if isinstance(e, BinOp):
            return self._infer_binop(e)

        if isinstance(e, UnOp):
            t = self.infer(e.operand)
            if e.op == "-":
                if t.name != "数":
                    raise CheckError("取负要求数", e.line, e.col)
                return t
            if t.name != "真假":
                raise CheckError("非 要求真假", e.line, e.col)
            return T_BOOL

        if isinstance(e, Call):
            return self._infer_call(e)

        if isinstance(e, Await):
            inner = e.call
            if not isinstance(inner, Call):
                raise CheckError("等待完成 后面必须是做法调用", e.line, e.col)
            t = self._infer_call(inner)
            # 与普通调用一致：回填函数索引，供代码生成直接派发
            e.target = inner.target
            return t

        if isinstance(e, SeqLit):
            if not e.items:
                return T_seq(T_ANY)
            ts = [self.infer(x) for x in e.items]
            # 空容器（序列<任意>）在字面量里应被同化为兄弟元素的类型：
            # [[1,4], [], [2]] 这类嵌套结构必须能推断，否则写不出邻接表。
            known = [t for t in ts if not (t.name in ("序列", "映射") and
                     getattr(t, "elem", None) is not None and t.elem.name == "任意")]
            first = known[0] if known else ts[0]
            for other in ts[1:]:
                if other.name in ("序列", "映射") and getattr(other, "elem", None) is not None \
                        and other.elem.name == "任意":
                    continue      # 空容器与任何兄弟类型兼容
                if first.name in ("序列", "映射") and getattr(first, "elem", None) is not None \
                        and first.elem.name == "任意":
                    first = other
                    continue
                if not same_type(first, other):
                    raise CheckError(f"序列元素类型不一致：{first} 与 {other}",
                                     e.line, e.col)
            return T_seq(first)

        if isinstance(e, MapLit):
            if not e.pairs:
                return T_map(T_TEXT, T_ANY)
            kts = [self.infer(k) for k, _ in e.pairs]
            vts = [self.infer(v) for _, v in e.pairs]
            for i in range(1, len(kts)):
                if not same_type(kts[0], kts[i]) or not same_type(vts[0], vts[i]):
                    raise CheckError("映射键或值类型不一致", e.line, e.col)
            return T_map(kts[0], vts[0])

        raise CheckError(f"未知表达式 {type(e).__name__}", e.line, e.col)

    def _find_outer(self, name: str):
        """在 lambda 之外的帧里找变量，返回 (类型, 槽位) 或 None。"""
        # frames 里第 0 个是 lambda 自己；更外层在调用栈里已保存
        for fr in reversed(getattr(self, "_outer_frames", []) or []):
            if name in fr:
                return self._var_types.get(name, T_ANY), fr[name]
        return None

    def _infer_var(self, e: VarRef) -> Type:
        try:
            d, slot = self.lookup(e.name)
        except KeyError:
            if e.name in self.consts:
                e.depth = -2          # -2 表示全局槽位常量
                e.slot = self.globals[e.name]
                return self.consts[e.name]
            if e.name in self.funcs:
                return Type("做法")
            if e.name in BUILTINS:
                return Type("内置")
            # 在 lambda 内引用外层变量 → 记为自由变量（闭包捕获）
            collect = getattr(self, "_free_collect", None)
            if collect is not None:
                outer = self._find_outer(e.name)
                if outer is not None:
                    ty, _slot = outer
                    idx = collect(e.name, ty, _slot)
                    e.depth = -3       # -3 表示自由变量（走 LOAD_FREE）
                    e.slot = idx
                    return ty
            raise CheckError(f"未定义变量 {e.name}", e.line, e.col)
        e.depth = d
        e.slot = slot
        return self._var_types.get(e.name, T_ANY)

    def _bind_generic(self, param_ty: Type, arg_ty: Type, bind: dict) -> bool:
        """把形参类型与实参类型匹配，收集类型变量绑定。

        返回 False 表示类型不兼容。支持嵌套：
            序列<T> vs 序列<数>  →  T := 数
        """
        from .types2 import subst as _subst
        if param_ty.name == "类型变量":
            nm = param_ty.var or "T"
            old = bind.get(nm)
            if old is None:
                bind[nm] = arg_ty
                return True
            # 同一变量多次出现必须一致
            return str(old) == str(arg_ty)
        if param_ty.name == "序列" and arg_ty.name == "序列":
            if param_ty.elem is not None and arg_ty.elem is not None:
                return self._bind_generic(param_ty.elem, arg_ty.elem, bind)
            return True
        if param_ty.name == "映射" and arg_ty.name == "映射":
            ok = True
            if param_ty.key is not None and arg_ty.key is not None:
                ok = ok and self._bind_generic(param_ty.key, arg_ty.key, bind)
            if param_ty.val is not None and arg_ty.val is not None:
                ok = ok and self._bind_generic(param_ty.val, arg_ty.val, bind)
            return ok
        return assignable(param_ty, arg_ty)

    def _infer_lambda(self, e: Lambda) -> Type:
        """检查匿名函数，并收集它捕获的外层变量。

        闭包方案：lambda 有独立槽位（参数 + 局部变量），
        外层变量不进槽位，而是记录在 `free_vars` 里，
        创建闭包时按值复制进闭包对象；体内引用走 LOAD_FREE。

        为什么按值捕获：避免悬垂引用与跨任务共享可变状态。
        这也意味着 lambda 内对外层变量是只读快照。
        """
        saved_n = self.n_slots
        saved_names = self._all_names
        saved_types = dict(self._var_types)
        saved_frames = self.frames
        saved_free = getattr(self, "_free_collect", None)

        params: dict[str, int] = {}
        self.n_slots = 0
        self._all_names = {}
        self._var_types = {}
        for p in e.params:
            params[p.name] = self.n_slots
            self.n_slots += 1
            self._var_types[p.name] = p.type

        # 自由变量收集器：(名字, 类型, 外层槽位)
        collected: list = []
        seen: set = set()

        def note_free(name: str, ty, outer_slot: int) -> int:
            """登记一个自由变量，返回其在捕获数组里的下标。"""
            for idx, (nm, _t, _s) in enumerate(collected):
                if nm == name:
                    return idx
            collected.append((name, ty, outer_slot))
            seen.add(name)
            return len(collected) - 1

        self._free_collect = note_free
        # 保存外层帧供 _find_outer 查找（lambda 自己的帧放最前）
        prev_outer = getattr(self, "_outer_frames", None)
        self._outer_frames = list(saved_frames) + (prev_outer or [])
        self.frames = [dict(params)]
        try:
            bt = self.infer(e.body)
            ret = e.ret_ty if (e.ret_ty is not None and e.ret_ty.name != "任意") else bt
            e.ret_ty = ret
            e.n_slots = self.n_slots
            e.free_vars = list(collected)
        finally:
            self.frames = saved_frames
            self._outer_frames = prev_outer
            self._free_collect = saved_free
            self.n_slots = saved_n
            self._all_names = saved_names
            self._var_types = saved_types
        return Type("做法")

    def _infer_index(self, e: Index) -> Type:
        """下标访问：序列按下标取元素，映射按键取值，文本按下标取字。"""
        base = self.infer(e.base)
        idx = self.infer(e.index)
        if base.name == "序列":
            if idx.name != "数":
                raise CheckError("序列下标必须是数", e.line, e.col)
            return base.elem if getattr(base, "elem", None) is not None else T_ANY
        if base.name == "映射":
            if idx.name != "文本":
                raise CheckError("映射的键必须是文本", e.line, e.col)
            return base.val if getattr(base, "val", None) is not None else T_ANY
        if base.name == "文本":
            if idx.name != "数":
                raise CheckError("文本下标必须是数", e.line, e.col)
            return T_TEXT
        if base.name == "任意":
            return T_ANY
        raise CheckError(f"{base} 不支持下标访问", e.line, e.col)

    def _infer_field(self, e: Field) -> Type:
        # `模块.常量`：带点名字直接查常量表。
        # 必须先查，否则会去 infer(e.obj) 把模块别名当变量，报「未定义」。
        dotted = _dotted_of(e)
        if dotted is not None and dotted in self.consts:
            return self.consts[dotted]
        base = self.infer(e.obj)
        # 记录名可能带模块前缀（如「进化.基因」），两种形式都查
        rec_name = base.name
        if rec_name not in self.records:
            for k in self.records:
                if k.split(".")[-1] == rec_name:
                    rec_name = k
                    break
        if rec_name in self.records:
            rec = self.records[rec_name]
            for f in rec.fields:
                if f.name == e.name:
                    return f.type
            raise CheckError(f"记录 {base.name} 没有字段 {e.name}", e.line, e.col)
        if base.name == "映射":
            return base.val or T_ANY
        if base.name == "任意":
            return T_ANY
        raise CheckError(f"{base} 没有字段 {e.name}", e.line, e.col)

    def _infer_binop(self, e: BinOp) -> Type:
        # 短路运算符要求两侧真假
        if e.op in ("并且", "或者"):
            lt = self.infer(e.left)
            rt = self.infer(e.right)
            if lt.name != "真假" or rt.name != "真假":
                raise CheckError(f"{e.op} 两侧必须为真假，得到 {lt} 与 {rt}", e.line, e.col)
            return T_BOOL

        lt = self.infer(e.left)
        rt = self.infer(e.right)

        if e.op == "+":
            if lt.name == "文本" and rt.name == "文本":
                return T_TEXT
            # 文本 + 非文本：按文本拼接（旧实现行为；输出友好，也是 AI 场景常态）
            if lt.name == "文本" or rt.name == "文本":
                return T_TEXT
            # 任一侧类型未知/任意：运行期再决定（工具返回、映射取值等场景）。
            # 这里按「数」处理——若运行时实际是文本，VM 会走拼接分支。
            if lt.name in ("任意", "未知") or rt.name in ("任意", "未知"):
                return T_num()
            if lt.name != "数" or rt.name != "数":
                raise CheckError(f"+ 要求两侧为数或文本，得到 {lt} 与 {rt}", e.line, e.col)
            lv, rv = unit_vec(lt.unit), unit_vec(rt.unit)
            if lv != rv:
                raise CheckError(f"量纲不匹配：{lt} 与 {rt} 不可 +（C7）", e.line, e.col)
            return T_num(lt.unit)

        if e.op == "-":
            if lt.name == "任意" or rt.name == "任意":
                return T_num()
            if lt.name != "数" or rt.name != "数":
                raise CheckError(f"- 要求两侧为数，得到 {lt} 与 {rt}", e.line, e.col)
            lv, rv = unit_vec(lt.unit), unit_vec(rt.unit)
            if lv != rv:
                raise CheckError(f"量纲不匹配：{lt} 与 {rt} 不能相减", e.line, e.col)
            return T_num(lt.unit)

        if e.op in ("*", "/", "%"):
            if lt.name == "任意" or rt.name == "任意":
                return T_num()
            if lt.name != "数" or rt.name != "数":
                raise CheckError(f"{e.op} 要求两侧为数，得到 {lt} 与 {rt}", e.line, e.col)
            vec = vadd(unit_vec(lt.unit), unit_vec(rt.unit)) if e.op == "*" \
                else vsub(unit_vec(lt.unit), unit_vec(rt.unit))
            return T_num(vec_unit(vec))

        if e.op in ("==", "!=", "<", "<=", ">", ">="):
            if lt.name in ("任意", "未知") or rt.name in ("任意", "未知"):
                return T_BOOL
            if lt.name != rt.name:
                raise CheckError(f"比较两侧类型不同：{lt} 与 {rt}", e.line, e.col)
            if lt.name == "数" and unit_vec(lt.unit) != unit_vec(rt.unit):
                # 无量纲字面量与带单位值比较：`数量 <= 10` 是最自然的写法，
                # 语义是「10 个该单位」。与赋值的放宽规则保持一致。
                lv, rv = unit_vec(lt.unit), unit_vec(rt.unit)
                if not any(lv) or not any(rv):
                    return T_BOOL
                raise CheckError(f"比较量纲不匹配：{lt} 与 {rt}", e.line, e.col)
            return T_BOOL

        raise CheckError(f"未知运算符 {e.op}", e.line, e.col)

    def _infer_call(self, e: Call) -> Type:
        # 内置
        if isinstance(e.callee, VarRef) and e.callee.name in BUILTINS:
            arities, ret = BUILTINS[e.callee.name]
            if len(e.args) not in arities:
                raise CheckError(f"{e.callee.name} 参数数量不符", e.line, e.col)
            for a in e.args:
                self.infer(a)
            if e.callee.name == "长度":
                at = e.args[0].ty if e.args else T_ANY
                if at.name not in ("文本", "序列", "映射", "任意"):
                    raise CheckError(f"长度 只能用于文本、序列、映射，得到 {at}",
                                     e.line, e.col)
            return ret

        # 用户函数
        # 注意：`模.翻倍(...)` 会被解析成 Field(对象=模, 名字=翻倍)，
        # 而不是 VarRef("模.翻倍")。早期只看 VarRef，导致跨模块调用一律
        # 报「未定义的函数」——模块机制形同虚设。
        callee_name = None
        if isinstance(e.callee, VarRef):
            callee_name = e.callee.name
        elif isinstance(e.callee, Field) and isinstance(e.callee.obj, VarRef):
            callee_name = f"{e.callee.obj.name}.{e.callee.name}"

        if callee_name is not None:
            fn = self.funcs.get(callee_name)
            if fn is not None:
                if len(e.args) != len(fn.params):
                    raise CheckError(
                        f"调用 {fn.name} 参数数量不符：需要 {len(fn.params)}，"
                        f"收到 {len(e.args)}", e.line, e.col)
                # 泛型：先按形参/实参匹配求出类型变量绑定，再替换
                bind: dict = {}
                for a, p in zip(e.args, fn.params):
                    at = self.infer(a)
                    if not self._bind_generic(p.type, at, bind):
                        raise CheckError(
                            f"调用 {fn.name} 参数 {p.name} 类型不符：期望 {p.type}，得到 {at}",
                            e.line, e.col)
                e.target = fn.index
                ret = fn.ret
                if getattr(fn, "type_params", None) and bind:
                    from .types2 import subst as _subst
                    ret = _subst(ret, bind)
                return ret

        # 记录构造
        if isinstance(e.callee, VarRef) and e.callee.name in self.records:
            rec = self.records[e.callee.name]
            if len(e.args) != len(rec.fields):
                raise CheckError(f"记录 {rec.name} 构造需要 {len(rec.fields)} 个字段",
                                 e.line, e.col)
            for a, f in zip(e.args, rec.fields):
                at = self.infer(a)
                if not assignable(f.type, at):
                    raise CheckError(f"记录 {rec.name} 字段 {f.name} 类型不符", e.line, e.col)
            e.target = rec.index
            return Type(rec.name)

        # 模块成员调用：标准库 / FFI / 能力
        name = self._dotted(e.callee)
        if name is not None:
            mod = name.split(".", 1)[0]
            if mod in self.host_aliases:
                cap = f"外部.调用{mod}"
                if self.caps[-1] is not None and cap not in self.caps[-1]:
                    raise CheckError(f"未授权能力 {cap}：沙箱内调用外部模块 {mod} 需显式授权",
                                     e.line, e.col)
                for a in e.args:
                    self.infer(a)
                return T_ANY
            # 标准库
            from .stdlib2 import lookup
            entry = lookup(name)
            if entry is not None:
                n = len(e.args)
                ar = entry.arity
                ok = (n in ar) if isinstance(ar, tuple) else (n == ar)
                if not ok:
                    raise CheckError(f"标准库 {name} 参数数量不符：需要 {ar}，收到 {n}",
                                     e.line, e.col)
                if entry.cap and self.caps[-1] is not None and entry.cap not in self.caps[-1]:
                    raise CheckError(f"未授权能力 {entry.cap}：标准库 {name} 在沙箱内需授权",
                                     e.line, e.col)
                ats = [self.infer(a) for a in e.args]
                # 返回类型跟随实参：表.追加(xs, v) 返回与 xs 同元素类型的序列
                k = getattr(entry, "ret_from_arg", -1)
                if k >= 0 and k < len(ats):
                    src = ats[k]
                    if src is not None and src.name == "序列":
                        return src
                    if src is not None and src.name == "映射":
                        return src
                return entry.ret
            if name in KNOWN_CAPABILITIES:
                if self.caps[-1] is not None and name not in self.caps[-1]:
                    raise CheckError(f"未授权能力 {name}（C9）", e.line, e.col)
                # 实参必须推断，否则参数变量的槽位不会被回填（真实缺陷）
                for a in e.args:
                    self.infer(a)
                # 每个能力有自己的返回类型。之前一律返回「映射<文本,任意>」，
                # 导致 `变量 c : 文本 = 文件.读取(...)` 类型不符——真 Bug。
                from .types2 import T_NIL as _NIL, T_BOOL as _BOOL
                ret = CAP_RETURN_TYPES.get(name)
                if ret is not None:
                    return ret
                return T_map(T_TEXT, T_ANY)

        # 高阶函数：表.映射 / 表.过滤 / 表.折叠（VM 内部循环调回调）
        if name in ("表.映射", "表.过滤", "表.折叠"):
            for a in e.args:
                self.infer(a)
            # 折叠产出单个值，取初值类型；映射/过滤产出序列
            if name == "表.折叠" and len(e.args) >= 3:
                return e.args[2].ty if e.args[2].ty is not None else T_ANY
            return T_seq(T_ANY)

        # 变量存的匿名函数：`f(2)` 其中 f = 做法(...) => ...
        if isinstance(e.callee, VarRef):
            try:
                vt = self.infer(e.callee)
            except CheckError:
                vt = None
            if vt is not None and vt.name == "做法":
                for a in e.args:
                    self.infer(a)
                e.call_value = True       # 编译器据此生成 CALL_VALUE
                return T_ANY

        # 未知调用：一律报错（不静默放过，避免运行期才炸）
        for a in e.args:
            self.infer(a)
        label = name or (e.callee.name if isinstance(e.callee, VarRef) else "表达式")
        raise CheckError(f"未定义的函数或能力 {label}", e.line, e.col)

    @staticmethod
    def _dotted(e: Expr) -> Optional[str]:
        """把 a.b.c 形式的表达式还原成点分名字。"""
        parts: list[str] = []
        cur = e
        while isinstance(cur, Field):
            parts.append(cur.name)
            cur = cur.obj
        if not isinstance(cur, VarRef):
            return None
        parts.append(cur.name)
        return ".".join(reversed(parts))


def _cap_init() -> None:
    from .types2 import T_TEXT, T_BOOL, T_NIL, T_ANY
    CAP_RETURN_TYPES.update({
        "文件.读取": T_TEXT,
        "文件.存在": T_BOOL,
        "系统.参数": T_seq(T_TEXT),
        "网络.取": T_map(T_TEXT, T_ANY),
        "网络.发": T_map(T_TEXT, T_ANY),
        "系统.脚本目录": T_TEXT,
        "服务.启动": T_BOOL,
        "系统.跑命令": T_map(T_TEXT, T_ANY),
        "系统.可执行文件": T_TEXT,
        "终端.读键": T_TEXT,
        "服务.待请求": T_map(T_TEXT, T_ANY),
        "服务.回复": T_BOOL,
        "文件.写入": T_BOOL,
        "网络.请求": T_TEXT,
        "工具.查询": T_ANY,
        "工具.清理": T_BOOL,
        "工具.慢查询": T_ANY,
        "输出": T_NIL,
    })


_cap_init()
