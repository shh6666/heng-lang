#!/usr/bin/env python3
"""并行验证所有 validation 项目。

200+ 个项目串行跑要几分钟。这里用进程池并行编译，
再批量调原生 VM——用于开发时快速反馈。

用法：
    python3 tools/check_all.py            # 全部
    python3 tools/check_all.py W_erp      # 只跑某目录
    python3 tools/check_all.py -v         # 显示每个项目的输出
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def one(args):
    path = args
    from heng2.api2 import compile_src, HengError
    try:
        mod, _ = compile_src(Path(path).read_text(encoding="utf-8"))
    except HengError as e:
        return (path, "编译失败", str(e)[:160], "")
    except Exception as e:              # noqa: BLE001
        return (path, "编译异常", f"{type(e).__name__}: {e}"[:160], "")
    fd, tmp = tempfile.mkstemp(suffix=".hbc")
    os.close(fd)
    try:
        with open(tmp, "wb") as fh:
            fh.write(mod.to_bytes())
        vm = str(ROOT / "native" / "bin" / "hengvm")
        r = subprocess.run([vm, tmp], capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            return (path, "运行失败", r.stderr.strip()[:160], "")
        out = r.stdout.strip()
        if not out:
            return (path, "无输出", "", "")
        return (path, "通过", "", out)
    except subprocess.TimeoutExpired:
        return (path, "超时", "", "")
    finally:
        os.unlink(tmp)


def main() -> int:
    verbose = "-v" in sys.argv
    want = [a for a in sys.argv[1:] if not a.startswith("-")]

    files = []
    vroot = ROOT / "validation"
    for d in sorted(vroot.iterdir()):
        if not d.is_dir():
            continue
        if want and d.name not in want:
            continue
        files += sorted(str(f) for f in d.glob("*.hng"))
    if not files:
        print("没有找到项目")
        return 1

    with ProcessPoolExecutor(max_workers=min(16, os.cpu_count() or 4)) as ex:
        results = list(ex.map(one, files))

    bad = [r for r in results if r[1] != "通过"]
    print(f"共 {len(results)} 个项目：通过 {len(results) - len(bad)}，问题 {len(bad)}")
    if bad:
        print()
        for path, kind, msg, _ in bad:
            print(f"  ✗ {os.path.basename(path):<32} {kind}  {msg}")
    if verbose:
        print()
        for path, kind, _msg, out in results:
            if kind == "通过":
                head = out.splitlines()
                preview = " | ".join(head[:3])
                more = f" …(+{len(head) - 3})" if len(head) > 3 else ""
                print(f"  {os.path.basename(path):<32} {preview}{more}")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
