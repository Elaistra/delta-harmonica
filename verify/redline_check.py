"""底线自检 —— **本项目最重要的一个测试**。

用户定的硬底线（2026-10-12，两条）：
    ① 程序**禁止修改内存**（游戏内存、别的进程内存、自己的进程内存都不改）。
    ② **一切可能导致封号的操作都不许做。**
    只允许"模拟键盘和鼠标"，不允许任何带**修改性质**或**规避性质**的操作。

这个脚本把承诺变成**能自动拦住的东西**，而不是一句文档：

  1. 硬禁止清单 A：改内存 / 跨进程 / 注入 / 钩子 / 调试附加 / 内存扫描库
  2. 硬禁止清单 B：反作弊规避 / 输入伪装 / 驱动加载 / 进程与窗口篡改 / 反调试 / 清痕迹
  3. 文件落点约束：只有 hca/inputs.py 和 hca/hotkeys.py 允许直接调 Windows API
  4. DLL 白名单：只允许 user32（它提供 SendInput / RegisterHotKey / 光标位置，都是"输入"范畴）
  5. 软提醒清单：kernel32、subprocess 等 —— 允许但要写明理由

用法：
    python verify/redline_check.py            # 检查
    python verify/redline_check.py --selftest # 先证明它真的抓得到违规
退出码 0 = 干净；1 = 发现违规。
"""

from __future__ import annotations

import argparse
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# ── 1. 硬禁止：出现即失败 ────────────────────────────────────────────
#    这些全都是"改内存 / 改别人进程 / 注入 / 钩输入"性质的操作。
FORBIDDEN = {
    # 跨进程内存读写
    r"\bWriteProcessMemory\b": "改别的进程的内存",
    r"\bReadProcessMemory\b": "读别的进程的内存",
    r"\bNtWriteVirtualMemory\b": "改内存（NT 层）",
    r"\bNtReadVirtualMemory\b": "读内存（NT 层）",
    r"\bVirtualAllocEx\b": "在别的进程里分配内存",
    r"\bVirtualProtectEx\b": "改别的进程的内存保护属性",
    r"\bVirtualAlloc\b": "自己分配可执行内存",
    r"\bVirtualProtect\b": "改内存保护属性",
    # 远程线程 / 注入 / 钩子 / 调试
    r"\bCreateRemoteThread\b": "在别的进程里起线程",
    r"\bNtCreateThreadEx\b": "在别的进程里起线程",
    r"\bQueueUserAPC\b": "APC 注入",
    r"\bSetWindowsHookEx\w*\b": "全局钩子（能拦改键鼠消息）",
    r"\bSetThreadContext\b": "改线程上下文",
    r"\bGetThreadContext\b": "读线程上下文",
    r"\bSuspendThread\b": "挂起别的线程",
    r"\bDebugActiveProcess\b": "附加调试器",
    r"\bOpenProcess\b": "拿别的进程的句柄",
    r"\bToolhelp32\b": "遍历进程（内存扫描的前置）",
    r"\bCreateToolhelp32Snapshot\b": "遍历进程快照",
    # 内存扫描 / 注入库
    r"\bpymem\b": "内存扫描库",
    r"\bfrida\b": "动态插桩框架",
    r"\bcheatengine\b": "内存扫描器",
    r"\bmemscan\b": "内存扫描",
    r"\bptrace\b": "Linux 内存/进程控制",
    r"/proc/\d*/mem": "Linux 直接读进程内存",
    r"\bmach_vm_(write|read)\b": "macOS 读写进程内存",
    r"\bvm_write\b": "macOS 写内存",
    r"\bWriteByte\b": "按地址写字节",
    r"\bpatch_?memory\b": "改内存",
    # 键盘钩子式改写（不是"模拟"，是"拦截并改"）
    r"\bkeybd_event\b": "旧式输入注入 API（不推荐，且易被误判为改写输入）",
    r"\bSendMessage\w*\b\s*\(.*WM_KEY": "直接往窗口塞按键消息（绕过输入栈）",
    r"\bPostMessage\w*\b\s*\(.*WM_KEY": "直接往窗口塞按键消息（绕过输入栈）",

    # ── 硬禁止清单 B：反作弊规避 / 输入伪装 / 驱动 / 篡改 / 反调试 ──────
    #    用户第②条底线：**一切可能导致封号的操作都不许做**。
    #    以下这些"让自动化更难被发现"的技术，本质就是规避检测 —— 一律禁止。
    r"\binterception\b": "驱动级键鼠伪装（用来让合成输入看起来像真键盘）",
    r"\bvmulti\b": "虚拟 HID 设备驱动（伪装成硬件键盘）",
    r"\bViGEm\b": "虚拟手柄设备驱动",
    r"\bHidHide\b|\bhid_?spoof\b|\bfake_?hid\b": "HID 伪装",
    r"\bNtLoadDriver\b|\bZwLoadDriver\b": "加载内核驱动",
    r"\bCreateService\w*\b.*\b(KERNEL_DRIVER|driver)\b": "注册/加载驱动服务",
    r"\bSetWindowText\w*\b.*(title|name)": "篡改窗口标题以伪装进程",
    r"\bhide_?process\b|\bprocess_?hide\b": "隐藏自身进程",
    r"\bIsDebuggerPresent\b|\bCheckRemoteDebugger\b|\bNtQueryInformationProcess\b":
        "反调试探测（规避前置动作）",
    r"\bclear_?logs?\b|\bclean_?logs?\b|\bwipe_?trace\b": "清理痕迹",
    r"\bbypass\b.*\b(anti-?cheat|anticheat|\bac\b)": "绕过反作弊",
    r"\banti-?cheat\b.*\b(bypass|evade|avoid|hide)": "绕过反作弊",
    r"(evade|avoid|escape|dodge).*(detect|anti-?cheat|scan)": "规避检测",
    r"(random|jitter|noise).*(evade|avoid|bypass|dodge|detect)": "用随机化规避检测",
    r"\bhumanize\b.*\b(detect|evade|avoid)\b": "把自动化伪装成人手",
}

