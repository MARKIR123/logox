# LOGOX

LOGOX 是可自定义的终端编程助手，参考 Pi Agent 与 Claude Code。模型通过文件和命令工具工作，TUI 展示正文、思考、工具进度与审批；会话和压缩状态可恢复，支持受限文件回滚。

- 默认主屏使用终端历史，`--fullscreen` 提供独立消息视口。
- 内置 read、write、edit、glob、grep、shell，支持 Skill、插件、Hook 和 stdio MCP。
- 上下文采用工具归档和随轮摘要复用，必要时使用本地模型汇总历史。
- Anamnesis 在 TUI 空闲时用独立本地模型只读回顾项目，核验后更新用户/项目活档案，并保存可展开过程。

权限为应用策略；文件回滚不覆盖任意命令副作用。入梦不写源码、不执行测试；模型能力和优化效果以实际验证为准。

## 快速开始

在已有仓库目录中：

```powershell
uv sync
rg --version
uv run logox --new
```

需要 Python 3.11–3.13、uv；grep 需要 PATH 中的 ripgrep。进入后使用 /login 配置连接，再用 /model 选择模型。命令中的模型名必须来自你实际可用的服务。

完整步骤见 [快速开始](docs/user/getting-started.md)，本地 Qwen 配置见 [配置指南](docs/user/configuration.md)。在其它项目使用可先安装 `uv tool install --editable .`，再进入目标目录运行 `logox`；已运行的进程不会自动加载源码修改。

## 文档入口

| 想做什么 | 阅读 |
|---|---|
| 找到合适文档 | [文档导航](docs/README.md) |
| 配置、操作、恢复和入梦 | [用户指南](docs/user/README.md) |
| 理解产品范围和架构 | [PRD](docs/PRD.md)、[架构](docs/ARCHITECTURE.md) |
| 理解当前实现 | [模块入口](docs/modules/README.md) |
| 开发和接手 | [WORKFLOW](docs/development/WORKFLOW.md)、[TESTING](docs/development/TESTING.md)、[STATUS](docs/development/STATUS.md) |

文档反映当前工作树，包含尚未提交实现。当前说明随代码改写，重要历史保留日期和适用范围；旧原文归入本地 legacy，不作为使用入口。
