"""无头回归：批量导出的进度、取消、失败清单与「只重试失败项」。

轻量假 flet 顶掉界面，PIL 用真的（要真的写 PNG），直接驱动 ExportMixin。
每个用例独立临时目录；等待一律用谓词轮询，不用固定 sleep：

    python tests/test_export_progress.py -v
    cd tests && python -m unittest test_export_progress.ExportCancelTest -v
"""

from __future__ import annotations

import threading
import unittest
from pathlib import Path
from unittest import mock

try:
    from . import support
except ImportError:  # 直接 python tests/test_export_progress.py
    import support

support.install_fake_flet()

from PIL import Image  # noqa: E402

import fvpapp.export as export_mod  # noqa: E402
from FVPTachieComposerFlet import ComposerApp  # noqa: E402

# 节流在真机上是为了别把事件循环淹了；测试里要看到每一帧，关掉。
export_mod.PROGRESS_PUSH_S = 0.0

FILENAME = "CHR_T_基_私服"


class ExportTestBase(support.AsyncEnvTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.out = self.tmp / "out"
        self.out.mkdir()

    def make_app(self, frames: int = 12, base_only: bool = False, target=None):
        page = support.FakePage()
        app = ComposerApp(page)
        app._chime = lambda: None  # 测试里别真响
        app.selected_info = {"filename": FILENAME}
        app.selected_filename = FILENAME
        images = [Image.new("RGBA", (8, 8), (i * 7 % 256, 40, 90, 255))
                  for i in range(frames)]
        app.base_imgs = list(images) if base_only else [Image.new("RGBA", (8, 8), (10, 20, 30, 255))]
        app.part_imgs = images
        app.part_info = {"offset_x": 0, "offset_y": 0}
        app.base_only = base_only
        app.export_dir = None

        directory = target if target is not None else self.out

        async def pick(**_kwargs):
            return str(directory)

        app.file_picker.get_directory_path = pick
        app._loop = __import__("asyncio").get_running_loop()
        return app

    @staticmethod
    def spy_progress(app, sink, on_frame=None):
        def spy(gen, done, total):
            sink.append((gen, done, total))
            app._orig_show_export_progress(gen, done, total)
            if on_frame is not None:
                on_frame(done, total)

        app._orig_show_export_progress = app._show_export_progress
        app._show_export_progress = spy
        return spy

    def png_files(self, folder: Path):
        return sorted(p.name for p in folder.glob("*.png") if p.is_file())

    async def wait_status(self, app, needle: str, timeout: float = 5.0):
        await support.pump_until(lambda: needle in app.status_text.value,
                                 timeout=timeout, what=f"状态栏出现 {needle!r}")

    async def wait_progress_full(self, frames, total: int, timeout: float = 5.0):
        await support.pump_until(
            lambda: bool(frames) and frames[-1][1:] == (total, total),
            timeout=timeout, what=f"进度报满 {total}")


class ExportProgressTest(ExportTestBase):
    async def test_writes_all_frames_and_reports_full_progress(self):
        app = self.make_app(12)
        frames = []
        self.spy_progress(app, frames)

        await app._batch_export(None)
        await self.wait_progress_full(frames, 12)

        names = self.png_files(self.out)
        self.assertEqual(len(names), 12)
        self.assertEqual(names, [f"{FILENAME}_diff_{i:03d}.png" for i in range(12)])
        self.assertEqual([f[1] for f in frames], list(range(1, 13)))
        self.assertTrue(all(b[1] >= a[1] for a, b in zip(frames, frames[1:])))
        self.assertEqual(frames[-1][1], 12)

        with Image.open(self.out / names[0]) as img:
            self.assertEqual(img.size, (8, 8))
            self.assertEqual(img.mode, "RGBA")

        self.assertIn("已导出 12 张", app.status_text.value)
        self.assertFalse(app.parse_progress.visible)
        self.assertIsNone(app.parse_progress.value)
        self.assertFalse(app.export_cancel_btn.visible)
        self.assertFalse(app.status_fail_action.visible)

    async def test_progress_and_cancel_visible_while_exporting(self):
        app = self.make_app(6)
        seen = {}

        def on_frame(done, total):
            if done == 1 and not seen:
                seen["bar"] = app.parse_progress.visible
                seen["cancel"] = app.export_cancel_btn.visible
                seen["text"] = app.status_text.value
                seen["meta"] = app.status_meta.value

        self.spy_progress(app, [], on_frame)
        await app._batch_export(None)
        await self.wait_status(app, "已导出 6 张")

        self.assertEqual(seen, {"bar": True, "cancel": True,
                                "text": "正在批量导出…", "meta": "导出 1/6"})

    async def test_zero_jobs_finishes_cleanly(self):
        app = self.make_app(0)
        frames = []
        self.spy_progress(app, frames)
        ctx = {"dir": str(self.out), "fname": FILENAME,
               "base": Image.new("RGBA", (8, 8)), "ox": 0, "oy": 0, "base_only": False}

        await app._run_export([], ctx)

        self.assertEqual(frames, [])
        self.assertEqual(self.png_files(self.out), [])
        self.assertIn("已导出 0 张", app.status_text.value)
        self.assertFalse(app.parse_progress.visible)
        self.assertIsNone(app.parse_progress.value)
        self.assertFalse(app.export_cancel_btn.visible)

    async def test_base_only_uses_base_naming(self):
        app = self.make_app(4, base_only=True)
        await app._batch_export(None)
        await self.wait_status(app, "已导出 4 张")
        self.assertEqual(self.png_files(self.out),
                         [f"{FILENAME}_base_{i:03d}.png" for i in range(4)])


class ExportCancelTest(ExportTestBase):
    async def test_cancel_stops_early_and_keeps_written_files(self):
        app = self.make_app(40)
        release = threading.Event()
        cancel_seen = []
        real_compose = export_mod.compose_preview
        calls = []

        def gated_compose(base, part, ox, oy):
            calls.append(part)
            if len(calls) >= 2:
                # 让除第一张以外的写入卡到取消生效之后，结果才可预期
                release.wait(5)
            return real_compose(base, part, ox, oy)

        def on_frame(done, total):
            if done == 1 and not cancel_seen:
                cancel_seen.append(True)
                app._cancel_export()
                release.set()

        self.spy_progress(app, [], on_frame)
        with mock.patch.object(export_mod, "compose_preview", side_effect=gated_compose):
            await app._batch_export(None)
        await self.wait_status(app, "已取消导出")

        written = len(self.png_files(self.out))
        self.assertGreater(written, 0, "第一张应该已经写出去")
        self.assertLess(written, 40, "取消后不该继续写满")
        self.assertIn(f"已写出 {written} 张", app.status_text.value)
        self.assertTrue(cancel_seen)
        self.assertFalse(app.parse_progress.visible)
        self.assertIsNone(app.parse_progress.value)
        self.assertFalse(app.export_cancel_btn.visible)

        # 取消位必须复位：下一批还能完整导出
        again = self.tmp / "again"
        again.mkdir()

        async def pick_again(**_kwargs):
            return str(again)

        app.file_picker.get_directory_path = pick_again
        app.part_imgs = app.part_imgs[:3]
        await app._batch_export(None)
        await self.wait_status(app, "已导出 3 张")
        self.assertEqual(len(self.png_files(again)), 3)

        # 结束之后再点取消不应改状态栏（没有活动批次）
        before = app.status_text.value
        app._cancel_export()
        self.assertEqual(app.status_text.value, before)

    async def test_cancel_without_active_batch_is_noop(self):
        app = self.make_app(2)
        app.status_text.value = "空闲"
        app._cancel_export()
        self.assertEqual(app.status_text.value, "空闲")


class ExportFailureTest(ExportTestBase):
    async def test_one_failure_does_not_stop_others_and_can_be_retried(self):
        app = self.make_app(6)
        blocked = self.out / f"{FILENAME}_diff_003.png"
        blocked.mkdir()  # 同名目录占位，写盘必然失败

        await app._batch_export(None)
        await self.wait_status(app, "1 张失败")

        self.assertEqual(len(self.png_files(self.out)), 5)
        self.assertIn("5 张成功", app.status_text.value)
        self.assertIn("1 张失败", app.status_text.value)
        self.assertTrue(app.status_fail_action.visible)

        # 失败清单对话框：标题与逐条「文件名 — 原因」
        app._show_export_failures()
        self.assertEqual(len(app.page.dialogs), 1)
        dialog = app.page.dialogs[-1]
        self.assertEqual(dialog.title.value, "导出失败 1 张")
        lines = [ctrl.value for ctrl in dialog.content._args[0]]
        self.assertEqual(len(lines), 1)
        self.assertIn(f"{FILENAME}_diff_003.png", lines[0])
        self.assertIn("Error", lines[0])

        # 重试只补失败项：把一张成功文件改成标记内容，重试后应原样保留
        marker = self.out / f"{FILENAME}_diff_000.png"
        marker.write_bytes(b"marker")
        blocked.rmdir()
        frames = []
        self.spy_progress(app, frames)
        await app._retry_failed(None)
        await self.wait_status(app, "已导出 1 张")

        self.assertTrue(blocked.is_file())
        self.assertEqual(marker.read_bytes(), b"marker", "成功项不该被重写")
        self.assertEqual([f[1:] for f in frames], [(1, 1)])
        self.assertFalse(app.status_fail_action.visible)

    async def test_unwritable_target_fails_every_frame_without_crashing(self):
        blocked = self.tmp / "target_is_a_file"
        blocked.write_bytes(b"x")
        app = self.make_app(6, target=blocked)

        await app._batch_export(None)
        await self.wait_status(app, "6 张失败")

        self.assertIn("0 张成功", app.status_text.value)
        self.assertEqual(list(self.tmp.glob("*.png")), [])
        self.assertTrue(app.status_fail_action.visible)

        app._show_export_failures()
        dialog = app.page.dialogs[-1]
        self.assertEqual(dialog.title.value, "导出失败 6 张")
        self.assertEqual(len(dialog.content._args[0]), 6)


class ExportStaleTest(ExportTestBase):
    async def test_stale_progress_frame_is_ignored(self):
        app = self.make_app(2)
        frames = []
        self.spy_progress(app, frames)
        await app._batch_export(None)
        await self.wait_progress_full(frames, 2)

        stale_gen = frames[-1][0]
        before_text = app.status_text.value
        before_meta = app.status_meta.value
        app._show_export_progress(stale_gen, 1, 9)

        self.assertEqual(app.status_text.value, before_text)
        self.assertEqual(app.status_meta.value, before_meta)
        self.assertFalse(app.parse_progress.visible)

    async def test_unload_during_directory_dialog_aborts_without_writing(self):
        app = self.make_app(6)
        gen_before = app._export_gen

        async def unload_in_dialog(**_kwargs):
            # 模拟用户在目录对话框开着时点「载出」：状态清空、代数作废
            app._abort_export()
            app.selected_info = None
            app._clear_previews()
            return str(self.out)

        app.file_picker.get_directory_path = unload_in_dialog
        await app._batch_export(None)

        self.assertEqual(self.png_files(self.out), [])
        self.assertFalse(app.parse_progress.visible)
        self.assertFalse(app.export_cancel_btn.visible)

        before = app.status_text.value
        app._show_export_progress(gen_before + 1, 3, 6)
        self.assertEqual(app.status_text.value, before)
        self.assertFalse(app.parse_progress.visible)


if __name__ == "__main__":
    unittest.main(verbosity=2)
