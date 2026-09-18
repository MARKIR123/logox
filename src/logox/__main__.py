"""``python -m logox`` 入口（D4 决定的稳定入口）。

与 ``logox`` console script 的区别：不依赖任何脚本安装，任何环境下都可运行。
"""

from __future__ import annotations

import sys

from logox.cli import main

if __name__ == "__main__":
    sys.exit(main())
