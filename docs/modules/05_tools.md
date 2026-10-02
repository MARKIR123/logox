# 05 · 内置工具与执行结果

> 核对日期：2026-09-29。范围：当前工作区源码（包含已有未提交改动）。本文描述已实现行为；性能保证与系统安全保证必须另有测试和测量依据。

## 1. 定位与边界

模型要读代码、修改文件和运行测试，需要通过工具连接文件系统与子进程。本模块提供六个内置工具和统一输入输出；调用顺序、授权、超时外层收尾与检查点由调度器 / Runtime 配合，不能把工具自身当作完整安全沙箱。

工具执行失败应尽可能返回可解释结果，让模型知道路径错误、替换文本不唯一或命令失败。错误反馈提供纠正机会，不保证模型下一步一定正确。

## 2. 源码地图与公开名称

| 文件 | 参数 / 工具 | 模型实际调用名称 | 关键行为 |
|---|---|---|---|
| [base.py](../../src/logox/tools/base.py) | `Tool`, `ToolArgs`, `ToolSpec`, `ToolContext`, `ToolResult` | — | 协议、参数校验与双轨输出 |
| [fs_read.py](../../src/logox/tools/fs_read.py) | `ReadArgs`, `ReadTool` | `read` | 编码/BOM/二进制识别、行号与分页 |
| [fs_write.py](../../src/logox/tools/fs_write.py) | `WriteArgs`, `WriteTool` | `write` | 创建或覆盖文件，输出 Diff/统计 |
| [fs_edit.py](../../src/logox/tools/fs_edit.py) | `EditArgs`, `EditTool` | `edit` | 局部替换、匹配歧义与文本格式保护 |
| [fs_grep.py](../../src/logox/tools/fs_grep.py) | `GrepArgs`, `GrepTool` | `grep` | 异步 ripgrep、auto 编码、范围过滤与全局匹配上限 |
| [fs_glob.py](../../src/logox/tools/fs_glob.py) | `GlobArgs`, `GlobTool` | `glob` | 路径通配、忽略目录、字母序和总数 |
| [shell.py](../../src/logox/tools/shell.py) | `ShellArgs`, `ShellTool`, `ShellBackend` | `shell` | 后端探测、异步子进程、超时/取消、输出截断 |

`fs_*` 是文件名与部分历史兼容规则名称，当前注册工具名为表中短名称。扩展工具属于 [08](08_app_and_collaboration.md)，共享本模块协议。

## 3. 执行与输出约束

调度器先做授权及参数校验，再创建 `ToolContext` 并调用 `run(args, ctx)`。`ToolArgs` 使用 `extra='forbid'`，拼错字段不会被忽略；`ToolSpec.params` 生成模型可见 Schema。只读分类由 `ToolSpec.readonly` 控制，是否进入权限决策由 `requires_permission` 控制，两者是不同开关。

`ToolResult.content` 是回灌给模型的纯文本；`display` 是人类卡片的展示数据，由界面决定怎样渲染；`error` 是分类错误，`change_stat` 是行变更统计。不要在工具层输出 Rich 控件或把 ANSI 样式混入模型内容。

`read` 默认最多返回 2000 行，单行最多 2000 字符，offset 从 1 开始；当前会先读完整文件并拆行，分页限制的是输出，不是磁盘读取与峰值内存。`grep` 默认最多 100 条，最高 500 条，每条展示约 200 字符，使用 PATH 中 ripgrep 的默认 Rust 正则和 auto 编码；保留 UTF-8/BOM 检测，不再逐文件 GBK/latin-1 回退。结果按文件结束事件的 binary_offset 判断二进制，单个 JSON 事件仍可能包含超长源行。`glob` 默认显示 200 条、最高 1000 条，为了报告精确总数并按字母序展示仍要遍历所有匹配。

局部 `edit` 降低需要生成的文本量，但依赖明确匹配与歧义处理；整文件 `write` 适合新建和小文件。两者的具体编码、换行与写入算法以对应源码及用例为准，不把“文件级原子替换”写成跨文件事务。

Shell 根据配置探测平台后端；执行环境注入非交互变量、传递退出码，合并输出并在超出 8000 字符时保留首尾各 2000 字符。默认命令超时为 30 秒，超时/取消尝试终止进程树。当前截断发生在收集输出之后，不能限制子进程持续输出带来的峰值内存。非交互环境变量也不能保证每个程序都不会等待输入。

## 4. 接口、失败与测试

源码接口：

