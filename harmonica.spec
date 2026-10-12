# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置。

用法（先装 PyInstaller：pip install pyinstaller）：
    pyinstaller harmonica.spec           # 单文件 exe，产出在 dist/

★ 两个必须带上的资源目录，否则打包出来的程序会缺功能：
    mapping/   键位映射 JSON。运行时按「程序目录/mapping」找
               （hca/mapping.py: Path(__file__).parent.parent / "mapping"）。
               丢了会静默退回内置网页映射，三角洲那份就没了。
    web/       自建网页口琴（验证"扫描码 → e.code"用），run.bat 菜单第 8 项会打开它。
"""

from PyInstaller.utils.hooks import collect_submodules

# tkinter 的子模块（ttk 等）要显式收集，否则打包后会 ImportError
hiddenimports = collect_submodules("hca") + [
    "tkinter",
    "tkinter.ttk",
    "tkinter.font",
    "tkinter.filedialog",
]

datas = [
    ("mapping", "mapping"),
    ("web", "web"),
]

a = Analysis(
    ["main.py"],
    pathex=["."],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        # 用不到，砍掉能显著减小体积
        "numpy", "scipy", "matplotlib", "PIL", "pandas",
        "pytest", "IPython", "notebook",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="三角洲口琴自动演奏",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,          # 图形程序，不弹控制台黑框
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,              # 有图标就填 "app.ico"
)
