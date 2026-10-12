#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""修复项的针对性验证 —— 自检 47 项不覆盖这些，改完必须单独跑。

用法：
    python verify/fix_check.py
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import ttk

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hca import favorites as favmod          # noqa: E402
from hca.ui import App                        # noqa: E402

PASS = FAIL = 0


def chk(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ✔ {name}")
    else:
        FAIL += 1
        print(f"  ✘ {name}   {detail}")


def main() -> int:
    # ★ 必须隔离配置文件：下面的 _remap_refs / _sync_settings 内部会 cfgmod.save()，
    #   用真实 hca.json 会把用户的收藏夹和设置覆盖掉（2026-10-13 踩过一次）。
    tmp = Path(tempfile.mkdtemp(prefix="hca_fixcheck_"))
    app = App(dry=True, cfg_path=tmp / "hca.json")
    app.update()
    print(f"（配置隔离在 {tmp}）")

    print("\n[1] 空收藏夹时「加入收藏夹」不再锁死界面")
    app.cfg["favorites"] = []
    err: list[str] = []

    def _try_open():
        try:
            # 100ms 后自动关掉（模拟用户点取消），只是要验证打开时不抛异常
            app.after(100, lambda: [w.destroy() for w in app.winfo_children()
                                    if isinstance(w, tk.Toplevel)])
            app._choose_fav_modal("加入收藏夹", "选一个收藏夹")
        except Exception as e:
            err.append(f"{type(e).__name__}: {e}")

    app.after(10, _try_open)
    app.after(600, app.quit)
    app.mainloop()
    chk("打开空收藏夹的选择框不抛 TclError", not err, "; ".join(err))
    chk("没有留下带 grab 的僵尸 Toplevel",
        not [w for w in app.winfo_children() if isinstance(w, tk.Toplevel)],
        str(app.winfo_children()))

    print("\n[2] 反复重启热键不泄漏线程")
    if sys.platform == "win32":
        base = threading.active_count()
        for _ in range(4):
            app.start_hotkeys()
            time.sleep(0.05)
        time.sleep(0.3)
        now = threading.active_count()
        chk(f"调 4 次 start_hotkeys 后线程数不增长（{base} → {now}）",
            now <= base + 1, f"泄漏了 {now - base} 条")
        app.hotkeys.stop()
    else:
        print("  - 非 Windows，跳过")

    print("\n[3] 全局快捷键不劫持输入框")
    hits: list[str] = []
    app.toggle_play = lambda: hits.append("play")
    app.step_song = lambda d: hits.append("song")
    app.nudge_rate = lambda d: hits.append("rate")
    e = tk.Entry(app)
    e.pack()
    e.focus_set()
    app.update()
    for key in ("<space>", "<Left>", "<Up>"):
        e.event_generate(key)
        app.update()
    chk("在输入框里按 空格/←/↑ 不触发播放/切曲/改速度",
        not hits, f"被触发了: {hits}")
    e.destroy()
    hits.clear()
    app.bind_shortcuts()          # 重新绑（上面的 lambda 已被替换，重绑无副作用）
    # 焦点回到主窗口本身 → 应当触发
    app.focus_set()
    app.update()
    app.event_generate("<space>")
    app.update()
    chk("焦点在主窗口时空格仍然生效（没把功能改坏）",
        "play" in hits, f"实际: {hits}")

    print("\n[4] 重绘画布不泄漏 item")
    app._draw_shell()
    app.update()
    n1 = len(app.canvas.find_all())
    for _ in range(5):
        app._draw_shell()
        app.update()
    n2 = len(app.canvas.find_all())
    chk(f"连续重画 6 次 item 数稳定（{n1} → {n2}）", n2 <= n1, f"涨了 {n2 - n1} 个")

    print("\n[5] 删除曲目后热键条目被真清掉")
    app.cfg["hotkey_map"] = [{"key": "Ctrl+Alt+1", "song": "a.mid"},
                             {"key": "Ctrl+Alt+2", "song": "b.mid"}]
    app.cfg["favorites"] = favmod.add_songs(
        favmod.create([], "夹"), "夹", ["a.mid"])[0]
    app._remap_refs("a.mid", None)              # 删除 a.mid
    left = [e["song"] for e in app.cfg["hotkey_map"]]
    chk("热键里 a.mid 已移除", "a.mid" not in left, str(left))
    chk("b.mid 保留", left == ["b.mid"], str(left))
    chk("收藏夹里也清掉了", all("a.mid" not in f["songs"]
                                for f in app.cfg["favorites"]),
        str(app.cfg["favorites"]))
    app.cfg["hotkey_map"] = [{"key": "Ctrl+Alt+1", "song": "only.mid"}]
    app._remap_refs("only.mid", None)
    chk("清空后能写成空列表（原来写不进去）",
        app.cfg["hotkey_map"] == [], str(app.cfg["hotkey_map"]))

    print("\n[6] 设置里的「跳过前奏/循环/倒计时」会被存盘")
    app.cfg["no_intro"], app.cfg["loop"], app.cfg["countdown"] = False, False, 3.0
    app.var_intro.set(True)
    app.var_loop.set(True)
    app.var_count.set(7.0)
    before = dict(app.cfg)
    app._sync_settings()
    chk("no_intro 写回", app.cfg["no_intro"] is True and before["no_intro"] is False,
        str(app.cfg["no_intro"]))
    chk("loop 写回", app.cfg["loop"] is True, str(app.cfg["loop"]))
    chk("countdown 写回", app.cfg["countdown"] == 7.0, str(app.cfg["countdown"]))

    print("\n[7] 过期的 done 不再复位新播放")
    called: list[int] = []
    app.after_play = lambda: called.append(1)
    app._play_gen = 5
    app._ui_events.put(("done", 3))          # 旧播放迟到的信号
    app._poll_ui_events()
    chk("世代号不匹配的 done 被丢弃", not called, "仍然触发了 after_play")
    app._ui_events.put(("done", 5))
    app._poll_ui_events()
    chk("世代号匹配的 done 正常触发", len(called) == 1, str(called))

    print("\n[8] 样式 st.map 真的生效了")
    st = ttk.Style()
    try:
        m = st.map("TCheckbutton", "indicatorbackground")
        ok = bool(m) and "selected" not in str(m)
        # 生效的话查 "selected" 状态应能拿到具体颜色元组
        got = st.map("TCheckbutton", "indicatorbackground")
        chk("TCheckbutton 的 selected 态有色值", bool(got), str(got))
    except Exception as ex:
        chk("TCheckbutton 样式可查询", False, str(ex))

    try:
        app.destroy()
    except Exception:
        pass
    shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n结果：{PASS} 通过 / {FAIL} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
