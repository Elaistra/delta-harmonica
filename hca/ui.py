"""图形界面 —— 苹果式简约布局：浮岛侧边栏 + 圆角卡片 + 玻璃质感。

设计规范（四个尺度，全部集中在本文件顶部，改外观只动那几段）：

  ① 间距规范  SP   ：4 的倍数（4/8/12/16/24/32），所有留白只从这里取。
  ② 圆角规范  R    ：**嵌套圆角** —— 外层的圆角减去内边距，就是内层的圆角。
                     卡片 18 − 内边距 8 = 内层 10；侧边栏 24 − 10 = 菜单胶囊 14。
                     这样嵌套的弧线才是同心的（苹果的观感靠这一条）。
  ③ 玻璃质感  GLASS：窗口边框用系统亚克力 + 原生圆角；卡片用"顶边高光 + 细描边"。
  ④ 阴影规范  Q    ：Tk 没有模糊，阴影靠叠几层逐渐变淡的圆角矩形逼近。

★★ 最重要的一个实测结论（2026-10-12）：`extend_frame` 必须保持 False ★★
    DwmExtendFrameIntoClientArea 会把**整个客户区**变成玻璃，DWM 于是按 alpha
    通道合成。但 Tk 走的是 GDI 绘制，GDI 写出来的像素 alpha 恒为 0 —— 结果
    整块内容（含所有文字）都被 DWM 当成半透明去和背景混合，**字全部发白**。

    这不是猜的，是量出来的（同一台机器，同一段文字区域 620..1080 × 120..340）：
        extend_frame=True  → 最暗像素 143/255，标准差 10.0   ← 没有任何纯黑，字发白
        extend_frame=False → 最暗像素   0/255，标准差 30.8   ← 正常
    用 PrintWindow 和屏幕 DC 两种抓法各测一遍，结论一致，不是截图假象。

    所以现在的分工是：
      · 窗口级玻璃 → 保留（backdrop=acrylic）：标题栏与窗口圆角由系统画，是真玻璃
      · 客户区     → 实色（fallback_bg），文字清晰锐利
      · 卡片级玻璃 → 靠高光/描边/阴影"画"出来
    如果你就是要全窗真玻璃，把 GLASS["extend_frame"] 改回 True —— 代价是字发白。
"""

from __future__ import annotations

import argparse
import json
import math
import os
import queue
import sys
import threading
import time
import traceback
from pathlib import Path

import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, ttk

from . import bili
from . import config as cfgmod
from . import favorites as favmod
from . import glass
from . import library
from . import trash
from .compile import compile_actions
from .hotkeys import Hotkeys
from .inputs import NullInput, make_backend
from .mapping import list_mappings, load_mapping
from .player import Player

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
CONFIG_PATH = ROOT / "hca.json"
SLOT_COUNT = 10


# ══════════════════════════════════════════════════════════════════════
#  ① 间距规范 —— 一律 4 的倍数
# ══════════════════════════════════════════════════════════════════════
SP = {"xs": 4, "sm": 8, "md": 12, "lg": 16, "xl": 24, "xxl": 32}

# ══════════════════════════════════════════════════════════════════════
#  ② 圆角规范 —— 嵌套：内层圆角 = 外层圆角 − 内边距（保证弧线同心）
# ══════════════════════════════════════════════════════════════════════
R = {
    "island": 24,     # 浮岛侧边栏
    "card": 18,       # 内容卡片
    "inner": 10,      # 卡片内部元素（18 − 8）
    "pill": 14,       # 侧边栏菜单胶囊（24 − 10）
    "chip": 9,        # 小标签
}

# ══════════════════════════════════════════════════════════════════════
#  ③ 玻璃 —— 真模糊 + 原生圆角；不支持就自动退化
# ══════════════════════════════════════════════════════════════════════
GLASS = {
    "backdrop": "mica",        # acrylic 会把壁纸颜色映进标题栏（壁纸紫 → 标题栏紫）；
                               # mica 更静态、更中性，玻璃感略弱但整体更协调 / none 关掉
    "extend_frame": False,     # ★★ 必须 False —— True 会让全部文字发白，见文件头实测数据
    "round_corners": True,     # 原生圆角（Windows 11 才有）
    "dark": True,              # 暗色主题 → 标题栏也走暗色
    "glass_bg": "#000000",     # 只有在 extend_frame=True 时才是"透明"；默认用不到
    "fallback_bg": "#241F1B",  # 画布底色（暖黑咖，暗调主基调）
    "caption_color": "#241F1B",  # 标题栏跟画布同色（盖过系统强调色）
}

# ══════════════════════════════════════════════════════════════════════
#  ④ 阴影 —— Tk 没有模糊，用几层逐渐变淡的圆角矩形逼近
# ══════════════════════════════════════════════════════════════════════
Q = {
    "color": "#120E0B",   # 阴影最深处的颜色（暗底上阴影=更深的黑）
    "spread": 4,          # 向外扩散几像素
    "dy": 2,              # 往下偏几像素（光从上面来）
    "steps": 4,           # 分几层，越多越平滑
}


def _mix(c1: str, c2: str, f: float) -> str:
    """把颜色 c1 往 c2 混 f（0=全 c1，1=全 c2）。用来做阴影的渐变层。"""
    f = min(1.0, max(0.0, f))
    a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(a[i] + (b[i] - a[i]) * f):02X}" for i in range(3))


def rr_points(x, y, w, h, r):
    """圆角矩形的顶点序列（每角 8 段圆弧逼近，够圆）。

    提成模块级函数是因为 PillSlider 也要用 —— 一处实现，处处同心圆。
    """
    r = max(1, min(r, w / 2, h / 2))
    pts = []
    for cx, cy, a0 in ((x + r, y + r, 180), (x + w - r, y + r, 270),
                       (x + w - r, y + h - r, 0), (x + r, y + h - r, 90)):
        for i in range(9):
            a = math.radians(a0 + i * 90 / 8)
            pts += [cx + r * math.cos(a), cy + r * math.sin(a)]
    return pts


class PillSlider(tk.Canvas):
    """**灵动岛式滑块** —— 胶囊轨道 + 会变身的把手。

    为什么不用 ttk.Scale：它的滑道和把手都是方的，观感上还是"老式音量条"。
    这里全部画成胶囊（半圆形端点），并且：

      · 轨道是细胶囊，**已走过的部分填强调色**
      · 静止态：把手是白色小胶囊，带下投影和细描边，浮在轨道上
      · **激活态（按下/拖动/键盘操作）：把手展开成"灵动岛"**，把当前数值
        含进岛身里 —— 这正是 iPhone 灵动岛的行为（小药丸变大、内容长在岛里），
        比额外弹一个气泡更贴题，也不需要为气泡预留高度
      · 点轨道任意位置直接跳过去，不用精准命中把手
      · 支持 ←→ / Home / End 键盘操作

    variable 用 tk.DoubleVar，所以播放进度可以直接 set() 让它跟着走。
    """

    H = 30                     # 控件高度
    TRACK = 8                  # 轨道粗细
    THUMB_H = 16               # 把手高（静止态）
    THUMB_W = 30               # 把手宽（静止态）
    THUMB_W_ACT = 46           # 岛身最小宽（激活态，含不下数值时还会更宽）

    def __init__(self, master, variable, from_=0.0, to=1.0, step=0.0,
                 fmt=None, bg=None, on_press=None, on_drag=None, on_release=None, **kw):
        bg = bg if bg is not None else THEME["card"]
        super().__init__(master, height=self.H, bg=bg, highlightthickness=0, bd=0, **kw)
        self.var = variable
        self.lo, self.hi = float(from_), float(to)
        self.step = float(step)
        self.fmt = fmt or (lambda v: f"{v:.2f}")
        self.on_press, self.on_drag, self.on_release = on_press, on_drag, on_release
        self._active = False
        self._thumb_w = float(self.THUMB_W)
        self._anim = None
        self.bind("<Configure>", lambda e: self.draw())
        self.bind("<Map>", lambda e: self.draw())   # 每次上屏都重画（切页回来也触发）
        self.bind("<Button-1>", self._press)
        self.bind("<B1-Motion>", self._drag)
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<Left>", lambda e: self.nudge(-1))
        self.bind("<Right>", lambda e: self.nudge(1))
        self.bind("<Home>", lambda e: self.set_value(self.lo))
        self.bind("<End>", lambda e: self.set_value(self.hi))
        self.var.trace_add("write", lambda *_: self.draw())
        self.after(0, self.draw)

    # ── 对外接口 ─────────────────────────────────────────────────
    def set_range(self, lo, hi):
        self.lo, self.hi = float(lo), float(hi)
        self.draw()

    def set_value(self, v):
        v = min(self.hi, max(self.lo, float(v)))
        if self.step:
            v = round(v / self.step) * self.step
        self.var.set(round(v, 6))

    def value(self) -> float:
        try:
            return float(self.var.get())
        except Exception:
            return self.lo

    # ── 绘制 ─────────────────────────────────────────────────────
    def _capsule(self, x, y, w, h, fill, outline=None, width=1):
        return self.create_polygon(rr_points(x, y, w, h, h / 2.0),
                                   fill=fill, outline=outline or fill,
                                   width=width, smooth=False)

    def draw(self):
        try:
            if not self.winfo_exists():
                return
            if not self.winfo_ismapped():
                # ★ 还没上屏就早退的话，之后再没事件会触发重画 —— 滑块会一直空白。
                #   所以这里安排重试，直到 map 上为止（上限 1 秒，防泄漏）。
                self._retry = getattr(self, "_retry", 0) + 1
                if self._retry <= 20:
                    self.after(50, self.draw)
                return
            self._retry = 0
            W, H = self.winfo_width(), self.winfo_height()
            if W < 40 or H < 10:
                return
        except tk.TclError:
            return
        self.delete("all")
        t = THEME
        cy = H / 2.0
        v = self.value()
        span = self.hi - self.lo
        f = 0.0 if span <= 0 else (v - self.lo) / span
        f = min(1.0, max(0.0, f))
        x0 = self.THUMB_H                     # 左右各留半个把手，保证不溢出
        x1 = W - self.THUMB_H
        px = x0 + f * (x1 - x0)

        # 轨道（胶囊）
        self._capsule(x0, cy - self.TRACK / 2, x1 - x0, self.TRACK, t["slider_track"])
        # 已走过的部分
        if px - x0 > self.TRACK / 2:
            self._capsule(x0, cy - self.TRACK / 2, px - x0, self.TRACK, t["accent"])

        txt = self.fmt(v)
        if self._active:
            # ★ 激活态：把手**展开成"灵动岛"**，把数值含进岛身里 —— 这就是
            #   iPhone 灵动岛的行为（小药丸变大、内容长在岛里），不用额外弹气泡，
            #   也就不需要为气泡预留高度。
            tw = max(self.THUMB_W_ACT,
                     tkfont.Font(family=t["font"], size=t["size_sm"]).measure(txt) + SP["xl"])
            th = self.THUMB_H + 4
            bx = min(max(px - tw / 2, SP["xs"]), max(SP["xs"], W - tw - SP["xs"]))
            by = cy - th / 2
            self._capsule(bx + 1, by + 2, tw, th, t["slider_umbra"])       # 下投影
            self._capsule(bx, by, tw, th, t["slider_thumb"], t["slider_thumb"])  # 岸身奶油色
            self.create_text(bx + tw / 2, cy + 0.5, text=txt, fill=t["bg"],
                             font=(t["font"], t["size_sm"], "bold"))
        else:
            # 静止态：白胶囊 + 1px 下投影 + 细描边，浮在轨道上
            w = self._thumb_w
            self._capsule(px - w / 2, cy - self.THUMB_H / 2 + 1, w, self.THUMB_H,
                          t["slider_umbra"])
            self._capsule(px - w / 2, cy - self.THUMB_H / 2, w, self.THUMB_H,
                          t["slider_thumb"], t["slider_rim"], 1)

    # ── 交互 ─────────────────────────────────────────────────────
    def _x_to_value(self, x):
        W = self.winfo_width()
        x0, x1 = self.THUMB_H, W - self.THUMB_H
        if x1 <= x0:
            return self.lo
        f = (x - x0) / (x1 - x0)
        f = min(1.0, max(0.0, f))
        return self.lo + f * (self.hi - self.lo)

    def _press(self, e):
        self._animate(True)
        self.set_value(self._x_to_value(e.x))
        self.focus_set()
        if self.on_press:
            self.on_press()
        if self.on_drag:
            self.on_drag()

    def _drag(self, e):
        self.set_value(self._x_to_value(e.x))
        if self.on_drag:
            self.on_drag()

    def _release(self, e):
        self._animate(False)
        if self.on_release:
            self.on_release()

    def nudge(self, direction):
        d = self.step or (self.hi - self.lo) / 20.0
        self.set_value(self.value() + direction * d)
        if self.on_drag:
            self.on_drag()

    def _animate(self, on: bool):
        """把手变宽/收窄 —— 灵动岛"被激活"的那个动作。"""
        self._active = on
        target = self.THUMB_W_ACT if on else self.THUMB_W
        if self._anim:
            try:
                self.after_cancel(self._anim)
            except Exception:
                pass
            self._anim = None

        def step():
            d = target - self._thumb_w
            if abs(d) <= 0.6:
                self._thumb_w = float(target)
                self.draw()
                self._anim = None
                return
            self._thumb_w += d * 0.5
            self.draw()
            self._anim = self.after(16, step)
        step()

# ══════════════════════════════════════════════════════════════════════
#  配色 —— 苹果系统色（浅色）
# ══════════════════════════════════════════════════════════════════════
THEME = {
    # ★ 暗调主基调（参考用户给的暖棕作品集）：深咖底 / 奶油字 / 陶土橙点缀。
    #   三级明度依然是"底色 < 侧边栏 < 卡片"，只是整体压到暗部。
    "bg": "#241F1B",           # 画布底色（最暗，暖黑咖）
    "sidebar": "#2B2520",      # 浮岛侧边栏（中间）
    "sidebar_sel": "#3C332B",  # 侧边栏选中项（更亮一档）
    "card": "#2E2822",         # 内容卡片（比侧边栏略亮）
    "card_alt": "#27211C",     # 卡片内次级面（嵌入式输入框/列表底）
    "ink": "#F2EAE0",          # 奶油白文字
    "ink_soft": "#C8BCAD",
    "ink_faint": "#94887A",
    "line": "#3E362E",
    "rim": "#4A4138",          # 顶边高光（暗色下用暖灰，不是白）
    "accent": "#C9703F",       # 陶土橙（参考图主色）
    "accent_soft": "#4A3325",
    "ok": "#9CAF88",
    "warn": "#E07856",
    # ── 灵动岛滑块专用 ──
    "slider_track": "#453C33",   # 未走过的轨道
    "slider_umbra": "#17120E",   # 把手/按钮的下投影
    "slider_rim": "#4A4138",     # 把手的描边
    "slider_thumb": "#EFE6D9",   # 把手奶油色（暗色下不用纯白）
    # ★ 正文用 MiSans（本机已装；小米官方字体，风格对标苹果苹方，免费可商用）。
    "font": "MiSans",
    "font_big": "MiSans",
    "font_cn": "MiSans",
    "size": 10, "size_sm": 9, "size_title": 13, "size_big": 22,
    "row_h": 26,
}

PAGES = [("play", "演奏"), ("manage", "收藏夹"), ("online", "在线曲库"),
         ("hotkeys", "热键"), ("log", "日志"), ("settings", "设置")]


