"""输入注入层 —— 把"按一下键 / 按住鼠标键"变成系统级真实输入事件。

★★ 本项目**硬底线**：只允许模拟键鼠，**禁止任何修改内存的操作**。
   本模块（和 `hotkeys.py`）是全项目**唯一**允许直接调 Windows API 的地方，
   而且只允许 `user32`。这条由 `verify/redline_check.py` 自动检查，不是靠自觉。

三个后端走同一个接口，方便切换与测试：
  * InputBackend —— 抽象基类
  * NullInput    —— 只记录、不发送（预演 / dry-run / 自动化测试用）
  * Win32Input   —— 真正干活：ctypes 调 user32.SendInput

★ 两个必须记住的坑（都是原程序踩过并写进注释的）：
  1. 键盘要发**扫描码**（KEYEVENTF_SCANCODE），不是虚拟键码 —— 游戏读 e.code（物理位置）。
  2. INPUT 结构体必须按 **MOUSEINPUT 的大小**来算（x64 上 40 字节）。只放 KEYBDINPUT
     的话结构只有 24 字节，SendInput 的 cbSize 校验不过，会**静默返回 0**，
     看起来就像"按键没生效"。
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from typing import Callable

from .mapping import Mapping

__all__ = ["InputBackend", "NullInput", "Win32Input", "make_backend", "tap_combo"]

INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
KEYEVENTF_KEYUP, KEYEVENTF_SCANCODE = 0x0002, 0x0008
# 鼠标事件标志：按下 / 抬起
MOUSEEVENTF = {"left": (0x0002, 0x0004), "right": (0x0008, 0x0010), "middle": (0x0020, 0x0040)}


class InputBackend:
    """抽象后端。"""

    def __init__(self, mapping: Mapping):
        self.mapping = mapping

    def key_down(self, ch: str) -> None:
        raise NotImplementedError

    def key_up(self, ch: str) -> None:
        raise NotImplementedError

    def mouse_down(self, mod: str) -> None:
        raise NotImplementedError

    def mouse_up(self, mod: str) -> None:
        raise NotImplementedError

    def release_all(self) -> None:
        """把手上的东西全部放开（保险动作，防止卡键）。"""
        for k in self.mapping.keys:
            try:
                self.key_up(k.key)
            except Exception:
                pass
        for mod in self.mapping.modifiers:
            try:
                self.mouse_up(mod)
            except Exception:
                pass

    def close(self) -> None:
        pass


class NullInput(InputBackend):
    """只记录、不发送。预演模式与自动化测试的基础。"""

    def __init__(self, mapping: Mapping | None = None):
        super().__init__(mapping or Mapping())
        self.log: list[tuple[str, str]] = []

    def key_down(self, ch: str) -> None:
        self.log.append(("keydown", ch))

    def key_up(self, ch: str) -> None:
        self.log.append(("keyup", ch))

    def mouse_down(self, mod: str) -> None:
        self.log.append(("mousedown", mod))

    def mouse_up(self, mod: str) -> None:
        self.log.append(("mouseup", mod))


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_void_p)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_void_p)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


class Win32Input(InputBackend):
    """SendInput 注入（Windows）。"""

    def __init__(self, mapping: Mapping | None = None, park_cursor: bool = False):
        super().__init__(mapping or Mapping())
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
        self.user32.SendInput.restype = wintypes.UINT
        self.park_cursor = park_cursor
        self._saved_pos: tuple[int, int] | None = None

    # ── 光标归位（原程序文档里承诺过、但代码里没实现的能力）─────────────
    def park(self) -> None:
        """把鼠标挪到屏幕一个不响应点击的角落，并记住原位置。

        为什么需要：鼠标左/中/右键在游戏里是移调键，按下它们的同时也是一次真实点击；
        如果光标正好停在琴孔上，就会多弹出几个不该有的音。
        """
        if not self.park_cursor:
            return
        pt = wintypes.POINT()
        self.user32.GetCursorPos(ctypes.byref(pt))
        self._saved_pos = (pt.x, pt.y)
        self.user32.SetCursorPos(0, 0)

    def unpark(self) -> None:
        if self.park_cursor and self._saved_pos:
            self.user32.SetCursorPos(*self._saved_pos)
            self._saved_pos = None

    # ── 发送 ────────────────────────────────────────────────────────
    def _send(self, inp: INPUT) -> None:
        sent = self.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
        if sent != 1:
            raise OSError(f"SendInput 失败：错误码 {ctypes.get_last_error()}")

    def _key(self, ch: str, up: bool) -> None:
        scan = self.mapping.scan_of(ch.upper())
        if scan is None:
            raise KeyError(f"映射里没有这个键：{ch!r}")
        flags = KEYEVENTF_SCANCODE | (KEYEVENTF_KEYUP if up else 0)
        inp = INPUT(type=INPUT_KEYBOARD)
        inp.u.ki = KEYBDINPUT(0, scan, flags, 0, None)
        self._send(inp)

    def key_down(self, ch: str) -> None:
        self._key(ch, False)

    def key_up(self, ch: str) -> None:
        self._key(ch, True)

    def _mouse(self, mod: str, up: bool) -> None:
        pair = MOUSEEVENTF.get(mod)
        if pair is None:
            raise KeyError(f"未知鼠标键：{mod!r}")
        inp = INPUT(type=INPUT_MOUSE)
        inp.u.mi = MOUSEINPUT(0, 0, 0, pair[1 if up else 0], 0, None)
        self._send(inp)

    def mouse_down(self, mod: str) -> None:
        self._mouse(mod, False)

    def mouse_up(self, mod: str) -> None:
        self._mouse(mod, True)


def make_backend(mapping: Mapping, dry_run: bool = False, park_cursor: bool = False) -> InputBackend:
    if dry_run:
        return NullInput(mapping)
    if sys.platform != "win32":
        raise RuntimeError("真实输入注入目前只做了 Windows；其他系统请用 --dry-run 看演奏计划")
    return Win32Input(mapping, park_cursor=park_cursor)


def tap_combo(spec: str, mapping: Mapping | None = None) -> bool:
    """真的按一下某个组合键（如 Ctrl+Shift+3），返回是否成功。

    给"验证全局热键"用：注册成功 ≠ 按下去有反应，得真发一次才算数。
    单独建一套结构体不是冗余 —— 见本模块开头的坑 2。
    """
    if sys.platform != "win32":
        return False
    from .hotkeys import Hotkeys

    mods, vk = Hotkeys.parse(spec)
    if mods is None or vk is None:
        return False
    # 修饰键：MOD_* 标志位 → 虚拟键码；按下顺序固定，抬起时反序
    pairs = [(0x0002, 0x11), (0x0004, 0x10), (0x0001, 0x12), (0x0008, 0x5B)]
    holding = [code for flag, code in pairs if mods & flag]

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
    user32.SendInput.restype = wintypes.UINT

    def send(code: int, up: bool) -> bool:
        inp = INPUT(type=INPUT_KEYBOARD)
        inp.u.ki = KEYBDINPUT(code, 0, KEYEVENTF_KEYUP if up else 0, 0, None)
        return bool(user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT)))

    try:
        seq = [(c, False) for c in holding] + [(vk, False), (vk, True)]
        seq += [(c, True) for c in reversed(holding)]
        return all(send(code, up) for code, up in seq)
    except Exception:
        return False
