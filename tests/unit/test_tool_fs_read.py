"""``read`` 工具测试（MODULE_kernel_loop §7.3）。

这个工具看起来最简单，实际上有五个必须处理的边界：**编码、BOM、CRLF、二进制、超长行**。
每一个都对应一种"用户以为读到了、其实读到的是垃圾"的失败方式，所以逐个测。
"""

from __future__ import annotations

import unittest

from logox.errors import ErrorCategory
from logox.tools.base import ToolContext
from logox.tools.fs_read import DEFAULT_LIMIT, MAX_LINE_CHARS, ReadArgs, ReadTool
from tests.unit.support import make_temp_dir, remove_temp_dir


class ReadToolTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = make_temp_dir("read-")
        self.tool = ReadTool()
        self.ctx = ToolContext(cwd=self.root)

    def tearDown(self) -> None:
        remove_temp_dir(self.root)

    def write_bytes(self, name: str, payload: bytes):  # type: ignore[no-untyped-def]
        path = self.root / name
        path.write_bytes(payload)
        return path

    def write_text(self, name: str, text: str, encoding: str = "utf-8"):  # type: ignore[no-untyped-def]
        return self.write_bytes(name, text.encode(encoding))

    async def read(self, **kwargs: object):  # type: ignore[no-untyped-def]
        args = ReadArgs(**kwargs)  # type: ignore[arg-type]
        return await self.tool.run(args, self.ctx)


class BasicReadTests(unittest.IsolatedAsyncioTestCase, ReadToolTestCase):
    async def test_t40_basic_read_has_line_numbers(self) -> None:
        self.write_text("a.txt", "第一行\n第二行\n")
        result = await self.read(path="a.txt")
        self.assertTrue(result.ok)
        self.assertIn("1\t第一行", result.content)
        self.assertIn("2\t第二行", result.content)

    async def test_t41_content_has_no_ansi_escapes(self) -> None:
        """``content`` 是给模型的——ANSI 转义对它纯属噪声，还会白烧 token。"""
        self.write_text("a.txt", "正文\n")
        result = await self.read(path="a.txt")
        self.assertNotIn("\x1b[", result.content)

    async def test_t42_display_is_separate_from_content(self) -> None:
        self.write_text("a.txt", "正文\n")
        result = await self.read(path="a.txt")
        assert result.display is not None
        self.assertEqual(result.display.kind, "lines")
        self.assertEqual(result.display.payload["total_lines"], 1)  # 以换行结尾的文件只有 1 行

    async def test_t43_absolute_path_is_shown_in_the_header(self) -> None:
        """报错与摘要里必须是**解析后的绝对路径**，用户一眼能看出读的是哪个文件。"""
        self.write_text("a.txt", "x\n")
        result = await self.read(path="a.txt")
        self.assertIn(str(self.root.resolve()), result.content)

    async def test_t44_absolute_path_argument_works(self) -> None:
        path = self.write_text("a.txt", "x\n")
        result = await self.read(path=str(path))
        self.assertTrue(result.ok)

    async def test_t45_empty_file(self) -> None:
        self.write_text("empty.txt", "")
        result = await self.read(path="empty.txt")
        self.assertTrue(result.ok)


class WindowingTests(unittest.IsolatedAsyncioTestCase, ReadToolTestCase):
    async def test_t50_offset_and_limit_slice_precisely(self) -> None:
        self.write_text("a.txt", "".join(f"line{i}\n" for i in range(1, 21)))
        result = await self.read(path="a.txt", offset=5, limit=3)
        self.assertIn("5\tline5", result.content)
        self.assertIn("7\tline7", result.content)
        self.assertNotIn("8\tline8", result.content)

    async def test_t51_truncation_is_explicitly_reported(self) -> None:
        """★ 静默截断是本工具最危险的失误：模型会以为看到了整个文件。"""
        self.write_text("a.txt", "".join(f"line{i}\n" for i in range(1, 51)))
        result = await self.read(path="a.txt", limit=10)
        self.assertIn("共 50 行", result.content)  # 末尾换行不算多一行
        self.assertIn("offset=11", result.content)  # 明确告诉它怎么继续读

    async def test_t52_offset_beyond_eof_is_not_an_error(self) -> None:
        self.write_text("a.txt", "只有一行\n")
        result = await self.read(path="a.txt", offset=99)
        self.assertTrue(result.ok)
        self.assertIn("超出", result.content)

    async def test_t53_default_limit_is_declared(self) -> None:
        self.assertGreater(DEFAULT_LIMIT, 0)
        self.assertEqual(ReadArgs(path="x").limit, DEFAULT_LIMIT)

    async def test_t54_display_reports_the_window(self) -> None:
        self.write_text("a.txt", "".join(f"l{i}\n" for i in range(30)))
        result = await self.read(path="a.txt", offset=3, limit=5)
        assert result.display is not None
        self.assertEqual(result.display.payload["start"], 3)
        self.assertEqual(result.display.payload["end"], 7)
        self.assertTrue(result.display.payload["truncated"])


