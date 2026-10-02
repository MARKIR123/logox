"""装配根（L2）——**全程序唯一知道所有零件的地方**（M4 / D55 / R1 的唯一例外）。

它解决什么问题
==============

内核需要五样东西才能跑：事件总线、Provider、工具注册表、上下文组装器、权限决策器。
如果这些由内核自己去找（自己 ``import`` 具体实现），内核就「认识所有人」了——
从此无法单独测试、也无法替换（`ARCHITECTURE.md` 规则 R2）。

**没有它会怎样**：`import` 会在各层之间互相纠缠，你说不清「改这个会不会影响那个」。
本项目把「插线」这件事集中到这一个文件里，于是：

* `kernel/` 不知道谁实现了 `Provider`；
* `tui/` 不知道内核是哪个类（只认 :class:`~logox.kernel.port.KernelPort` 三成员协议）；
* M5/M6/M7 加东西时**界面一行都不用改**——只在这里多注册一个工具 / 换一个决策器。

一句话：**这是 R1（单向依赖）唯一被允许的例外，也是它成立的前提。**

本模块的职责
============

| # | 职责 |
|---|---|
| R1 | 读配置 → 建 Provider → 建工具注册表（含安全闸门）→ 建总线 → 建内核 → 生成主题 → 交给界面 |
| R2 | 发 ``SessionStart``（环境事实的公示）、退出时关总线 |
| R3 | **启动期四类失败给出可行动提示**（缺密钥 / 无模型 / 安全闸门 / 主题损坏），**不进全屏、不抛堆栈** |

**不负责**：不实现任何能力（那是 L3/L4/L5）、不渲染（那是 `tui/`）、不解析参数（那是 `cli.py`）。
"""

from __future__ import annotations

import contextlib
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NamedTuple

from logox.config.schema import LogoxConfig, ProviderConfig, ThemeFile
from logox.context import HierarchicalContextBuilder
from logox.kernel import events as ev
from logox.kernel.bus import EventBus
from logox.kernel.loop import KernelLoop
from logox.kernel.registry import ToolRegistry
from logox.paths import LogoxPaths
from logox.permission_types import PermissionAsk, PermissionChoice
from logox.tui.metrics import MetricsReducer
from logox.tui.theme import DEFAULT_THEME, load_theme

__all__ = [
    "Runtime",
    "ReloadItem",
    "ResourceReloadReport",
    "StartupError",
    "UiPermissionDecider",
    "build_runtime",
    "build_session_start",
    "prepare_runtime",
    "render_startup_error",
    "resolve_env_file",
]

logger = logging.getLogger("logox.app")

EXIT_OK = 0
EXIT_CONFIG_ERROR = 1
EXIT_NOT_READY = 3


class StartupError(NamedTuple):
    """启动期失败。**必须带"该怎么办"**，而不只是一句错误。

    四类错误各有稳定的 ``kind``，测试据此断言（`test_app_startup.py`），
    因此这里的取值是**契约**，不要随手改。
    """

    kind: str  # "provider" | "auth" | "model" | "unsafe_tools" | "unexpected"
    message: str
    hint: str = ""
    exit_code: int = EXIT_CONFIG_ERROR


@dataclass(frozen=True)
class ReloadItem:
    """`/reload` 里的一项资源：成功给 ``detail``，失败给 ``error``。"""

    name: str
    detail: str = ""
    error: str = ""


@dataclass(frozen=True)
class ResourceReloadReport:
    """一次资源重载的结果（`/reload`，见 docs/modules/08_app_and_collaboration.md §5.2）。

    ``prefix_changed`` 是这份报告里**最容易被忽略、却最该被说出来**的一项：
    记忆与技能索引都拼在系统提示里，而系统提示是厂商侧 KV 缓存的前缀。
    前缀一变，下一次请求那段就按全价重算。账本不会算错（锚点靠 `prefix_digest`
    自动失效），但用户有权知道"这一下多花了钱"。
    """

    items: list[ReloadItem] = field(default_factory=list)
    #: 重扫之后系统提示是否变了（决定要不要提示"前缀重算"）
    prefix_changed: bool = False
    system_tokens_before: int = 0
    system_tokens_after: int = 0
    #: 需要重启才生效的东西（**诚实清单**，不是错误）
    not_reloaded: list[str] = field(default_factory=list)

    @property
    def failures(self) -> list[ReloadItem]:
        return [item for item in self.items if item.error]


