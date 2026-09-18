"""工具注册表（L3）。

名字 → ``Tool`` 的查找与 schema 列举。**内核部署工具，但不生产工具**：
具体工具由 L2 装配根注册进来（``registry.register(read_tool())``），内核因此对
"有哪些工具、工具怎么实现"完全无知——这正是 R2 要的东西。

为什么重名必须报错而不是覆盖
----------------------------
插件、MCP 与内置工具最后都汇到这一张表。若静默覆盖，一个同名插件会让内置工具
"凭空消失"，而症状是模型忽然不会用某个工具了——极难排查。宁可在装配期炸掉。
"""

from __future__ import annotations

from logox.errors import LogoxError
from logox.kernel.messages import ToolSchema
from logox.tools.base import Tool, ToolSpec

__all__ = ["InvalidToolError", "ToolNameConflictError", "ToolRegistry", "UnsafeToolError"]


class InvalidToolError(LogoxError):
    """注册了不符合 :class:`Tool` 协议的对象，或工具没有名字。"""

    def __init__(self, name: str, reason: str) -> None:
        super().__init__(f"工具 {name!r} 不符合 Tool 协议：{reason}")


class ToolNameConflictError(LogoxError):
    """重名注册。**绝不静默覆盖**——症状（"模型忽然不会用某个工具了"）极难排查。"""

    def __init__(self, name: str) -> None:
        super().__init__(f"工具 {name!r} 已经注册过了；请改名，或先注销旧的那个（不自动覆盖）")


class UnsafeToolError(LogoxError):
    """在**没有权限系统**的情况下注册了非只读工具（M3 的安全闸门，§5.6）。

    这是刻意"过度严格"的一道闸：M3 的决策器恒为放行一切，若此时表里出现写工具，
    模型就可以在无人确认的情况下改文件。宁可启动即失败，也不要静默地改用户的东西。
    """

    def __init__(self, writers: list[str], all_names: list[str]) -> None:
        self.writers = tuple(writers)
        available = "、".join(sorted(all_names)) if all_names else "（无）"
        super().__init__(
            f"当前权限决策器会放行一切，但注册表里有非只读工具：{'、'.join(sorted(writers))}。"
            f"已注册的全部工具：{available}。"
            "M6 接入真实权限系统前请只注册只读工具；"
            "确需调试可设置环境变量 LOGOX_ALLOW_UNSAFE_TOOLS=1（会打印醒目警告）。"
        )


class ToolRegistry:
    """工具注册表。**无状态以外的任何逻辑**——不做权限、不做执行、不记日志。"""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    # ------------------------------------------------------------------ #
    # 注册与查找
    # ------------------------------------------------------------------ #

    def register(self, tool: Tool) -> None:
        """注册一个工具。重名或不符合协议 → 立刻报错（装配期失败优于运行期诡异）。"""
        name = getattr(getattr(tool, "spec", None), "name", "")
        if not name:
            raise InvalidToolError("<未命名>", "spec.name 为空")
        if not isinstance(tool, Tool):
            raise InvalidToolError(name, "需要 spec 属性与 async run 方法")
        if name in self._tools:
            raise ToolNameConflictError(name)
        self._tools[name] = tool

    def register_all(self, tools: list[Tool]) -> None:
        for tool in tools:
            self.register(tool)

    def get(self, name: str) -> Tool | None:
        """按名查找。**找不到返回 ``None``**——由循环转成可自愈的结果，而不是抛异常。

        模型幻觉出一个不存在的工具是最常见的一类模型错误；用异常打断会话体验很差，
        而回灌"没有这个工具，可用的是……"能让模型自己改对（D22 的 ``feedable_to_model``）。
        """
        return self._tools.get(name)

    def spec(self, name: str) -> ToolSpec | None:
        tool = self._tools.get(name)
        return None if tool is None else tool.spec

    def names(self) -> list[str]:
        return sorted(self._tools)

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    # ------------------------------------------------------------------ #
    # 交给 Provider 与调度器的东西
    # ------------------------------------------------------------------ #

    def schemas(self) -> list[ToolSchema]:
        """全部工具的线级描述，交给 Provider（**顺序稳定**，便于快照对比）。"""
        return [self._tools[name].spec.schema() for name in self.names()]

    def is_readonly(self, name: str) -> bool:
        """该工具是否只读（D27）。

        **未注册的工具保守视为"写"**：MCP 工具可能没声明只读注解，
        把未知当成可并发等于赌它没有副作用——赌输的代价是数据竞争。
        """
        tool = self._tools.get(name)
        return bool(tool is not None and tool.spec.readonly)

    # ------------------------------------------------------------------ #
    # 安全闸门
    # ------------------------------------------------------------------ #

    def writers(self) -> list[str]:
        """所有**非只读**工具的名字。"""
        return sorted(name for name, tool in self._tools.items() if not tool.spec.readonly)

    def assert_no_writers(self) -> None:
        """M3 的安全闸门：没有权限系统时不允许存在写工具。

        详见 ``MODULE_kernel_loop.md`` §5.6。任何人在 M6 之前给 ``--chat`` 注册一个
        写工具都会**立刻失败**，而不是某天静默改掉用户的文件。
        """
        writers = self.writers()
        if writers:
            raise UnsafeToolError(writers, self.names())
