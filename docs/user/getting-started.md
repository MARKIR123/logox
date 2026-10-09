# 快速开始

核对日期：2026-10-09。以下命令从已有 LOGOX 仓库目录执行；TUI 的工作目录决定项目范围。

## 准备与启动

需要 Python 3.11–3.13 和 uv。运行：

```powershell
uv sync
rg --version
uv run logox --new
```

grep 使用 ripgrep，部署到其它电脑也需安装 rg 并放入 PATH；LOGOX 不自动下载。没有 rg 时其它工具仍可用，grep 返回明确失败。搜索正则/编码边界见 [工具模块](../modules/05_tools.md)。

源码目录开发运行用 uv run。想在其它项目直接运行，可在 LOGOX 仓库安装：

```powershell
uv tool install --editable .
```

随后进入目标项目目录运行 logox；若入口不在 PATH，用 uv tool update-shell 后重开终端。Windows 用 `Get-Command logox -All` 核对实际命中入口。editable 指向源码，已运行进程仍需重启加载修改。

## 连接并发送第一条指令

1. /login 选择供应商；云端需要 Key，本地 Ollama/LM Studio 不需真实 Key。
2. /model 选择该连接的模型，本地新模型可用 /model refresh 刷新。
3. 输入具体任务并 Enter，例如“阅读当前项目入口，说明主要模块，不修改文件”。
4. 查看正文、工具状态和终态；需要参数/Diff 用 Ctrl+O，思考/入梦用 Ctrl+T。
5. /status 确认实际工作目录、模型、工具和上下文；/help 查看帮助。

可以在登录时选择仅本次或保存凭据。保存为 .env，不写明文 TOML；详见 [配置指南](configuration.md)。

## 选择启动方式

| 命令 | 结果 |
|---|---|
| logox | 默认继续当前目录最近会话，没有历史则新建 |
| logox --new | 新建干净会话 |
| logox --continue | 继续最近会话 |
| logox --resume | 选择历史会话 |
| logox --fullscreen | 备用全屏，输入区停靠底部 |
| logox --cwd <路径> | 用指定目录作为工作目录 |
| logox --chat | 最小文本入口，不启动入梦调度 |

表中尖括号需替换实际路径。主屏进入时会清理终端现有画面与回滚缓冲；正常运行刷新保留本次历史。先保留需要的 Shell 输出，再启动 TUI。

## 常见启动问题

- 入口版本不对：检查 Get-Command 命中位置，重启旧进程；源码重载不由 /reload 完成。
- 模型不可用：确认本地服务已启动或 Key/远端地址正确；目录列表不是完整可用性证明。
- 配置未生效：运行 logox --check-config 和 --print-config，核对覆盖来源，不反复改错文件。
- 字形异常：换合适终端字体或设置 ui.icon_set 为 ascii 后重启。
- 输入/滚动错位：记录主屏还是全屏、历史规模、是否有模型/工具在运行，再按 [交互指南](interaction.md) 提交复现信息。
