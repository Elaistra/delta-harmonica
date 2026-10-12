"""网页对账 —— 把「我们发的」和「浏览器实际收到的」逐条比。

这是**唯一**能端到端验证"扫描码 → 浏览器 e.code"这一步的办法，
而原程序整套设计都押在这个映射上（它发扫描码而不是虚拟键码，就是为了让游戏收到
正确的 e.code），却从来没被验证过。

用法：
    # 1. 浏览器打开 web/harmonica.html，点一下页面让它有焦点
    # 2. 开始录制
    # 3. 跑演奏（任意一种）：
    #      python -m hca.cli --song 曲名 --engine reference
    #      （或旧程序 口琴自动演奏器.exe 直接弹）
    # 4. 停止录制 → 复制录制结果 → 存成 rec.json
    # 5. 对照：
    python verify/web_conformance.py rec.json 动作表.json

  动作表 JSON 就是 [{"t":秒,"midi":音高,"hold":秒,"key":"C","mods":["right"],...}, ...]
  可以直接用 .workbuddy/analysis/golden_actions.json 里的某一首（是个 dict，用 --song 指定）。

判据：
  * **顺序**必须完全一致（这是硬指标 —— 顺序错就是映射错）
  * **时刻**只做参考对比（浏览器投递有延迟，容差不设死线，只报偏移量）
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 键字符 → 浏览器 e.code（按标准物理位置表）
KEY_TO_CODE = {
    "Z": "KeyZ", "X": "KeyX", "C": "KeyC", "V": "KeyV",
    "B": "KeyB", "N": "KeyN", "M": "KeyM", ",": "Comma",
}
CODE_TO_KEY = {v: k for k, v in KEY_TO_CODE.items()}


def expected_stream(actions: list[dict]) -> list[dict]:
    """把动作表展开成"应该发出的按键序列"。"""
    out = []
    for i, a in enumerate(actions):
        mods = list(a.get("mods") or [])
        for m in mods:
            out.append({"type": "mousedown", "mod": m, "idx": i})
        if a.get("key"):
            out.append({"type": "keydown", "code": KEY_TO_CODE.get(a["key"], a["key"]), "idx": i})
            out.append({"type": "keyup", "code": KEY_TO_CODE.get(a["key"], a["key"]), "idx": i})
        else:
            out.append({"type": "skip-keyless", "idx": i,
                        "why": f"孔 {a.get('hole')} 没有键位（原程序按不出这种音，会跳过）"})
        for m in reversed(mods):
            out.append({"type": "mouseup", "mod": m, "idx": i})
        out[-1]["t"] = a.get("t")
    return out


def received_stream(events: list[dict]) -> list[dict]:
    """浏览器录到的原始事件 → 可比较的序列（丢掉无关按键）。"""
    out = []
    for e in events:
        t = e.get("type")
        if t == "keydown-other":
            out.append({"type": "other", "code": e.get("code"), "dt": e.get("dt")})
        elif t in ("keydown", "keyup"):
            out.append({"type": t, "code": e.get("code"), "dt": e.get("dt")})
        elif t in ("mousedown", "mouseup"):
            out.append({"type": t, "mod": e.get("mod"), "dt": e.get("dt")})
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("rec", help="浏览器导出的录制 JSON")
    ap.add_argument("actions", help="动作表 JSON（数组，或 golden_actions.json 那种字典）")
    ap.add_argument("--song", help="当动作表是字典时，指定用哪一首")
    ap.add_argument("--loose", action="store_true", help="忽略 other 事件（手动误按的键）")
    args = ap.parse_args()

    rec = json.loads(Path(args.rec).read_text(encoding="utf-8"))
    events = rec["events"] if isinstance(rec, dict) else rec

    data = json.loads(Path(args.actions).read_text(encoding="utf-8"))
    if isinstance(data, dict):
        if args.song:
            acts = data[args.song]
        else:
            k = next(iter(data))
            print(f"（动作表里有 {len(data)} 首，未指定 --song，用第一首：{k}）")
            acts = data[k]
    else:
        acts = data

    exp = expected_stream(acts)
    got = received_stream(events)
    if args.loose:
        got = [g for g in got if g["type"] != "other"]

    print("=" * 92)
    print(f"动作表：{len(acts)} 个动作 → 期望 {len(exp)} 个输入事件")
    print(f"录制到：{len(events)} 条原始事件 → 可比 {len(got)} 个")
    print("=" * 92)

    def sig(e):
        return (e["type"], e.get("code") or e.get("mod"))

    n = min(len(exp), len(got))
    first_bad = None
    for i in range(n):
        we, wg = sig(exp[i]), sig(got[i])
        if we != wg:
            first_bad = i
            break

    if first_bad is None:
        if len(exp) == len(got):
            print(f"✔ 顺序完全一致：{n} 个输入事件逐个对上。")
            print("  → 说明「键盘字符 → 扫描码 → 浏览器 e.code」这条链完全正确。")
        else:
            which = "期望的更多" if len(exp) > len(got) else "收到的更多"
            print(f"⚠ 前 {n} 个事件顺序一致，但数量不同（{which}）。")
            print(f"  期望 {len(exp)} / 收到 {len(got)}")
    else:
        print(f"✘ 第 {first_bad + 1} 个事件不一致：")
        print(f"   期望：{sig(exp[first_bad])}")
        print(f"   收到：{sig(got[first_bad])}")
        for k in range(max(0, first_bad - 2), min(n, first_bad + 3)):
            mark = "✘" if k == first_bad else " "
            print(f"   {mark} [{k+1:>4}] 期望 {str(sig(exp[k])):<22} 收到 {str(sig(got[k]))}")

    # 时刻对比（只报偏移，不判死）
    xs = [(exp[i].get("t"), got[i].get("dt")) for i in range(n)
          if exp[i].get("t") is not None and got[i].get("dt") is not None]
    if len(xs) > 2:
        base = xs[0][1] - xs[0][0]
        diffs = [(g - base) - e for e, g in xs]
        print(f"\n时刻偏移（浏览器投递延迟已扣除基准）："
              f"平均 {sum(diffs)/len(diffs)*1000:+.0f} ms，"
              f"最大 {max(abs(d) for d in diffs)*1000:.0f} ms")
        print("  （只作参考：浏览器定时器精度 + 我们的调度轮询都会带来抖动）")

    return 0 if first_bad is None else 1


if __name__ == "__main__":
    sys.exit(main())
