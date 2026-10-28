"""导出：保存当前帧与批量导出。"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import math
import os
import sys
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
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
    PROGRESS_PUSH_S,
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

        base = self.base_imgs[0]
        ox = self.part_info.get("offset_x", 0) if self.part_info else 0
        oy = self.part_info.get("offset_y", 0) if self.part_info else 0
        # 无差分模式：不合成，铺平底图全部帧
        parts_data = list(self.part_imgs) if not self.base_only else list(self.base_imgs)
        # 重试要用同一份上下文（输出目录、底图、偏移），所以整份先留在这里
        ctx = {
            "dir": save_dir,
            "fname": self.selected_info["filename"],
            "base": base,
            "ox": ox,
            "oy": oy,
            "base_only": self.base_only,
        }
        self._export_ctx = ctx
        await self._run_export(list(enumerate(parts_data)), ctx)


    async def _run_export(self, jobs, ctx):
        """后台逐张写盘，进度按 PROGRESS_PUSH_S 节流推回 UI 线程。

        jobs 是 [(帧号, PIL 图)]；取消位一置，还没开跑的那几张直接跳过。
        已经写出去的 PNG 一律保留——目标目录里可能本来就有同名文件，删不得。
        """
        gen = self._export_begin(len(jobs))
        cancel = self._export_cancel
        loop = self._loop
        pushed = [0.0]

        def report(done, total):
            """导出线程调用：只把最新一帧丢回 UI 线程，自己一个控件都不碰。"""
            now = time.monotonic()
            if done != total and now - pushed[0] < PROGRESS_PUSH_S:
                return
            pushed[0] = now
            if loop is None:
                return
            try:
                loop.call_soon_threadsafe(self._show_export_progress, gen, done, total)
            except Exception:
                log.debug("忽略异常（report）", exc_info=True)

        def one(item):
            idx, part = item
            if cancel.is_set():
                return None
            suffix = "base" if ctx["base_only"] else "diff"
            out = Path(ctx["dir"]) / f"{ctx['fname']}_{suffix}_{idx:03d}.png"
            try:
                composed = part if ctx["base_only"] else compose_preview(
                    ctx["base"], part, ctx["ox"], ctx["oy"])
                composed.save(out, "PNG")
                return True, idx, part, out.name, ""
            except Exception as ex:
                log.debug("导出单帧失败", exc_info=True)
                return False, idx, part, out.name, f"{type(ex).__name__}: {ex}"[:120]

        def work():
            saved, failures, cancelled = 0, [], False
            total = len(jobs)
            done = 0
            with ThreadPoolExecutor(max_workers=min(8, (os.cpu_count() or 4))) as ex:
                futures = [ex.submit(one, job) for job in jobs]
                for fut in as_completed(futures):
                    try:
                        res = fut.result()
                    except Exception:
                        # one() 自己兜了写入异常，这里只是兜底，别让一张拖垮整批
                        log.debug("导出任务异常", exc_info=True)
                        res = None
                    if res is None:
                        cancelled = True
                        continue
                    done += 1
                    if res[0]:
                        saved += 1
                    else:
                        failures.append(res[1:])
                    report(done, total)
            return saved, failures, cancelled

        try:
            saved, failures, cancelled = await asyncio.to_thread(work)
        except Exception as ex:
            log.exception("批量导出失败")
            self._finish_export(gen, 0, [], False, ctx["dir"], error=str(ex))
            return
        self._finish_export(gen, saved, failures, cancelled, ctx["dir"])


    def _export_begin(self, total):
        """导出开局：换一代号（上一代迟到的回调就此作废）、亮进度条与「取消」。"""
        self._export_gen += 1
        gen = self._export_gen
        self._export_cancel = threading.Event()
        self._export_failed = []
        self._export_shown = True
        if self.status_fail_action is not None:
            self.status_fail_action.visible = False
        if self.export_cancel_btn is not None:
            self.export_cancel_btn.visible = True
        self.status_text.value = "正在批量导出…"
        self.status_meta.value = f"导出 0/{total}" if total else "导出"
        self.parse_progress.value = 0.0 if total else None
        self.parse_progress.visible = True
        self.page.update()
        return gen


    def _show_export_progress(self, gen, done, total):
        """UI 线程：把导出线程报来的一帧画进状态栏。"""
        if gen != self._export_gen or not self._export_shown:
            return
        self.parse_progress.value = (done / total) if total else None
        self.status_meta.value = f"导出 {done}/{total}"
        self.status_meta.update()
        self.parse_progress.update()


    def _cancel_export(self, e=None):
        """取消按钮：只置位，让后台在下一张之前停下；磁盘上已写出的不碰。"""
        if self._export_cancel is None or self._export_cancel.is_set():
            return None
        self._export_cancel.set()
        self.status_text.value = "正在取消导出…"
        self.page.update()
        return None


    def _abort_export(self):
        """换资源 / 载出：让在途导出静默作废（不写状态栏，状态留给调用方）。"""
        self._export_gen += 1
        if self._export_cancel is not None:
            self._export_cancel.set()
            self._export_cancel = None
        self._export_shown = False
        if self.export_cancel_btn is not None and self.export_cancel_btn.visible:
            self.export_cancel_btn.visible = False
        if self.status_fail_action is not None and self.status_fail_action.visible:
            self.status_fail_action.visible = False
        if self.parse_progress.visible:
            self.parse_progress.visible = False
            self.parse_progress.value = None


    def _finish_export(self, gen, saved, failures, cancelled, save_dir, error=None):
        """三个收尾路口（成功 / 失败 / 取消）共用：进度条与「取消」一律收起。"""
        if gen != self._export_gen:
            # 这一代已经作废（换资源、或又开了一次导出），不许再写状态栏
            return
        self._export_gen += 1
        self._export_cancel = None
        self._export_shown = False
        self._export_failed = list(failures)
        if self.export_cancel_btn is not None and self.export_cancel_btn.visible:
            self.export_cancel_btn.visible = False
        if self.parse_progress.visible:
            self.parse_progress.visible = False
            self.parse_progress.value = None
        self.page.update()
        if error is not None:
            self._snack(f"导出失败: {error}", error=True)
            self._set_status_meta()
            return
        if cancelled:
            self._set_status(f"已取消导出，已写出 {saved} 张")
            self._set_status_meta()
            return
        if failures:
            # 失败不吞掉成功的那部分：状态栏报总数，右侧留一个「查看失败项」
            self._done(f"导出完成：{saved} 张成功，{len(failures)} 张失败",
                       folder=save_dir, chime=False)
            if self.status_fail_action is not None:
                self.status_fail_action.visible = True
                self.page.update()
            return
        self._done(f"已导出 {saved} 张到 {Path(save_dir).name}", folder=save_dir)


    def _show_export_failures(self, e=None):
        """失败清单：文件名与原因可选中复制，也能只把这批重跑一遍。"""
        items = self._export_failed
        if not items:
            self._done("没有失败项", chime=False)
            return
        ink = self.ink
        content = ft.Column(
            [
                ft.Text(f"{name} — {reason}", size=12, color=ink["text"], selectable=True)
                for _idx, _img, name, reason in items
            ],
            spacing=6,
            scroll=ft.ScrollMode.AUTO,
            width=520,
            height=min(320, 34 + 26 * len(items)),
        )
        text_all = "\n".join(f"{name} — {reason}" for _i, _m, name, reason in items)

        async def copy_all(e, _t=text_all):
            try:
                await self.clipboard.set(_t)
            except Exception:
                log.debug("忽略异常（copy_all）", exc_info=True)

        self.page.show_dialog(
            ft.AlertDialog(
                modal=True,
                bgcolor=ink["panel"],
                title=ft.Text(f"导出失败 {len(items)} 张", weight=ft.FontWeight.W_800,
                              color=ink["text"]),
                content=content,
                actions=[
                    ft.TextButton("复制全部", on_click=copy_all),
                    ft.TextButton("关闭", on_click=lambda e: self.page.pop_dialog()),
                    ft.TextButton("只重试这些", on_click=self._retry_failed),
                ],
                actions_alignment=ft.MainAxisAlignment.END,
            )
        )
        self.page.update()


    async def _retry_failed(self, e=None):
        """只重跑上一轮失败的那几张：成功的不再重复写盘。"""
        jobs = [(idx, img) for idx, img, _name, _reason in self._export_failed]
        ctx = getattr(self, "_export_ctx", None)
        if not jobs or not ctx:
            return
        try:
            self.page.pop_dialog()
        except Exception:
            log.debug("忽略异常（_retry_failed）", exc_info=True)
        await self._run_export(jobs, ctx)

