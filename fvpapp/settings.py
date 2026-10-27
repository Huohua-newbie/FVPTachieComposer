"""偏好设置落盘：目前只存窗口（尺寸 / 位置 / 最大化）。

文件在 `%LOCALAPPDATA%\\FVPTachieComposer\\settings.json`（非 Windows 走
`$XDG_CONFIG_HOME`），`FVP_SETTINGS_PATH` 可以整条覆盖——测试与排障用。

**读永远不抛**：文件不存在、JSON 坏掉、字段类型不对，一律当"没设过"回默认值。
这是偏好不是素材，不能因为它让程序起不来；删掉文件要干净地回到 1280×760。

位置必须校验：拔掉一块显示器、分辨率改小之后，上次记下的坐标可能整个落在屏幕外，
窗口看不见也拖不回来。`screen_bounds()` 问 Win32 拿虚拟桌面的范围，
`plan_window()` 据此决定坐标还用不用；拿不到范围（Linux/WSL 开发机）就只还原
尺寸，不冒险还原位置。
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Optional

from .version import APP_NAME

log = logging.getLogger("fvptachie")

SECTION_WINDOW = "window"

# 与 `ui._setup_page` 原来的写死值一致：第一次运行、或设置被删掉时的样子
DEFAULT_WIDTH = 1280
DEFAULT_HEIGHT = 760
MIN_WIDTH = 1020
MIN_HEIGHT = 600

# 拖动和缩放期间 Windows 一路发事件，攒一下再写盘
SAVE_DELAY_S = 0.45
# 至少要有这么多像素落在屏幕里，否则算出界（横向留够，才还拖得回来）
MIN_ONSCREEN_X = 64
MIN_ONSCREEN_Y = 48

_GEOMETRY_KEYS = ("width", "height", "left", "top")


def settings_path() -> Path:
    override = os.environ.get("FVP_SETTINGS_PATH")
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA")
        base = Path(local) if local else Path.home() / "AppData" / "Local"
        return Path(base) / APP_NAME / "settings.json"
    cfg = os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
    return Path(cfg) / APP_NAME / "settings.json"


def load() -> dict:
    """整个设置字典；文件缺失、读不动或内容不是对象都回 `{}`。"""
    path = settings_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        log.debug("设置文件读不出来，按默认值继续: %s", path, exc_info=True)
        return {}
    return raw if isinstance(raw, dict) else {}


def save_window(prefs: dict) -> None:
    """写回 window 这一段（其他段原样保留）。写失败只记日志，不打扰用户。"""
    data = load()
    data[SECTION_WINDOW] = prefs
    path = settings_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # 先写临时文件再替换：中途掉电/被杀也不会留下半截 JSON
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        log.debug("设置没写进去: %s", path, exc_info=True)


def _clean_size(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if number > 0 else None


def _clean_coord(value: Any) -> Optional[float]:
    """坐标允许负数（副屏在主屏左边/上边），但 NaN、无穷和荒诞值不算。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if abs(number) < 1_000_000 else None


def window_prefs() -> dict:
    """存过的窗口偏好，键同 `_GEOMETRY_KEYS` + `maximized`，没有的键直接不出现。"""
    raw = load().get(SECTION_WINDOW)
    if not isinstance(raw, dict):
        return {}
    out = {}
    for key in _GEOMETRY_KEYS:
        cleaner = _clean_size if key in ("width", "height") else _clean_coord
        value = cleaner(raw.get(key))
        if value is not None:
            out[key] = value
    if isinstance(raw.get("maximized"), bool):
        out["maximized"] = raw["maximized"]
    return out


def screen_bounds() -> Optional[tuple[float, float, float, float]]:
    """虚拟桌面（所有显示器并集）的外接矩形 `(x, y, 宽, 高)`。

    flet 0.86 的 Python 侧拿不到显示器信息，只能问 Win32。那里的单位是物理像素，
    而 `window.left/top` 用的是逻辑像素，不折算的话 150% 缩放的屏会把合法位置
    误判成出界。拿不到就回 `None`（非 Windows、API 缺失都被吞掉）。
    """
    if os.name != "nt":
        return None
    try:
        from ctypes import windll

        user32 = windll.user32
        left = user32.GetSystemMetrics(76)   # SM_XVIRTUALSCREEN
        top = user32.GetSystemMetrics(77)    # SM_YVIRTUALSCREEN
        width = user32.GetSystemMetrics(78)  # SM_CXVIRTUALSCREEN
        height = user32.GetSystemMetrics(79)  # SM_CYVIRTUALSCREEN
        if width <= 0 or height <= 0:
            return None
        scale = (getattr(user32, "GetDpiForSystem", lambda: 96)() or 96) / 96.0
        return (left / scale, top / scale, width / scale, height / scale)
    except Exception:
        log.debug("拿不到屏幕范围，位置不做校验", exc_info=True)
        return None


def _on_screen(left: float, top: float, width: float, height: float, bounds) -> bool:
    x0, y0, bw, bh = bounds
    overlap_x = min(left + width, x0 + bw) - max(left, x0)
    overlap_y = min(top + height, y0 + bh) - max(top, y0)
    return overlap_x >= min(MIN_ONSCREEN_X, width) and overlap_y >= min(MIN_ONSCREEN_Y, height)


_UNSET = object()


def plan_window(prefs: Optional[dict] = None, bounds: Any = _UNSET) -> dict:
    """把存下来的偏好折算成这次能安全应用的窗口值。

    返回 `width/height/left/top/maximized`；`left/top` 为 `None` 表示位置不还原
    （没存过、坐标不可信、或者现在已经在别的屏幕上）。
    """
    if prefs is None:
        prefs = window_prefs()
    if bounds is _UNSET:
        bounds = screen_bounds()

    width = prefs.get("width") or DEFAULT_WIDTH
    height = prefs.get("height") or DEFAULT_HEIGHT
    if bounds is not None:
        width = min(width, bounds[2])
        height = min(height, bounds[3])
    width = max(width, MIN_WIDTH)
    height = max(height, MIN_HEIGHT)

    left, top = prefs.get("left"), prefs.get("top")
    if left is None or top is None:
        left = top = None
    elif bounds is None:
        # 校验不了就宁可回默认位置：看不见的位置比丢一次位置更糟
        log.debug("拿不到屏幕范围，本次不还原窗口位置")
        left = top = None
    elif not _on_screen(left, top, width, height, bounds):
        log.info("上次记下的位置 (%.0f, %.0f) 不在当前屏幕内，改用默认位置", left, top)
        left = top = None

    return {"width": width, "height": height, "left": left, "top": top,
            "maximized": bool(prefs.get("maximized"))}


def capture_window(window, maximized: bool, previous: Optional[dict] = None) -> dict:
    """读回当前窗口状态，攒成一段可写盘的偏好。

    最大化那会儿的宽高/坐标是整个显示器那一圈（Windows 还把不可见边框算进去），
    存下来下次还原就变成一个铺满的浮窗。所以最大化只改标记，尺寸和位置沿用
    上一次非最大化时的记录。
    """
    previous = previous or {}
    out = {"maximized": bool(maximized)}
    for key in _GEOMETRY_KEYS:
        if maximized:
            value = previous.get(key)
        else:
            cleaner = _clean_size if key in ("width", "height") else _clean_coord
            value = cleaner(getattr(window, key, None))
            if value is None:
                value = previous.get(key)
        if value is not None:
            out[key] = value
    return out
