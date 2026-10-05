"""TUI 表现层：Rich 内容与自研 ANSI 行渲染，支持主屏及备用全屏。

包入口不加载 UI、模型 SDK 或配置，保留 CLI 快速路径的惰性导入。
具体组件、主题与终端仅在对应入口需要时导入。
"""

from __future__ import annotations

__all__: list[str] = []