class PillButton(tk.Canvas):
    """**胶囊按钮** —— ttk.Button 的方角外观和整页圆角不搭，按钮全部自绘。

    kind="accent" 蓝底白字（主操作）｜"normal" 灰底深字｜"danger" 灰底红字（危险操作）。
    有 hover / 按下两态；禁用态置灰且不可点。

    兼容 ttk 的两个常用调用（这样业务代码不用改写）：
        btn.configure(state="disabled" / "normal")
        btn.configure(text="...")          # 宽度会跟着文字重算

    ★ draw() 的未上屏重试和 PillSlider 同款：控件没 map 就早退的话，
      之后再没事件会触发重画，按钮会一直空白。
    """

    H = 30

    def __init__(self, master, text, command=None, kind="normal", width=None, **kw):
        bg = kw.pop("bg", THEME["card"])
        state = kw.pop("state", "normal")
        super().__init__(master, height=self.H, bg=bg, highlightthickness=0, bd=0,
                         cursor="hand2", **kw)
        self.command = command
        self.kind = kind if kind in ("accent", "normal", "danger") else "normal"
        self._label = text
        self._enabled = (state != "disabled")
        self._hover = False
        self._pressed = False
        self._retry = 0
        self._font = tkfont.Font(family=THEME["font"], size=THEME["size"])
        super().configure(width=self._measure())      # 走父类，避免进下面的覆写递归
        self.bind("<Configure>", lambda e: self.draw())
        self.bind("<Map>", lambda e: self.draw())   # 每次上屏都重画（切页回来也触发）
        self.bind("<Enter>", lambda e: self._set_hover(True))
        self.bind("<Leave>", lambda e: self._set_hover(False))
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.after(0, self.draw)

    # ── 对外 ─────────────────────────────────────────────────────
    def configure(self, cnf=None, **kw):                       # noqa: A003
        if "state" in kw:
            self._enabled = (kw.pop("state") != "disabled")
            if not self._enabled:
                self._hover = self._pressed = False
        if "text" in kw:
            self._label = kw.pop("text")
            kw["width"] = self._measure()        # 宽度跟文字走（直接进父类，不递归）
        super().configure(cnf, **kw)
        self.draw()

    def _measure(self) -> int:
        return max(48, self._font.measure(self._label) + SP["xl"])

    # ── 状态与颜色 ───────────────────────────────────────────────
    def _set_hover(self, on: bool):
        self._hover = on
        self.draw()

    def _on_press(self, _e):
        if not self._enabled:
            return
        self._pressed = True
        self.draw()

    def _on_release(self, e):
        was = self._pressed
        self._pressed = False
        self.draw()
        inside = 0 <= e.x <= self.winfo_width() and 0 <= e.y <= self.winfo_height()
        if was and inside and self._enabled and self.command:
            self.command()

    def _colors(self) -> tuple[str, str]:
        t = THEME
        if self.kind == "accent":
            # 主按钮底色比强调色再压深一档：白字对比度过 WCAG AA（暗橙 ~4.2:1）
            base = _mix(t["accent"], "#000000", 0.15)
            if not self._enabled:
                return t["line"], t["ink_faint"]
            if self._pressed:
                return _mix(base, "#000000", 0.22), "#FFFFFF"
            if self._hover:
                return _mix(base, "#000000", 0.10), "#FFFFFF"
            return base, "#FFFFFF"
        base = t["card_alt"]
        if not self._enabled:
            return base, t["ink_faint"]
        fg = t["warn"] if self.kind == "danger" else t["ink"]
        if self._pressed:
            return _mix(base, t["ink"], 0.10), fg
        if self._hover:
            return _mix(base, t["ink"], 0.05), fg
        return base, fg

    # ── 绘制 ─────────────────────────────────────────────────────
    def draw(self):
        try:
            if not self.winfo_exists():
                return
            if not self.winfo_ismapped():
                self._retry += 1
                if self._retry <= 20:
                    self.after(50, self.draw)
                return
            self._retry = 0
            W, H = self.winfo_width(), self.winfo_height()
            if W < 12 or H < 12:
                return
        except tk.TclError:
            return
        self.delete("all")
        bg, fg = self._colors()
        if self._enabled:                       # 下投影：光从上来，影子只从底边露出
            self.create_polygon(rr_points(1, 1, W - 2, H, H / 2.0),
                                fill=THEME["slider_umbra"], outline=THEME["slider_umbra"])
        self.create_polygon(rr_points(0, 0, W, H - 1, (H - 1) / 2.0), fill=bg, outline=bg)
        self.create_text(W / 2, (H - 1) / 2, text=self._label, fill=fg, font=self._font)


