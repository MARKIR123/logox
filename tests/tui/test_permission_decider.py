"""权限决策器的测试（D10 / UI-SPEC §5.8）。

这一层是**内核与界面之间的翻译**，而它出错的方式全都指向同一个方向：
**把"没问过"变成"允许"**。所以这里的断言几乎每一条都在检查"失败时是不是拒绝"。

* 没有界面在听 → 交给调度器按拒绝处理（并说明原因）
* 提问抛异常 → 拒绝
* 界面返回意料之外的东西 → 拒绝
* "持久允许" → 真的写进 `state.toml`，而且**重启后仍然生效**
"""

from __future__ import annotations

import unittest
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from logox.app import UiPermissionDecider, _format_args
from logox.kernel.scheduler import Decision
from logox.permission_types import PermissionAsk, PermissionChoice


@dataclass
class FakeSpec:
    name: str = "shell"
    readonly: bool = False


@dataclass
class FakeTool:
    spec: FakeSpec = field(default_factory=FakeSpec)


@dataclass
class FakeCall:
    name: str = "shell"
    args: dict[str, Any] = field(default_factory=lambda: {"command": "npm install left-pad"})
    call_id: str = "c1"


class FakePrompter:
    """假界面：脚本化回答，并记住问了什么。"""

    def __init__(self, *answers: Any) -> None:
        self.answers = list(answers)
        self.asks: list[PermissionAsk] = []

    async def ask_permission(self, ask: PermissionAsk) -> Any:
        self.asks.append(ask)
        if not self.answers:
            raise AssertionError("决策器问了更多次，但脚本里没有答案了")
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


class FakeState:
    def __init__(self) -> None:
        self.learned: list[tuple[str, str]] = []

    def learn_permission(self, kind: str, rule: str) -> bool:
        self.learned.append((kind, rule))
        return True


class NoPrompterTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_prompter_falls_back_to_ask(self) -> None:
        """★ 没有界面在听 → 返回 ``ask``，由调度器按拒绝处理**并说明原因**。

        这里**不能**直接返回 ``deny``：那样一个事件都不会发，用户只看到
        "工具失败了"却不知道为什么（调度器那条路径本来就会解释
        "当前没有权限确认界面，已按拒绝处理"）。
        """
        decider = UiPermissionDecider(cwd=".")
        self.assertIs(await decider.decide(FakeCall(), FakeTool(), None), Decision.ASK)

    async def test_unregistered_prompter_also_falls_back(self) -> None:
        """界面退出时会把自己注销（`InlineApp.stop`）——之后必须回到安全路径。"""
        decider = UiPermissionDecider(cwd=".")
        decider.prompter = FakePrompter(PermissionChoice.ONCE)
        decider.prompter = None
        self.assertIs(await decider.decide(FakeCall(), FakeTool(), None), Decision.ASK)


