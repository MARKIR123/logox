# 模型响应终止与 Agent 完成语义

调查日期：2026-10-08。整理日期：2026-10-09。本文保留当时一手资料的比较，不是整理日重新联网验证的 API 参考；后续接入应固定版本并复核。

## 调查问题

模型一次生成停止，是否代表任务成功？结论是不能等同。需要结合原始停止原因、可见正文、完整工具参数以及 Agent 的执行预算判断。

- Anthropic 文档区分自然结束、长度上限、工具使用和服务端工具暂停。pause_turn 具有特定服务端工具续接语义，不能视为任意“停了一下”都应自动续问。
- 推理模型的输出预算可能同时容纳推理与最终文本。得到推理片段不意味着正文已经完成，长度到顶也不能执行半段工具 JSON。
- Agent 框架通常在模型请求之外管理工具循环与最终运行结果。一个请求的 stop reason 与整次运行的完成状态属于不同层次。
- Pi Agent 的源码可用于比较简洁的工具循环，但调查时未固定提交 SHA。不能将 main 上某段逻辑解释为所有版本、所有截断情形的通用恢复策略。

## 一手来源

- Anthropic：[Stop reasons](https://platform.claude.com/docs/en/build-with-claude/handling-stop-reasons)、[Tool use](https://platform.claude.com/docs/en/agents-and-tools/tool-use/how-tool-use-works)、[Server tools](https://platform.claude.com/docs/en/agents-and-tools/tool-use/server-tools)、[Thinking troubleshooting](https://platform.claude.com/docs/en/build-with-claude/thinking-troubleshooting)。
- OpenAI：[Reasoning](https://developers.openai.com/api/docs/guides/reasoning)、[Running agents](https://developers.openai.com/api/docs/guides/agents/running-agents)。
- Pi：[agent-loop.ts](https://raw.githubusercontent.com/badlogic/pi-mono/main/packages/agent/src/agent-loop.ts)。
- Claude Agent SDK：[Agent loop](https://code.claude.com/docs/en/agent-sdk/agent-loop)。

## 对 LOGOX 的判断

保留厂商原始终止原因，再归一化为内核可判断的事件；工具参数完整性检查位于执行前；恢复请求共享整轮次数预算。摘要生成和任务完成分开，避免兜底摘要掩盖前台未完成。

当前实现以 [内核](../../modules/01_kernel.md) 与 [提供商](../../modules/04_providers.md) 为准；历史排查看 [终止误判复盘](../reviews/2026-10-08-response-termination.md)。本文不声称所有外部框架采用相同策略。
