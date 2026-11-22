"""衡语 LSP Server：基于 stdio 的 JSON-RPC 语言服务器。

复用现有 Lexer/Parser/TypeChecker，提供：
  - textDocument/publishDiagnostics  实时诊断（词法/语法/类型/量纲/能力错误）
  - textDocument/completion         关键字与标识符补全
  - textDocument/hover              关键字悬停说明
  - textDocument/definition          函数/变量定义跳转
  - textDocument/didOpen/didChange   文档同步

零第三方依赖，符合 LSP 3.17 基础协议子集。
"""

from __future__ import annotations

import json
import sys
from typing import Any, Optional

from .lexer import Lexer, LexError, KEYWORDS
from .parser import Parser, ParseError
from .typecheck import TypeChecker, TypeError_

# 关键字悬停文档
KEYWORD_DOCS = {
    "开始": "程序入口：`开始` 或 `开始:`，直到 `结束`。",
    "如果": "条件：`如果 条件: … 否则: … 结束`。",
    "否则": "条件分支，与 `如果` 配套。",
    "结束": "块结束标记，与开块语句配套（替代旧词「完毕」）。",
    "每个": "遍历：`每个 集合 为 变量: … 结束`（替代「遍历…为…」）。",
    "放到": "赋值：`将 表达式 放到 变量`（替代「赋于」）。",
    "说出": "输出：`说出 表达式` 或 `说出(表达式)`（替代「输出」）。",
    "做法": "函数：`定义 做法 名(参数:类型) -> 返回类型: … 结束`（替代「函数」）。",
    "尝试": "异常：`尝试: … 接住 错误<类别> 为 e: … 最后: … 结束`。",
    "接住": "捕获子句（替代「捕获」）。",
    "报错": "抛出错误：`报错 错误<类别>(\"消息\")`（替代「抛出」）。",
    "最后": "收尾子句，无论成败都执行（替代「终结」）。",
    "必须": "断言：`必须 条件`，不成立则报错（替代「断言」）。",
    "同时": "并行块：`同时: … 结束`，块末自动会合（替代「并行」）。",
    "发起": "启动并发任务：`发起 函数名(参数)`（替代「启」）。",
    "接收": "接收消息：`接收(信箱) 为 变量`（替代「收」）。",
    "约定": "契约块：`约定: 要求… 保证… 结束`（替代「规约」）。",
    "要求": "前置条件（替代「前置」）。",
    "保证": "后置条件，可用 `结果`（替代「后置」）。",
    "重复": "计数循环：`重复 N 次: … 结束`。",
    "定义": "定义结构：`定义 做法/记录 名称`。",
    "记录": "记录定义：`定义 记录 名: 字段… 结束`。",
    "字段": "记录字段：`字段 名 : 类型`。",
    "常量": "常量：`常量 名 : 类型 = 值`。",
    "变量": "变量：`变量 名 : 类型 = 初值`。块级作用域，禁止同名遮蔽。",
    "数": "数值类型，可带单位：`数 元/个` 或 `数<元/个>`。",
    "文本": "文本（字符串）类型。",
    "真假": "布尔类型，字面量 `真`/`假`。",
    "序列": "序列类型：`序列 文本` 或 `序列<文本>`。",
    "映射": "映射类型：`映射 文本 数` 或 `映射<文本,数>`。",
    "真": "布尔真值。",
    "假": "布尔假值。",
    "空": "空值。",
    "非": "逻辑非。",
    "并且": "逻辑与（短路）：`甲 并且 乙`。",
    "或者": "逻辑或（短路）：`甲 或者 乙`。",
    "循环": "当型循环：`循环 条件: … 结束`。",
    "返回": "返回：`返回 表达式`。",
    "可能生": "错误签名：`可能生 错误<超时>, 错误<拒绝>`。",
    "错误": "错误类型：`错误<类别>`。",
    "沙箱": "沙箱块：`沙箱: … 结束`，默认仅 `说出` 可用。",
    "授权": "能力授权：`授权 工具.查询`。",
    "执行": "工具执行：`执行 x = 工具.动作(...)`。",
    "观察": "观察块：`观察 x: … 结束`，构成执行—观察闭环。",
    "将": "赋值引导词：`将 表达式 放到 变量`。",
    "为": "遍历/接收的绑定词：`每个 … 为 …`。",
    "模块": "命名空间：`模块 名称`。",
    "导入": "导入模块。",
    "导出": "导出定义。",
    "注": "注释：`注：…` 到行尾。",
}

COMPLETION_ITEMS = [
    {"label": w, "kind": 14, "detail": "关键字", "documentation": KEYWORD_DOCS.get(w, "")}
    for w in sorted(KEYWORDS)
]


