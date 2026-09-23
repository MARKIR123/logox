"""多级配置加载、深度合并、来源溯源与可定位报错（D30 / D44）。

合并优先级（低 → 高，D44 §5.1）
-------------------------------
1. 内置默认值（``schema.py`` 的字段默认值）
2. 全局 ``~/.logox/config.toml``
3. 项目级 ``<dir>/.logox/config.toml``——**从 cwd 向上逐级，远 → 近（近者覆盖）**
4. ``state.toml`` 中的「上次使用」字段（仅覆盖它拥有的键：provider/model/theme/effort）
5. 环境变量 ``LOGOX__<PATH>``（双下划线分隔，如 ``LOGOX__PROVIDER__MODEL``）
6. 命令行参数

合并语义（必须精确，否则行为不可预测）
--------------------------------------
* 嵌套 table → **深度合并**
* 标量 → 后者整体覆盖
* 数组与数组表 → **整体替换，绝不拼接**（避免"删不掉旧元素"的经典陷阱）

失败策略（D44）
---------------
**配置错误一律不阻断启动**：出错字段回退为默认值，全部问题以
:class:`ConfigIssue` 返回并在启动摘要块中醒目列出。``strict=True``
（``--strict-config``）时改为抛 :class:`~logox.errors.ConfigValidationError`。
"""

from __future__ import annotations

import copy
import os
import re
import tomllib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from logox.config.schema import (
    BUILTIN_PROVIDERS,
    DEFAULT_ENABLED_TOOLS,
    SCHEMA_VERSION,
    LogoxConfig,
    StateFile,
)
from logox.config.theme import BUILTIN_THEMES, discover_themes
from logox.errors import ConfigValidationError
from logox.paths import LogoxPaths, ProjectPaths, discover_project_chain, nearest_project

__all__ = [
    "ConfigBundle",
    "ConfigIssue",
    "ConfigSource",
    "load",
    "render_issues",
]

ENV_PREFIX = "LOGOX__"
_MAX_STRIP_ROUNDS = 40

_INTERCEPTOR_HOOK_EVENTS = ("pre_tool_use", "user_prompt_submit", "pre_tool_use_deny")
_LINE_COLUMN_RE = re.compile(r"at line (\d+), column (\d+)")

Scope = Literal["defaults", "global", "project", "state", "env", "cli"]


class ConfigSource(BaseModel):
    """一个配置来源（用于 ``origin`` 溯源与错误归属）。"""

    model_config = ConfigDict(frozen=True)

    path: Path
    scope: Scope
    depth: int = 0

    @property
    def label(self) -> str:
        return f"{self.scope}:{self.path}"


class ConfigIssue(BaseModel):
    """一条配置问题（**可定位报错**：文件 + 字段 + 原因）。"""

    model_config = ConfigDict(frozen=True)

    path: Path
    message: str
    field: str | None = None
    severity: Literal["error", "warning"] = "error"
    line: int | None = None
    column: int | None = None


class ConfigBundle(BaseModel):
    """加载结果：配置本体 + 来源清单 + 字段溯源 + 问题清单。"""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    config: LogoxConfig
    sources: list[ConfigSource] = Field(default_factory=list)
    origin: dict[str, ConfigSource] = Field(default_factory=dict)
    issues: list[ConfigIssue] = Field(default_factory=list)

    @property
    def errors(self) -> list[ConfigIssue]:
        return [issue for issue in self.issues if issue.severity == "error"]

    @property
    def warnings(self) -> list[ConfigIssue]:
        return [issue for issue in self.issues if issue.severity == "warning"]

    @property
    def has_errors(self) -> bool:
        return any(issue.severity == "error" for issue in self.issues)

    def origin_of(self, field: str) -> str | None:
        """回答「这个值是从哪来的」——供 ``/debug`` 面板与用户排查使用。"""
        source = self.origin.get(field)
        return source.label if source is not None else None


# --------------------------------------------------------------------------- #
# 对外入口
# --------------------------------------------------------------------------- #


