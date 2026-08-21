"""FVP 立绘查看与合成工具 — 现代化 Flet 界面（亚克力玻璃 + 新拟态风格）

核心解析 / 合成逻辑复用自 FVPTachieComposer.py，
界面全部使用自定义容器实现：

- 亚克力：窗口底层铺彩色极光渐变，面板使用半透明背景 + 高斯模糊，
  形成 Windows 11 亚克力般的磨砂玻璃质感；
- 新拟态：卡片与按钮通过「暗色右下投影 + 亮色左上投影」的双重阴影实现浮雕效果。

依赖：flet>=0.86  Pillow
运行：python FVPTachieComposerFlet.py
"""

import asyncio
import base64
import io
import random
from functools import lru_cache
from pathlib import Path

import flet as ft
from PIL import Image, ImageDraw, ImageFilter

from FVPTachieComposer import (
    compose_preview,
    hzc_data_to_pil_list,
    parse_bin_info_extended,
)

HELP_TEXT = (
    "【快速开始】\n"
    "1. 点击左上角「打开 BIN」选择 .bin 文件。\n"
    "2. 左侧树形列表按 角色 -> 服装 -> 动作 分类，点击动作即可预览底图。\n\n"
    "【表情与合成】\n"
    "1. 选中底图后，中间下方会自动加载对应的「_表情」差分部件帧。\n"
    "2. 点击任意帧缩略图，右侧立即生成合成预览。\n\n"
    "【导出】\n"
    "1. 保存当前图：导出右侧当前合成结果。\n"
    "2. 批量合成并导出：按当前底图与全部表情帧批量生成 PNG 到指定目录。\n\n"
    "【说明】\n"
    "- 工具不仅可用于立绘，也可查看包内其他可识别图像资源。\n"
    "- 若某文件解析失败，通常是资源格式异常或数据不完整。"
)

# ---------- 主题 ----------
DARK = {
    "key": "dark",
    "aurora": ["#0e1736", "#1c1442", "#0d2138", "#251248"],
    "glow": [(0.36, 0.42, 0.98), (0.62, 0.30, 0.98), (0.20, 0.62, 0.96)],
    "surface": "#171e32",
    "surface_alt": "#1f2843",
    "well": "#121828",
    "glass": "rgba(24, 32, 57, 0.58)",
    "glass_border": "rgba(255, 255, 255, 0.10)",
    "text": "#e9edf9",
    "muted": "#8f9bbd",
    "accent": "#7c9bff",
    "accent_2": "#b48cff",
    "accent_alpha": "rgba(124, 155, 255, 0.24)",
    "accent_glow": "rgba(124, 155, 255, 0.42)",
    "shadow_dark": "#090e1b",
    "shadow_light": "#253050",
    "badge": "#28334f",
    "danger": "#ff7d7d",
    "ok": "#5eea8f",
    "scrim": "rgba(8, 12, 24, 0.55)",
}

LIGHT = {
    "key": "light",
    "aurora": ["#dfe6ff", "#ffe6f1", "#e3f0ff", "#efe4ff"],
    "glow": [(0.55, 0.48, 1.0), (1.0, 0.58, 0.82), (0.42, 0.72, 1.0)],
    "surface": "#e9edf6",
    "surface_alt": "#f4f7fd",
    "well": "#dce3f1",
    "glass": "rgba(255, 255, 255, 0.55)",
    "glass_border": "rgba(255, 255, 255, 0.75)",
    "text": "#232a3d",
    "muted": "#66708f",
    "accent": "#5b7cfa",
    "accent_2": "#9d6bff",
    "accent_alpha": "rgba(91, 124, 250, 0.16)",
    "accent_glow": "rgba(91, 124, 250, 0.40)",
    "shadow_dark": "#c2cadf",
    "shadow_light": "#ffffff",
    "badge": "#dfe5f2",
    "danger": "#e05252",
    "ok": "#2ea860",
    "scrim": "rgba(30, 38, 60, 0.40)",
}

THEMES = {"dark": DARK, "light": LIGHT}

# ---------- 工具函数 ----------


def _hex2rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i : i + 2], 16) for i in (0, 2, 4))


def _img_bytes(img, fmt="PNG"):
    buf = io.BytesIO()
    img.save(buf, fmt)
    return buf.getvalue()


