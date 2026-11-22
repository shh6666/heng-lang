"""衡语测试套件（标准库 unittest，无需第三方依赖）。"""

import os
import unittest

from heng.lexer import Lexer, LexError
from heng.parser import Parser, ParseError
from heng.typecheck import TypeChecker, TypeError_
from heng.interpreter import Runtime, HError
from heng.api import compile_source, CompileError

EXAMPLES = os.path.join(os.path.dirname(__file__), "..", "examples")


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def load(name: str) -> str:
    with open(os.path.join(EXAMPLES, name), encoding="utf-8") as f:
        return f.read()


class LexerTests(unittest.TestCase):
    def test_chinese_max_match(self):
        toks = [(t.kind, t.value) for t in Lexer(load("order.hng")).tokens()]
        self.assertIn(("定义", "定义"), toks)
        # 示例已统一为规范词
        self.assertIn(("每个", "每个"), toks)
        self.assertIn(("放到", "放到"), toks)
        self.assertIn(("结束", "结束"), toks)

    def test_legacy_alias_normalized(self):
        # 旧词仍可编译，且被归一化为规范种类
        toks = [(t.kind, t.value) for t in Lexer("遍历 列表 为 项\n完毕\n").tokens()]
        self.assertIn(("每个", "遍历"), toks)
        self.assertIn(("结束", "完毕"), toks)

    def test_disambiguation_single_char(self):
        # 「为」是合法关键字；「道/法/行/是/在」等单字不作关键字
        toks = [(t.kind, t.value) for t in Lexer("遍历 列表 为 项\n").tokens()]
        self.assertIn(("为", "为"), toks)
        toks2 = [(t.kind, t.value) for t in Lexer("道 法 行 是 在\n").tokens()]
        self.assertTrue(all(k == "IDENT" for k, v in toks2 if k != "NEWLINE" and k != "EOF"))

    def test_unknown_char(self):
        with self.assertRaises(LexError):
            Lexer("@#$").tokens()

    def test_fullwidth_space(self):
        # 全角空格 U+3000 在中文文本中常见，须当作空白（bug 修复回归）
        toks = [(t.kind, t.value) for t in Lexer("真　假").tokens()]
        self.assertEqual([k for k, v in toks if k not in ("EOF",)], ["真", "假"])

    def test_string_escape(self):
        toks = [(t.kind, t.value) for t in Lexer('"a\\nb\\t\\"c"').tokens()]
        self.assertEqual(toks[0][1], 'a\nb\t"c')


class BugRegressionTests(unittest.TestCase):
    """逐模块压力探测发现的真实 bug 修复回归。"""

    def test_empty_seq_literal_assign(self):
        # 空序列字面量应可赋给任意具体序列类型（修复前误拒）
        r = compile_source('入口:\n 变量 s : 序列<数> = []\n 输出(s)\n完毕\n')
        self.assertEqual(r.output, ["[]"])

    def test_empty_map_literal_assign(self):
        r = compile_source('入口:\n 变量 m : 映射<文本,数> = {}\n 输出(m)\n完毕\n')
        self.assertEqual(r.output, ["{}"])

    def test_for_each_map_yields_key_text(self):
        # 遍历映射应得到「键」（文本），不得暴露 Python HValue repr
        r = compile_source('入口:\n 变量 m : 映射<文本,数> = {"甲":1,"乙":2}\n 遍历 m 为 k:\n  输出(k)\n 完毕\n完毕\n')
        self.assertEqual(r.output, ["甲", "乙"])

    def test_recursion(self):
        r = compile_source('定义 函数 f(n : 数) -> 数:\n 倘若 n <= 1:\n  返回 n\n 完毕\n 返回 f(n-1) + f(n-2)\n完毕\n入口:\n 输出(f(10))\n完毕\n')
        self.assertEqual(r.output, ["55"])

    def test_comparison_not_confused_with_unit(self):
        # 关键回归：`1 < 2` 曾因 `<` 被当单位起始符而解析失败
        r = compile_source('入口:\n 变量 x : 数 = 3\n 倘若 x > 1 并且 x < 5:\n  输出("区间")\n 完毕\n完毕\n')
        self.assertEqual(r.output, ["区间"])
        # 单位标注仍须正常
        r2 = compile_source('入口:\n 变量 a : 数<元> = 1<元>\n 输出(a)\n完毕\n')
        self.assertEqual(r2.output, ["1元"])

    def test_nested_function_rejected(self):
        with self.assertRaises(ParseError):
            Parser(Lexer('入口:\n 定义 函数 g() -> 数:\n  返回 1\n 完毕\n完毕\n').tokens()).parse_program()

    def test_shadowing_rejected(self):
        with self.assertRaises(TypeError_):
            TypeChecker(Parser(Lexer('入口:\n 变量 a : 数 = 1\n 变量 a : 数 = 2\n 输出(a)\n完毕\n').tokens()).parse_program()).check()

    def test_unclosed_block_rejected(self):
        with self.assertRaises(ParseError):
            Parser(Lexer('入口:\n 输出("x")\n').tokens()).parse_program()


