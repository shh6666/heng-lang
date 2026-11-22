"""衡语内核 v2 —— 词法分析。

中文最大匹配 + 别名归一化 + 全角符号友好提示。
与旧实现的差异：产出整数枚举 token、支持「之」粘连切分、
全角提示表更完整（针对零基础用户的真实高频错误）。
"""

from __future__ import annotations

from typing import Optional

from .token import K, KEYWORDS, ALIASES, Token


class LexError(Exception):
    pass


# 空白字符（含全角空格 U+3000 —— 中文输入法常打出来）
_BLANK = frozenset(" \t\r\u3000\u00a0")

# 全角 → 半角 提示表
_FULLWIDTH = {
    "：": ":", "，": ",", "（": "(", "）": ")",
    "“": '"', "”": '"', "［": "[", "］": "]",
    "｛": "{", "｝": "}", "＝": "=", "＋": "+", "－": "-",
    "＜": "<", "＞": ">", "　": " ", "、": ",",
}

# 可无空格粘连切分的关键字前缀（语句起始词，几乎不会是标识符前缀）
_GLUE_PREFIX = (
    "如果", "否则", "循环", "返回", "说出", "必须", "尝试", "接住",
    "最后", "报错", "执行", "观察", "结束", "重复", "每个",
)


class Lexer:
    __slots__ = ("src", "i", "n", "line", "col", "out")

    def __init__(self, src: str):
        self.src = src
        self.i = 0
        self.n = len(src)
        self.line = 1
        self.col = 1
        self.out: list[Token] = []

    # ---- 基础游标 ----
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

    def _emit(self, kind: K, value: str, line: int, col: int) -> None:
        self.out.append(Token(kind, value, line, col))

    # ---- 主循环 ----
    def tokens(self) -> list[Token]:
        while self.i < self.n:
            ch = self._peek()

            if ch in _BLANK:
                self._advance()
                continue

            if ch == "\n":
                nl = self.line
                nc = self.col
                self._emit(K.NEWLINE, "\n", nl, nc)
                self._advance()
                continue

            # 注释：注： 到行尾（也接受「#」与「//」）
            if ch == "注" and self._peek(1) == "：":
                self._skip_line()
                continue
            if ch == "#" or (ch == "/" and self._peek(1) == "/"):
                self._skip_line()
                continue

            # 字符串：双引号，或中文引号「」
            if ch == '"' or ch == "「":
                self._string(ch)
                continue

            # 数字
            if ch.isdigit():
                self._number()
                continue

            # 标识符 / 关键字
            if ch == "_" or ch.isalpha():
                self._word()
                continue

            # 单字符与双字符符号
            if self._symbol():
                continue

            # 全角符号：给出可操作提示
            if ch in _FULLWIDTH:
                half = _FULLWIDTH[ch]
                raise LexError(
                    f"{self.line}:{self.col} 用了中文全角符号「{ch}」，"
                    f"请换成英文半角符号 {half}（切换输入法到英文再打一遍）")

            raise LexError(f"{self.line}:{self.col} 无法识别的字符 {ch!r}")

        self._emit(K.EOF, "", self.line, self.col)
        return self.out

    def _skip_line(self) -> None:
        while self.i < self.n and self._peek() != "\n":
            self._advance()

    # ---- 各词类 ----
    def _string(self, quote: str) -> None:
        line, col = self.line, self.col
        close = '"' if quote == '"' else "」"
        self._advance()
        parts: list[str] = []
        while self.i < self.n and self._peek() != close:
            ch = self._peek()
            if ch == "\\" and self._peek(1) is not None:
                nxt = self._peek(1)
                parts.append({"n": "\n", "t": "\t", '"': '"', "\\": "\\",
                              "」": "」", "r": "\r"}.get(nxt, nxt))
                self._advance(2)
                continue
            if ch == "\n":
                raise LexError(f"{line}:{col} 字符串未闭合（缺少 {close}）")
            parts.append(ch)
            self._advance()
        if self.i >= self.n:
            raise LexError(f"{line}:{col} 字符串未闭合")
        self._advance()
        self._emit(K.STRING, "".join(parts), line, col)

    def _number(self) -> None:
        line, col = self.line, self.col
        buf: list[str] = []
        while self.i < self.n and self._peek().isdigit():
            buf.append(self._peek())
            self._advance()
        # 小数点：仅当后面还有数字（避免吃掉 1.字段 这种）
        if self._peek() == "." and (self._peek(1) or "").isdigit():
            buf.append(".")
            self._advance()
            while self.i < self.n and self._peek().isdigit():
                buf.append(self._peek())
                self._advance()
        self._emit(K.NUMBER, "".join(buf), line, col)

    def _word(self) -> None:
        line, col = self.line, self.col
        buf: list[str] = []
        while self.i < self.n:
            ch = self._peek()
            if ch == "_" or ch.isalnum():
                buf.append(ch)
                self._advance()
            else:
                break
        word = "".join(buf)

        # 1) 精确命中（含别名归一化）
        kind = KEYWORDS.get(word)
        if kind is not None:
            self._emit(kind, word, line, col)
            return
        alias = ALIASES.get(word)
        if alias is not None:
            self._emit(KEYWORDS[alias], word, line, col)
            return

        # 2) 关键字粘连切分（中文不加空格）：如「如果条件」
        for prefix in _GLUE_PREFIX:
            if word.startswith(prefix) and len(word) > len(prefix):
                rest = word[len(prefix):]
                if rest and (rest[0].isalnum() or rest[0] == "_") and rest not in KEYWORDS:
                    self._emit(KEYWORDS[prefix], prefix, line, col)
                    self._emit(K.IDENT, rest, line, col + len(prefix))
                    return

        # 3) 「之」粘连切分（导入别名）：工具之工具
        if word.count("之") == 1:
            left, _, right = word.partition("之")
            if len(left) >= 2 and right and left not in KEYWORDS and right not in KEYWORDS:
                self._emit(K.IDENT, left, line, col)
                self._emit(K.IDENT, "之", line, col + len(left))
                self._emit(K.IDENT, right, line, col + len(left) + 1)
                return

        self._emit(K.IDENT, word, line, col)

    def _symbol(self) -> bool:
        line, col = self.line, self.col
        two = (self._peek() or "") + (self._peek(1) or "")
        if two == "=>":
            self._emit(K.FATARROW, "=>", line, col)
            self._advance(2)
            return True

        if two == "->":
            self._emit(K.ARROW, "->", line, col)
            self._advance(2)
            return True
        table2 = {":=": K.EQ, "==": K.EQEQ, "!=": K.NE, "<=": K.LE, ">=": K.GE}
        if two in table2:
            self._emit(table2[two], two, line, col)
            self._advance(2)
            return True

        ch = self._peek()
        table1 = {
            ":": K.COLON, ",": K.COMMA, ".": K.DOT,
            "(": K.LPAREN, ")": K.RPAREN,
            "[": K.LBRACK, "]": K.RBRACK,
            "{": K.LBRACE, "}": K.RBRACE,
            "+": K.PLUS, "-": K.MINUS, "*": K.STAR, "/": K.SLASH, "%": K.PERCENT,
            "=": K.EQ, "<": K.LT, ">": K.GT,
        }
        if ch in table1:
            self._emit(table1[ch], ch, line, col)
            self._advance()
            return True
        return False


def tokenize(src: str) -> list[Token]:
    """词法分析，并丢弃「括号内的换行」。

    为什么要这样做：长表达式换行是最基本的可维护性需求，
    但衡语早期把换行当作语句分隔，导致
        x = 甲 + 乙
          + 丙
    这种写法直接语法错误。中大型项目里长表达式很常见，
    只能硬写成一行，可读性极差。
    在 ( [ { 内部忽略换行，是几乎所有语言的通行做法。
    """
    toks = Lexer(src).tokens()
    out: list[Token] = []
    depth = 0
    for t in toks:
        if t.kind in (K.LPAREN, K.LBRACK, K.LBRACE):
            depth += 1
            out.append(t)
            continue
        if t.kind in (K.RPAREN, K.RBRACK, K.RBRACE):
            depth = max(0, depth - 1)
            out.append(t)
            continue
        if t.kind == K.NEWLINE and depth > 0:
            continue          # 括号内换行不产生语句分隔
        out.append(t)
    return out
