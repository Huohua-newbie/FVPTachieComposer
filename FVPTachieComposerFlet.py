"""FVP Tachie Composer — Flet UI

FVP engine character sprite viewer & composer.
Parsing and composition logic reused from FVPTachieComposer.py.

Dependencies: flet>=0.86  Pillow>=10
Run: python FVPTachieComposerFlet.py
"""

import asyncio
import io
from pathlib import Path

import flet as ft
from PIL import Image

from FVPTachieComposer import (
    compose_preview,
    hzc_data_to_pil_list,
    parse_bin_info_extended,
)

TRANSPARENT = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"

HELP_TEXT = (
    "【快速开始】\n"
    "1. 点击「打开 BIN」选择 .bin 文件。\n"
    "2. 左侧按 角色 → 服装 → 动作 展开，点击动作预览底图。\n\n"
    "【表情与合成】\n"
    "1. 选中底图后，中间自动加载对应的「_表情」差分部件。\n"
    "2. 点击缩略图，右侧立即生成合成预览。\n\n"
    "【导出】\n"
    "1. 保存当前图：导出右侧当前合成结果。\n"
    "2. 批量合成并导出：批量生成全部差分帧。\n"
)

# ── Themes ──────────────────────────────────────────────────

DARK = {
    "bg": "#1e2028",
    "surface": "#282c34",
    "surface2": "#32363f",
    "surface3": "#3c404a",
    "accent": "#5b9bf5",
    "accent2": "#a78bfa",
    "accent_bg": "rgba(91,155,245,0.12)",
    "accent_border": "rgba(91,155,245,0.35)",
    "text": "#e1e4eb",
    "text2": "#a0a6b8",
    "text3": "#6c7391",
    "border": "#3a3e48",
    "danger": "#f06060",
    "ok": "#50c878",
    "shadow": "rgba(0,0,0,0.35)",
}

LIGHT = {
    "bg": "#f0f2f5",
    "surface": "#ffffff",
    "surface2": "#f7f8fa",
    "surface3": "#ebedf2",
    "accent": "#3b82f6",
    "accent2": "#7c5bf5",
    "accent_bg": "rgba(59,130,246,0.08)",
    "accent_border": "rgba(59,130,246,0.30)",
    "text": "#1a1d27",
    "text2": "#5c6378",
    "text3": "#8b92a8",
    "border": "#dde0e8",
    "danger": "#dc3545",
    "ok": "#28a745",
    "shadow": "rgba(0,0,0,0.08)",
}

THEMES = {"dark": DARK, "light": LIGHT}


def _img_bytes(img, fmt="PNG"):
    buf = io.BytesIO()
    img.save(buf, fmt)
    return buf.getvalue()


# ── Component helpers ───────────────────────────────────────

def _card(t, content, expand=False, pad=16, radius=14, **kw):
    return ft.Container(
        content=content,
        bgcolor=t["surface"],
        border_radius=radius,
        border=ft.Border.all(1, t["border"]),
        padding=ft.Padding.all(pad),
        shadow=[ft.BoxShadow(6, 0, ft.Offset(0, 2), t["shadow"])],
        expand=expand,
        **kw,
    )


def _section(t, text, icon=None):
    items = []
    if icon:
        items.append(ft.Icon(icon, size=15, color=t["accent"]))
    items.append(ft.Text(text, size=13, weight=ft.FontWeight.W_600, color=t["text"]))
    return ft.Row(items, spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER)


def _btn(t, text, on_click, icon=None, primary=False, disabled=False, expand=False):
    fg = "#ffffff" if primary else t["text"]
    bg = t["accent"] if primary else t["surface2"]
    border = t["accent"] if primary else t["border"]

    def _hover(e):
        e.control.bgcolor = t["accent2"] if (primary and e.data == "true") else (
            t["surface3"] if e.data == "true" else bg
        )
        e.control.update()

    return ft.Container(
        content=ft.Row(
            [ft.Icon(icon, size=15, color=fg)] if icon else []
            + [ft.Text(text, size=12, weight=ft.FontWeight.W_600, color=fg)],
            spacing=6,
            alignment=ft.MainAxisAlignment.CENTER,
        ),
        bgcolor=bg,
        border=ft.Border.all(1, border),
        border_radius=10,
        height=40,
        padding=ft.Padding.symmetric(horizontal=14),
        alignment=ft.Alignment.CENTER,
        on_click=on_click,
        on_hover=_hover,
        opacity=0.4 if disabled else 1.0,
        disabled=disabled,
        expand=expand,
    )


