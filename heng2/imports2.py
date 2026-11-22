"""衡语内核 v2 —— 模块导入与 FFI。

两条通路：
  导入 「工具.hng」之 工具          衡语模块：编译其函数/记录，加命名空间前缀并入
  导入 外部 「libm」之 数学        FFI：记录动态库名，由原生 VM 用 dlopen 加载，
                                  按 数学.sqrt(...) 调用——不依赖任何宿主语言

安全：FFI 调用编译成 CAP_CHECK 指令，沙箱内必须 授权 外部.调用<模块名>，
由虚拟机在指令级强制——不是靠某处代码记得检查。
"""

from __future__ import annotations

import importlib
import os
import sys
from dataclasses import dataclass
from typing import Optional

from .ast2 import Program, Func, Import, Stmt
from .ast2 import Expr, Call, BinOp, UnOp, Field, SeqLit, MapLit, VarRef


class ImportFail(Exception):
    pass


@dataclass
class HostModule:
    module_name: str
    module: object
    alias: str


# ---------------- heng.mod 包描述 ----------------

@dataclass
class Manifest:
    name: str = ""
    version: str = ""
    deps: dict[str, str] = None
    search: list[str] = None

    def __post_init__(self):
        self.deps = self.deps or {}
        self.search = self.search or []

    @classmethod
    def load(cls, path: str) -> "Manifest":
        m = cls()
        m.search.append(os.path.dirname(os.path.abspath(path)) or ".")
        with open(path, encoding="utf-8") as f:
            raw = f.read()
        for line in raw.splitlines():
            line = line.split("注：")[0].split("#")[0].strip()
            if not line:
                continue
            parts = line.replace("=", " ").split()
            if len(parts) < 2:
                continue
            key = parts[0]
            if key == "名称":
                m.name = parts[1]
            elif key == "版本":
                m.version = parts[1]
            elif key == "路径":
                m.search.append(parts[1])
            elif key == "依赖" and len(parts) >= 3:
                m.deps[parts[1]] = " ".join(parts[2:])
        return m


def find_manifest(start: str) -> Optional[str]:
    cur = os.path.abspath(start if os.path.isdir(start) else os.path.dirname(start) or ".")
    while True:
        cand = os.path.join(cur, "heng.mod")
        if os.path.isfile(cand):
            return cand
        parent = os.path.dirname(cur)
        if parent == cur:
            return None
        cur = parent


# ---------------- 导入解析 ----------------

def resolve_imports(prog: Program, base_dir: str):
    seen: set[str] = set()
    manifest = None
    mp = find_manifest(base_dir)
    if mp:
        manifest = Manifest.load(mp)

    hosts: dict[str, HostModule] = {}
    search = [base_dir] + (manifest.search if manifest else [])

    for spec in prog.imports:
        if spec.kind == "外部":
            # 外部库：不再加载宿主语言模块，只记录「别名 → 动态库名」。
            # 真正的加载由原生 VM 用 dlopen 完成——FFI 不依赖任何宿主语言。
            hosts[spec.alias] = spec.path
            continue

        target = _find_file(spec, manifest, search)
        if target is None:
            raise ImportFail(f"导入失败：找不到模块 {spec.path}（搜索目录：{', '.join(search)}）")
        sub = compile_module(target, seen)
        _namespace(sub, spec.alias)
        # 合并：函数索引重排
        for fn in sub.funcs:
            fn.index = len(prog.funcs)
            prog.funcs.append(fn)
        prog.records.extend(sub.records)
        prog.consts.extend(sub.consts)

    return prog, hosts


def _find_file(spec: Import, manifest: Optional[Manifest],
               search: list[str]) -> Optional[str]:
    cands: list[str] = []
    if manifest is not None:
        dep = manifest.deps.get(spec.alias) or manifest.deps.get(spec.path)
        if dep and not dep.startswith("外部 "):
            cands.append(dep)
    cands.append(spec.path)
    if not spec.path.endswith(".hng"):
        cands.append(spec.path + ".hng")
    for cand in cands:
        for root in search:
            p = cand if os.path.isabs(cand) else os.path.join(root, cand)
            if os.path.isfile(p):
                return p
    return None


def compile_module(path: str, seen: set[str]) -> Program:
    real = os.path.abspath(path)
    if real in seen:
        raise ImportFail(f"循环导入：{path}")
    seen.add(real)
    from .lexer2 import tokenize
    from .parser2 import Parser
    with open(real, encoding="utf-8") as f:
        src = f.read()
    prog = Parser(tokenize(src)).parse_program()
    prog.entry = None      # 被导入的模块不执行自己的入口
    return prog