@lru_cache(maxsize=4)
def _aurora_bytes(key, w, h):
    """生成极光渐变背景图（PNG bytes），作为亚克力面板背后的"壁纸"。"""
    t = THEMES[key]
    img = Image.new("RGB", (w, h), _hex2rgb(t["aurora"][0]))
    d = ImageDraw.Draw(img, "RGBA")
    rnd = random.Random(sum(ord(c) for c in key) * 131)
    blobs = [
        ((0.08 + rnd.random() * 0.22, 0.06 + rnd.random() * 0.28), t["glow"][0], 1.7),
        ((0.50 + rnd.random() * 0.32, 0.30 + rnd.random() * 0.34), t["glow"][1], 1.35),
        ((0.72 + rnd.random() * 0.16, 0.68 + rnd.random() * 0.18), t["glow"][2], 1.2),
        ((0.22 + rnd.random() * 0.30, 0.82 + rnd.random() * 0.12), t["glow"][1], 1.45),
    ]
    for (cx, cy), color, scale in blobs:
        r = int(min(w, h) * scale)
        cx, cy = int(cx * w), int(cy * h)
        steps = 26
        glow_rgb = tuple(int(c * 255) for c in color)
        for i in range(steps, 0, -1):
            rr = int(r * i / steps)
            a = int(230 * ((1 - i / steps) ** 2.2))
            d.ellipse((cx - rr, cy - rr, cx + rr, cy + rr), fill=(*glow_rgb, a))
    img = img.filter(ImageFilter.GaussianBlur(min(w, h) * 0.09))
    return _img_bytes(img)


# ---------- 新拟态 / 亚克力 组件工厂 ----------


def _dual_shadow(t, spread=0, blur=18, dist=6):
    """新拟态浮雕投影：亮色左上 + 暗色右下。"""
    return [
        ft.BoxShadow(blur_radius=blur, spread_radius=spread, offset=ft.Offset(dist, dist), color=t["shadow_dark"]),
        ft.BoxShadow(blur_radius=blur, spread_radius=spread, offset=ft.Offset(-dist, -dist), color=t["shadow_light"]),
    ]


def _soft_shadow(t):
    """轻微投影（用于缩略图等小元素）。"""
    return [
        ft.BoxShadow(blur_radius=8, spread_radius=0, offset=ft.Offset(3, 3), color=t["shadow_dark"]),
        ft.BoxShadow(blur_radius=8, spread_radius=0, offset=ft.Offset(-3, -3), color=t["shadow_light"]),
    ]


def _glow_shadow(t):
    return [ft.BoxShadow(blur_radius=20, spread_radius=1, offset=ft.Offset(0, 0), color=t["accent_glow"])]


def _glass_panel(t, content, expand=False, width=None, height=None, radius=26, padding=18):
    """亚克力磨砂玻璃面板。"""
    return ft.Container(
        content=content,
        bgcolor=t["glass"],
        blur=ft.Blur(22, 22),
        border=ft.Border.all(1, t["glass_border"]),
        border_radius=radius,
        padding=padding,
        shadow=_dual_shadow(t),
        expand=expand,
        width=width,
        height=height,
    )


def _well(t, content, expand=False, radius=18, height=None):
    """内凹预览区（图像画布）。"""
    return ft.Container(
        content=content,
        bgcolor=t["well"],
        border_radius=radius,
        border=ft.Border.all(1, t["glass_border"]),
        padding=10,
        expand=expand,
        height=height,
        shadow=[
            ft.BoxShadow(blur_radius=6, spread_radius=0, offset=ft.Offset(2, 2), color=t["shadow_dark"]),
            ft.BoxShadow(blur_radius=6, spread_radius=0, offset=ft.Offset(-2, -2), color=t["shadow_light"]),
        ],
    )


def _section_header(t, text, icon=None, sub=None, expand=False):
    controls = [
        ft.Container(
            width=4,
            height=16,
            border_radius=2,
            gradient=ft.LinearGradient(
                begin=ft.Alignment(-1, -1), end=ft.Alignment(1, 1), colors=[t["accent"], t["accent_2"]]
            ),
        )
    ]
    if icon:
        controls.append(ft.Icon(icon, size=16, color=t["accent"]))
    controls.append(ft.Text(text, size=13, weight=ft.FontWeight.BOLD, color=t["text"]))
    if sub:
        controls.append(ft.Text(sub, size=11, color=t["muted"]))
    row = ft.Row(controls, spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER)
    if expand:
        return ft.Row([row, ft.Container(expand=True)], spacing=0, vertical_alignment=ft.CrossAxisAlignment.CENTER)
    return row


