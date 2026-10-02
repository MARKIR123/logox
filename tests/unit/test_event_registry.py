"""事件注册一致性看护（F-19 / R-08）。

为什么要单独一个文件
====================

`kernel/events.py` 里的事件注册依赖**手工维护三处**：

1. ``_EVENT_CLASSES`` —— 运行时元组（``EVENT_TYPES`` 由它派生）
2. ``AnyEvent``       —— 静态的 ``Annotated[Union[...], Field(discriminator="type")]``
3. ``__all__``        —— 导出名单

新增一个事件却漏掉其中一处时：类型检查过、其它单测全绿，
**只有真实回放 telemetry / 恢复历史会话时**才会炸在 ``UnknownEventError``
（``parse_event`` 刻意不静默跳过，见 E-13）。

这与 `PROJECT-REVIEW.md` §5.7 里「``__all__`` 导出了一个不存在的 ``PromptQueue``」
**是同一类维护陷阱**——只是那次被 ruff 的 ``F822`` 抓到了，这次没有静态检查覆盖它。
所以补一条可执行的看护。

刻意**不写死"一共 22 个事件"**：那个数字会随功能增长而变，
写死会让这条用例变成"每次加事件都要改它"的负担。
真正的不变量是**三者之间的一致**，而不是某个具体数值。
"""

from __future__ import annotations

import unittest
from typing import get_args

from logox.kernel.events import _EVENT_CLASSES, EVENT_TYPES, AnyEvent, Event

__all__ = ["EventRegistryTests"]


class EventRegistryTests(unittest.TestCase):
    def test_event_subclasses_are_all_registered(self) -> None:
        """定义在 `events.py` 里的每个 `Event` 子类都必须出现在 `EVENT_TYPES` 中。

        `__module__` 过滤是必要的：测试或插件可以定义自己的 `Event` 子类
        （总线对它们一视同仁），那不是"漏注册"，不该让这条用例变红。
        """
        declared = {
            cls.model_fields["type"].default
            for cls in Event.__subclasses__()
            if cls.__module__ == "logox.kernel.events"
        }
        self.assertEqual(declared, set(EVENT_TYPES))

    def test_any_event_union_matches_the_registry(self) -> None:
        """`AnyEvent` 的联合成员必须与 `_EVENT_CLASSES` 完全一致（不只是数量一致）。

        `AnyEvent` 是 ``Annotated[Union[...], Field(discriminator="type")]``，
        所以 ``get_args(AnyEvent)[0]`` 拿到的是联合类型本身，
        再取一层 ``get_args`` 才是成员。
        """
        union = get_args(AnyEvent)[0]
        members = get_args(union)
        self.assertEqual(set(members), set(_EVENT_CLASSES))
        self.assertEqual(len(members), len(_EVENT_CLASSES))

    def test_registry_type_name_matches_each_class_literal(self) -> None:
        """注册表的 key 必须等于该类自己声明的 ``type`` 字面量。

        这条把"注册表"与"类的判别字段"两个事实源连起来：
        单独看两者各自都对，**连不起来**才是真正的失败模式
        （`parse_event` 正是靠这个字面量做判别联合的）。
        """
        for type_name, cls in EVENT_TYPES.items():
            self.assertEqual(
                cls.model_fields["type"].default,
                type_name,
                f"{cls.__name__} 的 type 字面量与注册表 key {type_name!r} 不一致",
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
