"""双轨标记开关（`Ctrl+B`，D176）的用例。

用户需求：「我很喜欢这个双轨的设计……但是复制的时候会复制到 `▌`/`▎`，
有什么方法让它们无法复制吗？」

终端里做不到「标记为装饰」（占了格子的字形一定会被复制），所以解法是**显示开关**。
本文件守三件事：

1. **关掉后一个标记都不剩**（`▌` 与 `▎` 都要消失，不是换成空格）；
2. **文字本身不变**（只少了装饰，不该顺手改内容）；
3. **按一下必须真的变**（★ 缓存键守卫：漏了 `show_track` 会"按键毫无反应且不报错" ——
   D125-d 踩过同型坑，本轮实现时也**真的**踩了一次，靠这条用例才钉住）。
"""

from __future__ import annotations

import unittest

from logox.tui.render.keys import Key
from tests.tui.test_render_inline_app import make_app


class TrackToggleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app, _terminal, _runtime = make_app(width=80, height=24)
        buffer = self.app.timeline.buffer
        buffer.add_user("用户的问题")
        buffer.add_delta("模型的回复")
        buffer.flush_delta()
        self.buffer = buffer

    # ---------------------------------------------------------------- 默认与切换
    def test_t01_track_is_on_by_default(self) -> None:
        """默认**开着**：用户明确说喜欢这个双轨设计，开关只是为了复制而存在。"""
        self.assertTrue(self.buffer.show_track)
        frame = self.app.frame_text()
        self.assertIn("▌", frame)
        self.assertIn("▎", frame)

    def test_t02_toggling_off_removes_every_marker(self) -> None:
        """★ 关掉后 `▌`（用户）与 `▎`（模型）**都**要消失 —— 用户要的就是能干净复制。"""
        self.app.press(Key("b", ctrl=True))

        self.assertFalse(self.buffer.show_track)
        frame = self.app.frame_text()
        self.assertNotIn("▌", frame, "用户消息的轨道还在 —— 复制出来仍会带上它")
        self.assertNotIn("▎", frame, "模型输出的轨道还在（尾部渲染漏传开关时就是这个症状）")

    def test_t03_toggling_back_restores_the_markers(self) -> None:
        """再按一次恢复。★ 这条同时是**缓存键守卫**：

        实现时漏了 `cache.show_track = show_track` ⇒ 第二次切换被 `matches()` 判成"参数没变" ⇒
        **直接复用旧画面**，症状是「按键毫无反应、也不报错」（D125-d 的同型坑）。
        """
        before = self.app.frame_text()
        self.app.press(Key("b", ctrl=True))
        off = self.app.frame_text()
        self.app.press(Key("b", ctrl=True))
        restored = self.app.frame_text()

        self.assertNotEqual(before, off, "第一次按 Ctrl+B 画面没变 —— 开关没生效")
        self.assertNotEqual(off, restored, "第二次按 Ctrl+B 画面没变 —— 缓存键漏了 show_track")
        self.assertIn("▌", restored)
        self.assertIn("▎", restored)

    def test_t04_only_the_decoration_changes(self) -> None:
        """文字内容不该被动到：去掉每行开头的轨道前缀后，两边应当**完全一致**。"""
        with_track = self.app.frame_text().split("\n")
        self.app.press(Key("b", ctrl=True))
        without = self.app.frame_text().split("\n")

        stripped = [line.replace("▌ ", "").replace("▎ ", "") for line in with_track]
        self.assertEqual(stripped, without, "关掉轨道顺手改了正文 —— 那就不只是装饰了")

    def test_t05_toggle_does_not_touch_history(self) -> None:
        """开关只影响渲染：块的条数与内容都不变（不写历史、不进持久化）。"""
        counts_before = (len(self.buffer.blocks), len(self.buffer.visible_blocks))
        self.app.press(Key("b", ctrl=True))
        self.assertEqual((len(self.buffer.blocks), len(self.buffer.visible_blocks)), counts_before)


if __name__ == "__main__":
    unittest.main()
