"""衡语最小标准库（报告优先级 2）。

评估报告的结论是：衡语当前「没有包管理器、没有标准库」，而 AI 应用离不开
JSON、HTTP、向量运算——这三样都缺，就无法打通 AI 应用开发。本模块按报告
给出的最小集合实现：

    文本  —— UTF-8 文本处理：切分、替换、包含、子串、大小写、去空白、分/合行
    数   —— 基础数学函数（三角/对数/指数/取整/极值/绝对值）
    序列 —— 数组操作：追加、取、连接、切片、排序、求和、平均值、最大最小
    映射 —— 键值：取值、设值、键集、包含、删除（不可变，返回新映射）
    时间 —— 当前时间戳、格式化（免授权，无副作用）
    JSON —— 编解码（AI 工具调用的通用载荷）
    HTTP —— 网络请求（受能力沙箱约束，必须显式授权 网络.请求）

设计约束（与语言既有语义保持一致）：
  1. 调用形式为「模块.函数(...)」，与 工具.查询 这类能力名同形，因此复用
     Attr 节点，不需要改语法；词法上「文本」「序列」是类型关键字，所以模块名
     统一用「文字」「表」等非关键字形式，避免与类型名冲突（见 MODULE_ALIAS）。
  2. 纯函数（文本/数/表/字典）默认可用——它们不触达外部世界，符合
     「沙箱默认拒绝」只约束真实副作用的设计。
  3. 真实副作用（网络、文件）必须受 caps 约束：未授权时直接报 错误<未授权>，
     与 工具.查询 走同一套能力沙箱，不引入第二套安全模型。
"""

from __future__ import annotations

import json as _json
import math as _math
import time as _time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Optional

from .ast_nodes import Type, Unit


# ---------------- 与解释器共享的值包装 ----------------
# 延迟导入避免循环依赖（interpreter 在调用时才 import 本模块）。

def _hvalue(t, v):
    from .interpreter import HValue
    return HValue(t, v)


def _type(name: str, unit: Optional[list[str]] = None, den: Optional[list[str]] = None):
    return Type(name, unit=Unit(list(unit or []), list(den or [])) if unit or den else None)


@dataclass
class StdEntry:
    """一条标准库函数定义。

    ret 为返回类型名；ret_elem 用于序列返回值的元素类型标注
    （如 文字.切分 返回 序列 文本，而非粗糙的 序列 任意，
    否则「"前缀" + 切分(...) 的元素」这类正常写法会被类型检查器误拒）。
    """
    arity: int | tuple[int, ...]
    ret: str                      # 返回类型名：数/文本/真假/序列/映射/空
    impl: Callable[..., Any]
    needs_cap: Optional[str] = None   # 需要的沙箱能力（None = 纯函数）
    doc: str = ""
    ret_elem: Optional[str] = None    # 序列元素类型名


# ---------------- 参数解包 / 结果包装 ----------------

def _raw(v):
    """把 HValue 还原成 Python 值（记录转 dict，保持嵌套结构）。"""
    from .interpreter import HValue, HRecord
    if isinstance(v, HValue):
        return _raw(v.value)
    if isinstance(v, HRecord):
        return {k: _raw(x) for k, x in v.fields.items()}
    if isinstance(v, list):
        return [_raw(x) for x in v]
    if isinstance(v, dict):
        return {k: _raw(x) for k, x in v.items()}
    return v


def _wrap(value: Any, kind: str, entry_elem: Optional[str] = None):
    """把 Python 结果包装回 HValue。"""
    if kind == "数":
        if isinstance(value, bool):
            value = float(value)
        return _hvalue(_type("数"), float(value))
    if kind == "真假":
        return _hvalue(_type("真假"), bool(value))
    if kind == "文本":
        return _hvalue(_type("文本"), "" if value is None else str(value))
    if kind == "空":
        return _hvalue(_type("空"), None)
    if kind == "序列":
        if entry_elem is not None:
            elem = _type(entry_elem) if entry_elem != "任意" else _type("任意")
        else:
            elem = _infer_elem(value)
        return _hvalue(Type("序列", elem=elem), [_wrap_inferred(x) for x in value])
    if kind == "映射":
        return _hvalue(Type("映射", key=_type("文本"), val=_type("任意")),
                       {str(k): _wrap_inferred(v) for k, v in value.items()})
    raise ValueError(f"未知返回类型 {kind}")


def _infer_elem(seq: Any) -> Type:
    if not seq:
        return _type("任意")
    first = seq[0]
    if isinstance(first, bool):
        return _type("真假")
    if isinstance(first, (int, float)):
        return _type("数")
    if isinstance(first, str):
        return _type("文本")
    if isinstance(first, dict):
        return Type("映射", key=_type("文本"), val=_type("任意"))
    if isinstance(first, list):
        return Type("序列", elem=_infer_elem(first))
    return _type("任意")


