"""工具协议——内核与所有工具之间的**唯一契约**（ARCHITECTURE §4.2）。

放在 L4 但只含**协议与纯数据**，因此 L3 的 ``kernel/`` 可以引用它而不违反 R2
（内核"只能依赖 Protocol 与 pydantic 模型"——这里正是）。具体工具
（``fs_read.py`` 等）**绝不能被内核 import**，否则内核将无法脱离工具集测试。

两条贯穿全文件的分隔
--------------------
1. **``ToolResult.content`` 与 ``ToolResult.display`` 严格分开。**
   ``content`` 是**给模型的**（唯一进入上下文的部分），``display`` 是**给人的**。
   把 diff 的彩色文本塞进 ``content``，模型会收到一堆 ANSI 转义与 ``+``/``-`` 噪声；
   把给模型的行号塞进 ``display``，界面就会显示得一塌糊涂。二者混过一次就再也拆不开。
2. **失败也是一种结果，不是异常。** 工具失败要回灌给模型自愈（D22 的
   ``feedable_to_model``），所以它必须表达成 ``ToolResult(ok=False, error=...)``
   而不是抛出去——抛出去就等于丢掉了"让模型自己改正"的机会。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from logox.errors import ErrorCategory
from logox.kernel.events import ChangeStat
from logox.kernel.messages import ToolSchema

__all__ = [
    "DisplayHint",
    "Tool",
    "ToolArgs",
    "ToolContext",
    "ToolError",
    "ToolResult",
    "ToolSpec",
    "schema_from_model",
]

_FROZEN = ConfigDict(frozen=True, extra="forbid")


class ToolArgs(BaseModel):
    """**所有工具参数模型的基类**：未知字段一律报错。

    为什么不用 pydantic 的默认行为（静默忽略未知字段）：模型给工具塞错字段名是常见的
    一类错误（``file`` 而不是 ``path``），静默忽略会让工具"成功地"按默认值跑一遍，
    然后返回一个**看似正常但完全不对**的结果。而报错会把字段路径回灌给模型，
    它下一轮就能改对（D22 的 ``feedable_to_model``）。

    与项目其余模型（配置、事件、消息）的 ``extra="forbid"`` 保持一致：拼错的字段名
    必须当场暴露，不能等到用户发现结果不对。
    """

    model_config = ConfigDict(extra="forbid")


def schema_from_model(params: type[BaseModel]) -> dict[str, Any]:
    """由 pydantic 模型生成 JSON Schema（D26）。

    单独抽一个函数是为了让 schema 的生成方式**只有一处**——将来若要剥掉
    ``title`` 之类的冗余键、或统一 ``additionalProperties``，改这里就够了。
    """
    schema = params.model_json_schema()
    # ``title`` 是 pydantic 给模型类加的，对模型理解参数没有帮助，只会白占 token
    schema.pop("title", None)
    return schema


class _EmptyParams(ToolArgs):
    """无参数工具的占位参数模型（比 ``None`` 少一层分支）。"""


class ToolSpec(BaseModel):
    """工具的线级描述。**内核与 Provider 只需要这些信息**，不需要知道它怎么执行。"""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    name: str
    description: str = ""
    #: 参数模型；JSON Schema 由它生成（D26），不手写
    params: type[BaseModel] = _EmptyParams
    #: D27 并发分类的**唯一依据**：同批全只读才并发
    readonly: bool = False
    #: 是否需要经过权限决策（只读工具通常为 False）
    requires_permission: bool = True
    #: D13 折叠卡片的一行摘要模板，如 ``"读取 {path}"``
    summary_template: str = ""
    source: Literal["builtin", "mcp", "plugin"] = "builtin"

    def schema(self) -> ToolSchema:
        """转成 Provider 需要的线级描述。"""
        return ToolSchema(name=self.name, description=self.description, parameters=schema_from_model(self.params))

    def summary(self, args: dict[str, Any]) -> str:
        """按模板渲染一行摘要；模板缺字段时**退回工具名**而不是抛异常。"""
        if not self.summary_template:
            return self.name
        try:
            return self.summary_template.format(**args)
        except (KeyError, IndexError, ValueError):
            return self.name


class ToolError(BaseModel):
    """分类后的工具错误。

    ``category`` 复用 D22 的 :class:`ErrorCategory`——它同时决定
    **可否回灌模型自愈**（``feedable_to_model``），所以不能是自由字符串。
    """

    model_config = _FROZEN

    category: ErrorCategory
    message: str
    detail: str | None = None

    def render(self) -> str:
        """回灌给模型的文本。**刻意简短**——错误细节进 ``detail``（给人看）。"""
        if self.detail:
            return f"{self.message}\n{self.detail}"
        return self.message


class DisplayHint(BaseModel):
    """给界面的渲染提示。**纯数据**（R5 无裸 dict），不在 L4 产生任何渲染代码。

    这样界面才能被替换掉（D3）——工具只说"这是一段 diff"，怎么说由 ``tui/`` 决定。
    """

    model_config = _FROZEN

    kind: Literal["text", "diff", "lines", "table", "error"]
    payload: dict[str, Any] = Field(default_factory=dict)


class ToolContext(BaseModel):
    """工具执行时能拿到的环境。

    **刻意很窄**：工具拿不到事件总线、拿不到配置、拿不到内核——它只能读 cwd 与
    查询取消状态。工具一旦能发事件，内核的事件序列就不再是单一来源了。
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    cwd: Path
    #: 查询"是否已被要求取消"。**工具不得依赖它保证正确性**（取消由 task 完成），
    #: 它只用于长循环里尽早退出、少做无用功。
    is_cancelled: Callable[[], bool] = lambda: False


class ToolResult(BaseModel):
    """工具执行结果。**失败也走这里**，不抛异常（见模块文档第 2 条）。"""

    model_config = _FROZEN

    ok: bool
    #: 回灌给模型的文本——**唯一**进入上下文的部分
    content: str = ""
    #: 只给界面看，不进上下文
    display: DisplayHint | None = None
    #: 失败时应有；成功时为 ``None``
    error: ToolError | None = None
    #: 变更统计（D40/M5）：用于驱动 TUI 卡片徽标与 diff 展示
    change_stat: ChangeStat | None = None

    @property
    def digest(self) -> str:
        """结果摘要（给 ``ToolCallFinished.result_digest``）——**不存全文**。

        手机上翻事件日志时，一行摘要比几百行文件内容有用得多。
        """
        prefix = self.content.strip()[:81]
        first = prefix.splitlines()[0] if prefix else ""
        if len(first) > 80:
            first = first[:77] + "…"
        return first

    @classmethod
    def failure(
        cls,
        category: ErrorCategory,
        message: str,
        *,
        detail: str | None = None,
        content: str | None = None,
    ) -> ToolResult:
        """构造一个失败结果。

        ``content`` 默认与错误消息同文——因为**模型也需要知道失败了什么**，
        否则它会以为工具成功返回了空内容，然后基于空数据继续推理。
        """
        error = ToolError(category=category, message=message, detail=detail)
        return cls(ok=False, content=error.render() if content is None else content, error=error)


@runtime_checkable
class Tool(Protocol):
    """工具的统一接口。

    ``runtime_checkable`` 让注册表能做 ``isinstance`` 检查——注册一个不符合协议的对象
    必须在**装配期**就报错，而不是等模型第一次调用它。
    """

    spec: ToolSpec

    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult: ...
