"""缩略图后台解码流水线：解码在 worker、替换+刷新必须回到 UI 线程。

假 flet 顶掉界面，Pillow 用真的——磁盘里的缩略图要能被 `Image.open` 读出来，
否则「写进去的是不是一张 PNG」永远验不到。等条件一律用谓词轮询，不排固定 sleep。

    python tests/test_thumb_threading.py -v
    cd tests && python -m unittest test_thumb_threading.ThumbThreadTest -v
"""

from __future__ import annotations

import asyncio
import io
import sys
import threading
import unittest
from unittest import mock

try:
    from . import support
except ImportError:  # 直接 python tests/test_thumb_threading.py
    import support

support.install_fake_flet()

from PIL import Image  # noqa: E402

import fvpapp.resources as resources_mod  # noqa: E402
from fvpapp import cache  # noqa: E402
from FVPTachieComposerFlet import ComposerApp, _is_expr  # noqa: E402
from FVPTachieComposerFlet import main as app_main  # noqa: E402


# ── 假 flet 控件树导航 ────────────────────────────────────────────────────
def _role_column(wrap):
    """载入那一轮角色外面套了错峰动画的 Container 壳，剥壳取里层 Column。"""
    args = getattr(wrap, "_args", ())
    return args[0] if args else wrap


def role_tiles(app):
    """[(role, header, body, toggle)]，保持树里的顺序。"""
    tiles = []
    for wrap in app.tree_list.controls:
        header, body = _role_column(wrap)._args[0]
        tiles.append((header._kwargs["title"].value, header, body, header._kwargs["on_click"]))
    return tiles


def outfit_tiles(role_body):
    tiles = []
    for col in role_body.controls:
        header, body = col._args[0]
        tiles.append((header._kwargs["title"].value, header, body, header._kwargs["on_click"]))
    return tiles


def action_tiles(outfit_body):
    """动作行是 Container(导轨边线) → Container(高亮) → ListTile。"""
    tiles = []
    for row in outfit_body.controls:
        tile = row._kwargs["content"]._kwargs["content"]
        tiles.append((tile._kwargs["title"].value, tile, tile._kwargs["on_click"]))
    return tiles


def pushed_src(placeholder):
    """占位容器是否已被 `_push_thumb` 换成带 src 的 Image（读实例属性）。"""
    widget = placeholder.__dict__.get("content")
    return None if widget is None else widget._kwargs.get("src")


class ThumbTestCase(support.AsyncEnvTestCase):
    async def asyncSetUp(self):
        self.main_ident = threading.get_ident()
        # 记录每次真实解码发生在哪个线程；PNG 还是用真的，只是套一层观察
        self.decodes: list[tuple[int, int]] = []
        real_thumb_png = resources_mod._thumb_png

        def record(img, size):
            self.decodes.append((threading.get_ident(), size))
            return real_thumb_png(img, size)

        patcher = mock.patch.object(resources_mod, "_thumb_png", record)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.apps = []
        self.app = self.new_app()

    async def asyncTearDown(self):
        # 线程池是模块级单例：不排干，上一用例的推送会落在下一用例的树上
        for app in self.apps:
            await support.drain_thumb_pool(app)

    def new_app(self):
        page = support.FakePage()
        app = ComposerApp(page)
        app._chime = lambda: None  # 测试里别真响
        app._loop = asyncio.get_running_loop()
        app.page_ref = page
        self.apps.append(app)
        return app

    def make_fixture(self, roles=("R",), outfits=("私服", "制服"),
                     actions=("基", "喜"), with_expr=True):
        src = self.tmp / "src"
        src.mkdir(exist_ok=True)
        for role in roles:
            for outfit in outfits:
                for action in actions:
                    support.make_hzc(src / f"CHR_{role}_{action}_{outfit}.hzc")
                    if with_expr:
                        support.make_hzc(src / f"CHR_{role}_{action}_{outfit}_表情.hzc",
                                         image_type=2, frames=2)
        return src

    @staticmethod
    async def wait_thumb(placeholder, what):
        await support.pump_until(lambda: pushed_src(placeholder) is not None, what=what)

    def disk_thumbs(self, app, size):
        """按资源指纹把缩略图键算出来，从磁盘缓存读回 PNG。"""
        out = []
        for info in app.file_infos:
            if _is_expr(info["filename"]):
                continue
            fp = cache.resource_fingerprint(info, app.input_file)
            key = cache.thumb_key(fp, info["filename"], size)
            png = cache.thumbs.read(key) if key is not None else None
            if png:
                out.append((info["filename"], png))
        return out


