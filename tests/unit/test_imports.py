"""依赖红线断言（ARCHITECTURE.md §1.2 规则 R1/R2）。

两条必须成立的结构约束：

1. **内核与配置层不得拉入重依赖**（``textual`` / ``rich`` / ``httpx`` / ``openai``
   / ``anthropic`` / ``mcp``）——否则 D29 的 ``--version`` < 300ms 立刻失守，
   而且内核将无法在精简环境里被测试。
2. **``kernel/`` 不得 import 任何具体实现层**（``tui`` / ``providers`` / ``store``
   / ``telemetry`` / ``config`` / ``cli``）——这是「事件总线内核」得以成立的前提。

两种互补的检查方式：

* **运行时探针**（子进程 import 后检查 ``sys.modules``）——能抓到间接依赖。
  输出重定向到**文件而非管道**，因为本沙箱禁止通过管道捕获子进程输出。
* **AST 静态扫描**——精确、无文档字符串误报，并能看到 ``importlib`` 动态导入。
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
import unittest
import uuid
from pathlib import Path

from tests.unit.support import TEMP_ROOT

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = REPO_ROOT / "src"

HEAVY_DEPENDENCIES = ("textual", "rich", "httpx", "openai", "anthropic", "mcp", "pydantic_cli")
"""内核与配置层禁止拉入的重依赖（pydantic 允许，它是 D26 选定的数据模型层）。"""

IMPLEMENTATION_ROOTS = ("tui", "providers", "store", "telemetry", "config", "cli", "plugins", "hooks")
"""``kernel/`` 不得依赖的 ``logox`` 子包。"""

KERNEL_AND_CONFIG_MODULES = (
    "logox.errors",
    "logox.paths",
    "logox.kernel.errors",
    "logox.kernel.events",
    "logox.kernel.bus",
    "logox.config.schema",
    "logox.config.writer",
    "logox.config.loader",
    "logox.config.state",
    "logox.config.theme",
    "logox.telemetry",
    "logox.cli",
)

_PROBE = """
import importlib, sys

for name in {modules!r}:
    importlib.import_module(name)

