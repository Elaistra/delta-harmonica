"""打包入口（PyInstaller 用这个当 script）。

为什么需要它：
    `hca/ui.py` 用的是包内相对导入（`from . import bili`），直接当顶层脚本跑会报
    `ImportError: attempted relative import with no known parent package`。
    所以封包必须走一个只做绝对导入的薄壳。

源码运行时仍然是 `python -m hca.ui`，这个文件只为封包存在。
"""

from __future__ import annotations

import sys

from hca.ui import main

if __name__ == "__main__":
    sys.exit(main())
