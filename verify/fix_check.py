#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""修复项的针对性验证 —— 自检 47 项不覆盖这些，改完必须单独跑。

用法：
    python verify/fix_check.py
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import ttk

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hca import config as cfgmod              # noqa: E402
from hca import favorites as favmod           # noqa: E402
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

    print("\n[9] 录制热键不再凭空多出 Alt")
    # Windows 下 Tk 的 event.state：0x0001=Shift 0x0004=Ctrl 0x20000=Alt，
    # 而 0x0008 是 NumLock 指示灯位。老代码把 0x0008 当 Alt，于是 NumLock 开着时
    # 录 Ctrl+1 会变成 Ctrl+Alt+1。下面用合成事件直接喂给录制窗复现。
    HK_CASES = [
        ("Ctrl+1（NumLock 开，state=0xc）",
         [("<Key>", "1", 0x0000000C, 0x31)], "Ctrl+1"),
        ("Alt+J（state=0x20008）",
         [("<Key>", "j", 0x00020008, 0x4A)], "Alt+J"),
        ("Ctrl+Alt+J（state=0x2000c）",
         [("<Key>", "j", 0x0002000C, 0x4A)], "Ctrl+Alt+J"),
        ("Ctrl+Shift+1（keysym 会变成 exclam）",
         [("<Key>", "exclam", 0x0000000D, 0x31)], "Ctrl+Shift+1"),
        ("Win+K（按下被系统吃掉，只剩抬键）",
         [("<Key>", "Win_L", 0x00000008, 0x5B),
          ("<KeyRelease>", "k", 0x00000008, 0x4B)], "Win+K"),
        ("单独按 J（没修饰键，应拒绝）",
         [("<Key>", "j", 0x00000008, 0x4A)], None),
    ]
    for label, evs, expect in HK_CASES:
        def _drive(evs=evs):
            tops = [c for c in app.winfo_children() if isinstance(c, tk.Toplevel)]
            if not tops:
                return
            w = tops[-1]
            for seq, ks, st, kc in evs:
                w.event_generate(seq, keysym=ks, state=st, keycode=kc)
                app.update()

        app.after(30, _drive)
        app.after(1200, lambda: [w.destroy() for w in app.winfo_children()
                                 if isinstance(w, tk.Toplevel)])
        got = app._capture_key_modal()
        chk(label, got == expect, f"得到 {got!r} / 期望 {expect!r}")

    print("\n[10] 第二轮审查修的几条")
    # 10a 暂停不该吃掉曲子：_wait_until 用墙钟减 _t0，pause() 必须重锚时间基准
    try:
        from hca.model import Action
        from hca.player import Player

        class _Fake:
            def __init__(self):
                self.n = 0

            def key_down(self, _k):
                self.n += 1

            def key_up(self, _k):
                pass

            def mouse_down(self, _m):
                pass

            def mouse_up(self, _m):
                pass

            def release_all(self):
                pass

        be = _Fake()
        acts = [Action(t=i * 0.5, midi=60, hold=0.2, key="z", mods=[],
                       hole="1", label="C4", bar=1) for i in range(20)]
        pl = Player(be)
        threading.Thread(target=lambda: pl.play(acts), daemon=True).start()
        time.sleep(1.2)
        before = pl.sent
        pl.pause()
        time.sleep(3.0)
        frozen = pl.elapsed
        pl.resume()
        time.sleep(1.0)
        after = pl.sent
        pl.stop()
        chk("暂停 3 秒后不多弹（老版本会跳过 6 个音）",
            after - before <= 4, f"{before} → {after}")
        chk("暂停期间进度冻住", abs(frozen - 1.2) < 0.4, f"{frozen:.2f}s")
    except Exception as ex:
        chk("暂停不跳音", False, f"{type(ex).__name__}: {ex}")

    # 10b 畸形拍号的 MIDI：分母 255 曾让切小节循环跑不完（界面永久卡死）
    try:
        from hca.midi import parse_midi

        def _mk(den_byte, num_byte):
            body = bytes([0x00, 0xFF, 0x58, 0x04, num_byte, den_byte, 24, 8])
            body += bytes([0x00, 0x90, 60, 64, 0x40, 0x80, 60, 0,
                           0x40, 0x90, 62, 64, 0x40, 0x80, 62, 0,
                           0x00, 0xFF, 0x2F, 0x00])
            trk = b"MTrk" + len(body).to_bytes(4, "big") + body
            return b"MThd" + (6).to_bytes(4, "big") + b"\x00\x00\x00\x01\x00\x60" + trk

        for label, den, num in (("分母=255", 255, 4), ("分子=0", 4, 0)):
            t0 = time.perf_counter()
            try:
                parse_midi(_mk(den, num))
                good, detail = True, ""
            except Exception as e:
                good, detail = False, f"{type(e).__name__}: {e}"
            dt = time.perf_counter() - t0
            chk(f"拍号 {label} 不卡死也不除零", good and dt < 5.0, detail or f"{dt:.1f}s")
    except Exception as ex:
        chk("畸形拍号", False, f"{type(ex).__name__}: {ex}")

    # 10c 配置文件：坏文件留底 + 字段类型矫正 + 不残留 .tmp
    cfg_tmp = Path(tempfile.mkdtemp(prefix="hca_cfg_"))
    try:
        bad = cfg_tmp / "hca.json"
        bad.write_text('{"favorites": [{"name": "甲", "songs": ["a.mid"]}], "rate"',
                       encoding="utf-8")
        cfgmod.load(bad)
        chk("坏配置另存 .bad（内容还在）",
            "a.mid" in bad.with_name("hca.json.bad").read_text(encoding="utf-8"))
        weird = cfg_tmp / "w.json"
        weird.write_text(json.dumps({"rate": "快", "countdown": None, "lib_dir": [],
                                     "favorites": "abc", "control_keys": 5}),
                         encoding="utf-8")
        c2 = cfgmod.load(weird)
        chk("rate='快' → 默认 1.0", c2["rate"] == 1.0, str(c2["rate"]))
        chk("countdown=null → 默认 3.0", c2["countdown"] == 3.0, str(c2["countdown"]))
        chk("lib_dir=[] → 字符串", isinstance(c2["lib_dir"], str), str(c2["lib_dir"]))
        chk("favorites='abc' → []", c2["favorites"] == [], str(c2["favorites"]))
        chk("control_keys=5 → 四键齐全",
            set(c2["control_keys"]) == {"toggle", "stop", "prev", "next"},
            str(c2["control_keys"]))
        cfgmod.save(weird, c2)
        chk("存盘后读回一致", cfgmod.load(weird)["favorites"] == c2["favorites"])
        chk("没留下 .tmp", not list(cfg_tmp.glob("*.tmp")))
    except Exception as ex:
        chk("配置文件加固", False, f"{type(ex).__name__}: {ex}")
    finally:
        shutil.rmtree(cfg_tmp, ignore_errors=True)

    # 10d 同一首丢失的曲子在 2 个收藏夹里：老版本按曲名当 iid → 重复 → 启动崩溃
    try:
        app.cfg["favorites"] = [{"name": "甲", "songs": ["gone.mid"]},
                                {"name": "乙", "songs": ["gone.mid"]}]
        app._fav_reload_left()
        app._fav_reload_right()
        miss_rows = [i for i in app.fav_songs.get_children() if str(i).startswith("miss::")]
        chk("丢失曲目只插一行（不撞重复 iid）", len(miss_rows) == 1, str(miss_rows))
    except Exception as ex:
        chk("收藏夹重复丢失曲目", False, f"{type(ex).__name__}: {ex}")

    # 10e 热键改绑：确认后旧条目必须真被删掉
    try:
        app.cfg["hotkey_map"] = [{"key": "Ctrl+1", "song": "a.mid"},
                                 {"key": "Ctrl+2", "song": "b.mid"}]
        app._modal = lambda *a, **k: "ok"
        res = app._hk_dup_warn("Ctrl+1", exclude=1)
        chk("确认改绑 → 旧条目被替换",
            res is True and app.hotkey_map() == [{"key": "Ctrl+2", "song": "b.mid"}],
            str(app.hotkey_map()))
        app.cfg["hotkey_map"] = [{"key": "Ctrl+1", "song": "a.mid"},
                                 {"key": "Ctrl+2", "song": "b.mid"}]
        app._modal = lambda *a, **k: None
        chk("取消 → 一条都不删",
            app._hk_dup_warn("Ctrl+1", exclude=1) is False
            and len(app.hotkey_map()) == 2, str(app.hotkey_map()))
    except Exception as ex:
        chk("热键改绑", False, f"{type(ex).__name__}: {ex}")

    try:
        app.destroy()
    except Exception:
        pass
    shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n结果：{PASS} 通过 / {FAIL} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
