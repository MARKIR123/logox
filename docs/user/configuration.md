# 配置与模型连接

核对日期：2026-10-09。完整字段查 [Schema](../../src/logox/config/schema.py) 和 [默认样本](../../src/logox/config/defaults.toml)，不要把声明字段直接当成已接入能力。

## 配置位置和覆盖

用户配置为 ~/.logox/config.toml，项目配置为当前目录或祖先的 .logox/config.toml。项目与程序安装目录无关。

覆盖从低到高：Schema 默认 → 用户配置 → 从远到近项目配置 → 上次使用 state → LOGOX__ 环境 → CLI。表深合并，数组整体替换。state 记住模型、主题等选择，可能覆盖手写默认；模型/主题命令可更新偏好。

```powershell
logox --check-config
logox --print-config
logox --check-config --strict-config
```

普通校验报告问题并回退，strict 有任何 issue 即失败。print-config 输出生效配置，不应把私人端点或状态未经检查直接分享。

## 本地 Qwen 示例

在用户 config.toml 中合并以下片段，保留其它配置：

```toml
[provider]
name = "ollama"
model = "qwen3.8:27b"

[providers.ollama]
kind = "openai_compat"
base_url = "http://127.0.0.1:11434/v1"
api_key_env = ""
models = ["qwen3.8:27b"]
context_window = 32768

[providers.ollama.model_windows]
"qwen3.8:27b" = 32768
```

模型名称和 32768 都是示例：名称以本地服务实际目录为准，窗口填写服务真正启用容量。LOGOX 不因配置模型名自动下载或加载它；窗口按静态配置查表，不用 /api/ps 或 /api/show 动态检测。更换服务容量也需同步配置。

LM Studio 使用已有连接名 lm-studio，将 base_url 和实际窗口填入同名段。前台换连接用 /login <连接名>；/model 只换当前连接模型，不替代换供应商。

## 云端凭据

TOML 仅使用 api_key_env 指向环境变量；明文 api_key 字段被拒绝。本地免密端点用空 api_key_env。

/login 输入后可选择仅本次，或明确保存到当前装配选择的 .env。当前项目通常为 .logox/.env，用户级可为 ~/.logox/.env；确认框显示实际目标。环境变量优先于文件，不在文档中粘贴真实 Key。

.env 被 Git 忽略，但 Windows 的 chmod 不构成 ACL 隔离保证；本地账户访问控制需由使用者管理。

## 常用调整

| 需求 | 设置/入口 | 注意 |
|---|---|---|
| 输出与思考 | provider.max_tokens、/effort | 前台额度与入梦无固定输出额度不同，服务端限制仍生效 |
| 上下文压缩 | context.reserve_tokens、low_watermark_ratio、max_budget_tokens | 估算与双水位，不是服务端容量保证 |
| 权限模式 | /mode default 或 creative | creative 仍保留黑名单和高风险询问 |
| 全屏 | --fullscreen 或 ui.fullscreen | 主屏和全屏浏览边界不同 |
| 主题/字形 | /theme、ui.icon_set | 用户主题目录；修改配置需重启 |
| 入梦 | anamnesis 段及独立 model 子命令 | 见 [入梦指南](anamnesis.md) |
| MCP | mcp.servers | 当前仅 stdio，http 会明确拒绝 |

tools.enabled 中 todo、部分 Shell/UI 字段属于声明与实现需要核对的范围。实际工具和行为以 /status 与源码为准，不能仅依据 print-config 推断已生效。

## 修改后如何生效

`/reload` 重扫人工规范、Skill、模板命令、主题并校验配置。它不替换运行中的提供商或构造期字段，不重新导入 Python，不重连 MCP/插件。修改 config.toml 后通常重启；已保存的选择还受更高优先级覆盖影响。

/model refresh 和 /anamnesis model refresh 查询本地目录；刷新目录不验证模型全部能力。前台本机回环连接绕过环境代理，远端仍保留 SDK 默认代理行为；入梦另禁止代理和重定向。