roots = {{name.split(".")[0] for name in sys.modules}}
heavy = {{name for name in roots if name in {heavy!r}}}
leaked = sorted(
    name for name in sys.modules
    if not name.startswith("_") and name.split(".")[0] == "logox"
    and name.count(".") >= 1 and name.split(".")[1] in {layers!r}
)
print("HEAVY:" + ",".join(sorted(heavy)))
print("LEAKED:" + ",".join(leaked))
print("DONE")
"""


def _run_probe(script: str) -> str:
    """在子进程里执行探针并把输出写到文件（**不用管道**，沙箱禁止）。"""
    TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    out_path = TEMP_ROOT / f"probe-{uuid.uuid4().hex[:10]}.txt"
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(SRC_DIR) + (os.pathsep + existing if existing else "")
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        with open(out_path, "w", encoding="utf-8") as handle:
            completed = subprocess.run(  # noqa: S603 - 固定命令，无外部输入
                [sys.executable, "-c", script],
                stdout=handle,
                stderr=subprocess.STDOUT,
                env=env,
                cwd=str(REPO_ROOT),
                timeout=180,
                check=False,
            )
        text = out_path.read_text(encoding="utf-8")
    finally:
        out_path.unlink(missing_ok=True)
    if completed.returncode != 0:
        raise AssertionError(f"探针脚本以 {completed.returncode} 退出：\n{text}")
    return text


def _extract(output: str, prefix: str) -> list[str]:
    line = next((item for item in output.splitlines() if item.startswith(prefix)), None)
    if line is None:
        raise AssertionError(f"探针输出缺少 {prefix} 行：\n{output}")
    return [name for name in line[len(prefix) :].split(",") if name]


def imported_roots(source: str) -> set[str]:
    """用 AST 取出一个源文件里出现的所有顶层导入名（含 ``importlib`` 动态导入）。"""
    tree = ast.parse(source)
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.level == 0:
                found.add(node.module.split(".")[0])
        elif isinstance(node, ast.Call):
            func = node.func
            name = getattr(func, "attr", None) or getattr(func, "id", None)
            if name in ("import_module", "__import__") and node.args:
                first = node.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    found.add(first.value.split(".")[0])
    return found


def imported_modules(source: str) -> set[str]:
    """文件里 ``from X import ...`` 的完整模块名（用于断言"只能从某处导入"）。"""
    return {
        node.module
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module
    }


class ImportBoundaryTests(unittest.TestCase):
    def test_kernel_and_config_pull_no_heavy_dependency(self) -> None:
        """约束 1：内核 + 配置层不拉入 ``textual`` / ``rich`` / ``httpx`` / SDK / ``mcp``。"""
        output = _run_probe(
            _PROBE.format(
                modules=KERNEL_AND_CONFIG_MODULES,
                heavy=HEAVY_DEPENDENCIES,
                layers=IMPLEMENTATION_ROOTS,
            )
        )
        self.assertIn("DONE", output, f"探针未完成：{output}")
        heavy = _extract(output, "HEAVY:")
        self.assertEqual(heavy, [], f"内核/配置层不得拉入重依赖：{heavy}")

    def test_kernel_does_not_import_implementation_layers(self) -> None:
        """约束 2（R2）：``kernel/`` 不得依赖任何具体实现层。"""
        output = _run_probe(
            _PROBE.format(
                modules=("logox.kernel.errors", "logox.kernel.events", "logox.kernel.bus"),
                heavy=HEAVY_DEPENDENCIES,
                layers=IMPLEMENTATION_ROOTS,
            )
        )
        self.assertIn("DONE", output, f"探针未完成：{output}")
        leaked = [
            name
            for name in _extract(output, "LEAKED:")
            if name not in ("logox.kernel", "logox.errors")
        ]
        self.assertEqual(leaked, [], f"kernel 不得依赖具体实现层：{leaked}")

    def test_ast_scan_of_kernel_package(self) -> None:
        """AST 扫描 ``kernel/``：只允许依赖标准库、pydantic 与**共享契约模块**。

        这条比"枚举允许的模块"更耐用——只守住真正会腐化的两件事：
        不得依赖重依赖、不得依赖具体实现层。

        允许对内引用 ``logox.tools.base`` 与 ``logox.providers.base``（M3 / D50）：
        它们是 **Protocol 与 pydantic 模型**，没有行为、没有 I/O，正是 R2 所说的
        "内核只能依赖 Protocol 与 pydantic 模型"。**具体**工具与**具体**适配器
        仍然禁止——那由 ``test_kernel_does_not_import_concrete_capabilities`` 守住。
        """
        shared_contracts = ("logox.errors", "logox.kernel", "logox.tools.base", "logox.providers.base")
        scanned = 0
        for path in sorted((SRC_DIR / "logox" / "kernel").rglob("*.py")):
            scanned += 1
            source = path.read_text(encoding="utf-8")
            for root in sorted(imported_roots(source)):
                with self.subTest(file=path.name, imported=root):
                    self.assertNotIn(root, IMPLEMENTATION_ROOTS, f"{path.name} 不得 import {root}")
                    self.assertNotIn(root, HEAVY_DEPENDENCIES, f"{path.name} 不得 import {root}")
                    if root == "logox":
                        # 只审视 logox 内部依赖：标准库与 pydantic 由上面的重依赖检查负责。
                        internal = {
                            module
                            for module in imported_modules(source)
                            if module.startswith("logox")
                        }
                        for module in sorted(internal):
                            self.assertTrue(
                                any(module == item or module.startswith(item + ".") for item in shared_contracts),
                                f"{path.name} 只允许引用 {shared_contracts}，实际：{module}",
                            )
        self.assertGreaterEqual(scanned, 4, "应扫描到 kernel 包的全部模块")

    def test_ast_scan_of_config_package_for_heavy_deps(self) -> None:
        """AST 扫描 ``config/``：不得拉入重依赖。"""
        scanned = 0
        for path in sorted((SRC_DIR / "logox" / "config").rglob("*.py")):
            scanned += 1
            source = path.read_text(encoding="utf-8")
            roots = imported_roots(source)
            for heavy in HEAVY_DEPENDENCIES:
                with self.subTest(file=path.name, imported=heavy):
                    self.assertNotIn(heavy, roots, f"{path.name} 不得 import {heavy}")
        self.assertGreaterEqual(scanned, 6, "应扫描到 config 包的全部模块")

    def test_ast_scan_of_providers_package(self) -> None:
        """AST 扫描 ``providers/``（L5 适配层）。

        适配层在最底下，因此它**只能**向内依赖三件事：标准库 / pydantic / 共享契约。
        具体地：

        * 不得依赖界面层与业务层（``tui`` / ``store`` / ``tools`` / ``config`` / ``cli``）
        * 对内只允许引用 ``logox.errors`` 与 ``logox.kernel.*``（**纯契约模块**：
          数据模型 + 纯函数，零 I/O、零行为）。跨层引用契约是允许的，引用行为不是
        * 不得顶层 import 厂商 SDK（见 ``tests/contract/test_adapter_hygiene.py`` 的 T-06）
        """
        allowed_internal = ("logox.errors", "logox.kernel", "logox.providers")
        scanned = 0
        for path in sorted((SRC_DIR / "logox" / "providers").rglob("*.py")):
            scanned += 1
            source = path.read_text(encoding="utf-8")
            for root in sorted(imported_roots(source)):
                with self.subTest(file=path.name, imported=root):
                    # 厂商 SDK 是"必须延迟导入"而非"禁止使用"，由顶层扫描单独把关
                    self.assertNotIn(root, ("textual", "rich", "mcp"))
                    self.assertNotIn(
                        root,
                        ("tui", "store", "tools", "config", "cli", "telemetry", "permissions", "hooks"),
                        f"{path.name} 属于 L5，不得依赖上层实现",
                    )
            for module in sorted(imported_modules(source)):
                if not module.startswith("logox"):
                    continue
                with self.subTest(file=path.name, module=module):
                    self.assertTrue(
                        any(module == item or module.startswith(item + ".") for item in allowed_internal),
                        f"{path.name} 只允许引用 {allowed_internal}，实际：{module}",
                    )
        self.assertGreaterEqual(scanned, 6, "应扫描到 providers 包的全部模块")

    def test_providers_does_not_import_config(self) -> None:
        """规则 R1 的具体落点：装配根（L2）读配置，适配层（L5）只收已解析好的值。

        这条挡住的是最容易被"顺手"写出来的一种腐化：适配器为了图方便直接
        ``import logox.config`` 去读配置项，从此再也没法脱离配置单独测试。
        """
        for path in sorted((SRC_DIR / "logox" / "providers").rglob("*.py")):
            source = path.read_text(encoding="utf-8")
            with self.subTest(file=path.name):
                self.assertFalse(
                    any(module.startswith("logox.config") for module in imported_modules(source)),
                    f"{path.name} 不得 import logox.config",
                )

    def test_providers_keeps_vendor_sdks_out_of_module_toplevel(self) -> None:
        """厂商 SDK 只许在**函数体内**延迟导入（D29 的 ``--version`` < 300ms）。

        ``tests/contract/test_adapter_hygiene.py`` 的 T-06 用黑名单做了同一件事，
        这里从"哪些模块可以出现在顶层"的正面角度再钉一遍——两边都失败才说明真的坏了。
        """
        seen_sdk = False
        for path in sorted((SRC_DIR / "logox" / "providers").rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            top_level: set[str] = set()
            for node in tree.body:
                if isinstance(node, ast.Import):
                    top_level.update(alias.name.split(".")[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                    top_level.add(node.module.split(".")[0])
            # "确实用了 SDK"要在**整棵树**里看，因为合法用法恰恰在函数体内
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module in ("openai", "anthropic", "httpx"):
                    seen_sdk = True
            for name in sorted(top_level):
                with self.subTest(file=path.name, imported=name):
                    self.assertNotIn(name, ("openai", "anthropic", "httpx"))
        self.assertTrue(seen_sdk, "适配器应当确实（延迟）使用了厂商 SDK")

    def test_kernel_does_not_import_concrete_capabilities(self) -> None:
        """M3 的红线：``kernel/`` **不得** import 具体工具、具体 Provider、具体存储。

        R2 允许内核依赖 **Protocol 与 pydantic 模型**（``tools.base``、
        ``providers.base``），因为它们没有行为。但一旦 import 了
        ``tools.fs_read`` 或 ``providers.openai_compat``，内核就再也无法脱离
        具体实现单独测试了——而"能在精简环境里测内核"正是 M3 全部测试的前提。
        """
        allowed_internal = ("logox.errors", "logox.kernel", "logox.tools.base", "logox.providers.base")
        scanned = 0
        for path in sorted((SRC_DIR / "logox" / "kernel").rglob("*.py")):
            scanned += 1
            source = path.read_text(encoding="utf-8")
            for module in sorted(imported_modules(source)):
                if not module.startswith("logox"):
                    continue
                with self.subTest(file=path.name, module=module):
                    self.assertTrue(
                        any(module == item or module.startswith(item + ".") for item in allowed_internal),
                        f"{path.name} 只允许引用 {allowed_internal}，实际：{module}",
                    )
        self.assertGreaterEqual(scanned, 8, "应扫描到 kernel 包的全部模块（M3 后至少 8 个）")

    def test_kernel_does_not_import_the_l4_concrete_tool_modules(self) -> None:
        """把上一条里最容易破的那一半单独钉死：**具体工具**。

        写 ``fs_read`` 时"顺手"在 ``loop.py`` 里 import 一下能省掉注册步骤，
        但那样内核就被工具集绑死了。这条断言让那个诱惑当场失败。
        """
        for path in sorted((SRC_DIR / "logox" / "kernel").rglob("*.py")):
            source = path.read_text(encoding="utf-8")
            concrete = [
                module
                for module in imported_modules(source)
                if module.startswith("logox.tools.") and module != "logox.tools.base"
            ]
            with self.subTest(file=path.name):
                self.assertEqual(concrete, [], f"{path.name} 不得 import 具体工具：{concrete}")

    def test_cli_module_has_no_heavy_toplevel_import(self) -> None:
        """``cli.py`` 顶层不得 import 配置层 / 界面层 / pydantic（D29 性能红线）。"""
        source = (SRC_DIR / "logox" / "cli.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        top_level: set[str] = set()
        for node in tree.body:  # 只看模块顶层，函数内部的延迟导入不算
            if isinstance(node, ast.Import):
                top_level.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                top_level.add(node.module.split(".")[0])

        for name in sorted(top_level):
            with self.subTest(imported=name):
                self.assertNotIn(name, ("pydantic", "textual", "rich", "httpx", "openai", "anthropic"))
                self.assertNotIn(name, ("logox.config", "logox.tui", "logox.kernel", "logox.telemetry"))
        self.assertIn("argparse", top_level)
        self.assertIn("logox", top_level, "cli.py 需要版本号")

    def test_root_package_is_pure(self) -> None:
        """``logox/__init__.py`` 只暴露版本号，不做任何重导入（惰性导入的前提）。"""
        source = (SRC_DIR / "logox" / "__init__.py").read_text(encoding="utf-8")
        roots = imported_roots(source) - {"__future__"}
        self.assertEqual(roots, set(), "__init__.py 不得 import 任何运行时模块")


if __name__ == "__main__":
    unittest.main()