def _btn(t, text, on_click, icon=None, accent=False, disabled=False, expand=False, tooltip=None):
    """新拟态按钮（含悬停凹陷反馈）。"""
    if accent:
        fg = "#ffffff"
        bg = ft.LinearGradient(
            begin=ft.Alignment(-1, -1), end=ft.Alignment(1, 1), colors=[t["accent"], t["accent_2"]]
        )
        shadow = [ft.BoxShadow(blur_radius=18, spread_radius=0, offset=ft.Offset(0, 6), color=t["accent_glow"])]
    else:
        fg = t["text"]
        bg = t["surface"]
        shadow = _dual_shadow(t)

    def _hover(e):
        if e.data == "true":
            if accent:
                e.control.shadow = [ft.BoxShadow(blur_radius=10, spread_radius=0, offset=ft.Offset(0, 2), color=t["accent_glow"])]
            else:
                e.control.shadow = _dual_shadow(t, blur=10, dist=3)
                e.control.bgcolor = t["surface_alt"]
        else:
            e.control.shadow = shadow
            if not accent:
                e.control.bgcolor = bg
        e.control.update()

    return ft.Container(
        content=ft.Row(
            [
                ft.Icon(icon, size=16, color=fg) if icon else None,
                ft.Text(text, size=13, weight=ft.FontWeight.BOLD, color=fg),
            ],
            spacing=8,
            alignment=ft.MainAxisAlignment.CENTER,
        ),
        bgcolor=bg,
        border_radius=16,
        height=44,
        padding=ft.Padding.symmetric(horizontal=16),
        alignment=ft.Alignment(0, 0),
        shadow=shadow,
        opacity=0.45 if disabled else 1.0,
        on_click=on_click,
        on_hover=_hover,
        tooltip=tooltip,
        expand=expand,
    )


def _icon_btn(t, icon, handler, tip, danger=False, size=34):
    """扁平玻璃图标按钮（标题栏用）。"""
    c = ft.Container(
        content=ft.Icon(icon, size=17, color=t["danger"] if danger else t["muted"]),
        width=size,
        height=size,
        border_radius=10,
        alignment=ft.Alignment(0, 0),
        tooltip=tip,
        on_click=handler,
    )

    def _hover(e):
        if e.data == "true":
            e.control.bgcolor = "rgba(255, 105, 105, 0.25)" if danger else t["surface_alt"]
            if not danger:
                e.control.content.color = t["text"]
        else:
            e.control.bgcolor = None
            if not danger:
                e.control.content.color = t["muted"]
        e.control.update()

    c.on_hover = _hover
    return c


# ---------- 应用 ----------


