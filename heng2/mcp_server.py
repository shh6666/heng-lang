#!/usr/bin/env python3
"""衡语编译器的 MCP 服务端。

为什么需要它：AI 要能可靠地「写衡语代码 → 检查 → 运行 → 看结构化错误 → 修」，
靠人读文本输出不可靠，靠猜命令格式容易错。MCP 是标准协议，
接进去后 AI 可以直接把编译器当工具调用。

暴露的工具：
  heng_check      检查源码，返回结构化诊断
  heng_run        运行源码，返回输出与退出码
  heng_build      编译为字节码，返回产物路径与大小
  heng_list       列出标准库与能力
  heng_explain    解释错误码
  heng_examples   列出示例与验证项目（供学习语法）

运行（stdio 传输，符合 MCP 规范）：
  python3 -m heng2.mcp_server
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _vm() -> str:
    for cand in (Path(os.environ.get("HENG_VM", "")),
                 ROOT / "heng2" / "bin" / "hengvm",
                 ROOT / "native" / "bin" / "hengvm"):
        if cand and cand.is_file() and os.access(cand, os.X_OK):
            return str(cand)
    return "hengvm"


def _heng() -> str:
    """找 henger 可执行；退回本仓库的 __main__。"""
    exe = os.environ.get("HENG_SELF", "")
    if exe and os.path.isfile(exe):
        return exe
    for cand in (Path.home() / ".local" / "bin" / "henger",
                 Path("/usr/local/bin/henger"),
                 ROOT / "dist" / "henger"):
        if cand.is_file():
            return str(cand)
    return f"{sys.executable} -m heng2"


# ---------------- 工具实现 ----------------

def tool_check(src: str) -> dict:
    """检查源码。优先走 henger --check --json（与产品行为一致）。"""
    with tempfile.NamedTemporaryFile("w", suffix=".hng", delete=False,
                                     encoding="utf-8") as fh:
        fh.write(src)
        path = fh.name
    try:
        cmd = _heng().split() + ["--check", path, "--json"]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        out = r.stdout.strip()
        if out.startswith("{"):
            data = json.loads(out)
            data["exit_code"] = r.returncode
            return data
        # 退回 Python 侧检查
        from heng2.__main__ import _do_check
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            _do_check(src, path, True)
        return json.loads(buf.getvalue() or "{}")
    finally:
        os.unlink(path)


def tool_run(src: str, args: list[str] | None = None) -> dict:
    with tempfile.NamedTemporaryFile("w", suffix=".hng", delete=False,
                                     encoding="utf-8") as fh:
        fh.write(src)
        path = fh.name
    try:
        cmd = _heng().split() + [path] + (args or [])
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        return {
            "ok": r.returncode == 0,
            "exit_code": r.returncode,
            "stdout": r.stdout,
            "stderr": r.stderr,
        }
    finally:
        os.unlink(path)


def tool_build(src: str) -> dict:
    with tempfile.NamedTemporaryFile("w", suffix=".hng", delete=False,
                                     encoding="utf-8") as fh:
        fh.write(src)
        path = fh.name
    out = path.replace(".hng", ".hbc")
    try:
        cmd = _heng().split() + ["--build", path, "-o", out]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        size = os.path.getsize(out) if os.path.exists(out) else 0
        return {"ok": r.returncode == 0, "result": r.stdout or r.stderr,
                "bytes": size}
    finally:
        for p in (path, out):
            if os.path.exists(p):
                os.unlink(p)


def tool_list() -> dict:
    cmd = _heng().split() + ["--list-builtins", "--json"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    try:
        return json.loads(r.stdout)
    except Exception:      # noqa: BLE001
        from heng2.stdlib2 import REG
        from heng2.types2 import KNOWN_CAPABILITIES
        return {"builtins": sorted(REG), "capabilities": sorted(KNOWN_CAPABILITIES)}


def tool_explain(code: str) -> dict:
    from heng2.diagnostics import CODE_BY_CATEGORY, HINTS
    code = code.upper()
    for cat, c in CODE_BY_CATEGORY.items():
        if c == code:
            return {"code": code, "category": cat,
                    "hints": [h for k, h in HINTS]}
    return {"code": code, "error": "未知错误码",
            "available": sorted(CODE_BY_CATEGORY.values())}


def tool_examples(limit: int = 40) -> dict:
    vroot = ROOT / "validation"
    items = []
    if vroot.is_dir():
        for d in sorted(vroot.iterdir()):
            if d.is_dir():
                for f in sorted(d.glob("*.hng"))[:3]:
                    try:
                        items.append({"name": f"{d.name}/{f.name}",
                                      "source": f.read_text(encoding="utf-8")})
                    except OSError:
                        pass
    return {"count": len(items), "examples": items[:limit]}


TOOLS = [
    {
        "name": "heng_check",
        "description": "检查衡语源码（语法与类型），返回结构化诊断列表。"
                       "写代码后应先调用它确认无误再运行。",
        "inputSchema": {
            "type": "object",
            "properties": {"src": {"type": "string", "description": "衡语源码"}},
            "required": ["src"],
        },
    },
    {
        "name": "heng_run",
        "description": "运行衡语源码，返回 stdout / stderr / 退出码。",
        "inputSchema": {
            "type": "object",
            "properties": {"src": {"type": "string", "description": "衡语源码"},
                           "args": {"type": "array", "items": {"type": "string"},
                                    "description": "传给程序的命令行参数"}},
            "required": ["src"],
        },
    },
    {
        "name": "heng_build",
        "description": "把衡语源码编译成 .hbc 字节码，返回字节数。",
        "inputSchema": {
            "type": "object",
            "properties": {"src": {"type": "string"}},
            "required": ["src"],
        },
    },
    {
        "name": "heng_list",
        "description": "列出全部标准库函数与需要授权的能力名。写代码前查一下能用什么。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "heng_explain",
        "description": "解释一个错误码（E01~E09 / W01~W03），给出常见原因与修复建议。",
        "inputSchema": {
            "type": "object",
            "properties": {"code": {"type": "string"}},
            "required": ["code"],
        },
    },
    {
        "name": "heng_examples",
        "description": "取若干真实项目源码作为语法参考。",
        "inputSchema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "default": 20}},
        },
    },
]


def dispatch(name: str, args: dict) -> dict:
    if name == "heng_check":
        return tool_check(args.get("src", ""))
    if name == "heng_run":
        return tool_run(args.get("src", ""), args.get("args"))
    if name == "heng_build":
        return tool_build(args.get("src", ""))
    if name == "heng_list":
        return tool_list()
    if name == "heng_explain":
        return tool_explain(args.get("code", ""))
    if name == "heng_examples":
        return tool_examples(int(args.get("limit", 20)))
    raise ValueError(f"未知工具：{name}")


# ---------------- MCP stdio 协议 ----------------

def _send(obj: dict) -> None:
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    sys.stdout.buffer.write(f"Content-Length: {len(body)}\r\n\r\n".encode())
    sys.stdout.buffer.write(body)
    sys.stdout.buffer.flush()


def _read_message() -> dict | None:
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        if line in (b"\r\n", b"\n"):
            break
        k, _, v = line.decode().partition(":")
        headers[k.strip().lower()] = v.strip()
    n = int(headers.get("content-length", "0"))
    if n <= 0:
        return None
    return json.loads(sys.stdin.buffer.read(n).decode("utf-8"))


def main() -> int:
    while True:
        msg = _read_message()
        if msg is None:
            return 0
        method = msg.get("method", "")
        mid = msg.get("id")

        if method == "initialize":
            _send({"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "heng-compiler", "version": "2.0.0"},
            }})
        elif method == "tools/list":
            _send({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            params = msg.get("params", {})
            name = params.get("name", "")
            args = params.get("arguments", {}) or {}
            try:
                result = dispatch(name, args)
                _send({"jsonrpc": "2.0", "id": mid, "result": {
                    "content": [{"type": "text",
                                 "text": json.dumps(result, ensure_ascii=False, indent=2)}],
                    "isError": False,
                }})
            except Exception as e:      # noqa: BLE001
                _send({"jsonrpc": "2.0", "id": mid, "result": {
                    "content": [{"type": "text", "text": f"工具执行失败：{e}"}],
                    "isError": True,
                }})
        elif method in ("notifications/initialized", "notifications/cancelled"):
            continue
        elif mid is not None:
            _send({"jsonrpc": "2.0", "id": mid, "error": {
                "code": -32601, "message": f"未实现的方法：{method}"}})


if __name__ == "__main__":
    sys.exit(main())
