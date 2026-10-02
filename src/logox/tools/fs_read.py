"""``read`` 工具——M3 唯一的具体工具（L4）。

它看起来最简单，实际上有五个必须处理的边界：**编码、BOM、CRLF、二进制、超长行**。
每一个都对应一种"用户以为读到了、其实读到的是垃圾"的失败方式：

* 用错编码 → 中文变乱码，而模型会**基于乱码继续推理**
* 不剥 BOM → 第一行凭空多一个不可见字符，后续 ``edit`` 的匹配永远失败
* 不处理 CRLF → 行尾带 ``\\r``，回灌给模型后它照抄进 ``edit`` 的 ``old_string`` 里
* 二进制不拦 → 回灌几十 KB 的控制字符，白烧上下文还会让模型胡言乱语
* 超长行不截 → 一个压缩成一行的 5 MB 文件会当场撑爆上下文

因此这里的策略是：**宁可明确告诉模型"这里我做了处理"，也绝不静默给出错误的内容。**
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

from pydantic import Field

from logox.errors import ErrorCategory
from logox.tools.base import DisplayHint, ToolArgs, ToolContext, ToolResult, ToolSpec

__all__ = ["ReadArgs", "ReadTool", "build"]

#: 单次默认可读的行数。与 ``context.max_tool_result_chars=8000`` 同量级：
#: 2000 行的代码大约是 1–2 万 token，一次读完一个中等文件而不撑爆上下文。
DEFAULT_LIMIT = 2000

#: 单行最大字符数（超长行的截断阈值）
MAX_LINE_CHARS = 2000

#: 用来判定"这是二进制"的探测字节数
_BINARY_PROBE_BYTES = 8192

#: 解码回退顺序。UTF-8 优先（绝大多数现代代码），其次是中文环境最常见的 GBK，
#: 最后用 ``latin-1`` 兜底——它**永不失败**，保证任何字节序列都能给出确定的文本，
#: 而不是抛一个 "can't decode byte" 让模型完全拿不到内容。
_ENCODINGS = ("utf-8", "gbk", "latin-1")

#: BOM 前缀 → 该用什么编码解码。**必须先于 :data:`_ENCODINGS` 判断**：
#: UTF-8 的 BOM 是合法 UTF-8，所以朴素地先试 ``utf-8`` 会"成功"解出一个
#: 开头带 ``\ufeff`` 的字符串——这正是那个"第一行凭空多一个不可见字符"的经典 bug。
_BOMS: tuple[tuple[bytes, str], ...] = (
    (b"\xef\xbb\xbf", "utf-8-sig"),
    (b"\xff\xfe", "utf-16-le"),  # PowerShell 的 `>` 重定向就产出这个
    (b"\xfe\xff", "utf-16-be"),
)


class ReadArgs(ToolArgs):
    """``read`` 的参数。**描述写给模型看**，所以要说明边界而不是复述字段名。"""

    path: str = Field(description="要读取的文件路径（相对当前工作目录或绝对路径）")
    offset: int = Field(
        default=1,
        ge=1,
        description="从第几行开始读（1 起）。文件很大时用它翻页",
    )
    limit: int = Field(
        default=DEFAULT_LIMIT,
        ge=1,
        le=20_000,
        description=f"最多读多少行（默认 {DEFAULT_LIMIT}）",
    )


class ReadTool:
    """读取文本文件，带行号返回。"""

    spec = ToolSpec(
        name="read",
        description=(
            "读取一个文本文件的内容，返回带行号的文本。"
            "超过 limit 行时只返回前 limit 行并说明总行数——请据此用 offset 继续读。"
        ),
        params=ReadArgs,
        readonly=True,
        requires_permission=False,
        summary_template="读取 {path}",
    )

    async def run(self, args: ToolArgs, ctx: ToolContext) -> ToolResult:
        return await run_readonly_worker(self._run_sync, args, ctx)

    def _run_sync(self, args: ToolArgs, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, ReadArgs)
        target = _resolve(ctx.cwd, args.path)

        if not target.exists():
            return ToolResult.failure(
                ErrorCategory.BAD_REQUEST,
                f"文件不存在：{target}",
                detail="请检查路径是否正确；也可以用 glob 工具先看看目录里有什么。",
            )
        if target.is_dir():
            return ToolResult.failure(
                ErrorCategory.BAD_REQUEST,
                f"{target} 是一个目录，不是文件",
                detail="要列出目录内容请用 glob 工具。",
            )

        if ctx.is_cancelled():
            return ToolResult.failure(ErrorCategory.CANCELLED, "读取操作已被取消")
        raw = target.read_bytes()
        if ctx.is_cancelled():
            return ToolResult.failure(ErrorCategory.CANCELLED, "读取操作已被取消")
        if _looks_binary(raw):
            return ToolResult(
                ok=True,
                content=f"{target} 看起来是二进制文件（{len(raw)} 字节），没有作为文本读取。",
                display=DisplayHint(
                    kind="text",
                    payload={"path": str(target), "size": len(raw), "binary": True},
                ),
            )

        text, encoding = _decode(raw)
        lines = _split_lines(text)
        total = len(lines)
        start = args.offset - 1
        if start >= total:
            return ToolResult(
                ok=True,
                content=f"文件共 {total} 行，offset={args.offset} 超出了文件末尾，没有内容可读。",
                display=DisplayHint(kind="text", payload={"path": str(target), "total_lines": total}),
            )

        window = lines[start : start + args.limit]
        rendered: list[str] = []
        clipped_lines = 0
        for number, line in enumerate(window, start=args.offset):
            body = line.rstrip("\r")  # CRLF：行尾的 \r 绝不能进内容
            if len(body) > MAX_LINE_CHARS:
                body = body[:MAX_LINE_CHARS]
                clipped_lines += 1
            rendered.append(f"{number}\t{body}")

        end = args.offset + len(window) - 1
        header = _header(target, encoding, args, total, end, clipped_lines)
        return ToolResult(
            ok=True,
            content=header + "\n".join(rendered),
            display=DisplayHint(
                kind="lines",
                payload={
                    "path": str(target),
                    "start": args.offset,
                    "end": end,
                    "total_lines": total,
                    "encoding": encoding,
                    "truncated": end < total,
                },
            ),
        )


# --------------------------------------------------------------------------- #
# 内部
# --------------------------------------------------------------------------- #


def _resolve(cwd: Path, raw_path: str) -> Path:
    """把用户给的路径解析成绝对路径。

    **刻意允许读到项目外**（``../`` 之类）：``read`` 是只读的，读不到比读到更糟，
    而"能不能读到"属于权限问题、不属于工具问题（M6 的 ``permissions/`` 负责）。
    这里只保证报错与摘要里显示的是**解析后的绝对路径**，用户一眼能看出读的是哪个文件。
    """
    candidate = Path(raw_path)
    return candidate if candidate.is_absolute() else (Path(cwd) / candidate).resolve()


def _looks_binary(raw: bytes) -> bool:
    """字节探测：出现 NUL 或高比例的控制字符即判定为二进制。

    **BOM 必须先于这条判断**：UTF-16 的文本**满篇都是 NUL 字节**，
    按"含 NUL 就是二进制"会被判成二进制——而 PowerShell 的 ``>`` 重定向
    产出的正是 UTF-16LE。先认 BOM，这类文件才会被当作文本读进来。
    """
    if raw.startswith(tuple(bom for bom, _ in _BOMS)):
        return False
    if b"\x00" in raw[:_BINARY_PROBE_BYTES]:
        return True
    probe = raw[:_BINARY_PROBE_BYTES]
    if not probe:
        return False
    control = sum(1 for byte in probe if byte < 9 or 13 < byte < 32)
    return control / len(probe) > 0.15


def _decode(raw: bytes) -> tuple[str, str]:
    """按 BOM → :data:`_ENCODINGS` 的顺序解码，返回 ``(文本, 实际编码)``。"""
    for bom, encoding in _BOMS:
        if raw.startswith(bom):
            try:
                # 一并剥掉 BOM：不剥的话第一行会凭空多一个不可见字符，
                # 后续 edit 的精确匹配会永远失败，而用户完全看不出为什么。
                return raw.decode(encoding).lstrip("\ufeff"), encoding
            except UnicodeDecodeError:  # pragma: no cover - BOM 与实际编码不符
                break
    for encoding in _ENCODINGS:
        try:
            text = raw.decode(encoding)
        except UnicodeDecodeError:
            continue
        return text.lstrip("\ufeff") if text.startswith("\ufeff") else text, encoding
    # latin-1 永不失败，理论上到不了这里
    return raw.decode("latin-1", errors="replace"), "latin-1"  # pragma: no cover


def _split_lines(text: str) -> list[str]:
    """按行切分，并**丢掉末尾那个由换行符造成的空元素**。

    ``"a\\n".split("\\n")`` 得到 ``["a", ""]``——直接拿它算行数，会让
    **每一个以换行结尾的文件**（也就是绝大多数文件）都多报一行。
    """
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def _header(target: Path, encoding: str, args: ReadArgs, total: int, end: int, clipped: int) -> str:
    """给模型的一行说明。

    **截断必须说出来。** 静默截断是本工具最危险的失误：模型会以为看到了整个文件，
    然后基于"剩下的部分"下结论——而它根本没看到剩下的部分。
    """
    parts = [f"{target}（共 {total} 行"]
    if end < total:
        parts.append(f"，本次显示第 {args.offset}–{end} 行；要继续读请用 offset={end + 1}")
    if encoding not in ("utf-8", "utf-8-sig"):
        parts.append(f"；按 {encoding} 解码")
    if clipped:
        parts.append(f"；有 {clipped} 行超过 {MAX_LINE_CHARS} 字符已被截断")
    return "".join(parts) + "）\n"


def build() -> ReadTool:
    """工厂函数——注册表要的是实例，而 L2 不该 import 具体的类名。"""
    return ReadTool()


async def run_readonly_worker(worker, args: ToolArgs, ctx: ToolContext) -> ToolResult:
    """Move filesystem reads off the event loop and signal cancellation to workers."""
    stop = threading.Event()
    worker_ctx = ctx.model_copy(update={"is_cancelled": lambda: stop.is_set() or ctx.is_cancelled()})
    task = asyncio.create_task(asyncio.to_thread(worker, args, worker_ctx))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        stop.set()
        # A blocking OS read cannot be forcibly interrupted; consume its eventual result.
        task.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        raise
