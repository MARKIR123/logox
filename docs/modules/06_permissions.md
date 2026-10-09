# 权限决策与路径审计

核对日期：2026-10-09。本模块提供应用策略和人工审批，不提供操作系统进程隔离。

## 职责与边界

模型读写和运行命令可能超出用户意图。权限系统在执行前判断允许、拒绝或询问，并记录范围规则。人在回路（human in the loop, HITL）使有风险的请求等待用户裁决，而非由模型自行确认。

正常运行的 Scheduler 对全部工具调用决策器，普通工作区只读可静默允许。界面负责提示和等待，策略负责规则。开发调试显式绕过权限的装配不具备此保证，不能用于安全能力证明。

## 判定顺序

1. Shell 词法归一化，识别命令前缀和复合操作符。
2. 命中已有自毁黑名单则直接拒绝。
3. 显式 deny 规则优先拒绝。
4. 检查所有路径参数与敏感命令/模式；风险目标进入人工单次确认。
5. default 模式的复合 Shell 命令先询问。
6. 匹配会话允许、项目允许或普通只读/内置基线。
7. 剩余普通操作：creative 自动允许，default 询问。

允许规则不覆盖黑名单、显式拒绝或已识别高风险。default 不是每次写入都询问，已有合法允许规则可生效；creative 也不是关闭防护。

路径按工作区根解析，resolve 处理 .. 和现有符号链接；审计 path、file_path、target_file、file、dir、cwd 等所有存在字段，正常 cwd 不能掩盖敏感 command。敏感资源包括 .git、以 .env 开头的片段，以及 .logox 中的配置与权限文件。越界或敏感为 ASK，允许明确裁决，不直接等同永久 DENY。

递归 grep/glob 还过滤敏感子路径和逃出搜索根的结果。文件扫描边界由 [工具模块](05_tools.md) 配合，不是权限引擎单独实现所有过滤。

## 规则、审批与保存

PermissionRule 描述工具、模式、决策和作用域。会话规则在内存；项目规则通过当前项目状态存储持久化，不能跨项目误用。学习和撤销采用明确入口，保存失败需要诊断，不能声称已经永久授权。

`PermissionEvaluation` 包含 decision、reason、risk_level、matched_rule 和 suggested_rule。策略枚举与调度枚举在桥接处转换；相同字符串不意味着可直接混用类型。

Runtime 通过 PermissionAsk 与待完成结果 Future 暂停该工具协程；其他输入仍可运行。取消、关闭或无法询问时不默许执行，返回拒绝/取消结果。无人值守入梦的 ASK 直接拒绝，不出现等待人工的浮层。

用户操作见 [权限与恢复](../user/safety-and-recovery.md)。

## 源码入口

| 源码 | 职责 |
|---|---|
| [engine.py](../../src/logox/permissions/engine.py) | PermissionEngine.evaluate、learn_rule、revoke_rule、规则快照 |
| [sandbox.py](../../src/logox/permissions/sandbox.py) | PathSandbox，路径解析与风险标记 |
| [normalizer.py](../../src/logox/permissions/normalizer.py) | Shell 词法、前缀和复合操作符 |
| [models.py](../../src/logox/permissions/models.py) | Decision、PermissionMode、RiskLevel、PermissionRule |
| [decider.py](../../src/logox/permissions/decider.py)、[app.py](../../src/logox/app.py) | 层级与界面决策桥接 |
| [permission_types.py](../../src/logox/permission_types.py) | 不依赖界面的审批数据 |
| [scheduler.py](../../src/logox/kernel/scheduler.py) | 内核注入接口与执行前裁决 |

## 安全限制与验证

应用不能完整理解任意脚本内部行为；pytest 等基线命令也能执行项目代码。resolve 检查后文件或链接仍可能被外部进程替换，即检查到使用之间的竞争（TOCTOU）。已允许的 Shell 拥有用户账户权限；没有容器、受限令牌或系统级隔离。

测试需要走真实 Scheduler，而非只调用引擎。验证普通只读、敏感和越界、多个路径、符号链接、显式拒绝优先、保存撤销、审批取消与无界面拒绝。入口：[规则](../../tests/unit/test_permissions.py)、[模式](../../tests/unit/test_permission_mode.py)、[管理命令](../../tests/unit/test_permissions_command.py)、[审批流程](../../tests/tui/test_permission_flow.py)、[贯通边界](../../tests/unit/test_audit_a_choices.py)。方法见 [测试指南](../development/TESTING.md)。
