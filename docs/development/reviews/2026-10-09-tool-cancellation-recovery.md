# 工具取消后的消息配对修复

日期：2026-10-09。状态：修复完成，离线验证通过；当前工作树已有其他未提交修改，本次保留它们。

## 现象与复现

用户取消 Shell 工具后再次提问，兼容接口返回 400：`insufficient tool messages following tool_calls message`。

离线复现命令：`.venv/Scripts/python.exe -m pytest tests/unit/test_tool_pairing_recovery.py -q --tb=short`。
首次结果：2 failed，0.54 秒。两条用例直接检查实际生成的 OpenAI 请求体：

- 第一批工具完成，第二批工具执行时取消，然后提交下一轮输入；第二批缺结果。
- 回放含多个调用、只有部分结果的中断记录，然后构造请求；未开始的调用缺结果。

## 修复思路与验收条件

内核已有补齐结果逻辑，但扫描整个历史后将所有缺失结果合并到历史最后一条 tool 消息，可能把新批次的结果写到旧批次。回放与 OpenAI 适配器的现有兜底仅处理“结果缺调用”，没有处理相反方向。普通 400 不可重试，原样重发也不会改变消息缺口。

按每条 assistant 的工具批次补齐结果：只检查紧随它的 tool 消息，在进入下一条用户或 assistant 消息之前补齐；同批已有结果保留，已有元数据保留。内核取消/失败收尾与会话回放共用这一规则。补出的结果明确表示中断、结果不可用，不重新执行工具、不伪造成功；原始会话文件保持不变。

验收：

- 第二批或跨轮取消后，实际下一轮请求中的每个调用都有紧随结果。
- 串行批次未开始的调用、回放尾部缺结果和跨批次重复调用 ID 都按所属批次恢复。
- 已有结果正文、成功状态和原始行号保持；重复修复不继续添加结果。
- 内核、调度、回放、上下文与两个厂商适配相关回归通过。

## 验证结果

代码状态：`codex/anamnesis` 分支的当前未提交工作树。修复前 2 条最小用例失败；修复后扩充为 6 条，全部通过。

| 验证 | 命令与结果 |
|---|---|
| 内核、调度、响应结束、存储、会话恢复、厂商协议 | `.venv/Scripts/python.exe -m pytest tests/unit/test_tool_pairing_recovery.py tests/unit/test_kernel_loop.py tests/unit/test_kernel_scheduler.py tests/unit/test_response_termination.py tests/unit/test_store.py tests/unit/test_resume_fidelity.py tests/unit/test_context_state_resume.py tests/contract/test_openai_compat.py tests/contract/test_anthropic.py -q --tb=short`：286 passed，84 subtests passed，7.38 秒 |
| 上下文、压缩、回滚、依赖与端到端 | `.venv/Scripts/python.exe -m pytest tests/unit/test_context.py tests/unit/test_context_cache.py tests/unit/test_compaction_turns.py tests/unit/test_compaction_rehydrate.py tests/unit/test_compaction_index.py tests/unit/test_compaction_stage3.py tests/unit/test_rewind.py tests/unit/test_imports.py tests/e2e -q --tb=short`：104 passed，303 subtests passed，9.00 秒 |
| 静态检查 | `.tools/ruff/bin/ruff.exe check src/logox/kernel/messages.py src/logox/kernel/loop.py src/logox/store/replay.py tests/unit/test_tool_pairing_recovery.py`：All checks passed |

本次没有添加临时调试日志或修改私人会话；原始报错的网络请求未回放，使用离线替身复现相同的请求体缺口。未重新运行全量测试。

真实终端 Ctrl+C 与真实服务尚未复验。下一步：重启 LOGOX 继续原会话，先用可中断的无副作用命令复验“取消 → 再提问”；也检查串行批次尚未开始的调用和重启后的会话恢复。

## 实现位置

- `kernel/messages.py::complete_tool_results` 按 assistant 的调用批次补齐结果，完整批次保持原消息；补入已有工具消息时保留元数据。
- `kernel/loop.py::_complete_history` 共用该规则，保持会话历史列表引用，避免把新批次结果塞到旧批次。
- `store/replay.py::reconstruct_messages` 在真实行号回填完成后补齐缺失结果，原始文件不变，合成消息不伪造行号。

请求适配器现有的“结果缺调用”兜底保持；“调用缺结果”在内核收尾和回放入口修复。普通 400 仍然不盲目重试。
