#!/bin/bash
# 衡语工具的启动器：在终端里打开 REPL。
# 双击 .app 时由 macOS 执行本脚本。
# 注意：Finder 启动的进程没有终端，必须先告诉 Terminal 打开一个新窗口。

APP_BIN="$1"

osascript <<APPLESCRIPT
tell application "Terminal"
  activate
  do script "'$APP_BIN' --repl"
end tell
APPLESCRIPT
