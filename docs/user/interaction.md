# 命令、输入与浏览

核对日期：2026-10-09。内置命令见 [路由声明](../../src/logox/tui/commands.py)，行为以 [执行器](../../src/logox/tui/render/commands.py) 和实际按键处理为准。

## 输入与快捷键

| 操作 | 按键 |
|---|---|
| 发送 | Enter；补全列表存在时先接受候选再发送 |
| 换行 | Ctrl+J / Ctrl+Enter；或行尾反斜杠再 Enter |
| 条件换行 | Shift+Enter，需要终端传修饰位，部分终端与 Enter 相同 |
| 光标与编辑 | 方向/Home/End、Ctrl+A/E、Ctrl+W、Ctrl+U/K |
| 工具和 Diff | Ctrl+O 展开/折叠 |
| 普通思考和入梦 | Ctrl+T 展开/折叠 |
| 轨道装饰 | Ctrl+B 显示/隐藏 |
| 关闭/取消 | Esc 先处理浮层，审批中表示拒绝；前台忙时无浮层则中断 |
| 中断/退出 | Ctrl+C 忙时中断，空闲两次退出；无浮层 Ctrl+D 退出 |
| 浮层选择 | 方向、Enter、Esc；可筛选列表接受输入，长内容可翻页 |

粘贴作为一次插入，不逐行提交。忙时仍可编辑，但当前没有可靠的待发送自动队列；再次提交可能被内核拒绝。需要新任务时等待当前轮结束，或明确中断后再发送。

## 内置命令

| 目的 | 命令 |
|---|---|
| 帮助与退出 | /help、/exit；/q 和 /quit 是退出别名 |
| 登录/模型/思考 | /login [连接名] [--reset]、/model [名称]、/model refresh、/effort off\|low\|medium\|high\|auto |
| 查看事实 | /status、/summary、/debug |
| 会话 | /new、/resume [序号]、/clear |
| 文件回退 | /rewind [序号]、/undo |
| 上下文 | /compact |
| 配色与资源 | /theme [名称]、/reload |
| 权限 | /mode default\|creative、/permissions |
| 扩展目录 | /mcp、/skills、/commands |
| 入梦 | /anamnesis 及 stop/status/history/report/trace/model 子命令 |

方括号表示可选参数。/files 与 /memory 尚未实现；自定义模板命令由配置目录另行提供。

/clear 只清当前显示，不清模型原始历史，不等于 /new，也不清终端已保存回滚。/compact 共用分层压缩：复用摘要阶段不调用模型，极小窗口仍超预算时可调用明确本地模型；帮助中的“不调模型”描述不能覆盖该条件路径。

/reload 不是代码热更新，前台整轮忙时拒绝。/summary 展示回合演进及用量，摘要来源可为模型末行、补写或本地兜底；不能仅凭摘要“完成”判断任务正确。

## 两种浏览方式

主屏由终端处理滚轮、选择和复制，例如 Windows Terminal 的 Ctrl+Shift+C。启动清理之前的终端回滚，正常刷新保留本次会话；已滚出屏的旧卡片保留当时状态，最新报告可查询。

全屏由应用维护视口和滚动锚点，底部才跟随新消息。拖选释放会尝试复制纯净文本并显示提示；滚动条拖动、卡片点击与选择是不同动作。复制取决于平台剪贴板可用性，不能仅凭提示证明剪贴板成功。

工具卡片当前状态可展开；重开默认折叠。Ctrl+T 与 Ctrl+O 不混用。模型没有返回思考时不会凭空显示；入梦完整内容见 [Anamnesis](anamnesis.md)。

## 排查卡顿或错位

提供终端和字体、窗口尺寸、主屏/全屏、历史规模、触发操作、前台是否输出/执行工具、重复步骤和截图。分别描述按键延迟、发送首帧、动画、流式节奏和滚动落点，它们可能有不同原因。

模拟终端通过不代表现场流畅；目前仍需真实终端长历史、缩放和展开检查。原理见 [TUI 模块](../modules/03_tui.md)，当前未验证事项见 [STATUS](../development/STATUS.md)。
