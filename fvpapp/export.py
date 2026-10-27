"""导出：保存当前帧与批量导出。"""

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



class ExportMixin:
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
            self._done(f"已保存 {Path(result).name}", folder=str(Path(result).parent))
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
                log.debug("忽略异常（one）", exc_info=True)
                return False

        def work():
            with ThreadPoolExecutor(max_workers=min(8, (os.cpu_count() or 4))) as ex:
                oks = list(ex.map(one, enumerate(parts_data)))
            saved = sum(1 for ok in oks if ok)
            return saved, len(oks) - saved

        try:
            saved, errors = await asyncio.to_thread(work)
            if errors:
                self._snack(f"导出完成: {saved} 成功, {errors} 失败", error=True, folder=save_dir)
                self._set_status(f"导出完成: {saved} 成功, {errors} 失败")
            else:
                self._done(f"已导出 {saved} 张到 {Path(save_dir).name}", folder=save_dir)
        except Exception as ex:
            self._snack(f"导出失败: {ex}", error=True)
            self._set_status("导出失败")
