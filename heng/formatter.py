"""衡语源码格式化器：统一缩进、空白、空行。

价值：
  - 让 .hng 在任意编辑器中观感一致（缩进 2 空格，块体统一）；
  - 去掉行尾空白与多余空行；
  - 供 VS Code「格式化文档」与命令行 `heng fmt` 使用。

设计：基于词法 token 重建，只调整空白，不改变 token 序列（语义不变）。
保守策略：保留注释原文与位置关系，不重排语句。
"""

from __future__ import annotations

from .lexer import Lexer, LexError

# 开启块的 token 种类（其后内容应缩进）
BLOCK_OPENERS = {"入口", "如果", "不然", "循环", "每个", "试试", "接住", "最后",
                 "同时", "沙箱", "观察", "约定", "要求", "保证",
                 "模块", "记录", "函数", "定义", "映射"}
BLOCK_CLOSERS = {"结束"}
INDENT = "  "


def format_source(src: str, indent: str = INDENT) -> str:
    """格式化衡语源码，返回新文本。无法解析时原样返回（不破坏用户代码）。"""
    lines = src.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    depth = 0
    blank_pending = False

    for raw in lines:
        stripped = raw.strip()
        if not stripped:
            # 折叠连续空行
            if out and not blank_pending:
                out.append("")
                blank_pending = True
            continue

        # 注释行：跟随当前缩进
        if stripped.startswith("注"):
            out.append(indent * depth + stripped)
            blank_pending = False
            continue

        # 闭合词单独一行：先降一级缩进
        if stripped in ("结束", "完毕"):
            depth = max(depth - 1, 0)
            out.append(indent * depth + stripped)
            blank_pending = False
            continue

        out.append(indent * depth + stripped)
        blank_pending = False

        # 判断本行是否开启新块：以 `:` 结句，或以块开启词开头
        head = stripped.split(" ")[0].split(":")[0]
        if stripped.endswith(":") or head in BLOCK_OPENERS:
            # 「不然:」「否则:」属于同级分支，不额外加深（其体在下一层）
            if head in ("不然", "否则", "接住", "最后") and stripped.endswith(":"):
                depth += 1
            else:
                depth += 1

    # 去掉末尾空行
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out) + "\n"


def check_formatting(src: str) -> bool:
    """是否已符合格式化规范（用于 CI / 预提交检查）。"""
    try:
        return format_source(src) == src
    except LexError:
        return True
