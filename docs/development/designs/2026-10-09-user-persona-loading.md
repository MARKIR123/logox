# 用户级人设接入（`~/.logox/LOGOX.md`）

日期：2026-10-09。状态：方向已由用户确认为 A（接通），**代码与测试已实施**；工作树未提交，尚需重启 logox 后做真实终端/模型复验。

## 1. 问题与证据

`~/.logox/LOGOX.md` 是一份完整的人格基底（6642 字节 / 84 行，含 `λόγος` 词源与「言出于理，行成于证，事毕于明」）。它当前**不进入任何一次请求**：

- `builder.py:322` 装配出的 system 为 3534 字符，`memory.sources` 只有 `AGENTS.md`；该文件的 5 个独有标记（`λόγος`、言出于理、匠人、哲学、logos）在装配结果中 0 命中。
- 系统提示的第一段来自 `app.py:1879`／`cli.py:645` 的硬编码串（303 字符：身份句 + cwd + 工具清单 + 4 条工具规范）。
- `paths.py:107` 的 `LogoxPaths.memory`（= `~/.logox/LOGOX.md`）**全仓无读取方**：只有定义、一个只用于展示的事件字段、一个测试夹具。
- `tools/sync_system_prompt.py` 声称"仓库母本 `docs/SYSTEM-PROMPT.md` → `~/.logox/LOGOX.md`"，但母本不存在：`--check` 退出码 2。该脚本未被 git 跟踪。

后果：编辑这份文件对 Logox 的行为**零影响**，而文档（`paths.py:19` 的「记忆文件（D21）」）与脚本都暗示它是生效的。

## 2. 目标与范围

目标：该文件存在时，成为系统提示的**第一段（人格层）**，位于项目规范之前、与自动背景记忆分开，并且"改了能生效"。

范围：

- 读取、注入、热重载、来源可见、失败边界。
- 不做：不回流仓库母本、不新增配置开关、不写回该文件、不改自动档案（`ANAMNESIS.md`）路径。

## 3. 当前行为与预期行为

| | 当前 | 预期 |
|---|---|---|
| 文件存在 | 完全忽略 | 作为 system 第一段注入 |
| 文件缺失 | 不受影响 | 不受影响（回落内置基础人设） |
| 文件改内容 | 无效果 | 下次启动或 `/reload` 生效 |
| 来源可见 | 不显示 | `/status`、会话开始分隔线、`SessionStart.memory_sources` 均含该路径 |
| 文件损坏/超大 | 无感知 | 整份跳过并给出原因，不半截注入 |

## 4. 方案与取舍

用户已裁定方向：**A 接通**（否决 B 删除机制、C 仅登记）。同时裁定**人设源就是该文件本身**，不设仓库母本，因此本方案不新增同步链。

| 分支 | 选择 | 代价 |
|---|---|---|
| 内置 `base_system` 的去留 | **保留**（运行环境 cwd/工具清单 + 4 条工具规范与人格无关） | 文件存在时出现两个身份句（"高效专业编码助手"与"哲学家气质的开发搭档"）；消除它需把 `base_system` 拆成"身份句 + 运行块"，留作后续 |
| 注入位置 | 第一段（人格 → 项目规范 → 背景记忆 → 技能索引 → 摘要契约） | 与既有"越具体越靠后"的拓扑一致 |
| 开关 | 不加：文件在 = 生效 | 少一个字段；不想用时删/改名文件 |
| `project_memory_enabled=False` 的影响 | 不影响人设（该开关语义是"项目规范"） | 关了项目记忆仍有人格层 |
| 归类 | 独立 `PersonaMemory`，不混进 `ProjectMemory` | 多一个类；换来职责清楚、`/reload` 与失败原因可分项报告 |

## 5. 影响的模块与接口

- 新增 `src/logox/context/persona.py`：`PersonaMemory(path)`，字段 `block` / `skipped` / `path`，方法 `load()`。
- `context/builder.py`：新增关键字参数 `persona_path: Path | None`；新增 `refresh_persona()` 与公开的 `memory_source_paths()`；`_assemble_system_prompt()` 首段注入人设。
- `app.py`：`build_runtime` 传 `persona_path=paths.memory`；`reload_resources` 增加「人设」项；`SessionStart.memory_sources` 改用 `memory_source_paths()`（此前只列项目规范，漏了背景记忆）。
- `cli.py`：`_context_params(bundle, paths)` 增加 `persona_path`，避免 `--chat` 与 TUI 两条装配漂移（F-07）。

## 6. 边界

| 情况 | 行为 |
|---|---|
| 文件不存在 | 正常：不注入、不报错、不列来源 |
| 内容全为空白 | 同"不存在"（不把空块拼进 system） |
| 大于 64KiB | 整份跳过，`skipped` 记录原因，回落内置人设 |
| 符号链接（含父目录） | 跳过并记录，沿用 `AnamesisMemory` 的口径 |
| UTF-8 解码失败 / 读取失败 | 跳过并记录，不影响会话 |
| cwd 恰为 home（该文件也会被项目记忆发现） | 只出现一次：此时由项目记忆承载，人设段不再重复注入 |
| 文件被改 | 启动或 `/reload` 后生效；同一次运行内不自动重读 |

## 7. 实施清单与验证

| 步骤 | 验证 |
|---|---|
| 1. `PersonaMemory` + 边界（缺失/空白/超大/符号链接/解码失败） | 新增单元用例，含先红后绿 |
| 2. builder 注入与顺序 | 断言 system 中人设段出现在项目规范之前；无文件时与改动前逐字相同 |
| 3. 来源可见（`memory_source_paths`） | 断言 `bundle.memory_sources` 含该路径 |
| 4. `app.py` / `cli.py` 两条装配接线 | 端到端 `build_runtime` 用例 + `_context_params` 用例 |
| 5. `/reload` 分项 | 改文件后 `refresh_persona` 取到新内容；报告项含人设 |
| 6. 文档改写 | `02_context.md`、`paths.py` 注释、STATUS/决策记录 |

## 8. 验收

- 该文件写入一句独有标记后，`build_runtime` 产出的 system 第一段含该标记；把文件改名后，system 与改动前逐字一致。
- 文件缺失、超大、损坏三种情况下会话仍可启动，且 `/reload` 报告能说出原因。
- `SessionStart.memory_sources` 与 `/status` 能列出该文件路径。

## 9. 未覆盖

- 不验证真实模型读人设后的行为差异（离线只能验证"文本确实发出去了"）。
- 不处理"内置身份句与人设并存"的措辞重复（见 §4 第一行）。
- `tools/sync_system_prompt.py` 的去留（未被 git 跟踪，删除会丢文件）另行确认。