class OptimizerTests(unittest.TestCase):
    """常量折叠 pass：语义保持 + 折叠统计。"""

    SRC = '''定义 函数 计算() -> 数:
 变量 a : 数 = 2 + 3 * 4
 变量 b : 数 = -a + 10
 倘若 1 = 2:
  返回 999
 否则:
  返回 b
 完毕
完毕
入口:
 输出(计算())
完毕
'''

    def test_fold_preserves_semantics(self):
        normal = compile_source(self.SRC)
        opt = compile_source(self.SRC, optimize=True)
        self.assertEqual(normal.output, opt.output)
        self.assertEqual(opt.output, ["-4"])

    def test_fold_counts(self):
        opt = compile_source(self.SRC, optimize=True)
        self.assertGreaterEqual(opt.folded, 3)

    def test_fold_keeps_divzero_semantics(self):
        # 保守策略：1/0 不折叠，保留运行时 错误<除零>
        src = ('入口:\n 尝试:\n  变量 x : 数 = 1 / 0\n  输出(x)\n'
               ' 捕获 错误<除零> 为 e:\n  输出("捕获")\n 完毕\n完毕\n')
        r = compile_source(src, optimize=True)
        self.assertEqual(r.output, ["捕获"])

    def test_no_fold_when_disabled(self):
        r = compile_source(self.SRC)
        self.assertEqual(r.folded, 0)


class LogicOperatorTests(unittest.TestCase):
    """G1 逻辑运算符：并且/或者（短路 + 优先级 + 类型检查 + wasm）。"""

    def test_basic_and_or(self):
        src = '入口:\n 倘若 真 并且 假:\n  输出("错")\n 否则:\n  倘若 真 或者 假:\n   输出("OK")\n  完毕\n 完毕\n完毕\n'
        self.assertEqual(compile_source(src).output, ["OK"])

    def test_precedence(self):
        # 真 或者 (假 并且 假) = 真
        src = '入口:\n 倘若 真 或者 假 并且 假:\n  输出("OK")\n 完毕\n完毕\n'
        self.assertEqual(compile_source(src).output, ["OK"])

    def test_short_circuit(self):
        # 假 并且 (1/0) 不应触发除零——右侧不被求值
        src = '入口:\n 倘若 假 并且 (1 / 0) = 0:\n  输出("x")\n 完毕\n 输出("短路OK")\n完毕\n'
        self.assertEqual(compile_source(src).output, ["短路OK"])

    def test_type_error(self):
        src = '入口:\n 倘若 1 并且 真:\n  输出("x")\n 完毕\n完毕\n'
        with self.assertRaises(TypeError_):
            TypeChecker(Parser(Lexer(src).tokens()).parse_program()).check()

    def test_wasm_compile(self):
        from heng.wasm_backend import compile_to_wasm
        src = '定义 函数 判(甲 : 数, 乙 : 数) -> 真假:\n 返回 甲 > 0 并且 乙 > 0\n完毕\n'
        p = Parser(Lexer(src).tokens()).parse_program()
        TypeChecker(p).check()
        m = compile_to_wasm(p)
        self.assertIn("判", m.exports)


    def test_wasm_bool_local_and_params(self):
        # 回归：local 段曾重复声明参数导致索引错位，布尔 local 校验失败
        import subprocess, tempfile, os, json
        src = ('定义 函数 判(甲 : 数) -> 真假:\n 变量 结果 : 真假 = 甲 > 0\n 返回 结果\n完毕\n')
        p = Parser(Lexer(src).tokens()).parse_program()
        TypeChecker(p).check()
        from heng.wasm_backend import compile_to_wasm
        m = compile_to_wasm(p)
        with tempfile.NamedTemporaryFile(suffix=".wasm", delete=False) as f:
            f.write(m.binary); path = f.name
        try:
            script = (f"const fs=require('fs');"
                      f"WebAssembly.instantiate(fs.readFileSync({json.dumps(path)}),{{env:{{输出:()=>{{}}}}}})"
                      f".then(r=>{{const g=r.instance.exports['判'];console.log(JSON.stringify([g(5),g(-1)]));}})"
                      f".catch(e=>{{console.error(e.message);process.exit(1);}});")
            out = subprocess.check_output(["node", "-e", script], text=True, timeout=20)
            self.assertEqual(json.loads(out.strip()), [1, 0])
        finally:
            os.unlink(path)

    def test_wasm_two_params(self):
        # 多参数（回归：参数被误当 local 重复声明）
        import subprocess, tempfile, os, json
        src = ('定义 函数 加(甲 : 数, 乙 : 数) -> 数:\n 返回 甲 + 乙\n完毕\n')
        p = Parser(Lexer(src).tokens()).parse_program()
        TypeChecker(p).check()
        from heng.wasm_backend import compile_to_wasm
        m = compile_to_wasm(p)
        with tempfile.NamedTemporaryFile(suffix=".wasm", delete=False) as f:
            f.write(m.binary); path = f.name
        try:
            script = (f"const fs=require('fs');"
                      f"WebAssembly.instantiate(fs.readFileSync({json.dumps(path)}),{{env:{{输出:()=>{{}}}}}})"
                      f".then(r=>console.log(r.instance.exports['加'](3,4)))"
                      f".catch(e=>{{console.error(e.message);process.exit(1);}});")
            out = subprocess.check_output(["node", "-e", script], text=True, timeout=20)
            self.assertEqual(float(out.strip()), 7.0)
        finally:
            os.unlink(path)