def _icon_btn(t, icon, on_click, tip=None, danger=False):
    c = ft.Container(
        content=ft.Icon(icon, size=16, color=t["danger"] if danger else t["text2"]),
        width=32, height=32, border_radius=8,
        alignment=ft.Alignment.CENTER,
        tooltip=tip,
        on_click=on_click,
    )

    def _hover(e):
        bg = None
        fg = t["text2"]
        if e.data == "true":
            bg = "rgba(240,96,96,0.18)" if danger else t["surface3"]
            fg = t["danger"] if danger else t["text"]
        c.bgcolor = bg
        c.content.color = fg
        c.update()

    c.on_hover = _hover
    return c


# ── App ─────────────────────────────────────────────────────

class ComposerApp:
    def __init__(self, page: ft.Page):
        self.page = page
        self.dark = True
        self.t = DARK

        self.input_file = None
        self.file_infos = []
        self.hierarchy = {}
        self.selected_info = None
        self.selected_filename = None
        self.selected_action_ctrl = None

        self.base_imgs = []
        self.base_idx = 0
        self.part_info = None
        self.part_imgs = []
        self.part_idx = 0
        self.composed_img = None
        self.thumb_refs = []

        self._build()

    # ── Build ───────────────────────────────────────────────

    def _build(self):
        t = self.t
        p = self.page
        p.title = "FVP Tachie Composer"
        p.window.width = 1440
        p.window.height = 880
        p.window.min_width = 1080
        p.window.min_height = 640
        p.bgcolor = self.t["bg"]
        p.padding = 0
        p.spacing = 0

        self.title_bar = self._build_title_bar()
        self.tree_col = self._build_tree_column()
        self.base_col = self._build_base_column()
        self.result_col = self._build_result_column()
        self.status_text = ft.Text("就绪", size=11, color=self.t["text3"])

        status_bar = ft.Container(
            content=ft.Row([self.status_text]),
            height=32,
            padding=ft.Padding.symmetric(horizontal=16),
            bgcolor=t["surface"],
            border=ft.Border.only(top=ft.BorderSide(1, t["border"])),
        )

        body = ft.Row(
            [
                ft.Container(self.tree_col, width=290, padding=ft.Padding.only(left=12, top=10, bottom=10)),
                ft.Container(self.base_col, expand=True, padding=ft.Padding.symmetric(horizontal=6, vertical=10)),
                ft.Container(self.result_col, width=360, padding=ft.Padding.only(right=12, top=10, bottom=10)),
            ],
            expand=True,
            spacing=0,
        )

        p.add(ft.Column([self.title_bar, body, status_bar], spacing=0, expand=True))
        p.update()

    # ── Title bar ───────────────────────────────────────────

    def _build_title_bar(self):
        t = self.t
        logo = ft.Row(
            [
                ft.Container(
                    content=ft.Icon(ft.Icons.IMAGE, size=16, color="#ffffff"),
                    width=28, height=28, border_radius=7,
                    bgcolor=t["accent"],
                    alignment=ft.Alignment.CENTER,
                ),
                ft.Text("FVP Tachie Composer", size=13, weight=ft.FontWeight.W_700, color=t["text"]),
                ft.Text("立绘查看与合成", size=11, color=t["text3"]),
            ],
            spacing=8,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

        self.theme_icon = ft.Icons.LIGHT_MODE if self.dark else ft.Icons.DARK_MODE
        right = ft.Row(
            [
                _icon_btn(t, ft.Icons.HELP_OUTLINE, self._help, "使用说明"),
                _icon_btn(t, self.theme_icon, self._toggle_theme, "切换主题"),
                ft.Container(width=1, height=20, bgcolor=t["border"]),
                _icon_btn(t, ft.Icons.MINIMIZE, self._minimize, "最小化"),
                _icon_btn(t, ft.Icons.CROP_SQUARE, self._toggle_maximize, "最大化"),
                _icon_btn(t, ft.Icons.CLOSE, self._close, "关闭", danger=True),
            ],
            spacing=4,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

        bar = ft.Container(
            content=ft.Row([logo, ft.Container(expand=True), right], vertical_alignment=ft.CrossAxisAlignment.CENTER),
            height=46,
            padding=ft.Padding.symmetric(horizontal=14),
            bgcolor=t["surface"],
            border=ft.Border.only(bottom=ft.BorderSide(1, t["border"])),
        )
        return ft.WindowDragArea(bar, maximizable=True)

    # ── Left: tree ──────────────────────────────────────────

    def _build_tree_column(self):
        t = self.t
        header = ft.Row(
            [
                _section(t, "角色库", ft.Icons.PEOPLE_OUTLINE),
                _icon_btn(t, ft.Icons.FOLDER_OPEN, self._open_bin, "打开 BIN"),
            ],
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

        self.empty_hint = ft.Container(
            content=ft.Column(
                [
                    ft.Icon(ft.Icons.FOLDER_OPEN, size=40, color=t["text3"]),
                    ft.Text("尚未加载文件", size=12, color=t["text3"]),
                    ft.Text("点击右上角文件夹图标", size=11, color=t["text3"]),
                ],
                spacing=8,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            alignment=ft.Alignment.CENTER,
            expand=True,
        )

        self.tree_list = ft.ListView(spacing=0, expand=True, auto_scroll=False)
        self.tree_list.visible = False

        body = ft.Stack(
            [self.empty_hint, self.tree_list],
            expand=True,
            fit=ft.StackFit.EXPAND,
        )
        return _card(
            t,
            ft.Column([header, body], spacing=0, expand=True),
            expand=True,
            pad=12,
        )

    # ── Center: base preview + parts ────────────────────────

    def _build_base_column(self):
        t = self.t

        self.base_hint = ft.Container(
            content=ft.Column(
                [
                    ft.Icon(ft.Icons.IMAGE_OUTLINED, size=48, color=t["text3"]),
                    ft.Text("在左侧选择底图", size=12, color=t["text3"]),
                ],
                spacing=10,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            alignment=ft.Alignment.CENTER,
            expand=True,
        )

        self.base_image = ft.Image(
            src=TRANSPARENT,
            fit=ft.BoxFit.CONTAIN,
            expand=True,
        )

        base_well = ft.Container(
            content=ft.Stack(
                [self.base_hint, self.base_image],
                expand=True,
                fit=ft.StackFit.EXPAND,
            ),
            bgcolor=t["surface2"],
            border_radius=12,
            border=ft.Border.all(1, t["border"]),
            expand=True,
        )

        self.frame_label = ft.Text("", size=11, color=t["text3"])
        self.prev_btn = ft.Container(
            content=ft.Icon(ft.Icons.CHEVRON_LEFT, size=18, color=t["text2"]),
            width=30, height=30, border_radius=15,
            bgcolor=t["surface3"],
            alignment=ft.Alignment.CENTER,
            on_click=self._prev_frame,
        )
        self.next_btn = ft.Container(
            content=ft.Icon(ft.Icons.CHEVRON_RIGHT, size=18, color=t["text2"]),
            width=30, height=30, border_radius=15,
            bgcolor=t["surface3"],
            alignment=ft.Alignment.CENTER,
            on_click=self._next_frame,
        )
        frame_nav = ft.Row(
            [
                self.prev_btn,
                self.frame_label,
                self.next_btn,
            ],
            spacing=8,
            alignment=ft.MainAxisAlignment.CENTER,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )
        self.frame_nav_holder = ft.Container(frame_nav, visible=False)

        self.part_count = ft.Text("", size=11, color=t["text3"])
        part_header = ft.Row(
            [_section(t, "差分部件", ft.Icons.TUNE), self.part_count],
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

        self.part_hint = ft.Container(
            content=ft.Column(
                [
                    ft.Icon(ft.Icons.TUNE, size=32, color=t["text3"]),
                    ft.Text("选择底图后显示差分帧", size=11, color=t["text3"]),
                ],
                spacing=8,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            alignment=ft.Alignment.CENTER,
            expand=True,
        )

        self.thumb_list = ft.ListView(
            spacing=8,
            horizontal=True,
            height=130,
            auto_scroll=False,
        )
        self.thumb_list.visible = False

        part_area = ft.Stack(
            [self.part_hint, self.thumb_list],
            expand=True,
            fit=ft.StackFit.EXPAND,
        )

        top = ft.Column(
            [
                _section(t, "底图预览", ft.Icons.IMAGE_OUTLINED),
                base_well,
                self.frame_nav_holder,
            ],
            spacing=8,
            expand=True,
        )

        bottom = ft.Column(
            [part_header, part_area],
            spacing=6,
            height=180,
        )

        return _card(
            t,
            ft.Column([top, bottom], spacing=10, expand=True),
            expand=True,
            pad=12,
        )

    # ── Right: result ───────────────────────────────────────

    def _build_result_column(self):
        t = self.t

        self.result_hint = ft.Container(
            content=ft.Column(
                [
                    ft.Icon(ft.Icons.PALETTE_OUTLINED, size=48, color=t["text3"]),
                    ft.Text("点击部件帧后显示合成结果", size=12, color=t["text3"]),
                ],
                spacing=10,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            alignment=ft.Alignment.CENTER,
            expand=True,
        )

        self.result_image = ft.Image(
            src=TRANSPARENT,
            fit=ft.BoxFit.CONTAIN,
            expand=True,
        )

        result_well = ft.Container(
            content=ft.Stack(
                [self.result_hint, self.result_image],
                expand=True,
                fit=ft.StackFit.EXPAND,
            ),
            bgcolor=t["surface2"],
            border_radius=12,
            border=ft.Border.all(1, t["border"]),
            expand=True,
        )

        self.compose_btn = _btn(t, "合成预览", self._compose, icon=ft.Icons.TUNE, primary=True, expand=True)
        self.save_btn = _btn(t, "保存当前图", self._save_current, icon=ft.Icons.SAVE_ALT, disabled=True, expand=True)
        self.batch_btn = _btn(t, "批量合成并导出", self._batch_export, icon=ft.Icons.DOWNLOAD, disabled=True, expand=True)

        return _card(
            t,
            ft.Column(
                [
                    _section(t, "合成结果", ft.Icons.PALETTE_OUTLINED),
                    result_well,
                    ft.Container(height=8),
                    self.compose_btn,
                    self.save_btn,
                    self.batch_btn,
                ],
                spacing=8,
                expand=True,
            ),
            expand=True,
            pad=12,
        )

    # ── Title bar events ────────────────────────────────────

    async def _minimize(self, e):
        self.page.window.minimized = True
        self.page.update()

    async def _toggle_maximize(self, e):
        self.page.window.maximized = not self.page.window.maximized
        self.page.update()

    async def _close(self, e):
        await self.page.window.destroy()

    def _help(self, e):
        self.page.show_dialog(
            ft.AlertDialog(
                modal=True,
                title=ft.Text("使用说明", weight=ft.FontWeight.W_700),
                content=ft.Text(HELP_TEXT, size=13, selectable=True),
                actions=[ft.TextButton("知道了", on_click=lambda e: self.page.pop_dialog())],
                actions_alignment=ft.MainAxisAlignment.END,
            )
        )
        self.page.update()

    async def _toggle_theme(self, e):
        self.dark = not self.dark
        self.t = DARK if self.dark else LIGHT
        self.page.clean()
        self._build()
        if self.input_file:
            self._render_tree()
            if self.selected_info:
                self._load_base(self.selected_info)
            self.status_text.value = f"已加载 {Path(self.input_file).name}"
        self.page.update()

    # ── Status ──────────────────────────────────────────────

    def _snack(self, msg, error=False):
        color = self.t["danger"] if error else self.t["ok"]
        self.page.open(ft.SnackBar(ft.Text(msg, size=12, color=color), bgcolor=self.t["surface2"], duration=3000))

    def _set_status(self, text):
        self.status_text.value = text
        self.page.update()

    # ── Open BIN ────────────────────────────────────────────

    async def _open_bin(self, e):
        fp = ft.FilePicker()
        self.page.overlay.append(fp)
        self.page.update()
        try:
            result = await fp.pick_files(dialog_title="选择 BIN 文件", allowed_extensions=["bin"], allow_multiple=False)
        finally:
            try:
                self.page.overlay.remove(fp)
            except ValueError:
                pass
        if not result or not result.files:
            return
        path = result.files[0].path
        if not path:
            return
        await self._load_bin(path)

    async def _load_bin(self, path):
        self._set_status("正在解析…")

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
            self._snack(f"解析失败: {ex}", error=True)
            return

        self.input_file = path
        self.file_infos = infos
        self.hierarchy = hier
        self.selected_info = None
        self.selected_filename = None
        self.selected_action_ctrl = None
        self._clear_previews()
        self._render_tree()
        hzc = sum(1 for i in infos if i["type"] == "hzc")
        self._set_status(f"{Path(path).name} — {len(hier)} 角色, {hzc} 图像")

    def _clear_previews(self):
        self.base_imgs = []
        self.part_imgs = []
        self.part_info = None
        self.composed_img = None
        self.base_image.src = TRANSPARENT
        self.result_image.src = TRANSPARENT
        self.base_hint.visible = True
        self.result_hint.visible = True
        self.frame_label.value = ""
        self.frame_nav_holder.visible = False
        self.thumb_list.controls.clear()
        self.thumb_list.visible = False
        self.part_hint.visible = True
        self.part_count.value = ""
        self.compose_btn.disabled = True
        self.save_btn.disabled = True
        self.batch_btn.disabled = True
        self.compose_btn.opacity = 0.4
        self.save_btn.opacity = 0.4
        self.batch_btn.opacity = 0.4

    # ── Tree ────────────────────────────────────────────────

    def _render_tree(self):
        self.tree_list.controls.clear()
        for role, outfits in sorted(self.hierarchy.items()):
            self.tree_list.controls.append(self._role_tile(role, outfits))
        self.tree_list.visible = True
        self.empty_hint.visible = False
        self.page.update()

    def _role_tile(self, role, outfits):
        t = self.t
        state = {"open": False}
        count = sum(len(v) for v in outfits.values())
        chevron = ft.Icon(ft.Icons.CHEVRON_RIGHT, size=16, color=t["text3"])

        body = ft.Column(spacing=0, visible=False)
        for outfit, infos in sorted(outfits.items()):
            body.controls.append(self._outfit_tile(outfit, infos))

        def toggle(e):
            state["open"] = not state["open"]
            body.visible = state["open"]
            chevron.name = ft.Icons.EXPAND_MORE if state["open"] else ft.Icons.CHEVRON_RIGHT
            self.page.update()

        header = ft.Container(
            content=ft.Row(
                [
                    ft.Icon(ft.Icons.PERSON, size=16, color=t["accent"]),
                    ft.Text(role, size=13, weight=ft.FontWeight.W_600, color=t["text"], expand=True,
                            max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                    ft.Container(
                        content=ft.Text(str(count), size=10, color=t["text3"]),
                        bgcolor=t["surface3"], border_radius=8,
                        padding=ft.Padding.symmetric(horizontal=6, vertical=1),
                    ),
                    chevron,
                ],
                spacing=8,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            padding=ft.Padding.symmetric(horizontal=10, vertical=8),
            border_radius=10,
            on_click=toggle,
        )
        return ft.Column([header, body], spacing=0)

    def _outfit_tile(self, outfit, infos):
        t = self.t
        state = {"open": False}
        chevron = ft.Icon(ft.Icons.CHEVRON_RIGHT, size=14, color=t["text3"])

        body = ft.Column(spacing=0, visible=False)
        for info in sorted(infos, key=lambda x: x["filename"]):
            if info["filename"].endswith("_表情"):
                continue
            parts = info["filename"].split("_")
            name = parts[4] if len(parts) >= 5 else info["filename"]
            body.controls.append(self._action_tile(name, info))

        def toggle(e):
            state["open"] = not state["open"]
            body.visible = state["open"]
            chevron.name = ft.Icons.EXPAND_MORE if state["open"] else ft.Icons.CHEVRON_RIGHT
            self.page.update()

        header = ft.Container(
            content=ft.Row(
                [
                    ft.Icon(ft.Icons.FOLDER, size=14, color=t["accent2"]),
                    ft.Text(outfit, size=12, color=t["text"], expand=True,
                            max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                    chevron,
                ],
                spacing=8,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            padding=ft.Padding.symmetric(horizontal=20, vertical=6),
            border_radius=8,
            on_click=toggle,
        )
        return ft.Column([header, body], spacing=0)

    def _action_tile(self, name, info):
        t = self.t
        selected = info["filename"] == self.selected_filename

        c = ft.Container(
            content=ft.Row(
                [
                    ft.Icon(ft.Icons.IMAGE_OUTLINED, size=13, color=t["accent"] if selected else t["text3"]),
                    ft.Text(name, size=12, color=t["text"] if selected else t["text2"], expand=True,
                            max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                ],
                spacing=8,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            padding=ft.Padding.symmetric(horizontal=20, vertical=5),
            border_radius=8,
            bgcolor=t["accent_bg"] if selected else None,
            on_click=lambda e: self._select_action(info, c),
        )
        return c

    def _select_action(self, info, ctrl):
        if self.selected_action_ctrl is not None:
            self.selected_action_ctrl.bgcolor = None
            self.selected_action_ctrl.update()
        self.selected_action_ctrl = ctrl
        self.selected_filename = info["filename"]
        ctrl.bgcolor = self.t["accent_bg"]
        ctrl.update()
        self._load_base(info)

    # ── Base image ──────────────────────────────────────────

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
        img = self.base_imgs[self.base_idx]
        self.base_image.src = _img_bytes(img)
        self.base_hint.visible = False
        n = len(self.base_imgs)
        self.frame_label.value = f"{self.base_idx + 1}/{n}" if n > 1 else ""
        self.frame_nav_holder.visible = n > 1
        self.prev_btn.disabled = self.base_idx <= 0
        self.next_btn.disabled = self.base_idx >= n - 1
        self.prev_btn.opacity = 0.4 if self.prev_btn.disabled else 1.0
        self.next_btn.opacity = 0.4 if self.next_btn.disabled else 1.0
        self.page.update()

    def _prev_frame(self, e):
        if self.base_idx > 0:
            self.base_idx -= 1
            self._show_base_frame()

    def _next_frame(self, e):
        if self.base_idx < len(self.base_imgs) - 1:
            self.base_idx += 1
            self._show_base_frame()

    # ── Parts ───────────────────────────────────────────────

    def _load_parts(self, info):
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
        self.compose_btn.opacity = 0.4
        self.save_btn.opacity = 0.4
        self.batch_btn.opacity = 0.4

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
        mx = 108
        s = min(1.0, mx / max(w, h))
        thumb = img.resize((max(1, int(w * s)), max(1, int(h * s))), Image.Resampling.LANCZOS)
        data = _img_bytes(thumb)
        selected = idx == self.part_idx

        c = ft.Container(
            content=ft.Stack(
                [
                    ft.Image(src=data, fit=ft.BoxFit.CONTAIN, width=104, height=104),
                    ft.Container(
                        content=ft.Text(str(idx), size=10, color="#fff"),
                        bgcolor="rgba(0,0,0,0.5)",
                        border_radius=6,
                        padding=ft.Padding.symmetric(horizontal=5, vertical=1),
                        alignment=ft.Alignment(-1, -1),
                        margin=ft.Margin.only(left=4, top=4, right=0, bottom=0),
                    ),
                ],
            ),
            width=108,
            height=108,
            bgcolor=t["surface3"],
            border_radius=10,
            border=ft.Border.all(2, t["accent"] if selected else t["border"]),
            padding=2,
            on_click=lambda e, i=idx: self._pick_part(i),
        )
        self.thumb_refs.append((c, idx))
        return c

    def _pick_part(self, idx):
        if not self.part_imgs or idx >= len(self.part_imgs):
            return
        self.part_idx = idx
        for c, i in self.thumb_refs:
            c.border = ft.Border.all(2, self.t["accent"] if i == idx else self.t["border"])
            c.update()
        self._compose()

    def _compose(self, e=None):
        if not self.part_info or not self.selected_info or not self.part_imgs:
            return
        if self.part_idx >= len(self.part_imgs) or not self.base_imgs:
            return
        composed = compose_preview(
            self.base_imgs[0],
            self.part_imgs[self.part_idx],
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

    # ── Save / Export ───────────────────────────────────────

    async def _save_current(self, e):
        if not self.composed_img or not self.selected_info:
            self._snack("请先选择底图并合成", error=True)
            return
        name = f"{self.selected_info['filename']}_diff_{self.part_idx:03d}.png"
        fp = ft.FilePicker()
        self.page.overlay.append(fp)
        self.page.update()
        try:
            result = await fp.save_file(dialog_title="保存当前合成图像", file_name=name)
        finally:
            try:
                self.page.overlay.remove(fp)
            except ValueError:
                pass
        if not result or not result.path:
            return
        try:
            self.composed_img.save(result.path, "PNG")
            self._snack(f"已保存: {Path(result.path).name}")
        except Exception as ex:
            self._snack(f"保存失败: {ex}", error=True)

    async def _batch_export(self, e):
        if not self.part_info or not self.selected_info or not self.part_imgs:
            self._snack("请先选择底图并合成", error=True)
            return
        fp = ft.FilePicker()
        self.page.overlay.append(fp)
        self.page.update()
        try:
            result = await fp.get_directory_path(dialog_title="选择导出目录")
        finally:
            try:
                self.page.overlay.remove(fp)
            except ValueError:
                pass
        if not result or not result.path:
            return
        save_dir = result.path
        self._set_status("正在批量导出…")

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
            self._snack(f"已导出 {n} 张到 {Path(save_dir).name}")
            self._set_status("导出完成")
        except Exception as ex:
            self._snack(f"导出失败: {ex}", error=True)
            self._set_status("导出失败")


def main(page: ft.Page):
    ComposerApp(page)


if __name__ == "__main__":
    ft.run(main)
