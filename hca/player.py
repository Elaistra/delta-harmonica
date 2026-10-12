"""播放调度器 —— 可暂停 / 可停止 / 能报进度 / **播放中可变速**。

时间基准的设计（照搬原程序、也是本层最关键的一点）：
    动作表里的时刻永远是「**1.0× 速度下的秒数**」，
    速度只在调度时除一下（见 _wait_until）。
好处：按 ↑↓ 变速不用重新编译动作表，播放中也能立刻生效。
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Iterable

from .inputs import InputBackend
from .model import Action

IDLE, COUNTDOWN, PLAYING, PAUSED, DONE = "idle", "countdown", "playing", "paused", "done"
RATE_MIN, RATE_MAX = 0.05, 8.0        # 内核能力范围（界面可以再收窄）

__all__ = ["Player", "IDLE", "COUNTDOWN", "PLAYING", "PAUSED", "DONE", "RATE_MIN", "RATE_MAX"]


class Player:
    def __init__(self, backend: InputBackend, on_progress: Callable[[dict], None] | None = None,
                 on_log: Callable[[str], None] | None = None):
        self.backend = backend
        self.on_progress = on_progress or (lambda st: None)
        self.on_log = on_log or (lambda msg: None)
        self._stop = threading.Event()
        self._pause = threading.Event()
        self._state = IDLE
        self._t0 = 0.0
        self._rate = 1.0
        self._sent = 0
        self._total = 0
        self._duration = 0.0

    # ── 状态 ────────────────────────────────────────────────────────
    @property
    def state(self) -> str:
        return self._state

    @property
    def rate(self) -> float:
        return self._rate

    @property
    def is_running(self) -> bool:
        return self._state in (COUNTDOWN, PLAYING, PAUSED)

    @property
    def sent(self) -> int:
        return self._sent

    @property
    def elapsed(self) -> float:
        """已经弹到第几秒（**基准速度**下的秒数，和动作表同一口径）。"""
        if self._state not in (PLAYING, PAUSED):
            return 0.0
        return max(0.0, (time.perf_counter() - self._t0) * self._rate)

    def set_rate(self, rate: float) -> float:
        """改速度。**正在播也能改** —— 靠把时间基准重锚到"当前位置"。

        不做这件事的话，改成 0.5× 会把已经弹过的那一段又重弹一遍。
        """
        rate = max(RATE_MIN, min(RATE_MAX, float(rate)))
        if abs(rate - self._rate) > 1e-9 and self._state in (PLAYING, COUNTDOWN):
            played = max(0.0, (time.perf_counter() - self._t0) * self._rate)   # 基准秒
            self._t0 = time.perf_counter() - played / rate
        self._rate = rate
        self._emit(self._state)
        return rate

    def pause(self) -> None:
        if self._state == PLAYING:
            self._state = PAUSED
            self._pause.set()
            self._quiet()
            self._emit(PAUSED)

    def resume(self) -> None:
        if self._state == PAUSED:
            self._pause.clear()
            self._state = PLAYING

    def toggle_pause(self) -> None:
        if self._state == PLAYING:
            self.pause()
        elif self._state == PAUSED:
            self.resume()

    def stop(self) -> None:
        """只立旗；线程要等当前这一步走完（最多一次 _wait_until，约 20ms）才真的退出。"""
        self._stop.set()
        self._pause.clear()

    def join(self, timeout: float = 3.0) -> bool:
        """等它真的停下来。★ 不等就接着开播，会被"已在播"挡掉，且看不出报错。"""
        t = time.perf_counter()
        while self.is_running and time.perf_counter() - t < timeout:
            time.sleep(0.01)
        return not self.is_running

    # ── 内部 ────────────────────────────────────────────────────────
    def _quiet(self) -> None:
        try:
            self.backend.release_all()
        except Exception:
            pass

    def _wait_until(self, target: float) -> bool:
        """等到"基准秒数 target"该发生的时刻。返回 False 表示被叫停。

        ★ 速度是在这里除的，不在动作表里。
        """
        while True:
            if self._stop.is_set():
                return False
            if self._pause.is_set():
                # ★ 曾写 `self._pause.wait(0.05)` —— 在"已 set"的事件上 wait
                #   **立即返回**，等于暂停期间纯自旋（实测 150 万圈/秒），GIL 把
                #   主线程饿到帧耗时 120ms+，整个界面卡顿。改睡在 stop 事件上：
                #   真的会睡，且停止请求能立刻打断。
                if self._stop.wait(0.05):
                    return False
                continue
            remain = target / self._rate - (time.perf_counter() - self._t0)
            if remain <= 0:
                return True
            time.sleep(min(remain, 0.02))

    def _emit(self, state: str, **extra) -> None:
        try:
            self.on_progress({
                "state": state, "index": extra.get("index", 0),
                "sent": self._sent, "total": self._total, "rate": self._rate,
                "elapsed": extra.get("elapsed", self.elapsed),
                "duration": self._duration, "action": extra.get("action"),
            })
        except Exception:
            pass

    # ── 主流程 ──────────────────────────────────────────────────────
    def play(self, actions: Iterable[Action], countdown: float = 0.0, loop: bool = False,
             limit_s: float | None = None, rate: float = 1.0,
             start_s: float = 0.0) -> None:
        """阻塞式播放（通常放在独立线程里跑）。

        actions  : 动作表（时刻是基准秒）
        countdown: 开始前倒数几秒
        limit_s  : 只演前多少基准秒（试听用）
        start_s  : **从第几基准秒开始弹**（进度条拖运用）。实现方式是把时间基准
                   往前挪 start_s，比"重算整张表"简单且不丢精度。

        ★ 有意**没有**"时间抖动/随机化"参数。那种东西在游戏场景里唯一的实际用途
          是让自动化显得不像自动化，属于规避检测 —— 按项目底线（禁止一切可能导致
          封号的操作）不做。
        """
        acts = list(actions)
        self._rate = max(RATE_MIN, min(RATE_MAX, float(rate)))
        self._stop.clear()
        self._pause.clear()
        self._total = len(acts)
        self._sent = 0
        if limit_s:
            acts = [a for a in acts if a.t <= limit_s]
        if not acts:
            self._state = IDLE
            self._emit(IDLE)
            return
        self._duration = max(a.t + a.hold for a in acts)

        if countdown and countdown > 0:
            self._state = COUNTDOWN
            for i in range(int(countdown), 0, -1):
                self._emit(COUNTDOWN, index=0, elapsed=0.0)
                self.on_log(f"倒计时 {i}……")
                end = time.perf_counter() + 1.0
                while time.perf_counter() < end:
                    if self._stop.is_set():
                        # ★ 要 _quiet()：倒计时阶段可能已经按下鼠标移调键，
                        #   另外两条 stop 分支都调了，这里原来漏了 → 移调键卡住不放。
                        self._quiet()
                        self._state = IDLE
                        self._emit(IDLE)
                        return
                    time.sleep(0.02)

        self._state = PLAYING
        self._t0 = time.perf_counter() - start_s / self._rate
        self.on_log("▶ 开始")
        self._emit(PLAYING, index=0, elapsed=start_s)

        while True:
            for i, a in enumerate(acts):
                if a.t + a.hold <= start_s + 1e-9:
                    self._sent = i + 1
                    continue                      # 起点之前的动作直接跳过
                if not self._wait_until(a.t):
                    self._quiet()
                    self._state = IDLE
                    self._emit(IDLE)
                    return
                for mod in a.mods:
                    self.backend.mouse_down(mod)
                if a.key:
                    self.backend.key_down(a.key)
                self._sent = i + 1
                self._emit(PLAYING, index=i, action=a)
                # 按住时长：至少让事件发出去，别在同一毫秒按下抬起
                hold = max(a.hold, 0.01)
                end = a.t + hold
                if not self._wait_until(end):
                    self._quiet()
                    self._state = IDLE
                    self._emit(IDLE)
                    return
                if a.key:
                    self.backend.key_up(a.key)
                for mod in reversed(a.mods):
                    self.backend.mouse_up(mod)

            if not loop:
                break
            self._quiet()
            self.on_log("—— 再来一遍 ——")
            start_s = 0.0                          # 第二遍从头上开始
            self._t0 = time.perf_counter()

        self._quiet()
        self._state = DONE
        self._emit(DONE, index=len(acts), elapsed=self._duration)
        self.on_log("演奏结束")
