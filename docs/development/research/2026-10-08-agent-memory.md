# 后台记忆整理范式调研

调查日期：2026-10-08。整理日期：2026-10-09。范围：当时官方文档可确认的机制，不代表最新版本，也未在整理日重新核验。外部框架的能力不等于 LOGOX 已实现；原始私人材料不进入共享记录。

## 回顾与结论

后台记忆可以分为“何时启动”“怎样提炼”“存在哪里”“如何回到前台”四个问题。多数记忆库只提供其中部分接口，不能将记忆抽取自动解释为空闲调度、审批或完整入梦系统。

| 范式 | 调查时可确认的机制 | 对 LOGOX 的意义 |
|---|---|---|
| Claude Code | 手写规范与自动记忆分层；自动记忆入口用于后续会话 | 区分人工约束与系统整理，控制注入内容；不据此推断所有账户都具备相同 dreaming 能力 |
| Letta | sleep-time 后台记忆处理，以及可版本化的 Markdown/memfs 思路 | 记忆生成与前台回应分开；版本与审查不天然等于人工批准 |
| LangGraph / LangMem | 状态与长期存储分工；延迟处理、重新调度可由应用安排 | 调度仍需应用定义，多窗口和唤醒规则不是存储自动提供 |
| Mem0 | 从输入抽取记忆并提供存储操作；不同版本的操作语义存在差异 | 使用前固定版本，不能照搬旧 ADD/UPDATE/DELETE 宣称当前行为 |
| CrewAI / AutoGen | 记忆检索、上下文注入或工作流反思组件 | 组件需要应用组合，不能直接称为与 LOGOX 相同的夜间入梦 |

## 一手来源

- [Claude Code Memory](https://code.claude.com/docs/en/memory)、[官方使用提示](https://support.claude.com/en/articles/14554000-claude-code-power-user-tips)。
- [Letta Memory](https://docs.letta.com/configuration/memory)、[memfs](https://docs.letta.com/concepts/memfs)、[Sleep-time compute](https://www.letta.com/blog/sleep-time-compute/)。
- [LangGraph Memory](https://docs.langchain.com/oss/python/concepts/memory)、[LangMem Delayed Processing](https://langchain-ai.github.io/langmem/guides/delayed_processing/)。
- [Mem0 Add](https://docs.mem0.ai/core-concepts/memory-operations/add)。
- [CrewAI v1.15.25 Memory](https://docs.crewai.com/v1.15.25/en/concepts/memory)、[AutoGen Memory](https://microsoft.github.io/autogen/stable/user-guide/agentchat-user-guide/memory.html)。

## LOGOX 的采用边界

借鉴后台整理与记忆注入分工，但空闲阈值、多窗口队列、只读工具、固定会话归属、提案核验和中断续做由 LOGOX 自身定义。当前机制看 [Anamnesis](../../modules/09_anamnesis.md)，取舍看 [边界决策](../decisions/2026-10-09-anamnesis-boundaries.md)。

没有使用这些框架的公开效果数字作为 LOGOX 的指标。相同任务下 token、工具调用及召回质量需要独立测量。