@dataclass
class Runtime:
    """一次会话的全部对象。``prepare_runtime`` 就是「配置 → 装配」，随后交给界面。"""

    bus: EventBus
    kernel: KernelLoop
    reducer: MetricsReducer
    theme: ThemeFile
    config: LogoxConfig
    provider_name: str
    model: str
    cwd: Path
    tools: list[str]
    state_store: Any | None = None
    warnings: list[str] = field(default_factory=list)
    #: 是否「还没登录」（缺密钥，当前 provider 是占位适配器）。
    #: 界面据此在启动时提示「运行 /login」，而不是让用户对着沉默的模型发呆。
    needs_login: bool = False
    #: Provider 注册表（``/login`` 与 ``/model`` 要靠它列举与构造适配器）。
    #:
    #: **它只在装配根与界面之间传递，界面不会 import ``providers/``**
    #: ——界面拿到的是一个对象，调它的方法即可（鸭子类型），B1/B2 红线因此不破。
    registry: Any | None = None
    #: 密钥文件（``.env``）的位置；``/login`` 成功且用户选择「记住」时写它。
    #:
    #: 为什么由装配根决定：找哪个文件是**环境知识**（项目级优先、其次用户级，
    #: 见 :func:`resolve_env_file`），界面不该自己猜。界面只拿到一个路径。
    env_file: Path | None = None
    #: 权限决策器。界面在构造时把 ``self`` 注册成它的 ``prompter`` 即可参与授权
    #: （见 :class:`UiPermissionDecider`）——**不注册就是"拒绝并说明原因"**。
    permission_decider: Any | None = None
    #: 上下文构建器与记忆管理器（/memory 与 /compact 命令直接操作它）
    context_builder: Any | None = None
    #: 正在恢复的历史会话文件路径（若为开启新会话则为 None）
    resume_file: Path | None = None
    #: 历史会话时间线回放器 (Callable[[Any], None])，装配根注入以保持架构单向依赖
    session_replayer: Any | None = None
    persistence_writer: Any | None = None
    paths: Any | None = None
    blob_store: Any | None = None
    mcp_manager: Any | None = None
    hook_runner: Any | None = None
    plugin_manager: Any | None = None
    command_manager: Any | None = None
    skill_manager: Any | None = None
    anamnesis: Any | None = None
    #: 运行时动态或测试注入的模型窗口缓存 (model_id -> context_window)
    _dynamic_model_windows: dict[Any, int] = field(default_factory=dict)
    #: 切模型前的上一个窗口容量（用于 D193 扩容安全豁免判定）
    _previous_window: int | None = None

    def window_for(self, model: str) -> int | None:
        """查某个模型的上下文窗口（查不到返回 None）。**纯静态、不发网络请求。**"""
        if model in self._dynamic_model_windows:
            return self._dynamic_model_windows[model]
        if self.registry is None:
            return None
        try:
            if hasattr(self.registry, "window_for"):
                win = self.registry.window_for(self.provider_name, model)
                if win is not None:
                    return win
            for info in self.registry.list_models(self.provider_name):
                if info.id == model:
                    return getattr(info, "context_window", None)
        except Exception as exc:  # noqa: BLE001 - 查不到不是错误，只是少了优化
            logger.debug("查询模型窗口失败（沿用当前窗口）：%s", exc)
        return None

    def _get_local_fallback_provider(self) -> tuple[Any, str] | None:
        """Use an explicitly selected/configured local endpoint; never a cloud fallback."""
        from urllib.parse import urlparse

        local_names = {"ollama", "lm-studio"}
        if self.provider_name in local_names:
            spec = self.registry.spec(self.provider_name) if self.registry else None
            url = str(getattr(spec, "base_url", "") or "http://127.0.0.1")
            if urlparse(url).hostname not in {"localhost", "127.0.0.1", "::1"}:
                return None
            provider = getattr(self.kernel, "_provider", None)
            if provider is not None and type(provider).__name__ != "MissingKeyProvider":
                return provider, getattr(self.kernel, "_model", self.model)
        configured = getattr(self.config, "providers", {})
        instance = configured.get("ollama") if isinstance(configured, dict) else None
        models = getattr(instance, "models", []) if instance is not None else []
        if not models or self.registry is None:
            return None
        spec = self.registry.spec("ollama")
        if urlparse(spec.base_url).hostname not in {"localhost", "127.0.0.1", "::1"}:
            return None
        try:
            return self.registry.build("ollama"), models[0]
        except Exception as exc:
            logger.warning("本地压缩模型不可用：%s", exc)
            return None

    async def _invoke_summarizer(
        self,
        provider: Any,
        model: str,
        transcript: str,
        target_tokens: int,
        timeout_s: float,
    ) -> str | None:
        import asyncio

        from logox.context.compaction import MEMO_SYSTEM_PROMPT
        from logox.kernel.messages import Message, TextBlock
        from logox.providers.base import (
            ChatRequest,
            DeltaEvent,
            ProviderErrorEvent,
            StopEvent,
        )

        user_prompt = (
            f"请将以下早期历史会话深度压缩为全局工作状态备忘录，目标输出长度约为 {target_tokens} tokens。\n\n"
            f"{transcript}"
        )
        request = ChatRequest(
            model=model,
            system=MEMO_SYSTEM_PROMPT,
            messages=[Message(role="user", blocks=[TextBlock(text=user_prompt)])],
            tools=[],
            temperature=0.2,
            max_tokens=max(1, min(target_tokens, 3000)),
            thinking=None,
        )
        chunks: list[str] = []
        try:
            async with asyncio.timeout(timeout_s):
                async for event in provider.stream(request):
                    if isinstance(event, DeltaEvent):
                        if event.kind != "reasoning" and event.text:
                            chunks.append(event.text)
                    elif isinstance(event, StopEvent):
                        break
                    elif isinstance(event, ProviderErrorEvent):
                        logger.warning("模型提炼备忘录发生供应商错误: %s", event.message)
                        return None
        except Exception as exc:
            logger.warning("模型提炼备忘录执行异常: %s", exc)
            return None

        result_text = "".join(chunks).strip()
        return result_text if result_text else None

    def create_memo_summarizer(self) -> Any:
        """Summarize extreme-window history through a configured local model only."""
        from logox.context.compaction import format_messages_for_summary

        async def _summarizer(messages: list[Any], *, target_tokens: int = 1500) -> str | None:
            transcript = format_messages_for_summary(messages)
            local = self._get_local_fallback_provider()
            if not transcript.strip() or local is None:
                return None
            provider, model = local
            from logox.context.compaction import MEMO_SYSTEM_PROMPT
            from logox.context.tokens import TokenEstimator

            if self.provider_name in {"ollama", "lm-studio"}:
                window = self.window_for(model)
            else:
                window = getattr(self.registry.spec("ollama"), "context_window", None)
            if isinstance(window, int) and window > 0:
                tokens = TokenEstimator().estimate_text(transcript + MEMO_SYSTEM_PROMPT) + 128
                available = window - tokens
                if available <= 0:
                    logger.warning("历史摘要输入超过本地压缩模型窗口，保留摘要并暂停")
                    return None
                target_tokens = min(target_tokens, available)
            return await self._invoke_summarizer(provider, model, transcript, target_tokens, timeout_s=30.0)

        return _summarizer

    async def apply_compact(self) -> Any | None:
        """手动压缩一次（`/compact`，D161）。返回 `CompactionReport`（没压出东西则 None）。

        为什么放在装配根：压缩在 `context/`（L4）里做，而**事件总线属于 L3** ——
        界面不该直接把事件塞进总线。装配根是唯一同时认识两侧的地方，所以由它
        ①调 builder ②发 `CompactionStarted`/`CompactionFinished` ③更新状态栏。
        界面只调这一个方法，与 `apply_model()` 同一形状。
        """
        builder = self.context_builder
        if builder is None or not (hasattr(builder, "force_compact") or hasattr(builder, "force_compact_async")):
            return None

        kernel = self.kernel
        history = list(getattr(kernel, "history", []) or [])
        last_usage = getattr(kernel, "_last_request_usage", None)

        # 先发「即将压缩」（钩子 `pre_compact` 在这里触发）——与自动路径保持一致
        plan = getattr(builder, "plan", None)
        if callable(plan):
            # force=True：手动压缩有意跳过水位线，判据换成"有没有可做的活"
            planned = plan(history, last_usage=last_usage, force=True)
            if planned is not None:
                await self.bus.publish(
                    ev.CompactionStarted(
                        session_id=self.bus.session_id,
                        turn=0,
                        strategy="manual",
                        tokens_before=planned.tokens_before,
                        message_count_before=planned.message_count_before,
                    )
                )

        summarizer = self.create_memo_summarizer()
        if hasattr(builder, "force_compact_async"):
            bundle = await builder.force_compact_async(
                history, last_usage=last_usage, summarizer=summarizer
            )
        else:
            bundle = builder.force_compact(history, last_usage=last_usage)

        report = getattr(bundle, "compaction", None)
        if report is None:
            return None

        await self.bus.publish(
            ev.CompactionFinished(
                session_id=self.bus.session_id,
                turn=0,
                tokens_after=report.tokens_after,
                message_count_after=report.message_count_after,
                degraded=report.degraded,
                tokens_before=report.tokens_before,
                pruned_count=report.pruned_count,
                folded_turns=report.folded_turns,
                strategy=report.strategy,
            )
        )
        metrics = getattr(self.reducer, "metrics", None)
        if metrics is not None:
            metrics.context_tokens = report.tokens_after
        return report

    def reload_resources(self) -> ResourceReloadReport:
        """重扫磁盘上的可热重载资源（`/reload`，见 MODULE_08 §5.2）。

        为什么放在装配根：这是**唯一同时认识**构建器 / 技能包 / 模板命令 / 配置的地方。
        界面只知道"我要重载"，不知道"要动几个对象、每个对象怎么重扫"（与 `apply_model` 同一形状）。

        **边界（不做的事和做的一样重要）**：

        * 不重载 Python 代码 —— 改 `.py` 仍要重启（`/status` 的「代码」行继续承担告警）；
        * 不重连 MCP、不重建插件注册表（`PluginManager` 没有卸载路径，重扫会重复注册）；
        * 不替换 `config` 的构造期字段（窗口 / 水位线 / reserve 已烤进 builder 与 compactor）；
        * 每项独立 try/except —— 技能包扫失败不该让记忆也不刷（与 `/login`、`/theme` 同一口径：
          降级成一行提示，会话继续）。
        """
        from logox.context.tokens import estimate_text_tokens

        items: list[ReloadItem] = []
        builder = self.context_builder

        before = ""
        if builder is not None and hasattr(builder, "system_prompt_snapshot"):
            with contextlib.suppress(Exception):
                before = builder.system_prompt_snapshot()

        # ---- ① 项目记忆（LOGOX.md / AGENTS.md，D119） ----
        if builder is None:
            items.append(ReloadItem("项目记忆", error="当前运行时没有接上下文构建器"))
        else:
            try:
                memory = builder.refresh_memory()
                if getattr(memory, "is_empty", True):
                    items.append(ReloadItem("项目记忆", detail="没有生效的记忆文件"))
                else:
                    names = "、".join(
                        Path(getattr(src, "path", "?")).name for src in memory.sources
                    )
                    items.append(
                        ReloadItem("项目记忆", detail=f"{names} · {memory.total_tokens:,} tokens")
                    )
            except Exception as exc:  # noqa: BLE001 - 一项失败不影响其他项
                logger.warning("重扫项目记忆失败：%s", exc)
                items.append(ReloadItem("项目记忆", error=f"{type(exc).__name__}: {exc}"))

        # ---- ② 技能包（M10 / D113） ----
        skills = self.skill_manager
        if skills is None:
            items.append(ReloadItem("技能包", detail="未启用"))
        else:
            try:
                skills.reload()
                items.append(ReloadItem("技能包", detail=f"{len(skills.list_skills())} 个"))
            except Exception as exc:  # noqa: BLE001
                logger.warning("重扫技能包失败：%s", exc)
                items.append(ReloadItem("技能包", error=f"{type(exc).__name__}: {exc}"))

        # ---- ③ 模板命令（M10 / D112） ----
        manager = self.command_manager
        if manager is None:
            items.append(ReloadItem("模板命令", detail="未启用"))
        else:
            try:
                manager.reload()
                items.append(ReloadItem("模板命令", detail=f"{len(manager.list_commands())} 条"))
            except Exception as exc:  # noqa: BLE001
                logger.warning("重扫模板命令失败：%s", exc)
                items.append(ReloadItem("模板命令", error=f"{type(exc).__name__}: {exc}"))

        # ---- ④ 配置：**只校验，不替换** ----
        items.append(self._check_config_on_reload())

        # ---- ⑤ 前缀代价（记忆与技能索引都在系统提示里） ----
        after = ""
        if builder is not None and hasattr(builder, "system_prompt_snapshot"):
            with contextlib.suppress(Exception):
                after = builder.system_prompt_snapshot()

        def _tokens(text: str) -> int:
            if not text:
                return 0
            with contextlib.suppress(Exception):
                return estimate_text_tokens(text)
            return 0

        return ResourceReloadReport(
            items=items,
            prefix_changed=bool(builder is not None and before != after),
            system_tokens_before=_tokens(before),
            system_tokens_after=_tokens(after),
            not_reloaded=[
                "Python 代码（改 .py 仍需重启）",
                "MCP 服务连接与插件注册",
                "config.toml 的上下文与压缩参数",
            ],
        )

    def _check_config_on_reload(self) -> ReloadItem:
        """重读 `config.toml` 并**只报告**（`/reload` 的第 ④ 项）。

        为什么不替换运行中的 `config`：窗口容量、水位线、reserve、压缩参数在装配时就已经
        传到 builder / compactor 里了（`ctx_config = getattr(bundle.config, "context")`）。
        只换 `self.config` 会造成"两份事实"——配置文件里写着 A、跑着的是 B，
        而这正是本项目反复出现的那类缺陷。所以这里的价值是**尽早暴露配置写错**，
        而不是"让配置立刻生效"。
        """
        try:
            from logox.config.loader import load as load_config
            from logox.providers.registry import BUILTIN_SPECS

            paths = self.paths if self.paths is not None else LogoxPaths.default()
            bundle = load_config(
                self.cwd,
                paths=paths,
                known_providers=tuple(BUILTIN_SPECS),
            )
        except Exception as exc:  # noqa: BLE001 - 校验本身失败也只报告
            return ReloadItem("配置", error=f"{type(exc).__name__}: {exc}")

        errors = list(getattr(bundle, "errors", []) or [])
        if errors:
            first = errors[0]
            where = Path(first.path).name
            if getattr(first, "line", None):
                where = f"{where}:{first.line}"
            field = f" [{first.field}]" if getattr(first, "field", None) else ""
            more = f"（共 {len(errors)} 处）" if len(errors) > 1 else ""
            return ReloadItem("配置", error=f"{where}{field} {first.message}{more}")

        warnings = list(getattr(bundle, "warnings", []) or [])
        detail = f"{len(warnings)} 条警告" if warnings else "无问题"
        return ReloadItem("配置", detail=f"{detail}（改动需重启生效）")

    def apply_model(self, model: str) -> int | None:
        """切换模型：内核 + 上下文计量（窗口/κ 桶）+ 状态栏**一起**更新（D159 / D193）。

        为什么要收成一个方法：`/model` 之前只调 `kernel.set_model()`，于是
        上下文构建器仍在用旧模型的窗口与 κ 桶 —— 换到窗口更小的模型后压缩永不触发，
        而请求会撞上厂商的 400。界面不该知道「窗口从哪查、要改几个对象」，
        这些是**装配知识**，属于装配根。
        """
        old_window = getattr(self.context_builder, "window_capacity", None) if self.context_builder else None
        self._previous_window = old_window

        setter = getattr(self.kernel, "set_model", None)
        if callable(setter):
            setter(model)
        self.model = model

        window = self.window_for(model)
        builder = self.context_builder
        if builder is not None and hasattr(builder, "set_model"):
            builder.set_model(model_key=f"{self.provider_name}/{model}", window_capacity=window)
        if window is not None:
            metrics = getattr(self.reducer, "metrics", None)
            if metrics is not None:
                metrics.context_window = window
        return window

    async def eager_compact_if_needed(self) -> Any | None:
        """换模型后若当前上下文超过新模型的高水位线，及早压缩一次（D188 / D193）。

        若未超标、处于扩容安全区或无需压缩返回 None；压缩成功返回 CompactionReport。
        """
        builder = self.context_builder
        if builder is None:
            return None

        kernel = self.kernel
        history = list(getattr(kernel, "history", []) or [])
        if not history:
            return None

        plan = getattr(builder, "plan", None)
        if not callable(plan):
            return None
        # Builder's fold cache is the actual effective view; previous-model usage is not.
        planned = plan(history, last_usage=None, force=False)
        if planned is None:
            return None

        # 越过高水位线，立即就地压缩！
        return await self.apply_compact()

    def list_mcp_servers(self) -> list[Any]:
        if self.mcp_manager is None:
            return []
        return self.mcp_manager.get_status_list()

    def list_skills(self) -> list[Any]:
        if self.skill_manager is None:
            return []
        return self.skill_manager.list_skills()

    def get_skill(self, name: str) -> Any | None:
        if self.skill_manager is None:
            return None
        return self.skill_manager.get_skill(name)

    def read_skill_content(self, name: str) -> str | None:
        if self.skill_manager is None:
            return None
        return self.skill_manager.read_skill_content(name)

    def list_custom_commands(self) -> list[Any]:
        if self.command_manager is None:
            return []
        return self.command_manager.list_commands()

    def render_custom_command(self, name: str, args: str = "") -> str | None:
        if self.command_manager is None:
            return None
        return self.command_manager.render(name, args)

    def list_hooks(self) -> list[Any]:
        if self.hook_runner is None:
            return []
        return self.hook_runner.history

    async def async_close(self) -> None:
        """异步关闭运行时持有的外部进程与连接。"""
        if self.anamnesis is not None:
            await self.anamnesis.aclose()
        if self.mcp_manager is not None:
            await self.mcp_manager.close_all()

    def close(self) -> None:
        """同步关闭运行时资源。"""
        if self.mcp_manager is not None or self.anamnesis is not None:
            import asyncio

            try:
                loop = asyncio.get_running_loop()
                if self.anamnesis is not None:
                    self.anamnesis.request_close()
                loop.create_task(self.async_close())
            except RuntimeError:
                asyncio.run(self.async_close())

    # ------------------------------------------------------------------ #
    # 供 `/login` 与 `/model` 使用的窄接口
    # ------------------------------------------------------------------ #

    def provider_details(self, name: str) -> dict[str, Any]:
        """一个 provider 的**展示用**信息：是否需要密钥、默认端点、候选模型。

        界面据此画出选择列表（"需要 DEEPSEEK_API_KEY"这种提示）。全部是纯数据，
        界面不需要知道 ``ProviderSpec`` 这个模型的存在。
        """
        empty = {"name": name, "api_key_env": "", "base_url": "", "models": [], "has_key": False}
        if self.registry is None:
            return empty
        # `spec()` 对未知名字**抛异常**（装配期就炸掉是它的设计）——
        # 但这里只是画一个展示列表，不该因为一个拼错的名字把界面搞崩。
        if name not in self.registry.names():
            return empty
        spec = self.registry.spec(name)
        has_key = False
        if not spec.api_key_env:
            has_key = True
        else:
            try:
                from logox.providers.registry import resolve_api_key

                resolve_api_key(spec, getattr(self.registry, "_environ", None))
                has_key = True
            except Exception:
                has_key = False

        return {
            "name": name,
            "api_key_env": spec.api_key_env,
            "base_url": spec.base_url,
            "models": list(spec.models),
            "has_key": has_key,
        }

    def available_providers(self) -> list[str]:
        if self.registry is None:
            return [self.provider_name]
        return list(self.registry.names())

    def build_provider(self, name: str, *, api_key: str | None = None) -> Any:
        """构造一个适配器实例（**不改变**当前运行时）。

        :raises LogoxError: 缺密钥、端点非法等。调用方应当**先在界面上验证再替换**
            ——构造失败时当前会话里的 provider 原封不动，用户不会因为一次输错密钥
            就丢掉正在用的连接。
        """
        if self.registry is None:  # pragma: no cover - 装配根总会注入
            raise RuntimeError("Provider 注册表不可用（装配根未注入 registry）")
        return self.registry.build(name, api_key=api_key)

    def default_model_for(self, name: str) -> str:
        if self.registry is None:
            return ""
        return self.registry.default_model(name) or ""

    def list_models(self, name: str, *, api_key: str | None = None) -> list[str]:
        """列举一个 provider 可用的模型 id（**纯本地、零网络**）。

        来源按优先级合并：**上次抓到的真实列表** → 本地预设表。
        抓取本身在 `/login` 成功那一步做（见 :meth:`refresh_models`），
        本地端点则在启动时后台抓一次、或用 `/model refresh` 现抓（D188）——
        这样 `/model` 弹窗点开就是瞬时的，不用等网络。

        ★ **免鉴权端点（本地 Ollama / LM Studio）有缓存时直接用缓存**，不与预设合并：
        本机 `/v1/models` 就是权威全集，而预设表注定过期；合并会把"根本没拉取的模型"
        留在候选里，用户选了就报错——比「少列一个」更糟。
        """
        preset = list(self.provider_details(name)["models"])
        cached = self._cached_models(name)
        if not cached:
            return preset
        if self._is_keyless(name):
            return list(cached)
        from logox.providers.discovery import merge_models

        return merge_models(cached, preset)

    def _cached_models(self, name: str) -> list[str]:
        if self.state_store is None:
            return []
        try:
            return self.state_store.cached_models(name)
        except Exception:  # pragma: no cover - 状态文件坏了不该影响选择器
            return []

    def _is_keyless(self, name: str) -> bool:
        """该 provider 是否本地免鉴权端点（判据见 `providers.registry.is_keyless`）。"""
        registry = self.registry
        if registry is None:
            return False
        try:
            if name not in registry.names():
                return False
            from logox.providers.registry import is_keyless

            return bool(is_keyless(registry.spec(name)))
        except Exception:  # pragma: no cover - 注册表异常时当作普通端点
            return False

    async def refresh_local_models(self, *, timeout_s: float | None = None) -> list[tuple[str, Any]]:
        """抓取**所有免鉴权端点**的模型清单，逐个进行、互不影响（D188）。

        两个调用方共用：① 启动时的**后台**任务；② `/model refresh`（显式、要看结果）。

        为什么**逐个**而不是并发：本地端点最多两三个，串行总耗时仍在毫秒级，
        而并发会引入「同时写 state.toml」的竞争面——不值得为省几毫秒承担那个风险。

        返回 ``[(provider_name, DiscoveryResult), ...]``（**成功与失败都回传**，
        显式路径据此如实报告，而不是假装刷新成功）。异常不外抛。
        """
        if self.registry is None:
            return []
        from logox.providers.discovery import LOCAL_TIMEOUT_S

        limit = LOCAL_TIMEOUT_S if timeout_s is None else timeout_s
        results: list[tuple[str, Any]] = []
        for name in self._keyless_provider_names():
            try:
                result = await self.refresh_models(name, timeout_s=limit)
            except Exception as exc:  # pragma: no cover - refresh_models 已吞异常，这里是双保险
                from logox.providers.discovery import DiscoveryResult

                result = DiscoveryResult(error=f"{type(exc).__name__}: {exc}", attempted=True)
            results.append((name, result))
        return results

    def _keyless_provider_names(self) -> list[str]:
        """免鉴权端点名单。

        优先用注册表自己的 ``keyless_names()``；没有这个方法时（测试里的**假注册表**、
        或将来的其它实现）就地从 ``names()`` + ``spec()`` 算出来 —— 启动路径上的
        一个 AttributeError 会让整个后台任务报错，不值得为了少写两行而冒这个风险。
        """
        registry = self.registry
        if registry is None:
            return []
        fast = getattr(registry, "keyless_names", None)
        if callable(fast):
            return list(fast())
        return [name for name in registry.names() if self._is_keyless(name)]

    async def refresh_models(
        self, name: str, *, api_key: str | None = None, timeout_s: float | None = None
    ) -> Any:
        """**向端点抓一次真实模型列表**，成功后写进 `state.toml` 缓存（D65）。

        这是「输入 API 之后自动抓取可用模型」的落点。返回
        :class:`~logox.providers.discovery.DiscoveryResult`——**成功与失败都走返回值**，
        不抛异常：抓不到只是「回退到预设表」，绝不能让登录失败。

        成功时的两个副作用：① 缓存进 `state.toml`（下次 `/model` 零网络就能看到）；
        ② 更新刚构造的那个适配器的本地列表（同一会话里立刻可用）。
        """
        from logox.providers.discovery import DEFAULT_TIMEOUT_S, DiscoveryResult, discover_models

        try:
            provider_instance = self.build_provider(name, api_key=api_key)
        except Exception as exc:
            return DiscoveryResult(error=f"{type(exc).__name__}: {exc}", attempted=True)

        result = await discover_models(
            provider_instance,
            timeout_s=DEFAULT_TIMEOUT_S if timeout_s is None else timeout_s,
        )
        if result.ok:
            self._remember_models(name, result.models)
            self._update_provider_models(provider_instance, result.models)
            # `/login` 之后内核里装的正是这个实例，因此这一步同时让 `/model` 立刻看到
        return result

    def _remember_models(self, name: str, models: list[str]) -> None:
        if self.state_store is None:
            return
        try:
            self.state_store.set_models(name, models)
        except Exception as exc:  # 写盘失败不影响"本次已经用上了"
            logger.warning("模型列表未能写入 state.toml：%s", exc)

    @staticmethod
    def _update_provider_models(provider_instance: Any, models: list[str]) -> None:
        setter = getattr(provider_instance, "set_models", None)
        if callable(setter):
            setter(models)

    # ------------------------------------------------------------------ #
    # 供 `/resume` 与 `/new` 使用的窄接口（D98 会话管理）
    # ------------------------------------------------------------------ #

    def list_sessions(self, cwd: Path | str | None = None) -> list[Any]:
        """列举指定工作区（默认当前工作区）的所有历史会话，按最近活跃时间倒序。"""
        from logox.paths import LogoxPaths
        from logox.store.manager import SessionManager

        target_cwd = cwd or self.cwd
        base_dir = (
            self.paths.sessions
            if self.paths and hasattr(self.paths, "sessions")
            else LogoxPaths.default().sessions
        )
        mgr = SessionManager(base_dir)
        return mgr.list_sessions(target_cwd)

    def switch_session(self, file_path: Path | str, timeline: Any | None = None) -> int:
        """热切换会话：加载目标会话、重构消息历史、重放进时间线并更新持久化写出器。"""
        from logox.store.replay import (
            filter_rewound_records,
            load_session_records,
            reconstruct_messages,
            replay_into_timeline,
        )

        target_path = Path(file_path).resolve()
        records = filter_rewound_records(load_session_records(target_path))

        count = 0
        if timeline is not None:
            # ★ D138：注入摘要函数 —— 回放时卡片上的参数摘要**不该是原始字典 dump**。
            #   为什么让装配根注入而不是 store 直接 import：分层红线（store 不依赖 tui）。
            from logox.tui.format import format_args, summarize_args

            count = replay_into_timeline(
                records,
                timeline,
                summarize=summarize_args,
                format_args=format_args,
            )

        if self.persistence_writer is not None and hasattr(self.persistence_writer, "switch_target"):
            self.persistence_writer.switch_target(target_path)

        if self.kernel is not None and hasattr(self.kernel, "history"):
            self.kernel.history.clear()
            messages = reconstruct_messages(records, session_dir=target_path.parent)
            self.kernel.history.extend(messages)
            self.kernel._last_request_usage = None
            if self.context_builder and hasattr(self.context_builder, "restore_state"):
                self.context_builder.restore_state(self.kernel.history, records)
            if self.reducer is not None:
                user_turns = sum(1 for m in messages if m.role == "user")
                self.reducer.metrics.turn = user_turns + 1
                if self.context_builder and hasattr(self.context_builder, "estimator"):
                    estimator = getattr(self.context_builder, "estimate_context", None)
                    self.reducer.metrics.context_tokens = (estimator(self.kernel.history) if callable(estimator)
                        else self.context_builder.estimator.estimate_messages(messages))

        self.resume_file = target_path
        return count

    def create_new_session(
        self, cwd: Path | str | None = None, initial_title: str = "新会话"
    ) -> Any:
        """开启全新会话：重置内核历史，分配新会话文件并绑定持久化目标。"""
        from logox.paths import LogoxPaths
        from logox.store.manager import SessionManager

        target_cwd = cwd or self.cwd
        base_dir = (
            self.paths.sessions
            if self.paths and hasattr(self.paths, "sessions")
            else LogoxPaths.default().sessions
        )
        mgr = SessionManager(base_dir)
        info = mgr.create_session(target_cwd, initial_title=initial_title)

        if self.kernel is not None and hasattr(self.kernel, "history"):
            self.kernel.history.clear()
            self.kernel._last_request_usage = None
        if self.context_builder and hasattr(self.context_builder, "reset"):
            self.context_builder.reset()

        if self.reducer is not None:
            self.reducer.metrics.turn = 1
            self.reducer.metrics.context_tokens = 0

        if self.persistence_writer is not None and hasattr(self.persistence_writer, "switch_target"):
            self.persistence_writer.switch_target(info.file_path)

        self.resume_file = info.file_path
        return info

    def is_current_session(self, session_path: Path | str) -> bool:
        """检查指定路径是否为当前正在激活的会话文件。"""
        cur = self._get_current_session_file()
        if not cur:
            return False
        return cur == Path(session_path).resolve()

    def delete_session(self, session_path: Path | str, *, soft: bool = True) -> Path:
        """删除指定会话文件（D104 会话回收站）。"""
        from logox.paths import LogoxPaths
        from logox.store.manager import SessionManager

        base_dir = (
            self.paths.sessions
            if self.paths and hasattr(self.paths, "sessions")
            else LogoxPaths.default().sessions
        )
        mgr = SessionManager(base_dir)
        return mgr.delete_session(session_path, soft=soft)

    # ------------------------------------------------------------------ #
    # 供 `/rewind` 与 `/undo` 使用的窄接口（D102 快照与回滚）
    # ------------------------------------------------------------------ #

    def _get_current_session_file(self) -> Path | None:
        if self.resume_file:
            return Path(self.resume_file).resolve()
        if self.persistence_writer and hasattr(self.persistence_writer, "log_file"):
            return Path(self.persistence_writer.log_file).resolve()
        return None

    @property
    def permission_mode(self) -> str:
        """当前项目的权限模式（D130：default / creative）。"""
        if self.permission_decider is not None and hasattr(self.permission_decider, "mode"):
            return str(self.permission_decider.mode)
        if self.state_store is not None and hasattr(self.state_store, "read"):
            try:
                return str(self.state_store.read().permissions.mode)
            except Exception:
                pass
        return "default"

    def set_permission_mode(self, mode: str) -> None:
        """切换权限模式并同步更新决策器、状态仓与度量模型。"""
        if self.permission_decider is not None and hasattr(self.permission_decider, "set_mode"):
            self.permission_decider.set_mode(mode)
        elif self.state_store is not None and hasattr(self.state_store, "set_permission_mode"):
            self.state_store.set_permission_mode(mode)
        if self.reducer is not None and hasattr(self.reducer, "metrics"):
            self.reducer.metrics.permission_mode = mode

    def estimate_cost(self, usage: Any, model: str | None = None) -> float | None:
        """估算用量费用（美元）。装配根统一提供，避免界面直接 import providers（T41）。"""
        from logox.providers.pricing import estimate_cost_usd

        return estimate_cost_usd(usage, model or self.model)

    def get_permission_snapshot(self) -> dict[str, Any]:
        """获取权限规则与物理沙箱快照（D132）。"""
        if self.permission_decider is not None and hasattr(self.permission_decider, "get_rules_snapshot"):
            return self.permission_decider.get_rules_snapshot()
        root = Path(self.cwd or ".").resolve()
        perms = (
            self.state_store.read().permissions
            if self.state_store is not None and hasattr(self.state_store, "read")
            else None
        )
        return {
            "workspace_root": root,
            "mode": getattr(perms, "mode", "default") if perms else "default",
            "project_rules": [],
            "session_rules": [],
            "sensitive_items": [".git/", ".env", ".logox/config.toml", ".logox/permissions.toml"],
        }

    def revoke_permission_rule(self, scope: str, kind: str, rule_str: str) -> bool:
        """撤销指定作用域与类型的权限规则（D132）。"""
        if self.permission_decider is not None and hasattr(self.permission_decider, "engine"):
            engine = self.permission_decider.engine
            from logox.permissions.models import Decision, RuleScope

            target_scope = RuleScope.SESSION if scope == "session" else RuleScope.PROJECT
            target_dec = Decision.ALLOW if kind == "allow" else Decision.DENY
            return engine.revoke_by_str(rule_str, scope=target_scope, decision=target_dec)
        if scope == "project" and self.state_store is not None and hasattr(self.state_store, "revoke_permission"):
            return self.state_store.revoke_permission(kind, rule_str)
        return False

    def list_checkpoints(self) -> list[Any]:
        """提取当前会话的所有代码修改轮次检查点，按轮次倒序排列（最近的排最前）。"""
        from logox.store.checkpoint import CheckpointTracker
        from logox.store.replay import load_session_records

        log_file = self._get_current_session_file()
        if not log_file or not log_file.is_file():
            return []
        records = load_session_records(log_file)
        checkpoints = CheckpointTracker.extract_turn_checkpoints(records)
        checkpoints.reverse()
        return checkpoints

    def check_rewind_conflicts(self, to_turn: int) -> list[Any]:
        """检查回滚到 to_turn 是否会与外部手写代码产生冲突。"""
        from logox.store.replay import load_session_records
        from logox.store.rewind import check_conflicts

        log_file = self._get_current_session_file()
        if not log_file or not log_file.is_file():
            return []
        records = load_session_records(log_file)
        return check_conflicts(records, to_turn, self.cwd)

    async def rewind(
        self,
        to_turn: int,
        *,
        force: bool = False,
        timeline: Any | None = None,
    ) -> Any:
        """执行时空穿梭回滚：还原磁盘文件、截断内核历史、截断时间线并记录事件。"""
        from logox.kernel import events as ev
        from logox.paths import LogoxPaths
        from logox.store.blob import BlobStore
        from logox.store.checkpoint import RewindResult
        from logox.store.replay import (
            filter_rewound_records,
            load_session_records,
            reconstruct_messages,
            replay_into_timeline,
        )
        from logox.store.rewind import execute_rewind

        log_file = self._get_current_session_file()
        if not log_file or not log_file.is_file():
            return RewindResult(
                success=False,
                to_turn=to_turn,
                message="当前会话文件不存在，无法执行回滚。",
            )

        records = load_session_records(log_file)
        store = self.blob_store or BlobStore(
            self.paths.blobs if self.paths and hasattr(self.paths, "blobs") else LogoxPaths.default().blobs
        )

        result = execute_rewind(records, to_turn, self.cwd, store, force=force)
        if not result.success:
            return result

        # 1. 发布事件通知总线与持久化订阅者
        conflicts_str = [c.path for c in result.conflicts]
        await self.bus.publish(
            ev.RewindPerformed(
                session_id=self.bus.session_id,
                turn=to_turn,
                to_turn=to_turn,
                restored=result.restored_files,
                deleted=result.deleted_files,
                conflicts=conflicts_str,
            )
        )

        # 2. 截断内核历史与校准状态栏
        simulated_records = filter_rewound_records(records + [{"type": "session_rewind", "to_turn": to_turn}])
        new_messages = reconstruct_messages(simulated_records, session_dir=log_file.parent)
        if self.kernel is not None and hasattr(self.kernel, "history"):
            self.kernel.history.clear()
            self.kernel.history.extend(new_messages)
            self.kernel._last_request_usage = None
        if self.context_builder and hasattr(self.context_builder, "restore_state"):
            self.context_builder.restore_state(new_messages, simulated_records)

        if self.reducer is not None:
            user_turns = sum(1 for m in new_messages if m.role == "user")
            self.reducer.metrics.turn = user_turns + 1
            if self.context_builder and hasattr(self.context_builder, "estimator"):
                estimator = getattr(self.context_builder, "estimate_context", None)
                self.reducer.metrics.context_tokens = (estimator(new_messages) if callable(estimator)
                    else self.context_builder.estimator.estimate_messages(new_messages))

        # 3. 截断时间线（若传入 timeline）
        if timeline is not None and hasattr(timeline, "buffer"):
            buffer = timeline.buffer
            if hasattr(buffer, "clear"):
                buffer.clear()
            elif hasattr(buffer, "blocks"):
                buffer.blocks.clear()
            from logox.tui.format import format_args, summarize_args

            replay_into_timeline(
                simulated_records,
                timeline,
                summarize=summarize_args,
                format_args=format_args,
            )

        return result