class LSPServer:
    def __init__(self):
        self.docs: dict[str, str] = {}
        self.positions: dict[str, list[int]] = {}  # uri → 每行起始 offset

    # ---- 协议框架 ----
    def send(self, msg: dict) -> None:
        data = json.dumps(msg, ensure_ascii=False).encode("utf-8")
        sys.stdout.buffer.write(f"Content-Length: {len(data)}\r\n\r\n".encode() + data)
        sys.stdout.buffer.flush()

    def log(self, msg: str) -> None:
        # 写到 stderr，不干扰 LSP 通道
        sys.stderr.write(f"[heng-lsp] {msg}\n")

    def run(self) -> None:
        while True:
            headers = {}
            while True:
                line = sys.stdin.buffer.readline()
                if not line:
                    return
                if line in (b"\r\n", b"\n"):
                    break
                if b":" in line:
                    k, v = line.decode().split(":", 1)
                    headers[k.strip().lower()] = v.strip()
            length = int(headers.get("content-length", 0))
            if not length:
                continue
            body = sys.stdin.buffer.read(length)
            msg = json.loads(body.decode("utf-8"))
            self.dispatch(msg)

    def dispatch(self, msg: dict) -> None:
        method = msg.get("method")
        mid = msg.get("id")
        params = msg.get("params", {})
        if method == "initialize":
            self.send({"jsonrpc": "2.0", "id": mid, "result": {
                "capabilities": {
                    "textDocumentSync": 1,
                    "completionProvider": {"triggerCharacters": []},
                    "hoverProvider": True,
                    "definitionProvider": True,
                },
                "serverInfo": {"name": "heng-lsp", "version": "1.0.0"},
            }})
        elif method == "initialized":
            pass
        elif method == "shutdown":
            self.send({"jsonrpc": "2.0", "id": mid, "result": None})
        elif method == "exit":
            sys.exit(0)
        elif method == "textDocument/didOpen":
            uri = params["textDocument"]["uri"]
            text = params["textDocument"]["text"]
            self.docs[uri] = text
            self.publish(uri)
        elif method == "textDocument/didChange":
            uri = params["textDocument"]["uri"]
            if params.get("contentChanges"):
                text = params["contentChanges"][-1]["text"]
                self.docs[uri] = text
                self.publish(uri)
        elif method == "textDocument/completion":
            self.send({"jsonrpc": "2.0", "id": mid, "result": COMPLETION_ITEMS})
        elif method == "textDocument/hover":
            self.hover(msg)
        elif method == "textDocument/definition":
            self.definition(msg)

    # ---- 诊断 ----
    def publish(self, uri: str) -> None:
        text = self.docs.get(uri, "")
        diags = self.diagnose(text)
        self.send({"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics",
                   "params": {"uri": uri, "diagnostics": diags}})

    def diagnose(self, text: str) -> list[dict]:
        """返回 LSP 3.17 诊断数组。类型阶段支持多错误一次性返回。"""
        try:
            toks = Lexer(text).tokens()
        except LexError as e:
            return [self._diag(str(e), 0, 0, 1)]
        try:
            prog = Parser(toks).parse_program()
        except ParseError as e:
            line = e.tok.line if e.tok else 1
            col = e.tok.col if e.tok else 1
            return [self._diag(str(e), max(0, line - 1), max(0, col - 1), 1)]
        # 多错误收集：一次返回全部可发现的类型/量纲/能力问题
        bag = TypeChecker(prog).collect_errors()
        return bag.to_lsp()

    @staticmethod
    def _diag(msg: str, line: int, col: int, severity: int = 1) -> dict:
        return {
            "range": {
                "start": {"line": line, "character": col},
                "end": {"line": line, "character": col + 1},
            },
            "severity": severity,
            "source": "衡语",
            "message": msg,
        }

    # ---- 悬停 ----
    def hover(self, msg: dict) -> None:
        params = msg["params"]
        uri = params["textDocument"]["uri"]
        pos = params["position"]
        text = self.docs.get(uri, "")
        word = self._word_at(text, pos["line"], pos["character"])
        doc = KEYWORD_DOCS.get(word)
        if doc:
            self.send({"jsonrpc": "2.0", "id": msg["id"], "result": {
                "contents": {"kind": "markdown", "value": f"**{word}**\n\n{doc}"}
            }})
        else:
            self.send({"jsonrpc": "2.0", "id": msg["id"], "result": None})

    # ---- 定义跳转 ----
    def definition(self, msg: dict) -> None:
        params = msg["params"]
        uri = params["textDocument"]["uri"]
        pos = params["position"]
        text = self.docs.get(uri, "")
        word = self._word_at(text, pos["line"], pos["character"])
        lines = text.split("\n")
        for i, line in enumerate(lines):
            # 函数/记录/变量/常量定义
            for prefix in (f"定义 函数 {word}", f"定义 记录 {word}",
                           f"变量 {word}", f"常量 {word}"):
                c = line.find(prefix)
                if c >= 0:
                    start = c + len(prefix) - len(word)
                    self.send({"jsonrpc": "2.0", "id": msg["id"], "result": {
                        "uri": uri,
                        "range": {
                            "start": {"line": i, "character": start},
                            "end": {"line": i, "character": start + len(word)},
                        },
                    }})
                    return
        self.send({"jsonrpc": "2.0", "id": msg["id"], "result": None})

    @staticmethod
    def _word_at(text: str, line: int, character: int) -> str:
        lines = text.split("\n")
        if line >= len(lines):
            return ""
        s = lines[line]
        # 中文/字母/数字/下划线视为词字符
        def isw(ch: str) -> bool:
            return ch.isalnum() or ch == "_"
        if character >= len(s):
            return ""
        # 向前找词首
        a = character
        while a > 0 and isw(s[a - 1]):
            a -= 1
        b = character
        while b < len(s) and isw(s[b]):
            b += 1
        return s[a:b]


def main() -> None:
    LSPServer().run()


if __name__ == "__main__":
    main()