class ThumbThreadTest(ThumbTestCase):
    async def test_role_thumbnail_decodes_on_worker_and_pushes_on_ui_thread(self):
        src = self.make_fixture()
        self.assertIs(await self.app._load_bin(str(src)), True)

        (role, header, body, toggle), = role_tiles(self.app)
        self.assertEqual(role, "R")
        thumb = header._kwargs["leading"]
        # 载入返回时推送还没跑：loop 回调要等我们让出事件循环
        self.assertIsNone(pushed_src(thumb))

        await self.wait_thumb(thumb, "角色缩略图")
        self.assertTrue(pushed_src(thumb).startswith("data:image/png;base64,"))
        self.assertTrue(thumb.update_threads)
        self.assertEqual(set(thumb.update_threads), {self.main_ident}, "缩略图 update 不在 UI 线程")

        role_decodes = [ident for ident, size in self.decodes if size == 40]
        self.assertEqual(len(role_decodes), 1, self.decodes)
        self.assertNotEqual(role_decodes[0], self.main_ident, "解码发生在 UI 线程了")

    async def test_outfit_and_action_thumbnails_follow_same_pipeline(self):
        src = self.make_fixture()
        await self.app._load_bin(str(src))

        (_, _, body, toggle), = role_tiles(self.app)
        toggle(None)
        outfits = outfit_tiles(body)
        self.assertEqual({name for name, *_ in outfits}, {"私服", "制服"})
        for name, oheader, _, _ in outfits:
            thumb = oheader._kwargs["leading"]
            await self.wait_thumb(thumb, f"服装 {name} 缩略图")
            self.assertEqual(set(thumb.update_threads), {self.main_ident})

        otoggle = outfits[0][3]
        otoggle(None)
        actions = action_tiles(outfits[0][2])
        self.assertEqual(len(actions), 2)
        for name, tile, _ in actions:
            thumb = tile._kwargs["leading"]
            await self.wait_thumb(thumb, f"动作 {name} 缩略图")
            self.assertEqual(set(thumb.update_threads), {self.main_ident})

        self.assertEqual(sorted(size for _, size in self.decodes), [26, 26, 30, 30, 40])
        self.assertTrue(all(ident != self.main_ident for ident, _ in self.decodes))

    async def test_disk_png_reused_by_new_app_without_decoding(self):
        src = self.make_fixture()
        await self.app._load_bin(str(src))
        (_, header, _, _), = role_tiles(self.app)
        await self.wait_thumb(header._kwargs["leading"], "角色缩略图")

        pngs = self.disk_thumbs(self.app, 40)
        self.assertEqual(len(pngs), 1, pngs)
        with Image.open(io.BytesIO(pngs[0][1])) as img:
            self.assertEqual(img.format, "PNG")
            self.assertEqual(img.size, (8, 8))

        decodes_before = len(self.decodes)
        # 新实例 = 新的控件缓存；内存里的显示串也清掉，只留磁盘这一层
        support.reset_cache_singletons()
        app2 = self.new_app()
        await app2._load_bin(str(src))
        (_, header2, _, _), = role_tiles(app2)
        await self.wait_thumb(header2._kwargs["leading"], "第二次载入的角色缩略图")

        self.assertEqual(len(self.decodes), decodes_before, "磁盘已命中却还在解码")
        self.assertEqual(len(self.disk_thumbs(app2, 40)), 1)

    async def test_without_event_loop_push_happens_on_worker(self):
        src = self.make_fixture(outfits=("私服",), actions=("基",))
        await self.app._load_bin(str(src))
        (_, header, _, _), = role_tiles(self.app)
        await self.wait_thumb(header._kwargs["leading"], "角色缩略图")

        # 没有事件循环（例如非 async 调用路径）时只能就地替换
        self.app._loop = None
        info = next(i for i in self.app.file_infos if not _is_expr(i["filename"]))
        placeholder = support.FakeControl()
        self.app._queue_thumb(placeholder, info, 31, "manual")

        await support.pump_until(lambda: pushed_src(placeholder) is not None,
                                 what="无 loop 时的直接推送")
        worker = placeholder.update_threads[0]
        self.assertNotEqual(worker, self.main_ident)
        self.assertTrue(all(t == worker for t in placeholder.update_threads))
        self.assertIn((worker, 31), self.decodes)

    async def test_stale_decode_after_unload_never_touches_detached_placeholder(self):
        src = self.make_fixture(outfits=("私服",), actions=("基",))
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        real = resources_mod.hzc_decode_first_frame

        def gated(data, header):
            started.set()
            try:
                release.wait(5)
                return real(data, header)
            finally:
                finished.set()

        with mock.patch.object(resources_mod, "hzc_decode_first_frame", gated):
            await self.app._load_bin(str(src))
            (_, header, _, _), = role_tiles(self.app)
            thumb = header._kwargs["leading"]
            await support.pump_until(started.is_set, what="缩略图解码开始")
            await self.app._unload_bin()

        release.set()
        await support.pump_until(finished.is_set, what="被拦住的解码结束")
        await support.drain_thumb_pool(self.app)

        self.assertNotIn("content", thumb.__dict__, "载出后迟到的解码仍写进了旧控件")
        self.assertEqual(thumb.update_threads, [])

    async def test_stale_decode_during_second_parse_never_touches_placeholder(self):
        """换资源有个 token 还没换的窗口：_load_bin 一进门就升 gen，而
        _cache_token 要等新解析完、建树前才升。这期间回来的旧解码只有 deliver
        里的 gen 守卫拦得住——token 检查在这个窗口里是放行错误结果的。"""
        src = self.make_fixture(outfits=("私服",), actions=("基",))
        src2 = self.tmp / "src2"
        src2.mkdir()
        support.make_hzc(src2 / "CHR_R2_基_私服.hzc")

        started, release, finished = (threading.Event() for _ in range(3))
        real = resources_mod.hzc_decode_first_frame
        real_parse = resources_mod.parse_dir_infos
        parse_started, parse_release = threading.Event(), threading.Event()

        def gated(data, header):
            started.set()
            try:
                release.wait(5)
                return real(data, header)
            finally:
                finished.set()

        def slow_parse(*a, **k):
            parse_started.set()
            parse_release.wait(5)
            return real_parse(*a, **k)

        with mock.patch.object(resources_mod, "hzc_decode_first_frame", gated):
            await self.app._load_bin(str(src))
            (_, header, _, _), = role_tiles(self.app)
            thumb = header._kwargs["leading"]
            await support.pump_until(started.is_set, what="角色缩略图解码开始")

            # 只拦第二份的解析：第一份若也被拦，等到的就是它而不是窗口期
            with mock.patch.object(resources_mod, "parse_dir_infos", slow_parse):
                task = asyncio.create_task(self.app._load_bin(str(src2)))
                # gen 已升、token 未升：旧解码此刻回来，deliver 的守卫是唯一防线
                await support.pump_until(parse_started.is_set, what="第二份资源解析在途")
                release.set()
                await support.pump_until(finished.is_set, what="被拦住的解码结束")
                await support.drain_thumb_pool(self.app)
                parse_release.set()
                await task

        self.assertNotIn("content", thumb.__dict__,
                         "解析窗口期里迟到的解码写进了旧占位控件")
        self.assertEqual(thumb.update_threads, [])


