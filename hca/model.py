"""数据结构与数学工具 —— 与乐理/游戏无关的纯定义。

重写版设计原则：
  * 时刻用「基准秒」表达（1.0× 速度下的秒），速度只在调度时除 —— 这样变速不用重编译。
  * 音高只用两种形态：MIDI 整数 / (音级, 八度, 升降) 三元组。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

__all__ = ["js_round", "Note", "Bar", "Section", "Score", "Action", "midi_name"]


def js_round(x: float) -> float:
    """JavaScript 的 Math.round —— 逢五一律向上。

    ★ 必须用它，不能用 Python 内建 round()：内建是"四舍六入五成双"，
      在 1/4 拍网格上会把 .125 这类半点切到不同位置，导致音数和游戏对不上。
    """
    return math.floor(x + 0.5)


# 音符：(音级 1..7, 八度, 时值(拍), 升降 -1/0/1)；音级 0 = 休止
Note = tuple[int, int, float, int]


@dataclass
class Section:
    name: str = "主旋律"
    intro: bool = False
    bars: list[list[Note]] = field(default_factory=list)


@dataclass
class Score:
    """一首曲子的中间表示（与游戏的 ABCParser 输出保持同一形状）。"""

    title: str = ""
    composer: str = ""
    bpm: float = 120.0
    beats_per_bar: float = 4.0
    meter_den: int = 4
    unit_beats: float = 1.0
    tonic_index: int = 0
    octave_shift: int = 0
    sections: list[Section] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def bars(self) -> list[list[Note]]:
        return [b for s in self.sections for b in s.bars]

    def note_count(self) -> int:
        """发声的音数（休止不算）。"""
        return sum(1 for b in self.bars for n in b if n[0] != 0)

    def bar_count(self) -> int:
        return sum(len(s.bars) for s in self.sections)

    def total_beats(self, include_intro: bool = True) -> float:
        return sum(
            n[2] for s in self.sections if include_intro or not s.intro
            for b in s.bars for n in b
        )


_MOD_LABEL = {"left": "左键", "middle": "中键", "right": "右键"}


@dataclass(slots=True)
class Action:
    """一条播放动作。时刻 t 是**基准秒**（1.0× 下），不含速度。"""

    t: float              # 起始时刻（基准秒）
    midi: int             # 目标音高
    hold: float           # 按住多久（基准秒）
    key: str | None       # 键盘按键字符；None = 这个音只能用鼠标孔
    mods: list[str]       # 需要同时按住的鼠标键（移调）
    hole: str             # 琴孔号（显示用）
    label: str            # 音名（显示用）
    bar: int              # 属于第几小节（1 起）

    def describe(self) -> str:
        """给日志/界面用的一句话：「按住左键 再按 N  0.31s」。"""
        parts = []
        if self.mods:
            parts.append("按住" + "+".join(_MOD_LABEL.get(m, m) for m in self.mods))
        parts.append(f"按 {self.key}" if self.key else f"点孔 {self.hole}")
        return " 再".join(parts) + f"  {self.hold:.2f}s"


_NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")


def midi_name(m: int) -> str:
    return f"{_NAMES[m % 12]}{m // 12 - 1}"
