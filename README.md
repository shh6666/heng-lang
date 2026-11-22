# 衡语（Heng）

**一门用中文书写、带量纲检查、能原生画界面的编程语言。**

自研字节码 + 原生 C 虚拟机。附带图形 IDE、TUI 工具、AI 连接器。

```hl
定义 做法 单线成本(消耗 : 数 元, 线索 : 数 条) -> 数 元/条:
  如果 线索 <= 0:
    返回 0
  结束
  返回 消耗 / 线索
结束

开始:
  变量 成本 : 数 元/条 = 单线成本(97000<元>, 3650<条>)
  说出 成本
结束
```

## 三个和别的中文编程语言不一样的地方

### 1. 量纲进类型系统

单位不是注释，是类型的一部分。单位约不掉就编译不过：

```hl
变量 体重 : 数 公斤 = 60<公斤>
变量 单价 : 数 元/公斤 = 5<元/公斤>
变量 总价 : 数 元 = 体重 * 单价     注：✓ 公斤约掉了

变量 错 : 数 元 = 体重 + 单价       注：✗ 数 公斤 与 数 元/公斤 不能相加
```

### 2. 能力沙箱

读写文件、网络、执行命令，都要先授权。没授权直接编译报错：

```hl
开始:
  授权 文件.读取, 文件.写入
  文件.写入("out.txt", "内容")
结束
```

### 3. 图形界面用同一门语言写

不绑 Qt，不调浏览器。衡语通过薄 FFI 桥接 macOS AppKit，界面逻辑全在 `.hng` 里：

```hl
导入 外部 "libhengui.dylib" 之 界面

开始:
  界面.界面_启动_t("我的应用")
  界面.界面_加分区_t("主区,0,0,100,100")
  界面.界面_文本_t("主区|你好，图形界面")
  界面.界面_按钮_t("主区,点我,ACTION")
  循环 真:
    如果 界面.界面_等待_t("50") = "1":
      说出 界面.界面_取结果("事件")
    结束
  结束
结束
```

## 快速开始

### 安装（macOS）

```sh
git clone https://github.com/shh6666/heng-lang.git
cd heng-lang
python3 tools/build_macos.py      # 编译原生 VM 并打包
PREFIX=$HOME/.local ./install.sh  # 装到 ~/.local/bin/henger
```

装完直接：

```sh
henger examples/hello.hng           # 运行
henger --check examples/hello.hng   # 只检查
henger --debug examples/hello.hng   # 单步调试
henger --repl                       # 交互式
```

不想装也可以从源码跑：

```sh
python3 -m heng2 examples/hello.hng
```

### 图形 IDE

```sh
python3 tools/build_app.py --install
```

装到 `/Applications/衡语 IDE.app`，双击打开。七块布局（参考 Eclipse / NetBeans / Code::Blocks / CodeLite / VSCodium）：菜单栏、工具栏、文件树、编辑区（语法高亮）、输出与问题标签、大纲与诊断、状态栏。

## 语法速览

| 想做的事 | 写法 |
|---|---|
| 变量 | `变量 x : 数 = 1` |
| 赋值 | `x = 2` ／ `将 2 放到 映射["键"]` |
| 条件 | `如果 条件:` … `否则:` … `结束` |
| 循环 | `循环 i < n:` … `i = i + 1` … `结束` |
| 遍历 | `每个 列表 为 x:` … `结束` |
| 函数 | `定义 做法 名(a : 数) -> 数:` … `返回 v` … `结束` |
| 记录 | `定义 记录 名:` `字段 a : 数` `结束` |
| lambda | `做法(x : 数) -> 数 => x * 2` |
| 输出 | `说出 值` |
| 模块 | `导入 「工具.hng」之 工具` |
| 取反 | `非 条件` |

## 标准库

