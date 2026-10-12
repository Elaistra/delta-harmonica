"""键位映射 —— **可插拔配置**，这是本项目最重要的一处架构决定。

为什么单独抽出来：整套自动演奏的"乐谱音高 → 按哪个键"依赖**目标游戏自己的键位约定**。
原程序的整套映射是从那个网页复刻版（app.js）逆向出来的，**是否等于别的游戏（例如
三角洲行动本体）的键位，目前没有验证过**。所以：

  * 映射写成 JSON 配置文件，换游戏只改配置、不改代码；
  * 每个映射文件必须带 `source` 字段说明它的来历（逆向/实测/推测），
    推测级别的映射会被标记出来，避免"用一份猜的映射跑出来的结果被当成正确"。

内置的 `web_harmonica` 映射 = 从原程序里实测出来的常量，可作为对照基准。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

MAP_DIR = Path(__file__).resolve().parent.parent / "mapping"

__all__ = ["KeySlot", "Mapping", "load_mapping", "list_mappings"]


@dataclass
class KeySlot:
    key: str          # 键盘字符（大写）
    hole: str         # 琴孔号（给人看的）
    base_midi: int    # 这个键的基础音高
    scancode: int     # Windows 扫描码（游戏读物理位置，所以必须发扫描码）


# 从原程序实测出来的常量（web_harmonica）
WEB_KEYS = [
    KeySlot("Z", "1", 60, 44), KeySlot("X", "2", 62, 45), KeySlot("C", "3", 64, 46),
    KeySlot("V", "4", 65, 47), KeySlot("B", "5", 67, 48), KeySlot("N", "6", 69, 49),
    KeySlot("M", "7", 71, 50), KeySlot(",", "8", 72, 51),
]
WEB_KEYLESS = [("9", 74), ("10", 76)]          # 没有键盘键的孔，只能用鼠标
WEB_MODIFIERS = {"left": ("左键", -12), "middle": ("中键", 1), "right": ("右键", 12)}
WEB_COMBOS = [(0, []), (1, ["middle"]), (-11, ["left", "middle"]),
              (-12, ["left"]), (12, ["right"]), (13, ["middle", "right"])]


@dataclass
class Mapping:
    name: str = "web_harmonica"
    label: str = "网页复刻版（从原程序实测）"
    source: str = "reverse"          # reverse=逆向实测 / measured=手工实测 / guess=推测
    note: str = ""
    keys: list[KeySlot] = field(default_factory=lambda: list(WEB_KEYS))
    keyless_holes: list[tuple[str, int]] = field(default_factory=lambda: list(WEB_KEYLESS))
    modifiers: dict[str, tuple[str, int]] = field(default_factory=lambda: dict(WEB_MODIFIERS))
    combos: list[tuple[int, list[str]]] = field(default_factory=lambda: list(WEB_COMBOS))
    major_semitones: list[int] = field(default_factory=lambda: [0, 2, 4, 5, 7, 9, 11])
    max_offset: int = 12             # 单音最大半音偏移
    bend_range: int = 2              # 压音范围（半音）
    octave_shift_cost: int = 8       # 整小节挪八度的代价权重
    gap_ratio: float = 0.08          # 音间留白比例
    max_gap_beats: float = 0.09
    end_pad_beats: float = 1.5       # 收尾补时长
    grid: float = 0.25               # 量化网格（拍）

    # ── 派生量：从上面算出来，不要手填 ────────────────────────────────
    def program_range(self) -> tuple[int, int]:
        """**程序**通道够得到的音域（孔 + 半音偏移 + 压音）。原程序是 46~90。"""
        bases = [k.base_midi for k in self.keys] + [m for _, m in self.keyless_holes]
        return (min(bases) - self.max_offset - self.bend_range,
                max(bases) + self.max_offset + self.bend_range)

    def player_range(self) -> tuple[int, int]:
        """**玩家用键盘**够得到的音域（键位 × 移调组合）。原程序是 48~85。"""
        bases = [k.base_midi for k in self.keys]
        semis = [s for s, _ in self.combos]
        return (min(b + s for b in bases for s in semis),
                max(b + s for b in bases for s in semis))

    def scan_of(self, ch: str) -> int | None:
        for k in self.keys:
            if k.key == ch:
                return k.scancode
        return None

    def key_of_hole(self, hole: str) -> str | None:
        for k in self.keys:
            if k.hole == hole:
                return k.key
        return None

    # ── 读写 ────────────────────────────────────────────────────────
    def to_dict(self) -> dict:
        d = asdict(self)
        d["keys"] = [asdict(k) if hasattr(k, "__dataclass_fields__") else k for k in d["keys"]]
        d["keyless_holes"] = [list(x) for x in d["keyless_holes"]]
        d["combos"] = [[s, list(m)] for s, m in d["combos"]]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Mapping":
        d = dict(d)
        d["keys"] = [KeySlot(**k) if isinstance(k, dict) else KeySlot(*k) for k in d.get("keys", [])]
        d["keyless_holes"] = [tuple(x) for x in d.get("keyless_holes", [])]
        d["modifiers"] = {k: tuple(v) for k, v in d.get("modifiers", {}).items()}
        d["combos"] = [(s, list(m)) for s, m in d.get("combos", [])]
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=1), encoding="utf-8")


def list_mappings() -> list[str]:
    return sorted(p.stem for p in MAP_DIR.glob("*.json")) if MAP_DIR.is_dir() else []


def load_mapping(name_or_path: str) -> Mapping:
    """按名字（找 mapping/ 目录）或直接给路径加载。找不到就返回内置网页映射。"""
    p = Path(name_or_path)
    if not p.suffix:
        p = MAP_DIR / f"{name_or_path}.json"
    if p.is_file():
        return Mapping.from_dict(json.loads(p.read_text(encoding="utf-8")))
    return Mapping()
