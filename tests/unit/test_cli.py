"""命令行入口的单元测试（D29：``--version`` 快速路径 < 300ms）。

分为两类：
* **进程内**用例：参数处理、退出码、输出内容（用 ``mock`` 把用户目录重定向到临时目录，
  确保**绝不读写真实 ``~/.logox``**）
* **子进程**用例：真实冷启动耗时——这是 D29 指标的**唯一可信测法**
  （进程内调用测不到解释器启动与模块导入成本）
"""

from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import time
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from logox.cli import EXIT_CONFIG_ERROR, EXIT_OK, _context_window_of, main
from logox.paths import LogoxPaths
from tests.unit.support import TEMP_ROOT, make_temp_dir, remove_temp_dir

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = REPO_ROOT / "src"

VERSION_BUDGET_MS = 300.0
"""D29：``--version`` 快速路径的硬指标。"""




class Utf8StdioTests(unittest.TestCase):
    """★ D154：stdio 编码修复必须在**代码里**（不能靠启动脚本的环境变量）。

    为什么值得两条（其中一条还是**反向**断言）：

    * 界面层用 ``sys.stdout.write()`` 写**文本**（``✓ ▎ ⏺`` 与中文）；
    * 真正会炸的场景是 **stdout 被重定向**（管道 / 写文件）—— 那时 CPython 用 locale
      编码（Windows 上常见 cp936）包装文本流，字形直接 ``UnicodeEncodeError``；
    * ``uv tool install`` 生成的 ``logox.exe``（本项目的推荐入口）**不设任何环境变量**，
      所以这件事必须由代码自己保证。

    反向那条的意义：它把"没有这层修复会怎样"钉在测试里 ——
    否则将来有人删掉 `_force_utf8_stdio()`，正向用例可能仍是绿的（比如在 UTF-8 的 CI 上），
    而问题只会在用户的 Windows 机器上复现。
    """

    def _run_probe(self, code: str) -> tuple[int, bytes]:
        import os
        import subprocess
        import sys
        from pathlib import Path

        from tests.unit.test_imports import SRC_DIR

        out_path = Path(__file__).resolve().parents[2] / ".test-tmp" / "utf8-probe.bin"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ)
        env["PYTHONPATH"] = str(SRC_DIR)
        # 故意把 locale 编码设成 GBK：模拟用户的 Windows 控制台/重定向环境
        env["PYTHONIOENCODING"] = "cp936"
        # 沙箱禁止管道捕获子进程输出（DEV-ENVIRONMENT §4.2）→ 重定向到文件再读
        with out_path.open("wb") as handle:
            result = subprocess.run(  # noqa: S603 - 固定参数，无 shell
                [sys.executable, "-c", code], env=env, stdout=handle, stderr=handle
            )
        return result.returncode, out_path.read_bytes()

    def test_t01_forced_utf8_keeps_glyphs_alive(self) -> None:
        code = (
            "import sys;"
            "from logox.cli import _force_utf8_stdio;"
            "_force_utf8_stdio();"
            "sys.stdout.write('✓ 中文')"
        )
        code_ascii, data = self._run_probe(code)
        self.assertEqual(code_ascii, 0, data.decode("utf-8", errors="replace"))
        self.assertEqual(data, "✓ 中文".encode("utf-8"), "字形没有以 UTF-8 落盘")

    def test_t02_without_the_fix_it_would_fail(self) -> None:
        """反向断言：**不调** `_force_utf8_stdio()` ⇒ cp936 下直接抛异常。"""
        code = "import sys; sys.stdout.write('✓ 中文')"
        code_ascii, data = self._run_probe(code)
        self.assertNotEqual(code_ascii, 0, "没有这层修复竟然也成功了？那这条守卫就失去意义")
        self.assertIn(b"UnicodeEncodeError", data)


class CliInProcessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = make_temp_dir("cli-")
        self.addCleanup(remove_temp_dir, self.root)
        self.paths = LogoxPaths.at(self.root / "userhome")
        self.paths.ensure_dirs()
        self.project = self.root / "proj"
        (self.project / ".logox").mkdir(parents=True)

        patcher = mock.patch.object(LogoxPaths, "default", classmethod(lambda cls: self.paths))
        patcher.start()
        self.addCleanup(patcher.stop)

    # -- 辅助 ----------------------------------------------------------- #

    def invoke(self, *args: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["--cwd", str(self.project), *args])
        return code, out.getvalue(), err.getvalue()

    def write_config(self, text: str) -> None:
        (self.paths.config).write_text(text, encoding="utf-8")

    # -- 用例 ----------------------------------------------------------- #

    def test_version_fast_path(self) -> None:
        """``--version`` 打印版本并以 0 退出。"""
        code, out, _ = self.invoke("--version")
        self.assertEqual(code, EXIT_OK)
        self.assertRegex(out.strip(), r"^logox \d+\.\d+\.\d+$")

    def test_version_does_not_load_config_or_paths(self) -> None:
        """``--version`` **不得**触碰配置层——它是惰性导入红线的行为验证。

        ⚠️ 这里必须用 ``mock.patch.dict`` 而不是裸 ``sys.modules.pop``：
        后者会把 ``logox.config.loader`` **永久**从 ``sys.modules`` 里抹掉，
        于是本进程内**之后**任何 ``from logox.config.loader import ...`` 都会
        重新导入出一个**全新的模块对象**并把包属性指向它——而在此之前已经
        ``import`` 过它的测试模块，其函数仍然绑定在**旧对象**的 globals 上。

        后果极其难查：`mock.patch.object(loader, "_strip")` 打在 A 对象上，
        实际执行的却是 B 对象里的代码，补丁静默失效，测试以一条看似
        "业务逻辑不对"的断言失败收场。``patch.dict`` 在退出时把原对象放回去，
        检查的目的达到了，进程状态却分毫未动。
        """
        self.assertIn("logox.config.loader", sys.modules, "前置条件：配置层应已被导入")
        with mock.patch.dict(sys.modules):
            sys.modules.pop("logox.config.loader", None)
            self.invoke("--version")
            self.assertNotIn(
                "logox.config.loader",
                sys.modules,
                "--version 不得导入配置层（否则 D29 指标会失守）",
            )
        self.assertIn(
            "logox.config.loader",
            sys.modules,
            "检查结束后必须把原模块对象放回去，否则会污染同进程内的后续测试",
        )

    def test_check_config_reports_missing_model(self) -> None:
        """全新安装必须先告诉用户「用哪个模型」——这是启动时最重要的信息。"""
        code, out, _ = self.invoke("--check-config")
        self.assertEqual(code, EXIT_CONFIG_ERROR)
        self.assertIn("provider.model", out)
        self.assertIn("未指定模型", out)

    def test_check_config_ok_with_valid_config(self) -> None:
        self.write_config(
            'schema_version = 1\n[provider]\nname = "openai-compatible"\nmodel = "m"\n'
        )
        code, out, _ = self.invoke("--check-config")
        self.assertEqual(code, EXIT_OK)
        self.assertIn("openai-compatible / m", out)
        self.assertIn("工作目录", out)
        self.assertIn("状态栏", out)

    def test_check_config_shows_sources(self) -> None:
        """启动摘要必须公示配置来源（UI-SPEC §5.12）。"""
        self.write_config(
            'schema_version = 1\n[provider]\nname = "openai-compatible"\nmodel = "m"\n'
        )
        _, out, _ = self.invoke("--check-config")
        self.assertIn("来源·global", out)

    def test_strict_config_fails_on_warning_only_config(self) -> None:
        """D44 ①：``--strict-config`` 对**任何** issue（含 warning）都以非 0 退出。"""
        self.write_config(
            'schema_version = 1\n[provider]\nname = "openai-compatible"\nmodel = "m"\n'
            '[ui]\ntema = "logox-dark"\n'
        )
        lenient_code, _, _ = self.invoke("--check-config")
        self.assertEqual(lenient_code, EXIT_OK, "未知字段只是 warning，默认不阻断")

        strict_code, _, err = self.invoke("--check-config", "--strict-config")
        self.assertEqual(strict_code, EXIT_CONFIG_ERROR)
        self.assertIn("ui.tema", err)

    def test_print_config_emits_parsable_toml(self) -> None:
        """``--print-config`` 的输出必须能被标准库 ``tomllib`` 解析，且覆盖全部配置段。"""
        self.write_config(
            'schema_version = 1\n[provider]\nname = "openai-compatible"\nmodel = "m"\n'
        )
        code, out, _ = self.invoke("--print-config")
        self.assertEqual(code, EXIT_OK)

        parsed = tomllib.loads(out)
        for section in (
            "provider",
            "context",
            "shell",
            "permissions",
            "tools",
            "hooks",
            "mcp",
            "ui",
            "session",
            "plugins",
        ):
            self.assertIn(section, parsed, f"输出缺少 [{section}] 段")
        self.assertEqual(parsed["provider"]["model"], "m")
        self.assertEqual(parsed["ui"]["theme"], "logox-dark")

    def test_default_invocation_dispatches_to_the_new_interface(self) -> None:
        """`logox`（无参数）现在进入**新界面**（`tui/render`，D80 第 7 步的切换点）。

        ⚠️ 这是一个真实的测试事故留下的教训：本用例原来写的是"缺密钥时以非 0 退出"，
        而 M4 之后缺密钥**不再阻止启动**（`/login` 就是用来配密钥的）。
        于是裸调 ``main()`` 会**真的把界面跑起来**——在测试进程里它没有终端，
        结果是**整个测试套件挂死**（实测：跑到这里再无输出）。

        **凡是"会进入交互式界面"的入口，测试都必须打桩。**
        这里只测"CLI 有没有把控制权交给新界面"，界面内部的行为由
        `tests/tui/test_render_inline_app.py` 覆盖。
        """
        self.write_config(
            'schema_version = 1\n[provider]\nname = "openai-compatible"\nmodel = "m"\n'
        )
        with mock.patch("logox.tui.render.app.run_inline", return_value=EXIT_OK) as fake_run:
            code, _out, _err = self.invoke()

        self.assertEqual(code, EXIT_OK)
        fake_run.assert_called_once()
        _args, kwargs = fake_run.call_args
        self.assertIn("session_start", kwargs, "新界面要靠它公示会话（provider/model/tools）")

    def test_the_interface_is_the_only_one(self) -> None:
        """★ 只有一个界面入口：``logox`` 直接进新界面，**没有**"选哪个界面"的开关。

        D85 删掉了旧的 Textual 全屏界面（连带 ``--fullscreen`` / ``--demo-ui``）。
        这条用例钉住"它们不会悄悄回来"——同时回来的还会有它那一整套依赖与文档。
        """
        self.write_config(
            'schema_version = 1\n[provider]\nname = "openai-compatible"\nmodel = "m"\n'
        )
        with mock.patch("logox.tui.render.app.run_inline", return_value=EXIT_OK) as fake_run:
            code, _out, _err = self.invoke()

        self.assertEqual(code, EXIT_OK)
        fake_run.assert_called_once()

    def test_removed_flags_are_rejected(self) -> None:
        """删掉的开关必须**报错**，而不是被静默忽略。

        静默忽略会让老脚本"看起来还能跑"，实际用的却是另一条路径——
        那种偏差要过很久才会被发现。
        """
        self.write_config(
            'schema_version = 1\n[provider]\nname = "openai-compatible"\nmodel = "m"\n'
        )
        for flag in ("--inline", "--demo-ui"):
            with self.subTest(flag=flag):
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as ctx:
                    main([flag])
                self.assertEqual(ctx.exception.code, 2, "argparse 对未知参数应当以 2 退出")

    def test_fullscreen_flag_invokes_run_fullscreen(self) -> None:
        """--fullscreen 或 -f 参数必须调用 run_fullscreen（D179 双模并存架构）。"""
        self.write_config(
            'schema_version = 1\n[provider]\nname = "openai-compatible"\nmodel = "m"\n'
        )
        invoked = []

        def fake_run_fullscreen(runtime, **_kwargs):  # noqa: ANN001, ANN003
            invoked.append("fullscreen")
            return EXIT_OK

        with mock.patch("logox.tui.render.fullscreen.run_fullscreen", side_effect=fake_run_fullscreen):
            code, _out, _err = self.invoke("--fullscreen")
            self.assertEqual(code, EXIT_OK)
            self.assertEqual(invoked, ["fullscreen"])

            invoked.clear()
            code, _out, _err = self.invoke("-f")
            self.assertEqual(code, EXIT_OK)
            self.assertEqual(invoked, ["fullscreen"])

    def test_the_new_interface_loads_the_key_file(self) -> None:
        """★★ 新界面必须**加载 `.logox/.env`**。

        这条是实测踩到的 bug：`--inline` 最初直接调 ``build_runtime``，漏了加载
        ``.env`` 这一步，于是用户在 `/login` 里存好的密钥完全不生效——
        启动后显示"尚未登录"，每条消息都失败，而设置界面看着一切正常。
        症状与原因隔了三层（配置文件 → 环境变量 → provider 构造），因此必须钉住。
        """
        self.write_config(
            'schema_version = 1\n[provider]\nname = "openai-compatible"\nmodel = "m"\n'
        )
        logox_dir = self.project / ".logox"
        logox_dir.mkdir(parents=True, exist_ok=True)
        (logox_dir / ".env").write_text("OPENAI_API_KEY=sk-from-file\n", encoding="utf-8")

        captured: dict[str, object] = {}

        def fake_run_inline(runtime, **_kwargs):  # noqa: ANN001, ANN003
            captured["needs_login"] = runtime.needs_login
            captured["warnings"] = list(runtime.warnings)
            return EXIT_OK

        with mock.patch("logox.tui.render.app.run_inline", side_effect=fake_run_inline):
            code, _out, _err = self.invoke()

        self.assertEqual(code, EXIT_OK)
        self.assertFalse(captured["needs_login"], "密钥文件没有被加载（用户会看到「尚未登录」）")
        self.assertIn(
            "OPENAI_API_KEY",
            " ".join(captured["warnings"]),  # type: ignore[arg-type]
            "应当告诉用户密钥是从文件里读到的",
        )
        os.environ.pop("OPENAI_API_KEY", None)

    def test_check_config_shows_the_tools_that_actually_exist(self) -> None:
        """★★ 启动摘要必须显示**真的注册了哪些工具**。

        这条来自一次实测踩到的谎：摘要里写着"启用工具：read, write, edit,
        glob, grep, shell, todo"（那是配置里的**愿望清单**），而当时**只有
        ``read`` 真的注册了**。用户会据此以为 ``shell`` 能跑，
        然后对着"模型为什么不用 shell"发呆——**产品主动误导用户**。
        """
        self.write_config(
            'schema_version = 1\n[provider]\nname = "openai-compatible"\nmodel = "m"\n'
        )
        _code, out, _err = self.invoke("--check-config")

        self.assertIn("实际工具", out)
        self.assertIn("read", out)
        # 配置默认里的那些**还没有实现**，必须明说，而不是混在"启用工具"里
        self.assertIn("尚未实现", out)
        self.assertNotIn("启用工具", out, "旧的那一行是愿望清单，不该再出现")

    def test_help_mentions_available_flags(self) -> None:
        """``--help`` 由 argparse 直接打印并 ``SystemExit(0)``（这是 CLI 的正常语义）。"""
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as ctx:
            main(["--help"])
        self.assertEqual(ctx.exception.code, 0)
        text = out.getvalue()
        for flag in ("--version", "--check-config", "--print-config", "--strict-config"):
            self.assertIn(flag, text)
        self.assertIn("--chat", text)
        self.assertIn("--fullscreen", text)
        self.assertIn("-f", text)
        for removed in ("--inline", "--demo-ui"):
            self.assertNotIn(removed, text, f"--help 不该再提到已删除的 {removed}")



