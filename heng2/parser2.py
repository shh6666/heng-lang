"""衡语内核 v2 —— 递归下降解析器。

产出 AST，并在解析期完成「作用域层深」标注（VarRef.depth）：
这让代码生成阶段能把每个变量引用直接翻译成槽位访问，
运行期不再做任何名字查找——这是新内核比旧实现快的关键之一。
"""

from __future__ import annotations

from typing import Optional

from .token import K, Token
from .ast2 import (
    Expr, Num, Text, Bool, Nil, VarRef, Field, BinOp, UnOp, Call, Await,
    Index, SetIndex, SetField, Lambda, SeqLit, MapLit, Stmt, Let, SetVar, If, Loop, Repeat, Each, Return,
    Assert, Raise, Try, Say, Exec, Observe, Sandbox, Authorize, Parallel,
    Spawn, Receive, ExprStmt, Contract, Param, Func, Record, Import, Program,
)
from .types2 import Type, Unit, EMPTY_UNIT, T_NUM, T_TEXT, T_BOOL, T_NIL, T_ANY


class ParseError(Exception):
    def __init__(self, msg: str, tok: Optional[Token] = None):
        if tok is not None:
            super().__init__(f"{tok.line}:{tok.col} 语法错误：{msg}"
                             f"（遇到 {tok.kind.name} {tok.value!r}）")
        else:
            super().__init__(f"语法错误：{msg}")
        self.tok = tok


class Scope:
    """词法作用域：记录本层声明的变量名，用于解析引用深度。"""

    __slots__ = ("names", "parent")

    def __init__(self, parent: Optional["Scope"] = None):
        self.names: dict[str, int] = {}   # 名字 → 槽位（解析期先占位，生成期确定）
        self.parent = parent

    def declare(self, name: str) -> None:
        self.names[name] = len(self.names)

    def depth_of(self, name: str) -> int:
        """返回名字相对当前作用域的层深；找不到返回 -1。"""
        d = 0
        s: Optional[Scope] = self
        while s is not None:
            if name in s.names:
                return d
            s = s.parent
            d += 1
        return -1


