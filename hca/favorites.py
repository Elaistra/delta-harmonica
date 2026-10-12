"""收藏夹（Soundpad 式）—— 程序内置的分类，不挪动 sings/ 里的任何文件。

数据形状（存在 hca.json 的 "favorites" 键）：
    [{"name": "常弹", "songs": ["曲名.mid", ...]}, ...]

* 收藏夹存的是**曲名（basename）**，和热键同一口径；曲子文件本体不动。
* 一首曲子可以同时属于多个收藏夹（Soundpad 同款行为）。
* 全部函数都是纯函数：传进旧列表、返回新列表，出错抛 ValueError（消息可直接给用户）。
"""

from __future__ import annotations


def normalize(favs) -> list[dict]:
    """任意脏数据 → 合法结构。名字去空白、去重，曲名去重。"""
    out: list[dict] = []
    seen: set[str] = set()
    for f in favs or []:
        if not isinstance(f, dict):
            continue
        name = str(f.get("name") or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        songs: list[str] = []
        for s in f.get("songs") or []:
            s = str(s or "").strip()
            if s and s not in songs:
                songs.append(s)
        out.append({"name": name, "songs": songs})
    return out


def _find(favs: list[dict], name: str) -> dict:
    for f in favs:
        if f["name"] == name:
            return f
    raise ValueError(f"没有「{name}」这个收藏夹。")


def create(favs: list[dict], name: str) -> list[dict]:
    name = str(name or "").strip()
    if not name:
        raise ValueError("收藏夹名不能为空。")
    if any(f["name"] == name for f in favs):
        raise ValueError(f"已经有叫「{name}」的收藏夹了。")
    return favs + [{"name": name, "songs": []}]


def rename(favs: list[dict], old: str, new: str) -> list[dict]:
    new = str(new or "").strip()
    if not new:
        raise ValueError("收藏夹名不能为空。")
    if new != old and any(f["name"] == new for f in favs):
        raise ValueError(f"已经有叫「{new}」的收藏夹了。")
    out = [dict(f) for f in favs]
    _find(out, old)["name"] = new
    return out


def delete(favs: list[dict], name: str) -> list[dict]:
    _find(favs, name)                       # 不存在就报错
    return [f for f in favs if f["name"] != name]


def add_songs(favs: list[dict], name: str, songs) -> tuple[list[dict], int]:
    """把若干曲名加入收藏夹（已存在的跳过）。返回 (新列表, 实际加入数)。"""
    _find(favs, name)
    out = [dict(f, songs=list(f["songs"])) for f in favs]
    target = _find(out, name)
    n = 0
    for s in songs:
        s = str(s or "").strip()
        if s and s not in target["songs"]:
            target["songs"].append(s)
            n += 1
    return out, n


def remove_songs(favs: list[dict], name: str, songs) -> list[dict]:
    _find(favs, name)
    drop = {str(s or "").strip() for s in songs}
    out = [dict(f, songs=list(f["songs"])) for f in favs]
    target = _find(out, name)
    target["songs"] = [s for s in target["songs"] if s not in drop]
    return out


def favorites_of(favs: list[dict], basename: str) -> list[str]:
    """一首曲子属于哪些收藏夹（演奏页列表列要用的）。"""
    return [f["name"] for f in favs if basename in f["songs"]]


def sync_file_rename(favs: list[dict], old_base: str, new_base: str) -> list[dict]:
    """曲子文件改名后，同步所有收藏夹里的引用。"""
    out = [dict(f, songs=list(f["songs"])) for f in favs]
    for f in out:
        f["songs"] = [new_base if s == old_base else s for s in f["songs"]]
    return out


def sync_file_delete(favs: list[dict], base: str) -> list[dict]:
    """曲子文件删除后，从所有收藏夹里清掉引用。"""
    out = [dict(f, songs=list(f["songs"])) for f in favs]
    for f in out:
        f["songs"] = [s for s in f["songs"] if s != base]
    return out
