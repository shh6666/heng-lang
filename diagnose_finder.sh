#!/bin/bash
# 诊断：为什么访达里看不到「衡语 IDE」
#
# 用法：bash diagnose_finder.sh
# 这个脚本不修改任何东西，只报告状态。

APP="/Applications/衡语 IDE.app"
LR=/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister

echo "════════ 1. 文件在不在 ════════"
if [ -e "$APP" ]; then
  echo "✓ 存在"
  ls -ld@ "$APP"
else
  echo "✗ 不存在！请先运行 python3 tools/build_app.py --install"
  exit 1
fi

echo
echo "════════ 2. 是不是合法 app bundle ════════"
for f in Contents/Info.plist Contents/MacOS/launcher Contents/PkgInfo; do
  if [ -e "$APP/$f" ]; then
    echo "✓ $f"
  else
    echo "✗ 缺 $f   ← 这会导致访达不认为是应用"
  fi
done

echo
echo "════════ 3. 启动器类型（必须是 Mach-O）════════"
file "$APP/Contents/MacOS/launcher" | sed 's/^/  /'

echo
echo "════════ 4. 签名 ════════"
codesign -v "$APP" 2>&1 && echo "  ✓ 签名有效" || echo "  ✗ 签名无效（访达可能不显示）"
codesign -dv "$APP" 2>&1 | grep -E "Identifier|Signature" | sed 's/^/  /'

echo
echo "════════ 5. Spotlight 能否识别类型 ════════"
mdimport -t -d2 "$APP" 2>&1 | grep -E "kMDItemContentType |kMDItemKind =|kMDItemCFBundleIdentifier" | head -4 | sed 's/^/  /'

echo
echo "════════ 6. LaunchServices 登记的唯一性 ════════"
echo "  bundle id 出现次数（正常应该是几条同一路径的记录）："
"$LR" -dump 2>/dev/null | grep -c "cn.henglang.ide" | sed 's/^/    /'
echo "  登记到的路径："
"$LR" -dump 2>/dev/null | grep "path:.*衡语" | sed 's/^/    /'

echo
echo "════════ 7. 扩展属性（不应有 quarantine）════════"
xattr "$APP" 2>/dev/null | sed 's/^/  /' || echo "  （无）"

echo
echo "════════ 8. 编译功能自检 ════════"
printf '开始:\n  说出 6 * 7\n结束\n' > /tmp/_d.hng
OUT=$("$APP/Contents/Resources/henger" /tmp/_d.hng 2>&1)
[ "$OUT" = "42" ] && echo "  ✓ 输出 42" || echo "  ✗ 输出：$OUT"
rm -f /tmp/_d.hng

echo
echo "════════ 结论与建议 ════════"
echo "如果 1-8 全绿但访达仍看不到，请按顺序试："
echo
echo "  A. 刷新访达窗口：在 /Applications 里按 Cmd+R"
echo
echo "  B. 强制重建 LaunchServices 登记："
echo "     $LR -f -R -trusted \"$APP\""
echo "     killall Finder"
echo
echo "  C. 直接在访达里定位（绕过图标视图）："
echo "     按 Cmd+Shift+G → 输入 /Applications/衡语 IDE.app → 回车"
echo
echo "  D. 用终端打开（不依赖访达）："
echo "     open \"$APP\""
echo
echo "  E. 如果还是不行，说明 Spotlight 索引坏了，重建索引："
echo "     sudo mdutil -E /"
