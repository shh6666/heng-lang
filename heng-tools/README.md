# 衡语工具集

三个中大型工具，全部用**已安装的 henger** 开发与运行。

## 安装

```sh
cd <heng-lang>
python3 tools/build_macos.py     # 生成 dist/henger（9.2 MB 单文件）
PREFIX=~/.local ./install.sh     # 安装到 ~/.local/bin/henger
```

## 1. 招生数据台（enroll-console）

读渠道 CSV + 目标 CSV，算单线成本、转化漏斗、目标达成，输出 Markdown 报表。

```sh
cd enroll-console
henger main.hng
```

输出：控制台报表 + `out/report.md`

关键指标：单线成本、报名成本、线索/到访/报名三级转化、各渠道目标达成率、成本最优渠道建议。

## 2. 脚本工坊（script-forge）

模板库 × 素材参数做笛卡尔组合，批量生成可直接拍摄的短视频脚本，带时长校验。

```sh
cd script-forge
henger main.hng            # 全部题材
henger main.hng 散打       # 按题材筛选
```

输出：`out/scripts.md` 或 `out/scripts-<题材>.md`

## 3. 库存台账（stock-ledger）

命令式库存管理，CSV 持久化，支持增删改查、低库预警、金额汇总、报导出。

```sh
cd stock-ledger
henger main.hng list                # 全部库存
henger main.hng low                 # 低于安全库存
henger main.hng in  M002 20         # 入库
henger main.hng out M002 3          # 出库（不足会拒绝）
henger main.hng add M006 头盔F 护具 10 320 5
henger main.hng value               # 分类金额
henger main.hng report              # 导出 data/report.md
```

## 这些工具验证了什么

| 能力 | 用在哪 |
|---|---|
| 记录与序列 | 三个工具的数据模型 |
| 文件读写（真实 IO） | CSV 加载、报表落盘 |
| 命令行参数 | 题材筛选、台账命令 |
| 脚本目录 | 相对路径定位数据文件 |
| 字符串处理 | CSV 解析、表格对齐、模板填充 |
| 值语义 | 台账修改时不污染原数据 |

## 开发中发现并修复的语言缺陷

写这三个工具的过程暴露了 4 个真实缺陷，都已修复：

1. **长表达式不能换行** — 只能硬写成一行，中大型项目无法维护
2. **没有命令行参数能力** — 工具没法接受输入
3. **文件 IO 是桩实现** — 调用成功但文件是空的
4. **`文字.合并` 用 512 字节缓冲中转** — 长文本静默截断，中文被切在半字节，写出的文件不是合法 UTF-8

第 4 条最隐蔽：ASan 查不出，因为不是越界而是逻辑截断。靠 `file` 命令发现编码非法才定位到。
