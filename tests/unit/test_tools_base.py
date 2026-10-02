"""工具协议与注册表契约测试（MODULE_kernel_loop §7）。

关注三件事：**只读声明是并发决策的唯一依据**、**失败是结果不是异常**、
**重名注册绝不静默覆盖**。
"""

from __future__ import annotations

import unittest

from pydantic import BaseModel, Field, ValidationError

from logox.errors import ErrorCategory
from logox.kernel.registry import (
    InvalidToolError,
    ToolNameConflictError,
    ToolRegistry,
    UnsafeToolError,
)
from logox.tools.base import (
    DisplayHint,
    Tool,
    ToolContext,
    ToolResult,
    ToolSpec,
    schema_from_model,
)


class EchoArgs(BaseModel):
    text: str = Field(description="要回显的文本")


class EchoTool:
    spec = ToolSpec(
        name="echo",
        description="回显",
        params=EchoArgs,
        readonly=True,
        requires_permission=False,
        summary_template="回显 {text}",
    )

    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, EchoArgs)
        return ToolResult(ok=True, content=args.text, display=DisplayHint(kind="text"))


class WriteTool:
    spec = ToolSpec(name="writer", params=EchoArgs, readonly=False)

    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:  # pragma: no cover
        return ToolResult(ok=True)


class NotATool:
    """没有 spec、也没有 run——注册它必须在装配期就报错。"""


class ToolSpecTests(unittest.TestCase):
    def test_t01_spec_is_frozen_and_closed(self) -> None:
        spec = ToolSpec(name="x")
        with self.assertRaises(ValidationError):
            spec.name = "y"  # type: ignore[misc]
        with self.assertRaises(ValidationError):
            ToolSpec(name="x", readonly_flg=True)  # type: ignore[call-arg]

    def test_t02_defaults_are_conservative(self) -> None:
        """默认 ``readonly=False``：**忘了声明时按"会写"处理**，宁可串行也不并发。"""
        spec = ToolSpec(name="x")
        self.assertFalse(spec.readonly)
        self.assertTrue(spec.requires_permission)

    def test_t03_schema_is_generated_from_the_model(self) -> None:
        """D26：JSON Schema 由 pydantic 生成，不手写。"""
        schema = ToolSpec(name="echo", params=EchoArgs).schema()
        self.assertEqual(schema.name, "echo")
        self.assertEqual(schema.parameters["type"], "object")
        self.assertIn("text", schema.parameters["properties"])
        self.assertEqual(schema.parameters["required"], ["text"])

    def test_t04_generated_schema_drops_pydantic_title_noise(self) -> None:
        """``title`` 是 pydantic 给类加的，对模型理解参数没帮助，只会白占 token。"""
        self.assertNotIn("title", schema_from_model(EchoArgs))

    def test_t05_summary_uses_the_template(self) -> None:
        spec = ToolSpec(name="echo", params=EchoArgs, summary_template="回显 {text}")
        self.assertEqual(spec.summary({"text": "你好"}), "回显 你好")

    def test_t06_summary_never_raises_on_missing_keys(self) -> None:
        """模板缺字段时退回工具名——摘要渲染失败绝不该让一次工具调用崩掉。"""
        spec = ToolSpec(name="echo", params=EchoArgs, summary_template="回显 {missing}")
        self.assertEqual(spec.summary({}), "echo")

    def test_t07_empty_params_model_gives_an_empty_object_schema(self) -> None:
        schema = ToolSpec(name="x").schema()
        self.assertEqual(schema.parameters.get("type"), "object")


class ToolResultTests(unittest.TestCase):
    def test_t08_failure_keeps_content_feedable_to_the_model(self) -> None:
        """失败必须带上给模型看的文本——否则模型会以为工具成功返回了空内容。"""
        result = ToolResult.failure(ErrorCategory.BAD_REQUEST, "参数不对", detail="path: 必填")
        self.assertFalse(result.ok)
        self.assertIn("参数不对", result.content)
        self.assertIn("path: 必填", result.content)
        assert result.error is not None
        self.assertIs(result.error.category, ErrorCategory.BAD_REQUEST)

    def test_t09_bad_request_failure_is_feedable(self) -> None:
        """D22：``BAD_REQUEST`` 可回灌让模型自愈。"""
        result = ToolResult.failure(ErrorCategory.BAD_REQUEST, "x")
        assert result.error is not None
        self.assertTrue(result.error.category.feedable_to_model)

    def test_t10_digest_is_one_short_line(self) -> None:
        result = ToolResult(ok=True, content="第一行\n第二行\n第三行")
        self.assertEqual(result.digest, "第一行")

    def test_t11_digest_truncates_long_lines(self) -> None:
        result = ToolResult(ok=True, content="x" * 500)
        self.assertEqual(len(result.digest), 78)  # 77 + 省略号
        self.assertTrue(result.digest.endswith("…"))

    def test_t12_digest_of_empty_content_is_empty(self) -> None:
        self.assertEqual(ToolResult(ok=True, content="   ").digest, "")

    def test_t13_display_is_separate_from_content(self) -> None:
        """``content`` 给模型、``display`` 给人——两者混过一次就再也拆不开。"""
        result = ToolResult(ok=True, content="干净文本", display=DisplayHint(kind="diff", payload={"a": 1}))
        self.assertEqual(result.content, "干净文本")
        self.assertNotIn("diff", result.content)


