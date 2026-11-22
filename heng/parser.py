"""衡语递归下降解析器，严格对应 docs/EBNF.md。"""

from __future__ import annotations

import os
from typing import Optional

from .ast_nodes import (
    Type, Unit, Expr, Number, String, Bool, Null, Var, Attr, Binary, Unary,
    Call, SeqLiteral, MapLiteral, Stmt, VarDecl, Assign, If, Loop, Repeat, ForEach,
    Return, Assert, Throw, Try, Exec, Observe, Sandbox, Authorize, Parallel,
    Spawn, Receive, Output, ExprStmt, Contract, Param, FuncDef, RecordDef, Program,
    ImportSpec,
)
from .lexer import Token, KEYWORDS


class ParseError(Exception):
    def __init__(self, msg: str, tok: Token | None = None):
        if tok is not None:
            super().__init__(f"{tok.line}:{tok.col} 语法错误：{msg}（遇到 {tok.kind} {tok.value!r}）")
        else:
            super().__init__(f"语法错误：{msg}")
        self.tok = tok


class Parser:
    def __init__(self, tokens: list[Token]):
        self.toks = tokens
        self.i = 0

    # ---- token 操作 ----
    def cur(self) -> Token:
        return self.toks[self.i]

    def peek(self, k: int = 1) -> Token:
        j = min(self.i + k, len(self.toks) - 1)
        return self.toks[j]

    def at(self, kind: str) -> bool:
        return self.cur().kind == kind

    def accept(self, kind: str) -> Optional[Token]:
        if self.at(kind):
            t = self.cur()
            self.i += 1
            return t
        return None

    def expect(self, kind: str, msg: str = "") -> Token:
        if self.at(kind):
            t = self.cur()
            self.i += 1
            return t
        raise ParseError(msg or f"期望 {kind}", self.cur())

    def expect_word(self, word: str) -> Token:
        # 只比较 token 种类（kind）——旧词已由词法器归一化为规范种类，
        # 故「结束」既能匹配新词「结束」也能匹配旧词「完毕」。
        if self.cur().kind == word:
            t = self.cur()
            self.i += 1
            return t
        raise ParseError(f"期望关键字 {word}", self.cur())

    def skip_newlines(self) -> None:
        while self.at("NEWLINE"):
            self.i += 1

    # ---- 程序 ----
    def parse_program(self) -> Program:
        prog = Program()
        self.skip_newlines()

        # 导入段
        while self.at("导入"):
            spec = self.parse_import()
            prog.import_specs.append(spec)
            prog.imports.append(spec.alias)
            self.skip_newlines()

        while not self.at("EOF"):
            self.skip_newlines()
            if self.at("EOF"):
                break

            if self.at("开始"):
                prog.entry = self.parse_entry()
                continue

            if self.at("常量"):
                prog.constants.append(self.parse_constant())
                continue

            if self.at("模块"):
                prog.imports.append(self.parse_module(prog))
                continue

            if self.at("定义"):
                if self.peek().kind == "记录":
                    prog.records.append(self.parse_record())
                elif self.peek().kind == "做法":
                    prog.functions.append(self.parse_function())
                else:
                    raise ParseError("定义 后期望 记录 或 做法", self.peek())
                continue

            raise ParseError("期望顶层定义（模块/定义/常量/入口/导入）", self.cur())

        return prog

    def parse_import(self) -> ImportSpec:
        """解析一条导入声明。

        三种写法（别名均可省略，省略时用文件名/模块名的末段）：
          导入 「工具.hng」之 工具        本目录的衡语模块
          导入 lib/统计.hng 之 统计       相对路径（也可带引号）
          导入 外部 「math」之 数学       FFI：宿主 Python 模块
        """
        t = self.expect_word("导入")

        # FFI：导入 外部 「模块名」
        if self.at("外部"):
            self.i += 1
            path = self._import_path()
            alias = self._import_alias() or path.split(".")[0]
            return ImportSpec("外部", path, alias, t.line, t.col)

        path = self._import_path()
        alias = self._import_alias() or os.path.splitext(os.path.basename(path))[0]
        # 裸模块名（无扩展名、无路径）：兼容旧写法 `导入 math` / `导入 工具 之 工具`
        kind = "模块" if ("/" in path or path.endswith(".hng")) else "模块"
        return ImportSpec(kind, path, alias, t.line, t.col)

    def _import_path(self) -> str:
        """导入目标：字符串、或由 IDENT/DOT/SLASH 组成的裸名。

        裸名遇到「之」（别名连接词）即停止——`导入 工具之工具` 已被词法切成
        IDENT(工具) IDENT(之) IDENT(工具)，这里必须只取第一段。
        """
        if self.at("STRING"):
            return self.expect("STRING").value
        parts: list[str] = []
        while self.at("IDENT") or self.at("DOT") or self.at("SLASH"):
            tok = self.cur()
            if tok.kind == "IDENT" and tok.value == "之":
                break
            self.i += 1
            if tok.kind == "DOT":
                parts.append(".")
            elif tok.kind == "SLASH":
                parts.append("/")
            else:
                parts.append(tok.value)
        name = "".join(parts)
        if not name:
            raise ParseError("导入 后期望模块名或文件路径", self.cur())
        return name

    def _import_alias(self) -> Optional[str]:
        """可选的「之 别名」；「之」未收录关键字表，按 IDENT 识别。

        注意：「之」本身不能当别名——否则 `导入 工具 之 工具` 会把第一个
        「工具」后面的「之」误认为别名，导致真正的别名被当成多余 token。
        """
        if self.at("IDENT") and self.cur().value == "之" and self.peek().kind == "IDENT":
            self.i += 1
            return self.expect("IDENT", "之 后期望别名").value
        return None

    def parse_module(self, prog: Program) -> str:
        t = self.expect_word("模块")
        name = self.expect("IDENT", "模块名").value
        self.expect("NEWLINE", "模块声明后需换行")
        self.skip_newlines()
        # 模块体内允许导入与定义，直接并入程序（简化为命名空间前缀）
        while not self.at("EOF") and not self.at("开始"):
            self.skip_newlines()
            if self.at("导入"):
                self.i += 1
                prog.imports.append(self.expect("IDENT", "导入模块名").value)
            elif self.at("定义"):
                if self.peek().kind == "做法":
                    fn = self.parse_function()
                    fn.name = f"{name}.{fn.name}"
                    prog.functions.append(fn)
                elif self.peek().value == "记录":
                    prog.records.append(self.parse_record())
                else:
                    raise ParseError("模块内 定义 后期望 函数 或 记录", self.peek())
            elif self.at("常量"):
                prog.constants.append(self.parse_constant())
            else:
                break
        return name

    def parse_constant(self) -> tuple[str, Type, Expr]:
        self.expect_word("常量")
        name = self.expect("IDENT", "常量名").value
        self.expect("COLON")
        typ = self.parse_type()
        self.expect("EQ", "常量必须有初值")
        val = self.parse_expr()
        self.skip_newlines()
        return name, typ, val

    def parse_record(self) -> RecordDef:
        t = self.expect_word("定义")
        self.expect_word("记录")
        name = self.expect("IDENT", "记录名").value
        self.expect("COLON")
        self.expect("NEWLINE")
        self.skip_newlines()
        fields: list[Param] = []
        while not self.at("结束"):
            self.skip_newlines()
            if self.at("EOF"):
                raise ParseError("记录定义缺少 完毕")
            self.expect_word("字段")
            fname = self.expect("IDENT", "字段名").value
            self.expect("COLON")
            ftyp = self.parse_type()
            fields.append(Param(fname, ftyp))
            self.skip_newlines()
        self.expect_word("结束")
        self.skip_newlines()
        return RecordDef(name, fields, t.line, t.col)

    def parse_function(self) -> FuncDef:
        t = self.expect_word("定义")
        self.expect_word("做法")
        name = self.expect("IDENT", "函数名").value
        self.expect("LPAREN")
        params: list[Param] = []
        if not self.at("RPAREN"):
            while True:
                pname = self.expect("IDENT", "参数名").value
                self.expect("COLON")
                ptyp = self.parse_type()
                params.append(Param(pname, ptyp))
                if not self.accept("COMMA"):
                    break
        self.expect("RPAREN")
        self.expect("MINUS")  # -> 拆分为 MINUS + GT
        self.expect("GT")
        ret = self.parse_type()

        errors: list[str] = []
        if self.at("可能生"):
            self.i += 1
            while True:
                errors.append(self.parse_error_type())
                if not self.accept("COMMA"):
                    break

        self.expect("COLON")
        self.expect("NEWLINE")
        self.skip_newlines()

        contract: Optional[Contract] = None
        if self.at("约定"):
            self.i += 1
            self.expect("COLON")
            self.expect("NEWLINE")
            self.skip_newlines()
            pre = post = None
            if self.at("要求"):
                self.i += 1
                self.expect("COLON")
                pre = self.parse_expr()
                self.skip_newlines()
            if self.at("保证"):
                self.i += 1
                self.expect("COLON")
                post = self.parse_expr()
                self.skip_newlines()
            self.expect_word("结束")
            self.skip_newlines()
            contract = Contract(pre, post)

        body = self.parse_block(stop_at="结束")
        self.expect_word("结束")
        self.skip_newlines()
        return FuncDef(name, params, ret, errors, contract, body, t.line, t.col)

    def parse_entry(self) -> list[Stmt]:
        self.expect_word("开始")
        # 冒号可选：`开始:` 或 `开始`（零基础首课避免冒号全角/半角摩擦）
        if self.at("COLON"):
            self.i += 1
        self.expect("NEWLINE")
        self.skip_newlines()
        body = self.parse_block(stop_at="结束")
        self.expect_word("结束")
        self.skip_newlines()
        return body

    def parse_error_type(self) -> str:
        self.expect_word("错误")
        self.expect("LT")
        name = self.expect("IDENT", "错误类别").value
        self.expect("GT")
        return name

    # ---- 语句块 ----
    def parse_block(self, stop_at: str = "结束") -> list[Stmt]:
        body: list[Stmt] = []
        while True:
            self.skip_newlines()
            if self.at("EOF") or self.at(stop_at) or self.at("否则") or self.at("接住") or self.at("最后"):
                break
            body.append(self.parse_stmt())
            # 语句后必须换行或块结束
            if not self.at("NEWLINE") and not self.at(stop_at) and not self.at("否则") \
                    and not self.at("接住") and not self.at("最后") and not self.at("EOF"):
                raise ParseError("语句后需换行", self.cur())
            self.skip_newlines()
        return body

    def parse_stmt(self) -> Stmt:
        t = self.cur()
        k = t.kind

        if k == "变量":
            self.i += 1
            name = self.expect("IDENT", "变量名").value
            self.expect("COLON")
            typ = self.parse_type()
            init = None
            if self.accept("EQ"):
                init = self.parse_expr()
            return VarDecl(t.line, t.col, name, typ, init)

        if k == "将":
            self.i += 1
            val = self.parse_expr()
            self.expect_word("放到")
            target = self.expect("IDENT", "赋值目标").value
            return Assign(t.line, t.col, target, val)

        if k == "如果":
            self.i += 1
            cond = self.parse_expr()
            self.expect("COLON")
            self.expect("NEWLINE")
            self.skip_newlines()
            then_body = self.parse_block()
            else_body = None
            if self.at("否则"):
                self.i += 1
                self.expect("COLON")
                self.expect("NEWLINE")
                self.skip_newlines()
                else_body = self.parse_block()
            self.expect_word("结束")
            return If(t.line, t.col, cond, then_body, else_body)

        if k == "重复":
            # 重复 N 次: ... 结束（「次」可省：重复 N:）
            # 注意：`N 次` 中的「次」会被数字单位吸收逻辑吞掉，故此处只需可选匹配。
            self.i += 1
            times = self.parse_expr()
            if self.at("IDENT") and self.cur().value == "次":
                self.i += 1
            self.expect("COLON")
            self.expect("NEWLINE")
            self.skip_newlines()
            body = self.parse_block()
            self.expect_word("结束")
            return Repeat(t.line, t.col, times, body)

        if k == "循环":
            self.i += 1
            cond = self.parse_expr()
            self.expect("COLON")
            self.expect("NEWLINE")
            self.skip_newlines()
            body = self.parse_block()
            self.expect_word("结束")
            return Loop(t.line, t.col, cond, body)

        if k == "每个":
            self.i += 1
            iterable = self.parse_expr()
            self.expect_word("为")
            var = self.expect("IDENT", "遍历变量名").value
            self.expect("COLON")
            self.expect("NEWLINE")
            self.skip_newlines()
            body = self.parse_block()
            self.expect_word("结束")
            return ForEach(t.line, t.col, iterable, var, body)

        if k == "返回":
            self.i += 1
            return Return(t.line, t.col, self.parse_expr())

        if k == "必须":
            self.i += 1
            return Assert(t.line, t.col, self.parse_expr())

        if k == "报错":
            self.i += 1
            et = self.parse_error_type()
            self.expect("LPAREN")
            msg = self.parse_expr()
            self.expect("RPAREN")
            return Throw(t.line, t.col, et, msg)

        if k == "尝试":
            self.i += 1
            self.expect("COLON")
            self.expect("NEWLINE")
            self.skip_newlines()
            body = self.parse_block()
            catch_type = catch_var = None
            catch_body = None
            if self.at("接住"):
                self.i += 1
                catch_type = self.parse_error_type()
                self.expect_word("为")
                catch_var = self.expect("IDENT", "捕获变量名").value
                self.expect("COLON")
                self.expect("NEWLINE")
                self.skip_newlines()
                catch_body = self.parse_block()
            finally_body = None
            if self.at("最后"):
                self.i += 1
                self.expect("COLON")
                self.expect("NEWLINE")
                self.skip_newlines()
                finally_body = self.parse_block()
            self.expect_word("结束")
            return Try(t.line, t.col, body, catch_type, catch_var, catch_body, finally_body)

        if k == "执行":
            self.i += 1
            # 两种形式：
            #   执行 x = 表达式   （带结果变量）
            #   执行 表达式        （无结果，如工具.清理()）
            if self.at("IDENT") and self.peek().kind == "EQ":
                name = self.expect("IDENT", "执行结果变量名").value
                self.expect("EQ")
                call = self.parse_expr()
                return Exec(t.line, t.col, name, call)
            call = self.parse_expr()
            return Exec(t.line, t.col, "_", call)

        if k == "观察":
            self.i += 1
            name = self.expect("IDENT", "观察变量名").value
            self.expect("COLON")
            self.expect("NEWLINE")
            self.skip_newlines()
            body = self.parse_block()
            self.expect_word("结束")
            return Observe(t.line, t.col, name, body)

        if k == "沙箱":
            self.i += 1
            self.expect("COLON")
            self.expect("NEWLINE")
            self.skip_newlines()
            body = self.parse_block()
            self.expect_word("结束")
            return Sandbox(t.line, t.col, body)

        if k == "授权":
            self.i += 1
            caps: list[str] = []
            while True:
                # 能力名形如 工具.查询 —— 由 IDENT DOT IDENT 组成
                first = self.expect("IDENT", "能力名").value
                if self.accept("DOT"):
                    second = self.expect("IDENT", "能力方法").value
                    caps.append(f"{first}.{second}")
                else:
                    caps.append(first)
                if not self.accept("COMMA"):
                    break
            return Authorize(t.line, t.col, caps)

        if k == "同时":
            self.i += 1
            self.expect("COLON")
            self.expect("NEWLINE")
            self.skip_newlines()
            body = self.parse_block()
            self.expect_word("结束")
            return Parallel(t.line, t.col, body)

        if k == "发起":
            self.i += 1
            fname = self.expect("IDENT", "任务函数名").value
            self.expect("LPAREN")
            args = self.parse_args()
            self.expect("RPAREN")
            return Spawn(t.line, t.col, fname, args)

        if k == "接收":
            self.i += 1
            self.expect("LPAREN")
            mailbox = self.parse_expr()
            self.expect("RPAREN")
            # 可选绑定：`收(信箱) 为 变量`（补齐 ast_nodes.Receive.target 这个死字段）
            target = None
            if self.at("为"):
                self.i += 1
                target = self.expect("IDENT", "接收变量名").value
            return Receive(t.line, t.col, mailbox, target)

        if k == "说出":
            self.i += 1
            # 两种写法：输出(x) 或 输出 x（零基础推荐后者，少一对括号）
            if self.at("LPAREN"):
                self.i += 1
                val = self.parse_expr()
                self.expect("RPAREN")
            else:
                val = self.parse_expr()
            return Output(t.line, t.col, val)

        # 表达式语句
        return ExprStmt(t.line, t.col, self.parse_expr())

    def parse_args(self) -> list[Expr]:
        args: list[Expr] = []
        if not self.at("RPAREN"):
            while True:
                args.append(self.parse_expr())
                if not self.accept("COMMA"):
                    break
        return args

    def parse_args_with_kw(self) -> tuple[list[Expr], list[tuple[str, Expr]]]:
        """解析实参列表，允许尾部出现 `名=值` 关键字实参（FFI 用）。

        关键字实参必须在位置实参之后，例如：
            数据.dumps(载荷, ensure_ascii=假)
        """
        args: list[Expr] = []
        kwargs: list[tuple[str, Expr]] = []
        if not self.at("RPAREN"):
            while True:
                if self.at("IDENT") and self.peek().kind == "EQ" and \
                        self.cur().value not in ("真", "假"):
                    name = self.expect("IDENT").value
                    self.expect("EQ")
                    kwargs.append((name, self.parse_expr()))
                else:
                    if kwargs:
                        raise ParseError("关键字实参必须写在位置实参之后", self.cur())
                    args.append(self.parse_expr())
                if not self.accept("COMMA"):
                    break
        return args, kwargs

    # ---- 类型 ----
    def parse_type(self) -> Type:
        t = self.cur()
        if self.at("数"):
            self.i += 1
            unit = None
            # 两种写法等价：
            #   旧式（需尖括号）：数<元/个>
            #   新式（零符号，推荐零基础使用）：数 元/个
            if self.at("LT"):
                unit = self.parse_unit()
            elif self.at("IDENT"):
                unit = self.parse_unit_bare()
            return Type("数", unit=unit)
        if self.at("文本"):
            self.i += 1
            return Type("文本")
        if self.at("真假"):
            self.i += 1
            return Type("真假")
        if self.at("空"):
            self.i += 1
            return Type("空")
        if self.at("序列"):
            self.i += 1
            # 序列<文本> 或 序列 文本（零符号写法）
            if self.at("LT"):
                self.i += 1
                elem = self.parse_type()
                self.expect("GT")
            else:
                elem = self.parse_type()
            return Type("序列", elem=elem)
        if self.at("映射"):
            self.i += 1
            if self.at("LT"):
                self.i += 1
                key = self.parse_type()
                self.expect("COMMA")
                val = self.parse_type()
                self.expect("GT")
            else:
                # 映射 文本 数（零符号写法，两个类型以空白分隔）
                key = self.parse_type()
                val = self.parse_type()
            return Type("映射", key=key, val=val)
        if self.at("IDENT"):
            self.i += 1
            return Type(t.value)
        raise ParseError("期望类型", t)

    def parse_unit(self) -> Unit:
        self.expect("LT")
        num: list[str] = []
        den: list[str] = []
        num.append(self.expect("IDENT", "单位名").value)
        while self.at("STAR"):
            self.i += 1
            num.append(self.expect("IDENT", "单位名").value)
        if self.accept("SLASH"):
            den.append(self.expect("IDENT", "单位名").value)
            while self.at("SLASH"):
                self.i += 1
                den.append(self.expect("IDENT", "单位名").value)
        self.expect("GT")
        return Unit(num, den)

    def _is_unit_word(self, word: str) -> bool:
        """是否为内置单位名（用于无尖括号写法 `12 元/个` 的判定）。

        只认内置基本单位，避免把 `12 个变量` 之类误吸收为带单位数字。
        """
        return word in ("米", "秒", "千克", "元", "个", "次", "摄氏度", "开尔文", "比例")

    def parse_unit_bare(self) -> Unit:
        """无尖括号单位：元/个、米/秒、元/个/次。"""
        num: list[str] = []
        den: list[str] = []
        num.append(self.expect("IDENT", "单位名").value)
        while self.at("STAR"):
            self.i += 1
            num.append(self.expect("IDENT", "单位名").value)
        if self.accept("SLASH"):
            den.append(self.expect("IDENT", "单位名").value)
            while self.at("SLASH"):
                self.i += 1
                den.append(self.expect("IDENT", "单位名").value)
        return Unit(num, den)

    # ---- 表达式 ----
    def parse_expr(self) -> Expr:
        return self.parse_or()

    def parse_or(self) -> Expr:
        """逻辑或：或者（最低优先级，左结合）。"""
        left = self.parse_and()
        while self.at("或者"):
            t = self.cur()
            self.i += 1
            right = self.parse_and()
            left = Binary(t.line, t.col, "或者", left, right)
        return left

    def parse_and(self) -> Expr:
        """逻辑与：并且（优先级高于或者，低于比较）。"""
        left = self.parse_equality()
        while self.at("并且"):
            t = self.cur()
            self.i += 1
            right = self.parse_equality()
            left = Binary(t.line, t.col, "并且", left, right)
        return left

    def parse_equality(self) -> Expr:
        left = self.parse_comparison()
        while self.at("EQ") or self.at("NE"):
            op = "==" if self.at("EQ") else "!="
            t = self.cur()
            self.i += 1
            right = self.parse_comparison()
            left = Binary(t.line, t.col, op, left, right)
        return left

    def parse_comparison(self) -> Expr:
        left = self.parse_add()
        while self.at("LT") or self.at("LE") or self.at("GT") or self.at("GE"):
            op = {"LT": "<", "LE": "<=", "GT": ">", "GE": ">="}[self.cur().kind]
            t = self.cur()
            self.i += 1
            right = self.parse_add()
            left = Binary(t.line, t.col, op, left, right)
        return left

    def parse_add(self) -> Expr:
        left = self.parse_mul()
        while self.at("PLUS") or self.at("MINUS"):
            op = "+" if self.at("PLUS") else "-"
            t = self.cur()
            self.i += 1
            right = self.parse_mul()
            left = Binary(t.line, t.col, op, left, right)
        return left

    def parse_mul(self) -> Expr:
        left = self.parse_unary()
        while self.at("STAR") or self.at("SLASH"):
            op = "*" if self.at("STAR") else "/"
            t = self.cur()
            self.i += 1
            right = self.parse_unary()
            left = Binary(t.line, t.col, op, left, right)
        return left

    def parse_unary(self) -> Expr:
        t = self.cur()
        if self.at("MINUS"):
            self.i += 1
            return Unary(t.line, t.col, "-", self.parse_unary())
        if self.cur().kind == "非":
            self.i += 1
            return Unary(t.line, t.col, "非", self.parse_unary())
        return self.parse_atom()

    def _looks_like_unit(self) -> bool:
        """当前在 LT 上，判断是否构成单位后缀 `<单位>` 或 `<单位/单位>`。

        规则：`<` 之后必须是 IDENT（单位名），并且紧随 `>` 或 `/`。
        这样 `1 < 2`（后跟数字）、`1 < x`（后跟变量但无 `>`）、`a < b` 都归比较运算。
        """
        if not self.at("LT"):
            return False
        j = self.i + 1
        if j >= len(self.toks) or self.toks[j].kind != "IDENT":
            return False
        j += 1
        # 扫描单位单项式：IDENT (/ IDENT)*
        while j < len(self.toks) and self.toks[j].kind == "SLASH":
            if j + 1 >= len(self.toks) or self.toks[j + 1].kind != "IDENT":
                return False
            j += 2
        return j < len(self.toks) and self.toks[j].kind == "GT"

    def parse_atom(self) -> Expr:
        t = self.cur()
        if self.at("NUMBER"):
            self.i += 1
            # 单位标注 vs 小于比较的歧义消解：只有 `<` 后面紧跟标识符（单位名）
            # 且再后面是 `>` 或 `/` 时才当作单位后缀；否则 `<` 归比较运算。
            unit = None
            if self.at("LT") and self._looks_like_unit():
                unit = self.parse_unit()
            elif self.at("IDENT") and self._is_unit_word(self.cur().value):
                # 无尖括号写法：12 元/个（零基础推荐）。仅当词是已知单位时才吸收，
                # 避免把 `12 个变量...` 之类误判。
                unit = self.parse_unit_bare()
            return Number(t.line, t.col, float(t.value), unit)
        if self.at("STRING"):
            self.i += 1
            return String(t.line, t.col, t.value)
        if self.at("真"):
            self.i += 1
            return Bool(t.line, t.col, True)
        if self.at("假"):
            self.i += 1
            return Bool(t.line, t.col, False)
        if self.at("空"):
            self.i += 1
            return Null(t.line, t.col)
        if self.at("LPAREN"):
            self.i += 1
            e = self.parse_expr()
            self.expect("RPAREN")
            return e
        if self.at("LBRACK"):
            self.i += 1
            items: list[Expr] = []
            if not self.at("RBRACK"):
                while True:
                    items.append(self.parse_expr())
                    if not self.accept("COMMA"):
                        break
            self.expect("RBRACK")
            return SeqLiteral(t.line, t.col, items)
        if self.at("LBRACE"):
            self.i += 1
            pairs: list[tuple[Expr, Expr]] = []
            if not self.at("RBRACE"):
                while True:
                    k = self.parse_expr()
                    self.expect("COLON")
                    v = self.parse_expr()
                    pairs.append((k, v))
                    if not self.accept("COMMA"):
                        break
            self.expect("RBRACE")
            return MapLiteral(t.line, t.col, pairs)
        if self.at("IDENT"):
            self.i += 1
            node: Expr = Var(t.line, t.col, t.value)
            while self.at("DOT"):
                self.i += 1
                # 属性名允许用关键字：标准库/FFI 的成员名可能正好是关键字
                # （例如模块里有个叫「函数」的函数）。点号之后不存在语句歧义，
                # 因此这里接受 IDENT 与任意关键字 token。
                node = Attr(t.line, t.col, node, self._expect_member_name())
            if self.at("LPAREN"):
                self.i += 1
                args, kwargs = self.parse_args_with_kw()
                self.expect("RPAREN")
                node = Call(t.line, t.col, node, args, kwargs)
            return node
        raise ParseError("期望表达式", t)

    def _expect_member_name(self) -> str:
        """读取属性名：标识符或关键字（关键字作成员名是合法的）。

        例：文字.长度、数据.函数 —— 点号之后不存在语句歧义，
        所以只要不是标点/运算符/字面量，都可以作成员名。
        """
        tok = self.cur()
        if tok.kind in ("EOF", "NEWLINE", "COLON", "COMMA", "DOT",
                        "LPAREN", "RPAREN", "LBRACK", "RBRACK", "LBRACE", "RBRACE",
                        "EQ", "NE", "LT", "GT", "LE", "GE", "PLUS", "MINUS",
                        "STAR", "SLASH", "STRING", "NUMBER"):
            raise ParseError("期望属性名", tok)
        self.i += 1
        return tok.value