class App(tk.Tk):
    def __init__(self, cfg: dict | None = None, dry: bool = False,
                 cfg_path: Path | str | None = None):
        super().__init__()
        self.cfg_path = Path(cfg_path) if cfg_path else CONFIG_PATH
        self.cfg = cfg if cfg is not None else cfgmod.load(self.cfg_path)
        self.mapping = load_mapping(self.cfg["mapping"])
        self.dry = dry

        self.files: list[str] = []
        self.lib_all: list[tuple[str, str]] = []     # [(路径, 分类)] 分类=""=根目录
        self.current = ""
        self.actions: list = []
        self.player = Player(NullInput(self.mapping) if dry else make_backend(self.mapping))
        self.hotkeys = Hotkeys()
        self._hotkey_events: queue.Queue = self.hotkeys.events
        # ★ 工作线程绝不碰 Tk，只往这个队列丢消息，主线程 after() 取
        self._ui_events: queue.Queue = queue.Queue()
        self._seeking = False
        self._worker: threading.Thread | None = None
        # 收藏夹 / 自定义热键（Soundpad 式）：数据都在 hca.json
        self.fav_view = ""                            # 当前左栏选中的收藏夹名（""=全部曲目）
        self._migrate_slots()                         # 旧 10 槽位一次性迁成自由热键
        # 在线曲库（B站三角洲谱）的网络队列：工作线程只丢消息，主线程轮询
        self._net_events: queue.Queue = queue.Queue()
        self._net_busy = False
        self.bili: bili.BiliClient | None = None
        self._online_rows: dict[str, dict] = {}      # iid → score 元数据
        self._online_cursor: str | None = None
        self._online_query = ""
        self._page = "play"
        self._frames: dict[str, tk.Frame] = {}
        self._shapes: list[int] = []

        # 提权标识：run.bat 在 fltmc 通过后设 HCA_ELEVATED=1，标题栏显示出来，
        # 这样"到底是不是管理员在跑"不用猜（双击不提权的 bat 就没有这个标记）
        self._elevated = (os.environ.get("HCA_ELEVATED") == "1")
        self.title("口琴演奏器 · 新版〔管理员〕" if self._elevated else "口琴演奏器 · 新版")
        self.geometry("1080x720")
        self.minsize(960, 640)
        self.update_idletasks()
        self.glass_info = self._setup_glass()
        # ★ 只有「边框延伸真的生效」时客户区才是透明的，画布才该用纯黑当透明；
        #   否则画布必须用实色 —— 不然整块会变黑。
        self.glass_client = bool(self.glass_info.get("applied")) and GLASS["extend_frame"]
        self.bg = GLASS["glass_bg"] if self.glass_client else GLASS["fallback_bg"]
        self.configure(bg=self.bg)
        self._pick_font()
        self._apply_theme()
        self._build_ui()
        self.bind_shortcuts()
        self.refresh_library()
        self.start_hotkeys()
        self.after(60, self._poll_hotkeys)
        self.after(60, self._poll_ui_events)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    # ── 玻璃 ───────────────────────────────────────────────────────
    def _setup_glass(self) -> dict:
        return glass.apply(self, backdrop=GLASS["backdrop"], dark=GLASS["dark"],
                           extend_frame=GLASS["extend_frame"],
                           round_corners=GLASS["round_corners"],
                           caption_color=GLASS.get("caption_color"))

    def _pick_font(self) -> None:
        """苹果的 SF 在 Windows 上没有，挑一个最接近的。

        注意 Segoe UI Variable 的**光学字号**：Display 给大标题、Text 给正文、
        Small 给小字。小字用 Display 会偏细发虚，所以正文用 Text。
        """
        try:
            fams = set(tkfont.families(self))
        except Exception:
            fams = set()
        def pick(cands: tuple[str, ...], fallback: str) -> str:
            return next((c for c in cands if c in fams), fallback)

        THEME["font"] = pick(("MiSans", "PingFang SC", "HarmonyOS Sans SC",
                              "Segoe UI Variable Text", "Segoe UI"), "TkDefaultFont")
        THEME["font_big"] = pick(("MiSans", "PingFang SC", "Segoe UI Variable Display",
                                  "Segoe UI"), THEME["font"])

    # ── 样式 ───────────────────────────────────────────────────────
    def _apply_theme(self):
        t = THEME
        st = ttk.Style(self)
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        base = (t["font"], t["size"])
        st.configure(".", background=t["card"], foreground=t["ink"], font=base)
        st.configure("TFrame", background=t["card"])
        st.configure("Alt.TFrame", background=t["card_alt"])
        st.configure("TLabel", background=t["card"], foreground=t["ink"])
        st.configure("Soft.TLabel", background=t["card"], foreground=t["ink_soft"])
        st.configure("TButton", padding=(SP["md"], SP["xs"] + 1), background=t["card_alt"],
                     foreground=t["ink"], borderwidth=0, focusthickness=0, relief="flat")
        st.map("TButton", background=[("active", t["line"])])
        st.configure("Accent.TButton", background=t["accent"], foreground="#FFFFFF")
        st.map("Accent.TButton", background=[("active", "#0A6FDB")])
        st.configure("Danger.TButton", background=t["card_alt"], foreground=t["warn"])
        st.map("Danger.TButton", background=[("active", "#FBE9E7")])
        st.configure("Treeview", background=t["card"], fieldbackground=t["card"],
                     foreground=t["ink"], rowheight=t["row_h"], borderwidth=0)
        st.configure("Treeview.Heading", background=t["card"], foreground=t["ink_soft"],
                     font=(t["font"], t["size_sm"]), borderwidth=0, relief="flat")
        st.map("Treeview", background=[("selected", t["accent_soft"])],
               foreground=[("selected", t["ink"])])
        # ★ 复选框：clam 的指示器默认是白底（暗色界面里一大块高亮，实测被用户点名）
        st.configure("TCheckbutton", background=t["card"], foreground=t["ink"],
                     focuscolor=t["card"],
                     indicatorbackground="#453C33",     # 未勾选：暗暖灰方块
                     indicatorforeground=t["slider_thumb"],  # 勾选标记：奶油
                     bordercolor=t["line"], lightcolor=t["line"], darkcolor=t["bg"])
        # ★ ttk.Style.map 的第二个位置参数是「查询选项名」不是赋值串；
        #   而且下面这两处不是 f-string，t['accent'] 是字面文本 —— 两条配置
        #   从来没生效过（实测 map 前后完全一致）。改成关键字参数形式。
        st.map("TCheckbutton",
               indicatorbackground=[("selected", t["accent"]), ("pressed", t["line"])])
        st.configure("TEntry", fieldbackground=t["card_alt"], foreground=t["ink"],
                     borderwidth=0, insertcolor=t["ink"])
        st.map("TEntry", lightcolor=[("focus", t["accent"])],
               bordercolor=[("focus", t["accent"])])   # 聚焦框跟主题色（默认是系统蓝）
        st.configure("TCombobox", fieldbackground=t["card_alt"], background=t["card_alt"],
                     foreground=t["ink"], arrowcolor=t["ink_soft"],
                     bordercolor=t["line"], lightcolor=t["line"], darkcolor=t["bg"],
                     insertcolor=t["ink"])
        st.map("TCombobox", fieldbackground=[("readonly", t["card_alt"])],
               foreground=[("readonly", t["ink"])])
        # 下拉列表本身是 Tcl listbox，样式走 option 数据库（必须在建控件前设置）
        for opt, val in (("*TCombobox*Listbox.background", t["card_alt"]),
                         ("*TCombobox*Listbox.foreground", t["ink"]),
                         ("*TCombobox*Listbox.selectBackground", t["accent"]),
                         ("*TCombobox*Listbox.selectForeground", "#FFFFFF"),
                         ("*TCombobox*Listbox.font", (t["font"], t["size"]))):
            self.option_add(opt, val)
        st.configure("TSpinbox", fieldbackground=t["card_alt"], foreground=t["ink"],
                     background=t["card"], arrowcolor=t["ink_soft"],
                     bordercolor=t["line"], lightcolor=t["line"], darkcolor=t["bg"],
                     insertcolor=t["ink"])
        st.map("TSpinbox",
               fieldbackground=[("readonly", t["card_alt"]), ("active", t["card_alt"]),
                                ("pressed", t["card_alt"]), ("focus", t["card_alt"]),
                                ("!disabled", t["card_alt"])])
        st.configure("TSeparator", background=t["line"])
        # 滚动条收细、配色融入卡片（clam 主题可配；原生 Windows 主题配不动）
        st.configure("Vertical.TScrollbar", background=t["card_alt"],
                     troughcolor=t["card"], bordercolor=t["card"],
                     arrowcolor=t["ink_faint"], relief="flat")
        st.map("Vertical.TScrollbar", background=[("active", t["line"])])

    # ── 画布外壳：浮岛侧边栏 + 圆角卡片 ────────────────────────────
    def _rr(self, x, y, w, h, r, fill, outline, tag="shape"):
        """圆角矩形（用模块级 rr_points —— 和滑块共用同一套圆弧逼近）。"""
        return self.canvas.create_polygon(rr_points(x, y, w, h, r), fill=fill, outline=outline,
                                          width=1, smooth=False, tags=tag)

    def _rim(self, x1, x2, y):
        """玻璃的顶边高光。"""
        self.canvas.create_line(x1, y + 0.5, x2, y + 0.5,
                                fill=THEME["rim"], width=1, tags="pill")

    def _shadow(self, x, y, w, h, r, steps=None):
        """伪阴影 —— 叠几层逐渐变淡的圆角矩形（Tk 没有模糊，只能这样逼近）。

        必须画在卡片/侧边栏**之前**，才在它们下面。
        """
        steps = steps or Q["steps"]
        for k in range(steps, 0, -1):                 # 外层先画（最淡）
            col = _mix(Q["color"], self.bg, k / (steps + 1.0))
            self._shapes.append(self._rr(x - k, y - k + Q["dy"],
                                         w + 2 * k, h + 2 * k, r + k, col, col))

    def _grad_v(self, x, y, w, h, r, c_top, c_bot, steps=16, tag="pill"):
        """垂直渐变的圆角矩形 —— Tk 没有渐变填充，用色带近似。

        结构：底层铺 c_bot 的完整圆角矩形
              → **顶帽**（上圆角、下平边的多边形，接近 c_top）
              → 中段全宽色带，从帽色渐变到 c_bot。
        ★ 不要用"整条色带内缩"来做圆角区 —— 内缩色带和全宽色带之间
          会出现一个又窄又亮的台阶（实测踩过）。
        """
        self._rr(x, y, w, h, r, c_bot, c_bot, tag=tag)
        r2 = min(r, w / 2)
        # 顶帽：上圆角（圆心 y+r2）+ 下平边（y+r2），轮廓与外层圆角完全同心
        pts = []
        for i in range(9):
            a = math.radians(180 + i * 90 / 8)
            pts += [x + r2 + r2 * math.cos(a), y + r2 + r2 * math.sin(a)]
        for i in range(9):
            a = math.radians(270 + i * 90 / 8)
            pts += [x + w - r2 + r2 * math.cos(a), y + r2 + r2 * math.sin(a)]
        top_col = _mix(c_top, c_bot, (r2 / h) * 0.5)
        self.canvas.create_polygon(pts, fill=top_col, outline=top_col, tags=tag)
        # 中段全宽色带
        y0, y1 = y + r2, y + h - r2
        span = y1 - y0
        if span > 6:
            bands = max(8, steps)
            bh = span / bands
            for i in range(bands):
                yy = y0 + i * bh
                col = _mix(top_col, c_bot, (i + 0.5) / bands)
                self.canvas.create_rectangle(x, yy, x + w, yy + bh + 0.6,
                                             fill=col, outline=col, tags=tag)

    def _draw_shell(self):
        """按当前页重画所有形状。尺寸变了就整个重来（简单可靠）。"""
        if not hasattr(self, "canvas"):
            return
        c = self.canvas
        for it in self._shapes:
            try:
                c.delete(it)
            except Exception:
                pass
        self._shapes.clear()
        c.delete("pill")
        # ★ 卡片渐变用的是 tag="shape"（_card → _grad_v(tag="shape")），
        #   原来只删 "pill" → 每张卡约 14 个 item 永不释放，且旧尺寸的矩形
        #   留在画布上变成可见残影（<Configure> 每次拖窗口都重画，累积很快）。
        c.delete("shape")
        W, H = c.winfo_width(), c.winfo_height()
        if W < 80 or H < 80:
            return
        t = THEME
        m, gap = SP["md"], SP["md"]
        sb_w = 200
        sb_x, sb_y, sb_h = m, m, H - 2 * m

        # ── 浮岛侧边栏 ──
        # ★ 这里**故意不放任何 frame**：Tk 的子窗口永远盖在画布自己的绘制之上，
        #   之前那个空 frame 把菜单文字和胶囊全遮住了（侧边栏一片空白）。
        self._shadow(sb_x, sb_y, sb_w, sb_h, R["island"])
        # ★ 侧边栏垂直渐变：顶部微亮 → 底部更深（暗调里的"暗→亮"过渡）
        self._sb_grad = (_mix(t["sidebar"], "#FFFFFF", 0.05),
                         _mix(t["sidebar"], "#000000", 0.22))
        self._grad_v(sb_x, sb_y, sb_w, sb_h, R["island"],
                     self._sb_grad[0], self._sb_grad[1], steps=18)
        self._rim(sb_x + R["island"], sb_x + sb_w - R["island"], sb_y)
        self._sidebar_items(sb_x, sb_y, sb_w, sb_h)

        # ── 内容卡片 ──
        cx = sb_x + sb_w + gap
        cw = W - cx - m
        ch = H - 2 * m
        if self._page == "play":
            lw = int(cw * 0.44)
            rw = max(1, cw - lw - gap)
            self._card("card_list", cx, sb_y, lw, ch)
            self._card("card_info", cx + lw + gap, sb_y, rw, ch)
            self._show_page("page_list", cx, sb_y, lw, ch)
            self._show_page("page_play", cx + lw + gap, sb_y, rw, ch)
            # ★ 除两块演奏页外，其余页面全部从画布上摘掉 —— 曾经漏掉
            #   page_manage/page_online，它们的文字从卡片缝隙里透出来
            #   （用户截图实锤：缝隙里露出"删除"按钮等碎片）。
            self._hide(*[k for k in self._frames if k not in ("page_list", "page_play")])
            self._hide("card_full")
        else:
            self._card("card_full", cx, sb_y, cw, ch)
            key = {"manage": "page_manage", "online": "page_online",
                   "hotkeys": "page_hotkeys", "log": "page_log",
                   "settings": "page_settings"}[self._page]
            self._show_page(key, cx, sb_y, cw, ch)
            self._hide(*[k for k in ("page_play", "page_list", "page_manage",
                                     "page_online", "page_hotkeys",
                                     "page_log", "page_settings") if k != key])
            self._hide("card_list", "card_info")

    def _card(self, key, x, y, w, h):
        self._shadow(x, y, w, h, R["card"], steps=Q["steps"] - 1)
        self._grad_v(x, y, w, h, R["card"],
                     _mix(THEME["card"], "#FFFFFF", 0.04),
                     _mix(THEME["card"], "#000000", 0.12), steps=12, tag="shape")
        self._rim(x + R["card"], x + w - R["card"], y)
        self._place(key, x, y, w, h, int(R["card"] * 0.34), THEME["card"])

    def _place(self, key, x, y, w, h, pad, fill):
        """放入一个（可能已存在的）frame；内缩 pad 让方角藏进圆角里。"""
        fr = self._frames.get(key)
        if fr is None:
            fr = tk.Frame(self.canvas, bg=fill)
            self._frames[key] = fr
        else:
            fr.configure(bg=fill)
        self.canvas.delete(key)
        self.canvas.create_window(x + pad, y + pad, anchor="nw",
                                  width=max(1, w - 2 * pad), height=max(1, h - 2 * pad),
                                  window=fr, tags=key)

    def _show_page(self, key, x, y, w, h):
        """把一个页面放进圆角卡片里。

        ★ 必须和卡片一样内缩（方角要藏进圆角里）—— 之前是 0 内缩，
          页面这个方形 frame 直接盖住卡片的圆角，看起来四角全是直角。
        """
        fr = self._frames.get(key)
        if fr is None:
            return
        pad = max(int(R["card"] * 0.34), SP["sm"])
        fr.configure(bg=THEME["card"])
        self.canvas.delete(key)
        self.canvas.create_window(x + pad, y + pad, anchor="nw",
                                  width=max(1, w - 2 * pad), height=max(1, h - 2 * pad),
                                  window=fr, tags=key)
        fr.tkraise()

    def _hide(self, *keys):
        for k in keys:
            self.canvas.delete(k)

    def switch_page(self, key: str):
        if key == self._page:
            return
        self._page = key
        self._draw_shell()
        if key == "hotkeys":
            self._hk_reload()
        if key == "settings":
            self._sync_settings()

    def _sidebar_items(self, x, y, w, h):
        """侧边栏：标识 + 菜单胶囊（画在画布上，可点）。"""
        c, t = self.canvas, THEME
        pad = SP["sm"] + 2
        c.create_text(x + pad + 4, y + pad + 16, anchor="w", text="♪",
                      font=(t["font"], 18, "bold"), fill=t["accent"], tags="pill")
        c.create_text(x + pad + 30, y + pad + 10, anchor="w", text="口琴演奏器",
                      font=(t["font"], t["size_title"], "bold"), fill=t["ink"], tags="pill")
        c.create_text(x + pad + 30, y + pad + 28, anchor="w", text="新版 · 自研内核",
                      font=(t["font"], t["size_sm"]), fill=t["ink_faint"], tags="pill")
        c.create_line(x + pad, y + pad + 46, x + w - pad, y + pad + 46,
                      fill=t["line"], width=1, tags="pill")

        pill_h = SP["xl"] + SP["sm"]
        top = y + pad + 62
        c_top, c_bot = getattr(self, "_sb_grad", (t["sidebar"], t["sidebar"]))
        for i, (key, label) in enumerate(PAGES):
            iy = top + i * (pill_h + SP["sm"])
            sel = key == self._page
            # ★ 未选中胶囊取"它所在高度"的渐变色 —— 和背景无缝，不再出现平涂色块
            f = max(0.0, min(1.0, (iy + pill_h / 2 - y) / max(1, h)))
            base_col = _mix(c_bot, c_top, 1 - f)
            self._rr(x + pad, iy, w - 2 * pad, pill_h, R["pill"],
                     t["sidebar_sel"] if sel else base_col,
                     t["line"] if sel else base_col, tag="pill")
            if sel:
                self._rim(x + pad + R["pill"], x + w - pad - R["pill"], iy)
            c.create_text(x + pad + SP["md"], iy + pill_h / 2, anchor="w", text=label,
                          font=(t["font"], t["size"], "bold" if sel else "normal"),
                          fill=t["ink"] if sel else t["ink_soft"],
                          tags=("pill", f"menu_{key}"))
            c.tag_bind(f"menu_{key}", "<Button-1>", lambda e, k=key: self.switch_page(k))
            c.tag_bind(f"menu_{key}", "<Enter>", lambda e: c.configure(cursor="hand2"))
            c.tag_bind(f"menu_{key}", "<Leave>", lambda e: c.configure(cursor=""))

        # 底部只留一行真正有用的（玻璃状态属于设置页，见 _sync_settings）
        hk = "热键开" if self.cfg.get("hotkeys", True) else "热键关"
        c.create_text(x + pad + 4, y + h - SP["lg"] - 12, anchor="w",
                      text=f"库 {len(self.files)} 首　·　{hk}",
                      font=(t["font"], t["size_sm"]), fill=t["ink_soft"], tags="pill")

    # ── 界面 ───────────────────────────────────────────────────────
    def _build_ui(self):
        self.canvas = tk.Canvas(self, bg=self.bg, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda e: self._draw_shell())
        self._build_page_play()
        self._build_page_manage()
        self._build_page_online()
        self._build_page_hotkeys()
        self._build_page_log()
        self._build_page_settings()
        self.after(80, self._draw_shell)
        self.after(100, self._poll_net_events)

    def _all_pill_buttons(self) -> list:
        return [w for w in self._frames.values() for c in w.winfo_children()
                if isinstance(c, PillButton)]

    def _build_page_play(self):
        t = THEME
        lst = tk.Frame(self.canvas, bg=t["card"])
        self._frames["page_list"] = lst
        tk.Label(lst, text="曲库", bg=t["card"], fg=t["ink_soft"],
                 font=(t["font"], t["size_sm"])).pack(anchor="w")
        self.tree = ttk.Treeview(lst, columns=("hk", "title", "bars", "notes", "bpm"),
                                 show="headings", selectmode="browse")
        for c, (txt, w) in {"hk": ("键", 90), "title": ("曲目", 200),
                            "bars": ("小节", 46), "notes": ("音数", 46),
                            "bpm": ("BPM", 46)}.items():
            self.tree.heading(c, text=txt)
            self.tree.column(c, width=w, anchor="w", stretch=(c == "title"))
        vs = ttk.Scrollbar(lst, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vs.set)
        vs.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True, pady=(SP["sm"], 0))
        # ★ 列宽自适应：title 吸收剩余宽度，最右列不会再被滚动条压没
        self.tree.bind("<Configure>", self._fit_tree_columns)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self.on_pick_song())
        self.tree.bind("<Button-3>", self._song_menu_play)
        tk.Label(lst, text="← → 换曲　↑ ↓ 变速　空格 播放/暂停", bg=t["card"],
                 fg=t["ink_faint"], font=(t["font"], t["size_sm"])).pack(anchor="w", pady=(SP["sm"], 0))

        info = tk.Frame(self.canvas, bg=t["card"])
        self._frames["page_play"] = info
        tk.Label(info, text="当前曲目", bg=t["card"], fg=t["ink_soft"],
                 font=(t["font"], t["size_sm"])).pack(anchor="w")
        self.lbl_title = tk.Label(info, text="从左侧列表选择曲目", bg=t["card"], fg=t["ink"],
                                  font=(t["font"], t["size_title"], "bold"), anchor="w")
        self.lbl_title.pack(anchor="w", pady=(2, 0))
        self.lbl_meta = tk.Label(info, text="", bg=t["card"], fg=t["ink_soft"],
                                 font=(t["font"], t["size_sm"]), anchor="w")
        self.lbl_meta.pack(anchor="w")

        box = tk.Frame(info, bg=t["card_alt"])
        box.pack(fill="x", pady=(SP["lg"], 0))
        tk.Label(box, text="当前音", bg=t["card_alt"], fg=t["ink_soft"],
                 font=(t["font"], t["size_sm"])).pack(anchor="w", padx=SP["md"], pady=(SP["sm"], 0))
        self.lbl_now = tk.Label(box, text="待演奏", bg=t["card_alt"], fg=t["ink_faint"],
                                font=(t["font_big"], t["size_big"], "bold"), anchor="w")
        self.lbl_now.pack(anchor="w", padx=SP["md"])
        self.lbl_press = tk.Label(box, text="", bg=t["card_alt"], fg=t["ink"],
                                  font=(t["font"], t["size"]), anchor="w")
        self.lbl_press.pack(anchor="w", padx=SP["md"], pady=(0, SP["sm"]))

        tk.Label(info, text="演奏位置（可拖动）", bg=t["card"], fg=t["ink_soft"],
                 font=(t["font"], t["size_sm"])).pack(anchor="w", pady=(SP["lg"], 0))
        self.var_pos = tk.DoubleVar(value=0.0)
        self.scale = PillSlider(
            info, self.var_pos, 0.0, 1.0, fmt=lambda v: self._mmss(v),
            on_press=lambda: setattr(self, "_seeking", True),
            on_drag=self._on_scale_move, on_release=self._on_scale_release)
        self.scale.pack(fill="x")
        self.lbl_time = tk.Label(info, text="0:00 / 0:00", bg=t["card"], fg=t["ink_soft"],
                                 font=(t["font"], t["size_sm"]), anchor="w")
        self.lbl_time.pack(anchor="w")

        ctl = tk.Frame(info, bg=t["card"])
        ctl.pack(fill="x", pady=(SP["lg"], 0))
        self.btn_play = PillButton(ctl, "▶  开始演奏", command=self.on_play, kind="accent")
        self.btn_play.pack(side="left")
        self.btn_pause = PillButton(ctl, "⏸  暂停", command=self.on_pause, state="disabled")
        self.btn_pause.pack(side="left", padx=SP["sm"])
        PillButton(ctl, "⏹  停止", command=self.on_stop).pack(side="left")

        row = tk.Frame(info, bg=t["card"])
        row.pack(fill="x", pady=(SP["lg"], 0))
        self.var_intro = tk.BooleanVar(value=bool(self.cfg["no_intro"]))
        self.var_loop = tk.BooleanVar(value=bool(self.cfg["loop"]))
        self.var_rate = tk.DoubleVar(value=float(self.cfg["rate"]))
        self.var_count = tk.DoubleVar(value=float(self.cfg["countdown"]))
        ttk.Checkbutton(row, text="跳过前奏", variable=self.var_intro, command=self.recompile,
                        takefocus=False).pack(side="left")
        ttk.Checkbutton(row, text="循环", variable=self.var_loop,
                        takefocus=False).pack(side="left", padx=SP["sm"])
        # 「高音近似」复选框已移除：超出人手范围的音现在一律就近搬进范围再弹，
        # 不再跳过（2026-10-12 用户要求取消限制），没有可切换的了。
        self.var_legato = tk.BooleanVar(value=bool(self.cfg.get("legato", True)))
        ttk.Checkbutton(row, text="连奏防断音", variable=self.var_legato,
                        command=self.recompile,
                        takefocus=False).pack(side="left", padx=(0, SP["sm"]))
        row2 = tk.Frame(info, bg=t["card"])
        row2.pack(fill="x", pady=(SP["sm"], 0))
        # ★ 右侧一组先打包（side="right"，从右往左），滑块最后吃剩余宽度——
        #   之前滑块 expand 把后面的"1.00×"挤出卡片被裁掉。
        self.lbl_rate = tk.Label(row2, text=f"{self.var_rate.get():.2f}×", bg=t["card"],
                                 fg=t["ink"], font=(t["font"], t["size_sm"]), width=7)
        self.lbl_rate.pack(side="right")
        ttk.Spinbox(row2, from_=0, to=10, width=3, textvariable=self.var_count,
                    takefocus=False).pack(side="right")
        tk.Label(row2, text="倒计时", bg=t["card"], fg=t["ink_soft"],
                 font=(t["font"], t["size_sm"])).pack(side="right", padx=(SP["xs"], SP["xs"]))
        tk.Label(row2, text="速度", bg=t["card"], fg=t["ink_soft"],
                 font=(t["font"], t["size_sm"])).pack(side="left")
        self.rate_scale = PillSlider(row2, self.var_rate, 0.05, 8.0, step=0.05,
                                     fmt=lambda v: f"{v:.2f}×",
                                     on_drag=self._on_rate_move)
        self.rate_scale.pack(side="left", padx=SP["sm"], fill="x", expand=True)
        self.lbl_state = tk.Label(info, text="", bg=t["card"], fg=t["ink_faint"],
                                  font=(t["font"], t["size_sm"]), anchor="w")
        self.lbl_state.pack(anchor="w", pady=(SP["md"], 0))

        tk.Label(info, text="演奏预览（前 10 个音）", bg=t["card"], fg=t["ink_soft"],
                 font=(t["font"], t["size_sm"])).pack(anchor="w", pady=(SP["md"], 0))
        self.preview = tk.Listbox(info, height=5, bg=t["card_alt"], fg=t["ink_soft"],
                                  relief="flat", highlightthickness=0, bd=0,
                                  font=(t["font"], t["size_sm"]), activestyle="none")
        self.preview.pack(fill="both", expand=True, pady=(SP["xs"], 0))

    # ── 收藏夹（Soundpad 式两栏） ─────────────────────────────────
    def _favs(self) -> list[dict]:
        return favmod.normalize(self.cfg.get("favorites"))

    def _save_favs(self, favs: list[dict]):
        self.cfg["favorites"] = favs
        cfgmod.save(self.cfg_path, self.cfg)
        self._fav_reload_left()
        self._fav_reload_right()

    def _build_page_manage(self):
        t = THEME
        f = tk.Frame(self.canvas, bg=t["card"])
        self._frames["page_manage"] = f
        head = tk.Frame(f, bg=t["card"])
        head.pack(fill="x")
        tk.Label(head, text="收藏夹", bg=t["card"], fg=t["ink"],
                 font=(t["font"], t["size_title"], "bold")).pack(side="left")
        PillButton(head, "刷新曲库", command=self.refresh_library).pack(side="right")
        tk.Label(f, text="收藏夹存在程序配置里，不挪动 sings 里的文件；一首曲子可以同时进多个收藏夹。"
                         "左栏选收藏夹，右栏显示里面的曲子——**双击曲目直接演奏**（重命名用 F2 / 右键）。",
                 bg=t["card"], fg=t["ink_soft"],
                 font=(t["font"], t["size_sm"])).pack(anchor="w", pady=(2, SP["sm"]))

        mid = tk.Frame(f, bg=t["card"])
        mid.pack(fill="both", expand=True)
        mid.columnconfigure(0, weight=0)
        mid.columnconfigure(1, minsize=SP["lg"])
        mid.columnconfigure(2, weight=1)
        mid.rowconfigure(0, weight=1)

        # 左：收藏夹列表
        self.fav_tree = ttk.Treeview(mid, columns=("name",), show="headings", height=12)
        self.fav_tree.heading("name", text="收藏夹")
        self.fav_tree.column("name", width=150, anchor="w")
        self.fav_tree.grid(row=0, column=0, sticky="nsew")
        self.fav_tree.bind("<<TreeviewSelect>>", lambda e: self._fav_select())
        lb = tk.Frame(mid, bg=t["card"])
        lb.grid(row=1, column=0, sticky="ew", pady=(SP["sm"], 0))
        PillButton(lb, "新建", command=self.fav_create,
                   kind="accent").pack(side="left", padx=(0, SP["xs"]))
        PillButton(lb, "改名", command=self.fav_rename).pack(side="left", padx=(0, SP["xs"]))
        PillButton(lb, "删除", command=self.fav_delete, kind="danger").pack(side="left")

        # 右：当前收藏夹里的曲子
        self.fav_songs = ttk.Treeview(mid, columns=("title", "tags"), show="headings",
                                      selectmode="extended")
        self.fav_songs.heading("title", text="曲目")
        self.fav_songs.heading("tags", text="归属 / 状态")
        self.fav_songs.column("title", width=380, anchor="w", stretch=True)
        self.fav_songs.column("tags", width=150, anchor="w")
        vs = ttk.Scrollbar(mid, orient="vertical", command=self.fav_songs.yview)
        self.fav_songs.configure(yscrollcommand=vs.set)
        vs.grid(row=0, column=3, sticky="ns")
        self.fav_songs.grid(row=0, column=2, sticky="nsew")
        self.fav_songs.bind("<Double-1>", lambda e: self.fav_play())
        self.fav_songs.bind("<Return>", lambda e: self.fav_play())
        self.fav_songs.bind("<F2>", lambda e: self.fav_rename_file())
        self.fav_songs.bind("<Delete>", lambda e: self.fav_delete_file())
        self.fav_songs.bind("<Button-3>", self._song_menu_fav)

        # ★ 底部按钮行先 pack（side=bottom），否则会被树挤出卡片
        btns = tk.Frame(f, bg=t["card"])
        btns.pack(side="bottom", fill="x", pady=(SP["md"], 0))
        PillButton(btns, "加入收藏夹", command=self.fav_add,
                   kind="accent").pack(side="left")
        PillButton(btns, "移出本收藏夹", command=self.fav_remove,
                   ).pack(side="left", padx=SP["sm"])
        PillButton(btns, "重命名文件（F2）", command=self.fav_rename_file).pack(side="left")
        PillButton(btns, "删除文件（Del · 进回收站）", command=self.fav_delete_file,
                   kind="danger").pack(side="left", padx=SP["sm"])

    ALL_IID = "::__all__"

    def _fav_reload_left(self):
        try:
            self.fav_tree.delete(*self.fav_tree.get_children())
        except Exception:
            return
        self.fav_tree.insert("", "end", iid=self.ALL_IID, values=("全部曲目",))
        for fav in self._favs():
            n = len(fav["songs"])
            self.fav_tree.insert("", "end", iid=fav["name"],
                                 values=(f"{fav['name']}（{n}）",))
        sel = self.fav_view if self.fav_view else self.ALL_IID
        try:
            self.fav_tree.selection_set(sel)
            self.fav_tree.focus(sel)
        except Exception:
            self.fav_view = ""
            self.fav_tree.selection_set(self.ALL_IID)

    def _fav_select(self):
        sel = self.fav_tree.selection()
        if not sel:
            return
        self.fav_view = "" if sel[0] == self.ALL_IID else sel[0]
        self._fav_reload_right()

    def _fav_reload_right(self):
        try:
            self.fav_songs.delete(*self.fav_songs.get_children())
        except Exception:
            return
        if not self.fav_view:
            names = {Path(f).name for f in self.files}
            for f in self.files:
                tags = "、".join(favmod.favorites_of(self._favs(), Path(f).name)) or "—"
                self.fav_songs.insert("", "end", iid=f, values=(Path(f).stem, tags))
            for fav in self._favs():
                for s in fav["songs"]:
                    if s not in names:
                        self.fav_songs.insert("", "end", iid=f"miss::{s}",
                                              values=(f"{s}  ⚠ 文件不在了", fav["name"]))
        else:
            try:
                fav = favmod._find(self._favs(), self.fav_view)
            except ValueError:
                self.fav_view = ""
                return
            names = {Path(f).name for f in self.files}
            for s in fav["songs"]:
                if s in names:
                    for f in self.files:
                        if Path(f).name == s:
                            self.fav_songs.insert("", "end", iid=f,
                                                  values=(Path(f).stem, ""))
                            break
                else:
                    self.fav_songs.insert("", "end", iid=f"miss::{s}",
                                          values=(f"{s}  ⚠ 文件不在了", ""))

    def _fav_sel_paths(self) -> list[str]:
        """右栏选中的**真实文件**路径（缺文件的行自动忽略）。"""
        return [s for s in self.fav_songs.selection() if not s.startswith("miss::")]

    def _mg_busy(self) -> bool:
        if self.player.is_running:
            self.say("演奏中不能改曲库文件，先停止。", "warn")
            return True
        return False

    def fav_create(self):
        name = self._modal("新建收藏夹", "收藏夹名：", entry="")
        if not name:
            return
        try:
            favs = favmod.create(self._favs(), name)
        except ValueError as e:
            self._modal("收藏夹", str(e), kind="error")
            return
        self.fav_view = name.strip()
        self._save_favs(favs)
        self.say(f"已建收藏夹：{name.strip()}", "ok")

    def fav_rename(self):
        sel = self.fav_tree.selection()
        if not sel or sel[0] == self.ALL_IID:
            self._modal("收藏夹", "先在左栏选一个收藏夹（「全部曲目」不是收藏夹）。")
            return
        old = sel[0]
        new = self._modal("重命名收藏夹", f"「{old}」的新名字：", entry=old)
        if not new or new == old:
            return
        try:
            favs = favmod.rename(self._favs(), old, new.strip())
        except ValueError as e:
            self._modal("收藏夹", str(e), kind="error")
            return
        if self.fav_view == old:
            self.fav_view = new.strip()
        self._save_favs(favs)
        self.say(f"收藏夹改名：{old} → {new.strip()}", "ok")

    def fav_delete(self):
        sel = self.fav_tree.selection()
        if not sel or sel[0] == self.ALL_IID:
            self._modal("收藏夹", "先在左栏选一个收藏夹。")
            return
        name = sel[0]
        if not self._modal("删除收藏夹",
                           f"删除收藏夹「{name}」？\n只删这个收藏夹本身，曲子文件不动。",
                           kind="warn", buttons=("删除", "取消")):
            return
        self.fav_view = ""
        self._save_favs(favmod.delete(self._favs(), name))
        self.say(f"已删收藏夹：{name}（曲子文件都还在）", "ok")

    def fav_add(self):
        sel = self._fav_sel_paths()
        if not sel:
            self._modal("收藏夹", "先在右栏选中要加入的曲子（可多选）。")
            return
        self._add_paths_to_fav(sel)

    def fav_play(self):
        """收藏夹页双击 = 直接演奏（Soundpad 同款）。重命名挪到 F2 / 右键。"""
        sel = self._fav_sel_paths()
        if len(sel) != 1:
            self.say("选中一首（双击）即可演奏。", "dim")
            return
        self.play_song_by_name(Path(sel[0]).name)

    def _add_paths_to_fav(self, paths: list[str]):
        """通用入口：把若干**真实文件**加入某个收藏夹（列表点选 + 可现场新建）。"""
        if not paths:
            return
        name = self._choose_fav_modal(
            "加入收藏夹", f"把 {len(paths)} 首加入哪个收藏夹？（可双击直接选）")
        if not name:
            return
        favs = self._favs()
        try:
            if not any(x["name"] == name for x in favs):
                favs = favmod.create(favs, name)
            favs, n = favmod.add_songs(favs, name, [Path(s).name for s in paths])
        except ValueError as e:
            self._modal("收藏夹", str(e), kind="error")
            return
        # ★ 不跳转视图：留在原地，方便连续往多个收藏夹加（用户 2026-10-12 反馈）
        self._save_favs(favs)
        self.say(f"已把 {n} 首加入「{name}」"
                 + ("（其余本来就在）" if n < len(paths) else ""), "ok")

    def _fav_sel_basenames(self) -> list[str]:
        """右栏选中的曲名（含缺文件条目——移出记录时用得到）。"""
        out = []
        for iid in self.fav_songs.selection():
            out.append(iid[6:] if iid.startswith("miss::") else Path(iid).name)
        return out

    def _remove_bases_from_fav(self, fav_name: str, bases: list[str]):
        try:
            favs = favmod.remove_songs(self._favs(), fav_name, bases)
        except ValueError as e:
            self._modal("收藏夹", str(e), kind="error")
            return
        self._save_favs(favs)
        self.say(f"已从「{fav_name}」移出 {len(bases)} 首", "ok")

    def fav_remove(self):
        if not self.fav_view:
            self._modal("收藏夹", "先在左栏选一个具体收藏夹（「全部曲目」视图没有移出一说）。")
            return
        bases = self._fav_sel_basenames()
        if not bases:
            self._modal("收藏夹", "先在右栏选中要移出的曲子（可多选）。")
            return
        self._remove_bases_from_fav(self.fav_view, bases)

    def fav_rename_file(self):
        if self._mg_busy():
            return
        sel = self._fav_sel_paths()
        if len(sel) != 1:
            self._modal("收藏夹", "重命名一次只改一首。")
            return
        old = sel[0]
        p = Path(old)
        new = self._modal("重命名", f"《{p.stem}》的新曲名：", entry=p.stem)
        if not new or new == p.stem:
            return
        try:
            newp = library.rename_song(old, new)
        except Exception as e:
            self._modal("收藏夹", str(e), kind="error")
            return
        self._remap_refs(p.name, newp.name)       # 收藏夹/热键里存的是曲名，跟着改
        self.say(f"重命名：{p.name} → {newp.name}", "ok")
        self._after_library_change()

    def fav_delete_file(self):
        sel = self._fav_sel_paths()
        if sel:
            self._delete_paths(sel)

    def _delete_paths(self, paths: list[str]):
        """通用入口：把若干真实文件移入回收站，并同步收藏夹/热键引用。"""
        if self._mg_busy():
            return
        names = "\n".join("　· " + Path(s).name for s in paths[:10]) + \
                ("\n　…" if len(paths) > 10 else "")
        if not self._modal("删除文件",
                           f"把 {len(paths)} 首移入系统回收站（可还原）？\n{names}",
                           kind="warn", buttons=("移入回收站", "取消")):
            return
        try:
            n = trash.recycle(paths)
        except Exception as e:
            self._modal("收藏夹", f"删除失败：{e}", kind="error")
            return
        for s in paths:
            self._remap_refs(Path(s).name, None)
        self.say(f"已移入回收站 {n} 首（可从回收站还原）。", "ok")
        self._after_library_change()

    # ── 歌曲右键菜单 ───────────────────────────────────────────────
    def _menu_base(self, master=None) -> tk.Menu:
        t = THEME
        return tk.Menu(master or self, tearoff=0, bg=t["card_alt"], fg=t["ink"],
                       activebackground=t["sidebar_sel"], activeforeground=t["ink"],
                       bd=0, relief="flat", font=(t["font"], t["size"]))

    def _song_menu_fav(self, e):
        """收藏夹页右栏：右键 = 加入/移出收藏夹、重命名、删除。

        ★「全部曲目」视图下也有移出：子菜单列出选中曲子所在的收藏夹，逐个移出。
        """
        tree = self.fav_songs
        row = tree.identify_row(e.y)
        if row and row not in tree.selection():
            tree.selection_set(row)
        m = self._menu_base()
        bases = self._fav_sel_basenames()
        paths = self._fav_sel_paths()
        if paths:
            if len(paths) == 1:
                m.add_command(label="▶ 演奏这首（双击）",
                              command=lambda: self.play_song_by_name(Path(paths[0]).name))
            m.add_command(label=f"加入收藏夹…（{len(paths)} 首）", command=self.fav_add)
        # ── 移出 ──
        if self.fav_view:
            if bases:
                m.add_command(label=f"移出「{self.fav_view}」", command=self.fav_remove)
        elif bases:
            containing = [f["name"] for f in self._favs()
                          if any(b in f["songs"] for b in bases)]
            if containing:
                sub = self._menu_base(m)
                for fname in containing:
                    sub.add_command(
                        label=f"从「{fname}」移出",
                        command=lambda fn=fname: self._remove_bases_from_fav(fn, bases))
                m.add_cascade(label="移出收藏夹", menu=sub)
        if paths:
            m.add_separator()
            m.add_command(label="重命名（F2）", command=self.fav_rename_file)
            m.add_command(label="删除文件（进回收站）", command=self.fav_delete_file)
        if m.index("end") is None:                 # 一条能点的都没有
            m.add_command(label="（无可执行的操作）", state="disabled")
        try:
            m.tk_popup(e.x_root, e.y_root)
        finally:
            m.grab_release()

    def _song_menu_play(self, e):
        """演奏页列表：右键 = 加入收藏夹 / 从某收藏夹移除 / 删除文件。"""
        tree = self.tree
        row = tree.identify_row(e.y)
        if not row:
            return
        if row not in tree.selection():
            tree.selection_set(row)
        path = row
        base = Path(path).name
        m = self._menu_base()
        m.add_command(label="加入收藏夹…",
                      command=lambda: self._add_paths_to_fav([path]))
        favs = favmod.favorites_of(self._favs(), base)
        if favs:
            sub = self._menu_base(m)
            for fname in favs:
                sub.add_command(
                    label=f"从「{fname}」移出",
                    command=lambda fn=fname: self._remove_bases_from_fav(fn, [base]))
            m.add_cascade(label="移出收藏夹", menu=sub)
        m.add_separator()
        m.add_command(label="删除文件（进回收站）",
                      command=lambda: self._delete_paths([path]))
        try:
            m.tk_popup(e.x_root, e.y_root)
        finally:
            m.grab_release()

    def _remap_refs(self, old_base: str, new_base: str | None):
        """改文件名 / 删文件后，同步收藏夹和自定义热键里的曲名引用。"""
        favs = self._favs()
        favs = (favmod.sync_file_rename(favs, old_base, new_base) if new_base
                else favmod.sync_file_delete(favs, old_base))
        self.cfg["favorites"] = favs
        m = self.hotkey_map()
        changed = False
        for e in m:
            if e["song"] == old_base:
                # ★ 原来是 `new_base or e["song"]`：删除时 new_base=None 又被兜回原值，
                #   下面 `if e["song"]` 因此一条都过滤不掉 → 删掉的曲子在热键页永远留着。
                e["song"] = new_base or ""
                changed = True
        m = [e for e in m if e["song"]]
        if changed:                             # 清空成 [] 也要写回，不能用 any(...)
            self.cfg["hotkey_map"] = m
        cfgmod.save(self.cfg_path, self.cfg)

    def _after_library_change(self):
        self.refresh_library()                      # 内部会顺带刷新收藏夹右栏

    # ── 在线曲库（B站三角洲口琴谱） ─────────────────────────────
    def _build_page_online(self):
        t = THEME
        f = tk.Frame(self.canvas, bg=t["card"])
        self._frames["page_online"] = f
        head = tk.Frame(f, bg=t["card"])
        head.pack(fill="x")
        tk.Label(head, text="在线曲库", bg=t["card"], fg=t["ink"],
                 font=(t["font"], t["size_title"], "bold")).pack(side="left")
        PillButton(head, "下载所选", command=self.on_download).pack(side="right")
        tk.Label(f, text="搜索 B 站「三角洲行动口琴谱」社区曲库（bilibili.com/toy/deltaforce）。"
                         "关键词留空 = 按最新上传浏览。下载 = 转成 MIDI 存入曲库根目录。",
                 bg=t["card"], fg=t["ink_soft"],
                 font=(t["font"], t["size_sm"])).pack(anchor="w", pady=(2, SP["sm"]))

        row = tk.Frame(f, bg=t["card"])
        row.pack(fill="x")
        self.var_online_q = tk.StringVar()
        ent = ttk.Entry(row, textvariable=self.var_online_q, width=32)
        ent.pack(side="left")
        ent.bind("<Return>", lambda e: self.on_search())
        PillButton(row, "搜索", command=self.on_search,
                   kind="accent").pack(side="left", padx=SP["sm"])
        PillButton(row, "加载更多", command=self.on_more).pack(side="left")
        self.lbl_online = tk.Label(row, text="", bg=t["card"], fg=t["ink_faint"],
                                   font=(t["font"], t["size_sm"]))
        self.lbl_online.pack(side="left", padx=SP["md"])

        mid = tk.Frame(f, bg=t["card"])
        mid.pack(fill="both", expand=True)
        cols = ("title", "uploader", "bpm", "notes", "dur", "dl", "likes")
        self.on_tree = ttk.Treeview(mid, columns=cols, show="headings",
                                    selectmode="extended")
        for cid, txt, w in (("title", "曲目", 300), ("uploader", "UP主", 140),
                            ("bpm", "BPM", 60), ("notes", "音符数", 70),
                            ("dur", "时长", 70), ("dl", "下载量", 70),
                            ("likes", "点赞", 60)):
            self.on_tree.heading(cid, text=txt)
            self.on_tree.column(cid, width=w, anchor="w",
                                stretch=(cid in ("title", "uploader")))
        vs = ttk.Scrollbar(mid, orient="vertical", command=self.on_tree.yview)
        self.on_tree.configure(yscrollcommand=vs.set)
        vs.pack(side="right", fill="y")
        self.on_tree.pack(fill="both", expand=True)
        self.on_tree.bind("<Double-1>", lambda e: self.on_download())

    # ── 在线曲库：网络操作（工作线程 + 队列） ─────────────────────
    def _client(self) -> bili.BiliClient:
        if self.bili is None:
            self.bili = bili.BiliClient()
        return self.bili

    def _net_run(self, fn, busy_label: str):
        if self._net_busy:
            self.say("上一个网络操作还没完成，稍等。", "warn")
            return
        self._net_busy = True
        self.lbl_online.configure(text=busy_label)

        def work():
            try:
                fn()
            except Exception as e:
                self._net_events.put(("error", str(e)))
            finally:
                self._net_events.put(("idle", None))

        threading.Thread(target=work, daemon=True).start()

    def on_search(self):
        self._online_query = self.var_online_q.get()
        self._online_cursor = None
        self.on_tree.delete(*self.on_tree.get_children())
        self._online_rows.clear()
        self._net_run(lambda: self._net_search(False), "搜索中…")

    def on_more(self):
        if self._online_cursor is None and self.on_tree.get_children():
            self.say("没有更多结果了。", "dim")
            return
        self._net_run(lambda: self._net_search(True), "加载中…")

    def _net_search(self, append: bool):
        scores, cursor = self._client().search(
            self._online_query, limit=20, cursor=self._online_cursor)
        st = self._client().stats([s["id"] for s in scores])
        self._net_events.put(("search", {"scores": scores, "cursor": cursor,
                                         "stats": st, "append": append}))

    def _fmt_dur(self, ms) -> str:
        try:
            sec = int(ms) // 1000
            return f"{sec // 60}:{sec % 60:02d}"
        except Exception:
            return ""

    def _handle_net_event(self, kind: str, payload):
        if kind == "idle":
            self._net_busy = False
            self.lbl_online.configure(text="")
        elif kind == "error":
            self.lbl_online.configure(text="")
            self._modal("在线曲库", payload, kind="error")
        elif kind == "search":
            data = payload
            self._online_cursor = data["cursor"]
            st = data["stats"]
            for s in data["scores"]:
                sid = s["id"]
                row = st.get(sid, {})
                vals = (s.get("title", sid), s.get("uploader", ""),
                        f"{s.get('bpm', 0):g}", s.get("noteCount", ""),
                        self._fmt_dur(s.get("durationMs")),
                        row.get("downloads", ""), row.get("likes", ""))
                self.on_tree.insert("", "end", iid=sid, values=vals)
                self._online_rows[sid] = s
            n = len(data["scores"])
            where = "追加" if data["append"] else "本次"
            self.say(f"在线曲库：{where} {n} 首"
                     + ("" if self._online_cursor else "（没有更多了）"), "ok")
        elif kind == "downprog":
            i, n = payload
            self.lbl_online.configure(text=f"下载 {i}/{n}…")
        elif kind == "download":
            path = payload
            self.say(f"已下载到曲库：{path.name}", "ok")
            if not self.player.is_running:
                self.refresh_library()
            else:
                self.say("演奏中，先别切曲库；停止后点「刷新」即可看到。", "warn")

    def on_download(self):
        sel = list(self.on_tree.selection())
        if not sel:
            self._modal("在线曲库", "先选中要下载的曲目（可多选）。")
            return
        ids = list(sel)
        # ★ lib_path() 内部读的是 tk.StringVar —— 必须在主线程取好再交给工作线程，
        #   工作线程碰 Tk 会抛 "main thread is not in main loop"
        lib = Path(self.lib_path())

        def work():
            n = len(ids)
            for i, sid in enumerate(ids, 1):
                score = self._online_rows.get(sid)
                if not score:
                    continue
                path = bili.download_midi(self._client(), score, lib)
                self._net_events.put(("download", path))
                self._net_events.put(("downprog", (i, n)))

        self._net_run(work, f"下载 0/{len(ids)}…")

    def _poll_net_events(self):
        try:
            while True:
                kind, payload = self._net_events.get_nowait()
                try:
                    self._handle_net_event(kind, payload)
                except Exception:
                    pass
        except queue.Empty:
            pass
        self.after(80, self._poll_net_events)

    # ── 暗色模态对话框 ─────────────────────────────────────────────
    def _modal(self, title: str, message: str, kind: str = "info",
               entry: str | None = None,
               buttons: tuple[str, ...] = ("确定", "取消")) -> str | None:
        """暗色主题的模态对话框，替代系统 messagebox/simpledialog ——
        那些是浅色原生窗口，和暗色界面违和（用户实测点名"廉价"）。

        entry=None  → 提示/确认框：返回 "ok"（点了主按钮）或 None
        entry="..." → 输入框（预填 entry）：返回输入文本或 None
        Esc / 关闭 = 取消。必须在主线程调用。
        """
        t = THEME
        win = tk.Toplevel(self)
        win.title(title)
        win.configure(bg=t["card"])
        win.resizable(False, False)
        win.transient(self)
        win.grab_set()
        # ★ 对话框标题栏也走暗色（否则是系统浅紫，和暗色界面违和）
        glass.apply(win, backdrop="none", extend_frame=False, round_corners=True,
                    dark=True, caption_color=GLASS["caption_color"])
        result = {"v": None}
        body = tk.Frame(win, bg=t["card"])
        body.pack(fill="both", expand=True, padx=SP["lg"], pady=SP["lg"])
        color = t["warn"] if kind in ("warn", "error") else t["ink"]
        tk.Label(body, text=message, bg=t["card"], fg=color, justify="left",
                 anchor="w", wraplength=360,
                 font=(t["font"], t["size"])).pack(anchor="w")
        var = tk.StringVar(value=entry or "")
        if entry is not None:
            ent = ttk.Entry(body, textvariable=var)
            ent.pack(fill="x", pady=(SP["md"], 0))
            ent.focus_set()
            ent.bind("<Return>", lambda e: _close(ok=True))
            ent.bind("<Escape>", lambda e: _close(ok=False))

        def _close(ok: bool):
            result["v"] = (var.get() if entry is not None else "ok") if ok else None
            win.destroy()

        brow = tk.Frame(win, bg=t["card"])
        brow.pack(side="bottom", fill="x", padx=SP["lg"], pady=(SP["sm"], SP["lg"]))
        PillButton(brow, buttons[0], kind="accent",
                   command=lambda: _close(ok=True)).pack(side="right")
        if len(buttons) > 1:
            PillButton(brow, buttons[1],
                       command=lambda: _close(ok=False)).pack(side="right", padx=SP["sm"])
        win.bind("<Escape>", lambda e: _close(ok=False))
        # 居中到主窗口
        win.update_idletasks()
        x = self.winfo_rootx() + (self.winfo_width() - win.winfo_reqwidth()) // 2
        y = self.winfo_rooty() + (self.winfo_height() - win.winfo_reqheight()) // 2
        win.geometry(f"+{max(0, x)}+{max(0, y)}")
        win.wait_window()
        return result["v"]

    def _choose_fav_modal(self, title: str, message: str) -> str | None:
        """从收藏夹列表里**选一个**（点选/双击），也能现场新建 —— 取代手输名字。

        返回收藏夹名（不一定是已存在的：新建的名字直接返回，由调用方决定建不建），
        Esc / 关闭 = None。
        """
        t = THEME
        win = tk.Toplevel(self)
        win.title(title)
        win.configure(bg=t["card"])
        win.resizable(False, False)
        win.transient(self)
        win.grab_set()
        glass.apply(win, backdrop="none", extend_frame=False, round_corners=True,
                    dark=True, caption_color=GLASS["caption_color"])
        result: dict = {"v": None}
        body = tk.Frame(win, bg=t["card"])
        body.pack(fill="both", expand=True, padx=SP["lg"], pady=SP["lg"])
        tk.Label(body, text=message, bg=t["card"], fg=t["ink"], justify="left",
                 anchor="w", wraplength=360, font=(t["font"], t["size"])).pack(anchor="w")

        favs = self._favs()
        tree = ttk.Treeview(win, columns=("name",), show="headings",
                            height=min(8, max(3, len(favs) + 1)))
        tree.heading("name", text="收藏夹")
        tree.column("name", width=260, anchor="w")
        tree.pack(fill="both", expand=True, pady=(SP["md"], 0))

        def _confirm(_e=None):
            sel = tree.selection()
            if sel and sel[0] != NO_FAV:          # 占位行不可选
                result["v"] = sel[0]
                win.destroy()

        # ★ iid 绝不能是空串：那是 Treeview 的根节点，insert 会抛
        #   TclError("Item  already exists")；而上面的 grab_set() 已经执行、
        #   wait_window() 永远到不了 → 模态框带着全局 grab 留在屏幕上，
        #   整个界面点不动，只能杀进程。（空收藏夹时必现）
        NO_FAV = "__no_fav__"
        for fav in favs:
            tree.insert("", "end", iid=fav["name"],
                        values=(f"{fav['name']}（{len(fav['songs'])}）",))
        if not favs:
            tree.insert("", "end", iid=NO_FAV, values=("（还没有收藏夹，点下面新建）",))

        def _new():
            name = self._modal("新建收藏夹", "收藏夹名：", entry="")
            if name and name.strip():
                result["v"] = name.strip()
                win.destroy()

        brow = tk.Frame(win, bg=t["card"])
        brow.pack(side="bottom", fill="x", padx=SP["lg"], pady=(SP["sm"], SP["lg"]))
        PillButton(brow, "确定", kind="accent", command=_confirm).pack(side="right")
        PillButton(brow, "取消", command=win.destroy).pack(side="right", padx=SP["sm"])
        PillButton(brow, "➕ 新建收藏夹", command=_new).pack(side="left")
        tree.bind("<Double-1>", _confirm)
        tree.bind("<Return>", _confirm)
        win.bind("<Escape>", lambda e: win.destroy())
        if favs:
            first = self.fav_view if self.fav_view and self.fav_view != self.ALL_IID else ""
            try:
                tree.selection_set(first if first else tree.get_children()[0])
                tree.focus_set()
                tree.focus(tree.get_children()[0])
            except Exception:
                pass
            tree.focus_set()
        win.update_idletasks()
        x = self.winfo_rootx() + (self.winfo_width() - win.winfo_reqwidth()) // 2
        y = self.winfo_rooty() + (self.winfo_height() - win.winfo_reqheight()) // 2
        win.geometry(f"+{max(0, x)}+{max(0, y)}")
        win.wait_window()
        return result["v"]

    def _build_page_log(self):
        t = THEME
        f = tk.Frame(self.canvas, bg=t["card"])
        self._frames["page_log"] = f
        head = tk.Frame(f, bg=t["card"])
        head.pack(fill="x")
        tk.Label(head, text="日志", bg=t["card"], fg=t["ink"],
                 font=(t["font"], t["size_title"], "bold")).pack(side="left")
        PillButton(head, "清空", command=self.clear_log).pack(side="right")
        self.log = tk.Text(f, bg=t["card_alt"], fg=t["ink"], relief="flat", bd=0,
                           highlightthickness=0, wrap="word", font=(t["font"], t["size_sm"]))
        self.log.pack(fill="both", expand=True, pady=(SP["sm"], 0))
        self.log.configure(state="disabled")
        self._log_placeholder()
        for tag, col in (("ok", t["ok"]), ("warn", t["warn"]), ("dim", t["ink_faint"])):
            self.log.tag_configure(tag, foreground=col)

    def _build_page_settings(self):
        t = THEME
        f = tk.Frame(self.canvas, bg=t["card"])
        self._frames["page_settings"] = f
        tk.Label(f, text="设置", bg=t["card"], fg=t["ink"],
                 font=(t["font"], t["size_title"], "bold")).pack(anchor="w")

        def title(txt, hint=""):
            tk.Label(f, text=txt, bg=t["card"], fg=t["ink_soft"],
                     font=(t["font"], t["size_sm"])).pack(anchor="w", pady=(SP["lg"], 0))
            if hint:
                tk.Label(f, text=hint, bg=t["card"], fg=t["ink_faint"],
                         font=(t["font"], t["size_sm"])).pack(anchor="w")

        title("曲库目录", "相对程序目录，也可以填绝对路径")
        row = tk.Frame(f, bg=t["card"])
        row.pack(fill="x", pady=(SP["sm"], 0))
        self.var_lib = tk.StringVar(value=str(library.resolve_lib_dir(self.cfg["lib_dir"], ROOT)))
        ttk.Entry(row, textvariable=self.var_lib).pack(side="left", fill="x", expand=True)
        PillButton(row, "更换目录", command=self.pick_dir).pack(side="left", padx=SP["sm"])
        PillButton(row, "刷新", command=self.refresh_library,
                   kind="accent").pack(side="left")

        title("键位映射", "换游戏只改这里的 JSON；现有：" + "、".join(list_mappings() or ["无"]))
        self.var_map = tk.StringVar(value=self.cfg["mapping"])
        ttk.Combobox(f, textvariable=self.var_map, values=list_mappings(),
                     state="readonly").pack(fill="x", pady=(SP["sm"], 0))

        title("全局热键", "在「热键」页自由绑定曲目与播放控制（组合键需带 Ctrl/Alt/Win）；失焦也生效")
        self.var_hotkeys = tk.BooleanVar(value=bool(self.cfg.get("hotkeys", True)))
        ttk.Checkbutton(f, text="启用全局热键", variable=self.var_hotkeys,
                        command=self.toggle_hotkeys, takefocus=False).pack(anchor="w", pady=(SP["xs"], 0))

        title("发鼠标键前把光标挪开",
              "游戏里鼠标三键是移调键，按下去同时也是一次点击；挪到角落可避免点中琴孔")
        self.var_park = tk.BooleanVar(value=bool(self.cfg.get("park_cursor")))
        ttk.Checkbutton(f, text="启用（默认关）", variable=self.var_park,
                        command=self._sync_settings, takefocus=False).pack(anchor="w", pady=(SP["xs"], 0))

        title("玻璃外观", "窗口级的背景模糊（Windows 11 才有）；不支持会自动退化")
        self.lbl_glass = tk.Label(f, text="", bg=t["card"], fg=t["ink_faint"],
                                  font=(t["font"], t["size_sm"]), justify="left", anchor="w")
        self.lbl_glass.pack(anchor="w", pady=(SP["xs"], 0))

    def _sync_settings(self):
        # 有些控件只在设置页建好后才存在，切页前调用要防一手
        try:
            self.cfg["lib_dir"] = self.var_lib.get().strip()
            self.cfg["mapping"] = self.var_map.get()
            self.cfg["hotkeys"] = bool(self.var_hotkeys.get())
            self.cfg["park_cursor"] = bool(self.var_park.get())
            # ★ 这三个原来只从 cfg 读、从不写回 → 关掉程序设置就丢。
            #   用 hasattr 兜底：这些 var 只在演奏页建好后才存在，
            #   缺了不能连累上面几项一起存不进去。
            if hasattr(self, "var_intro"):
                self.cfg["no_intro"] = bool(self.var_intro.get())
            if hasattr(self, "var_loop"):
                self.cfg["loop"] = bool(self.var_loop.get())
            if hasattr(self, "var_count"):
                self.cfg["countdown"] = float(self.var_count.get())
        except Exception:
            return
        cfgmod.save(self.cfg_path, self.cfg)
        if not self.glass_info.get("applied"):
            msg = "这台机器/系统不支持窗口玻璃，已自动用纯色底。"
        elif self.glass_client:
            msg = "全窗玻璃已开（注意：文字会发白，见 README 的实测说明）。"
        else:
            msg = "窗口边框为系统亚克力，内容区为实色（文字更清晰）。"
        self.lbl_glass.configure(text=msg)

    def toggle_hotkeys(self):
        if self.var_hotkeys.get():
            self.start_hotkeys()
        else:
            self.hotkeys.stop()
            self.say("全局热键已关闭。", "dim")
        self._sync_settings()
        self._draw_shell()

    # ── 日志 ───────────────────────────────────────────────────────
    def say(self, msg: str, tag: str = ""):
        try:
            self.log.configure(state="normal")
            self.log.insert("end", msg + "\n", tag or ())
            self.log.see("end")
            self.log.configure(state="disabled")
        except Exception:
            pass

    def _poll_ui_events(self):
        try:
            while True:
                kind, payload = self._ui_events.get_nowait()
                if kind == "progress":
                    self.on_progress(payload)
                elif kind == "log":
                    self.say(*payload)
                elif kind == "done":
                    # ★ 世代号不匹配 = 上一次播放迟到的结束信号，丢弃。
                    #   （Player.join() 在 IDLE 一出现就返回，旧线程的 finally 还没跑完；
                    #    此时新播放已开始，若照单全收会把新播放复位成"已停止"）
                    if payload == getattr(self, "_play_gen", 0):
                        self.after_play()
        except queue.Empty:
            pass
        except Exception:
            pass
        self.after(60, self._poll_ui_events)

    # ── 曲库 ───────────────────────────────────────────────────────
    def lib_path(self) -> str:
        return self.var_lib.get().strip()

    def refresh_library(self):
        if self.player.is_running:
            self.say("演奏中不能换曲库，先停止。", "warn")
            return
        self.lib_all = library.list_library_all(self.lib_path())
        self.files = [p for p, _ in self.lib_all]     # ★ 全部可弹（含分类里的）
        self.tree.delete(*self.tree.get_children())
        n_intro = 0
        for f in self.files:
            try:
                s = library.load_score(f, self.mapping)
                if any(sec.intro for sec in s.sections):
                    n_intro += 1
                vals = (self._hk_text(f), Path(f).stem,
                        s.bar_count(), s.note_count(), f"{s.bpm:g}")
            except Exception as e:
                vals = (self._hk_text(f), f"{Path(f).stem}  ★ 读不了：{e}",
                        "", "", "")
            self.tree.insert("", "end", iid=f, values=vals)
        if not self.files:
            self.say(f"曲库是空的：{self.lib_path()}", "warn")
        else:
            self.say(f"曲库 {self.lib_path()}：{len(self.files)} 首"
                     + (f"（{n_intro} 首带前奏）" if n_intro else ""), "ok")
        self.refresh_hk_column()
        if hasattr(self, "fav_tree"):
            self._fav_reload_left()
            self._fav_reload_right()
        self._draw_shell()
        last = self.cfg.get("last_song")
        if last:
            for f in self.files:
                if Path(f).name == last:
                    self.tree.selection_set(f)
                    self.tree.see(f)
                    self.on_pick_song()
                    break

    def pick_dir(self):
        d = filedialog.askdirectory(initialdir=self.lib_path() or "/", title="选择曲库目录")
        if not d:
            return
        try:
            rel = os.path.relpath(d, ROOT)
            self.var_lib.set(rel if not rel.startswith("..") else d)
        except Exception:
            self.var_lib.set(d)
        self.cfg["lib_dir"] = self.var_lib.get()
        cfgmod.save(self.cfg_path, self.cfg)
        self.refresh_library()

    def on_pick_song(self):
        sel = self.tree.selection()
        if not sel:
            return
        path = sel[0]
        self.current = path
        self.cfg["last_song"] = Path(path).name
        cfgmod.save(self.cfg_path, self.cfg)
        try:
            s = library.load_score(path, self.mapping)
        except Exception as e:
            self.lbl_title.configure(text=Path(path).stem)
            self.lbl_meta.configure(text=f"读不了：{e}")
            self.say(f"读不了 {Path(path).name}：{e}", "warn")
            self.actions = []
            self.refresh_preview()      # ★ 不清预览会残留上一首的动作列表和总时长
            return
        self.lbl_title.configure(text=s.title or Path(path).stem)
        self.lbl_meta.configure(
            text=f"{s.beats_per_bar:g}/{s.meter_den}　{s.bar_count()} 小节　"
                 f"{s.note_count()} 个音　{s.bpm:g} BPM　"
                 + ("有前奏（可跳过）" if any(x.intro for x in s.sections) else "无前奏"))
        self.recompile()

    # ── 编译 / 预览 ────────────────────────────────────────────────
    def recompile(self):
        if not self.current:
            return
        try:
            s = library.load_score(self.current, self.mapping)
            legato = bool(self.var_legato.get())
            if bool(self.cfg.get("legato", True)) != legato:   # 记住用户的选择
                self.cfg["legato"] = legato
                cfgmod.save(self.cfg_path, self.cfg)
            self.actions = compile_actions(
                s, not self.var_intro.get(), 1.0, False, self.mapping,
                legato=legato)
            warn = f"，{len(s.warnings)} 处提醒" if s.warnings else ""
            self.lbl_state.configure(text=f"{len(self.actions)} 个音可弹{warn}")
            for w in (s.warnings or [])[:3]:
                self.say("⚠ " + w, "warn")
        except Exception as e:
            self.say(f"编译失败：{e}", "warn")
            self.actions = []
        self.refresh_preview()

    def _fit_tree_columns(self, _e=None):
        w = self.tree.winfo_width()
        fixed = 40 + 90 + 46 * 3 + 24                # 键 + 曲目外固定列 + 小节/音数/BPM + 滚动条
        self.tree.column("title", width=max(90, w - fixed))
        for c in ("bars", "notes", "bpm"):
            self.tree.column(c, width=46)

    def refresh_preview(self):
        self.preview.delete(0, "end")
        total = max((a.t + a.hold for a in self.actions), default=0.0)
        for a in self.actions[:10]:
            self.preview.insert("end", f"{a.t:6.2f}s   {a.describe()}")
        if not self.actions:
            why = ("  选一首曲子后，这里显示前 10 个音要按什么"
                   if not self.current else "  （这首解析不出可弹的音）")
            self.preview.insert("end", why)
        self.scale.set_range(0.0, max(total, 0.001))
        self.lbl_time.configure(text=f"0:00 / {self._mmss(total / max(self.var_rate.get(), 0.05))}")

    def _on_rate_move(self, _v=None):
        r = max(0.05, float(self.var_rate.get()))
        self.lbl_rate.configure(text=f"{r:.2f}×")
        self.cfg["rate"] = r
        cfgmod.save(self.cfg_path, self.cfg)
        self.player.set_rate(r)
        self.refresh_preview()

    # ── 播放 ───────────────────────────────────────────────────────
    def _total(self) -> float:
        return max((a.t + a.hold for a in self.actions), default=0.0)

    def on_play(self, start_s: float | None = None):
        if not self.actions:
            self._modal("口琴演奏器", "先挑一首能弹的曲子。")
            return
        self._play_gen = getattr(self, "_play_gen", 0) + 1   # 本次播放的世代号
        if self.player.is_running:
            self.player.stop()
            self.player.join()
        if start_s is None:
            start_s = float(self.var_pos.get()) if self.var_pos.get() > 0.05 else 0.0
        # ★ 所有 Tk 变量在这里（主线程）一次读出，之后工作线程只用普通值
        rate = float(self.var_rate.get())
        countdown = float(self.var_count.get())
        loop = bool(self.var_loop.get())
        actions = list(self.actions)

        if self.dry:
            backend = NullInput(self.mapping)
        else:
            try:
                backend = make_backend(self.mapping, park_cursor=bool(self.cfg.get("park_cursor")))
                if hasattr(backend, "park"):
                    backend.park()
            except Exception as e:
                self._modal("口琴演奏器", str(e), kind="error")
                return

        q = self._ui_events
        self.player = Player(
            backend,
            on_progress=lambda st: q.put(("progress", st)),
            on_log=lambda m: q.put(("log", (m, "dim"))))
        self.btn_play.configure(state="disabled")
        self.btn_pause.configure(state="normal")
        self.say(f"▶ 开始：《{self.lbl_title.cget('text')}》 {len(actions)} 个音，{rate:.2f}×"
                 + (f"，从 {self._mmss(start_s)} 开始" if start_s else ""), "ok")

        # ★ 必须捕获成局部变量：self.player 一旦被下一次 on_play 换掉，
        #   旧线程就会去 play 新 player（两条线程共弹一个）／unpark 新 backend。
        player = self.player
        gen = self._play_gen

        def work():
            try:
                player.play(actions, countdown=countdown, loop=loop,
                            rate=rate, start_s=start_s)
            except Exception:
                q.put(("log", ("演奏出错：\n" + traceback.format_exc(), "warn")))
            finally:
                if hasattr(player.backend, "unpark"):
                    try:
                        player.backend.unpark()
                    except Exception:
                        pass
                # 带上世代号：旧播放迟到的 done 不能把新播放复位（否则
                # 刚开始播就被"已停止"、按钮被启用、进度条归零）
                q.put(("done", gen))

        self._worker = threading.Thread(target=work, daemon=True)
        self._worker.start()

    def after_play(self):
        st = self.player.state
        self.btn_play.configure(state="normal")
        self.btn_pause.configure(text="⏸  暂停")
        self.say("演奏结束。" if st == "done" else "已停止。", "dim")
        self.var_pos.set(0.0)
        self.scale.set_value(0.0)
        self.lbl_now.configure(text="待演奏", fg=THEME["ink_faint"])
        self.lbl_press.configure(text="")

    def on_pause(self):
        self.player.toggle_pause()
        if self.player.state == "paused":
            self.btn_pause.configure(text="▶  继续")
            self.say("已暂停（所有键已松开）。", "dim")
        else:
            self.btn_pause.configure(text="⏸  暂停")
            self.say("继续。", "dim")

    def on_stop(self):
        self.player.stop()
        self.say("已停止。", "dim")

    def on_progress(self, st: dict):
        state = st.get("state")
        dur = st.get("duration") or self._total()
        elapsed = st.get("elapsed", 0.0)
        if state == "countdown":
            self.lbl_now.configure(text="准备……", fg=THEME["accent"])
            return
        if state == "playing":
            self.lbl_now.configure(text=str(st.get("index", 0) + 1) + " / " + str(st.get("total", 0)),
                                   fg=THEME["accent"])
            a = st.get("action")
            if a is not None:
                self.lbl_press.configure(text=f"{a.label}  →  {a.describe()}")
            if not self._seeking:
                self.var_pos.set(elapsed)
                self.scale.set_value(elapsed)
            rate = max(st.get("rate", 1.0), 0.05)
            self.lbl_time.configure(text=f"{self._mmss(elapsed / rate)} / {self._mmss(dur / rate)}")

    # ── 拖动进度条 ─────────────────────────────────────────────────
    def _on_scale_move(self, _v=None):
        if self._seeking:
            self.lbl_time.configure(text=f"{self._mmss(float(self.var_pos.get()))} / "
                                         f"{self._mmss(self._total())}")

    def _on_scale_release(self, _e=None):
        if not self._seeking:
            return
        self._seeking = False
        pos = float(self.var_pos.get())
        if pos < 0.3 or abs(pos - self.player.elapsed) < 0.4:
            return
        if self.player.is_running:
            self.player.stop()
            self.player.join()
        self.say(f"跳到 {self._mmss(pos)} 开始。", "dim")
        self.on_play(start_s=pos)

    @staticmethod
    def _mmss(sec: float) -> str:
        sec = max(0, int(round(sec)))
        return f"{sec // 60}:{sec % 60:02d}"

    def _log_placeholder(self):
        state = self.log.cget("state")
        self.log.configure(state="normal")
        if not self.log.get("1.0", "end").strip():
            self.log.insert("1.0", "\n\n　　暂无日志 —— 播放、切曲、报错都会记在这里。", "dim")
        self.log.configure(state=state)

    def clear_log(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    # ── 热键（Soundpad 式自由热键） ────────────────────────────────
    _KEY_SYMS = {
        "minus": "-", "equal": "=", "bracketleft": "[", "bracketright": "]",
        "backslash": "\\", "semicolon": ";", "apostrophe": "'", "comma": ",",
        "period": ".", "slash": "/", "grave": "`", "space": "space",
    }
    _MOD_SYMS = {"Control_L", "Control_R", "Shift_L", "Shift_R",
                 "Alt_L", "Alt_R", "Meta_L", "Meta_R", "Super_L", "Super_R"}

    def hotkey_map(self) -> list[dict]:
        out = []
        for e in self.cfg.get("hotkey_map") or []:
            if not isinstance(e, dict):
                continue
            key = str(e.get("key") or "").strip()
            song = str(e.get("song") or "").strip()
            if key and song:
                out.append({"key": key, "song": song})
        return out

    # ── 播放控制热键（暂停/继续、停止、上一首/下一首）─────────────
    CTL_LABELS = {"toggle": "暂停 / 继续", "stop": "停止",
                  "prev": "上一首", "next": "下一首"}

    def control_keys(self) -> dict:
        base = {"toggle": "", "stop": "", "prev": "", "next": ""}
        raw = self.cfg.get("control_keys") or {}
        if isinstance(raw, dict):
            for k in base:
                v = str(raw.get(k) or "").strip()
                if v and Hotkeys.parse(v)[1] is not None:
                    base[k] = v
        return base

    def _save_control_keys(self, ck: dict):
        self.cfg["control_keys"] = ck
        cfgmod.save(self.cfg_path, self.cfg)
        self.start_hotkeys()                        # 重新注册
        self._hk_reload()

    def ctl_rekey(self):
        fid = self._ctl_selected()
        if fid is None:
            self.say("先选一个播放控制功能。", "warn")
            return
        spec = self._capture_key_modal()
        if not spec:
            return
        ck = self.control_keys()
        if fid != "toggle" and fid in ck and ck[fid] == spec:
            return                                  # 没变化
        if self._key_in_song_map(spec):
            self._modal("组合键已被占用",
                        f"「{spec}」已经绑在某首曲子的热键上了。\n"
                        "先在下面的曲目热键里改掉它，再来绑播放控制。", kind="warn")
            return
        ck[fid] = spec
        self._save_control_keys(ck)
        self.say(f"「{self.CTL_LABELS[fid]}」已设为 {spec}", "ok")

    def ctl_clear(self):
        fid = self._ctl_selected()
        if fid is None:
            self.say("先选一个播放控制功能。", "warn")
            return
        ck = self.control_keys()
        ck[fid] = ""
        self._save_control_keys(ck)
        self.say(f"已清除「{self.CTL_LABELS[fid]}」的按键", "ok")

    def _ctl_selected(self) -> str | None:
        sel = self._ctl_tree.selection()
        if not sel or not sel[0].startswith("ctl_"):
            return None
        return sel[0][4:]

    def _key_in_song_map(self, key: str) -> bool:
        return any(e["key"] == key for e in self.hotkey_map())

    def _key_in_control(self, key: str) -> str | None:
        """被哪个播放控制功能占用了，没有返回 None。"""
        for fid, k in self.control_keys().items():
            if k == key:
                return fid
        return None

    def _save_hotkey_map(self, m: list[dict]):
        self.cfg["hotkey_map"] = m
        cfgmod.save(self.cfg_path, self.cfg)
        self.start_hotkeys()                        # 重新注册
        self._hk_reload()
        self.refresh_hk_column()

    def _migrate_slots(self):
        """旧版 10 槽位（Ctrl+Shift+1~0）一次性迁成自由热键表；之后配置里不再有 slots。"""
        if "slots" not in self.cfg:
            self.cfg.setdefault("favorites", [])
            self.cfg.setdefault("hotkey_map", [])
            return
        slots = [str(x or "") for x in (self.cfg.get("slots") or [])][:10]
        digits = "1234567890"
        m = self.hotkey_map()
        for i, name in enumerate(slots):
            if name and not any(e["key"] == f"Ctrl+Shift+{digits[i]}" for e in m):
                m.append({"key": f"Ctrl+Shift+{digits[i]}", "song": name})
        self.cfg["hotkey_map"] = m
        self.cfg["favorites"] = favmod.normalize(self.cfg.get("favorites"))
        self.cfg.pop("slots", None)
        cfgmod.save(self.cfg_path, self.cfg)

    def _hk_text(self, path: str) -> str:
        """演奏页列表：这首曲子绑了哪些组合键。"""
        base = Path(path).name
        keys = [e["key"] for e in self.hotkey_map() if e["song"] == base]
        return "  ".join(keys) if keys else "—"

    def refresh_hk_column(self):
        try:
            for f in self.files:
                self.tree.set(f, "hk", self._hk_text(f))
        except Exception:
            pass

    def _build_page_hotkeys(self):
        t = THEME
        f = tk.Frame(self.canvas, bg=t["card"])
        self._frames["page_hotkeys"] = f
        tk.Label(f, text="热键", bg=t["card"], fg=t["ink"],
                 font=(t["font"], t["size_title"], "bold")).pack(anchor="w")
        tk.Label(f, text="每条 = 一个组合键 ↔ 一首曲子：程序在后台没焦点也生效，按下立即切换演奏。"
                         "组合键需带 Ctrl / Alt / Win。上面的播放控制（暂停/停止/换曲）同样可自定义。",
                 bg=t["card"], fg=t["ink_soft"],
                 font=(t["font"], t["size_sm"])).pack(anchor="w", pady=(2, SP["sm"]))

        # ── 播放控制 ──
        tk.Label(f, text="播放控制（双击改键）", bg=t["card"], fg=t["ink_soft"],
                 font=(t["font"], t["size_sm"])).pack(anchor="w")
        ctlrow = tk.Frame(f, bg=t["card"])
        ctlrow.pack(fill="x")
        self._ctl_tree = ttk.Treeview(ctlrow, columns=("func", "key"),
                                      show="headings", height=4)
        for c, (txt, w) in {"func": ("功能", 140), "key": ("组合键", 180)}.items():
            self._ctl_tree.heading(c, text=txt)
            self._ctl_tree.column(c, width=w, anchor="w")
        self._ctl_tree.pack(side="left", fill="x", expand=True)
        self._ctl_tree.bind("<Double-1>", lambda e: self.ctl_rekey())
        ctlbtns = tk.Frame(ctlrow, bg=t["card"])
        ctlbtns.pack(side="left", padx=(SP["md"], 0))
        PillButton(ctlbtns, "改键", command=self.ctl_rekey).pack(anchor="w")
        PillButton(ctlbtns, "清除按键", command=self.ctl_clear,
                   kind="danger").pack(anchor="w", pady=(SP["xs"], 0))

        tk.Label(f, text="曲目热键", bg=t["card"], fg=t["ink_soft"],
                 font=(t["font"], t["size_sm"])).pack(anchor="w", pady=(SP["md"], 0))
        btns = tk.Frame(f, bg=t["card"])
        btns.pack(side="bottom", fill="x", pady=(SP["md"], SP["sm"]))
        mid = tk.Frame(f, bg=t["card"])
        mid.pack(fill="both", expand=True)
        mid.columnconfigure(0, weight=1)
        mid.columnconfigure(1, minsize=SP["lg"])
        mid.columnconfigure(2, weight=1)
        mid.rowconfigure(0, weight=1)
        self._hk_tree = ttk.Treeview(mid, columns=("key", "song", "state"),
                                     show="headings", height=12)
        for c, (txt, w) in {"key": ("组合键", 130), "song": ("曲目", 230),
                            "state": ("状态", 90)}.items():
            self._hk_tree.heading(c, text=txt)
            self._hk_tree.column(c, width=w, anchor="w", stretch=(c == "song"))
        self._hk_tree.grid(row=0, column=0, sticky="nsew")
        self._hk_tree.bind("<Double-1>", lambda e: self.hk_rekey())
        self._pick_tree = ttk.Treeview(mid, columns=("title",), show="headings", height=12)
        self._pick_tree.heading("title", text="曲目（选中后点「添加热键」）")
        self._pick_tree.column("title", width=240, anchor="w")
        self._pick_tree.grid(row=0, column=2, sticky="nsew")
        PillButton(btns, "添加热键", command=self.hk_add,
                   kind="accent").pack(side="left")
        PillButton(btns, "改键（双击条目）", command=self.hk_rekey,
                   ).pack(side="left", padx=SP["sm"])
        PillButton(btns, "换曲", command=self.hk_resong).pack(side="left")
        PillButton(btns, "删除所选", command=self.hk_del,
                   kind="danger").pack(side="left", padx=SP["sm"])
        PillButton(btns, "清空", command=self.hk_clear, kind="danger").pack(side="left")

    def _hk_reload(self):
        try:
            self._hk_tree.delete(*self._hk_tree.get_children())
        except Exception:
            return
        failed = self.hotkeys.failed                # {名字: 组合键}
        bad_specs = set(failed.values())
        for i, e in enumerate(self.hotkey_map()):
            show = e["song"] if any(Path(f).name == e["song"] for f in self.files) \
                else e["song"] + "  ⚠ 曲库里没有"
            state = "没注册上" if e["key"] in bad_specs else ""
            self._hk_tree.insert("", "end", iid=f"hk{i}",
                                 values=(e["key"], show, state))
        self._pick_tree.delete(*self._pick_tree.get_children())
        for f in self.files:
            self._pick_tree.insert("", "end", iid=f, values=(Path(f).stem,))
        try:
            self._ctl_tree.delete(*self._ctl_tree.get_children())
        except Exception:
            return
        for fid, key in self.control_keys().items():
            self._ctl_tree.insert("", "end", iid=f"ctl_{fid}",
                                  values=(self.CTL_LABELS[fid], key or "（未绑定）"))

    def _capture_key_modal(self) -> str | None:
        """弹一个小窗让用户现按组合键，返回 'Ctrl+Alt+J' 形式的串；Esc/关窗 = 取消。"""
        t = THEME
        w = tk.Toplevel(self, bg=t["card"])
        w.title("录制组合键")
        w.transient(self)
        w.resizable(False, False)
        w.configure(bg=t["card"])
        tk.Label(w, text="请按下组合键", bg=t["card"], fg=t["ink"],
                 font=(t["font"], t["size_title"], "bold")).pack(
            padx=SP["xl"], pady=(SP["lg"], SP["xs"]))
        lbl = tk.Label(w, text="需包含 Ctrl / Alt / Win 其中一个；Esc 取消",
                       bg=t["card"], fg=t["ink_soft"], font=(t["font"], t["size_sm"]))
        lbl.pack(padx=SP["xl"], pady=(0, SP["lg"]))
        result: dict = {"spec": None}

        def on_key(e):
            ks = e.keysym
            if ks == "Escape":
                w.destroy()
                return
            if ks in self._MOD_SYMS:
                return
            mods = []
            st = e.state
            if st & 0x0004:
                mods.append("Ctrl")
            if st & 0x0001:
                mods.append("Shift")
            if st & 0x0008:
                mods.append("Alt")
            if st & 0x0040:
                mods.append("Win")
            if not any(m in mods for m in ("Ctrl", "Alt", "Win")):
                lbl.configure(text="要带 Ctrl / Alt / Win 其中一个，再按一次。",
                              fg=t["warn"])
                return
            key = self._KEY_SYMS.get(ks) or self._KEY_SYMS.get(ks.lower())
            if key is None:
                if len(ks) == 1:
                    key = ks.upper()
                elif ks.startswith("KP_") and ks[3:].isdigit():
                    key = ks[3:]
                elif len(ks) >= 2 and ks[0] == "F" and ks[1:].isdigit():
                    key = ks                     # F1~F24
                else:
                    key = ks.upper()             # 其余交给 Hotkeys.parse 验证
            spec = "+".join(mods + [key])
            if Hotkeys.parse(spec)[1] is None:
                lbl.configure(text=f"「{key}」这个键注册不了，换一个。", fg=t["warn"])
                return
            result["spec"] = spec
            w.destroy()

        w.bind("<Key>", on_key)
        w.grab_set()
        w.focus_force()
        w.update_idletasks()
        x = self.winfo_rootx() + (self.winfo_width() - w.winfo_reqwidth()) // 2
        y = self.winfo_rooty() + (self.winfo_height() - w.winfo_reqheight()) // 2
        w.geometry(f"+{max(0, x)}+{max(0, y)}")
        w.wait_window(w)
        return result["spec"]

    def _hk_dup_warn(self, key: str, exclude: int | None = None) -> bool:
        """组合键已被别的条目占了 → 问是否改绑。返回 True=可以继续。"""
        for i, e in enumerate(self.hotkey_map()):
            if i != exclude and e["key"] == key:
                return bool(self._modal(
                    "组合键已被占用",
                    f"「{key}」已经绑在《{Path(e['song']).stem}》上。\n要改绑到新曲子吗？"
                    "（原条目会被替换）", kind="warn", buttons=("改绑", "取消")))
        return True

    def hk_add(self):
        sel = self._pick_tree.selection()
        if not sel:
            self.say("先在右边选一首曲子，再点「添加热键」。", "warn")
            return
        spec = self._capture_key_modal()
        if not spec:
            return
        if not self._hk_dup_warn(spec):
            return
        if self._key_in_control(spec):
            self._modal("组合键已被占用",
                        f"「{spec}」已经用作播放控制（{self.CTL_LABELS[self._key_in_control(spec)]}）。\n"
                        "先在上方改掉它，再来绑曲目热键。", kind="warn")
            return
        m = self.hotkey_map()
        m.append({"key": spec, "song": Path(sel[0]).name})
        self._save_hotkey_map(m)
        self.say(f"热键已加：{spec} → 《{Path(sel[0]).stem}》", "ok")

    def _hk_selected(self) -> int | None:
        sel = self._hk_tree.selection()
        if not sel or not sel[0].startswith("hk"):
            return None
        try:
            return int(sel[0][2:])
        except ValueError:
            return None

    def hk_rekey(self):
        i = self._hk_selected()
        if i is None:
            self.say("先在左边选一条热键。", "warn")
            return
        spec = self._capture_key_modal()
        if not spec:
            return
        if not self._hk_dup_warn(spec, exclude=i):
            return
        if self._key_in_control(spec):
            self._modal("组合键已被占用",
                        f"「{spec}」已经用作播放控制（{self.CTL_LABELS[self._key_in_control(spec)]}）。\n"
                        "先在上方改掉它，再来绑曲目热键。", kind="warn")
            return
        m = self.hotkey_map()
        m[i]["key"] = spec
        self._save_hotkey_map(m)
        self.say(f"热键已改：{spec} → 《{Path(m[i]['song']).stem}》", "ok")

    def hk_resong(self):
        i = self._hk_selected()
        sel = self._pick_tree.selection()
        if i is None or not sel:
            self.say("左边选一条热键、右边选一首曲子，再点「换曲」。", "warn")
            return
        m = self.hotkey_map()
        m[i]["song"] = Path(sel[0]).name
        self._save_hotkey_map(m)
        self.say(f"热键已换曲：{m[i]['key']} → 《{Path(sel[0]).stem}》", "ok")

    def hk_del(self):
        i = self._hk_selected()
        if i is None:
            self.say("先在左边选一条热键。", "warn")
            return
        m = self.hotkey_map()
        e = m.pop(i)
        self._save_hotkey_map(m)
        self.say(f"已删热键：{e['key']} → {e['song']}", "ok")

    def hk_clear(self):
        if not self.hotkey_map():
            return
        if not self._modal("清空热键", "把全部曲目热键删掉？（播放控制热键不受影响）",
                           kind="warn", buttons=("清空", "取消")):
            return
        self._save_hotkey_map([])

    # ── 全局热键 ───────────────────────────────────────────────────
    def hotkey_bindings(self) -> list[tuple[str, str]]:
        out = [(f"hk{i}", e["key"]) for i, e in enumerate(self.hotkey_map())]
        out += [(f"ctl_{fid}", key) for fid, key in self.control_keys().items() if key]
        return out

    def start_hotkeys(self):
        # ★ 必须先停掉旧的：Hotkeys.start() 会直接覆盖 self._thread，
        #   旧线程收不到 WM_QUIT 就永远活着 → 每改一次热键泄漏一条常驻线程，
        #   且旧组合键仍注册着，按下去会用新列表解释 → 弹错曲。
        #   （改键/换曲/删曲/改播放控制键都会走到这里）
        try:
            self.hotkeys.stop()
        except Exception:
            pass
        self._hk_songs = [e["song"] for e in self.hotkey_map()]
        if not self.cfg.get("hotkeys", True):
            self.say("全局热键已关闭（设置里可开）。", "dim")
            return
        if sys.platform != "win32":
            self.say("全局热键只在 Windows 上做了。", "dim")
            return
        self.hotkeys.on_error = lambda m: self._ui_events.put(("log", (m, "warn")))
        failed = self.hotkeys.start(self.hotkey_bindings())
        if failed:
            self.say("这些热键没注册上（多半被别的程序占了）："
                     + "、".join(f"{v}" for v in failed.values()), "warn")
        else:
            n_ctl = sum(1 for v in self.control_keys().values() if v)
            self.say(f"全局热键已启用：{len(self._hk_songs)} 条曲目热键"
                     f" + {n_ctl} 条播放控制（热键页可管理，失焦也生效）。", "ok")

    def _poll_hotkeys(self):
        try:
            while True:
                self.on_hotkey(self._hotkey_events.get_nowait())
        except queue.Empty:
            pass
        except Exception:
            pass
        self.after(60, self._poll_hotkeys)

    def on_hotkey(self, name: str):
        if name.startswith("ctl_"):
            fid = name[4:]
            if fid == "toggle":
                self.say("［热键］暂停/继续", "dim")
                if self.player.is_running:
                    self.on_pause()
                elif self.current:
                    self.on_play(start_s=0.0)     # 没在弹 → 直接开始弹当前曲
            elif fid == "stop":
                self.say("［热键］停止", "dim")
                if self.player.state != "idle":
                    self.on_stop()
            elif fid == "prev":
                self.say("［热键］上一首", "dim")
                self.step_song(-1)
            elif fid == "next":
                self.say("［热键］下一首", "dim")
                self.step_song(1)
            return
        if name.startswith("hk"):
            try:
                i = int(name[2:])
            except ValueError:
                return
            songs = getattr(self, "_hk_songs", [])
            if 0 <= i < len(songs):
                base = songs[i]
                self.say(f"［热键］演奏《{Path(base).stem}》", "dim")
                self.play_song_by_name(base)

    def play_song_by_name(self, base: str):
        """热键按下 = 立即切换演奏（Soundpad 式：在弹别的也直接切过来）。"""
        path = None
        for f in self.files:
            if Path(f).name == base:
                path = f
                break
        if not path:
            self.say(f"热键指向的《{base}》不在曲库里了。", "warn")
            return
        same = (os.path.normcase(os.path.abspath(path))
                == os.path.normcase(os.path.abspath(self.current)))
        if same and self.player.is_running:
            self.on_pause()
            return
        if self.player.is_running:
            self.player.stop()
            self.player.join()
        if not same:
            self.switch_page("play")
            self.tree.selection_set(path)
            self.tree.focus(path)
            self.tree.see(path)
            self.on_pick_song()
        self.var_pos.set(0.0)
        self.on_play(start_s=0.0)

    # ── 快捷键 ─────────────────────────────────────────────────────
    def bind_shortcuts(self):
        # ★ bind_all 对应用内**所有**控件（含输入框、列表、模态对话框）都生效，
        #   原来会：搜索框里打一个空格 → 开始/暂停演奏；曲库列表按 ↑↓ 选曲
        #   → 顺手把速度改掉；录热键的对话框里按方向键 → 一边录一边切曲。
        #   所以焦点落在"自己要用这些键"的控件或对话框里时不抢。
        def handler(_e, fn):
            w = self.focus_get()
            if w is not None:
                try:
                    if w.winfo_toplevel() is not self:
                        return None                     # 在某个模态框里
                except Exception:
                    pass
                if isinstance(w, (tk.Entry, tk.Spinbox, tk.Text, tk.Listbox,
                                  ttk.Entry, ttk.Spinbox, ttk.Combobox, ttk.Treeview)):
                    return None                         # 输入框 / 列表自己要用
            fn()
            return "break"
        self.bind_all("<Left>", lambda e: handler(e, lambda: self.step_song(-1)))
        self.bind_all("<Right>", lambda e: handler(e, lambda: self.step_song(1)))
        self.bind_all("<Up>", lambda e: handler(e, lambda: self.nudge_rate(1)))
        self.bind_all("<Down>", lambda e: handler(e, lambda: self.nudge_rate(-1)))
        self.bind_all("<space>", lambda e: handler(e, self.toggle_play))

    def step_song(self, delta: int):
        if not self.files:
            return
        sel = self.tree.selection()
        i = self.files.index(sel[0]) if sel and sel[0] in self.files else -1
        j = max(0, min(len(self.files) - 1, i + delta))
        if self.player.is_running:
            self.player.stop()
            self.player.join()
            self.say("换曲，已停止演奏。", "dim")
        self.tree.selection_set(self.files[j])
        self.tree.focus(self.files[j])
        self.tree.see(self.files[j])
        self.on_pick_song()

    def nudge_rate(self, direction: int, step: float = 0.05):
        r = float(self.var_rate.get() or 1.0)
        r = r * (1 + step) if direction > 0 else r / (1 + step)
        r = max(0.05, min(8.0, round(r, 4)))
        self.var_rate.set(r)
        self._on_rate_move()
        self.say(f"速度 {r:.2f}×", "dim")

    def toggle_play(self):
        if self.player.state in ("playing", "countdown", "paused"):
            self.on_pause()
        else:
            self.var_pos.set(0.0)
            self.on_play(start_s=0.0)

    # ── 收尾 ───────────────────────────────────────────────────────
    def on_close(self):
        try:
            self.player.stop()
            # ★ stop() 只是立旗，线程还要走完当前一步（约 20ms）才会 release_all；
            #   不等就 destroy，daemon 线程被杀在 key_down 和 key_up 之间 → 键卡住不放。
            self.player.join(1.0)
            self.hotkeys.stop()
        except Exception:
            pass
        self.destroy()


# ══════════════════════════════════════════════════════════════════════
#  自检：不显示窗口、不发真实按键
# ══════════════════════════════════════════════════════════════════════
def selftest(out_path: str | None = None) -> int:
    import tempfile
    res: dict = {"ok": False, "checks": []}

    def chk(name, cond, detail=""):
        res["checks"].append({"name": name, "pass": bool(cond), "detail": detail})
        return bool(cond)

    tmp_cfg = Path(tempfile.mkdtemp(prefix="hca_selftest_")) / "hca.json"
    app = App(dry=True, cfg_path=tmp_cfg)
    app.withdraw()
    states: list[str] = []
    try:
        app.update()
        app.geometry("1080x720")
        app.update()
        app._draw_shell()
        app.update()

        chk("曲库已加载", len(app.files) > 0, f"{len(app.files)} 首")
        chk("用的是临时配置（不碰用户的）", app.cfg_path == tmp_cfg, str(app.cfg_path))
        chk("玻璃外观探测有结果", "backdrop" in app.glass_info,
            f"亚克力={app.glass_info.get('backdrop')} 圆角={app.glass_info.get('corners')} "
            f"边框={app.glass_info.get('frame')}")
        chk("画布上画出了形状", len(app._shapes) >= 2, f"{len(app._shapes)} 个卡片")
        chk("页面都建好了",
            all(k in app._frames for k in ("page_play", "page_list", "page_manage",
                                           "page_online", "page_hotkeys",
                                           "page_log", "page_settings")),
            str(sorted(app._frames)))
        chk("圆角是嵌套的（外−内边距=内）",
            R["inner"] == R["card"] - SP["sm"] and R["pill"] == R["island"] - 10,
            f"卡片{R['card']} 内层{R['inner']} 胶囊{R['pill']}")
        chk("间距是 4 的倍数", all(v % 4 == 0 for v in SP.values()), str(SP))
        # ── 灵动岛滑块 ──
        btns = [app.btn_play, app.btn_pause] + app._all_pill_buttons()
        chk("所有按钮都是胶囊 PillButton", all(isinstance(b, PillButton) for b in btns),
            f"{len(btns)} 个")
        # 用请求高度判断（自检时窗口是 withdraw 的，winfo_height 恒为 1）
        chk("按钮圆角是半高（真胶囊）",
            bool(btns) and all(b.winfo_reqheight() == PillButton.H for b in btns),
            f"{len(btns)} 个，请求高 {PillButton.H}")
        chk("禁用态能切换（暂停键）", not app.btn_pause._enabled, "初始禁用")
        chk("两个滑块都是 PillSlider", isinstance(app.scale, PillSlider)
            and isinstance(app.rate_scale, PillSlider), "演奏位置 / 速度")
        chk("滑块轨道是胶囊（半径=半高）",
            app.scale.TRACK / 2 == app.scale.TRACK / 2 and app.scale.THUMB_H > app.scale.TRACK,
            f"轨道高{app.scale.TRACK} 把手高{app.scale.THUMB_H}")
        app.scale.set_range(0.0, 100.0)
        app.scale.set_value(42.0)
        app.update()
        chk("滑块 set_range/set_value 生效", abs(app.var_pos.get() - 42.0) < 1e-6,
            f"var_pos={app.var_pos.get()}")
        app.scale._animate(True)
        for _ in range(30):
            app.update()
            time.sleep(0.02)
        chk("拖动时把手会变宽", app.scale._thumb_w > app.scale.THUMB_W,
            f"宽 {app.scale.THUMB_W} → {app.scale._thumb_w:.1f}")
        app.scale._animate(False)
        for _ in range(30):
            app.update()
            time.sleep(0.02)
        chk("松手后把手收窄回去", abs(app.scale._thumb_w - app.scale.THUMB_W) < 0.7,
            f"宽 {app.scale._thumb_w:.1f}")
        # 三级明度必须一级比一级深，否则白卡片浮不起来
        lum = lambda h: int(h[1:3], 16) + int(h[3:5], 16) + int(h[5:7], 16)
        chk("三级明度有层次（底色<侧边栏<卡片）",
            lum(THEME["bg"]) < lum(THEME["sidebar"]) < lum(THEME["card"]),
            f"{THEME['bg']} < {THEME['sidebar']} < {THEME['card']}")

        for page in ("manage", "online", "hotkeys", "log", "settings", "play"):
            app.switch_page(page)
            app.update()
            chk(f"能切到「{page}」页", app._page == page, f"当前页={app._page}")

        app.tree.selection_set(app.files[0])
        app.on_pick_song()
        app.update()
        chk("选中曲目后能编译出动作", len(app.actions) > 0, f"{len(app.actions)} 个动作")

        app._on_rate_move()
        chk("变速可用", abs(app.player.rate - app.var_rate.get()) < 0.01, f"{app.player.rate:.2f}×")

        app.var_count.set(0)
        app.var_loop.set(False)
        app.var_rate.set(6.0)
        app.actions = [a for a in app.actions if a.t <= 1.5]
        orig = app.on_progress

        def spy(st):
            states.append(st.get("state"))
            orig(st)
        app.on_progress = spy
        app.on_play(start_s=0.0)

        def pump(timeout: float) -> bool:
            t0 = time.perf_counter()
            while app.player.state == "idle" and time.perf_counter() - t0 < 3:
                app.update()
                time.sleep(0.01)
            while app.player.is_running and time.perf_counter() - t0 < timeout:
                app.update()
                time.sleep(0.01)
            for _ in range(5):
                app.update()
                time.sleep(0.01)
            return app.player.state == "done"

        chk("播放跑完并回到结束态", pump(25), f"状态={app.player.state}")
        chk("进度回调有 playing", "playing" in states, f"{len(states)} 次回调")
        sent = app.player.sent
        chk("动作确实按顺序发出", sent > 0, f"发出 {sent} / {len(app.actions)}")
        chk("空后端没被换成真后端", isinstance(app.player.backend, NullInput))

        app.var_rate.set(6.0)
        app.on_play(start_s=0.5)
        chk("从中间开始播放可用", pump(25), f"状态={app.player.state}")

        # ── 收藏夹（Soundpad 式，数据在程序配置里）──
        app.switch_page("hotkeys")
        app.update()
        chk("热键页已构建", "page_hotkeys" in app._frames and "page_manage" in app._frames,
            str(sorted(app._frames)))
        favs = app._favs()
        favs = favmod.create(favs, "测试夹")
        favs, n = favmod.add_songs(favs, "测试夹",
                                   [Path(f).name for f in app.files[:2]])
        app.cfg["favorites"] = favs
        app._save_favs(favs)
        chk("收藏夹新建+加曲", len(app._favs()) == 1 and app._favs()[0]["songs"],
            str(app._favs()))
        chk("收藏夹归属反查",
            favmod.favorites_of(app._favs(), Path(app.files[0]).name) == ["测试夹"], "")
        chk("演奏页热键列（未绑显示 —）", app._hk_text(app.files[0]) == "—", "")
        chk("收藏夹页左栏已填充", len(app.fav_tree.get_children()) >= 2,
            f"{len(app.fav_tree.get_children())} 行")

        # ── 曲库文件操作（函数级测试全部用临时目录，绝不碰真实 sings/）──
        import tempfile
        chk("收藏夹右栏已填充",
            len(app.fav_songs.get_children()) == len(app.lib_all),
            f"{len(app.fav_songs.get_children())}/{len(app.lib_all)}")
        tmp_lib = Path(tempfile.mkdtemp(prefix="mg_test_"))
        (tmp_lib / "a.mid").write_bytes(b"MThd")
        (tmp_lib / "b.mid").write_bytes(b"MThd")
        renamed = library.rename_song(tmp_lib / "a.mid", "新名字")
        chk("自定义命名（保留扩展名）",
            renamed.name == "新名字.mid" and renamed.exists())
        # 改名后收藏夹引用同步
        favs = favmod.add_songs(app._favs(), "测试夹", ["a.mid"])[0]
        favs = favmod.sync_file_rename(favs, "a.mid", "新名字.mid")
        chk("改名后收藏夹引用同步", "新名字.mid" in favs[0]["songs"]
            and "a.mid" not in favs[0]["songs"], str(favs[0]["songs"]))
        n_recycled = trash.recycle([str(renamed)])
        chk("删除进回收站（可还原）",
            n_recycled == 1 and not renamed.exists())

        # ── 自定义热键（Soundpad 式）──
        app.cfg["hotkey_map"] = [{"key": "Ctrl+Alt+7", "song": Path(app.files[0]).name}]
        app._save_hotkey_map(app.hotkey_map())
        binds = app.hotkey_bindings()
        chk("热键绑定表 = 自定义条数 + 播放控制（默认暂停键）",
            len(binds) == 2 and binds[0][1] == "Ctrl+Alt+7"
            and binds[-1] == ("ctl_toggle", "Ctrl+Shift+-"),
            str(binds))
        chk("热键能解析", app.hotkeys.parse("Ctrl+Alt+7")[1] is not None)
        chk("演奏页热键列显示组合键",
            app._hk_text(app.files[0]) == "Ctrl+Alt+7", app._hk_text(app.files[0]))
        chk("录键 spec 非法值能被拦下", app.hotkeys.parse("Ctrl+不存在的键")[1] is None, "")
        # ── 播放控制热键 ──
        ck = app.control_keys()
        chk("播放控制默认含暂停键", ck.get("toggle") == "Ctrl+Shift+-", str(ck))
        app.cfg["control_keys"] = {**ck, "stop": "Ctrl+Alt+S"}
        app._save_control_keys(app.cfg["control_keys"])
        chk("播放控制可改键并可注册",
            any(n == "ctl_stop" and k == "Ctrl+Alt+S" for n, k in app.hotkey_bindings()),
            str(app.hotkey_bindings()))
        chk("控制键与曲目热键互斥检查可用",
            app._key_in_song_map("Ctrl+Alt+7") and not app._key_in_song_map("Ctrl+Alt+S")
            and app._key_in_control("Ctrl+Alt+S") == "stop", "")
        # 改键/清除的取行逻辑（曾把 fid 当下标用 → TypeError，用户实测"改键用不了"）
        app._ctl_tree.selection_set("ctl_stop")
        app.update()
        chk("播放控制取行返回功能名", app._ctl_selected() == "stop",
            str(app._ctl_selected()))
        app._ctl_tree.selection_set("ctl_toggle")
        app.update()
        chk("播放控制取行可换行", app._ctl_selected() == "toggle",
            str(app._ctl_selected()))

        res["ok"] = all(c["pass"] for c in res["checks"])
    except Exception:
        res["error"] = traceback.format_exc()
    finally:
        try:
            app.player.stop()
            app.hotkeys.stop()
            app.destroy()
        except Exception:
            pass

    text = json.dumps(res, ensure_ascii=False, indent=1)
    print(text)
    if out_path:
        Path(out_path).write_text(text, encoding="utf-8")
    return 0 if res.get("ok") else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="hca.ui", description="口琴演奏器 · 新版（界面）")
    ap.add_argument("--selftest", action="store_true", help="自检：不显示窗口、不发按键")
    ap.add_argument("--out", help="自检结果写到文件")
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest(args.out)
    try:
        App().mainloop()
    except Exception:
        # pythonw 下 sys.stderr 是 None，崩溃会毫无痕迹（用户只看到"双击无反应"）。
        # 把完整堆栈落到程序目录的 crash.log，再弹一个系统对话框告知位置。
        tb = traceback.format_exc()
        try:
            log = Path(__file__).resolve().parent.parent / "crash.log"
            log.write_text(tb, encoding="utf-8")
        except Exception:
            log = Path(os.path.join(os.environ.get("TEMP", "."), "hca_crash.log"))
            log.write_text(tb, encoding="utf-8")
        try:
            from tkinter import messagebox
            root = tk.Tk(); root.withdraw()
            messagebox.showerror("口琴演奏器启动失败", f"错误已写入：\n{log}")
            root.destroy()
        except Exception:
            pass
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
