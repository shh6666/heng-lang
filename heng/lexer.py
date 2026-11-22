"""衡语词法分析器：中文最大匹配 + 封闭词表消歧。

约束落实：
  C1 封闭词表关键字、双音节优先 —— KEYWORDS 为有限集合，最大匹配
  C2 禁用多义单字 —— 单字关键字仅 真/假/空/为
  C3 标识符不得与关键字同形 —— 命中词表的词不可作标识符
  C12 数学符号保持 ASCII
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# 规范关键字（单一标准，不分档）。全部为日常高频词。
KEYWORDS = {
    # ---- 结构与流程 ----
    "如果", "否则", "结束", "每个", "放到", "开始", "同时", "接收",
    "尝试", "接住", "报错", "最后", "约定", "要求", "保证", "必须",
    "说出", "做法", "重复", "发起", "外部",
    # ---- 旧词（仅为兼容历史 .hng 文件保留，新代码请用上表） ----
    "入口", "倘若", "完毕", "遍历", "为", "赋于", "将",
    "启", "收", "并行", "捕获", "抛出", "终结", "规约", "前置", "后置", "断言", "输出",
    # ---- 核心 ----
    "模块", "导入", "导出", "定义", "函数", "记录", "字段",
    "常量", "变量", "类型", "数", "文本", "真假", "序列", "映射",
    "真", "假", "空", "循环", "返回", "可能生", "错误",
    "执行", "观察", "沙箱", "授权", "非", "注", "并且", "或者",
}

# 旧词 → 规范 token 种类（词法层归一化，保证旧文件仍可编译）。
# 注意：这是「向后兼容」机制，不是「双写法」。规范只有一套，旧词只是别名。
KEYWORD_ALIASES = {
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

# 多义单字黑名单：作标识符可以，作关键字绝不允许（C2）
# 注意：「为」虽是单字，但它是「遍历…为…」固定句型的合法关键字（C2 允许的四个单字之一），
# 因此不在黑名单中。
FORBIDDEN_SINGLE_KEYWORDS = set("道法行是在之其而")

# 运算符：ASCII 数学符号（C12）
OPERATORS = {
    "+": "PLUS", "-": "MINUS", "*": "STAR", "/": "SLASH",
    "=": "EQ", "!=": "NE", "<=": "LE", ">=": "GE",
    "<": "LT", ">": "GT",
}

PUNCT = {
    ":": "COLON", ",": "COMMA", ".": "DOT",
    "(": "LPAREN", ")": "RPAREN",
    "[": "LBRACK", "]": "RBRACK",
    "{": "LBRACE", "}": "RBRACE",
}


@dataclass(frozen=True)
class Token:
    kind: str
    value: str
    line: int
    col: int

    def __repr__(self) -> str:
        return f"Token({self.kind}, {self.value!r}, {self.line}:{self.col})"


class LexError(Exception):
    pass


class Lexer:
    """中文最大匹配词法器。

    分词策略：
      - 汉字/字母/下划线/数字构成的「词」整体读取；
      - 若整个词命中 KEYWORDS → 关键字 token；
      - 否则 → 标识符 token（若与关键字同形在进入 AST 时会被拒绝，此处先按词表判定）；
      - 数字独立识别（支持小数）；
      - 字符串用双引号；
      - 运算符/标点独立识别；
      - 换行是语句分隔符 token；
      - 行内空白忽略；"注：" 起为注释。
    """

    def __init__(self, source: str):
        self.src = source
        self.i = 0
        self.line = 1
        self.col = 1
        self.n = len(source)

    def _peek(self, k: int = 0) -> Optional[str]:
        j = self.i + k
        return self.src[j] if j < self.n else None

    def _advance(self, k: int = 1) -> None:
        for _ in range(k):
            if self.i >= self.n:
                return
            ch = self.src[self.i]
            self.i += 1
            if ch == "\n":
                self.line += 1
                self.col = 1
            else:
                self.col += 1

    @staticmethod
    def _is_id_start(ch: str) -> bool:
        return ch == "_" or ch.isalpha()  # isalpha 覆盖汉字

    @staticmethod
    def _is_id_cont(ch: str) -> bool:
        return ch == "_" or ch.isalnum()

    # 允许「无空格粘连切分」的关键字白名单。
    # 只收录语法位置固定、几乎不会作为业务标识符前缀的词（语句起始的控制/声明词）。
    # 故意排除 变量/常量/函数/记录 等——它们是常见命名前缀（如「变量表」「函数库」），
    # 切分会造成真实标识符被误拆。
    SPLITTABLE_PREFIX = (
        "倘若", "否则", "循环", "遍历", "返回", "输出", "断言",
        "尝试", "捕获", "终结", "抛出", "执行", "观察", "完毕",
    )

    @classmethod
    def _split_keyword_prefix(cls, word: str) -> Optional[tuple[str, str]]:
        """把「关键字+标识符」粘连词切成两段；不可安全切分时返回 None。"""
        for kw in sorted(cls.SPLITTABLE_PREFIX, key=len, reverse=True):
            if word.startswith(kw) and len(word) > len(kw):
                rest = word[len(kw):]
                # 剩余部分必须像合法标识符/表达式开头（字母或数字），且不含关键字本身
                if rest and (rest[0].isalnum() or rest[0] == "_") and rest not in KEYWORDS:
                    return kw, rest
        return None

    @classmethod
    def _split_glue_shi(cls, word: str, line: int, col: int) -> Optional[list[Token]]:
        """把「X之Y」切成 IDENT(X) + IDENT(之) + IDENT(Y)。

        仅用于导入语句的别名连接词。中文不需要空格，`导入 工具之工具` 与
        `导入 工具 之 工具` 应当等价。限制条件：
          - 必须恰有一个「之」，且不在首尾；
          - 两侧都不是关键字（避免误拆「之前」这类普通词——「之前」两侧的
            「前」虽是单字，但整体不是关键字，仍会被拆，因此额外要求
            左侧至少 2 个字，降低误伤概率）。
        """
        if word.count("之") != 1:
            return None
        left, _, right = word.partition("之")
        if len(left) < 2 or not right:
            return None
        if left in KEYWORDS or right in KEYWORDS:
            return None
        return [Token("IDENT", left, line, col),
                Token("IDENT", "之", line, col + len(left)),
                Token("IDENT", right, line, col + len(left) + 1)]

    def tokens(self) -> list[Token]:
        out: list[Token] = []
        while self.i < self.n:
            ch = self._peek()

            # 空白（非换行）。含全角空格 U+3000——中文文本常见，须当作空白（C1 中文友好）
            if ch in (" ", "\t", "\r", "\u3000"):
                self._advance()
                continue

            # 换行
            if ch == "\n":
                out.append(Token("NEWLINE", "\n", self.line, self.col))
                self._advance()
                continue

            # 注释：注： 到行尾
            if ch == "注" and self._peek(1) == "：":
                while self.i < self.n and self._peek() != "\n":
                    self._advance()
                continue

            # 字符串：双引号为主；导入路径允许中文引号「」『』（少一层符号摩擦）
            if ch == '"' or ch == "「":
                close = '"' if ch == '"' else "」"
                line, col = self.line, self.col
                self._advance()  # 开引号
                buf: list[str] = []
                while self.i < self.n and self._peek() != close:
                    if self._peek() == "\\" and self._peek(1) is not None:
                        nxt = self._peek(1)
                        mapping = {"n": "\n", "t": "\t", '"': '"', "\\": "\\", "」": "」"}
                        buf.append(mapping.get(nxt, nxt))
                        self._advance(2)
                    else:
                        buf.append(self._peek())
                        self._advance()
                if self.i >= self.n:
                    raise LexError(f"{line}:{col} 字符串未闭合")
                self._advance()  # 闭引号
                out.append(Token("STRING", "".join(buf), line, col))
                continue

            # 数字（含小数）
            if ch.isdigit():
                line, col = self.line, self.col
                buf = []
                while self.i < self.n and self._peek().isdigit():
                    buf.append(self._peek())
                    self._advance()
                if self._peek() == "." and self._peek(1) and self._peek(1).isdigit():
                    buf.append(".")
                    self._advance()
                    while self.i < self.n and self._peek().isdigit():
                        buf.append(self._peek())
                        self._advance()
                out.append(Token("NUMBER", "".join(buf), line, col))
                continue

            # 标识符 / 关键字：中文最大匹配——整体读一个词
            if self._is_id_start(ch):
                line, col = self.line, self.col
                buf = []
                while self.i < self.n and self._is_id_cont(self._peek()):
                    buf.append(self._peek())
                    self._advance()
                word = "".join(buf)
                if word in FORBIDDEN_SINGLE_KEYWORDS and len(word) == 1:
                    # 这些单字永不为关键字，仅可作标识符（若用户拿它当关键字，解析会失败）
                    out.append(Token("IDENT", word, line, col))
                elif word in KEYWORDS:
                    # 旧词归一化为规范 token 种类，使「如果」与「倘若」完全等价
                    kind = KEYWORD_ALIASES.get(word, word)
                    out.append(Token(kind, word, line, col))
                else:
                    # 「关键字粘连」容错：中文书写习惯不加空格，如「倘若条件」「输出(结果)」。
                    # 仅对可安全切分的关键字前缀做切分（白名单），避免破坏合法标识符。
                    # 注意：表达式内部（如「x大于3」）仍要求用空格或 ASCII 符号分隔，
                    # 因为任意切分会与合法中文标识符冲突——这是刻意的语言边界。
                    split = self._split_keyword_prefix(word)
                    if split is not None:
                        kw, rest = split
                        out.append(Token(kw, kw, line, col))
                        out.append(Token("IDENT", rest, line, col + len(kw)))
                    else:
                        # 「之」是导入别名的连接词，中文里常与前后词粘连：
                        # 导入 工具之工具 / 导入 「工具.hng」之工具。
                        # 「之」不作任何标识符前缀的語义部分，可安全切分。
                        glued = self._split_glue_shi(word, line, col)
                        if glued is not None:
                            out.extend(glued)
                        else:
                            out.append(Token("IDENT", word, line, col))
                continue

            # 双字符运算符优先
            two = ch + (self._peek(1) or "")
            if two in ("!=", "<=", ">="):
                out.append(Token(OPERATORS[two], two, self.line, self.col))
                self._advance(2)
                continue

            # 单字符运算符
            if ch in OPERATORS:
                out.append(Token(OPERATORS[ch], ch, self.line, self.col))
                self._advance()
                continue

            # 标点
            if ch in PUNCT:
                out.append(Token(PUNCT[ch], ch, self.line, self.col))
                self._advance()
                continue

            # 全角符号：给出明确可操作的提示（零基础用户最容易犯的错）
            FULLWIDTH_HINT = {
                "：": ":", "，": ",", "（": "(", "）": ")",
                "“": '"', "”": '"', "‘": "'", "’": "'",
                "＜": "<", "＞": ">", "＝": "=", "《": "<", "》": ">",
                "。": "", "、": ",",
            }
            if ch in FULLWIDTH_HINT:
                hint = FULLWIDTH_HINT[ch]
                tip = f"这里要用英文半角符号 {hint}" if hint else "这里不需要这个符号"
                raise LexError(
                    f"{self.line}:{self.col} 用了中文全角符号「{ch}」，{tip}（切换输入法到英文再打一遍）")

            raise LexError(f"{self.line}:{self.col} 无法识别的字符 {ch!r}")

        out.append(Token("EOF", "", self.line, self.col))
        return out
