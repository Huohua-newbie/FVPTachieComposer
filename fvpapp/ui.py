"""界面：布局、控件树、三栏与卡片构建。"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import math
import os
import sys
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import flet as ft
import flet.canvas as fc
from PIL import Image

from .engine import (
    compose_preview,
    hzc_data_to_pil_list,
    hzc_decode_first_frame,
    parse_bin_info_extended,
    parse_dir_infos,
)

from .dropzone import DROPZONE_ON, _custom_client_available, _dropzone, _dropzone_on, pick_drop_path
from .media import (
    EXPR_SUFFIXES,
    _SRC_CACHE,
    _SRC_CACHE_MAX,
    _THUMB_POOL,
    _THUMB_SRC_CACHE,
    _THUMB_SRC_CACHE_MAX,
    _cached_src,
    _cached_thumb,
    _checker_src,
    _find_expr_info,
    _img_bytes,
    _img_src,
    _is_expr,
    _make_thumb_widget,
    _part_thumb_srcs,
    _thumb_bytes,
)
from .theme import (
    ANIM_ROW,
    ANIM_VEIL,
    BUSY_DELAY,
    GUTTER,
    INDENT_ACTION,
    INDENT_OUTFIT,
    PART_HINT_EMPTY,
    PART_HINT_IDLE,
    PART_STRIP_H,
    RADIUS_PANEL,
    RADIUS_STAGE,
    RADIUS_TILE,
    STAGGER_MS,
    STAGGER_RISE,
    THUMB,
    TRANSPARENT,
    _app_icon,
    _palette,
    _rgb,
    _tip,
)

log = logging.getLogger("fvptachie")


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


class UiMixin:
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
        # 成功提示右侧的「打开文件夹」，默认藏起来，只在刚做完导出/保存时露脸
        self.status_action = ft.Container(
            content=ft.Text("打开文件夹", size=11, color=self.ink["accent"]),
            on_click=self._open_status_folder,
            visible=False,
            padding=ft.Padding.only(left=8, right=2),
        )

        status_bar = ft.Container(
            content=ft.Row(
                [
                    ft.Container(width=6, height=6, border_radius=3, bgcolor=self.ink["accent"]),
                    ft.Container(self.status_text, expand=True),
                    self.status_action,
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
            log.debug("忽略异常（_set_tile_bg）", exc_info=True)
            pass


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
                        self._spawn(self._select_action(info))
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
            on_click=lambda e: self._spawn(self._select_action(info)),
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


    def _result_meta_text(self, size, suffix):
        """结果面板不重复底图尺寸：与当前底图帧同尺寸时只留后缀（偏移/底图模式），
        避免左右两个面板头同时显示同一个「583×989」。尺寸不同才带上。"""
        base = self.base_imgs[self.base_idx].size if self.base_imgs else None
        return suffix if base == size else f"{size[0]}×{size[1]} · {suffix}"