class SecurityTests(unittest.TestCase):
    """P0 安全修复：沙箱能力运行时闸门。"""

    def test_sandbox_blocking_unauthorized_at_runtime(self):
        # 绕过静态检查直接构造 Runtime，验证运行时闸门（旧实现 _expr_name(Call)=None 永不拦截）
        from heng.interpreter import Runtime, HError
        src = '入口:\n 沙箱:\n  执行 x = 工具.查询("secret")\n  输出(x)\n 完毕\n完毕\n'
        prog = Parser(Lexer(src).tokens()).parse_program()
        with self.assertRaises(HError) as cm:
            Runtime(prog).run()
        self.assertIn("未授权", str(cm.exception))

    def test_authorized_call_succeeds(self):
        from heng.interpreter import Runtime
        src = '入口:\n 沙箱:\n  授权 工具.查询\n  执行 x = 工具.查询("ok")\n  输出(x)\n 完毕\n完毕\n'
        prog = Parser(Lexer(src).tokens()).parse_program()
        out = Runtime(prog).run()
        self.assertTrue(any("ok" in x for x in out))


class BeginnerFriendlyTests(unittest.TestCase):
    """高频词 + 少符号改造：面向零基础用户。"""

    def test_if_else_new_keywords(self):
        src = '入口:\n 如果 真:\n  说出 "是"\n 否则:\n  说出 "否"\n 结束\n结束\n'
        self.assertEqual(compile_source(src).output, ["是"])

    def test_old_keywords_still_work(self):
        src = '入口:\n 倘若 真:\n  输出("是")\n 否则:\n  输出("否")\n 完毕\n完毕\n'
        self.assertEqual(compile_source(src).output, ["是"])

    def test_unit_without_angle_brackets(self):
        src = ('入口:\n 变量 单价 : 数 元/个 = 12 元/个\n 变量 数量 : 数 个 = 35 个\n'
               ' 变量 总额 : 数 元 = 单价 * 数量\n 说出 总额\n结束\n')
        self.assertEqual(compile_source(src).output, ["420元"])

    def test_seq_without_angle_brackets(self):
        src = '入口:\n 变量 名单 : 序列 文本 = ["甲", "乙"]\n 每个 名单 为 名:\n  说出 名\n 结束\n结束\n'
        self.assertEqual(compile_source(src).output, ["甲", "乙"])

    def test_output_without_parens(self):
        src = '入口:\n 说出 "无括号"\n结束\n'
        self.assertEqual(compile_source(src).output, ["无括号"])

    def test_func_alias_zuofa(self):
        src = ('定义 做法 翻倍(数值 : 数) -> 数:\n 返回 数值 * 2\n结束\n'
               '入口:\n 说出 翻倍(21)\n结束\n')
        self.assertEqual(compile_source(src).output, ["42"])

    def test_lesson_00_runs(self):
        r = compile_source(load("lesson_00_first.hng"))
        self.assertIn("420元", r.output)
        self.assertIn("赚钱了", r.output)
        self.assertIn("42", r.output)

    def test_repeat_n_times(self):
        src = '开始:\n 变量 c : 数 = 0\n 重复 3 次:\n  将 c + 1 放到 c\n  说出 c\n 结束\n结束\n'
        self.assertEqual(compile_source(src).output, ["1", "2", "3"])

    def test_start_as_entry(self):
        self.assertEqual(compile_source('开始:\n 说出 "ok"\n结束\n').output, ["ok"])

    def test_friendly_fullwidth_hint(self):
        with self.assertRaises(LexError) as cm:
            Lexer("开始：\n结束").tokens()
        self.assertIn("全角", str(cm.exception))
        self.assertIn(":", str(cm.exception))

    def test_standard_keyword_set(self):
        # 统一规范：开始/如果/否则/结束/每个/放到/说出/做法/尝试/接住/最后/发起/接收
        src = ('定义 做法 f(x : 数) -> 数:\n 返回 x\n结束\n'
               '开始:\n 如果 真:\n  说出 f(1)\n 否则:\n  说出 0\n 结束\n'
               ' 每个 [1,2] 为 i:\n  说出 i\n 结束\n 尝试:\n  说出 1\n 接住 错误<除零> 为 e:\n  说出 e\n 最后:\n  说出 "完"\n 结束\n结束\n')
        r = compile_source(src)
        self.assertIn("1", r.output)
        self.assertIn("完", r.output)


