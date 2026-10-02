"""斜杠命令的**实现**（新界面，D80 §9 第 6 步）。

为什么单独一个文件
==================

`render/app.py` 的职责是「把内核接到渲染器上」（订阅事件、驱动帧、管输入）。
而 ``/login`` 这类命令要做的是**一串带浮层的交互**：选供应商 → 输密钥 →
抓模型 → 确认保存 → 切模型。把它塞进 `app.py` 会让那个文件同时负责
"渲染循环「与」业务对话"，两边都不好读、也不好测。

于是分成两边，中间只隔一个窄接口（:class:`CommandHost`）：

============================ ==================================================
``render/app.py``            提供能力：弹浮层、写一行提示、换主题、退出
``render/commands.py``       决定流程：什么时候问什么、失败怎么办
============================ ==================================================

**这样拆的直接好处**：命令流程可以用一个假 host + 假 runtime 完整测出来，
不需要终端、不需要事件循环跑起来、更不需要 Textual。

两个贯穿全部命令的原则
====================

1. **先验证再替换**（``/login``）：新 provider 构造失败时，当前会话里的连接
   原封不动。用户不会因为一次输错密钥就丢掉正在用的东西。
2. **失败必须看得见，且不能中断会话**（P-5）：抓模型失败、写 ``state.toml``
   失败、密钥落盘失败——全部降级成一行提示，会话继续。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from rich.cells import cell_len
from rich.text import Text

from logox.tui.commands import (
    PLANNED_COMMANDS,
    ResolvedCommand,
    format_planned_notice,
    format_unknown_notice,
)
from logox.tui.content.help import render_help
from logox.tui.content.overlay import Choice, PickerState
from logox.tui.format import clip, format_cost, format_duration, format_ratio, format_tokens
from logox.tui.render.components.overlay import (
    ConfirmComponent,
    PanelComponent,
    PickerComponent,
    PromptComponent,
)

__all__ = ["EFFORT_LEVELS", "CommandHost", "CommandRunner", "_render_summary_content"]

logger = logging.getLogger("logox.tui.render.commands")

#: 思考档位。**必须与** ``ProviderConfig.thinking_effort`` 的 Literal 一致
#: （测试里有一条断言盯着这件事，见 `tests/tui/test_render_commands.py`）。
#:
#: 为什么不直接引用 schema 里那个 Literal：界面只该知道「档位」这个名字，
#: 不该去反射厂商侧的数据模型；而且内核接受的是**字符串**，由它自己转配置。
EFFORT_LEVELS: tuple[str, ...] = ("off", "low", "medium", "high", "auto")


class CommandHost(Protocol):
    """`commands.py` 需要 `app.py` 提供的能力（**只有这些**）。

    刻意保持很小：每多一个成员，命令流程就多一处与界面耦合的地方，
    测试里的假 host 也就多一件要实现的事。
    """

    runtime: Any
    theme: Any
    effort: str
    #: 可用内容宽度（浮层与帮助按它排版）。为什么由界面给：命令不该去问终端。
    content_width: int
    #: 可用内容高度（面板据此决定滚动窗口）
    content_rows: int

    @property
    def busy(self) -> bool:
        """是否正在生成回复。

        `/reload` 据此**拒绝执行**（Q3-A 裁定）：重扫记忆/技能会换掉系统提示，
        而正在流式生成的那一次请求参数在发起时就固化进 `ChatRequest` 了 ——
        中途重载得到的是"半旧的这一轮 + 全新的下一轮"，两边都不是用户想要的状态。
        """
        ...

    async def push_overlay(self, component: Any, *, max_rows: int | None = None) -> Any:
        """显示一个浮层并等它交出结果（Esc 取消 → 组件自己决定返回什么）。"""
        ...

    def notice(self, message: str, *, token: str = "text_muted") -> None:
        """在时间线上写一行提示（**不是**浮层里的通知）。"""
        ...

    def refresh_status(self) -> None:
        """让状态行重画（模型 / 档位变了之后要立刻看到）。"""
        ...

    def clear_timeline(self) -> None:
        """清空当前对话显示。**不动**终端回滚缓冲里的历史。"""
        ...

    def apply_theme(self, name: str) -> str:
        """换主题，返回**实际生效的名字**；失败时抛出异常由调用方解释。"""
        ...

    def apply_effort(self, effort: str) -> None:
        """把档位设进内核并更新显示。"""
        ...

    def recent_events(self) -> list[str]:
        """最近的事件（``/debug`` 用）。"""
        ...

    def switch_session(self, file_path: Any) -> int:
        """切换会话并重放历史。"""
        ...

    def new_session(self) -> Any:
        """开启全新会话。"""
        ...

    async def rewind(self, to_turn: int, *, force: bool = False) -> Any:
        """回滚代码并截断历史。"""
        ...

    def stop(self) -> None:
        """退出。"""
        ...

    def delete_session(self, file_path: Any, *, soft: bool = True) -> Any:
        """删除会话。"""
        ...

    def is_current_session(self, file_path: Any) -> bool:
        """是否是当前激活的会话。"""
        ...



class CommandRunner:
    """把一条 `ResolvedCommand` 执行成真实动作。"""

    def __init__(self, host: CommandHost) -> None:
        self.host = host

    # ------------------------------------------------------------------ #
    # 入口
    # ------------------------------------------------------------------ #

    async def run(self, command: ResolvedCommand) -> None:
        """执行一条命令。**任何异常都不该冒到调用方**——命令出错不能让会话挂掉。"""
        handler = getattr(self, f"_cmd_{command.name}", None)
        if handler is not None:
            try:
                await handler(command.argument)
            except Exception as exc:  # 命令流程出问题时只提示，不崩
                logger.exception("命令 /%s 失败", command.name)
                self.host.notice(
                    f"/{command.name} 执行失败：{type(exc).__name__}: {exc}", token="danger"
                )
            return

        if command.name in PLANNED_COMMANDS:
            self.host.notice(
                format_planned_notice(command) + "（见 docs/ARCHITECTURE.md §11 的路线图）",
                token="warning",
            )
            return

        # 检查是否为 L1 提示词模板命令 (M10 / D112)
        if hasattr(self._runtime, "render_custom_command"):
            rendered = self._runtime.render_custom_command(command.name, command.argument)
            if rendered is not None:
                self.host.notice(f"—— 展开模板命令 /{command.name} ——", token="text_faint")
                submit_func = getattr(self.host, "_on_submit", None)
                if callable(submit_func):
                    submit_func(rendered)
                return

        self.host.notice(format_unknown_notice(command), token="warning")

    # ------------------------------------------------------------------ #
    # 与界面无关的小工具
    # ------------------------------------------------------------------ #

    @property
    def _runtime(self) -> Any:
        return self.host.runtime

    def _palette(self) -> Any:
        return self.host.theme.palette

    async def _persist(self, action: Any, label: str) -> None:
        """写回 ``state.toml``（D30）。**失败只提示，不中断**。

        放进线程池是因为文件写入是阻塞的——直接 ``await`` 同步版本会卡住事件循环，
        而事件循环同时还在跑流式输出（症状是「打字突然一顿」）。
        """
        store = getattr(self._runtime, "state_store", None)
        if store is None:
            return
        try:
            await asyncio.to_thread(action)
        except Exception as exc:
            logger.warning("%s 未能写入 state.toml：%s", label, exc)
            self.host.notice(f"{label} 未能记住（{exc}）；本次会话仍然可用", token="warning")

    async def _pick(
        self,
        state: PickerState,
        *,
        on_delete: Callable[[Choice], bool] | None = None,
    ) -> Choice | None:
        """弹一个选择器，等结果。"""
        component = PickerComponent(state, self._palette(), on_delete=on_delete)
        return await self.host.push_overlay(component)

    def _state(
        self, title: str, choices: list[Choice], *, current: str = ""
    ) -> PickerState:
        """构造选择器状态，并把**光标停在当前值上**。

        为什么这很重要：默认光标在第一项，而列表是按字母序的——于是
        `/login` 一打开停在 `anthropic`，用户顺手一个回车就**换掉了自己的供应商**。
        "当前值在光标下"是这类选择器最基本的一条安全感（少按几次方向键只是附带好处）。
        """
        state = PickerState(title=title, choices=choices, footer=self._close_footer())
        if current:
            for index, choice in enumerate(state.choices):
                if choice.value == current:
                    state.index = index
                    break
        return state

    def _close_footer(self) -> str:
        return "↑↓ 选择 · 数字直达 · 输入可筛选 · Enter 确认 · Esc 取消"

    # ------------------------------------------------------------------ #
    # /login（D62 / D65）
    # ------------------------------------------------------------------ #

    async def _cmd_login(self, argument: str) -> None:
        """选供应商 → 输密钥 → 抓模型 → 确认是否记住 → 热替换内核的 provider。

        三步都要能**优雅退出**（Esc = 取消，当前会话原封不动）。这类多步交互最容易
        写出的缺陷是「取消到一半留下半成品状态」，因此本方法只在**最后一步之后**
        才碰内核与 `state.toml`。
        """
        runtime = self._runtime
        if getattr(runtime, "registry", None) is None:
            self.host.notice("/login 需要 Provider 注册表（当前装配没有提供）", token="warning")
            return

        names = list(runtime.available_providers())
        if not names:
            self.host.notice("没有任何可用的 Provider", token="warning")
            return

        # 支持 /login [provider] [--reset]（D190：凭据复用与显式重置）
        tokens = argument.strip().split()
        is_reset = any(tok.lower() in ("--reset", "-r", "reset") for tok in tokens)
        explicit_provider = next(
            (tok for tok in tokens if not tok.startswith("-") and tok.lower() != "reset"), None
        )

        provider_name = explicit_provider if (explicit_provider and explicit_provider in names) else None

        if provider_name is None:
            choices: list[Choice] = []
            for name in names:
                details = runtime.provider_details(name)
                env_name = details.get("api_key_env") or ""
                has_key = bool(details.get("has_key"))
                if not env_name:
                    hint = "无需密钥"
                elif has_key:
                    hint = f"已配置 ({env_name})"
                else:
                    hint = f"未配置 · 需要 {env_name}"
                if name == runtime.provider_name:
                    hint = f"当前 · {hint}"
                choices.append(Choice(value=name, label=name, hint=hint))

            picked = await self._pick(
                self._state("选择供应商", choices, current=runtime.provider_name)
            )
            if picked is None:
                self.host.notice("已取消登录（当前会话不变）", token="text_faint")
                return
            provider_name = picked.value

        details = runtime.provider_details(provider_name)
        env_name = details.get("api_key_env") or ""
        has_key = bool(details.get("has_key"))

        api_key: str | None = None
        if env_name and (not has_key or is_reset):
            prompt_title = (
                f"输入 API Key · {provider_name}"
                if not has_key
                else f"重设 API Key · {provider_name}"
            )
            entered = await self.host.push_overlay(
                PromptComponent(
                    title=prompt_title,
                    label=f"环境变量名：{env_name}（粘贴后按 Enter）",
                    palette=self._palette(),
                )
            )
            if not entered:
                self.host.notice("已取消登录（当前会话不变）", token="text_faint")
                return
            api_key = entered
        elif env_name and has_key:
            self.host.notice(f"复用已保存的密钥凭据（{env_name}）", token="text_faint")

        # **先验证再替换**：构造失败时当前 provider 原封不动。
        try:
            provider = runtime.build_provider(provider_name, api_key=api_key)
        except Exception as exc:
            self.host.notice(f"无法使用 {provider_name}：{exc}", token="danger")
            return

        # ---- ★ 自动抓取真实可用模型（D65） ----
        # 密钥刚到手或复用，查询端点「你有哪些模型」。抓取失败**不影响登录**：
        # 回退到本地预设表，并把原因说出来。
        if hasattr(runtime, "refresh_models"):
            self.host.notice(f"正在向 {provider_name} 查询可用模型…", token="text_faint")
            try:
                result = await runtime.refresh_models(provider_name, api_key=api_key)
            except Exception as exc:  # 连抓取本身崩了也不该挡住登录
                logger.warning("抓取 %s 的模型列表失败：%s", provider_name, exc)
                result = None
            if result is not None:
                self.host.notice(
                    f"模型列表：{result.summary()}",
                    token="text_faint" if result.ok else "warning",
                )
                setter = getattr(provider, "set_models", None)
                if result.ok and callable(setter):
                    setter(result.models)

        if api_key and env_name:
            await self._remember_key(env_name, api_key)

        # 到这里才开始真正改变运行时
        model = runtime.default_model_for(provider_name) or runtime.model
        self._swap_provider(provider, provider_name, model)

        applier = getattr(runtime, "apply_model", None)
        if callable(applier):
            prober = getattr(runtime, "probe_model_window", None)
            if callable(prober):
                with contextlib.suppress(Exception):
                    await prober(model)
            applier(model)
            eager_compactor = getattr(runtime, "eager_compact_if_needed", None)
            if callable(eager_compactor):
                try:
                    compact_report = await eager_compactor()
                    if compact_report is not None:
                        desc = "已及早压缩"
                        token = "text_faint"
                        if compact_report.strategy == "prune+fold+memo":
                            desc = "已深度提炼为全局备忘录"
                            token = "accent"
                        elif compact_report.strategy == "prune+fold+truncated":
                            desc = "已执行安全机械截断"
                            token = "warning"
                        self.host.notice(
                            f"—— 切换供应商后历史会话 ({compact_report.tokens_before:,} tokens) {desc}至 {compact_report.tokens_after:,} tokens ——",
                            token=token,
                        )
                except Exception as exc:
                    logger.warning("及早压缩执行失败：%s", exc)

        await self._persist(
            lambda: runtime.state_store.set_last_model(provider=provider_name, model=model),
            f"供应商 {provider_name}",
        )

    async def _remember_key(self, env_name: str, api_key: str) -> None:
        """问「要不要把密钥写进文件」，然后照办。

        **默认焦点在"否"**（见 `ConfirmComponent`）：误按一次 Enter 不应该做出
        "把密钥写进磁盘"这种决定。
        """
        env_file = getattr(self._runtime, "env_file", None)
        if env_file is None:
            os.environ[env_name] = api_key  # 没有可写的文件 → 只放进程环境，不落盘
            self.host.notice("密钥仅本次有效（没有可写的密钥文件）", token="text_faint")
            return

        paths = getattr(self._runtime, "paths", None)
        is_global = bool(paths and hasattr(paths, "env") and env_file == paths.env)
        scope_note = "全局生效，所有项目免输入" if is_global else "当前项目专用，已 gitignore"
        remember = await self.host.push_overlay(
            ConfirmComponent(
                title="记住这个密钥？",
                question=f"要把 {env_name} 写入密钥文件吗？",
                detail=f"{env_file}（{scope_note}）",
                palette=self._palette(),
                yes="记住（下次启动免输入）",
                no="仅本次（退出即失效）",
            )
        )
        if remember is True:
            try:
                from logox.config.envfile import update_env_file

                await asyncio.to_thread(update_env_file, env_file, {env_name: api_key})
                os.environ[env_name] = api_key  # 本次也立刻生效
                self.host.notice(f"已记住密钥 → {env_file}", token="text_faint")
            except Exception as exc:
                # 写盘失败绝不能中断会话（P-5）：密钥已在内存里生效，只是没被记住
                self.host.notice(
                    f"密钥未能写入文件（{exc}）；本次会话仍然可用", token="warning"
                )
        elif remember is None:
            os.environ[env_name] = api_key
            self.host.notice("已取消写入（密钥仅本次有效）", token="text_faint")
        else:
            os.environ[env_name] = api_key  # 仅本次：只放进进程环境，**不落盘**
            self.host.notice("密钥仅本次有效（未写入文件）", token="text_faint")

    def _swap_provider(self, provider: object, provider_name: str, model: str) -> None:
        """把新 provider 装进内核并同步界面显示（**只影响下一次请求**）。"""
        kernel = getattr(self._runtime, "kernel", None)
        swapper = getattr(kernel, "set_provider", None)  # 可选能力，探测而非强制
        if callable(swapper):
            swapper(provider)
        self._runtime.provider_name = provider_name
        self._runtime.model = model
        self._runtime.needs_login = False
        self.host.notice(
            f"—— 已登录 {provider_name} · 模型 {model}（下一次请求生效）——", token="accent"
        )
        self.host.refresh_status()

    # ------------------------------------------------------------------ #
    # /model
    # ------------------------------------------------------------------ #

    async def _cmd_model(self, argument: str) -> None:
        """不带参数 → 弹窗选；带参数 → 直接切；``refresh`` → 重抓本地清单。

        直接切是留给「我知道要哪个」的场景（也是脚本化与测试的入口），
        弹窗是留给「我不记得有哪些」的场景。**同一个切换实现**，两条入口。

        ``/model refresh``（D188）专给本地免鉴权端点：刚 `ollama pull` 了新模型时，
        重启太重、盲打 `/model <名字>` 又要求你先记住名字 —— 打它刷新一次。
        """
        wanted = argument.strip()
        if wanted.lower() == "refresh":
            await self._refresh_models()
            return
        if wanted:
            await self._switch_model(wanted)
            return

        runtime = self._runtime
        if getattr(runtime, "registry", None) is None:
            self.host.notice("/model 需要 Provider 注册表（当前装配没有提供）", token="warning")
            return

        provider_name = runtime.provider_name
        models = list(runtime.list_models(provider_name))
        if not models:
            self.host.notice(
                f"{provider_name} 没有已知模型；可以显式指定：/model <模型名>", token="warning"
            )
            return
        current = runtime.model
        choices = [
            Choice(value=name, label=name, hint="当前" if name == current else "")
            for name in models
        ]
        picked = await self._pick(
            self._state(f"选择模型 · {provider_name}", choices, current=current)
        )
        if picked is None:
            self.host.notice("已取消（模型未变）", token="text_faint")
            return
        await self._switch_model(picked.value)

    async def _refresh_models(self) -> None:
        """重抓**本地**端点（Ollama / LM Studio）的模型清单，并**如实**报告结果。

        这里刻意不静默降级：用户主动打了一条命令，就该知道到底成没成
        （D47 第三条：宁可少列，也不假装成功）。启动时那一次才是静默的。
        """
        runtime = self._runtime
        refresher = getattr(runtime, "refresh_local_models", None)
        if not callable(refresher):
            self.host.notice(
                "当前装配不支持刷新本地模型；可直接用 /model <模型名>",
                token="warning",
            )
            return
        results = await refresher()
        if not results:
            self.host.notice(
                "没有免鉴权端点（本地 Ollama / LM Studio）。云端模型在 /login 成功后自动抓取。",
                token="text_faint",
            )
            return
        for name, result in results:
            if getattr(result, "ok", False):
                count = len(getattr(result, "models", []) or [])
                if count == 0:
                    self.host.notice(
                        f"—— {name} 刷新完成，但端点返回 0 个模型（沿用上次结果）——",
                        token="warning",
                    )
                else:
                    self.host.notice(
                        f"—— 已刷新 {name}：{count} 个模型（再打 /model 就能看到）——",
                        token="accent",
                    )
            else:
                self.host.notice(
                    f"—— {name} 刷新失败：{getattr(result, 'error', '未知原因')}（沿用上次结果）——",
                    token="warning",
                )

    async def _switch_model(self, model: str) -> None:
        """切换模型：改内核 → 更新状态行 → 写 `state.toml`。"""
        runtime = self._runtime
        # ★ D159：优先走装配根的 `apply_model` —— 它会把**内核 + 上下文计量（窗口/κ 桶）
        #   + 状态栏窗口**一起更新。此前只调 `kernel.set_model()`，于是换到窗口更小的
        #   模型后水位线仍按旧窗口算，压缩永不触发（请求会撞厂商 400）。
        applier = getattr(runtime, "apply_model", None)
        window: int | None = None
        try:
            if callable(applier):
                window = applier(model)
            else:  # 兼容：测试里的假 runtime 只提供 kernel
                setter = getattr(getattr(runtime, "kernel", None), "set_model", None)
                if callable(setter):
                    setter(model)
                runtime.model = model
        except Exception as exc:
            self.host.notice(f"无法切换到 {model}：{exc}", token="danger")
            return

        # ★ D188：及早压缩（Eager Compaction）——换到小窗口模型后若当前历史超标，立即就地压缩
        compact_report = None
        eager_compactor = getattr(runtime, "eager_compact_if_needed", None)
        if callable(eager_compactor):
            try:
                compact_report = await eager_compactor()
            except Exception as exc:
                logger.warning("及早压缩执行失败（不影响模型切换）：%s", exc)

        if compact_report is not None:
            size = f"，窗口 {window // 1000}k" if window else ""
            desc = "已自动压缩"
            token = "text_faint"
            if compact_report.strategy == "prune+fold+memo":
                desc = "已深度提炼为全局备忘录"
                token = "accent"
            elif compact_report.strategy == "prune+fold+truncated":
                desc = "已执行安全机械截断"
                token = "warning"
            self.host.notice(
                f"—— 模型已切换为 {model}{size}；历史会话 ({compact_report.tokens_before:,} tokens) {desc}至 {compact_report.tokens_after:,} tokens ——",
                token=token,
            )
        elif window is None and callable(applier):
            # 自定义模型名（不在预设表里）⇒ 查不到上下文窗口：如实告知，而不是默默沿用旧窗口
            self.host.notice(
                f"—— 模型已切换为 {model}；未查到该模型的上下文窗口，压缩阈值沿用当前值 ——",
                token="warning",
            )
        else:
            size = f"，窗口 {window // 1000}k" if window else ""
            self.host.notice(f"—— 模型已切换为 {model}{size}（下一次请求生效）——", token="text_faint")
        self.host.refresh_status()
        await self._persist(
            lambda: runtime.state_store.set_last_model(model=model), f"模型 {model}"
        )

    # ------------------------------------------------------------------ #
    # /theme
    # ------------------------------------------------------------------ #

    async def _cmd_theme(self, argument: str) -> None:
        """换主题（D17 / D43）。加载失败**不崩、不退出**，保持当前主题并说明原因。"""
        from logox.tui import theme as theme_module

        name = argument.strip()
        # ★ D152-c：列表必须带上**用户主题目录**，否则会出现
        #   "文件已放好、`/theme` 里却看不到它"（而 `load_theme` 其实能加载它）——
        #   列表与加载两条路各持一份事实，正是本轮要消灭的那类不一致。
        available = theme_module.list_themes(getattr(self.host, "themes_dir", None))
        if not name:
            choices = [
                Choice(
                    value=item,
                    label=item,
                    hint="当前" if item == self.host.theme.name else "",
                )
                for item in sorted(available)
            ]
            picked = await self._pick(
                self._state("选择主题", choices, current=self.host.theme.name)
            )
            if picked is None:
                self.host.notice("已取消（主题未变）", token="text_faint")
                return
            name = picked.value

        if name not in available:
            self.host.notice(
                f"主题 {name!r} 不存在；可用：{'、'.join(sorted(available))}", token="warning"
            )
            return
        try:
            applied = self.host.apply_theme(name)
        except Exception as exc:  # ThemeError 等：主题文件损坏
            self.host.notice(f"主题 {name!r} 加载失败：{exc}", token="danger")
            return
        self.host.notice(f"—— 主题已切换为 {applied} ——", token="text_faint")
        await self._persist(
            lambda: self._runtime.state_store.set_theme(name), f"主题 {name}"
        )

    # ------------------------------------------------------------------ #
    # /reload
    # ------------------------------------------------------------------ #

    async def _cmd_reload(self, argument: str) -> None:
        """重扫磁盘上的资源：记忆 / 技能 / 模板命令 / 主题，并校验配置。

        **不重载 Python 代码**（改 `.py` 仍要重启）——边界见 MODULE_08 §5.2。
        正在生成回复时拒绝执行：中途重载会得到"半旧的这一轮 + 全新的下一轮"，
        而正在流式生成的那次请求参数在发起时就固化进 `ChatRequest` 了。
        """
        del argument  # 没有"只重载某一项"这回事：重扫是廉价的，选择性重载只会多一份开关
        runtime = self._runtime
        reloader = getattr(runtime, "reload_resources", None)
        if not callable(reloader):
            self.host.notice("当前环境不支持资源重载", token="warning")
            return
        if getattr(self.host, "busy", False):
            self.host.notice("—— 正在回答，等这一轮结束后再 /reload ——", token="warning")
            return

        try:
            # 走线程池：记忆扫描与配置校验都是同步文件 IO，
            # 直接 await 会把事件循环卡住（症状是"打字一顿"，与 `_persist` 同一个理由）。
            report = await asyncio.to_thread(reloader)
        except Exception as exc:  # 重载本身炸了也必须看得见，且不能中断会话
            logger.exception("/reload 失败")
            self.host.notice(f"资源重载失败：{type(exc).__name__}: {exc}", token="danger")
            return

        for item in report.items:
            if item.error:
                self.host.notice(f"· {item.name}：{item.error}", token="danger")
            else:
                self.host.notice(f"· {item.name}：{item.detail}", token="text_faint")

        # 主题要在界面侧重读才算生效：`load_theme` 每次调用都读文件，
        # 所以"同名文件改了内容"这条路径只有这里能走通（`/theme` 切走再切回也可以）。
        try:
            applied = self.host.apply_theme(self.host.theme.name)
            self.host.notice(f"· 主题：{applied}（文件已重读，视口已重绘）", token="text_faint")
        except Exception as exc:
            self.host.notice(
                f"· 主题：{self.host.theme.name} 重读失败（保持当前主题）：{exc}", token="danger"
            )

        if report.prefix_changed:
            self.host.notice(
                f"—— 已重载；系统提示已变（{report.system_tokens_before:,} → "
                f"{report.system_tokens_after:,} tokens），下一次请求的缓存前缀按全价重算一次 ——",
                token="warning",
            )
        else:
            self.host.notice("—— 已重载；系统提示未变（缓存前缀不受影响）——", token="text_faint")

        for note in report.not_reloaded:
            self.host.notice(f"（本项仍需重启：{note}）", token="text_muted")
        self.host.refresh_status()

    # ------------------------------------------------------------------ #
    # /effort
    # ------------------------------------------------------------------ #

    async def _cmd_effort(self, argument: str) -> None:
        """切换思考档位（D42 / **D58：设置即生效**）。

        "生效"的精确含义：**下一次将要发起的模型请求**用新档位。正在流式生成的那一次
        不受影响——请求参数在发起时就固化进 `ChatRequest` 了；要「追溯」只能取消当前
        请求再重发，那会把用户正在读的回答拦腰截断。
        """
        effort = argument.strip().lower()
        if not effort:
            choices = [
                Choice(
                    value=item,
                    label=item,
                    hint="当前" if item == self.host.effort else "",
                )
                for item in EFFORT_LEVELS
            ]
            picked = await self._pick(
                self._state("选择思考档位", choices, current=self.host.effort)
            )
            if picked is None:
                self.host.notice("已取消（档位未变）", token="text_faint")
                return
            effort = picked.value

        if effort not in EFFORT_LEVELS:
            # 非法档位：**明确报错、不静默忽略、不写盘**（E-29）
            self.host.notice(
                f"未知档位 {effort!r}；可用：{'、'.join(EFFORT_LEVELS)}", token="warning"
            )
            return
        self.host.apply_effort(effort)
        self.host.notice(f"—— 思考档位：{effort}（下次请求生效）——", token="text_faint")
        await self._persist(
            lambda: self._runtime.state_store.set_effort(effort), f"思考档位 {effort}"
        )

    # ------------------------------------------------------------------ #
    # /mode（D130 权限运行模式切换：default / creative）
    # ------------------------------------------------------------------ #

    async def _cmd_mode(self, argument: str) -> None:
        """切换权限模式（/mode default|creative）。"""
        runtime = self._runtime
        arg = argument.strip().lower()
        valid_modes = ("default", "creative")

        current_mode = getattr(runtime, "permission_mode", "default")

        target_mode: str | None = None
        if arg:
            if arg not in valid_modes:
                self.host.notice(
                    f"未知权限模式 '{argument.strip()}'；可用模式：default、creative",
                    token="warning",
                )
                return
            target_mode = arg
        else:
            choices = [
                Choice(
                    value="default",
                    label="[1] default (默认防护)",
                    hint="每项未授权写操作均需人工确认 (安全推荐)",
                ),
                Choice(
                    value="creative",
                    label="[2] creative (创造模式)",
                    hint="除高危黑名单与越界敏感文件外，常规操作免打扰放行",
                ),
            ]
            state = self._state("选择权限模式", choices, current=current_mode)
            picked = await self._pick(state)
            if picked is None:
                self.host.notice("已取消（权限模式未变）", token="text_faint")
                return
            target_mode = picked.value

        if target_mode == current_mode:
            self.host.notice(f"当前已处于 {target_mode} 模式（未变）", token="text_faint")
            return

        if hasattr(runtime, "set_permission_mode"):
            runtime.set_permission_mode(target_mode)

        await self._persist(
            lambda: self._runtime.state_store.set_permission_mode(target_mode),
            f"权限模式 → {target_mode}",
        )

        reducer = getattr(runtime, "reducer", None) or getattr(self.host, "reducer", None)
        if reducer and hasattr(reducer, "metrics"):
            reducer.metrics.permission_mode = target_mode
        self.host.refresh_status()

        desc = "除高危与越界敏感文件外直接执行" if target_mode == "creative" else "常规写操作需人工审批"
        self.host.notice(f"—— 权限模式已切换为 {target_mode}（{desc}） ——", token="text_faint")

    async def _cmd_permissions(self, argument: str) -> None:
        """/permissions（或 /permission、/perm）：弹出权限规则与物理沙箱管理框（D132）。"""
        del argument
        runtime = self._runtime

        getter = getattr(runtime, "get_permission_snapshot", None)
        snapshot = getter() if callable(getter) else None
        if not snapshot:
            root = Path(getattr(runtime, "cwd", None) or ".").resolve()
            mode = getattr(runtime, "permission_mode", "default")
            snapshot = {
                "workspace_root": root,
                "mode": mode,
                "project_rules": [],
                "session_rules": [],
                "sensitive_items": [".git/", ".env", ".logox/config.toml", ".logox/permissions.toml"],
            }

        workspace_root = str(snapshot.get("workspace_root", "") or ".")
        mode = str(snapshot.get("mode", "default") or "default")
        mode_desc = "创造免打扰" if mode == "creative" else "严格审批"
        sensitive_list = ", ".join(snapshot.get("sensitive_items", []) or [".git/", ".env"])

        header_lines = [
            f"  [工作区沙箱] {workspace_root}",
            f"  [敏感文件保护] {sensitive_list}",
            f"  [当前运行模式] {mode} ({mode_desc})",
        ]

        choices: list[Choice] = []

        # 1. 项目持久化规则 (state.toml)
        project_rules = snapshot.get("project_rules", []) or []
        for rule in project_rules:
            rule_str = rule.to_str() if hasattr(rule, "to_str") else str(rule)
            decision = getattr(rule, "decision", "allow")
            dec_str = "allow" if str(decision).lower().endswith("allow") else "deny"
            label_prefix = "[项目允许]" if dec_str == "allow" else "[项目拒绝]"
            hint = "state.toml · 持久放行" if dec_str == "allow" else "state.toml · 持久拦截"
            choices.append(
                Choice(
                    value=f"project:{dec_str}:{rule_str}",
                    label=f"{label_prefix} {rule_str}",
                    hint=hint,
                )
            )

        # 2. 会话临时规则 (内存)
        session_rules = snapshot.get("session_rules", []) or []
        for rule in session_rules:
            rule_str = rule.to_str() if hasattr(rule, "to_str") else str(rule)
            decision = getattr(rule, "decision", "allow")
            dec_str = "allow" if str(decision).lower().endswith("allow") else "deny"
            label_prefix = "[会话允许]" if dec_str == "allow" else "[会话拒绝]"
            hint = "会话内存 · 临时放行" if dec_str == "allow" else "会话内存 · 临时拦截"
            choices.append(
                Choice(
                    value=f"session:{dec_str}:{rule_str}",
                    label=f"{label_prefix} {rule_str}",
                    hint=hint,
                )
            )

        has_rules = bool(project_rules or session_rules)
        if not has_rules:
            choices.append(
                Choice(
                    value="none",
                    label="（暂无自定义规则 · 工具审批通过选择记住时自动记录）",
                    hint="按 Esc 退出",
                )
            )

        state = self._state("权限规则与沙箱防护 (/permission)", choices)
        state.header_lines = header_lines
        state.allow_delete = True
        state.delete_prompt = "确定撤销权限规则「{target_name}」？"
        state.footer = (
            "↑↓ 切换 · Ctrl+D/Delete 撤销规则 · 输入可筛选 · Esc 退出"
            if has_rules
            else "↑↓ 浏览 · Esc 退出"
        )

        def handle_delete(choice: Choice) -> bool:
            if getattr(choice, "disabled", False) or not choice.value.startswith(("project:", "session:")):
                self.host.notice("系统沙箱与内置信息项不可删除", token="warning")
                return True

            parts = choice.value.split(":", 2)
            if len(parts) != 3:
                return True
            scope, kind, rule_str = parts

            revoker = getattr(runtime, "revoke_permission_rule", None)
            success = False
            if callable(revoker):
                success = revoker(scope, kind, rule_str)
            elif hasattr(runtime, "permission_decider"):
                dec = runtime.permission_decider
                if hasattr(dec, "revoke_permission_rule"):
                    success = dec.revoke_permission_rule(scope, kind, rule_str)
                elif hasattr(dec, "engine") and hasattr(dec.engine, "revoke_by_str"):
                    # 不在此处 import permissions.models（严格恪守 T41 架构红线）
                    # 直接传字符串由底层自行处理或通过 getattr
                    engine = dec.engine
                    try:
                        success = engine.revoke_by_str(rule_str, scope=scope, decision=kind)
                    except Exception:
                        success = False

            scope_name = "项目持久" if scope == "project" else "会话临时"
            if success:
                self.host.notice(f"已撤销{scope_name}权限规则：{rule_str}", token="text_faint")
            else:
                self.host.notice(f"撤销规则未生效或未找到：{rule_str}", token="warning")
            return True

        picked = await self._pick(state, on_delete=handle_delete if has_rules else None)
        if picked is None or picked.value == "none":
            return

    # ------------------------------------------------------------------ #
    # /help、/status、/debug
    # ------------------------------------------------------------------ #

    async def _cmd_help(self, argument: str) -> None:
        del argument
        body = render_help(self._palette(), width=self._panel_width())
        await self.host.push_overlay(
            PanelComponent(body, palette=self._palette(), max_rows=self._panel_rows())
        )

    async def _cmd_compact(self, argument: str) -> None:
        """`/compact`：立即压缩上下文（本地重算，**不调模型**）。

        与自动压缩共用同一条实现（`builder.force_compact`）与同一对事件，
        所以：时间线会显示压缩提示、状态栏的 ctx 会立刻变小、`pre_compact` 钩子也会触发。
        """
        del argument  # 本项目压缩是确定性的（拼每轮摘要），没有「给模型的压缩指令」这回事
        runtime = self._runtime
        applier = getattr(runtime, "apply_compact", None)
        if not callable(applier):
            self.host.notice("当前环境不支持手动压缩", token="warning")
            return
        report = await applier()
        if report is None:
            self.host.notice("—— 无须压缩（当前上下文未超过阈值）——", token="text_faint")
            return
        details = f"（修剪 {report.pruned_count} 条工具结果，折叠 {report.folded_turns} 轮）"
        token = "text_faint"
        if report.strategy == "prune+fold+memo":
            details = f"（阶段 3 提炼为全局工作状态备忘录，折叠 {report.folded_turns} 轮）"
            token = "accent"
        elif report.strategy == "prune+fold+truncated":
            details = f"（阶段 3 安全机械截断兜底，折叠 {report.folded_turns} 轮）"
            token = "warning"
        self.host.notice(
            f"—— 已压缩：{report.tokens_before:,} → {report.tokens_after:,} tokens {details}——",
            token=token,
        )
        self.host.refresh_status()

    async def _cmd_status(self, argument: str) -> None:
        """一行行列出当前会话的**事实**，而不是让用户去猜。"""
        del argument
        runtime = self._runtime
        palette = self._palette()
        lines = [
            ("供应商", str(runtime.provider_name)),
            ("模型", str(runtime.model)),
            ("思考档位", str(self.host.effort)),
            ("工作目录", str(runtime.cwd)),
            ("工具", "、".join(runtime.tools) or "（无）"),
            ("主题", str(self.host.theme.name)),
            # ★ D137：源码最后改动时间 —— 用来确认"我手上这个进程跑的是不是最新代码"
            ("代码", _code_stamp_with_hint()),
        ]
        # ★ D158/D159：上下文**计量方式**与 κ 样本数 ——
        #   "精确锚点 / 纯估算「以及」这个模型校准过几次"都是排查压缩行为的关键事实
        builder = getattr(runtime, "context_builder", None)
        ledger = getattr(builder, "ledger", None)
        estimator = getattr(builder, "estimator", None)
        if ledger is not None:
            anchor = getattr(ledger, "anchor", None)
            mode = "锚点+增量" if anchor is not None else f"纯估算（{ledger.last_reason}）"
            lines.append(("上下文计量", mode))
        if estimator is not None:
            key = f"{runtime.provider_name}/{runtime.model}"
            samples = estimator.samples_for(key)
            lines.append(
                (
                    "κ 校准",
                    f"{estimator.factor_for(key):.3f}（{key}，样本 {samples}）",
                )
            )
        reducer = getattr(runtime, "reducer", None) or getattr(self.host, "reducer", None)
        metrics = getattr(reducer, "metrics", None)
        win = getattr(metrics, "context_window", 0) or (
            getattr(getattr(runtime, "context_builder", None), "window_capacity", 200_000)
        )
        tokens = getattr(metrics, "context_tokens", 0) if metrics else 0
        ratio = (tokens / win) if win > 0 else 0.0
        lines.append(
            (
                "上下文窗口",
                f"{format_tokens(win)} tokens ({win:,}) · 当前占用 {tokens:,} tokens ({format_ratio(ratio)})",
            )
        )
        if getattr(runtime, "needs_login", False):
            lines.append(("登录状态", "尚未登录 —— 运行 /login"))
        perm_mode = getattr(runtime, "permission_mode", "default")
        mode_desc = (
            "creative（创造模式，常规操作免打扰）"
            if perm_mode == "creative"
            else "default（默认防护，未授权写操作需确认）"
        )
        lines.append(("权限模式", mode_desc))
        body = Text()
        for label, value in lines:
            body.append(f"  {label:<10}", style=f"bold {palette.text_primary}")
            body.append(f"{value}\n", style=palette.text_muted)
        for warning in getattr(runtime, "warnings", []) or []:
            body.append(f"\n  ⚠ {warning}\n", style=palette.warning)
        await self.host.push_overlay(
            PanelComponent(body, palette=palette, max_rows=self._panel_rows(), footer="Esc 关闭")
        )

    # ------------------------------------------------------------------ #
    # /summary（D131 会话概览与用量大盘）
    # ------------------------------------------------------------------ #

    async def _cmd_anamnesis(self, argument: str) -> None:
        service = getattr(self._runtime, "anamnesis", None)
        if service is None:
            self.host.notice("当前运行时没有入梦服务", token="warning")
            return
        parts = argument.strip().split()
        action = parts[0].lower() if parts else "auto"
        run_id = parts[1] if len(parts) == 2 else ""
        if len(parts) > 2 or (run_id and action not in {"report", "trace"}):
            self.host.notice("用法：/anamnesis [nap|sleep|stop|status|history|report [run_id]|trace [run_id]]", token="warning")
            return
        if action == "stop":
            service.note_activity("input")
            service.request_wake("用户停止入梦")
            self.host.notice("已请求暂停入梦；已完成分析会保留", token="text_faint")
        elif action in {"auto", "nap", "sleep"}:
            service.note_submission()
            self.host.notice(await service.start(action), token="text_faint")
        elif action in {"status", "report", "history", "trace"}:
            if action == "status":
                status = service.status()
                text = (f"Anamnesis · {status.mode or '待机'} · {status.phase}\n"
                        f"模型：{status.model or '尚未配置'}\n问题：{status.question or '—'}\n"
                        f"原因：{status.reason or '—'}\n剩余资料：{status.remaining}\n"
                        "空闲超过配置阈值后自动开始；发送消息或 /anamnesis stop 暂停当前窗口。")
                memory = getattr(getattr(self._runtime, "context_builder", None), "anamnesis_memory", None)
                if memory is not None and memory.skipped:
                    text += "\n本次前台跳过的记忆：\n" + "\n".join(memory.skipped)
            elif action == "history":
                history = await service.history()
                text = "Anamnesis · 当前项目入梦历史\n\n" + ("\n".join(
                    f"{item['run_id']} · {item.get('mode', '旧记录')} · {item.get('phase', '未知')}\n"
                    f"  会话：{item.get('session_id') or '旧记录未绑定会话'}\n"
                    f"  {item.get('error') or item.get('question', '')}"
                    for item in reversed(history)) or "尚无入梦记录")
                text += "\n\n/anamnesis report <run_id> 查看报告；/anamnesis trace <run_id> 查看完整过程。"
            elif action == "trace" or run_id:
                try:
                    if not run_id:
                        history = await service.history()
                        run_id = history[-1]["run_id"] if history else ""
                    text = await service.run_record(run_id, trace=action == "trace") if run_id else "尚无入梦记录"
                except (ValueError, OSError) as exc:
                    text = f"无法读取入梦记录：{exc}"
            else:
                text = await service.latest_report()
            await self.host.push_overlay(PanelComponent(Text(text), palette=self._palette(),
                                                        max_rows=self._panel_rows(), footer="↑↓ 滚动浏览 · Esc 关闭"))
        else:
            self.host.notice("用法：/anamnesis [nap|sleep|stop|status|history|report [run_id]|trace [run_id]]", token="warning")

    async def _cmd_summary(self, argument: str) -> None:
        """查看当前会话演进脉络与用量大盘（/summary）。"""
        del argument
        runtime = self._runtime
        palette = self._palette()

        # 1. 聚合轮次列表（正序展示历史演进脉络）
        lister = getattr(runtime, "list_checkpoints", None)
        checkpoints = lister() if callable(lister) else []
        chronology = list(reversed(checkpoints))

        # 2. 采集指标
        reducer = getattr(runtime, "reducer", None) or getattr(self.host, "reducer", None)
        metrics = getattr(reducer, "metrics", None)

        # 3. 排版 Rich Text
        cost_estimator = getattr(runtime, "estimate_cost", None)
        body = _render_summary_content(
            chronology,
            metrics,
            palette,
            model=str(getattr(runtime, "model", "")),
            cost_estimator=cost_estimator,
        )

        # 4. 呼起浮层面板
        await self.host.push_overlay(
            PanelComponent(
                body,
                palette=palette,
                max_rows=self._panel_rows(),
                footer="↑↓ 滚动浏览 · Esc 关闭",
            )
        )

    async def _cmd_debug(self, argument: str) -> None:
        """最近的事件流（D25）。**内核为此一行代码都没加**——它只是又一个订阅者。"""
        del argument
        events = self.host.recent_events()
        body = Text()
        if not events:
            body.append("  （还没有事件）\n", style=self._palette().text_faint)
        for line in events:
            body.append(f"  {line}\n", style=self._palette().text_muted)
        await self.host.push_overlay(
            PanelComponent(
                body,
                palette=self._palette(),
                max_rows=self._panel_rows(),
                footer="↑↓ 滚动 · Esc 关闭",
            )
        )

    # ------------------------------------------------------------------ #
    # /mcp（M9 MCP 外部服务生态状态）
    # ------------------------------------------------------------------ #

    async def _cmd_mcp(self, argument: str) -> None:
        """/mcp：查看 MCP 服务连接状态与已挂载工具。"""
        del argument
        runtime = self._runtime
        palette = self._palette()
        lister = getattr(runtime, "list_mcp_servers", None)
        statuses = lister() if callable(lister) else []

        if not statuses:
            self.host.notice("未配置任何外部 MCP 服务（可在 config.toml 中配置 [mcp.servers]）", token="warning")
            return

        body = Text()
        body.append("MCP 外部生态服务状态：\n\n", style=f"bold {palette.accent}")
        for s in statuses:
            state_val = getattr(s, "state", "stopped")
            state_str = state_val.value if hasattr(state_val, "value") else str(state_val)

            if state_str == "connected":
                state_style = palette.success
                state_label = "● CONNECTED"
            elif state_str == "offline":
                state_style = palette.danger
                state_label = "✖ OFFLINE"
            elif state_str == "disabled":
                state_style = palette.text_muted
                state_label = "○ DISABLED"
            else:
                state_style = palette.warning
                state_label = f"◒ {state_str.upper()}"

            body.append(f"  • {s.name:<12} ", style=f"bold {palette.text_primary}")
            body.append(f"[{state_label}] ", style=state_style)
            cmd = s.command or "（默认）"
            body.append(f"命令: {cmd}  ", style=palette.text_muted)
            body.append(f"工具数: {s.tool_count}\n", style=palette.text_faint)

            if s.description:
                body.append(f"    说明: {s.description}\n", style=palette.text_faint)
            if s.tools:
                tools_str = ", ".join(s.tools[:6])
                if len(s.tools) > 6:
                    tools_str += f" 等 {len(s.tools)} 个工具"
                body.append(f"    工具: {tools_str}\n", style=palette.text_faint)
            if s.error:
                body.append(f"    异常: {s.error}\n", style=palette.danger)
            body.append("\n")

        await self.host.push_overlay(
            PanelComponent(
                body,
                palette=palette,
                max_rows=self._panel_rows(),
                footer="↑↓ 滚动 · Esc 关闭",
            )
        )

    # ------------------------------------------------------------------ #
    # /skills、/commands（M10 扩展生态：技能包与模板命令）
    # ------------------------------------------------------------------ #

    async def _cmd_skills(self, argument: str) -> None:
        """/skills：查看与阅读大模型专业技能包。"""
        runtime = self._runtime
        palette = self._palette()
        target_name = argument.strip()

        # 如果用户传了技能名（如 /skills nature-plot 或 /skill nature-plot），直接阅读该技能全文
        if target_name:
            reader = getattr(runtime, "read_skill_content", None)
            content = reader(target_name) if callable(reader) else None
            if not content:
                self.host.notice(f"未找到名为 {target_name!r} 的技能包（运行 /skills 查看列表）", token="warning")
                return
            body = Text()
            body.append(f"【技能说明书: {target_name}】\n\n", style=f"bold {palette.accent}")
            body.append(content, style=palette.text_primary)
            await self.host.push_overlay(
                PanelComponent(
                    body,
                    palette=palette,
                    max_rows=self._panel_rows(),
                    footer="↑↓ 滚动 · Esc 关闭",
                )
            )
            return

        lister = getattr(runtime, "list_skills", None)
        skills = lister() if callable(lister) else []

        if not skills:
            self.host.notice("当前未挂载任何技能包（可在 .logox/skills/<name>/SKILL.md 中定义）", token="warning")
            return

        body = Text()
        body.append("已挂载的大模型专业技能包 (Skills)：\n\n", style=f"bold {palette.accent}")
        for s in skills:
            body.append(f"  • {s.name:<18} ", style=f"bold {palette.text_primary}")
            body.append(f"[{s.scope.upper()}]\n", style=palette.accent)
            body.append(f"    说明: {s.description}\n", style=palette.text_muted)
            body.append(f"    文件: {s.path}\n\n", style=palette.text_faint)

        await self.host.push_overlay(
            PanelComponent(
                body,
                palette=palette,
                max_rows=self._panel_rows(),
                footer="输入 /skill <名称> 查看说明书 · ↑↓ 滚动 · Esc 关闭",
            )
        )

    async def _cmd_commands(self, argument: str) -> None:
        """/commands：查看已配置的 L1 模板命令。"""
        del argument
        runtime = self._runtime
        palette = self._palette()
        lister = getattr(runtime, "list_custom_commands", None)
        cmds = lister() if callable(lister) else []

        if not cmds:
            self.host.notice("未配置任何自定义模板命令（可在 .logox/commands/<name>.md 中定义）", token="warning")
            return

        body = Text()
        body.append("已配置的 L1 提示词模板命令 (Custom Commands)：\n\n", style=f"bold {palette.accent}")
        for c in cmds:
            body.append(f"  • /{c.name:<16} ", style=f"bold {palette.text_primary}")
            body.append(f"[{c.scope.upper()}]\n", style=palette.accent)
            body.append(f"    说明: {c.description}\n", style=palette.text_muted)
            body.append(f"    文件: {c.path}\n\n", style=palette.text_faint)

        await self.host.push_overlay(
            PanelComponent(
                body,
                palette=palette,
                max_rows=self._panel_rows(),
                footer="↑↓ 滚动 · Esc 关闭",
            )
        )

    # ------------------------------------------------------------------ #
    # /clear、/exit
    # ------------------------------------------------------------------ #

    async def _cmd_clear(self, argument: str) -> None:
        del argument
        self.host.clear_timeline()
        self.host.notice("已清空当前对话显示（终端回滚缓冲里的历史不受影响）", token="text_faint")

    async def _cmd_exit(self, argument: str) -> None:
        del argument
        self.host.stop()

    # ------------------------------------------------------------------ #
    # /resume、/new（M8 会话持久化与热切换）
    # ------------------------------------------------------------------ #

    async def _cmd_resume(self, argument: str) -> None:
        """/resume [序号]：弹出 Pi 风格会话列表框，选择后热切换；也可以直接 /resume <序号>。"""
        from pathlib import Path

        cwd = getattr(self._runtime, "cwd", None) or Path.cwd()
        lister = getattr(self._runtime, "list_sessions", None)
        sessions = lister(cwd) if callable(lister) else []

        if not sessions:
            self.host.notice("当前项目目录没有历史会话（已在当前会话中）", token="warning")
            return

        arg = argument.strip()
        if arg:
            # 允许用户直接 /resume 1 直达
            if arg.isdigit():
                idx = int(arg)
                if 1 <= idx <= len(sessions):
                    target = sessions[idx - 1]
                    count = self.host.switch_session(target.file_path)
                    self.host.notice(
                        f"—— 已恢复历史会话 [{idx}]：{target.title_summary}（共 {count} 条消息）——",
                        token="text_faint",
                    )
                    return
            matched = [
                s
                for s in sessions
                if arg in s.session_id or arg in s.title_summary
            ]
            if len(matched) == 1:
                target = matched[0]
                count = self.host.switch_session(target.file_path)
                self.host.notice(
                    f"—— 已恢复历史会话：{target.title_summary}（共 {count} 条消息）——",
                    token="text_faint",
                )
                return

        choices: list[Choice] = []
        current_session_file = str(getattr(self._runtime, "resume_file", ""))
        content_w = getattr(self.host, "content_width", 80)
        for idx, s in enumerate(sessions, start=1):
            is_current = (
                current_session_file
                and str(s.file_path.resolve()) == str(Path(current_session_file).resolve())
            )
            hint = "当前" if is_current else ("最近" if idx == 1 else "")
            # 计算摘要可占用的最大宽度（占满一行），保证序号、轮次和 hint 均不被挤出屏幕
            prefix = f"[{idx}] "
            suffix = f" · {s.turn_count} 轮"
            hint_cells = (cell_len(hint) + 2) if hint else 0
            fixed_cells = 3 + cell_len(prefix) + cell_len(suffix) + hint_cells + 2
            avail_summary = max(10, content_w - fixed_cells)

            if cell_len(s.title_summary) > avail_summary:
                summary_disp = clip(s.title_summary, avail_summary, ellipsis="…")
            else:
                summary_disp = s.title_summary

            label = f"{prefix}{summary_disp}{suffix}"
            choices.append(Choice(value=str(s.file_path), label=label, hint=hint))

        state = self._state("历史会话列表", choices)
        state.allow_delete = True
        state.footer = "↑↓ 切换 · Enter 恢复 · Ctrl+D 删除 · Esc 取消"
        deleted_current = False

        def handle_delete(choice: Choice) -> bool:
            nonlocal deleted_current
            path = Path(choice.value)
            is_current = self.host.is_current_session(path)
            try:
                self.host.delete_session(path, soft=True)
                if is_current:
                    deleted_current = True
                    self.host.new_session()
                    self.host.notice("已将当前会话移入回收站并创建新会话", token="warning")
                    return False  # 关闭选择器
                self.host.notice(f"已将历史会话移入回收站：{path.name}", token="text_faint")
                return True  # 保持选择器打开
            except Exception as exc:
                self.host.notice(f"删除会话失败：{exc}", token="danger")
                return True

        picked = await self._pick(state, on_delete=handle_delete)
        if picked is None:
            if not deleted_current:
                self.host.notice("已取消会话切换（当前会话未变）", token="text_faint")
            return

        selected_path = Path(picked.value)
        title = next(
            (s.title_summary for s in sessions if s.file_path == selected_path),
            selected_path.stem,
        )
        count = self.host.switch_session(selected_path)
        self.host.notice(
            f"—— 已恢复历史会话：{title}（共 {count} 条消息）——", token="text_faint"
        )

    async def _cmd_new(self, argument: str) -> None:
        """/new：清空当前屏幕并开启全新会话。"""
        del argument
        new_path = self.host.new_session()
        stem = Path(new_path).stem if new_path else "新会话"
        self.host.notice(f"—— 已开启全新会话（ID: {stem}）——", token="text_faint")

    # ------------------------------------------------------------------ #
    # /rewind、/undo（M8 代码快照与时空穿梭回滚）
    # ------------------------------------------------------------------ #

    async def _cmd_rewind(self, argument: str) -> None:
        """/rewind [序号]：弹出 Pi 风格检查点列表框选择回滚；也可以直接 /rewind <序号>。"""
        lister = getattr(self._runtime, "list_checkpoints", None)
        checkpoints = lister() if callable(lister) else []

        if not checkpoints:
            self.host.notice("当前会话没有代码修改记录，无法回滚", token="warning")
            return

        target_turn: int | None = None
        arg = argument.strip()
        if arg:
            if arg.isdigit():
                idx = int(arg)
                if 1 <= idx <= len(checkpoints):
                    target_turn = checkpoints[idx - 1].turn
                else:
                    self.host.notice(f"序号 {idx} 超出检查点范围（1~{len(checkpoints)}）", token="warning")
                    return
            else:
                self.host.notice("用法：/rewind 或 /rewind <序号>", token="warning")
                return

        if target_turn is None:
            choices: list[Choice] = []
            for idx, cp in enumerate(checkpoints, start=1):
                if cp.files:
                    file_names = [Path(f.path).name for f in cp.files]
                    names_summary = ", ".join(file_names[:2])
                    if len(file_names) > 2:
                        names_summary += f" 等 {len(file_names)} 个文件"
                    file_part = f"{len(cp.files)} 个文件 ({names_summary})"
                else:
                    file_part = "纯对话 (无文件修改)"

                summary_desc = getattr(cp, "turn_summary", "") or cp.user_prompt or ""
                # ★ 用户裁定 Q-D：**标出摘要的来源**，否则用户分不清「模型自己写的」
                # 与「我们补写/自动生成的」—— 也就没法据此判断该不该信这条摘要。
                source = getattr(cp, "summary_source", "") or ""
                if source in ("model_fallback", "deterministic"):
                    mark = "，自动摘要" if source == "deterministic" else "，模型补写"
                    summary_desc = f"{summary_desc}（{mark.lstrip('，')}）"
                label = f"[{idx}] 轮次 {cp.turn} · {file_part}"
                hint = summary_desc
                choices.append(Choice(value=str(cp.turn), label=label, hint=hint))

            state = self._state("时空穿梭检查点回滚", choices)
            state.footer = "↑↓ 切换 · Enter 回滚 · Esc 取消"
            picked = await self._pick(state)
            if picked is None:
                self.host.notice("已取消回滚（当前代码未变）", token="text_faint")
                return

            target_turn = int(picked.value)

        # 检查外部修改冲突 (External Drift Guard)
        conflict_checker = getattr(self._runtime, "check_rewind_conflicts", None)
        conflicts = conflict_checker(target_turn) if callable(conflict_checker) else []
        force = False
        if conflicts:
            conflict_paths = ", ".join(c.path for c in conflicts[:2])
            if len(conflicts) > 2:
                conflict_paths += f" 等 {len(conflicts)} 个文件"

            conflict_choices = [
                Choice(value="force", label="[1] 强制覆盖本地改动 (还原至快照)", hint="覆盖外部手动修改"),
                Choice(value="abort", label="[2] 终止回滚 (保留当前物理文件)", hint="不进行任何还原"),
            ]
            conflict_state = self._state(f"外部文件修改冲突：{conflict_paths}", conflict_choices)
            conflict_state.footer = "↑↓ 切换 · Enter 确认 · Esc 终止"
            resolution = await self._pick(conflict_state)
            if resolution is None or resolution.value != "force":
                self.host.notice("已终止回滚（外部修改文件保留不变）", token="warning")
                return
            force = True

        res = await self.host.rewind(target_turn, force=force)
        if res and getattr(res, "success", False):
            self.host.refresh_status()
            self.host.notice(f"—— {res.message} ——", token="success")
        elif res:
            self.host.notice(f"回滚未完成：{res.message}", token="danger")

    async def _cmd_undo(self, argument: str) -> None:
        """/undo：快捷撤销上一轮代码修改（等同于回滚最近一个修改轮次）。"""
        del argument
        await self._cmd_rewind("1")


    # `/quit` 与 `/q` **不需要各自的实现**：`tui/commands.py` 的 `ALIASES`
    # 在解析阶段就把它们归一成 `exit` 了。少三个分支，也少三处会漏改的地方。

    # ------------------------------------------------------------------ #
    # 浮层尺寸（由界面决定，命令只管用）
    # ------------------------------------------------------------------ #

    def _panel_width(self) -> int:
        return int(getattr(self.host, "content_width", 80))

    def _panel_rows(self) -> int:
        return int(getattr(self.host, "content_rows", 20))

def _code_stamp_with_hint() -> str:
    """`/status` 里那一行的值：源码时间 + 一句判定提示（D137）。"""
    import time as _time

    from logox.tui.buildinfo import code_root, code_stamp

    stamp = code_stamp()
    try:
        latest = max(path.stat().st_mtime for path in code_root().rglob("*.py"))
        started = getattr(_code_stamp_with_hint, "_process_started", None)
        if started is None:
            started = _code_stamp_with_hint._process_started = _time.time()  # type: ignore[attr-defined]
        # 进程启动**早于**源码最后改动 ⇒ 这个进程跑的是旧代码（提示重启）
        return f"{stamp}（⚠️ 进程启动于源码改动之前，重启 logox 才生效）" if latest > started else stamp
    except OSError:  # pragma: no cover
        return stamp


def _render_summary_content(
    chronology: list[Any],
    metrics: Any,
    palette: Any,
    model: str = "",
    cost_estimator: Callable[[Any, str], float | None] | None = None,
) -> Text:
    """排版会话演进脉络与用量大盘（D131 / MODULE_summary_command.md）。"""
    body = Text()
    body.append("会话概览与用量大盘 (/summary)\n\n", style=f"bold {palette.accent}")

    # 1. 对话演进脉络
    body.append("【对话演进脉络】\n", style=f"bold {palette.text_primary}")
    if not chronology:
        body.append("  （当前会话尚无交互轮次）\n\n", style=palette.text_faint)
    else:
        for idx, cp in enumerate(chronology, start=1):
            if getattr(cp, "files", None):
                file_names = [Path(f.path).name for f in cp.files]
                names_summary = ", ".join(file_names[:2])
                if len(file_names) > 2:
                    names_summary += f" 等 {len(file_names)} 个文件"
                file_part = f"改动 {len(cp.files)} 个文件: {names_summary}"
            else:
                file_part = "纯对话 (无文件修改)"

            prompt_snippet = getattr(cp, "user_prompt", "") or ""
            if prompt_snippet:
                prompt_snippet = prompt_snippet.splitlines()[0].strip()
                if len(prompt_snippet) > 30:
                    prompt_snippet = prompt_snippet[:27] + "…"
                header_line = f" #{idx} 轮次 {cp.turn} · {prompt_snippet} ({file_part})"
            else:
                header_line = f" #{idx} 轮次 {cp.turn} · {file_part}"

            body.append(header_line + "\n", style=f"bold {palette.text_primary}")

            # 意图/摘要
            summary_desc = getattr(cp, "turn_summary", "") or ""
            source = getattr(cp, "summary_source", "") or ""
            if not summary_desc:
                summary_desc = getattr(cp, "user_prompt", "（无轮次摘要）")
            elif source in ("model_fallback", "deterministic"):
                mark = "自动摘要" if source == "deterministic" else "模型补写"
                summary_desc = f"{summary_desc}（{mark}）"

            body.append(f"    意图: {summary_desc}\n\n", style=palette.text_muted)

    # 2. 分割线
    body.append("  " + "─" * 68 + "\n\n", style=palette.text_faint)

    # 3. 全局统计大盘
    body.append("【全局统计大盘】\n", style=f"bold {palette.text_primary}")

    usage_in = getattr(metrics, "usage_input", 0) if metrics else 0
    usage_out = getattr(metrics, "usage_output", 0) if metrics else 0
    total_tokens = (
        getattr(metrics, "total_tokens", usage_in + usage_out)
        if metrics
        else (usage_in + usage_out)
    )
    cached_in = getattr(metrics, "cached_input", None) if metrics else None
    cache_ratio = getattr(metrics, "cache_ratio", None) if metrics else None

    body.append(
        f"  📊 Token 总量：{total_tokens:,} tokens\n",
        style=f"bold {palette.text_primary}",
    )
    cached_str = (
        f"{cached_in:,} tok ({format_ratio(cache_ratio)})"
        if cached_in is not None
        else "— (未上报)"
    )
    body.append(
        f"     ├─ 输入: {usage_in:,} tok │ 输出: {usage_out:,} tok │ 缓存命中: {cached_str}\n",
        style=palette.text_muted,
    )

    tool_calls = getattr(metrics, "tool_calls", 0) if metrics else 0
    tool_ms = getattr(metrics, "tool_ms", 0) if metrics else 0
    duration_str = format_duration(tool_ms) if tool_ms > 0 else "0s"
    body.append(
        f"  🛠️ 工具调用：{tool_calls} 次 (累计耗时 {duration_str})\n",
        style=palette.text_muted,
    )

    cost_usd = getattr(metrics, "cost_usd", 0.0) if metrics else 0.0
    model_name = model or getattr(metrics, "model", "") or "未知模型"
    if cost_usd > 0:
        cost_str = f"{format_cost(cost_usd)} USD"
    elif total_tokens > 0 and cost_estimator is not None:
        from logox.kernel.events import Usage

        est = cost_estimator(
            Usage(
                input_tokens=usage_in,
                output_tokens=usage_out,
                cached_input_tokens=cached_in,
            ),
            model_name,
        )
        cost_str = f"{format_cost(est)} USD" if est is not None else "— (未知模型定价)"
    elif total_tokens > 0:
        cost_str = "— (未知模型定价)"
    else:
        cost_str = "$0 USD"

    body.append(
        f"  💰 预估费用：{cost_str} (当前模型: {model_name})\n",
        style=palette.text_muted,
    )

    return body
