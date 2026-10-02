"""从**厂商端点**抓取真实可用的模型列表（D65）。

它解决什么问题
==============

在此之前，`/model` 的候选来自**本地预设表**（`providers/registry.py::BUILTIN_SPECS`）。
那张表的问题是它**一定会过期**：厂商上新速度远快于本项目发版速度，而用户为某个
新模型付了钱却在自己的工具里选不到它——这会让人怀疑"这工具是不是坏的"。

本模块做的事就一件：**拿用户的密钥去问端点"你有哪些模型"**，然后把答案拿回来。

三个设计选择
============

**① 它不是 ``Provider`` 协议的一部分。** 协议上只有 ``list_models()``（**同步、只读本地**），
因为"列出模型"必须能在离线、无密钥的前提下工作（`/model` 弹窗不该因为网断了就打不开）。
抓取是**额外的、可失败的、异步的**能力，因此单独一个函数，而不是塞进协议。

**② 用鸭子类型调 SDK，不 import 厂商类型。** 官方 SDK 的 `models.list()` 在
OpenAI 与 Anthropic 上**形状不同**（OpenAI 返回嵌套的 `{id, object, ...}`，
Anthropic 返回 `{id, display_name, created_at}`），因此这里按字段存在性取值，
而不是按类判断——这样将来接第三个厂商时**不用改这里**（D9 的"厂商细节不外泄"）。

**③ 失败必须是无害的。** 网络慢、密钥无效、端点不支持 `/models`（部分兼容实现没有）
——统统只是"没抓到"，回退到预设表即可。它**绝不能**让 `/login` 或启动失败。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from logox.providers.base import ModelInfo, Provider

__all__ = ["DEFAULT_TIMEOUT_S", "LOCAL_TIMEOUT_S", "DiscoveryResult", "discover_models", "merge_models"]

#: 抓取超时。**故意比请求超时短得多**：这是登录流程里的一步，
#: 用户站在那儿等着；抓不到就用预设表，不值得让他等 2 分钟。
DEFAULT_TIMEOUT_S = 12.0

#: **本地（免鉴权）端点**的抓取超时（D188）。
#:
#: 比 `DEFAULT_TIMEOUT_S` 短得多，因为两类端点的"慢"含义完全不同：
#: 云端慢 = 网络抖动，值得等；本机慢 = **服务根本没在跑**，等下去只是白等
#: （连接被拒在毫秒级就返回了，真等满 3 秒说明它确实不可达）。
LOCAL_TIMEOUT_S = 3.0


@dataclass
class DiscoveryResult:
    """抓取结果。**成功与失败都走这个对象**，不抛异常。"""

    models: list[str] = field(default_factory=list)
    #: 失败原因（给人看的一句话）。成功时为空。
    error: str = ""
    #: 是否真的问了端点（`False` = 该适配器没有 SDK 客户端，比如测试用的假 provider）
    attempted: bool = False

    @property
    def ok(self) -> bool:
        return bool(self.models) and not self.error

    def summary(self) -> str:
        """一行摘要（给界面用）。"""
        if self.ok:
            return f"抓到 {len(self.models)} 个模型"
        if not self.attempted:
            return "该适配器不支持抓取（用本地预设表）"
        return f"未能抓取（{self.error}）——已回退到本地预设表"


def _model_id_of(item: Any) -> str:
    """从一项里取 id（对象或 dict 都认）。取不到返回空串。"""
    model_id = getattr(item, "id", None)
    if model_id is None and isinstance(item, dict):
        model_id = item.get("id")
    return model_id.strip() if isinstance(model_id, str) else ""


def _ids_from_page(page: Any) -> list[str]:
    """从"一页"里取 id 列表（``data`` 字段）。"""
    items = getattr(page, "data", None)
    if items is None and isinstance(page, dict):
        items = page.get("data")
    if not isinstance(items, list):
        return []
    return [model_id for model_id in (_model_id_of(item) for item in items) if model_id]


async def _collect_ids(result: Any, *, max_models: int) -> list[str]:
    """把 ``models.list()`` 的返回值统一取成一份 id 列表。

    ⚠️ **这里踩过一次真实的坑，值得记住**：官方 SDK 的 ``AsyncModels.list()``
    **不是协程函数**（``asyncio.iscoroutinefunction`` 为 False），它返回一个
    ``AsyncPaginator``——**异步可迭代，但顶层没有 ``.data``**。
    最初的实现把"不是协程"当成"已经是一页了"，于是直接去读 ``.data``：
    拿到空列表，然后报告"端点返回了空列表"——**把一个完全正常的端点报成没有模型**。

    而当时**所有**用替身的用例都发现不了它（替身返回的普通对象，`.data` 就在顶层）。
    抓住它靠的是真起一个 HTTP 服务器让 SDK 真的去请求
    （``tests/contract/test_model_discovery_live.py``）：
    **替身测试只能证明"给定一页结果我能解析"，证明不了"我问对了地方、取对了形状"。**

    因此这里按**从具体到宽松**的顺序试四种形状：异步可迭代（SDK 的实际形状）
    → 同步列表 → 带 ``.data`` 的一页 → ``.get_next_page()``。
    """
    # ① SDK 的实际形状：异步分页器（协议文档也是这么写的）
    if hasattr(result, "__aiter__"):
        ids: list[str] = []
        async for item in result:
            model_id = _model_id_of(item)
            if model_id:
                ids.append(model_id)
            if len(ids) >= max_models:
                break
        if ids:
            return ids

    # ② 同步列表
    if isinstance(result, list):
        ids = [model_id for model_id in (_model_id_of(item) for item in result) if model_id]
        if ids:
            return ids

    # ③ 已经是一页（带 .data）
    ids = _ids_from_page(result)
    if ids:
        return ids

    # ④ 分页对象：主动取下一页
    get_next = getattr(result, "get_next_page", None)
    if callable(get_next):
        try:
            page = get_next()
            if asyncio.iscoroutine(page):
                page = await page
        except Exception:  # pragma: no cover - 取下一页失败就当没有
            return []
        return _ids_from_page(page)
    return []


async def discover_models(
    provider: Provider,
    *,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    max_models: int = 500,
) -> DiscoveryResult:
    """向端点要一次模型列表。

    **任何异常都被转成 ``DiscoveryResult(error=...)``**，绝不向上抛——
    调用方（`/login`）永远可以安全地"抓到就用、抓不到就回退"。

    只翻**一页**：大多数兼容端点的 `/models` 一次给全（OpenAI 的 `has_more`
    极少为真，DeepSeek / Ollama 甚至不分页），而翻页会把超时风险乘以页数。
    """
    client_getter = getattr(provider, "_ensure_client", None)
    if not callable(client_getter):
        # 假 provider（测试用）或占位适配器：没有 SDK 客户端，也就无从抓取
        return DiscoveryResult(attempted=False)

    try:
        client = client_getter()
    except Exception as exc:  # 缺密钥 / SDK 装不上
        return DiscoveryResult(error=f"{type(exc).__name__}: {exc}", attempted=True)

    models_api = getattr(client, "models", None)
    lister = getattr(models_api, "list", None)
    if not callable(lister):
        return DiscoveryResult(error="该 SDK 客户端没有 models.list()", attempted=True)

    async def _call() -> Any:
        result = lister()
        # SDK 的 models.list() 可能是协程，也可能直接返回一个分页对象
        if asyncio.iscoroutine(result):
            return await result
        return result

    try:
        page = await asyncio.wait_for(_call(), timeout=timeout_s)
        ids = _stable_unique(
            await asyncio.wait_for(_collect_ids(page, max_models=max_models), timeout=timeout_s)
        )
    except TimeoutError:
        return DiscoveryResult(error=f"超时（{timeout_s:.0f}s）", attempted=True)
    except asyncio.CancelledError:
        # 用户在抓取过程中按了 Esc：**原样传播**（E-5，与内核同一条规矩）
        raise
    except Exception as exc:  # noqa: BLE001 - 任何网络/鉴权/协议错误都只是"没抓到"
        return DiscoveryResult(error=_friendly(exc), attempted=True)

    if not ids:
        return DiscoveryResult(error="端点没有返回任何模型", attempted=True)
    return DiscoveryResult(models=ids[:max_models], attempted=True)


def _stable_unique(items: list[str]) -> list[str]:
    """去重并**保持端点给出的顺序**。

    **刻意不排序**：厂商的 `/models` 顺序本身带信息（OpenAI 把新模型排前面），
    而 `/model` 弹窗的第一屏就是用户最先看到的内容。按字母序排会把这个信息抹掉，
    换来一个谁也没要求的"整齐"。
    """
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _friendly(exc: BaseException) -> str:
    """把 SDK 异常压成一句人话。**不泄露密钥**（SDK 的报错里偶尔会带上它）。"""
    text = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
    return text[:160]


def merge_models(discovered: list[str], preset: list[str]) -> list[str]:
    """把抓到的模型与本地预设合并。

    **抓到的排前面**（那是这个账号真实可用的），预设里没被抓到的补在后面
    （某些兼容端点不实现 `/models`，或者抓取时刚好网络抖动）。
    去重且保序——顺序会影响 `/model` 弹窗的第一屏，而第一项往往就是用户要选的。
    """
    seen: set[str] = set()
    merged: list[str] = []
    for model_id in [*discovered, *preset]:
        if model_id and model_id not in seen:
            seen.add(model_id)
            merged.append(model_id)
    return merged


def as_model_infos(ids: list[str], provider: Provider) -> list[ModelInfo]:
    """把 id 列表转成 ``ModelInfo``（带上 provider 已知的思考能力与上下文窗口）。

    抓到的模型**没有**价格与上下文窗口信息（`/models` 不返回这些），
    因此状态栏对它们会显示 `—` 而不是编一个数字（D39 的口径）。
    """
    supports = getattr(provider, "supports_thinking", None)
    window = getattr(provider, "context_window", None)
    return [
        ModelInfo(
            id=model_id,
            provider=getattr(provider, "name", ""),
            supports_thinking=bool(supports(model_id)) if callable(supports) else False,
            context_window=window if isinstance(window, int) else None,
        )
        for model_id in ids
    ]
