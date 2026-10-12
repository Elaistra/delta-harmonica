"""动作编译 —— Score（乐谱中间表示）→ [Action]（带时刻的按键表）。

时间口径（照旧内核，也是整个程序最关键的一条约定）：
    动作表里的时刻永远是「**1.0× 基准速度下的秒数**」，**不含速度**。
    速度只在播放调度时除一下（见 player.Player._wait_until）。

时值口径（实测）：
    按住时长 = (音符时值 − 留白) × 每拍秒数
    留白     = min(时值 × GAP_RATIO, MAX_GAP_BEATS)      ← 0.5 拍时留 0.04 拍
    末尾那个音额外补 END_PAD_BEATS 拍（1.5 拍），免得收得太干。
"""

from __future__ import annotations

from .mapping import Mapping
from .model import Action, Score, midi_name
from .pitch import (choose_octave_offset, degree_to_step, fold_into_range,
                    fold_to_player_range, step_to_midi, suggest)

__all__ = ["compile_actions", "degree_label"]


def degree_label(degree: int, octave: int) -> str:
    return f"{degree}{'+' if octave >= 0 else ''}{octave}"


def compile_actions(score: Score, include_intro: bool = True, rate: float = 1.0,
                    approximate: bool = False, mapping: Mapping | None = None,
                    legato: bool = True) -> list[Action]:
    """Score → [Action]。rate 参数**不参与时间计算**（只为了兼容旧签名）。

    ★ 人手够不到的音**一律就近搬进人手范围再弹**（2026-10-12 用户要求取消
    "跳过"限制）。`approximate` 参数仅为兼容旧调用保留，已无效。

    legato（连奏防断音，默认开）：
        旧内核给每个音砍掉最多 8% 的按住时长（gap_ratio）当"音间留白"，
        逐音累积起来就是明显的断音感。连奏模式按住**满时值**，只在松开→
        再按之间留 20ms 安全间隔（调度器是顺序发送，松开必先于下一次按下）。
    """
    mp = mapping or Mapping()
    sec_per_beat = 60.0 / (score.bpm or 120.0)
    tonic_midi = 60 + score.tonic_index

    actions: list[Action] = []
    t = 0.0
    prev_offset = 0
    bar_index = 0
    total_bars = score.bar_count()

    for section in score.sections:
        if section.intro and not include_intro:
            continue
        for bar in section.bars:
            bar_index += 1
            pairs = [(degree_to_step(n[0], n[1]), n[3]) for n in bar if n[0] != 0]
            offset = choose_octave_offset(pairs, prev_offset, tonic_midi, mp) if pairs else prev_offset
            prev_offset = offset

            for degree, octave, beats, acc in bar:
                if degree == 0:                       # 休止：只推进时间
                    t += beats * sec_per_beat
                    continue
                step = degree_to_step(degree, octave) + offset * 7
                midi = step_to_midi(step, tonic_midi, acc, mp) + 12 * score.octave_shift

                target, _shift = fold_into_range(midi, mp)
                sug = suggest(target, mp)
                if sug is None:
                    # ★ 2026-10-12 用户要求取消"跳过"限制：够不到的音一律
                    #   就近搬进人手范围，不再丢弃（原版只有勾"高音近似"才搬）。
                    alt = fold_to_player_range(target, mp)
                    if alt is not None:
                        score.warnings.append(
                            f"第 {bar_index} 小节 {degree_label(degree, octave)}"
                            f"（MIDI {target}）人手按不出来，已搬进 {midi_name(alt)}")
                        target, sug = alt, suggest(alt, mp)
                    else:
                        score.warnings.append(
                            f"第 {bar_index} 小节 {degree_label(degree, octave)}"
                            f"（MIDI {midi}）超出**人手**可吹范围，已跳过")
                        t += beats * sec_per_beat
                        continue

                if legato:
                    hold_beats = beats          # 连奏：按住满时值，不留"音间留白"
                else:
                    hold_beats = beats - min(beats * mp.gap_ratio, mp.max_gap_beats)
                hold_s = hold_beats * sec_per_beat
                if legato and hold_s > 0.03:
                    hold_s -= 0.02              # 20ms 换指安全间隔
                actions.append(Action(
                    t=round(t, 6), midi=target, hold=round(max(hold_s, 0.01), 6),
                    key=sug["key"], mods=list(sug["mods"]), hole=sug["hole"],
                    label=midi_name(target), bar=bar_index))
                # ★ 时间推进（19:08 加连奏时被编辑误伤丢过一行——丢了的后果是
                #   全曲的音挤进前几秒，"时长对不上"，勾指起誓 456 音只剩 2.45s）
                t += beats * sec_per_beat

    if legato and actions:
        # ★ 连奏补缝：时值量化到 0.25 拍网格后，向下取整会在音与音之间留下
        #   最多一整个 16 分音符的空洞（断音主因，比 gap_ratio 大得多）。
        #   把"半拍以内"的小空隙填满，只留 20ms 给松开→按下。
        #   ≥1 拍的空隙是曲子本身的乐句呼吸，保持原样不动。
        fill_limit = 0.5 * sec_per_beat + 0.03       # ≤ 半拍（含安全间隔余量）
        for a, b in zip(actions, actions[1:]):
            gap = b.t - (a.t + a.hold)
            if 1e-6 < gap <= fill_limit and b.t - a.t > 0.06:
                a.hold = round(max(b.t - a.t - 0.02, a.hold), 6)

    return actions
