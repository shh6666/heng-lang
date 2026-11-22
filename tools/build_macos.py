#!/usr/bin/env python3
"""把衡语编译器 + 原生 VM 打包成 macOS 单文件可执行 `henger`。

产物结构（PyInstaller onefile 内部）：
    henger                 ← 单文件可执行（Python 运行时 + 编译器 + VM 全在里面）
    heng2/bin/hengvm       ← 原生 VM（作为数据文件随包带上）

为什么必须带 VM：`ensure_native()` 会尝试现场用 clang 编译，
但目标机器不一定装了编译器。打包产物必须自带已编译好的 VM。

用法：
    python3 tools/build_macos.py            # 生成 dist/henger
    python3 tools/build_macos.py --check    # 只检查环境
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
BUILD = ROOT / "build"

ENTRY = '''
import sys
from heng2.__main__ import main
if __name__ == "__main__":
    sys.exit(main() or 0)
'''


def sh(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, **kw)


def check_env() -> bool:
    ok = True
    print(f"平台: {platform.system()} {platform.machine()}")
    print(f"Python: {sys.version.split()[0]}")

    try:
        import PyInstaller  # noqa: F401
        print("PyInstaller: 已安装")
    except ImportError:
        print("PyInstaller: 缺失 → pip install pyinstaller")
        ok = False

    vm = ROOT / "native" / "bin" / "hengvm"
    if vm.exists():
        print(f"原生 VM: {vm}")
    else:
        print("原生 VM: 缺失，尝试编译…")
        cc = shutil.which("clang") or shutil.which("cc")
        if not cc:
            print("  找不到 C 编译器")
            ok = False
        else:
            vm.parent.mkdir(parents=True, exist_ok=True)
            r = sh([cc, "-O3", "-o", str(vm), str(ROOT / "native" / "hengvm.c"), "-lm"])
            print("  编译" + ("成功" if r.returncode == 0 else f"失败：{r.stderr[:200]}"))
            ok = ok and r.returncode == 0
    return ok


def build() -> int:
    if not check_env():
        print("\n环境不满足，未打包")
        return 1

    print("\n开始打包…")
    vm = ROOT / "native" / "bin" / "hengvm"

    # 把 VM 放到包内可寻址的位置：heng2/bin/hengvm
    pkg_bin = ROOT / "heng2" / "bin"
    pkg_bin.mkdir(parents=True, exist_ok=True)
    shutil.copy2(vm, pkg_bin / "hengvm")
    os.chmod(pkg_bin / "hengvm", 0o755)

    entry = BUILD / "_entry.py"
    BUILD.mkdir(parents=True, exist_ok=True)
    entry.write_text(ENTRY, encoding="utf-8")

    sep = os.pathsep
    add_data = f"{pkg_bin / 'hengvm'}{sep}heng2/bin"

    # 图形 IDE 的原生桥接库：有则打进包，供 导入 外部 使用
    gui_lib = ROOT / "native" / "bin" / "libhengui.dylib"
    gui_src = ROOT / "native" / "hengui.m"
    extra = []
    if not gui_lib.exists() and gui_src.exists():
        print("编译图形桥接库…")
        subprocess.run(["clang", "-O2", "-fobjc-arc", "-dynamiclib",
                        "-o", str(gui_lib), str(gui_src), "-framework", "Cocoa"],
                       cwd=ROOT, capture_output=True)
    if gui_lib.exists():
        shutil.copy2(gui_lib, pkg_bin / "libhengui.dylib")
        extra += ["--add-data", f"{pkg_bin / 'libhengui.dylib'}{sep}heng2/bin"]

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",
        "--clean",
        "--noconfirm",
        "--name", "henger",
        "--distpath", str(DIST),
        "--workpath", str(BUILD),
        "--specpath", str(BUILD),
        # VM 二进制作为数据文件
        "--add-data", add_data,
        *extra,
        # 编译器只用标准库，排除大件以缩小体积
        "--exclude-module", "tkinter",
        "--exclude-module", "unittest",
        "--exclude-module", "pydoc",
        "--exclude-module", "test",
        str(entry),
    ]
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("打包失败：")
        print((r.stdout + r.stderr)[-2500:])
        return 1

    out = DIST / "henger"
    if not out.exists():
        print("打包失败：未生成 dist/henger")
        return 1
    size_mb = out.stat().st_size / 1024 / 1024
    print(f"\n✓ 已生成 {out}（{size_mb:.1f} MB）")

    # 自检：跑一个最小程序
    print("\n自检…")
    probe = BUILD / "_probe.hng"
    probe.write_text("\n".join(["开始:", "  说出 1 + 2", "结束", ""]), encoding="utf-8")
    pr = subprocess.run([str(out), str(probe)], capture_output=True, text=True)
    if pr.returncode != 0:
        print(f"自检失败（退出码 {pr.returncode}）：{pr.stderr[:400]}")
        return 1
    print(f"  运行输出: {pr.stdout.strip()}")
    if pr.stdout.strip() != "3":
        print("自检失败：输出不是 3")
        return 1
    print("  ✓ 自检通过")
    return 0


if __name__ == "__main__":
    sys.exit(0 if ("--check" in sys.argv and check_env()) or ("--check" not in sys.argv and build() == 0) else 1)
