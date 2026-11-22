"""诊断渲染：把「行:列 错误信息」渲染成带源码上下文的报错。

调研主流编译器（clang / rustc）后确定的格式：

    /tmp/bad.hng:4:6: 错误[E01]: 未定义变量 undefined_var
      |
    4 |   说出 undefined_var
      |        ^^^^^^^^^^^^^
      |
    帮助: 检查变量名是否拼错；局部变量需要先「变量 名字 : 类型 = 值」声明

为什么值得做：衡语的目标用户是中文业务人员。他们看不懂「4:6」，
但一定看得懂带箭头指向的源码行。这是语言可用性的关键一环。
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# 错误类别 → 稳定的短编号，便于文档与搜索。
# 编号只增不改，保证已有报错信息的可引用性。
CODE_BY_CATEGORY = {
    "类型": "E01",
    "语法": "E02",
    "越界": "E03",
    "除零": "E04",
    "未定义": "E05",
    "能力": "E06",
    "断言": "E07",
    "配置": "E08",
    "悬空": "E09",
}

# 常见错误的修复建议。键是「错误信息里会出现的关键字」。
HINTS = [
    ("未定义变量", "检查变量名是否拼错；局部变量需要先「变量 名字 : 类型 = 值」声明"),
    ("未定义的函数或能力", "检查函数名；标准库以「模块.函数」形式调用，如 文字.长度"),
    ("类型不符", "检查两侧类型是否一致；数值与文本不能直接相加，用 化成文 转换"),
    ("量纲不匹配", "检查单位是否约掉正确，如 个 × 元/个 = 元"),
    ("下标超出范围", "下标从 0 开始；可用 表.长度 检查上界"),
    ("除零", "除数可能为 0，先判断再除"),
    ("该值不支持下标访问", "只有 序列 与 映射 支持下标；文本用 文字.截 取子串"),
    ("该值没有字段", "检查记录是否有该字段，或字段名是否拼错"),
    ("参数数量不符", "检查调用时传入的参数个数"),
    ("缺少返回", "有返回类型的做法必须在所有分支返回值"),
]

RESET = "\033[0m"
BOLD = "\033[1m"
RED = "\033[31m"
CYAN = "\033[36m"
YELLOW = "\033[33m"


@dataclass
class Diagnostic:
    message: str
    category: str = "类型"
    line: int = 0
    col: int = 0
    path: str = ""
    source: str = ""
    help: str = ""
    # 警告用：级别名与固定编号（如 警告 / W01）
    severity: str = "错误"
    fixed_code: str = ""

    @property
    def code(self) -> str:
        if self.fixed_code:
            return self.fixed_code
        return CODE_BY_CATEGORY.get(self.category, "E00")

    def render(self, color: bool | None = None) -> str:
        if color is None:
            color = os.environ.get("NO_COLOR") is None and os.isatty(1)
        c_err = RED + BOLD if color else ""
        c_dim = CYAN if color else ""
        c_help = YELLOW if color else ""
        c_off = RESET if color else ""

        where = self.path or "<输入>"
        head = (f"{where}:{self.line}:{self.col}: "
                f"{c_err}{self.severity}[{self.code}]{c_off}: {self.message}")

        if not self.source or self.line <= 0:
            if self.help:
                return head + f"\n  {c_help}帮助{c_off}: {self.help}"
            return head

        lines = self.source.splitlines()
        if self.line > len(lines):
            return head

        text = lines[self.line - 1]
        num = str(self.line)
        pad = " " * len(num)
        # 制表符会破坏对齐，先展开
        text = text.expandtabs(4)
        # 列号按字符计；用等宽位置近似即可（中文是双宽，这里不追求像素级精确）
        col = max(1, self.col)
        span = 1
        rest = text[col - 1:] if col - 1 < len(text) else ""
        # 尽量把整个标识符/词语括起来，视觉上比单个 ^ 更有用
        if rest and (rest[0].isalnum() or rest[0] == "_" or ord(rest[0]) > 127):
            i = 0
            while i < len(rest) and (rest[i].isalnum() or rest[i] == "_" or ord(rest[i]) > 127):
                i += 1
            span = max(1, i)

        out = [
            head,
            f"  {c_dim}|{c_off}",
            f"{c_dim}{num} |{c_off} {text}",
            f"  {c_dim}|{c_off} {pad} " + " " * (col - 1) + c_err + "^" * span + c_off,
        ]
        if self.help:
            out.append(f"  {c_dim}|{c_off}")
            out.append(f"  {c_help}帮助{c_off}: {self.help}")
        return "\n".join(out)


def hint_for(message: str) -> str:
    for key, hint in HINTS:
        if key in message:
            return hint
    return ""


def make_diagnostic(err: Exception, source: str = "", path: str = "") -> Diagnostic:
    """从异常构造诊断。异常的 str 形如「4:6 类型错误：未定义变量 x」。"""
    msg = str(err)
    line = col = 0
    category = "类型"
    body = msg

    import re
    m = re.match(r"^\s*(\d+):(\d+)\s*(.*)$", msg)
    if m:
        line, col = int(m.group(1)), int(m.group(2))
        body = m.group(3)
    # 去掉「类型错误：」这类前缀，类别单独取
    m2 = re.match(r"^(\S*?错误)[:：]\s*(.*)$", body, re.S)
    if m2:
        cat_word = m2.group(1)
        category = cat_word.replace("错误", "") or "类型"
        body = m2.group(2)
    else:
        m3 = re.match(r"^错误<([^>]+)>[:：]?\s*(.*)$", body, re.S)
        if m3:
            category, body = m3.group(1), m3.group(2)

    return Diagnostic(
        message=body.strip(),
        category=category,
        line=line,
        col=col,
        path=path,
        source=source,
        help=hint_for(body),
    )
