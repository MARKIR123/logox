# 应用装配、配置与扩展

核对日期：2026-10-09。Runtime 连接模块，不在各模块重复配置和生命周期算法。

## 职责与启动

装配根（composition root）统一创建提供商、工具、权限、上下文、存储和界面，再将所需能力传入内核。依赖注入（dependency injection）使内核使用接口而非自行选择厂商、配置和审批 UI，测试可以替换实现。

CLI → 路径和配置 → build_runtime → Runtime → InlineApp / FullscreenApp。help/version 延迟重导入，文本 --chat 有独立入口；Anamnesis 调度只随 TUI.run 启动，关闭取消并等待收尾，没有独立守护进程。

## 配置与状态

优先级从低到高：Schema 默认值 → 用户 config → 从远到近项目 config → 上次使用 state → LOGOX__ 环境变量 → CLI。嵌套表深合并，数组整体替换。defaults.toml 是参考样本，运行时不以读取它作为基线。

ConfigBundle 保存生效值、文件来源、字段来源和 issues。普通模式诊断并回退问题字段；strict 存在任何 issue 即失败。API Key 不接受明文 TOML 字段，通过环境/.env 解析；具体使用见 [配置指南](../user/configuration.md)。

启动发现/初始化当前项目 .logox，偏好与模型状态可跨项目继承，学习的权限规则按项目隔离。配置字段存在不表示 Runtime 已接入该字段；注册工具表当前为六个，不能依据 tools.enabled 宣称 todo 已实现。

## 跨模块操作

| 操作 | 当前协作 |
|---|---|
| 切提供商/前台模型 | 构造可用适配器，同步 Kernel、窗口、计量桶和界面，按新模型及早压缩 |
| 本地全局汇总 | Runtime 注入摘要回调；无明确可用本地模型不转云端 |
| 权限询问 | UiPermissionDecider 把策略结果转成 PermissionAsk，等待界面选择 |
| 会话切换/回滚 | 重建有效历史、上下文状态、度量和时间线，禁止整轮中途切换 |
| 入梦模型选择 | 独立本地目录和静态窗口校验，先保存独立状态再更新选择，不影响前台 /model |
| 关闭 | 停止新任务、解除等待、关闭入梦、扩展连接及订阅资源 |

/reload 在前台整轮忙时拒绝；空闲时重扫人工记忆、技能、模板命令和当前主题，校验配置并分别报告。它不重新导入 Python、不重建提供商、MCP 或插件，也不半替换构造期配置。系统提示改变会使计量前缀失效，历史和检查点保持原样。

## Skill、命令与外部扩展

- Skill 首次注入名称、描述和路径，正文按需读取；工具能力不会仅因有技能文件自动注册。
- 用户文件命令展开参数和提示；内置 slash 路由属于 TUI，查看入口为 /commands。
- Python 插件通过 PluginContext 注册工具、命令和订阅。插件运行本机代码，没有卸载重载或系统隔离承诺。
- Shell Hook 只支持声明的观察型事件，匹配、超时和阻塞行为由 HookRunner 处理；不是通用执行拦截策略。
- MCP 服务首次 list/call 才惰性连接，默认给模型一个 mcp 元工具：list 取紧凑目录，call 代理执行。目录只保留提示性类型，不能代替远端完整 Schema 校验。
- 当前 MCP 实现 stdio；配置保留 http 以给存量配置诊断，但连接前明确拒绝，不误启动 stdio。启动取消和重复关闭清理连接栈。

扩展可信度由启用者判断；普通工具权限不隔离插件、钩子或远端服务。诊断 EventLog 通过非阻塞队列批量写盘，可能丢积压事件；它与会话持久化链不同。

## 源码与接口

| 源码 | 职责 |
|---|---|
| [cli.py](../../src/logox/cli.py)、[app.py](../../src/logox/app.py) | CLI、Runtime、build_runtime、UiPermissionDecider |
| [paths.py](../../src/logox/paths.py)、[config](../../src/logox/config/) | 路径、合并、校验、状态、主题和凭据文件 |
| [mcp](../../src/logox/mcp/) | Manager、Client、MetaTool 和生命周期状态 |
| [plugins.py](../../src/logox/plugins.py)、[hooks.py](../../src/logox/hooks.py) | 扩展注册及观察钩子 |
| [skills](../../src/logox/skills/)、[commands](../../src/logox/commands/) | 资源扫描和按需展开 |
| [telemetry.py](../../src/logox/telemetry.py) | 脱敏与诊断日志 |
| [anamnesis/service.py](../../src/logox/anamnesis/service.py) | 独立后台生命周期，Runtime 仅装配 |

build_runtime 返回 Runtime 或 StartupError；Runtime 的跨模块方法不是稳定外部 SDK。KernelPort 只有 start/cancel/current_turn，不应由界面反向依赖内核私有实现。

## 验证入口

验证六级覆盖、字段来源、密钥处理、状态项目隔离、真实装配摘要回调、经过 Scheduler 的 MCP 参数、连接取消和重复关闭，以及资源重载边界。入口：[配置](../../tests/unit/test_config.py)、[启动](../../tests/tui/test_app_startup.py)、[重载](../../tests/unit/test_reload_resources.py)、[MCP 生命周期](../../tests/unit/test_mcp_lifecycle.py)、[插件](../../tests/unit/test_plugins.py)、[钩子](../../tests/unit/test_hooks.py)、[技能](../../tests/unit/test_skills.py)、[入梦模型](../../tests/anamnesis/test_model_selection.py)。方法与环境隔离见 [测试指南](../development/TESTING.md)。
