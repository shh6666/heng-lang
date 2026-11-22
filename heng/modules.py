"""衡语模块加载与 FFI（报告优先级 3）。

评估报告的判断是：衡语「零 FFI、零包管理，导入 是未实现的空语法」，
因此短期只能做编排层。本模块把 导入 从空语法变成两条真实通路：

  1. 衡语模块导入：`导入 「工具.hng」之 工具`
     读取同目录/包目录下的 .hng 文件，先编译其函数与记录，合并进当前程序，
     函数名加 `工具.` 前缀做命名空间隔离。

  2. 外部库 FFI：`导入 外部 「math」之 数学`
     加载宿主 Python 模块，其公开函数按 `数学.函数名(...)` 调用的方式暴露，
     参数与返回值自动在衡语值/宿主值之间转换。

安全模型：FFI 是能力，不是默认权限。
  - 只有在沙箱外（不受限上下文）才允许直接调用；
  - 沙箱内必须 `授权 外部.调用<模块名>` 才能调用，
    与 工具.查询 / 网络.请求 走同一套「默认拒绝 + 显式授权」。

包管理（最小可用）：包描述文件 `heng.mod`，每行一条：
    依赖 工具 = 工具.hng          注：本地模块
    依赖 数学 = 外部 math         注：宿主模块
    依赖 统计 = lib/统计.hng
加载时按描述文件解析相对路径；找不到文件时报明确错误，而不是静默忽略。
"""

from __future__ import annotations

import importlib
import os
from dataclasses import dataclass, field
from typing import Any, Optional

from .ast_nodes import (
    Type, Program, FuncDef, RecordDef, Param, Stmt, Return, Expr, Number, String,
    Bool, Null, SeqLiteral, MapLiteral, Var,
)


class ImportError_(Exception):
    """导入失败（区别于 Python 内置 ImportError）。"""


# ---------------- 包描述文件 ----------------

@dataclass
class PackageManifest:
    """heng.mod 的内容。"""
    name: str = ""
    version: str = ""
    dependencies: dict[str, str] = field(default_factory=dict)  # 别名 → 目标
    search_paths: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, path: str) -> "PackageManifest":
        m = cls()
        m.search_paths.append(os.path.dirname(os.path.abspath(path)) or ".")
        try:
            with open(path, encoding="utf-8") as f:
                raw = f.read()
        except FileNotFoundError:
            raise ImportError_(f"找不到包描述文件 {path}")
        for lineno, line in enumerate(raw.splitlines(), 1):
            line = line.split("注：")[0].split("#")[0].strip()
            if not line:
                continue
            parts = line.replace("=", " ").split()
            if len(parts) < 2:
                continue
            key = parts[0]
            if key == "名称" and len(parts) >= 2:
                m.name = parts[1]
            elif key == "版本" and len(parts) >= 2:
                m.version = parts[1]
            elif key == "路径" and len(parts) >= 2:
                m.search_paths.append(parts[1])
            elif key == "依赖" and len(parts) >= 3:
                m.dependencies[parts[1]] = " ".join(parts[2:])
        return m

    def resolve(self, alias: str) -> Optional[str]:
        return self.dependencies.get(alias)


def find_manifest(start: str) -> Optional[str]:
    """从 start 目录向上查找 heng.mod（类似 Cargo.toml / pyproject.toml）。"""
    cur = os.path.abspath(start if os.path.isdir(start) else os.path.dirname(start) or ".")
    while True:
        cand = os.path.join(cur, "heng.mod")
        if os.path.isfile(cand):
            return cand
        parent = os.path.dirname(cur)
        if parent == cur:
            return None
        cur = parent


# ---------------- 衡语模块（.hng） ----------------

def load_heng_module(path: str, alias: str, seen: Optional[set[str]] = None) -> Program:
    """编译一个衡语模块文件，返回其 Program（已完成语法与类型检查）。"""
    from .lexer import Lexer, LexError
    from .parser import Parser, ParseError
    from .typecheck import TypeChecker, TypeError_

    real = os.path.abspath(path)
    if seen is None:
        seen = set()
    if real in seen:
        raise ImportError_(f"循环导入：{path}")
    seen.add(real)

    try:
        with open(real, encoding="utf-8") as f:
            src = f.read()
    except FileNotFoundError:
        raise ImportError_(f"找不到模块文件 {path}")
    try:
        prog = Parser(Lexer(src).tokens()).parse_program()
        TypeChecker(prog).check()
    except (LexError, ParseError, TypeError_) as e:
        raise ImportError_(f"模块 {path} 编译失败：{e}")
    return prog


def namespace_program(prog: Program, prefix: str) -> Program:
    """给模块内的函数名加命名空间前缀，并重写模块内部的相互调用。"""
    rename = {fn.name: f"{prefix}.{fn.name}" for fn in prog.functions}
    for fn in prog.functions:
        fn.name = rename[fn.name]
        _rewrite_calls(fn.body, rename)
    prog.entry = None  # 被导入的模块不执行自己的入口
    return prog