class DiagnosticsTests(unittest.TestCase):
    """诊断基础设施：多错误收集（现代编译器必备）。"""

    SRC = ('定义 函数 f() -> 文本:\n  返回 1\n完毕\n'
           '定义 函数 g() -> 数:\n  返回 "abc"\n完毕\n'
           '入口:\n  变量 x : 数<元> = 1<秒>\n完毕\n')

    def test_multi_error_collection(self):
        from heng.lsp import LSPServer
        diags = LSPServer().diagnose(self.SRC)
        self.assertGreaterEqual(len(diags), 3)

    def test_diagnostic_has_code_and_range(self):
        from heng.lsp import LSPServer
        diags = LSPServer().diagnose(self.SRC)
        for d in diags:
            self.assertIn("range", d)
            self.assertIn("source", d)
            self.assertEqual(d["severity"], 1)

    def test_bag_api(self):
        from heng.diagnostics import DiagnosticBag, Severity
        bag = DiagnosticBag()
        bag.error("e1", 1, 2, "C7")
        bag.warning("w1", 1, 5)
        self.assertTrue(bag.has_errors)
        self.assertEqual(bag.error_count, 1)
        self.assertIn("错误", str(bag.sorted()[0]))

    def test_clean_program_no_diagnostics(self):
        from heng.lsp import LSPServer
        self.assertEqual(LSPServer().diagnose('入口:\n 输出("ok")\n完毕\n'), [])


class ControlFlowBugTests(unittest.TestCase):
    """控制流与内置函数回归。"""

    def test_return_inside_catch_propagates(self):
        # 回归：catch 块内 return 曾被丢弃，导致调用方拿到 None
        src = ('定义 函数 f(x : 数) -> 文本 可能生 错误<除零>:\n'
               ' 尝试:\n  变量 y : 数 = 10 / x\n  返回 "商" + 化成文(y)\n'
               ' 捕获 错误<除零> 为 e:\n  返回 "除零捕获"\n 完毕\n完毕\n'
               '入口:\n 输出(f(0))\n输出(f(2))\n完毕\n')
        r = compile_source(src)
        self.assertEqual(r.output[0], "除零捕获")
        self.assertIn("商", r.output[1])

    def test_to_text_builtin(self):
        r = compile_source('入口:\n 变量 n : 数 = 8\n 输出("缺 " + 化成文(n) + " 人")\n完毕\n')
        self.assertEqual(r.output, ["缺 8 人"])

    def test_to_number_builtin(self):
        r = compile_source('入口:\n 输出(化为数("42") + 1)\n完毕\n')
        self.assertEqual(r.output, ["43"])

    def test_receive_binding(self):
        src = ('定义 函数 任务(x : 数) -> 文本:\n 返回 "完成"\n完毕\n'
               '入口:\n 并行:\n  启 任务(1)\n  收(任务_信箱) 为 结果\n  输出(结果)\n 完毕\n完毕\n')
        self.assertEqual(compile_source(src).output, ["完成"])

    def test_lesson_examples_run(self):
        for name in ["lesson_01_shop.hng", "lesson_02_leads.hng",
                     "lesson_03_parallel.hng", "lesson_04_agent.hng",
                     "lesson_05_units.hng", "lesson_06_report.hng"]:
            r = compile_source(load(name))
            self.assertTrue(len(r.output) > 0, name)

    def test_dimensionless_unit_not_printed(self):
        # 回归：无量纲单位（Unit([],[]) 的 str 为 "1"）曾被拼到数值后，5 显示成 "51"
        r = compile_source('定义 函数 除(甲 : 数, 乙 : 数) -> 数:\n 返回 甲 / 乙\n完毕\n入口:\n 输出(除(10, 2))\n完毕\n')
        self.assertEqual(r.output, ["5"])
        # 真实单位仍须显示
        r2 = compile_source('入口:\n 变量 a : 数<元/个> = 10<元/个>\n 输出(a)\n完毕\n')
        self.assertEqual(r2.output, ["10元/个"])


class ParserTests(unittest.TestCase):
    def test_all_examples_parse(self):
        for name in ["hello.hng", "order.hng", "agent.hng", "error_handling.hng",
                     "bad_unit.hng", "bad_sandbox.hng", "concurrency.hng",
                     "admission_analyzer.hng", "admission_wasm.hng"]:
            prog = Parser(Lexer(load(name)).tokens()).parse_program()
            self.assertIsNotNone(prog)

    def test_record_and_function(self):
        prog = Parser(Lexer(load("order.hng")).tokens()).parse_program()
        self.assertEqual([r.name for r in prog.records], ["订单项"])
        self.assertEqual([f.name for f in prog.functions], ["结算"])

    def test_contract_and_error_signature(self):
        prog = Parser(Lexer(load("agent.hng")).tokens()).parse_program()
        fn = prog.functions[0]
        self.assertEqual(fn.errors, ["超时"])
        self.assertIsNotNone(fn.contract)
        self.assertIsNotNone(fn.contract.pre)
        self.assertIsNotNone(fn.contract.post)


