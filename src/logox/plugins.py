"""Pi 风格轻量单入口插件系统（M10 / D3 / D111）。

设计原则：
1. 单入口纯函数契约：插件仅需暴露 `register(ctx: PluginContext)`，学习成本为零；
2. 门面沙箱隔离（Facade Pattern）：`PluginContext` 仅提供安全的注册接口，屏蔽内核底层私有细节；
3. 异常就地隔离（Exception Quarantine）：任一插件语法错误或执行异常被捕获隔离，绝不影响其他插件或主程序。
"""

from __future__ import annotations

import importlib.util
import logging
import traceback
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

from logox.config.schema import PluginsConfig
from logox.kernel.bus import EventBus
from logox.kernel.events import Event
from logox.kernel.registry import ToolRegistry
from logox.tools.base import Tool

logger = logging.getLogger("logox.plugins")


class PluginContext:
    """供第三方插件使用的安全门面上下文。"""

    def __init__(
        self,
        tool_registry: ToolRegistry,
        bus: EventBus,
        commands_registry: dict[str, tuple[Callable[..., Any], str]] | None = None,
    ) -> None:
        self._tool_registry = tool_registry
        self._bus = bus
        self._custom_commands = commands_registry if commands_registry is not None else {}

    def register_tool(self, tool: Tool) -> None:
        """注册第三方自定义工具到系统工具注册表。"""
        self._tool_registry.register(tool)
        logger.info("插件注册了新工具: %s", tool.name)

    def subscribe(
        self,
        event_type: type[Event],
        handler: Callable[[Any], Coroutine[Any, Any, None]],
        *,
        name: str | None = None,
    ) -> None:
        """向事件总线订阅感兴趣的内核或系统事件。"""
        sub_name = name or getattr(handler, "__name__", "plugin_subscriber")
        self._bus.subscribe(event_type, handler, name=sub_name)
        logger.debug("插件订阅了事件: %s (name=%s)", event_type.__name__, sub_name)


    def register_slash_command(
        self,
        name: str,
        handler: Callable[..., Any],
        description: str = "",
    ) -> None:
        """注册自定义斜杠命令。"""
        clean_name = name.lstrip("/")
        self._custom_commands[clean_name] = (handler, description)
        logger.info("插件注册了新命令: /%s (%s)", clean_name, description)

    def get_logger(self, name: str) -> logging.Logger:
        """获取命名规范的插件专属日志记录器。"""
        return logging.getLogger(f"logox.plugin.{name}")


class PluginManager:
    """插件管理器：负责多源扫描、动态加载与异常隔离。"""

    def __init__(
        self,
        config: PluginsConfig,
        cwd: Path | str,
        user_dir: Path | str | None = None,
    ) -> None:
        self.config = config
        self.cwd = Path(cwd).resolve()
        self.user_dir = Path(user_dir).resolve() if user_dir else Path.home() / ".logox"
        self.loaded_plugins: dict[str, Any] = {}
        self.failed_plugins: dict[str, str] = {}
        self.custom_commands: dict[str, tuple[Callable[..., Any], str]] = {}

    def discover_plugin_files(self) -> list[Path]:
        """按优先级发现所有插件脚本文件。

        优先级：
        1. config.plugins.paths 中的显式文件/目录；
        2. <cwd>/.logox/plugins/*.py（项目级）；
        3. ~/.logox/plugins/*.py（用户全局级）。
        """
        if not self.config.enabled:
            return []

        discovered: list[Path] = []
        seen_names: set[str] = set()


        def _add_file(path: Path) -> None:
            if (
                path.is_file()
                and path.suffix == ".py"
                and not path.name.startswith("_")
                and path.name not in seen_names
            ):
                discovered.append(path)
                seen_names.add(path.name)

        # 1. 配置中的显式路径
        for p_str in self.config.paths:
            p = Path(p_str)
            if not p.is_absolute():
                p = (self.cwd / p).resolve()
            if p.is_file():
                _add_file(p)
            elif p.is_dir():
                for sub in sorted(p.glob("*.py")):
                    _add_file(sub)

        # 2. 项目级插件目录
        project_dir = self.cwd / ".logox" / "plugins"
        if project_dir.is_dir():
            for f in sorted(project_dir.glob("*.py")):
                _add_file(f)

        # 3. 用户全局插件目录
        user_plugins_dir = self.user_dir / "plugins"
        if user_plugins_dir.is_dir():
            for f in sorted(user_plugins_dir.glob("*.py")):
                _add_file(f)

        return discovered

    def load_all(self, ctx: PluginContext) -> int:
        """加载所有被发现的插件并注入上下文。"""
        if not self.config.enabled:
            logger.info("插件系统已禁用 (plugins.enabled=false)")
            return 0

        files = self.discover_plugin_files()
        loaded_count = 0

        for file_path in files:
            plugin_name = file_path.stem
            try:
                spec = importlib.util.spec_from_file_location(
                    f"logox_plugin_{plugin_name}",
                    file_path,
                )
                if not spec or not spec.loader:
                    raise ImportError(f"无法为插件创建加载器: {file_path}")

                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)

                # 检查 Pi 风格单一契约 register(ctx)
                if hasattr(module, "register") and callable(module.register):
                    module.register(ctx)
                    self.loaded_plugins[plugin_name] = module
                    loaded_count += 1
                    logger.info("成功加载插件: %s (%s)", plugin_name, file_path)
                else:
                    msg = f"插件 {plugin_name} 未定义 register(ctx) 函数"
                    self.failed_plugins[plugin_name] = msg
                    logger.warning(msg)

            except Exception as exc:
                err_msg = f"{exc}\n{traceback.format_exc()}"
                self.failed_plugins[plugin_name] = err_msg
                logger.error("加载插件 %s 失败，已执行隔离: %s", plugin_name, exc)

        return loaded_count

