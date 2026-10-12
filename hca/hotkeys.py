"""全局热键 —— 程序在后台、没有焦点时也照样触发。

为什么必须单开一条线程：RegisterHotKey 注册的热键消息只会投递到**注册它的那条线程**
的消息队列里，所以那条线程必须一直在跑 GetMessageW 循环。界面线程在跑 Tk 的
mainloop，不能同时跑这个循环 —— 于是"注册 + 收消息"整条链放进独立线程，
收到的事件走 queue 交回主线程（主线程不能从别的线程直接碰控件）。

退出用 PostThreadMessageW(WM_QUIT)，比让线程自己超时轮询干净。
"""

from __future__ import annotations

import ctypes
import queue
import sys
import threading
from ctypes import wintypes
from typing import Callable

MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x0001, 0x0002, 0x0004, 0x0008, 0x4000
WM_HOTKEY, WM_QUIT = 0x0312, 0x0012

# 虚拟键码表：字符 → VK。数字/字母/OEM 符号/F1~F24/空格
VK: dict[str, int] = {
    "-": 0xBD, "=": 0xBB, "[": 0xDB, "]": 0xDD, "\\": 0xDC,
    ";": 0xBA, "'": 0xDE, ",": 0xBC, ".": 0xBE, "/": 0xBF, "`": 0xC0,
    " ": 0x20, "space": 0x20,
}
for _c in "0123456789":
    VK[_c] = 0x30 + int(_c)
for _c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
    VK[_c] = ord(_c)
    VK[_c.lower()] = ord(_c)
for _i in range(1, 25):
    VK[f"F{_i}"] = 0x6F + _i


class Hotkeys:
    """系统级热键管理器。"""

    def __init__(self, on_fire: Callable[[str], None] | None = None,
                 on_error: Callable[[str], None] | None = None):
        self.on_fire = on_fire or (lambda name: None)
        self.on_error = on_error or (lambda msg: None)
        self.events: queue.Queue[str] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._failed: dict[str, str] = {}

    @staticmethod
    def parse(spec: str) -> tuple[int | None, int | None]:
        """'Ctrl+Shift+1' → (mods, vk)。解析不了返回 (None, None)。"""
        if not isinstance(spec, str):
            return None, None
        mods, vk = 0, None
        for part in str(spec).split("+"):
            p = part.strip()
            low = p.lower()
            if low in ("ctrl", "control"):
                mods |= MOD_CONTROL
            elif low == "shift":
                mods |= MOD_SHIFT
            elif low == "alt":
                mods |= MOD_ALT
            elif low in ("win", "super"):
                mods |= MOD_WIN
            else:
                vk = VK.get(p) or VK.get(p.upper()) or VK.get(low)
                if vk is None:
                    return None, None
        if vk is None:
            return None, None
        return mods | MOD_NOREPEAT, vk

    @property
    def failed(self) -> dict[str, str]:
        """没注册上的：{名字: 快捷键}。多半是被别的程序占了。"""
        return dict(self._failed)

    def start(self, bindings: list[tuple[str, str]]) -> dict[str, str]:
        """bindings: [(名字, 'Ctrl+Shift+1'), ...]。返回注册失败的字典。"""
        if sys.platform != "win32":
            self._failed = {n: s for n, s in bindings}
            return self.failed
        # 兜底：调用方忘了先 stop() 也不会泄漏常驻线程 / 留下旧绑定
        if self._thread is not None and self._thread.is_alive():
            self.stop()
        self._failed = {}
        self._stop.clear()
        ready = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(bindings, ready), daemon=True)
        self._thread.start()
        ready.wait(2.0)
        return self.failed

    def stop(self) -> None:
        self._stop.set()
        if sys.platform == "win32" and self._thread and self._thread.is_alive():
            try:
                user32 = ctypes.WinDLL("user32", use_last_error=True)
                user32.PostThreadMessageW.argtypes = (wintypes.DWORD, wintypes.UINT,
                                                      wintypes.WPARAM, wintypes.LPARAM)
                user32.PostThreadMessageW(self._thread.ident, WM_QUIT, 0, 0)
            except Exception:
                pass
        if self._thread:
            self._thread.join(1.5)
            self._thread = None

    def _run(self, bindings: list[tuple[str, str]], ready: threading.Event) -> None:
        try:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.RegisterHotKey.argtypes = (wintypes.HWND, ctypes.c_int, wintypes.UINT,
                                              wintypes.UINT)
            user32.RegisterHotKey.restype = wintypes.BOOL
            user32.UnregisterHotKey.argtypes = (wintypes.HWND, ctypes.c_int)
            user32.GetMessageW.argtypes = (ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                                           wintypes.UINT, wintypes.UINT)
        except Exception as e:
            ready.set()
            self.on_error(f"热键线程初始化失败：{e}")
            return

        idmap: dict[int, str] = {}
        for hid, (name, spec) in enumerate(bindings, 1):
            mods, vk = self.parse(spec)
            if mods is None:
                self._failed[name] = spec
                continue
            if not user32.RegisterHotKey(None, hid, mods, vk):
                self._failed[name] = spec
                self.on_error(f"热键 {spec} 注册失败（错误码 {ctypes.get_last_error()}）"
                              f"—— 多半被别的程序占了")
                continue
            idmap[hid] = name
        ready.set()

        msg = wintypes.MSG()
        try:
            while not self._stop.is_set():
                r = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if r in (0, -1):
                    break
                if msg.message == WM_HOTKEY:
                    name = idmap.get(int(msg.wParam))
                    if name:
                        self.events.put(name)
                        try:
                            self.on_fire(name)
                        except Exception:
                            pass
        except Exception as e:
            self.on_error(f"热键线程异常：{e}")
        finally:
            for hid in idmap:
                try:
                    user32.UnregisterHotKey(None, hid)
                except Exception:
                    pass
