# LOGOX 文档导航

维护日期：2026-10-09。当前说明基于本仓库工作树，包含未提交实现；历史结论不代表本轮重新测试。

## 按问题阅读

| 读者/任务 | 入口 | 回答什么 |
|---|---|---|
| 初次使用 | [用户指南](user/README.md) | 安装、配置、交互、恢复、入梦和主题 |
| 确认能力范围 | [PRD](PRD.md) | 当前产品目标、概念、已实现及范围外能力 |
| 理解整体协作 | [ARCHITECTURE](ARCHITECTURE.md) | 分层、职责、数据流和跨模块约束 |
| 修改界面 | [UI-SPEC](UI-SPEC.md) | 当前布局、视觉、焦点、卡片和滚动契约 |
| 阅读源码 | [模块入口](modules/README.md) | 当前机制、接口、失败边界和验证入口 |
| 开发/修复 | [WORKFLOW](development/WORKFLOW.md) | 需求讨论至交付的步骤与完成条件 |
| 测试验收 | [TESTING](development/TESTING.md) | 离线执行、覆盖边界、真实终端和模型检查 |
| 接手当前工作 | [STATUS](development/STATUS.md) | 未完成、未验证和下一步 |
| 理解重要取舍 | [文档整理设计](development/designs/2026-10-09-documentation-organization.md)、[入梦边界决策](development/decisions/2026-10-09-anamnesis-boundaries.md) | 决策时间语境、替代方案与代价 |
| 理解典型排查 | [长任务结束复盘](development/reviews/2026-10-08-response-termination.md)、[本机代理复盘](development/reviews/2026-10-07-loopback-proxy.md) | 证据、根因、修改及适用限制 |
| 查看外部依据 | [工作流调研](development/research/2026-10-09-ai-assisted-documentation-practices.md)、[记忆框架调研](development/research/2026-10-08-agent-memory.md)、[响应结束调研](development/research/2026-10-08-response-termination.md) | 当时查到的一手资料，不自动成为产品要求 |

## 内容维护原则

用户指南描述操作与可见结果；模块描述当前实现；开发历史描述当时的问题、取舍和证据。当前说明直接改写，不追加旧实现、临时测试数量、聊天过程或孤立编号。

产品概念融入 PRD，实现概念在架构或所属模块就近解释。一次变更默认一份设计；只在独立用途明确时拆文件。完成任务移出 STATUS，普通改动由 Git 保存。

## 旧文档归档

旧版原文和原始审计材料保存在 `docs/legacy/2026-10-09-documentation-refresh/`，保持原目录结构。此前已有的 legacy 保持原样。归档供人工历史查证，Agent 不读取、不修改，不作为当前设计或验收依据；当前导航不依赖归档。

legacy、自动档案、私人会话和凭据继续本地忽略。正式指南、设计、精选决策/复盘/调研可纳入 Git；允许跟踪不等于已经提交或发布。
