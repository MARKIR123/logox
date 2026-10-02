"""``KernelPort`` 协议与内核公开消费面的边界测试（MODULE_tui_integration §7.4）。

本文件守住两件 M4 特别关心的事：

1. **界面能用内核做哪些事**——`KernelPort` 刻意只有三个成员（`start` / `cancel` /
   `current_turn`）。**刻意不含** `submit`（会阻塞到回合结束，界面用了它就按不了 Esc）
   与 `history`（内核拥有，持久化在 M8 才读）。协议小，界面才能被单独测试。
2. **D58 的生效边界**——`set_thinking()` 只影响"下一次将要发起的请求"，
   **正在流式生成的那一次拿的是旧档位**（请求参数在 `_model_phase` 开头就固化进
   `ChatRequest` 了）。这不是妥协，而是"重试必须用同一份请求"（D52）这条不变量的必然结果。
"""

from __future__ import annotations

import unittest
from typing import Any

from logox.kernel.port import KernelPort
from logox.providers.base import ThinkingConfig
from tests.unit.kernel_support import Pause, install, text_chunks, tool_chunks


class CapturingProvider:
    """包一层真 provider，把每次请求原样记下来。

    为什么不用 mock：`ChatRequest` 是内核**唯一**交给适配层的东西，
    所以"请求里到底是什么"只能从这一层看。用一个真实的适配器包一层，
    走的是与生产完全相同的装配路径。
    """

    def __init__(self, inner: object) -> None:
        self._inner = inner
        self.requests: list[object] = []
        self.name = getattr(inner, "name", "capturing")

    def list_models(self):  # type: ignore[no-untyped-def]
        return self._inner.list_models()  # type: ignore[attr-defined]

    def stream(self, request):  # type: ignore[no-untyped-def]
        self.requests.append(request)
        return self._inner.stream(request)  # type: ignore[attr-defined]


def _install_capturing(script, **kwargs):  # type: ignore[no-untyped-def]
    env = install(script, **kwargs)
    capturing = CapturingProvider(env.kernel._provider)  # noqa: SLF001 - 测试要替换内核持有的 provider
    env.kernel._provider = capturing  # noqa: SLF001
    return env, capturing


class KernelPortTests(unittest.IsolatedAsyncioTestCase):
    def test_t40_kernel_loop_satisfies_protocol(self) -> None:
        """`KernelLoop` 必须满足 `KernelPort`——否则界面接不上真内核。"""
        env = install([text_chunks("hi")])
        self.assertIsInstance(env.kernel, KernelPort)

    def test_t40b_protocol_does_not_require_submit(self) -> None:
        """协议里**不能**有 `submit`：界面用了它就无法响应 Esc。

        这条断言是"把设计意图写进测试"——将来有人觉得方便把 `submit` 加进协议，
        这里会当场失败并提醒他为什么不能加。
        """
        self.assertFalse(hasattr(KernelPort, "submit"))
        self.assertFalse(hasattr(KernelPort, "history"))

    def test_t40c_fake_kernel_with_three_members_satisfies_protocol(self) -> None:
        """只有三个成员的假对象也算合格实现——这是界面测试的基础。"""

        class FakeKernel:
            async def start(self, text: str) -> object:  # pragma: no cover - 不实际调用
                return object()

            def cancel(self) -> bool:
                return False

            @property
            def current_turn(self) -> None:
                return None

        self.assertIsInstance(FakeKernel(), KernelPort)


