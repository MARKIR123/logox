"""``edit`` 工具——局部确定性代码编辑（L4）。

核心设计原则（ARCHITECTURE §7.8 / D6 / D14）：
1. **确定性唯一替换（四级匹配阶梯）**：
   - 级别 1：精确匹配且唯一 → 直接替换；命中多处且未开 replace_all 则报错提示扩大上下文；
   - 级别 2：空白与换行归一化匹配 → 容忍 CRLF/LF 与行尾空白微小偏差，唯一命中则安全替换；
   - 级别 3：计算最相近代码片段 → 报错回灌并提供行号，辅助模型自我纠正；
   - 级别 4：绝对禁止模糊语义替换（防止改错位置的致命缺陷）。
2. **编码与格式守卫**：
   - 保留原文件 CRLF / LF 换行风格（防止 Git 全文行尾漂移污染）；
   - 保留 UTF-8 BOM（若原文件存在）；
   - 拦截二进制文件。
3. **Diff 与变更统计**：
   - 生成 Unified Diff（``DiffHunk``）与 ``ChangeStat(kind="modify", added=N, removed=M)``，
     驱动 TUI 卡片徽标与事件发布。
4. **原子写（Atomic Write）**：
   - 通过隐藏临时文件与 ``os.replace`` 确保写入安全。
"""

from __future__ import annotations

import difflib
import os
import uuid
from dataclasses import asdict
from pathlib import Path

from pydantic import Field

from logox.errors import ErrorCategory
from logox.kernel.events import ChangeStat
from logox.tools.base import DisplayHint, ToolArgs, ToolContext, ToolResult, ToolSpec
# ★ D140 / F-50：解析函数住在**中立模块**，不再从界面层拿 ——
#   工具层反向依赖界面层会让「没有界面也能用工具」（headless）这条能力失效。
from logox.difftext import parse_unified_diff

__all__ = ["EditArgs", "EditTool", "build"]

_BINARY_PROBE_BYTES = 8192
_BOM_BYTES = b"\xef\xbb\xbf"


def _resolve(cwd: Path, raw_path: str) -> Path:
    p = Path(raw_path)
    return p.resolve() if p.is_absolute() else (cwd.resolve() / p).resolve()


class EditArgs(ToolArgs):
    """``edit`` 工具参数模型。"""

    path: str = Field(description="要编辑的文件路径（相对工作目录或绝对路径）")
    old_string: str = Field(description="要被替换的原文本片段（必须在文件中唯一出现）")
    new_string: str = Field(description="替换后的新文本片段")
    replace_all: bool = Field(
        default=False,
        description="当 old_string 在文件中出现多次时，是否全部替换（默认 False，此时出现多次会报错）",
    )


