"""衡语内核 v2 —— 标准库。

与旧实现的区别：返回类型是 types2.Type（含量纲），
因此标准库调用也能参与编译期类型与量纲检查。
纯函数默认可用；真实副作用（网络/文件）带 cap，沙箱内需授权。
"""

from __future__ import annotations

import json as _json
import math as _math
import time as _time
import urllib.error
import urllib.parse
import urllib.request
import os
from dataclasses import dataclass
from typing import Any, Callable, Optional

from .types2 import Type, Unit, EMPTY_UNIT, T_NUM, T_TEXT, T_BOOL, T_NIL, T_ANY, T_seq, T_map


class HErr(Exception):
    """衡语运行时错误（新内核统一错误类型）。"""

    def __init__(self, category: str, message: str):
        self.category = category
        self.message = message
        super().__init__(f"错误<{category}>: {message}")


@dataclass(slots=True)
class Entry:
    arity: int | tuple[int, ...]
    ret: Type
    fn: Callable[..., Any]
    cap: Optional[str] = None
    doc: str = ""
    # 返回类型跟随第一个实参（如 表.追加 返回同元素类型的序列）。
    # 这让标准库在无泛型声明的情况下也能给出精确类型。
    ret_from_arg: int = -1


REG: dict[str, Entry] = {}
ALIAS = {"文本": "文字", "数值": "数", "序列": "表", "列表": "表",
         "映射": "字典", "数组": "表"}


def _e(fn, arity, ret, cap=None, doc="", ret_from_arg=-1):
    return Entry(arity, ret, fn, cap, doc, ret_from_arg)


def _t(s: Any) -> str:
    return "" if s is None else str(s)


def _n(v: Any) -> float:
    return float(v)


def _lst(v: Any) -> list:
    return list(v or [])


# ---------------- 文本 ----------------
REG.update({
    "文字.切分": _e(lambda s, sep: _t(s).split(_t(sep)), 2, T_seq(T_TEXT), doc="按分隔符切分"),
    "文字.合并": _e(lambda xs, sep: _t(sep).join(_t(x) for x in _lst(xs)), 2, T_TEXT, doc="拼接序列"),
    "文字.替换": _e(lambda s, a, b: _t(s).replace(_t(a), _t(b)), 3, T_TEXT),
    "文字.含": _e(lambda s, sub: _t(sub) in _t(s), 2, T_BOOL),
    "文字.开头是": _e(lambda s, p: _t(s).startswith(_t(p)), 2, T_BOOL),
    "文字.结尾是": _e(lambda s, p: _t(s).endswith(_t(p)), 2, T_BOOL),
    "文字.去空白": _e(lambda s: _t(s).strip(), 1, T_TEXT),
    "文字.大写": _e(lambda s: _t(s).upper(), 1, T_TEXT),
    "文字.小写": _e(lambda s: _t(s).lower(), 1, T_TEXT),
    "文字.长度": _e(lambda s: float(len(_t(s))), 1, T_NUM),
    "文字.字节数": _e(lambda s: float(len(_t(s).encode("utf-8"))), 1, T_NUM),
    "文字.截": _e(lambda s, n: _t(s)[:int(_n(n))], 2, T_TEXT,
                 doc="取前 n 个字符"),
    # 取「从第 n 个字符开始到末尾」。文字.截 是「取前 n 个」，
    # 两者语义不同；解析 "按钮|RUN" 这类带前缀的字符串必须用 文字.从。
    "文字.从": _e(lambda s, n: _t(s)[int(_n(n)):], 2, T_TEXT,
                 doc="从第 n 个字符开始取到末尾"),
    "文字.分行": _e(lambda s: _t(s).splitlines(), 1, T_seq(T_TEXT)),
    # 字符码：返回首字符的 Unicode 码点（中文处理与散列必须用到）
    "文字.字符码": _e(lambda s: float(ord(_t(s)[0])) if _t(s) else 0.0, 1, T_NUM,
                    doc="首字符的 Unicode 码点"),
    "文字.由码点": _e(lambda n: chr(int(_n(n))), 1, T_TEXT, doc="由码点构造字符"),
})