# --------------------------------------------------------------------------- #
# 装配
# --------------------------------------------------------------------------- #


def build_runtime(
    bundle: Any,
    cwd: Path,
    paths: LogoxPaths,
    *,
    session_id: str | None = None,
    allow_unsafe_tools: bool | None = None,
    resume_file: Path | str | None = None,
) -> Runtime | StartupError:
    """装配全部零件。**失败时返回 :class:`StartupError` 而不是抛异常。**

    为什么返回而不是抛：这四类失败都是**预期内**的（用户没配密钥、没填模型、配置指向
    不存在的主题……）。用异常表达会让调用方写一堆 ``try/except``，而用返回值表达
    则强迫调用方**必须处理**它——这正是我们想要的（缺密钥时绝不能静默进全屏再闪退）。
    """
    config: LogoxConfig = bundle.config
    provider_config: ProviderConfig = config.provider

    # ---- ① Provider ----
    from logox.errors import LogoxError
    from logox.providers.base import ThinkingConfig
    from logox.providers.pricing import estimate_cost_usd
    from logox.providers.registry import ProviderRegistry, ProviderSpec

    overrides = {
        name: ProviderSpec(
            name=name,
            **instance.model_dump(
                include={"kind", "base_url", "api_key_env", "models", "context_window", "model_windows"},
                exclude_unset=True,
            ),
        )
        for name, instance in config.providers.items()
    }
    registry = ProviderRegistry.with_builtins(overrides)

    # 体验模式（M4）：**只替换模型响应**，其余全是真的。见 `logox/devsetup.py`。
    # 它与生产走完全相同的 `prepare_runtime()`，因此连「四类启动检查」都被一起验证。
    from logox.devsetup import BANNER, ENV_FLAG, MODEL_NAME, scripted_provider

    dev_mode = os.environ.get(ENV_FLAG) == "1"
    # 体验模式下**不要**去解析配置里的 provider：用户可能根本没配过，而且
    # "我的配置指向 deepseek 但实际在跑体验模式"这件事，应当在状态栏上看得出来
    # ——所以 provider 名字也换成体验模式自己的，而不是照抄配置。
    provider_name = "logox-dev" if dev_mode else provider_config.name
    if dev_mode:
        sys.stderr.write(BANNER)
        provider: Any = scripted_provider()
    else:
        try:
            # ★ `allow_missing_key=True`：缺密钥时**不再拒绝启动**，而是给一个
            # "占位适配器「——它一被调用就返回可行动的错误（」运行 /login"）。
            #
            # 为什么必须这样：`/login` 正是用来配密钥的。若缺密钥就启动失败，
            # 用户**根本没有机会执行 `/login`**——先有鸡还是先有蛋（实测踩到）。
            provider = registry.build(provider_config.name, allow_missing_key=True)
        except LogoxError as exc:
            return _provider_error(exc, provider_config.name, paths)

    # ---- ② 模型 ----
    model = (
        MODEL_NAME
        if dev_mode
        else (provider_config.model or registry.default_model(provider_config.name) or "")
    )
    if not model:
        available = _known_models(registry, provider_config.name)
        return StartupError(
            kind="model",
            message=(
                f"Provider {provider_config.name!r} 没有可用的模型"
                + (f"（已知：{'、'.join(available)}）" if available else "")
            ),
            hint=(
                f"请在 {paths.config} 的 [provider] 段里设置 model = \"<模型名>\"；"
                "用 logox --check-config 可看到当前生效的配置来源"
            ),
            exit_code=EXIT_CONFIG_ERROR,
        )

    # ---- ②b 模型**存在性**提醒（D67 的后续）--------------------------------- #
    #
    # 为什么要有这一步：用户实机验收时，`state.toml` 里存着一个**早就退役的模型名**
    # （`deepseek-chat`），而当时没人发现——因为「名字格式合法」和"端点认这个名字"
    # 是两件事，而所有测试都只验证前者。症状是每次请求都失败，但设置看着一切正常。
    #
    # ⚠️ **只提醒，绝不擅自改**。第一版写的是「不在清单里就回退到默认模型」，
    # 那是错的：已知清单**不是**权威清单（实测 `/models` 端点并不返回全部可调用模型，
    # 用户真正在用的多模态模型就不在里面但能调通），自动回退会把用户**有意指定的**
    # 自建/中转模型名悄悄替换掉——正是这个项目一直在防的「静默改配置」。
    # 因此这里只把「可疑」这件事说出来，选择权留给用户。
    model_notice = ""
    if config.provider.model:
        known = _known_models(registry, provider_config.name)
        if known and config.provider.model not in known:
            model_notice = (
                f"当前模型 {config.provider.model!r} 不在已知清单里"
                f"（已知：{'、'.join(known)}）。它可能是**已退役的旧名**——"
                "那种情况下每次请求都会失败；也可能是端点还没列出来的新模型。"
                "用 /model 查看并切换，或先发一句话试试。"
            )
            logger.warning("模型名不在已知清单：%s（已知：%s）", config.provider.model, known)

    # ---- ③ 工具（含安全闸门） ----
    tools = build_tool_registry()

    unsafe_allowed = (
        bool(os.environ.get("LOGOX_ALLOW_UNSAFE_TOOLS")) if allow_unsafe_tools is None else allow_unsafe_tools
    )
    if unsafe_allowed:
        sys.stderr.write("⚠ LOGOX_ALLOW_UNSAFE_TOOLS=1：权限检查已关闭，仅限开发调试。\n")

    # ---- ④ 主题（失败**不退出**，回退默认并记一条警告） ----
    #
    # ★ D152-c：主题只从**内置目录 + 用户目录 `~/.logox/themes`** 解析。
    #   项目级 `.logox/themes/` 已被**刻意移除**（用户裁定："不要有什么项目级配置覆盖了"）——
    #   主题是「用户对界面的偏好」，不是「项目对代码的规范」。
    warnings: list[str] = []
    theme_name = config.ui.theme or DEFAULT_THEME
    try:
        theme = load_theme(theme_name, paths.themes)
    except Exception as exc:  # ThemeError 等
        # UI-SPEC §8.2：加载失败回退 logox-dark 并在界面上提示，**不阻断启动**（P-5）
        theme = load_theme(DEFAULT_THEME, paths.themes)
        warnings.append(f"主题 {theme_name!r} 加载失败（{exc}）；已回退 {DEFAULT_THEME}")
        logger.warning("主题 %r 加载失败，已回退 %s：%s", theme_name, DEFAULT_THEME, exc)

    # ★ D152-b：对比度**体检**（不阻断，但必须说出来）。
    #
    # 为什么要有这一步：`validate_contrast` 此前只被 `tools/gen_themes.py` 使用 ——
    # 也就是**只保护内置主题**。而自定义主题走的是同一条加载路径却完全不做体检，
    # 于是「用户自己配了一个看不见的输入框框线」会**静默生效**，
    # 然后再来报一次和这次一模一样的障。
    #
    # 为什么是**警告**而不是拒绝：自定义主题是用户的自由 ——
    # 他可能故意要一条很淡的框线。拒绝加载会把「我想要的风格」判成「错误」。
    # 所以口径与既有的 `model_notice` 一致：**说清楚，但不擅自改**。
    try:
        from logox.config.theme import validate_contrast

        for problem in validate_contrast(theme):
            warnings.append(f"主题 {theme.name!r} 对比度不达标：{problem}")
            logger.warning("主题 %s 对比度不达标：%s", theme.name, problem)
    except Exception as exc:  # pragma: no cover - 体检本身不该拖垮启动
        logger.debug("主题对比度体检跳过：%s", exc)

    # 模型名可疑（②b）的提示要让它出现在界面上，而不是只进日志——
    # "设置界面看着正常、请求一直失败"正是这一步想消灭的症状。
    if model_notice:
        warnings.append(model_notice)

    # ---- ⑤ 状态写回（只写 state.toml，D30 / D119 / D120） ----
    #
    # ⚠️ 这一步**在构造内核之前**：权限决策器要用它读「持久允许」的规则，
    # 而内核要拿决策器。顺序反了就只能先造内核、再想办法把存储塞进去
    # ——那是「得记得补第二次调用」的老问题（见 `prepare_runtime` 的说明）。
    # ★ D120：采用分层状态存储（LayeredStateStore）——
    # 偏好与模型（provider/model/theme）双写全局，保证换项目直接继承；
    # 权限规则（learn_permission）严格单写项目级 state.toml，保证工程间物理隔离。
    state_store = None
    try:
        from logox.config.state import LayeredStateStore, StateStore
        from logox.paths import nearest_project

        proj = nearest_project(cwd)
        project_state_path = proj.state if proj is not None else paths.state
        project_store = StateStore(project_state_path)
        global_store = StateStore(paths.state)
        state_store = LayeredStateStore(project_store, global_store)
    except Exception as exc:  # pragma: no cover - 构造失败极少见
        warnings.append(f"状态文件不可用（{exc}）；本次会话的选择不会被记住")
        logger.warning("无法构造 StateStore(%s)：%s", paths.state, exc)

    # ---- ⑥ 总线 + 内核 + 归约器 ----
    session_path = Path(resume_file).resolve() if resume_file else None
    effective_session_id = session_id or (session_path.stem if session_path else None)
    context_window = getattr(provider, "context_window", None) or 200_000
    bus = EventBus(session_id=effective_session_id or f"tui-{os.getpid()}")
    reducer = MetricsReducer(context_window=context_window)

    # ---- ⑥a 会话分桶持久化订阅 (D98) ----
    try:
        from logox.context.storage import SessionTranscriptWriter
        from logox.kernel import events as ev
        from logox.store.manager import SessionManager
        from logox.store.persistence import SessionPersistenceSubscriber

        session_mgr = SessionManager(paths.sessions if hasattr(paths, "sessions") else None)
        project_dir = session_mgr.get_project_dir(cwd)
        project_dir.mkdir(parents=True, exist_ok=True)
        (project_dir / "tools").mkdir(parents=True, exist_ok=True)
        out_file = session_path if session_path else (project_dir / f"{bus.session_id}.jsonl")
        persistence_writer = SessionTranscriptWriter(log_file=out_file)
        persistence = SessionPersistenceSubscriber(persistence_writer)
        bus.subscribe(ev.Event, persistence.handle, name="session-persistence")
    except Exception as exc:  # pragma: no cover - 持久化挂钩失败不阻塞启动
        logger.warning("未能启动会话持久化订阅器：%s", exc)

    # ---- ⑥b 权限决策器（界面在构造时把自己注册进来） ----
    #
    # 没有界面注册时它返回 ``ask``，调度器会按拒绝处理**并说明原因**——
    # 与 M3 的行为完全一致（那时根本没有权限界面）。
    permission_decider = UiPermissionDecider(cwd=cwd)
    permission_decider.state_store = state_store
    if state_store is not None:
        try:
            # 载入「持久允许」与权限运行模式 (D130)
            perms = state_store.read().permissions
            permission_decider.seed_persisted(perms.allow, mode=perms.mode)
            reducer.metrics.permission_mode = perms.mode
        except Exception as exc:  # pragma: no cover - 状态文件损坏
            logger.warning("读取持久权限规则失败：%s", exc)

    # ---- ⑥f 技能包管理 (M10 / D113) ----
    skill_manager = None
    try:
        from logox.skills import SkillManager

        user_dir = paths.user_config_dir if paths and hasattr(paths, "user_config_dir") else None
        skill_manager = SkillManager(cwd=cwd, user_dir=user_dir)
    except Exception as exc:  # pragma: no cover
        logger.warning("技能包管理器初始化失败：%s", exc)

    # ★ D157：`[context]` 配置**真的传进去**。
    #   此前这 6 个字段一个都没接线 —— 用户在 config.toml 里怎么写都不生效
    #   （`compact_threshold` / `reasoning_in_context` / `max_tool_result_chars` 三个
    #   与实现不符的字段已在同一次改动里删除）。
    #   注意：所有默认值都与代码默认一致 ⇒ **接线本身不改变默认行为**。
    ctx_config = getattr(getattr(bundle, "config", None), "context", None)
    context_builder = HierarchicalContextBuilder(
        system=_system_prompt(cwd, tools),
        cwd=cwd,
        session_id=bus.session_id,
        window_capacity=context_window,
        transcript_writer=persistence_writer,
        skill_manager=skill_manager,
        reserve_tokens=getattr(ctx_config, "reserve_tokens", 32_768),
        low_watermark_ratio=getattr(ctx_config, "low_watermark_ratio", 0.50),
        # 配置里 0 = 不封顶（哨兵值）⇒ 转成 None 交给 Compactor
        max_budget_tokens=getattr(ctx_config, "max_budget_tokens", 0) or None,
        keep_recent_turns=getattr(ctx_config, "keep_recent_turns", 2),
        keep_recent_tool_results=getattr(ctx_config, "keep_recent_tool_results", 2),
        rehydrate_files=getattr(ctx_config, "rehydrate_files", 5),
        rehydrate_max_chars=getattr(ctx_config, "rehydrate_max_chars", 2000),
        project_memory_enabled=getattr(ctx_config, "project_memory_enabled", True),
        # ★ D158：κ 按 (provider, model) 分桶 —— 不同分词器的偏差不能互相污染
        model_key=f"{provider_name}/{model}",
        anamnesis_home=paths.root,
        anamnesis_enabled=config.anamnesis.memory_enabled,
        anamnesis_ratio=config.anamnesis.prompt_memory_max_ratio,
    )

    # ---- ⑥d 快照对象池 (D102) ----
    blob_store = None
    try:
        from logox.store.blob import BlobStore

        blob_store = BlobStore(paths.blobs if hasattr(paths, "blobs") else None)
    except Exception as exc:  # pragma: no cover
        logger.warning("未能初始化快照对象池：%s", exc)

    # ---- ⑥e MCP 外部工具生态扩展（M9 / D106 / D107） ----
    mcp_manager = None
    if getattr(config, "mcp", None) and config.mcp.enabled:
        try:
            from logox.mcp.manager import McpManager

            mcp_manager = McpManager(config.mcp, cwd=cwd, bus=bus)
            if mcp_manager.clients:
                tools.register(mcp_manager.meta_tool)
        except Exception as exc:  # pragma: no cover
            logger.warning("MCP 管理器初始化失败：%s", exc)
            warnings.append(f"MCP 生态未启用（{exc}）")

    # ---- ⑥g 模板命令管理 (M10 / D112) ----
    command_manager = None
    try:
        from logox.commands import CommandManager

        user_dir = paths.user_config_dir if paths and hasattr(paths, "user_config_dir") else None
        command_manager = CommandManager(cwd=cwd, user_dir=user_dir)
    except Exception as exc:  # pragma: no cover
        logger.warning("模板命令管理器初始化失败：%s", exc)

    # ---- ⑥h 插件生态管理 (M10 / D111) ----
    plugin_manager = None
    if getattr(config, "plugins", None) and config.plugins.enabled:
        try:
            from logox.plugins import PluginContext, PluginManager

            user_dir = paths.user_config_dir if paths and hasattr(paths, "user_config_dir") else None
            plugin_manager = PluginManager(config.plugins, cwd=cwd, user_dir=user_dir)
            plugin_ctx = PluginContext(tool_registry=tools, bus=bus)
            plugin_manager.load_all(plugin_ctx)
        except Exception as exc:  # pragma: no cover
            logger.warning("插件管理器初始化失败：%s", exc)

    # ---- ⑥i 观察型钩子调度器 (M10 / D110) ----
    hook_runner = None
    if getattr(config, "hooks", None) and config.hooks.enabled:
        try:
            from logox.hooks import HookRunner

            user_dir = paths.user_config_dir if paths and hasattr(paths, "user_config_dir") else None
            hook_runner = HookRunner(config.hooks, cwd=cwd, user_dir=user_dir)
            hook_runner.attach_to_bus(bus)
        except Exception as exc:  # pragma: no cover
            logger.warning("钩子调度器初始化失败：%s", exc)

    kernel = KernelLoop(
        bus,
        provider,
        tools,
        context_builder,
        model=model,
        temperature=provider_config.temperature,
        max_tokens=provider_config.max_tokens,
        thinking=ThinkingConfig(effort=provider_config.thinking_effort),
        max_iterations=config.kernel.max_iterations,
        max_retries=config.kernel.max_retries,
        retry_base_s=config.kernel.retry_base_s,
        retry_max_s=config.kernel.retry_max_s,
        concurrency=config.kernel.concurrency,
        cwd=cwd,
        # 费用估算由装配根注入（内核不认识 L5 的价格表，见 MODULE_kernel_loop §10 偏差 A）
        cost_estimator=lambda usage, model_name: estimate_cost_usd(usage, model_name),
        # ★ 权限决策的注入点（D10）：内核只认 allow/deny/ask，
        # "ask 之后怎么问用户"是界面的事，而把两者接起来是装配根的职责（B4）。
        decider=permission_decider,
        blob_store=blob_store,
    )


    # ---- ⑥c 历史会话恢复与回放注入 (D98) ----
    session_replayer = None
    if session_path and session_path.exists():
        try:
            from logox.store.replay import replay_session

            replay_session(session_path, kernel_loop=kernel, timeline=None, context_builder=context_builder)

            def session_replayer(timeline: Any) -> None:
                from logox.tui.format import format_args, summarize_args

                replay_session(
                    session_path,
                    kernel_loop=None,
                    timeline=timeline,
                    summarize=summarize_args,
                    format_args=format_args,
                )

            if kernel.history:
                user_turns = sum(1 for m in kernel.history if m.role == "user")
                reducer.metrics.turn = user_turns + 1
                if context_builder and hasattr(context_builder, "estimator"):
                    reducer.metrics.context_tokens = context_builder.estimate_context(kernel.history)
        except Exception as exc:  # pragma: no cover
            logger.warning("回放历史会话失败：%s", exc)

    # 还没登录（缺密钥）时，**必须让用户知道下一步**：不然他会对着
    # "模型怎么不说话"发呆。这条提示会一路留到 /login 成功为止。
    missing_env = getattr(provider, "missing_env", "")
    if getattr(provider, "is_placeholder", False):
        warnings.append(
            f"尚未登录 {provider_name}"
            + (f"（缺环境变量 {missing_env}）" if missing_env else "")
            + "：运行 /login 选择供应商并粘贴 API Key；在那之前发送消息会收到一条提示"
        )

    runtime = Runtime(
        bus=bus,
        kernel=kernel,
        reducer=reducer,
        theme=theme,
        config=config,
        provider_name=provider_name,
        model=model,
        cwd=cwd,
        tools=tools.names(),
        state_store=state_store,
        warnings=warnings,
        registry=registry,
        needs_login=bool(getattr(provider, "is_placeholder", False)),
        # 权限决策器：界面在构造时把自己注册成 `prompter`（见 `InlineApp.__init__`）
        permission_decider=permission_decider,
        context_builder=context_builder,
        resume_file=session_path,
        session_replayer=session_replayer,
        persistence_writer=persistence_writer,
        paths=paths,
        blob_store=blob_store,
        mcp_manager=mcp_manager,
        hook_runner=hook_runner,
        plugin_manager=plugin_manager,
        command_manager=command_manager,
        skill_manager=skill_manager,
        # 密钥文件的位置在这里定下来（`/login` 要写它）。
        # ⚠️ 注意：**加载**它是调用方的事（见 `load_env_file_quietly`）——
        # 装配根只回答「用哪个文件」，不产生 `os.environ` 副作用，
        # 否则测试里一个仓库级的 `.logox/.env` 就会悄悄改变所有用例的行为。
        env_file=resolve_env_file(cwd, paths),
    )
    if hasattr(kernel, "memo_summarizer"):
        kernel.memo_summarizer = runtime.create_memo_summarizer()
    from logox.anamnesis.local import make_local_runner
    from logox.anamnesis.service import AnamesisService

    async def anamnesis_runner(collector):
        return await make_local_runner(registry, config.anamnesis, collector, permission_decider.engine)

    runtime.anamnesis = AnamesisService(config=config.anamnesis, home=paths.root,
                                       cwd=cwd, sessions=paths.sessions, runner_factory=anamnesis_runner)
    def anamnesis_session():
        path = runtime._get_current_session_file()
        return path.stem if path is not None else ""

    def link_anamnesis(run_id, owner):
        writer = runtime.persistence_writer
        if (writer is not None and owner == anamnesis_session()
                and writer.write_step(turn=0, step=0, role="", event_type="anamnesis_ref", run_id=run_id,
                                      submission_ts=runtime.anamnesis._last_submission) is None):
            raise OSError("无法保存入梦会话引用")

    runtime.anamnesis.current_session = anamnesis_session
    runtime.anamnesis.on_started = link_anamnesis
    return runtime


