#!/usr/bin/env python3
"""Token 编号冲突自检。

教训：给 % 运算符新加 PERCENT 时，赋了 24，而 EQ（=）也是 24。
结果 `如果 x = 5` 被解析成取模运算，41 个验证项目同时失败。
枚举值重复不会报错，只会静默改变语义——必须机器检查。

用法：python3 tools/check_token_ids.py [--strict]
"""

from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from heng2.token import K  # noqa: E402


def main() -> int:
    seen: dict[int, list[str]] = defaultdict(list)
    for name in sorted(dir(K)):
        if not name.isupper():
            continue
        val = getattr(K, name)
        try:
            seen[int(val)].append(name)
        except (TypeError, ValueError):
            continue

    dups = {v: names for v, names in seen.items() if len(names) > 1}
    if not dups:
        print(f"✓ Token 编号无冲突（共 {len(seen)} 个）")
        return 0

    print("⚠ Token 编号冲突（会导致语义静默错乱）:")
    for v, names in sorted(dups.items()):
        print(f"    {v}: {' / '.join(names)}")
    return 1 if "--strict" in sys.argv else 0


if __name__ == "__main__":
    sys.exit(main())
