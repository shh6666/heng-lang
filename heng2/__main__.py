"""衡语内核 v2 命令行：`python3 -m heng2 <文件.hng>`。

子命令与选项：
  <文件.hng>              编译并运行（优先用原生 VM）
  -c 「源码」             直接运行一段源码
  --build <文件.hng>      只编译，输出 .hbc 字节码
  --run <文件.hbc>        直接运行字节码（不需要源码）
  --disasm                打印字节码反汇编（显式 IR）
  --tokens                打印词法 token
  --py-vm                 用 Python VM 执行（对照用，默认用原生 VM）
  --wasm <out>            另存一份 WASM（保留旧后端能力）
  --version / --help

设计：默认走原生 VM（native/bin/hengvm），因为那是衡语自己的执行内核；
Python VM 保留用于调试与交叉验证。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile

from .api2 import VERSION, compile_src, HengError
from .disasm import disasm
from .bytecode import Module

HERE = os.path.dirname(os.path.abspath(__file__))

def _find_native() -> str:
    """定位原生 VM。

    查找顺序（打包后与前缀安装都要能用）：
      1. 环境变量 HENG_VM
      2. 与本模块同级的 bin/hengvm（打包后的布局）
      3. 源码树的 native/bin/hengvm（开发布局）
      4. PATH 里的 hengvm
    """
    env = os.environ.get("HENG_VM")
    if env and os.path.isfile(env):
        return env
    for cand in (os.path.join(HERE, "bin", "hengvm"),
                 os.path.join(HERE, "..", "native", "bin", "hengvm")):
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return os.path.normpath(cand)
    from shutil import which
    found = which("hengvm")
    return found or ""


NATIVE = _find_native()

# 用户原始脚本路径（用于 系统.脚本目录；不能传临时字节码的路径）
SCRIPT_PATH: str = ""
NATIVE_SRC = os.path.join(HERE, "..", "native", "hengvm.c")


def ensure_native() -> str | None:
    """确保原生 VM 可用；必要时现场编译。

    注意：打包分发时必须随包带上 VM 二进制——不能假设用户装了编译器。
    """
    if NATIVE and os.path.isfile(NATIVE) and os.access(NATIVE, os.X_OK):
        return NATIVE
    if not os.path.isfile(NATIVE_SRC):
        return None
    os.makedirs(os.path.dirname(NATIVE), exist_ok=True)
    for cc in ("clang", "cc", "gcc"):
        try:
            subprocess.run([cc, "-O3", "-o", NATIVE, NATIVE_SRC, "-lm"],
                           check=True, capture_output=True)
            return NATIVE
        except (FileNotFoundError, subprocess.CalledProcessError):
            continue
    return None


USAGE = f"""{VERSION}

用法：
  henger <文件.hng>              编译并运行
  henger -c "说出 1 + 2"          直接运行一段源码
  henger --build <文件.hng>      只编译为 .hbc 字节码
  henger --run <文件.hbc>        运行已编译的字节码

选项：
  -o <路径>            指定输出文件（配合 --build）
  -O0 / -O1 / -O2 / -O3  优化级别（默认 -O3；-O0 关闭超指令便于调试）
  -w, --no-warn        不输出警告
  -Werror              把警告当作错误（CI 用）
  --disasm             打印字节码反汇编（衡语的显式 IR）
  --tokens             打印词法 token
  -V, --version        显示版本
  -h, --help           显示帮助

诊断：
  错误与警告都会附带源码行、位置指示与修复建议。
  例：
    app.hng:3:6: 错误[E01]: 未定义变量 xxx
      |
    3 |   说出 xxx
      |        ^^^

示例：
  henger examples/hello.hng
  henger --build app.hng -o app.hbc
  henger --run app.hbc
