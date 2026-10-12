"""MIDI 解析 —— 重写版。

规则不是猜的：把造好的小 MIDI 喂给原程序、看它输出什么，反推出的**实测规则**
（见 `docs/解析规则-实测.md`）。要点：

  * 只认 PPQ 计时（division 高位为 1 的 SMPTE 直接报错）
  * 默认 tempo 500000us（120 BPM）；调号（key signature）**被忽略** —— tonic 恒为 0
  * 量化到 1/4 拍，用 JS 式四舍五入；**发声最短 0.25 拍**
  * 单声化：同一时刻多个音**只留最高音**；音与音重叠时前一个被截断
  * 音符跨小节线要**切开**
  * 每小节末尾补休止填满拍号；最后一小节也补满
  * 全曲没有音符 → 报错
"""

from __future__ import annotations

import math
import struct

from .mapping import Mapping
from .model import Score, Section, js_round

__all__ = ["looks_like_midi", "parse_midi", "midi_to_note"]


def looks_like_midi(data: bytes) -> bool:
    return len(data) >= 14 and data[:4] == b"MThd"


def _vlq(data: bytes, p: int) -> tuple[int, int]:
    """读变长时值。返回 (值, 新位置)。"""
    val = 0
    n = len(data)
    while p < n:
        b = data[p]
        p += 1
        val = (val << 7) | (b & 0x7F)
        if not b & 0x80:
            break
    return val, p


def _read_meta_len(body: bytes, i: int) -> tuple[int, int]:
    ln = 0
    while i < len(body):
        b = body[i]
        i += 1
        ln = (ln << 7) | (b & 0x7F)
        if not b & 0x80:
            break
    return ln, i


def _collect_notes(data: bytes, division: int) -> tuple[list[tuple[float, float, int]], float, float, int]:
    """扫所有 MTrk，取出 (起始拍, 结束拍, 音高) 与头信息。"""
    pos = 14
    events: list[tuple[float, float, int]] = []
    tempo_us = 500000
    beats_per_bar, meter_den = 4.0, 4
    got_tempo = False

    while pos + 8 <= len(data):
        if data[pos:pos + 4] != b"MTrk":
            pos += 1
            continue
        (tlen,) = struct.unpack(">I", data[pos + 4:pos + 8])
        body = data[pos + 8:pos + 8 + tlen]
        pos += 8 + tlen

        i = 0
        tick = 0
        running = 0
        open_notes: dict[int, int] = {}          # 音高 → 起始 tick（同一音高只留最后一个）
        while i < len(body):
            delta, i = _vlq(body, i)
            tick += delta
            if i >= len(body):
                break
            st = body[i]
            if st & 0x80:
                i += 1
                running = st
            else:
                st = running
            if st == 0:
                break

            if st == 0xFF:                        # meta
                if i >= len(body):
                    break
                mt = body[i]
                i += 1
                ln, i = _read_meta_len(body, i)
                chunk = body[i:i + ln]
                i += ln
                if mt == 0x51 and ln == 3 and not got_tempo:
                    tempo_us = (chunk[0] << 16) | (chunk[1] << 8) | chunk[2]
                    got_tempo = True
                elif mt == 0x58 and ln >= 2:
                    # ★ 实测：拍号要换算成「四分音符拍」—— 6/8 → 3.0，1/8 → 0.5，2/2 → 4.0
                    meter_den = 2 ** chunk[1]
                    beats_per_bar = chunk[0] * 4.0 / meter_den
                continue

            if st in (0xF0, 0xF7):                # sysex
                ln, i = _read_meta_len(body, i)
                i += ln
                continue

            hi = st & 0xF0
            if hi in (0xC0, 0xD0):                # program change / channel pressure
                i += 1
                continue
            if i + 2 > len(body):
                break
            d1 = body[i]
            d2 = body[i + 1]
            i += 2
            if hi == 0x90 and d2 > 0:             # note on
                open_notes.setdefault(d1, tick)   # ★ 已经在响的音再按一次 → 忽略（实测）
            elif hi == 0x80 or (hi == 0x90 and d2 == 0):   # note off
                on = open_notes.pop(d1, None)
                if on is not None:
                    events.append((on / division, tick / division, d1))

    # 没收尾的音：给一个 1 拍的默认长度
    for key, on in open_notes.items():
        events.append((on / division, on / division + 1.0, key))

    events.sort(key=lambda e: (e[0], -e[2]))
    bpm = round(60_000_000 / tempo_us) if tempo_us else 120
    return events, float(bpm), beats_per_bar, meter_den


def _quant(v: float, grid: float) -> float:
    return js_round(v / grid) * grid


