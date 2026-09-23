"""平台目录约定（``platformdirs`` 封装）。

职责
----
给出配置 / 状态 / 主题 / 插件 / 会话 / 日志 / 缓存的**规范路径**，并提供
「项目级目录向上逐级发现」的能力（D44：从 cwd 向上查找，近者优先）。

不负责
------
不读写任何文件（``ensure_user_dirs`` 只创建目录）、不解析配置、不做校验。

路径布局（D30）
---------------
用户级（所有平台统一放在 ``~/.logox``，便于文档与排查）::

    ~/.logox/config.toml        用户手写，永久只读
    ~/.logox/state.toml         程序生成，可写
    ~/.logox/permissions.toml   用户手写，永久只读
    ~/.logox/LOGOX.md           记忆文件（D21：只认这一个名字）
    ~/.logox/themes/*.toml      主题（UI-SPEC §8）
    ~/.logox/plugins/*.py       L2 插件
    ~/.logox/sessions/<sid>/    会话 JSONL
    ~/.logox/blobs/<sha256>     检查点内容寻址存储（D11）
    ~/.logox/logs/              事件日志（D25）

项目级（``.logox`` 目录可以出现在任意祖先目录中）::

    <dir>/.logox/config.toml        用户手写，永久只读
    <dir>/.logox/state.toml         程序生成，可写（项目级学习到的权限规则）
    <dir>/.logox/permissions.toml   用户手写，永久只读
    <dir>/.logox/LOGOX.md           记忆文件

缓存目录跟随平台约定（Windows 为 ``%LOCALAPPDATA%\\logox\\Cache``）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from platformdirs import PlatformDirs

__all__ = [
    "CONFIG_DIRNAME",
    "ENV_FILENAME",
    "MEMORY_FILENAME",
    "LogoxPaths",
    "ProjectPaths",
    "cache_dir",
    "discover_project_chain",
    "ensure_project_initialized",
    "home_root",
    "nearest_project",
]

APP_NAME = "logox"
CONFIG_DIRNAME = ".logox"
MEMORY_FILENAME = "LOGOX.md"
#: API Key 文件的名字（D59）。**唯一允许落盘密钥的地方**，由 `.gitignore` 排除。
ENV_FILENAME = ".env"

_dirs = PlatformDirs(appname=APP_NAME, appauthor=False, roaming=False)


def home_root() -> Path:
    """用户级 Logox 根目录：``~/.logox``。"""
    return Path.home() / CONFIG_DIRNAME


def cache_dir() -> Path:
    """缓存目录（跟随平台约定）。"""
    return Path(_dirs.user_cache_dir)


@dataclass(frozen=True, slots=True)
class LogoxPaths:
    """用户级（全局）路径集合。"""

    root: Path
    config: Path
    state: Path
    permissions: Path
    memory: Path
    themes: Path
    plugins: Path
    sessions: Path
    blobs: Path
    logs: Path
    cache: Path
    #: ``.env`` 密钥文件（D59）。**唯一允许落盘 API Key 的地方**，已 gitignore。
    #: 放在最后并给默认值，是为了让既有调用方（按位置传参的测试）不受影响。
    env: Path = Path(ENV_FILENAME)

    @classmethod
    def default(cls) -> LogoxPaths:
        return cls.at(home_root())

    @classmethod
    def at(cls, root: Path, *, cache: Path | None = None) -> LogoxPaths:
        """在指定根目录下布局（供测试与嵌入式使用，避免碰真实 ``~/.logox``）。"""
        root = Path(root)
        return cls(
            root=root,
            config=root / "config.toml",
            state=root / "state.toml",
            permissions=root / "permissions.toml",
            memory=root / MEMORY_FILENAME,
            themes=root / "themes",
            plugins=root / "plugins",
            sessions=root / "sessions",
            blobs=root / "blobs",
            logs=root / "logs",
            cache=Path(cache) if cache is not None else root / "cache",
            env=root / ENV_FILENAME,
        )

    def ensure_dirs(self) -> None:
        """创建**属于 Logox 的**目录（不含任何文件）。

        只创建程序自己管理的目录；``~/.logox`` 本身也在此创建，因为
        ``state.toml`` 的原子写入需要父目录存在（D30 §5.4）。
        """
        for directory in (
            self.root,
            self.themes,
            self.plugins,
            self.sessions,
            self.blobs,
            self.logs,
            self.cache,
        ):
            directory.mkdir(parents=True, exist_ok=True)


@dataclass(frozen=True, slots=True)
class ProjectPaths:
    """项目级路径集合。

    ``root`` 是**包含 ``.logox`` 目录的那个目录**——不是 cwd，也不是 git 根。
    """

    root: Path
    config: Path
    state: Path
    permissions: Path
    memory: Path
    plugins: Path
    #: 项目级 ``.env`` 密钥文件（D59）。``/login`` 默认写这里——
    #: "这个项目用哪个密钥"一眼可见，换项目不会误用别人的额度。
    env: Path = Path(ENV_FILENAME)

    @classmethod
    def for_root(cls, root: Path) -> ProjectPaths:
        base = root / CONFIG_DIRNAME
        return cls(
            root=root,
            config=base / "config.toml",
            state=base / "state.toml",
            permissions=base / "permissions.toml",
            memory=base / MEMORY_FILENAME,
            plugins=base / "plugins",
            env=base / ENV_FILENAME,
        )


def _ancestors(cwd: Path) -> list[Path]:
    """返回 ``cwd`` 及其全部祖先目录，顺序为 **近 → 远**，并已去重与规范化。

    去重是为了满足 E-15：``..``、符号链接或大小写差异不得导致同一目录被加载两次。
    """
    try:
        current = cwd.resolve()
    except OSError:  # pragma: no cover - 极端文件系统异常
        current = cwd.absolute()

    seen: set[str] = set()
    result: list[Path] = []
    for candidate in [current, *current.parents]:
        key = str(candidate).casefold() if _is_case_insensitive() else str(candidate)
        if key in seen:
            continue
        seen.add(key)
        result.append(candidate)
    return result


def _is_case_insensitive() -> bool:
    import os

    return os.name == "nt"


def discover_project_chain(cwd: Path) -> list[ProjectPaths]:
    """从 ``cwd`` 向上逐级查找含 ``.logox`` 的目录。

    返回顺序为 **远 → 近**，正好是配置合并所需的覆盖顺序（近者优先，D44）：
    调用方按返回顺序依次覆盖即可。
    """
    chain = [
        ProjectPaths.for_root(directory)
        for directory in _ancestors(cwd)
        if (directory / CONFIG_DIRNAME).is_dir()
    ]
    chain.reverse()
    return chain


def nearest_project(cwd: Path) -> ProjectPaths | None:
    """最近的项目级路径（用于写入项目 `state.toml`）；不存在则返回 ``None``。

    **不自动创建 ``.logox``**：程序不会在用户的目录里凭空建目录。若项目内
    没有 ``.logox``，调用方应退化为只使用全局 ``state.toml``。
    """
    for directory in _ancestors(cwd):
        if (directory / CONFIG_DIRNAME).is_dir():
            return ProjectPaths.for_root(directory)
    return None


def ensure_project_initialized(cwd: Path) -> tuple[ProjectPaths, bool]:
    """确保目标工作区具备轻量 ``.logox/`` 项目环境（D119 启动自动初始化）。

    若目标工作区无 ``.logox/`` 目录，自动创建：
    1. ``.logox/`` 目录；
    2. ``.logox/.gitignore``（排除 ``.env``, ``state.toml``, ``runs/``, ``*.log`` 等）；
    3. ``.logox/state.toml``（空状态骨架，使项目级权限不污染全局）。

    :returns: ``(project_paths, created)``，其中 ``created`` 表示本次是否新建了环境。
    """
    resolved_cwd = Path(cwd).resolve()
    base = resolved_cwd / CONFIG_DIRNAME
    created = not base.is_dir()
    base.mkdir(parents=True, exist_ok=True)

    gitignore_path = base / ".gitignore"
    if not gitignore_path.exists():
        gitignore_content = (
            "# Logox workspace ignore (D119)\n"
            ".env\n"
            "state.toml\n"
            # D153：不再有 `runs/`（运行产物早已搬到 ~/.logox/sessions 与 blobs）
            "*.log\n"
        )
        gitignore_path.write_text(gitignore_content, encoding="utf-8")

    state_path = base / "state.toml"
    if not state_path.exists():
        state_path.write_text("# Logox project state\nschema_version = 1\n", encoding="utf-8")

    return ProjectPaths.for_root(resolved_cwd), created
