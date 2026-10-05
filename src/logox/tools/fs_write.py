"""``write`` 工具——全量文件写入（L4）。

支持创建新文件与全量覆写现有文件。
核心防护设计：
1. **原子写（Atomic Write）**：先写入同目录下的隐藏临时文件，再通过 ``os.replace`` 事务级替换，
   彻底防止断电、进程崩溃或中途取消产生损坏的半截文件。
2. **自动创建父目录**：目标路径缺少中间目录时自动递归 ``mkdir(parents=True)``。
3. **变更指标计算**：计算 ``ChangeStat(kind="new"|"rewrite", added=..., removed=...)``，
   驱动 TUI 卡片徽标与事件总线发布。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import uuid
from pathlib import Path

from pydantic import Field

from logox.errors import ErrorCategory
from logox.kernel.events import ChangeStat
from logox.tools.base import DisplayHint, ToolArgs, ToolContext, ToolResult, ToolSpec

__all__ = ["WriteArgs", "WriteTool", "build"]


def _resolve(cwd: Path, raw_path: str) -> Path:
    """把路径解析为绝对路径（相对工作目录或绝对路径）。"""
    p = Path(raw_path)
    return p if p.is_absolute() else (cwd / p).resolve()


class WriteArgs(ToolArgs):
    """``write`` 工具参数模型。"""

    path: str = Field(description="目标文件路径（相对工作目录或绝对路径）")
    content: str = Field(description="要写入的文件完整文本内容")


class WriteTool:
    """创建新文件或覆盖写入完整文本。"""

    spec = ToolSpec(
        name="write",
        description=(
            "创建新文件或全量覆盖现有文件。会自动递归创建所需父目录。"
            "如果只需修改已有文件的一部分，请优先使用 edit 工具。"
        ),
        params=WriteArgs,
        readonly=False,
        requires_permission=True,
        summary_template="写入 {path}",
    )

    async def run(self, args: ToolArgs, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, WriteArgs)
        target = _resolve(ctx.cwd, args.path)

        if target.exists() and target.is_dir():
            return ToolResult.failure(
                ErrorCategory.BAD_REQUEST,
                f"目标路径是一个目录，无法写入：{target}",
                detail="write 工具只能写入文件，不能将内容写入已有目录。",
            )

        # 检查取消
        if ctx.is_cancelled():
            return ToolResult.failure(ErrorCategory.CANCELLED, "写入前操作已被取消")

        try:
            # 1. 确保父目录存在
            target.parent.mkdir(parents=True, exist_ok=True)

            # 2. 统计写入前的状态
            existed, bytes_before, old_lines = await asyncio.to_thread(self._existing_state, target)
            if ctx.is_cancelled():
                return ToolResult.failure(ErrorCategory.CANCELLED, "写入准备后操作已被取消，未写入文件")

            new_bytes = args.content.encode("utf-8")
            new_lines = len(args.content.splitlines())

            stat = ChangeStat(
                kind="rewrite" if existed and bytes_before > 0 else "new",
                added=new_lines,
                removed=old_lines,
                bytes_before=bytes_before,
                bytes_after=len(new_bytes),
            )

            # 3. 原子写入：先写临时文件，后 os.replace 重命名
            tmp_name = f".{target.name}.tmp_{uuid.uuid4().hex[:8]}"
            tmp_file = target.parent / tmp_name
            try:
                tmp_file.write_bytes(new_bytes)
                os.replace(tmp_file, target)
            finally:
                if tmp_file.exists():
                    with contextlib.suppress(Exception):
                        tmp_file.unlink(missing_ok=True)

            resolved_cwd = ctx.cwd.resolve()
            try:
                rel = target.relative_to(resolved_cwd)
            except ValueError:
                rel = target

            summary = f"已成功写入文件 {rel}（共 {new_lines} 行，{len(new_bytes)} 字节）"
            return ToolResult(
                ok=True,
                content=summary,
                change_stat=stat,
                display=DisplayHint(kind="text", payload={"stat": stat.model_dump()}),
            )
        except Exception as exc:
            return ToolResult.failure(
                ErrorCategory.TOOL_FAILURE,
                f"写入文件失败：{target}：{type(exc).__name__}: {exc}",
                detail="请检查磁盘权限或是否有其他程序独占锁定了该文件。",
            )


    @staticmethod
    def _existing_state(target: Path) -> tuple[bool, int, int]:
        """仅读取已有文件统计，提交仍在原协程的同步区间完成。"""
        existed = target.exists()
        bytes_before = target.stat().st_size if existed else 0
        if existed:
            try:
                old_lines = len(target.read_bytes().splitlines())
            except Exception:
                old_lines = 0
        else:
            old_lines = 0

        return existed, bytes_before, old_lines


def build() -> WriteTool:
    return WriteTool()