class AutoSelectTest(ThumbTestCase):
    async def test_single_outfit_single_action_cascades_into_selection(self):
        src = self.tmp / "auto"
        src.mkdir()
        support.make_hzc(src / "CHR_S_基_私服.hzc")
        support.make_hzc(src / "CHR_S_基_私服_表情.hzc", image_type=2, frames=2)
        support.make_hzc(src / "CHR_T_基_私服.hzc")
        support.make_hzc(src / "CHR_T_喜_私服.hzc")
        await self.app._load_bin(str(src))

        tiles = {name: (header, body, toggle) for name, header, body, toggle in role_tiles(self.app)}
        self.assertEqual(set(tiles), {"S", "T"})

        _, sbody, stog = tiles["S"]
        self.assertFalse(sbody.visible)
        stog(None)
        self.assertTrue(sbody.visible)
        (_, soheader, sobody, _), = outfit_tiles(sbody)
        self.assertTrue(sobody.visible, "单服装未自动展开")

        # 级联选中是 _spawn 出来的任务，等它把底图/差分解完
        await support.pump_until(lambda: self.app.composed_img is not None,
                                 what="单动作自动选中合成")
        self.assertEqual(self.app.selected_filename, "CHR_S_基_私服")

        before = self.app.composed_img
        stog(None)
        stog(None)
        self.assertEqual(self.app.selected_filename, "CHR_S_基_私服")
        self.assertIs(self.app.composed_img, before, "重复加载了已选项")
        self.assertTrue(sobody.visible, "重展开时已开的服装被误关闭")