"""


def main() -> None:
    global SCRIPT_PATH
    args = sys.argv[1:]
    if not args or args[0] in ("--help", "-h"):
        print(USAGE)
        return

    # ---- 选项解析 ----
    show_disasm = False
    show_tokens = False
    out_path = ""
    werror = False
    no_warn = False
    opt = 3
    as_json = False
    check_only = False
    positional: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("--version", "-V"):
            print(VERSION)
            return
        if a in ("--disasm", "--tokens"):
            show_disasm |= a == "--disasm"
            show_tokens |= a == "--tokens"
        elif a in ("-Werror", "--Werror"):
            werror = True
        elif a in ("-w", "--no-warn"):
            no_warn = True
        elif a == "--json":
            as_json = True
        elif a == "--check":
            check_only = True
        elif a in ("-o", "--output"):
            i += 1
            if i >= len(args):
                print("错误：-o 需要一个输出路径", file=sys.stderr)
                sys.exit(1)
            out_path = args[i]
        elif a in ("-O0", "-O1", "-O2", "-O3"):
            opt = int(a[2])
        elif a in ("--opt",):
            i += 1
            if i >= len(args) or args[i] not in "0123":
                print("错误：--opt 需要 0~3", file=sys.stderr)
                sys.exit(1)
            opt = int(args[i])
        elif a == "-":
            positional.append(a)
        else:
            positional.append(a)
        i += 1

    if opt < 3:
        # 优化级别：0 关闭超指令与常量折叠（便于调试字节码）
        os.environ["HENG_NO_SUPER"] = "1" if opt == 0 else os.environ.get("HENG_NO_SUPER", "")
    if werror:
        os.environ["HENG_WERROR"] = "1"
    if no_warn:
        os.environ["HENG_NOWARN"] = "1"

    args = positional

    # --run：直接跑字节码
    # --debug：逐条指令单步执行，把每步状态写进日志
    if args and args[0] == "--debug":
        sys.exit(_debug(args[1:]))

    # --repl：交互式输入（.app 双击进来的入口）
    if args and args[0] == "--repl":
        sys.exit(_repl())

    # --list-builtins：输出全部标准库与能力（AI 需要知道能用什么）
    if args and args[0] == "--list-builtins":
        from .stdlib2 import REG
        from .types2 import KNOWN_CAPABILITIES
        import json as _json
        builtins = sorted(REG.keys())
        caps = sorted(KNOWN_CAPABILITIES)
        if as_json:
            print(_json.dumps({"builtins": builtins, "capabilities": caps,
                               "count": len(builtins)}, ensure_ascii=False, indent=2))
        else:
            print(f"标准库函数（{len(builtins)} 个）：")
            for b in builtins:
                print("  " + b)
            print(f"\n能力（{len(caps)} 个，需 授权 后使用）：")
            for c in caps:
                print("  " + c)
        return

    # --explain：解释错误码
    if args and args[0] == "--explain":
        from .diagnostics import CODE_BY_CATEGORY, HINTS
        if len(args) < 2:
            print("用法：--explain <错误码>  如 --explain E01")
            print("现有错误码：")
            for cat, code in sorted(CODE_BY_CATEGORY.items(), key=lambda kv: kv[1]):
                print(f"  {code}  {cat}")
            return
        want = args[1].upper()
        for cat, code in CODE_BY_CATEGORY.items():
            if code == want:
                print(f"{code}（{cat}类错误）")
                for key, hint in HINTS:
                    print(f"  常见原因与建议：{hint}")
                return
        print(f"未知错误码：{want}")
        return

    if args and args[0] == "--run":
        if len(args) < 2:
            print("错误：--run 需要 .hbc 文件", file=sys.stderr)
            sys.exit(1)
        data = open(args[1], "rb").read()
        try:
            for line in run_bytes(data):
                print(line)
        except HengError as e:
            _report(e, "", args[1])
            sys.exit(1)
        return

    # --build：只编译
    if args and args[0] == "--build":
        if len(args) < 2:
            print("错误：--build 需要 .hng 文件", file=sys.stderr)
            sys.exit(1)
        path = args[1]
        src = _read(path)
        try:
            mod, _units = compile_src(src, filename=path)
        except HengError as e:
            _report(e, src, path)
            sys.exit(1)
        _warn(src, path)
        out = out_path or (os.path.splitext(path)[0] + ".hbc")
        with open(out, "wb") as f:
            f.write(mod.to_bytes())
        print(f"已生成字节码：{out}（{len(mod.to_bytes())} 字节，"
              f"{len(mod.funcs)} 个做法）")
        return

    # --check：只做检查，输出 JSON 供 AI 消费
    if check_only:
        if not args:
            print("错误：--check 需要 .hng 文件", file=sys.stderr)
            sys.exit(1)
        path = args[0]
        src = _read(path)
        sys.exit(_do_check(src, path, as_json))

    # -c：直接运行源码
    if args and args[0] == "-c":
        if len(args) < 2:
            print("错误：-c 需要一段源码", file=sys.stderr)
            sys.exit(1)
        src = args[1]
        stripped = src.strip()
        if stripped and not stripped.startswith(("开始", "定义", "常量", "记录", "导入", "模块")):
            src = "开始:\n" + "\n".join("  " + ln for ln in src.splitlines()) + "\n结束\n"
        _execute(src, None, show_disasm)
        return

    if not args:
        print("错误：未指定源文件", file=sys.stderr)
        print(USAGE, file=sys.stderr)
        sys.exit(1)

    path = args[0]
    del USER_ARGS[:]
    USER_ARGS.extend(args[1:])
    SCRIPT_PATH = os.path.abspath(path)
    src = _read(path)
    if show_tokens:
        from .lexer2 import tokenize
        for t in tokenize(src):
            print(t)
        return
    _execute(src, path, show_disasm)


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        print(f"文件不存在：{path}", file=sys.stderr)
        sys.exit(1)


def run_bytes(data: bytes) -> list[str]:
    """用原生 VM 执行字节码，返回输出的每一行。

    早期这里直接调一个不存在的函数，导致 --run 与 REPL 都抛 NameError——
    说明「只写了调用、没写实现」的路径从来没被测试过。
    """
    vm = ensure_native()
    if not vm:
        raise HengError("找不到原生 VM")
    import tempfile
    fd, tmp = tempfile.mkstemp(suffix=".hbc")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        r = subprocess.run([vm, tmp] + USER_ARGS, capture_output=True, text=True)
        if r.returncode != 0:
            msg = (r.stderr or f"退出码 {r.returncode}").strip()
            raise HengError(msg)
        return r.stdout.splitlines()
    finally:
        os.unlink(tmp)


def _debug(argv: list[str]) -> int:
    """单步调试。

    走原生 VM 的调试钩子（HENG_STEP / HENG_BP / HENG_DBG_LOG）。
    每停一步就把 PC 与槽位内容写进日志，用户能看到「执行到哪、变量是几」。
    这是真实的指令级单步，不是打印日志冒充的。
    """
    if not argv:
        print("用法：henger --debug <文件.hng> [断点PC,逗号分隔]")
        return 1
    path = argv[0]
    bps = argv[1] if len(argv) > 1 else ""
    src = _read(path)
    mod, _units = compile_src(src, filename=path)
    data = mod.to_bytes()
    vm = ensure_native()
    if not vm:
        print("找不到原生 VM", file=sys.stderr)
        return 1
    tmp = tempfile.mkdtemp()
    hbc = os.path.join(tmp, "a.hbc")
    log = os.path.join(tmp, "dbg.log")
    with open(hbc, "wb") as fh:
        fh.write(data)
    env = dict(os.environ)
    env["HENG_DBG_LOG"] = log
    if bps:
        env["HENG_BP"] = bps
    else:
        env["HENG_STEP"] = "1"
    r = subprocess.run([vm, hbc] + USER_ARGS, env=env, capture_output=True, text=True)
    print("── 程序输出 ──")
    print(r.stdout.rstrip() or "(无)")
    if r.stderr.strip():
        print("── 错误 ──")
        print(r.stderr.rstrip())
    print()
    print("── 单步轨迹（每步的 PC 与变量）──")
    if os.path.exists(log):
        with open(log, encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
        # 只打印 STOP 行与其后的槽位，跳过 WAITING/RESUME 噪音
        shown = 0
        for ln in lines:
            if ln.startswith("STOP|"):
                print(ln.replace("STOP|break|", "停 ▶ "))
                shown += 1
            elif ln.startswith("  [") and shown <= 200:
                print(ln)
        print(f"\n共 {sum(1 for l in lines if l.startswith('STOP|'))} 个停止点")
    else:
        print("(没有产生调试日志)")
    shutil.rmtree(tmp, ignore_errors=True)
    return 0


def _repl() -> int:
    """交互式解释器：输入一行或多行代码，回车执行。

    空行结束多行输入；`:q` 退出，`:h` 帮助，`:b` 列出标准库。
    变量不跨行保留（每次独立编译），这样语义最可预期。
    """
    print(VERSION)
    print("衡语交互式解释器。直接输入代码，空行执行；:q 退出，:h 帮助。")
    print()
    buf: list[str] = []
    while True:
        try:
            prompt = "衡语> " if not buf else "   ... "
            line = input(prompt)
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if line.strip() in (":q", ":quit", "退出"):
            return 0
        if line.strip() in (":h", ":help", "帮助"):
            print("  直接输入衡语代码，空行执行")
            print("  :q 退出   :h 帮助   :b 列出标准库")
            continue
        if line.strip() in (":b", "标准库"):
            from .stdlib2 import REG
            print("  " + "  ".join(sorted(REG)))
            continue
        if not line.strip():
            if not buf:
                continue
            src = "\n".join(buf)
            buf = []
            # 裸语句自动包进 开始…结束
            stripped = src.strip()
            if stripped and not stripped.startswith(
                    ("开始", "定义", "常量", "记录", "导入", "模块")):
                src = "开始:\n" + "\n".join("  " + ln for ln in src.splitlines()) + "\n结束\n"
            try:
                mod, units = compile_src(src)
                for out in run_bytes(mod.to_bytes()):
                    print("  " + out if out else "")
            except HengError as e:
                from .diagnostics import make_diagnostic
                print(make_diagnostic(e, src, "").render(), file=sys.stderr)
            continue
        buf.append(line)


def _do_check(src: str, path: str, as_json: bool) -> int:
    """只检查不运行。--json 时输出结构化结果供 AI / 编辑器消费。

    结构化输出是 AI 连接的基础：工具要能可靠地拿到
    「哪一行、哪一列、什么级别、什么建议」，而不是解析人类可读文本。
    """
    from .diagnostics import make_diagnostic
    from .lexer2 import tokenize
    from .parser2 import Parser
    from .warnings import check as check_warns
    import json

    diags: list[dict] = []
    ok = True
    try:
        prog = Parser(tokenize(src)).parse_program()
        for w in check_warns(prog):
            diags.append({
                "severity": "warning",
                "code": w.code,
                "message": w.message,
                "line": w.line,
                "column": w.col,
                "help": w.help,
                "file": path,
            })
        # 类型检查
        compile_src(src, filename=path)
    except HengError as e:
        d = make_diagnostic(e, src, path)
        diags.append({
            "severity": "error",
            "code": d.code,
            "message": d.message,
            "line": d.line,
            "column": d.col,
            "help": d.help,
            "file": path,
        })
        ok = False
    except Exception as e:      # noqa: BLE001 — 词法/解析异常
        diags.append({
            "severity": "error", "code": "E02", "message": str(e),
            "line": getattr(e, "line", 0), "column": getattr(e, "col", 0),
            "help": "", "file": path,
        })
        ok = False

    if as_json:
        print(json.dumps({
            "ok": ok,
            "file": os.path.abspath(path),
            "diagnostics": diags,
        }, ensure_ascii=False, indent=2))
    else:
        if not diags:
            print(f"{path}: 检查通过")
        else:
            for d in diags:
                level = "警告" if d["severity"] == "warning" else "错误"
                print(f"{path}:{d['line']}:{d['column']}: {level}[{d['code']}]: {d['message']}")
                if d["help"]:
                    print(f"  帮助: {d['help']}")
    return 0 if ok else 1


def _warn(src: str, path: str | None) -> None:
    """输出警告。设 HENG_WERROR=1 可升级为失败。"""
    import os as _os
    from .warnings import check as check_warns
    from .diagnostics import make_diagnostic
    from .lexer2 import tokenize
    from .parser2 import Parser
    try:
        ws = check_warns(Parser(tokenize(src)).parse_program())
    except Exception:      # noqa: BLE001 — 警告检查失败不影响主流程
        return
    if not ws:
        return
    fatal = _os.environ.get("HENG_WERROR") == "1"
    for w in ws:
        fake = Exception(f"{w.line}:{w.col} {w.message}")
        d = make_diagnostic(fake, src, path or "")
        d.help = w.help
        d.severity = "警告"
        d.fixed_code = w.code
        print(d.render(), file=sys.stderr)
    if fatal:
        print(f"错误：共 {len(ws)} 条警告，因 HENG_WERROR=1 视为失败", file=sys.stderr)
        sys.exit(1)


def _report(e: Exception, src: str, path: str | None) -> None:
    """统一的报错出口：带源码上下文与修复建议。"""
    from .diagnostics import make_diagnostic
    print(make_diagnostic(e, src, path or "").render(), file=sys.stderr)


def _report_runtime(stderr: str, mod, src: str, path: str | None,
                    code: int) -> None:
    """把 VM 的「@pc:func 错误<类>: 消息」翻成带源码行的诊断。"""
    import re
    from .diagnostics import make_diagnostic
    m = re.search(r"@(\d+):(-?\d+)\s*错误<([^>]+)>:\s*(.*)", stderr)
    if not m:
        if stderr.strip():
            sys.stderr.write(stderr if stderr.endswith("\n") else stderr + "\n")
        else:
            print(f"运行错误：原生 VM 退出码 {code}", file=sys.stderr)
        return
    pc, _fn, cat, msg = int(m.group(1)), int(m.group(2)), m.group(3), m.group(4)
    lines = getattr(mod, "entry_lines", []) or []
    line = lines[pc] if 0 <= pc < len(lines) else 0
    if not line:
        # 退一步：找最近的已知行
        for k in range(min(pc, len(lines) - 1), -1, -1):
            if lines[k]:
                line = lines[k]
                break
    diag = make_diagnostic(Exception(f"{line}:1 {cat}错误：{msg}"), src, path or "")
    print(diag.render(), file=sys.stderr)


# 传给衡语程序的用户参数（`henger app.hng a b` 里的 a b）
USER_ARGS: list[str] = []


def _execute(src: str, path: str | None, show_disasm: bool) -> None:
    try:
        mod, units = compile_src(src, filename=path)
    except HengError as e:
        _report(e, src, path)
        sys.exit(1)

    _warn(src, path)

    if show_disasm:
        print(disasm(mod))
        print()

    binary = mod.to_bytes()

    vm = ensure_native()
    if vm is None:
        print("错误：无法构建原生虚拟机。请确认已安装 C 编译器（clang 或 gcc）。",
              file=sys.stderr)
        sys.exit(1)

    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".hbc", delete=False) as f:
        f.write(binary)
        tmp = f.name
    try:
        # 环境变量把原始脚本路径带给 VM，避免用临时字节码的目录
        env = dict(os.environ)
        if SCRIPT_PATH:
            env["HENG_SCRIPT"] = SCRIPT_PATH
        # 让衡语程序知道自己是被谁跑的（IDE 要用它调编译器）
        env.setdefault("HENG_SELF", sys.executable if getattr(sys, "frozen", False)
                       else os.path.abspath(sys.argv[0]))
        if getattr(sys, "frozen", False):
            env["HENG_SELF"] = os.path.abspath(sys.executable)
        r = subprocess.run([vm, tmp] + USER_ARGS, capture_output=True, text=True, env=env)
        if r.stdout:
            sys.stdout.write(r.stdout)
        if r.returncode != 0:
            _report_runtime(r.stderr, mod, src, path, r.returncode)
            sys.exit(1)
    finally:
        os.unlink(tmp)


if __name__ == "__main__":
    main()