class FailureTests(unittest.IsolatedAsyncioTestCase, ReadToolTestCase):
    async def test_t60_missing_file_reports_path_and_reason(self) -> None:
        """D44 四要素：路径 + 原因 + 下一步。"""
        result = await self.read(path="nope.txt")
        self.assertFalse(result.ok)
        assert result.error is not None
        self.assertIs(result.error.category, ErrorCategory.BAD_REQUEST)
        self.assertIn("nope.txt", result.content)
        self.assertIn("文件不存在", result.content)
        self.assertTrue(result.error.category.feedable_to_model)

    async def test_t61_directory_is_refused_with_a_hint(self) -> None:
        (self.root / "sub").mkdir()
        result = await self.read(path="sub")
        self.assertFalse(result.ok)
        self.assertIn("目录", result.content)
        self.assertIn("glob", result.content)  # 告诉模型该用哪个工具

    async def test_t62_binary_file_is_not_poured_into_the_context(self) -> None:
        """二进制不拦 → 回灌几十 KB 控制字符，白烧上下文还会让模型胡言乱语。"""
        self.write_bytes("bin.dat", b"\x00\x01\x02\x03" * 500)
        result = await self.read(path="bin.dat")
        self.assertTrue(result.ok)
        self.assertIn("二进制文件", result.content)
        assert result.display is not None
        self.assertIs(result.display.payload["binary"], True)
        self.assertNotIn("\x00", result.content)

    async def test_t63_high_control_byte_ratio_is_also_binary(self) -> None:
        self.write_bytes("weird.dat", bytes(range(1, 9)) * 200)
        result = await self.read(path="weird.dat")
        self.assertIn("二进制文件", result.content)

    async def test_t64_invalid_arguments_are_rejected_by_the_model(self) -> None:
        from pydantic import ValidationError

        with self.assertRaises(ValidationError):
            ReadArgs(path="a.txt", offset=0)  # ge=1
        with self.assertRaises(ValidationError):
            ReadArgs(path="a.txt", limit=999_999)  # le=20000
        with self.assertRaises(ValidationError):
            ReadArgs(path="a.txt", unexpected=True)


