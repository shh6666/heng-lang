#!/usr/bin/env python3
"""把 henger 打包成 macOS 的 .app，装进 /Applications 后能在访达看到图标并双击。

为什么需要：命令行工具装在 ~/.local/bin，访达里根本看不见——
用户会觉得「没装上」。.app 是 macOS 的唯一可见产品形态。

双击行为：打开终端窗口并进入 REPL（不然后台进程一闪而过，什么都看不到）。

用法：
    python3 tools/build_app.py            # 生成 dist/衡语.app
    python3 tools/build_app.py --install  # 顺便装到 /Applications
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
APP_NAME = "衡语 IDE"
BUNDLE_ID = "cn.henglang.ide"

INFO_PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>{name}</string>
  <key>CFBundleDisplayName</key><string>{name}</string>
  <key>CFBundleIdentifier</key><string>{bid}</string>
  <key>CFBundleVersion</key><string>2.0.0</string>
  <key>CFBundleShortVersionString</key><string>2.0.0</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>launcher</string>
  <key>CFBundleIconFile</key><string>Heng</string>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>LSApplicationCategoryType</key>
  <string>public.app-category.developer-tools</string>
</dict>
</plist>
"""

# 启动器：双击时开一个新终端窗口跑 REPL。
# 直接跑二进制的话，Finder 启动的进程没有终端，输出全丢，看起来像没反应。
LAUNCHER = """#!/bin/bash
# 双击行为：打开终端并启动衡语 IDE（有 IDE 脚本时）；没有则进 REPL。
DIR="$(cd "$(dirname "$0")/../Resources" && pwd)"
BIN="$DIR/henger"
IDE="$DIR/ide.hng"
if [ -f "$IDE" ]; then
  CMD="'$BIN' '$IDE'"
else
  CMD="'$BIN' --repl"
fi
/usr/bin/osascript <<APPLESCRIPT
tell application "Terminal"
  activate
  do script "$CMD"
end tell
APPLESCRIPT
"""


def main() -> int:
    install = "--install" in sys.argv
    src = DIST / "henger"
    if not src.exists():
        print("找不到 dist/henger，请先运行：python3 tools/build_macos.py")
        return 1

    # 图标
    icns = ROOT / "build" / "icon" / "Heng.icns"
    if not icns.exists():
        print("生成图标…")
        subprocess.run([sys.executable, str(ROOT / "tools" / "make_icon.py")],
                       cwd=ROOT, check=False)

    app = DIST / f"{APP_NAME}.app"
    if app.exists():
        shutil.rmtree(app)
    macos = app / "Contents" / "MacOS"
    res = app / "Contents" / "Resources"
    macos.mkdir(parents=True)
    res.mkdir(parents=True)

    (app / "Contents" / "Info.plist").write_text(
        INFO_PLIST.format(name=APP_NAME, bid=BUNDLE_ID), encoding="utf-8")
    # PkgInfo 是 app bundle 的标配（8 字节）；缺了它某些系统组件认不出是应用
    (app / "Contents" / "PkgInfo").write_bytes(b"APPL????")

    # 启动器必须是真正的 Mach-O 二进制：
    # LaunchServices 不认 shell 脚本作为 CFBundleExecutable，
    # 用脚本时 Finder 里看不到这个 .app——这正是之前「装了却看不见」的原因。
    launcher = macos / "launcher"
    csrc = ROOT / "tools" / "app_main.c"
    built = subprocess.run(
        ["clang", "-O2", "-o", str(launcher), str(csrc)],
        capture_output=True, text=True)
    if built.returncode != 0:
        print("启动器编译失败：", built.stderr[:400])
        return 1
    os.chmod(launcher, 0o755)
    kind = subprocess.run(["file", "-b", str(launcher)],
                          capture_output=True, text=True).stdout.strip()
    print(f"  启动器: {kind[:60]}")

    shutil.copy2(src, res / "henger")
    os.chmod(res / "henger", 0o755)
    if icns.exists():
        shutil.copy2(icns, res / "Heng.icns")

    # 图形 IDE：脚本 + 原生桥接库，一起放进 Resources
    # 整个 ide 目录的 .hng 都要打进去：ide.hng 会 导入 highlight.hng / files.hng，
    # 只拷主文件会在启动时报「找不到模块」——这是「双击没反应」的真实原因。
    # IDE 源码在仓库内（heng-ide/），不是父目录——
    # 早期放在仓库外，导致 .app 里打不到 ide.hng，双击直接报「文件不存在」。
    ide_dir = ROOT / "heng-ide"
    if not ide_dir.is_dir():
        ide_dir = ROOT.parent / "heng-ide"
    gui_lib = ROOT / "native" / "bin" / "libhengui.dylib"
    if ide_dir.is_dir():
        packed = []
        # 跳过测试文件
        for f in sorted(ide_dir.glob("*.hng")):
            if f.name.startswith(("t_", "test_")):
                continue
            shutil.copy2(f, res / f.name)
            packed.append(f.name)
        print(f"  已打包 IDE 脚本 {len(packed)} 个：{', '.join(sorted(packed))}")
    if gui_lib.exists():
        shutil.copy2(gui_lib, res / "libhengui.dylib")
        print(f"  已打包图形桥接库：{gui_lib}")

    print(f"✓ 已生成 {app}")

    # 自检：直接用包内二进制跑一段代码
    probe = Path(tempfile.mkdtemp()) / "p.hng"
    probe.write_text("开始:\n  说出 6 * 7\n结束\n", encoding="utf-8")
    r = subprocess.run([str(res / "henger"), str(probe)], capture_output=True, text=True)
    print(f"  自检输出: {r.stdout.strip() or r.stderr[:120].strip()}")
    if r.stdout.strip() != "42":
        print("  自检失败")
        return 1
    print("  ✓ 自检通过")

    # 刷新图标缓存，否则访达可能显示旧图标
    subprocess.run(["touch", str(app)], capture_output=True)

    if install:
        target = Path("/Applications") / f"{APP_NAME}.app"
        try:
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(app, target, symlinks=True)
            subprocess.run(["xattr", "-cr", str(target)], capture_output=True)
            # ad-hoc 签名：未签名的 bundle 在某些系统上 Finder 显示异常
            sg = subprocess.run(["codesign", "--force", "--deep", "--sign", "-",
                                 str(target)], capture_output=True, text=True)
            print("  签名：" + ("成功" if sg.returncode == 0 else sg.stderr[:120]))
            # 强制登记到 LaunchServices，否则 Finder 不索引
            lr = ("/System/Library/Frameworks/CoreServices.framework/Frameworks/"
                  "LaunchServices.framework/Support/lsregister")
            subprocess.run([lr, "-f", "-R", "-trusted", str(target)],
                           capture_output=True)
            print("  已登记到 LaunchServices")
            print(f"✓ 已安装到 {target}")
            print("  现在打开「访达 → 应用程序」，就能看到「衡语」了。")
        except PermissionError:
            print(f"装到 /Applications 需要权限，请运行：")
            print(f"  sudo cp -R '{app}' /Applications/")
        except OSError as e:
            print(f"安装失败：{e}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
