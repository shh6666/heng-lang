"""衡语命令行：`python3 -m heng <file.hng>` 或 `python3 -m heng --repl`。

选项：
  --ir        打印显式 IR（三层管线的中间层）
  --tokens    打印词法 token
  --no-check  跳过类型检查（调试用）
  --optimize  开启常量折叠优化 pass
  --wasm OUT  编译为 WebAssembly 二进制
  --version   显示版本
  --help/-h   显示帮助
"""

from __future__ import annotations

VERSION = "衡语（Heng）参考实现 v1.2.0"

import os
import sys

from .lexer import Lexer, LexError
from .parser import Parser, ParseError
from .typecheck import TypeChecker, TypeError_
from .interpreter import Runtime, HError
from .api import compile_source, CompileError


def run_file(path: str, show_ir: bool = False, show_tokens: bool = False,
             no_check: bool = False, wasm_out: str | None = None,
             optimize: bool = False) -> int:
    try:
        src = open(path, encoding="utf-8").read()
        if show_tokens:
            for t in Lexer(src).tokens():
                print(t)

        if wasm_out:
            from .wasm_backend import compile_to_wasm, write_wasm
            prog = Parser(Lexer(src).tokens()).parse_program()
            if prog.import_specs:
                from .api import resolve_imports
                from .modules import ImportError_
                try:
                    prog, _hosts = resolve_imports(prog, os.path.dirname(os.path.abspath(path)))
                except ImportError_ as e:
                    print(f"编译/运行错误：{e}", file=sys.stderr)
                    return 1
            if not no_check:
                TypeChecker(prog).check()
            module = compile_to_wasm(prog)
            write_wasm(module, wasm_out)
            print(f"WASM 模块已生成：{wasm_out}（{len(module.binary)} 字节）")
            print("导出函数：" + ", ".join(module.exports))
            return 0

        result = compile_source(src, check=not no_check, optimize=optimize,
                                trace_ir=show_ir, filename=path)
        if show_ir:
            print("---- 显式 IR ----")
            print(result.ir)
            print("----------------")
        if optimize:
            print(f"[优化] 常量折叠 {result.folded} 处", file=sys.stderr)
        for line in result.output:
            print(line)
        return 0
    except (LexError, ParseError, TypeError_, HError, CompileError) as e:
        print(f"编译/运行错误：{e}", file=sys.stderr)
        return 1
    except FileNotFoundError:
        print(f"文件不存在：{path}", file=sys.stderr)
        return 1


def repl() -> None:
    print("衡语 REPL（输入 EXIT 退出）")
    from .lexer import Lexer
    from .parser import Parser
    from .typecheck import TypeChecker
    from .interpreter import Runtime

    buffer: list[str] = []
    env_rt: Runtime | None = None

    while True:
        try:
            line = input("衡> " if not buffer else "…> ")
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if line.strip() in ("EXIT", "退出"):
            break
        buffer.append(line)
        src = "\n".join(buffer) + "\n"

        # 单表达式快速求值：包装为入口输出
        try:
            toks = Lexer(src).tokens()
            prog = Parser(toks).parse_program()
        except (ParseError, LexError):
            continue  # 尚未完整，继续读
        buffer = []

        try:
            TypeChecker(prog).check()
            rt = Runtime(prog)
            rt.run()
        except (TypeError_, HError, ParseError, LexError) as e:
            print(f"错误：{e}")


USAGE = f"""{VERSION}
用法：
  python3 -m heng <文件.hng> [选项]
  python3 -m heng --repl

选项：
  --ir          打印显式 IR（三层管线中间层）
  --tokens      打印词法 token
  --no-check    跳过类型检查（调试用）
  --optimize    开启常量折叠优化 pass
  --wasm <路径> 编译为 WebAssembly 二进制并写出
  --version     显示版本
  -h, --help    显示本帮助

示例：
  python3 -m heng examples/admission_analyzer.hng
  python3 -m heng examples/compute.hng --wasm /tmp/compute.wasm
"""


def main() -> None:
    args = sys.argv[1:]
    if not args or args[0] in ("--help", "-h"):
        print(USAGE)
        return
    if args[0] == "--version":
        print(VERSION)
        return
    if args[0] == "--repl":
        repl()
        return
    # --wasm <out> 可在任意位置
    wasm_out = None
    if "--wasm" in args:
        wi = args.index("--wasm")
        if wi + 1 >= len(args):
            print("错误：--wasm 需指定输出路径", file=sys.stderr)
            sys.exit(1)
        wasm_out = args[wi + 1]
        args = args[:wi] + args[wi + 2:]
    # 位置参数：第一个非选项即源文件
    paths = [a for a in args if not a.startswith("--")]
    if not paths:
        print("错误：未指定源文件", file=sys.stderr)
        print(USAGE, file=sys.stderr)
        sys.exit(1)
    path = paths[0]
    show_ir = "--ir" in args
    show_tokens = "--tokens" in args
    no_check = "--no-check" in args
    optimize = "--optimize" in args
    sys.exit(run_file(path, show_ir, show_tokens, no_check, wasm_out, optimize))


if __name__ == "__main__":
    main()
