"""``.env`` 密钥文件——**用户提供的 API Key 的唯一落盘位置**（D62）。

它解决什么问题
==============

D28 有一条不可让渡的底线：**API Key 绝不落盘**。这条底线来自一个具体的风险——
密钥写进配置文件后会被 git、日志、终端回滚、截图、同事的屏幕一起带走。

但"每次启动都要重新粘贴密钥"也不成立：那不是安全，只是难用（用户会转而把密钥
写进 ``config.toml``，风险反而更大）。``/login`` 需要一个**可持久化**的落点。

因此本项目的方案是：**密钥与配置分开，且只进一个专门的、被 gitignore 的文件。**

============================ ==================================================
位置                          说明
============================ ==================================================
``<project>/.logox/.env``     **本模块管的唯一文件**。格式 ``KEY=value``，
                              权限 0o600，已写入 ``.gitignore``
``<project>/.logox/config.toml`` 用户手写、**永久只读**；写明文密钥会**报错拒绝**
``~/.logox/config.toml``      同上
============================ ==================================================

**为什么放在项目目录而不是家目录**：实测本机的家目录被沙箱挡住
（``C:\\Users\\Administrator\\.logox`` 不可写），而项目 ``.logox/`` 可写且已 gitignore。
顺带的好处是"这台机器的哪个项目用了哪个密钥"一眼可见。

启动时怎么用
============

``logox`` 启动时会**先加载这个文件到 ``os.environ``**（不覆盖已有的真实环境变量），
然后 provider 才去读 ``api_key_env``。因此它和"导出环境变量"这条正规路径完全等价，
只是少了一步手工操作——**密钥始终只以环境变量的形式存在于进程内**，
config 层永远看不到它。

安全上的取舍（诚实登记，不粉饰）
================================

* ✅ 不进 ``config.toml``、不进 ``state.toml``、不进事件流、不进日志、不进对话历史；
* ✅ 文件已 gitignore（**这一条才是真正的护栏**，见下）；
* ✅ 登录时**弹窗确认**才写入（D62）——用户始终知道"这次会被记住"；
* ⚠️ 它**是明文**。任何能读该文件的进程/人都能拿到密钥。
* ⚠️ **权限收紧在本机做不到**：``chmod(0o600)`` 在 Windows 上不改变 ACL，而
  ``icacls`` 被沙箱拒绝（实测：``Failed processing 1 files: Access is denied``）。
  实测该文件的 ACL 是继承来的 ``Authenticated Users:(M)`` / ``Users:(RX)``，
  即**同机其他账户可读**。这比"写进 config.toml"仍好得多（隔离 + gitignore +
  不进版本历史），但**不等于加密**，也不等于仅属主可读。
  真正的密钥管理（系统钥匙串 / 1Password CLI / 云 KMS）不在 v1 范围内。
"""

from __future__ import annotations

import contextlib
import os
import re
from pathlib import Path

__all__ = ["ENV_FILENAME", "load_env_file", "parse_env", "update_env_file"]

#: 文件名（固定在项目 ``.logox/`` 与用户 ``~/.logox/`` 下同名）
ENV_FILENAME = ".env"

#: 合法变量名：字母/下划线开头，后接字母数字下划线
_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: 值里**允许**的字符（**故意很严**）。密钥与端点 URL 都是可见 ASCII、不含空格。
_VALUE_RE = re.compile(r"^[!-~]+$")

#: 值里**禁止**的字符：空白与三种引号。
#:
#: **为什么必须单独禁引号**：``_VALUE_RE`` 只挡住空格，而 ``'`` 与 ``"`` 在它眼里
#: 是正常可打印字符。可 ``.env`` 的读取端会把**成对引号剥掉**——
#: 于是写出去的 ``KEY="abc"`` 读回来变成 ``abc``，密钥被**静默改掉**。
#: 这是"写进去的和读出来的不是同一个值"，比直接报错危险得多（实测：单元测试抓到）。
_FORBIDDEN_IN_VALUE = ("\t", "\n", "\r", " ", "'", '"', "\\")