class TypeCheckerTests(unittest.TestCase):
    def test_valid_programs(self):
        for name in ["hello.hng", "order.hng", "agent.hng", "error_handling.hng",
                     "admission_analyzer.hng"]:
            prog = Parser(Lexer(load(name)).tokens()).parse_program()
            TypeChecker(prog).check()  # 不应抛异常

    def test_unit_mismatch_rejected(self):
        prog = Parser(Lexer(load("bad_unit.hng")).tokens()).parse_program()
        with self.assertRaises(TypeError_) as cm:
            TypeChecker(prog).check()
        self.assertIn("量纲不匹配", str(cm.exception))

    def test_sandbox_unauthorized_rejected(self):
        prog = Parser(Lexer(load("bad_sandbox.hng")).tokens()).parse_program()
        with self.assertRaises(TypeError_) as cm:
            TypeChecker(prog).check()
        self.assertIn("未授权能力", str(cm.exception))

    def test_undeclared_error_rejected(self):
        src = """定义 函数 f() -> 数:
  抛出 错误<超时>("x")
  返回 1
完毕
"""
        prog = Parser(Lexer(src).tokens()).parse_program()
        with self.assertRaises(TypeError_) as cm:
            TypeChecker(prog).check()
        self.assertIn("未声明", str(cm.exception))

    def test_unit_algebra(self):
        # 元/个 × 个 = 元，应通过
        src = """入口:
  变量 a : 数<元/个> = 10<元/个>
  变量 b : 数<个> = 3<个>
  变量 c : 数<元> = a * b
  输出(c)
完毕
"""
        prog = Parser(Lexer(src).tokens()).parse_program()
        TypeChecker(prog).check()


class RuntimeTests(unittest.TestCase):
    def test_hello(self):
        r = compile_source(load("hello.hng"))
        self.assertEqual(r.output, ["你好，衡语"])

    def test_order_calculation(self):
        r = compile_source(load("order.hng"))
        self.assertEqual(r.output, ["33.9元"])  # 10×3×1.13

    def test_error_handling(self):
        r = compile_source(load("error_handling.hng"))
        self.assertEqual(r.output[0], "捕获异常: 除数不能为零")
        self.assertEqual(r.output[1], "程序继续运行")

    def test_agent_loop(self):
        r = compile_source(load("agent.hng"))
        self.assertIn("查询", r.output[0])

    def test_ir_emitted(self):
        # IR trace 默认关闭（性能优先），需显式开启
        r = compile_source(load("order.hng"), trace_ir=True)
        self.assertIn("ForEach", r.ir)
        self.assertIn("VarDecl", r.ir)

    def test_ir_off_by_default(self):
        r = compile_source(load("order.hng"))
        self.assertEqual(r.ir, "")

    def test_concurrency(self):
        r = compile_source(load("concurrency.hng"))
        self.assertEqual(r.output, ["并发任务完成"])

    def test_contract_precondition(self):
        # 静态类型检查应通过（前置是布尔，后置是布尔）
        src = """定义 函数 f(甲 : 数) -> 数:
  规约:
    前置: 甲 > 0
    后置: 结果 > 0
  完毕
  返回 甲
完毕
入口:
  变量 x : 数 = f(0)
  输出(x)
完毕
"""
        prog = Parser(Lexer(src).tokens()).parse_program()
        TypeChecker(prog).check()

    def test_contract_precondition_runtime(self):
        # 前置为假必须在运行时被拦截，而非仅作静态标注
        src = """定义 函数 f(甲 : 数) -> 数:
  规约:
    前置: 甲 > 0
    后置: 结果 > 0
  完毕
  返回 甲
完毕
入口:
  变量 x : 数 = f(0)
  输出(x)
完毕
"""
        with self.assertRaises(CompileError) as cm:
            compile_source(src)
        self.assertIn("前置条件不满足", str(cm.exception))

    def test_contract_postcondition_runtime(self):
        # 后置为假必须在运行时被拦截
        src = """定义 函数 f(甲 : 数) -> 数:
  规约:
    后置: 结果 > 0
  完毕
  返回 甲
完毕
入口:
  变量 x : 数 = f(-1)
  输出(x)
完毕
"""
        with self.assertRaises(CompileError) as cm:
            compile_source(src)
        self.assertIn("后置条件不满足", str(cm.exception))

    def test_admission_analyzer(self):
        r = compile_source(load("admission_analyzer.hng"))
        self.assertEqual(r.output[0], "招生投放效能分析器 v1.0")
        self.assertIn("40元/个", r.output)
        self.assertIn("0.4比例", r.output)
        self.assertIn("31.25元/个", r.output)
        self.assertIn("优", r.output)
        self.assertTrue(any("线索数为零" in x for x in r.output))
        self.assertTrue(any("日报已生成" in x for x in r.output))