def load(
    cwd: Path,
    *,
    cli_overrides: Mapping[str, Any] | None = None,
    env: Mapping[str, str] | None = None,
    strict: bool = False,
    paths: LogoxPaths | None = None,
    project_chain: Sequence[ProjectPaths] | None = None,
    state: StateFile | None = None,
    available_themes: Sequence[str] | None = None,
    known_providers: Sequence[str] | None = None,
) -> ConfigBundle:
    """加载并合并全部来源，产出经过校验的 :class:`ConfigBundle`。

    :param strict: ``True`` 时只要存在任何 ``error`` 级问题就抛异常
        （``--strict-config``，供脚本化使用）。
    :param known_providers: 实际可用的 provider 名字（由装配根从
        ``providers.registry.BUILTIN_SPECS`` 传入）。**不传则退回
        ``schema.BUILTIN_PROVIDERS`` 那份兜底名单**——那份名单漏过一次内置预设
        （`deepseek` 被判成"未定义"），所以正式路径务必显式传。
    """
    cwd = Path(cwd)
    paths = paths or LogoxPaths.default()
    env = env if env is not None else os.environ
    chain = list(project_chain) if project_chain is not None else discover_project_chain(cwd)

    fallback = chain[-1].config if chain else paths.config
    merged: dict[str, Any] = {}
    origin: dict[str, ConfigSource] = {}
    issues: list[ConfigIssue] = []
    sources: list[ConfigSource] = [ConfigSource(path=Path("<defaults>"), scope="defaults")]

    # ② 全局
    global_source = ConfigSource(path=paths.config, scope="global")
    _merge_file(merged, origin, issues, global_source, fallback)
    if global_source.path.is_file():
        sources.append(global_source)

    # ③ 项目级：远 → 近（近者覆盖）
    for index, project in enumerate(chain):
        source = ConfigSource(path=project.config, scope="project", depth=len(chain) - 1 - index)
        _merge_file(merged, origin, issues, source, fallback)
        if source.path.is_file():
            sources.append(source)

    # ④ state.toml 的「上次使用」（仅覆盖它拥有的键）
    effective_state = state if state is not None else _read_state_quietly(paths, cwd, issues)
    if effective_state is not None:
        state_source = ConfigSource(path=paths.state, scope="state")
        # ⚠️ 只有**真的提供了值**才登记来源（F-21）：空的 state.toml 不是"一个配置来源"，
        #    把它列进去会让"无配置 → 只有 defaults"这类断言无端失败。
        if _apply_state_overlay(merged, origin, effective_state, state_source):
            sources.append(state_source)

    # ⑤ 环境变量
    env_overrides = _env_overrides(env)
    if env_overrides:
        env_source = ConfigSource(path=Path("<env>"), scope="env")
        _merge(merged, env_overrides, origin, (), env_source)
        sources.append(env_source)

    # ⑥ 命令行
    if cli_overrides:
        cli_source = ConfigSource(path=Path("<cli>"), scope="cli")
        _merge(merged, cli_overrides, origin, (), cli_source)
        sources.append(cli_source)

    # 校验前先摘掉明文密钥（既不泄露，也避免整段 provider 被回退）
    _strip_plaintext_keys(merged, origin, issues, fallback)

    config = _validate(merged, origin, issues, fallback)
    _semantic_checks(
        config, origin, issues, fallback, paths, cwd, available_themes, known_providers
    )

    bundle = ConfigBundle(config=config, sources=sources, origin=origin, issues=issues)
    if strict and bundle.issues:
        # D44 ①：--strict-config 在存在**任何** issue（含 warning）时以非 0 退出码结束。
        raise ConfigValidationError(fallback, list(bundle.issues))
    return bundle


def render_issues(issues: Sequence[ConfigIssue]) -> str:
    """把问题清单渲染成用户看到的那段文字（MODULE_config §5.2 的格式）。"""
    if not issues:
        return ""
    lines: list[str] = []
    current: Path | None = None
    for issue in issues:
        if issue.path != current:
            current = issue.path
            lines.append(f"{'配置校验失败' if issue.severity == 'error' else '配置提示'}：{current}")
        location = f"[{issue.field}]" if issue.field else "<root>"
        position = ""
        if issue.line is not None:
            position = f"（第 {issue.line} 行" + (f"，第 {issue.column} 列）" if issue.column else "）")
        lines.append(f"  {location:<24} {issue.message}{position}")
    if any(issue.severity == "error" for issue in issues):
        lines.append("")
        lines.append("本次启动将忽略上述字段并使用内置默认值继续运行；修正后重启生效。")
        lines.append("（如需失败即退出，请加 --strict-config）")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 来源读取
