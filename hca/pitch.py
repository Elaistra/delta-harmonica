"""音高映射层 —— 决定"这个音怎么弹出来"。

这一层的每条规则都是**从旧内核实测出来的**（`probe_pitch.py` 穷举 0~127 + 读
`choose_octave_offset` 的字节码），不是推测。所有常量都从 Mapping 派生，
所以换一份键位映射，整套行为会跟着变。

三个概念别混：
  * **程序通道**（pose_for）：这台机器的能力 —— 10 个孔的 base ± 半音偏移 ± 压音，46~90
  * **玩家手**（suggest）：人手上的 8 个键 × 6 档鼠标移调，48~85
  * **整小节平移**（choose_octave_offset）：为了让一个小节尽量落在程序通道里，
    整小节挪几个八度。用 pose_for 判据（不是 suggest），否则会和游戏差一个八度。
"""

from __future__ import annotations

from .mapping import Mapping

__all__ = [
    "degree_to_step", "step_to_midi", "fold_into_range", "pose_for", "suggest",
    "fold_to_player_range", "choose_octave_offset", "program_range", "player_range",
]


def degree_to_step(degree: int, octave: int) -> int:
    """(音级 1..7, 八度) → 全音阶步数。实测：0 + 7×八度。"""
    return (degree - 1) + 7 * octave


def step_to_midi(s: int, tonic_midi: int, acc: int, mapping: Mapping | None = None) -> int:
    """全音阶步数 → MIDI。实测：主音 + 大调音阶[步数 mod 7] + 12×⌊步数/7⌋ + 升降。"""
    mp = mapping or Mapping()
    semi = mp.major_semitones[s % 7]
    return tonic_midi + semi + 12 * (s // 7) + acc


def program_range(mapping: Mapping | None = None) -> tuple[int, int]:
    return (mapping or Mapping()).program_range()


def player_range(mapping: Mapping | None = None) -> tuple[int, int]:
    return (mapping or Mapping()).player_range()


# ── 音域折叠 ────────────────────────────────────────────────────────
def fold_into_range(midi: int, mapping: Mapping | None = None) -> tuple[int, int]:
    """超音域就**整八度**搬进来。返回 (新音高, 搬了几个八度)。

    实测：44→(56,+1)、91→(79,−1)、114→(90,−2) —— 取"刚够进范围"的那一层。
    """
    mp = mapping or Mapping()
    lo, hi = mp.program_range()
    if midi < lo:
        k = -(-(lo - midi) // 12)                 # 向上取整
    elif midi > hi:
        k = -((midi - hi + 11) // 12)             # 向下取整
    else:
        k = 0
    return midi + 12 * k, k


def fold_to_player_range(midi: int, mapping: Mapping | None = None) -> int | None:
    """整八度搬进**玩家手**够得到的范围。搬不动返回 None。"""
    mp = mapping or Mapping()
    lo, hi = mp.player_range()
    if lo <= midi <= hi:
        return midi
    if midi < lo:
        k = -(-(lo - midi) // 12)
    else:
        k = -((midi - hi + 11) // 12)
    out = midi + 12 * k
    return out if lo <= out <= hi else None


# ── 程序通道：这个音"程序"怎么吹出来 ────────────────────────────────
def pose_for(target_midi: int, mapping: Mapping | None = None) -> dict | None:
    """返回 {hole, base, has_key, delta, offset, bend_semi, level}；吹不出来返回 None。

    实测规则：候选 = 每个孔（含无键位的 9/10 孔）的 base；把 delta = 目标 − base
    拆成 (半音偏移, 压音) 两部分，**先让 |偏移| 最小**，再让 |压音| 最小，
    并且**优先"往下压"**（负压音优于正压音）。因此 C#4 会被解释成
    "孔 2 的 B 音往下压半音"，而不是"孔 1 的 C 往上抬半音"。
    """
    mp = mapping or Mapping()
    bases: list[tuple[str, int, bool]] = [(k.hole, k.base_midi, True) for k in mp.keys]
    bases += [(h, m, False) for h, m in mp.keyless_holes]

    best = None
    for hole, base, has_key in bases:
        delta = target_midi - base
        if abs(delta) > mp.max_offset + mp.bend_range:
            continue
        # 试所有可行的 (offset, bend) 拆分，挑"偏移最小、压音最小、优先下压"的
        for offset in range(-mp.max_offset, mp.max_offset + 1):
            bend = delta - offset
            if abs(bend) > mp.bend_range:
                continue
            key = (abs(offset), abs(bend), 0 if bend <= 0 else 1, abs(delta))
            if best is None or key < best[0]:
                best = (key, {
                    "hole": hole, "base": base, "has_key": has_key,
                    "delta": delta, "offset": offset, "bend_semi": bend,
                    "level": abs(offset) // 4 + (1 if bend else 0),
                })
    return best[1] if best else None


# ── 玩家手：这个音"人"该怎么按 ──────────────────────────────────────
def suggest(target_midi: int, mapping: Mapping | None = None) -> dict | None:
    """返回 {key, hole, base, mods, transpose, has_key}；按不出来返回 None。

    实测判据链：**鼠标键最少 → |移调| 最小 → 移调最小 → 基础音最低**。
    所以 73（C#5）会用「, +中键」而不是「Z +中键+右键」（前者少按一个鼠标键）。
    """
    mp = mapping or Mapping()
    best = None
    for k in mp.keys:
        for semi, mods in mp.combos:
            if k.base_midi + semi != target_midi:
                continue
            key = (len(mods), abs(semi), semi, k.base_midi)
            if best is None or key < best[0]:
                best = (key, {
                    "key": k.key, "hole": k.hole, "base": k.base_midi,
                    "mods": list(mods), "transpose": semi, "has_key": True,
                })
    return best[1] if best else None


# ── 整小节挑八度 ────────────────────────────────────────────────────
def choose_octave_offset(notes: list[tuple[int, int]], prev_offset: int,
                         tonic_midi: int, mapping: Mapping | None = None) -> int:
    """给一个小节挑一个整八度平移量（单位：八度）。

    notes 是 **(全音阶步数, 升降)** 的列表 —— 注意不是 (音级, 八度)。

    实测算法（读字节码得到）：
        for offset in range(-4, 5):
            over = |offset| × OCTAVE_SHIFT_COST
            for s, acc in notes:
                pose = pose_for(step_to_midi(s + offset×7, tonic, acc))
                over += |pose.delta|  (pose 为 None 时 +99)
            drift = |offset − prev_offset|
            取 over 最小；平局取 drift 更小的（平局时保持先遇到的，即更小的 offset）
    返回选中的 offset。
    """
    mp = mapping or Mapping()
    best_off, best_over, best_drift = 0, None, None
    for offset in range(-4, 5):
        over = abs(offset) * mp.octave_shift_cost
        for s, acc in notes:
            pose = pose_for(step_to_midi(s + offset * 7, tonic_midi, acc, mp), mp)
            over += abs(pose["delta"]) if pose else 99
        drift = abs(offset - prev_offset)
        if best_over is None or over < best_over or (over == best_over and drift < best_drift):
            best_off, best_over, best_drift = offset, over, drift
    return best_off
