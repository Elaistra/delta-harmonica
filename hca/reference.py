"""参考引擎 —— 把原 exe 里解出来的内核**当库**加载。

★ 用途只有一个：**对照验证**。重写版每完成一层，就拿它跑同一首歌、逐条比。
  它不是新项目的运行路径（新项目的目标是彻底不依赖这份 pyc）。

为什么可行：marshal 能把 pyc 里的 code object 读出来，exec 进一个空模块命名空间即可；
内核只 import 标准库，所以没有额外依赖。**必须用 Python 3.14 执行**（字节码版本限制）。
"""

from __future__ import annotations

import marshal
import os
import sys
import types
from pathlib import Path

# ★ 不写死任何一台机器的绝对路径（开源时会把私人目录结构带出去）。
#   优先读环境变量 HCA_LEGACY_PYC；否则按「项目旁边的旧 exe 解包目录」推断。
_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
DEFAULT_PYC = Path(
    os.environ.get("HCA_LEGACY_PYC")
    or (_ROOT.parent / "口琴自动演奏器" / "口琴自动演奏器.exe_extracted"
        / "PYZ.pyz_extracted" / "autoplay.pyc")
)

__all__ = ["load_legacy", "available", "DEFAULT_PYC"]


def available(pyc: Path | None = None) -> bool:
    return (pyc or DEFAULT_PYC).is_file() and sys.version_info[:2] == (3, 14)


def load_legacy(pyc: Path | None = None) -> types.ModuleType:
    path = Path(pyc or DEFAULT_PYC)
    if not path.is_file():
        raise FileNotFoundError(
            f"找不到参考内核：{path}\n"
            "（它在旧 exe 的解包目录里；删了就没法做对照验证，但新项目仍可独立运行）")
    if sys.version_info[:2] != (3, 14):
        raise RuntimeError(
            f"参考内核是 Python 3.14 的字节码，当前是 {sys.version.split()[0]}，无法加载。"
            f"请用 3.14 运行。（其余功能不受影响）")
    mod = types.ModuleType("legacy_autoplay")
    mod.__file__ = str(path)
    exec(marshal.loads(path.read_bytes()[16:]), mod.__dict__)
    return mod