class ArgvTest(ThumbTestCase):
    async def run_main(self, argv):
        page = support.FakePage()
        old, sys.argv = sys.argv, ["prog", *argv]
        try:
            app = await app_main(page)
        finally:
            sys.argv = old
        if app is not None:
            app._chime = lambda: None
            self.apps.append(app)
        return app, page

    async def test_directory_argument_loads_on_startup(self):
        src = self.make_fixture(roles=("Q",), outfits=("私服",), actions=("基",),
                                with_expr=False)
        app, _ = await self.run_main([str(src)])
        self.assertEqual(app.input_file, str(src))
        self.assertIn("Q", app.hierarchy)

    async def test_packaged_argv_without_script_name_also_loads(self):
        # 打包态 SeriousPython 把用户参数从 argv[0] 起放：拖到 exe 图标时没有脚本名
        src = self.make_fixture(roles=("Q",), outfits=("私服",), actions=("基",),
                                with_expr=False)
        app, _ = await self.run_main([str(src)])
        self.assertEqual(app.input_file, str(src))

    async def test_missing_argument_shows_error_banner(self):
        app, page = await self.run_main([str(self.tmp / "不存在.bin")])
        self.assertIsNone(app.input_file)
        self.assertEqual(len(page.overlay), 1, "非法参数应弹报错横幅")


class SnackTest(ThumbTestCase):
    async def test_success_snack_dismisses_and_error_snack_stays(self):
        self.app._snack("成功", ttl=0.05)
        self.assertEqual(len(self.app.page_ref.overlay), 1, "横幅未挂载")
        await support.pump_until(lambda: not self.app.page_ref.overlay,
                                 timeout=2, what="成功横幅自动消失")

        seq = self.app._snack("失败", error=True)
        # 负向检查：没有可等的观察点，只留一个短窗口证明它确实不消失
        await asyncio.sleep(0.15)
        self.assertEqual(len(self.app.page_ref.overlay), 1, "失败横幅不应自动消失")
        self.assertTrue(self.app._snack_is_showing(seq))

        self.app._dismiss_snack(seq)
        await support.pump_until(lambda: not self.app.page_ref.overlay, what="手动关闭")

    async def test_new_snack_replaces_previous_overlay(self):
        self.app._snack("第一条", ttl=10)
        self.app._snack("第二条", error=True)
        self.assertEqual(len(self.app.page_ref.overlay), 1, "旧横幅没有摘掉")


if __name__ == "__main__":
    unittest.main(verbosity=2)
