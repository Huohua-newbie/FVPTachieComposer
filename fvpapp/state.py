"""状态：主题切换、状态栏/提示条、选中与预览状态、键盘快捷键。"""

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



class StateMixin:
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
            self._set_status_meta()
        self.page.update()


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
                log.debug("忽略异常（_later）", exc_info=True)
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
                    log.debug("忽略异常（_snack）", exc_info=True)
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
            log.debug("忽略异常（_snack）", exc_info=True)
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
            log.debug("忽略异常（_dismiss_snack）", exc_info=True)
            pass


    def _remove_snack(self, seq):
        try:
            if seq == self._snack_seq and self._snack_overlay in self.page.overlay:
                self.page.overlay.remove(self._snack_overlay)
                self.page.update()
        except Exception:
            log.debug("忽略异常（_remove_snack）", exc_info=True)
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


    def _restore_preview(self):
        """换主题后按缓存的 PIL 图重建预览：不重新读盘，也不重置帧号/差分选择。"""
        if self.base_imgs:
            self._show_base_frame()
        if self.part_imgs:
            self.thumb_refs = []
            self.thumb_list.controls.clear()
            datas = _part_thumb_srcs(
                self.part_info.get("filename") if self.part_info else None,
                self.part_imgs,
                self._cache_token,
            )
            for idx, data in enumerate(datas):
                self.thumb_list.controls.append(self._make_thumb(data, idx))
            self.part_count.value = f"{len(self.part_imgs)} 帧"
            self.part_hint.visible = False
            self.thumb_list.visible = True
        if self.composed_img is not None and self.part_info:
            ox = self.part_info.get("offset_x", 0)
            oy = self.part_info.get("offset_y", 0)
            self.result_image.src = _cached_src(
                _SRC_CACHE,
                (
                    "composed",
                    self.selected_info.get("filename") if self.selected_info else None,
                    self.part_idx,
                    ox,
                    oy,
                ),
                self.composed_img,
                _img_src,
                _SRC_CACHE_MAX,
            )
            self.result_hint.visible = False
            self.result_meta.value = self._result_meta_text(
                self.composed_img.size, f"偏移 {ox:+d}/{oy:+d}")
            self.save_btn.disabled = False
            self.batch_btn.disabled = False
        elif self.base_only and self.base_imgs:
            # 无差分底图模式：镜像与两按钮由 _show_base_frame 之外单独兜底
            img = self.base_imgs[self.base_idx]
            self.result_image.src = _cached_src(
                _SRC_CACHE,
                ("base", self.selected_filename, self.base_idx),
                img,
                _img_src,
                _SRC_CACHE_MAX,
            )
            self.result_hint.visible = False
            self.result_meta.value = self._result_meta_text(
                img.size, "底图（无表情差分）")
            self.save_btn.disabled = False
            self.batch_btn.disabled = False


    async def _select_action(self, info):
        if self.selected_filename == info.get("filename"):
            return
        self.selected_filename = info["filename"]
        for entry in self._sync_selected_row():
            try:
                entry["hl"].update()
            except Exception:
                log.debug("忽略异常（_sync_selected_row）", exc_info=True)
                pass
        # 解码可能几十毫秒到几百毫秒，丢回事件循环 await，别卡住点击响应
        await self._load_base(info)


    def _pick_part(self, idx):
        if not self.part_imgs or idx >= len(self.part_imgs):
            return
        self.part_idx = idx
        # 只改属性，交给紧随其后的 _compose() 里那次 page.update() 一起推上去，
        # 免得 28 个缩略图各发一次 update、整棵控件树跟着抖。
        for c, i in self.thumb_refs:
            c.border = ft.Border.all(2, self.ink["accent"] if i == idx else self.ink["line_soft"])
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
        self.result_meta.value = self._result_meta_text(composed.size, f"偏移 {ox:+d}/{oy:+d}")
        self.save_btn.disabled = False
        self.batch_btn.disabled = False
        self.page.update()


    def _show_base_frame(self):
        img = self.base_imgs[self.base_idx]
        src = _cached_src(
            _SRC_CACHE,
            ("base", self.selected_filename, self.base_idx),
            img,
            _img_src,
            _SRC_CACHE_MAX,
        )
        self.base_image.src = src
        self.base_hint.visible = False
        n = len(self.base_imgs)
        self.frame_label.value = f"{self.base_idx + 1}/{n}" if n > 1 else ""
        self.frame_nav_holder.visible = n > 1
        self.prev_btn.disabled = self.base_idx <= 0
        self.next_btn.disabled = self.base_idx >= n - 1
        self.base_meta.value = f"{img.size[0]}×{img.size[1]} · {n} 帧"
        if self.base_only:
            # 没有差分时右栏是底图镜像：换帧要跟手，否则导出的和看到的对不上
            self.result_image.src = src
            self.result_meta.value = self._result_meta_text(
                img.size, "底图（无表情差分）")
        self.page.update()


    async def _on_keyboard(self, e):
        key = (getattr(e, "key", None) or "").lower()
        if key == "escape":
            try:
                self.page.pop_dialog()
            except Exception:
                log.debug("忽略异常（_compose）", exc_info=True)
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
