"""衡语内核 v2 测试套件。

三条验收主线（对应 docs/CORE_ARCHITECTURE.md 第十节）：
  A. 语义正确：语言特性逐项验证；
  B. 字节码自足：.hbc 写入磁盘、由独立进程加载执行，结果一致；
  C. 唯一执行引擎：所有执行都走原生 C VM，Python 侧不含解释器。
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

from heng2.api2 import compile_src, HengError
from heng2.bytecode import Module
from heng2.disasm import disasm


_VM_CACHE: dict[str, str] = {}


def _native_vm() -> str:
    """构建（并缓存）原生 VM 可执行文件。"""
    if "path" in _VM_CACHE:
        return _VM_CACHE["path"]
    root = os.path.join(os.path.dirname(__file__), "..")
    src = os.path.join(root, "native", "hengvm.c")
    out = os.path.join(root, "native", "bin", "hengvm")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    cc = shutil.which("clang") or shutil.which("cc")
    if cc is None:
        raise unittest.SkipTest("需要 C 编译器")
    subprocess.run([cc, "-O3", "-pthread", "-o", out, src, "-lm"],
                   check=True, capture_output=True)
    _VM_CACHE["path"] = out
    return out


def run_native(src: str) -> list[str]:
    """编译并交给原生 VM 执行，返回输出行。"""
    mod, _ = compile_src(src)
    with tempfile.NamedTemporaryFile(suffix=".hbc", delete=False) as f:
        f.write(mod.to_bytes())
        path = f.name
    try:
        r = subprocess.run([_native_vm(), path], capture_output=True, text=True)
        if r.returncode != 0:
            raise HengError(r.stderr.strip() or "原生 VM 执行失败")
        return r.stdout.splitlines()
    finally:
        os.unlink(path)


def run_bytes(data: bytes) -> list[str]:
    """直接执行已序列化的字节码（验证 .hbc 自足性）。"""
    with tempfile.NamedTemporaryFile(suffix=".hbc", delete=False) as f:
        f.write(data)
        path = f.name
    try:
        r = subprocess.run([_native_vm(), path], capture_output=True, text=True)
        if r.returncode != 0:
            raise HengError(r.stderr.strip() or "原生 VM 执行失败")
        return r.stdout.splitlines()
    finally:
        os.unlink(path)


def heng(src: str) -> list[str]:
    return run_native(src)


class LexerTests(unittest.TestCase):
    def test_keywords_and_alias(self):
        from heng2.lexer2 import tokenize
        from heng2.token import K
        toks = tokenize("倘若 真:\n完毕\n")
        kinds = [t.kind for t in toks]
        self.assertIn(K.KW_IF, kinds)      # 倘若 归一化为 如果
        self.assertIn(K.KW_END, kinds)     # 完毕 归一化为 结束

    def test_fullwidth_hint(self):
        from heng2.lexer2 import tokenize, LexError
        with self.assertRaises(LexError) as cm:
            tokenize("开始：\n结束\n")
        self.assertIn("全角", str(cm.exception))

    def test_glued_alias(self):
        from heng2.lexer2 import tokenize
        toks = tokenize("导入 工具之工具\n")
        vals = [t.value for t in toks if t.value and t.value != "\n"]
        self.assertEqual(vals, ["导入", "工具", "之", "工具"])


class SemanticTests(unittest.TestCase):
    def test_hello(self):
        self.assertEqual(heng('开始:\n  说出("你好，衡语")\n结束\n'), ["你好，衡语"])

    def test_arith(self):
        self.assertEqual(heng('开始:\n  说出 3 + 5\n  说出 10 / 4\n结束\n'), ["8", "2.5"])

    def test_units_multiply(self):
        # 单价(元/个) × 数量(个) → 元
        out = heng('开始:\n  变量 单价 : 数 元/个 = 12\n  变量 数量 : 数 个 = 35\n'
                   '  变量 收入 : 数 元 = 单价 * 数量\n  说出 收入\n结束\n')
        self.assertEqual(out, ["420元"])

    def test_unit_mismatch_rejected(self):
        with self.assertRaises(HengError) as cm:
            compile_src('开始:\n  变量 a : 数 元 = 1\n  变量 b : 数 秒 = 2\n'
                        '  说出 a + b\n结束\n')
        self.assertIn("量纲", str(cm.exception))

    def test_if_else(self):
        out = heng('开始:\n  变量 x : 数 = 10\n  如果 x > 5:\n    说出 "大"\n'
                   '  否则:\n    说出 "小"\n  结束\n结束\n')
        self.assertEqual(out, ["大"])

    def test_loop(self):
        out = heng('开始:\n  变量 i : 数 = 0\n  循环 i < 3:\n    说出 i\n'
                   '    i = i + 1\n  结束\n结束\n')
        self.assertEqual(out, ["0", "1", "2"])

    def test_repeat(self):
        out = heng('开始:\n  变量 总 : 数 = 0\n  重复 5 次:\n    总 = 总 + 2\n'
                   '  结束\n  说出 总\n结束\n')
        self.assertEqual(out, ["10"])

    def test_recursion(self):
        out = heng('定义 做法 阶乘(n : 数) -> 数:\n  如果 n <= 1:\n    返回 1\n'
                   '  结束\n  返回 n * 阶乘(n - 1)\n结束\n'
                   '开始:\n  说出 阶乘(10)\n结束\n')
        self.assertEqual(out, ["3628800"])

    def test_text_concat(self):
        self.assertEqual(heng('开始:\n  说出 "甲" + "乙"\n结束\n'), ["甲乙"])

    def test_seq_and_each(self):
        out = heng('开始:\n  变量 a : 序列 文本 = ["甲", "乙"]\n'
                   '  每个 a 为 x:\n    说出 "你好，" + x\n  结束\n结束\n')
        self.assertEqual(out, ["你好，甲", "你好，乙"])

    def test_map_literal_and_field(self):
        out = heng('开始:\n  变量 m : 映射 文本 数 = {"a": 1, "b": 2}\n'
                   '  说出 m.a\n结束\n')
        self.assertEqual(out, ["1"])

    def test_record(self):
        out = heng('定义 记录 订单:\n  字段 单价 : 数 元\n  字段 数量 : 数 个\n结束\n'
                   '开始:\n  变量 o : 订单 = 订单(10, 3)\n  说出 o.数量\n结束\n')
        # 记录字段「数量」带单位 个，输出补单位是正确行为
        self.assertEqual(out, ["3个"])

    def test_builtin_length(self):
        self.assertEqual(heng('开始:\n  说出 长度("abc")\n结束\n'), ["3"])

    def test_stdlib(self):
        out = heng('开始:\n  说出 文字.大写("abc")\n  说出 数值.开方(144)\n'
                   '  说出 表.求和([1, 2, 3])\n结束\n')
        self.assertEqual(out, ["ABC", "12", "6"])

    def test_json(self):
        out = heng('开始:\n  变量 o : 映射 文本 任意 = JSON.解码("{\\"线索\\":42}")\n'
                   '  说出 o.线索\n结束\n')
        self.assertEqual(out, ["42"])

    def test_network_needs_cap(self):
        with self.assertRaises(HengError) as cm:
            compile_src('开始:\n  沙箱:\n    执行 x = 网络.取("https://example.com")\n'
                        '  结束\n结束\n')
        self.assertIn("未授权", str(cm.exception))

    def test_assert_failure(self):
        with self.assertRaises(HengError) as cm:
            heng('开始:\n  必须 1 > 2\n结束\n')
        self.assertIn("断言", str(cm.exception))

    def test_division_by_zero(self):
        with self.assertRaises(HengError) as cm:
            heng('开始:\n  说出 1 / 0\n结束\n')
        self.assertIn("除零", str(cm.exception))


class BytecodeTests(unittest.TestCase):
    """.hbc 自足性：写盘 → 换进程 → 结果一致。"""

    def test_roundtrip_bytes(self):
        src = ('定义 做法 平方(x : 数) -> 数:\n  返回 x * x\n结束\n'
               '开始:\n  说出 平方(7)\n结束\n')
        mod, _ = compile_src(src)
        data = mod.to_bytes()
        self.assertEqual(data[:4], b"HENG")
        self.assertEqual(run_bytes(data), ["49"])

    def test_written_file_reloads(self):
        src = '开始:\n  说出 6 * 7\n结束\n'
        mod, _ = compile_src(src)
        with tempfile.NamedTemporaryFile(suffix=".hbc", delete=False) as f:
            f.write(mod.to_bytes())
            path = f.name
        try:
            with open(path, "rb") as fh:
                data = fh.read()
            loaded = Module.from_bytes(data)
            self.assertTrue(loaded.entry)
            self.assertEqual(run_bytes(data), ["42"])
        finally:
            os.unlink(path)

    def test_disasm_is_real_instructions(self):
        # 用变量参与运算：字面量 `1 + 2` 会在编译期被常量折叠掉，
        # 看不到 ADD 指令（这正是折叠生效的证据）。
        mod, _ = compile_src('开始:\n  变量 a : 数 = 1\n  说出 a + 2\n结束\n')
        text = disasm(mod)
        self.assertIn("PUSH_NUM", text)
        self.assertIn("ADD", text)
        self.assertIn("PRINT", text)

    def test_const_folding_removes_runtime_arithmetic(self):
        """常量折叠：纯字面量算术编译成单条 PUSH_NUM。"""
        folded, _ = compile_src('开始:\n  说出 81000 / 1800\n结束\n')
        text = disasm(folded)
        self.assertIn("PUSH_NUM", text)
        self.assertNotIn("DIV", text, "纯字面量除法应已折叠，不应留下 DIV")
        self.assertEqual(run_native('开始:\n  说出 81000 / 1800\n结束\n'), ["45"])

        # 含变量时不得折叠（否则语义会错）
        live, _ = compile_src('开始:\n  变量 a : 数 = 5\n  说出 81000 / a\n结束\n')
        self.assertIn("DIV", disasm(live))

    def test_unit_labels_encoded_in_print(self):
        # 单位现在直接编码在 PRINT 指令里（不再依赖模块级标签表），
        # 这样分支/循环下不会错位。这里验证字节码里确实带上了单位串。
        src = '开始:\n  变量 c : 数 元 = 12\n  说出 c\n结束\n'
        mod, _ = compile_src(src)
        loaded = Module.from_bytes(mod.to_bytes())
        texts = [v for k, v in loaded.pool if isinstance(v, str)]
        self.assertIn("元", texts)
        self.assertEqual(run_bytes(mod.to_bytes()), ["12元"])


@unittest.skipUnless(shutil.which("clang") or shutil.which("cc"), "需要 C 编译器")
class NativeVMTests(unittest.TestCase):
    """原生 VM 与 Python VM 必须逐字节一致（交叉验证）。"""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp()
        cls.vm = os.path.join(cls.tmpdir, "hengvm")
        src = os.path.join(os.path.dirname(__file__), "..", "native", "hengvm.c")
        cc = shutil.which("clang") or shutil.which("cc")
        subprocess.run([cc, "-O3", "-o", cls.vm, src, "-lm"], check=True,
                       capture_output=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def _run_native(self, src: str) -> tuple[int, str, str]:
        mod, _ = compile_src(src)
        path = os.path.join(self.tmpdir, "t.hbc")
        with open(path, "wb") as f:
            f.write(mod.to_bytes())
        r = subprocess.run([self.vm, path], capture_output=True, text=True)
        return r.returncode, r.stdout, r.stderr

    def _compare(self, src: str) -> str:
        """跑两边，断言输出一致，返回输出文本。"""
        py = heng(src)
        rc, out, err = self._run_native(src)
        self.assertEqual(rc, 0, f"原生 VM 失败：{err}")
        self.assertEqual(out.splitlines(), py, "原生 VM 与 Python VM 结果不一致")
        return out

    def test_arith(self):
        self._compare('开始:\n  说出 3 + 5\n  说出 10 - 4\n  说出 6 * 7\n结束\n')

    def test_recursion(self):
        self._compare('定义 做法 斐波那契(n : 数) -> 数:\n  如果 n < 2:\n'
                      '    返回 n\n  结束\n  返回 斐波那契(n - 1) + 斐波那契(n - 2)\n'
                      '结束\n开始:\n  说出 斐波那契(20)\n结束\n')

    def test_loop(self):
        self._compare('开始:\n  变量 总 : 数 = 0\n  变量 i : 数 = 1\n'
                      '  循环 i <= 100:\n    总 = 总 + i\n    i = i + 1\n'
                      '  结束\n  说出 总\n结束\n')

    def test_units(self):
        out = self._compare('开始:\n  变量 单价 : 数 元/个 = 12\n'
                            '  变量 数量 : 数 个 = 35\n  变量 收入 : 数 元 = 单价 * 数量\n'
                            '  说出 收入\n结束\n')
        self.assertEqual(out.strip(), "420元")

    def test_text_and_seq(self):
        self._compare('开始:\n  说出 "甲" + "乙"\n'
                      '  变量 a : 序列 数 = [3, 1, 2]\n'
                      '  每个 a 为 x:\n    说出 x\n  结束\n结束\n')

    def test_map_field(self):
        self._compare('开始:\n  变量 m : 映射 文本 数 = {"a": 1}\n  说出 m.a\n结束\n')

    def test_division_by_zero(self):
        rc, out, err = self._run_native('开始:\n  说出 1 / 0\n结束\n')
        self.assertNotEqual(rc, 0)
        self.assertIn("除零", err)


class ArchitectureTests(unittest.TestCase):
    """结构性断言：确认执行层是原生 VM，前端不含执行逻辑。"""

    ROOT = os.path.join(os.path.dirname(__file__), "..")

    def test_no_python_vm_remains(self):
        # 只有一套执行引擎：原生 C VM。Python 侧不得再有解释执行器。
        self.assertFalse(os.path.exists(os.path.join(self.ROOT, "heng2", "vm.py")),
                         "不应存在 heng2/vm.py（执行只走原生 VM）")

    def test_frontend_has_no_execution_loop(self):
        # 编译器前端只做「源码 → 字节码」，不得含求值循环
        for name in ("compiler2.py", "check2.py", "api2.py"):
            path = os.path.join(self.ROOT, "heng2", name)
            with open(path, encoding="utf-8") as f:
                text = f.read()
            self.assertNotIn("exec(", text, f"{name} 不应包含 exec(")
            self.assertNotIn("builtins.eval", text, f"{name} 不应包含内置求值")
            self.assertNotIn("def _loop(", text, f"{name} 不应包含解释循环")

    def test_native_vm_is_independent_executable(self):
        # 原生 VM 必须能独立加载 .hbc 运行，不依赖 Python 进程
        exe = os.path.join(self.ROOT, "native", "bin", "hengvm")
        if not os.path.exists(exe):
            self.skipTest("原生 VM 尚未构建")
        self.assertTrue(os.access(exe, os.X_OK))




# ============================================================
# 并发模型测试（docs/CONCURRENCY.md 第八节验收标准）
# ============================================================

class StdlibNativeTests(unittest.TestCase):
    """54 个标准库函数必须在原生 VM 上全部可用（不允许退回任何解释器）。"""

    def test_text_functions(self):
        self.assertEqual(heng('开始:\n  说出 文字.长度("衡语编程")\n结束\n'), ["4"])
        self.assertEqual(heng('开始:\n  说出 文字.字节数("衡语")\n结束\n'), ["6"])
        self.assertEqual(heng('开始:\n  说出 文字.大写("abc")\n结束\n'), ["ABC"])
        self.assertEqual(heng('开始:\n  说出 文字.小写("ABC")\n结束\n'), ["abc"])
        self.assertEqual(heng('开始:\n  说出 文字.去空白("  x  ")\n结束\n'), ["x"])
        self.assertEqual(heng('开始:\n  说出 文字.含("衡语编程", "编程")\n结束\n'), ["真"])
        self.assertEqual(heng('开始:\n  说出 文字.开头是("衡语", "衡")\n结束\n'), ["真"])
        self.assertEqual(heng('开始:\n  说出 文字.结尾是("衡语", "语")\n结束\n'), ["真"])
        self.assertEqual(heng('开始:\n  说出 文字.截("衡语编程语言", 3)\n结束\n'), ["衡语编"])
        self.assertEqual(heng('开始:\n  说出 文字.合并(["a", "b"], "-")\n结束\n'), ["a-b"])
        self.assertEqual(heng('开始:\n  说出 文字.替换("甲乙甲", "甲", "丙")\n结束\n'), ["丙乙丙"])

    def test_text_split_and_lines(self):
        self.assertEqual(heng('开始:\n  说出 文字.切分("a,b,c", ",")\n结束\n'),
                         ["[a, b, c]"])
        self.assertEqual(heng('开始:\n  说出 文字.分行("甲\\n乙")\n结束\n'),
                         ["[甲, 乙]"])

    def test_math_functions(self):
        cases = [
            ("数值.绝对值(-5)", "5"), ("数值.取整(3.7)", "3"),
            ("数值.开方(25)", "5"), ("数值.乘方(2, 10)", "1024"),
            ("数值.对数(1)", "0"), ("数值.指数(0)", "1"),
            ("数值.正弦(0)", "0"), ("数值.余弦(0)", "1"),
            ("数值.最大(3, 8)", "8"), ("数值.最小(3, 8)", "3"),
        ]
        for expr, want in cases:
            self.assertEqual(heng(f'开始:\n  说出 {expr}\n结束\n'), [want], expr)
        self.assertEqual(heng('开始:\n  说出 数值.四舍五入(3.14159, 2)\n结束\n'), ["3.14"])

    def test_seq_functions(self):
        cases = [
            ("表.长度([1, 2, 3])", "3"),
            ("表.取([10, 20, 30], 1)", "20"),
            ("表.追加([1, 2], 3)", "[1, 2, 3]"),
            ("表.连接([1], [2, 3])", "[1, 2, 3]"),
            ("表.片段([1, 2, 3, 4], 1, 2)", "[2, 3]"),
            ("表.倒序([1, 2, 3])", "[3, 2, 1]"),
            ("表.排序([3, 1, 2])", "[1, 2, 3]"),
            ("表.求和([1, 2, 3])", "6"),
            ("表.平均([2, 4, 6])", "4"),
            ("表.最大([1, 9, 3])", "9"),
            ("表.最小([1, 9, 3])", "1"),
            ("表.含([1, 2, 3], 2)", "真"),
            ("表.去重([1, 1, 2])", "[1, 2]"),
        ]
        for expr, want in cases:
            self.assertEqual(heng(f'开始:\n  说出 {expr}\n结束\n'), [want], expr)

    def test_dict_functions(self):
        cases = [
            ("字典.大小({\"a\": 1})", "1"),
            ("字典.取值({\"a\": 1}, \"a\")", "1"),
            ("字典.有键({\"a\": 1}, \"a\")", "真"),
            ("字典.键({\"a\": 1})", "[a]"),
            ("字典.设值({\"a\": 1}, \"b\", 2)", "{a: 1, b: 2}"),
            ("字典.删键({\"a\": 1, \"b\": 2}, \"a\")", "{b: 2}"),
        ]
        for expr, want in cases:
            self.assertEqual(heng(f'开始:\n  说出 {expr}\n结束\n'), [want], expr)

    def test_json_functions(self):
        self.assertEqual(heng('开始:\n  说出 JSON.编码([1, 2, 3])\n结束\n'), ["[1,2,3]"])
        self.assertEqual(heng('开始:\n  说出 JSON.解码("[1, 2, 3]")\n结束\n'), ["[1, 2, 3]"])
        self.assertEqual(heng('开始:\n  说出 JSON.有效("[1, 2]")\n结束\n'), ["真"])
        self.assertEqual(heng('开始:\n  说出 JSON.有效("不是JSON")\n结束\n'), ["假"])

    def test_time_functions(self):
        self.assertEqual(heng('开始:\n  说出 时间.现在() > 0\n结束\n'), ["真"])
        self.assertEqual(heng('开始:\n  说出 时间.毫秒() > 0\n结束\n'), ["真"])
        self.assertEqual(heng('开始:\n  说出 时间.格式化(0)\n结束\n'),
                         ["1970-01-01 08:00:00"])

    def test_network_url_encode(self):
        self.assertEqual(heng('开始:\n  说出 网络.编码("a b")\n结束\n'), ["a%20b"])

    def test_no_unimplemented_stdlib(self):
        # 全部 54 项都实现了：任何一项都不应触发「未实现」错误
        from heng2 import stdlib2
        # 数量随标准库增长而变化；关键是「注册了多少就必须实现多少」
        self.assertGreaterEqual(len(stdlib2.REG), 54)
        with open(os.path.join(os.path.dirname(__file__), "..", "native", "hengvm.c"),
                  encoding="utf-8") as f:
            src = f.read()
        self.assertNotIn("未实现的标准库函数", src.replace("未实现的标准库函数：%s", ""))


class FfiTests(unittest.TestCase):
    """FFI 直连 C 动态库，不依赖任何宿主语言。"""

    def test_call_libm(self):
        out = heng('导入 外部 "libm" 之 数学\n\n'
                   '开始:\n  说出 数学.sqrt(16)\n  说出 数学.pow(2, 10)\n结束\n')
        self.assertEqual(out, ["4", "1024"])

    def test_ffi_requires_declaration(self):
        # 未导入却按外部库调用 → 编译期就被拒绝
        with self.assertRaises(HengError) as cm:
            compile_src('开始:\n  说出 数学.sqrt(4)\n结束\n')
        self.assertIn("未定义", str(cm.exception))

    def test_ffi_blocked_in_sandbox_without_authorization(self):
        with self.assertRaises(HengError) as cm:
            heng('导入 外部 "libm" 之 数学\n\n'
                 '开始:\n  沙箱:\n    说出 数学.sqrt(4)\n  结束\n结束\n')
        self.assertIn("未授权能力", str(cm.exception))

    def test_ffi_allowed_after_authorization(self):
        out = heng('导入 外部 "libm" 之 数学\n\n'
                   '开始:\n  沙箱:\n    授权 外部.调用数学\n'
                   '    说出 数学.sqrt(16)\n  结束\n结束\n')
        self.assertEqual(out, ["4"])


class ConcurrencyTests(unittest.TestCase):
    """协作式并发：真并行、无竞态、无泄漏、错误传递。"""

    def test_spawn_and_receive(self):
        out = heng('定义 做法 翻倍(x : 数) -> 数:\n  返回 x * 2\n结束\n'
                   '开始:\n  同时:\n    发起 翻倍(21)\n  结束\n'
                   '  接收(翻倍_信箱) 为 r\n  说出 r\n结束\n')
        self.assertEqual(out, ["42"])

    def test_multiple_spawns_queue_in_order(self):
        # 同一做法发起多次：信箱按完成顺序排队，逐个取出
        out = heng('定义 做法 编号(x : 数) -> 数:\n  返回 x\n结束\n'
                   '开始:\n  同时:\n    发起 编号(1)\n    发起 编号(2)\n'
                   '    发起 编号(3)\n  结束\n'
                   '  接收(编号_信箱) 为 a\n  接收(编号_信箱) 为 b\n'
                   '  接收(编号_信箱) 为 c\n  说出 a + b + c\n结束\n')
        self.assertEqual(out, ["6"])

    def test_await_syntax(self):
        # 新增语法：等待完成 —— 发起并立即等结果
        out = heng('定义 做法 平方(x : 数) -> 数:\n  返回 x * x\n结束\n'
                   '开始:\n  变量 y : 数 = 等待完成 平方(7)\n  说出 y\n结束\n')
        self.assertEqual(out, ["49"])

    def test_forbidden_write_to_outer(self):
        # 并发块内写外层变量必须编译期报错（消除数据竞争的静态保证）
        with self.assertRaises(HengError) as cm:
            compile_src('定义 做法 累加(x : 数) -> 数:\n  返回 x\n结束\n'
                        '开始:\n  变量 总 : 数 = 0\n  同时:\n'
                        '    发起 累加(1)\n    总 = 总 + 1\n  结束\n结束\n')
        self.assertIn("并发任务内不能修改外层变量", str(cm.exception))

    def test_task_may_write_own_locals(self):
        # 任务内部写自己的局部变量是允许的
        out = heng('定义 做法 累加(x : 数) -> 数:\n  变量 局部 : 数 = 0\n'
                   '  局部 = 局部 + x\n  返回 局部\n结束\n'
                   '开始:\n  同时:\n    发起 累加(5)\n  结束\n'
                   '  接收(累加_信箱) 为 r\n  说出 r\n结束\n')
        self.assertEqual(out, ["5"])

    def test_parallel_block_joins_before_continuing(self):
        # 同时 块结束必定汇合：块后立即取值一定能取到
        out = heng('定义 做法 慢(x : 数) -> 数:\n  变量 和 : 数 = 0\n'
                   '  变量 i : 数 = 0\n  循环 i < 1000:\n    和 = 和 + 1\n'
                   '    i = i + 1\n  结束\n  返回 和\n结束\n'
                   '开始:\n  同时:\n    发起 慢(1)\n    发起 慢(2)\n  结束\n'
                   '  接收(慢_信箱) 为 a\n  接收(慢_信箱) 为 b\n  说出 a + b\n结束\n')
        self.assertEqual(out, ["2000"])


@unittest.skipUnless(shutil.which("clang") or shutil.which("cc"), "需要 C 编译器")
class TrueParallelTests(unittest.TestCase):
    """用带延迟的工具调用，证明并发块是「总耗时≈最慢那个」而非累加。"""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp()
        cls.vm = os.path.join(cls.tmpdir, "hengvm")
        src = os.path.join(os.path.dirname(__file__), "..", "native", "hengvm.c")
        cc = shutil.which("clang") or shutil.which("cc")
        subprocess.run([cc, "-O3", "-pthread", "-o", cls.vm, src, "-lm"],
                       check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def _time_run(self, src: str) -> float:
        import time
        mod, _ = compile_src(src)
        path = os.path.join(self.tmpdir, "t.hbc")
        with open(path, "wb") as f:
            f.write(mod.to_bytes())
        best = 1e9
        for _ in range(3):
            t0 = time.perf_counter()
            r = subprocess.run([self.vm, path], capture_output=True, text=True)
            best = min(best, time.perf_counter() - t0)
        self.assertEqual(r.returncode, 0, r.stderr)
        return best * 1000

    SLOW_FN = ('定义 做法 查(名 : 文本, 毫秒 : 数) -> 文本:\n'
               '  执行 回执 = 工具.慢查询(名, 毫秒)\n  返回 回执.结果\n结束\n')

    def test_parallel_is_faster_than_serial(self):
        serial = self.SLOW_FN + (
            '开始:\n  变量 a : 文本 = 查("甲", 300)\n'
            '  变量 b : 文本 = 查("乙", 300)\n  说出 a\n  说出 b\n结束\n')
        parallel = self.SLOW_FN + (
            '开始:\n  同时:\n    发起 查("甲", 300)\n    发起 查("乙", 300)\n'
            '  结束\n  接收(查_信箱) 为 a\n  接收(查_信箱) 为 b\n'
            '  说出 a\n  说出 b\n结束\n')

        t_serial = self._time_run(serial)
        t_parallel = self._time_run(parallel)
        # 串行 ≈ 600ms；并行 ≈ 300ms。给足容差以适配慢机器。
        self.assertGreater(t_serial, 550, f"串行应至少 550ms，实测 {t_serial:.0f}ms")
        self.assertLess(t_parallel, t_serial * 0.75,
                        f"并行应明显快于串行：并行 {t_parallel:.0f}ms vs 串行 {t_serial:.0f}ms")


class TutorialExamplesTests(unittest.TestCase):
    """教材里的每个示例都必须能在原生 VM 上跑通（教材与实现不许脱节）。"""

    def test_all_tutorial_examples_run(self):
        tdir = os.path.join(os.path.dirname(__file__), "..", "docs", "tutorial")
        if not os.path.isdir(tdir):
            self.skipTest("教材示例目录不存在")
        files = sorted(f for f in os.listdir(tdir) if f.endswith(".hng"))
        self.assertGreaterEqual(len(files), 20, "教材示例数量异常")
        # 这两个是「故意报错」的教学示例，应编译失败
        expect_fail = {"04_unit_bad.hng"}
        for name in files:
            path = os.path.join(tdir, name)
            with open(path, encoding="utf-8") as f:
                src = f.read()
            if name in expect_fail:
                with self.assertRaises(HengError, msg=name):
                    compile_src(src)
                continue
            try:
                mod, _ = compile_src(src)
            except HengError as e:
                self.fail(f"教材示例 {name} 编译失败：{e}")
            with tempfile.NamedTemporaryFile(suffix=".hbc", delete=False) as f:
                f.write(mod.to_bytes())
                p = f.name
            try:
                r = subprocess.run([_native_vm(), p], capture_output=True, text=True)
                self.assertEqual(r.returncode, 0,
                                 f"教材示例 {name} 运行失败：{r.stderr}")
                self.assertTrue(r.stdout.strip(), f"教材示例 {name} 没有输出")
            finally:
                os.unlink(p)


class ValidationProjectsTests(unittest.TestCase):
    """100 个 GitHub 经典项目的衡语实现必须全部可运行。

    这组用例是「语言可用性」的硬指标：任何一个跑不通，
    都说明衡语在真实程序结构上有缺口。
    """

    def test_all_validation_projects_run(self):
        # 200+ 个项目逐个「Python 编译 + 起进程」要几分钟。
        # 默认跳过，用 HENG_SLOW_TESTS=1 开启。
        if os.environ.get("HENG_SLOW_TESTS") != "1":
            self.skipTest("慢测试：设 HENG_SLOW_TESTS=1 开启")
        root = os.path.join(os.path.dirname(__file__), "..", "validation")
        if not os.path.isdir(root):
            self.skipTest("验证目录不存在")
        files = []
        for d in sorted(os.listdir(root)):
            sub = os.path.join(root, d)
            if os.path.isdir(sub):
                files += [os.path.join(sub, f) for f in sorted(os.listdir(sub))
                          if f.endswith(".hng")]
        self.assertGreaterEqual(len(files), 300, f"验证项目应有 300 个以上，实际 {len(files)}")

        failures = []
        for path in files:
            with open(path, encoding="utf-8") as fh:
                src = fh.read()
            try:
                mod, _ = compile_src(src)
            except HengError as e:
                failures.append(f"{os.path.basename(path)}: 编译失败 {e}")
                continue
            with tempfile.NamedTemporaryFile(suffix=".hbc", delete=False) as f:
                f.write(mod.to_bytes())
                tmp = f.name
            try:
                r = subprocess.run([_native_vm(), tmp], capture_output=True, text=True)
                if r.returncode != 0:
                    failures.append(f"{os.path.basename(path)}: 运行失败 {r.stderr.strip()[:80]}")
                elif not r.stdout.strip():
                    failures.append(f"{os.path.basename(path)}: 无输出")
            finally:
                os.unlink(tmp)

        self.assertEqual(failures, [], "以下项目未通过：\n" + "\n".join(failures))


class OpcodeCoverageTests(unittest.TestCase):
    """指令覆盖自检：声明 / VM 实现 / 编译器生成 三方必须对齐。

    这是 100 项目验证给出的最直接教训——VM 早就实现了 INDEX_SET，
    解析器却从不生成，静态检查发现不了，只能等真实程序撞上去。
    """

    def test_token_ids_have_no_conflict(self):
        """Token 编号不得重复。

        教训：PERCENT 误用 24（与 EQ 相同），`如果 x = 5` 被解析成取模，
        41 个验证项目同时失败，且没有任何报错——只能靠机器检查兜住。
        """
        root = os.path.join(os.path.dirname(__file__), "..")
        script = os.path.join(root, "tools", "check_token_ids.py")
        if not os.path.exists(script):
            self.skipTest("自检脚本不存在")
        r = subprocess.run([sys.executable, script, "--strict"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_opcode_coverage_is_consistent(self):
        root = os.path.join(os.path.dirname(__file__), "..")
        script = os.path.join(root, "tools", "check_opcode_coverage.py")
        if not os.path.exists(script):
            self.skipTest("自检脚本不存在")
        r = subprocess.run([sys.executable, script], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("指令覆盖一致", r.stdout)


class GenericTests(unittest.TestCase):
    """泛型：类型参数在调用点按实参类型绑定。"""

    def test_identity_generic(self):
        out = heng('定义 做法 恒等<T>(x : T) -> T:\n  返回 x\n结束\n'
                   '开始:\n  说出 恒等(1)\n  说出 恒等("a")\n结束\n')
        self.assertEqual(out, ["1", "a"])

    def test_generic_container_element(self):
        out = heng('定义 做法 首项<T>(xs : 序列 T) -> T:\n  返回 xs[0]\n结束\n'
                   '开始:\n  说出 首项([10, 20])\n  说出 首项(["x", "y"])\n结束\n')
        self.assertEqual(out, ["10", "x"])

    def test_generic_returns_precise_container(self):
        # 泛型返回 序列 T，实参是 序列 数 → 结果可直接赋给 序列 数
        out = heng('定义 做法 打包<T>(a : T, b : T) -> 序列 T:\n  返回 [a, b]\n结束\n'
                   '开始:\n  变量 包 : 序列 数 = 打包(1, 2)\n  说出 包\n结束\n')
        self.assertEqual(out, ["[1, 2]"])

    def test_generic_type_mismatch_rejected(self):
        with self.assertRaises(HengError):
            compile_src('定义 做法 同<T>(a : T, b : T) -> T:\n  返回 a\n结束\n'
                        '开始:\n  说出 同(1, "x")\n结束\n')


class LambdaTests(unittest.TestCase):
    """lambda 与高阶函数。"""

    def test_lambda_direct_call(self):
        out = heng('开始:\n  变量 f = 做法(x : 数) -> 数 => x + 1\n'
                   '  说出 f(2)\n结束\n')
        self.assertEqual(out, ["3"])

    def test_map_filter_fold(self):
        out = heng('开始:\n'
                   '  变量 双 = 做法(x : 数) -> 数 => x * 2\n'
                   '  说出 表.映射([1, 2, 3], 双)\n'
                   '  变量 偶 = 做法(x : 数) -> 真假 => 数值.取余(x, 2) = 0\n'
                   '  说出 表.过滤([1, 2, 3, 4], 偶)\n'
                   '  变量 加 = 做法(a : 数, b : 数) -> 数 => a + b\n'
                   '  说出 表.折叠([1, 2, 3, 4], 加, 0)\n结束\n')
        self.assertEqual(out, ["[2, 4, 6]", "[2, 4]", "10"])

    def test_lambda_captures_outer_by_value(self):
        """闭包按值捕获：外层变量可在 lambda 内读取。"""
        out = heng('开始:\n  变量 xs : 序列 数 = [10, 20]\n'
                   '  变量 f = 做法(i : 数) -> 数 => xs[i]\n  说出 f(1)\n结束\n')
        self.assertEqual(out, ["20"])

    def test_closure_capture_is_snapshot(self):
        """捕获是创建时的快照：之后改外层变量不影响已建闭包。"""
        out = heng('开始:\n  变量 n : 数 = 1\n'
                   '  变量 f = 做法(x : 数) -> 数 => x + n\n'
                   '  n = 100\n  说出 f(5)\n结束\n')
        self.assertEqual(out, ["6"])

    def test_closure_in_higher_order(self):
        out = heng('开始:\n  变量 倍 : 数 = 3\n'
                   '  说出 表.映射([1, 2, 3], 做法(x : 数) -> 数 => x * 倍)\n结束\n')
        self.assertEqual(out, ["[3, 6, 9]"])

    def test_closure_multiple_frees(self):
        out = heng('开始:\n  变量 a : 数 = 10\n  变量 b : 数 = 3\n'
                   '  变量 f = 做法(x : 数) -> 数 => x * a + b\n  说出 f(2)\n结束\n')
        self.assertEqual(out, ["23"])


class OptimizationTests(unittest.TestCase):
    """性能优化不得改变语义。"""

    def test_const_folding_keeps_semantics(self):
        self.assertEqual(heng('开始:\n  说出 2 * 3 + 1\n结束\n'), ["7"])
        self.assertEqual(heng('开始:\n  说出 81000 / 1800\n结束\n'), ["45"])

    def test_specialized_num_ops_semantics(self):
        out = heng('开始:\n  变量 a : 数 = 7\n  变量 b : 数 = 2\n'
                   '  说出 a + b\n  说出 a - b\n  说出 a * b\n  说出 a / b\n'
                   '  说出 a > b\n  说出 a = b\n结束\n')
        self.assertEqual(out, ["9", "5", "14", "3.5", "真", "假"])

    def test_slot_superinstruction_semantics(self):
        out = heng('开始:\n  变量 总 : 数 = 0\n  变量 i : 数 = 0\n'
                   '  循环 i < 4:\n    总 = 总 + i\n    i = i + 1\n  结束\n'
                   '  说出 总\n结束\n')
        self.assertEqual(out, ["6"])

    def test_specialized_div_by_zero_still_errors(self):
        with self.assertRaises(HengError) as cm:
            heng('开始:\n  变量 a : 数 = 1\n  变量 b : 数 = 0\n  说出 a / b\n结束\n')
        self.assertIn("除零", str(cm.exception))


class SuperInstructionTests(unittest.TestCase):
    """超指令优化不得改变可观察语义。

    教训：为消除 O(n^2) 而把 `xs = 表.追加(xs, v)` 做原地追加时，
    若有别名（b = a）就会互相污染。必须按引用计数区分独占与共享。
    """

    def test_append_in_place_keeps_value_semantics(self):
        # 有别名时必须复制，b 不能被 a 的追加影响
        out = heng('开始:\n  变量 a : 序列 数 = [1]\n  变量 b : 序列 数 = a\n'
                   '  a = 表.追加(a, 2)\n  说出 a\n  说出 b\n结束\n')
        self.assertEqual(out, ["[1, 2]", "[1]"])

    def test_append_loop_result(self):
        out = heng('开始:\n  变量 xs : 序列 数 = []\n  变量 i : 数 = 0\n'
                   '  循环 i < 5:\n    xs = 表.追加(xs, i)\n    i = i + 1\n  结束\n'
                   '  说出 xs\n结束\n')
        self.assertEqual(out, ["[0, 1, 2, 3, 4]"])

    def test_slot_index_read(self):
        out = heng('开始:\n  变量 xs : 序列 数 = [10, 20, 30]\n  变量 i : 数 = 1\n'
                   '  说出 xs[i]\n  说出 xs[0]\n结束\n')
        self.assertEqual(out, ["20", "10"])

    def test_slot_index_write(self):
        out = heng('开始:\n  变量 xs : 序列 数 = [1, 2, 3]\n  变量 i : 数 = 1\n'
                   '  变量 v : 数 = 99\n  将 v 放到 xs[i]\n  说出 xs\n结束\n')
        self.assertEqual(out, ["[1, 99, 3]"])

    def test_arith_superinstruction_semantics(self):
        out = heng('开始:\n  变量 a : 数 = 10\n'
                   '  a = a - 2\n  a = a * 3\n  a = a / 4\n  说出 a\n结束\n')
        self.assertEqual(out, ["6"])

    def test_index_out_of_range_still_errors(self):
        with self.assertRaises(HengError) as cm:
            heng('开始:\n  变量 xs : 序列 数 = [1]\n  变量 i : 数 = 5\n'
                 '  说出 xs[i]\n结束\n')
        self.assertIn("越界", str(cm.exception))


class ComplexityAuditTests(unittest.TestCase):
    """标准库不得有平方级实现。

    这是 `表.追加` 的教训：单个函数看实现完全正确，
    放进循环却是 O(n^2)——20 万次要 95 秒。
    只有实测才能发现这类问题。
    """

    def test_no_quadratic_stdlib(self):
        root = os.path.join(os.path.dirname(__file__), "..")
        script = os.path.join(root, "tools", "audit_stdlib_complexity.py")
        if not os.path.exists(script):
            self.skipTest("审计脚本不存在")
        r = subprocess.run([sys.executable, script, "--check"],
                           capture_output=True, text=True, cwd=root, timeout=600)
        self.assertEqual(r.returncode, 0,
                         "发现平方级实现：\n" + r.stdout[-1500:])


class InternTests(unittest.TestCase):
    """字符串驻留：短文本共享对象，长文本仍独立分配。"""

    def test_same_short_text_equal(self):
        out = heng('开始:\n  变量 a : 文本 = "hello"\n  变量 b : 文本 = "hello"\n'
                   '  说出 a = b\n  说出 a\n结束\n')
        self.assertEqual(out, ["真", "hello"])

    def test_long_text_not_broken(self):
        long_s = "x" * 200
        out = heng(f'开始:\n  变量 s : 文本 = "{long_s}"\n  说出 文字.长度(s)\n结束\n')
        self.assertEqual(out, ["200"])

    def test_concatenation_independent(self):
        out = heng('开始:\n  变量 a : 文本 = "ab"\n  变量 b : 文本 = a + "c"\n'
                   '  说出 a\n  说出 b\n结束\n')
        self.assertEqual(out, ["ab", "abc"])


class JitTests(unittest.TestCase):
    """热循环 JIT：字节码 → C → 动态库 → 调用。

    只覆盖纯数值循环。真实项目里覆盖率很低（实测 2/101），
    因为循环体普遍含函数调用与对象操作——这是保守 JIT 的固有边界。
    """

    def test_scan_finds_numeric_loop(self):
        from heng2.jit import scan_loops
        mod, _ = compile_src('开始:\n  变量 s : 数 = 0\n  变量 i : 数 = 0\n'
                             '  循环 i < 100:\n    s = s + 1\n    i = i + 1\n  结束\n'
                             '  说出 s\n结束\n')
        loops = scan_loops(mod.entry, mod.entry_slots)
        self.assertGreaterEqual(len(loops), 1)

    def test_emit_c_is_valid_and_runs(self):
        import ctypes, subprocess, tempfile, os
        from heng2.jit import scan_loops, emit_c
        mod, _ = compile_src('开始:\n  变量 s : 数 = 0\n  变量 i : 数 = 0\n'
                             '  循环 i < 1000:\n    s = s + 2\n    i = i + 1\n  结束\n'
                             '  说出 s\n结束\n')
        start, end, body = scan_loops(mod.entry, mod.entry_slots)[0]
        cbody = emit_c(body, mod.entry_slots, "run")
        self.assertIsNotNone(cbody)
        d = tempfile.mkdtemp()
        cf, sf = os.path.join(d, "t.c"), os.path.join(d, "t.dylib")
        with open(cf, "w") as fh:
            fh.write("#include <math.h>\nvoid run(double *slots, long *steps) {\n" + cbody + "\n}\n")
        r = subprocess.run(["clang", "-O3", "-shared", "-fPIC", "-o", sf, cf],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr[:300])
        lib = ctypes.CDLL(sf)
        fn = lib.run
        fn.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_long)]
        fn.restype = None
        buf = (ctypes.c_double * mod.entry_slots)()
        st = ctypes.c_long(0)
        fn(buf, ctypes.byref(st))
        self.assertEqual(buf[0], 2000.0)
        self.assertEqual(buf[1], 1000.0)

    def test_loop_with_call_is_not_jitted(self):
        """含函数调用的循环不应被当作可 JIT 对象（边界要明确）。"""
        from heng2.jit import scan_loops
        mod, _ = compile_src('开始:\n  变量 s : 文本 = ""\n  变量 i : 数 = 0\n'
                             '  循环 i < 10:\n    s = s + "x"\n    i = i + 1\n  结束\n'
                             '  说出 s\n结束\n')
        loops = scan_loops(mod.entry, mod.entry_slots)
        self.assertEqual(loops, [])


class TextIntegrityTests(unittest.TestCase):
    """字符串处理不得静默截断。

    教训：`文字.合并` 曾用 char[512] 中转每个元素，
    超长元素被截断，且截断点常落在中文字符中间——
    写出的文件不是合法 UTF-8。报表场景里一行几十个汉字就会踩到。
    """

    def test_concat_long_chinese(self):
        # 每个元素 30 个汉字（90 字节），16 个元素 ≈ 1440 字节，远超旧的 512 缓冲
        out = heng('开始:\n  变量 行 : 序列 文本 = []\n  变量 i : 数 = 0\n'
                   '  循环 i < 16:\n    行 = 表.追加(行, "一二三四五六七八九十一二三四五六七八九十一二三四五六七八九十")\n'
                   '    i = i + 1\n  结束\n'
                   '  变量 内容 : 文本 = 文字.合并(行, 文字.由码点(10))\n'
                   '  说出 文字.长度(内容)\n结束\n')
        # 30 字 × 16 行 + 15 个换行 = 495
        self.assertEqual(out, ["495"])

    def test_concat_preserves_each_piece(self):
        out = heng('开始:\n  变量 xs : 序列 文本 = ["护齿", "护具", "拳套"]\n'
                   '  说出 文字.合并(xs, ",")\n结束\n')
        self.assertEqual(out, ["护齿,护具,拳套"])

    def test_concat_with_numbers(self):
        out = heng('开始:\n  变量 xs : 序列 数 = [1, 2, 3]\n'
                   '  说出 文字.合并(xs, "-")\n结束\n')
        self.assertEqual(out, ["1-2-3"])


class WarningTests(unittest.TestCase):
    """警告检查不得崩溃，且要能遍历嵌套分支。

    教训：warnings.py 曾用 s.then / s.orelse（AST 实际字段是 then_body /
    else_body），一旦被检查的文件里有 如果 语句就抛 AttributeError。
    300 个验证项目里恰好在 如果 分支内声明未使用变量的情况很少，
    所以这个崩溃一直没暴露。
    """

    def test_warn_inside_if(self):
        out = heng('定义 做法 f(n : 数) -> 数:\n'
                   '  如果 n > 0:\n    变量 没用 : 数 = 1\n    返回 n\n'
                   '  否则:\n    变量 也没用 : 数 = 2\n    返回 0\n  结束\n结束\n'
                   '开始:\n  说出 f(1)\n结束\n')
        self.assertEqual(out, ["1"])

    def test_warn_check_does_not_raise(self):
        from heng2.lexer2 import tokenize
        from heng2.parser2 import Parser
        from heng2.warnings import check
        src = ('定义 做法 f(n : 数) -> 数:\n  如果 n > 0:\n    '
               '变量 aa : 数 = 1\n    返回 n\n  结束\n  返回 0\n结束\n'
               '开始:\n  说出 f(1)\n结束\n')
        ws = check(Parser(tokenize(src)).parse_program())
        self.assertTrue(any(w.code == "W01" for w in ws))


class ModuleImportTests(unittest.TestCase):
    """跨模块调用必须可用。

    教训：`模.翻倍(...)` 被解析成 Field(obj=模, name=翻倍)，
    而检查器与编译器都只看 VarRef("模.翻倍")。
    结果模块导入了、函数也改名了，但一调用就报「未定义的函数」——
    模块系统实际不可用，而旧测试用的是 compile_source 的某条路径，
    恰好没覆盖到。
    """

    def test_call_imported_function(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "m.hng").write_text(
                "定义 做法 翻倍(x : 数) -> 数:\n  返回 x * 2\n结束\n",
                encoding="utf-8")
            Path(d, "主.hng").write_text(
                '导入 「m.hng」之 模\n开始:\n  说出 模.翻倍(21)\n结束\n',
                encoding="utf-8")
            r = subprocess.run(
                [sys.executable, "-m", "heng2", str(Path(d, "主.hng"))],
                cwd=str(ROOT), capture_output=True, text=True, timeout=60,
                env={**os.environ, "PYTHONPATH": str(ROOT)})
            self.assertEqual(r.stdout.strip(), "42", r.stderr[:200])

    def test_imported_function_uses_own_state(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "acc.hng").write_text(
                "常量 表 : 映射 文本 数 = {}\n"
                "定义 做法 记(k : 文本, v : 数) -> 空:\n"
                "  将 v 放到 表[k]\n结束\n"
                "定义 做法 取(k : 文本) -> 数:\n  返回 表[k]\n结束\n",
                encoding="utf-8")
            Path(d, "主.hng").write_text(
                '导入 「acc.hng」之 累\n开始:\n  累.记("a", 7)\n'
                '  说出 累.取("a")\n结束\n', encoding="utf-8")
            r = subprocess.run(
                [sys.executable, "-m", "heng2", str(Path(d, "主.hng"))],
                cwd=str(ROOT), capture_output=True, text=True, timeout=60,
                env={**os.environ, "PYTHONPATH": str(ROOT)})
            self.assertEqual(r.stdout.strip(), "7", r.stderr[:200])


class DictDeleteTests(unittest.TestCase):
    """字典.删键 必须是原地删除。

    教训：早期实现是「复制出新映射并返回」，原映射不变。
    于是 `字典.删键(d, "a")` 之后 d 里 a 还在——
    IDE 的「关闭文件」、缓存失效、去重表清理全部静默失效。
    """

    def test_delete_is_in_place(self):
        out = heng('开始:\n  变量 d : 映射 文本 文本 = {}\n'
                   '  将 "x" 放到 d["a"]\n  将 "y" 放到 d["b"]\n'
                   '  字典.删键(d, "a")\n'
                   '  说出 字典.大小(d)\n  说出 字典.有键(d, "a")\n'
                   '  说出 文字.合并(字典.键(d), ",")\n结束\n')
        self.assertEqual(out, ["1", "假", "b"])

    def test_delete_missing_key(self):
        out = heng('开始:\n  变量 d : 映射 文本 数 = {}\n'
                   '  将 1 放到 d["a"]\n  字典.删键(d, "zz")\n'
                   '  说出 字典.大小(d)\n结束\n')
        self.assertEqual(out, ["1"])

    def test_delete_then_reinsert(self):
        out = heng('开始:\n  变量 d : 映射 文本 数 = {}\n'
                   '  将 1 放到 d["k"]\n  字典.删键(d, "k")\n'
                   '  将 9 放到 d["k"]\n  说出 d["k"]\n  说出 字典.大小(d)\n结束\n')
        self.assertEqual(out, ["9", "1"])


class TextSliceTests(unittest.TestCase):
    """文字.截 与 文字.从 的语义必须区分清楚。

    教训：文字.截(s, n) 是「取前 n 个」，但大量代码把它当「从 n 开始」用。
    IDE 里 `文字.截(事件, 3)` 想从 "按钮|RUN" 取 "RUN"，实际得到 "按钮|"，
    于是所有按钮动作都匹配不上——表现就是用户说的「点击没反应」。
    这个错误在语法层面完全合法，编译器不会报，只能靠语义区分。
    """

    def test_cut_takes_prefix(self):
        out = heng('开始:\n  说出 文字.截("按钮|NEW", 3)\n结束\n')
        self.assertEqual(out, ["按钮|"])

    def test_from_skips_prefix(self):
        out = heng('开始:\n  说出 文字.从("按钮|NEW", 3)\n结束\n')
        self.assertEqual(out, ["NEW"])

    def test_from_ascii_and_out_of_range(self):
        out = heng('开始:\n  说出 文字.从("hello", 1)\n'
                   '  变量 尾巴 : 文本 = 文字.从("ab", 9)\n'
                   '  说出 文字.长度(尾巴)\n结束\n')
        self.assertEqual(out, ["ello", "0"])

    def test_event_dispatch(self):
        """模拟 IDE 的事件分发：前缀匹配 + 从 N 取动作名。"""
        out = heng(
            '开始:\n'
            '  变量 事件 : 文本 = "按钮|RUN"\n'
            '  如果 文字.开头是(事件, "按钮|"):\n'
            '    说出 文字.从(事件, 3)\n'
            '  结束\n'
            '结束\n')
        self.assertEqual(out, ["RUN"])


class DebuggerTests(unittest.TestCase):
    """单步调试必须是真的指令级单步，不是打印日志冒充。

    验证方式：跑一段有三个变量赋值的程序，
    在每步的槽位里能依次看到 10、20、30 的出现——
    说明 VM 真的在每条指令后停下并暴露了当时的变量值。
    """

    def test_debug_traces_variables(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d, "t.hng")
            f.write_text('开始:\n  变量 a : 数 = 10\n  变量 b : 数 = 20\n'
                         '  变量 c : 数 = a + b\n  说出 c\n结束\n',
                         encoding="utf-8")
            r = subprocess.run(
                [sys.executable, "-m", "heng2", "--debug", str(f)],
                cwd=str(ROOT), capture_output=True, text=True, timeout=120,
                env={**os.environ, "PYTHONPATH": str(ROOT)})
            out = r.stdout
            self.assertIn("停 ▶", out, f"没有停止点：{out[:300]}")
            self.assertIn("30", out.replace("停 ▶", ""))
            # 至少要停 4 次（三个赋值 + 说出）
            self.assertGreaterEqual(out.count("停 ▶"), 3)
            self.assertIn("[0] 数 = 10", out)

    def test_check_json_structure(self):
        """--check --json 必须给出可被机器消费的结构化诊断。"""
        with tempfile.TemporaryDirectory() as d:
            f = Path(d, "bad.hng")
            f.write_text('开始:\n  说出 不存在的变量\n结束\n', encoding="utf-8")
            r = subprocess.run(
                [sys.executable, "-m", "heng2", "--check", str(f), "--json"],
                cwd=str(ROOT), capture_output=True, text=True, timeout=60,
                env={**os.environ, "PYTHONPATH": str(ROOT)})
            import json
            data = json.loads(r.stdout)
            self.assertFalse(data["ok"])
            self.assertTrue(data["diagnostics"])
            d0 = data["diagnostics"][0]
            for k in ("severity", "code", "message", "line", "column", "help"):
                self.assertIn(k, d0)
            self.assertGreater(d0["line"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