class WasmTests(unittest.TestCase):
    def _compile(self, name):
        from heng.wasm_backend import compile_to_wasm
        prog = Parser(Lexer(load(name)).tokens()).parse_program()
        TypeChecker(prog).check()
        return compile_to_wasm(prog)

    def test_wasm_magic_and_exports(self):
        m = self._compile("compute.hng")
        self.assertEqual(m.binary[:4], b"\x00asm")
        self.assertEqual(m.binary[4:8], b"\x01\x00\x00\x00")
        self.assertIn("计算", m.exports)

    def test_wasm_executes_via_node(self):
        import subprocess, tempfile, os, json
        m = self._compile("compute.hng")
        with tempfile.NamedTemporaryFile(suffix=".wasm", delete=False) as f:
            f.write(m.binary)
            path = f.name
        try:
            script = f"""
const fs=require('fs');
WebAssembly.instantiate(fs.readFileSync({json.dumps(path)}),{{env:{{输出:()=>{{}}}}}})
 .then(r=>{{const f=r.instance.exports['计算'];
   console.log(JSON.stringify([f(2,3),f(4,5)]));}})
 .catch(e=>{{console.error(e.message);process.exit(1);}});
"""
            out = subprocess.check_output(["node", "-e", script], text=True, timeout=20)
            self.assertEqual(json.loads(out.strip()), [15.0, 45.0])
        finally:
            os.unlink(path)

    def test_wasm_condition_and_loop(self):
        import subprocess, tempfile, os, json
        results = {}
        for name, fn, arg, expect in [
            ("max.hng", "较大值", [3,7], 7.0),
            ("sum.hng", "求和", [10], 55.0),
        ]:
            m = self._compile(name)
            with tempfile.NamedTemporaryFile(suffix=".wasm", delete=False) as f:
                f.write(m.binary); path=f.name
            try:
                script = f"""
const fs=require('fs');
WebAssembly.instantiate(fs.readFileSync({json.dumps(path)}),{{env:{{输出:()=>{{}}}}}})
 .then(r=>console.log(r.instance.exports[{json.dumps(fn)}](...{json.dumps(arg)})))
 .catch(e=>{{console.error(e.message);process.exit(1);}});
"""
                out = subprocess.check_output(["node", "-e", script], text=True, timeout=20)
                self.assertEqual(float(out.strip()), expect)
            finally:
                os.unlink(path)


    def test_wasm_admission_core(self):
        # 覆盖等值运算符 ==（源写 =）与除零保护分支的真实 wasm 执行
        import subprocess, tempfile, os, json
        m = self._compile("admission_wasm.hng")
        self.assertIn("单线成本", m.exports)
        with tempfile.NamedTemporaryFile(suffix=".wasm", delete=False) as f:
            f.write(m.binary); path = f.name
        try:
            script = f"""
const fs=require('fs');
WebAssembly.instantiate(fs.readFileSync({json.dumps(path)}),{{env:{{输出:()=>{{}}}}}})
 .then(r=>{{const e=r.instance.exports;console.log(JSON.stringify([
   e['单线成本'](80000,2000), e['有效比'](800,2000),
   e['有效成本'](80000,800), e['单线成本'](0,0)]));}})
 .catch(e=>{{console.error(e.message);process.exit(1);}});
"""
            out = subprocess.check_output(["node", "-e", script], text=True, timeout=20)
            self.assertEqual(json.loads(out.strip()), [40.0, 0.4, 100.0, 0.0])
        finally:
            os.unlink(path)


class LSPTests(unittest.TestCase):
    def test_diagnose_valid(self):
        from heng.lsp import LSPServer
        d = LSPServer().diagnose(load("order.hng"))
        self.assertEqual(d, [])

    def test_diagnose_unit_error(self):
        from heng.lsp import LSPServer
        d = LSPServer().diagnose(load("bad_unit.hng"))
        self.assertEqual(len(d), 1)
        self.assertIn("量纲不匹配", d[0]["message"])
        self.assertEqual(d[0]["source"], "衡语")

    def test_diagnose_sandbox_error(self):
        from heng.lsp import LSPServer
        d = LSPServer().diagnose(load("bad_sandbox.hng"))
        self.assertTrue(any("未授权" in x["message"] for x in d))

    def test_hover_and_completion_data(self):
        from heng.lsp import KEYWORD_DOCS, COMPLETION_ITEMS
        self.assertIn("将", KEYWORD_DOCS)
        self.assertTrue(len(COMPLETION_ITEMS) >= 40)

    def test_word_at_chinese(self):
        from heng.lsp import LSPServer
        self.assertEqual(LSPServer._word_at("倘若 条件:", 0, 1), "倘若")

    def test_vscode_assets_valid(self):
        import json, os
        base = os.path.join(os.path.dirname(__file__), "..", "editors", "vscode")
        for f in ["package.json", "language-configuration.json", "syntaxes/heng.tmLanguage.json"]:
            with open(os.path.join(base, f), encoding="utf-8") as fh:
                json.load(fh)  # 不抛异常即合法


class ApiTests(unittest.TestCase):
    def test_check_only(self):
        from heng.api import check_only
        self.assertTrue(check_only(load("order.hng")))
        with self.assertRaises(CompileError):
            check_only(load("bad_unit.hng"))


# ============================================================
# 按「衡语-AI语言底座评估与优化报告」第六章优先级实施后的回归测试
# ============================================================

