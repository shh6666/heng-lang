"""衡语内核 v2 —— 类型与量纲系统。

量纲是新内核的核心优势所在：它在**编译期**完成全部代数运算，
运行期只算 f64。因此「量纲安全」是零成本的静态保证，而非运行期检查。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# 基本维度（顺序即向量下标）
DIMS = ("长度", "时间", "质量", "金额", "计数", "温度")
DIM_INDEX = {d: i for i, d in enumerate(DIMS)}

# 基本单位 → 维度
BASE_UNITS: dict[str, str] = {
    "米": "长度", "秒": "时间", "千克": "质量",
    "元": "金额", "个": "计数", "次": "计数",
    "摄氏度": "温度", "开尔文": "温度",
}

# 维度向量回推单位时的首选表示（同维度多单位取业务最常用者）
PREFERRED_UNIT = {
    "长度": "米", "时间": "秒", "质量": "千克",
    "金额": "元", "计数": "个", "温度": "摄氏度",
}


@dataclass(frozen=True, slots=True)
class Unit:
    """单位单项式。分子/分母为不可变元组，可安全共享与哈希。"""
    num: tuple[str, ...] = ()
    den: tuple[str, ...] = ()

    def __str__(self) -> str:
        if not self.num and not self.den:
            return ""
        top = "*".join(self.num) if self.num else "1"
        return f"{top}/{'/'.join(self.den)}" if self.den else top

    @property
    def is_none(self) -> bool:
        return not self.num and not self.den


EMPTY_UNIT = Unit()


def unit_vec(u: Unit) -> tuple[int, ...]:
    """单位 → 维度指数向量。未知单位视为无量纲（自定义单位不在代数内）。"""
    v = [0] * len(DIMS)
    for name in u.num:
        d = BASE_UNITS.get(name)
        if d:
            v[DIM_INDEX[d]] += 1
    for name in u.den:
        d = BASE_UNITS.get(name)
        if d:
            v[DIM_INDEX[d]] -= 1
    return tuple(v)


def vec_unit(v: tuple[int, ...]) -> Unit:
    """维度向量 → 首选单位表示。"""
    num: list[str] = []
    den: list[str] = []
    for i, e in enumerate(v):
        name = PREFERRED_UNIT[DIMS[i]]
        if e > 0:
            num.extend([name] * e)
        elif e < 0:
            den.extend([name] * (-e))
    return Unit(tuple(num), tuple(den))


def vadd(a: tuple[int, ...], b: tuple[int, ...]) -> tuple[int, ...]:
    return tuple(x + y for x, y in zip(a, b))


def vsub(a: tuple[int, ...], b: tuple[int, ...]) -> tuple[int, ...]:
    return tuple(x - y for x, y in zip(a, b))


# ---------------- 类型 ----------------

@dataclass(frozen=True, slots=True)
class Type:
    name: str                              # 数/文本/真假/空/序列/映射/记录名/任意
    unit: Unit = EMPTY_UNIT                # 仅「数」有意义
    elem: Optional["Type"] = None          # 序列元素
    key: Optional["Type"] = None           # 映射键
    val: Optional["Type"] = None           # 映射值
    var: Optional[str] = None              # 类型变量名（泛型参数）

    def __str__(self) -> str:
        # 错误消息里用尖括号形式（与旧实现一致，用户已熟悉）
        if self.name == "数":
            u = str(self.unit)
            return f"数<{u}>" if u else "数"
        if self.name == "类型变量":
            return self.var or "T"
        if self.name == "序列":
            return f"序列<{self.elem}>"
        if self.name == "映射":
            return f"映射<{self.key},{self.val}>"
        return self.name


T_NUM = Type("数")
T_TEXT = Type("文本")
T_BOOL = Type("真假")
T_NIL = Type("空")
T_ANY = Type("任意")
T_UNKNOWN = Type("未知")


def T_num(u: Unit = EMPTY_UNIT) -> Type:
    return T_NUM if u.is_none else Type("数", u)


def T_seq(elem: Type) -> Type:
    return Type("序列", elem=elem)


def T_map(k: Type, v: Type) -> Type:
    return Type("映射", key=k, val=v)


def same_type(a: Type, b: Type) -> bool:
    """类型等价判定。量纲按维度向量比较（元/个 与 元/次 不等价）。

    记录/常量被导入后名字会带模块前缀（内核.内存块），
    与只写短名（内存块）指向同一个类型，按短名比较。
    """
    if a.name != b.name:
        if a.name.split(".")[-1] != b.name.split(".")[-1]:
            return False
    if a.name == "数":
        return unit_vec(a.unit) == unit_vec(b.unit)
    if a.name == "序列":
        return a.elem is not None and b.elem is not None and same_type(a.elem, b.elem)
    if a.name == "映射":
        return (a.key is not None and b.key is not None and same_type(a.key, b.key)
                and a.val is not None and b.val is not None and same_type(a.val, b.val))
    return True


def T_var(name: str) -> Type:
    """类型变量：泛型参数（如 <T> 里的 T）。"""
    return Type("类型变量", var=name)


def subst(ty: Type, bind: dict) -> Type:
    """把类型变量按绑定表替换成具体类型。"""
    if ty is None:
        return ty
    if ty.name == "类型变量":
        return bind.get(getattr(ty, "var", ""), ty)
    if ty.name == "序列" and getattr(ty, "elem", None) is not None:
        return T_seq(subst(ty.elem, bind))
    if ty.name == "映射" and getattr(ty, "key", None) is not None:
        return T_map(subst(ty.key, bind), subst(getattr(ty, "val", T_ANY), bind))
    return ty


def assignable(declared: Type, actual: Type) -> bool:
    """赋值兼容。

    规则：
      - 任意 与任何类型互通（动态值）；
      - 无量纲数 可赋给带单位的数：`变量 单价 : 数 元/个 = 12` 里 12 本身
        不写单位，语义是「12 个该单位」——这是量纲系统最自然的读法，
        否则用户每处都必须重复写单位，反而成为负担；
      - 反向不允许：带单位的数不能静默退化为无量纲（会丢信息）。
    """
    if actual.name == "任意" or declared.name == "任意":
        return True
    # 未知类型同样放行：函数体在完整推断前可能先遇到部分未知
    if actual.name == "未知" or declared.name == "未知":
        return True
    # 容器：元素为「任意」时可赋给具体元素类型（标准库个别函数仍返回宽类型）。
    # 泛型与 ret_from_arg 已让主要函数给出精确类型，这里只保留最窄的放宽。
    if declared.name == actual.name and declared.name in ("序列", "映射"):
        de = getattr(declared, "elem", None)
        ae = getattr(actual, "elem", None)
        if ae is not None and ae.name == "任意":
            return True
        if de is not None and ae is not None and de.name != "任意" and de.name != ae.name:
            return False
        return True

    if declared.name == "数" and actual.name == "数":
        dv = unit_vec(declared.unit)
        av = unit_vec(actual.unit)
        if dv == av:
            return True
        # 无量纲 → 带单位：允许（字面量按声明单位解读）
        if not any(av):
            return True
        return False
    return same_type(declared, actual)


# ---------------- 错误类别 ----------------

KNOWN_ERRORS = {
    "超时", "拒绝", "除零", "类型", "量纲", "未授权", "越界", "未定义", "断言", "网络",
}

# 内置能力（沙箱白名单来源）
KNOWN_CAPABILITIES = {
    "工具.查询", "工具.清理", "工具.慢查询", "文件.读取", "文件.写入", "文件.存在", "网络.请求", "输出",
    "系统.参数", "系统.脚本目录", "服务.启动", "服务.待请求", "服务.回复", "系统.跑命令", "系统.可执行文件", "终端.读键",
}


# 能力的返回类型。放在这里是因为要引用 T_text / T_bool 等构造器。
def _init_cap_types() -> dict:
    return {
        "文件.读取": T_TEXT,
        "文件.写入": T_BOOL,
        "网络.请求": T_TEXT,
        "工具.查询": T_ANY,
        "工具.清理": T_BOOL,
        "工具.慢查询": T_ANY,
        "输出": T_NIL,
    }
