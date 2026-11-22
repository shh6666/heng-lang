"""衡语内核 v2 —— 词法单元定义。

与旧实现的区别：旧实现的 Token 只有 kind/value/line/col 三元组，
在解析与代码生成阶段要反复按字符串比较；新内核额外带 `slot` 预留字段，
并让 token 种类用整数枚举（便于代码生成阶段的快速分派）。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum


class K(IntEnum):
    """Token 种类。用整数而非字符串，编译期分派更快。"""
    EOF = 0
    NEWLINE = 1
    NUMBER = 2
    STRING = 3
    IDENT = 4

    # 标点与运算符
    COLON = 10
    COMMA = 11
    DOT = 12
    LPAREN = 13
    RPAREN = 14
    LBRACK = 15
    RBRACK = 16
    LBRACE = 17
    RBRACE = 18
    PLUS = 20
    MINUS = 21
    FATARROW = 35      # => 匿名函数体
    PERCENT = 34      # 与 EQ(=, 24) 区分，避免 compare 层误判
    STAR = 22
    SLASH = 23
    EQ = 24          # =  赋值
    EQEQ = 31        # == 相等比较
    NE = 25
    LT = 26
    LE = 27
    GT = 28
    GE = 29
    ARROW = 30

    # 关键字（从 100 起，与符号区隔开）
    KW_START = 100
    KW_END = 101
    KW_IF = 102
    KW_ELSE = 103
    KW_LOOP = 104
    KW_REPEAT = 105
    KW_EACH = 106
    KW_FOR = 106
    KW_AS = 107
    KW_SAY = 108
    KW_DEF = 109
    KW_RETURN = 110
    KW_TRY = 111
    KW_CATCH = 112
    KW_FINALLY = 113
    KW_RAISE = 114
    KW_ASSERT = 115
    KW_LET = 116
    KW_CONST = 117
    KW_RECORD = 118
    KW_FIELD = 119
    KW_MAY_RAISE = 120
    KW_ERROR = 121
    KW_SANDBOX = 122
    KW_AUTHORIZE = 123
    KW_EXEC = 124
    KW_OBSERVE = 125
    KW_PARALLEL = 126
    KW_SPAWN = 127
    KW_RECEIVE = 128
    KW_CONTRACT = 129
    KW_REQUIRE = 130
    KW_ENSURE = 131
    KW_AND = 132
    KW_OR = 133
    KW_NOT = 134
    KW_TRUE = 135
    KW_FALSE = 136
    KW_NIL = 137
    KW_AWAIT = 141
    KW_IMPORT = 138
    KW_FOREIGN = 139
    KW_MODULE = 140
    # 类型
    KW_NUM = 150
    KW_TEXT = 151
    KW_BOOL = 152
    KW_SEQ = 153
    KW_MAP = 154


# 关键字表：规范词 → token 种类
KEYWORDS: dict[str, K] = {
    # 结构与流程
    "开始": K.KW_START,
    "结束": K.KW_END,
    "如果": K.KW_IF,
    "否则": K.KW_ELSE,
    "循环": K.KW_LOOP,
    "重复": K.KW_REPEAT,
    "每个": K.KW_EACH,
    "为": K.KW_AS,
    "说出": K.KW_SAY,
    "做法": K.KW_DEF,
    "返回": K.KW_RETURN,
    "尝试": K.KW_TRY,
    "接住": K.KW_CATCH,
    "最后": K.KW_FINALLY,
    "报错": K.KW_RAISE,
    "必须": K.KW_ASSERT,
    # 声明
    "变量": K.KW_LET,
    "常量": K.KW_CONST,
    "定义": K.KW_DEF,
    "记录": K.KW_RECORD,
    "字段": K.KW_FIELD,
    "可能生": K.KW_MAY_RAISE,
    "错误": K.KW_ERROR,
    # 安全与并发
    "沙箱": K.KW_SANDBOX,
    "授权": K.KW_AUTHORIZE,
    "执行": K.KW_EXEC,
    "观察": K.KW_OBSERVE,
    "同时": K.KW_PARALLEL,
    "发起": K.KW_SPAWN,
    "接收": K.KW_RECEIVE,
    "等待完成": K.KW_AWAIT,
    "约定": K.KW_CONTRACT,
    "要求": K.KW_REQUIRE,
    "保证": K.KW_ENSURE,
    # 运算符词
    "并且": K.KW_AND,
    "或者": K.KW_OR,
    "非": K.KW_NOT,
    "真": K.KW_TRUE,
    "假": K.KW_FALSE,
    "空": K.KW_NIL,
    # 导入
    "导入": K.KW_IMPORT,
    "外部": K.KW_FOREIGN,
    "模块": K.KW_MODULE,
    # 类型
    "数": K.KW_NUM,
    "文本": K.KW_TEXT,
    "真假": K.KW_BOOL,
    "序列": K.KW_SEQ,
    "映射": K.KW_MAP,
    # 赋值引导（保留旧写法支持）
    "将": K.KW_LET,
    "放到": K.KW_AS,
}

# 旧词 → 规范词（词法层归一化；新旧完全等价，规范只有一套）
ALIASES: dict[str, str] = {
    "入口": "开始",
    "倘若": "如果",
    "完毕": "结束",
    "遍历": "每个",
    "赋于": "放到",
    "并行": "同时",
    "启": "发起",
    "收": "接收",
    "捕获": "接住",
    "抛出": "报错",
    "终结": "最后",
    "规约": "约定",
    "前置": "要求",
    "后置": "保证",
    "断言": "必须",
    "输出": "说出",
    "函数": "做法",
}

# 「放到」既作遍历绑定词也作赋值词，需按上下文区分；这里统一映射到 KW_AS，
# 由解析器依据所处语句判断语义。
# 「定义」与「做法」共用 KW_DEF，解析器看到 KW_DEF 后看下一个 token 决定。


@dataclass(slots=True)
class Token:
    kind: K
    value: str
    line: int
    col: int
    # 预留：代码生成阶段可写入解析结果（如关键字别名来源），避免再建表
    slot: int = -1

    def __repr__(self) -> str:
        return f"{self.kind.name}({self.value!r})@{self.line}:{self.col}"
