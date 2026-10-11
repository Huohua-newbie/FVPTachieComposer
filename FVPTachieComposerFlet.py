"""FVP Tachie Composer — Flet UI

FVP engine character sprite viewer & composer.
Parsing and composition logic reused from FVPTachieComposer.py.

Dependencies: flet>=0.86  Pillow>=10
Run: python FVPTachieComposerFlet.py
"""

import asyncio
import io
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import flet as ft
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
PART_STRIP_H = THUMB + GUTTER + 4
PLAY_INTERVAL = 0.12
PART_HINT_IDLE = "选择底图后显示差分帧"
PART_HINT_EMPTY = "该底图没有对应的表情差分"


def _palette(dark):
    if dark:
        return {
            "page": "#0e1116",
            "panel": "#151a21",
            "panel_alt": "#11161d",
            "stage": "#0b0e13",
            "line": "#252d39",
            "line_soft": "#1c232d",
            "text": "#e9edf5",
            "text_dim": "#8e9aae",
            "accent": "#7aa6ff",
            "accent_soft": "#1e2b45",
            "accent_2": "#f2879f",
            "chip": "#1a212b",
            "hover": "#1b222d",
            "sel": "#22334f",
            "checker_a": "#151b23",
            "checker_b": "#10151c",
            "ok_bg": "#1e2b45",
            "ok_fg": "#c8daff",
            "err_bg": "#3a1d24",
            "err_fg": "#ffc9d2",
        }
    return {
        "page": "#eceef5",
        "panel": "#f9fafd",
        "panel_alt": "#ffffff",
        "stage": "#f4f6fc",
        "line": "#dcdfec",
        "line_soft": "#e8ebf4",
        "text": "#1b2130",
        "text_dim": "#68728a",
        "accent": "#3f6ee0",
        "accent_soft": "#e4ecfd",
        "accent_2": "#d8577f",
        "chip": "#eef1f9",
        "hover": "#eef1f8",
        "sel": "#dde7fd",
        "checker_a": "#f1f3fa",
        "checker_b": "#e5e9f4",
        "ok_bg": "#e4ecfd",
        "ok_fg": "#2a4f9e",
        "err_bg": "#fbe3e7",
        "err_fg": "#9c2a44",
    }


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
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


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
             "也可把 .bin / HZC 文件夹拖到程序图标上直接打开。"),
    ("挑选", "左侧按 角色 → 服装 → 动作 展开，顶部搜索框可按任意层级名字过滤。"),
    ("预览", "中间是底图舞台：滚轮缩放、拖拽平移，下方滑块逐帧擦洗，▶ 可自动播放。"),
    ("合成", "点击「差分部件」里的任意一帧，右侧即时合成；X/Y 滑杆用于对位微调。"),
    ("导出", "「保存当前」导出一张 PNG，「批量导出」把该底图的全部差分一次性铺平。"),
)

