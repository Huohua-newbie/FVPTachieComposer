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
    "1. 点击「打开 BIN」选择 .bin 文件\n"
    "2. 左侧按 角色 → 服装 → 动作 展开\n"
    "3. 点击动作预览底图，中间自动加载差分部件\n"
    "4. 点击缩略图，右侧生成合成预览\n"
    "5. 保存当前图 / 批量合成并导出"
)


class ComposerApp:
    def __init__(self, page: ft.Page):
        self.page = page
        self.is_dark = False

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
        self.role_thumb_cache = {}

        self.file_picker = ft.FilePicker()
        self.page.services = [self.file_picker]
        self._setup_page()
        self._build()

    def _setup_page(self):
        p = self.page
        p.title = "FVP Tachie Composer"
        p.window.width = 1440
        p.window.height = 880
        p.window.min_width = 1080
        p.window.min_height = 640
        p.window.title_bar_hidden = True
        p.padding = 0
        p.spacing = 0
        self._apply_theme()

    def _apply_theme(self):
        p = self.page
        sb = ft.ScrollbarTheme(thumb_visibility=True, track_visibility=False)
        if self.is_dark:
            p.theme = ft.Theme(color_scheme_seed="#5b9bf5", use_material3=True, scrollbar_theme=sb)
            p.dark_theme = ft.Theme(color_scheme_seed="#5b9bf5", use_material3=True, scrollbar_theme=sb)
            p.theme_mode = ft.ThemeMode.DARK
            p.bgcolor = ft.Colors.SURFACE_CONTAINER_LOWEST
        else:
            p.theme = ft.Theme(color_scheme_seed="#3b82f6", use_material3=True, scrollbar_theme=sb)
            p.dark_theme = ft.Theme(color_scheme_seed="#3b82f6", use_material3=True, scrollbar_theme=sb)
            p.theme_mode = ft.ThemeMode.LIGHT
            p.bgcolor = ft.Colors.SURFACE_CONTAINER_LOWEST

    def _build(self):
        self._apply_theme()
        p = self.page

        self.title_bar = self._build_title_bar()
        self.tree_col = self._build_tree_column()
        self.base_col = self._build_base_column()
        self.result_col = self._build_result_column()
        self.status_text = ft.Text("就绪", size=11)

        status_bar = ft.Container(
            content=ft.Row([self.status_text]),
            height=32,
            padding=ft.Padding.symmetric(horizontal=16),
            border=ft.Border.only(top=ft.BorderSide(1, ft.Colors.OUTLINE_VARIANT)),
        )

        body = ft.Row(
            [
                ft.Container(self.tree_col, width=290),
                ft.Container(self.base_col, expand=True),
                ft.Container(self.result_col, width=360),
            ],
            expand=True,
            spacing=0,
        )

        p.add(ft.Column([self.title_bar, body, status_bar], spacing=0, expand=True))
        p.update()

    # ── Title bar ───────────────────────────────────────────

    def _build_title_bar(self):
        logo = ft.Row(
            [
                ft.Icon(ft.Icons.IMAGE, size=20, color=ft.Colors.PRIMARY),
                ft.Text("FVP Tachie Composer", size=14, weight=ft.FontWeight.W_700),
                ft.Text("立绘查看与合成", size=12, color=ft.Colors.ON_SURFACE_VARIANT),
            ],
            spacing=8,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

        right = ft.Row(
            [
                ft.IconButton(ft.Icons.HELP_OUTLINE, on_click=self._help, tooltip="使用说明", icon_size=18),
                ft.IconButton(
                    ft.Icons.DARK_MODE if self.is_dark else ft.Icons.LIGHT_MODE,
                    on_click=self._toggle_theme, tooltip="切换主题", icon_size=18,
                ),
                ft.IconButton(ft.Icons.MINIMIZE, on_click=self._minimize, tooltip="最小化", icon_size=18),
                ft.IconButton(ft.Icons.CROP_SQUARE, on_click=self._toggle_maximize, tooltip="最大化", icon_size=18),
                ft.IconButton(ft.Icons.CLOSE, on_click=self._close, tooltip="关闭", icon_size=18),
            ],
            spacing=0,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

        bar = ft.Container(
            content=ft.Row([logo, ft.Container(expand=True), right], vertical_alignment=ft.CrossAxisAlignment.CENTER),
            height=48,
            padding=ft.Padding.symmetric(horizontal=12),
            border=ft.Border.only(bottom=ft.BorderSide(1, ft.Colors.OUTLINE_VARIANT)),
        )
        return ft.WindowDragArea(bar, maximizable=True)

    # ── Left: tree ──────────────────────────────────────────

    def _build_tree_column(self):
        header = ft.Row(
            [
                ft.Icon(ft.Icons.PEOPLE, size=18, color=ft.Colors.PRIMARY),
                ft.Text("角色库", size=14, weight=ft.FontWeight.W_600, expand=True),
                ft.IconButton(ft.Icons.FOLDER_OPEN, on_click=self._open_bin, tooltip="打开 BIN", icon_size=20),
            ],
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

        self.empty_hint = ft.Container(
            content=ft.Column(
                [
                    ft.Icon(ft.Icons.FOLDER_OPEN, size=48, color=ft.Colors.OUTLINE),
                    ft.Text("尚未加载文件", size=13, color=ft.Colors.ON_SURFACE_VARIANT),
                    ft.Text("点击上方文件夹图标", size=11, color=ft.Colors.ON_SURFACE_VARIANT),
                ],
                spacing=8,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            alignment=ft.Alignment.CENTER,
            expand=True,
        )

        self.tree_list = ft.ListView(spacing=0, expand=True, scroll=ft.ScrollMode.AUTO)
        self.tree_list.visible = False

        body = ft.Stack([self.empty_hint, self.tree_list], expand=True, fit=ft.StackFit.EXPAND)

        return ft.Card(
            content=ft.Container(
                content=ft.Column([header, body], spacing=0, expand=True),
                padding=12,
                expand=True,
            ),
            expand=True,
            margin=ft.Margin.only(left=10, top=8, right=4, bottom=8),
        )

    # ── Center: base preview + parts ────────────────────────

    def _build_base_column(self):
        self.base_hint = ft.Container(
            content=ft.Column(
                [
                    ft.Icon(ft.Icons.IMAGE_OUTLINED, size=56, color=ft.Colors.OUTLINE),
                    ft.Text("在左侧选择底图", size=14, color=ft.Colors.ON_SURFACE_VARIANT),
                ],
                spacing=10,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            alignment=ft.Alignment.CENTER,
            expand=True,
        )

        self.base_image = ft.Image(src=TRANSPARENT, fit=ft.BoxFit.CONTAIN, expand=True)

        base_area = ft.Stack(
            [self.base_hint, self.base_image],
            expand=True,
            fit=ft.StackFit.EXPAND,
        )

        self.frame_label = ft.Text("", size=12)
        self.prev_btn = ft.IconButton(ft.Icons.CHEVRON_LEFT, on_click=self._prev_frame, icon_size=20)
        self.next_btn = ft.IconButton(ft.Icons.CHEVRON_RIGHT, on_click=self._next_frame, icon_size=20)
        frame_nav = ft.Row(
            [self.prev_btn, self.frame_label, self.next_btn],
            alignment=ft.MainAxisAlignment.CENTER,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )
        self.frame_nav_holder = ft.Container(frame_nav, visible=False)

        self.part_count = ft.Text("", size=11, color=ft.Colors.ON_SURFACE_VARIANT)
        part_header = ft.Row(
            [
                ft.Icon(ft.Icons.TUNE, size=16, color=ft.Colors.PRIMARY),
                ft.Text("差分部件", size=14, weight=ft.FontWeight.W_600, expand=True),
                self.part_count,
            ],
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

        self.part_hint = ft.Container(
            content=ft.Column(
                [
                    ft.Icon(ft.Icons.TUNE, size=36, color=ft.Colors.OUTLINE),
                    ft.Text("选择底图后显示差分帧", size=12, color=ft.Colors.ON_SURFACE_VARIANT),
                ],
                spacing=8,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            alignment=ft.Alignment.CENTER,
            expand=True,
        )

        self.thumb_list = ft.ListView(
            spacing=8, horizontal=True, height=140,
            auto_scroll=False, scroll=ft.ScrollMode.AUTO,
        )
        self.thumb_list.visible = False

        part_area = ft.Stack(
            [self.part_hint, self.thumb_list],
            expand=True,
            fit=ft.StackFit.EXPAND,
        )

        top = ft.Column(
            [
                ft.Row(
                    [
                        ft.Icon(ft.Icons.IMAGE_OUTLINED, size=16, color=ft.Colors.PRIMARY),
                        ft.Text("底图预览", size=14, weight=ft.FontWeight.W_600),
                    ],
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                base_area,
                self.frame_nav_holder,
            ],
            spacing=6,
            expand=True,
        )

        bottom = ft.Column([part_header, part_area], spacing=6, height=148)

        return ft.Card(
            content=ft.Container(
                content=ft.Column([top, bottom], spacing=10, expand=True),
                padding=12,
                expand=True,
            ),
            expand=True,
            margin=ft.Margin.only(left=4, top=8, right=4, bottom=8),
        )

    # ── Right: result ───────────────────────────────────────

    def _build_result_column(self):
        self.result_hint = ft.Container(
            content=ft.Column(
                [
                    ft.Icon(ft.Icons.PALETTE_OUTLINED, size=40, color=ft.Colors.OUTLINE),
                    ft.Text("点击部件帧后显示合成结果", size=12, color=ft.Colors.ON_SURFACE_VARIANT),
                ],
                spacing=8,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            alignment=ft.Alignment.CENTER,
            expand=True,
        )

        self.result_image = ft.Image(src=TRANSPARENT, fit=ft.BoxFit.CONTAIN, expand=True)

        result_area = ft.Stack(
            [self.result_hint, self.result_image],
            expand=True,
            fit=ft.StackFit.EXPAND,
        )

        self.save_btn = ft.IconButton(ft.Icons.SAVE_ALT, on_click=self._save_current, tooltip="保存当前图",
                                      icon_size=22, disabled=True)
        self.batch_btn = ft.IconButton(ft.Icons.DOWNLOAD, on_click=self._batch_export, tooltip="批量导出",
                                       icon_size=22, disabled=True)

        btn_bar = ft.Row(
            [self.save_btn, self.batch_btn],
            alignment=ft.MainAxisAlignment.CENTER,
            spacing=16,
        )

        return ft.Card(
            content=ft.Container(
                content=ft.Column(
                    [
                        ft.Row(
                            [
                                ft.Icon(ft.Icons.PALETTE_OUTLINED, size=16, color=ft.Colors.PRIMARY),
                                ft.Text("合成结果", size=14, weight=ft.FontWeight.W_600),
                            ],
                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        ),
                        result_area,
                        btn_bar,
                    ],
                    spacing=6,
                    expand=True,
                ),
                padding=12,
                expand=True,
            ),
            expand=True,
            margin=ft.Margin.only(left=4, top=8, right=10, bottom=8),
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

    def _snack(self, msg, error=False):
        self.page.open(
            ft.SnackBar(
                ft.Text(msg, size=12),
                bgcolor=ft.Colors.ERROR_CONTAINER if error else ft.Colors.PRIMARY_CONTAINER,
            )
        )

    def _set_status(self, text):
        self.status_text.value = text
        self.page.update()

    # ── Open BIN ────────────────────────────────────────────

    async def _open_bin(self, e):
        try:
            result = await self.file_picker.pick_files(
                dialog_title="选择 BIN 文件",
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
        self.thumb_refs = []
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
        self.save_btn.disabled = True
        self.batch_btn.disabled = True

    # ── Tree ────────────────────────────────────────────────

    def _render_tree(self):
        self.tree_list.controls.clear()
        for role, outfits in sorted(self.hierarchy.items()):
            self.tree_list.controls.append(self._role_tile(role, outfits))
        self.tree_list.visible = True
        self.empty_hint.visible = False
        self.page.update()

    def _role_tile(self, role, outfits):
        state = {"open": False}
        count = sum(len(v) for v in outfits.values())
        chevron = ft.Icon(ft.Icons.CHEVRON_RIGHT, size=16, color=ft.Colors.ON_SURFACE_VARIANT)

        body = ft.Column(spacing=0, visible=False)
        for outfit, infos in sorted(outfits.items()):
            body.controls.append(self._outfit_tile(outfit, infos))

        def toggle(e):
            state["open"] = not state["open"]
            body.visible = state["open"]
            chevron.name = ft.Icons.EXPAND_MORE if state["open"] else ft.Icons.CHEVRON_RIGHT
            self.page.update()

        thumb_widget = self._get_role_thumbnail(role, outfits)

        header = ft.ListTile(
            leading=thumb_widget,
            title=ft.Text(role, size=13, weight=ft.FontWeight.W_600,
                          max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
            trailing=chevron,
            on_click=toggle,
            content_padding=ft.Padding.symmetric(horizontal=4, vertical=0),
            dense=True,
        )
        return ft.Column([header, body], spacing=0)

    def _get_role_thumbnail(self, role, outfits):
        if role in self.role_thumb_cache:
            return self.role_thumb_cache[role]
        for outfit, infos in sorted(outfits.items()):
            for info in infos:
                if info["filename"].endswith("_表情"):
                    continue
                try:
                    imgs = self._read_pil_list(info)
                    if imgs:
                        img = imgs[0]
                        widget = _make_thumb_widget(img, 36)
                        self.role_thumb_cache[role] = widget
                        return widget
                except Exception:
                    break
                break
            break
        widget = ft.Icon(ft.Icons.PERSON, color=ft.Colors.PRIMARY)
        self.role_thumb_cache[role] = widget
        return widget

    def _make_mini_thumb(self, info, size=24):
        key = f"mini_{info['filename']}_{size}"
        if key in self.role_thumb_cache:
            return self.role_thumb_cache[key]
        try:
            imgs = self._read_pil_list(info)
            if imgs:
                widget = _make_thumb_widget(imgs[0], size)
                self.role_thumb_cache[key] = widget
                return widget
        except Exception:
            pass
        fallback = ft.Icon(ft.Icons.IMAGE_OUTLINED, size=14, color=ft.Colors.ON_SURFACE_VARIANT)
        self.role_thumb_cache[key] = fallback
        return fallback

    def _outfit_tile(self, outfit, infos):
        state = {"open": False}
        chevron = ft.Icon(ft.Icons.CHEVRON_RIGHT, size=14, color=ft.Colors.ON_SURFACE_VARIANT)

        body = ft.Column(spacing=0, visible=False)
        for info in sorted(infos, key=lambda x: x["filename"]):
            if info["filename"].endswith("_表情"):
                continue
            parts = info["filename"].split("_")
            name = parts[2] if parts[0] == "CHR" and len(parts) >= 3 else (
                parts[4] if len(parts) >= 5 else info["filename"])
            body.controls.append(self._action_tile(name, info))

        def toggle(e):
            state["open"] = not state["open"]
            body.visible = state["open"]
            chevron.name = ft.Icons.EXPAND_MORE if state["open"] else ft.Icons.CHEVRON_RIGHT
            self.page.update()

        thumb_info = next((i for i in infos if not i["filename"].endswith("_表情")), None)
        thumb_widget = self._make_mini_thumb(thumb_info, 30) if thumb_info else ft.Icon(ft.Icons.FOLDER, size=18, color=ft.Colors.SECONDARY)

        header = ft.ListTile(
            leading=thumb_widget,
            title=ft.Text(outfit, size=12,
                          max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
            trailing=chevron,
            on_click=toggle,
            content_padding=ft.Padding.only(left=32, right=4, top=0, bottom=0),
            dense=True,
        )
        return ft.Column([header, body], spacing=0)

    def _action_tile(self, name, info):
        selected = info["filename"] == self.selected_filename
        thumb_widget = self._make_mini_thumb(info, 26)

        c = ft.ListTile(
            leading=thumb_widget,
            title=ft.Text(name, size=12,
                          color=ft.Colors.ON_SURFACE if selected else ft.Colors.ON_SURFACE_VARIANT,
                          max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
            on_click=lambda e: self._select_action(info, c),
            content_padding=ft.Padding.only(left=52, right=4, top=0, bottom=0),
            dense=True,
            selected=selected,
        )
        return c

    def _select_action(self, info, ctrl):
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
        self.save_btn.disabled = True
        self.batch_btn.disabled = True

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
        data = _thumb_bytes(img, 100)
        selected = idx == self.part_idx

        c = ft.Container(
            content=ft.Image(src=data, fit=ft.BoxFit.CONTAIN, width=100, height=100),
            width=104,
            height=104,
            bgcolor=ft.Colors.TRANSPARENT,
            border=ft.Border.all(2, ft.Colors.PRIMARY if selected else ft.Colors.TRANSPARENT),
            border_radius=10,
            on_click=lambda e, i=idx: self._pick_part(i),
        )
        self.thumb_refs.append((c, idx))
        return c

    def _pick_part(self, idx):
        if not self.part_imgs or idx >= len(self.part_imgs):
            return
        self.part_idx = idx
        for c, i in self.thumb_refs:
            c.border = ft.Border.all(2, ft.Colors.PRIMARY if i == idx else ft.Colors.TRANSPARENT)
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
        self.page.update()

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
            self._snack(f"已保存: {Path(result).name}")
        except Exception as ex:
            self._snack(f"保存失败: {ex}", error=True)

    async def _batch_export(self, e):
        if not self.part_info or not self.selected_info or not self.part_imgs:
            self._snack("请先选择底图并合成", error=True)
            return
        try:
            result = await self.file_picker.get_directory_path(dialog_title="选择导出目录")
        except Exception:
            return
        if not result:
            return
        save_dir = result
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


def _img_bytes(img, fmt="PNG"):
    buf = io.BytesIO()
    img.save(buf, fmt)
    return buf.getvalue()


def _thumb_bytes(img, size=100):
    w, h = img.size
    s = min(1.0, size / max(w, h))
    thumb = img.resize((max(1, int(w * s)), max(1, int(h * s))), Image.Resampling.NEAREST)
    if thumb.mode == "RGBA":
        thumb = thumb.convert("RGB")
    buf = io.BytesIO()
    thumb.save(buf, "JPEG", quality=70)
    return buf.getvalue()


def _make_thumb_widget(img, size=36):
    data = _thumb_bytes(img, size)
    return ft.Image(src=data, width=size, height=size, fit=ft.BoxFit.CONTAIN)


def main(page: ft.Page):
    try:
        ComposerApp(page)
    except Exception as ex:
        page.add(ft.Text(f"初始化失败: {ex}", color=ft.Colors.ERROR))
        page.update()


if __name__ == "__main__":
    ft.run(main)
