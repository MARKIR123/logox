"""命令行入口（D29：快速路径禁止导入界面层与 Provider SDK）。

**性能红线**：``logox --version`` / ``--help`` 必须在 **300ms** 内返回，因此
本模块顶层只允许 import ``argparse`` / ``sys`` / ``os`` / ``pathlib`` 与
``logox``（版本号）。pydantic（配置层）与界面层一律**延迟到真正
需要时**才导入。
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path
from typing import Any

from logox import __version__

__all__ = ["build_parser", "main"]

EXIT_OK = 0
EXIT_CONFIG_ERROR = 1
EXIT_USAGE = 2
EXIT_NOT_READY = 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="logox",
        description="Logox —— 内核极简、外延极松、界面体面的终端 Agent（TUI）",
        epilog="文档：docs/PRD.md · docs/ARCHITECTURE.md · docs/UI-SPEC.md",
    )
    parser.add_argument(
        "-V",
        "--version",
        action="store_true",
        help="打印版本并退出（不加载配置与界面）",
    )
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="加载并校验配置，打印来源与问题清单后退出",
    )
    parser.add_argument(
        "--print-config",
        action="store_true",
        help="打印合并后的生效配置（TOML）后退出",
    )
    parser.add_argument(
        "--strict-config",
        action="store_true",
        help="配置存在任何问题时以非 0 退出码结束（供脚本化使用）",
    )
    parser.add_argument("--debug", action="store_true", help="输出调试级日志")
    parser.add_argument(
        "--chat",
        action="store_true",
        help="最小文本模式：在终端里直接与模型对话（不需要全屏界面；M3）",
    )
    parser.add_argument(
        "-n",
        "--new",
        dest="new_session",
        action="store_true",
        help="开启全新会话（不自动加载当前项目的历史会话）",
    )
    parser.add_argument(
        "-c",
        "--continue",
        dest="continue_session",
        action="store_true",
        help="继续当前项目的最近一次会话（若无历史会话则直接开启新会话）",
    )
    parser.add_argument(
        "-r",
        "--resume",
        dest="resume_session",
        action="store_true",
        help="交互式选择并恢复当前项目的历史会话",
    )
    parser.add_argument(
        "--cwd",
        default=None,
        metavar="路径",
        help="指定工作目录（默认使用当前目录）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # ---------------- 快速路径（不得 import pydantic / 界面层 / SDK） ----------------
    if args.version:
        sys.stdout.write(f"logox {__version__}\n")
        return EXIT_OK

    return _run(args)


# --------------------------------------------------------------------------- #
# 以下函数可以承担较重的导入
# --------------------------------------------------------------------------- #


def _format_time_ago(ts: float, now_ts: float | None = None) -> str:
    """把时间戳转为易读的人性化相对时间。"""
    from datetime import datetime, timedelta

    now = datetime.fromtimestamp(now_ts) if now_ts else datetime.now()
    dt = datetime.fromtimestamp(ts)
    diff_s = (now - dt).total_seconds()
    if diff_s < 60:
        return "刚刚"
    if diff_s < 3600:
        return f"{max(1, int(diff_s // 60))}分钟前"
    if diff_s < 86400 and dt.date() == now.date():
        return f"今天 {dt.strftime('%H:%M')}"
    if (now.date() - dt.date()) == timedelta(days=1):
        return f"昨天 {dt.strftime('%H:%M')}"
    return dt.strftime("%Y-%m-%d %H:%M")


def _resolve_session(args: argparse.Namespace, cwd: Path, paths: Any) -> Path | None:
    """根据 CLI 参数解析或交互式选择要恢复的会话文件（D98）。

    规则：
    1. 显式 --new / -n：直接开启全新会话（返回 None）。
    2. 无任何历史：静默开启新会话（返回 None）。
    3. 显式 --resume / -r：交互式列表展示并选择。
    4. 默认无参 或 --continue / -c：自动恢复最近活跃的会话（sessions[0].file_path）。
    """
    if getattr(args, "new_session", False):
        return None

    import time

    from logox.store.manager import SessionManager

    manager = SessionManager(paths.sessions if hasattr(paths, "sessions") else None)
    sessions = manager.list_sessions(cwd)

    # 规则：如果当前项目目录没有历史对话则直接拉起新对话
    if not sessions:
        if getattr(args, "resume_session", False):
            sys.stdout.write("当前项目目录没有历史会话，直接开启新会话。\n")
            sys.stdout.flush()
        return None

    # -r / --resume：交互式列表展示并选择
    if getattr(args, "resume_session", False):
        import re

        while True:
            if not sessions:
                sys.stdout.write("当前项目目录没有历史会话，直接开启新会话。\n")
                sys.stdout.flush()
                return None

            sys.stdout.write(f"历史会话列表（当前目录: {cwd}）：\n")
            now_ts = time.time()
            for idx, s in enumerate(sessions, start=1):
                recent_tag = " [最近]" if idx == 1 else ""
                time_str = _format_time_ago(s.updated_at, now_ts)
                sys.stdout.write(
                    f"  [{idx}] {s.title_summary} · {s.turn_count} 轮 ({time_str}){recent_tag}\n"
                )
            sys.stdout.write("  [n] 开启新会话\n")
            sys.stdout.write("  [d<序号>] 删除会话到回收站（如 d2）\n\n")
            sys.stdout.flush()

            prompt = "请选择要恢复的会话 [默认 1，直接按 Enter]: "
            try:
                if not sys.stdin.isatty():
                    return sessions[0].file_path
                sys.stdout.write(prompt)
                sys.stdout.flush()
                raw = sys.stdin.readline()
                if not raw:
                    return sessions[0].file_path
                choice = raw.strip()
                if not choice or choice == "1":
                    return sessions[0].file_path
                if choice.lower() in ("n", "new"):
                    return None
                if choice.lower().startswith("d") or choice.lower().startswith("del"):
                    match = re.search(r"\d+", choice)
                    if match:
                        del_idx = int(match.group())
                        if 1 <= del_idx <= len(sessions):
                            target = sessions[del_idx - 1]
                            manager.delete_session(target.file_path, soft=True)
                            sys.stdout.write(
                                f"已将会话 [{del_idx}] {target.title_summary} 移入回收站 (.trash)。\n\n"
                            )
                            sessions = manager.list_sessions(cwd)
                            continue
                        sys.stdout.write(f"序号越界：请输入 1 到 {len(sessions)} 之间的数字。\n\n")
                        continue
                if choice.isdigit():
                    val = int(choice)
                    if 1 <= val <= len(sessions):
                        return sessions[val - 1].file_path
                if choice.lower() in ("q", "quit", "exit"):
                    sys.exit(EXIT_OK)
                sys.stdout.write("无效选择，默认恢复最近会话 [1]。\n")
                sys.stdout.flush()
                return sessions[0].file_path
            except (KeyboardInterrupt, EOFError):
                sys.stdout.write("\n")
                sys.exit(EXIT_OK)

    # 默认（无参数）或显式 -c / --continue：自动加载最近一次活跃会话
    return sessions[0].file_path


def _run(args: argparse.Namespace) -> int:
    _configure_logging(args.debug)

    from logox.config.loader import load, render_issues
    from logox.errors import ConfigValidationError
    from logox.paths import LogoxPaths
    from logox.providers.registry import BUILTIN_SPECS

    cwd = Path(args.cwd).resolve() if args.cwd else Path.cwd()
    paths = LogoxPaths.default()

    try:
        bundle = load(
            cwd,
            strict=args.strict_config,
            paths=paths,
            known_providers=tuple(BUILTIN_SPECS),
        )
    except ConfigValidationError as exc:
        sys.stderr.write(render_issues(exc.issues) + "\n")
        return EXIT_CONFIG_ERROR

    if args.check_config:
        sys.stdout.write(_startup_summary(cwd, paths, bundle) + "\n")
        return EXIT_CONFIG_ERROR if bundle.has_errors else EXIT_OK

    if args.print_config:
        from logox.config import writer

        sys.stdout.write(writer.dumps(bundle.config.model_dump()))
        return EXIT_OK

    resume_file = _resolve_session(args, cwd, paths)

    if args.chat:
        return _run_chat(cwd, bundle, resume_file=resume_file)

    # `logox`（无参数）就是进**新界面**——它是唯一的界面（D85：删掉了旧的全屏实现）。
    return _run_tui(cwd, paths, bundle, resume_file=resume_file)


def _run_tui(cwd: Path, paths: Any, bundle: Any, resume_file: Path | None = None) -> int:
    """启动界面（`src/logox/tui/render/`：自研渲染器、无侧栏、走主屏）。

    这是**唯一的界面入口**。历史上有过第二条路（Textual 全屏 + 侧栏面板），
    已经在 D85 删掉——理由是它带来的东西（常驻侧栏）正是用户明确不要的，
    而代价是"两套渲染、两套测试、两份文档"。
    """
    from logox.app import (
        EXIT_CONFIG_ERROR,
        StartupError,
        build_session_start,
        prepare_runtime,
        render_startup_error,
    )

    # ⚠️ 必须走 `prepare_runtime`（解析并加载 `.env` + 装配），**不能**只调
    # `build_runtime`——这条路径最初就是这么写的，后果是"用户在 /login 里存的
    # 密钥完全不生效，启动后显示尚未登录"，而设置界面看着一切正常（实测踩到）。
    outcome = prepare_runtime(bundle, cwd, paths, resume_file=resume_file)
    if isinstance(outcome, StartupError):
        sys.stderr.write(render_startup_error(outcome) + "\n")
        return outcome.exit_code or EXIT_CONFIG_ERROR

    from logox.tui.render.app import run_inline

    for warning in outcome.warnings:
        sys.stderr.write(f"⚠ {warning}\n")

    log_dir = paths.logs if hasattr(paths, "logs") else (paths.root / "logs")
    log_dir.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(log_dir / "logox.log", encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    root_logger = logging.getLogger()
    old_handlers = list(root_logger.handlers)
    root_logger.handlers = [file_handler]
    try:
        return run_inline(outcome, session_start=build_session_start(outcome))
    finally:
        root_logger.handlers = old_handlers


def _chat_sigint_action(kernel: Any) -> bool:
    """Ctrl+C 的策略（**抽成函数是为了能直接测**，不必真的发信号）。

    返回 ``True`` = "已中断当前回合，继续运行"；``False`` = "该退出了"。

    语义与界面里的 Esc 完全一致（D51）：**第一次中断回合，第二次才退出**。
    回合跑在独立 task 上，所以中断它根本不需要退出进程。
    """
    return bool(kernel is not None and kernel.cancel())


def _run_chat(cwd: Path, bundle: Any, resume_file: Path | None = None) -> int:
    """最小文本模式（M3 / D53）：让"能读文件并回答"可以真的手工跑一遍。

    **stdout 只承载助手的正文**，其余一切（思考过程、工具调用、错误、重试）都走
    **stderr**。这样 ``logox --chat < input.txt > answer.txt`` 拿到的就是干净的答案，
    而不是掺着 ``[工具] read ✓`` 的日志。
    """
    import asyncio
    import signal

    state: dict[str, Any] = {"kernel": None}

    def on_sigint(_signum: int, _frame: Any) -> None:
        if _chat_sigint_action(state["kernel"]):
            sys.stderr.write("\n（已中断当前回合；再按一次 Ctrl+C 退出）\n")
            sys.stderr.flush()
            return
        raise KeyboardInterrupt

    previous = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, on_sigint)
    try:
        return int(asyncio.run(_chat_main(cwd, bundle, state, resume_file=resume_file)))
    except KeyboardInterrupt:
        sys.stderr.write("\n")
        return EXIT_OK
    finally:
        signal.signal(signal.SIGINT, previous)


async def _chat_main(
    cwd: Path,
    bundle: Any,
    state: dict[str, Any],
    resume_file: Path | None = None,
) -> int:
    """文本模式的主协程。**所有重依赖都在这里才导入**（D29 快速路径保护）。"""

    from logox.config.schema import ProviderConfig
    from logox.context import HierarchicalContextBuilder
    from logox.errors import LogoxError
    from logox.kernel.bus import EventBus
    from logox.kernel.loop import KernelLoop
    from logox.kernel.registry import ToolRegistry
    from logox.providers.base import ThinkingConfig
    from logox.providers.pricing import estimate_cost_usd
    from logox.providers.registry import ProviderRegistry, ProviderSpec

    config = bundle.config
    provider_config: ProviderConfig = config.provider

    # ---- 装配 Provider ----
    overrides = {
        name: ProviderSpec(name=name, **instance.model_dump(include={"kind", "base_url", "api_key_env", "models"}, exclude_unset=True))
        for name, instance in config.providers.items()
    }
    registry = ProviderRegistry.with_builtins(overrides)
    try:
        provider = registry.build(provider_config.name)
    except LogoxError as exc:
        sys.stderr.write(f"{exc}\n")
        return EXIT_CONFIG_ERROR

    model = provider_config.model or registry.default_model(provider_config.name) or ""
    if not model:
        sys.stderr.write(
            f"Provider {provider_config.name!r} 没有可用的模型。"
            "请在 config.toml 的 [provider] 里设置 model。\n"
        )
        return EXIT_CONFIG_ERROR

    # ---- 装配内核 ----
    from logox.tools.fs_read import build as build_read_tool

    tools = ToolRegistry()
    tools.register(build_read_tool())
    # M3 的安全闸门：没有权限系统时，注册表里只允许出现只读工具（§5.6）
    if not os.environ.get("LOGOX_ALLOW_UNSAFE_TOOLS"):
        try:
            tools.assert_no_writers()
        except LogoxError as exc:
            sys.stderr.write(f"{exc}\n")
            return EXIT_NOT_READY
    else:
        sys.stderr.write("⚠ LOGOX_ALLOW_UNSAFE_TOOLS=1：权限检查已关闭，仅限开发调试。\n")

    bus = EventBus(session_id=f"chat-{os.getpid()}")
    renderer = _ChatRenderer()
    bus.subscribe("*", renderer, name="chat-renderer")

    kernel = KernelLoop(
        bus,
        provider,
        tools,
        HierarchicalContextBuilder(
            system=_system_prompt(cwd, tools),
            cwd=cwd,
            session_id=bus.session_id,
            window_capacity=provider_config.max_tokens or 128_000,
        ),
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
        # 费用估算由装配根注入（内核不认识 L5 的价格表）
        cost_estimator=lambda usage, model: estimate_cost_usd(usage, model),
    )
    state["kernel"] = kernel

    if resume_file:
        from logox.store.replay import replay_session

        replay_session(resume_file, kernel_loop=kernel, timeline=None)

    await bus.publish(
        _chat_session_start(bus.session_id, cwd, provider_config.name, model, tools.names())
    )

    interactive = sys.stdin.isatty()
    if interactive:
        sys.stderr.write(
            f"Logox {__version__} 文本模式 · {provider_config.name}/{model} · {cwd}\n"
            f"工具：{', '.join(tools.names()) or '（无）'} · Ctrl+C 第一次中断、第二次退出 · Ctrl+D 或 /exit 结束\n\n"
        )
        sys.stderr.flush()

    return await _chat_loop(kernel, renderer, interactive)


async def _chat_loop(kernel: Any, renderer: Any, interactive: bool) -> int:
    import asyncio

    while True:
        try:
            if interactive:
                sys.stderr.write("❯ ")
                sys.stderr.flush()
            line = await asyncio.to_thread(sys.stdin.readline)
        except (KeyboardInterrupt, EOFError):
            break
        if not line:  # EOF
            break

        text = line.strip()
        if not text:
            continue
        if text in ("/exit", "/quit", "/q"):
            break

        renderer.begin_turn()
        turn = await kernel.submit(text)
        renderer.end_turn(turn)
    return EXIT_OK


class _ChatRenderer:  # noqa: D101 - 内部类型，模块文档已说明其职责
    # noqa 说明：这不是公开 API，它是"一个订阅者"的最小示例——
    # 说明 M1 的事件总线确实能让界面以外的东西订阅全部事件。

    def __init__(self) -> None:
        self._need_newline = False

    def begin_turn(self) -> None:
        self._need_newline = False

    async def __call__(self, event: Any) -> None:
        from logox.kernel import events as ev

        if isinstance(event, ev.ModelDelta):
            if event.kind == "text":
                sys.stdout.write(event.delta)
                sys.stdout.flush()
                self._need_newline = True
            elif event.kind == "reasoning":
                sys.stderr.write(f"[思考] {event.delta}")
                sys.stderr.flush()
        elif isinstance(event, ev.ToolCallRequested):
            sys.stderr.write(f"\n[工具] {event.name} {event.args}\n")
            sys.stderr.flush()
        elif isinstance(event, ev.ToolCallFinished):
            mark = "✓" if event.ok else "✗"
            suffix = f" {event.error_kind}" if event.error_kind else ""
            sys.stderr.write(f"[工具] {mark} {event.duration_ms}ms{suffix}\n")
            sys.stderr.flush()
        elif isinstance(event, ev.RetryScheduled):
            sys.stderr.write(f"[重试] 第 {event.attempt} 次，{event.delay_s:.1f}s 后（{event.reason}）\n")
            sys.stderr.flush()
        elif isinstance(event, ev.ErrorOccurred):
            sys.stderr.write(f"\n[错误] {event.category}：{event.message}\n")
            sys.stderr.flush()
        elif isinstance(event, ev.TurnFinished):
            if self._need_newline:
                sys.stdout.write("\n")
                sys.stdout.flush()
                self._need_newline = False
            self._summary(event)

    def _summary(self, event: Any) -> None:
        """回合结束时给一行度量。**未上报的项显示 ``—`` 而不是 0**（D39）。"""
        usage = event.usage
        if event.reason != "completed":
            sys.stderr.write(f"[回合结束] {event.reason}（{event.duration_ms}ms）\n")
        tokens = f"{usage.input_tokens} in / {usage.output_tokens} out"
        cache = "—" if usage.cached_input_tokens is None else f"{usage.cache_hit_ratio:.0%}"
        sys.stderr.write(
            f"[用量] {tokens} · cache {cache} · 工具 {event.tool_call_count} 次 · {event.duration_ms}ms\n"
        )
        sys.stderr.flush()

    def end_turn(self, turn: Any) -> None:
        if self._need_newline:
            sys.stdout.write("\n")
            sys.stdout.flush()
            self._need_newline = False
        if turn.status.value != "done":
            sys.stderr.write(f"[回合结束] {turn.status.value}\n")
            sys.stderr.flush()


def _system_prompt(cwd: Path, tools: Any) -> str:
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


def _chat_session_start(session_id: str, cwd: Path, provider: str, model: str, tools: list[str]) -> Any:
    from logox.kernel.events import SessionStart

    return SessionStart(
        session_id=session_id,
        cwd=str(cwd),
        provider=provider,
        model=model,
        shell_backend="unknown",
        memory_sources=[],
        terminal_caps={"style": "plain", "tools": ",".join(tools)},
    )


def _configure_logging(debug: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.WARNING,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def _startup_summary(cwd: Path, paths: Any, bundle: Any) -> str:
    """启动摘要块（UI-SPEC §5.12：会话开始时必须公示环境与来源）。"""
    from logox.app import registered_tool_names
    from logox.config.loader import render_issues

    config = bundle.config
    lines = [
        f"Logox {__version__}",
        f"  工作目录   {cwd}",
        f"  用户目录   {paths.root}",
    ]

    for source in bundle.sources:
        if source.scope == "defaults":
            continue
        exists = "存在" if Path(source.path).is_file() else "不存在"
        lines.append(f"  来源·{source.scope:<7} {source.path}（{exists}）")

    model = config.provider.model or "<未指定>"
    lines.append(f"  模型       {config.provider.name} / {model}")
    lines.append(f"  思考档位   {config.provider.thinking_effort}")
    lines.append(f"  主题       {config.ui.theme}")

    # ★ 这一行必须显示**真的注册了哪些工具**，而不是配置里的愿望清单。
    # 之前它打的是 `config.tools.enabled`（默认 7 个），而实际只注册了 `read`——
    # 用户会以为 `shell` 可用，然后对着"模型为什么不用 shell"发呆。
    actual = registered_tool_names()
    lines.append(f"  实际工具   {', '.join(actual) or '（无）'}")
    configured = [name for name in config.tools.enabled if name not in actual]
    if configured:
        lines.append(
            f"  配置里还有 {', '.join(configured)} —— 但它们**尚未实现**（M5/M6），"
            "因此模型看不到、也调用不了"
        )
    lines.append(f"  状态栏     {', '.join(config.ui.status_items.enabled_keys()) or '<全关>'}")

    rendered = render_issues(bundle.issues)
    if rendered:
        lines.append("")
        lines.append(rendered)
    return "\n".join(lines)
