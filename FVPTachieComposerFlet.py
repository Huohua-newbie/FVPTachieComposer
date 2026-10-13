"""FVP Tachie Composer — Flet UI

FVP engine character sprite viewer & composer.
Parsing and composition logic reused from FVPTachieComposer.py.

Dependencies: flet>=0.86  Pillow>=10
Run: python FVPTachieComposerFlet.py
"""

import asyncio
import base64
import io
import math
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import flet as ft
import flet.canvas as fc
from PIL import Image

from FVPTachieComposer import (
    compose_preview,
    hzc_data_to_pil_list,
    hzc_decode_first_frame,
    parse_bin_info_extended,
    parse_dir_infos,
)

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
# 慢载入才给过程反馈：解析超过这个时长才弹"正在解析"，快路径什么都不弹
BUSY_DELAY = 0.25

# 拖放能力：OS 级文件拖放必须由 Flutter 客户端里的 desktop_drop 插件承接，
# 插件只存在于 `flet build windows` 产出的自定义客户端里——官方客户端不认这个
# 控件，硬塞进去会让整棵树渲染失败。所以能不能开，取决于"客户端是谁"：
#   1. FVP_DROPZONE=0/1 显式强制（给 FLET_VIEW_PATH 指向的自定义客户端兜底）；
#   2. 打包产物里 Python 与 Flutter 同进程，FLET_PLATFORM 必被注入 → 肯定带插件；
#   3. 开发态：flet_desktop 按 cwd/build/windows/*.exe → FLET_VIEW_PATH → 官方缓存
#      的顺序选客户端，所以只要 build/windows 下有 exe，跑起来就会用它 → 开；
#   4. 其余（官方客户端 / PyInstaller 产物）→ 关。
try:
    import flet_dropzone as _dropzone
except Exception:
    _dropzone = None


def _custom_client_available():
    if os.environ.get("FLET_PLATFORM", "").strip():
        return True
    try:
        win_dir = Path.cwd() / "build" / "windows"
        return win_dir.is_dir() and any(
            p.suffix.lower() == ".exe" for p in win_dir.iterdir()
        )
    except OSError:
        return False


def _dropzone_on():
    if _dropzone is None:
        return False
    flag = os.environ.get("FVP_DROPZONE", "").strip().lower()
    if flag in ("0", "false", "no"):
        return False
    if flag in ("1", "true", "yes"):
        return True
    return _custom_client_available()


DROPZONE_ON = _dropzone_on()


def pick_drop_path(paths):
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


def _palette(dark):
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


def _rgb(hex_str):
    h = hex_str.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _checker_src(a, b, cell=11):
    """透明立绘的棋盘衬底：一眼分清“这里是透明的”和“这里是白的”。"""
    img = Image.new("RGB", (2, 2))
    img.putpixel((0, 0), _rgb(a))
    img.putpixel((1, 1), _rgb(a))
    img.putpixel((1, 0), _rgb(b))
    img.putpixel((0, 1), _rgb(b))
    img = img.resize((cell * 2, cell * 2), Image.Resampling.NEAREST)
    return _img_src(img)


def _app_icon():
    """返回随程序分发的 Shinku.ico 路径；开发环境取脚本目录，打包后取解包目录。"""
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", "")) or Path(sys.executable).parent
    else:
        base = Path(__file__).parent
    icon = base / "Shinku.ico"
    return str(icon) if icon.exists() else None


def _tip(text):
    """统一提示气泡：悬停 100ms 即显，移开 150ms 快速淡出。"""
    return ft.Tooltip(message=text, wait_duration=100, show_duration=150)


def _expr_suffixes():
    """生成“表情”在各编码组合下的后缀变体，兼容 GBK/BIG5 等误读 SJIS 文件名的拆包结果。"""
    cands = {"表情"}
    for enc in ("shift_jis", "utf-8"):
        try:
            raw = "表情".encode(enc)
        except Exception:
            continue
        for dst in ("gbk", "big5", "cp1252"):
            try:
                cands.add(raw.decode(dst))
            except Exception:
                pass
    return tuple(cands)


EXPR_SUFFIXES = _expr_suffixes()

_THUMB_POOL = ThreadPoolExecutor(max_workers=min(8, (os.cpu_count() or 4)), thread_name_prefix="thumb")


def _is_expr(filename):
    """判断文件名是否为表情差分文件（编码无关）。"""
    return any(filename.endswith(s) for s in EXPR_SUFFIXES)


def _find_expr_info(file_infos, base_name):
    """查找底图对应的表情差分条目：base_<表情后缀>，后缀按编码变体集合精确匹配。"""
    prefix = base_name + "_"
    for i in file_infos:
        if i.get("type") == "hzc" and i["filename"].startswith(prefix):
            if i["filename"][len(prefix):] in EXPR_SUFFIXES:
                return i
    return None


HELP_STEPS = (
    ("打开", "点击「打开 BIN」选择 .bin，或「HZC 文件夹」直接指向解包目录；"
             "也可把 .bin / HZC 文件夹拖到程序图标上直接打开。"
             + ("或把文件直接拖进窗口。" if DROPZONE_ON else "")),
    ("挑选", "左侧按 角色 → 服装 → 动作 展开，每级都有缩略图。"),
    ("预览", "中间是底图舞台：滚轮缩放、拖拽平移，多帧底图用左右箭头翻帧。"),
    ("合成", "点击「差分部件」里的任意一帧表情，右侧即时合成。"),
    ("导出", "「保存当前」导出一张 PNG，「批量导出」把该底图的全部差分一次性铺平。"),
    ("载出", "点角色库标题栏的载出图标或按 Ctrl+W，卸载当前文件回到初始状态。"),
)

HELP_KEYS = (
    ("← →", "上一帧 / 下一帧"),
    ("0", "复位缩放"),
    ("Ctrl+S", "保存当前合成图"),
    ("Ctrl+O", "打开 BIN"),
    ("Ctrl+W", "载出（回到初始状态）"),
    ("L", "切换深浅主题"),
    ("Esc", "关闭对话框"),
)