HELP_KEYS = (
    ("← →", "上一帧 / 下一帧"),
    ("[ ]", "上一个 / 下一个差分部件"),
    ("P", "自动播放 / 暂停"),
    ("0", "复位缩放"),
    ("Ctrl+S", "保存当前合成图"),
    ("Ctrl+O", "打开 BIN"),
    ("L", "切换深浅主题"),
    ("Esc", "清空搜索 / 关闭对话框"),
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
        self.selected_action_ctrl = None
        self.query = ""

        self.base_imgs = []
        self.base_idx = 0
        self.part_info = None
        self.part_imgs = []
        self.part_idx = 0
        self.nudge_x = 0
        self.nudge_y = 0
        self.composed_img = None
        self.thumb_refs = []
        self.role_thumb_cache = {}
        self._playing = False
        self._viewer = None

        # UI 事件循环引用（_load_bin/_toggle_theme 中捕获）：
        # 后台缩略图解码完成后，经由它把“替换+刷新”调度回 UI 线程执行，
        # 避免后台线程直接碰控件导致的更新丢失。
        self._loop = None

        self.file_picker = ft.FilePicker()
        self.clipboard = ft.Clipboard()
        self.page.services = [self.file_picker, self.clipboard]
        self._snack_seq = 0
        self._snack_overlay = None
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

    def _panel(self, controls, *, width=None, expand=False, pad_right=6):
        """描边式面板：不用 Material 阴影，靠 1px 边线 + 分层底色分区，
        三栏工具界面里阴影只会让边界变糊。"""
        ink = self.ink
        return ft.Container(
            content=ft.Column(controls, spacing=10, expand=True),
            width=width,
            expand=expand,
            bgcolor=ink["panel"],
            border=ft.Border.all(1, ink["line"]),
            border_radius=RADIUS_PANEL,
            padding=ft.Padding.only(left=12, top=10, right=pad_right, bottom=10),
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
            image=ft.Image(src=self._checker, fit=ft.BoxFit.NONE, repeat=ft.ImageRepeat.REPEAT,
                           opacity=0.85),
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

        p.add(ft.Column([self.title_bar, body, status_bar], spacing=0, expand=True))
        p.update()

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
            content=ft.Text("未载入文件", size=11, color=ink["text_dim"],
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
        header = self._section(
            ft.Icons.PEOPLE, "角色库",
            right=[
                ft.IconButton(ft.Icons.FOLDER_OPEN, on_click=self._open_bin,
                              tooltip=_tip("打开 BIN (Ctrl+O)"), icon_size=19, icon_color=ink["accent"]),
                ft.IconButton(ft.Icons.FOLDER, on_click=self._open_dir,
                              tooltip=_tip("打开 HZC 文件夹"), icon_size=19, icon_color=ink["accent"]),
            ],
        )

        self.filter_box = ft.TextField(
            hint_text="搜索角色 / 服装 / 动作",
            text_size=12,
            dense=True,
            border_radius=10,
            bgcolor=ink["panel_alt"],
            filled=True,
            fill_color=ink["panel_alt"],
            prefix_icon=ft.Icons.SEARCH,
            prefix_icon_size_constraints=ft.BoxConstraints(min_width=18, max_width=18),
            border_width=1,
            border_color=ink["line"],
            focused_border_color=ink["accent"],
            hint_style=ft.TextStyle(size=11, color=ink["text_dim"]),
            content_padding=ft.Padding.only(left=6, right=6, top=7, bottom=7),
            suffix=ft.IconButton(ft.Icons.CLOSE, icon_size=15, icon_color=ink["text_dim"],
                                 on_click=self._clear_filter, tooltip=_tip("清空 (Esc)")),
            on_change=self._on_filter,
            on_submit=self._on_filter,
        )
        self.filter_count = ft.Text("", size=10, color=ink["text_dim"])

        self.empty_hint = ft.Container(
            content=ft.Column(
                [
                    ft.Container(
                        ft.Icon(ft.Icons.FOLDER_OPEN, size=26, color=ink["accent"]),
                        width=58, height=58, border_radius=20, bgcolor=ink["accent_soft"],
                        alignment=ft.Alignment.CENTER,
                    ),
                    ft.Text("尚未加载文件", size=13, weight=ft.FontWeight.W_600, color=ink["text"]),
                    ft.Text("选择一个 .bin 或 HZC 文件夹开始", size=11,
                            color=ink["text_dim"], text_align=ft.TextAlign.CENTER),
                    ft.Container(height=4),
                    ft.FilledButton("打开 BIN", icon=ft.Icons.FOLDER_OPEN, on_click=self._open_bin,
                                    style=ft.ButtonStyle(
                                        bgcolor=ink["accent"], color="#ffffff",
                                        padding=ft.Padding.symmetric(horizontal=16, vertical=10),
                                        shape=ft.RoundedRectangleBorder(radius=10))),
                    ft.OutlinedButton("HZC 文件夹", icon=ft.Icons.FOLDER, on_click=self._open_dir,
                                      style=ft.ButtonStyle(
                                          color=ink["text_dim"],
                                          side=ft.BorderSide(1, ink["line"]),
                                          padding=ft.Padding.symmetric(horizontal=16, vertical=8),
                                          shape=ft.RoundedRectangleBorder(radius=10))),
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
            [header, self.filter_box, ft.Container(expand=True, content=body), self.filter_count],
            pad_right=6,
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
        self.play_btn = ft.IconButton(ft.Icons.PLAY_ARROW, on_click=self._toggle_play,
                                      icon_size=19, icon_color=ink["accent"], tooltip=_tip("自动播放 (P)"))
        self.frame_slider = ft.Slider(
            min=1, max=2, value=1, divisions=1,
            on_change=self._on_scrub,
            active_color=ink["accent"],
            inactive_color=ink["line"],
        )
        frame_nav = ft.Row(
            [
                self.prev_btn,
                ft.Container(expand=True, content=ft.Column([self.frame_slider], tight=True)),
                self.next_btn,
                ft.Container(width=1),
                self.play_btn,
                self.frame_label,
            ],
            spacing=4,
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
            padding=ft.Padding.only(left=1, right=1, top=1, bottom=GUTTER),
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

        def nudge(icon, slider, tip):
            return ft.Row(
                [
                    ft.Container(
                        ft.Icon(icon, size=14, color=ink["text_dim"]),
                        width=22, height=22, border_radius=7, bgcolor=ink["chip"],
                        alignment=ft.Alignment.CENTER,
                    ),
                    slider,
                ],
                spacing=6,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                tooltip=_tip(tip),
            )

        self.nudge_x_slider = ft.Slider(min=-48, max=48, value=0, on_change=self._on_nudge_x,
                                        active_color=ink["accent_2"], inactive_color=ink["line"],
                                        expand=True)
        self.nudge_y_slider = ft.Slider(min=-48, max=48, value=0, on_change=self._on_nudge_y,
                                        active_color=ink["accent_2"], inactive_color=ink["line"],
                                        expand=True)
        sx = nudge(ft.Icons.ARROW_BACK, self.nudge_x_slider, "水平偏移差分部件")
        sy = nudge(ft.Icons.EXPAND_MORE, self.nudge_y_slider, "垂直偏移差分部件")

        self.nudge_reset = ft.IconButton(ft.Icons.REFRESH, on_click=self._reset_nudge,
                                         icon_size=16, icon_color=ink["text_dim"],
                                         tooltip=_tip("复位偏移"))
        nudge_head = ft.Row(
            [
                ft.Text("对位微调", size=11, weight=ft.FontWeight.W_600, color=ink["text"]),
                ft.Container(expand=True),
                self.nudge_reset,
            ],
            spacing=4,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )
        self.nudge_box = ft.Container(
            content=ft.Column([nudge_head, sx, sy], spacing=2),
            visible=False,
            padding=ft.Padding.only(left=4, right=4, top=4, bottom=4),
            border_radius=RADIUS_TILE,
            bgcolor=ink["chip"],
        )

        self.save_btn = ft.FilledButton(
            "保存当前", icon=ft.Icons.SAVE_ALT, on_click=self._save_current, disabled=True,
            tooltip=_tip("导出这一张 PNG (Ctrl+S)"),
            style=ft.ButtonStyle(bgcolor=ink["accent"], color="#ffffff",
                                 padding=ft.Padding.symmetric(horizontal=12, vertical=10),
                                 shape=ft.RoundedRectangleBorder(radius=10)),
            expand=True,
        )
        self.batch_btn = ft.OutlinedButton(
            "批量导出", icon=ft.Icons.DOWNLOAD, on_click=self._batch_export, disabled=True,
            tooltip=_tip("把该底图的全部差分铺平成 PNG"),
            style=ft.ButtonStyle(color=ink["text_dim"], side=ft.BorderSide(1, ink["line"]),
                                 padding=ft.Padding.symmetric(horizontal=12, vertical=9),
                                 shape=ft.RoundedRectangleBorder(radius=10)),
            expand=True,
        )

        btn_bar = ft.Row([self.save_btn, self.batch_btn], spacing=8)

        return self._panel(
            [
                self._section(ft.Icons.PALETTE_OUTLINED, "合成结果", right=[self.result_meta]),
                result_area,
                self.nudge_box,
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
        self.selected_action_ctrl = None
        self.page.clean()
        self._apply_theme()
        self._build()
        if self.input_file:
            self._render_tree()
            if self.selected_info:
                self._load_base(self.selected_info)
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

    def _snack(self, msg, error=False, folder=None):
        self._snack_seq = getattr(self, "_snack_seq", 0) + 1
        seq = self._snack_seq
        ink = self.ink
        bg = ink["err_bg"] if error else ink["ok_bg"]
        fg = ink["err_fg"] if error else ink["ok_fg"]
        row = [
            ft.Icon(
                ft.Icons.ERROR if error else ft.Icons.CHECK_CIRCLE,
                size=17, color=fg,
            ),
            ft.Text(msg, size=12, color=fg, weight=ft.FontWeight.W_500,
                    expand=True, max_lines=3, overflow=ft.TextOverflow.ELLIPSIS),
        ]
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
        row.append(ft.IconButton(ft.Icons.CLOSE, icon_size=15, icon_color=fg,
                                 tooltip=_tip("关闭"),
                                 on_click=lambda e, _s=seq: self._dismiss_snack(_s)))
        toast = ft.Container(
            content=ft.Row(row, spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            bgcolor=bg,
            border_radius=12,
            border=ft.Border.all(1, ink["line"]),
            padding=ft.Padding.only(left=12, right=6, top=7, bottom=7),
            shadow=ft.BoxShadow(blur_radius=18, spread_radius=0, color="#00000033"),
        )
        # 定位在标题栏下方并居中：不再横跨整窗、也不再压住窗口按钮
        overlay = ft.Stack(
            [ft.Container(toast, top=56, left=0, right=0, alignment=ft.Alignment.CENTER)],
            top=0, left=0, right=0, bottom=0,
        )
        try:
            if self._snack_overlay in self.page.overlay:
                self.page.overlay.remove(self._snack_overlay)
        except Exception:
            pass
        self._snack_overlay = overlay
        self.page.overlay.append(overlay)
        self.page.update()

        if not error:
            # 成功横幅 2 秒后自动消除：走事件循环定时器，全程 UI 线程
            self._later(2.0, self._dismiss_snack, seq)

    def _dismiss_snack(self, seq):
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
            )
        except Exception:
            return
        if not result:
            return
        path = result[0].path
        if not path:
            return
        await self._load_bin(path)

    async def _open_dir(self, e):
        try:
            result = await self.file_picker.get_directory_path(dialog_title="选择包含 HZC 文件的文件夹")
        except Exception:
            return
        if not result:
            return
        await self._load_bin(str(result))

    async def _load_bin(self, path):
        self._capture_loop()
        self._set_status("正在解析…")

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
            self._snack(f"解析失败: {ex}", error=True)
            return

        self.input_file = path
        self.file_infos = infos
        self.hierarchy = hier
        self.selected_info = None
        self.selected_filename = None
        self.selected_action_ctrl = None
        self.query = ""
        if getattr(self, "filter_box", None) is not None:
            self.filter_box.value = ""
        self._clear_previews()
        self._render_tree()
        hzc = sum(1 for i in infos if i["type"] == "hzc")
        self.file_chip.content.value = Path(path).name
        self._set_status(f"{Path(path).name} — {len(hier)} 角色, {hzc} 图像")
        self._set_status_meta()

    def _clear_previews(self):
        self.base_imgs = []
        self.part_imgs = []
        self.part_info = None
        self.composed_img = None
        self.thumb_refs = []
        self.nudge_x = 0
        self.nudge_y = 0
        self._stop_play()
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
        self.nudge_box.visible = False
        self._sync_nudge_sliders()
        self.save_btn.disabled = True
        self.batch_btn.disabled = True

    # ── Tree ────────────────────────────────────────────────

    def _match(self, *names):
        q = (self.query or "").strip().lower()
        if not q:
            return True
        return any(q in (n or "").lower() for n in names)

    def _on_filter(self, e):
        self.query = (getattr(self.filter_box, "value") or "").strip()
        self._render_tree()

    def _clear_filter(self, e=None):
        self.query = ""
        self.filter_box.value = ""
        self._render_tree()

    def _render_tree(self):
        self.tree_list.controls.clear()
        shown = 0
        for role, outfits in sorted(self.hierarchy.items()):
            actions = [i for infos in outfits.values() for i in infos if not _is_expr(i["filename"])]
            if self.query:
                keep = self._match(role) or any(
                    self._match(outfit, i["filename"]) for outfit, infos in outfits.items()
                    for i in infos
                )
                if not keep:
                    continue
                shown += len(actions)
            self.tree_list.controls.append(self._role_tile(role, outfits, open_=bool(self.query)))
        if self.query:
            self.filter_count.value = f"匹配 {shown} 个动作" if shown else "无匹配"
            self.filter_count.visible = True
        else:
            self.filter_count.value = ""
            self.filter_count.visible = False
        self.tree_list.visible = True
        self.empty_hint.visible = False
        self.page.update()

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

    def _role_tile(self, role, outfits, open_=False):
        state = {"open": bool(open_)}
        count = sum(len([i for i in infos if not _is_expr(i["filename"])]) for infos in outfits.values())
        chevron = self._make_chevron(16)
        header_pending = []
        child_toggles = []

        body = ft.Column(spacing=0, visible=state["open"])
        for outfit, infos in sorted(outfits.items()):
            body.controls.append(self._outfit_tile(outfit, infos, header_registry=header_pending,
                                                   _expand_box=child_toggles, open_=state["open"]))

        def toggle(e):
            state["open"] = not state["open"]
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

        thumb_widget = self._get_role_thumbnail(role, outfits)

        header = ft.ListTile(
            leading=thumb_widget,
            title=ft.Text(role, size=13, weight=ft.FontWeight.W_700, color=self.ink["text"],
                          max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
            subtitle=ft.Text(f"{count} 动作 · {len(outfits)} 服装", size=10, color=self.ink["text_dim"]),
            trailing=ft.Row(
                [ft.Text(f"{count}", size=10, color=self.ink["text_dim"]), chevron],
                spacing=4,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            on_click=toggle,
            content_padding=ft.Padding.only(left=4, right=2, top=0, bottom=0),
            dense=True,
            min_height=46,
            hover_color=self.ink["hover"],
            bgcolor="#00000000",
        )
        if state["open"]:
            self._set_tile_bg(header, True)
            if header_pending:
                self._flush_tasks(header_pending)
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
            placeholder.update()
        except Exception:
            pass

    def _queue_thumb(self, placeholder, info, size, cache_key):
        """后台线程只做解码；完成后经事件循环把替换+刷新调度回 UI 线程。"""
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
        if self.query:
            # 过滤态整棵树都是展开的，逐个解码只会白烧 CPU——搜索看的是名字
            tasks.clear()
            return
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
            if not self.query:
                self._queue_thumb(*task)
        else:
            registry.append(task)
        return placeholder

    def _outfit_tile(self, outfit, infos, header_registry=None, _expand_box=None, open_=False):
        state = {"open": bool(open_)}
        chevron = self._make_chevron(14)
        action_pending = []
        action_tiles = []

        body = ft.Column(spacing=0, visible=state["open"])
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
            body.visible = state["open"]
            self._set_chevron(chevron, state["open"], 14)
            if state["open"]:
                if action_pending:
                    self._flush_tasks(action_pending)
                if len(action_tiles) == 1:
                    tile, info = action_tiles[0]
                    if self.selected_filename != info.get("filename"):
                        self._select_action(info, tile)
            body.update()
            chevron.update()
            self._set_tile_bg(header, state["open"])
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
            content_padding=ft.Padding.only(left=30, right=2, top=0, bottom=0),
            dense=True,
            min_height=42,
            hover_color=self.ink["hover"],
            bgcolor="#00000000",
        )
        if state["open"]:
            self._set_tile_bg(header, True)
            if action_pending:
                self._flush_tasks(action_pending)
        return ft.Column([header, body], spacing=0)

    def _action_tile(self, name, info, registry=None):
        selected = info["filename"] == self.selected_filename
        thumb_widget = self._make_mini_thumb(info, 26, registry=registry)

        c = ft.ListTile(
            leading=thumb_widget,
            title=ft.Text(name, size=12,
                          weight=ft.FontWeight.W_700 if selected else ft.FontWeight.W_400,
                          color=self.ink["accent"] if selected else self.ink["text_dim"],
                          max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
            trailing=ft.Container(width=6, height=6, border_radius=3,
                                  bgcolor=self.ink["accent"] if selected else "#00000000"),
            on_click=lambda e: self._select_action(info, c),
            content_padding=ft.Padding.only(left=30, right=8, top=0, bottom=0),
            dense=True,
            min_height=38,
            hover_color=self.ink["hover"],
            bgcolor=self.ink["sel"] if selected else "#00000000",
            selected=selected,
        )
        return c

    def _select_action(self, info, ctrl):
        if self.selected_filename == info.get("filename"):
            return
        if self.selected_action_ctrl is not None:
            try:
                self.selected_action_ctrl.selected = False
                self.selected_action_ctrl.update()
            except Exception:
                self.selected_action_ctrl = None
        self.selected_action_ctrl = ctrl
        self.selected_filename = info["filename"]
        try:
            ctrl.selected = True
            ctrl.update()
        except Exception:
            self.selected_action_ctrl = None
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
        self._stop_play()
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
        self.base_image.src = _img_bytes(img)
        self.base_hint.visible = False
        n = len(self.base_imgs)
        self.frame_label.value = f"{self.base_idx + 1}/{n}" if n > 1 else ""
        self.frame_nav_holder.visible = n > 1
        self.prev_btn.disabled = self.base_idx <= 0
        self.next_btn.disabled = self.base_idx >= n - 1
        if n > 1:
            self.frame_slider.max = n
            self.frame_slider.divisions = n - 1
            self.frame_slider.value = self.base_idx + 1
        self.base_meta.value = f"{img.size[0]}×{img.size[1]} · {n} 帧"
        self.page.update()

    def _prev_frame(self, e):
        if self.base_idx > 0:
            self.base_idx -= 1
            self._show_base_frame()

    def _next_frame(self, e):
        if self.base_idx < len(self.base_imgs) - 1:
            self.base_idx += 1
            self._show_base_frame()

    def _on_scrub(self, e):
        if not self.base_imgs:
            return
        try:
            idx = int(round(float(e.control.value))) - 1
        except (TypeError, ValueError, AttributeError):
            return
        idx = max(0, min(len(self.base_imgs) - 1, idx))
        if idx != self.base_idx:
            self.base_idx = idx
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
        if loop is not None:
            loop.create_task(coro)
        else:
            coro.close()

    def _reset_zoom(self, e=None):
        viewer = getattr(self, "base_viewer", None)
        if viewer is None:
            return
        self._spawn(viewer.reset())

    def _toggle_play(self, e):
        if len(self.base_imgs) < 2:
            return
        self._playing = not self._playing
        self.play_btn.icon = ft.Icons.PAUSE if self._playing else ft.Icons.PLAY_ARROW
        try:
            self.play_btn.update()
        except Exception:
            pass
        if self._playing:
            self._tick()

    def _stop_play(self):
        self._playing = False

    def _tick(self):
        if not self._playing or not self.base_imgs:
            return
        self.base_idx = (self.base_idx + 1) % len(self.base_imgs)
        self._show_base_frame()
        if not self._later(PLAY_INTERVAL, self._tick):
            self._playing = False

    # ── Parts ───────────────────────────────────────────────

    def _load_parts(self, info):
        self.part_info = None
        self.part_imgs = []
        self.thumb_refs = []
        self.nudge_x = 0
        self.nudge_y = 0
        self._sync_nudge_sliders()
        self.thumb_list.controls.clear()
        self.thumb_list.visible = False
        self.part_hint.visible = True
        self.part_hint_text.value = PART_HINT_IDLE
        self.part_count.value = ""
        self.composed_img = None
        self.result_image.src = TRANSPARENT
        self.result_hint.visible = True
        self.result_meta.value = ""
        self.nudge_box.visible = False
        self.save_btn.disabled = True
        self.batch_btn.disabled = True

        part_info = _find_expr_info(self.file_infos, info["filename"])
        if not part_info:
            self.part_hint_text.value = PART_HINT_EMPTY
            self.page.update()
            return

        try:
            imgs = self._read_pil_list(part_info)
        except Exception as ex:
            self._snack(f"读取部件失败: {ex}", error=True)
            self.page.update()
            return
        if not imgs:
            self.page.update()
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
            badge=ft.Badge(
                label=str(idx + 1),
                small_size=16,
                bgcolor=ink["accent"] if selected else ink["chip"],
                text_color="#ffffff" if selected else ink["text_dim"],
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

    def _on_nudge_x(self, e):
        self.nudge_x = int(round(self.nudge_x_slider.value or 0))
        self._compose()

    def _on_nudge_y(self, e):
        self.nudge_y = int(round(self.nudge_y_slider.value or 0))
        self._compose()

    def _sync_nudge_sliders(self):
        for slider, value in ((self.nudge_x_slider, self.nudge_x), (self.nudge_y_slider, self.nudge_y)):
            if slider is None:
                return
            slider.value = value
            try:
                slider.update()
            except Exception:
                pass

    def _reset_nudge(self, e):
        self.nudge_x = 0
        self.nudge_y = 0
        self._sync_nudge_sliders()
        self._compose()

    def _compose(self, e=None):
        if not self.part_info or not self.selected_info or not self.part_imgs:
            return
        if self.part_idx >= len(self.part_imgs) or not self.base_imgs:
            return
        ox = self.part_info.get("offset_x", 0) + self.nudge_x
        oy = self.part_info.get("offset_y", 0) + self.nudge_y
        composed = compose_preview(
            self.base_imgs[0],
            self.part_imgs[self.part_idx],
            ox,
            oy,
        )
        self.composed_img = composed
        self.result_image.src = _img_bytes(composed)
        self.result_hint.visible = False
        self.result_meta.value = f"{composed.size[0]}×{composed.size[1]} · 偏移 {ox:+d}/{oy:+d}"
        self.nudge_box.visible = True
        self.save_btn.disabled = False
        self.batch_btn.disabled = False
        self.page.update()

    # ── Keyboard ────────────────────────────────────────────

    async def _on_keyboard(self, e):
        key = (getattr(e, "key", None) or "").lower()
        if key == "escape":
            if self.query:
                self._clear_filter()
            try:
                self.page.pop_dialog()
            except Exception:
                pass
            return
        if getattr(e, "control", None) is not None:
            return  # 焦点在输入框里，不抢键
        if key in ("left", "right") and len(self.base_imgs) > 1:
            (self._prev_frame if key == "left" else self._next_frame)(None)
        elif key in ("bracketleft", "bracketright") and self.part_imgs:
            delta = -1 if key == "bracketleft" else 1
            self._pick_part(max(0, min(len(self.part_imgs) - 1, self.part_idx + delta)))
        elif key == "p":
            self._toggle_play(None)
        elif key == "0":
            self._reset_zoom()
        elif key == "l":
            await self._toggle_theme(None)
        elif e.ctrl and key == "s":
            await self._save_current(None)
        elif e.ctrl and key == "o":
            await self._open_bin(None)

    # ── Save / Export ───────────────────────────────────────

    async def _save_current(self, e):
        if not self.composed_img or not self.selected_info:
            self._snack("请先选择底图并合成", error=True)
            return
        name = f"{self.selected_info['filename']}_diff_{self.part_idx:03d}.png"
        try:
            result = await self.file_picker.save_file(dialog_title="保存当前合成图像", file_name=name)
        except Exception:
            return
        if not result:
            return
        try:
            self.composed_img.save(result, "PNG")
            self._snack(f"已保存: {Path(result).name}", folder=str(Path(result).parent))
            self._set_status(str(result))
        except Exception as ex:
            self._snack(f"保存失败: {ex}", error=True)

    async def _batch_export(self, e):
        if not self.part_info or not self.selected_info or not self.part_imgs:
            self._snack("请先选择底图并合成", error=True)
            return
        if not self.base_imgs:
            self._snack("底图为空", error=True)
            return
        try:
            result = await self.file_picker.get_directory_path(dialog_title="选择导出目录")
        except Exception:
            return
        if not result:
            return
        save_dir = str(result)
        self._set_status("正在批量导出…")

        base = self.base_imgs[0]
        ox = self.part_info.get("offset_x", 0) + self.nudge_x
        oy = self.part_info.get("offset_y", 0) + self.nudge_y
        fname = self.selected_info["filename"]
        parts_data = list(self.part_imgs)

        def one(args):
            idx, p = args
            try:
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


def _thumb_bytes(img, size=100):
    w, h = img.size
    s = min(1.0, size / max(w, h))
    thumb = img.resize((max(1, int(w * s)), max(1, int(h * s))), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    thumb.save(buf, "PNG")
    return buf.getvalue()


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
