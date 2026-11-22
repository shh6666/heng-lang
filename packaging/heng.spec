# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec：把衡语解释器 + .hng 程序打包为单文件可执行程序。

用法（在目标 OS 本机执行，PyInstaller 非交叉编译器）：
  pyinstaller packaging/heng.spec --distpath dist --workpath build --clean

Linux 上产出 dist/heng（ELF 可执行文件）；macOS 上产出 dist/heng（Mach-O）。
Windows 上产出 dist/heng.exe。

设计要点：
  - 衡语全部依赖为 Python 标准库，无需收集第三方 hiddenimports；
  - 把 examples/ 作为数据文件打进包内，用户可运行内置示例；
  - console=True 保留中文输出（无控制台的窗口模式在部分终端会丢失 stdout）。
"""

from PyInstaller.utils.hooks import collect_data_files

datas = []
import os
_root = os.path.dirname(os.path.dirname(os.path.abspath(SPEC)))

# 关键：把整个 heng 包作为数据文件打入，打包后 heng_entry 从 _MEIPASS 绝对导入源码
_heng_pkg = os.path.join(_root, "heng")
for f in os.listdir(_heng_pkg):
    if f.endswith(".py"):
        datas.append((os.path.join(_heng_pkg, f), "heng"))

# 打包示例程序与文档
for f in os.listdir(os.path.join(_root, "examples")):
    if f.endswith(".hng"):
        datas.append((os.path.join(_root, "examples", f), "examples"))
datas.append((os.path.join(_root, "docs", "EBNF.md"), "docs"))

block_cipher = None

a = Analysis(
    [os.path.join(_root, "packaging", "heng_entry.py")],
    pathex=[_root],
    binaries=[],
    datas=datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "numpy", "PyQt5", "PySide2"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="heng",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
