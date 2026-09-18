"""配置与状态模块（D30：自写极简 TOML writer + 读写分离 + 原子写回）。

职责
----
* ``schema.py``  —— ``config.toml`` / ``state.toml`` / 主题文件的 pydantic 模型（D26）
* ``loader.py``  —— 多级来源加载、深度合并、``origin`` 溯源、可定位报错
* ``writer.py``  —— 极简 TOML 序列化（约 60 行，只支持本项目用到的语法子集）
* ``state.py``   —— ``state.toml`` 的读 / 变换式更新 / 原子写回
* ``theme.py``   —— 主题文件发现、加载、对比度校验（UI-SPEC §8）

硬约束（D30 红线）
------------------
**绝不改写用户手写的 ``config.toml`` 与 ``permissions.toml``**。程序产生的
状态一律写入独立的 ``state.toml``。用户的注释、分组与排版分毫不动。
"""

from __future__ import annotations

__all__: list[str] = []