class StdlibTests(unittest.TestCase):
    """报告优先级 2：最小标准库（JSON / HTTP / 文本 / 数学 / 数组）。"""

    def run_src(self, src: str):
        return compile_source(src).output

    def test_text_functions(self):
        out = self.run_src('开始:\n 说出 文字.去空白("  甲  ")\n'
                           ' 说出 文字.替换("甲乙丙", "乙", "丁")\n'
                           ' 说出 文字.含("武校招生", "招生")\n结束\n')
        self.assertEqual(out, ["甲", "甲丁丙", "真"])

    def test_text_split_returns_text_sequence(self):
        # 序列元素类型必须是 文本，否则「前缀 + 元素」会被误拒
        out = self.run_src('开始:\n 每个 文字.切分("甲,乙", ",") 为 项:\n'
                           '  说出 "号码：" + 项\n 结束\n结束\n')
        self.assertEqual(out, ["号码：甲", "号码：乙"])

    def test_math_functions(self):
        out = self.run_src('开始:\n 说出 数值.开方(144)\n 说出 数值.四舍五入(3.14159, 2)\n'
                           ' 说出 数值.最大(3, 9)\n结束\n')
        self.assertEqual(out, ["12", "3.14", "9"])

    def test_seq_functions(self):
        out = self.run_src('开始:\n 变量 费 : 序列 数 = [40, 31, 60]\n'
                           ' 说出 表.求和(费)\n 说出 表.最大(费)\n 说出 表.长度(费)\n结束\n')
        self.assertEqual(out, ["131", "60", "3"])

    def test_json_roundtrip(self):
        out = self.run_src('开始:\n 变量 载荷 : 文本 = "{\\"线索\\": 42}"\n'
                           ' 变量 对象 : 映射 文本 任意 = JSON.解码(载荷)\n'
                           ' 说出 对象.线索\n 说出 JSON.有效("{bad")\n结束\n')
        self.assertEqual(out, ["42", "假"])

    def test_json_encoding_keeps_chinese(self):
        out = self.run_src('开始:\n 说出 JSON.编码({"名称": "东营"})\n结束\n')
        self.assertIn("东营", out[0])

    def test_arity_checked_statically(self):
        with self.assertRaises(CompileError) as cm:
            compile_source('开始:\n 说出 文字.切分("a,b")\n结束\n')
        self.assertIn("参数数量不符", str(cm.exception))

    def test_network_requires_authorization(self):
        # 真实副作用必须走沙箱能力模型：未授权时静态拒绝
        with self.assertRaises(CompileError) as cm:
            compile_source('开始:\n 沙箱:\n  执行 x = 网络.取("https://example.com")\n 结束\n结束\n')
        self.assertIn("未授权", str(cm.exception))

    def test_pure_functions_allowed_in_sandbox(self):
        out = self.run_src('开始:\n 沙箱:\n  说出 文字.大写("ab")\n 结束\n结束\n')
        self.assertEqual(out, ["AB"])