class ToolRegistryTests(unittest.TestCase):
    def test_t14_register_and_lookup(self) -> None:
        registry = ToolRegistry()
        registry.register(EchoTool())
        self.assertIsNotNone(registry.get("echo"))
        self.assertIn("echo", registry)
        self.assertEqual(len(registry), 1)

    def test_t15_unknown_name_returns_none_not_raise(self) -> None:
        """模型幻觉出一个工具名是最常见的错误；抛异常等于用报错打断会话。"""
        self.assertIsNone(ToolRegistry().get("nope"))
        self.assertIsNone(ToolRegistry().spec("nope"))

    def test_t16_duplicate_registration_is_refused(self) -> None:
        """静默覆盖会让内置工具"凭空消失"，而症状极难排查。"""
        registry = ToolRegistry()
        registry.register(EchoTool())
        with self.assertRaises(ToolNameConflictError):
            registry.register(EchoTool())

    def test_t17_non_tool_object_is_refused_at_assembly_time(self) -> None:
        with self.assertRaises(InvalidToolError):
            ToolRegistry().register(NotATool())  # type: ignore[arg-type]

    def test_t18_schemas_are_sorted_and_complete(self) -> None:
        registry = ToolRegistry()
        registry.register(EchoTool())
        registry.register(WriteTool())
        self.assertEqual([schema.name for schema in registry.schemas()], ["echo", "writer"])

    def test_t19_readonly_lookup(self) -> None:
        registry = ToolRegistry()
        registry.register(EchoTool())
        registry.register(WriteTool())
        self.assertTrue(registry.is_readonly("echo"))
        self.assertFalse(registry.is_readonly("writer"))

    def test_t20_unknown_tool_is_conservatively_a_writer(self) -> None:
        """D27 的边界用例②：未声明只读注解 → 保守视为写（串行）。"""
        self.assertFalse(ToolRegistry().is_readonly("mcp__something"))

    def test_t21_safety_gate_accepts_readonly_only(self) -> None:
        registry = ToolRegistry()
        registry.register(EchoTool())
        registry.assert_no_writers()  # 不抛

    def test_t22_safety_gate_rejects_writers(self) -> None:
        """§5.6：没有权限系统时存在写工具 → 装配期即失败，而不是某天静默改文件。"""
        registry = ToolRegistry()
        registry.register(EchoTool())
        registry.register(WriteTool())
        with self.assertRaises(UnsafeToolError) as ctx:
            registry.assert_no_writers()
        self.assertIn("writer", str(ctx.exception))
        self.assertIn("LOGOX_ALLOW_UNSAFE_TOOLS", str(ctx.exception))

    def test_t23_writers_lists_names(self) -> None:
        registry = ToolRegistry()
        registry.register(EchoTool())
        registry.register(WriteTool())
        self.assertEqual(registry.writers(), ["writer"])

    def test_t24_protocol_is_runtime_checkable(self) -> None:
        self.assertIsInstance(EchoTool(), Tool)
        self.assertNotIsInstance(NotATool(), Tool)


class EchoToolRunTests(unittest.IsolatedAsyncioTestCase):
    async def test_t25_tool_runs_and_returns_result(self) -> None:
        ctx = ToolContext(cwd=_cwd())
        result = await EchoTool().run(EchoArgs(text="你好"), ctx)
        self.assertTrue(result.ok)
        self.assertEqual(result.content, "你好")

    async def test_t26_tool_context_reports_cancellation(self) -> None:
        flag = {"v": False}
        ctx = ToolContext(cwd=_cwd(), is_cancelled=lambda: flag["v"])
        self.assertFalse(ctx.is_cancelled())
        flag["v"] = True
        self.assertTrue(ctx.is_cancelled())


class NoParamToolTests(unittest.TestCase):
    def test_t27_tool_without_params_validates_empty_args(self) -> None:
        """无参数工具是合法的：默认参数模型接受空 dict（比 ``None`` 少一层分支）。"""
        args = ToolSpec(name="ping").params.model_validate({})
        self.assertEqual(args.model_dump(), {})

    def test_t28_tool_without_params_rejects_unknown_keys(self) -> None:
        with self.assertRaises(ValidationError):
            ToolSpec(name="ping").params.model_validate({"unexpected": 1})


def _cwd():  # type: ignore[no-untyped-def]
    from pathlib import Path

    return Path.cwd()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
