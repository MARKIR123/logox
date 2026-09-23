"""``state.toml`` 的读取、变换式更新与**原子写回**（D30 / E-9 / E-10 / E-11 / E-23）。

三条硬规则
----------
1. **只写 ``state.toml``**，绝不碰用户手写的 ``config.toml`` / ``permissions.toml``（D30 红线）。
2. **原子写回**：先写同目录临时文件并 ``fsync``，再 ``os.replace`` 替换。
   断电或崩溃只会看到"旧内容"或"新内容"，不会出现半截文件。
3. **变换式更新 ``update(fn)``**：读 → 变换 → 写。检测到外部并发修改时，
   把同一个 ``fn`` 重新应用到**刚读到的状态**上即可，无需重放零散的字段修改
   ——这是让重试真正正确的唯一简洁做法。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from logox.config import writer
from logox.config.schema import SCHEMA_VERSION, STATUS_ITEM_KEYS, StateFile
from logox.errors import ConfigValidationError, ConfigWriteError

__all__ = ["StateStore", "LayeredStateStore"]

_MAX_CONFLICT_RETRIES = 3
_REPLACE_RETRIES = 3
_REPLACE_BACKOFF_S = 0.05


def _signature(raw: bytes | None) -> bytes | None:
    """并发检测的指纹——**直接用文件内容本身**。

    为什么不用 ``(mtime_ns, size)``：Windows 的文件时间戳粒度约 **15.6ms**
    （系统时钟更新周期），同一 tick 内的两次写入会得到完全相同的 mtime；若两次
    内容长度又恰好相同，基于 mtime+size 的检测就会**漏掉外部修改**，进而静默
    覆盖别人的写入。内容比对没有这个盲区，代价只是我们已经读过一遍的字节再比一次。
    """
    return raw


class StateStore:
    """单个 ``state.toml``（全局或项目级）的读写器。

    进程内调用方应保证写回是**串行**的；``update`` 会检测外部进程的并发修改
    并重试（最多 3 次），仍冲突则抛 :class:`~logox.errors.ConfigWriteError`。
    """

    def __init__(self, path: Path) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    # ------------------------------------------------------------------ #
    # 读
    # ------------------------------------------------------------------ #

    def read(self) -> StateFile:
        """纯读（**不写盘**）。

        * 文件不存在 → 返回全默认（E-1：缺失不是错误）
        * 内容损坏 → 抛 :class:`ConfigValidationError`（由调用方决定是否重建）
        """
        return self._parse(self._read_bytes())

    def _read_bytes(self) -> bytes | None:
        if not self._path.is_file():
            return None
        try:
            return self._path.read_bytes()
        except OSError as exc:
            raise ConfigValidationError(self._path, [f"无法读取状态文件：{exc}"]) from exc

    def _parse(self, raw: bytes | None) -> StateFile:
        if raw is None:
            return StateFile()

        try:
            data = tomllib.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            raise ConfigValidationError(self._path, [f"状态文件无法解析：{exc}"]) from exc

        if not isinstance(data, dict):  # pragma: no cover - tomllib 总是返回 dict
            raise ConfigValidationError(self._path, ["状态文件顶层必须是一张表"])

        version = data.get("schema_version", SCHEMA_VERSION)
        if version != SCHEMA_VERSION:
            raise ConfigValidationError(
                self._path,
                [f"schema_version 为 {version}，本版本只支持 {SCHEMA_VERSION}"],
            )

        try:
            return StateFile.model_validate(data)
        except ValidationError as exc:
            raise ConfigValidationError(self._path, [str(error) for error in exc.errors()]) from exc

    def read_or_rebuild(self) -> StateFile:
        """损坏时**先备份再重建**（E-11：绝不静默丢弃用户数据）。"""
        try:
            return self.read()
        except ConfigValidationError:
            if self._path.is_file():
                backup = self._path.with_name(self._path.name + ".bak")
                with contextlib.suppress(OSError):
                    self._path.replace(backup)
            return StateFile()

    # ------------------------------------------------------------------ #
    # 写
    # ------------------------------------------------------------------ #

    def write(self, state: StateFile) -> None:
        """原子写回（不带并发检测）；一般应优先使用 :meth:`update`。"""
        self._atomic_write(writer.dumps(state.model_dump()))

    def update(self, transform: Callable[[StateFile], StateFile]) -> None:
        """读 → 变换 → 原子写回，并检测外部并发修改（E-10 / E-23）。

        :raises ConfigWriteError: 写失败，或连续多次检测到外部修改。
            调用方**必须降级为「记日志 + 会话继续」**（P-5），不得中断会话。
        """
        last_reason = "未知原因"
        for _ in range(_MAX_CONFLICT_RETRIES):
            raw_before = self._read_bytes()
            current = self._parse(raw_before)
            updated = transform(current)

            if _signature(self._read_bytes()) != _signature(raw_before):
                # 外部进程在我们读取期间改过 → 重读、重算，避免覆盖别人的写入。
                last_reason = "检测到外部并发修改"
                continue

            self._atomic_write(writer.dumps(updated.model_dump()))
            return

        raise ConfigWriteError(
            self._path,
            f"状态写回失败（重试 {_MAX_CONFLICT_RETRIES} 次）：{last_reason}",
        )

    async def aupdate(self, transform: Callable[[StateFile], StateFile]) -> None:
        """异步上下文（TUI）中使用的 :meth:`update`：把阻塞的文件写入丢到线程池。

        文件写入本身是阻塞操作，直接 ``await`` 同步版本会卡住事件循环——
        而"卡住事件循环"正是 D37（界面友好优先）最不能接受的事。
        """
        await asyncio.to_thread(self.update, transform)

    def _atomic_write(self, text: str) -> None:
        directory = self._path.parent
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ConfigWriteError(self._path, f"无法创建目录 {directory}：{exc}", exc) from exc

        tmp = self._path.with_name(self._path.name + ".tmp")
        try:
            with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            _replace_with_retry(tmp, self._path)
        except OSError as exc:
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)
            raise ConfigWriteError(self._path, f"状态写回失败：{exc}", exc) from exc

    # ------------------------------------------------------------------ #
    # 便捷变换（全部走 update，保证原子性与并发检测）
    # ------------------------------------------------------------------ #

    def set_last_model(self, provider: str | None = None, model: str | None = None) -> None:
        def transform(state: StateFile) -> StateFile:
            if provider is not None:
                state.last.provider = provider
            if model is not None:
                state.last.model = model
            return state

        self.update(transform)

    def set_theme(self, theme: str) -> None:
        def transform(state: StateFile) -> StateFile:
            state.last.theme = theme
            return state

        self.update(transform)

    def set_effort(self, effort: str) -> None:
        def transform(state: StateFile) -> StateFile:
            state.last.effort = effort
            return state

        self.update(transform)

    def set_models(self, provider: str, models: list[str], *, fetched_at: float | None = None) -> None:
        """记住**从端点抓到的**模型列表（`/login` 成功后写入）。

        缓存它的理由：抓取要发一次网络请求（本机实测很慢），而 `/model` 是个
        点开就该立刻出结果的弹窗。缓存之后：登录时抓一次，之后每次打开选择器**零网络**。

        **抓到的列表为空时不覆盖已有缓存**：一次失败的空结果不该把上次
        好不容易拿到的列表清掉。
        """
        if not models:
            return
        stamp = time.time() if fetched_at is None else fetched_at

        def transform(state: StateFile) -> StateFile:
            state.last.models[provider] = list(models)
            state.last.models_fetched_at = stamp
            return state

        self.update(transform)

    def cached_models(self, provider: str) -> list[str]:
        """上次抓到的模型列表（没抓过则空）。**纯读，不发网络。**"""
        return list(self.read().last.models.get(provider, []))

    def set_status_item(self, name: str, enabled: bool) -> None:
        """记住状态栏某项的开关（D16 / D42）。未知项名直接报错，不静默接受。"""
        if name not in STATUS_ITEM_KEYS:
            raise ValueError(f"未知状态项 {name!r}；可用：{'、'.join(STATUS_ITEM_KEYS)}")

        def transform(state: StateFile) -> StateFile:
            state.ui.status_items[name] = enabled
            return state

        self.update(transform)

    def set_focus_mode(self, enabled: bool) -> None:
        def transform(state: StateFile) -> StateFile:
            state.ui.focus_mode = enabled
            return state

        self.update(transform)

    def learn_permission(self, kind: str, rule: str) -> bool:
        """记住一条权限规则（D10）。**已存在时不写盘**（避免无谓 IO，T-25）。"""
        if kind not in ("allow", "deny"):
            raise ValueError(f"kind 只能是 'allow' 或 'deny'，收到 {kind!r}")

        def transform(state: StateFile) -> StateFile:
            bucket: list[str] = getattr(state.permissions, kind)
            if rule not in bucket:
                bucket.append(rule)
            return state

        current = self.read()
        if rule in getattr(current.permissions, kind):
            return False
        self.update(transform)
        return True

    def revoke_permission(self, kind: str, rule: str) -> bool:
        """从 state.toml 中撤销一条权限规则（D132）。**不存在时不写盘**。"""
        if kind not in ("allow", "deny"):
            raise ValueError(f"kind 只能是 'allow' 或 'deny'，收到 {kind!r}")

        def transform(state: StateFile) -> StateFile:
            bucket: list[str] = getattr(state.permissions, kind)
            if rule in bucket:
                bucket.remove(rule)
            return state

        current = self.read()
        if rule not in getattr(current.permissions, kind):
            return False
        self.update(transform)
        return True

    def set_permission_mode(self, mode: str) -> None:
        """设置权限运行模式（D130：default / creative）。"""
        if mode not in ("default", "creative"):
            raise ValueError(f"权限模式只能是 'default' 或 'creative'，收到 {mode!r}")

        def transform(state: StateFile) -> StateFile:
            state.permissions.mode = mode
            return state

        self.update(transform)

    def cache_shell_backend(self, info: Any) -> None:
        """缓存 Shell 后端探测结果（D19）。"""

        def transform(state: StateFile) -> StateFile:
            state.shell.backend = str(getattr(info, "backend", "") or "")
            state.shell.executable = str(getattr(info, "executable", "") or "")
            state.shell.version = str(getattr(info, "version", "") or "")
            detected_at = getattr(info, "detected_at", None)
            state.shell.detected_at = float(detected_at) if detected_at is not None else time.time()
            return state

        self.update(transform)


def _replace_with_retry(source: Path, target: Path) -> None:
    """``os.replace`` 在 Windows 上可能被杀软/索引器短暂占用，重试几次。

    只在这里短暂阻塞（最多两次 50ms 退避），因为这只在罕见的文件锁竞争下发生。
    """
    last: OSError | None = None
    for attempt in range(_REPLACE_RETRIES):
        try:
            os.replace(source, target)
            return
        except OSError as exc:
            last = exc
            time.sleep(_REPLACE_BACKOFF_S * (attempt + 1))
    assert last is not None
    raise last


def _merge_state_file(global_state: StateFile, project_state: StateFile) -> StateFile:
    """合并全局状态与项目状态（项目级偏好覆盖全局，权限完全隔离）。"""
    base = global_state.model_dump()
    overlay = project_state.model_dump()

    # 1. 偏好与通用配置：项目有值的优先覆盖全局
    for section, values in overlay.items():
        if section == "permissions":
            continue  # 权限单独隔离处理，防止穿透
        if isinstance(values, dict) and isinstance(base.get(section), dict):
            for key, value in values.items():
                if value not in (None, [], {}):
                    base[section][key] = value
        elif values not in (None, [], {}):
            base[section] = values

    # 2. 权限隔离（核心防护）：严格采用项目本地权限，绝不继承全局
    base["permissions"] = overlay.get(
        "permissions", {"mode": "default", "allow": [], "deny": []}
    )
    return StateFile.model_validate(base)


class LayeredStateStore:
    """分层状态存储（D120 全局共享偏好 + 局部权限沙箱）。

    内部持有两个 StateStore：
    - ``project_store``: 当前项目工作区状态（如 ``<project>/.logox/state.toml``）
    - ``global_store``: 用户全局状态（如 ``~/.logox/state.toml``）

    分层行为规范：
    1. 权限隔离（Permission Sandbox）：``learn_permission`` 严格单写 ``project_store``，
       杜绝当前项目允许的高危命令白名单穿透至全局或其他工程。
    2. 偏好共享（Preference Sharing）：``set_last_model`` / ``set_theme`` / ``set_effort``
       / ``set_status_item`` / ``set_focus_mode`` 双写全局与项目，换项目免配置无缝继承。
    3. 端点模型缓存（Models Cache）：``set_models`` 双写（全局为主）；``cached_models`` 优先读项目，缺失回退全局。
    4. 状态读取（State Overlay）：``read()`` 以全局状态为底座，叠加当前项目状态；
       但 ``permissions`` 严格只采用 ``project_store`` 的权限规则。
    """

    def __init__(self, project_store: StateStore, global_store: StateStore) -> None:
        self.project_store = project_store
        self.global_store = global_store

    @property
    def path(self) -> Path:
        return self.project_store.path

    def read(self) -> StateFile:
        """读取合并状态：全局底座 + 项目覆盖（权限严格隔离）。"""
        return _merge_state_file(self.global_store.read(), self.project_store.read())

    def read_or_rebuild(self) -> StateFile:
        """损坏时先备份再重建，并返回合并状态。"""
        return _merge_state_file(
            self.global_store.read_or_rebuild(), self.project_store.read_or_rebuild()
        )

    def write(self, state: StateFile) -> None:
        """写入当前项目状态。"""
        self.project_store.write(state)

    def update(self, transform: Callable[[StateFile], StateFile]) -> None:
        """更新当前项目状态。"""
        self.project_store.update(transform)

    async def aupdate(self, transform: Callable[[StateFile], StateFile]) -> None:
        """异步更新当前项目状态。"""
        await self.project_store.aupdate(transform)

    # ------------------------------------------------------------------ #
    # 偏好写回（双写：全局持久化 + 当前项目同步）
    # ------------------------------------------------------------------ #

    def set_last_model(self, provider: str | None = None, model: str | None = None) -> None:
        self.global_store.set_last_model(provider=provider, model=model)
        if self.project_store.path != self.global_store.path:
            with contextlib.suppress(Exception):
                self.project_store.set_last_model(provider=provider, model=model)

    def set_theme(self, theme: str) -> None:
        self.global_store.set_theme(theme)
        if self.project_store.path != self.global_store.path:
            with contextlib.suppress(Exception):
                self.project_store.set_theme(theme)

    def set_effort(self, effort: str) -> None:
        self.global_store.set_effort(effort)
        if self.project_store.path != self.global_store.path:
            with contextlib.suppress(Exception):
                self.project_store.set_effort(effort)

    def set_status_item(self, name: str, enabled: bool) -> None:
        self.global_store.set_status_item(name, enabled)
        if self.project_store.path != self.global_store.path:
            with contextlib.suppress(Exception):
                self.project_store.set_status_item(name, enabled)

    def set_focus_mode(self, enabled: bool) -> None:
        self.global_store.set_focus_mode(enabled)
        if self.project_store.path != self.global_store.path:
            with contextlib.suppress(Exception):
                self.project_store.set_focus_mode(enabled)

    def cache_shell_backend(self, info: Any) -> None:
        self.global_store.cache_shell_backend(info)
        if self.project_store.path != self.global_store.path:
            with contextlib.suppress(Exception):
                self.project_store.cache_shell_backend(info)

    def set_models(self, provider: str, models: list[str], *, fetched_at: float | None = None) -> None:
        self.global_store.set_models(provider, models, fetched_at=fetched_at)
        if self.project_store.path != self.global_store.path:
            with contextlib.suppress(Exception):
                self.project_store.set_models(provider, models, fetched_at=fetched_at)

    def cached_models(self, provider: str) -> list[str]:
        models = self.project_store.cached_models(provider)
        if models:
            return models
        return self.global_store.cached_models(provider)

    # ------------------------------------------------------------------ #
    # 权限写回（严格单写：仅写项目级，绝不污染全局）
    # ------------------------------------------------------------------ #

    def learn_permission(self, kind: str, rule: str) -> bool:
        """记住一条权限规则（D10 / D120）。

        **核心安全约束**：绝不写入全局 ``global_store``，仅持久化至当前项目的 ``project_store``，
        确保项目间的命令与路径白名单物理隔离。
        """
        return self.project_store.learn_permission(kind, rule)

    def revoke_permission(self, kind: str, rule: str) -> bool:
        """从项目 state.toml 中撤销一条权限规则（D132）。"""
        return self.project_store.revoke_permission(kind, rule)

    def set_permission_mode(self, mode: str) -> None:
        """设置权限运行模式（D130：严格单写当前项目 project_store，绝不污染全局）。"""
        self.project_store.set_permission_mode(mode)

