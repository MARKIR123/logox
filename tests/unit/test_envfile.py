"""``.env`` 密钥文件的读写（MODULE_tui_integration §15 / D59）。

它存在的理由只有一条，但很硬：**API Key 必须有一个可持久化、又足够安全的落点**。
因此这里的每条用例都在盯"会不会意外把密钥写进别的地方 / 写坏 / 泄露到输出里"。

**本文件里的所有密钥都是假字符串**（``sk-test-…``），不是真凭据。
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path

from logox.config.envfile import load_env_file, parse_env, update_env_file
from tests.unit.support import make_temp_dir, remove_temp_dir


class ParseEnvTests(unittest.TestCase):
    """宽容读取：这个文件可能被用户手工编辑过，不该因为一行手滑让 Logox 起不来。"""

    def test_basic_key_value(self) -> None:
        self.assertEqual(parse_env("A=1\nB=two\n"), {"A": "1", "B": "two"})

    def test_comments_and_blank_lines_are_skipped(self) -> None:
        text = "# 注释\n\n   \nDEEPSEEK_API_KEY=sk-test-abc\n# 又一行注释\n"
        self.assertEqual(parse_env(text), {"DEEPSEEK_API_KEY": "sk-test-abc"})

    def test_export_prefix_is_tolerated(self) -> None:
        """从 shell 里复制一行过来时最常见的形式。"""
        self.assertEqual(parse_env("export OPENAI_API_KEY=sk-test-x\n"), {"OPENAI_API_KEY": "sk-test-x"})

    def test_paired_quotes_are_stripped(self) -> None:
        self.assertEqual(
            parse_env('A="sk-test-1"\nB=\'sk-test-2\'\n'),
            {"A": "sk-test-1", "B": "sk-test-2"},
        )

    def test_hash_inside_value_is_kept(self) -> None:
        """**只有行首的 # 才是注释**——密钥里出现 # 是合法的，剥掉会毁掉密钥。"""
        self.assertEqual(parse_env("KEY=abc#def\n"), {"KEY": "abc#def"})

    def test_invalid_lines_are_skipped_not_fatal(self) -> None:
        text = "这不是赋值\n=没有名字\nNAME=\nGOOD=ok\n"
        self.assertEqual(parse_env(text), {"NAME": "", "GOOD": "ok"})

    def test_value_may_contain_equals(self) -> None:
        """把 base64 之类带 = 的值截断，是这类解析器最经典的 bug。"""
        self.assertEqual(parse_env("KEY=abc=def==\n"), {"KEY": "abc=def=="})


class LoadEnvFileTests(unittest.TestCase):
    """并入环境变量：**真实环境变量永远优先**。"""

    def setUp(self) -> None:
        self.root = make_temp_dir("env-")
        # `addCleanup` 而不是 `tearDown`：用例失败时也一定会执行
        self.addCleanup(remove_temp_dir, self.root)

    def test_missing_file_is_not_an_error(self) -> None:
        environ: dict[str, str] = {}
        self.assertEqual(load_env_file(self.root / "nope.env", environ=environ), {})
        self.assertEqual(environ, {})

    def test_values_are_applied(self) -> None:
        path = self.root / ".env"
        path.write_text("DEEPSEEK_API_KEY=sk-test-1\n", encoding="utf-8")
        environ: dict[str, str] = {}
        applied = load_env_file(path, environ=environ)
        self.assertEqual(applied, {"DEEPSEEK_API_KEY": "sk-test-1"})
        self.assertEqual(environ["DEEPSEEK_API_KEY"], "sk-test-1")

    def test_real_environment_wins(self) -> None:
        """★ 临时想试另一个密钥时，`$env:XXX = ...` 必须能压过文件。

        否则用户会陷入"我明明改了却不起作用"的困惑——这是可用性问题，不是洁癖。
        """
        path = self.root / ".env"
        path.write_text("DEEPSEEK_API_KEY=sk-test-from-file\n", encoding="utf-8")
        environ = {"DEEPSEEK_API_KEY": "sk-test-from-shell"}
        applied = load_env_file(path, environ=environ)
        self.assertEqual(applied, {}, "已有真实环境变量时不该覆盖")
        self.assertEqual(environ["DEEPSEEK_API_KEY"], "sk-test-from-shell")

    def test_blank_existing_value_yields_to_the_file(self) -> None:
        """存在但为空的环境变量算"没设"——Windows 上很容易留下空变量。"""
        path = self.root / ".env"
        path.write_text("KEY=sk-test-file\n", encoding="utf-8")
        environ = {"KEY": "   "}
        load_env_file(path, environ=environ)
        self.assertEqual(environ["KEY"], "sk-test-file")


