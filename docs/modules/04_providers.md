# 模型适配、协议与凭据

核对日期：2026-10-09。提供商适配只转译请求和信号，不判定整个用户任务是否完成。

## 职责与边界

不同服务用不同字段表示正文、推理、工具参数和结束原因。适配层（adapter layer）将差异转成统一请求与事件，使内核可以更换实现。当前实现为 OpenAI 兼容接口和 Anthropic 原生接口，包含注册、凭据解析、模型发现、静态窗口和费用估算。

登录与切换由 Runtime / TUI 处理；请求循环与恢复由 [内核](01_kernel.md) 处理。本地入梦另有受限传输，不共用前台对话或扩展工具。

## 请求与流式契约

`Provider.stream(ChatRequest)` 返回异步事件；`list_models()` 返回已知信息，`window_for(model_id)` 查询配置窗口。

| 统一事件 | 内容 |
|---|---|
| `DeltaEvent` | `kind / text`，正文或推理 |
| `ToolCallEvent` | `call_id / name / arguments / index`，完整调用 |
| `UsageEvent` | 输入、输出及已知缓存用量 |
| `StopEvent` | `stop_reason / raw_stop_reason`，统一与原始停止信号 |
| `ProviderErrorEvent` | 分类、可重试性、细节及明确截断标记 |

OpenAI 正文取 content，推理优先取非空字符串 reasoning_content，再取 reasoning；同片双字段只发送一份推理。没有合法字段不补造文本。`ChatRequest.max_tokens=None` 时请求体省略该字段；服务端自身限制仍生效。

工具参数用片段列表积累，结束边界统一拼接并解析 JSON。缺名、非法 JSON 或非对象参数产生错误，不发送半截可执行调用；工具字段语义由 Scheduler 与参数模型再校验。

`stop` 转自然结束，`length` 转输出截断，工具结束转 tool_use；Anthropic 保留对应原因，上下文限制转 context_limit。未知值不伪装自然结束。明确截断时继续传递用量及结束信号，内核决定整批拒绝与恢复。拒绝、过滤及暂停保持独立分类。

## 注册、发现与窗口

提供商预设可由 config 中同名配置覆盖。免密本地端点不要求真实 Key；缺云端密钥可用占位提供商进入 TUI，再通过登录恢复。Key 存在仅表示可解析，不代表服务器验证成功。

模型发现调用服务端列表，失败提供诊断并保留可用信息；列表可能不穷尽可调用模型。免密本地连接已有发现缓存时不混入云端预设。第三方 SDK 在实际使用时导入，轻量 help/version 不加载网络 SDK。

窗口使用静态配置，优先精确 model_windows，其次 `:latest` 规范化匹配，再按提供商规则使用 context_window。已限定云端模型的未知名称不编造窗口。切换不查询 `/api/ps / /api/show`；配置需要匹配服务实际启用容量，不能只填模型训练上限。

价格来自本地静态表，未知价格返回未知；估算不是实时账单。安装和配置步骤见 [配置指南](../user/configuration.md)。

## 本地传输边界

OpenAI 兼容适配器按实际 URL 判断本机回环：本机 HTTP/HTTPS 使用不读取环境代理的客户端；远端和局域网保留 SDK 默认代理行为。不按连接名称或是否免密推断本机，`localhost.example` 不作为 localhost。

Anamnesis 仅接受指定本机端点，关闭代理与重定向、不发送凭据、不转云端。独立请求连续无数据期限和静态窗口要求见 [入梦模块](09_anamnesis.md)。前台客户端与入梦客户端的传输规则不同，不能混用。

## 源码与验证入口

| 源码 | 责任 |
|---|---|
| [base.py](../../src/logox/providers/base.py) | Provider、ChatRequest、事件、ToolCallBuffer 和停止原因 |
| [openai_compat.py](../../src/logox/providers/openai_compat.py) | 兼容 payload、流转换、用量和本机传输 |
| [anthropic.py](../../src/logox/providers/anthropic.py) | 原生消息块、推理、用量和流转换 |
| [registry.py](../../src/logox/providers/registry.py) | ProviderSpec、预设覆盖、凭据、窗口及构造 |
| [discovery.py](../../src/logox/providers/discovery.py) | 模型目录、稳定合并和失败报告 |
| [pricing.py](../../src/logox/providers/pricing.py) | 本地价格与费用估算 |

验证分片累积、双推理字段、非法参数、原始停止原因、鉴权和本机/远端代理差异。入口：[适配器契约](../../tests/contract/)、[本地传输](../../tests/contract/test_openai_local_transport.py)、[静态窗口](../../tests/unit/test_static_model_windows.py)、[模型发现](../../tests/unit/test_provider_discovery.py)。离线回放和假端点不代替真实服务兼容性验收。