def _mono(events: list[tuple[float, float, int]]) -> list[tuple[float, float, int]]:
    """单声化（在**原始时间**上做）—— 按源音符逐个扫，高音优先。

    规则只有两条：
      * 新音比正在响的音**高** → 正在响的截断到新音起点，改由新音继续
      * 新音比正在响的音**低或相等** → 新音**整个丢掉**
    ★ 不合并"相邻同音高的两个源音符"：旧内核会把它们保留成两个音（实测）。
    """
    evs = sorted(events, key=lambda e: (e[0], -e[2]))
    raw: list[tuple[float, float, int]] = []
    cur: list | None = None
    for on, off, key in evs:
        if cur is None:
            cur = [on, off, key]
            continue
        if on >= cur[1] - 1e-9:
            raw.append((cur[0], cur[1], cur[2]))
            cur = [on, off, key]
        elif key > cur[2]:
            raw.append((cur[0], on, cur[2]))
            cur = [on, off, key]
    if cur is not None:
        raw.append((cur[0], cur[1], cur[2]))
    return raw


def _to_bars(raw: list[tuple[float, float, int]], bar_beats: float,
             grid: float) -> list[list[list]]:
    """量化 + 切小节 —— ★ 口径已用合成 MIDI 黑盒实锤（2026-10-12，见
    docs/解析规则-实测.md 第二节）：

      1. 每个音的**位置 = 它自己原始起点的量化值**（绝对对表），不是顺序装箱；
         后面的音允许"叠在"前面音的显示范围里（红尘客栈 42 小节：两个 0.25 音
         同占 167.25，旧内核原样都保留）。
      2. 游标 cursor = max(cursor, pos + dur)；gap = pos − cursor > 0 才补休止，
         休止从 cursor 铺到 pos。
      3. 时值 = max(网格, quant(off − on))（原始时长，不变）。
      4. 小节 = 按绝对位置 floor(pos / 拍数) 归属；**允许溢出**（死别/刻我/红尘
         客栈都有 Σ4.25 > 4.0 的小节，旧内核就是这样，不裁）。
      5. 结尾：只把"最后一个音所落在的小节"补满休止，不再新建空小节（probe ①）。
    """
    items: list[tuple[float, float, tuple]] = []      # (绝对起点拍, 时值, 音)
    cursor = 0.0
    for on, off, key in raw:
        pos = _quant(on, grid)
        dur = max(grid, _quant(off - on, grid))
        if pos > cursor + 1e-9:                       # 空档 → 休止铺满
            items.append((cursor, pos - cursor, (0, 0, 0)))
        items.append((pos, dur, midi_to_note(key, 0)))
        cursor = max(cursor, pos + dur)

    bars: dict[int, list[list]] = {}
    for start, dur, item in items:
        t, remain = start, dur
        while remain > 1e-9:                          # 跨小节线切开
            bar_i = int(math.floor(t / bar_beats + 1e-9))
            take = min(remain, (bar_i + 1) * bar_beats - t)
            bars.setdefault(bar_i, []).append(
                [item[0], item[1], round(take, 6), item[2]])
            t += take
            remain -= take

    if cursor <= 1e-9:
        return [[]]
    last_i = int(math.floor((cursor - 1e-9) / bar_beats))
    bar_end = (last_i + 1) * bar_beats
    if cursor < bar_end - 1e-9:                       # 尾小节补满（只补这一小节）
        bars.setdefault(last_i, []).append(
            [0, 0, round(bar_end - cursor, 6), 0])
    return [bars.get(i, []) for i in range(max(bars) + 1 if bars else 1)]


def midi_to_note(midi: int, tonic_index: int = 0, semitones=(0, 2, 4, 5, 7, 9, 11)):
    """MIDI 音高 → (音级 1..7, 八度, 升降)。

    在"主音所在的大调音阶"里找不超过它的最大音级，差多少就是升降。
    八度以 C4(60) 为 0 号：C1(24) → -3，C7(96) → 3（与原程序一致，已实测）。
    """
    off = (midi - tonic_index) % 12
    best_deg, best_semi = 1, 0
    for idx, semi in enumerate(semitones):
        if semi <= off and semi >= best_semi:
            best_deg, best_semi = idx + 1, semi
    acc = off - best_semi
    octave = (midi - tonic_index) // 12 - 5
    return best_deg, octave, acc


def parse_midi(data: bytes, mapping: Mapping | None = None) -> Score:
    """标准 MIDI 文件 → Score。"""
    mp = mapping or Mapping()
    if not looks_like_midi(data):
        raise ValueError("不是 MIDI 文件（开头没有 MThd）")
    (fmt, _ntrk, division) = struct.unpack(">HHH", data[8:14])
    if division & 0x8000:
        raise ValueError("这个 MIDI 用的是 SMPTE 计时（按帧走），不支持")
    if division == 0:
        raise ValueError("MIDI 头里的 division 是 0，文件坏了")

    events, bpm, beats_per_bar, meter_den = _collect_notes(data, division)
    if not events:
        raise ValueError("这个 MIDI 里没有音符")

    mono = _mono(events)
    if not mono:
        raise ValueError("量化之后一个音都不剩")

    bars = _to_bars(mono, beats_per_bar, mp.grid)
    score = Score(bpm=bpm, beats_per_bar=beats_per_bar, meter_den=meter_den,
                  unit_beats=1.0, tonic_index=0, octave_shift=0)
    score.sections = [Section(name="主旋律", intro=False, bars=bars)]
    return score
