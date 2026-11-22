# 衡语 Linux 端打包

## 结论

Linux 是衡语**最容易落地**的目标端：

1. 衡语解释器是纯 Python 标准库实现（只用 dataclasses/json/struct/sys/threading/typing），
   没有任何第三方依赖，PyInstaller 打包时无需处理 hiddenimports。
2. Linux 是 Python 的原生环境，无需 Android SDK、Xcode 或付费开发者账号。
3. 唯一的硬性规则来自 PyInstaller 官方：**它不是交叉编译器**——
   Linux 可执行文件必须在 Linux 上构建（不能在 macOS/Windows 上产出）。

## 一键打包

```bash
# 在 Linux 本机（Ubuntu/Debian 等）
bash packaging/build_linux.sh
```

脚本会：检查 Python → 安装 PyInstaller → 跑全部 28 项测试 → 打包 → 用示例验证。

产出 `dist/heng`（ELF 单文件可执行程序），可直接运行：

```bash
./dist/heng examples/order.hng
# 33.9元
```

实测说明：本配置已在 macOS 上用 PyInstaller 6.22.3 完整跑通（产出 8.8MB 单文件
Mach-O，可运行 hello/order/compute 等示例并支持 `--wasm` 导出）。按 PyInstaller
「非交叉编译器」原则，Linux 产物需在 Linux 上用同一份 spec 重新构建。

打包入口说明：spec 使用 `packaging/heng_entry.py` 而非 `heng/__main__.py`——
后者使用相对导入，被 PyInstaller 当顶层脚本打包时会报
`attempted relative import with no known parent package`。入口脚本在 frozen 环境下
从 `_MEIPASS` 以绝对导入加载 heng 包，spec 负责把整个 heng 包作为数据打入。

## 分发格式

| 格式 | 命令/工具 | 说明 |
|---|---|---|
| 单文件 ELF | PyInstaller（本脚本默认） | 最通用，无依赖，拷贝即用 |
| .deb / .rpm / .AppImage | Tauri 内置打包器 | 需 Rust 工具链 + webkit2gtk 4.1（Ubuntu 22.04+）；适合带 GUI 的 WASM 应用 |
| 命令行工具发布 | 直接分发 dist/heng | 无签名要求，无账号门槛 |

Tauri 官方 GitHub 仓库明确列出其内置打包器可产出 `.deb`、`.rpm`、`.AppImage`；
Linux 支持要求 webkit2gtk 4.0（Tauri v1）或 4.1（Tauri v2）。

## 与五端路径的关系

- 只做命令行程序：PyInstaller 路径最短，本目录脚本即可。
- 做 GUI 程序（衡语 → WASM → WebView）：用 Tauri，一套代码库同时覆盖
  Linux + Windows + macOS + Android + iOS，各端分别构建。
- 鸿蒙 NEXT：不适用以上任何路径，需按五端调研报告用 ArkTS 重写。

## 来源

- PyInstaller Manual（官方，非交叉编译器原文）：https://pyinstaller.org/en/stable/
- Tauri GitHub README（官方，Linux 分发格式与 webkit2gtk 要求）：https://github.com/tauri-apps/tauri
- Tauri Prerequisites（官方）：https://tauri.app/start/prerequisites
