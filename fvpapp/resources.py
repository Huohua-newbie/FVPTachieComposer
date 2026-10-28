"""资源加载：BIN/HZC 解析、载入/载出、缩略图后台解码流水线。"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import math
import os
import sys
import time
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

from . import thumbcache
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
    _make_thumb_widget_from_src,
    _part_thumb_srcs,
    _thumb_bytes,
    reset_image_caches,
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



class ResourceMixin:
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
        # 在途的批量导出写的是旧资源的图，换资源就让它停手，别写进新目录
        self._abort_export()
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
        self._parse_shown = False
        self._later(BUSY_DELAY, self._show_parsing_busy, gen, Path(path).name)

        loop = self._loop
        pushed = [0.0, None]  # 上一次推送的时刻与阶段名

        def report(done, total, stage):
            """解析线程调用：只把最新一帧丢回 UI 线程，自己一个控件都不碰。

            九千条就是九千次回调，逐条刷会把事件循环淹了；所以同阶段内按
            PROGRESS_PUSH_S 节流，换阶段和最后一步立刻推。
            """
            now = time.monotonic()
            if stage == pushed[1] and done != total and now - pushed[0] < PROGRESS_PUSH_S:
                return
            pushed[0] = now
            pushed[1] = stage
            if loop is None:
                return
            try:
                loop.call_soon_threadsafe(self._show_parse_progress, gen, done, total, stage)
            except Exception:
                log.debug("忽略异常（report）", exc_info=True)

        def work():
            if Path(path).is_dir():
                infos = parse_dir_infos(path, progress=report)
            else:
                infos = parse_bin_info_extended(path, progress=report)
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
            self._end_parse_feedback()
            self.status_text.value = prev_status
            self.status_meta.value = prev_meta
            self._snack(f"解析失败: {ex}", error=True)
            self._busy_seq = None
            self.page.update()
            return False
        self._end_parse_feedback()
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
        self._drop_image_caches()
        self._clear_previews()
        # 只在"真的载入了一份新东西"时错峰浮入；换主题走的是默认整棵直出，
        # 不然每切一次深浅色都要把树重播一遍动画。
        self._render_tree(stagger=True)
        self._expand_role_tiles(gen)
        self.file_chip.content.value = Path(path).name
        if self.unload_btn is not None:
            self.unload_btn.visible = True
        # 状态栏一句话说"发生了什么"，右侧 meta 只说"载入了什么规模"，两者不重复。
        self._set_status(f"已加载 {Path(path).name}")
        self._set_status_meta()
        # 只有真的弹过"正在解析"，才补一句结果；快路径全程安静
        if self._snack_is_showing(self._busy_seq):
            self._dismiss_snack(self._busy_seq)
            self._chime()
        self._busy_seq = None
        return True


    async def _unload_bin(self, e=None):
        """载出：卸载当前文件，把应用恢复到刚打开时的初始状态。

        import_dir/export_dir 是对话框的记忆位置，不属于"载入态"，保留；
        其余由 _load_bin 写入的状态全部复位，UI 回到 empty_hint 空状态。
        """
        if not self.input_file:
            self._done("当前没有载入任何文件", chime=False)
            return
        # 作废在途解析与缩略图解码，迟到的回调会因代数不符而自弃
        self._state_gen += 1
        # 导出中途载出：这一批不再往下写，已写出的文件留在原处
        self._abort_export()
        # 解析中途载出：进度条和"正在解析"都不能带进初始态
        self._end_parse_feedback()

        self.input_file = None
        self.file_infos = []
        self.hierarchy = {}
        self.selected_info = None
        self.selected_filename = None
        self.base_idx = 0
        self.part_idx = 0
        self._drop_image_caches()

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
        self._chime()


    def _drop_image_caches(self):
        """换资源 / 载出：丢掉会话内的图缓存，释放上一份资源的整帧。

        三处内存缓存（底图与合成、树里的迷你缩略图、差分条）的 key 里都只有
        文件名，没有"是哪一份资源"，而不同 BIN 出现同名动作太正常了——同一
        角色的两份导出、换皮素材。不清的话新资源点开同名动作，看到的还是上一份。

        磁盘缩略图不在这里清：它的键自带资源指纹，多份资源可以安全共存；
        换资源后仍然能按各自指纹命中。
        """
        self.role_thumb_cache = {}
        thumbcache.clear_decoded()
        self._cache_token = reset_image_caches()


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


    def _capture_loop(self):
        """在 async 入口捕获当前事件循环（纯标准库，不依赖 Flet 版本 API）。"""
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            pass


    def _push_thumb(self, placeholder, widget, cache_key, token):
        """必须在 UI 线程执行：替换占位内容并做控件级精确推送。

        token 在这里对而不是在解码线程对上：解码线程对完还可能排在队列里，
        等它跑回来时资源已经换了——占位控件早不在新树里，缓存也清空了，
        这时候再写进去就是给新资源的同名动作挂上上一份的图。
        """
        if token != self._cache_token:
            return
        try:
            self.role_thumb_cache[cache_key] = widget
            placeholder.content = widget
            # 占位底色是给"还没解码出来"那一下用的：图到了还留着，
            # 半透明的立绘就会透出一个人形圆角块，看着像凭空多了个深色描边。
            placeholder.bgcolor = "#00000000"
            placeholder.update()
        except Exception:
            log.debug("忽略异常（_push_thumb）", exc_info=True)
            pass


    def _queue_thumb(self, placeholder, info, size, cache_key):
        """后台线程先查磁盘，未命中再解码；完成后经事件循环把替换+刷新调度回 UI。

        磁盘键带资源指纹 + 文件名 + 尺寸，所以同名动作不会串图；写盘就在这个
        工作线程里做，不占 UI 线程。
        """
        gen = self._state_gen  # 解码期间若载出/换文件，结果作废
        token = self._cache_token
        input_file = self.input_file

        def deliver(widget):
            if gen != self._state_gen:
                return  # 已载出（或换了文件）：占位控件不再属于当前树，缓存也已清
            loop = getattr(self, "_loop", None)
            if loop is not None:
                try:
                    loop.call_soon_threadsafe(self._push_thumb, placeholder, widget, cache_key, token)
                    return
                except Exception:
                    log.debug("忽略异常（job）", exc_info=True)
                    pass
            self._push_thumb(placeholder, widget, cache_key, token)

        def job():
            key = thumbcache.entry_key(info, size, input_file)
            if key is not None:
                png = thumbcache.load(key)
                if png:
                    try:
                        deliver(_make_thumb_widget_from_src(thumbcache.data_uri(png), size))
                    except Exception:
                        log.debug("忽略异常（job/cache）", exc_info=True)
                    else:
                        return

            try:
                data, header = self._read_hzc_bytes(info)
                img = self._read_first_frame(info, data, header)
            except Exception:
                log.debug("忽略异常（job）", exc_info=True)
                return
            if img is None:
                return
            try:
                png = thumbcache.thumb_png(img, size)
                widget = _make_thumb_widget_from_src(thumbcache.data_uri(png), size)
            except Exception:
                log.debug("忽略异常（job）", exc_info=True)
                return
            if key is not None:
                thumbcache.save(key, png)
            deliver(widget)
        _THUMB_POOL.submit(job)


    def _flush_tasks(self, tasks):
        """展开时在 UI 线程调用：已解码的当场应用，未完成的提交后台任务。"""
        for placeholder, info, size, key in tasks:
            cached = self.role_thumb_cache.get(key)
            if cached is not None:
                try:
                    placeholder.content = cached
                except Exception:
                    log.debug("忽略异常（_flush_tasks）", exc_info=True)
                    pass
            else:
                self._queue_thumb(placeholder, info, size, key)
        tasks.clear()


    def _read_hzc_bytes(self, info):
        """取一条记录的原始字节与解码头：独立 .hzc 直接读，否则从 bin 里按偏移读。"""
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
        return data, header


    def _read_first_frame(self, info, data=None, header=None):
        if data is None:
            data, header = self._read_hzc_bytes(info)
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


    def _read_pil_list(self, info):
        """整帧解码；同一份内容按 SHA-1 复用，来回切动作不再重解整包。"""
        data, header = self._read_hzc_bytes(info)
        key = thumbcache.source_digest(data)
        cached = thumbcache.get_decoded(key)
        if cached is not None:
            return cached
        images = hzc_data_to_pil_list(data, header)
        thumbcache.put_decoded(key, images)
        return images


    async def _load_base(self, info):
        self.selected_info = info
        gen = self._state_gen
        try:
            # 整帧解压/变换在线程里做，UI 线程只负责上屏
            base_imgs = await asyncio.to_thread(self._read_pil_list, info)
        except Exception as ex:
            if gen != self._state_gen:
                return
            self.base_imgs = []
            self._snack(f"读取底图失败: {ex}", error=True)
            return
        if gen != self._state_gen:
            # 期间又切了别的动作，这份结果已经过期，直接丢
            return
        if not base_imgs:
            self._snack("无法解析该 HZC 图像", error=True)
            return
        self.base_imgs = base_imgs
        self.base_idx = 0
        self._reset_zoom()
        self._show_base_frame()
        await self._load_parts(info)


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
                log.debug("忽略异常（_spawn）", exc_info=True)
                pass

        loop.create_task(quiet())


    def _reset_zoom(self, e=None):
        """两个舞台一起复位：底图与合成始终用同一视角，比较时不会一深一浅。"""
        for name in ("base_viewer", "result_viewer"):
            viewer = getattr(self, name, None)
            if viewer is not None:
                self._spawn(viewer.reset())


    def _enter_base_only(self):
        """没有差分可用：按当前底图帧铺出结果面板，让「保存/批量导出」走底图这条退路。"""
        self.base_only = True
        if not self.base_imgs:
            self.page.update()
            return
        img = self.base_imgs[self.base_idx]
        self.result_image.src = _cached_src(
            _SRC_CACHE,
            ("base", self.selected_filename, self.base_idx),
            img,
            _img_src,
            _SRC_CACHE_MAX,
        )
        self.result_hint.visible = False
        self.result_meta.value = self._result_meta_text(img.size, "底图（无表情差分）")
        self.save_btn.disabled = False
        self.batch_btn.disabled = False
        self.page.update()


    async def _load_parts(self, info):
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

        gen = self._state_gen
        try:
            imgs = await asyncio.to_thread(self._read_pil_list, part_info)
        except Exception as ex:
            if gen != self._state_gen:
                return
            self._snack(f"读取部件失败: {ex}", error=True)
            self.page.update()
            return
        if gen != self._state_gen:
            # 期间又切了别的动作，这份结果已经过期
            return
        if not imgs:
            self.part_hint_text.value = PART_HINT_EMPTY
            self._enter_base_only()
            return

        # 缩略图缩放 + PNG/base64 编码也丢线程里：几十帧在 UI 线程上做会明显掉帧
        datas = await asyncio.to_thread(_part_thumb_srcs, part_info.get("filename"), imgs, self._cache_token)
        if gen != self._state_gen:
            return

        self.part_info = part_info
        self.part_imgs = imgs
        self.part_count.value = f"{len(imgs)} 帧"
        for idx, data in enumerate(datas):
            self.thumb_list.controls.append(self._make_thumb(data, idx))
        self.part_hint.visible = False
        self.thumb_list.visible = True
        self.page.update()
        self._pick_part(0)


    def _make_thumb(self, data, idx):
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


    def _chime(self):
        """一次柔和的完成提示音。非 Windows、测试环境或 FVP_SOUND=0 时静默。"""
        if sys.platform != "win32":
            return
        # 测试挂的是假 Page（普通类，不是 flet.Page），别在跑测试时叮一下
        if not type(self.page).__module__.startswith("flet"):
            return
        if os.environ.get("FVP_SOUND", "").strip() == "0":
            return
        try:
            import winsound

            winsound.MessageBeep(winsound.MB_ICONASTERISK)
        except Exception:
            log.debug("提示音播放失败", exc_info=True)


    def _done(self, msg, folder=None, chime=True):
        """成功类反馈走状态栏：不占浮层、不挡内容，完成时配一声柔和提示音。

        folder 非空时，状态栏右侧短暂出现「打开文件夹」，6 秒后自动收起。
        """
        self._set_status(msg)
        self._status_folder = folder
        self._status_gen += 1
        gen = self._status_gen
        if self.status_action is not None:
            self.status_action.visible = folder is not None
            self.page.update()
        if folder is not None:
            self._later(6.0, self._hide_status_folder, gen)
        if chime:
            self._chime()


    def _open_status_folder(self, e=None):
        """状态栏「打开文件夹」：打开最近一次保存/导出的目录。"""
        if self._status_folder:
            self._open_folder(self._status_folder)
        return None


    def _hide_status_folder(self, gen=None):
        if gen is not None and gen != self._status_gen:
            return
        if self.status_action is not None and self.status_action.visible:
            self.status_action.visible = False
            self.page.update()


    def _show_parsing_busy(self, gen, name):
        """解析超过 BUSY_DELAY 才走到这里；已经解析完就不弹任何东西。"""
        if gen != self._parsing_gen:
            return
        self._busy_seq = self._snack(f"正在解析 {name}…", busy=True)
        # 进度条跟这条提示同一时刻亮：小文件在亮之前就解析完了，
        # 快路径全程安静，不该闪一下进度条再收回去。
        self._parse_shown = True
        self.parse_progress.value = None
        self.parse_progress.visible = True
        self.page.update()


    def _show_parse_progress(self, gen, done, total, stage):
        """UI 线程：把解析线程报来的一帧画进状态栏。

        total 为 0 表示"还不知道总共多少"（目录还在数文件），进度条走不定态，
        文案只报已数到的个数，别写成 0/0。
        """
        if gen != self._parsing_gen or not self._parse_shown:
            return
        self.parse_progress.value = (done / total) if total else None
        self.status_meta.value = f"{stage} {done}/{total}" if total else f"{stage} {done} 个"
        self.status_meta.update()
        self.parse_progress.update()


    def _end_parse_feedback(self):
        """解析收尾（成功、失败、被下一次载入接管都算）：反馈全部收起。

        进度条长在常驻的状态栏上，收起这件事不能指望下一次 update 顺手做——
        被接管那条路根本不会再走回这里刷新。
        """
        self._parsing_gen = None
        self._parse_shown = False
        if self.parse_progress.visible:
            self.parse_progress.visible = False
            self.parse_progress.value = None
            self.page.update()