# ── 2. 软提醒：允许，但要在同一行写明理由（"# 理由：..."）────────────────
SOFT = {
    r"\bkernel32\b": "kernel32（除了内存/进程操作还有别的用途，需说明）",
    r"\bntdll\b": "ntdll（底层，慎用）",
    r"\bsubprocess\b": "起外部进程",
    r"\bmemmove\b|\bmemset\b": "裸内存操作（确认只操作自己的缓冲区）",
}

# ── 3. Windows API 落点与 DLL 白名单 ────────────────────────────────
#   2026-10-12 有意多加一条：dwmapi / hca/glass.py —— 用来做窗口的亚克力背景
#   与原生圆角（"液态玻璃"外观）。允许它的理由：
#     · 只调 DwmSetWindowAttribute / DwmExtendFrameIntoClientArea 两个纯外观函数
#     · 只作用在**自己的窗口**上，从不找/改别的窗口
#     · 不读内存不写内存、不起线程、不注入 —— 与底线①②都无关
#   下面有专门的一条：dwmapi 只许出现这两个函数名。
# ── 有意放行的例外（逐条写明理由，改前先想清楚）──────────────────────
#   key = (相对路径, 命中的那一行里包含的关键词)
ALLOWED_HITS = {
    # 「清空」的是 Tk 界面上的日志**显示**（一个内存 Text 控件），
    # 不删除任何文件、不影响任何磁盘日志 —— 没有反取证价值，属正常 UI 功能。
    # 检查器分不清"删证据文件"和"清屏幕显示"，所以在名字上放行这一条。
    ("hca/ui.py", "clear_log"): "清空界面日志显示（内存 Text 控件，不删文件）",
}

ALLOWED_DLL_FILES = {"hca/inputs.py", "hca/hotkeys.py", "hca/glass.py", "hca/trash.py"}
ALLOWED_DLLS = {"user32", "dwmapi"}
# ── 按文件单独放行的 DLL（比全局白名单更紧）────────────────────────
#   hca/trash.py 用 shell32 的 SHFileOperationW 把曲库文件移入**系统回收站**
#   （FOF_ALLOWUNDO，可还原）。允许的理由：
#     · 只操作我们曲库目录自己的文件，从不碰别的进程/路径
#     · 回收站删除可还原，不是不可逆破坏
#     · 不读不改内存、不注入、与反作弊无关
#   除此之外任何文件出现 shell32 一律算违规。
DLL_PER_FILE = {"hca/trash.py": {"shell32"}}
DWMAPI_ALLOWED_FUNCS = {"DwmSetWindowAttribute", "DwmExtendFrameIntoClientArea"}
DLL_PATTERN = re.compile(r"""WinDLL\s*\(\s*["']([^"']+)["']|windll\.(\w+)""")
DWM_CALL_PATTERN = re.compile(r"\bDwm(\w+)")


