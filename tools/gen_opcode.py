"""生成原生 VM 的指令宽度表。

动机：C 端 VM 必须精确知道每条指令占多少字节，否则跳转目标换算会错位。
这份信息在 heng2/bytecode.py 的 OPERANDS 里已经定义——这里从**唯一真源**
生成 C 头文件，避免两份手写表长期不一致（这类 bug 曾导致异常处理连锁失效）。

用法：python3 tools/gen_opcode.py
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from heng2.bytecode import Op, OPERANDS  # noqa: E402

FIELD_WIDTH = {"a": 1, "b": 2, "c": 4, "d": 4, "f": 8}

lines = [
    "/* 由 tools/gen_opcode.py 自动生成，请勿手改。",
    "   指令格式唯一真源：heng2/bytecode.py 的 OPERANDS。 */",
    "static const unsigned char HENG_OP_WIDTH[256] = {",
]
for i in range(256):
    try:
        op = Op(i)
        w = 1 + sum(FIELD_WIDTH[c] for c in OPERANDS.get(op, ""))
    except ValueError:
        w = 0
    lines.append(f"    {w},")
lines.append("};")

out = os.path.join(ROOT, "native", "opcode_width.h")
with open(out, "w", encoding="utf-8") as f:
    f.write("\n".join(lines) + "\n")
print(f"已生成 {out}")
