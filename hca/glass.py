"""窗口外观 —— 亚克力/云母背景 + 原生圆角。**只操作自己的窗口，只碰外观。**

为什么单独成一个文件：
    项目底线规定"Windows API 只在指定文件里调"。为了做**真正的背景模糊**
    （苹果那种玻璃质感），这里要新增 `dwmapi`，所以白名单里多加了一条 ——
    见 `verify/redline_check.py`。允许的理由写在那里，也写在这里：

      · 只调 `DwmSetWindowAttribute` / `DwmExtendFrameIntoClientArea`
      · 只作用在**我们自己的 hwnd** 上，从不找/改别的窗口
      · 是纯外观 API：不读内存、不写内存、不起线程、不注入、与反作弊无关
      · 它不会让程序"更像自动化"或"更不容易被发现"—— 与底线②无关

实测（本机 Windows 11 24H2 build 26100）：
    Mica / Acrylic / MicaAlt / 原生圆角 —— HRESULT 全为 0，都支持。
"""

from __future__ import annotations

import ctypes
import sys

# DWMWA_SYSTEMBACKDROP_TYPE 的取值
BACKDROP_AUTO, BACKDROP_NONE, BACKDROP_MICA, BACKDROP_ACRYLIC, BACKDROP_MICA_ALT = 0, 1, 2, 3, 4
# DWMWA_WINDOW_CORNER_PREFERENCE 的取值
CORNER_DEFAULT, CORNER_DONOTROUND, CORNER_ROUND, CORNER_ROUNDSMALL = 0, 1, 2, 3

DWMWA_USE_IMMERSIVE_DARK_MODE = 20
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWA_SYSTEMBACKDROP_TYPE = 38
DWMWA_CAPTION_COLOR = 35            # Win11 22H2+：直接指定标题栏底色
DWMWA_BORDER_COLOR = 34             # 窗口边框颜色

BACKDROPS = {"none": BACKDROP_NONE, "auto": BACKDROP_AUTO,
             "mica": BACKDROP_MICA, "acrylic": BACKDROP_ACRYLIC, "mica_alt": BACKDROP_MICA_ALT}


def _colorref(hex_color: str) -> int:
    """#RRGGBB → COLORREF（0x00BBGGRR，注意字节序是反的）。"""
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return r | (g << 8) | (b << 16)


class _MARGINS(ctypes.Structure):
    _fields_ = [("left", ctypes.c_int), ("right", ctypes.c_int),
                ("top", ctypes.c_int), ("bottom", ctypes.c_int)]


def _hwnd_of(window) -> int | None:
    """拿到顶层窗口句柄。Tk 的 winfo_id() 给的是子窗口，真顶层是它的 parent。"""
    try:
        hwnd = int(window.winfo_id())
        user32 = ctypes.windll.user32
        parent = user32.GetParent(ctypes.c_void_p(hwnd))
        return int(parent) if parent else hwnd
    except Exception:
        return None


def apply(window, backdrop: str = "acrylic", dark: bool = False,
          extend_frame: bool = True, round_corners: bool = True,
          caption_color: str | None = None) -> dict:
    """给窗口套上玻璃外观。返回每一项的 HRESULT，方便自检断言。

    backdrop     : "acrylic"（最像液态玻璃）/ "mica"（更静态、更省电）/ "none"
    extend_frame : 把边框延伸到整个客户区 —— **这是让背景模糊真的露出来的关键**。
                   不打开的话，Tk 会把客户区涂满，模糊被挡住看不见。
                   打开后，客户区里"黑色"的部分会变成玻璃（见 THEME 里的 glass_bg）。
    """
    res: dict = {"platform": sys.platform, "applied": False,
                 "backdrop": None, "corners": None, "frame": None, "dark": None}
    if sys.platform != "win32":
        res["platform"] = "非 Windows，跳过"
        return res
    hwnd = _hwnd_of(window)
    if not hwnd:
        return res
    res["hwnd"] = hwnd
    try:
        dwm = ctypes.windll.dwmapi

        def setint(attr: int, value: int):
            arg = ctypes.c_int(value)
            return int(dwm.DwmSetWindowAttribute(
                ctypes.c_void_p(hwnd), ctypes.c_int(attr),
                ctypes.byref(arg), ctypes.sizeof(arg)))

        if round_corners:
            res["corners"] = setint(DWMWA_WINDOW_CORNER_PREFERENCE, CORNER_ROUND)
        res["dark"] = setint(DWMWA_USE_IMMERSIVE_DARK_MODE, 1 if dark else 0)
        # ★ 标题栏颜色：用户开了"标题栏用系统强调色"时，标题栏会被染成强调色
        #   （实测这台机器是紫 (118,111,155)，跟浅色主题打架）。
        #   DWMWA_CAPTION_COLOR 能直接盖过它，让它跟画布底色一致。
        if caption_color:
            res["caption"] = setint(DWMWA_CAPTION_COLOR, _colorref(caption_color))
            setint(DWMWA_BORDER_COLOR, _colorref(caption_color))
        key = (backdrop or "none").lower()
        backdrop_ok = False
        if key in BACKDROPS and BACKDROPS[key] != BACKDROP_NONE:
            res["backdrop"] = setint(DWMWA_SYSTEMBACKDROP_TYPE, BACKDROPS[key])
            backdrop_ok = res["backdrop"] == 0
        # ★ 只有在背景模糊真的启用之后才延伸边框。
        #   否则客户区的"透明"部分会露出**纯黑**，比纯色底还难看。
        if extend_frame and backdrop_ok:
            m = _MARGINS(-1, -1, -1, -1)
            res["frame"] = int(dwm.DwmExtendFrameIntoClientArea(
                ctypes.c_void_p(hwnd), ctypes.byref(m)))
        res["applied"] = backdrop_ok
    except Exception as e:                       # noqa: BLE001
        res["error"] = f"{type(e).__name__}: {e}"
    return res