class ChoiceTests(unittest.IsolatedAsyncioTestCase):
    def _decider(self, *answers: Any) -> tuple[UiPermissionDecider, FakePrompter, FakeState]:
        decider = UiPermissionDecider(cwd="G:/proj")
        prompter = FakePrompter(*answers)
        state = FakeState()
        decider.prompter = prompter
        decider.state_store = state
        return decider, prompter, state

    async def test_allow_once_runs_and_asks_again_next_time(self) -> None:
        """★ "仅本次允许"**只**放行这一次——下一次必须再问。"""
        decider, prompter, state = self._decider(PermissionChoice.ONCE, PermissionChoice.ONCE)
        self.assertIs(await decider.decide(FakeCall(), FakeTool(), None), Decision.ALLOW)
        self.assertIs(await decider.decide(FakeCall(), FakeTool(), None), Decision.ALLOW)
        self.assertEqual(len(prompter.asks), 2, "仅本次允许却记住了")
        self.assertEqual(state.learned, [])

    async def test_allow_session_stops_asking(self) -> None:
        """★ "本会话总是允许"之后**不再打扰**（这是那个选项存在的全部意义）。"""
        decider, prompter, _state = self._decider(PermissionChoice.SESSION)
        self.assertIs(await decider.decide(FakeCall(), FakeTool(), None), Decision.ALLOW)
        self.assertIs(await decider.decide(FakeCall(), FakeTool(), None), Decision.ALLOW)
        self.assertEqual(len(prompter.asks), 1, "会话内允许之后又问了一次")

    async def test_allow_project_persists_and_stops_asking(self) -> None:
        """★ "持久允许"要**真的写盘**，否则那个按钮只在本进程里有效。"""
        decider, prompter, state = self._decider(PermissionChoice.PROJECT)
        self.assertIs(await decider.decide(FakeCall(), FakeTool(), None), Decision.ALLOW)
        self.assertEqual(state.learned, [("allow", "shell:npm install")])
        self.assertIs(await decider.decide(FakeCall(), FakeTool(), None), Decision.ALLOW)
        self.assertEqual(len(prompter.asks), 1)

    async def test_deny_is_not_remembered(self) -> None:
        """四个选项里**没有**"总是拒绝"，所以拒绝只影响这一次。"""
        decider, prompter, state = self._decider(PermissionChoice.DENY, PermissionChoice.DENY)
        self.assertIs(await decider.decide(FakeCall(), FakeTool(), None), Decision.DENY)
        self.assertIs(await decider.decide(FakeCall(), FakeTool(), None), Decision.DENY)
        self.assertEqual(len(prompter.asks), 2)
        self.assertEqual(state.learned, [])

    async def test_persisted_rules_are_honoured_at_startup(self) -> None:
        """★ 启动时载入持久规则——不载入的话"持久允许"在重启后失效。

        那种 bug 的症状是"我明明允许过，它还问我"——用户会以为按钮坏了。
        """
        decider, prompter, _state = self._decider()
        decider.seed_persisted(["shell", "write"])
        self.assertIs(await decider.decide(FakeCall(), FakeTool(), None), Decision.ALLOW)
        self.assertEqual(prompter.asks, [], "持久允许过的工具不该再问")

    async def test_state_is_per_tool(self) -> None:
        """允许 ``shell`` 不等于允许 ``write``。"""
        decider, prompter, _state = self._decider(PermissionChoice.SESSION, PermissionChoice.DENY)
        self.assertIs(await decider.decide(FakeCall(name="shell"), FakeTool(), None), Decision.ALLOW)
        self.assertIs(
            await decider.decide(
                FakeCall(name="write"), FakeTool(spec=FakeSpec(name="write")), None
            ),
            Decision.DENY,
        )
        self.assertEqual(len(prompter.asks), 2)


class FailureModeTests(unittest.IsolatedAsyncioTestCase):
    """★ 权限路径上的**每一种失败都必须是拒绝**。"""

    async def _decide_with(self, prompter: Any) -> Any:
        decider = UiPermissionDecider(cwd=".")
        decider.prompter = prompter
        return await decider.decide(FakeCall(), FakeTool(), None)

    async def test_prompting_exception_denies(self) -> None:
        """界面炸了（没有事件循环、浮层构造失败……）→ 拒绝，绝不冒泡。"""
        self.assertIs(await self._decide_with(FakePrompter(RuntimeError("界面炸了"))), Decision.DENY)

    async def test_unexpected_return_value_denies(self) -> None:
        """界面返回 ``None`` 或字符串 → 拒绝（那些都不是"允许"）。"""
        for answer in (None, "allow", True, 1):
            with self.subTest(answer=answer):
                self.assertIs(await self._decide_with(FakePrompter(answer)), Decision.DENY)

    async def test_write_failure_still_allows_this_time(self) -> None:
        """`state.toml` 写不进去时：**本次已经答应了**，只是没被记住（P-5）。"""

        class ExplodingState:
            def learn_permission(self, kind: str, rule: str) -> bool:
                raise OSError("磁盘满了")

        decider = UiPermissionDecider(cwd=".")
        decider.prompter = FakePrompter(PermissionChoice.PROJECT)
        decider.state_store = ExplodingState()
        self.assertIs(await decider.decide(FakeCall(), FakeTool(), None), Decision.ALLOW)