class FfiAndImportTests(unittest.TestCase):
    """报告优先级 3：导入 不再是空语法，FFI 与包管理落地。"""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="heng_imp_")

    def _write(self, name: str, text: str) -> str:
        import os
        p = os.path.join(self.tmp, name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        return p

    def test_import_heng_module(self):
        self._write("工具.hng",
                    '定义 做法 单线成本(消耗 : 数, 线索 : 数) -> 数:\n'
                    '  如果 线索 = 0:\n    返回 0\n  结束\n  返回 消耗 / 线索\n结束\n')
        main = self._write("主.hng",
                           '导入 「工具.hng」之 工具\n开始:\n'
                           ' 说出 工具.单线成本(80000, 2000)\n结束\n')
        out = compile_source(_read(main), filename=main).output
        self.assertEqual(out, ["40"])

    def test_import_glued_alias(self):
        # 中文不加空格：「工具之工具」应等价于「工具 之 工具」
        self._write("工具.hng", '定义 做法 翻倍(x : 数) -> 数:\n  返回 x * 2\n结束\n')
        main = self._write("主.hng",
                           '导入 工具之工具\n开始:\n 说出 工具.翻倍(21)\n结束\n')
        out = compile_source(_read(main), filename=main).output
        self.assertEqual(out, ["42"])

    def test_import_foreign_module(self):
        main = self._write("主.hng",
                           '导入 外部 「math」之 数学\n开始:\n'
                           ' 说出 数学.sqrt(144)\n 说出 数学.factorial(5)\n结束\n')
        out = compile_source(_read(main), filename=main).output
        self.assertEqual(out, ["12", "120"])

    def test_foreign_call_blocked_in_sandbox(self):
        main = self._write("主.hng",
                           '导入 外部 「math」之 数学\n开始:\n 沙箱:\n'
                           '  说出 数学.sqrt(16)\n 结束\n结束\n')
        with self.assertRaises(CompileError) as cm:
            compile_source(_read(main), filename=main)
        self.assertIn("未授权", str(cm.exception))

    def test_missing_module_is_explicit_error(self):
        main = self._write("主.hng", '导入 「不存在.hng」之 x\n开始:\n 说出 1\n结束\n')
        with self.assertRaises(CompileError) as cm:
            compile_source(_read(main), filename=main)
        self.assertIn("找不到模块", str(cm.exception))

    def test_manifest_dependency(self):
        self._write("heng.mod", "名称 测试包\n依赖 工具 = lib/工具.hng\n")
        self._write("lib/工具.hng", '定义 做法 转化率(线索 : 数, 成交 : 数) -> 数:\n'
                                     '  如果 线索 = 0:\n    返回 0\n  结束\n'
                                     '  返回 成交 / 线索\n结束\n')
        main = self._write("主.hng",
                           '导入 工具 之 工具\n开始:\n 说出 工具.转化率(400, 40)\n结束\n')
        out = compile_source(_read(main), filename=main).output
        self.assertEqual(out, ["0.1"])


class ConsistencyTests(unittest.TestCase):
    """本轮发现并修复的一致性缺陷回归。"""

    def test_builtin_length_is_dimensionless(self):
        # 原实现返回 数<个>，「说出 长度(s)」打印成「3个」
        self.assertEqual(compile_source('开始:\n 说出 长度("abc")\n结束\n').output, ["3"])

    def test_user_function_not_shadowed_by_builtin(self):
        # 原实现：定义 做法 长度 后调用仍命中内置，返回「3个」——静默错误
        src = ('定义 做法 长度(x : 数) -> 数:\n  返回 x * 2\n结束\n'
               '开始:\n 说出 长度(5)\n结束\n')
        self.assertEqual(compile_source(src).output, ["10"])

    def test_length_on_number_is_compile_error(self):
        with self.assertRaises(CompileError) as cm:
            compile_source('开始:\n 说出 长度(5)\n结束\n')
        self.assertIn("长度", str(cm.exception))

    def test_no_python_stack_leak(self):
        # 用户可见契约：任何错误都必须是中文衡语错误，不得泄漏 Python 栈
        try:
            compile_source('开始:\n 说出 不存在的包.函数(1)\n结束\n')
        except CompileError as e:
            msg = str(e)
        self.assertNotIn("Traceback", msg)
        self.assertIn("错误<", msg)

    def test_keyword_as_member_name(self):
        # 标准库/FFI 成员名可能正好是关键字
        self.assertEqual(compile_source('开始:\n 说出 文字.长度("中文")\n结束\n').output, ["2"])


class WasmRepeatTests(unittest.TestCase):
    """报告优先级 1：WASM 后端语义补齐（重复 / 每个 / 必然返回）。"""

    def _run_wasm(self, src: str, fn: str, args: list):
        import subprocess, tempfile, os, json
        from heng.wasm_backend import compile_to_wasm
        prog = Parser(Lexer(src).tokens()).parse_program()
        TypeChecker(prog).check()
        m = compile_to_wasm(prog)
        with tempfile.NamedTemporaryFile(suffix=".wasm", delete=False) as f:
            f.write(m.binary)
            path = f.name
        try:
            script = f"""
const fs=require('fs');
WebAssembly.instantiate(fs.readFileSync({json.dumps(path)}),{{env:{{输出:()=>{{}}}}}})
 .then(r=>console.log(JSON.stringify(r.instance.exports[{json.dumps(fn)}](...{json.dumps(args)}))))
 .catch(e=>{{console.error(e.message);process.exit(1);}});
"""
            return json.loads(subprocess.check_output(["node", "-e", script], text=True).strip())
        finally:
            os.unlink(path)

    def test_repeat_loop(self):
        src = ('定义 做法 累加(上界 : 数) -> 数:\n  变量 总计 : 数 = 0\n'
               '  变量 一 : 数 = 1\n  重复 上界 次:\n    将 总计 + 一 放到 总计\n'
               '  结束\n  返回 总计\n结束\n')
        self.assertEqual(self._run_wasm(src, "累加", [10]), 10.0)

    def test_foreach_literal(self):
        src = ('定义 做法 加权(因子 : 数) -> 数:\n  变量 总计 : 数 = 0\n'
               '  每个 [1, 2, 3, 4] 为 项:\n    将 总计 + 项 放到 总计\n  结束\n'
               '  返回 总计 * 因子\n结束\n')
        self.assertEqual(self._run_wasm(src, "加权", [2]), 20.0)

    def test_both_branches_return(self):
        # 两分支都返回时，函数末尾必须补 unreachable，
        # 否则出现「fallthru 缺少栈值」的 WASM 验证失败（真实 bug）
        src = ('定义 做法 较大值(甲 : 数, 乙 : 数) -> 数:\n  如果 甲 > 乙:\n'
               '    返回 甲\n  否则:\n    返回 乙\n  结束\n结束\n')
        self.assertEqual(self._run_wasm(src, "较大值", [3, 7]), 7.0)

    def test_unsupported_still_errors(self):
        from heng.wasm_backend import compile_to_wasm, WasmCompileError
        src = ('定义 做法 f(x : 数) -> 数:\n  尝试:\n    返回 x\n'
               '  结束\n结束\n')
        prog = Parser(Lexer(src).tokens()).parse_program()
        TypeChecker(prog).check()
        with self.assertRaises(WasmCompileError):
            compile_to_wasm(prog)


if __name__ == "__main__":
    unittest.main(verbosity=2)
