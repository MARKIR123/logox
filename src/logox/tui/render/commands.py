"""斜杠命令的**实现**（新界面，D80 §9 第 6 步）。

为什么单独一个文件
==================

`render/app.py` 的职责是"把内核接到渲染器上"（订阅事件、驱动帧、管输入）。
而 ``/login`` 这类命令要做的是**一串带浮层的交互**：选供应商 → 输密钥 →
抓模型 → 确认保存 → 切模型。把它塞进 `app.py` 会让那个文件同时负责
"渲染循环"与"业务对话"，两边都不好读、也不好测。

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
import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from rich.text import Text

from rich.cells import cell_len

from logox.tui.commands import (
    PLANNED_COMMANDS,
    ResolvedCommand,
    format_planned_notice,
    format_unknown_notice,
)
from logox.tui.content.help import render_help
from logox.tui.content.overlay import Choice, PickerState
from logox.tui.format import clip, format_ratio, format_tokens
from logox.tui.render.components.overlay import (
    ConfirmComponent,
    PanelComponent,
    PickerComponent,
    PromptComponent,
)

__all__ = ["EFFORT_LEVELS", "CommandHost", "CommandRunner"]

logger = logging.getLogger("logox.tui.render.commands")

#: 思考档位。**必须与** ``ProviderConfig.thinking_effort`` 的 Literal 一致
#: （测试里有一条断言盯着这件事，见 `tests/tui/test_render_commands.py`）。
#:
#: 为什么不直接引用 schema 里那个 Literal：界面只该知道"档位"这个名字，
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
        而事件循环同时还在跑流式输出（症状是"打字突然一顿"）。
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
        写出的缺陷是"取消到一半留下半成品状态"，因此本方法只在**最后一步之后**
        才碰内核与 `state.toml`。
        """
        del argument  # /login 不接受参数（供应商名由选择器给）
        runtime = self._runtime
        if getattr(runtime, "registry", None) is None:
            self.host.notice("/login 需要 Provider 注册表（当前装配没有提供）", token="warning")
            return

        names = list(runtime.available_providers())
        if not names:
            self.host.notice("没有任何可用的 Provider", token="warning")
            return

        choices: list[Choice] = []
        for name in names:
            details = runtime.provider_details(name)
            env_name = details.get("api_key_env") or ""
            hint = f"需要 {env_name}" if env_name else "无需密钥"
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

        api_key: str | None = None
        if env_name:
            entered = await self.host.push_overlay(
                PromptComponent(
                    title=f"输入 API Key · {provider_name}",
                    label=f"环境变量名：{env_name}（粘贴后按 Enter）",
                    palette=self._palette(),
                )
            )
            if not entered:
                self.host.notice("已取消登录（当前会话不变）", token="text_faint")
                return
            api_key = entered

        # **先验证再替换**：构造失败时当前 provider 原封不动。
        try:
            provider = runtime.build_provider(provider_name, api_key=api_key)
        except Exception as exc:
            self.host.notice(f"无法使用 {provider_name}：{exc}", token="danger")
            return

        # ---- ★ 自动抓取真实可用模型（D65） ----
        # 密钥刚到手，这是唯一能问端点"你有哪些模型"的时刻。抓取失败**不影响登录**：
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
        await self._persist(
            lambda: runtime.state_store.set_last_model(provider=provider_name, model=model),
            f"供应商 {provider_name}",
        )

    async def _remember_key(self, env_name: str, api_key: str) -> None:
        """问"要不要把密钥写进文件"，然后照办。

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
        """不带参数 → 弹窗选；带参数 → 直接切（两种都支持）。

        直接切是留给"我知道要哪个"的场景（也是脚本化与测试的入口），
        弹窗是留给"我不记得有哪些"的场景。**同一个切换实现**，两条入口。
        """
        wanted = argument.strip()
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

    async def _switch_model(self, model: str) -> None:
        """切换模型：改内核 → 更新状态行 → 写 `state.toml`。"""
        runtime = self._runtime
        setter = getattr(getattr(runtime, "kernel", None), "set_model", None)
        if callable(setter):
            try:
                setter(model)
            except Exception as exc:
                self.host.notice(f"无法切换到 {model}：{exc}", token="danger")
                return
        runtime.model = model
        self.host.notice(f"—— 模型已切换为 {model}（下一次请求生效）——", token="text_faint")
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
        available = theme_module.list_themes()
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
    # /effort
    # ------------------------------------------------------------------ #

    async def _cmd_effort(self, argument: str) -> None:
        """切换思考档位（D42 / **D58：设置即生效**）。

        "生效"的精确含义：**下一次将要发起的模型请求**用新档位。正在流式生成的那一次
        不受影响——请求参数在发起时就固化进 `ChatRequest` 了；要"追溯"只能取消当前
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
    # /help、/status、/debug
    # ------------------------------------------------------------------ #

    async def _cmd_help(self, argument: str) -> None:
        del argument
        body = render_help(self._palette(), width=self._panel_width())
        await self.host.push_overlay(
            PanelComponent(body, palette=self._palette(), max_rows=self._panel_rows())
        )

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
        ]
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
        body = Text()
        for label, value in lines:
            body.append(f"  {label:<10}", style=f"bold {palette.text_primary}")
            body.append(f"{value}\n", style=palette.text_muted)
        for warning in getattr(runtime, "warnings", []) or []:
            body.append(f"\n  ⚠ {warning}\n", style=palette.warning)
        await self.host.push_overlay(
            PanelComponent(body, palette=palette, max_rows=self._panel_rows(), footer="Esc 关闭")
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
