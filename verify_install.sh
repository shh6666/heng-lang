#!/bin/bash
# 验证衡语 IDE 是否真的装好了。你可以自己跑这个脚本。
#
# 用法：bash verify_install.sh

echo "═══ 1. /Applications 里有没有 ═══"
if [ -d "/Applications/衡语.app" ]; then
  echo "  ✓ /Applications/衡语.app 存在"
  du -sh "/Applications/衡语.app"
else
  echo "  ✗ 不存在"
fi

echo
echo "═══ 2. 包结构 ═══"
find "/Applications/衡语.app" -type f 2>/dev/null | while read f; do
  printf "  %-58s %s\n" "${f#/Applications/衡语.app/}" "$(du -h "$f"|cut -f1)"
done

echo
echo "═══ 3. 启动器是不是原生二进制 ═══"
file "/Applications/衡语.app/Contents/MacOS/launcher" 2>/dev/null | sed 's/^/  /'

echo
echo "═══ 4. Info.plist 合法吗 ═══"
plutil -lint "/Applications/衡语.app/Contents/Info.plist" 2>&1 | sed 's/^/  /'

echo
echo "═══ 5. 系统是否认得它（LaunchServices）═══"
LR=/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister
"$LR" -dump 2>/dev/null | grep -A3 "path:.*Applications/衡语.app" | head -5 | sed 's/^/  /'

echo
echo "═══ 6. 图标 ═══"
ls -lh "/Applications/衡语.app/Contents/Resources/Heng.icns" 2>/dev/null | sed 's/^/  /'

echo
echo "═══ 7. 编译器能跑吗 ═══"
printf '开始:\n  说出 6 * 7\n结束\n' > /tmp/_verify.hng
OUT=$("/Applications/衡语.app/Contents/Resources/henger" /tmp/_verify.hng 2>&1)
if [ "$OUT" = "42" ]; then
  echo "  ✓ 输出 42，编译器正常"
else
  echo "  ✗ 输出异常：$OUT"
fi
rm -f /tmp/_verify.hng

echo
echo "═══ 8. 实际启动一次 ═══"
open "/Applications/衡语.app"
echo "  已发出启动指令（会打开一个终端窗口并启动 IDE）"
echo
echo "如果访达里看不到「衡语」，跑这一句强制刷新："
echo "  $LR -f -R -trusted /Applications/衡语.app"
echo "然后按 Cmd+Shift+G 输入 /Applications 查看。"
