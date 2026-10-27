"""拖放能力判定：能不能开 desktop_drop 取决于跑的是哪个客户端。"""

from __future__ import annotations

import os
from pathlib import Path

try:
    import flet_dropzone as _dropzone
except Exception:
    _dropzone = None


def _custom_client_available() -> bool:
    if os.environ.get("FLET_PLATFORM", "").strip():
        return True
    try:
        win_dir = Path.cwd() / "build" / "windows"
        return win_dir.is_dir() and any(
            p.suffix.lower() == ".exe" for p in win_dir.iterdir()
        )
    except OSError:
        return False


def _dropzone_on() -> bool:
    if _dropzone is None:
        return False
    flag = os.environ.get("FVP_DROPZONE", "").strip().lower()
    if flag in ("0", "false", "no"):
        return False
    if flag in ("1", "true", "yes"):
        return True
    return _custom_client_available()


def pick_drop_path(paths: list[str]) -> str | None:
    """从拖入的路径里挑出能载入的那个：.bin 优先，其次含 HZC 的文件夹。"""
    bins, dirs = [], []
    for p in paths:
        if not p:
            continue
        s = str(p)
        if s.lower().endswith(".bin"):
            bins.append(s)
        else:
            dirs.append(s)
    for s in bins:
        if Path(s).exists():
            return s
    for s in dirs:
        try:
            if Path(s).is_dir():
                return s
        except OSError:
            continue
    return bins[0] if bins else None


DROPZONE_ON = _dropzone_on()
