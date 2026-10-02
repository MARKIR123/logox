"""新界面的测试脚手架：**真内核 + 真总线 + 真界面**。

为什么要有它
============

界面之外的东西都可以用假替身糊过去（`FakeKernel`、`FakeTerminal`），
但有几类缺陷**只有在真东西连起来时才会现形**——而且每一条都是实测踩过的：

* 决策器没被装进内核 → 权限确认**一次都不触发**，而每个单测都通过；
* 订阅发生在 `run()` 里而不是构造时 → 启动瞬间的事件静默丢失；
* `.env` 没被加载 → 用户存好的密钥不生效，而设置看着正常。

所以 :func:`build_inline` 装配的是**真的 `KernelLoop` + 真的 `EventBus` +
真的 `InlineApp`**，只把两处最外层换成可控的：脚本化 provider（决定模型说什么）
与 `FakeTerminal`（决定字节写到哪）。

它取代了历史上那份 Textual 专用的 `tui_support.py`（随 D85 一起删除）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from logox.config.schema import LogoxConfig, ProviderConfig
from logox.kernel.bus import EventBus
from logox.kernel.loop import KernelLoop, SimpleContextBuilder
from logox.kernel.registry import ToolRegistry
from logox.tui.metrics import MetricsReducer
from logox.tui.render.app import InlineApp
from logox.tui.render.terminal import FakeTerminal
from tests.unit.kernel_support import scripted_provider

__all__ = ["InlineHarness", "build_inline", "make_config"]

SESSION = "render-support"


def make_config(**ui_kwargs: Any) -> LogoxConfig:
    """一份最小可用配置（界面要读主题、状态栏开关与思考档位）。"""
    from logox.config.schema import UiConfig

    return LogoxConfig(
        provider=ProviderConfig(name="openai-compatible", model="mock-model"),
        ui=UiConfig(**ui_kwargs),
    )


class _CapturingProvider:
    """把内核真正发出的每个 `ChatRequest` 记下来（断言"档位生效"要从它读）。"""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.name = getattr(inner, "name", "capturing")
        self.requests: list[Any] = []

    def list_models(self) -> Any:
        return self._inner.list_models()

    def stream(self, request: Any) -> Any:
        self.requests.append(request)
        return self._inner.stream(request)


@dataclass
class InlineHarness:
    """一套"真内核 + 真总线 + 真界面"的装配结果。"""

    app: InlineApp
    kernel: KernelLoop
    bus: EventBus
    registry: ToolRegistry
    terminal: FakeTerminal
    requests: list[Any] = field(default_factory=list)
    """内核实际发出的每个 `ChatRequest`（按顺序）。"""
    turns: list[Any] = field(default_factory=list)
    """`kernel.start()` 开出来的每个 `Turn`。

    **为什么需要它**：断言"回合跑完了"不能靠轮询状态栏的 busy 标记——
    那是事件驱动更新的，测试在事件流回来之前去看会读到过时的值（假通过或假失败）。
    直接 `await harness.turns[0].task` 才是确定的。
    """


class _Runtime:
    """`InlineApp` 需要的最小运行时（鸭子类型；**故意不 import 真正的装配根**）。"""

    def __init__(
        self,
        *,
        bus: EventBus,
        kernel: KernelLoop,
        decider: Any,
        cwd: Path,
        tool_names: list[str],
        config: LogoxConfig,
    ) -> None:
        self.bus = bus
        self.kernel = kernel
        self.reducer = MetricsReducer()
        self.model = "mock-model"
        self.provider_name = "openai-compatible"
        self.cwd = cwd
        self.tools = tool_names
        self.warnings: list[str] = []
        self.needs_login = False
        self.state_store = None
        self.env_file = None
        self.config = config
        self.permission_decider = decider


def build_inline(
    script: list[list[Any]],
    *,
    tools: list[Any] | None = None,
    decider: Any = None,
    cwd: str | Path = ".",
    rows: int = 30,
    columns: int = 100,
    config: LogoxConfig | None = None,
    session_start: Any = None,
) -> InlineHarness:
    """装配一套真内核 + 真界面。``script`` 是脚本化 provider 的分片序列。"""
    bus = EventBus(session_id=SESSION)
    registry = ToolRegistry()
    for tool in tools or []:
        registry.register(tool)

    capturing = _CapturingProvider(scripted_provider(script))
    cwd_path = Path(cwd)
    kernel = KernelLoop(
        bus,
        capturing,  # type: ignore[arg-type]
        registry,
        SimpleContextBuilder(system="你是测试助手"),
        decider,
        model="mock-model",
        cwd=cwd_path,
            # 测试默认**关掉**「再调一次模型补写摘要」（D135 第 2 层）：
        # 脚本化 provider 的短回答大多不合规，开着会让每个用工具的用例都多发一次请求，
        # 把「请求次数 / 用量」这类断言搅乱。要测第 2 层的用例显式把它打开。
        model_summary_fallback=False,
    )
    resolved_config = config or make_config()
    runtime = _Runtime(
        bus=bus,
        kernel=kernel,
        decider=decider,
        cwd=cwd_path,
        tool_names=[tool.spec.name for tool in (tools or [])],
        config=resolved_config,
    )
    terminal = FakeTerminal(columns=columns, rows=rows)
    app = InlineApp(runtime=runtime, terminal=terminal, session_start=session_start)

    harness = InlineHarness(
        app=app,
        kernel=kernel,
        bus=bus,
        registry=registry,
        terminal=terminal,
        requests=capturing.requests,
    )
    # 记录每个 Turn，便于 `await harness.turns[0].task`
    original_start = kernel.start

    async def recording_start(text: str) -> Any:
        turn = await original_start(text)
        harness.turns.append(turn)
        return turn

    kernel.start = recording_start  # type: ignore[method-assign]
    return harness


def screen_text(harness: InlineHarness) -> str:
    """屏幕上现在是什么（纯文本）。"""
    return harness.app.frame_text()


def start_runtime(harness: InlineHarness) -> list[Any]:
    """启动界面在 `run()` 里会启动的东西（屏 + 后台 ticker）。

    **测试必须调它**，否则测的就不是真实链路：

    * `TimelineBuffer.add_delta()` 只把增量放进缓冲，真正的"落块"由 ticker
      按 `ui.stream_fps` 做。漏掉这一步的测试会让"整段回答在回合结束时一次性蹦出来"
      这种缺陷**完全测不出来**——而那正是新界面最初的真实症状。
    * ticker 的循环条件是"屏还在跑"，所以得先真的把屏启动起来
      （`FakeTerminal.start()` 只是记下回调，没有副作用）。
    """
    import asyncio

    harness.app._loop = asyncio.get_running_loop()  # noqa: SLF001 - 脚手架本来就要碰内部状态
    harness.app.screen.start()
    return harness.app.start_tickers()
