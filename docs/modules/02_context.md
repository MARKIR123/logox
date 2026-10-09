# 上下文、计量与压缩

核对日期：2026-10-09。原始会话用于追溯，有效上下文用于模型请求；二者分开维护。

## 职责与边界

上下文窗口（context window）有限，完整工具输出和长期历史可能让请求超预算。本模块组装系统规则与消息、估算输入规模、归档工具结果、折叠历史并保存可恢复的有效视图。模型发现属于 [模型适配](04_providers.md)，档案生成属于 [Anamnesis](09_anamnesis.md)，JSONL 回放属于 [存储](07_store.md)。

`kernel.history` 保留原始记录。模型收到的是折叠前缀、已归档工具替换和未覆盖历史；不能拿原始全文计算压缩后的占用。

## 提示词与计量

系统提示按固定顺序拼接：用户级人设、基础规则与运行环境、人工项目规范、技能索引、随轮摘要约束。用户级人设文件为 `~/.logox/LOGOX.md`，存在即作为第一段注入；缺失、内容为空、超过 64KiB、符号链接或读取失败时整份跳过并回落到内置基础人设（原因可由 `/reload` 查看）。人工规范逐层发现，每层优先 `LOGOX.md`，其次 `AGENTS.md`，再其次 `CLAUDE.md`；自动 `ANAMNESIS.md` 单独作为背景记忆加载，不能替代人工规则。上述来源统一由 `memory_source_paths()` 汇总到 `ContextBundle.memory_sources`，`/status` 与会话开始分隔线显示的是这份清单，不是某一部分。

历史推理块在请求构建时过滤。本地估算按字符权重和消息外壳计算，不是精确 tokenizer；校准系数按提供商/模型分桶。`TokenLedger` 用厂商实测前缀作为计量锚点，只估算新增内容。模型切换、历史改写或系统提示变化使锚点失效；恢复后按当前模型和系统提示重新估算。

窗口和双水位按下式计算：

```text
W = 配置/提供商给出的窗口
R = min(reserve_tokens, W // 4)
H = W - R
L = int(W * low_watermark_ratio)
max_budget_tokens 可进一步降低 H；target_budget_tokens 可进一步降低 L。
输入达到 H 时触发压缩；L 是压缩目标，H 是请求保护线。
```

低于 H 但高于 L 的视图仍可请求。最终构建达到 H 时由内核暂停，保留恢复线索。估算误差与服务端实际配置差异仍可能导致窗口错误。

## 压缩流程

| 阶段 | 当前行为 | 模型请求 |
|---|---|---|
| 工具归档 | 旧大结果确认落盘后换成节选、路径和真实日志索引，暂保留最近结果 | 无 |
| 随轮摘要复用（TSRC） | 旧轮保留用户原文和已有助手摘要，暂保留最近完整轮次 | 无 |
| 极小窗口降级 | 仍超 L 时，所有已结束历史轮次只留摘要；当前轮保留，所有未归档工具结果确认落盘后换成指针 | 无 |
| 本地历史汇总 | 摘要视图仍达 H 时，汇总历史摘要、旧备忘录与归档线索；保留当前轮 | 最多一次本地请求 |

缺失随轮摘要的补写属于内核收尾，不属于 TSRC 的复用阶段；当前提供商若是云端，补写也使用该云端。

全局汇总输入来自当前有效视图，不重读全部大日志或完整工具输出。复用当前本机 Ollama / LM Studio，或使用明确配置的 Ollama 模型；端点须为本机。无合适模型、输入已超其窗口、空摘要或请求失败时保留摘要视图，不自动转云端、不机械丢弃剩余内容。同步构建入口不自行调用模型。

归档失败时保留原文；移除原始结果必须有可回读文件。逐轮摘要降级有损，不能保证保留全部语义。折叠索引的路径和行号取实际 writer，不编造缺失位置。

## 压缩状态与背景记忆

`context_state` 保存版本、来源单元数量/覆盖边界、原始内容摘要值、折叠前缀、工具替换、区间账本和工作集。它绑定有效原始分支；`compaction` 只记录统计，不能用来恢复摘要正文。

resume / 热切换 / rewind 先过滤已回滚记录，再重建历史，按新到旧寻找有效状态。检查版本、内容绑定、游标、工具 ID 与归档存在性；无匹配状态时如实回退原始历史。旧日志未保存的备忘录无法凭 token 计数恢复。有效状态变化后追加保存，失败记录警告并在后续构建重试。

缓存保留有效前缀和工具替换；历史替换、新会话及回滚重新绑定，推理过滤后的索引转换回来源下标。计量锚点与历史原文保护是两个概念，取消后者不等于取消计量校准。

自动档案在请求边界检查版本，优先保留必需规则与当前轮；按整份文档选择，默认最多占窗口 5%，放不下整份跳过并报告原因。当前目录、用户和祖先资料的优先级及开关见 [入梦模块](09_anamnesis.md)。

## 接口与源码

| 源码 | 入口与职责 |
|---|---|
| [builder.py](../../src/logox/context/builder.py) | `HierarchicalContextBuilder`：build / build_async、force_compact_async、estimate_context、restore_state、reset |
| [compaction.py](../../src/logox/context/compaction.py) | `Compactor / CompactionResult / FoldedEpoch`：阶段策略、索引与再读取工作集 |
| [tokens.py](../../src/logox/context/tokens.py) | `TokenEstimator / TokenLedger`：估算、分桶校准与实测锚点 |
| [memory.py](../../src/logox/context/memory.py) | 人工项目规范发现与来源 |
| [persona.py](../../src/logox/context/persona.py) | 用户级人设（`~/.logox/LOGOX.md`）读取、整份跳过的原因与来源 |
| [anamnesis.py](../../src/logox/context/anamnesis.py) | 自动背景记忆发现、缓存和预算 |
| [storage.py](../../src/logox/context/storage.py) | `SessionTranscriptWriter`：工具归档、JSONL 和物理行号 |

构建器显式接收 writer。`ContextBundle` 返回 system、messages、token_estimate、memory_sources 与压缩信息；报告包含 strategy、degraded 和前后估算值。完整数据格式以源码为准。

## 限制与验证入口

动态工具 Schema 尚未独立加入计量前缀指纹；Anthropic 签名推理跨请求兼容需要专门验证。工作集再读取会占预算，不代替完整历史。压缩转储可能含项目原文，按私有运行数据保存。

验证重复构建不涨索引、失败归档不丢原文、切小窗口、本地路由和恢复后实际请求内容。入口：[缓存](../../tests/unit/test_context_cache.py)、[计量](../../tests/unit/test_token_ledger.py)、[全局汇总](../../tests/unit/test_compaction_stage3.py)、[状态恢复](../../tests/unit/test_context_state_resume.py)、[索引](../../tests/unit/test_compaction_index.py)。方法见 [测试指南](../development/TESTING.md)。