def parse_env(text: str) -> dict[str, str]:
    """解析 ``.env`` 文本。**宽容读取、不抛异常**。

    接受的写法（照抄 dotenv 的主流约定）：

    * 空行与 ``#`` 开头的注释行 → 跳过
    * 可选的 ``export`` 前缀（从 shell 里复制过来时很常见）
    * ``KEY=value``、``KEY="value"``、``KEY='value'``（成对引号会被剥掉）
    * 值里允许 ``#``（只有**行首**才是注释——``sk-abc#1`` 这种密钥是合法的）

    无效行**静默跳过**而不是报错：这个文件可能被用户手工编辑过，
    为了一个手滑的行让整个 Logox 起不来，代价远大于收益。
    """
    result: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if _KEY_RE.match(key):
            result[key] = value
    return result


def load_env_file(path: Path, *, environ: dict[str, str] | None = None) -> dict[str, str]:
    """把 ``.env`` 里的键值**并入** ``environ``（默认 ``os.environ``）。

    **不覆盖已存在的键**：真实环境变量永远优先。理由很实际——
    用户临时想试另一个密钥时，``$env:DEEPSEEK_API_KEY = "..."`` 必须能压过文件，
    否则他会陷入"我明明改了却不起作用"的困惑。

    :returns: 实际**新写入**的键值对（供界面显示"从文件里读到了几个"）。
    """
    target = os.environ if environ is None else environ
    if not path.is_file():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}  # 读不了就当没有：绝不因为一个可选的便利文件让启动失败
    applied: dict[str, str] = {}
    for key, value in parse_env(text).items():
        if key in target and target[key].strip():
            continue  # 已有真实环境变量 → 让位
        target[key] = value
        applied[key] = value
    return applied


def update_env_file(path: Path, updates: dict[str, str]) -> None:
    """原子地写入/更新键值，**保留其它条目与注释**。

    写法与 ``config/state.py`` 一致（先写临时文件 → ``fsync`` → ``os.replace``）：
    中途崩溃只会看到"旧内容"或"新内容"，不会出现半截文件——
    而这个文件里放的是密钥，半截文件意味着**密钥被截断且用户以为它还在**。

    :raises ValueError: 变量名或值含不允许的字符（**宁可报错，不写出坏文件**）
    :raises OSError: 写盘失败（调用方必须降级为"记日志 + 会话继续"，见 P-5）
    """
    for key, value in updates.items():
        if not _KEY_RE.match(key):
            raise ValueError(f"非法的环境变量名：{key!r}（只允许字母、数字、下划线，且不以数字开头）")
        if not value:
            raise ValueError(f"变量 {key} 的值为空——空值等于没有配置，不该写进文件")
        if not _VALUE_RE.match(value):
            raise ValueError(
                f"变量 {key} 的值含不允许的字符（只接受可见 ASCII、不含空格与引号）；"
                "这几乎总是意味着粘贴时带进了换行或不可见字符"
            )
        found = [char for char in _FORBIDDEN_IN_VALUE if char in value]
        if found:
            shown = "、".join(repr(char) for char in dict.fromkeys(found))
            raise ValueError(
                f"变量 {key} 的值含 {shown}。"
                "引号尤其危险：读取端会把成对引号剥掉，于是**写进去的与读出来的不是同一个值**。"
                "请去掉首尾引号后重试（密钥本身不需要引号）。"
            )

    existing_lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    remaining = dict(updates)
    out: list[str] = []
    for line in existing_lines:
        stripped = line.strip()
        body = stripped[len("export ") :].lstrip() if stripped.startswith("export ") else stripped
        key = body.partition("=")[0].strip()
        if key in remaining and not stripped.startswith("#"):
            # 就地替换（保留原来的位置，diff 最小）
            out.append(f"{key}={remaining.pop(key)}")
        else:
            out.append(line)
    for key, value in remaining.items():
        out.append(f"{key}={value}")

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    text = "\n".join(out) + "\n"
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        _restrict(tmp)
        os.replace(tmp, path)
        _restrict(path)
    except OSError:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        raise


def _restrict(path: Path) -> None:
    """尝试把权限收紧到 0o600（仅属主可读写）。

    **在本机这是无效的，而且我知道**（实测记录在模块文档里）：

    * Windows 上 ``chmod`` 只改只读位，不改变 ACL —— 调用"成功"但什么也没发生；
    * ``icacls /inheritance:r`` 被沙箱拒绝（``Failed processing 1 files: Access is denied``）。

    因此这里保留调用（在 POSIX 上它确实生效），但**绝不把它当成安全保证**。
    本文件真正的护栏是 **gitignore**（不进版本历史）与 **登录时的显式确认**。
    """
    with contextlib.suppress(OSError):
        path.chmod(0o600)