class UiPermissionDecider:
    """把内核的 ``ask`` 变成一次**界面提问**（UI-SPEC §5.8 / D10）。

    为什么要这一层（而不是让内核直接问界面）
    --------------------------------------

    内核只认 ``allow`` / ``deny`` / ``ask`` 三个答案——它**不该知道**
    "ask 之后是谁在问、怎么问、能不能记住「。而」把两条腿接起来"正是装配根
    存在的理由（B4：``app.py`` 是唯一同时认识 L2–L5 的文件）。

    于是这里的职责很窄：**翻译**。

    =========================== ==============================================
    内核说 ``ask``                这里问界面 → 把四个选项翻成 allow/deny
    ``PermissionAsk`` / ``Choice`` 纯数据，界面与内核都只依赖它们（`tui/permission.py`）
    =========================== ==============================================

    三条安全默认（每一条都是「宁可不做，也不能默认放行」）
    -------------------------------------------------

    1. **没有界面在听 → 返回 ``ask``，让调度器按拒绝处理并说明原因**。
       不返回 ``deny`` 是因为那样会**一个事件都不发**，用户只会看到"工具失败"
       而不知道为什么（调度器那条路径本来就会解释「当前没有权限确认界面」）。
    2. **提问过程中出任何异常 → 拒绝**（``except`` 里兜住，绝不冒泡）。
    3. **界面返回意料之外的东西 → 拒绝**（例如 ``None``）。

    记忆的粒度（诚实说明）
    --------------------

    "本会话总是允许「与」持久允许"记住的是**工具名**。而 UI-SPEC 里那个选项的
    本意是「记住这条**规则**」——规则引擎（M5）还没有落地，所以现在能做的只有
    工具级记忆，选项标签上也**明写了工具名**（"本会话总是允许 shell"），
    让用户清楚自己允许的范围有多大。
    """

    def __init__(self, *, cwd: Any = None, engine: Any = None, state_store: Any = None) -> None:
        self.cwd = str(cwd or "")
        self._state_store = state_store
        from logox.permissions.engine import PermissionEngine

        self.engine: PermissionEngine = engine or PermissionEngine(
            self.cwd, state_store=self._state_store
        )
        #: 由界面在构造时注册（`InlineApp.__init__`）。没有它 → 回落到"问不了"
        self.prompter: Any = None
        #: 本会话内允许的工具名（"本会话总是允许"）
        self._session_allow: set[str] = set()
        #: 从 `state.toml` 读到的持久允许（"持久允许"写入的就是这里）
        self._project_allow: set[str] = set()
        #: 诊断用：上一次问了什么、答了什么（`/debug` 与测试看它）
        self.history: list[tuple[str, str]] = []
        #: 上一次用户拒绝时的具体原因或补充要求
        self.last_rejection_reason: str = ""

    # ------------------------------------------------------------------ #
    # 记忆
    # ------------------------------------------------------------------ #

    def seed_persisted(self, rules: Any, mode: Any = None) -> None:
        """载入 ``state.toml`` 里的持久允许规则与权限运行模式。

        **必须在启动时调用**：不载入的话，"持久允许"在重启后就失效了——
        那是一个**会撒谎的按钮**（用户以为记住了，下次又问一遍）。
        """
        raw_list = list(rules or [])
        self._project_allow = {str(rule) for rule in raw_list}
        self.engine.load_persisted(
            allow_rules=[str(r) for r in raw_list],
            mode=str(mode) if mode else None,
        )

    @property
    def mode(self) -> str:
        return self.engine.mode.value

    def set_mode(self, mode: str) -> None:
        self.engine.set_mode(mode)

    @property
    def allowed_tools(self) -> set[str]:
        return self._session_allow | self._project_allow

    def get_rules_snapshot(self) -> dict[str, Any]:
        """获取权限与沙箱快照（D132）。"""
        return self.engine.get_rules_snapshot()

    def revoke_rule(self, rule: Any) -> bool:
        """撤销一条规则并同步旧的 _session_allow / _project_allow 缓存。"""
        res = self.engine.revoke_rule(rule)
        rule_str = getattr(rule, "tool_name", "")
        if rule_str in self._session_allow:
            self._session_allow.discard(rule_str)
        if rule_str in self._project_allow:
            self._project_allow.discard(rule_str)
        return res

    def _remember_key(self, call: Any, tool: Any) -> str:
        """记住「什么」：目前是**工具名**（规则引擎落地后换成 rule_id）。"""
        return str(getattr(tool, "spec", None) and tool.spec.name or call.name)

    # ------------------------------------------------------------------ #
    # 决策
    # ------------------------------------------------------------------ #

    async def decide(self, call: Any, tool: Any, turn: Any) -> Any:
        """``PermissionDecider`` 的实现。**返回值只有 allow / deny / ask 三种。**"""
        from logox.kernel.scheduler import Decision

        key = self._remember_key(call, tool)
        args = getattr(call, "arguments", None) or getattr(call, "args", None) or {}

        # 运行权限引擎五层安全检测（完全由规则引擎按细粒度白名单裁决，严禁裸工具名短路穿透！）
        if self.state_store is not None and self.engine.state_store is None:
            self.engine.state_store = self.state_store
        evaluation = self.engine.evaluate(key, args, readonly=bool(tool.spec.readonly))

        if evaluation.decision == Decision.ALLOW:
            return Decision.ALLOW
        if evaluation.decision == Decision.DENY:
            return Decision.DENY

        # 3. 需人工确认 (HITL)
        prompter = self.prompter
        if prompter is None:
            # 没有界面在听 → 交给调度器按拒绝处理（它会**说明原因**，见类文档）
            return Decision.ASK

        ask = self._build_ask(call, tool, key, evaluation=evaluation)
        try:
            choice = await prompter.ask_permission(ask)
        except Exception as exc:  # 提问失败**绝不能**变成放行
            logger.warning("权限提问失败，按拒绝处理：%s", exc)
            choice = PermissionChoice.DENY
        if not isinstance(choice, PermissionChoice):
            choice = PermissionChoice.DENY

        feedback = getattr(prompter, "last_feedback", "").strip() if hasattr(prompter, "last_feedback") else ""
        if not choice.allowed:
            if feedback:
                self.last_rejection_reason = f"{call.name} 被用户拒绝。用户补充要求：{feedback}"
            else:
                self.last_rejection_reason = f"{call.name} 被策略拒绝，未执行"
        else:
            self.last_rejection_reason = ""

        self._record(ask, choice, key, turn, evaluation=evaluation)
        return Decision.ALLOW if choice.allowed else Decision.DENY

    def _build_ask(
        self,
        call: Any,
        tool: Any,
        key: str,
        evaluation: Any = None,
    ) -> PermissionAsk:
        """把一次工具调用翻译成界面要显示的东西。"""
        from logox.permissions.decider import format_permission_detail
        from logox.permissions.models import RiskLevel

        spec = getattr(tool, "spec", None)
        args = getattr(call, "arguments", None) or getattr(call, "args", None) or {}

        # 风险评级与说明
        if evaluation is not None and evaluation.risk_level != RiskLevel.NORMAL:
            risk = "high"
            risk_note = evaluation.reason
        else:
            risk = "normal" if getattr(spec, "readonly", False) else "high"
            risk_note = "这个工具不是只读的（可能修改文件或执行命令）"

        # 规则说明：如果已有具体推荐规则
        rule = ""
        rule_scope = ""
        if evaluation is not None and evaluation.suggested_rule is not None:
            rule = evaluation.suggested_rule.pattern
            rule_scope = "建议规则模式"
        elif evaluation is not None and evaluation.matched_rule is not None:
            rule = evaluation.matched_rule.pattern
            rule_scope = str(evaluation.matched_rule.scope.value)

        # 高危越界或敏感文件严禁持久化
        allow_project = True
        if evaluation is not None and evaluation.risk_level != RiskLevel.NORMAL:
            allow_project = False

        return PermissionAsk(
            tool=key,
            detail=format_permission_detail(key, args, cwd=self.cwd),
            rule=rule,
            rule_scope=rule_scope,
            cwd=self.cwd,
            risk=risk,
            risk_note=risk_note,
            allow_session=True,
            allow_project=allow_project,
            call_id=str(getattr(call, "call_id", "")),
        )

    def _record(
        self,
        ask: PermissionAsk,
        choice: PermissionChoice,
        key: str,
        turn: Any,
        evaluation: Any = None,
    ) -> None:
        from logox.permissions.models import Decision, PermissionRule, RuleScope

        choice_val = choice.value if hasattr(choice, "value") else str(choice)
        self.history.append((ask.tool, choice_val))
        pat = evaluation.suggested_rule.pattern if (evaluation and evaluation.suggested_rule) else "*"

        if choice in (PermissionChoice.SESSION, "session"):
            rule_obj = PermissionRule(key, pattern=pat, decision=Decision.ALLOW, scope=RuleScope.SESSION)
            self.engine.learn_rule(rule_obj)
            self._session_allow.add(rule_obj.to_str())
        elif choice in (PermissionChoice.PROJECT, "project"):
            rule_obj = PermissionRule(key, pattern=pat, decision=Decision.ALLOW, scope=RuleScope.PROJECT)
            self.engine.learn_rule(rule_obj)
            self._project_allow.add(rule_obj.to_str())
            if self.engine.state_store is None and self.state_store is not None:
                try:
                    self.state_store.learn_permission("allow", rule_obj.to_str())
                except Exception as exc:  # 写盘失败不影响"本次已经用上了"
                    logger.warning("权限规则未能写入 state.toml：%s", exc)
        del turn

    @property
    def state_store(self) -> Any:
        """由装配根注入（'持久允许'要写它）。"""
        return getattr(self, "_state_store", None)

    @state_store.setter
    def state_store(self, store: Any) -> None:
        self._state_store = store
        if hasattr(self, "engine") and self.engine is not None:
            self.engine.state_store = store


