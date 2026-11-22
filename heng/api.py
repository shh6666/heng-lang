"""衡语公共 API：编译源码 → 运行，返回显式 IR 与输出。

三层管线的统一入口：
  源程序 → 词法 → 语法 → 类型/量纲/能力检查 → 显式 IR → 解释执行
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from .lexer import Lexer, LexError
from .parser import Parser, ParseError
from .typecheck import TypeChecker, TypeError_
from .interpreter import Runtime, HError
from .modules import ImportError_


class CompileError(Exception):
    pass


@dataclass
class CompileResult:
    output: list[str] = field(default_factory=list)
    ir: str = ""
    program: object = None
    runtime: object = None
    folded: int = 0  # 常量折叠次数（optimize=True 时有效）


def compile_source(src: str, check: bool = True,
                   tools: dict | None = None,
                   optimize: bool = False,
                   trace_ir: bool = False,
                   filename: str | None = None,
                   base_dir: str | None = None) -> CompileResult:
    """编译并执行衡语源码。

    optimize=True 时在类型检查后运行常量折叠 pass（不改变程序语义）。

    filename / base_dir 用于解析 导入：以源文件所在目录为搜索起点，
    并向上查找 heng.mod 包描述文件（见 heng/modules.py）。
    """
    try:
        tokens = Lexer(src).tokens()
    except LexError as e:
        raise CompileError(str(e)) from e

    try:
        prog = Parser(tokens).parse_program()
    except ParseError as e:
        raise CompileError(str(e)) from e

    root = base_dir
    if root is None and filename:
        root = os.path.dirname(os.path.abspath(filename))

    host_modules = {}
    if prog.import_specs:
        try:
            prog, host_modules = resolve_imports(prog, root or os.getcwd())
        except ImportError_ as e:
            raise CompileError(str(e)) from e

    if check:
        try:
            TypeChecker(prog, host_aliases=set(host_modules)).check()
        except TypeError_ as e:
            raise CompileError(str(e)) from e

    folded = 0
    if optimize:
        from .optimizer import fold_constants
        prog, folded = fold_constants(prog)

    rt = Runtime(prog, tools=tools, trace_ir=trace_ir, host_modules=host_modules)
    try:
        output = rt.run()
    except HError as e:
        raise CompileError(str(e)) from e
    except RecursionError:
        raise CompileError("错误<越界>: 递归过深（超过 Python 栈上限），请检查递归终止条件") from None
    except Exception as e:
        # 兜底：任何内部异常都必须转成衡语可读错误，绝不把 Python 栈丢给用户。
        # 这是用户可见契约——「报错全中文、可操作」不能因内部缺陷而破功。
        raise CompileError(f"错误<未定义>: 运行时内部错误（{type(e).__name__}: {e}）") from e

    return CompileResult(output=output, ir=rt.ir_dump(), program=prog, runtime=rt, folded=folded)


def resolve_imports(prog, base_dir: str):
    """展开程序里的所有 导入 声明。

    返回 (合并后的 Program, 宿主模块表)。
      - 衡语模块：编译其函数/记录，加命名空间前缀后并入当前程序；
      - 外部模块：登记到宿主模块表，运行期按 `别名.函数` 调用（受能力约束）。
    """
    from .modules import (
        ImportError_, PackageManifest, find_manifest, load_heng_module,
        load_host_module, namespace_program,
    )

    seen: set[str] = set()
    manifest = None
    mpath = find_manifest(base_dir)
    if mpath:
        manifest = PackageManifest.load(mpath)

    host_modules: dict[str, object] = {}

    for spec in prog.import_specs:
        if spec.kind == "外部":
            mod = load_host_module(spec.path, spec.alias,
                                   search_dirs=[base_dir] + (manifest.search_paths if manifest else []))
            host_modules[spec.alias] = mod
            continue

        # 衡语模块：先按包描述文件解析，再按搜索路径解析
        candidates = []
        if manifest is not None:
            dep = manifest.resolve(spec.alias) or manifest.resolve(spec.path)
            if dep:
                if dep.startswith("外部 "):
                    host_modules[spec.alias] = load_host_module(dep.split(None, 1)[1], spec.alias)
                    continue
                candidates.append(dep)
        candidates.append(spec.path)
        if not spec.path.endswith(".hng"):
            candidates.append(f"{spec.path}.hng")

        target = None
        search = [base_dir] + (manifest.search_paths if manifest else [])
        for cand in candidates:
            for root in search:
                p = cand if os.path.isabs(cand) else os.path.join(root, cand)
                if os.path.isfile(p):
                    target = p
                    break
            if target:
                break
        if target is None:
            raise ImportError_(
                f"导入失败：找不到模块 {spec.path}"
                f"（已查找：{', '.join(candidates)}；搜索目录：{', '.join(search)}）")

        sub = load_heng_module(target, spec.alias, seen)
        sub = namespace_program(sub, spec.alias)
        prog.functions.extend(sub.functions)
        prog.records.extend(sub.records)
        prog.constants.extend(sub.constants)

    return prog, host_modules


def check_only(src: str) -> bool:
    """仅做静态检查，不执行。全部通过返回 True，否则抛 CompileError。"""
    try:
        tokens = Lexer(src).tokens()
        prog = Parser(tokens).parse_program()
        TypeChecker(prog).check()
    except (LexError, ParseError, TypeError_) as e:
        raise CompileError(str(e)) from e
    return True