class SetThinkingTests(unittest.IsolatedAsyncioTestCase):
    """D58：思考档位的"设置即生效"到底生效在哪一刻。"""

    async def test_t54a_initial_thinking_reaches_request(self) -> None:
        env, capturing = _install_capturing([text_chunks("hi")])
        env.kernel.set_thinking(ThinkingConfig(effort="low"))
        await env.kernel.submit("你好")
        self.assertEqual(len(capturing.requests), 1)
        self.assertEqual(capturing.requests[0].thinking.effort, "low")  # type: ignore[attr-defined]

    async def test_t54b_first_request_uses_value_at_assembly_time(self) -> None:
        """构造时给的档位就是第一次请求用的档位。"""
        env, capturing = _install_capturing([text_chunks("hi")], thinking=ThinkingConfig(effort="high"))
        await env.kernel.submit("你好")
        self.assertEqual(capturing.requests[0].thinking.effort, "high")  # type: ignore[attr-defined]

    async def test_t54c_in_flight_request_keeps_old_effort(self) -> None:
        """★ 核心边界：**正在生成的那一次请求不受影响**。

        流到一半时改档位，然后放开流——第 1 次请求必须仍是旧档位，
        即 `set_thinking` 绝不能"追溯到已经发出的请求"。
        """
        env, capturing = _install_capturing(
            [[{"choices": [{"index": 0, "delta": {"content": "前半"}, "finish_reason": None}], "model": "mock-model"},
              Pause(0.05),
              {"choices": [{"index": 0, "delta": {"content": "后半"}, "finish_reason": None}], "model": "mock-model"},
              {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}], "model": "mock-model"}]]
        )
        env.kernel.set_thinking(ThinkingConfig(effort="low"))

        turn = await env.kernel.start("你好")
        # 等到流已经吐了内容（说明请求已发出、且正在流式生成）
        for _ in range(50):
            if any(event.type == "model_delta" for event in env.events):
                break
            import asyncio

            await asyncio.sleep(0.01)

        env.kernel.set_thinking(ThinkingConfig(effort="high"))  # 生成中途改档位
        await env.kernel.wait(turn)

        self.assertEqual(len(capturing.requests), 1, "本次生成不应重发请求")
        self.assertEqual(
            capturing.requests[0].thinking.effort,  # type: ignore[attr-defined]
            "low",
            "正在生成的那一次请求必须仍用旧档位（D58 的明确边界）",
        )

    async def test_t54d_next_request_uses_new_effort(self) -> None:
        """改档位之后**下一次**请求必须用新值——这才是"设置即生效"的落点。"""
        env, capturing = _install_capturing([text_chunks("一"), text_chunks("二")])
        env.kernel.set_thinking(ThinkingConfig(effort="low"))

        await env.kernel.submit("第一条")
        env.kernel.set_thinking(ThinkingConfig(effort="high"))
        await env.kernel.submit("第二条")

        self.assertEqual([request.thinking.effort for request in capturing.requests], ["low", "high"])  # type: ignore[attr-defined]

    async def test_t54e_same_turn_later_request_uses_new_effort(self) -> None:
        """同一个回合内、工具调用之后再发的那次请求，用新档位。

        "下一次请求"的语义比"下一个回合"更细：一个回合可能发多次请求
        （模型要工具 → 执行 → 再问模型）。用户在中途改档位，
        应当在那之后的请求上就生效，而不是等到下一轮对话。
        """
        env, capturing = _install_capturing(
            [tool_chunks([("c1", "read", {"path": "a.txt"})]), text_chunks("读完了")]
        )

        # 在工具阶段之后、第二次模型请求之前改档位：用一个订阅者在 ToolCallFinished 时改
        from logox.kernel import events as ev

        holder = {"kernel": env.kernel}

        async def on_tool_finished(event: object) -> None:
            if isinstance(event, ev.ToolCallFinished):
                holder["kernel"].set_thinking(ThinkingConfig(effort="medium"))  # type: ignore[attr-defined]

        from tests.unit.kernel_support import StubTool

        env.registry.register(StubTool(name="read"))
        # ⚠️ **必须用 blocking 订阅**：非阻塞通道会在 `publish` 之后异步投递，
        # 于是"工具完成 → 改档位"这件事可能晚于第二次模型请求才发生。
        # 这一条是本用例第一次跑时踩到的（第二次请求仍是 None）——时序断言
        # 必须让被测的因果链是同步的，否则测的是调度器的运气。
        env.bus.subscribe(ev.ToolCallFinished, on_tool_finished, name="mid-turn-effort")

        await env.kernel.submit("读一下")
        await env.bus.drain()

        self.assertEqual(len(capturing.requests), 2)
        self.assertIsNone(capturing.requests[0].thinking, "第一次请求未设档位 → None")  # type: ignore[attr-defined]
        # 注意：本用例里第一次请求的 thinking 是 None，第二次必须是 medium
        self.assertEqual(capturing.requests[1].thinking.effort, "medium")  # type: ignore[attr-defined]

    async def test_t54f_none_restores_no_intervention(self) -> None:
        """传 `None` = 不干预（等价 `auto`），请求里就是 `thinking=None`。"""
        env, capturing = _install_capturing([text_chunks("hi")], thinking=ThinkingConfig(effort="high"))
        env.kernel.set_thinking(None)
        await env.kernel.submit("你好")
        self.assertIsNone(capturing.requests[0].thinking)  # type: ignore[attr-defined]

    async def test_t54g_optional_capability_is_probeable(self) -> None:
        """★ 界面靠 `getattr(kernel, "set_thinking", None)` 探测这个可选能力。

        所以：① 真内核必须有它；② 只有三成员的假内核**没有**它，也必须能构造并跑。
        """

        class MinimalFake:
            async def start(self, text: str) -> object:  # pragma: no cover
                return object()

            def cancel(self) -> bool:
                return False

            @property
            def current_turn(self) -> None:
                return None

        env = install([text_chunks("hi")])
        self.assertTrue(callable(getattr(env.kernel, "set_thinking", None)))
        self.assertIsNone(getattr(MinimalFake(), "set_thinking", None))
        self.assertIsInstance(MinimalFake(), KernelPort)