def _format_args(args: Any) -> str:
    """向后兼容辅助函数。"""
    from logox.permissions.decider import _format_args as _decider_format
    return _decider_format(args)


def build_tool_registry() -> ToolRegistry:
    """装配**内置工具**注册表（不含安全闸门）。

    为什么单独抽出来：`logox --check-config` 要显示「**实际注册了哪些工具**」，
    而它跑在装配之前。不抽出来的话，摘要只能显示配置里的 `tools.enabled` ——
    那是一份**愿望清单**，不是事实。

    ⚠️ 实测踩到过这个谎：摘要里写着"启用工具：read, write, edit, glob, grep,
    shell, todo"，而当时**只有 `read` 真的注册了**。用户会据此以为 `shell` 能用，
    然后对着「模型为什么不用 shell」发呆——**产品主动误导用户**，
    比少显示几行严重得多。
    """
    from logox.tools.fs_edit import build as build_edit_tool
    from logox.tools.fs_glob import build as build_glob_tool
    from logox.tools.fs_grep import build as build_grep_tool
    from logox.tools.fs_read import build as build_read_tool
    from logox.tools.fs_write import build as build_write_tool
    from logox.tools.shell import build as build_shell_tool

    tools = ToolRegistry()
    tools.register(build_read_tool())
    tools.register(build_write_tool())
    tools.register(build_edit_tool())
    tools.register(build_glob_tool())
    tools.register(build_grep_tool())
    tools.register(build_shell_tool())
    return tools