class UpdateEnvFileTests(unittest.TestCase):
    """写入：原子、只碰目标键、不泄露到别处。"""

    def setUp(self) -> None:
        self.root = make_temp_dir("envw-")
        self.addCleanup(remove_temp_dir, self.root)
        self.path = self.root / ".env"

    def test_creates_file_with_one_entry(self) -> None:
        update_env_file(self.path, {"DEEPSEEK_API_KEY": "sk-test-1"})
        self.assertEqual(parse_env(self.path.read_text(encoding="utf-8")), {"DEEPSEEK_API_KEY": "sk-test-1"})

    def test_updates_in_place_and_keeps_other_entries(self) -> None:
        self.path.write_text(
            "# 我的密钥\nDEEPSEEK_API_KEY=sk-test-old\nOPENAI_API_KEY=sk-test-open\n",
            encoding="utf-8",
        )
        update_env_file(self.path, {"DEEPSEEK_API_KEY": "sk-test-new"})
        text = self.path.read_text(encoding="utf-8")
        self.assertIn("sk-test-new", text)
        self.assertNotIn("sk-test-old", text, "旧值必须被替换掉，而不是留下两份")
        self.assertIn("sk-test-open", text, "**其它键不得被碰**")
        self.assertIn("# 我的密钥", text, "注释应当保留")

    def test_appends_a_new_key_without_touching_existing_lines(self) -> None:
        self.path.write_text("A=1\n", encoding="utf-8")
        update_env_file(self.path, {"B": "2"})
        self.assertEqual(parse_env(self.path.read_text(encoding="utf-8")), {"A": "1", "B": "2"})

    def test_round_trip(self) -> None:
        update_env_file(self.path, {"A": "1", "B": "2"})
        update_env_file(self.path, {"A": "3"})
        self.assertEqual(parse_env(self.path.read_text(encoding="utf-8")), {"A": "3", "B": "2"})

    def test_rejects_a_value_with_a_newline(self) -> None:
        """★ 粘贴时带进换行是最常见的意外，它会让**下一行变成一个坏条目**。"""
        with self.assertRaises(ValueError):
            update_env_file(self.path, {"KEY": "sk-test\nEVIL=1"})

    def test_rejects_a_value_with_spaces_or_quotes(self) -> None:
        for bad in ("sk test", "sk-test'", 'sk-"test"', "sk-test\t"):
            with self.subTest(value=bad), self.assertRaises(ValueError):
                update_env_file(self.path, {"KEY": bad})

    def test_rejects_an_invalid_variable_name(self) -> None:
        for bad in ("1KEY", "K-EY", "K EY", ""):
            with self.subTest(name=bad), self.assertRaises(ValueError):
                update_env_file(self.path, {bad: "sk-test"})

    def test_failed_write_leaves_no_temp_file(self) -> None:
        """失败时不能留下 ``.env.tmp``——它同样含密钥，而且更不容易被注意到。"""
        with self.assertRaises(ValueError):
            update_env_file(self.path, {"KEY": "bad\nvalue"})
        self.assertFalse(self.path.exists())
        self.assertFalse((self.root / ".env.tmp").exists())

    def test_file_is_restricted_to_owner(self) -> None:
        """权限尽力收紧到 0o600。**失败不报错**（Windows 位语义有限），
        但成功时必须是 600——这条断言在能设置的文件系统上会真正生效。"""
        update_env_file(self.path, {"KEY": "sk-test"})
        mode = self.path.stat().st_mode & 0o777
        self.assertIn(mode, (0o600, 0o666, 0o644), f"意外的权限位：{oct(mode)}")
        if os.name != "nt":
            self.assertEqual(mode, 0o600)

    def test_atomic_replace_leaves_a_valid_file(self) -> None:
        """写完之后的文件必须**可解析且内容完整**（半截文件意味着密钥被截断而用户不知道）。"""
        update_env_file(self.path, {"A": "1"})
        update_env_file(self.path, {"A": "2", "B": "3"})
        loaded = parse_env(self.path.read_text(encoding="utf-8"))
        self.assertEqual(loaded, {"A": "2", "B": "3"})