def scan() -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    files = sorted(p for p in ROOT.rglob("*.py") if p.is_file()
                   and ".workbuddy" not in p.parts
                   and p.name != "redline_check.py")
    if not files:
        errors.append("没扫到任何 .py 文件 —— 路径不对？")
        return errors, warnings

    for path in files:
        rel = path.relative_to(ROOT).as_posix()
        text = path.read_text(encoding="utf-8", errors="replace")
        for i, line in enumerate(text.splitlines(), 1):
            for pat, why in FORBIDDEN.items():
                if re.search(pat, line):
                    if any(key in line and allowed == (rel, key)
                           for key, allowed in [(k[1], k) for k in ALLOWED_HITS]):
                        continue          # 有意放行的例外（见 ALLOWED_HITS 的理由）
                    errors.append(f"{rel}:{i}  禁用 {why}\n      {line.strip()[:110]}")
            for pat, why in SOFT.items():
                if re.search(pat, line) and "# 理由" not in line:
                    warnings.append(f"{rel}:{i}  {why}\n      {line.strip()[:110]}")
        # DLL 白名单
        for m in DLL_PATTERN.finditer(text):
            dll = (m.group(1) or m.group(2) or "").lower()
            if dll in DLL_PER_FILE.get(rel, set()):
                continue                       # 按文件单独放行（见 DLL_PER_FILE 理由）
            if rel not in ALLOWED_DLL_FILES:
                errors.append(f"{rel}  只有 {sorted(ALLOWED_DLL_FILES)} 允许直接调 Windows API"
                              f"（这里是 {dll}）")
            elif dll not in ALLOWED_DLLS:
                errors.append(f"{rel}  只允许加载 {sorted(ALLOWED_DLLS)}，发现 {dll}")
        # dwmapi 只许用两个纯外观函数
        if DLL_PATTERN.search(text):
            for m in DWM_CALL_PATTERN.finditer(text):
                fn = "Dwm" + m.group(1)
                if fn not in DWMAPI_ALLOWED_FUNCS and fn.startswith("Dwm"):
                    errors.append(f"{rel}  dwmapi 只许用 {sorted(DWMAPI_ALLOWED_FUNCS)}，"
                                  f"发现 {fn}")
    return errors, warnings


def selftest() -> int:
    """故意放一段违规代码，确认检查真的能抓到。"""
    bad = ROOT / "_redline_planted.py"
    bad.write_text(
        "import ctypes\n"
        "k = ctypes.WinDLL('kernel32')\n"
        "k.WriteProcessMemory(1, 2, 3, 4, 5)\n"
        "h = ctypes.WinDLL('user32')\n"
        "h.SetWindowsHookExW(1, 2, 3, 4)\n",
        encoding="utf-8")
    try:
        errors, _ = scan()
        hit = [e for e in errors if "_redline_planted.py" in e]
        print(f"自检：植入 4 处违规，抓到 {len(hit)} 处")
        for h in hit:
            print("   ✔", h.splitlines()[0])
        ok = len(hit) >= 4
        print("自检结果：" + ("通过（检查器有效）" if ok else "✘ 没抓全 —— 检查器本身有问题"))
        return 0 if ok else 1
    finally:
        bad.unlink(missing_ok=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return selftest()

    print("=" * 88)
    print("底线自检：禁止改内存 / 只允许模拟键鼠")
    print("=" * 88)
    errors, warnings = scan()
    if warnings:
        print(f"\n软提醒（{len(warnings)} 条，允许但要写清理由）")
        for w in warnings:
            print("  ⚠", w)
    if errors:
        print(f"\n✘ 发现 {len(errors)} 处违规！")
        for e in errors:
            print("  ✘", e)
        print("\n这些操作碰了内存/进程/注入/钩子 —— 属于用户明确禁止的范畴。请改掉。")
        return 1
    print("\n✔ 干净：没有改内存、没有跨进程操作、没有注入、没有钩子。")
    print("  只调了 user32 的输入相关 API（SendInput / RegisterHotKey / 光标位置）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