```python
# Tool.run
async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult: ...

# ToolSpec.schema
def schema(self) -> ToolSchema: ...

# ToolResult.failure
@classmethod
def failure(cls, category: ErrorCategory, message: str, *, detail: str | None=None, content: str | None=None) -> ToolResult: ...
```

`Tool.run()` 接收参数模型，兼容工具内部可以有特定扩展；`ToolContext` 只提供 `cwd` 和 `is_cancelled()`。`ToolResult.digest` 是第一行最长 80 字符的摘要，全文仍在 content 中。调用方不得把 digest 当成完整观察结果。

| 场景 | 期望处理 | 测试入口 |
|---|---|---|
| 文件不存在、目录误当文件、分页越界 | 明确错误或空页说明 | `test_tool_fs_read.py` |
| BOM、CRLF、GBK、二进制与超长行 | 编码/格式和截断信息可核对 | `test_tool_fs_read.py`, `test_tool_fs_edit.py` |
| 目标不匹配、重复匹配、错误参数 | 不误写文件，返回可解释失败 | `test_tool_fs_edit.py`, `test_tools_base.py` |
| 忽略目录、匹配上限、取消 | 不进入忽略目录；停止后有正确报告 | `test_tool_fs_grep.py`, `test_tool_fs_glob.py` |
| Shell 超时、非零退出、取消 | 清理进程并返回分类失败 | `test_tool_shell.py` |
| 写前后快照 | 由 Scheduler 保存并发布检查点 | `test_rewind.py` |

```powershell
$env:PYTHONPATH = 'src'
.venv\Scripts\python.exe -m pytest -q tests/unit/test_tools_base.py tests/unit/test_tool_fs_read.py tests/unit/test_tool_fs_write.py tests/unit/test_tool_fs_edit.py tests/unit/test_tool_fs_grep.py tests/unit/test_tool_fs_glob.py tests/unit/test_tool_shell.py
```

## 5. 本轮优化设计、权衡与限制

`grep` 当前通过异步 ripgrep 子进程搜索，复杂匹配在 LOGOX Python 进程之外执行。按 JSON 文件事件收集、确认二进制状态后输出，达到全局上限终止并回收进程。相较最初 Python 惰性遍历方案，正则、编码、行边界和遍历顺序存在用户已接受的兼容变化，见 §5.2。

`ToolResult.digest` 只截取判断摘要所需的前 81 字符再拆首行，避免对大结果构造所有行的副本；先做与原算法的多种换行符对照。ASCII token 估算的优化归入 [02](02_context.md)。

**这是面试常考的：惰性遍历（lazy iteration）与时间/空间复杂度。** 先收集所有路径会在第一条有效结果前扫描整个树；按需生成路径后，达到上限就能停止。read / glob 的读取与扫描卸载到工作线程，grep 使用异步子进程，事件循环可继续处理输入和事件。取消设置线程可检查标志，glob 在目录/文件循环检查；grep 取消时终止并回收进程；read 在整文件读取前后检查。系统调用无法强行中止，大文件解码与 glob 字母序全量排序仍需内存，不等同于流式文件解析。Shell 同时读取两个输出流，分别增量验证 UTF-8 / GBK / Latin-1，保存固定大小首尾和完整字符计数；按原 8000 字符阈值及 2000/2000 首尾格式呈现。PowerShell 错误标记在完整读取过程中识别，即使位于被省略的中间内容也不会漏报。

当前通用验收见 [A 方案回归](../../tests/unit/test_audit_a_choices.py) 与对应模块测试；当前接手状态见 [架构入口](../ARCHITECTURE.md)。

### 5.1 已确认 A：线程卸载与受控 Shell 收集（grep 已由 D201 演进）

read/glob 的同步扫描放到工作线程（grep 现为 §5.2 的子进程），异步入口等待结果；取消同时设置线程可检查标志，在目录/文件/行循环退出，不让后台扫描继续无限运行。线程不能强制终止系统调用，等待点之外的取消响应仍受文件系统限制。写工具继续原路径，避免取消后后台线程继续写。

Shell 用并发流读取 stdout/stderr，保留有界首尾与总字符计数，再按原组合顺序及原 8000/2000/2000 规则展示；编码增量解码，短输出完全一致，持续输出不占无限内存。检测 PowerShell 错误特征独立于截断内容。超时/取消清理读任务与子进程树。验收大输出、双流、编码分片、无输出、非零退出、超时、取消与扫描中输入任务仍能运行。


### 5.2 grep 迁移 ripgrep 的实施设计（2026-09-29，用户选择 B，已实现）