# --------------------------------------------------------------------------- #


def _merge_file(
    merged: dict[str, Any],
    origin: dict[str, ConfigSource],
    issues: list[ConfigIssue],
    source: ConfigSource,
    fallback: Path,
) -> None:
    data = _parse_file(source.path, issues)
    if data is None:
        return
    _merge(merged, data, origin, (), source)


def _parse_file(path: Path, issues: list[ConfigIssue]) -> dict[str, Any] | None:
    """读取并解析一个 TOML 文件。缺失不是错误（E-1）。"""
    if not path.is_file():
        return None

    try:
        raw = path.read_bytes()
    except OSError as exc:
        issues.append(ConfigIssue(path=path, message=f"无法读取文件：{exc}"))
        return None

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        issues.append(ConfigIssue(path=path, message=f"文件不是有效的 UTF-8：{exc}"))
        return None

    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        line, column = _line_column(str(exc))
        issues.append(
            ConfigIssue(path=path, message=f"TOML 语法错误：{exc}", line=line, column=column)
        )
        return None

    if not isinstance(data, dict):  # pragma: no cover - tomllib 总是返回 dict
        issues.append(ConfigIssue(path=path, message="顶层必须是一张表"))
        return None

    version = data.get("schema_version", SCHEMA_VERSION)
    if version != SCHEMA_VERSION:
        issues.append(
            ConfigIssue(
                path=path,
                field="schema_version",
                message=f"版本 {version} 不受支持，本版本只支持 {SCHEMA_VERSION}（不自动迁移）；该文件已被忽略",
            )
        )
        return None

    return data


def _line_column(message: str) -> tuple[int | None, int | None]:
    """从 ``tomllib`` 的异常消息中尽力提取行列（CPython 会附带位置信息）。"""
    match = _LINE_COLUMN_RE.search(message)
    if match is None:
        return None, None
    return int(match.group(1)), int(match.group(2))


def _read_state_quietly(paths: LogoxPaths, cwd: Path, issues: list[ConfigIssue]) -> StateFile | None:
    """读取 state.toml（**纯读**，不写盘、不备份）；损坏时只记一条提示。

    真正需要"备份并重建"的调用方应使用 ``StateStore.read_or_rebuild()``。
    """
    from logox.config.state import StateStore  # 延迟导入，避免模块级循环

    candidates: list[Path] = [paths.state]
    project = nearest_project(cwd)
    if project is not None:
        candidates.append(project.state)

    merged_state = StateFile()
    found = False
    for candidate in candidates:
        if not candidate.is_file():
            continue
        try:
            parsed = StateStore(candidate).read()
        except ConfigValidationError as exc:
            issues.append(
                ConfigIssue(
                    path=candidate,
                    message=f"状态文件无法解析（{len(exc.issues)} 处问题），本次忽略其内容",
                    severity="warning",
                )
            )
            continue
        merged_state = _merge_state(merged_state, parsed)
        found = True
    return merged_state if found else None


def _merge_state(base: StateFile, incoming: StateFile) -> StateFile:
    """项目级状态覆盖全局状态（后者优先）。"""
    data = base.model_dump()
    overlay = incoming.model_dump()
    for section, values in overlay.items():
        if isinstance(values, dict) and isinstance(data.get(section), dict):
            for key, value in values.items():
                if value not in (None, [], {}):
                    data[section][key] = value
        elif values not in (None, [], {}):
            data[section] = values
    return StateFile.model_validate(data)