class SecretHygieneTests(unittest.TestCase):
    """**密钥不得出现在任何会被打印/落盘的地方**——本项目不可让渡的底线。"""

    def setUp(self) -> None:
        self.root = make_temp_dir("envsec-")
        self.addCleanup(remove_temp_dir, self.root)

    def test_env_file_is_gitignored(self) -> None:
        """★ 这是这条底线能否成立的关键：文件必须被 git 忽略。

        断言的是**仓库的 .gitignore 真的排除了它**，而不是"我们打算忽略它"。
        """
        repo_root = Path(__file__).resolve().parents[2]
        gitignore = (repo_root / ".gitignore").read_text(encoding="utf-8")
        lines = {line.strip() for line in gitignore.splitlines()}
        self.assertIn(
            ".env",
            lines,
            ".gitignore 必须显式排除 .env——否则用户一旦提交，密钥就进了版本历史（无法撤回）",
        )
        self.assertIn("!.env.example", lines, "同时应保留一个可提交的示例文件")

    def test_env_example_contains_no_real_secret(self) -> None:
        """示例文件是**唯一**会被提交的那个，因此里面只能是注释掉的占位符。

        判据要精确，否则会误报：`sk-` 这个前缀**本来就该出现在示例里**
        （用户需要知道长什么样）。真正要防的是两件事：

        ① 有**未被注释**的赋值行——那样复制文件就直接带上了一个占位值；
        ② 赋值行里的值不是明显的占位符（例如一串看着像真密钥的随机字符）。
        """
        import re

        repo_root = Path(__file__).resolve().parents[2]
        example = repo_root / ".env.example"
        self.assertTrue(example.is_file(), "缺少 .env.example：用户不知道该往 .env 里写什么")

        # 只认"赋值行"：可选的 # 前缀 + 全大写变量名 + = + 值
        assignment = re.compile(r"^(?P<comment>#\s*)?(?P<name>[A-Z_][A-Z0-9_]*)=")
        found: list[tuple[bool, str, str]] = []
        for line in example.read_text(encoding="utf-8").splitlines():
            match = assignment.match(line.strip())
            if match:
                found.append((bool(match.group("comment")), match.group("name"), line.split("=", 1)[1].strip()))

        self.assertTrue(found, "示例里应当至少给出一条赋值示例，否则用户不知道格式")
        for commented, name, value in found:
            with self.subTest(name=name):
                self.assertTrue(commented, f"{name} 必须是注释掉的示例，否则复制文件会带上占位值")
                self.assertTrue(
                    _looks_like_placeholder(value),
                    f"{name} 的值 {value!r} 不像占位符——示例里绝不能出现真实密钥",
                )


def _looks_like_placeholder(value: str) -> bool:
    """判断一个值是否明显是占位符。

    规则很保守（宁可误报也不放过）：去掉已知前缀后，剩下的字符必须**只有一种**
    （如 ``xxxx`` / ``****``），或者整串是 ``...`` / 空。真人密钥做不到只有一种字符。
    """
    body = value.strip().strip("'\"")
    if body in ("", "...", "<在这里粘贴你的密钥>"):
        return True
    for prefix in ("sk-ant-", "sk-", "ghp_", "AKIA"):
        if body.startswith(prefix):
            body = body[len(prefix) :]
            break
    return len(set(body)) <= 1

    def test_config_schema_still_forbids_plaintext_keys(self) -> None:
        """`config.toml` 里的明文密钥必须**继续被拒绝**——D59 没有放松这条。"""
        from pydantic import ValidationError

        from logox.config.schema import ProviderConfig

        with self.assertRaises(ValidationError) as ctx:
            ProviderConfig(name="deepseek", model="m", api_key="sk-test-plaintext")
        self.assertIn("api_key_env", str(ctx.exception), "报错必须告诉用户正确的写法")

    def test_load_does_not_write_any_env_file(self) -> None:
        """**读**环境文件不得顺手创建它（避免"我只是启动了一下就有个密钥文件"）。"""
        target = self.root / ".env"
        load_env_file(target, environ={})
        self.assertFalse(target.exists())

    def test_paths_expose_env_next_to_state(self) -> None:
        """`.env` 的落点必须在配置目录里、与 `state.toml` 同级。"""
        from logox.paths import LogoxPaths

        paths = LogoxPaths.at(self.root)
        self.assertEqual(paths.env.name, ".env")
        self.assertEqual(paths.env.parent, paths.state.parent)


if __name__ == "__main__":
    unittest.main()