| 模块 | 函数 |
|---|---|
| `表` | 长度 追加 排序 反转 含 求和 平均 最大 最小 映射 过滤 折叠 去重 连接 片段 |
| `文字` | 长度 切分 合并 分行 替换 去空白 含 截 从 小写 大写 开头是 结尾是 由码点 字符码 |
| `字典` | 键 有键 大小 取值 删键 |
| `数值` | 取整 取余 整除 乘方 开方 |
| `JSON` | 编码 解码 有效 |
| `时间` | 现在 毫秒 格式化 |

完整清单：`henger --list-builtins`

> `文字.截(s, n)` 取前 n 个字符；`文字.从(s, n)` 从第 n 个开始。
> 解析 `"按钮|RUN"` 这类带前缀的字符串必须用 `文字.从`——用错不会报错，只会让条件永远不成立。

## 能力（需授权）

`文件.读取` `文件.写入` `文件.存在` `网络.取` `网络.发` `系统.参数` `系统.脚本目录` `系统.可执行文件` `系统.跑命令` `终端.读键` `服务.启动` `服务.待请求` `服务.回复`

## 工具链

| 命令 | 作用 |
|---|---|
| `henger 文件.hng` | 编译并运行 |
| `henger --check 文件.hng` | 只检查语法与类型 |
| `henger --check 文件.hng --json` | 结构化诊断（给 AI / 编辑器用） |
| `henger --build 文件.hng -o a.hbc` | 编译为字节码 |
| `henger --run a.hbc` | 运行字节码 |
| `henger --debug 文件.hng [断点]` | 指令级单步调试 |
| `henger --repl` | 交互式解释器 |
| `henger --list-builtins` | 列全部标准库与能力 |
| `henger --explain E01` | 解释错误码 |
| `henger --disasm 文件.hng` | 反汇编 |

### 给 AI 用的结构化诊断

```sh
$ henger --check bad.hng --json
{
  "ok": false,
  "diagnostics": [{
    "severity": "error",
    "code": "E05",
    "message": "未定义变量 x",
    "line": 3, "column": 6,
    "help": "检查变量名是否拼错；局部变量需要先「变量 名字 : 类型 = 值」声明"
  }]
}
```

仓库带 MCP 服务端（`heng2/mcp_server.py`），把编译器接成 AI 工具：
`heng_check` / `heng_run` / `heng_build` / `heng_list` / `heng_explain` / `heng_examples`。

## 性能

原生 C 虚拟机，真实测量：

| 实现 | fib(25) |
|---|---|
| 原生 C VM（当前） | 11.9 ms |
| v1 参考实现（Python AST 求值） | 1180 ms |

## 项目结构

```
heng2/          编译器（词法 → 语法 → 类型检查 → 字节码）
native/         原生 C 虚拟机 + AppKit 图形桥接
docs/           教程、EBNF、架构文档
examples/       入门示例
validation/     300 个验证项目
tests/          单元测试
heng-ide/       图形 IDE（用衡语写的）
heng-os/        AI 自进化手机 OS 原型（用衡语写的）
heng-tools/     招生数据台等实用工具
skill/          AI Skill 描述文件
```

## 验证与测试

```sh
python3 tools/check_all.py              # 300 个验证项目
python3 -m unittest tests.test_heng2    # 内核测试
python3 -m unittest tests.test_heng     # 旧测试
```

## 文档

- [入门教程（31 章）](docs/TUTORIAL.md)
- [EBNF 语法](docs/EBNF.md)
- [核心架构](docs/CORE_ARCHITECTURE.md)
- [并发模型](docs/CONCURRENCY.md)

## 已知限制

诚实列出，不是没做而是受语言或平台约束：

1. **标识符不能以关键字开头** —— `结束进程` 会报语法错误，改 `销毁进程`
2. **顶层不能声明变量** —— 要全局状态用 `常量 状态 : 映射 文本 文本 = {}`
3. **表达式跨行只能在括号内**
4. **链式调用后不能接字段或下标** —— `a.b().c` 需拆成两步
5. **模块只能从主文件所在目录导入** —— 不支持子目录
6. **图形界面目前只支持 macOS** —— AppKit 桥接

## 许可

MIT