class ComposerApp:
    def __init__(self, page: ft.Page):
        self.page = page
        self.is_dark = False
        self.ink = _palette(False)

        self.input_file = None
        self.file_infos = []
        self.hierarchy = {}
        self.selected_info = None
        self.selected_filename = None

        self.base_imgs = []
        self.base_idx = 0
        self.part_info = None
        self.part_imgs = []
        self.part_idx = 0
        self.composed_img = None
        # 没有表情差分可用：结果面板与导出链路都走「直接导出底图」这条退路
        self.base_only = False
        self.thumb_refs = []
        self.role_thumb_cache = {}
        # 树的展开状态记在 app 上而不是 tile 闭包里，
        # 这样换主题整树重建后能原样展开回去（否则用户展开的角色/服装会被强制收起）。
        self.open_roles = set()
        self.open_outfits = set()
        self._tree_toggles = {}
        # 角色错峰入场：本次载入的角色外壳（透明度/位移在 _stagger_next 里逐条推）
        self._tree_wraps = []
        self._stagger_i = 0
        self._stagger_gen = -1
        # 重建树期间屏蔽"展开即选中"等级联副作用，见 _outfit_tile.toggle
        self._restoring = False
        # ── 拖放态（画布长在角色库面板上）────────────────────
        # 文件是否正悬在面板上方；文案和错峰入场都看它。
        self._drop_armed = False
        # 画布在册（visible）吗。动画全靠逐帧重绘，见 _drop_open / _tick_drop。
        self._drop_hint_shown = False
        # 画布尺寸：Canvas 不参与排版时不会有尺寸，重新亮起来后要靠它补出第一帧
        self._drop_size = (0.0, 0.0)
        # 整层淡入淡出进度 0→1（算进每个 shape 的颜色 alpha）与它的方向
        self._drop_a = 0.0
        self._drop_dir = 0
        # 配色（accent / err）与光斑弧长
        self._drop_tone = "accent"
        self._drop_dist = 0.0
        # 计时器代数：+1 即可让所有在途的 tick / 错峰链自己作废，
        # 不用去 cancel 具体的 call_later 句柄。
        self._drop_gen = 0
        self._drop_canvas = None
        self._hint_icon = None
        self._hint_title = None
        self._hint_sub = None
        self._hint_idle_sub = ""
        self._hint_state = "idle"
        # 树里所有动作行的选中态控件组；换选时按 filename 统一复原，不记"上一次点了谁"。
        self._action_rows = []
        # 载入代数：载入/载出各 +1。后台解析与缩略图解码都跨"载入中→载出"这段
        # 时间，靠它判定结果是否已经过期——否则载出之后迟到的回调会把状态写回来。
        self._state_gen = 0
        # 载出按钮（_build_tree_column 建）；仅在已载入时可见
        self.unload_btn = None

        # UI 事件循环引用（_load_bin/_toggle_theme 中捕获）：
        # 后台缩略图解码完成后，经由它把“替换+刷新”调度回 UI 线程执行，
        # 避免后台线程直接碰控件导致的更新丢失。
        self._loop = None

        self.file_picker = ft.FilePicker()
        self.clipboard = ft.Clipboard()
        self.page.services = [self.file_picker, self.clipboard]
        # 导入/导出各自记住自己的目录：Windows 原生对话框按进程记忆最后位置，
        # 不分别传 initial_directory 的话，导出一选完，下次打开 bin 就被带去导出目录。
        self.import_dir = None
        self.export_dir = None
        self._snack_seq = 0
        self._snack_overlay = None
        self._snack_toast = None
        # busy 横幅：_parsing_gen 非空表示"这一代解析还在途中"，_busy_seq 是那条卡片
        self._parsing_gen = None
        self._busy_seq = None
        self._setup_page()
        self._build()

    def _setup_page(self):
        p = self.page
        p.title = "FVP Tachie Composer"
        p.window.width = 1280
        p.window.height = 760
        p.window.min_width = 1020
        p.window.min_height = 600
        p.window.title_bar_hidden = True
        icon = _app_icon()
        if icon:
            p.window.icon = icon
        p.padding = 0
        p.spacing = 0
        p.on_keyboard_event = self._on_keyboard
        self._apply_theme()

    def _apply_theme(self):
        p = self.page
        self.ink = _palette(self.is_dark)
        self._checker = _checker_src(self.ink["checker_a"], self.ink["checker_b"])
        theme = ft.Theme(
            color_scheme_seed=self.ink["accent"],
            use_material3=True,
            scrollbar_theme=ft.ScrollbarTheme(
                thumb_visibility=True,
                track_visibility=False,
                thickness=6,
                radius=8,
                min_thumb_length=36,
                interactive=True,
                cross_axis_margin=3,
                main_axis_margin=6,
                thumb_color=ft.Colors.with_opacity(0.45, ft.Colors.ON_SURFACE_VARIANT),
            ),
        )
        p.theme = theme
        p.dark_theme = theme
        p.theme_mode = ft.ThemeMode.DARK if self.is_dark else ft.ThemeMode.LIGHT
        p.bgcolor = self.ink["page"]

    # ── 通用小构件 ──────────────────────────────────────────

    def _panel(self, controls, *, width=None, expand=False, pad_right=6, overlay=None):
        """面板：白卡片压在浅灰页底上，靠 1px 细描边分栏。

        这里刻意不用渐变、纹理和阴影——工具界面要的是边界清楚，
        材质效果只会让三栏看起来糊在一起。

        overlay 是给"贴在面板外框上的动效层"用的（拖放态的画布）。留白必须
        从本体挪进内容层，否则 overlay 拿到的是内容矩形，画出来的框会缩在
        描边里面一圈，看着像另贴了一层。
        """
        ink = self.ink
        pad = ft.Padding.only(left=12, top=10, right=pad_right, bottom=10)
        col = ft.Column(controls, spacing=10, expand=True)
        if overlay is None:
            content = col
        else:
            content = ft.Stack(
                [ft.Container(content=col, padding=pad), overlay],
                expand=True,
                fit=ft.StackFit.EXPAND,
            )
            pad = None
        return ft.Container(
            content=content,
            width=width,
            expand=expand,
            bgcolor=ink["panel"],
            border=ft.Border.all(1, ink["line"]),
            border_radius=RADIUS_PANEL,
            padding=pad,
            clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
        )

    def _section(self, icon, title, right=None):
        ink = self.ink
        return ft.Row(
            [
                ft.Container(
                    ft.Icon(icon, size=15, color=ink["accent"]),
                    width=24, height=24, border_radius=8, bgcolor=ink["accent_soft"],
                    alignment=ft.Alignment.CENTER,
                ),
                ft.Text(title, size=13, weight=ft.FontWeight.W_700, color=ink["text"], expand=True),
                *(right or []),
            ],
            spacing=8,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

    def _stage(self, content):
        """图像舞台：圆角 + 描边 + 棋盘底，并把溢出裁在自己的框里，
        预览图永远不会糊到相邻区块上。"""
        ink = self.ink
        return ft.Container(
            content=content,
            expand=True,
            bgcolor=ink["stage"],
            image=ft.DecorationImage(src=self._checker, fit=ft.BoxFit.NONE,
                                     repeat=ft.ImageRepeat.REPEAT, opacity=0.85),
            border=ft.Border.all(1, ink["line_soft"]),
            border_radius=RADIUS_STAGE,
            clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
        )

    def _meta(self, text=""):
        return ft.Text(text, size=11, color=self.ink["text_dim"],
                       max_lines=1, overflow=ft.TextOverflow.ELLIPSIS)

    def _kbd(self, text):
        """快捷键键帽。"""
        ink = self.ink
        return ft.Container(
            content=ft.Text(text, size=10, weight=ft.FontWeight.W_700, color=ink["text_dim"],
                            font_family="Consolas"),
            padding=ft.Padding.only(left=7, right=7, top=3, bottom=4),
            border_radius=6,
            bgcolor=ink["chip"],
            border=ft.Border.only(
                left=ft.BorderSide(1, ink["line"]),
                top=ft.BorderSide(1, ink["line"]),
                right=ft.BorderSide(1, ink["line_soft"]),
                bottom=ft.BorderSide(2, ink["line_soft"]),
            ),
        )

    def _build(self):
        self._apply_theme()
        p = self.page

        # 拖放反馈的动效全靠逐帧重绘，重建时把状态清干净：
        # overlay 整棵换掉，残留的 shown/进度会让下一次 enter 不动。
        self._drop_hint_shown = False
        self._drop_armed = False
        self._drop_dir = 0
        self._drop_a = 0.0
        self._drop_dist = 0.0
        self._drop_tone = "accent"
        self._drop_gen += 1

        self.title_bar = self._build_title_bar()
        self.tree_col = self._build_tree_column()
        self.base_col = self._build_base_column()
        self.result_col = self._build_result_column()
        self.status_text = ft.Text("就绪", size=11, color=self.ink["text_dim"],
                                   max_lines=1, overflow=ft.TextOverflow.ELLIPSIS)
        self.status_meta = ft.Text("", size=11, color=self.ink["text_dim"])

        status_bar = ft.Container(
            content=ft.Row(
                [
                    ft.Container(width=6, height=6, border_radius=3, bgcolor=self.ink["accent"]),
                    ft.Container(self.status_text, expand=True),
                    self.status_meta,
                ],
                spacing=8,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            height=30,
            padding=ft.Padding.only(left=16, right=16, top=0, bottom=2),
            border=ft.Border.only(top=ft.BorderSide(1, self.ink["line_soft"])),
        )

        body = ft.Container(
            content=ft.Row(
                [
                    ft.Container(self.tree_col, width=300),
                    ft.Container(self.base_col, expand=True),
                    ft.Container(self.result_col, width=372),
                ],
                expand=True,
                spacing=10,
            ),
            expand=True,
            padding=ft.Padding.only(left=12, right=12, top=10, bottom=10),
        )

        root = ft.Column([self.title_bar, body, status_bar], spacing=0, expand=True)
        # 落点是整扇窗口（Dropzone 最外层包一层，见 _drop_wrap）：
        # 文件一进窗口就该亮反馈，等它挪到角色库栏才动会像没接住。
        p.add(self._drop_wrap(root))
        p.update()

    def _drop_wrap(self, content):
        """把整扇窗口包成拖放目标；未启用（官方客户端没有该插件）时原样返回。

        落点铺满全窗：文件一进窗口就该有反应（进场亮面板、松手即清场），
        等它挪到角色库栏才动会让人以为没接住。反馈视觉仍画在角色库面板的画布上（_build_tree_column 的 overlay），落点大、反馈贴着"资源入口"。
        """
        if not DROPZONE_ON:
            return content
        # expand 不可省：Dropzone 是 LayoutControl，没有尺寸时 Dart 侧按 0 高度排版，
        # 整棵树静默消失（白屏、无异常、无日志）。
        return _dropzone.Dropzone(
            expand=True,
            content=content,
            on_dropped=self._on_dropped,
            on_entered=lambda e: self._drag_entered(),
            on_exited=lambda e: self._drag_exited(),
        )

    def _drop_rgba(self, alpha_max=255):
        """当前色调（accent / err_frame）× 淡入进度，拼成 #AARRGGBB。

        Flet 的 hex 是 alpha 在最前，拼反了会被当成另一个色（近乎不透明），
        整块面板变成一坨脏色——这里只有一处拼接，别再手写第二处。
        """
        rgb = _rgb(self.ink["err_frame" if self._drop_tone == "err" else "frame"])
        a = max(0, min(255, int(round(alpha_max * self._drop_a))))
        return "#%02X%02X%02X%02X" % (a, *rgb)

    def _frame_point(self, w, h, m, d):
        """圆角矩形周长上的参数化取点：顺时针，从上边直段的起点开始。

        光斑要贴着框边爬，而 Flet 的 Paint 只有 stroke_dash_pattern、没有
        stroke_dash_offset，没法靠 dash 位移做"行进"，只能自己按弧长算点。
        """
        r = min(RADIUS_PANEL - m, (w - 2 * m) / 2.0, (h - 2 * m) / 2.0)
        lx = max(0.0, w - 2 * m - 2 * r)
        ly = max(0.0, h - 2 * m - 2 * r)
        arc = math.pi * r / 2.0
        d %= 2 * lx + 2 * ly + 8 * arc

        def take(n):
            nonlocal d
            if d <= n:
                return True
            d -= n
            return False

        if take(lx):                                    # 上直边 →
            return (m + r + d, m)
        if take(arc):                                   # 右上圆角
            a = d / arc * math.pi / 2
            return (w - m - r + r * math.sin(a), m + r - r * math.cos(a))
        if take(ly):                                    # 右直边 ↓
            return (w - m, m + r + d)
        if take(arc):                                   # 右下圆角
            a = d / arc * math.pi / 2
            return (w - m - r + r * math.cos(a), h - m - r + r * math.sin(a))
        if take(lx):                                    # 下直边 ←
            return (w - m - r - d, h - m)
        if take(arc):                                   # 左下圆角
            a = d / arc * math.pi / 2
            return (m + r - r * math.sin(a), h - m - r + r * math.cos(a))
        if take(ly):                                    # 左直边 ↑
            return (m, h - m - r - d)
        a = d / arc * math.pi / 2                       # 左上圆角
        return (m + r - r * math.cos(a), m + r - r * math.sin(a))

    def _streak(self, w, h, m, head, color, width):
        """一截沿框边爬的拖尾。按弧长采 4 段折线，过圆角时才不会切角。"""
        pts = [self._frame_point(w, h, m, head - i * DROP_STREAK / 3.0)
               for i in range(4)]
        els = [fc.Path.MoveTo(x=pts[0][0], y=pts[0][1])]
        els += [fc.Path.LineTo(x=p[0], y=p[1]) for p in pts[1:]]
        return fc.Path(
            elements=els,
            paint=ft.Paint(
                style=ft.PaintingStyle.STROKE,
                stroke_width=width,
                stroke_cap=ft.StrokeCap.ROUND,
                color=color,
            ),
        )

    def _drop_shapes(self, w, h):
        """画布的全部内容：底纹 + 辉光边 + 虚线边 + 爬行的光斑（拖尾 + 核心）。

        五个形状都贴面板外框走。整层的淡入淡出不走 Container 的隐式动画
        （0.86 没有 animate_color），而是把进度算进每个 shape 的颜色 alpha，
        所以一切由 Python 逐帧重绘。
        """
        m = DROP_INSET
        if self._drop_a <= 0.0 or w <= m * 2 + 4 or h <= m * 2 + 4:
            return []
        box = dict(x=m, y=m, width=w - 2 * m, height=h - 2 * m,
                   border_radius=ft.BorderRadius.all(RADIUS_PANEL - m))
        return [
            # 底纹 = "我在听"。最深只有 18% 的强调色，不能压过面板里的文字。
            fc.Path(
                elements=[fc.Path.Rect(x=0, y=0, width=w, height=h,
                                       border_radius=ft.BorderRadius.all(RADIUS_PANEL))],
                paint=ft.Paint(style=ft.PaintingStyle.FILL,
                               color=self._drop_rgba(0x2E)),
            ),
            fc.Path(
                elements=[fc.Path.Rect(**box)],
                paint=ft.Paint(style=ft.PaintingStyle.STROKE,
                               stroke_width=6, color=self._drop_rgba(0x33)),
            ),
            # 虚线边 = "落点就是这块面板"。它替代的是面板那 1px 实描边，
            # 所以圆角、位置都必须跟面板本体严丝合缝，否则就又是"贴了一层"。
            fc.Path(
                elements=[fc.Path.Rect(**box)],
                paint=ft.Paint(style=ft.PaintingStyle.STROKE,
                               stroke_width=2, color=self._drop_rgba(),
                               stroke_dash_pattern=[10, 7]),
            ),
            self._streak(w, h, m, self._drop_dist, self._drop_rgba(0x59), 8),
            self._streak(w, h, m, self._drop_dist, self._drop_rgba(), 2.5),
        ]

    def _redraw_drop(self):
        canvas = self._drop_canvas
        if canvas is None:
            return
        w, h = self._drop_size
        canvas.shapes = self._drop_shapes(w, h)
        self._safe_update(canvas)

    def _on_drop_resize(self, e):
        self._drop_size = (float(e.width), float(e.height))
        self._redraw_drop()

    def _tick_drop(self, gen):
        """淡入淡出和光斑爬行共用这一条计时链，只在画布在册时跑。"""
        if gen != self._drop_gen or not self._drop_hint_shown:
            return
        if self._drop_dir > 0:
            self._drop_a = min(1.0, self._drop_a + 1.0 / DROP_IN_FRAMES)
            if self._drop_a >= 1.0:
                self._drop_dir = 0
        elif self._drop_dir < 0:
            self._drop_a = max(0.0, self._drop_a - 1.0 / DROP_OUT_FRAMES)
            if self._drop_a <= 0.0:
                # 走完就收画布：Canvas 会吃掉自己矩形内的点击，留着树就点不动了
                self._drop_dir = 0
                self._close_drop_canvas()
                return
        self._drop_dist += DROP_STEP
        self._redraw_drop()
        self._later(DROP_TICK, self._tick_drop, gen)

    def _drop_open(self):
        """进入拖放态：画布先亮起来，淡入交给 _tick_drop 逐帧推。"""
        if self._drop_canvas is None:
            return
        self._drop_gen += 1              # 在途的淡出链就此作废
        self._drop_hint_shown = True
        self._drop_tone = "accent"
        self._drop_dir = 1
        # 起点不能是 0：alpha=0 时形状整个不画，亮起来的第一帧会是空白板，
        # 白掉一帧的进场看着就是"慢半拍"。从第一帧的量起步。
        self._drop_a = max(self._drop_a, 1.0 / DROP_IN_FRAMES)
        self._drop_canvas.visible = True
        # 亮起来之前没有排版，尺寸靠随后到达的 on_resize（约 10ms）补；
        # 这里先用上一次记下的尺寸画一帧，免得淡入的头两帧是空的。
        self._redraw_drop()
        self._safe_update(self._drop_canvas)
        self._kick_drop()

    def _kick_drop(self):
        """保证有且在跑的计时链——淡入淡出和光斑爬行都吊在这条链上。"""
        self._later(DROP_TICK, self._tick_drop, self._drop_gen)

    def _drop_close(self, immediate=False):
        """退场：淡出后再收。immediate 给"松手快门"用——快门不需要谢幕。"""
        if self._drop_canvas is None:
            return
        if immediate or self._drop_a <= 0.0:
            self._drop_gen += 1
            self._drop_dir = 0
            self._close_drop_canvas()
        else:
            self._drop_dir = -1
            self._kick_drop()

    def _close_drop_canvas(self):
        self._drop_hint_shown = False
        if self._drop_canvas is None:
            return
        self._drop_canvas.shapes = []
        self._drop_canvas.visible = False
        self._safe_update(self._drop_canvas)

    def _hint_stage(self, on):
        """提示文字的舞台调度：拖入期间让它上场（树让位），退场后按"有没有树"归位。

        空态时永远是提示在场；载入过就轮到树。只改 visible 不换 Stack 顺序，
        点击归属不变（提示本来就 expand=True 不吞点击）。
        """
        has_tree = bool(self.tree_list.controls)
        self.tree_list.visible = has_tree and not on
        self.empty_hint.visible = on or not has_tree

    def _drag_entered(self):
        if self._drop_armed:
            return
        self._drop_armed = True
        self._drop_open()
        self._hint_stage(True)       # 已载入时提示被树盖着：先让树让位
        self._set_hint_state("armed")
        self.page.update()

    def _drag_exited(self):
        if not self._drop_armed:
            return
        self._drop_armed = False
        self._drop_close()
        self._set_hint_state("idle")
        self._hint_stage(False)      # 树回来了就归位
        self.page.update()

    def _set_hint_state(self, state):
        """状态文案就地改字：idle / armed / reject / loading 四套说法长在同一个容器里。

        不再新起一层提示——落点上叠东西就又回到"给应用贴取景框"了。
        loading 是松手后的第一帧反馈：旧树已撤、解析还没回来，这段时间
        得有个东西占着场子，不然用户以为没松手成功。
        """
        if self._hint_title is None or state == self._hint_state:
            return
        self._hint_state = state
        ink = self.ink
        if state == "loading":
            self._hint_icon.name = ft.Icons.HOURGLASS_TOP
            self._hint_icon.color = ink["accent"]
            self._hint_title.value = "正在解析…"
            self._hint_title.color = ink["accent"]
            self._hint_sub.value = "松手后马上就好"
        elif state == "armed":
            self._hint_icon.name = ft.Icons.UPLOAD_FILE
            self._hint_icon.color = ink["accent"]
            self._hint_title.value = "松手即载入"
            self._hint_title.color = ink["accent"]
            self._hint_sub.value = "支持 .bin 或整个 HZC 文件夹"
        elif state == "reject":
            self._hint_icon.name = ft.Icons.UPLOAD_FILE
            self._hint_icon.color = ink["err_frame"]
            self._hint_title.value = "拖进来的不是资源"
            self._hint_title.color = ink["err_frame"]
            self._hint_sub.value = "只能放 .bin 或 HZC 文件夹进来"
        else:
            self._hint_icon.name = ft.Icons.FOLDER_OPEN
            self._hint_icon.color = ink["accent"]
            self._hint_title.value = "尚未加载文件"
            self._hint_title.color = ink["text"]
            self._hint_sub.value = self._hint_idle_sub
        self._safe_update(self.empty_hint)

    def _reject_drop(self):
        """拖进来的东西不认：面板边框变红停 160ms，再淡出。

        不弹底部横幅——报错就该发生在落点旁边，用户的视线正停在那儿。
        """
        self._drop_tone = "err"
        self._drop_a = 1.0
        self._hint_stage(True)
        self._set_hint_state("reject")
        self._redraw_drop()
        self.page.update()
        if not self._later(DROP_REJECT_MS / 1000, self._reject_fade):
            self._reject_fade()

    def _reject_fade(self):
        if self._drop_tone != "err":
            return          # 这 160ms 里又有新的进入/拖入，本次拒绝不算数
        self._drop_armed = False
        # 画布没在册（比如拖进来前没触发 enter）就没必要淡出，直接把它清干净
        self._drop_close(immediate=not self._drop_hint_shown)
        self._set_hint_state("idle")
        self._hint_stage(False)
        self.page.update()

    def _safe_update(self, control):
        try:
            control.update()
        except Exception:
            pass

    async def _on_dropped(self, e):
        paths = [getattr(f, "path", None) or getattr(f, "name", None)
                 for f in (getattr(e, "files", None) or [])]
        target = pick_drop_path(paths)
        if not target:
            self._reject_drop()
            return
        # 快门：画布立刻收掉，不谢幕——松手之后的确认交给内容本身（角色逐条长出来）。
        self._drop_armed = False
        self._drop_close(immediate=True)
        # 松手即清场：旧树先撤、"正在解析"当场上场。
        # 否则要等 to_thread 解析完才换内容，中间那 100ms 左右旧树纹丝不动
        # （初始态则是一片没有任何反馈的空窗），用户以为没松手成功。
        self.tree_list.controls.clear()
        self._tree_toggles = {}
        self._action_rows = []
        self._tree_wraps = []
        self._stagger_i = 0
        self._hint_stage(True)
        self._set_hint_state("loading")
        self.page.update()

        gen0 = self._state_gen
        ok = await self._load_bin(target)
        if ok:
            self._set_hint_state("idle")
            return
        if ok is None or self._state_gen != gen0 + 1:
            # 过期（被载出/被另一次载入接管）：界面归接管者管，这里不越权还原。
            return
        # 解析失败：旧数据其实没动过（_load_bin 在写入前就退出了），
        # 把旧树原样长回来；若本来就是空态，_hint_stage(False) 会把提示还回去。
        self._restoring = True
        try:
            self._render_tree()
            self._restore_tree_state()
        finally:
            self._restoring = False
        self._sync_selected_row()
        self._set_hint_state("idle")
        self._hint_stage(False)
        self.page.update()

    # ── Title bar ───────────────────────────────────────────

    def _build_title_bar(self):
        ink = self.ink
        logo = ft.Row(
            [
                ft.Container(
                    ft.Icon(ft.Icons.IMAGE, size=15, color="#ffffff"),
                    width=26, height=26, border_radius=9,
                    bgcolor=ink["accent"],
                    alignment=ft.Alignment.CENTER,
                ),
                ft.Text("FVP Tachie Composer", size=13, weight=ft.FontWeight.W_800, color=ink["text"]),
                ft.Text("立绘查看与合成", size=11, color=ink["text_dim"]),
            ],
            spacing=8,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

        self.file_chip = ft.Container(
            # 标题栏随主题重建，初值必须从当前状态推导，
            # 否则切一次主题就把已加载的文件名刷回"未载入文件"。
            content=ft.Text(Path(self.input_file).name if self.input_file else "未载入文件",
                            size=11, color=ink["text_dim"],
                            max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
            padding=ft.Padding.only(left=10, right=10, top=3, bottom=3),
            border_radius=99,
            bgcolor=ink["chip"],
            border=ft.Border.all(1, ink["line_soft"]),
            tooltip=_tip("当前打开的 BIN / HZC 目录"),
        )

        self.max_btn = ft.IconButton(
            ft.Icons.CROP_SQUARE, on_click=self._toggle_maximize,
            tooltip=_tip("最大化"), icon_size=17, icon_color=ink["text_dim"],
        )

        right = ft.Row(
            [
                ft.IconButton(ft.Icons.HELP_OUTLINE, on_click=self._help, tooltip=_tip("使用说明"),
                              icon_size=17, icon_color=ink["text_dim"]),
                ft.IconButton(
                    ft.Icons.DARK_MODE if self.is_dark else ft.Icons.LIGHT_MODE,
                    on_click=self._toggle_theme, tooltip=_tip("切换主题 (L)"),
                    icon_size=17, icon_color=ink["text_dim"],
                ),
                ft.IconButton(ft.Icons.MINIMIZE, on_click=self._minimize, tooltip=_tip("最小化"),
                              icon_size=17, icon_color=ink["text_dim"]),
                self.max_btn,
                ft.IconButton(
                    ft.Icons.CLOSE, on_click=self._close, tooltip=_tip("关闭"),
                    icon_size=17, icon_color=ink["text_dim"], hover_color="#e0526b",
                ),
            ],
            spacing=0,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

        bar = ft.Container(
            content=ft.Row(
                [logo, ft.Container(expand=True), self.file_chip, ft.Container(width=12), right],
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            height=46,
            padding=ft.Padding.only(left=14, right=8, top=0, bottom=0),
            bgcolor=ink["panel"],
            border=ft.Border.only(bottom=ft.BorderSide(1, ink["line"])),
        )
        return ft.WindowDragArea(bar, maximizable=True)

    # ── Left: tree ──────────────────────────────────────────

    def _build_tree_column(self):
        ink = self.ink
        # 载出：卸载当前 bin，回到刚打开时的空状态。载入前不显示，
        # 否则"载出"在一个什么都没载的界面上是个死按钮。
        unload_btn = ft.IconButton(ft.Icons.EJECT, on_click=self._unload_bin,
                                   tooltip=_tip("载出 (Ctrl+W)"), icon_size=19,
                                   icon_color=ink["text_dim"], visible=bool(self.input_file))
        self.unload_btn = unload_btn
        header = self._section(
            ft.Icons.PEOPLE, "角色库",
            right=[
                ft.IconButton(ft.Icons.FOLDER_OPEN, on_click=self._open_bin,
                              tooltip=_tip("打开 BIN (Ctrl+O)"), icon_size=19, icon_color=ink["accent"]),
                ft.IconButton(ft.Icons.FOLDER, on_click=self._open_dir,
                              tooltip=_tip("打开 HZC 文件夹"), icon_size=19, icon_color=ink["accent"]),
                unload_btn,
            ],
        )

        # 拖放态的画布：贴面板外框画底纹/虚线/光斑。idle 时 shapes 空着且
        # visible=False——Canvas 会吃掉自己矩形内的点击，留着不关就点不动树。
        self._drop_canvas = fc.Canvas(
            top=0, left=0, right=0, bottom=0,
            shapes=[],
            visible=False,
            on_resize=self._on_drop_resize,
        ) if DROPZONE_ON else None

        self._hint_icon = ft.Icon(ft.Icons.FOLDER_OPEN, size=26, color=ink["accent"])
        self._hint_title = ft.Text("尚未加载文件", size=13,
                                   weight=ft.FontWeight.W_600, color=ink["text"])
        self._hint_idle_sub = ("点右上角的 打开 BIN / 打开 HZC 图标\n或按 Ctrl+O 选择文件"
                               + ("\n或把文件拖进窗口" if DROPZONE_ON else ""))
        self._hint_sub = ft.Text(self._hint_idle_sub, size=11, color=ink["text_dim"],
                                 text_align=ft.TextAlign.CENTER)
        self._hint_state = "idle"

        self.empty_hint = ft.Container(
            content=ft.Column(
                [
                    ft.Container(
                        self._hint_icon,
                        width=58, height=58, border_radius=20, bgcolor=ink["accent_soft"],
                        alignment=ft.Alignment.CENTER,
                    ),
                    self._hint_title,
                    # 载入入口只留标题栏那两个图标，空状态不再重复一套按钮，
                    # 这里只负责告诉用户去哪儿找（自定义客户端下多一句拖入）。
                    self._hint_sub,
                ],
                spacing=8,
                tight=True,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            alignment=ft.Alignment.CENTER,
            expand=True,
        )

        # padding.right = 滚动条通道，树内容永远不进这条
        self.tree_list = ft.ListView(spacing=2, expand=True, scroll=ft.ScrollMode.AUTO,
                                     padding=ft.Padding.only(left=0, right=GUTTER, top=2, bottom=2))
        self.tree_list.visible = False

        body = ft.Stack([self.empty_hint, self.tree_list], expand=True, fit=ft.StackFit.EXPAND)

        return self._panel(
            [header, ft.Container(expand=True, content=body)],
            pad_right=6,
            overlay=self._drop_canvas,
        )

    # ── Center: base preview + parts ────────────────────────

    def _build_base_column(self):
        ink = self.ink
        self.base_hint = ft.Container(
            content=ft.Column(
                [
                    ft.Container(
                        ft.Icon(ft.Icons.IMAGE_OUTLINED, size=30, color=ink["text_dim"]),
                        width=64, height=64, border_radius=22, bgcolor=ink["chip"],
                        alignment=ft.Alignment.CENTER,
                    ),
                    ft.Text("在左侧选择底图", size=13, weight=ft.FontWeight.W_600, color=ink["text"]),
                    ft.Text("滚轮缩放 · 拖拽平移 · ←/→ 切帧", size=11, color=ink["text_dim"]),
                ],
                spacing=10,
                tight=True,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            alignment=ft.Alignment.CENTER,
            expand=True,
        )

        self.base_image = ft.Image(src=TRANSPARENT, fit=ft.BoxFit.CONTAIN, expand=True)
        self.base_viewer = ft.InteractiveViewer(
            content=ft.Container(self.base_image, expand=True, alignment=ft.Alignment.CENTER),
            min_scale=1.0,
            max_scale=12.0,
            constrained=True,
            expand=True,
        )

        base_area = self._stage(ft.Stack([self.base_viewer, self.base_hint],
                                         expand=True, fit=ft.StackFit.EXPAND))
        self.base_meta = self._meta()

        self.frame_label = ft.Text("", size=11, color=ink["text_dim"])
        self.prev_btn = ft.IconButton(ft.Icons.CHEVRON_LEFT, on_click=self._prev_frame,
                                      icon_size=19, icon_color=ink["text_dim"])
        self.next_btn = ft.IconButton(ft.Icons.CHEVRON_RIGHT, on_click=self._next_frame,
                                      icon_size=19, icon_color=ink["text_dim"])
        frame_nav = ft.Row(
            [self.prev_btn, self.frame_label, self.next_btn],
            alignment=ft.MainAxisAlignment.CENTER,
            spacing=6,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )
        self.frame_nav_holder = ft.Container(frame_nav, visible=False)

        self.part_count = ft.Text("", size=11, color=ink["text_dim"])
        part_header = self._section(
            ft.Icons.TUNE, "差分部件",
            right=[
                self.part_count,
                ft.IconButton(ft.Icons.FIT_SCREEN, on_click=self._reset_zoom,
                              icon_size=17, icon_color=ink["text_dim"], tooltip=_tip("复位缩放 (0)")),
            ],
        )

        self.part_hint_text = ft.Text(PART_HINT_IDLE, size=11, color=ink["text_dim"])
        self.part_hint = ft.Container(
            content=ft.Row(
                [
                    ft.Icon(ft.Icons.TUNE, size=18, color=ink["text_dim"]),
                    self.part_hint_text,
                ],
                spacing=8,
                tight=True,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            height=PART_STRIP_H,
            alignment=ft.Alignment.CENTER,
            border=ft.Border.all(1, ink["line_soft"]),
            border_radius=RADIUS_TILE,
        )

        # 横向列表的滚动条在底部：bottom padding 就是它的通道
        self.thumb_list = ft.ListView(
            spacing=10, horizontal=True, height=PART_STRIP_H,
            auto_scroll=False, scroll=ft.ScrollMode.AUTO,
            padding=ft.Padding.only(left=1, right=1, top=12, bottom=GUTTER),
        )
        self.thumb_list.visible = False

        top = ft.Column(
            [
                self._section(ft.Icons.IMAGE_OUTLINED, "底图预览", right=[self.base_meta]),
                base_area,
                self.frame_nav_holder,
            ],
            spacing=8,
            expand=True,
        )

        bottom = ft.Column([part_header, self.part_hint, self.thumb_list], spacing=8)

        return self._panel([top, bottom], expand=True, pad_right=10)

    # ── Right: result ───────────────────────────────────────

    def _build_result_column(self):
        ink = self.ink
        self.result_hint = ft.Container(
            content=ft.Column(
                [
                    ft.Container(
                        ft.Icon(ft.Icons.PALETTE_OUTLINED, size=24, color=ink["text_dim"]),
                        width=54, height=54, border_radius=18, bgcolor=ink["chip"],
                        alignment=ft.Alignment.CENTER,
                    ),
                    ft.Text("点击部件帧后显示合成结果", size=11,
                            color=ink["text_dim"], text_align=ft.TextAlign.CENTER),
                ],
                spacing=8,
                tight=True,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            alignment=ft.Alignment.CENTER,
            expand=True,
        )

        self.result_image = ft.Image(src=TRANSPARENT, fit=ft.BoxFit.CONTAIN, expand=True)
        self.result_viewer = ft.InteractiveViewer(
            content=ft.Container(self.result_image, expand=True, alignment=ft.Alignment.CENTER),
            min_scale=1.0,
            max_scale=12.0,
            constrained=True,
            expand=True,
        )
        result_area = self._stage(ft.Stack([self.result_viewer, self.result_hint],
                                           expand=True, fit=ft.StackFit.EXPAND))
        self.result_meta = self._meta()

        # 主按钮用"淡底+蓝字"的 tonal，不用实心蓝块：
        # 整窗是白卡片压浅灰底，一个饱和色大块会把视线从立绘上抢走。
        self.save_btn = ft.FilledTonalButton(
            "保存当前", icon=ft.Icons.SAVE_ALT, on_click=self._save_current, disabled=True,
            tooltip=_tip("导出这一张 PNG (Ctrl+S)"),
            style=ft.ButtonStyle(bgcolor=ink["accent_soft"], color=ink["ok_fg"],
                                 elevation=0,
                                 padding=ft.Padding.symmetric(horizontal=12, vertical=10),
                                 shape=ft.RoundedRectangleBorder(radius=10)),
            expand=True,
        )
        self.batch_btn = ft.OutlinedButton(
            "批量导出", icon=ft.Icons.DOWNLOAD, on_click=self._batch_export, disabled=True,
            tooltip=_tip("把该底图的全部差分铺平成 PNG"),
            style=ft.ButtonStyle(color=ink["text"], bgcolor=ink["panel"],
                                 side=ft.BorderSide(1, ink["line"]),
                                 elevation=0,
                                 padding=ft.Padding.symmetric(horizontal=12, vertical=9),
                                 shape=ft.RoundedRectangleBorder(radius=10)),
            expand=True,
        )

        btn_bar = ft.Row([self.save_btn, self.batch_btn], spacing=8)

        return self._panel(
            [
                self._section(ft.Icons.PALETTE_OUTLINED, "合成结果", right=[self.result_meta]),
                result_area,
                btn_bar,
            ],
            pad_right=10,
        )

    # ── Title bar events ────────────────────────────────────

    async def _minimize(self, e):
        self.page.window.minimized = True
        self.page.update()

    async def _toggle_maximize(self, e):
        self.page.window.maximized = not self.page.window.maximized
        maximized = bool(self.page.window.maximized)
        if getattr(self, "max_btn", None):
            self.max_btn.icon = ft.Icons.FILTER_NONE if maximized else ft.Icons.CROP_SQUARE
            self.max_btn.tooltip = _tip("还原" if maximized else "最大化")
        self.page.update()

    async def _close(self, e):
        await self.page.window.destroy()

    def _help(self, e):
        ink = self.ink

        def step_row(idx, title, body):
            return ft.Row(
                [
                    ft.Container(
                        ft.Text(str(idx), size=11, weight=ft.FontWeight.W_800, color=ink["accent"]),
                        width=22, height=22, border_radius=8, bgcolor=ink["accent_soft"],
                        alignment=ft.Alignment.CENTER,
                    ),
                    ft.Column(
                        [
                            ft.Text(title, size=12, weight=ft.FontWeight.W_700, color=ink["text"]),
                            ft.Text(body, size=11, color=ink["text_dim"]),
                        ],
                        spacing=1, expand=True,
                    ),
                ],
                spacing=10,
                vertical_alignment=ft.CrossAxisAlignment.START,
            )

        def key_row(keys, desc):
            return ft.Row(
                [self._kbd(keys), ft.Text(desc, size=11, color=ink["text_dim"])],
                spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER,
            )

        content = ft.Column(
            [
                *[step_row(i + 1, t, b) for i, (t, b) in enumerate(HELP_STEPS)],
                ft.Divider(height=14, color=ink["line_soft"]),
                ft.Text("快捷键", size=12, weight=ft.FontWeight.W_700, color=ink["text"]),
                *([key_row(k, d) for k, d in HELP_KEYS][:4]),
                ft.Row([*[key_row(k, d) for k, d in HELP_KEYS[4:]]], spacing=18, wrap=True),
            ],
            spacing=8,
            width=430,
            scroll=ft.ScrollMode.AUTO,
            height=430,
        )
        self.page.show_dialog(
            ft.AlertDialog(
                modal=True,
                bgcolor=ink["panel"],
                title=ft.Text("使用说明", weight=ft.FontWeight.W_800, color=ink["text"]),
                content=content,
                actions=[
                    ft.TextButton("知道了", on_click=lambda e: self.page.pop_dialog()),
                ],
                actions_alignment=ft.MainAxisAlignment.END,
            )
        )
        self.page.update()

    async def _toggle_theme(self, e):
        self._capture_loop()
        self.is_dark = not self.is_dark
        self._apply_theme()
        self.page.clean()
        self._build()
        if self.input_file:
            self._restoring = True
            try:
                self._render_tree()
                self._restore_tree_state()
            finally:
                self._restoring = False
            self._sync_selected_row()
            self._restore_preview()
            self.status_text.value = f"已加载 {Path(self.input_file).name}"
        self.page.update()

    # ── Status ──────────────────────────────────────────────

    def _later(self, delay, callback, *args):
        """在 UI 线程延迟执行（替代后台 sleep 线程，后者跨线程碰 UI 会丢更新甚至破坏 session）。"""
        loop = getattr(self, "_loop", None)
        if loop is None:
            try:
                loop = asyncio.get_running_loop()
                self._loop = loop
            except RuntimeError:
                loop = None
        if loop is not None:
            try:
                loop.call_later(delay, callback, *args)
                return True
            except Exception:
                pass
        return False

    def _snack(self, msg, error=False, folder=None, busy=False, ttl=None):
        """底部居中的过程/结果横幅。

        三种变体：busy（转圈、不自退，只给"真的等了"的操作）、ok（到点自退）、
        err（常驻、带复制）。返回 seq，调用方可以用 _snack_is_showing(seq) 判断
        自己那条还在不在，从而决定要不要用结果去替换它。
        """
        self._snack_seq = getattr(self, "_snack_seq", 0) + 1
        seq = self._snack_seq
        ink = self.ink
        if busy:
            bg, fg = ink["accent_soft"], ink["text"]
        else:
            bg = ink["err_bg"] if error else ink["ok_bg"]
            fg = ink["err_fg"] if error else ink["ok_fg"]
        # 短文案保持自然宽度，长文案限宽换行——不然会撑成一条横跨整窗的横幅
        label = ft.Text(msg, size=12, color=fg, weight=ft.FontWeight.W_500,
                        max_lines=3, overflow=ft.TextOverflow.ELLIPSIS)
        if len(msg) > 34:
            label = ft.Container(label, width=300)
        if busy:
            # 转圈由 Dart 侧驱动，Python 这边零开销，不用自己排帧
            lead = ft.ProgressRing(width=16, height=16, stroke_width=2,
                                   color=ink["accent"])
        else:
            lead = ft.Icon(ft.Icons.ERROR if error else ft.Icons.CHECK_CIRCLE,
                           size=17, color=fg)
        row = [lead, label]
        if not error and folder:
            async def open_dir(e, _d=folder):
                self._open_folder(_d)
            row.append(ft.TextButton("打开文件夹", on_click=open_dir, style=ft.ButtonStyle(color=fg)))
        if error:
            copy_btn = ft.TextButton("复制报错", style=ft.ButtonStyle(color=fg))

            async def copy_err(e, _m=msg, _b=copy_btn):
                try:
                    await self.clipboard.set(_m)
                    _b.text = "已复制"
                    _b.update()
                except Exception:
                    pass

            copy_btn.on_click = copy_err
            row.append(copy_btn)
        if not busy:
            row.append(ft.IconButton(ft.Icons.CLOSE, icon_size=15, icon_color=fg,
                                     tooltip=_tip("关闭"),
                                     on_click=lambda e, _s=seq: self._dismiss_snack(_s)))
        toast = ft.Container(
            content=ft.Row(row, spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            bgcolor=bg,
            border_radius=12,
            border=ft.Border.all(1, ink["line"]),
            padding=ft.Padding.only(left=14, right=6, top=8, bottom=8),
            shadow=ft.BoxShadow(blur_radius=22, spread_radius=1, color="#00000055"),
            opacity=0.0,
            offset=ft.Offset(0, 14),
            animate=ft.Animation(ANIM_VEIL, ft.AnimationCurve.EASE_OUT),
        )
        # 居中下方、从底部浮起：右下角那版既压着侧栏操作区，又不在视线落点上，
        # 基本注意不到。外层 Container 只负责定位、没有底色，命中的只有卡片本身，
        # 不会像整条 left=0/right=0 的横幅那样把一整排控件的点击吞掉。
        overlay = ft.Stack(
            [ft.Container(toast, left=0, right=0, bottom=38,
                          alignment=ft.Alignment.CENTER)],
            top=0, left=0, right=0, bottom=0,
        )
        try:
            if self._snack_overlay in self.page.overlay:
                self.page.overlay.remove(self._snack_overlay)
        except Exception:
            pass
        self._snack_overlay = overlay
        self._snack_toast = toast
        self.page.overlay.append(overlay)
        self.page.update()

        # 先按初始状态（透明+下偏）建好控件，下一帧再改属性，否则没有动画起点
        if not self._later(0.02, self._set_snack_alpha, seq, True):
            self._set_snack_alpha(seq, True)

        # 报错不自动消失（要留时间复制）；busy 也不自退，等结果来替换它；
        # 其余默认 2 秒，慢载入的结果横幅只留 1 秒——它只是"刚才那次没白等"的补话。
        if busy:
            self._busy_seq = seq
        elif not error:
            self._later(2.0 if ttl is None else ttl, self._dismiss_snack, seq)
        return seq

    def _snack_is_showing(self, seq):
        """seq 那条横幅是否还是当前正在显示的那条。"""
        return seq is not None and seq == self._snack_seq \
            and self._snack_overlay in self.page.overlay

    def _set_snack_alpha(self, seq, on):
        if seq != self._snack_seq or self._snack_toast is None:
            return
        toast = self._snack_toast
        toast.opacity = 1.0 if on else 0.0
        toast.offset = ft.Offset(0, 0 if on else 14)
        self._safe_update(toast)

    def _dismiss_snack(self, seq):
        try:
            if seq != self._snack_seq or self._snack_overlay not in self.page.overlay:
                return
            self._set_snack_alpha(seq, False)
            if not self._later(ANIM_VEIL / 1000 + 0.03, self._remove_snack, seq):
                self._remove_snack(seq)
        except Exception:
            pass

    def _remove_snack(self, seq):
        try:
            if seq == self._snack_seq and self._snack_overlay in self.page.overlay:
                self.page.overlay.remove(self._snack_overlay)
                self.page.update()
        except Exception:
            pass

    def _open_folder(self, path):
        import platform
        import subprocess
        try:
            if platform.system() == "Windows":
                os.startfile(path)
            elif platform.system() == "Darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception as ex:
            self._snack(f"打开文件夹失败: {ex}", error=True)

    def _set_status(self, text):
        self.status_text.value = text
        self.page.update()

    def _set_status_meta(self):
        if not self.hierarchy:
            self.status_meta.value = ""
            return
        hzc = sum(1 for i in self.file_infos if i.get("type") == "hzc")
        self.status_meta.value = f"{len(self.hierarchy)} 角色 · {hzc} 图像"
        self.page.update()

    # ── Open BIN ────────────────────────────────────────────

    async def _open_bin(self, e):
        try:
            result = await self.file_picker.pick_files(
                dialog_title="选择 BIN 文件",
                file_type=ft.FilePickerFileType.CUSTOM,
                allowed_extensions=["bin"],
                allow_multiple=False,
                initial_directory=self.import_dir,
            )
        except Exception as ex:
            self._snack(f"打开文件对话框失败: {ex}", error=True)
            return
        if not result:
            return
        path = result[0].path
        if not path:
            return
        await self._load_bin(path)

    async def _open_dir(self, e):
        try:
            result = await self.file_picker.get_directory_path(
                dialog_title="选择包含 HZC 文件的文件夹",
                initial_directory=self.import_dir,
            )
        except Exception as ex:
            self._snack(f"打开文件夹对话框失败: {ex}", error=True)
            return
        if not result:
            return
        await self._load_bin(str(result))

    async def _load_bin(self, path):
        self._capture_loop()
        # 新一轮载入使上一轮（含进行中的解析/缩略图）全部作废
        self._state_gen += 1
        gen = self._state_gen
        src = Path(path)
        self.import_dir = str(src if src.is_dir() else src.parent)
        # 解析失败时状态栏要退回原状——"正在解析…"不能挂在那儿当永久伤。
        prev_status = self.status_text.value
        prev_meta = self.status_meta.value
        self._set_status("正在解析…")
        # 快路径不该被打扰：只有解析真的超过 BUSY_DELAY 才补一条"正在解析"，
        # 完成时再用结果替换它（成功 1 秒自退，失败常驻）。
        self._parsing_gen = gen
        self._busy_seq = None
        self._later(BUSY_DELAY, self._show_parsing_busy, gen, Path(path).name)

        def work():
            if Path(path).is_dir():
                infos = parse_dir_infos(path)
            else:
                infos = parse_bin_info_extended(path)
            hier = {}
            for info in infos:
                if info["type"] != "hzc":
                    continue
                parts = info["filename"].split("_")
                if len(parts) >= 2 and parts[0] == "CHR":
                    role = parts[1]
                    outfit = parts[3] if len(parts) >= 4 else "默认"
                    # Handle ru suffix for silhouette files
                    if parts[-1].lower() == "ru":
                        outfit = f"{outfit}_ru"
                else:
                    role = info["filename"]
                    outfit = "默认"
                hier.setdefault(role, {}).setdefault(outfit, []).append(info)
            return infos, hier

        try:
            infos, hier = await asyncio.to_thread(work)
        except Exception as ex:
            self._parsing_gen = None
            self.status_text.value = prev_status
            self.status_meta.value = prev_meta
            self._snack(f"解析失败: {ex}", error=True)
            self._busy_seq = None
            self.page.update()
            return False
        self._parsing_gen = None
        if gen != self._state_gen:
            # 解析期间被载出/被另一次载入接管：这份结果已经过期，丢弃。
            # 但"正在解析"的转圈卡片不能留着，否则它会一直转下去。
            if self._snack_is_showing(self._busy_seq):
                self._dismiss_snack(self._busy_seq)
            self._busy_seq = None
            return None

        self.input_file = path
        self.file_infos = infos
        self.hierarchy = hier
        self.selected_info = None
        self.selected_filename = None
        self._clear_previews()
        # 只在"真的载入了一份新东西"时错峰浮入；换主题走的是默认整棵直出，
        # 不然每切一次深浅色都要把树重播一遍动画。
        self._render_tree(stagger=True)
        self._expand_role_tiles(gen)
        hzc = sum(1 for i in infos if i["type"] == "hzc")
        self.file_chip.content.value = Path(path).name
        if self.unload_btn is not None:
            self.unload_btn.visible = True
        self._set_status(f"{Path(path).name} — {len(hier)} 角色, {hzc} 图像")
        self._set_status_meta()
        # 只有真的弹过"正在解析"，才补一句结果；快路径全程安静
        if self._snack_is_showing(self._busy_seq):
            self._snack(f"已载入 {Path(path).name} — {len(hier)} 角色 / {hzc} 图像",
                        ttl=1.0)
        self._busy_seq = None
        return True

    def _show_parsing_busy(self, gen, name):
        """解析超过 BUSY_DELAY 才走到这里；已经解析完就不弹任何东西。"""
        if gen != self._parsing_gen:
            return
        self._busy_seq = self._snack(f"正在解析 {name}…", busy=True)

    async def _unload_bin(self, e=None):
        """载出：卸载当前文件，把应用恢复到刚打开时的初始状态。

        import_dir/export_dir 是对话框的记忆位置，不属于"载入态"，保留；
        其余由 _load_bin 写入的状态全部复位，UI 回到 empty_hint 空状态。
        """
        if not self.input_file:
            self._snack("当前没有载入任何文件")
            return
        old = Path(self.input_file).name
        # 作废在途解析与缩略图解码，迟到的回调会因代数不符而自弃
        self._state_gen += 1

        self.input_file = None
        self.file_infos = []
        self.hierarchy = {}
        self.selected_info = None
        self.selected_filename = None
        self.base_idx = 0
        self.part_idx = 0
        self.role_thumb_cache = {}

        self._clear_previews()
        # _render_tree 只会把 empty_hint 藏起来，恢复必须在这里做（回初始态的根因点）
        self.tree_list.controls.clear()
        self._tree_toggles = {}
        self._action_rows = []
        self.tree_list.visible = False
        self.empty_hint.visible = True
        # 解析中被载出：别把"正在解析"带进初始态。
        self._set_hint_state("idle")

        self.file_chip.content.value = "未载入文件"
        if self.unload_btn is not None:
            self.unload_btn.visible = False
        self.status_meta.value = ""
        self._set_status("就绪")
        self.page.update()
        self._snack(f"已载出: {old}")

    def _clear_previews(self):
        self.base_imgs = []
        self.part_imgs = []
        self.part_info = None
        self.composed_img = None
        self.base_only = False
        self.thumb_refs = []
        self.open_roles = set()
        self.open_outfits = set()
        self.base_image.src = TRANSPARENT
        self.result_image.src = TRANSPARENT
        self.base_hint.visible = True
        self.result_hint.visible = True
        self.base_meta.value = ""
        self.result_meta.value = ""
        self.frame_label.value = ""
        self.frame_nav_holder.visible = False
        self.thumb_list.controls.clear()
        self.thumb_list.visible = False
        self.part_hint.visible = True
        self.part_hint_text.value = PART_HINT_IDLE
        self.part_count.value = ""
        self.save_btn.disabled = True
        self.batch_btn.disabled = True

    # ── Tree ────────────────────────────────────────────────

    def _render_tree(self, stagger=False):
        self.tree_list.controls.clear()
        self._tree_toggles = {}
        # 旧行容器已经不在树里了，控件组必须一起丢，否则换选时会去刷废弃控件。
        self._action_rows = []
        self._tree_wraps = []
        for role, outfits in sorted(self.hierarchy.items()):
            tile = self._role_tile(role, outfits)
            if not stagger:
                self.tree_list.controls.append(tile)
                continue
            # 入场动画套一层外壳：ListTile 自己不吃 opacity/offset。
            # 这里只给 animate=Animation(...)，不显式写 animate_size/position 之类
            # ——那几个显式开关在这版 flet 上会把整块内容弄得不渲染。
            wrap = ft.Container(
                tile,
                opacity=0.0,
                offset=ft.Offset(0, STAGGER_RISE),
                animate=ft.Animation(ANIM_ROW, ft.AnimationCurve.EASE_OUT),
            )
            self._tree_wraps.append(wrap)
            self.tree_list.controls.append(wrap)
        self.tree_list.visible = True
        self.empty_hint.visible = False
        self.page.update()

    def _expand_role_tiles(self, gen):
        """角色逐条错峰浮入：文件进来了，动的是内容本身，而不是再盖一层提示。

        必须等 _render_tree 的 page.update() 把 opacity=0 送出去之后再推第二帧，
        同一帧里既创建又改终值，Flutter 只会按最终状态建控件，动画没有起点。
        """
        self._stagger_gen = gen
        self._stagger_i = 0
        # 起步让出一帧：_render_tree 的 opacity=0 得先落到客户端，动画才有起点
        self._later(STAGGER_MS, self._stagger_step)

    def _stagger_step(self):
        if self._stagger_gen != self._state_gen:
            return                      # 这棵树已被换文件/载出作废，控件都丢了
        if self._stagger_i >= len(self._tree_wraps):
            return
        if self._drop_armed:
            self._settle_stagger()      # 中途又拖进来：剩下的直接落终点，不留半透明
            return
        wrap = self._tree_wraps[self._stagger_i]
        wrap.opacity = 1.0
        wrap.offset = ft.Offset(0, 0)
        self._safe_update(wrap)
        self._stagger_i += 1
        self._later(STAGGER_MS, self._stagger_step)

    def _settle_stagger(self):
        for wrap in self._tree_wraps[self._stagger_i:]:
            wrap.opacity = 1.0
            wrap.offset = ft.Offset(0, 0)
        self._stagger_i = len(self._tree_wraps)
        self._safe_update(self.tree_list)

    def _restore_tree_state(self):
        """按记录重新展开角色/服装（角色先开，服装的 toggle 才存在）。"""
        for kind, names, key_of in (("role", self.open_roles, lambda n: ("role", n)),
                                    ("outfit", self.open_outfits, lambda n: ("outfit", n))):
            for name in list(names):
                entry = self._tree_toggles.get(key_of(name))
                if entry is None:
                    continue
                state, tog = entry
                if not state["open"]:
                    tog(None)

    def _restore_preview(self):
        """换主题后按缓存的 PIL 图重建预览：不重新读盘，也不重置帧号/差分选择。"""
        if self.base_imgs:
            self._show_base_frame()
        if self.part_imgs:
            self.thumb_refs = []
            self.thumb_list.controls.clear()
            for idx, img in enumerate(self.part_imgs):
                self.thumb_list.controls.append(self._make_thumb(img, idx))
            self.part_count.value = f"{len(self.part_imgs)} 帧"
            self.part_hint.visible = False
            self.thumb_list.visible = True
        if self.composed_img is not None and self.part_info:
            self.result_image.src = _img_src(self.composed_img)
            self.result_hint.visible = False
            ox = self.part_info.get("offset_x", 0)
            oy = self.part_info.get("offset_y", 0)
            self.result_meta.value = (f"{self.composed_img.size[0]}×"
                                      f"{self.composed_img.size[1]} · 偏移 {ox:+d}/{oy:+d}")
            self.save_btn.disabled = False
            self.batch_btn.disabled = False
        elif self.base_only and self.base_imgs:
            # 无差分底图模式：镜像与两按钮由 _show_base_frame 之外单独兜底
            img = self.base_imgs[self.base_idx]
            self.result_image.src = _img_src(img)
            self.result_hint.visible = False
            self.result_meta.value = f"{img.size[0]}×{img.size[1]} · 底图（无表情差分）"
            self.save_btn.disabled = False
            self.batch_btn.disabled = False

    def _make_chevron(self, size=16):
        """带旋转方向动画的展开箭头：收起朝右，展开旋转为朝下。"""
        return ft.AnimatedSwitcher(
            ft.Icon(ft.Icons.CHEVRON_RIGHT, size=size, color=self.ink["text_dim"]),
            transition=ft.AnimatedSwitcherTransition.ROTATION,
            duration=180,
            reverse_duration=180,
            switch_in_curve=ft.AnimationCurve.EASE_IN_OUT,
            switch_out_curve=ft.AnimationCurve.EASE_IN_OUT,
        )

    def _set_chevron(self, holder, open_, size=16):
        holder.content = ft.Icon(
            ft.Icons.EXPAND_MORE if open_ else ft.Icons.CHEVRON_RIGHT,
            size=size,
            color=self.ink["text_dim"],
        )

    def _role_tile(self, role, outfits):
        state = {"open": False}
        count = sum(len([i for i in infos if not _is_expr(i["filename"])]) for infos in outfits.values())
        chevron = self._make_chevron(16)
        header_pending = []
        child_toggles = []

        body = ft.Column(spacing=0, visible=False)
        for outfit, infos in sorted(outfits.items()):
            body.controls.append(self._outfit_tile(outfit, infos, role=role,
                                                   header_registry=header_pending,
                                                   _expand_box=child_toggles))

        def toggle(e):
            state["open"] = not state["open"]
            if state["open"]:
                self.open_roles.add(role)
            else:
                self.open_roles.discard(role)
            body.visible = state["open"]
            self._set_chevron(chevron, state["open"], 16)
            if state["open"]:
                if header_pending:
                    self._flush_tasks(header_pending)
                if len(child_toggles) == 1:
                    tog, bod = child_toggles[0]
                    if not bod.visible:
                        tog(None)
            body.update()
            chevron.update()
            self._set_tile_bg(header, state["open"])

        self._tree_toggles[("role", role)] = (state, toggle)

        thumb_widget = self._get_role_thumbnail(role, outfits)

        header = ft.ListTile(
            leading=thumb_widget,
            title=ft.Text(role, size=13, weight=ft.FontWeight.W_700, color=self.ink["text"],
                          max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
            subtitle=ft.Text(f"{count} 动作 · {len(outfits)} 服装", size=10, color=self.ink["text_dim"]),
            trailing=chevron,
            on_click=toggle,
            content_padding=ft.Padding.only(left=4, right=2, top=0, bottom=0),
            dense=True,
            min_height=46,
            hover_color=self.ink["hover"],
            bgcolor="#00000000",
        )
        return ft.Column([header, body], spacing=0)

    def _set_tile_bg(self, tile, on):
        try:
            tile.bgcolor = self.ink["chip"] if on else "#00000000"
            tile.update()
        except Exception:
            pass

    def _capture_loop(self):
        """在 async 入口捕获当前事件循环（纯标准库，不依赖 Flet 版本 API）。"""
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            pass

    def _push_thumb(self, placeholder, widget, cache_key):
        """必须在 UI 线程执行：替换占位内容并做控件级精确推送。"""
        try:
            self.role_thumb_cache[cache_key] = widget
            placeholder.content = widget
            # 占位底色是给"还没解码出来"那一下用的：图到了还留着，
            # 半透明的立绘就会透出一个人形圆角块，看着像凭空多了个深色描边。
            placeholder.bgcolor = "#00000000"
            placeholder.update()
        except Exception:
            pass

    def _queue_thumb(self, placeholder, info, size, cache_key):
        """后台线程只做解码；完成后经事件循环把替换+刷新调度回 UI 线程。"""
        gen = self._state_gen  # 解码期间若载出/换文件，结果作废

        def job():
            try:
                img = self._read_first_frame(info)
            except Exception:
                return
            if img is None:
                return
            try:
                widget = _make_thumb_widget(img, size)
            except Exception:
                return
            if gen != self._state_gen:
                return  # 已载出（或换了文件）：占位控件不再属于当前树，缓存也已清
            loop = getattr(self, "_loop", None)
            if loop is not None:
                try:
                    loop.call_soon_threadsafe(self._push_thumb, placeholder, widget, cache_key)
                    return
                except Exception:
                    pass
            self._push_thumb(placeholder, widget, cache_key)
        _THUMB_POOL.submit(job)

    def _flush_tasks(self, tasks):
        """展开时在 UI 线程调用：已解码的当场应用，未完成的提交后台任务。"""
        for placeholder, info, size, key in tasks:
            cached = self.role_thumb_cache.get(key)
            if cached is not None:
                try:
                    placeholder.content = cached
                except Exception:
                    pass
            else:
                self._queue_thumb(placeholder, info, size, key)
        tasks.clear()

    def _read_first_frame(self, info):
        src = info.get("path")
        if src:
            with open(src, "rb") as f:
                data = f.read()
        else:
            with open(self.input_file, "rb") as f:
                f.seek(info["offset"])
                data = f.read(info["size"])
        header = {
            "image_type": info.get("image_type", 0),
            "width": info.get("width", 0),
            "height": info.get("height", 0),
            "frame_count": info.get("frame_count", 1),
        }
        return hzc_decode_first_frame(data, header)

    def _get_role_thumbnail(self, role, outfits):
        if role in self.role_thumb_cache:
            return self.role_thumb_cache[role]
        target = None
        for outfit, infos in sorted(outfits.items()):
            for info in infos:
                if not _is_expr(info["filename"]):
                    target = info
                    break
            if target:
                break
        placeholder = ft.Container(
            content=ft.Icon(ft.Icons.PERSON, size=22, color=self.ink["text_dim"]),
            width=40,
            height=40,
            border_radius=12,
            bgcolor=self.ink["chip"],
            alignment=ft.Alignment.CENTER,
        )
        if target is None:
            self.role_thumb_cache[role] = placeholder
            return placeholder
        self._queue_thumb(placeholder, target, 40, role)
        return placeholder

    def _make_mini_thumb(self, info, size=24, registry=None):
        """registry 为 None 时立即排队解码；传入列表则登记任务，待节点展开可见后再排队。"""
        key = f"mini_{info['filename']}_{size}"
        if key in self.role_thumb_cache:
            return self.role_thumb_cache[key]
        placeholder = ft.Container(
            content=ft.Icon(ft.Icons.IMAGE_OUTLINED, size=max(12, size - 10), color=self.ink["text_dim"]),
            width=size,
            height=size,
            border_radius=max(4, size // 4),
            alignment=ft.Alignment.CENTER,
        )
        task = (placeholder, info, size, key)
        if registry is None:
            self._queue_thumb(*task)
        else:
            registry.append(task)
        return placeholder

    def _outfit_tile(self, outfit, infos, role="", header_registry=None, _expand_box=None):
        state = {"open": False}
        outfit_key = f"{role}/{outfit}"
        chevron = self._make_chevron(14)
        action_pending = []
        action_tiles = []

        body = ft.Column(spacing=0, visible=False)
        for info in sorted(infos, key=lambda x: x["filename"]):
            if _is_expr(info["filename"]):
                continue
            parts = info["filename"].split("_")
            name = parts[2] if parts[0] == "CHR" and len(parts) >= 3 else (
                parts[4] if len(parts) >= 5 else info["filename"])
            tile = self._action_tile(name, info, registry=action_pending)
            action_tiles.append((tile, info))
            body.controls.append(tile)

        def toggle(e):
            state["open"] = not state["open"]
            if state["open"]:
                self.open_outfits.add(outfit_key)
            else:
                self.open_outfits.discard(outfit_key)
            body.visible = state["open"]
            self._set_chevron(chevron, state["open"], 14)
            if state["open"]:
                if action_pending:
                    self._flush_tasks(action_pending)
                # 单动作服装展开即选中是省一步操作；换主题重建树时不能触发，
                # 否则会把用户真正选着的那张底图悄悄换掉。
                if len(action_tiles) == 1 and not self._restoring:
                    tile, info = action_tiles[0]
                    if self.selected_filename != info.get("filename"):
                        self._select_action(info)
            body.update()
            chevron.update()
            self._set_tile_bg(header, state["open"])

        self._tree_toggles[("outfit", outfit_key)] = (state, toggle)
        if _expand_box is not None:
            _expand_box.append((toggle, body))

        n_actions = len(body.controls)
        thumb_info = next((i for i in infos if not _is_expr(i["filename"])), None)
        thumb_widget = self._make_mini_thumb(thumb_info, 30, registry=header_registry) if thumb_info else ft.Icon(ft.Icons.FOLDER, size=18, color=self.ink["text_dim"])

        header = ft.ListTile(
            leading=thumb_widget,
            title=ft.Text(outfit, size=12, color=self.ink["text"],
                          max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
            subtitle=ft.Text(f"{n_actions} 动作", size=10, color=self.ink["text_dim"]),
            trailing=chevron,
            on_click=toggle,
            content_padding=ft.Padding.only(left=INDENT_OUTFIT, right=2, top=0, bottom=0),
            dense=True,
            min_height=42,
            hover_color=self.ink["hover"],
            bgcolor="#00000000",
        )
        return ft.Column([header, body], spacing=0)

    def _action_tile(self, name, info, registry=None):
        """第三级：整行往右退到导轨之后，层级关系靠缩进 + 导轨线表达，
        不靠字号差异（字号再小就看不清了）。"""
        ink = self.ink
        thumb_widget = self._make_mini_thumb(info, 26, registry=registry)
        title = ft.Text(name, size=12, color=ink["text_dim"],
                        max_lines=1, overflow=ft.TextOverflow.ELLIPSIS)
        dot = ft.Container(width=6, height=6, border_radius=3, bgcolor="#00000000")

        tile = ft.ListTile(
            leading=thumb_widget,
            title=title,
            trailing=dot,
            on_click=lambda e: self._select_action(info),
            content_padding=ft.Padding.only(left=8, right=8, top=0, bottom=0),
            dense=True,
            min_height=38,
            hover_color=ink["hover"],
            bgcolor="#00000000",
        )
        # 高亮画在外层 Container 上：ListTile 自己不会做 bgcolor 过渡，
        # 只有带 animate 的容器能让"选中"变成一个滑过去的动作而不是闪一下。
        hl = ft.Container(
            content=tile,
            border_radius=RADIUS_TILE,
            bgcolor="#00000000",
            border=ft.Border.only(left=ft.BorderSide(2, "#00000000")),
            animate=ft.Animation(ANIM_ROW, ft.AnimationCurve.EASE_OUT),
        )
        # 导轨用行容器的左边线画，不用"一根撑满行高的细条"：
        # ListView 沿滚动方向不给高度约束，任何 STRETCH/撑满高度的写法都会
        # 让布局直接抛异常，代价是列表里那一项之后的所有节点全部消失。
        row = ft.Container(
            content=hl,
            margin=ft.Margin.only(left=INDENT_ACTION, right=4, top=0, bottom=0),
            padding=ft.Padding.only(left=11, right=0, top=0, bottom=0),
            border=ft.Border.only(left=ft.BorderSide(1, ink["line_soft"])),
        )
        entry = {"hl": hl, "title": title, "dot": dot,
                 "filename": info["filename"]}
        self._action_rows.append(entry)
        self._paint_action_row(entry, info["filename"] == self.selected_filename)
        return row

    def _paint_action_row(self, entry, on):
        """选中态集中在这里改：底色/左侧标记条/字重字色/尾部圆点四处必须同步。"""
        if entry is None:
            return
        ink = self.ink
        entry["hl"].bgcolor = ink["sel"] if on else "#00000000"
        entry["hl"].border = ft.Border.only(
            left=ft.BorderSide(2, ink["accent"] if on else "#00000000"))
        entry["title"].weight = ft.FontWeight.W_700 if on else ft.FontWeight.W_400
        entry["title"].color = ink["accent"] if on else ink["text_dim"]
        entry["dot"].bgcolor = ink["accent"] if on else "#00000000"
        entry["on"] = on

    def _sync_selected_row(self):
        """按当前 selected_filename 重刷已登记行的选中态，返回真正变了的那些。

        不依赖"记住上一次点了哪一行"：树可以整棵重建（换主题）、
        选中也可以由 _load_bin 等级联路径改写，任何一条路都不该留下两行同时亮。
        """
        changed = []
        for entry in self._action_rows:
            on = entry["filename"] == self.selected_filename
            if entry.get("on") == on:
                continue
            self._paint_action_row(entry, on)
            changed.append(entry)
        return changed

    def _select_action(self, info):
        if self.selected_filename == info.get("filename"):
            return
        self.selected_filename = info["filename"]
        for entry in self._sync_selected_row():
            try:
                entry["hl"].update()
            except Exception:
                pass
        self._load_base(info)

    # ── Base image ──────────────────────────────────────────

    def _read_pil_list(self, info):
        src = info.get("path")
        if src:
            with open(src, "rb") as f:
                data = f.read()
        else:
            with open(self.input_file, "rb") as f:
                f.seek(info["offset"])
                data = f.read(info["size"])
        header = {
            "image_type": info.get("image_type", 0),
            "width": info.get("width", 0),
            "height": info.get("height", 0),
            "frame_count": info.get("frame_count", 1),
        }
        return hzc_data_to_pil_list(data, header)

    def _load_base(self, info):
        self.selected_info = info
        try:
            self.base_imgs = self._read_pil_list(info)
        except Exception as ex:
            self.base_imgs = []
            self._snack(f"读取底图失败: {ex}", error=True)
            return
        if not self.base_imgs:
            self._snack("无法解析该 HZC 图像", error=True)
            return
        self.base_idx = 0
        self._reset_zoom()
        self._show_base_frame()
        self._load_parts(info)

    def _show_base_frame(self):
        img = self.base_imgs[self.base_idx]
        self.base_image.src = _img_src(img)
        self.base_hint.visible = False
        n = len(self.base_imgs)
        self.frame_label.value = f"{self.base_idx + 1}/{n}" if n > 1 else ""
        self.frame_nav_holder.visible = n > 1
        self.prev_btn.disabled = self.base_idx <= 0
        self.next_btn.disabled = self.base_idx >= n - 1
        self.base_meta.value = f"{img.size[0]}×{img.size[1]} · {n} 帧"
        if self.base_only:
            # 没有差分时右栏是底图镜像：换帧要跟手，否则导出的和看到的对不上
            self.result_image.src = _img_src(img)
            self.result_meta.value = f"{img.size[0]}×{img.size[1]} · 底图（无表情差分）"
        self.page.update()

    def _prev_frame(self, e):
        if self.base_idx > 0:
            self.base_idx -= 1
            self._show_base_frame()

    def _next_frame(self, e):
        if self.base_idx < len(self.base_imgs) - 1:
            self.base_idx += 1
            self._show_base_frame()

    def _spawn(self, coro):
        """把协程丢回 UI 事件循环执行；没有循环时显式关闭，避免 never-awaited 警告。"""
        if not asyncio.iscoroutine(coro):
            return
        loop = getattr(self, "_loop", None)
        if loop is None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
        if loop is None:
            coro.close()
            return

        async def quiet():
            # 控件还没在客户端挂上时 invoke 会超时（例如加载瞬间调 reset），
            # 这类失败只影响一次缩放复位，不该冒泡成未捕获的任务异常。
            try:
                await coro
            except Exception:
                pass

        loop.create_task(quiet())

    def _reset_zoom(self, e=None):
        viewer = getattr(self, "base_viewer", None)
        if viewer is None:
            return
        self._spawn(viewer.reset())

    # ── Parts ───────────────────────────────────────────────

    def _enter_base_only(self):
        """没有差分可用：按当前底图帧铺出结果面板，让「保存/批量导出」走底图这条退路。"""
        self.base_only = True
        if not self.base_imgs:
            self.page.update()
            return
        img = self.base_imgs[self.base_idx]
        self.result_image.src = _img_src(img)
        self.result_hint.visible = False
        self.result_meta.value = f"{img.size[0]}×{img.size[1]} · 底图（无表情差分）"
        self.save_btn.disabled = False
        self.batch_btn.disabled = False
        self.page.update()

    def _load_parts(self, info):
        self.part_info = None
        self.part_imgs = []
        self.thumb_refs = []
        self.thumb_list.controls.clear()
        self.thumb_list.visible = False
        self.part_hint.visible = True
        self.part_hint_text.value = PART_HINT_IDLE
        self.part_count.value = ""
        self.composed_img = None
        # 换底图就先退出「直接导出底图」模式，是否再进由下面两个早退分支决定
        self.base_only = False
        self.result_image.src = TRANSPARENT
        self.result_hint.visible = True
        self.result_meta.value = ""
        self.save_btn.disabled = True
        self.batch_btn.disabled = True

        part_info = _find_expr_info(self.file_infos, info["filename"])
        if not part_info:
            self.part_hint_text.value = PART_HINT_EMPTY
            self._enter_base_only()
            return

        try:
            imgs = self._read_pil_list(part_info)
        except Exception as ex:
            self._snack(f"读取部件失败: {ex}", error=True)
            self.page.update()
            return
        if not imgs:
            self.part_hint_text.value = PART_HINT_EMPTY
            self._enter_base_only()
            return

        self.part_info = part_info
        self.part_imgs = imgs
        self.part_count.value = f"{len(imgs)} 帧"
        for idx, img in enumerate(imgs):
            self.thumb_list.controls.append(self._make_thumb(img, idx))
        self.part_hint.visible = False
        self.thumb_list.visible = True
        self.page.update()
        self._pick_part(0)

    def _make_thumb(self, img, idx):
        data = _thumb_bytes(img, THUMB)
        selected = idx == self.part_idx
        ink = self.ink

        c = ft.Container(
            content=ft.Image(src=data, fit=ft.BoxFit.CONTAIN, width=THUMB, height=THUMB),
            width=THUMB,
            height=THUMB,
            bgcolor="#00000000",
            border=ft.Border.all(2, ink["accent"] if selected else ink["line_soft"]),
            border_radius=RADIUS_TILE,
            on_click=lambda e, i=idx: self._pick_part(i),
            tooltip=_tip(f"第 {idx + 1} 帧"),
            animate=ft.Animation(ANIM_ROW, ft.AnimationCurve.EASE_OUT),
            # 只动颜色，不动几何：差分条会跟着窗口一起变宽变窄，
            # size/position 也参与隐式动画的话，拉伸过程中每一格都在重排滑动，
            # 看上去就是整条在闪。
            # 角标只做编号，永远保持中性色：选中态已经由描边承担，
            # 再叠一块主题蓝上去，28 格里就有一格在抢画面。
            badge=ft.Badge(
                label=str(idx + 1),
                small_size=16,
                bgcolor=ink["badge_bg"],
                text_color=ink["badge_fg"],
                alignment=ft.Alignment.TOP_RIGHT,
                padding=ft.Padding.only(left=4, right=4, top=0, bottom=0),
                label_visible=True,
            ),
        )
        self.thumb_refs.append((c, idx))
        return c

    def _pick_part(self, idx):
        if not self.part_imgs or idx >= len(self.part_imgs):
            return
        self.part_idx = idx
        for c, i in self.thumb_refs:
            c.border = ft.Border.all(2, self.ink["accent"] if i == idx else self.ink["line_soft"])
            try:
                c.update()
            except Exception:
                pass
        self._compose()

    def _compose(self, e=None):
        if not self.part_info or not self.selected_info or not self.part_imgs:
            return
        if self.part_idx >= len(self.part_imgs) or not self.base_imgs:
            return
        ox = self.part_info.get("offset_x", 0)
        oy = self.part_info.get("offset_y", 0)
        composed = compose_preview(
            self.base_imgs[0],
            self.part_imgs[self.part_idx],
            ox,
            oy,
        )
        self.composed_img = composed
        self.result_image.src = _img_src(composed)
        self.result_hint.visible = False
        self.result_meta.value = f"{composed.size[0]}×{composed.size[1]} · 偏移 {ox:+d}/{oy:+d}"
        self.save_btn.disabled = False
        self.batch_btn.disabled = False
        self.page.update()

    # ── Keyboard ────────────────────────────────────────────

    async def _on_keyboard(self, e):
        key = (getattr(e, "key", None) or "").lower()
        if key == "escape":
            try:
                self.page.pop_dialog()
            except Exception:
                pass
            return
        # 只让真正在打字的控件抢走按键。列表项/按钮点过之后也会持有焦点，
        # 拿 control 是否为空判断会把 ←/→ 全吞掉，导致切不了帧。
        owner = getattr(e, "control", None)
        if type(owner).__name__ in ("TextField", "SearchBar", "Autocomplete"):
            return
        if key in ("left", "right") and len(self.base_imgs) > 1:
            (self._prev_frame if key == "left" else self._next_frame)(None)
        elif key == "0":
            self._reset_zoom()
        elif key == "l":
            await self._toggle_theme(None)
        elif e.ctrl and key == "s":
            await self._save_current(None)
        elif e.ctrl and key == "o":
            await self._open_bin(None)
        elif e.ctrl and key == "w":
            await self._unload_bin(None)

    # ── Save / Export ───────────────────────────────────────

    async def _save_current(self, e):
        # 无差分模式下没有合成结果，直接导出当前底图帧
        img = self.composed_img
        if img is None and self.base_only and self.base_imgs:
            img = self.base_imgs[self.base_idx]
        if img is None or not self.selected_info:
            self._snack("请先选择底图并合成", error=True)
            return
        if self.composed_img is not None:
            name = f"{self.selected_info['filename']}_diff_{self.part_idx:03d}.png"
        else:
            name = f"{self.selected_info['filename']}_base_{self.base_idx:03d}.png"
        try:
            result = await self.file_picker.save_file(
                dialog_title="保存当前合成图像", file_name=name,
                initial_directory=self.export_dir,
            )
        except Exception as ex:
            self._snack(f"保存对话框失败: {ex}", error=True)
            return
        if not result:
            return
        self.export_dir = str(Path(result).parent)
        try:
            img.save(result, "PNG")
            self._snack(f"已保存: {Path(result).name}", folder=str(Path(result).parent))
            self._set_status(str(result))
        except Exception as ex:
            self._snack(f"保存失败: {ex}", error=True)

    async def _batch_export(self, e):
        # 无差分模式下放行：铺平底图全部帧，文件名走 _base_ 而非 _diff_
        if not self.selected_info or (not self.base_only and (not self.part_info or not self.part_imgs)):
            self._snack("请先选择底图并合成", error=True)
            return
        if not self.base_imgs:
            self._snack("底图为空", error=True)
            return
        try:
            result = await self.file_picker.get_directory_path(
                dialog_title="选择导出目录",
                initial_directory=self.export_dir,
            )
        except Exception as ex:
            self._snack(f"导出对话框失败: {ex}", error=True)
            return
        if not result:
            return
        save_dir = str(result)
        self.export_dir = save_dir
        self._set_status("正在批量导出…")

        base = self.base_imgs[0]
        ox = self.part_info.get("offset_x", 0) if self.part_info else 0
        oy = self.part_info.get("offset_y", 0) if self.part_info else 0
        fname = self.selected_info["filename"]
        # 无差分模式：不合成，铺平底图全部帧
        parts_data = list(self.part_imgs) if not self.base_only else list(self.base_imgs)

        def one(args):
            idx, p = args
            try:
                if self.base_only:
                    composed = p
                    out = Path(save_dir) / f"{fname}_base_{idx:03d}.png"
                else:
                    composed = compose_preview(base, p, ox, oy)
                    out = Path(save_dir) / f"{fname}_diff_{idx:03d}.png"
                composed.save(out, "PNG")
                return True
            except Exception:
                return False

        def work():
            with ThreadPoolExecutor(max_workers=min(8, (os.cpu_count() or 4))) as ex:
                oks = list(ex.map(one, enumerate(parts_data)))
            saved = sum(1 for ok in oks if ok)
            return saved, len(oks) - saved

        try:
            saved, errors = await asyncio.to_thread(work)
            if errors:
                self._snack(f"导出完成: {saved} 成功, {errors} 失败", error=errors > 0, folder=save_dir)
            else:
                self._snack(f"已导出 {saved} 张到 {Path(save_dir).name}", folder=save_dir)
            self._set_status(save_dir)
        except Exception as ex:
            self._snack(f"导出失败: {ex}", error=True)
            self._set_status("导出失败")


def _img_bytes(img, fmt="PNG"):
    buf = io.BytesIO()
    img.save(buf, fmt)
    return buf.getvalue()


def _img_src(img, fmt="PNG"):
    """给 ft.Image 用的 data URI。

    注意：src 直接塞 bytes 只在「构造时」有效，构造后 `img.src = bytes` 赋值
    不会被重新编码，客户端拿到的是乱码路径 → 图像区一片空白。
    统一走 data URI，构造和更新两条路都成立。
    """
    return "data:image/png;base64," + base64.b64encode(_img_bytes(img, fmt)).decode()


def _thumb_bytes(img, size=100):
    w, h = img.size
    s = min(1.0, size / max(w, h))
    thumb = img.resize((max(1, int(w * s)), max(1, int(h * s))), Image.Resampling.LANCZOS)
    return _img_src(thumb)


def _make_thumb_widget(img, size=36):
    data = _thumb_bytes(img, size)
    return ft.Image(src=data, width=size, height=size, fit=ft.BoxFit.CONTAIN)


async def main(page: ft.Page):
    try:
        app = ComposerApp(page)
    except Exception as ex:
        page.add(ft.Text(f"初始化失败: {ex}", color=ft.Colors.ERROR))
        page.update()
        return
    # 命令行/拖到程序图标打开：首个存在的文件或目录参数自动载入
    if len(sys.argv) > 1:
        p = Path(sys.argv[1])
        if p.exists() and (p.is_dir() or p.suffix.lower() == ".bin"):
            await app._load_bin(str(p))
        else:
            app._snack(f"无法打开: {sys.argv[1]}", error=True)
    return app


if __name__ == "__main__":
    ft.run(main)