class CliStartupBudgetTests(unittest.TestCase):
    """真实冷启动耗时（D29：< 300ms，含解释器启动）。"""

    def test_version_startup_under_budget(self) -> None:
        env = dict(os.environ)
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = str(SRC_DIR) + (os.pathsep + existing if existing else "")
        env["PYTHONUTF8"] = "1"
        TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        out_path = TEMP_ROOT / "cli-version-probe.txt"

        command = [sys.executable, "-m", "logox", "--version"]
        try:
            # 先预热一次（文件系统缓存），再计时——指标衡量的是冷启动而非磁盘抖动。
            with open(out_path, "w", encoding="utf-8") as handle:
                subprocess.run(  # noqa: S603
                    command, stdout=handle, stderr=subprocess.STDOUT, env=env,
                    cwd=str(REPO_ROOT), timeout=120, check=False,
                )

            samples: list[float] = []
            for _ in range(3):
                start = time.perf_counter()
                with open(out_path, "w", encoding="utf-8") as handle:
                    completed = subprocess.run(  # noqa: S603
                        command, stdout=handle, stderr=subprocess.STDOUT, env=env,
                        cwd=str(REPO_ROOT), timeout=120, check=False,
                    )
                samples.append((time.perf_counter() - start) * 1000)
            output = out_path.read_text(encoding="utf-8")
        finally:
            out_path.unlink(missing_ok=True)

        self.assertEqual(completed.returncode, 0, output)
        self.assertIn("logox", output)

        best = min(samples)
        self.assertLess(
            best,
            VERSION_BUDGET_MS,
            f"`--version` 最快一次为 {best:.0f}ms，超出 D29 的 {VERSION_BUDGET_MS:.0f}ms 预算；"
            f"全部样本：{[f'{value:.0f}' for value in samples]}ms",
        )


class ContextWindowResolutionTests(unittest.TestCase):
    """压缩阈值必须基于**上下文窗口**，而不是单次输出上限（CODE-AUDIT F-22）。

    背景：``--chat`` 装配路径曾把 ``provider_config.max_tokens``（单次**输出**上限，
    D118 之后默认 16384）当成 ``window_capacity`` 传下去。
    而 ``Compactor`` 的高水位是 ``min(window_capacity * 0.75, 80_000)``
    —— 于是 **12288 token 就开始压缩**，而模型可能支持 1M 上下文。
    """

    def test_prefers_the_provider_context_window(self) -> None:
        class _Provider:
            context_window = 1_048_576

        self.assertEqual(_context_window_of(_Provider()), 1_048_576)

    def test_falls_back_when_the_provider_does_not_report_one(self) -> None:
        class _Provider:
            context_window = None

        self.assertEqual(_context_window_of(_Provider()), 128_000)

    def test_never_uses_the_output_token_cap(self) -> None:
        """关键回归：输出上限再大也不得被当成窗口。"""

        class _Provider:
            context_window = None
            max_tokens = 999_999

        self.assertEqual(_context_window_of(_Provider()), 128_000)


if __name__ == "__main__":
    unittest.main()