def _apply_state_overlay(
    merged: dict[str, Any],
    origin: dict[str, ConfigSource],
    state: StateFile,
    source: ConfigSource,
) -> bool:
    """把「上次使用」映射到配置字段（**只覆盖 state 拥有的键**，D44 §5.1 步骤 ④）。

    返回**是否真的应用了任何键** —— 调用方据此决定要不要把它登记进"来源列表"。
    F-21 的教训：一个**什么值都没有**的 `state.toml` 会让来源列表里多出一条 `state`，
    于是"无任何配置 → 只有 defaults"这类断言会莫名其妙地失败。
    """
    pairs = (
        (("provider", "name"), state.last.provider),
        (("provider", "model"), state.last.model),
        (("provider", "thinking_effort"), state.last.effort),
        (("ui", "theme"), state.last.theme),
    )
    applied = False
    for path, value in pairs:
        if value is None:
            continue
        node = merged
        for key in path[:-1]:
            child = node.get(key)
            if not isinstance(child, dict):
                child = {}
                node[key] = child
            node = child
        node[path[-1]] = value
        origin[".".join(path)] = source
        applied = True
    return applied


def _env_overrides(env: Mapping[str, str]) -> dict[str, Any]:
    """``LOGOX__PROVIDER__MODEL=x`` → ``{"provider": {"model": "x"}}``。"""
    out: dict[str, Any] = {}
    for name, raw in env.items():
        if not name.startswith(ENV_PREFIX):
            continue
        parts = [part.lower() for part in name[len(ENV_PREFIX) :].split("__") if part]
        if not parts:
            continue
        node = out
        for part in parts[:-1]:
            child = node.get(part)
            if not isinstance(child, dict):
                child = {}
                node[part] = child
            node = child
        node[parts[-1]] = _parse_env_value(raw)
    return out


def _parse_env_value(raw: str) -> Any:
    """尽力把环境变量解析成 TOML 标量；失败则原样当字符串。"""
    try:
        return tomllib.loads(f"value = {raw}")["value"]
    except tomllib.TOMLDecodeError:
        return raw


# --------------------------------------------------------------------------- #
# 合并
# --------------------------------------------------------------------------- #


def _merge(
    target: dict[str, Any],
    incoming: Mapping[str, Any],
    origin: dict[str, ConfigSource],
    prefix: tuple[str, ...],
    source: ConfigSource,
) -> None:
    for key, value in incoming.items():
        path = (*prefix, str(key))
        existing = target.get(key)
        if isinstance(value, Mapping) and isinstance(existing, dict):
            _merge(existing, value, origin, path, source)
            continue
        if isinstance(value, Mapping):
            copied = copy.deepcopy(dict(value))
            target[key] = copied
            _record_origin(origin, path, copied, source)
            continue
        target[key] = copy.deepcopy(value)
        origin[".".join(path)] = source


def _record_origin(
    origin: dict[str, ConfigSource],
    path: tuple[str, ...],
    value: Any,
    source: ConfigSource,
) -> None:
    """为整棵子树登记来源，使 ``origin_of("a.b.c")`` 能直接命中。"""
    if isinstance(value, dict):
        for key, child in value.items():
            _record_origin(origin, (*path, str(key)), child, source)
    else:
        origin[".".join(path)] = source


# --------------------------------------------------------------------------- #
# 校验与回退
# --------------------------------------------------------------------------- #


def _strip_plaintext_keys(
    data: Any,
    origin: dict[str, ConfigSource],
    issues: list[ConfigIssue],
    fallback: Path,
    prefix: str = "",
) -> None:
    """摘掉任何 ``api_key = "明文"``（E-6 / D37-a 底线 ④）。

    **绝不回显用户写的值**——只报告字段位置与所属文件。
    """
    if isinstance(data, dict):
        for key in list(data.keys()):
            child_path = f"{prefix}.{key}" if prefix else str(key)
            value = data[key]
            if key == "api_key" and value:
                del data[key]
                issues.append(
                    ConfigIssue(
                        path=_attribute(child_path, origin, fallback),
                        field=child_path,
                        message=(
                            "不允许写明文 api_key（会被日志与终端泄露）；"
                            '请改用 api_key_env = "<环境变量名>"'
                        ),
                    )
                )
            else:
                _strip_plaintext_keys(value, origin, issues, fallback, child_path)
    elif isinstance(data, list):
        for index, item in enumerate(data):
            _strip_plaintext_keys(item, origin, issues, fallback, f"{prefix}[{index}]")


