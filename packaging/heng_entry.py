"""PyInstaller 打包入口：以绝对导入方式启动衡语 CLI。

为什么需要这个文件：
  heng/__main__.py 使用相对导入（from .lexer import ...），
  当被 PyInstaller 作为顶层脚本打包时没有父包，相对导入失败。
  本文件作为顶层脚本，通过把项目根加入 sys.path 后用绝对导入规避。
"""

import os
import sys

# 打包后，项目根通过 spec 的 datas 打入 _MEIPASS；开发态用文件所在位置的上一级
if getattr(sys, "frozen", False):
    root = sys._MEIPASS
else:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if root not in sys.path:
    sys.path.insert(0, root)

from heng.__main__ import main  # 绝对导入

if __name__ == "__main__":
    main()
