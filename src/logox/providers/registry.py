"""Provider 的发现与实例化（D9）。

职责只有两件事：**把「配置里的一个名字」变成一个可用的适配器实例**，
以及**列举这个名字下有哪些模型**。业务逻辑一律不在这里。

为什么它不 import ``logox.config``
---------------------------------
``providers/`` 是 L5（最底层），``config/`` 属于装配侧。若在这里 import 配置模型，
就产生了"适配层反向依赖上层"的结构（ARCHITECTURE §1.2 规则 R1）。因此这里的入参是
:class:`ProviderSpec`——一个由 L2 装配根从 ``ProviderInstanceConfig`` 翻译过来的、
**只包含适配器真正需要的那几个字段**的模型。

密钥处理
--------
只从**环境变量**读取，配置文件里写明文密钥会被 ``config`` 层直接拒绝。
本模块在任何情况下都**不回显密钥内容**（错误消息里只出现变量名）。
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from logox.errors import ErrorCategory, MissingApiKeyError, UnknownProviderError
from logox.providers.anthropic import AnthropicProvider
from logox.providers.base import (
    ChatRequest,
    ModelInfo,
    Provider,
    ProviderErrorEvent,
    ProviderEvent,
    RawStream,
)
from logox.providers.openai_compat import OpenAICompatProvider
from logox.providers.pricing import PriceTable

__all__ = [
    "BUILTIN_SPECS",
    "NO_AUTH_PLACEHOLDER",
    "ProviderRegistry",
    "ProviderSpec",
    "build_provider",
    "merge_spec",
    "resolve_api_key",
]

_FROZEN = ConfigDict(frozen=True, extra="forbid")

#: 不需要鉴权的端点（Ollama / LM Studio）用的占位密钥。
#:
#: **为什么必须有它**：官方 SDK 的 ``api_key`` 参数**不能为空**——``AsyncOpenAI(api_key=None)``
#: 会回退去读 ``OPENAI_API_KEY``，读不到就直接抛错。于是"本地端点不需要密钥"这件事
#: 会变成"本地端点连不上"，而且报的是 ``auth`` 类错误，指向性极差（实测踩到）。
#: 这里传一个显式占位符：既满足 SDK 的形参要求，又不会让人误以为真的配了密钥。
NO_AUTH_PLACEHOLDER = "not-needed"


class ProviderSpec(BaseModel):
    """一个 Provider 实例的**适配器层视图**。

    字段名与 ``config.ProviderInstanceConfig`` 刻意保持一致，这样装配根可以用
    ``exclude_unset=True`` 的 dump 直接构造它，而**不必让本模块认识配置模型**。
    """

    model_config = _FROZEN

    name: str
    kind: Literal["openai_compat", "anthropic"] = "openai_compat"
    #: 空字符串 = 用适配器自带的默认端点
    base_url: str = ""
    #: 空字符串 = 该端点不需要密钥（本地 Ollama / LM Studio 就是这种情况）
    api_key_env: str = ""
    models: tuple[str, ...] = ()
    context_window: int | None = None
    #: 首次使用时默认选中的模型（留空 = 取 ``models[0]``）。
    #: 存在的理由：`models` 只保证"都在这一行"，不保证**第一个就是想要的**——
    #: 而"默认选哪个"是要按能力挑的（例如必须选多模态那个，见 deepseek 预设）。
    default_model: str = ""


#: 开箱可用的 Provider 预设。
#:
#: ``models`` 是**起步建议，不是穷举**——厂商上新速度远快于本项目发版速度，
#: 因此它只用来让 ``/model`` 在首次启动时有事可展示，用户配置里的 ``models``
#: 会合并进来。真正的"有哪些模型"应由厂商的 ``/v1/models`` 端点回答（需要联网，
#: 见 MODULE_providers 待办）。
BUILTIN_SPECS: dict[str, ProviderSpec] = {
    "openai-compatible": ProviderSpec(
        name="openai-compatible",
        kind="openai_compat",
        api_key_env="OPENAI_API_KEY",
        models=("gpt-4o", "gpt-4o-mini", "o3", "o4-mini"),
    ),
    "deepseek": ProviderSpec(
        name="deepseek",
        kind="openai_compat",
        # ⚠️ 官方是 `https://api.deepseek.com`，**没有** `/v1`（2026-09 核对官方文档）。
        # 写成 `/v1` 会让 `/models` 与 `/chat/completions` 都打错路径。
        base_url="https://api.deepseek.com",
        api_key_env="DEEPSEEK_API_KEY",
        # 取值依据：**本机真实端点实测**（2026-09，带真密钥打 `https://api.deepseek.com`）。
        #
        # 这些名字曾经是 `deepseek-chat` / `deepseek-reasoner`——那是**已经退役的
        # 旧名**，而这里的预设表当初只是"让 /model 首次启动时有东西可展示"的占位，
        # 从来没核对过。用户一眼就看出来了（D67）。
        #
        # 实测结论（**逐个发一张纯色 PNG 让它说颜色**，这比读文档硬）：
        #   * `deepseek-v4.1-flash-expires-on-0910` → 认得出颜色（**原生多模态**）；
        #   * `deepseek-flash`                       → 认得出颜色（多模态）；
        #   * `deepseek-v4-pro`                      → 思考链里明确说
        #     "图片无法查看 / [Unsupported Image]"，**不支持图片**。
        # 也就是说 `/models` 端点只列两个名字，但**别把 `/models` 当成完整清单**：
        # 用户实际在用的多模态模型不在里面，而能调通。
        #
        # 因此预设表**刻意大于 `/models` 的返回**。
        #
        # ⚠️ 顺序：**先列不会过期的稳定名**。名字里带 `expires-on-<日期>` 的模型
        # 到期后就会消失（那正是它名字的意思），把它摆在"第一个"等于给预设表埋了一颗
        # 定时炸弹。它的地位由下面的 `default_model` 表达——**默认选它，但列表按稳定性排**。
        models=(
            "deepseek-flash",
            "deepseek-v4.1-flash-expires-on-0910",
            "deepseek-v4-pro",
        ),
        # 上下文长度 **1M**（此前写的 65_536 是旧模型的数字）
        context_window=1_048_576,
        #: 默认模型用**多模态**那个：Logox 未来要能贴图（M5+），
        #: 而默认选中一个看不了图的模型会让"贴图"这条路悄悄断掉。
        default_model="deepseek-v4.1-flash-expires-on-0910",
    ),
    "ollama": ProviderSpec(
        name="ollama",
        kind="openai_compat",
        base_url="http://127.0.0.1:11434/v1",
        # 本地端点不需要密钥：留空即可，不要逼用户导出假变量
        models=("qwen3:8b", "llama3.2"),
    ),
    "lm-studio": ProviderSpec(
        name="lm-studio",
        kind="openai_compat",
        base_url="http://127.0.0.1:1234/v1",
        models=(),
    ),
    "anthropic": ProviderSpec(
        name="anthropic",
        kind="anthropic",
        api_key_env="ANTHROPIC_API_KEY",
        models=("claude-sonnet-4-5", "claude-opus-4-1", "claude-haiku-4-5"),
        context_window=200_000,
    ),
}


def merge_spec(base: ProviderSpec, override: ProviderSpec) -> ProviderSpec:
    """把用户配置**叠加**在预设之上。

    只覆盖用户在配置文件里**真的写过**的字段（``model_fields_set`` 给出这个信息），
    其余继承预设。这样 ``[providers.deepseek]`` 里只写一行 ``models`` 也不会把
    ``base_url`` 弄丢——那是最容易踩、又最难查的一类配置坑。
    """
    data = base.model_dump()
    for field in override.model_fields_set:
        data[field] = getattr(override, field)
    data["name"] = base.name  # 名字由键决定，不允许被字段值改掉
    return ProviderSpec(**data)


def resolve_api_key(spec: ProviderSpec, environ: Mapping[str, str] | None = None) -> str | None:
    """从环境变量取密钥。

    * ``api_key_env`` 为空 → ``None``，表示**该端点不需要密钥**
    * 变量缺失或只有空白 → 抛 :class:`MissingApiKeyError`（**消息里只有变量名**）

    选择"抛"而不是"返回 None 让厂商去 401"：未配密钥是最常见的首次启动问题，
    晚一步报错就要多花一轮网络往返，且错误信息来自厂商、指向性差。

    ``None`` 的语义是"无需鉴权"，**不是**"忘了配"——后一种情况上面已经抛了。
    """
    if not spec.api_key_env:
        return None
    source = os.environ if environ is None else environ
    value = (source.get(spec.api_key_env) or "").strip()
    if not value:
        raise MissingApiKeyError(spec.name, spec.api_key_env)
    return value


class MissingKeyProvider:
    """**还没登录**时的占位适配器：一被调用就返回一条可行动的错误事件。

    它存在的唯一理由是让"首次使用"能走通。没有它的话，`logox` 会在启动期
    因为缺密钥而拒绝服务，于是**用户根本没有机会执行 `/login`**——
    先有鸡还是先有蛋（实测踩到）。

    它的行为特征（三条都很重要）：

    * ``list_models()`` **照常工作**（读本地预设表），因此 `/model` 在登录前也能列候选；
    * ``stream()`` 不发网络请求，立刻产出一条 ``ProviderErrorEvent(category=AUTH)``，
      消息里给出**环境变量名**与"运行 /login"这个下一步；
    * 它**不是**静默失败：混进来的调用一定会在界面上留下一条错误，不会被当成"模型没说话"。
    """

    def __init__(self, spec: ProviderSpec) -> None:
        self._spec = spec
        self.name = spec.name
        #: 供界面判断"当前还没登录"（装配根与 `/debug` 用它给提示）
        self.is_placeholder = True
        self.missing_env = spec.api_key_env

    def list_models(self) -> list[ModelInfo]:
        return [
            ModelInfo(id=model_id, provider=self.name, supports_thinking=False, context_window=self._spec.context_window)
            for model_id in self._spec.models
        ]

    def stream(self, request: ChatRequest) -> AsyncIterator[ProviderEvent]:  # noqa: ARG002 - 签名对齐协议
        env_name = self._spec.api_key_env or "<未配置 api_key_env>"

        async def generator() -> AsyncIterator[ProviderEvent]:
            yield ProviderErrorEvent(
                category=ErrorCategory.AUTH,
                message=(
                    f"还没有配置 {self.name} 的 API Key（环境变量 {env_name}）。"
                    "在 Logox 里运行 /login 选择供应商并粘贴密钥即可。"
                ),
                detail="（这条错误来自占位适配器：装配期缺密钥时不再拒绝启动，避免「无法登录」的死锁）",
            )

        return generator()


def build_provider(
    spec: ProviderSpec,
    *,
    api_key: str | None = None,
    raw_stream: RawStream | None = None,
    price_table: PriceTable | None = None,
) -> Provider:
    """按 ``kind`` 构造适配器实例。**这里是唯一知道"有哪些适配器"的地方。**

    两个适配器的构造签名刻意保持一致，因此这里不需要 if/else 去凑参数
    （只按 ``kind`` 选类）。
    """
    adapter = AnthropicProvider if spec.kind == "anthropic" else OpenAICompatProvider
    return adapter(
        api_key=api_key,
        base_url=spec.base_url,
        raw_stream=raw_stream,
        price_table=price_table,
        context_window=spec.context_window,
        models=list(spec.models),
    )


class ProviderRegistry:
    """一组已解析好的 Provider 实例。由 L2 装配根持有。"""

    def __init__(
        self,
        specs: Mapping[str, ProviderSpec],
        *,
        environ: Mapping[str, str] | None = None,
        price_table: PriceTable | None = None,
    ) -> None:
        self._specs = dict(specs)
        self._environ = environ
        self.price_table = price_table

    # ------------------------------------------------------------------ #
    # 构造
    # ------------------------------------------------------------------ #

    @classmethod
    def with_builtins(cls, overrides: Mapping[str, Any] | None = None, **kwargs: Any) -> ProviderRegistry:
        """内置预设 + 用户配置。用户配置里的新名字会被添加，同名则叠加。"""
        specs = dict(BUILTIN_SPECS)
        for name, raw in (overrides or {}).items():
            override = raw if isinstance(raw, ProviderSpec) else ProviderSpec(name=name, **raw)
            specs[name] = merge_spec(specs[name], override) if name in specs else override
        return cls(specs, **kwargs)

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #

    def names(self) -> list[str]:
        return sorted(self._specs)

    def spec(self, name: str) -> ProviderSpec:
        try:
            return self._specs[name]
        except KeyError:
            raise UnknownProviderError(name, self.names()) from None

    def default_model(self, name: str) -> str | None:
        """该 Provider 的首选模型（供 ``config.provider.model`` 为空时兜底）。

        优先用预设里显式声明的 ``default_model``——"列表里的第一个"只是排版顺序，
        而"默认选哪个"通常是按**能力**挑的（见 deepseek 预设为什么选多模态那个）。
        """
        spec = self.spec(name)
        if spec.default_model:
            return spec.default_model
        return spec.models[0] if spec.models else None

    # ------------------------------------------------------------------ #
    # 实例化与列举
    # ------------------------------------------------------------------ #

    def build(
        self,
        name: str,
        *,
        raw_stream: RawStream | None = None,
        api_key: str | None = None,
        allow_missing_key: bool = False,
    ) -> Provider:
        """构造适配器。``api_key`` 显式传入时不读环境变量（测试用）。

        :param allow_missing_key: **缺密钥时不要抛异常**，改成一个"占位适配器"——
            它一被调用就返回一条**可行动**的错误事件（"请运行 /login"）。
            这是首次使用能走通的前提：没有它，`logox` 会在启动期就拒绝服务，
            **用户根本没机会执行 `/login`**（实测踩到的先有鸡还是先有蛋）。
        """
        spec = self.spec(name)
        try:
            key = api_key if api_key is not None else resolve_api_key(spec, self._environ)
        except MissingApiKeyError:
            if not allow_missing_key:
                raise
            return MissingKeyProvider(spec)  # type: ignore[return-value]
        if key is None:
            # 无需鉴权的端点（见 NO_AUTH_PLACEHOLDER）：SDK 要求 api_key 非空，
            # 但服务端根本不校验它。不换成占位符的话，Ollama / LM Studio
            # 会以一个 auth 错误启动失败——而它们的配置完全正确。
            key = NO_AUTH_PLACEHOLDER
        return build_provider(spec, api_key=key, raw_stream=raw_stream, price_table=self.price_table)

    def list_models(self, name: str) -> list[ModelInfo]:
        """该 Provider 的模型信息（含价格与上下文窗口）。

        **不发网络请求**：模型来自预设 + 用户配置。未知模型的价格是 ``None``，
        状态栏据此显示 ``—`` 而不是一个假数字。
        """
        spec = self.spec(name)
        adapter = build_provider(spec, api_key="unused", price_table=self.price_table)
        return adapter.list_models()

    def all_models(self) -> dict[str, list[ModelInfo]]:
        """全部 Provider 的模型，按名字排序（供 ``/model`` 选择器使用）。"""
        return {name: self.list_models(name) for name in self.names()}

    def model_ids(self) -> list[tuple[str, str]]:
        """``[(provider_name, model_id)]`` 扁平列表，选择器直接可用。"""
        return [(name, info.id) for name, infos in self.all_models().items() for info in infos]