class BuildStampTests(unittest.TestCase):
    """D137：构建标识必须**永远能给出一个可读的值**（取不到也不能抛）。"""

    def test_stamp_is_a_short_human_readable_time(self) -> None:
        import re

        from logox.tui.buildinfo import code_root, code_stamp

        stamp = code_stamp(refresh=True)
        self.assertTrue(re.fullmatch(r"\d{2}-\d{2} \d{2}:\d{2}|\?", stamp), f"格式不对：{stamp!r}")
        self.assertTrue(code_root().is_dir(), "被监视的源码根目录不存在")


class _ImportBoundaryTests(unittest.TestCase):
    """E-14 / E-15：界面层的 import 边界（MODULE_tui_integration §2.2 的 B1/B2/B4）。

    **为什么必须用 AST 断言而不是靠自觉**：M1.5 的文档只禁止了 ``tui/`` 引用
    ``providers`` / ``tools`` / ``store`` / ``permissions``，但那时 ``kernel.loop``
    还不存在，于是"界面偷偷 import 内核类、自己 new 一个"这条路**从未被宣布为禁止**
    ——而它恰恰是最省事的写法。没有断言的红线迟早会破（M3 已经吃过一次：
    内核想直接 import `providers.pricing` 时被同一条断言拦下）。

    代价必须说清楚：破了这条线，**界面测试就得装齐 Provider SDK 才能跑**。
    """

    #: `tui/` 允许引用的 `logox.*` 前缀
    #:
    #: ⚠️ `logox.difftext`（D140）：**中立叶子模块** —— diff 的数据类型与解析函数。
    #: 它必须两边都能用（工具层的生成侧 + 界面层的渲染侧），所以家放在**两层之下**，
    #: 与 `logox.errors` / `logox.paths` 同级。往这个元组里加东西前先问一句：
    #: **这个模块自己 import 了谁？** 中立叶子只允许依赖标准库。
    TUI_ALLOWED = (
        "logox.errors",
        "logox.paths",
        "logox.config",  # 只读配置模型与主题（R3 允许：配置不是内核状态）
        "logox.difftext",  # 中立叶子：diff 的数据类型与解析（零项目内依赖）
        "logox.permission_types",  # 中立叶子：权限问答的数据形状（同上，D140）
        "logox.kernel",  # 仅限 events / bus / port —— 具体实现由下一条断言单独禁止
        "logox.tui",
    )

    def _tui_files(self) -> list[Any]:
        """`tui/` 包下的全部模块。

        **`logox/app.py` 不在这里**——它是 :mod:`logox.tui` 的**兄弟**，属 L2 装配根，
        本来就该跨层（B4）。用路径前缀遍历时容易顺手把它一起扫进来，
        于是断言会在一个"本不该受此约束"的文件上报错。
        """
        from pathlib import Path

        tui = Path(__file__).resolve().parents[2] / "src" / "logox" / "tui"
        return sorted(tui.rglob("*.py"))

    def test_t41_tui_imports_only_events_bus_and_own_package(self) -> None:
        import ast

        scanned = 0
        for path in self._tui_files():
            scanned += 1
            source = path.read_text(encoding="utf-8")
            modules = {
                node.module
                for node in ast.walk(ast.parse(source))
                if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module
            }
            for module in sorted(modules):
                if not module.startswith("logox"):
                    continue
                with self.subTest(file=path.name, module=module):
                    self.assertTrue(
                        any(module == item or module.startswith(item + ".") for item in self.TUI_ALLOWED),
                        f"{path.name} 引用了不允许的模块 {module}；"
                        f"tui/ 只允许 {self.TUI_ALLOWED}",
                    )
        self.assertGreaterEqual(scanned, 10, "应扫描到 tui 包的绝大部分模块")

    def test_t41b_tui_never_imports_the_concrete_kernel(self) -> None:
        """把最容易破的那一条单独钉死：**界面不得 import `kernel.loop` / `kernel.registry`**。

        一旦破了，界面测试就必须构造 `KernelLoop`（要 Provider），
        而 `tests/tui/tui_support.FakeKernel` 那套"三成员假内核"也就失去意义了。
        """
        import ast

        forbidden = (
            "logox.kernel.loop",
            "logox.kernel.registry",
            "logox.kernel.scheduler",
            "logox.kernel.turn",
            "logox.kernel.port",  # 协议可以引用，但**不能构造**——见下条
        )
        for path in self._tui_files():
            source = path.read_text(encoding="utf-8")
            modules = {
                node.module
                for node in ast.walk(ast.parse(source))
                if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module
            }
            hits = [module for module in modules if module in forbidden]
            with self.subTest(file=path.name):
                if path.name != "app.py":
                    self.assertEqual(hits, [], f"{path.name} 不得 import 具体内核实现：{hits}")
                else:
                    # `app.py` 是**唯一例外**：它要写 `kernel: KernelPort | None` 的类型注解
                    self.assertLessEqual(set(hits), {"logox.kernel.port"}, f"app.py 只能额外引协议：{hits}")

    def test_t42_app_py_is_the_only_cross_layer_file(self) -> None:
        """B4：`app.py` 是**唯一**允许同时认识 L2–L5 的文件（它就是装配根）。

        同时断言 `cli.py` 顶层仍然干净：`--version` 的 300ms 红线靠它守住
        （快速路径不得加载配置层、界面层与 pydantic）。
        """
        import ast
        from pathlib import Path

        src = Path(__file__).resolve().parents[2] / "src" / "logox"

        app_modules = {
            node.module
            for node in ast.walk(ast.parse((src / "app.py").read_text(encoding="utf-8")))
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module
        }
        # 装配根必须真的跨层：内核、Provider 注册表、工具、界面
        for required in ("logox.kernel.loop", "logox.providers.registry", "logox.tools.fs_read"):
            with self.subTest(required=required):
                self.assertIn(required, app_modules, f"装配根应当引用 {required}")

        cli_source = (src / "cli.py").read_text(encoding="utf-8")
        top_level: set[str] = set()
        for node in ast.parse(cli_source).body:
            if isinstance(node, ast.Import):
                top_level.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                top_level.add(node.module.split(".")[0])
        for name in ("pydantic", "textual", "rich"):
            with self.subTest(imported=name):
                self.assertNotIn(name, top_level, f"cli.py 顶层不得 import {name}")
        self.assertNotIn("logox.app", top_level, "app.py 必须在函数体内惰性导入")

    #: 反向红线允许的例外：**装配根与启动入口**。
    #: `app.py` 的职责就是"认识所有层并把它们接起来"（T42 已经单独断言它是唯一跨层文件）；
    #: `cli.py` 是进程入口，它的界面 import 写在函数体里（懒加载），
    #: `--version` 快路径不加载界面层这件事由 T43 用子进程探针单独守着。
    TUI_IMPORTERS_ALLOWED = ("app.py", "cli.py")

    def _non_tui_files(self) -> list[Any]:
        """`src/logox/` 下**除 `tui/` 包以外**的全部模块。"""
        from pathlib import Path

        root = Path(__file__).resolve().parents[2] / "src" / "logox"
        return sorted(path for path in root.rglob("*.py") if "tui" not in path.relative_to(root).parts)

    #: 根下的**中立叶子模块**：两个以上层共用的小模块。
    #: 判据是"零项目内依赖、只 import 标准库"—— 一旦它们开始 import `logox.*`，
    #: 就不再中立（会重新引入"谁依赖谁"的问题），也就不该留在根下。
    NEUTRAL_LEAVES = (
        "errors.py",
        "paths.py",
        "difftext.py",  # D140：diff 的数据类型与解析
        "permission_types.py",  # D140：权限问答的数据形状
    )

    def test_t45_neutral_leaf_modules_stay_free_of_project_deps(self) -> None:
        """★ **D140**：中立叶子必须**零项目内依赖**（只 import 标准库）。

        为什么必须有这条：`difftext.py` / `permission_types.py` 之所以能同时被
        工具层与界面层引用，**唯一的原因**就是它们不依赖任何 `logox.*` 模块。
        哪天有人图方便在里面 import 了 `logox.config` 或 `logox.kernel`，
        它立刻变成一个"需要分层判断"的模块 —— 而**这件事不会有任何现象**：
        测试照样绿、界面照样跑，只是那条架构一致性悄悄没了。
        """
        import ast
        from pathlib import Path

        root = Path(__file__).resolve().parents[2] / "src" / "logox"
        for name in self.NEUTRAL_LEAVES:
            path = root / name
            with self.subTest(module=name):
                self.assertTrue(path.is_file(), f"中立叶子 {name} 不见了")
                tree = ast.parse(path.read_text(encoding="utf-8"))
                deps: list[str] = []
                for node in ast.walk(tree):
                    if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                        if node.module == "logox" or node.module.startswith("logox."):
                            deps.append(node.module)
                    elif isinstance(node, ast.Import):
                        deps += [
                            alias.name
                            for alias in node.names
                            if alias.name == "logox" or alias.name.startswith("logox.")
                        ]
                self.assertEqual(
                    deps,
                    [],
                    f"{name} 不再中立：它 import 了 {deps}。"
                    f"中立叶子只允许标准库 —— 需要项目内依赖就该换位置，"
                    f"而不是破坏这个不变量（见 docs/CHANGE-020 §2.1）",
                )

    def test_t44_only_the_composition_root_may_import_the_ui(self) -> None:
        """★ **D140 / F-50**：**依赖方向**红线 —— 除了装配根，谁也不许 import 界面层。

        为什么需要这条：项目原有四条边界断言（T41 / T41b / T42 / T43）**全是查"界面往上伸手"**，
        没有一条查"下面往上伸手"。于是 `tools/fs_edit.py` 直接
        ``from logox.tui.content.cards import parse_unified_diff`` 这件事
        **一路绿灯地躺了很久**（实测：`import logox.tools.base` 会连带加载
        `tui` / `tui.content` / `tui.content.cards` / `tui.format` 四个界面模块
        —— 工具层"可以脱离界面独立使用"这条声明当时是**假的**）。

        破了它会怎样：

        * **循环 import 风险**：界面迟早要用工具层的数据（如 `/tools` 显示参数 schema），
          一旦 `tui → tools` 也出现，就成了环。Python 只在部分加载顺序下能容忍，
          症状是"测试里好好的、换个入口就 ImportError"；
        * **headless 能力静默失效**：脚本 / 服务 / CI 里直接跑工具时会拖进渲染栈
          （`rich.text`、主题色板），今天是几毫秒的代价，将来可能是副作用。

        为什么允许 `app.py` / `cli.py`：它们是**装配根与进程入口**，
        职责就是"认识所有层"（见 `TUI_IMPORTERS_ALLOWED` 的注释）。
        """
        import ast
        from dataclasses import dataclass as _dataclass

        @_dataclass
        class _Hit:
            module: str
            symbol: str

        scanned = 0
        for path in self._non_tui_files():
            if path.name in self.TUI_IMPORTERS_ALLOWED:
                continue
            scanned += 1
            tree = ast.parse(path.read_text(encoding="utf-8"))
            hits: list[Any] = []
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    if node.module == "logox.tui" or node.module.startswith("logox.tui."):
                        hits.append(_Hit(node.module, "*"))
                elif isinstance(node, ast.Import):
                    hits.extend(
                        _Hit(alias.name, "*")
                        for alias in node.names
                        if alias.name == "logox.tui" or alias.name.startswith("logox.tui.")
                    )
            with self.subTest(file=path.name):
                self.assertEqual(
                    [hit.module for hit in hits],
                    [],
                    f"{path.name} 反向依赖了界面层：{[hit.module for hit in hits]}；"
                    f"工具/内核/存储等层不得 import logox.tui.*"
                    f"（共享的数据类型请放到中立叶子模块，见 logox/difftext.py）",
                )
        self.assertGreaterEqual(scanned, 20, "应扫描到界面层以外的绝大部分模块")

    def test_t43_app_module_is_not_loaded_by_the_version_fast_path(self) -> None:
        """`--version` 不能把装配根（以及它拖来的 Provider/工具）一起加载进来。"""
        import os
        import subprocess
        import sys
        from pathlib import Path

        from tests.unit.test_imports import SRC_DIR

        env = dict(os.environ)
        env["PYTHONPATH"] = str(SRC_DIR)
        env["PYTHONUTF8"] = "1"
        # 沙箱禁止管道捕获子进程输出（DEV-ENVIRONMENT §4.2）→ 重定向到文件再读
        out_path = Path(__file__).resolve().parents[2] / ".test-tmp" / "app-modules-probe.txt"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        code = (
            "import logox.cli, sys;"
            "mods=[m for m in sys.modules if m.startswith('logox.')];"
            f"open(r'{out_path}','w',encoding='utf-8').write('\\n'.join(mods))"
        )
        subprocess.run(  # noqa: S603 - 固定参数，无 shell
            [sys.executable, "-c", code], check=True, env=env, capture_output=True
        )
        loaded = out_path.read_text(encoding="utf-8").splitlines()
        self.assertNotIn("logox.app", loaded, "cli.py 顶层导入不得拖入装配根")
        self.assertNotIn("logox.tui", loaded, "cli.py 顶层导入不得拖入界面层")
        self.assertNotIn("pydantic", "\n".join(loaded))


if __name__ == "__main__":
    unittest.main()