# ---------------- 数学 ----------------
REG.update({
    "数.绝对值": _e(lambda x: abs(_n(x)), 1, T_NUM),
    "数.取整": _e(lambda x: float(_math.floor(_n(x))), 1, T_NUM),
    "数.四舍五入": _e(lambda x, d=0.0: float(round(_n(x), int(_n(d)))), (1, 2), T_NUM),
    "数.开方": _e(lambda x: _math.sqrt(_n(x)), 1, T_NUM),
    "数.乘方": _e(lambda x, y: _n(x) ** _n(y), 2, T_NUM),
    "数.对数": _e(lambda x: _math.log(_n(x)), 1, T_NUM),
    "数.指数": _e(lambda x: _math.exp(_n(x)), 1, T_NUM),
    "数.正弦": _e(lambda x: _math.sin(_n(x)), 1, T_NUM),
    "数.余弦": _e(lambda x: _math.cos(_n(x)), 1, T_NUM),
    "数.最大": _e(lambda a, b: max(_n(a), _n(b)), 2, T_NUM),
    "数.最小": _e(lambda a, b: min(_n(a), _n(b)), 2, T_NUM),
    "数.圆周率": _e(lambda: _math.pi, 0, T_NUM),
    "数.整除": _e(lambda a, b: float(int(_n(a) // _n(b))), 2, T_NUM, doc="整数除法（向下取整）"),
    "数.取余": _e(lambda a, b: float(_math.fmod(_n(a), _n(b))), 2, T_NUM, doc="取余"),
})

# ---------------- 序列 ----------------
REG.update({
    "表.追加": _e(lambda xs, x: _lst(xs) + [x], 2, T_seq(T_ANY), ret_from_arg=0),
    "表.取": _e(lambda xs, i: _lst(xs)[int(_n(i))], 2, T_ANY),
    "表.长度": _e(lambda xs: float(len(_lst(xs))), 1, T_NUM),
    "表.连接": _e(lambda a, b: _lst(a) + _lst(b), 2, T_seq(T_ANY)),
    "表.片段": _e(lambda xs, i, n: _lst(xs)[int(_n(i)):int(_n(i)) + int(_n(n))], 3, T_seq(T_ANY)),
    "表.排序": _e(lambda xs: sorted(_lst(xs), key=lambda v: (isinstance(v, str), v)), 1, T_seq(T_ANY), ret_from_arg=0),
    "表.倒序": _e(lambda xs: list(reversed(_lst(xs))), 1, T_seq(T_ANY), ret_from_arg=0),
    "表.求和": _e(lambda xs: float(sum(_n(x) for x in _lst(xs))), 1, T_NUM),
    "表.平均": _e(lambda xs: (float(sum(_n(x) for x in _lst(xs))) / len(_lst(xs)))
                  if _lst(xs) else 0.0, 1, T_NUM),
    "表.最大": _e(lambda xs: float(max(_n(x) for x in _lst(xs))), 1, T_NUM),
    "表.最小": _e(lambda xs: float(min(_n(x) for x in _lst(xs))), 1, T_NUM),
    "表.含": _e(lambda xs, x: x in _lst(xs), 2, T_BOOL),
    "表.去重": _e(lambda xs: list(dict.fromkeys(_lst(xs))), 1, T_seq(T_ANY), ret_from_arg=0),
    "表.过滤非空": _e(lambda xs: [x for x in _lst(xs) if _t(x).strip()], 1, T_seq(T_ANY), ret_from_arg=0),
})

# ---------------- 映射 ----------------
REG.update({
    "字典.取值": _e(lambda m, k, d="": dict(m or {}).get(_t(k), d), (2, 3), T_ANY),
    "字典.设值": _e(lambda m, k, v: {**(m or {}), _t(k): v}, 3, T_map(T_TEXT, T_ANY)),
    "字典.有键": _e(lambda m, k: _t(k) in (m or {}), 2, T_BOOL),
    "字典.键": _e(lambda m: list((m or {}).keys()), 1, T_seq(T_TEXT)),
    "字典.删键": _e(lambda m, k: {kk: vv for kk, vv in (m or {}).items() if kk != _t(k)}, 2,
                  T_map(T_TEXT, T_ANY)),
    "字典.大小": _e(lambda m: float(len(m or {})), 1, T_NUM),
})


# ---------------- JSON ----------------
def _clean(v: Any) -> Any:
    if v is None:
        return ""
    if isinstance(v, bool):
        return v
    if isinstance(v, float):
        if _math.isnan(v) or _math.isinf(v):
            return None
        return int(v) if v.is_integer() else v
    if isinstance(v, list):
        return [_clean(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _clean(x) for k, x in v.items()}
    return v


def _json_encode(v: Any, indent: float = 0) -> str:
    return _json.dumps(_clean(v), ensure_ascii=False,
                       indent=(int(indent) if indent and indent > 0 else None))


def _json_decode(s: str) -> Any:
    try:
        return _json.loads(_t(s))
    except ValueError as e:
        raise HErr("类型", f"JSON 解析失败：{e}")


def _json_ok(s: str) -> bool:
    try:
        _json.loads(_t(s))
        return True
    except ValueError:
        return False


REG.update({
    "JSON.编码": _e(_json_encode, (1, 2), T_TEXT),
    "JSON.解码": _e(_json_decode, 1, T_map(T_TEXT, T_ANY)),
    "JSON.有效": _e(_json_ok, 1, T_BOOL),
})

# ---------------- 时间 ----------------
REG.update({
    # 时间戳是绝对数值：带量纲会导致「与纯数比较」被拒（实用上很别扭），
    # 单位仅作为命名约定（现在=秒，毫秒=毫秒）。
    "时间.现在": _e(lambda: float(_time.time()), 0, T_NUM, doc="当前时间戳（秒）"),
    "时间.毫秒": _e(lambda: float(int(_time.time() * 1000)), 0, T_NUM, doc="当前时间戳（毫秒）"),
    "时间.格式化": _e(lambda ts, fmt="%Y-%m-%d %H:%M:%S":
                      _time.strftime(_t(fmt), _time.localtime(_n(ts))), (1, 2), T_TEXT),
})


# ---------------- 网络（需授权） ----------------
def _http_get(url: str, timeout: float = 10.0) -> dict:
    req = urllib.request.Request(_t(url), headers={"User-Agent": "Heng/2"})
    try:
        with urllib.request.urlopen(req, timeout=_n(timeout)) as r:
            return {"状态": float(r.status), "正文": r.read().decode("utf-8", "replace")}
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            pass
        return {"状态": float(e.code), "正文": body}
    except Exception as e:
        raise HErr("网络", f"请求失败：{e}")


def _http_post(url: str, payload: str, timeout: float = 10.0) -> dict:
    req = urllib.request.Request(
        _t(url), data=_t(payload).encode("utf-8"),
        headers={"User-Agent": "Heng/2", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=_n(timeout)) as r:
            return {"状态": float(r.status), "正文": r.read().decode("utf-8", "replace")}
    except urllib.error.HTTPError as e:
        return {"状态": float(e.code), "正文": ""}
    except Exception as e:
        raise HErr("网络", f"请求失败：{e}")


REG.update({
    "网络.取": _e(_http_get, (1, 2), T_map(T_TEXT, T_ANY), cap="网络.请求"),
    "网络.发": _e(_http_post, (2, 3), T_map(T_TEXT, T_ANY), cap="网络.请求"),
    "网络.编码": _e(lambda s: urllib.parse.quote(_t(s), safe=""), 1, T_TEXT),
})


def lookup(name: str) -> Optional[Entry]:
    e = REG.get(name)
    if e is not None:
        return e
    if "." in name:
        mod, fn = name.split(".", 1)
        canon = ALIAS.get(mod)
        if canon:
            return REG.get(f"{canon}.{fn}")
    return None


def call(name: str, entry: Entry, args: list[Any], caps: Optional[set[str]]) -> Any:
    if entry.cap is not None and caps is not None and entry.cap not in caps:
        raise HErr("未授权", f"{name} 需要能力 {entry.cap}，沙箱内必须显式授权")
    try:
        return entry.fn(*args)
    except HErr:
        raise
    except ZeroDivisionError:
        raise HErr("除零", f"{name} 发生除零")
    except (IndexError, KeyError) as e:
        raise HErr("越界", f"{name} 下标或键越界：{e}")
    except (TypeError, ValueError) as e:
        raise HErr("类型", f"{name} 参数类型不对：{e}")
    except Exception as e:
        raise HErr("未定义", f"{name} 执行失败：{e}")
