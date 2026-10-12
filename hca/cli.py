"""命令行入口。

    python -m hca.cli --list                          列出曲库
    python -m hca.cli --song 晴天 --dry-run            预演（只打印要按什么，不发键）
    python -m hca.cli --index 1 --engine reference     用旧内核真跑（对照用）
    python -m hca.cli --info 晴天                      只看解析结果（小节/音数/头信息）

引擎（--engine）：
    own        自己的内核（重写中：解析层已可用，动作编译层待做）
    reference  原 exe 里的内核（用来对照验证，需要 3.14 + 那份 pyc）
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import config as cfgmod
from . import library
from .inputs import make_backend
from .mapping import list_mappings, load_mapping
from .model import Action, midi_name
from .compile import compile_actions
from .player import RATE_MAX, RATE_MIN, Player

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
CONFIG_PATH = ROOT / "hca.json"


def _fmt_meter(score) -> str:
    return f"{score.beats_per_bar:g}/{score.meter_den}"


def _act_line(a) -> str:
    """统一格式地描述一条动作（新旧内核的 Action 对象都能用）。"""
    mods = ""
    if a.mods:
        lbl = {"left": "左键", "middle": "中键", "right": "右键"}
        mods = "  按住" + "+".join(lbl.get(m, m) for m in a.mods)
    where = f"按 {a.key}" if a.key else f"点孔 {a.hole}"
    return f"{a.t:7.2f}s  {a.label:<4} {where}{mods}  {a.hold:.2f}s"


def _summary(score) -> str:
    warns = f"  ⚠{len(score.warnings)}" if score.warnings else ""
    return (f"{score.title}  {_fmt_meter(score)} · {score.bar_count()} 小节 · "
            f"{score.note_count()} 音 · {score.bpm:g} BPM{warns}")


def main(argv=None) -> int:
    cfg = cfgmod.load(CONFIG_PATH)
    ap = argparse.ArgumentParser(
        prog="三角洲口琴自动演奏", description="三角洲口琴自动演奏 —— 用真实键鼠事件驱动游戏",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--lib", default=cfg["lib_dir"], help="曲库目录（相对程序目录）")
    ap.add_argument("--mapping", default=cfg["mapping"],
                    help=f"键位映射名或路径（现有：{', '.join(list_mappings()) or '无'}）")
    ap.add_argument("--engine", choices=("own", "reference"), default=cfg["engine"])
    ap.add_argument("--list", action="store_true", help="列出曲库")
    ap.add_argument("--info", metavar="名字", help="只看解析结果，不演奏")
    ap.add_argument("--song", metavar="名字", help="按曲名或文件名挑一首")
    ap.add_argument("--index", type=int, help="挑第 N 首（从 1 数）")
    ap.add_argument("--rate", type=float, default=cfg["rate"],
                    help=f"速度倍率 {RATE_MIN}~{RATE_MAX}（默认 1）")
    ap.add_argument("--seconds", type=float, help="只演前 N 秒（试听）")
    ap.add_argument("--countdown", type=float, default=cfg["countdown"], help="开始前倒数几秒")
    ap.add_argument("--loop", action="store_true", help="循环演奏")
    ap.add_argument("--no-intro", action="store_true", help="跳过前奏")
    ap.add_argument("--approx", action="store_true", help="人手够不到的音整八度搬进来")
    ap.add_argument("--dry-run", action="store_true", help="只打印演奏计划，不真的按键")
    ap.add_argument("--park-cursor", action="store_true",
                    help="发鼠标移调键前把光标挪到角落（免得点中琴孔）")
    args = ap.parse_args(argv)

    mapping = load_mapping(args.mapping)
    if mapping.source != "measured":
        note = (mapping.note or "").strip()
        note = note[:78] + "…" if len(note) > 78 else note
        print(f"⚠ 键位映射「{mapping.name}」来源={mapping.source}（未经目标游戏实测）"
              f"，结果对不对无法保证。{note}")

    lib_dir = library.resolve_lib_dir(args.lib, ROOT)
    files = library.list_library(lib_dir)
    if args.list or (not args.song and not args.index and not args.info):
        print(f"曲库 {lib_dir}（{len(files)} 首）")
        for i, f in enumerate(files, 1):
            try:
                s = library.load_score(f, mapping)
                print(f"  {i:>2}. {Path(f).name[:44]:<46} {_fmt_meter(s)} "
                      f"{s.bar_count():>3} 小节 {s.note_count():>4} 音 {s.bpm:g} BPM")
            except Exception as e:
                print(f"  {i:>2}. {Path(f).name[:44]:<46} ✘ {type(e).__name__}: {e}")
        return 0

    # 选曲
    if args.info:
        needle = args.info
    elif args.song:
        needle = args.song
    else:
        needle = None
    if needle:
        picked = [f for f in files
                  if needle.lower() in Path(f).name.lower()]
        if not picked:
            for f in files:
                try:
                    if needle.lower() in library.load_score(f, mapping).title.lower():
                        picked.append(f)
                except Exception:
                    pass
        if not picked:
            print(f"没有匹配「{needle}」的曲子。")
            return 2
        target = picked[0]
    else:
        idx = (args.index or 1) - 1
        if not 0 <= idx < len(files):
            print(f"序号超出范围（曲库 {len(files)} 首）。")
            return 2
        target = files[idx]

    # ── 出动作表：自己的内核，或借旧内核对照 ──────────────────────
    if args.engine == "own":
        score = library.load_score(target, mapping)
        acts = compile_actions(score, not args.no_intro, 1.0, args.approx, mapping)
    else:
        from .reference import load_legacy
        core = load_legacy()
        print("（参考引擎：原程序内核 —— 只用于对照；此模式下 --mapping 不生效）")
        score = core.load_score(target)
        acts = core.compile_actions(score, not args.no_intro, 1.0, args.approx)

    print(_summary(score))
    for w in (score.warnings or [])[:5]:
        print(f"   ⚠ {w}")
    if len(score.warnings or []) > 5:
        print(f"   ⚠ …… 还有 {len(score.warnings) - 5} 条")
    print(f"→ {len(acts)} 个动作，原速约 {max((a.t + a.hold for a in acts), default=0):.1f} 秒"
          f"（当前 {args.rate:g}×）")
    for a in acts[:10]:
        print("   " + _act_line(a))

    if args.dry_run:
        print("\n[dry-run] 上面就是演奏计划，没有发送任何按键。")
        return 0

    if not acts:
        print("这首曲子一个音都弹不出来。")
        return 3
    backend = make_backend(mapping, dry_run=False, park_cursor=args.park_cursor)
    if hasattr(backend, "park"):
        backend.park()
    player = Player(backend, on_log=print)
    try:
        player.play(acts, countdown=args.countdown, loop=args.loop,
                    limit_s=args.seconds, rate=args.rate)
    except KeyboardInterrupt:
        player.stop()
        print("\n中断。")
    finally:
        if hasattr(backend, "unpark"):
            backend.unpark()
    return 0


if __name__ == "__main__":
    sys.exit(main())
