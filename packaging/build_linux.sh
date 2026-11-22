#!/usr/bin/env bash
# 衡语 Linux 端一键打包脚本
# 必须在 Linux 本机运行（PyInstaller 官方：非交叉编译器，Linux app 要在 Linux 上构建）
# 用法：bash packaging/build_linux.sh

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

echo "==> [1/4] 检查 Python"
python3 --version

echo "==> [2/4] 安装 PyInstaller（若未安装）"
python3 -c "import PyInstaller" 2>/dev/null || python3 -m pip install --user pyinstaller

echo "==> [3/4] 运行测试，确保打包的是可运行状态"
python3 -m unittest discover -s tests

echo "==> [4/4] 打包单文件可执行程序"
python3 -m PyInstaller packaging/heng.spec \
  --distpath dist --workpath build --noconfirm

OUT="$ROOT/dist/heng"
if [ -f "$OUT" ]; then
  chmod +x "$OUT"
  echo "==> 打包完成：$OUT"
  echo "==> 验证："
  "$OUT" examples/hello.hng
  echo "==> 生成 AppImage（可选，需 appimagetool）："
  echo "    curl -L -o appimagetool https://github.com/AppImage/AppImageKit/releases/download/continuous/appimagetool-x86_64.AppImage"
  echo "    chmod +x appimagetool"
  echo "    详见 packaging/README.md"
else
  echo "打包失败：未找到 $OUT" >&2
  exit 1
fi
