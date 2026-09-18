"""工具层（L4）。

``base.py`` 里的 ``Tool`` 协议与 ``ToolResult`` 等纯数据模型是 **L3 内核唯一允许
引用的部分**（R2：内核只能依赖 Protocol 与 pydantic 模型）。具体工具文件
（``fs_read.py`` 等）绝不能被 ``kernel/`` 引用——否则内核再也无法脱离工具集测试。

工具的三个不变式
----------------
1. **无副作用声明必须诚实**：``ToolSpec.readonly`` 是 D27 并发决策的**唯一依据**，
   标错了会让写操作并发执行、互相覆盖。
2. **失败返回结果，不抛异常**：失败要回灌给模型自愈（D22）。
3. **不做权限判断**：工具的职责是执行，放不放行由 ``permissions/`` 决定。
"""

from __future__ import annotations

from logox.tools.base import (
    ChangeStat,
    DisplayHint,
    Tool,
    ToolArgs,
    ToolContext,
    ToolError,
    ToolResult,
    ToolSpec,
)
from logox.tools.fs_edit import EditArgs, EditTool
from logox.tools.fs_glob import GlobArgs, GlobTool
from logox.tools.fs_grep import GrepArgs, GrepTool
from logox.tools.fs_read import ReadArgs, ReadTool
from logox.tools.fs_write import WriteArgs, WriteTool
from logox.tools.shell import ShellArgs, ShellBackend, ShellTool

__all__ = [
    "ChangeStat",
    "DisplayHint",
    "EditArgs",
    "EditTool",
    "GlobArgs",
    "GlobTool",
    "GrepArgs",
    "GrepTool",
    "ReadArgs",
    "ReadTool",
    "ShellArgs",
    "ShellBackend",
    "ShellTool",
    "Tool",
    "ToolArgs",
    "ToolContext",
    "ToolError",
    "ToolResult",
    "ToolSpec",
    "WriteArgs",
    "WriteTool",
]
