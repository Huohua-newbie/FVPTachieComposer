# -*- mode: python ; coding: utf-8 -*-
"""FVPTachieComposer Windows 正式构建 spec（入库版本）。

用法（仓库根目录）：
    pyinstaller --noconfirm FVPTachieComposer.win.spec --distpath dist --workpath build
或直接运行 build_win.ps1。

体积优化点（见 Release 体积优化案）：
1. excludes 剔除 tkinter / PIL.ImageTk / numpy / pygments / rich / markdown_it /
   setuptools / pkg_resources / chardet / oauthlib / flet.auth / httpx._main；
   其中 tkinter 相关已在源码侧下沉为函数级 import，Flet 入口不受影响。
2. _slim() 对 binaries 按源文件去重：flet 会把同一批 DLL 同时收进
   顶层与 flet_desktop\\app\\flet 子树（约 60MB 原始体积重复）。
3. 剔除 libmpv（视频/音频库，本程序只看图不用）。
"""
import os

# PyInstaller 执行 spec 时预定义 SPECPATH（spec 文件所在目录，即仓库根目录）
HERE = SPECPATH if "SPECPATH" in dir() else os.getcwd()
ICON = os.path.join(HERE, "Shinku.ico")


def _slim(toc, drop_libmpv=True):
    seen, out = set(), []
    for dest, src, kind in toc:
        base = os.path.basename(dest).lower() if isinstance(dest, str) else ""
        if drop_libmpv and "libmpv" in base:
            continue
        if kind in ("BINARY", "EXTENSION") and isinstance(src, str) and src:
            key = os.path.normcase(os.path.abspath(src))
            if key in seen:
                continue
            seen.add(key)
        out.append((dest, src, kind))
    return out


a = Analysis(
    ["FVPTachieComposerFlet.py"],
    pathex=[],
    binaries=[],
    datas=[(ICON, ".")],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "PIL.ImageTk",
        "numpy",
        "pygments",
        "rich",
        "markdown_it",
        "setuptools",
        "pkg_resources",
        "chardet",
        "oauthlib",
        "flet.auth",
        "httpx._main",
    ],
    noarchive=False,
    optimize=0,
)

a.binaries = _slim(a.binaries)
a.datas = _slim(a.datas)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="FVPTachieComposer",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=[ICON],
)