**问题与裁定**：Python `re` 在复杂匹配期间可能持有全局解释器锁（GIL），工作线程不能保证界面获得运行时间。用户选择 ripgrep，接受正则与编码兼容变化；独立 Python 进程虽保留语义，但增加启动与进程协议维护，并继续承担回溯正则的极端耗时，未采用。

**输入和依赖**：保持 `GrepArgs(pattern, path, case_sensitive, max_matches)` 与 `ToolResult`，从 PATH 查找 `rg`。当前环境已实测 `rg --version` 为 15.2.0；部署环境需自行安装 ripgrep。缺失或不可启动时返回明确 TOOL_FAILURE，不静默退回阻塞式 Python 正则。调用采用参数列表，`--regexp` 传递模式、`--` 分隔目标，无 shell 拼接；禁用用户 rg 配置、预处理与自动 PCRE2，Windows 隐藏子进程窗口。

**搜索边界**：采用默认 Rust 正则引擎，前后查找和反向引用不支持，错误作为 BAD_REQUEST 回灌。使用 `--hidden --no-ignore` 保留此前包含普通隐藏文件、无视 gitignore 的范围，再显式排除现有忽略目录、`.tmp*` 目录及敏感路径；显式敏感目标延续原权限流程。默认不跟随递归符号链接，结果再次检查解析路径及搜索根范围。编码改为 ripgrep auto：UTF-8 与 BOM 自动识别，不再承诺 GBK / Latin-1 逐文件回退；行边界改为 LF/CRLF，Unicode 分隔符不保证等同 Python splitlines。依据 [ripgrep 官方指南](https://github.com/BurntSushi/ripgrep/blob/master/GUIDE.md)，这些差异必须向使用者和模型明确。

**输出和生命周期**：异步消费 JSON 行事件，提取文件、行号与匹配文本，再转换原 `path:line: content`、约 200 字符单行截断及 DisplayHint。每个文件暂存至结束事件，binary_offset 非空即丢弃该文件匹配，避免 JSON 先报告匹配、后发现 NUL 的内容泄露。保留全局 100/500 条上限；达到上限即终止进程并收尾，不将每文件 max-count 冒充全局上限。不收集整个搜索输出；单个 JSON 匹配事件仍可能包含超长源行，其峰值内存不承诺固定上限。stderr 仅保留末尾固定大小；错误退出不得静默包装成无匹配。二进制文件不作为文本返回，保留 rg 的 NUL 检测及 BOM 转码行为。搜索遍历顺序不承诺与旧 os.walk 相同。

取消包括外层 task.cancel 与 ToolContext 标志：启动期间也必须接住创建任务并清理已启动进程；运行中终止子进程、等待回收、取消输出读取任务。rg 不启动预处理子进程，按直接进程清理；不改 shell 的进程树机制。外层 Scheduler 的超时继续生效。

**验收**：关键词、大小写、行号、CRLF、中文路径、正则错误/不支持、无匹配、二进制、忽略/敏感目录、显式敏感目标、符号链接、全局上限、超长行；缺失依赖、退出错误、畸形 JSON、stderr 限额；取消期间实际子进程回收、输出持续时心跳可运行。旧测试中以 Python os.walk/open 作为搜索成本契约的断言改为 rg 的结果与进程生命周期验收，不删除产品边界测试。


**验证结果**：`test_grep_ripgrep.py` 14 项测试通过；包含真实子进程取消、启动中取消、全局上限与协议失败后的退出码核查。既有 grep 与 A 方案/审计回归合计 101 项通过；全项目 1893 passed / 3170 subtests passed。24 字有界复杂正则心跳最大间隔从约 361ms 降为约 33ms；这是单次本机合成探测，仍包含 Windows 调度与进程启动开销，不代表真实项目扫描或终端时延。


**本机依赖交付**：已把现有 ripgrep 15.2.0 复制至 `.venv/Scripts/rg.exe`，并只使用持久系统/用户 PATH 实测查找与 GrepTool 调用；不依赖 Codex 注入 PATH。源码/wheel 不包含此本机二进制，跨机器或重建 .venv 时按 README 安装并核对 rg --version。winget 本次源连接失败，未把它记作安装成功。

### 5.3 入梦的只读工具边界

入梦为自身 glob 实例注入路径过滤，为 grep 实例注入排除模式；普通工具默认行为不变。长眠只开放当前项目 read／glob／grep，排除 LEGACY、敏感文件、生成档案及越界路径；无 Shell、write／edit、测试和 MCP 工具。无人值守 ASK 直接拒绝；来源登记与档案写入属于 [09 入梦](09_anamnesis.md) 宿主逻辑。范围验证使用真实工具实现。
