# AI 辅助编码与文档组织实践调研（2026-10-09）

调查日期：2026-10-09。状态：调研记录；择取原则已结合项目需要用于 [WORKFLOW](../WORKFLOW.md)，没有整体照搬外部范式。

本次只依据作者本人文章和框架官方资料，核验具体文件职责与生命周期。个人经验不等于效果实验；本文不据此推断 LOGOX 的效率、token 消耗或准确率收益。

## 开发者公开工作流

### Harper Reed：先规格，再分步计划

[My LLM codegen workflow atm](https://harper.blog/2025/02/16/my-llm-codegen-workflow-atm/) 发布于 2025-02-16。作者通过逐轮问答形成 `spec.md`，再生成 `prompt_plan.md` 和可勾选的 `todo.md`；后者帮助跨会话保留执行状态。已有项目按任务规划。文章没有规定完成后的归档方式，并明确指出多人协作和上下文管理仍有困难。

| 实际文件 | 原文明确的职责 |
|---|---|
| `spec.md` | 开发规格：需求、架构选择、数据处理、错误处理和测试计划。 |
| `prompt_plan.md` | 由规格生成的分步实施提示，供代码生成工具逐步执行。 |
| `todo.md` | 实施时勾选的检查清单，保存执行状态。 |

以上是代码生成过程的材料分工，并非完整的长期项目文档目录规范。[原文：Idea honing、Planning、Non-greenfield](https://harper.blog/2025/02/16/my-llm-codegen-workflow-atm/)

### Addy Osmani：规格、执行与常驻规则分工

[My LLM coding workflow going into 2026](https://addyosmani.com/blog/ai-coding-workflow/) 的站内发布日期为 2026-01-04。作者先形成 `spec.md` 和实施计划，再小步编码、测试与人工审查；执行时读取 `spec.md` 或 `plan.md`。他定期维护 `CLAUDE.md`、`GEMINI.md` 保存过程和风格规则，并用 Git 控制变更。文章没有给出完整目录或历史归档制度。

该文实际出现的规则文件是 `CLAUDE.md`、`GEMINI.md`；不能将其他工具的 `AGENTS.md` 或 `tasks.md` 名称归到作者这篇工作流中。[原文：Customize the AI’s behavior with rules and examples](https://addyosmani.com/blog/ai-coding-workflow/)

### Simon Willison：文档要反映系统当前状态

Simon 的 Agentic Engineering Patterns 将适当且反映当前系统的文档列为好代码的一部分，行为改变时应更新已有说明。他区分不审查生成代码的 vibe coding 与需要验证的 agentic engineering；这不是固定目录规范，而是持续开发的质量要求。[Code is cheap](https://simonwillison.net/guides/agentic-engineering-patterns/code-is-cheap/)、[What is agentic engineering?](https://simonwillison.net/guides/agentic-engineering-patterns/what-is-agentic-engineering/)

## 可以参考的组织与生命周期范式

### Diátaxis：按读者正在解决的问题分内容

Diátaxis 区分教程、操作指南、参考说明和解释：分别帮助学习、完成具体任务、查询准确事实、理解原因。官方强调从现有材料逐小块改进，不必先建四个空目录或一次推倒重来。它规范内容目的，不要求每个小项目机械创建四套文件。[框架介绍](https://diataxis.fr/)、[How to use Diátaxis](https://diataxis.fr/how-to-use-diataxis/)

### GitHub Spec Kit：规格、技术计划与执行任务分阶段

官方当前流程包括项目级 constitution，以及功能级 specify、plan、tasks、implement、converge；bug、idea 是可选的其他入口。它将需求和技术实施拆开，便于执行前澄清及实施后检查一致性。不能据此要求每个小修都走完整流程，也不能把一份任务清单当作长期使用说明。[官方 README](https://github.com/github/spec-kit/blob/main/README.md)、[Agentic SDD reference](https://github.github.io/spec-kit/reference/agentic-sdd.html)

官方参考实际使用 `spec.md`、`plan.md`、`tasks.md`：分别记录规格、技术方案、实施步骤；analyze 检查三者冲突，converge 在实施后对照结果。本次核验的是调查日的官方 main 与参考资料，不代表旧版本都包含相同命令。[Agentic SDD reference](https://github.github.io/spec-kit/reference/agentic-sdd.html)

### OpenSpec：当前契约、变更提案与历史分离

`openspec/specs/` 保存当前行为契约；`changes/` 保存一次变更的提案、设计、任务及增改删规格。完成后将变更合入当前规格，并归档变更材料。它清楚区分当前与历史，但行为规格不等于类名和技术实现步骤，不能直接替代 LOGOX 的模块实现说明。[官方概念说明](https://github.com/Fission-AI/OpenSpec/blob/main/docs/concepts.md)

### Docs-as-Code：让文档进入代码的维护流程

Write the Docs 的 Docs-as-Code 指南建议文档使用 Git、纯文本标记、代码审查、问题跟踪和自动检查等开发工具与流程。功能改动可以同步维护文档，历史由版本管理保存。这是维护方式，并不意味着每次修改都创建新文件，也不能代替对内容是否过期的人工判断。[官方社区指南](https://www.writethedocs.org/guide/docs-as-code/)

## 对 LOGOX 的应用判断

以下是调查时结合 LOGOX 文档问题提出的适配判断，不是作者对 LOGOX 的建议。采用范围见 [整理设计](../designs/2026-10-09-documentation-organization.md) 与现行工作流；本文保留调研的来源和判断过程。

- 借鉴文件职责，优先复用已有文档：产品规格归入 PRD，长期模块机制归入模块说明；一次修改的方案与检查清单放在同一份设计中，避免为每次修改机械新增三份文件。
- 执行计划可以勾选并完成；当前产品、架构和模块说明要随代码变化改写。历史设计的归档、取代关系需要 LOGOX 自己明确，不能假设作者已有规定。
- 常驻 Agent 规则只放持续有效的约束与读取指引，按任务引用详细材料。是否减少常驻内容及怎样分工，仍以 LOGOX 确认的维护规则为准。
- 借鉴 Diátaxis 的读者问题分类即可，不按框架名称增加空目录；用户操作、准确参考和原因说明可以在有必要时分篇，也可以放在职责明确的现有章节中。
- 借鉴 OpenSpec 的生命周期：设计完成后，落实后的行为归入当前产品及模块说明，设计作为带日期的历史保留；当前正文不反复追加旧问题、旧方案和当时测试计数。
- Spec Kit 可用于检查“需求是否清楚、实施是否可执行、结果是否与规格一致”；不需要为套用工具而再增加独立的规则、计划或词汇文件。
- 文档和实现一起审查：检查受影响说明是否准确、是否存在失效链接、示例是否还能执行。先采用已有工具能完成的检查，避免为文档另造庞大流程。
