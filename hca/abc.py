"""ABC 记谱解析 —— **尚未实现**。

为什么不现在写：曲库里 25 首全是 MIDI，**一个 .abc 样本都没有**，
写完没法做逐条对照验证。而"看起来能解析、实际和游戏对不上"是最坏的结果
（音准错了但听不出原因）。

第 3 阶段会和 pitch/compile 一起做，届时需要：
  1. 找 1~2 个 .abc 样本（原程序注释里提到过 `sings/README.md` 里的语法子集）；
  2. 用探针把原内核的 ABC 规则同样问出来（照 `probe_midi.py` 的做法）；
  3. 才能开始写。

在那之前，这里明确报错，不猜。
"""

from __future__ import annotations

from .mapping import Mapping
from .model import Score

__all__ = ["parse_abc"]


def parse_abc(text: str, mapping: Mapping | None = None) -> Score:
    raise NotImplementedError(
        "ABC 解析还没实现（第 3 阶段）。原因：没有 .abc 样本可做对照验证，"
        "不打算写一份'凭感觉对'的解析器。\n"
        "临时办法：用 MIDI 文件，或者先用 --engine reference 跑旧内核。")