class EncodingTests(unittest.IsolatedAsyncioTestCase, ReadToolTestCase):
    async def test_t70_gbk_falls_back(self) -> None:
        """中文环境最常见的编码。乱码回灌给模型，它会**基于乱码继续推理**。"""
        self.write_text("gbk.txt", "你好，世界\n", encoding="gbk")
        result = await self.read(path="gbk.txt")
        self.assertTrue(result.ok)
        self.assertIn("你好，世界", result.content)
        self.assertIn("gbk", result.content)  # 如实说明用了回退编码

    async def test_t71_utf8_bom_is_stripped(self) -> None:
        """不剥 BOM → 第一行凭空多一个不可见字符，后续 edit 的匹配永远失败。"""
        self.write_bytes("bom.txt", "\ufeff第一行\n第二行\n".encode("utf-8-sig"))
        result = await self.read(path="bom.txt")
        self.assertNotIn("\ufeff", result.content)
        self.assertIn("1\t第一行", result.content)

    async def test_t72_plain_utf8_reports_no_fallback(self) -> None:
        self.write_text("u.txt", "普通\n")
        result = await self.read(path="u.txt")
        self.assertNotIn("解码", result.content)

    async def test_t73_crlf_line_endings_are_stripped(self) -> None:
        """行尾的 ``\\r`` 若进了内容，模型会照抄进 ``edit`` 的 ``old_string`` 里。"""
        self.write_bytes("crlf.txt", b"first\r\nsecond\r\n")
        result = await self.read(path="crlf.txt")
        self.assertNotIn("\r", result.content)
        self.assertIn("1\tfirst", result.content)
        self.assertIn("2\tsecond", result.content)

    async def test_t74_undecodable_bytes_still_yield_text(self) -> None:
        """``latin-1`` 兜底永不失败——宁可给出确定但可能不完美的文本，也不要抛异常。"""
        self.write_bytes("raw.bin", b"\xff\xfe not utf8 and not gbk \xc3\x28")
        result = await self.read(path="raw.bin")
        self.assertTrue(result.ok)

    async def test_t75_utf16_le_from_powershell_redirection(self) -> None:
        """PowerShell 的 ``>`` 重定向产出 UTF-16LE；按 GBK 解会得到满屏乱码。"""
        self.write_bytes("ps.txt", "第一行\n第二行\n".encode("utf-16"))
        result = await self.read(path="ps.txt")
        self.assertIn("第一行", result.content)
        self.assertNotIn("\x00", result.content)

    async def test_t76_trailing_newline_does_not_add_a_phantom_line(self) -> None:
        """``"a\\n".split("\\n")`` 是 ``["a", ""]``——直接算行数会让**每个以换行结尾的
        文件**（也就是绝大多数文件）都多报一行。"""
        self.write_bytes("three.txt", b"a\nb\nc\n")
        result = await self.read(path="three.txt")
        self.assertIn("共 3 行", result.content)
        assert result.display is not None
        self.assertEqual(result.display.payload["total_lines"], 3)

    async def test_t77_file_without_a_trailing_newline_is_counted_too(self) -> None:
        self.write_bytes("two.txt", b"a\nb")
        result = await self.read(path="two.txt")
        self.assertIn("共 2 行", result.content)


class LongLineTests(unittest.IsolatedAsyncioTestCase, ReadToolTestCase):
    async def test_t80_overlong_line_is_clipped_and_reported(self) -> None:
        """一个压缩成一行的 5 MB 文件会当场撑爆上下文。"""
        self.write_text("long.txt", "x" * (MAX_LINE_CHARS * 3) + "\n")
        result = await self.read(path="long.txt")
        self.assertIn(f"超过 {MAX_LINE_CHARS} 字符", result.content)
        self.assertLess(len(result.content), MAX_LINE_CHARS * 3)

    async def test_t81_digest_stays_one_short_line(self) -> None:
        self.write_text("long.txt", "y" * 5000 + "\n")
        result = await self.read(path="long.txt")
        self.assertLessEqual(len(result.digest), 80)


class SpecTests(unittest.TestCase):
    def test_t90_read_is_declared_readonly(self) -> None:
        """``readonly`` 是 D27 并发决策的**唯一依据**，标错会导致写操作并发执行。"""
        self.assertTrue(ReadTool.spec.readonly)

    def test_t91_read_needs_no_permission_prompt(self) -> None:
        self.assertFalse(ReadTool.spec.requires_permission)

    def test_t92_schema_matches_the_params_model(self) -> None:
        schema = ReadTool.spec.schema()
        self.assertEqual(schema.name, "read")
        self.assertIn("path", schema.parameters["properties"])
        self.assertEqual(schema.parameters["required"], ["path"])
        self.assertNotIn("title", schema.parameters)

    def test_t93_summary_renders_the_path(self) -> None:
        self.assertEqual(ReadTool.spec.summary({"path": "src/a.py"}), "读取 src/a.py")

    def test_t94_description_tells_the_model_how_to_continue(self) -> None:
        """描述要说明**边界**（截断后怎么办），而不是复述字段名。"""
        self.assertIn("limit", ReadTool.spec.description)
        self.assertIn("offset", ReadTool.spec.description)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