def registered_tool_names() -> list[str]:
    """当前**真的**注册了哪些工具（供启动摘要与 `/status` 用）。"""
    try:
        return build_tool_registry().names()
    except Exception:  # pragma: no cover - 仅用于显示
        return []


def _known_models(registry: Any, name: str) -> list[str]:
    try:
        return [info.id for info in registry.build(name).list_models()]
    except Exception:  # pragma: no cover - 仅用于提示文案
        return []


def _provider_error(exc: Exception, name: str, paths: LogoxPaths) -> StartupError:
    """把 Provider 构造失败分类，并在**缺密钥**时补上「还有什么别的选择」。

    **实测发现（M4）**：`providers/registry.py` 自己已经把缺密钥这件事说得很清楚了——

        Provider 'deepseek' 需要环境变量 DEEPSEEK_API_KEY，但它未设置或为空。
        请在启动 Logox 前导出它，或在 config.toml 里把 [providers.deepseek]
        的 api_key_env 改成实际使用的变量名。

    那是 M3 §10 缺陷 4 的直接产物（本地端点因为「缺密钥」启动失败、报错指向性极差）。
    因此装配根**不再重复解释一遍**，只做两件事：
    ① 分类（决定退出码与测试口径）；② 补一句「本地端点不需要密钥」——
    后者是用户此刻最可能想知道的下一步，而适配层无从得知。
    """
    text = str(exc)
    lowered = text.lower()
    looks_like_auth = any(
        word in lowered
        for word in ("api_key", "api key", "apikey", "unauthorized", "401", "未设置或为空")
    )
    if looks_like_auth:
        return StartupError(
            kind="auth",
            message=text,
            hint=(
                "也可以改用**不需要密钥**的本地端点：在配置里设 "
                'provider.name = "ollama"（默认 http://127.0.0.1:11434/v1）或 "lm-studio"；'
                f"配置文件：{paths.config}"
            ),
            exit_code=EXIT_CONFIG_ERROR,
        )
    return StartupError(
        kind="provider", message=text, hint=f"配置文件：{paths.config}", exit_code=EXIT_CONFIG_ERROR
    )


