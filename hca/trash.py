"""回收站删除 —— 只删我们自己的曲库文件，且**永远进系统回收站**（可还原）。

为什么单独一个文件、还要进底线白名单：
    删除走 shell32 的 SHFileOperationW，而底线检查器规定"只有指定文件能调
    Windows API"。允许它的理由（写在 verify/redline_check.py）：
      · 只操作我们曲库目录自己的文件
      · FOF_ALLOWUNDO → 进回收站，可还原（不用 os.remove 那种不可逆删除）
      · 不碰进程、不碰内存、不注入 —— 与底线①②无关

限制：SHFileOperationW 对超长路径（>260 字符）支持不好；曲库路径一般远短于此。
"""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from pathlib import Path

FO_DELETE = 3
FOF_ALLOWUNDO = 0x40          # 进回收站（可还原）
FOF_NOCONFIRMATION = 0x10     # 不弹"确定要删除吗"
FOF_SILENT = 0x4              # 不显示进度对话框
FOF_NOERRORUI = 0x400         # 出错不弹系统错误框（由我们自己报告）


class SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("wFunc", wintypes.UINT),
        ("pFrom", wintypes.LPCWSTR),
        ("pTo", wintypes.LPCWSTR),
        ("fFlags", wintypes.WORD),
        ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", ctypes.c_void_p),
        ("lpszProgressTitle", wintypes.LPCWSTR),
    ]


def recycle(paths) -> int:
    """把一批文件移入回收站。返回处理数；用户取消或系统失败则抛异常。

    ★ 安全承诺：只做"进回收站"这一种删除。本模块永远不调用 os.remove /
      os.unlink 之类的不可逆删除 —— 这是数据安全底线。
    """
    if os.name != "nt":
        raise OSError("回收站删除只在 Windows 上可用")
    items = [str(Path(p).absolute()) for p in paths]
    items = [p for p in items if Path(p).exists()]
    if not items:
        return 0
    # pFrom 需要双 null 结尾的路径列表
    pfrom = "".join(p + "\x00" for p in items) + "\x00"

    op = SHFILEOPSTRUCTW()
    op.hwnd = None
    op.wFunc = FO_DELETE
    op.pFrom = pfrom
    op.pTo = None
    op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI
    op.fAnyOperationsAborted = False
    op.hNameMappings = None
    op.lpszProgressTitle = None

    ret = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    # ★ 实测：成功移入回收站时 SHFileOperationW 也可能返回非 0（本机 ret=2），
    #   所以**以"文件是否消失"判定成败**，不以上一时返回值为准。
    gone = sum(1 for p in items if not Path(p).exists())
    if gone == len(items):
        return gone
    if op.fAnyOperationsAborted:
        raise RuntimeError(f"移入回收站被中止：只完成 {gone}/{len(items)}")
    raise OSError(f"移入回收站失败（系统错误码 {ret}，只完成 {gone}/{len(items)}）")
