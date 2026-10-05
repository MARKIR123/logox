# Logox

> **内核极简、外延极松、界面体面的终端 Agent（TUI）工具。**
> 内核只负责「消息循环 + 工具调度」，其余一切皆可替换；「高度自定义」是一等公民而非事后补丁。

---

## 简介

Logox 是一个在终端里运行的 AI 编码 Agent：读你的代码、改你的文件、跑你的命令，
把整个过程流式渲染在终端界面上。

权限、日志、渲染、上下文压缩、持久化全部是挂在**事件总线**上的订阅者，
因此任何一层都能被替换而不动内核。

- **事件总线内核**：内核禁止 import 任何具体实现，这条硬约束由静态断言守住。
- **自研终端界面**：不用任何界面框架、走主屏，保留终端原生的滚动与划选复制。
- **读查改测闭环**：`read` / `write` / `edit` / `glob` / `grep` / `shell` 六大工具。
- **改动可控**：五层权限纵深防御 + 人在回路审批，写文件前先看 diff。
- **省 token**：上下文分层装配、双水位线压缩、前缀缓存友好。
- **能回退**：零 Git 依赖的检查点，任意轮次可时空回滚。
- **可扩展**：MCP 接入、生命周期钩子、插件、技能包。
- **多厂商**：OpenAI 兼容端点（DeepSeek / Ollama / LM Studio / OpenRouter）+ Anthropic。

## 快速开始

### 装好它

```powershell
git clone https://github.com/<你的账号>/Logox.git   # ← 换成你的仓库地址
Set-Location Logox

uv sync                         # 建 .venv 并装好运行时依赖
uv tool install --editable .    # ★ 推荐：装成全局工具，任意目录敲 logox 都能用
```

装完后**在任意目录**打开终端敲 `logox`，那个目录就成为独立工作区。

> **两种入口，跑的都是同一个 `logox.cli:main`**，按需选一个：
>
> | 场景 | 命令 | 说明 |
> |---|---|---|
> | 日常使用（推荐） | `logox` | `uv tool install --editable .` 装出来的全局入口；`--editable` ⇒ 源码改动即时生效 |
> | 任何环境兜底 | `python -m logox` | 只需 `PYTHONPATH=src`，不需要任何安装 |
>
> `uv tool install` 把可执行文件放到 `uv tool dir --bin`（Windows 上是
> `%USERPROFILE%\.local\bin`）。若它**不在 `PATH` 上**，跑一次 `uv tool update-shell` ——
> 这是唯一需要碰 PATH 的地方，而且由 uv 负责，不需要任何自定义脚本。
>
> ⚠️ **如果 `PATH` 上还有别的 `logox`**（例如你以前用旧安装器生成的 `~/.logox/bin/logox.cmd`，
> 或某个仓库的 `.venv\Scripts`），先出现的那个会**遮蔽** uv 装的那个。
> 用 `where logox`（Windows）/ `which -a logox`（Unix）确认命中谁；旧的手写包装器可以直接删掉。

