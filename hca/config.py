"""配置 —— 新项目自己的设置文件，与旧程序完全不共享。

字段保持和旧程序相近，方便你把旧的 autoplay_app.json 抄过来；
但**不主动去读旧文件**，避免两个程序互相踩。
"""

from __future__ import annotations

import json
from pathlib import Path

DEFAULT: dict = {
    # 默认曲库：程序目录旁边的「曲库」文件夹。
    # ★ 用相对路径（按程序所在目录解析），不要写死某台机器的绝对路径 ——
    #   否则开源分发时会把私人目录结构带出去，别人拿到也指向无效位置。
    "lib_dir": "../曲库",
    "mapping": "web_harmonica",  # 用哪份键位映射（mapping/*.json）
    "engine": "own",             # own=自己的内核（重写中） / reference=旧内核（对照用）
    "rate": 1.0,                 # 速度倍率（内核支持 0.05~8.0，界面只放 0.5~2.0）
    "no_intro": False,
    "loop": False,
    "legato": True,              # 连奏防断音：按住满时值（只留 20ms 换指间隔）
    "countdown": 3.0,
    "hotkeys": True,
    "park_cursor": False,        # 发鼠标移调键前把光标挪开（原程序文档提过但没实现）
    "last_song": "",
    "favorites": [],             # 收藏夹（Soundpad 式）：[{name, songs:[曲名]}]
    "hotkey_map": [],            # 自定义热键：[{key: "Ctrl+Alt+J", song: "曲名.mid"}]
    # 播放控制热键（空串 = 未绑定）：toggle=暂停/继续 stop=停止 prev/next=换曲
    "control_keys": {"toggle": "Ctrl+Shift+-", "stop": "", "prev": "", "next": ""},
}

__all__ = ["DEFAULT", "load", "save"]


def load(path: Path) -> dict:
    cfg = dict(DEFAULT)
    try:
        if Path(path).is_file():
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            if isinstance(data, dict):
                # "slots" 是已废弃的旧键，放行一次给 ui 的迁移逻辑读，迁移完会删掉
                cfg.update({k: v for k, v in data.items()
                            if k in DEFAULT or k == "slots"})
    except Exception:
        pass
    return cfg


def save(path: Path, cfg: dict) -> None:
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        # ★ cfg 里的键优先（覆盖 DEFAULT），且**保留 DEFAULT 之外的新键**
        #   （favorites/hotkey_map 就是这么加进来的；曾因只存 DEFAULT 键丢过数据）
        Path(path).write_text(
            json.dumps({**DEFAULT, **cfg},
                       ensure_ascii=False, indent=1),
            encoding="utf-8")
    except Exception:
        pass