class EditTool:
    """确定性局部代码替换工具。"""

    spec = ToolSpec(
        name="edit",
        description=(
            "在已有文件中查找唯一的 old_string 并将其替换为 new_string。"
            "old_string 必须在文件中恰好唯一出现一次。支持保留 CRLF/LF 与 BOM。"
        ),
        params=EditArgs,
        readonly=False,
        requires_permission=True,
        summary_template="编辑 {path}",
    )

    async def run(self, args: ToolArgs, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, EditArgs)
        target = _resolve(ctx.cwd, args.path)

        if not target.exists():
            return ToolResult.failure(
                ErrorCategory.BAD_REQUEST,
                f"文件不存在：{target}",
                detail="请检查文件路径是否正确，或使用 glob 工具确认文件位置。",
            )
        if target.is_dir():
            return ToolResult.failure(
                ErrorCategory.BAD_REQUEST,
                f"目标是一个目录，无法编辑：{target}",
            )

        if ctx.is_cancelled():
            return ToolResult.failure(ErrorCategory.CANCELLED, "编辑前操作已被取消")

        # 1. 读取原始字节与二进制判定
        raw = target.read_bytes()
        if b"\x00" in raw[:_BINARY_PROBE_BYTES]:
            return ToolResult.failure(
                ErrorCategory.BAD_REQUEST,
                f"文件是二进制文件，无法进行文本编辑：{target}",
            )

        # 2. BOM 检测与换行规范探测
        has_bom = raw.startswith(_BOM_BYTES)
        body_bytes = raw[len(_BOM_BYTES) :] if has_bom else raw

        crlf_count = body_bytes.count(b"\r\n")
        lf_count = body_bytes.count(b"\n") - crlf_count
        is_crlf = crlf_count > lf_count

        # 3. 解码为统一文本（内存中统一按 \n 处理，写入时再还原换行风格）
        encoding = "utf-8"
        try:
            content = body_bytes.decode("utf-8")
        except UnicodeDecodeError:
            try:
                content = body_bytes.decode("gbk")
                encoding = "gbk"
            except UnicodeDecodeError:
                content = body_bytes.decode("latin-1")
                encoding = "latin-1"

        # 统一内存内部换行符为 \n
        normalized_content = content.replace("\r\n", "\n")
        target_old = args.old_string.replace("\r\n", "\n")
        target_new = args.new_string.replace("\r\n", "\n")

        # 4. 四级匹配阶梯
        new_content, applied_note, match_err = self._apply_matching_ladder(
            normalized_content, target_old, target_new, replace_all=args.replace_all
        )
        if match_err is not None:
            return match_err

        assert new_content is not None

        # 5. 计算 Diff 与 ChangeStat
        old_lines_list = normalized_content.splitlines(keepends=True)
        new_lines_list = new_content.splitlines(keepends=True)

        diff_gen = difflib.unified_diff(
            old_lines_list,
            new_lines_list,
            fromfile=f"a/{args.path}",
            tofile=f"b/{args.path}",
        )
        diff_text = "".join(diff_gen)
        hunks = parse_unified_diff(diff_text)
        added_lines = sum(h.added for h in hunks)
        removed_lines = sum(h.removed for h in hunks)

        # 6. 还原换行符与 BOM，原子写回磁盘
        if is_crlf:
            # 还原为 \r\n
            final_text = new_content.replace("\n", "\r\n")
        else:
            final_text = new_content

        final_bytes = final_text.encode(encoding)
        if has_bom:
            final_bytes = _BOM_BYTES + final_bytes

        stat = ChangeStat(
            kind="modify",
            added=added_lines,
            removed=removed_lines,
            bytes_before=len(raw),
            bytes_after=len(final_bytes),
        )

        tmp_name = f".{target.name}.tmp_{uuid.uuid4().hex[:8]}"
        tmp_file = target.parent / tmp_name
        try:
            tmp_file.write_bytes(final_bytes)
            os.replace(tmp_file, target)
        except Exception as exc:
            return ToolResult.failure(
                ErrorCategory.TOOL_FAILURE,
                f"原子写回文件失败：{target}：{type(exc).__name__}: {exc}",
            )
        finally:
            if tmp_file.exists():
                try:
                    tmp_file.unlink(missing_ok=True)
                except Exception:
                    pass

        resolved_cwd = ctx.cwd.resolve()
        try:
            rel = target.relative_to(resolved_cwd)
        except ValueError:
            rel = target

        summary = f"已成功编辑文件 {rel}（+{added_lines} -{removed_lines} 行）{applied_note}"
        return ToolResult(
            ok=True,
            content=summary,
            change_stat=stat,
            display=DisplayHint(
                kind="diff",
                payload={
                    "path": str(rel),
                    "hunks": [asdict(h) for h in hunks],
                    "stat": stat.model_dump(),
                },
            ),
        )

    def _apply_matching_ladder(
        self,
        content: str,
        old_str: str,
        new_str: str,
        *,
        replace_all: bool,
    ) -> tuple[str | None, str, ToolResult | None]:
        """执行四级匹配阶梯。返回 (新内容, 提示附注, 失败结果)。"""
        # --- 级别 1：精确匹配 ---
        count = content.count(old_str)
        if count == 1:
            return content.replace(old_str, new_str, 1), "", None
        if count > 1:
            if replace_all:
                return content.replace(old_str, new_str), f"（全量替换了 {count} 处）", None
            # 收集所有出现位置的行号
            lines = content.splitlines()
            line_numbers: list[int] = []
            cur = 0
            for idx, line in enumerate(lines, 1):
                cur_end = cur + len(line) + 1
                if old_str in content[cur:cur_end]:
                    line_numbers.append(idx)
                cur = cur_end
            line_hint = ", ".join(f"第 {ln} 行" for ln in line_numbers[:5])
            if len(line_numbers) > 5:
                line_hint += f" 等共 {count} 处"
            return (
                None,
                "",
                ToolResult.failure(
                    ErrorCategory.BAD_REQUEST,
                    f"old_string 在文件中命中了 {count} 处，无法确定唯一替换位置",
                    detail=(
                        f"命中位置分布在：{line_hint}。\n"
                        "请在 old_string 前后多附带 1~2 行上下文代码以确保唯一匹配，"
                        "或者如果确实需要全部替换，请传入 replace_all=True。"
                    ),
                ),
            )

        # --- 级别 2：空白归一化行匹配 ---
        # 当模型少打了行尾空格、或缩进存在微小差异时进行归一化匹配
        normalized_res = self._match_normalized(content, old_str, new_str)
        if normalized_res is not None:
            return normalized_res, "（已通过空白归一化自动对齐替换）", None

        # --- 级别 3：计算最相近代码行号辅助自愈 ---
        nearest_line, nearest_snippet = self._find_nearest_block(content, old_str)
        hint = (
            f"最相近的代码片段位于第 {nearest_line} 行附近：\n{nearest_snippet}\n"
            "请比对上述片段并修正 old_string 后重新尝试。"
            if nearest_line > 0
            else "文件中未找到任何结构相近的代码片段，请用 read 工具重新核对文件最新内容。"
        )
        return (
            None,
            "",
            ToolResult.failure(
                ErrorCategory.BAD_REQUEST,
                "未在文件中找到与 old_string 匹配的内容",
                detail=hint,
            ),
        )

    def _match_normalized(self, content: str, old_str: str, new_str: str) -> str | None:
        """按行剔除行首尾多余空白后寻找唯一匹配。"""
        doc_lines = content.splitlines()
        target_lines = [line.strip() for line in old_str.splitlines() if line.strip()]
        if not target_lines:
            return None

        window_size = len(target_lines)
        matched_indices: list[int] = []

        for i in range(len(doc_lines) - window_size + 1):
            window = [doc_lines[i + k].strip() for k in range(window_size)]
            if window == target_lines:
                matched_indices.append(i)

        if len(matched_indices) == 1:
            start = matched_indices[0]
            # 把原文件中这几行替换为 new_str 的多行
            new_lines = new_str.splitlines()
            result_lines = doc_lines[:start] + new_lines + doc_lines[start + window_size :]
            # 保持末尾空行一致
            ends_with_newline = content.endswith("\n")
            out = "\n".join(result_lines)
            return out + "\n" if ends_with_newline else out
        return None

    def _find_nearest_block(self, content: str, old_str: str) -> tuple[int, str]:
        """寻找文件中与 old_str 相似度最高的代码片段及其行号。"""
        doc_lines = content.splitlines()
        target_lines = old_str.splitlines()
        window_size = max(1, len(target_lines))

        best_ratio = 0.0
        best_line = 0
        best_snippet = ""

        matcher = difflib.SequenceMatcher(None, target_lines, [])
        for i in range(len(doc_lines) - window_size + 1):
            candidate = doc_lines[i : i + window_size]
            matcher.set_seq2(candidate)
            ratio = matcher.ratio()
            if ratio > best_ratio and ratio > 0.4:
                best_ratio = ratio
                best_line = i + 1
                best_snippet = "\n".join(f"  {ln}: {l}" for ln, l in enumerate(candidate, best_line))

        return best_line, best_snippet


def build() -> EditTool:
    return EditTool()