class Parser:
    def __init__(self, tokens: list[Token]):
        self.toks = tokens
        self.i = 0
        self.scope = Scope()
        self.globals: set[str] = set()      # 全局名字（常量/函数/记录）
        self.func_index: dict[str, int] = {}
        self.record_index: dict[str, int] = {}
        self.error_types: set[str] = set()

    # ---- token 基础 ----
    def cur(self) -> Token:
        return self.toks[self.i]

    def peek(self, k: int = 1) -> Token:
        j = self.i + k
        return self.toks[j] if j < len(self.toks) else self.toks[-1]

    def at(self, k: K) -> bool:
        # 括号内允许换行：`表.追加(xs,\n  v)` 这种写法在长表达式里很必要。
        # 早期不支持，中大型项目只能把长表达式硬写成一行，极难维护。
        if k != K.NEWLINE and getattr(self, "_soft_nl", 0) > 0:
            while self.toks[self.i].kind == K.NEWLINE:
                self.i += 1
        return self.toks[self.i].kind == k

    def accept(self, k: K) -> Optional[Token]:
        if self.at(k):
            t = self.toks[self.i]
            self.i += 1
            return t
        return None

    def _enter_soft(self) -> None:
        self._soft_nl = getattr(self, "_soft_nl", 0) + 1

    def _exit_soft(self) -> None:
        self._soft_nl = max(0, getattr(self, "_soft_nl", 0) - 1)

    def expect(self, k: K, what: str = "") -> Token:
        if self.at(k):
            t = self.toks[self.i]
            self.i += 1
            return t
        label = what or k.name
        raise ParseError(f"期望 {label}", self.cur())

    def skip_nl(self) -> None:
        while self.at(K.NEWLINE):
            self.i += 1

    # ============================================================
    # 程序
    # ============================================================
    def parse_program(self) -> Program:
        prog = Program()
        self.skip_nl()

        # 导入段
        while self.at(K.KW_IMPORT):
            prog.imports.append(self.parse_import())
            self.skip_nl()

        while not self.at(K.EOF):
            self.skip_nl()
            if self.at(K.EOF):
                break
            if self.at(K.KW_START):
                prog.entry = self.parse_entry()
                continue
            if self.at(K.KW_CONST):
                prog.consts.append(self.parse_const())
                continue
            if self.at(K.KW_DEF):
                self.parse_definition(prog)
                continue
            if self.at(K.KW_RECORD):
                prog.records.append(self.parse_record())
                continue
            raise ParseError("期望顶层定义（导入/定义/常量/记录/开始）", self.cur())

        return prog

    def parse_import(self) -> Import:
        t = self.expect(K.KW_IMPORT)
        if self.at(K.KW_FOREIGN):
            self.i += 1
            path = self._import_target()
            alias = self._import_alias() or path.split(".")[0]
            return Import("外部", path, alias, t.line, t.col)
        path = self._import_target()
        alias = self._import_alias() or path.rsplit("/", 1)[-1].removesuffix(".hng")
        return Import("模块", path, alias, t.line, t.col)

    def _import_target(self) -> str:
        if self.at(K.STRING):
            return self.expect(K.STRING).value
        parts: list[str] = []
        while self.at(K.IDENT) or self.at(K.DOT) or self.at(K.SLASH):
            tok = self.cur()
            if tok.kind == K.IDENT and tok.value == "之":
                break
            self.i += 1
            parts.append({K.DOT: ".", K.SLASH: "/"}.get(tok.kind, tok.value))
        if not parts:
            raise ParseError("导入 后期望模块名或文件路径", self.cur())
        return "".join(parts)

    def _import_alias(self) -> Optional[str]:
        if self.at(K.IDENT) and self.cur().value == "之" and self.peek().kind == K.IDENT:
            self.i += 1
            return self.expect(K.IDENT).value
        return None

    def parse_const(self) -> tuple[str, Type, Expr]:
        self.expect(K.KW_CONST)
        name = self.expect(K.IDENT, "常量名").value
        self.expect(K.COLON)
        typ = self.parse_type()
        self.expect(K.EQ, "= ")
        val = self.parse_expr()
        self.skip_nl()
        self.globals.add(name)
        return name, typ, val

    def parse_definition(self, prog: Program) -> None:
        """定义 做法 名(...) -> 类型: ... 结束

        兼容旧写法：「定义 函数 名」「做法 名」「定义 记录 名」。
        """
        t = self.expect(K.KW_DEF)
        # 「定义 记录 名」走记录定义
        if self.at(K.KW_RECORD):
            self.i -= 1
            self.expect(K.KW_DEF)
            prog.records.append(self.parse_record())
            return
        # 「定义 做法 名」与「做法 名」两种写法
        if self.at(K.KW_DEF):
            self.i += 1
        name = self.expect(K.IDENT, "做法名").value
        # 泛型参数：定义 做法 恒等<T>(x : T) -> T
        type_params: list[str] = []
        if self.at(K.LT):
            self.i += 1
            while True:
                type_params.append(self.expect(K.IDENT, "类型参数").value)
                if not self.accept(K.COMMA):
                    break
            self.expect(K.GT)
        prev_tp = getattr(self, "type_param_scope", ())
        self.type_param_scope = tuple(type_params)
        self.expect(K.LPAREN)
        params: list[Param] = []
        if not self.at(K.RPAREN):
            while True:
                pn_tok = self._name_token()
                if pn_tok is None:
                    raise ParseError("期望 参数名", self.cur())
                pname = pn_tok.value
                self.i += 1
                self.expect(K.COLON)
                ptype = self.parse_type()
                params.append(Param(pname, ptype))
                if not self.accept(K.COMMA):
                    break
        self.expect(K.RPAREN)
        self.expect(K.ARROW, "-> 返回类型")
        ret = self.parse_type()

        errors: list[str] = []
        if self.at(K.KW_MAY_RAISE):
            self.i += 1
            while True:
                errors.append(self.parse_error_type())
                if not self.accept(K.COMMA):
                    break

        self.accept(K.COLON)
        self.expect(K.NEWLINE, "此处需换行")
        self.skip_nl()

        # 函数体作用域：参数先入作用域
        saved = self.scope
        self.scope = Scope(saved)
        for p in params:
            self.scope.declare(p.name)

        contract: Optional[Contract] = None
        if self.at(K.KW_CONTRACT):
            self.i += 1
            self.accept(K.COLON)
            self.expect(K.NEWLINE)
            self.skip_nl()
            req = ens = None
            if self.at(K.KW_REQUIRE):
                self.i += 1
                self.accept(K.COLON)
                req = self.parse_expr()
                self.skip_nl()
            if self.at(K.KW_ENSURE):
                self.i += 1
                self.accept(K.COLON)
                # 后置条件里的 结果 代表返回值
                self.scope.declare("结果")
                ens = self.parse_expr()
                self.skip_nl()
            self.expect(K.KW_END)
            self.skip_nl()
            contract = Contract(req, ens)

        body = self.parse_block()
        self.expect(K.KW_END, "结束")
        self.skip_nl()
        self.scope = saved

        fn = Func(name, params, ret, errors, contract, body, t.line, t.col)
        fn.type_params = list(type_params)
        fn.index = len(prog.funcs)
        self.func_index[name] = fn.index
        self.globals.add(name)
        prog.funcs.append(fn)

    def parse_record(self) -> Record:
        t = self.expect(K.KW_RECORD)
        name = self.expect(K.IDENT, "记录名").value
        self.accept(K.COLON)
        self.expect(K.NEWLINE)
        self.skip_nl()
        fields: list[Param] = []
        while not self.at(K.KW_END):
            self.skip_nl()
            if self.at(K.EOF):
                raise ParseError("记录定义缺少 结束", self.cur())
            self.expect(K.KW_FIELD)
            fname = self.expect(K.IDENT, "字段名").value
            self.expect(K.COLON)
            fields.append(Param(fname, self.parse_type()))
            self.skip_nl()
        self.expect(K.KW_END)
        self.skip_nl()
        self.globals.add(name)
        return Record(name, fields, t.line, t.col)

    def parse_entry(self) -> list[Stmt]:
        self.expect(K.KW_START)
        self.accept(K.COLON)          # 冒号可选（零基础首课不打冒号）
        self.expect(K.NEWLINE)
        self.skip_nl()
        saved = self.scope
        self.scope = Scope(saved)
        body = self.parse_block()
        self.expect(K.KW_END)
        self.skip_nl()
        self.scope = saved
        return body

    def parse_error_type(self) -> str:
        self.expect(K.KW_ERROR)
        self.expect(K.LT)
        name = self.expect(K.IDENT, "错误类别").value
        self.expect(K.GT)
        self.error_types.add(name)
        return name

    # ============================================================
    # 语句
    # ============================================================
    def parse_block(self) -> list[Stmt]:
        out: list[Stmt] = []
        stop = (K.KW_END, K.KW_ELSE, K.KW_CATCH, K.KW_FINALLY, K.EOF)
        while True:
            self.skip_nl()
            if self.cur().kind in stop:
                break
            out.append(self.parse_stmt())
            if self.cur().kind not in stop and not self.at(K.NEWLINE):
                raise ParseError("语句后需换行", self.cur())
            self.skip_nl()
        return out

    def parse_stmt(self) -> Stmt:
        t = self.cur()
        k = t.kind

        if k == K.KW_LET:
            return self.parse_let()
        if k == K.KW_IF:
            return self.parse_if()
        if k == K.KW_LOOP:
            return self.parse_loop()
        if k == K.KW_REPEAT:
            return self.parse_repeat()
        if k == K.KW_EACH:
            return self.parse_each()
        if k == K.KW_RETURN:
            self.i += 1
            val = None if self.cur().kind in (K.NEWLINE, K.KW_END) else self.parse_expr()
            return Return(t.line, t.col, val)
        if k == K.KW_ASSERT:
            self.i += 1
            return Assert(t.line, t.col, self.parse_expr())
        if k == K.KW_RAISE:
            self.i += 1
            et = self.parse_error_type()
            self.expect(K.LPAREN)
            msg = self.parse_expr()
            self.expect(K.RPAREN)
            return Raise(t.line, t.col, et, msg)
        if k == K.KW_TRY:
            return self.parse_try()
        if k == K.KW_SAY:
            self.i += 1
            # `说出 (a + b) * 3` 里的括号属于表达式本身，不是 `说出(...)` 的调用括号。
            # 判别：括号内表达式解析后若紧跟 `)`，且 `)` 之后还有运算符，
            # 则整体按表达式解析；否则按 `说出(表达式)` 处理。
            if self.at(K.LPAREN):
                save = self.i
                self.i += 1
                try:
                    inner = self.parse_expr()
                    self.expect(K.RPAREN)
                    nxt = self.cur().kind
                    if nxt in (K.NEWLINE, K.EOF, K.KW_END):
                        return Say(t.line, t.col, inner)
                except Exception:
                    pass
                self.i = save          # 不是调用形式，回退按表达式解析
            v = self.parse_expr()
            return Say(t.line, t.col, v)
        if k == K.KW_EXEC:
            return self.parse_exec()
        if k == K.KW_OBSERVE:
            return self.parse_observe()
        if k == K.KW_SANDBOX:
            self.i += 1
            self.accept(K.COLON)
            self.expect(K.NEWLINE)
            self.skip_nl()
            body = self.parse_block()
            self.expect(K.KW_END)
            return Sandbox(t.line, t.col, body)
        if k == K.KW_AUTHORIZE:
            self.i += 1
            caps: list[str] = []
            while True:
                # 能力名首段允许关键字：`外部.调用数学`（FFI 能力名以「外部」开头）
                tok = self.cur()
                if tok.kind in (K.IDENT, K.KW_FOREIGN):
                    first = tok.value
                    self.i += 1
                else:
                    raise ParseError("期望 能力名", tok)
                if self.accept(K.DOT):
                    caps.append(f"{first}.{self.expect(K.IDENT, '能力方法').value}")
                else:
                    caps.append(first)
                if not self.accept(K.COMMA):
                    break
            return Authorize(t.line, t.col, caps)
        if k == K.KW_PARALLEL:
            self.i += 1
            self.accept(K.COLON)
            self.expect(K.NEWLINE)
            self.skip_nl()
            body = self.parse_block()
            self.expect(K.KW_END)
            return Parallel(t.line, t.col, body)
        if k == K.KW_SPAWN:
            self.i += 1
            fname = self.expect(K.IDENT, "任务做法名").value
            self.expect(K.LPAREN)
            args = self.parse_args()
            self.expect(K.RPAREN)
            return Spawn(t.line, t.col, fname, args)
        if k == K.KW_RECEIVE:
            self.i += 1
            self.expect(K.LPAREN)
            box = self.parse_expr()
            self.expect(K.RPAREN)
            target = None
            if self.at(K.KW_AS):
                self.i += 1
                target = self.expect(K.IDENT, "接收变量名").value
            return Receive(t.line, t.col, box, target)

        # 赋值：将 表达式 放到 变量 / 变量 = 表达式 / 变量 := 表达式
        return self.parse_assign_or_expr()

    def _name_token(self):
        """可作变量名/参数名的 token。

        关键字里有些是业务高频词（数、次、值、时间……），
        直接用 IDENT 判断会让 `变量 数 : 数 = 5` 报语法错误——
        实测四批业务项目每人都在这里栽过一次。
        这里允许「能当名字的关键字」出现在名字位置，靠上下文区分。
        """
        k = self.cur()
        if k.kind == K.IDENT:
            return k
        if k.kind in getattr(self, "_NAME_LIKE_KW", ()):
            return k
        return None

    def parse_let(self) -> Let:
        t = self.expect(K.KW_LET)
        # 「将 值 放到 变量」是赋值，不是声明——旧写法兼容
        if self._name_token() is not None:
            save = self.i
            name = self.cur().value
            nxt = self.peek(1).kind
            if nxt == K.COLON:
                self.i += 1
                self.expect(K.COLON)
                typ = self.parse_type()
                init = None
                if self.accept(K.EQ):
                    init = self.parse_expr()
                self.scope.declare(name)
                return Let(t.line, t.col, name, typ, init, False)
            if nxt in (K.EQ,):
                self.i += 1
                self.expect(K.EQ)
                # 无类型标注：类型由初值推断
                init = self.parse_expr()
                self.scope.declare(name)
                return Let(t.line, t.col, name, T_ANY, init, False)
            self.i = save

        # 「将 表达式 放到 变量」或「将 表达式 放到 容器[下标]」
        val = self.parse_expr()
        self.expect(K.KW_AS, "放到")
        # 赋值目标：支持 a[i]、a[i][j]、以及记录字段 a.b[i] 等链式下标。
        # 解析完整后缀链，最后一个 [...] 作为写入位置。
        if self.at(K.IDENT):
            save2 = self.i
            name = self.cur().value
            self.i += 1
            node: Optional[Expr] = None
            while self.at(K.LBRACK) or self.at(K.DOT):
                if self.at(K.LBRACK):
                    self.i += 1
                    idx = self.parse_expr()
                    self.expect(K.RBRACK)
                    inner = node if node is not None else VarRef(
                        t.line, t.col, name, depth=self.scope.depth_of(name))
                    node = Index(t.line, t.col, inner, idx)
                else:
                    self.i += 1
                    inner = node if node is not None else VarRef(
                        t.line, t.col, name, depth=self.scope.depth_of(name))
                    node = Field(t.line, t.col, inner, self._member_name())
            if node is not None and isinstance(node, Index):
                return SetIndex(t.line, t.col, node.base, node.index, val)
            if node is not None and isinstance(node, Field):
                # 记录字段赋值：将 v 放到 r.字段（字段本身是下标则走上一条）
                return SetField(t.line, t.col, node.obj, node.name, val)
            self.i = save2
        target = self.expect(K.IDENT, "赋值目标").value
        depth = self.scope.depth_of(target)
        return SetVar(t.line, t.col, target, val, depth)

    def parse_assign_or_expr(self) -> Stmt:
        """赋值或表达式语句。

        形式：变量 = 表达式 / 变量 := 表达式 / 表达式（求值丢弃）

        **必须先判断赋值再解析表达式**：表达式语法里 `=` 也作相等比较
        （向后兼容旧文件），若先 parse_expr，`i = i + 1` 会被整体吃掉成比较，
        赋值静默失效——这是回归测试抓到的真实 bug。
        """
        t = self.cur()
        if self.at(K.IDENT) and self.peek().kind in (K.EQ,):
            name = self.cur().value
            self.i += 2                  # 跳过 名字 与 =
            value = self.parse_expr()
            depth = self.scope.depth_of(name)
            return SetVar(t.line, t.col, name, value, depth)
        expr = self.parse_expr()
        return ExprStmt(t.line, t.col, expr)

    def parse_if(self) -> If:
        t = self.expect(K.KW_IF)
        cond = self.parse_expr()
        self.accept(K.COLON)
        self.expect(K.NEWLINE)
        self.skip_nl()
        then_body = self.parse_block()
        else_body = None
        if self.at(K.KW_ELSE):
            self.i += 1
            self.accept(K.COLON)
            self.expect(K.NEWLINE)
            self.skip_nl()
            else_body = self.parse_block()
        self.expect(K.KW_END)
        return If(t.line, t.col, cond, then_body, else_body)

    def parse_loop(self) -> Loop:
        t = self.expect(K.KW_LOOP)
        cond = self.parse_expr()
        self.accept(K.COLON)
        self.expect(K.NEWLINE)
        self.skip_nl()
        body = self.parse_block()
        self.expect(K.KW_END)
        return Loop(t.line, t.col, cond, body)

    def parse_repeat(self) -> Repeat:
        t = self.expect(K.KW_REPEAT)
        times = self.parse_expr()
        # 「次」是单位，会被数字单位逻辑吸收；此处可选
        if self.at(K.IDENT) and self.cur().value == "次":
            self.i += 1
        self.accept(K.COLON)
        self.expect(K.NEWLINE)
        self.skip_nl()
        body = self.parse_block()
        self.expect(K.KW_END)
        return Repeat(t.line, t.col, times, body)

    def parse_each(self) -> Each:
        t = self.expect(K.KW_EACH)
        iterable = self.parse_expr()
        self.expect(K.KW_AS, "为")
        var = self.expect(K.IDENT, "遍历变量名").value
        self.accept(K.COLON)
        self.expect(K.NEWLINE)
        self.skip_nl()
        saved = self.scope
        self.scope = Scope(saved)
        self.scope.declare(var)
        body = self.parse_block()
        self.expect(K.KW_END)
        self.scope = saved
        return Each(t.line, t.col, iterable, var, body)

    def parse_try(self) -> Try:
        t = self.expect(K.KW_TRY)
        self.accept(K.COLON)
        self.expect(K.NEWLINE)
        self.skip_nl()
        body = self.parse_block()
        catch_type = catch_var = None
        catch_body = None
        if self.at(K.KW_CATCH):
            self.i += 1
            catch_type = self.parse_error_type()
            self.expect(K.KW_AS, "为")
            catch_var = self.expect(K.IDENT, "捕获变量名").value
            self.accept(K.COLON)
            self.expect(K.NEWLINE)
            self.skip_nl()
            saved = self.scope
            self.scope = Scope(saved)
            self.scope.declare(catch_var)
            catch_body = self.parse_block()
            self.scope = saved
        finally_body = None
        if self.at(K.KW_FINALLY):
            self.i += 1
            self.accept(K.COLON)
            self.expect(K.NEWLINE)
            self.skip_nl()
            finally_body = self.parse_block()
        self.expect(K.KW_END)
        return Try(t.line, t.col, body, catch_type, catch_var, catch_body, finally_body)

    def parse_exec(self) -> Exec:
        t = self.expect(K.KW_EXEC)
        if self.at(K.IDENT) and self.peek().kind == K.EQ:
            name = self.expect(K.IDENT).value
            self.expect(K.EQ)
            self.scope.declare(name)
            return Exec(t.line, t.col, name, self.parse_expr())
        return Exec(t.line, t.col, "_", self.parse_expr())

    def parse_observe(self) -> Observe:
        t = self.expect(K.KW_OBSERVE)
        name = self.expect(K.IDENT, "观察变量名").value
        self.accept(K.COLON)
        self.expect(K.NEWLINE)
        self.skip_nl()
        body = self.parse_block()
        self.expect(K.KW_END)
        return Observe(t.line, t.col, name, body)

    # ============================================================
    # 类型
    # ============================================================
    def parse_type(self) -> Type:
        t = self.cur()
        if self.at(K.KW_NUM):
            self.i += 1
            unit = EMPTY_UNIT
            if self.at(K.LT):
                unit = self._unit_angle()
            elif self.at(K.IDENT) and self._is_unit_name(self.cur().value):
                unit = self._unit_bare()
            return Type("数", unit)
        if self.at(K.KW_TEXT):
            self.i += 1
            return T_TEXT
        if self.at(K.KW_BOOL):
            self.i += 1
            return T_BOOL
        if self.at(K.KW_NIL):
            self.i += 1
            return T_NIL
        if self.at(K.KW_SEQ):
            self.i += 1
            if self.at(K.LT):
                self.i += 1
                elem = self.parse_type()
                self.expect(K.GT)
            else:
                elem = self.parse_type()
            return Type("序列", elem=elem)
        if self.at(K.KW_MAP):
            self.i += 1
            if self.at(K.LT):
                self.i += 1
                k = self.parse_type()
                self.expect(K.COMMA)
                v = self.parse_type()
                self.expect(K.GT)
            else:
                k = self.parse_type()
                v = self.parse_type()
            return Type("映射", key=k, val=v)
        if self.at(K.IDENT):
            self.i += 1
            # 单字母大写标识符（T / U / K / V）视为类型变量，
            # 由调用点按实参类型替换；其余按记录名处理。
            nm = t.value
            # 只有显式声明在 <...> 里的名字才是类型变量。
            # 早期按「单字母大写」猜测，导致记录名 R / T 被误判为类型变量，
            # 变量声明 `变量 r : R = R(1)` 直接类型不符。
            if nm in getattr(self, "type_param_scope", ()):
                return Type("类型变量", var=nm)
            return Type(t.value)
        raise ParseError("期望类型", t)

    # 裸写单位时（`数 元`）允许的名字。业务里高频的单位都收进来——
    # 早期只有 9 个，写医疗/物流业务时 `数 公斤` 直接语法错误。
    # 允许当变量名用的关键字（业务里高频，禁用会逼用户造别扭的名字）
    # 「数」「文本」在业务里极常被用作变量名（数量、文本内容…），
    # 禁掉会逼用户写成「件数」「内容」这种别扭名字。
    _NAME_LIKE_KW = frozenset((K.KW_NUM, K.KW_TEXT))

    _UNIT_NAMES = frozenset((
        # 基础量
        "米", "秒", "千克", "克", "吨", "升", "毫升", "摄氏度", "开尔文", "比例",
        # 时间
        "分钟", "小时", "天", "周", "月", "年", "毫秒",
        # 长度与面积体积
        "公里", "厘米", "毫米", "平方米", "立方米",
        # 重量
        "公斤", "毫克", "斤",
        # 商业计数
        "元", "个", "次", "件", "箱", "台", "套", "张", "条", "人", "户", "单",
        "份", "笔", "种", "门", "道", "篇", "部", "期", "股",
        # 速率与比率
        "元/个", "元/次", "元/件", "元/月", "元/年", "元/户", "元/单",
        "个/人", "件/箱", "次/天", "次/月", "人/月", "元/人", "元/公斤",
        "公斤/人", "毫克/公斤", "公里/小时", "元/小时", "元/平方米",
        # 其他常用
        "分", "秒/次", "度", "点", "倍", "次/人",
    ))

    def _is_unit_name(self, name: str) -> bool:
        return name in self._UNIT_NAMES

    def _unit_angle(self) -> Unit:
        self.expect(K.LT)
        num = [self.expect(K.IDENT, "单位名").value]
        while self.at(K.STAR):
            self.i += 1
            num.append(self.expect(K.IDENT, "单位名").value)
        den: list[str] = []
        if self.accept(K.SLASH):
            den.append(self.expect(K.IDENT, "单位名").value)
            while self.at(K.SLASH):
                self.i += 1
                den.append(self.expect(K.IDENT, "单位名").value)
        self.expect(K.GT)
        return Unit(tuple(num), tuple(den))

    def _unit_bare(self) -> Unit:
        num = [self.expect(K.IDENT, "单位名").value]
        den: list[str] = []
        if self.accept(K.SLASH):
            den.append(self.expect(K.IDENT, "单位名").value)
            while self.at(K.SLASH):
                self.i += 1
                den.append(self.expect(K.IDENT, "单位名").value)
        return Unit(tuple(num), tuple(den))

    # ============================================================
    # 表达式（优先级爬升）
    # ============================================================
    def parse_expr(self) -> Expr:
        return self.parse_or()

    def parse_or(self) -> Expr:
        left = self.parse_and()
        while self.at(K.KW_OR):
            t = self.cur()
            self.i += 1
            left = BinOp(t.line, t.col, "或者", left, self.parse_and())
        return left

    def parse_and(self) -> Expr:
        left = self.parse_not()
        while self.at(K.KW_AND):
            t = self.cur()
            self.i += 1
            left = BinOp(t.line, t.col, "并且", left, self.parse_not())
        return left

    def parse_not(self) -> Expr:
        if self.at(K.KW_NOT):
            t = self.cur()
            self.i += 1
            return UnOp(t.line, t.col, "非", self.parse_not())
        return self.parse_cmp()

    def parse_cmp(self) -> Expr:
        left = self.parse_add()
        # 相等比较：推荐 ==；同时接受单个 = 作为相等（向后兼容旧 .hng 文件）。
        # 语句层面的赋值由 parse_assign_or_expr 单独识别（只看「变量 = 表达式」开头），
        # 因此表达式内部把 = 当相等不会与赋值混淆。
        ops = {K.EQEQ: "==", K.EQ: "==", K.NE: "!=", K.LT: "<", K.LE: "<=",
               K.GT: ">", K.GE: ">="}
        while self.cur().kind in ops:
            t = self.cur()
            op = ops[t.kind]
            self.i += 1
            left = BinOp(t.line, t.col, op, left, self.parse_add())
        return left

    def parse_add(self) -> Expr:
        left = self.parse_mul()
        while self.at(K.PLUS) or self.at(K.MINUS):
            t = self.cur()
            op = "+" if t.kind == K.PLUS else "-"
            self.i += 1
            left = BinOp(t.line, t.col, op, left, self.parse_mul())
        return left

    def parse_mul(self) -> Expr:
        left = self.parse_unary()
        ops = {K.STAR: "*", K.SLASH: "/", K.PERCENT: "%"}
        while self.cur().kind in ops:
            t = self.cur()
            op = ops[t.kind]
            self.i += 1
            left = BinOp(t.line, t.col, op, left, self.parse_unary())
        return left

    def parse_unary(self) -> Expr:
        t = self.cur()
        if self.at(K.MINUS):
            self.i += 1
            return UnOp(t.line, t.col, "-", self.parse_unary())
        return self.parse_atom()

    def _looks_like_unit_angle(self) -> bool:
        """当前在 <，判断是否为单位后缀 <单位> 而非小于比较。"""
        j = self.i + 1
        if j >= len(self.toks) or self.toks[j].kind != K.IDENT:
            return False
        j += 1
        while j < len(self.toks) and self.toks[j].kind == K.SLASH:
            if j + 1 >= len(self.toks) or self.toks[j + 1].kind != K.IDENT:
                return False
            j += 2
        return j < len(self.toks) and self.toks[j].kind == K.GT

    def parse_atom(self) -> Expr:
        t = self.cur()

        if self.at(K.NUMBER):
            self.i += 1
            unit = EMPTY_UNIT
            if self.at(K.LT) and self._looks_like_unit_angle():
                unit = self._unit_angle()
            elif self.at(K.IDENT) and self._is_unit_name(self.cur().value):
                unit = self._unit_bare()
            return Num(t.line, t.col, float(t.value), unit)

        if self.at(K.STRING):
            self.i += 1
            return Text(t.line, t.col, t.value)

        if self.at(K.KW_TRUE):
            self.i += 1
            return Bool(t.line, t.col, True)
        if self.at(K.KW_FALSE):
            self.i += 1
            return Bool(t.line, t.col, False)
        if self.at(K.KW_NIL):
            self.i += 1
            return Nil(t.line, t.col)

        if self.at(K.KW_DEF) and self.peek().kind == K.LPAREN:
            # 匿名函数：做法(x : 数) -> 数 => 表达式
            self.i += 1
            self.expect(K.LPAREN)
            params: list[Param] = []
            saved_scope = self.scope
            self.scope = Scope(parent=saved_scope)
            try:
                if not self.at(K.RPAREN):
                    while True:
                        pn_tok2 = self._name_token()
                        if pn_tok2 is None:
                            raise ParseError("期望 参数名", self.cur())
                        pn = pn_tok2.value
                        self.i += 1
                        self.expect(K.COLON)
                        pt = self.parse_type()
                        params.append(Param(pn, pt))
                        self.scope.declare(pn)
                        if not self.accept(K.COMMA):
                            break
                self.expect(K.RPAREN)
                ret_ty = T_ANY
                if self.accept(K.ARROW):
                    ret_ty = self.parse_type()
                self.expect(K.FATARROW, "=>")
                body = self.parse_expr()
            finally:
                self.scope = saved_scope
            return Lambda(t.line, t.col, params, body, ret_ty)

        if self.at(K.KW_AWAIT):
            self.i += 1
            inner = self.parse_atom()
            if not isinstance(inner, Call):
                raise ParseError("等待完成 后面必须跟一次做法调用", t)
            return Await(t.line, t.col, inner)

        if self.at(K.LPAREN):
            self.i += 1
            e = self.parse_expr()
            self.expect(K.RPAREN)
            return e

        if self.at(K.LBRACK):
            self.i += 1
            items: list[Expr] = []
            if not self.at(K.RBRACK):
                while True:
                    items.append(self.parse_expr())
                    if not self.accept(K.COMMA):
                        break
            self.expect(K.RBRACK)
            return SeqLit(t.line, t.col, items)

        if self.at(K.LBRACE):
            self.i += 1
            pairs: list[tuple[Expr, Expr]] = []
            if not self.at(K.RBRACE):
                while True:
                    k = self.parse_expr()
                    self.expect(K.COLON)
                    pairs.append((k, self.parse_expr()))
                    if not self.accept(K.COMMA):
                        break
            self.expect(K.RBRACE)
            return MapLit(t.line, t.col, pairs)

        # 标识符，或「能当名字的关键字」（数/文本 在表达式位置就是变量）
        if self.at(K.IDENT) or self.cur().kind in getattr(self, "_NAME_LIKE_KW", ()):
            self.i += 1
            node: Expr = VarRef(t.line, t.col, t.value,
                                depth=self.scope.depth_of(t.value))
            # 后缀：字段访问（.）与下标访问（[...]）可任意交替
            while self.at(K.DOT) or self.at(K.LBRACK):
                if self.at(K.DOT):
                    self.i += 1
                    node = Field(t.line, t.col, node, self._member_name())
                else:
                    self.i += 1
                    idx = self.parse_expr()
                    self.expect(K.RBRACK)
                    node = Index(t.line, t.col, node, idx)
            if self.at(K.LPAREN):
                self.i += 1
                args, kwargs = self.parse_args_kw()
                self.expect(K.RPAREN)
                node = Call(t.line, t.col, node, args, kwargs)
            return node

        raise ParseError("期望表达式", t)

    def _member_name(self) -> str:
        """点号后的成员名（允许关键字，如 文字.长度）。"""
        tok = self.cur()
        if tok.kind in (K.EOF, K.NEWLINE, K.COLON, K.COMMA, K.DOT, K.LPAREN,
                        K.RPAREN, K.LBRACK, K.RBRACK, K.LBRACE, K.RBRACE,
                        K.STRING, K.NUMBER, K.ARROW):
            raise ParseError("期望属性名", tok)
        self.i += 1
        return tok.value

    def parse_args(self) -> list[Expr]:
        args: list[Expr] = []
        if not self.at(K.RPAREN):
            while True:
                args.append(self.parse_expr())
                if not self.accept(K.COMMA):
                    break
        return args

    def parse_args_kw(self) -> tuple[list[Expr], list[tuple[str, Expr]]]:
        args: list[Expr] = []
        kwargs: list[tuple[str, Expr]] = []
        if not self.at(K.RPAREN):
            while True:
                if self.at(K.IDENT) and self.peek().kind == K.EQ:
                    name = self.expect(K.IDENT).value
                    self.expect(K.EQ)
                    kwargs.append((name, self.parse_expr()))
                else:
                    if kwargs:
                        raise ParseError("关键字实参必须写在位置实参之后", self.cur())
                    args.append(self.parse_expr())
                if not self.accept(K.COMMA):
                    break
        return args, kwargs


def parse(src: str) -> Program:
    from .lexer2 import tokenize
    return Parser(tokenize(src)).parse_program()
