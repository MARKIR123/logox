"""大模型技能包（Skills）数据模型（M10 / D113）。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal


@dataclass
class SkillMeta:
    """技能包元数据。"""

    name: str
    description: str
    path: Path
    skill_dir: Path
    scope: Literal["project", "global"]