def _validate(
    merged: dict[str, Any],
    origin: dict[str, ConfigSource],
    issues: list[ConfigIssue],
    fallback: Path,
) -> LogoxConfig:
    """校验合并结果；**出错字段回退默认值后重试**，直到通过（D44）。

    采用"摘除 → 重校验"而不是"逐字段修补"，是因为 pydantic 的错误定位
    （``loc``）本身就是精确的字段路径，直接摘除它即可让该字段回到默认值。
    """
    data = copy.deepcopy(merged)

    for _ in range(_MAX_STRIP_ROUNDS):
        try:
            return LogoxConfig.model_validate(data)
        except ValidationError as exc:
            removed_any = False
            for error in exc.errors():
                location = tuple(error.get("loc", ()))
                field = _format_location(location)
                # 未知字段只是"你可能拼错了"，不应阻断启动（D44 ③ → warning）；
                # 其余校验失败才回退该字段并报 error。
                severity = "warning" if error.get("type") == "extra_forbidden" else "error"
                issues.append(
                    ConfigIssue(
                        path=_attribute(field, origin, fallback),
                        field=field or None,
                        message=_humanize(error),
                        severity=severity,  # type: ignore[arg-type]
                    )
                )
                if _strip(data, location):
                    removed_any = True
            if not removed_any:
                break

    issues.append(
        ConfigIssue(
            path=fallback,
            field=None,
            message="配置问题过多或无法定位，已整体回退为内置默认值",
        )
    )
    return LogoxConfig()


def _strip(data: Any, location: Sequence[Any]) -> bool:
    """从合并结果中摘掉出错的那个叶子，使其回退为内置默认值。

    返回**是否真的摘掉了**——这个返回值是防死循环的关键：如果某个问题无法定位
    （例如模型级校验失败，``loc`` 为空），循环必须立刻退出并整体回退默认值，
    否则会无限重试。
    """
    if not location:
        return False

    node: Any = data
    for key in location[:-1]:
        if isinstance(node, dict) and key in node or isinstance(node, list) and isinstance(key, int) and 0 <= key < len(node):
            node = node[key]
        else:
            return False

    last = location[-1]
    if isinstance(node, dict) and last in node:
        del node[last]
        return True
    if isinstance(node, list) and isinstance(last, int) and 0 <= last < len(node):
        del node[last]
        return True
    return False


def _format_location(location: Sequence[Any]) -> str:
    parts: list[str] = []
    for item in location:
        if isinstance(item, int):
            if parts:
                parts[-1] = f"{parts[-1]}[{item}]"
            else:  # pragma: no cover - 顶层不可能是索引
                parts.append(f"[{item}]")
        else:
            parts.append(str(item))
    return ".".join(parts)


def _attribute(field: str, origin: dict[str, ConfigSource], fallback: Path) -> Path:
    """把字段归属到提供它的那个文件（近者优先向上查找）。"""
    candidate = field
    while True:
        source = origin.get(candidate)
        if source is not None:
            return source.path
        if "." not in candidate:
            break
        candidate = candidate.rsplit(".", 1)[0]
    return fallback


def _humanize(error: Mapping[str, Any]) -> str:
    """把 pydantic 的错误转成人话。

    **对 ``value_error`` 只取 ``msg``，绝不回显 ``input``**——明文密钥那类
    自定义校验失败的 ``input`` 是整个模型字典，回显会泄露用户写的内容。
    """
    etype = str(error.get("type", ""))
    ctx = error.get("ctx") or {}
    value = error.get("input")
    location = tuple(error.get("loc", ()))
    leaf = str(location[-1]) if location else ""

    if etype == "missing":
        return "缺少必填项"
    if etype == "extra_forbidden":
        return f"未知字段（拼写错误？）：{leaf}"
    if etype == "literal_error":
        expected = ctx.get("expected") or "允许的取值之一"
        message = f"取值必须是 {expected}；实际为 {value!r}"
        if str(value) in _INTERCEPTOR_HOOK_EVENTS:
            message += "（v1 仅支持观察型钩子，拦截型尚未支持，见 D23）"
        return message
    if etype in ("greater_than", "greater_than_equal", "less_than", "less_than_equal"):
        limits = {key: ctx[key] for key in ("gt", "ge", "lt", "le") if key in ctx}
        return f"取值超出允许范围 {limits}；实际为 {value!r}"
    if etype in ("int_parsing", "float_parsing", "bool_parsing", "int_type", "float_type", "bool_type"):
        return f"类型错误：这里应当是数字或布尔值；实际为 {value!r}"
    if etype == "string_pattern_mismatch":
        return f"格式不符合要求（仅接受 #RRGGBB）；实际为 {value!r}"
    if etype == "string_too_short":
        return "内容不能为空"
    if etype == "value_error":
        # 不回显 input：防密钥泄露。
        return str(error.get("msg", "取值非法")).removeprefix("Value error, ")
    return str(error.get("msg", "校验失败"))


