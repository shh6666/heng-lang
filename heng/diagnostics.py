"""衡语诊断基础设施：结构化诊断 + 多错误收集。

现代编译器要求（LSP 3.17 / 主流编译器实践）：
  - 诊断带 code（可检索）、severity（1=错误 2=警告 3=信息）、精确范围（行/列起止）；
  - 一次编译可返回多条诊断，而非遇到第一个错误就停；
  - 诊断可与源码片段关联，供编辑器高亮。

本模块提供 Diagnostic 数据结构与 DiagnosticBag 收集器，
供 typecheck / parser / lsp 共用，是「多错误报告」的地基。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum


class Severity(IntEnum):
    ERROR = 1
    WARNING = 2
    INFORMATION = 3
    HINT = 4


@dataclass
class Diagnostic:
    """一条结构化诊断。"""
    message: str
    line: int = 1
    col: int = 1
    end_col: int | None = None
    severity: Severity = Severity.ERROR
    code: str = ""
    source: str = "衡语"

    def to_lsp(self) -> dict:
        """转换为 LSP 3.17 Diagnostic 结构（0 基行列）。"""
        end = self.end_col if self.end_col is not None else self.col + 1
        return {
            "range": {
                "start": {"line": max(self.line - 1, 0), "character": max(self.col - 1, 0)},
                "end": {"line": max(self.line - 1, 0), "character": max(end - 1, 1)},
            },
            "severity": int(self.severity),
            "code": self.code,
            "source": self.source,
            "message": self.message,
        }

    def __str__(self) -> str:
        tag = {Severity.ERROR: "错误", Severity.WARNING: "警告",
               Severity.INFORMATION: "信息", Severity.HINT: "提示"}[self.severity]
        code = f"[{self.code}] " if self.code else ""
        return f"{self.line}:{self.col} {tag}：{code}{self.message}"


@dataclass
class DiagnosticBag:
    """诊断收集器：允许多错误累积，支持按严重级别过滤与排序。"""
    items: list[Diagnostic] = field(default_factory=list)

    def error(self, message: str, line: int = 1, col: int = 1,
              code: str = "", end_col: int | None = None) -> None:
        self.items.append(Diagnostic(message, line, col, end_col, Severity.ERROR, code))

    def warning(self, message: str, line: int = 1, col: int = 1,
                code: str = "", end_col: int | None = None) -> None:
        self.items.append(Diagnostic(message, line, col, end_col, Severity.WARNING, code))

    def extend(self, other: "DiagnosticBag") -> None:
        self.items.extend(other.items)

    @property
    def has_errors(self) -> bool:
        return any(d.severity == Severity.ERROR for d in self.items)

    @property
    def error_count(self) -> int:
        return sum(1 for d in self.items if d.severity == Severity.ERROR)

    def sorted(self) -> list[Diagnostic]:
        return sorted(self.items, key=lambda d: (d.line, d.col, d.severity))

    def to_lsp(self) -> list[dict]:
        return [d.to_lsp() for d in self.sorted()]

    def format_all(self, limit: int = 20) -> str:
        """格式化为人类可读的多错误报告。"""
        diags = self.sorted()
        if not diags:
            return ""
        lines = [str(d) for d in diags[:limit]]
        if len(diags) > limit:
            lines.append(f"……另有 {len(diags) - limit} 条诊断未显示")
        return "\n".join(lines)

    def __len__(self) -> int:
        return len(self.items)
