"""配置与状态的 pydantic 模型（D26 / D30 / D42 / D44）。

三类模型
--------
* :class:`LogoxConfig` —— ``config.toml``（**用户手写，永久只读**）
* :class:`StateFile`   —— ``state.toml``（**程序生成，可写**）
* :class:`ThemeFile`   —— ``themes/*.toml``（用户手写，UI-SPEC §8）

字段级约束全部写在这里（pydantic 负责）；**跨字段语义约束**放在
``loader.py`` 的语义检查阶段，因为那里才能给出带精确字段路径的可定位报错。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

__all__ = [
    "BUILTIN_PROVIDERS",
    "DEFAULT_ENABLED_TOOLS",
    "STATUS_ITEM_KEYS",
    "ContextConfig",
    "HookEntry",
    "HooksConfig",
    "KernelConfig",
    "LastUsed",
    "LearnedPermissions",
    "LogoxConfig",
    "McpConfig",
    "McpServerConfig",
    "PermissionsConfig",
    "PluginsConfig",
    "ProviderConfig",
    "ProviderInstanceConfig",
    "SessionConfig",
    "ShellCache",
    "ShellConfig",
    "StateFile",
    "StatusItems",
    "ThemeFile",
    "ThemeGlyphs",
    "ThemePalette",
    "ThemeSyntax",
    "TimingFields",
    "ToolsConfig",
    "UiConfig",
    "UiState",
]

SCHEMA_VERSION = 1

_STRICT = ConfigDict(extra="forbid")
# 状态文件由程序生成：允许未知键（前/后版本的 Logox 都能读），pydantic 会忽略它们。
_STATE = ConfigDict(extra="ignore")

#: **未知 provider 报错文案里的兜底名单**。
#:
#: ⚠️ 真正的名单在 ``logox.providers.registry.BUILTIN_SPECS``——但 config 是 L2、
#: providers 是 L5，**不能反向 import**（R1）。而这里曾经只写了 3 个名字，
#: 漏掉了 ``deepseek`` 等内置预设，于是"配置里写 name = "deepseek""会被判成
#: **未定义的 Provider**（实测踩到，而且预设表里本来就有它）。
#: 修法：装配根通过 ``load(known_providers=...)`` 把真名单传进来，这里只留兜底；
#: 版本自检由 ``tests/unit/test_config.py`` 的一条断言守住（防止再次漂移）。
BUILTIN_PROVIDERS = ("openai-compatible", "openai_compat", "anthropic")
DEFAULT_ENABLED_TOOLS = ("read", "write", "edit", "glob", "grep", "shell", "todo")
STATUS_ITEM_KEYS = (
    "model",
    "context",
    "throughput",
    "cache",
    "tool",
    "turn",
    "usage",
    "timing",
    "permission",
    "git",
    "session",
)

_PLAINTEXT_KEY_MESSAGE = (
    "不允许在配置文件中写明文 api_key——它会被日志、终端与截图泄露。"
    '请改用 api_key_env = "<环境变量名>"，Logox 只从环境变量读取密钥。'
)


# --------------------------------------------------------------------------- #
# provider
# --------------------------------------------------------------------------- #


class ProviderConfig(BaseModel):
    """当前生效的 Provider 与模型（``[provider]``）。"""

    model_config = _STRICT

    name: str = "openai-compatible"
    model: str = ""
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    max_tokens: int = Field(default=16384, gt=0)
    request_timeout_s: float = Field(default=120.0, gt=0)
    thinking_effort: Literal["off", "low", "medium", "high", "auto"] = "auto"

    # 仅用于「拒绝」明文密钥：声明该字段是为了给出**明确的安全提示**，
    # 而不是让 pydantic 报一句含糊的 "extra field not permitted"。
    # exclude=True 保证它永远不会出现在 repr / model_dump / 日志里（E-6）。
    api_key: str | None = Field(default=None, exclude=True, repr=False)

    @model_validator(mode="after")
    def _forbid_plaintext_key(self) -> ProviderConfig:
        if self.api_key:
            # 注意：消息中**绝不回显**用户写的值。
            raise ValueError(_PLAINTEXT_KEY_MESSAGE)
        return self


class ProviderInstanceConfig(BaseModel):
    """用户自定义的 Provider 实例（``[providers.<name>]``）。"""

    model_config = _STRICT

    kind: Literal["openai_compat", "anthropic"] = "openai_compat"
    base_url: str = ""
    api_key_env: str = ""
    models: list[str] = Field(default_factory=list)
    api_key: str | None = Field(default=None, exclude=True, repr=False)

    @model_validator(mode="after")
    def _forbid_plaintext_key(self) -> ProviderInstanceConfig:
        if self.api_key:
            raise ValueError(_PLAINTEXT_KEY_MESSAGE)
        return self


# --------------------------------------------------------------------------- #
# 其余配置段
# --------------------------------------------------------------------------- #


class ContextConfig(BaseModel):
    model_config = _STRICT

    compact_threshold: float = Field(default=0.75, ge=0.1, le=0.95)
    keep_recent_turns: int = Field(default=6, ge=1)
    tool_result_keep_turns: int = Field(default=3, ge=0)
    max_tool_result_chars: int = Field(default=8000, ge=256)
    project_memory_enabled: bool = True
    reasoning_in_context: bool = False


class ShellConfig(BaseModel):
    model_config = _STRICT

    backend: Literal["auto", "gitbash", "wsl", "powershell"] = "auto"
    timeout_s: float = Field(default=120.0, gt=0)
    kill_tree: bool = True


class PermissionsConfig(BaseModel):
    model_config = _STRICT

    default_decision: Literal["allow", "deny", "ask"] = "ask"
    learn_scope: Literal["session", "project", "global"] = "project"


class ToolsConfig(BaseModel):
    model_config = _STRICT

    enabled: list[str] = Field(default_factory=lambda: list(DEFAULT_ENABLED_TOOLS))
    readonly_concurrency: int = Field(default=4, ge=1)


OBSERVABLE_HOOK_EVENTS = ("session_start", "post_tool_use", "pre_compact", "notification", "stop")


class HookEntry(BaseModel):
    """一条观察型钩子（D23）。

    ``event`` 只接受**观察型**事件名；拦截型（``pre_tool_use`` /
    ``user_prompt_submit``）会被 pydantic 直接拒绝，消息里说明 v1 不支持。
    """

    model_config = _STRICT

    event: Literal["session_start", "post_tool_use", "pre_compact", "notification", "stop"]
    command: str
    matcher: str = ""


class HooksConfig(BaseModel):
    model_config = _STRICT

    enabled: bool = True
    blocking: bool = True
    timeout_s: float = Field(default=5.0, gt=0)
    entries: list[HookEntry] = Field(default_factory=list)


class McpServerConfig(BaseModel):
    model_config = _STRICT

    name: str = ""
    transport: Literal["stdio", "http"] = "stdio"
    command: str = ""
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str | None = None
    url: str = ""
    enabled: bool = True
    timeout: float = Field(default=30.0, gt=0)
    startup_timeout_s: float = Field(default=20.0, gt=0)
    disabled: bool = False
    description: str = ""

    def is_enabled(self) -> bool:
        return self.enabled and not self.disabled

    def resolved_env(self) -> dict[str, str]:
        """展开环境变量中的 ${VAR} 或 $VAR。"""
        import os
        import re

        result = {}
        pattern = re.compile(r"\$\{([A-Za-z0-9_]+)\}|\$([A-Za-z0-9_]+)")
        for k, v in self.env.items():

            def _sub(match: re.Match[str]) -> str:
                var_name = match.group(1) or match.group(2)
                return os.environ.get(var_name, "")

            result[k] = pattern.sub(_sub, v)
        return result


class McpConfig(BaseModel):
    model_config = _STRICT

    enabled: bool = True
    default_timeout: float = Field(default=30.0, gt=0)
    servers: dict[str, McpServerConfig] | list[McpServerConfig] = Field(default_factory=dict)

    def get_servers(self) -> dict[str, McpServerConfig]:
        """规范化返回 {name: McpServerConfig} 字典。"""
        if isinstance(self.servers, list):
            res: dict[str, McpServerConfig] = {}
            for s in self.servers:
                server_name = s.name or "unknown"
                res[server_name] = s
            return res
        res = {}
        for name, s in self.servers.items():
            if not s.name:
                s = s.model_copy(update={"name": name})
            res[name] = s
        return res


class StatusItems(BaseModel):
    """状态栏逐项开关（D16 / D39 / D42）。

    默认开启 **仅 4 项**——``model``（含思考档位）/ ``context`` / ``throughput``
    / ``cache``。其余全部默认关（D42）。
    """

    model_config = _STRICT

    model: bool = True
    context: bool = True
    throughput: bool = True
    cache: bool = True
    tool: bool = False
    turn: bool = False
    usage: bool = False
    timing: bool = False
    permission: bool = False
    git: bool = False
    session: bool = False

    def enabled_keys(self) -> list[str]:
        return [key for key in STATUS_ITEM_KEYS if getattr(self, key)]


class TimingFields(BaseModel):
    """``timing`` 项的字段级开关（D39，仅当 ``status_items.timing`` 为真时生效）。"""

    model_config = _STRICT

    total: bool = True
    llm: bool = True
    tool: bool = True


class UiConfig(BaseModel):
    model_config = _STRICT

    theme: str = "logox-dark"
    layout: Literal["auto", "dual", "single"] = "auto"
    sidebar_width: int = Field(default=32, ge=24, le=48)
    diff_context_lines: int = Field(default=3, ge=0, le=10)
    stream_fps: int = Field(default=10, ge=5, le=30)
    show_reasoning: Literal["collapsed", "expanded", "hidden"] = "collapsed"
    icon_set: Literal["unicode", "ascii", "nerd"] = "unicode"
    animations: bool = True
    status_items: StatusItems = Field(default_factory=StatusItems)
    timing_fields: TimingFields = Field(default_factory=TimingFields)


class SessionConfig(BaseModel):
    model_config = _STRICT

    store_dir: str = ""
    log_events: bool = True
    log_redact: bool = True


class PluginsConfig(BaseModel):
    model_config = _STRICT

    enabled: bool = True
    paths: list[str] = Field(default_factory=list)


class KernelConfig(BaseModel):
    """内核循环的可调参数（``[kernel]``，M3 / D27 / D52）。

    这些值都直接影响"一次对话会花多久、会不会卡住"，因此**必须可配**：
    默认值只是"大多数情况下合适"，而不是"永远正确"。
    """

    model_config = _STRICT

    #: 一个回合内最多允许几次"模型请求工具 → 执行 → 再请求"。
    #: 到达上限会**明确报错**而不是静默停止（静默停止会让用户以为模型不说话了）。
    max_iterations: int = Field(default=50, ge=1, le=200)
    #: 可重试错误的最大重试次数（不含首次尝试）。
    max_retries: int = Field(default=3, ge=0, le=10)
    #: 首次退避时长；此后指数增长。
    retry_base_s: float = Field(default=0.5, gt=0, le=60)
    #: 退避上限，同时也是厂商 ``Retry-After`` 的裁剪上限。
    retry_max_s: float = Field(default=30.0, gt=0, le=600)
    #: 只读工具批的并发度（D27）。
    concurrency: int = Field(default=4, ge=1, le=32)


class LogoxConfig(BaseModel):
    """``config.toml`` 的完整模型。"""

    model_config = _STRICT

    schema_version: int = SCHEMA_VERSION
    provider: ProviderConfig = Field(default_factory=ProviderConfig)
    providers: dict[str, ProviderInstanceConfig] = Field(default_factory=dict)
    kernel: KernelConfig = Field(default_factory=KernelConfig)
    context: ContextConfig = Field(default_factory=ContextConfig)
    shell: ShellConfig = Field(default_factory=ShellConfig)
    permissions: PermissionsConfig = Field(default_factory=PermissionsConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    hooks: HooksConfig = Field(default_factory=HooksConfig)
    mcp: McpConfig = Field(default_factory=McpConfig)
    ui: UiConfig = Field(default_factory=UiConfig)
    session: SessionConfig = Field(default_factory=SessionConfig)
    plugins: PluginsConfig = Field(default_factory=PluginsConfig)


# --------------------------------------------------------------------------- #
# state.toml（程序独占，可写）
# --------------------------------------------------------------------------- #


class LastUsed(BaseModel):
    """「上次使用」的运行时选择（``/model``、``/theme``、``/effort`` 会写入这里）。"""

    model_config = _STATE

    provider: str | None = None
    model: str | None = None
    theme: str | None = None
    effort: str | None = None
    #: **从端点抓到的真实模型列表**（`provider_name -> [model_id, ...]`）。
    #:
    #: 为什么要缓存它：抓取要发一次网络请求（本机实测很慢），而 `/model` 是个
    #: 点开就要立刻出结果的弹窗。缓存之后：登录时抓一次，之后每次打开选择器
    #: **零网络、瞬时**。它也可能过期（厂商上新），因此界面会说明来源与时间。
    models: dict[str, list[str]] = Field(default_factory=dict)
    #: 上次抓取成功的时间戳（展示 "3 分钟前抓取" 用；``None`` = 从未抓过）
    models_fetched_at: float | None = None


class ShellCache(BaseModel):
    """Shell 后端探测结果缓存（D19：避免每次启动重新探测）。"""

    model_config = _STATE

    backend: str | None = None
    executable: str | None = None
    version: str | None = None
    detected_at: float | None = None


class LearnedPermissions(BaseModel):
    """本作用域学习到的权限规则与模式（D10 / D130）。"""

    model_config = _STATE

    mode: str = "default"
    allow: list[str] = Field(default_factory=list)
    deny: list[str] = Field(default_factory=list)


class UiState(BaseModel):
    model_config = _STATE

    status_items: dict[str, bool] = Field(default_factory=dict)
    focus_mode: bool = False


class StateFile(BaseModel):
    """``state.toml`` 的完整模型。

    **只存程序生成的数据**；任何用户手写的配置都不在这里（D30 读写分离）。
    """

    model_config = _STATE

    schema_version: int = SCHEMA_VERSION
    last: LastUsed = Field(default_factory=LastUsed)
    shell: ShellCache = Field(default_factory=ShellCache)
    ui: UiState = Field(default_factory=UiState)
    permissions: LearnedPermissions = Field(default_factory=LearnedPermissions)


# --------------------------------------------------------------------------- #
# themes/*.toml（UI-SPEC §8）
# --------------------------------------------------------------------------- #

_HEX_COLOR = r"^#[0-9a-fA-F]{6}$"

#: 全部语义 token（顺序即文档顺序）。
#:
#: 前 18 个是 M1.5 就定下的**通用** token；后面那些是 **D79 按 Pi agent 的视觉语言
#: 补的语义 token**（读 `@mariozechner/pi-coding-agent` 的 `theme/dark.json` 得来）。
#:
#: 为什么必须补：原来的 18 个里**没有任何一个背景色能区分内容类型**，
#: 于是用户消息、助手回答、工具卡片全都画在同一个底色上——这就是"界面平淡"的
#: 根本原因。Pi 用 5 种语义底色把不同类型的内容**分层**，这是它看起来"有设计"的关键。
PALETTE_TOKENS = (
    # -- 通用（M1.5 既有） --
    "bg_base",
    "bg_raised",
    "bg_overlay",
    "overlay_scrim",
    "text_primary",
    "text_muted",
    "text_faint",
    "accent",
    "success",
    "warning",
    "danger",
    "info",
    "border_subtle",
    "border_strong",
    "diff_add_fg",
    "diff_add_bg",
    "diff_del_fg",
    "diff_del_bg",
    # -- 内容分层底色（D79：对齐 Pi 的 userMessageBg / tool*Bg / customMessageBg）--
    "user_message_bg",
    "user_message_fg",
    "custom_message_bg",
    "custom_message_fg",
    "custom_message_label",
    "tool_pending_bg",
    "tool_success_bg",
    "tool_error_bg",
    "tool_title_fg",
    "tool_output_fg",
    "selected_bg",
    # -- 思考块（D79：Pi 用颜色表达思考档位）--
    "thinking_text",
    "thinking_off",
    "thinking_low",
    "thinking_medium",
    "thinking_high",
    # -- Markdown（D79：对齐 Pi 的 md* 全套）--
    "md_heading",
    "md_link",
    "md_link_url",
    "md_code",
    "md_code_block",
    "md_code_block_border",
    "md_quote",
    "md_quote_border",
    "md_hr",
    "md_list_bullet",
    # -- Markdown 表格（D115）--
    "md_table_border",
    # -- 代码语法高亮（D79：对齐 Pi 的 syntax* 全套）--
    "syntax_comment",
    "syntax_keyword",
    "syntax_function",
    "syntax_variable",
    "syntax_string",
    "syntax_number",
    "syntax_type",
    "syntax_operator",
    "syntax_punctuation",
)


#: D79 新增 token 的**默认值**（取自 Pi 的 `theme/dark.json`，映射到 Logox 的深色底）。
#:
#: 为什么要给默认值而不是"必填"：加 token 的那一天，**用户自己写的主题文件里没有它们**。
#: 若设成必填，所有人的自定义主题会立刻加载失败（E-12 是"缺失即拒绝加载"）。
#: 语义上 18 个基础 token 是"设计的一个整体"，而 D79 这些是**可以逐个覆盖的增强**——
#: 给默认值既保住了旧主题，也让新主题能只写差异。
#:
#: 三层底色刻意压得**很轻**（#262636 / #282832 / #283228 / #3c2828）：
#: 目的是"分区"而不是"抢眼"。Pi 的取值同样克制——底色一亮，正文就不好读了。
_PI_ALIGNED_DEFAULTS: dict[str, str] = {
    # 内容分层底色
    "user_message_bg": "#343541",
    "user_message_fg": "#e6e6e6",
    "custom_message_bg": "#2d2838",
    "custom_message_fg": "#e6e6e6",
    "custom_message_label": "#9575cd",
    "tool_pending_bg": "#282832",
    "tool_success_bg": "#283228",
    "tool_error_bg": "#3c2828",
    "tool_title_fg": "#e6e6e6",
    "tool_output_fg": "#a6adc8",
    "selected_bg": "#3a3a4a",
    # 思考块：颜色即"档位"（越亮越高），对齐 Pi 的 thinkingOff→Xhigh 梯度
    "thinking_text": "#a6adc8",
    "thinking_off": "#505050",
    "thinking_low": "#5f87af",
    "thinking_medium": "#81a2be",
    "thinking_high": "#b294bb",
    # Markdown
    "md_heading": "#f0c674",
    "md_link": "#81a2be",
    "md_link_url": "#6c7086",
    "md_code": "#8abeb7",
    "md_code_block": "#b5bd68",
    "md_code_block_border": "#808080",
    "md_quote": "#a6adc8",
    "md_quote_border": "#6c7086",
    "md_hr": "#6c7086",
    "md_list_bullet": "#8abeb7",
    "md_table_border": "#808080",
    # 语法高亮
    "syntax_comment": "#6a9955",
    "syntax_keyword": "#569cd6",
    "syntax_function": "#dcdcaa",
    "syntax_variable": "#9cdcfe",
    "syntax_string": "#ce9178",
    "syntax_number": "#b5cea8",
    "syntax_type": "#4ec9b0",
    "syntax_operator": "#d4d4d4",
    "syntax_punctuation": "#d4d4d4",
}


class ThemePalette(BaseModel):
    """§3.1 的全部语义 token，**缺一不可**（E-12：缺失即拒绝加载，不做静默兜底）。"""

    model_config = _STRICT

    bg_base: str = Field(pattern=_HEX_COLOR)
    bg_raised: str = Field(pattern=_HEX_COLOR)
    bg_overlay: str = Field(pattern=_HEX_COLOR)
    overlay_scrim: str = Field(pattern=_HEX_COLOR)
    text_primary: str = Field(pattern=_HEX_COLOR)
    text_muted: str = Field(pattern=_HEX_COLOR)
    text_faint: str = Field(pattern=_HEX_COLOR)
    accent: str = Field(pattern=_HEX_COLOR)
    success: str = Field(pattern=_HEX_COLOR)
    warning: str = Field(pattern=_HEX_COLOR)
    danger: str = Field(pattern=_HEX_COLOR)
    info: str = Field(pattern=_HEX_COLOR)
    border_subtle: str = Field(pattern=_HEX_COLOR)
    border_strong: str = Field(pattern=_HEX_COLOR)
    diff_add_fg: str = Field(pattern=_HEX_COLOR)
    diff_add_bg: str = Field(pattern=_HEX_COLOR)
    diff_del_fg: str = Field(pattern=_HEX_COLOR)
    diff_del_bg: str = Field(pattern=_HEX_COLOR)

    # -- D79：对齐 Pi 视觉语言的语义 token（都可逐个覆盖，默认值见上表）--
    user_message_bg: str = Field(default=_PI_ALIGNED_DEFAULTS["user_message_bg"], pattern=_HEX_COLOR)
    user_message_fg: str = Field(default=_PI_ALIGNED_DEFAULTS["user_message_fg"], pattern=_HEX_COLOR)
    custom_message_bg: str = Field(default=_PI_ALIGNED_DEFAULTS["custom_message_bg"], pattern=_HEX_COLOR)
    custom_message_fg: str = Field(default=_PI_ALIGNED_DEFAULTS["custom_message_fg"], pattern=_HEX_COLOR)
    custom_message_label: str = Field(
        default=_PI_ALIGNED_DEFAULTS["custom_message_label"], pattern=_HEX_COLOR
    )
    tool_pending_bg: str = Field(default=_PI_ALIGNED_DEFAULTS["tool_pending_bg"], pattern=_HEX_COLOR)
    tool_success_bg: str = Field(default=_PI_ALIGNED_DEFAULTS["tool_success_bg"], pattern=_HEX_COLOR)
    tool_error_bg: str = Field(default=_PI_ALIGNED_DEFAULTS["tool_error_bg"], pattern=_HEX_COLOR)
    tool_title_fg: str = Field(default=_PI_ALIGNED_DEFAULTS["tool_title_fg"], pattern=_HEX_COLOR)
    tool_output_fg: str = Field(default=_PI_ALIGNED_DEFAULTS["tool_output_fg"], pattern=_HEX_COLOR)
    selected_bg: str = Field(default=_PI_ALIGNED_DEFAULTS["selected_bg"], pattern=_HEX_COLOR)
    thinking_text: str = Field(default=_PI_ALIGNED_DEFAULTS["thinking_text"], pattern=_HEX_COLOR)
    thinking_off: str = Field(default=_PI_ALIGNED_DEFAULTS["thinking_off"], pattern=_HEX_COLOR)
    thinking_low: str = Field(default=_PI_ALIGNED_DEFAULTS["thinking_low"], pattern=_HEX_COLOR)
    thinking_medium: str = Field(default=_PI_ALIGNED_DEFAULTS["thinking_medium"], pattern=_HEX_COLOR)
    thinking_high: str = Field(default=_PI_ALIGNED_DEFAULTS["thinking_high"], pattern=_HEX_COLOR)
    md_heading: str = Field(default=_PI_ALIGNED_DEFAULTS["md_heading"], pattern=_HEX_COLOR)
    md_link: str = Field(default=_PI_ALIGNED_DEFAULTS["md_link"], pattern=_HEX_COLOR)
    md_link_url: str = Field(default=_PI_ALIGNED_DEFAULTS["md_link_url"], pattern=_HEX_COLOR)
    md_code: str = Field(default=_PI_ALIGNED_DEFAULTS["md_code"], pattern=_HEX_COLOR)
    md_code_block: str = Field(default=_PI_ALIGNED_DEFAULTS["md_code_block"], pattern=_HEX_COLOR)
    md_code_block_border: str = Field(
        default=_PI_ALIGNED_DEFAULTS["md_code_block_border"], pattern=_HEX_COLOR
    )
    md_quote: str = Field(default=_PI_ALIGNED_DEFAULTS["md_quote"], pattern=_HEX_COLOR)
    md_quote_border: str = Field(default=_PI_ALIGNED_DEFAULTS["md_quote_border"], pattern=_HEX_COLOR)
    md_hr: str = Field(default=_PI_ALIGNED_DEFAULTS["md_hr"], pattern=_HEX_COLOR)
    md_list_bullet: str = Field(default=_PI_ALIGNED_DEFAULTS["md_list_bullet"], pattern=_HEX_COLOR)
    md_table_border: str = Field(default=_PI_ALIGNED_DEFAULTS["md_table_border"], pattern=_HEX_COLOR)
    syntax_comment: str = Field(default=_PI_ALIGNED_DEFAULTS["syntax_comment"], pattern=_HEX_COLOR)
    syntax_keyword: str = Field(default=_PI_ALIGNED_DEFAULTS["syntax_keyword"], pattern=_HEX_COLOR)
    syntax_function: str = Field(default=_PI_ALIGNED_DEFAULTS["syntax_function"], pattern=_HEX_COLOR)
    syntax_variable: str = Field(default=_PI_ALIGNED_DEFAULTS["syntax_variable"], pattern=_HEX_COLOR)
    syntax_string: str = Field(default=_PI_ALIGNED_DEFAULTS["syntax_string"], pattern=_HEX_COLOR)
    syntax_number: str = Field(default=_PI_ALIGNED_DEFAULTS["syntax_number"], pattern=_HEX_COLOR)
    syntax_type: str = Field(default=_PI_ALIGNED_DEFAULTS["syntax_type"], pattern=_HEX_COLOR)
    syntax_operator: str = Field(default=_PI_ALIGNED_DEFAULTS["syntax_operator"], pattern=_HEX_COLOR)
    syntax_punctuation: str = Field(
        default=_PI_ALIGNED_DEFAULTS["syntax_punctuation"], pattern=_HEX_COLOR
    )

    def as_dict(self) -> dict[str, str]:
        return {token: getattr(self, token) for token in PALETTE_TOKENS}


class ThemeSyntax(BaseModel):
    model_config = _STRICT

    theme: str = "monokai"


class ThemeGlyphs(BaseModel):
    model_config = _STRICT

    set: Literal["unicode", "ascii", "nerd"] = "unicode"
    running: str = "⏺"
    success: str = "✓"
    error: str = "✗"
    denied: str = "⊘"
    cancelled: str = "◼"
    thinking: str = "▸"
    user: str = "❯"
    assistant: str = "◆"
    ellipsis: str = "…"


class ThemeFile(BaseModel):
    """主题文件模型（``~/.logox/themes/<name>.toml`` 或内置主题）。"""

    model_config = _STRICT

    schema_version: int = SCHEMA_VERSION
    name: str
    label: str = ""
    variant: Literal["dark", "light", "high_contrast"] = "dark"
    palette: ThemePalette
    syntax: ThemeSyntax = Field(default_factory=ThemeSyntax)
    glyphs: ThemeGlyphs = Field(default_factory=ThemeGlyphs)

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("主题 name 不能为空")
        return value