def _system_prompt(cwd: Path, tools: ToolRegistry) -> str:
    """系统人设提示词。"""
    tool_list = ", ".join(tools.names()) if hasattr(tools, "names") else "（无）"
    return (
        "你是 Logox，一个在用户终端里工作的高效专业编码助手。\n"
        f"当前工作目录：{cwd}。\n"
        f"你可以使用这些工具：{tool_list or '（无）'}。\n"
        "【核心行为与工具使用规范】：\n"
        "1. 需要查看文件或目录时请直接调用 read/glob/grep 工具，严禁凭空猜测。\n"
        "2. 修改已有代码文件时，【必须优先使用 edit 工具】进行精确局部替换（函数/代码块级），【严禁使用 write 工具全量覆盖重写】已有中大型文件！全量重写会因代码过长触发单次输出 Token 上限导致截断报错。\n"
        "3. write 工具仅用于新建文件或小于 50 行的极小文件。\n"
        "4. 单次工具调用应保持紧凑，避免一次性生成数百上千行代码。"
    )


# --------------------------------------------------------------------------- #
# 启动界面
# --------------------------------------------------------------------------- #


def render_startup_error(error: StartupError) -> str:
    """把启动失败渲染成**给人看**的几行文本（无堆栈、有下一步）。"""
    lines = [f"✗ 无法启动 Logox：{error.message}"]
    if error.hint:
        lines.append(f"  怎么办：{error.hint}")
    lines.append("  其他可用：logox --chat（文本模式）· logox --check-config · logox --print-config")
    return "\n".join(lines)