class AskContentTests(unittest.IsolatedAsyncioTestCase):
    """弹窗上显示什么——**这些字段就是用户做判断的全部依据**。"""

    def _build(self, **call: Any) -> PermissionAsk:
        decider = UiPermissionDecider(cwd="G:/hz/codes/Logox")
        return decider._build_ask(FakeCall(**call), FakeTool(), "shell")  # noqa: SLF001

    async def test_full_arguments_are_included(self) -> None:
        """★ 完整参数（**不截断**）——用户得看得见到底要执行什么。"""
        ask = self._build(args={"command": "npm install " + "x" * 300})
        self.assertIn("x" * 300, ask.detail)
        self.assertIn("npm install", ask.detail)

    async def test_multiline_arguments_stay_readable(self) -> None:
        ask = self._build(args={"path": "a.txt", "content": "第一行\n第二行"})
        self.assertIn("path: a.txt", ask.detail)
        self.assertIn("  第一行", ask.detail)

    async def test_rule_is_empty_until_the_rule_engine_exists(self) -> None:
        """★ 规则引擎（M5）还没落地 → **留空**，界面就不显示那一行。

        编一个看起来很像的规则名（"builtin.shell 非白名单"）会让用户
        以为自己看懂了命中逻辑，而那是假的。
        """
        ask = self._build()
        self.assertEqual(ask.rule, "")
        self.assertEqual(ask.rule_scope, "")

    async def test_non_readonly_tools_are_flagged_high_risk(self) -> None:
        """非只读工具一律标高危：**保守方向**（多警示比少警示安全）。"""
        ask = self._build()
        self.assertEqual(ask.risk, "high")
        self.assertIn("不是只读", ask.risk_note)

    async def test_readonly_tools_are_normal_risk(self) -> None:
        decider = UiPermissionDecider(cwd=".")
        ask = decider._build_ask(  # noqa: SLF001
            FakeCall(), FakeTool(spec=FakeSpec(readonly=True)), "read"
        )
        self.assertEqual(ask.risk, "normal")

    async def test_cwd_and_call_id_are_carried(self) -> None:
        ask = self._build(call_id="call-7")
        self.assertEqual(ask.cwd, "G:/hz/codes/Logox")
        self.assertEqual(ask.call_id, "call-7")


class FormatArgsTests(unittest.TestCase):
    def test_empty_args(self) -> None:
        self.assertEqual(_format_args({}), "")

    def test_one_line_per_argument(self) -> None:
        self.assertEqual(
            _format_args({"path": "a.txt", "n": 3}), "path: a.txt\nn: 3"
        )

    def test_long_values_are_not_truncated(self) -> None:
        text = _format_args({"content": "行" * 500})
        self.assertIn("行" * 500, text)

    def test_non_dict_args_do_not_crash(self) -> None:
        """模型有时会给出奇怪的东西 —— 权限弹窗不该因此炸掉。"""
        self.assertEqual(_format_args(None), "")
        self.assertEqual(_format_args("oops"), "")


class WiringTests(unittest.TestCase):
    """装配根必须把决策器真的交给内核（漏了它 = 权限形同虚设）。"""

    def test_build_runtime_installs_the_decider(self) -> None:
        import tempfile

        from logox.app import build_runtime
        from logox.config.loader import load
        from logox.paths import LogoxPaths
        from logox.providers.registry import BUILTIN_SPECS

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "proj"
            (project / ".logox").mkdir(parents=True)
            (project / ".logox" / "config.toml").write_text(
                'schema_version = 1\n[provider]\nname = "ollama"\nmodel = "qwen3:8b"\n',
                encoding="utf-8",
            )
            paths = LogoxPaths.at(root / "home")
            paths.ensure_dirs()
            bundle = load(project, paths=paths, known_providers=tuple(BUILTIN_SPECS))
            runtime = build_runtime(bundle, project, paths)
            self.assertFalse(hasattr(runtime, "exit_code"), "装配失败了")

            decider = runtime.permission_decider
            self.assertIsInstance(decider, UiPermissionDecider)
            self.assertIs(runtime.kernel._decider, decider)  # noqa: SLF001
            # 还没有界面注册 → 决策器必须落在"问不了"这条安全路径上
            self.assertIsNone(decider.prompter)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
