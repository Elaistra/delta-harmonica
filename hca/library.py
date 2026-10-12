"""曲库：扫目录 + 按内容载入乐谱。

与原程序保持一致的判据（这样两边的行为可以直接对比）：
  * **只扫一层**（不递归）—— 子目录里的曲子不会被列出来
  * 扩展名 `.mid .midi .abc .txt`
  * 按**文件内容**判断格式（嗅 MThd），不看扩展名

★ 注意：这是"为了对照"而保持的兼容行为。新项目本来可以做得更好（递归、和弦保留），
  那是第 4 阶段"底层新玩法"要动的地方 —— 现在先一模一样，方便逐层验证。
"""

from __future__ import annotations

from pathlib import Path

from .mapping import Mapping
from .midi import looks_like_midi, parse_midi
from .model import Score

SCORE_EXT = (".mid", ".midi", ".abc", ".txt")

__all__ = ["SCORE_EXT", "list_library", "list_library_all", "list_categories",
           "create_category", "move_to_category", "rename_song",
           "sanitize_name", "load_score", "resolve_lib_dir"]

_ILLEGAL = '\\/:*?"<>|'


def sanitize_name(name: str) -> str:
    """曲名/分类名 → 合法文件名：去掉 Windows 非法字符和首尾空白。"""
    cleaned = "".join("" if ch in _ILLEGAL else ch for ch in name).strip().strip(".")
    return cleaned


def resolve_lib_dir(rel: str, base: Path) -> Path:
    """曲库目录：相对路径按「程序所在目录」解析。"""
    p = Path(rel)
    return p if p.is_absolute() else (Path(base) / p)


def list_library(lib_dir) -> list[str]:
    root = Path(lib_dir)
    if not root.is_dir():
        return []
    out = []
    for entry in sorted(root.iterdir()):
        if entry.is_file() and entry.suffix.lower() in SCORE_EXT:
            out.append(str(entry))
    return out


def list_categories(lib_dir) -> list[str]:
    """曲库根目录下的子文件夹 = 分类文件夹（与旧版/游戏"只读一层"兼容）。"""
    root = Path(lib_dir)
    if not root.is_dir():
        return []
    return sorted(e.name for e in root.iterdir() if e.is_dir() and not e.name.startswith("."))


def list_library_all(lib_dir) -> list[tuple[str, str]]:
    """全部曲目（含分类）→ [(路径, 分类)]；分类 = 子文件夹名，根目录为 ""。

    只扫两层：根目录 + 各分类文件夹一层（更深的忽略，与旧版口径一致）。
    """
    root = Path(lib_dir)
    if not root.is_dir():
        return []
    out: list[tuple[str, str]] = []
    for entry in sorted(root.iterdir()):
        if entry.is_file() and entry.suffix.lower() in SCORE_EXT:
            out.append((str(entry), ""))
    for sub in list_categories(root):
        for entry in sorted((root / sub).iterdir()):
            if entry.is_file() and entry.suffix.lower() in SCORE_EXT:
                out.append((str(entry), sub))
    return out


def create_category(lib_dir, name: str) -> Path:
    """新建分类文件夹（同名已存在不算错误——文件夹本来就该幂等）。"""
    clean = sanitize_name(name)
    if not clean:
        raise ValueError("分类名不能为空（或只含非法字符）")
    d = Path(lib_dir) / clean
    d.mkdir(parents=True, exist_ok=True)
    return d


def _unique(dest: Path) -> Path:
    if not dest.exists():
        return dest
    stem, suffix = dest.stem, dest.suffix
    for i in range(2, 999):
        cand = dest.with_name(f"{stem} ({i}){suffix}")
        if not cand.exists():
            return cand
    raise FileExistsError(f"目标位置已有同名文件：{dest}")


def move_to_category(path, category: str) -> Path:
    """把一首曲子移进分类（category="" 或 None = 移回根目录）。重名自动加 (2)。"""
    src = Path(path)
    if not src.is_file():
        raise FileNotFoundError(f"找不到文件：{src}")
    root = src.parent if category in (None, "") else src.parent / sanitize_name(category)
    if category not in (None, ""):
        root.mkdir(parents=True, exist_ok=True)
    dest = _unique(root / src.name)
    if src.parent == dest.parent:
        return src                                   # 已在目标位置
    return Path(__import__("shutil").move(str(src), str(dest)))


def rename_song(path, new_title: str) -> Path:
    """自定义命名：改文件名（保留扩展名），重名自动加 (2)。"""
    src = Path(path)
    if not src.is_file():
        raise FileNotFoundError(f"找不到文件：{src}")
    clean = sanitize_name(new_title)
    if not clean:
        raise ValueError("曲名不能为空（或只含非法字符）")
    dest = _unique(src.with_name(clean + src.suffix))
    if dest == src:
        return src
    return Path(__import__("shutil").move(str(src), str(dest)))


def load_score(path, mapping: Mapping | None = None) -> Score:
    """读一个乐谱文件 → Score。按内容判格式。"""
    p = Path(path)
    data = p.read_bytes()
    if looks_like_midi(data):
        score = parse_midi(data, mapping)
    else:
        from .abc import parse_abc
        score = parse_abc(data.decode("utf-8", "replace"), mapping)
    score.title = p.stem                 # 曲名取文件名（和原程序一致）
    return score