def resolve_env_file(cwd: Path, paths: LogoxPaths) -> Path:
    """决定 ``.env`` 密钥文件的目标路径（D120 全局共享凭据）。

    规则：
    1. 若当前工作区或其祖先**已经显式存在私有 ``.env`` 文件**，返回该文件进行项目级覆盖；
    2. 默认情况下，返回用户全局密钥文件 ``~/.logox/.env``（``paths.env``）。
       用户通过 ``/login`` 记住的密钥将默认持久化至全局，实现一次登录、所有项目终身免输。
    """
    from logox.paths import discover_project_chain

    chain = discover_project_chain(cwd)  # 远 → 近
    nearest_first = list(reversed(chain))
    for project in nearest_first:
        if project.env.is_file():
            return project.env
    return paths.env


def load_env_file_quietly(path: Path) -> list[str]:
    """把 ``.env`` 并入 ``os.environ``（**不覆盖已有的真实环境变量**）。

    :returns: 实际生效的变量名（供启动摘要与 `/debug` 显示「从文件里读到了几个」）。
        返回值里**只有变量名，绝不含值**——它可能被打印出去。
    """
    from logox.config.envfile import load_env_file

    try:
        applied = load_env_file(path)
    except Exception as exc:  # pragma: no cover - 读文件失败不该影响启动
        logger.debug("读取密钥文件 %s 失败：%s", path, exc)
        return []
    return sorted(applied)


def prepare_runtime(
    bundle: Any,
    cwd: Path,
    paths: LogoxPaths | None = None,
    *,
    resume_file: Path | str | None = None,
) -> Runtime | StartupError:
    """**装配根对外的统一入口**：解析密钥文件 → 加载它 → 装配。

    为什么要合成一个函数（M4 实测踩到）：`logox --inline` 最初直接调
    ``build_runtime``，**漏掉了加载 ``.env`` 这一步**，于是用户在 `/login` 里
    存好的密钥完全不生效——启动后显示「尚未登录」，每条消息都失败，
    而设置界面看着一切正常。

    根因是「装配」被拆成了两个必须成对出现的调用（``load_env_file_quietly``
    + ``build_runtime``），而**只做对一次是不够的**：任何新增的启动入口都会
    再踩一遍。所以现在只有一个入口，顺序由它保证。
    """
    from logox.paths import ensure_project_initialized

    # ★ D119：目标工作区轻量自动初始化（确立独立工程沙箱，防配置向上漂移）
    _, newly_created = ensure_project_initialized(cwd)

    resolved_paths = paths or LogoxPaths.default()
    # ★ D152-c：创建 Logox 自己的目录（含 `~/.logox/themes`）。
    #
    # 为什么在这里调用：`LogoxPaths.ensure_dirs()` 的 docstring 写的就是这个职责，
    # 但它**此前从未被任何地方调用**（死代码）—— 于是 `~/.logox/themes` 根本不存在，
    # 而用户要放自定义主题就必需它存在。方向是安全的：`mkdir(exist_ok=True)` 幂等，
    # 失败也不该阻断启动（用户可能只是想跑一下）。
    with contextlib.suppress(OSError):
        resolved_paths.ensure_dirs()

    env_file = resolve_env_file(cwd, resolved_paths)

    # ★ D120：凭据分层加载机制（全局共享为主，项目私有覆盖为辅）
    # 1. 始终先行加载用户全局密钥文件（~/.logox/.env），实现全机多项目免密共享
    loaded_sources: list[str] = []
    global_keys = load_env_file_quietly(resolved_paths.env)
    if global_keys:
        loaded_sources.append(f"{resolved_paths.env} ({', '.join(global_keys)})")

    # 2. 若检测到当前工作区存在私有 .env 且不是全局文件，额外加载进行局部变量覆盖
    if env_file != resolved_paths.env and env_file.is_file():
        project_keys = load_env_file_quietly(env_file)
        if project_keys:
            loaded_sources.append(f"{env_file} ({', '.join(project_keys)})")

    outcome = build_runtime(bundle, cwd, resolved_paths, resume_file=resume_file)
    if isinstance(outcome, StartupError):
        return outcome

    runtime = outcome
    runtime.env_file = env_file
    if newly_created:
        runtime.warnings.append("[+] 已初始化当前项目工作区配置 (.logox/)")
    if loaded_sources:
        runtime.warnings.append(f"已加载密钥凭据：{' · '.join(loaded_sources)}")
    # "还没登录"的提示已经在 build_runtime 里加进 warnings 了（那里才知道 provider 详情），
    # 这里不再重复探测内核的私有字段——`Runtime.needs_login` 就是为这件事存在的。
    return runtime


def build_session_start(runtime: Runtime, *, terminal_kind: str = "inline") -> Any:
    """构造 ``SessionStart``（环境事实的公示，UI-SPEC §5.12）。

    为什么放在装配根而不是界面里：这条事件的字段来自**配置与装配结果**
    （provider / model / 工作目录 / 工具清单），界面拿不到全部；
    而且它是「事件总线内核」的第一条证据——**内核自己不发它**。

    ``terminal_kind`` 默认 ``"inline"``：界面只有一种（主屏行式渲染，不切备用屏）。
    这一项留在 ``terminal_caps`` 里是为了将来能按终端能力分档。
    """
    from logox.kernel import events as ev

    shell = "unknown"
    try:
        from logox.tools.shell import detect_shell

        shell = detect_shell().name
    except Exception:
        pass

    memory_sources: list[str] = []
    if getattr(runtime, "context_builder", None) is not None:
        with contextlib.suppress(Exception):
            memory_sources = [str(s.path) for s in runtime.context_builder.memory.sources]

    win = getattr(runtime.context_builder, "window_capacity", None)
    if not win and getattr(runtime, "reducer", None):
        win = runtime.reducer.metrics.context_window

    return ev.SessionStart(
        session_id=runtime.bus.session_id,
        cwd=str(runtime.cwd),
        provider=runtime.provider_name,
        model=runtime.model,
        thinking_effort=runtime.config.provider.thinking_effort,
        shell_backend=shell,
        memory_sources=memory_sources,
        context_window=win or 200_000,
        terminal_caps={"style": terminal_kind, "tools": ",".join(runtime.tools)},
        resumed=bool(getattr(runtime, "resume_file", None)),
    )
