"""会话概览与用量大盘命令（/summary）单元测试（D131 / MODULE_summary_command.md）。

验证核心契约：
1. 演进脉络提取：支持纯对话轮次、文件改动轮次、多文件省略展示、摘要来源标注；
2. 用量统计：Token 总量、输入/输出/缓存细分与缓存命中率；
3. 工具与耗时：工具调用次数与累计耗时呈现；
4. 费用估算：内置模型查表估算、未知模型不瞎猜、零消耗兜底；
5. Slash 命令流程：呼起只读 PanelComponent 浮层，支持 Esc 关闭与滚动。
"""

from __future__ import annotations

import unittest
from pathlib import Path
from typing import Any

from logox.store.checkpoint import FileSnapshot, TurnCheckpoint
from logox.tui.commands import ResolvedCommand
from logox.tui.metrics import MetricsReducer, SessionMetrics
from logox.tui.render.commands import CommandRunner, _render_summary_content
from logox.tui.theme import load_theme


class FakeSummaryHost:
    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime
        self.theme = load_theme("logox-dark")
        self.effort = "auto"
        self.content_width = 100
        self.content_rows = 24
        self.overlays: list[Any] = []
        self.notices: list[str] = []

    async def push_overlay(self, component: Any, *, max_rows: int | None = None) -> Any:
        del max_rows
        self.overlays.append(component)
        return None

    def notice(self, message: str, *, token: str = "text_muted") -> None:
        del token
        self.notices.append(message)


class FakeSummaryRuntime:
    def __init__(
        self,
        checkpoints: list[TurnCheckpoint] | None = None,
        metrics: SessionMetrics | None = None,
        model: str = "deepseek-v4-pro",
    ) -> None:
        self._checkpoints = list(checkpoints or [])
        self.reducer = MetricsReducer()
        if metrics is not None:
            self.reducer.metrics = metrics
        self.model = model
        self.state_store = None

    def list_checkpoints(self) -> list[TurnCheckpoint]:
        # app.py 返回倒序
        return list(reversed(self._checkpoints))


class SummaryContentRenderingTests(unittest.TestCase):
    """排版与格式化测试（_render_summary_content）。"""

    def setUp(self) -> None:
        self.palette = load_theme("logox-dark").palette

    def test_empty_session_rendering(self) -> None:
        """空会话：显示当前尚无交互轮次，指标显示 0。"""
        metrics = SessionMetrics()
        body = _render_summary_content([], metrics, self.palette, model="deepseek-v4-pro")
        plain = body.plain

        self.assertIn("当前会话尚无交互轮次", plain)
        self.assertIn("Token 总量：0 tokens", plain)
        self.assertIn("工具调用：0 次", plain)
        self.assertIn("$0 USD", plain)

    def test_chronology_with_dialogue_and_files(self) -> None:
        """轮次演进：区分纯对话轮次与文件改动轮次。"""
        cp1 = TurnCheckpoint(
            turn=1,
            files=[],
            user_prompt="你好，帮我分析下项目结构",
            turn_summary="分析当前工程骨架并梳理模块分工",
        )
        cp2 = TurnCheckpoint(
            turn=2,
            files=[
                FileSnapshot(path="src/auth.py", before_hash="", after_hash="h1"),
                FileSnapshot(path="src/token.py", before_hash="", after_hash="h2"),
                FileSnapshot(path="src/config.py", before_hash="", after_hash="h3"),
            ],
            user_prompt="实现 JWT 鉴权模块",
            turn_summary="编写 JWT 签发算法与核心配置",
            summary_source="deterministic",
        )
        cp3 = TurnCheckpoint(
            turn=3,
            files=[FileSnapshot(path="tests/test_auth.py", before_hash="", after_hash="h4")],
            user_prompt="运行测试并修复边界用例",
            turn_summary="修复 test_jwt_expire 边界断言",
            summary_source="model_fallback",
        )

        metrics = SessionMetrics(
            usage_input=12_000,
            usage_output=4_000,
            cached_input=2_000,
            tool_calls=6,
            tool_ms=1800,
            cost_usd=0.0038,
            model="deepseek-v4-pro",
        )

        body = _render_summary_content([cp1, cp2, cp3], metrics, self.palette, model="deepseek-v4-pro")
        plain = body.plain

        # 轮次 1：纯对话
        self.assertIn("轮次 1", plain)
        self.assertIn("纯对话 (无文件修改)", plain)
        self.assertIn("意图: 分析当前工程骨架并梳理模块分工", plain)

        # 轮次 2：改动 3 个文件（包含等 N 个文件折叠与自动摘要标记）
        self.assertIn("轮次 2", plain)
        self.assertIn("改动 3 个文件: auth.py, token.py 等 3 个文件", plain)
        self.assertIn("自动摘要", plain)

        # 轮次 3：模型补写标记
        self.assertIn("轮次 3", plain)
        self.assertIn("改动 1 个文件: test_auth.py", plain)
        self.assertIn("模型补写", plain)

        # 全局用量看板
        self.assertIn("16,000 tokens", plain)
        self.assertIn("输入: 12,000 tok", plain)
        self.assertIn("输出: 4,000 tok", plain)
        self.assertIn("缓存命中: 2,000 tok (17%)", plain)
        self.assertIn("工具调用：6 次 (累计耗时 1.8s)", plain)
        self.assertIn("$0.0038 USD", plain)

    def test_pricing_estimation_for_known_and_unknown_models(self) -> None:
        """测试预估费用：已知模型查表计算 vs 未知模型提示未定价。"""
        from logox.providers.pricing import estimate_cost_usd

        # 1. 未知模型且 cost_usd == 0
        metrics_unknown = SessionMetrics(
            usage_input=10_000,
            usage_output=2_000,
            cost_usd=0.0,
        )
        body_unknown = _render_summary_content(
            [], metrics_unknown, self.palette, model="custom-finetuned-llm", cost_estimator=estimate_cost_usd
        )
        self.assertIn("未知模型定价", body_unknown.plain)

        # 2. 已知模型且 cost_usd == 0 时自动查表估算
        metrics_known = SessionMetrics(
            usage_input=1_000_000,
            usage_output=1_000_000,
            cached_input=0,
            cost_usd=0.0,
        )
        body_known = _render_summary_content(
            [], metrics_known, self.palette, model="deepseek-flash", cost_estimator=estimate_cost_usd
        )
        # deepseek-flash: input $0.30/M, output $1.20/M -> $1.50
        self.assertIn("$1.50 USD", body_known.plain)


class SummaryCommandFlowTests(unittest.IsolatedAsyncioTestCase):
    """Slash 命令 /summary 浮层呼起流程测试。"""

    async def test_cmd_summary_opens_overlay_panel(self) -> None:
        cp = TurnCheckpoint(turn=1, files=[], user_prompt="test", turn_summary="init")
        runtime = FakeSummaryRuntime(checkpoints=[cp])
        host = FakeSummaryHost(runtime)
        runner = CommandRunner(host)  # type: ignore[arg-type]

        await runner.run(ResolvedCommand(raw="/summary", name="summary", argument="", state="ready"))

        self.assertEqual(len(host.overlays), 1)
        panel = host.overlays[0]
        self.assertEqual(type(panel).__name__, "PanelComponent")
        self.assertIn("Esc 关闭", panel.footer)
        self.assertIn("会话概览与用量大盘", panel.text.plain)
        self.assertIn("轮次 1", panel.text.plain)