def _namespace(prog: Program, prefix: str) -> None:
    """给模块内函数加命名空间前缀，并重写模块内部相互调用。"""
    rename = {fn.name: f"{prefix}.{fn.name}" for fn in prog.funcs}
    for fn in prog.funcs:
        fn.name = rename[fn.name]
        _rewrite(fn.body, rename)
    rec_names = {rec.name for rec in prog.records}
    for rec in prog.records:
        rec.name = f"{prefix}.{rec.name}"
    # 常量也要加前缀。否则多个模块里都叫「表」「状态」时会互相覆盖，
    # 且主模块里 `模块.常量` 这种访问无处可查。
    # 注意：只改「本模块内」对这些常量的引用（函数体现在已经被加了前缀，
    # 它们引用的是本模块的常量），不要碰外部已经写好的 `模块.常量`。
    local = {name for name, _t, _v in prog.consts}
    for i, (name, typ, val) in enumerate(prog.consts):
        prog.consts[i] = (f"{prefix}.{name}", typ, val)
    if local or rec_names:
        for fn in prog.funcs:
            _rewrite_consts(fn.body, local, prefix, rec_names)


def _rewrite_consts(stmts: list, cmap: dict, prefix: str,
                    rec_names: set | None = None) -> None:
    """把模块内部对自身常量/记录的引用改成「前缀.名字」。

    记录构造 `内存块(...)` 在 AST 里是 Call(VarRef("内存块"))，
    记录本身被加了前缀，构造调用也必须一起改，否则运行时找不到记录。
    """
    names = {n: f"{prefix}.{n}" for n in cmap}
    for rn in (rec_names or ()):
        names[rn] = f"{prefix}.{rn}"
    def walk_expr(e):
        if e is None:
            return
        from .ast2 import VarRef, Field, BinOp, UnOp, Call, Index, SeqLit, MapLit, Lambda
        if isinstance(e, VarRef) and e.name in names:
            e.name = names[e.name]
        elif isinstance(e, Field):
            walk_expr(e.obj)
        elif isinstance(e, BinOp):
            walk_expr(e.left); walk_expr(e.right)
        elif isinstance(e, UnOp):
            walk_expr(e.operand)
        elif isinstance(e, Call):
            walk_expr(e.callee)
            for a in e.args: walk_expr(a)
        elif isinstance(e, Index):
            walk_expr(e.base); walk_expr(e.index)
        elif isinstance(e, SeqLit):
            for it in e.items: walk_expr(it)
        elif isinstance(e, MapLit):
            for k, v in e.pairs: walk_expr(k); walk_expr(v)
        elif isinstance(e, Lambda):
            walk_expr(e.body)

    for s in stmts:
        # SetVar.target 是「名字字符串」，不是表达式，必须单独改写，
        # 否则 `内存表 = ...` 这种对模块常量的赋值会指向未定义名字。
        tgt = getattr(s, "target", None)
        if isinstance(tgt, str) and tgt in names:
            s.target = names[tgt]
        for attr in ("value", "init", "cond", "expr", "times", "iterable",
                     "message", "call", "base", "index"):
            walk_expr(getattr(s, attr, None))
        for attr in ("body", "then_body", "else_body", "catch_body", "finally_body"):
            sub = getattr(s, attr, None)
            if isinstance(sub, list):
                _rewrite_consts(sub, cmap, prefix, rec_names)


def _rewrite(stmts: list[Stmt], rename: dict[str, str]) -> None:
    for s in stmts:
        for attr in ("value", "init", "cond", "expr", "times", "iterable", "message", "call"):
            node = getattr(s, attr, None)
            if isinstance(node, Expr):
                _rewrite_expr(node, rename)
        for attr in ("body", "then_body", "else_body", "catch_body", "finally_body"):
            sub = getattr(s, attr, None)
            if isinstance(sub, list):
                _rewrite(sub, rename)
        for a in getattr(s, "args", []) or []:
            _rewrite_expr(a, rename)


def _rewrite_expr(e: Expr, rename: dict[str, str]) -> None:
    if isinstance(e, Call):
        if isinstance(e.callee, VarRef) and e.callee.name in rename:
            e.callee.name = rename[e.callee.name]
        _rewrite_expr(e.callee, rename)
        for a in e.args:
            _rewrite_expr(a, rename)
    elif isinstance(e, BinOp):
        _rewrite_expr(e.left, rename)
        _rewrite_expr(e.right, rename)
    elif isinstance(e, UnOp):
        _rewrite_expr(e.operand, rename)
    elif isinstance(e, Field):
        _rewrite_expr(e.obj, rename)
    elif isinstance(e, SeqLit):
        for x in e.items:
            _rewrite_expr(x, rename)
    elif isinstance(e, MapLit):
        for k, v in e.pairs:
            _rewrite_expr(k, rename)
            _rewrite_expr(v, rename)


# ---------------- FFI ----------------

def load_host(module_name: str, alias: str, search: list[str]) -> HostModule:
    added: list[str] = []
    for d in search:
        if d and d not in sys.path:
            sys.path.insert(0, d)
            added.append(d)
    try:
        mod = importlib.import_module(module_name)
    except Exception as e:
        raise ImportFail(f"无法加载外部模块 {module_name}：{e}")
    finally:
        for d in added:
            try:
                sys.path.remove(d)
            except ValueError:
                pass
    return HostModule(module_name, mod, alias)
