"""L1 提示词模板命令数据模型（M10 / D3 / D112）。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal


@dataclass
class CommandTemplate:
    """单个 L1 提示词模板命令。"""

    name: str
    description: str
    template: str
    path: Path
    scope: Literal["project", "global"]