# --------------------------------------------------------------------------- #
# 跨字段语义约束（在字段校验通过之后运行）
# --------------------------------------------------------------------------- #


def _semantic_checks(
    config: LogoxConfig,
    origin: dict[str, ConfigSource],
    issues: list[ConfigIssue],
    fallback: Path,
    paths: LogoxPaths,
    cwd: Path,
    available_themes: Sequence[str] | None,
    known_providers: Sequence[str] | None = None,
) -> None:
    # 用户自定义的 provider（[providers.x]）永远算"已知"；
    # 内置的以装配根传进来的真名单为准，没传才退回兜底名单。
    known = set(config.providers) | set(known_providers or BUILTIN_PROVIDERS)
    if config.provider.name not in known:
        options = "、".join(sorted(known))
        issues.append(
            ConfigIssue(
                path=_attribute("provider.name", origin, fallback),
                field="provider.name",
                message=f"未定义的 Provider {config.provider.name!r}；可选：{options}",
            )
        )

    if not config.provider.model.strip():
        issues.append(
            ConfigIssue(
                path=_attribute("provider.model", origin, fallback),
                field="provider.model",
                message="未指定模型；请在 config.toml 里设置 provider.model，或用 /model 选择",
            )
        )

    themes = set(available_themes) if available_themes is not None else _available_themes(paths, cwd)
    if config.ui.theme not in themes:
        issues.append(
            ConfigIssue(
                path=_attribute("ui.theme", origin, fallback),
                field="ui.theme",
                message=f"主题 {config.ui.theme!r} 不存在；可用：{'、'.join(sorted(themes))}",
            )
        )

    unknown_tools = sorted(set(config.tools.enabled) - set(DEFAULT_ENABLED_TOOLS))
    if unknown_tools:
        issues.append(
            ConfigIssue(
                path=_attribute("tools.enabled", origin, fallback),
                field="tools.enabled",
                message=f"未知工具名将被忽略：{'、'.join(unknown_tools)}",
                severity="warning",
            )
        )


def _available_themes(paths: LogoxPaths, cwd: Path) -> set[str]:
    """可用主题 = **内置 + 用户目录**。

    ★ D152-c（用户裁定）：**刻意不再读项目级 ``.logox/themes/``**。

    为什么删掉项目级来源
    -------------------
    用户原话："主题配色统一放在 ``~/.logox/themes/`` 中，不要有什么项目级配置覆盖了"。
    这条口径是对的：**主题是"用户对界面的偏好"，不是"项目对代码的规范"**。
    项目级主题天然做不到"整机一套配色"——换个项目界面就变；
    而"这个项目的规矩"另有归属（``LOGOX.md`` / ``.logox/state.toml`` 的权限规则）。

    它顺带修掉了一个**双事实来源**（与 D150 修的 ``content``/``turn_summary`` 同类）：
    这里曾把项目级主题算作"可用"，于是 ``ui.theme = "my"`` 能**通过校验**，
    而真正加载时（`load_theme` 只搜内置目录）**失败** —— 校验与运行时各持一份事实。

    注意 ``cwd`` 参数保留在签名里（调用方与契约不变），但**不再被使用**。
    """
    return set(BUILTIN_THEMES) | set(discover_themes([paths.themes]))