def _wrap_inferred(value: Any):
    t = _infer_elem([value])
    if t.name == "序列":
        return _wrap(value, "序列")
    if t.name == "映射":
        return _wrap(value, "映射")
    if t.name == "数":
        return _wrap(float(value), "数")
    if t.name == "真假":
        return _wrap(bool(value), "真假")
    if t.name == "文本":
        return _wrap(str(value), "文本")
    return _hvalue(_type("任意"), value)


def _as_text(v: Any) -> str:
    from .interpreter import HValue
    if isinstance(v, HValue):
        v = v.value
    return "" if v is None else str(v)


def _as_num(v: Any) -> float:
    from .interpreter import HValue
    if isinstance(v, HValue):
        v = v.value
    return float(v)


def _as_list(v: Any) -> list:
    from .interpreter import HValue
    if isinstance(v, HValue):
        return list(v.value or [])
    return list(v or [])


# ---------------- 数据清洗（AI 场景核心） ----------------

def _flatten(v: Any) -> Any:
    """递归清洗：None→空串，去掉 NaN/Inf（JSON 不合法值），保留中文原样。"""
    if v is None:
        return ""
    if isinstance(v, bool):
        return v
    if isinstance(v, float):
        if _math.isnan(v) or _math.isinf(v):
            return None
        return int(v) if v.is_integer() else v
    if isinstance(v, list):
        return [_flatten(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _flatten(x) for k, x in v.items()}
    return v


def to_json(v: Any, indent: int = 0) -> str:
    """任意衡语值 → JSON 文本（UTF-8 中文不转义，便于人与 LLM 阅读）。"""
    return _json.dumps(_flatten(v), ensure_ascii=False,
                       indent=(indent if indent and indent > 0 else None))


def parse_json(text: str) -> Any:
    try:
        return _json.loads(text)
    except ValueError as e:
        from .interpreter import HError
        raise HError("类型", f"JSON 解析失败：{e}")


# ---------------- HTTP ----------------

def http_get(url: str, timeout: float = 10.0) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "Heng/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
            return {"状态": resp.status, "正文": body}
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            pass
        return {"状态": e.code, "正文": body}
    except Exception as e:
        from .interpreter import HError
        raise HError("拒绝", f"网络请求失败：{e}")


def http_post(url: str, payload: str, timeout: float = 10.0) -> dict:
    data = payload.encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={"User-Agent": "Heng/1.0", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return {"状态": resp.status, "正文": resp.read().decode("utf-8", "replace")}
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            pass
        return {"状态": e.code, "正文": body}
    except Exception as e:
        from .interpreter import HError
        raise HError("拒绝", f"网络请求失败：{e}")


def http_encode(text: str) -> str:
    return urllib.parse.quote(text, safe="")


# ---------------- 注册表 ----------------

def _m(fn, arity, ret, cap=None, doc="", elem=None):
    return StdEntry(arity, ret, fn, cap, doc, elem)


REGISTRY: dict[str, StdEntry] = {}


def _reg(table: dict[str, StdEntry]) -> None:
    REGISTRY.update(table)


# 文字.xxx —— 文本处理
_reg({
    "文字.切分": _m(lambda s, sep: _as_text(s).split(_as_text(sep)), 2, "序列", doc="按分隔符切成序列", elem="文本"),
    "文字.合并": _m(lambda xs, sep: _as_text(sep).join(_as_text(x) for x in _as_list(xs)), 2, "文本", doc="把序列拼成一段文本"),
    "文字.替换": _m(lambda s, a, b: _as_text(s).replace(_as_text(a), _as_text(b)), 3, "文本", doc="全部替换"),
    "文字.取段": _m(lambda s, i, n: _as_text(s)[int(_as_num(i)):int(_as_num(i)) + int(_as_num(n))], 3, "文本", doc="取子串（起点, 长度）"),
    "文字.含": _m(lambda s, sub: _as_text(sub) in _as_text(s), 2, "真假", doc="是否包含"),
    "文字.开头是": _m(lambda s, p: _as_text(s).startswith(_as_text(p)), 2, "真假", doc="是否以…开头"),
    "文字.结尾是": _m(lambda s, p: _as_text(s).endswith(_as_text(p)), 2, "真假", doc="是否以…结尾"),
    "文字.去空白": _m(lambda s: _as_text(s).strip(), 1, "文本", doc="去掉首尾空白（含全角空格）"),
    "文字.大写": _m(lambda s: _as_text(s).upper(), 1, "文本", doc="英文字母转大写"),
    "文字.小写": _m(lambda s: _as_text(s).lower(), 1, "文本", doc="英文字母转小写"),
    "文字.字节数": _m(lambda s: float(len(_as_text(s).encode("utf-8"))), 1, "数", doc="UTF-8 编码后的字节数"),
    "文字.长度": _m(lambda s: float(len(_as_text(s))), 1, "数", doc="字符个数（中文按一个算）"),
    "文字.分行": _m(lambda s: _as_text(s).splitlines(), 1, "序列", doc="按行切分", elem="文本"),
    "文字.截": _m(lambda s, n: _as_text(s)[:int(_as_num(n))], 2, "文本", doc="取前 n 个字符"),
})

# 数.xxx —— 数学函数
_reg({
    "数.绝对值": _m(lambda x: abs(_as_num(x)), 1, "数"),
    "数.取整": _m(lambda x: float(_math.floor(_as_num(x))), 1, "数", doc="向下取整"),
    "数.四舍五入": _m(lambda x, n=0.0: float(round(_as_num(x), int(_as_num(n)))), (1, 2), "数"),
    "数.开方": _m(lambda x: _math.sqrt(_as_num(x)), 1, "数"),
    "数.乘方": _m(lambda x, y: _as_num(x) ** _as_num(y), 2, "数"),
    "数.对数": _m(lambda x: _math.log(_as_num(x)), 1, "数", doc="自然对数"),
    "数.指数": _m(lambda x: _math.exp(_as_num(x)), 1, "数"),
    "数.正弦": _m(lambda x: _math.sin(_as_num(x)), 1, "数"),
    "数.余弦": _m(lambda x: _math.cos(_as_num(x)), 1, "数"),
    "数.最大": _m(lambda a, b: max(_as_num(a), _as_num(b)), 2, "数"),
    "数.最小": _m(lambda a, b: min(_as_num(a), _as_num(b)), 2, "数"),
    "数.圆周率": _m(lambda: _math.pi, 0, "数"),
})

# 表.xxx —— 序列操作（表 = 序列，用非关键字模块名）
_reg({
    "表.追加": _m(lambda xs, x: _as_list(xs) + [x], 2, "序列", doc="返回追加后新序列", elem="任意"),
    "表.取": _m(lambda xs, i: _as_list(xs)[int(_as_num(i))], 2, "任意", doc="按下标取（从 0 开始）"),
    "表.长度": _m(lambda xs: float(len(_as_list(xs))), 1, "数"),
    "表.连接": _m(lambda a, b: _as_list(a) + _as_list(b), 2, "序列", elem="任意"),
    "表.片段": _m(lambda xs, i, n: _as_list(xs)[int(_as_num(i)):int(_as_num(i)) + int(_as_num(n))], 3, "序列", elem="任意"),
    "表.排序": _m(lambda xs: sorted(_as_list(xs),
                                    key=lambda v: (isinstance(v, str), v)), 1, "序列",
                 doc="升序（数与文本分别排序）", elem="任意"),
    "表.倒序": _m(lambda xs: list(reversed(_as_list(xs))), 1, "序列", elem="任意"),
    "表.求和": _m(lambda xs: float(sum(_as_num(x) for x in _as_list(xs))), 1, "数"),
    "表.平均": _m(lambda xs: (float(sum(_as_num(x) for x in _as_list(xs))) / len(_as_list(xs))
                             if _as_list(xs) else 0.0), 1, "数"),
    "表.最大": _m(lambda xs: float(max(_as_num(x) for x in _as_list(xs))), 1, "数"),
    "表.最小": _m(lambda xs: float(min(_as_num(x) for x in _as_list(xs))), 1, "数"),
    "表.含": _m(lambda xs, x: x in _as_list(xs), 2, "真假"),
    "表.去重": _m(lambda xs: list(dict.fromkeys(_as_list(xs))), 1, "序列", elem="任意"),
    "表.过滤非空": _m(lambda xs: [x for x in _as_list(xs) if _as_text(x).strip()], 1, "序列", elem="任意"),
})

# 字典.xxx —— 映射操作（不可变风格：set 返回新映射，便于校验 AI 输出）
_reg({
    "字典.取值": _m(lambda m, k, d=None: _raw(m).get(_as_text(k), _raw(d) if d is not None else ""),
                  (2, 3), "任意"),
    "字典.设值": _m(lambda m, k, v: {**_raw(m), _as_text(k): _raw(v)}, 3, "映射"),
    "字典.有键": _m(lambda m, k: _as_text(k) in _raw(m), 2, "真假"),
    "字典.键": _m(lambda m: list(_raw(m).keys()), 1, "序列", elem="文本"),
    "字典.删键": _m(lambda m, k: {kk: vv for kk, vv in _raw(m).items() if kk != _as_text(k)}, 2, "映射"),
    "字典.大小": _m(lambda m: float(len(_raw(m))), 1, "数"),
})

# JSON.xxx —— AI 载荷编解码（纯函数）
_reg({
    "JSON.编码": _m(lambda v, n=0.0: to_json(_raw(v), int(_as_num(n))), (1, 2), "文本"),
    "JSON.解码": _m(lambda s: parse_json(_as_text(s)), 1, "映射"),
    "JSON.有效": _m(lambda s: _json_ok(_as_text(s)), 1, "真假", doc="是否为合法 JSON"),
})


def _json_ok(s: str) -> bool:
    try:
        _json.loads(s)
        return True
    except ValueError:
        return False


# 时间.xxx —— 免授权时钟
_reg({
    "时间.现在": _m(lambda: float(_time.time()), 0, "数", doc="Unix 时间戳（秒）"),
    "时间.毫秒": _m(lambda: float(int(_time.time() * 1000)), 0, "数"),
    "时间.格式化": _m(lambda ts, fmt="%Y-%m-%d %H:%M:%S": _time.strftime(
        _as_text(fmt), _time.localtime(_as_num(ts))), (1, 2), "文本"),
})

# 网络.xxx —— 真实副作用，必须授权 网络.请求
_reg({
    "网络.取": _m(http_get, (1, 2), "映射", cap="网络.请求", doc="GET 请求，返回 {状态, 正文}"),
    "网络.发": _m(http_post, (2, 3), "映射", cap="网络.请求", doc="POST JSON，返回 {状态, 正文}"),
    "网络.编码": _m(http_encode, 1, "文本", doc="URL 编码"),
})


# 模块别名：词法上「文本」「数值」「序列」是类型关键字，直接写 Type 名会与
# 关键字冲突，因此提供同义别名，让用户写「文本.切分」也能工作。
MODULE_ALIAS = {
    "文本": "文字",
    "数值": "数",
    "序列": "表",
    "列表": "表",
    "映射": "字典",
    "数组": "表",
}


def resolve(fname: str) -> Optional[StdEntry]:
    """按「模块.函数」查找标准库条目，支持模块别名。"""
    entry = REGISTRY.get(fname)
    if entry is not None:
        return entry
    if "." in fname:
        mod, fn = fname.split(".", 1)
        canon = MODULE_ALIAS.get(mod)
        if canon:
            return REGISTRY.get(f"{canon}.{fn}")
    return None


def call_stdlib(fname: str, args: list, caps: set[str], fmt: Callable[[Any], str]):
    """执行标准库调用。返回 None 表示「不是标准库函数」，交由调用方继续处理。

    能力检查与解释器的沙箱模型一致：caps 为空表示不受限上下文；
    非空时，需要能力的函数必须在白名单内。
    """
    from .interpreter import HError

    entry = resolve(fname)
    if entry is None:
        return None

    # 能力校验（默认拒绝）
    if entry.needs_cap is not None and caps:
        if entry.needs_cap not in caps:
            raise HError("未授权", f"标准库 {fname} 需要能力 {entry.needs_cap}，"
                                   f"沙箱内必须显式授权")

    # 参数数量校验
    n = len(args)
    if isinstance(entry.arity, tuple):
        if n not in entry.arity:
            raise HError("类型", f"{fname} 需要 {entry.arity} 个参数，收到 {n} 个")
    elif n != entry.arity:
        raise HError("类型", f"{fname} 需要 {entry.arity} 个参数，收到 {n} 个")

    raw = [_raw(a) for a in args]
    try:
        result = entry.impl(*raw)
    except HError:
        raise
    except ZeroDivisionError:
        raise HError("除零", f"{fname} 计算出除零")
    except (IndexError, KeyError) as e:
        raise HError("越界", f"{fname} 下标或键越界：{e}")
    except (TypeError, ValueError) as e:
        raise HError("类型", f"{fname} 参数类型不对：{e}")
    except Exception as e:  # 兜底：标准库异常必须转成衡语错误，不能泄漏 Python 栈
        raise HError("未定义", f"{fname} 执行失败：{e}")

    return _wrap(result, entry.ret, entry.ret_elem)


def is_stdlib_name(name: str) -> bool:
    """供类型检查器判断「这个 模块.函数 名是否属于标准库」。"""
    return resolve(name) is not None


def stdlib_signature(name: str) -> Optional[str]:
    entry = resolve(name)
    if entry is None:
        return None
    arity = entry.arity
    if isinstance(arity, tuple):
        argc = "/".join(str(a) for a in arity)
    else:
        argc = str(arity)
    cap = f"，需授权 {entry.needs_cap}" if entry.needs_cap else ""
    return f"{name} → {entry.ret}（{argc} 个参数{cap}）"


def list_names() -> list[str]:
    return sorted(REGISTRY)
