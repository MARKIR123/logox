# 内置工具与执行结果

核对日期：2026-10-09。运行时内置六个工具；配置声明与注册实现需要分别核对。

## 职责与协议

工具把模型请求连接到文件和子进程，返回可解释结果供下一请求纠错。调用顺序、权限、快照和外层收尾由 [内核](01_kernel.md) / Runtime 承担；工具自身不是完整安全沙箱。

`ToolSpec` 给出名称、参数模型、只读属性与 Schema；`ToolArgs` 拒绝多余字段。`run(args, ctx)` 接收已校验参数和工作目录/取消检查，返回 `ToolResult`。

`ToolResult.content` 给模型纯文本，display 给人类卡片中立展示数据，error 给分类失败，change_stat 给行变更统计。digest 只是短首行摘要，不能代替完整观察。工具层不输出 Rich 控件，不向模型混入 ANSI。

## 工具行为

| 名称与源码 | 当前机制 | 主要边界 |
|---|---|---|
| [read](../../src/logox/tools/fs_read.py) | 编码/BOM/二进制识别，行号和分页 | 输出限额不等于只读取一页文件 |
| [write](../../src/logox/tools/fs_write.py) | 创建或全量覆盖，返回 Diff 和统计 | 适合明确全量写入，不替代局部匹配 |
| [edit](../../src/logox/tools/fs_edit.py) | 匹配梯度、局部替换、歧义反馈、编码及换行保护 | 准备后原字节漂移则要求重读 |
| [glob](../../src/logox/tools/fs_glob.py) | 忽略目录、通配匹配、字母序和总数 | 精确总数与排序仍需全量扫描 |
| [grep](../../src/logox/tools/fs_grep.py) | 异步 ripgrep 子进程，结构化匹配和全局上限 | 需要 PATH 中 rg，Rust 正则和 auto 编码 |
| [shell](../../src/logox/tools/shell.py) | 自动后端、异步子进程、双流读取、超时/取消 | 可以运行用户代码，审批不等于 OS 隔离 |

`todo` 在默认配置声明中保留，但 `build_tool_registry()` 没有对应内置实现。不能将它列为已提供工具；运行时真实工具列表以注册表和 /status 为准。

read 默认最多 2000 行，单行最多 2000 字符；glob 默认展示 200、最多 1000 个匹配。它们在线程中做读取/扫描，取消通过标志协作，不能强制打断系统调用。

edit 在线程里只读原字节、匹配并生成候选；返回后检查取消和当前字节，再提交替换。write 的原文件统计准备也为只读；实际写入没有移到可在取消后继续写的后台线程。临时文件 replace 是单文件提交，不是跨文件事务。

## grep 范围与兼容性

调用采用参数列表，模式通过 --regexp 传入，目标由 -- 分隔；不拼 Shell。禁用用户 rg 配置、预处理和自动 PCRE2，包含普通隐藏/被 gitignore 忽略的文件，同时显式排除既有忽略目录和敏感范围。

默认 Rust 正则不支持前后查找和反向引用；auto 编码不承诺 GBK/Latin-1 逐文件回退，行边界为 LF/CRLF。错误返回明确参数或工具失败，不静默切回 Python 正则。

JSON 文件事件确认 binary_offset 后才输出该文件匹配；递归不跟随链接，结果校验搜索根范围。普通搜索过滤敏感路径，经权限批准的显式敏感目标按指定范围处理。

匹配默认 100、最多 500 条，为全局上限；到达上限即终止并回收进程。单个 JSON 事件可能包含超长行，因此不保证严格固定内存峰值。取消覆盖启动期间和运行中，清理进程及读取任务；缺少 rg 返回可行动错误。

## Shell 与权限

ShellArgs 的 `timeout_seconds` 当前默认 60 秒、范围 1–600；ShellConfig 另有字段，但不能把声明值直接当成本工具实际参数。后端实际探测与构造以源码为准。

stdout/stderr 并发收集，增量解码，保存有界首尾和完整字符计数；超过 8000 字符保留首尾各 2000。PowerShell 错误检查独立于展示截断，超时/取消尝试终止进程树。非交互环境变量不能保证所有程序不等输入。

正常 Scheduler 装配中全部工具进入权限决策，readonly 只影响调度。`requires_permission` 元数据不构成跳过审计的保证。插件和 MCP 仍需单独考虑真实副作用；入梦只使用受限的 read/glob/grep，没有 Shell、写入、测试或扩展入口。

## 接口与验证入口

共用协议见 [base.py](../../src/logox/tools/base.py)，注册入口见 [app.py](../../src/logox/app.py) 的 build_tool_registry。失败以 ToolResult.failure 携带分类、内容与详细原因，不能仅凭工具返回字符串判断成功。

入口：[文件读写与匹配](../../tests/unit/test_tool_fs_edit.py)、[只读准备](../../tests/unit/test_tool_preparation.py)、[glob](../../tests/unit/test_tool_fs_glob.py)、[ripgrep 生命周期](../../tests/unit/test_grep_ripgrep.py)、[Shell](../../tests/unit/test_tool_shell.py)、[工具契约](../../tests/unit/test_tools_base.py)。平台后端、真实进程树和特殊编码需按 [测试指南](../development/TESTING.md) 验证。
