"""用户级人设（``~/.logox/LOGOX.md``）——系统提示的第一段。

与 :mod:`logox.context.memory` 的区别：那边的 ``ProjectMemory`` 沿 cwd 祖先链发现
"**这个仓库**的规范"；本模块只认**一个**用户级文件，跨项目生效。分开的理由是
**失败语义不同**：项目规范缺失只是少几条规则；人设是身份层，它缺失/损坏时必须
回落到内置人设，并且让"为什么没生效"可见（``skipped``）。

设计取舍：

* **不设 token 预算**——人设不该被别的记忆挤掉，只设 64KiB 硬上限；
* **不合并进项目记忆**——否则 ``/status`` 看不出它是用户级还是项目级；
* **不写回文件**——这里只读；不做"自动补全人设"这类会覆盖用户手写内容的事。
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["MAX_PERSONA_BYTES", "PersonaMemory"]

#: 人设文件的最大读取字节数。超限**整份跳过**而不是截断：人设被砍掉一半
#: 会变成一段自相矛盾的规则，比"没有"更糟。
MAX_PERSONA_BYTES = 64 * 1024


class PersonaMemory:
    """一份用户级人设文件的内存表示（纯读）。"""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else None
        self.block = ""
        self.skipped = ""
        self.load()

    def load(self) -> None:
        """（重）读磁盘。任何失败都降级成空 ``block`` + 一句 ``skipped``。

        "文件不存在"是**正常情况**（不是错误）：``skipped`` 保持空串，
        调用方据此与"有文件但没读进来"区分。
        """
        self.block = ""
        self.skipped = ""
        if self.path is None:
            return
        try:
            if not self.path.exists():
                return
            if self.path.is_symlink() or any(p.is_symlink() for p in self.path.parents):
                self.skipped = f"{self.path}：符号链接不加载"
                return
            if self.path.stat().st_size > MAX_PERSONA_BYTES:
                self.skipped = f"{self.path}：超过 64KiB，整份跳过"
                return
            text = self.path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            self.skipped = f"{self.path}：读取失败 {exc}"
            return
        self.block = text.strip()

    @property
    def loaded(self) -> bool:
        """是否真的拿到了可注入的人设文本。"""
        return bool(self.block)

    def source(self) -> str:
        """给 UI／事件用的展示路径（统一正斜杠）。"""
        return str(self.path).replace("\\", "/") if self.path is not None else ""