class ComposerApp:
    def __init__(self, page: ft.Page):
        self.page = page
        self.is_dark = True
        self.t = THEMES["dark"]

        self.input_file = None
        self.file_infos = []
        self.hierarchy = {}
        self.selected_info = None
        self.selected_filename = None
        self.selected_action = None

        self.base_imgs = []
        self.base_idx = 0
        self.part_info = None
        self.part_imgs = []
        self.part_idx = 0
        self.composed_img = None
        self.thumb_refs = []

        self.file_picker = ft.FilePicker()
        page.overlay.append(self.file_picker)

        self._setup_page()
        self._build_layout()

    # ---------- 页面初始化 ----------
    def _setup_page(self):
        page = self.page
        page.title = "FVP Tachie Composer"
        page.window.width = 1600
        page.window.height = 900
        page.window.min_width = 1180
        page.window.min_height = 700
        page.window.title_bar_hidden = True
        page.bgcolor = self.t["well"]
        page.padding = 0
        page.spacing = 0
        page.update()

    def _build_layout(self):
        t = self.t
        self.bg_image = ft.Image(
            src=_aurora_bytes(t["key"], 1600, 1000),
            fit=ft.BoxFit.COVER,
            expand=True,
        )
        self.title_bar = self._build_title_bar()
        self.left_panel = self._build_left_panel()
        self.mid_panel = self._build_mid_panel()
        self.right_panel = self._build_right_panel()
        self.status_text = ft.Text("就绪", size=12, color=t["muted"])
        self.busy_ring = ft.ProgressRing(width=16, height=16, stroke_width=2, color=t["accent"], visible=False)
        status_bar = ft.Container(
            content=ft.Row(
                [self.status_text, ft.Container(expand=True), self.busy_ring],
                spacing=10,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            padding=ft.Padding.symmetric(horizontal=24, vertical=10),
            bgcolor=t["glass"],
            blur=ft.Blur(20, 20),
            border=ft.Border.all(1, t["glass_border"]),
            border_radius=ft.BorderRadius.only(bottom_left=26, bottom_right=26),
        )

        content_row = ft.Row(
            [
                ft.Container(
                    self.left_panel,
                    width=300,
                    padding=ft.Padding.only(left=14, bottom=14),
                    expand=False,
                ),
                ft.Container(
                    self.mid_panel,
                    expand=True,
                    padding=ft.Padding.only(left=7, right=7, bottom=14),
                ),
                ft.Container(
                    self.right_panel,
                    width=384,
                    padding=ft.Padding.only(right=14, bottom=14),
                    expand=False,
                ),
            ],
            expand=True,
            spacing=0,
        )

        self.loading_text = ft.Text("处理中…", size=13, color=t["muted"])
        self.loading_overlay = ft.Container(
            content=ft.Container(
                content=ft.Column(
                    [
                        ft.ProgressRing(width=38, height=38, stroke_width=3, color=t["accent"]),
                        self.loading_text,
                    ],
                    spacing=14,
                    horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                bgcolor=t["surface"],
                border_radius=20,
                padding=ft.Padding.symmetric(horizontal=36, vertical=28),
                shadow=_dual_shadow(t),
            ),
            bgcolor=t["scrim"],
            blur=ft.Blur(8, 8),
            alignment=ft.Alignment(0, 0),
            expand=True,
            visible=False,
        )

        main_col = ft.Column([self.title_bar, content_row, status_bar], spacing=0, expand=True)
        self.page.add(
            ft.Stack([self.bg_image, main_col, self.loading_overlay], expand=True, fit=ft.StackFit.EXPAND)
        )
        self.page.update()

    # ---------- 标题栏 ----------
    def _build_title_bar(self):
        t = self.t
        logo = ft.Row(
            [
                ft.Container(
                    content=ft.Icon(ft.Icons.AUTO_AWESOME, size=16, color="#ffffff"),
                    width=30,
                    height=30,
                    border_radius=9,
                    alignment=ft.Alignment(0, 0),
                    gradient=ft.LinearGradient(
                        begin=ft.Alignment(-1, -1), end=ft.Alignment(1, 1), colors=[t["accent"], t["accent_2"]]
                    ),
                    shadow=_glow_shadow(t),
                ),
                ft.Text("FVP Tachie Composer", size=14, weight=ft.FontWeight.BOLD, color=t["text"]),
                ft.Text("立绘查看与合成", size=11, color=t["muted"]),
            ],
            spacing=10,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

        self.theme_btn = _icon_btn(t, ft.Icons.DARK_MODE, self._toggle_theme, "切换主题")
        right = ft.Row(
            [
                _icon_btn(t, ft.Icons.HELP_OUTLINE, self._help, "使用说明"),
                self.theme_btn,
                _icon_btn(t, ft.Icons.MINIMIZE, self._minimize, "最小化"),
                _icon_btn(t, ft.Icons.CROP_SQUARE, self._toggle_maximize, "最大化 / 还原"),
                _icon_btn(t, ft.Icons.CLOSE, self._close, "关闭", danger=True),
            ],
            spacing=6,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

        strip = ft.Container(
            content=ft.Row([logo, ft.Container(expand=True), right], vertical_alignment=ft.CrossAxisAlignment.CENTER),
            height=54,
            padding=ft.Padding.symmetric(horizontal=18),
            bgcolor=t["glass"],
            blur=ft.Blur(20, 20),
            border=ft.Border.all(1, t["glass_border"]),
            border_radius=ft.BorderRadius.only(top_left=26, top_right=26),
        )
        return ft.WindowDragArea(strip, maximizable=True)

    def _build_left_panel(self):
        t = self.t
        header = ft.Row(
            [
                _section_header(t, "角色库", ft.Icons.PERSON, expand=True),
                _icon_btn(t, ft.Icons.FOLDER_OPEN, self._open_bin, "打开 BIN", size=38),
            ],
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

        self.empty_hint = ft.Container(
            content=ft.Column(
                [
                    ft.Icon(ft.Icons.FOLDER_OPEN, size=46, color=t["muted"]),
                    ft.Text("尚未加载 BIN 文件", size=12, color=t["muted"]),
                    ft.Text("点击右上角文件夹图标打开", size=11, color=t["muted"]),
                ],
                spacing=10,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            alignment=ft.Alignment(0, 0),
            expand=True,
        )
        self.role_list = ft.ListView(controls=[], spacing=8, expand=True, padding=ft.Padding.only(top=2))

        nav_area = ft.Stack(
            [self.empty_hint, self.role_list],
            expand=True,
            fit=ft.StackFit.EXPAND,
        )
        self.role_list.visible = False

        return _glass_panel(self.t, ft.Column([header, nav_area], spacing=12, expand=True), expand=True)

    def _build_mid_panel(self):
        t = self.t
        self.frame_label = ft.Text("帧 0/0", size=12, color=t["muted"])

        self.base_hint = ft.Container(
            content=ft.Column(
                [
                    ft.Icon(ft.Icons.IMAGE, size=52, color=t["muted"]),
                    ft.Text("在左侧选择底图进行预览", size=12, color=t["muted"]),
                ],
                spacing=10,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            alignment=ft.Alignment(0, 0),
            expand=True,
        )
        self.base_image = ft.Image(src=None, fit=ft.BoxFit.CONTAIN, expand=True, border_radius=14)
        base_well = _well(
            t,
            ft.Stack([self.base_hint, self.base_image], expand=True, fit=ft.StackFit.EXPAND),
            expand=True,
        )

        self.prev_btn = self._round_btn(ft.Icons.CHEVRON_LEFT, self._prev_frame)
        self.next_btn = self._round_btn(ft.Icons.CHEVRON_RIGHT, self._next_frame)
        frame_row = ft.Row(
            [
                ft.Text("底图多帧浏览", size=11, color=t["muted"]),
                ft.Container(expand=True),
                self.prev_btn,
                self.frame_label,
                self.next_btn,
            ],
            spacing=8,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )
        self.frame_row_holder = ft.Container(frame_row, visible=False)

        base_card = ft.Column(
            [
                _section_header(t, "底图预览", ft.Icons.IMAGE, expand=True),
                base_well,
                self.frame_row_holder,
            ],
            spacing=10,
            expand=True,
        )

        self.part_count = ft.Text("", size=11, color=t["muted"])
        part_header = ft.Row(
            [_section_header(t, "差分部件", ft.Icons.TUNE, expand=True), self.part_count],
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )
        self.part_hint = ft.Container(
            content=ft.Column(
                [
                    ft.Icon(ft.Icons.TUNE, size=34, color=t["muted"]),
                    ft.Text("选择底图后在此显示表情部件帧", size=11, color=t["muted"]),
                ],
                spacing=8,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            alignment=ft.Alignment(0, 0),
            expand=True,
        )
        self.thumb_list = ft.ListView(
            controls=[],
            spacing=10,
            horizontal=True,
            height=150,
            padding=ft.Padding.only(bottom=4),
        )
        self.thumb_list.visible = False

        part_card = ft.Column(
            [
                part_header,
                ft.Stack(
                    [self.part_hint, self.thumb_list],
                    expand=True,
                    fit=ft.StackFit.EXPAND,
                ),
            ],
            spacing=8,
            expand=False,
        )

        return _glass_panel(self.t, ft.Column([base_card, part_card], spacing=12, expand=True), expand=True)

    def _build_right_panel(self):
        t = self.t
        self.result_hint = ft.Container(
            content=ft.Column(
                [
                    ft.Icon(ft.Icons.PALETTE, size=52, color=t["muted"]),
                    ft.Text("点击部件帧后显示合成结果", size=12, color=t["muted"]),
                ],
                spacing=10,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            alignment=ft.Alignment(0, 0),
            expand=True,
        )
        self.result_image = ft.Image(src=None, fit=ft.BoxFit.CONTAIN, expand=True, border_radius=14)
        result_well = _well(
            t,
            ft.Stack([self.result_hint, self.result_image], expand=True, fit=ft.StackFit.EXPAND),
            expand=True,
        )

        self.compose_btn = _btn(t, "合成预览", self._compose, icon=ft.Icons.TUNE, accent=True, disabled=True, expand=True)
        self.save_btn = _btn(t, "保存当前图", self._save_current, icon=ft.Icons.SAVE_ALT, disabled=True, expand=True)
        self.batch_btn = _btn(t, "批量合成并导出", self._batch_export, icon=ft.Icons.DOWNLOAD, disabled=True, expand=True)

        return _glass_panel(
            self.t,
            ft.Column(
                [
                    _section_header(t, "合成结果", ft.Icons.PALETTE, expand=True),
                    result_well,
                    ft.Column([self.compose_btn, self.save_btn, self.batch_btn], spacing=12),
                ],
                spacing=14,
                expand=True,
            ),
            expand=True,
        )

    def _round_btn(self, icon, handler):
        t = self.t
        c = ft.Container(
            content=ft.Icon(icon, size=18, color=t["text"]),
            width=36,
            height=36,
            border_radius=18,
            alignment=ft.Alignment(0, 0),
            bgcolor=t["surface"],
            shadow=_soft_shadow(t),
            on_click=handler,
        )
        c._disabled = False
        return c

    def _set_round_enabled(self, btn, enabled):
        btn._disabled = not enabled
        btn.opacity = 1.0 if enabled else 0.35
        btn.update()

    # ---------- 标题栏事件 ----------
    async def _minimize(self, e):
        self.page.window.minimized = True
        self.page.update()

    async def _toggle_maximize(self, e):
        self.page.window.maximized = not self.page.window.maximized
        self.page.update()

    async def _close(self, e):
        await self.page.window.destroy()

    def _help(self, e):
        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text("使用说明", weight=ft.FontWeight.BOLD),
            content=ft.Text(HELP_TEXT, size=13, selectable=True),
            actions=[ft.TextButton("知道了", on_click=lambda e: self.page.pop_dialog())],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        self.page.show_dialog(dlg)
        self.page.update()

    async def _toggle_theme(self, e):
        self.is_dark = not self.is_dark
        self.t = THEMES["dark" if self.is_dark else "light"]
        self.page.clean()
        self._build_layout()
        if self.input_file:
            self._render_roles()
            if self.selected_info:
                self._load_base(self.selected_info)
            self._set_status(f"已加载 {Path(self.input_file).name}")
        self.page.update()

    # ---------- 状态 ----------
    def _set_status(self, text):
        self.status_text.value = text
        self.page.update()

    def _set_busy(self, on):
        self.busy_ring.visible = on
        self.page.update()

    def _set_loading(self, on, text=None):
        self.loading_overlay.visible = on
        if text is not None:
            self.loading_text.value = text
        self.page.update()

    def _snack(self, msg, error=False, warning=False):
        t = self.t
        color = t["danger"] if error else (t["accent"] if warning else t["ok"])
        self.page.show_dialog(
            ft.SnackBar(
                content=ft.Text(msg, size=13, color=color),
                bgcolor=t["surface_alt"],
                show_close_icon=True,
                duration=ft.Duration(seconds=3),
            )
        )
        self.page.update()

    # ---------- 打开 / 解析 BIN ----------
    async def _open_bin(self, e):
        try:
            files = await self.file_picker.pick_files(
                dialog_title="选择 BIN 文件",
                allowed_extensions=["bin"],
                allow_multiple=False,
            )
        except Exception:
            files = None
        if not files:
            return
        await self._load_bin(files[0].path)

    async def _load_bin(self, path):
        self._set_loading(True, "正在解析 BIN 文件…")

        def work():
            infos = parse_bin_info_extended(path)
            hier = {}
            for info in infos:
                if info["type"] != "hzc":
                    continue
                parts = info["filename"].split("_")
                if len(parts) >= 2 and parts[0] == "CHR":
                    role = parts[1]
                    outfit = parts[3] if len(parts) >= 4 else "默认"
                else:
                    role = info["filename"]
                    outfit = "默认"
                hier.setdefault(role, {}).setdefault(outfit, []).append(info)
            return infos, hier

        try:
            infos, hier = await asyncio.to_thread(work)
        except Exception as ex:
            self._set_loading(False)
            self._snack(f"解析失败: {ex}", error=True)
            return

        self.input_file = path
        self.file_infos = infos
        self.hierarchy = hier
        self.selected_info = None
        self.selected_filename = None
        self.selected_action = None
        self._clear_previews()
        self._render_roles()
        self._set_loading(False)
        hzc_count = sum(1 for i in infos if i["type"] == "hzc")
        self._set_status(f"已加载 {Path(path).name} · {len(hier)} 个角色 · {hzc_count} 个图像")

    def _clear_previews(self):
        t = self.t
        self.base_imgs = []
        self.part_imgs = []
        self.part_info = None
        self.composed_img = None
        self.base_image.src = None
        self.result_image.src = None
        self.base_hint.visible = True
        self.result_hint.visible = True
        self.frame_label.value = "帧 0/0"
        self.frame_row_holder.visible = False
        self.thumb_list.controls.clear()
        self.thumb_list.visible = False
        self.part_hint.visible = True
        self.part_count.value = ""
        self.compose_btn.disabled = True
        self.save_btn.disabled = True
        self.batch_btn.disabled = True
        self.compose_btn.opacity = 0.45
        self.save_btn.opacity = 0.45
        self.batch_btn.opacity = 0.45

    # ---------- 角色树 ----------
    def _render_roles(self):
        self.role_list.controls.clear()
        for role, outfits in sorted(self.hierarchy.items()):
            self.role_list.controls.append(self._make_role_tile(role, outfits))
        self.role_list.visible = True
        self.empty_hint.visible = False
        self.page.update()

    def _make_role_tile(self, role, outfits):
        t = self.t
        state = {"open": False}
        count = sum(len(v) for v in outfits.values())

        body = ft.Column(controls=[], spacing=2, visible=False)
        chev = ft.Icon(ft.Icons.CHEVRON_RIGHT, size=18, color=t["muted"])
        header = ft.Container(
            content=ft.Row(
                [
                    ft.Icon(ft.Icons.PERSON, size=17, color=t["accent"]),
                    ft.Text(
                        role,
                        size=13,
                        weight=ft.FontWeight.BOLD,
                        color=t["text"],
                        expand=True,
                        max_lines=1,
                        overflow=ft.TextOverflow.ELLIPSIS,
                    ),
                    ft.Container(
                        ft.Text(str(count), size=10, color=t["muted"]),
                        bgcolor=t["badge"],
                        border_radius=10,
                        padding=ft.Padding.symmetric(horizontal=8, vertical=2),
                    ),
                    chev,
                ],
                spacing=8,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            bgcolor=t["surface_alt"],
            border_radius=14,
            padding=ft.Padding.symmetric(horizontal=12, vertical=10),
            shadow=_soft_shadow(t),
            on_click=lambda e: self._toggle_body(body, chev, state),
        )
        for outfit, infos in sorted(outfits.items()):
            body.controls.append(self._make_outfit_tile(outfit, infos))
        return ft.Column([header, body], spacing=4)

    def _make_outfit_tile(self, outfit, infos):
        t = self.t
        state = {"open": False}
        body = ft.Column(controls=[], spacing=2, visible=False)
        chev = ft.Icon(ft.Icons.CHEVRON_RIGHT, size=15, color=t["muted"])
        header = ft.Container(
            content=ft.Row(
                [
                    ft.Icon(ft.Icons.FOLDER, size=15, color=t["accent_2"]),
                    ft.Text(
                        outfit,
                        size=12,
                        color=t["text"],
                        expand=True,
                        max_lines=1,
                        overflow=ft.TextOverflow.ELLIPSIS,
                    ),
                    chev,
                ],
                spacing=8,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            border_radius=12,
            padding=ft.Padding.symmetric(horizontal=10, vertical=7),
            on_click=lambda e: self._toggle_body(body, chev, state),
        )
        for info in sorted(infos, key=lambda x: x["filename"]):
            if info["filename"].endswith("_表情"):
                continue
            parts = info["filename"].split("_")
            name = parts[4] if len(parts) >= 5 else info["filename"]
            body.controls.append(self._make_action_tile(name, info))
        return ft.Column([header, body], spacing=2)

    def _toggle_body(self, body, chev, state):
        state["open"] = not state["open"]
        body.visible = state["open"]
        chev.name = ft.Icons.EXPAND_MORE if state["open"] else ft.Icons.CHEVRON_RIGHT
        self.page.update()

    def _make_action_tile(self, name, info):
        t = self.t
        c = ft.Container(
            content=ft.Row(
                [
                    ft.Icon(ft.Icons.IMAGE, size=14, color=t["muted"]),
                    ft.Text(
                        name,
                        size=12,
                        color=t["text"],
                        expand=True,
                        max_lines=1,
                        overflow=ft.TextOverflow.ELLIPSIS,
                    ),
                ],
                spacing=8,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            border_radius=10,
            padding=ft.Padding.symmetric(horizontal=10, vertical=7),
            bgcolor=t["accent_alpha"] if info["filename"] == self.selected_filename else None,
            on_click=lambda e: self._select_base(info, c),
        )
        return c

    def _select_base(self, info, c):
        t = self.t
        if self.selected_action is not None:
            self.selected_action.bgcolor = None
        self.selected_action = c
        self.selected_filename = info["filename"]
        c.bgcolor = t["accent_alpha"]
        self.page.update()
        self._load_base(info)

    # ---------- 底图 / 部件 ----------
    def _read_pil_list(self, info):
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
        self._show_base_frame()
        self._load_parts(info)

    def _show_base_frame(self):
        t = self.t
        img = self.base_imgs[self.base_idx]
        self.base_image.src = _img_bytes(img)
        self.base_hint.visible = False
        self.frame_label.value = f"帧 {self.base_idx + 1}/{len(self.base_imgs)}"
        multi = len(self.base_imgs) > 1
        self.frame_row_holder.visible = multi
        self._set_round_enabled(self.prev_btn, multi)
        self._set_round_enabled(self.next_btn, multi)
        self.page.update()

    def _prev_frame(self, e):
        if getattr(self.prev_btn, "_disabled", True) or self.base_idx <= 0:
            return
        self.base_idx -= 1
        self._show_base_frame()

    def _next_frame(self, e):
        if getattr(self.next_btn, "_disabled", True) or self.base_idx >= len(self.base_imgs) - 1:
            return
        self.base_idx += 1
        self._show_base_frame()

    def _load_parts(self, info):
        t = self.t
        self.part_info = None
        self.part_imgs = []
        self.thumb_refs = []
        self.thumb_list.controls.clear()
        self.thumb_list.visible = False
        self.part_hint.visible = True
        self.part_count.value = ""
        self.compose_btn.disabled = True
        self.save_btn.disabled = True
        self.batch_btn.disabled = True
        self.compose_btn.opacity = 0.45
        self.save_btn.opacity = 0.45
        self.batch_btn.opacity = 0.45

        part_name = info["filename"] + "_表情"
        part_info = next(
            (i for i in self.file_infos if i["filename"] == part_name and i["type"] == "hzc"),
            None,
        )
        if not part_info:
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
        t = self.t
        w, h = img.size
        max_side = 116
        scale = min(1.0, max_side / max(w, h))
        thumb = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.Resampling.LANCZOS)
        data = _img_bytes(thumb)

        c = ft.Container(
            content=ft.Stack(
                [
                    ft.Image(src=data, fit=ft.BoxFit.CONTAIN, width=112, height=112, border_radius=10),
                    ft.Container(
                        ft.Text(str(idx), size=10, color=t["text"]),
                        bgcolor=t["badge"],
                        border_radius=8,
                        padding=ft.Padding.symmetric(horizontal=6, vertical=1),
                        alignment=ft.Alignment(-1, -1),
                        margin=ft.Margin.all(4),
                    ),
                ],
            ),
            width=112,
            height=112,
            bgcolor=t["surface_alt"],
            border_radius=14,
            border=ft.Border.all(1, t["glass_border"]),
            padding=0,
            shadow=_soft_shadow(t),
            on_click=lambda e, i=idx: self._pick_part(i),
        )
        self.thumb_refs.append((c, idx))
        return c

    def _pick_part(self, idx):
        t = self.t
        if not self.part_imgs or idx >= len(self.part_imgs):
            return
        self.part_idx = idx
        for c, i in self.thumb_refs:
            if i == idx:
                c.border = ft.Border.all(2, t["accent"])
                c.shadow = _glow_shadow(t)
            else:
                c.border = ft.Border.all(1, t["glass_border"])
                c.shadow = _soft_shadow(t)
        self._compose()

    def _compose(self, e=None):
        t = self.t
        if not self.part_info or not self.selected_info or not self.part_imgs:
            return
        if self.part_idx >= len(self.part_imgs) or not self.base_imgs:
            return
        part_img = self.part_imgs[self.part_idx]
        base_img = self.base_imgs[0]
        composed = compose_preview(
            base_img,
            part_img,
            self.part_info.get("offset_x", 0),
            self.part_info.get("offset_y", 0),
        )
        self.composed_img = composed
        self.result_image.src = _img_bytes(composed)
        self.result_hint.visible = False
        self.save_btn.disabled = False
        self.batch_btn.disabled = False
        self.save_btn.opacity = 1.0
        self.batch_btn.opacity = 1.0
        self.page.update()

    # ---------- 保存 / 导出 ----------
    async def _save_current(self, e):
        if not self.composed_img or not self.selected_info:
            self._snack("请先选择底图并生成合成预览", warning=True)
            return
        name = f"{self.selected_info['filename']}_diff_{self.part_idx:03d}.png"
        try:
            path = await self.file_picker.save_file(
                dialog_title="保存当前合成图像",
                file_name=name,
                file_type=ft.FilePickerFileType.CUSTOM,
                allowed_extensions=["png"],
            )
        except Exception:
            path = None
        if not path:
            return
        try:
            self.composed_img.save(path, "PNG")
            self._snack(f"已保存: {Path(path).name}")
        except Exception as ex:
            self._snack(f"保存失败: {ex}", error=True)

    async def _batch_export(self, e):
        if not self.part_info or not self.selected_info or not self.part_imgs:
            self._snack("请先选择底图并生成合成预览", warning=True)
            return
        try:
            save_dir = await self.file_picker.get_directory_path(dialog_title="选择保存图像的目录")
        except Exception:
            save_dir = None
        if not save_dir:
            return
        self._set_loading(True, "正在批量合成…")

        def work():
            base = self.base_imgs[0]
            ox = self.part_info.get("offset_x", 0)
            oy = self.part_info.get("offset_y", 0)
            saved = 0
            for idx, p in enumerate(self.part_imgs):
                composed = compose_preview(base, p, ox, oy)
                out = Path(save_dir) / f"{self.selected_info['filename']}_diff_{idx:03d}.png"
                composed.save(out, "PNG")
                saved += 1
            return saved

        try:
            n = await asyncio.to_thread(work)
            self._snack(f"已导出 {n} 个合成图像到 {save_dir}")
        except Exception as ex:
            self._snack(f"导出失败: {ex}", error=True)
        finally:
            self._set_loading(False)


def main(page: ft.Page):
    ComposerApp(page)


if __name__ == "__main__":
    ft.run(main)
