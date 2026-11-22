#!/bin/sh
# 安装衡语编译器（macOS）。
#
# 用法：
#   ./install.sh                安装到 /usr/local/bin
#   PREFIX=~/.local ./install.sh  安装到自定义前缀
#
# 做的事：
#   1. 检查 dist/henger 是否存在（先跑 tools/build_macos.py）
#   2. 复制到 $PREFIX/bin/henger
#   3. 去掉 macOS 的隔离属性（否则首次运行会被 Gatekeeper 拦）
#   4. 打印版本，验证安装

set -e

HERE=$(cd "$(dirname "$0")" && pwd)
SRC="$HERE/dist/henger"
PREFIX="${PREFIX:-/usr/local}"
BIN="$PREFIX/bin"

if [ ! -f "$SRC" ]; then
  echo "找不到 $SRC"
  echo "请先运行：python3 tools/build_macos.py"
  exit 1
fi

mkdir -p "$BIN" 2>/dev/null || {
  echo "无法创建 $BIN，可能需要 sudo："
  echo "  sudo PREFIX=$PREFIX ./install.sh"
  exit 1
}

cp "$SRC" "$BIN/henger"
chmod +x "$BIN/henger"
# 本地构建的产物带 quarantine 属性时会被系统拦
xattr -d com.apple.quarantine "$BIN/henger" 2>/dev/null || true

echo "已安装到 $BIN/henger"
"$BIN/henger" -V

case ":$PATH:" in
  *":$BIN:"*) ;;
  *) echo ""
     echo "提示：$BIN 不在 PATH 里，请加一行到 ~/.zshrc："
     echo "  export PATH=\"$BIN:\$PATH\"" ;;
esac
