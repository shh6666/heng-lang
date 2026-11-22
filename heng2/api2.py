"""衡语内核 v2 —— 公共 API。

管线：源码 → 词法 → 语法 → 检查（类型/量纲/能力/槽位）→ 代码生成 → VM 执行。
注意：本管线中**没有任何 AST 解释执行**。AST 只用于生成字节码，
执行阶段只跑字节码。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

from .lexer2 import tokenize, LexError
from .parser2 import Parser, ParseError
from .check2 import Checker, CheckError
from .compiler2 import Compiler, CompileError
from .bytecode import Module
from .stdlib2 import HErr

VERSION = "衡语（Heng）内核 v2.0.0"


class HengError(Exception):
    """编译或运行的统一错误。"""
    pass


@dataclass
class RunResult:
    output: list[str] = field(default_factory=list)
    bytes: bytes = b""
    module: Optional[Module] = None
    say_units: list[str] = field(default_factory=list)


def compile_src(src: str, filename: Optional[str] = None,
                base_dir: Optional[str] = None) -> tuple[Module, list[str]]:
    """编译到 .hbc 字节码，返回 (module, 输出单位标签表)。"""
    try:
        toks = tokenize(src)
    except LexError as e:
        raise HengError(str(e)) from e

    try:
        prog = Parser(toks).parse_program()
    except ParseError as e:
        raise HengError(str(e)) from e

    root = base_dir
    if root is None and filename:
        root = os.path.dirname(os.path.abspath(filename))

    host_modules: dict = {}
    host_aliases: set[str] = set()
    if prog.imports:
        from .imports2 import resolve_imports
        try:
            prog, host_modules = resolve_imports(prog, root or os.getcwd())
        except Exception as e:
            raise HengError(str(e)) from e
        # 别名 → 动态库名（FFI 由原生 VM 加载）
        host_aliases = set(host_modules)

    try:
        checker = Checker(prog, host_aliases)
        checker.run()
    except CheckError as e:
        raise HengError(str(e)) from e

    # 检查层把槽位数写到 Program 上，供代码生成使用
    prog.n_globals = checker.n_globals

    try:
        comp = Compiler(prog, host_aliases, global_slots=checker.globals)
        mod = comp.compile()
    except CompileError as e:
        raise HengError(str(e)) from e

    mod.say_units = comp.say_units
    mod.host_modules = host_modules
    # 写进字节码的 FFI 库表：别名 → 动态库名
    mod.ffi_libs = dict(host_modules) if host_modules else {}
    return mod, comp.say_units


def _attach_unit(text: str, unit: str) -> str:
    """给纯数值输出补上显示单位（字符串/布尔等原样返回）。"""
    if not unit:
        return text
    try:
        float(text)
    except ValueError:
        return text
    return f"{text}{unit}"


