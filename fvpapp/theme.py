"""设计 token 与配色：尺寸常量、浅深两套色板、棋盘底、图标与 tooltip。"""

from __future__ import annotations

import base64
import logging
import math
import os
import sys
from pathlib import Path

import flet as ft

log = logging.getLogger("fvptachie")

TRANSPARENT = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"

# ── 设计 token ────────────────────────────────────────────
# 滚动条是覆盖式绘制（Flutter OverlayScrollbar），它不会自己让位。
# 所以所有可滚动区都必须显式留一条 GUTTER 通道，内容不许探进去——
# 这是"滚动条压住缩略图/文字"那类重合的唯一根治点。
GUTTER = 12
RADIUS_PANEL = 16
RADIUS_STAGE = 14
RADIUS_TILE = 10
THUMB = 104
PART_STRIP_H = THUMB + GUTTER + 14   # 上沿给序号角标留位，下沿给横向滚动条留槽
PART_HINT_IDLE = "选择底图后显示差分帧"
PART_HINT_EMPTY = "该底图没有对应的表情差分，将直接导出底图"

# 三级树每级递进的缩进量。第三级（动作）必须比第二级（服装）明显更深，
# 否则"服装 → 动作"读起来像同级，右侧那条导轨就是层级的视觉锚。
INDENT_OUTFIT = 30
INDENT_ACTION = 48
ANIM_ROW = 180
# 提示横幅的通用淡入淡出时长
ANIM_VEIL = 150
# ── 拖放反馈（长在角色库面板上，不是覆盖层）──────────────────
# Flet 0.86 的 Container 没有 animate_color，bgcolor/border 的隐式动画不可用，
# 所以拖放态的一切视觉都画在一张 Canvas 上，由 Python 逐帧重算 alpha 和位置。
# 进场慢（6 帧，看得清"进入拖放态"）、退场快（4 帧，取消不该有戏剧性）。
DROP_TICK = 0.03
DROP_IN_FRAMES = 6
DROP_OUT_FRAMES = 4
# 虚线贴面板内边 0px：画布拿到的是面板扣掉 1px 描边后的内矩形，所以 2px 的描边
# 正好压在那条灰线上，读起来是"这块面板的边框变了状态"而不是里面又框了一层。
DROP_INSET = 0.0
# 行进光斑：拖尾长度与每帧弧长步长。步长必须小于拖尾长度，否则光斑是"跳"不是"爬"；
# 面板周长约 1100px，12px/帧 ≈ 5s 一圈。
DROP_STREAK = 30.0
DROP_STEP = 12.0
# 拒绝态：红框停留多久再退场
DROP_REJECT_MS = 160
# 角色错峰入场：每条间隔与浮起高度
STAGGER_MS = 0.04
STAGGER_RISE = 10.0
# 慢载入才给过程反馈：解析超过这个时长才弹"正在解析"，快路径什么都不弹。
# 卡③「载入超过 2 秒的资源要看得见进度」说的是**哪类资源**得给反馈，
# 不是"等满 2 秒再亮条"——真按 2 秒，2 秒级的载入刚亮就该收，等于没有。
# 0.25 秒：比人开始觉得"卡了"更早，又足够让几十毫秒的小资源全程安静。
BUSY_DELAY = 0.25
# 进度往 UI 线程推的最小间隔：解析九千条就是九千次回调，逐条刷会把事件循环淹了
PROGRESS_PUSH_S = 0.1


def _palette(dark: bool) -> dict:
    if dark:
        p = {
            "page": "#101318",
            "panel": "#1a1e25",
            "stage": "#0d1014",
            "line": "#282e38",
            "line_soft": "#1f242c",
            "text": "#e9edf5",
            "text_dim": "#8e9aae",
            "accent": "#7aa6ff",
            "accent_soft": "#1e2b45",
            "chip": "#1a212b",
            "hover": "#1b222d",
            "sel": "#22334f",
            "badge_bg": "#232c3a",
            "badge_fg": "#b7c3d8",
            "checker_a": "#171c22",
            "checker_b": "#12161b",
            "ok_bg": "#1e2b45",
            "ok_fg": "#c8daff",
            "err_bg": "#3a1d24",
            "err_fg": "#ffc9d2",
        }
    else:
        p = {
            "page": "#eff1f4",
            "panel": "#ffffff",
            "stage": "#f6f7f9",
            "line": "#dfe2e8",
            "line_soft": "#eceef2",
            "text": "#1b2130",
            "text_dim": "#6b7488",
            "accent": "#3f6ee0",
            "accent_soft": "#e8f0fe",
            "chip": "#f1f3f7",
            "hover": "#f2f4f8",
            "sel": "#e3ecfb",
            "badge_bg": "#ffffff",
            "badge_fg": "#5b6780",
            "checker_a": "#f2f4f7",
            "checker_b": "#e7eaef",
            "ok_bg": "#e4ecfd",
            "ok_fg": "#2a4f9e",
            "err_bg": "#fbe3e7",
            "err_fg": "#9c2a44",
        }
    # 拖放态的两色：常态走主题强调色，拒绝态走红。注意 Flet 的 hex 是
    # #AARRGGBB（alpha 在最前），把 alpha 拼到 6 位色值后面会被解析成不透明的
    # 错色，整块面板会变成一坨脏色——所有带透明度的色值都由 _drop_rgba 现算。
    p["frame"] = p["accent"]
    p["err_frame"] = "#ff5f6d" if dark else "#d0304f"
    return p


def _rgb(hex_str: str) -> tuple[int, int, int]:
    h = hex_str.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _app_icon() -> str | None:
    """返回随程序分发的 Shinku.ico 路径；开发环境取脚本目录，打包后取解包目录。"""
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", "")) or Path(sys.executable).parent
    else:
        base = Path(__file__).resolve().parent.parent
    icon = base / "Shinku.ico"
    return str(icon) if icon.exists() else None


def _tip(text: str) -> ft.Tooltip:
    """统一提示气泡：悬停 100ms 即显，移开 150ms 快速淡出。"""
    return ft.Tooltip(message=text, wait_duration=100, show_duration=150)
