"""黄金对照回归 —— 重写版每完成一层，就跑一次这个。

验收基准由**原程序的内核**生成（见 ../.workbuddy/analysis/dump_golden.py）：
    golden_scores.json   25 首的中间表示（头信息 + 每小节的音）
    golden_actions.json  25 首的全部动作（11,363 条）

用法（在 autoplay-ng 目录下）：
    python verify/golden_regression.py            # 只比解析层
    python verify/golden_regression.py --actions  # 连动作层也比（动作层做完才有意义）

判定：**逐条**比，不是抽样。第一处不同就报出来，方便定位。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from hca import library                      # noqa: E402
from hca.mapping import Mapping              # noqa: E402

GOLD_DIR = ROOT.parent / ".workbuddy" / "analysis"
# ★ 不写死绝对路径：优先环境变量 HCA_LIB_DIR，否则用项目旁的「曲库」目录
LIB_DIR = Path(os.environ.get("HCA_LIB_DIR") or (ROOT.parent / "曲库"))
TOL = 1e-6


def bars_of(score) -> list[list]:
    return [list(b) for s in score.sections for b in s.bars]


def diff_bars(mine: list[list], gold: list[list]) -> str | None:
    if len(mine) != len(gold):
        return f"小节数不同：我 {len(mine)} / 基准 {len(gold)}"
    for bi, (mb, gb) in enumerate(zip(mine, gold)):
        if len(mb) != len(gb):
            return (f"第 {bi+1} 小节音符数不同：我 {len(mb)} / 基准 {len(gb)}\n"
                    f"      我  : {mb[:8]}\n      基准: {gb[:8]}")
        for ni, (mn, gn) in enumerate(zip(mb, gb)):
            if int(mn[0]) != int(gn[0]) or int(mn[1]) != int(gn[1]) or int(mn[3]) != int(gn[3]) \
                    or abs(float(mn[2]) - float(gn[2])) > TOL:
                return (f"第 {bi+1} 小节第 {ni+1} 个音不同：\n"
                        f"      我  : {mn}\n      基准: {gn}")
    return None


def compare_scores(gold_scores: dict) -> tuple[int, int]:
    ok = bad = 0
    print("=" * 96)
    print("解析层对照（中间表示逐小节、逐音比对）")
    print("=" * 96)
    for name, g in sorted(gold_scores.items()):
        path = LIB_DIR / name
        if not path.is_file():
            print(f"  － {name[:52]:<54} 跳过（曲库已无此文件）")
            continue
        try:
            s = library.load_score(path, Mapping())
        except Exception as e:
            bad += 1
            print(f"  ✘ {name[:52]:<54} 解析抛异常：{type(e).__name__}: {e}")
            continue
        head = []
        for field, gv in (("bpm", g["bpm"]), ("beats_per_bar", g["beats_per_bar"]),
                          ("meter_den", g["meter_den"]), ("unit_beats", g["unit_beats"]),
                          ("tonic_index", g["tonic_index"]), ("octave_shift", g["octave_shift"])):
            mv = getattr(s, field)
            if abs(float(mv) - float(gv)) > TOL:
                head.append(f"{field}: 我 {mv} / 基准 {gv}")
        gbars = [b for sec in g["sections"] for b in sec["bars"]]
        d = diff_bars(bars_of(s), gbars)
        if not head and not d:
            ok += 1
            print(f"  ✔ {name[:52]:<54} {len(gbars):>3} 小节 / {s.note_count():>4} 音  完全一致")
        else:
            bad += 1
            print(f"  ✘ {name[:52]:<54} {len(gbars):>3} 小节")
            for h in head:
                print(f"     头信息 → {h}")
            if d:
                print(f"     {d}")
    print(f"\n解析层：{ok} / {ok + bad} 首完全一致")
    return ok, bad


def compare_actions(gold: dict, scores: dict) -> tuple[int, int, int]:
    """动作层逐条对照：时刻 / 音高 / 按住时长 / 按键 / 鼠标组合。

    ★ 只对**解析层已对齐**的曲子判定 —— 解析没对上时，时间轴本身就不同，
      再去比动作层没有意义（差异是上游带来的）。跳过的会被单独计数。
    """
    from hca.compile import compile_actions
    ok = bad = skipped = 0
    print("=" * 96)
    print("动作层对照（只对解析层已对齐的曲子判定）")
    print("=" * 96)
    for name, ga in sorted(gold.items()):
        path = LIB_DIR / name
        if not path.is_file():
            continue
        try:
            s = library.load_score(path, Mapping())
        except Exception:
            continue
        g = scores.get(name)
        if g is not None:
            gbars = [b for sec in g["sections"] for b in sec["bars"]]
            if diff_bars(bars_of(s), gbars) is not None:
                skipped += 1
                continue
        try:
            mine = compile_actions(s, True, 1.0, False, Mapping())
        except Exception as e:
            bad += 1
            print(f"  ✘ {name[:50]:<52} 编译抛异常：{type(e).__name__}: {e}")
            continue
        if len(mine) != len(ga):
            bad += 1
            print(f"  ✘ {name[:50]:<52} 动作数不同：我 {len(mine)} / 基准 {len(ga)}")
            continue
        diff = None
        for i, (m, gg) in enumerate(zip(mine, ga)):
            if (abs(m.t - gg["t"]) > TOL or m.midi != gg["midi"]
                    or abs(m.hold - gg["hold"]) > 1e-5 or (m.key or None) != (gg["key"] or None)
                    or list(m.mods) != list(gg["mods"])):
                diff = (i, m, gg)
                break
        if diff is None:
            ok += 1
            print(f"  ✔ {name[:50]:<52} {len(ga):>4} 条动作完全一致")
        else:
            bad += 1
            i, m, gg = diff
            print(f"  ✘ {name[:50]:<52} 第 {i+1} 条不同")
            print(f"      我  : t={m.t} midi={m.midi} hold={m.hold} key={m.key} mods={m.mods}")
            print(f"      基准: t={gg['t']} midi={gg['midi']} hold={gg['hold']} "
                  f"key={gg['key']} mods={gg['mods']}")
    print(f"\n动作层：{ok} / {ok + bad} 首完全一致"
          f"（另有 {skipped} 首因解析层未对齐而跳过）")
    return ok, bad, skipped


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--actions", action="store_true", help="连动作层一起比")
    args = ap.parse_args()

    gs_path = GOLD_DIR / "golden_scores.json"
    if not gs_path.is_file():
        print(f"找不到基准文件 {gs_path}\n"
              f"先用 Python 3.14 跑一遍 ../.workbuddy/analysis/dump_golden.py 生成。")
        return 2
    gold = json.loads(gs_path.read_text(encoding="utf-8"))
    ok, bad = compare_scores(gold)

    if args.actions:
        ap_path = GOLD_DIR / "golden_actions.json"
        if not ap_path.is_file():
            print(f"\n找不到 {ap_path}")
            return 2
        ga = json.loads(ap_path.read_text(encoding="utf-8"))
        a_ok, a_bad, _skipped = compare_actions(ga, gold)
        return 0 if bad == 0 and a_bad == 0 else 1

    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