`grep` 工具需要 PATH 中的 [ripgrep](https://github.com/BurntSushi/ripgrep#installation)（`rg`）。先运行 `rg --version` 核对；Windows 可使用 `winget install BurntSushi.ripgrep.MSVC`。LOGOX 不自动下载可执行文件，缺失时会返回安装提示。搜索采用 ripgrep 默认正则与 auto 编码，前后查找、反向引用和旧版 GBK 回退存在兼容变化，见 [工具模块](docs/modules/05_tools.md)。

### 用一下

```powershell
logox            # 进主屏界面（真实内核）

#    进去之后：
#      /login   选供应商（弹窗 ↑↓）→ 粘贴 API Key（以圆点显示、不回显）→ 选「记住 / 仅本次」
#               「记住」→ 写 <项目>/.logox/.env（已 gitignore）；「仅本次」→ 绝不落盘
#               提交后会自动向端点抓取真实模型列表
#      /model   选模型（弹窗，零网络），或直接 /model deepseek-v4.1-flash-expires-on-0910
#      /effort  思考档位（off/low/medium/high/auto），设置即生效
#      /rewind  时空回滚（查看全轮次意图并回退）
#      /help    完整键位与命令表（F1 同）

# ② 【不需要 API Key】体验模式：真实内核，只有模型响应是预置脚本（建议先建个空目录再跑）
mkdir demo; Set-Location demo
$env:LOGOX_SCRIPTED_PROVIDER = "1"   # 与真实 logox 只差这一行；删掉它即回到真实模型
logox                                # 或 python -m logox
#    启动后键入任意一句话按 Enter：能看到流式输出、工具卡片 ✓、状态栏 tok/s 与 cache。
#    试 /help · /debug · /effort high · /theme logox-light · 生成中按 Esc。

# ③ 最小文本模式：stdout 只有助手正文，可重定向
logox --chat
```

**还没有模型端点？** 本机端点不需要任何密钥：装个 [Ollama](https://ollama.com) 跑起来，
在 `~/.logox/config.toml` 里写 `[provider]` + `name = "ollama"` + `model = "qwen3:8b"` 即可。
用 `logox --check-config` 看当前生效的配置来自哪里。

> **统一入口**：所有平台都以 `python -m logox` 为稳定入口（`PYTHONPATH` 只需指向 `src`）。

---

## 开发

### Anamnesis 入梦（功能分支）

TUI 打开且前台任务结束后，空闲超过 30 分钟自动回顾；本地时区 00:00—08:00 可只读研究项目。发送消息或 `/anamnesis stop` 暂停当前窗口；编辑未发送的草稿不暂停，关闭 TUI 停止任务。首版不执行实验、修改代码或运行测试。

在 `~/.logox/config.toml` 增加并重启：

```toml
[anamnesis]
provider = "ollama"
model = "" # 填写自己安装的本地模型完整名称；留空不会运行
```

`/anamnesis nap`、`/anamnesis sleep` 手动开始，`/anamnesis stop` 暂停，`/anamnesis status` 查看状态，`/anamnesis history` 查看当前项目全部运行，`/anamnesis report [run_id]` 查看完整分析与晨报，`/anamnesis trace [run_id]` 查看原始过程。记忆按条采纳：合格项保存，证据不足或旧状态留在候选报告中，不因一条拒绝而整批失败。未形成完整提案时保留进度并停止同批自动重试，可手动 `nap`／`sleep` 或等待真实新资料继续；正常批次交接只更新原卡片和剩余数量，不刷暂停通知。卡片用点击或 `Ctrl+T` 展开：实际返回的思考实时显示，阶段结论、档案差异及读取明细可查；新记录绑定启动会话，resume 恢复原卡片。

只整理当前仍打开的项目，每个任务只读自己项目会话和源码。同项目全部窗口空闲超过 30 分钟、前台结束后才自动入梦；项目按最后提交用户指令从新到旧轮转，最近会话承载，本次归属不迁移。用户／项目档案分别为 `~/.logox/ANAMNESIS.md` 和当前目录 `ANAMNESIS.md`，按证据更新并保存版本；前台背景记忆默认最多占窗口 5%。本地窗口取 `[providers.<name>].context_window` 或 `model_windows` 静态配置，未配置模型或足够窗口会说明原因，不转云端、不探测 `/api/ps`。会话新记录统一保存 Unix 秒 timestamp；旧资料缺失时间在读取时用 null 表示未知，不重写原日志。入梦整理与核验会同时得到当前时间和来源时间，用于判断短期状态时效；明确长期偏好不会只因时间经过自动失效。完整配置、恢复与失败边界见 [入梦模块](docs/modules/09_anamnesis.md)。

### 离线流畅性复测

```powershell
.venv\Scripts\python.exe tools\benchmark_tui.py --pairs 100 800 3200 --frames 15 --profile
```

使用假终端和合成历史，输出输入／流式帧耗时 JSON，不启动模型或读取用户配置；结果不代表实际 Windows Terminal 延迟。优化内容、全量测试与剩余问题见 [清理验收](docs/CLEANUP-REPORT-2026-10-03.md)。运行中的 LOGOX 需退出并重启才能加载新的 Python 代码。

### 开发命令

```powershell
uv add <包名>                  # 加依赖（同时更新 uv.lock）
uv run ruff check src          # 静态检查
uv run python -m logox         # 用锁定的环境跑
```

### 常用参数

```powershell
logox --version          # 打印版本（快速路径，不加载配置与界面）
logox --check-config     # 加载并校验配置，打印来源与问题清单
logox --print-config     # 打印合并后的生效配置（TOML）
```

---

## 许可

MIT
