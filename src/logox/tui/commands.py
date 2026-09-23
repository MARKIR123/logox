"""Slash 命令的解析与路由（D5 的一部分）。

为什么先做出来
--------------
用户要求"去掉提示行，改成输入指令查看快捷键"——那就必须先有命令路由。
本模块只做**解析与归类**（纯逻辑、无 Textual 依赖），真正的动作由 ``app.py`` 执行。

三类命令
--------
* ``ready``   —— 本里程碑已可用（``/help`` / ``/quit`` / ``/clear``）
* ``planned`` —— 已列入路线图但需接入内核（``/model``、``/compact`` …）
* ``unknown`` —— 不存在

诚实地把 ``planned`` 与 ``unknown`` 分开报，比笼统说一句"请稍后重试"有用得多：
用户能立刻知道"这个功能是没做，还是我打错了"。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

__all__ = [
    "ALIASES",
    "AVAILABLE_COMMANDS",
    "COMMAND_PREFIX",
    "PLANNED_COMMANDS",
    "ResolvedCommand",
    "command_catalog",
    "format_planned_notice",
    "format_unknown_notice",
    "is_command",
    "resolve",
]

COMMAND_PREFIX = "/"

#: **当前可用**的命令 → 一句话说明。
#:
#: 判断标准是"它做的事**现在真的会发生**"——没有实现的能力一律留在
#: :data:`PLANNED_COMMANDS` 里。宁可少列，也不假装成功（D47 的第三条要求）：
#: 用户能立刻知道"这个功能是没做，还是我打错了"。
AVAILABLE_COMMANDS: dict[str, str] = {
    "help": "显示帮助与完整键位表",
    "login": "选择供应商并输入 API Key（弹窗）",
    "model": "切换模型（弹窗选择，或 /model <名字>）",
    "theme": "切换主题（弹窗选择，或 /theme <名字>）",
    "effort": "切换思考档位（/effort off|low|medium|high|auto）",
    "status": "显示当前会话的环境与用量",
    "debug": "显示最近的事件流",
    "clear": "清空当前对话显示",
    "resume": "选择并恢复当前项目的历史会话（弹窗）",
    "new": "开启全新干净会话",
    "rewind": "回滚代码到指定历史检查点（弹窗）",
    "undo": "快捷撤销最近一轮的代码改动",
    "mcp": "查看 MCP 服务连接状态与已挂载工具",
    "skills": "查看与阅读大模型专业技能包（Skills）",
    "commands": "查看已配置的 L1 提示词模板命令",
    "exit": "退出 Logox",
    "compact": "立即压缩上下文（本地重算，不调模型）",
    "mode": "切换权限模式（/mode default|creative）",
    "summary": "查看当前会话演进脉络与用量大盘",
    "permissions": "查看与管理权限规则及物理沙箱（弹窗）",
}

#: 已列入路线图、但**还没有实现**的能力 → 说明。
#:
#: 保留它们是为了让用户能区分"这个功能没做"与"我打错字了"。
PLANNED_COMMANDS: dict[str, str] = {
    "files": "本次会话读写的文件清单（M5）",
    "memory": "项目记忆（M7）",
}

#: 别名 → 规范命令名。
#:
#: 为什么要有它：用户会打 ``/q`` 和 ``/quit``。让 :func:`resolve` 在这里就把它们
#: 归一，命令实现就只需要各写一份（否则每加一个别名就要在所有分派处补一个分支）。
ALIASES: dict[str, str] = {
    "q": "exit",
    "quit": "exit",
    "?": "help",
    "c": "resume",
    "continue": "resume",
    "skill": "skills",
    "cmd": "commands",
    "permission": "permissions",
    "perm": "permissions",
}

CommandState = Literal["ready", "planned", "unknown"]


@dataclass(frozen=True)
class ResolvedCommand:
    """解析结果。``name`` 已去掉前导斜杠、转小写、并把别名归一成规范名。"""

    name: str
    argument: str
    state: CommandState
    raw: str
    #: 用户实际敲的那个名字（别名时与 ``name`` 不同）
    typed: str = ""

    @property
    def is_ready(self) -> bool:
        return self.state == "ready"


def is_command(text: str) -> bool:
    """是否是一条斜杠命令（**行首**才是命令，避免误伤正文里的路径）。"""
    return text.lstrip().startswith(COMMAND_PREFIX) and len(text.lstrip()) > 1


def resolve(text: str) -> ResolvedCommand | None:
    """解析一条斜杠命令；不是命令则返回 ``None``。"""
    stripped = text.lstrip()
    if not is_command(stripped):
        return None
    body = stripped[len(COMMAND_PREFIX) :].strip()
    if not body:
        return None
    parts = body.split(maxsplit=1)
    typed = parts[0].lower()
    name = ALIASES.get(typed, typed)
    argument = parts[1].strip() if len(parts) > 1 else ""

    if name in AVAILABLE_COMMANDS:
        state: CommandState = "ready"
    elif name in PLANNED_COMMANDS:
        state = "planned"
    else:
        state = "unknown"
    return ResolvedCommand(name=name, argument=argument, state=state, raw=stripped, typed=typed)


def command_catalog() -> list[tuple[str, str, bool]]:
    """按字母序返回 ``(命令, 说明, 是否可用)``，供帮助与错误提示使用。"""
    rows = [(f"/{name}", desc, True) for name, desc in AVAILABLE_COMMANDS.items()]
    rows += [(f"/{name}", desc, False) for name, desc in PLANNED_COMMANDS.items()]
    return sorted(rows, key=lambda row: row[0])


def format_unknown_notice(command: ResolvedCommand) -> str:
    """未知命令的提示：**列出可用命令**，而不是只说"命令无效"。"""
    available = "、".join(row[0] for row in command_catalog() if row[2])
    return f"未知命令 /{command.typed}；当前可用：{available}（输入 /help 查看完整键位）"


def format_planned_notice(command: ResolvedCommand) -> str:
    return f"/{command.name}（{PLANNED_COMMANDS.get(command.name, '')}）还没有实现"