def _rewrite_calls(stmts: list[Stmt], rename: dict[str, str]) -> None:
    """把模块内部对同模块函数的调用改写为带前缀的形式。

    只重写裸标识符调用（Var callee）；已在 rename 表中的名字才改。
    """
    for s in stmts:
        for attr in ("value", "init", "cond", "expr", "times", "iterable", "message"):
            if hasattr(s, attr):
                node = getattr(s, attr)
                if node is not None:
                    _rewrite_expr(node, rename)
        for attr in ("body", "then_body", "else_body", "catch_body", "finally_body"):
            sub = getattr(s, attr, None)
            if isinstance(sub, list):
                _rewrite_calls(sub, rename)
        call = getattr(s, "call", None)
        if call is not None:
            _rewrite_expr(call, rename)


def _rewrite_expr(e: Any, rename: dict[str, str]) -> None:
    from .ast_nodes import Call, Binary, Unary, Attr

    if isinstance(e, Call):
        if isinstance(e.callee, Var) and e.callee.name in rename:
            e.callee.name = rename[e.callee.name]
        _rewrite_expr(e.callee, rename)
        for a in e.args:
            _rewrite_expr(a, rename)
    elif isinstance(e, Binary):
        _rewrite_expr(e.left, rename)
        _rewrite_expr(e.right, rename)
    elif isinstance(e, Unary):
        _rewrite_expr(e.operand, rename)
    elif isinstance(e, Attr):
        _rewrite_expr(e.obj, rename)
    elif isinstance(e, SeqLiteral):
        for x in e.items:
            _rewrite_expr(x, rename)
    elif isinstance(e, MapLiteral):
        for k, v in e.pairs:
            _rewrite_expr(k, rename)
            _rewrite_expr(v, rename)


# ---------------- FFI：宿主模块 ----------------

class HostModule:
    """一个已加载的宿主 Python 模块。"""

    def __init__(self, module_name: str, module: Any, alias: str):
        self.module_name = module_name
        self.module = module
        self.alias = alias

    def public_functions(self) -> list[str]:
        out = []
        for n in dir(self.module):
            if n.startswith("_"):
                continue
            v = getattr(self.module, n)
            if callable(v) and not isinstance(v, type):
                out.append(n)
        return sorted(out)

    def capability(self) -> str:
        return f"外部.调用{self.module_name}"


def load_host_module(module_name: str, alias: str,
                     search_dirs: Optional[list[str]] = None) -> HostModule:
    """加载宿主 Python 模块。

    除标准 sys.path 外，还把源文件所在目录、包目录加入搜索路径——
    这样放一个 张量后端.py 在同目录即可 `导入 外部 「张量后端」之 后端`，
    符合脚本语言的直觉（类似 Python 的「同目录可导入」）。
    """
    import sys
    added: list[str] = []
    for d in (search_dirs or []):
        if d and d not in sys.path:
            sys.path.insert(0, d)
            added.append(d)
    try:
        mod = importlib.import_module(module_name)
    except Exception as e:
        raise ImportError_(f"无法加载外部模块 {module_name}：{e}")
    finally:
        for d in added:
            try:
                sys.path.remove(d)
            except ValueError:
                pass
    return HostModule(module_name, mod, alias)


def _to_host(v: Any) -> Any:
    """衡语值 → 宿主值。

    衡语内部数值统一为 f64，但宿主库常要求 int（如 math.factorial、range）。
    规则：整数值的 float 转为 int（不丢信息）；真正的小数保留 float。
    递归处理列表与字典。
    """
    from .interpreter import HValue, HRecord

    if isinstance(v, HValue):
        v = v.value
    if isinstance(v, HRecord):
        return {k: _to_host(x) for k, x in v.fields.items()}
    if isinstance(v, bool):
        return v
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if isinstance(v, list):
        return [_to_host(x) for x in v]
    if isinstance(v, dict):
        return {k: _to_host(x) for k, x in v.items()}
    return v


def call_host(mod: HostModule, func: str, args: list[Any], caps: set[str],
              kwargs: Optional[dict[str, Any]] = None) -> Any:
    """调用宿主模块函数。沙箱内必须授权 外部.调用<模块名>。"""
    from .interpreter import HError

    if caps and mod.capability() not in caps:
        raise HError("未授权",
                     f"调用外部模块 {mod.module_name} 需要授权「{mod.capability()}」")
    if not hasattr(mod.module, func):
        raise HError("未定义", f"外部模块 {mod.module_name} 没有函数 {func}")
    fn = getattr(mod.module, func)
    if not callable(fn):
        raise HError("类型", f"{mod.module_name}.{func} 不是函数")
    host_args = [_to_host(a) for a in args]
    host_kwargs = {k: _to_host(v) for k, v in (kwargs or {}).items()}
    try:
        return fn(*host_args, **host_kwargs)
    except TypeError as e:
        raise HError("类型", f"外部调用 {mod.module_name}.{func} 参数不对：{e}")
    except Exception as e:
        raise HError("未定义", f"外部调用 {mod.module_name}.{func} 失败：{e}")
