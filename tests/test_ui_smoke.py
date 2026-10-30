"""真实 flet 下的 UI 冒烟测试：把整棵控件树建出来并驱动一遍主流程。

与 test_thumb_threading.py 的分工：那个用宽容的假 flet 验证线程契约，
这个用**真** flet 构造控件——任何拼错的属性名/类型都会在这里炸出来，
顺带断言"滚动条必须留在预留槽内"这类布局不变量（重合 bug 的回归防线）。

需要已安装 flet + Pillow 的解释器（Windows：`py.exe -3.13 tests/test_ui_smoke.py`）。
缺依赖是**失败**不是跳过——静默跳过的回归防线等于没有。
真 flet 与假 flet 不能混在一个进程里跑，入口见 tests/run_all.py。
"""

import asyncio
import shutil
import sys
import unittest
from pathlib import Path
from unittest import mock

try:
    from . import support
except ImportError:  # 直接 `python tests/test_ui_smoke.py` 时没有包上下文
    import support

try:
    import flet as ft
    import PIL.Image  # noqa: F401
except Exception as ex:
    raise RuntimeError(
        f"test_ui_smoke 需要真实 flet 与 Pillow（当前缺失: {ex}）。"
        "请在装有 flet 的解释器上运行本文件，例如 Windows: py.exe -3.13 tests/test_ui_smoke.py"
    ) from ex

from FVPTachieComposerFlet import ComposerApp, main as app_main
from fvpapp import settings as st
from fvpapp.dropzone import (
    DROPZONE_ON,
    _custom_client_available,
    _dropzone,
    _dropzone_on,
    pick_drop_path,
)
from fvpapp.theme import TRANSPARENT

_need_dropzone = unittest.skipUnless(
    DROPZONE_ON, "flet_dropzone 不可用或非自定义客户端：拖放反馈整条链路不存在")


# ── 事件替身 ──────────────────────────────────────────────────────────────
class _Resize:
    """假 CanvasResizeEvent：只带宽高，够驱动 _on_drop_resize。"""

    def __init__(self, w, h):
        self.width = w
        self.height = h


class _Key:
    """假键盘事件：只带 _on_keyboard 真正读的三个字段。"""

    def __init__(self, key, control=None, ctrl=False):
        self.key = key
        self.control = control
        self.ctrl = ctrl


class _WinEvt:
    def __init__(self, t):
        self.type = t


class _DropFile:
    def __init__(self, p):
        self.path, self.name = p, Path(p).name


class _DropEvt:
    def __init__(self, ps):
        self.files = [_DropFile(p) for p in ps]


# ── 控件树取点的小工具 ────────────────────────────────────────────────────
def _unwrap(wrap):
    """载入时角色套在错峰动画的 Container 外壳里，剥壳取里层 Column。"""
    if isinstance(wrap, ft.Container):
        return wrap.content
    return wrap


def _tile_title(ctrl):
    title = getattr(ctrl, "title", None)
    return getattr(title, "value", None)


def _find_role(app, role):
    for wrap in app.tree_list.controls:
        col = _unwrap(wrap)
        if col.controls and _tile_title(col.controls[0]) == role:
            return col
    raise AssertionError(f"树里找不到角色 {role}")


def _find_outfit(app, role, outfit):
    body = _find_role(app, role).controls[1]
    for wrap in body.controls:
        col = _unwrap(wrap)
        if col.controls and _tile_title(col.controls[0]) == outfit:
            return col
    raise AssertionError(f"树里找不到服装 {role}/{outfit}")


def strip_srcs(app):
    return [getattr(c.content, "src", None) for c in app.thumb_list.controls]


def role_thumb_src(app, role):
    w = app.role_thumb_cache.get(role)
    if w is None:
        return None
    return getattr(w, "src", None) or getattr(getattr(w, "content", None), "src", None)


class RealFletUiTest(support.AsyncEnvTestCase):
    """每用例：新的事件循环 + 独立缓存/设置目录 + 全新 App 实例。

    夹具源文件按类共享（只读）；缓存目录每个用例新建，命中/未命中才可预期。
    """

    @classmethod
    def setUpClass(cls):
        cls.fixture = support.build_fixture()
        cls.addClassCleanup(shutil.rmtree, cls.fixture, True)

    def setUp(self):
        super().setUp()
        self.page = support.RealFletPage()
        self.addCleanup(self.page.close)
        self.app = ComposerApp(self.page)
        self._apps = [self.app]
        # 线程池是模块级单例：收尾先作废代数、再等池里在途任务落地，
        # 保证无论用例走到哪一步失败都不把解码带进下一个用例。
        # addCleanup 是 LIFO，这条登记在最后、最先执行。
        self.addCleanup(self._invalidate)

    def _invalidate(self):
        for app in self._apps:
            app._state_gen += 1
            app._cache_token += 1
        try:
            import fvpapp.resources as res
            res._THUMB_POOL.submit(lambda: None).result(timeout=5)
        except Exception:
            pass

    def new_page(self):
        page = support.RealFletPage()
        self.addCleanup(page.close)
        return page

    def new_app(self):
        app = ComposerApp(self.new_page())
        self._apps.append(app)
        return app

    async def load(self, app=None, target=None):
        app = app if app is not None else self.app
        ok = await app._load_bin(str(target if target is not None else self.fixture))
        self.assertTrue(ok, "载入返回失败")
        await support.pump_until(lambda: app.tree_list.visible, what="树显示")
        return app

    @staticmethod
    def toggle(app, key):
        state, tog = app._tree_toggles[key]
        if not state["open"]:
            tog(None)

    def expand_all(self, app=None):
        app = app if app is not None else self.app
        for role in sorted(app.hierarchy):
            self.toggle(app, ("role", role))
        for role in sorted(app.hierarchy):
            for outfit in sorted(app.hierarchy[role]):
                self.toggle(app, ("outfit", f"{role}/{outfit}"))

    @staticmethod
    def info(app, filename):
        return next(i for i in app.file_infos if i["filename"] == filename)

    async def select(self, app, filename):
        await app._select_action(self.info(app, filename))
        await support.pump_until(
            lambda: app.base_image.src != TRANSPARENT and all(strip_srcs(app)),
            what="底图与差分条上屏")


# ── 装机与布局不变量 ──────────────────────────────────────────────────────
class BootstrapTest(RealFletUiTest):
    async def test_theme_switch_survives_full_rebuild(self):
        """两套主题都要能把整棵控件树建出来（属性拼错在真 flet 下当场炸）。"""
        self.assertTrue(self.page.added, "页面根控件未添加")
        await self.app._toggle_theme(None)
        await self.app._toggle_theme(None)
        self.assertTrue(self.page.added, "换主题后根控件丢了")

    async def test_images_and_scroll_lanes(self):
        """图像 src 必须是字符串 data URI，滚动条必须留在预留槽内。

        src 给 bytes 只在构造时有效，之后 `img.src = bytes` 不会被重新编码，
        客户端当成路径解析 → 预览区空白（真机踩过）。
        """
        await self.load()
        self.toggle(self.app, ("role", "翠"))
        self.toggle(self.app, ("outfit", "翠/私服"))
        await self.select(self.app, "CHR_翠_基_私服")
        for name in ("base_image", "result_image"):
            src = getattr(self.app, name).src
            self.assertTrue(isinstance(src, str) and src.startswith("data:image/"),
                            f"{name}.src 不是 data URI: {type(src)}")
        nodes = [c for r in self.page.added for c in support.walk_controls(r)]
        for c in nodes:
            if getattr(c, "image", None) is not None:
                self.assertIsInstance(
                    c.image, ft.DecorationImage,
                    "Container.image 只认 DecorationImage，塞 ft.Image 会被静默丢掉")
        for name, lv in (("tree_list", self.app.tree_list),
                        ("thumb_list", self.app.thumb_list)):
            p = lv.padding
            right = getattr(p, "right", 0) or 0
            bottom = getattr(p, "bottom", 0) or 0
            self.assertTrue(right >= 8 or bottom >= 8,
                            f"{name} 未为滚动条预留槽位 (right={right}, bottom={bottom})")
        sb = self.page.theme.scrollbar_theme
        self.assertIs(sb.track_visibility, False, "滚动条轨道不应占视觉重量")
        self.assertLessEqual(sb.thickness or 0, 10, f"滚动条过粗: {sb.thickness}")

    async def test_rolling_containers_and_tile_rows(self):
        """交叉轴 STRETCH 在滚动容器里给不出有界高度，布局当场抛异常、
        该项之后的节点全部消失（ListView 沿滚动方向不约束高度）。"""
        await self.load()
        nodes = [c for r in self.page.added for c in support.walk_controls(r)]
        for c in nodes:
            if isinstance(c, ft.Row):
                self.assertNotEqual(c.vertical_alignment, ft.CrossAxisAlignment.STRETCH,
                                    "Row 在滚动容器里不能用 STRETCH")
            if isinstance(c, ft.Column):
                self.assertNotEqual(c.horizontal_alignment, ft.CrossAxisAlignment.STRETCH,
                                    "Column 在滚动容器里不能用 STRETCH")
            if isinstance(c, ft.ListTile) and isinstance(c.trailing, ft.Row):
                self.assertTrue(c.trailing.tight,
                                "ListTile.trailing 的 Row 必须 tight，否则标题被挤成竖排")

    async def test_visual_baseline(self):
        """白卡片 + 浅灰页底 + 1px 细描边；禁止材质特效与重复入口回潮。"""
        await self.load()
        nodes = [c for r in self.page.added for c in support.walk_controls(r)]
        from fvpapp.theme import RADIUS_PANEL
        self.assertTrue(self.app._checker, "透明棋盘底缺失")
        panels = [c for c in nodes
                  if type(c) is ft.Container
                  and c.border_radius == RADIUS_PANEL
                  and c.bgcolor == self.app.ink["panel"]
                  and c.border is not None and c.border.top.width == 1]
        self.assertGreaterEqual(len(panels), 3, "三栏必须是实色细描边卡片")
        for c in panels:
            self.assertIsNone(c.gradient, "面板不该再用渐变")
            self.assertIsNone(c.shadow, "面板不该再用投影")
        for c in nodes:
            if getattr(c, "image", None) is not None \
                    and c.image.repeat == ft.ImageRepeat.REPEAT:
                self.assertIsNotNone(c.border_radius, "页面底层不该再铺满无边界纹理")
        from flet.controls.material.button import Button as _Button
        empty_btns = [c for c in support.walk_controls(self.app.empty_hint)
                      if isinstance(c, _Button)]
        self.assertFalse(empty_btns, "载入入口只留角色库标题栏那两个图标")
        solid = [c for c in nodes if isinstance(c, _Button)
                 and c.style is not None and c.style.bgcolor == self.app.ink["accent"]]
        self.assertFalse(solid, "不许有实心 accent 大按钮")

    def test_dropzone_wraps_root(self):
        """开启拖放时整扇窗口根节点是 Dropzone（未开启时保持原 Column）。"""
        self.assertTrue(self.page.added, "页面根控件未添加")
        root = self.page.added[0]
        if DROPZONE_ON:
            self.assertIsInstance(root, _dropzone.Dropzone)
        else:
            self.assertNotIsInstance(root, type(_dropzone.Dropzone)
                                     if _dropzone is not None else ft.Container)


# ── 拖放能力门控 ──────────────────────────────────────────────────────────
class DropzoneGateTest(support.EnvTestCase):
    def test_dropzone_flag_follows_client(self):
        """官方客户端没有 desktop_drop 插件，未 build 的运行时塞 Dropzone
        会让整棵控件树渲染失败——门控必须跟着"客户端是谁"走。"""
        self.assertIs(DROPZONE_ON, bool(_dropzone) and _custom_client_available())

    def test_env_flag_forces_off(self):
        self.sandbox.set(FVP_DROPZONE="0")
        self.assertIs(_dropzone_on(), False)

    def test_env_flag_forces_on(self):
        if _dropzone is None:
            self.skipTest("flet_dropzone 未安装：强制开关无从谈起")
        self.sandbox.set(FVP_DROPZONE="1")
        self.assertIs(_dropzone_on(), True)

    def test_pick_drop_path(self):
        d = self.tmp / "dir"
        d.mkdir()
        bin_a = self.tmp / "graph_a.bin"
        bin_a.write_bytes(b"x")
        missing = str(self.tmp / "graph_b.bin")
        self.assertEqual(pick_drop_path([str(d), str(bin_a)]), str(bin_a),
                         ".bin 应优先于文件夹")
        self.assertEqual(pick_drop_path([str(bin_a), str(d)]), str(bin_a))
        self.assertEqual(pick_drop_path([str(d)]), str(d))
        self.assertEqual(pick_drop_path([missing]), missing,
                         "不存在的 bin 仍要交给 _load_bin 报错")
        self.assertIsNone(pick_drop_path(["", None]))


# ── 树交互：展开、缩进、选中、合成链路 ────────────────────────────────────
class TreeInteractionTest(RealFletUiTest):
    async def _open(self):
        await self.load()
        self.toggle(self.app, ("role", "翠"))
        self.toggle(self.app, ("outfit", "翠/私服"))
        return self.app

    async def test_hierarchy_indent_and_rail(self):
        """第三级必须比第二级更深，且带一条导轨线，否则层级读起来像同级。"""
        from fvpapp.theme import INDENT_ACTION, INDENT_OUTFIT
        self.assertGreater(INDENT_ACTION, INDENT_OUTFIT)
        app = await self._open()
        outfit_col = _find_outfit(app, "翠", "私服")
        action_row = outfit_col.controls[1].controls[0]
        self.assertTrue(outfit_col.controls[1].controls, "动作层为空")
        self.assertEqual(action_row.margin.left, INDENT_ACTION)
        self.assertEqual(outfit_col.controls[0].content_padding.left, INDENT_OUTFIT)
        self.assertEqual(action_row.border.left.width, 1, "动作层缺少导轨线")
        self.assertIsNotNone(action_row.content.animate,
                             "选中态必须有过渡，不能只是闪一下变色")

    async def test_selection_highlight_is_exclusive(self):
        """选中态由 filename 决定而不是"上次点了哪个控件"：换选后不许两行同时亮。"""
        app = await self._open()
        info = self.info(app, "CHR_翠_基_私服")
        await app._select_action(info)
        row = support.find_action_row(app, info["filename"])
        self.assertEqual(row.hl.bgcolor, app.ink["sel"])
        self.assertEqual(row.hl.border.left.color, app.ink["accent"])
        self.assertEqual(row.hl.content.title.weight, ft.FontWeight.W_700)
        other = support.find_action_row(app, "CHR_翠_喜_私服")
        self.assertNotEqual(other.hl.bgcolor, app.ink["sel"])

        await app._select_action(self.info(app, "CHR_翠_喜_私服"))
        self.assertEqual(other.hl.bgcolor, app.ink["sel"], "新选中的行没有亮起")
        self.assertEqual(row.hl.bgcolor, "#00000000", "上一行的选中态没被清掉")
        self.assertEqual(row.hl.content.title.weight, ft.FontWeight.W_400)

    async def test_preview_pipeline(self):
        """底图 → 差分 → 合成 全链路，以及两个面板头的尺寸不重复显示。"""
        app = await self._open()
        await app._select_action(self.info(app, "CHR_翠_基_私服"))
        self.assertTrue(app.base_imgs, "底图未加载")
        self.assertTrue(app.part_imgs, "差分部件未加载")
        self.assertIsNotNone(app.composed_img, "合成结果为空")
        self.assertIn("×", app.base_meta.value)
        self.assertNotIn("×", app.result_meta.value, "结果面板头不该重复底图尺寸")
        self.assertTrue(app.result_meta.value.startswith("偏移"))
        self.assertTrue(app.thumb_list.visible, "缩略图条未显示")

        app._pick_part(min(2, len(app.part_imgs) - 1))
        badges = {t.badge.bgcolor for t in app.thumb_list.controls}
        self.assertEqual(badges, {app.ink["badge_bg"]},
                         "差分角标只做编号，不许被选中态带跑")
        picked = app.thumb_list.controls[app.part_idx]
        self.assertEqual(picked.border.top.color, app.ink["accent"],
                         "选中的差分没有描边提示")
        self.assertIsNotNone(picked.animate, "差分切换应有过渡")
        # Flet 0.86 下给 Container 显式设 animate_size/position=False 会让
        # 整块内容不渲染（真机截图里动作行和差分条变成纯灰矩形），必须保持默认。
        self.assertIsNone(picked.animate_size)
        self.assertIsNone(picked.animate_position)
        self.assertTrue(isinstance(app.thumb_list.controls[0].content.src, str),
                        "差分缩略图 src 必须是字符串")


# ── 换主题：现场保留与拖放态复位 ──────────────────────────────────────────
class ThemeRebuildTest(RealFletUiTest):
    async def test_rebuild_keeps_everything_visible(self):
        await self.load()
        app = self.app
        self.toggle(app, ("role", "翠"))
        self.toggle(app, ("outfit", "翠/私服"))
        info = self.info(app, "CHR_翠_基_私服")
        await app._select_action(info)
        app._pick_part(min(2, len(app.part_imgs) - 1))
        kept_idx = app.part_idx
        snapshot = app.composed_img
        kept_sel = app.selected_filename
        kept_base_src = app.base_image.src

        await app._toggle_theme(None)
        self.assertTrue(app.is_dark)
        self.assertEqual(app.selected_filename, kept_sel, "换主题后选中的动作变了")
        self.assertEqual(app.base_image.src, kept_base_src, "换主题后底图被换成了另一张")
        # 重建树期间"单动作服装展开即选中"不能触发级联，也不许把展开态洗掉
        self.assertTrue(_find_role(app, "翠").controls[1].visible,
                        "换主题把已展开的角色收起了")
        self.assertTrue(_find_outfit(app, "翠", "私服").controls[1].visible,
                        "换主题把已展开的服装收起了")
        self.assertEqual(app.part_idx, kept_idx, "换主题后选中的差分帧被重置")
        self.assertIs(app.composed_img, snapshot, "换主题重新读盘重算了（应复用缓存）")
        self.assertEqual(len(app.thumb_list.controls), len(app.part_imgs),
                         "换主题后差分条没重建")
        for name in ("base_image", "result_image"):
            src = getattr(app, name).src
            self.assertTrue(isinstance(src, str) and src.startswith("data:image/"),
                            f"换主题后 {name}.src 丢了，预览区会空")
        self.assertFalse(app.save_btn.disabled)
        self.assertFalse(app.batch_btn.disabled)
        self.assertEqual(app.file_chip.content.value, Path(app.input_file).name,
                         "标题栏文件名胶囊必须从当前状态推导")
        # 重建树后的选中态也要在新树上原样点亮（不是刷废弃控件）
        row = support.find_action_row(app, kept_sel)
        self.assertEqual(row.hl.bgcolor, app.ink["sel"])

    @_need_dropzone
    async def test_rebuild_resets_drop_state(self):
        """换主题会整棵重建，拖放画布也在其中；状态不归零的话下一次拖入不再亮。"""
        app = self.app
        app._on_drop_resize(_Resize(300, 700))
        app._drag_entered()
        self.assertTrue(app._drop_canvas.visible)
        await app._toggle_theme(None)
        self.assertFalse(app._drop_canvas.visible,
                         "换主题后画布还亮着——它会吃掉面板里所有的点击")
        app._drag_entered()
        self.assertTrue(app._drop_canvas.visible, "拖放态残留导致下一次拖入没有反馈")
        app._drag_exited()


# ── 无表情差分 → 直接导出底图 ─────────────────────────────────────────────
class NoDiffBaseOnlyTest(RealFletUiTest):
    async def _select_nodiff(self):
        await self.load()
        self.toggle(self.app, ("role", "翠"))
        self.toggle(self.app, ("outfit", "翠/私服"))
        await self.app._select_action(self.info(self.app, "CHR_翠_无_私服"))

    async def test_mirror_and_export_names(self):
        """只让看不让导出的状态是不完整的：没有差分变体时结果面板要镜像
        当前底图帧，保存/批量导出都退到底图本身。"""
        from fvpapp.theme import PART_HINT_EMPTY
        await self._select_nodiff()
        app = self.app
        self.assertTrue(app.base_only, "没有差分时应进入「直接导出底图」模式")
        self.assertEqual(app.part_imgs, [])
        self.assertIsNone(app.composed_img)
        self.assertEqual(app.part_hint_text.value, PART_HINT_EMPTY)
        self.assertFalse(app.result_hint.visible)
        self.assertFalse(app.save_btn.disabled)
        self.assertFalse(app.batch_btn.disabled)
        mirrored = app.result_image.src
        self.assertTrue(mirrored.startswith("data:image/"))
        self.assertEqual(mirrored, app.base_image.src, "右栏镜像的不是当前底图帧")

        app._next_frame(None)   # 换帧要跟手：所见即所导
        self.assertEqual(app.base_idx, 1)
        self.assertNotEqual(app.result_image.src, mirrored, "翻帧后右栏还停在上一帧")
        self.assertEqual(app.result_image.src, app.base_image.src)
        self.assertIn("底图（无表情差分）", app.result_meta.value)

        picked_names = []
        batch_dir = self.tmp / "batch"
        batch_dir.mkdir()

        async def fake_save(dialog_title=None, file_name=None,
                            initial_directory=None, **kw):
            picked_names.append(file_name)
            return str(self.tmp / file_name)

        async def fake_dir(dialog_title=None, initial_directory=None, **kw):
            return str(batch_dir)

        app.file_picker.save_file = fake_save
        app.file_picker.get_directory_path = fake_dir
        try:
            await app._save_current(None)
            self.assertEqual(picked_names, ["CHR_翠_无_私服_base_001.png"],
                             "底图导出命名不对")
            self.assertTrue((self.tmp / picked_names[0]).exists(), "底图没真正写盘")
            await app._batch_export(None)
            self.assertEqual(sorted(p.name for p in batch_dir.glob("*.png")),
                             [f"CHR_翠_无_私服_base_{i:03d}.png" for i in range(3)],
                             "无差分批量导出没有铺平底图全部帧")
        finally:
            del app.file_picker.save_file
            del app.file_picker.get_directory_path

    async def test_theme_rebuild_keeps_base_only(self):
        await self._select_nodiff()
        app = self.app
        app._next_frame(None)
        kept_base_src = app.base_image.src
        await app._toggle_theme(None)
        self.assertTrue(app.base_only, "换主题把「直接导出底图」模式弄丢了")
        self.assertEqual(app.base_idx, 1, "换主题后底图帧号被重置")
        self.assertEqual(app.base_image.src, kept_base_src)
        self.assertEqual(app.result_image.src, app.base_image.src, "换主题后右栏没跟住底图")
        self.assertFalse(app.save_btn.disabled)
        self.assertFalse(app.batch_btn.disabled)


# ── 拖放反馈（画在角色库面板的画布上）─────────────────────────────────────
class DropFeedbackTest(RealFletUiTest):
    def setUp(self):
        super().setUp()
        self.app._on_drop_resize(_Resize(300, 700))

    def _dash_color(self):
        # shapes 每次重绘都是新对象，必须现取，不能握住旧引用比
        return self.app._drop_canvas.shapes[2].paint.color

    @_need_dropzone
    def test_enter_paints_frame(self):
        app = self.app
        app._drag_entered()
        canvas = app._drop_canvas
        self.assertTrue(canvas.visible, "拖入后面板没进入拖放态")
        self.assertEqual((canvas.top, canvas.left, canvas.right, canvas.bottom),
                         (0, 0, 0, 0),
                         "画布必须贴满面板，否则虚线框缩在描边里一圈")
        self.assertEqual(len(canvas.shapes), 5,
                         "画布应是 底纹/辉光边/虚线边/光斑拖尾/光斑核心 五层")
        wash = canvas.shapes[0]
        self.assertEqual(wash.paint.style, ft.PaintingStyle.FILL)
        self.assertRegex(wash.paint.color, r"^#[0-9A-F]{8}$",
                         f"底纹颜色不是 #AARRGGBB: {wash.paint.color}")
        self.assertTrue(canvas.shapes[2].paint.stroke_dash_pattern, "落点边框不是虚线")
        self.assertEqual(app._hint_title.value, "松手即载入")
        app._drag_exited()

    @_need_dropzone
    def test_fade_in_then_streak_crawls(self):
        """淡入：进度逐帧推到全亮后停住；行进光斑：tick 一次必须沿框边挪动。"""
        app = self.app
        app._drag_entered()
        app._drop_gen += 1          # 掐掉自发排队的计时链，下面手工逐帧推
        gen = app._drop_gen
        for _ in range(12):         # DROP_IN_FRAMES=6，多推几帧看过冲和停表
            app._tick_drop(gen)
            if self._dash_color().startswith("#FF"):
                break
        self.assertTrue(self._dash_color().startswith("#FF"), "淡入没走到全亮")
        head_before = (app._drop_canvas.shapes[4].elements[0].x,
                       app._drop_canvas.shapes[4].elements[0].y)
        app._tick_drop(gen)
        head_after = (app._drop_canvas.shapes[4].elements[0].x,
                      app._drop_canvas.shapes[4].elements[0].y)
        self.assertNotEqual(head_after, head_before, "光斑没跟着 tick 沿框边行进")

    @_need_dropzone
    async def test_reject_shows_red_then_fades(self):
        """拖进来的东西不认：边框变红停 160ms 再淡出，报错就地发生不弹底部横幅。"""
        app = self.app
        app._drag_entered()          # 真实路径：先拖进来才谈松手，画布得先在册
        canvas = app._drop_canvas
        self.assertTrue(canvas.visible, "进入拖放态后画布没亮，后面的观察全是空的")
        app._reject_drop()
        err = "#FF" + app.ink["err_frame"].lstrip("#").upper()
        self.assertEqual(canvas.shapes[2].paint.color, err, "拒绝时虚线框没变红")
        self.assertEqual(app._hint_title.value, "拖进来的不是资源")
        self.assertNotIn(app._snack_overlay, self.page.overlay,
                         "拒绝不该弹底部横幅（视线就在落点上）")
        # 不收回的画布会吃掉面板里的点击
        await support.pump_until(lambda: canvas.visible is False,
                                 timeout=3.0, what="拒绝态淡出收回")
        self.assertEqual(app._hint_title.value, "尚未加载文件")

    @_need_dropzone
    async def test_hint_stage_swaps_with_tree(self):
        """载入着再拖入：提示必须让树上位——它在 Stack 下层，不让位就永远看不见。"""
        await self.load()
        app = self.app
        app._drag_entered()
        self.assertTrue(app.empty_hint.visible)
        self.assertFalse(app.tree_list.visible)
        self.assertEqual(app._hint_title.value, "松手即载入")
        app._drag_exited()
        self.assertTrue(app.tree_list.visible, "拖放退出后旧树没回来")
        self.assertFalse(app.empty_hint.visible)


# ── 松手载入：快门清场与失败还原 ──────────────────────────────────────────
class DropLoadTest(RealFletUiTest):
    async def test_release_clears_before_parse(self):
        """松手即清场：解析在后台跑，这期间旧树必须先撤、"正在解析"当场接上，
        不能让用户对着一动不动的旧树干等。"""
        await self.load()
        app = self.app
        seen = {}

        async def hold(path):
            seen["controls"] = len(app.tree_list.controls)
            seen["visible"] = app.tree_list.visible
            seen["hint"] = (app.empty_hint.visible, app._hint_title.value)
            await asyncio.Event().wait()   # 一直挂着，由测试收场

        app._load_bin = hold
        self.addCleanup(delattr, app, "_load_bin")
        task = asyncio.create_task(app._on_dropped(_DropEvt([str(self.fixture)])))
        await support.pump_until(lambda: seen, what="松手清场")
        self.assertEqual(seen["controls"], 0, "松手后旧树还挂着——要先清场再解析")
        self.assertIs(seen["visible"], False)
        self.assertEqual(seen["hint"], (True, "正在解析…"), "松手后没有当场反馈")
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def test_failed_drop_restores_old_tree(self):
        """解析失败：旧数据没动过，旧树要原样回来，状态栏不能被坏文件冲掉。"""
        await self.load()
        app = self.app
        prev_status = app.status_text.value
        broken = self.tmp / "broken.bin"
        broken.write_bytes(b"not a bin")
        await app._on_dropped(_DropEvt([str(broken)]))
        self.assertEqual(app.input_file, str(self.fixture), "坏文件把载入状态冲掉了")
        self.assertTrue(app.tree_list.controls and app.tree_list.visible,
                        "解析失败后旧树没回来")
        self.assertFalse(app.empty_hint.visible, "解析失败后空态提示冒出来了")
        self.assertEqual(app.status_text.value, prev_status,
                         f"状态栏卡在 {app.status_text.value!r}，没回到解析前")
        self.assertEqual(app._hint_title.value, "尚未加载文件")


# ── 提示横幅与帮助对话框 ──────────────────────────────────────────────────
class SnackTest(RealFletUiTest):
    async def test_ok_snack_lifecycle(self):
        """居中下方浮起的小卡片：定位层不吞点击，成功横幅到点自退。"""
        app = self.app
        app._snack("已载出: demo.bin")
        holder = app._snack_overlay.controls[0]
        self.assertIs(holder.content, app._snack_toast,
                      "横幅应是单个小卡片而不是整宽横幅")
        self.assertEqual((holder.left, holder.right), (0, 0))
        self.assertIsNotNone(holder.alignment, "横幅要在窗口底部居中")
        self.assertIsNone(holder.bgcolor, "定位层有底色会吞掉整条横带的点击")
        self.assertEqual(app._snack_toast.opacity, 0.0, "初始态没给动画起点")
        await support.pump_until(
            lambda: app._snack_toast.opacity == 1.0
            and app._snack_toast.offset == ft.Offset(0, 0), what="横幅浮起到位")
        await support.pump_until(
            lambda: app._snack_overlay not in app.page.overlay,
            timeout=5.0, what="成功横幅自动消失")

    async def test_error_snack_only_yields_to_its_own_seq(self):
        app = self.app
        seq = app._snack("报错横幅", error=True)
        await support.pump_until(lambda: app._snack_toast.opacity == 1.0)
        app._dismiss_snack(seq - 1)
        self.assertIn(app._snack_overlay, app.page.overlay,
                      "错误横幅不该被别的序号消掉")
        app._dismiss_snack(seq)
        await support.pump_until(lambda: app._snack_overlay not in app.page.overlay,
                                 timeout=3.0, what="手动关闭后错误横幅移除")

    def test_help_dialog(self):
        self.app._help(None)
        self.assertTrue(self.page.dialogs, "帮助对话框未弹出")
        self.assertIsNotNone(self.page.dialogs[0].content, "帮助内容为空")
        self.page.pop_dialog()
        self.assertEqual(self.page.dialogs, [])


# ── 解析过程反馈：慢路径才有横幅与进度条 ──────────────────────────────────
class ParseFeedbackTest(RealFletUiTest):
    async def test_busy_card_only_for_still_running_parse(self):
        app = self.app
        app._show_parsing_busy(999, "x.bin")   # 上一轮解析的迟到触发
        self.assertIsNone(app._busy_seq)
        self.assertEqual(self.page.overlay, [], "快路径不该被弹横幅")
        self.assertFalse(app.parse_progress.visible, "没到揭示时刻就把进度条亮出来了")

        app._parsing_gen = 7                   # 场景：7 这一代还在途中
        app._show_parsing_busy(7, "x.bin")
        self.assertEqual(app._busy_seq, app._snack_seq, "慢路径没弹\"正在解析\"")
        # 进度条和这条横幅同一时刻上场；此刻还不知道总数 → 不定态
        self.assertTrue(app.parse_progress.visible)
        self.assertIsNone(app.parse_progress.value)
        self.assertTrue(app._snack_is_showing(app._busy_seq))
        await asyncio.sleep(0.3)
        self.assertIn(app._snack_overlay, self.page.overlay, "busy 卡片不该自己消失")
        app._dismiss_snack(app._busy_seq)
        await support.pump_until(lambda: app._snack_overlay not in self.page.overlay,
                                 timeout=3.0)
        app._end_parse_feedback()
        self.assertFalse(app.parse_progress.visible, "解析收尾后进度条没收回去")

    def test_progress_frames_drive_status_bar(self):
        """没到揭示时刻不画、total=0 是"还不知道总数"（不定态，不能写 0/0）、
        过期代号的迟到帧不许再改状态栏。"""
        app = self.app
        app._parsing_gen, app._parse_shown = 21, False
        app._show_parse_progress(21, 5, 400, "读文件头")
        self.assertFalse(app.parse_progress.visible, "还没到揭示时刻就把进度条画出去了")
        app._parse_shown = True
        app._show_parse_progress(21, 5, 400, "读文件头")
        self.assertEqual(app.parse_progress.value, 5 / 400)
        self.assertEqual(app.status_meta.value, "读文件头 5/400")
        app._show_parse_progress(21, 30, 0, "扫描目录")
        self.assertIsNone(app.parse_progress.value, "total=0 该是不定态，不该给个具体比例")
        self.assertEqual(app.status_meta.value, "扫描目录 30 个")
        app._show_parse_progress(22, 399, 400, "读文件头")   # 上一轮解析的迟到帧
        self.assertEqual(app.status_meta.value, "扫描目录 30 个", "过期代号的帧还在改状态栏")
        app._end_parse_feedback()
        self.assertFalse(app.parse_progress.visible)
        self.assertIsNone(app.parse_progress.value, "收尾没清干净")


# ── 键盘：焦点在列表项上时快捷键仍然生效 ──────────────────────────────────
class KeyboardNavTest(RealFletUiTest):
    async def _load_multiframe(self):
        await self.load()
        await self.select(self.app, "CHR_翠_无_私服")   # 多帧底图才有帧可翻
        self.assertGreater(len(self.app.base_imgs), 1)

    async def test_arrows_work_while_control_holds_focus(self):
        """列表项/按钮点过之后会持有焦点，e.control 不为空是常态而不是"在打字"，
        拿它当"焦点被占用"判断会把 ←/→ 全吞掉。"""
        await self._load_multiframe()
        app = self.app
        before = app.base_idx
        await app._on_keyboard(_Key("Right", control=object()))
        self.assertNotEqual(app.base_idx, before, "控件持有焦点时方向键切不了帧")
        await app._on_keyboard(_Key("Left", control=object()))
        self.assertEqual(app.base_idx, before, "往回切没恢复")

    async def test_escape_pops_dialog(self):
        self.app._help(None)
        await self.app._on_keyboard(_Key("Escape"))
        self.assertEqual(self.page.dialogs, [])

    async def test_zero_resets_both_viewers(self):
        await self._load_multiframe()
        self.app._reset_zoom(None)
        await support.pump_until(
            lambda: any(m == "reset" for m, _a in self.page.session.calls),
            what="复位缩放真正下发")

    async def test_real_key_events_do_not_crash(self):
        for key in ("Left", "Right", "Escape", "0", "l"):
            ev = ft.KeyboardEvent(name=key, data="", control=None, key=key,
                                  shift=False, ctrl=False, alt=False, meta=False)
            await self.app._on_keyboard(ev)


# ── 对话框目录记忆：导入/导出各自记各自的 ─────────────────────────────────
class DialogDirMemoryTest(RealFletUiTest):
    async def _prepare(self):
        await self.load()
        self.toggle(self.app, ("role", "翠"))
        self.toggle(self.app, ("outfit", "翠/私服"))
        await self.select(self.app, "CHR_翠_基_私服")
        (self.tmp / "export").mkdir()

    async def test_import_and_export_dirs_stay_separate(self):
        """选完导出目录后，再开 bin 不能被带去导出那边。"""
        await self._prepare()
        app = self.app
        seen = {}

        async def fake_pick(**kw):
            seen["import"] = kw.get("initial_directory")
            return None

        async def fake_save(**kw):
            seen["export"] = kw.get("initial_directory")
            return str(self.tmp / "export" / "one.png")

        app.file_picker.pick_files = fake_pick
        app.file_picker.save_file = fake_save
        try:
            await app._open_bin(None)
            await app._save_current(None)
        finally:
            del app.file_picker.pick_files
            del app.file_picker.save_file
        self.assertEqual(seen["import"], str(self.fixture), "导入对话框初始目录不对")
        self.assertIsNone(seen["export"], "导出目录首次应为系统默认")
        self.assertEqual(app.export_dir, str(self.tmp / "export"))
        self.assertEqual(app.import_dir, str(self.fixture), "导出动作不该改写导入记忆目录")

        seen.clear()
        app.file_picker.pick_files = fake_pick
        try:
            await app._open_bin(None)
        finally:
            del app.file_picker.pick_files
        self.assertEqual(seen["import"], str(self.fixture), "导出之后导入初始目录被带跑")

    async def test_dirs_persist_for_next_start(self):
        """退出时落盘成 dialogs 段（打包态只有 CLOSE 事件来得及，_close 也写）。"""
        await self._prepare()
        app = self.app
        app.export_dir = str(self.tmp / "export")
        app._write_dialog_dirs()
        self.assertEqual(st.dialog_dirs(),
                         {"import": str(self.fixture), "export": str(self.tmp / "export")})


# ── 窗口事件：拖动/缩放/最大化都要落盘 ────────────────────────────────────
class WindowEventTest(RealFletUiTest):
    async def test_resize_and_maximize_are_remembered(self):
        """拖动和缩放都不走关闭流程：只存"退出时"会漏掉整个「拖过一遍再关」。"""
        self.assertTrue(callable(self.page.window.on_event), "窗口事件没挂上")
        w = self.page.window
        w.width, w.height, w.left, w.top = 1500, 900, 210, 130
        w.on_event(_WinEvt(ft.WindowEventType.RESIZED))
        await support.pump_until(
            lambda: st.window_prefs().get("width") == 1500
            and st.window_prefs().get("left") == 210,
            timeout=st.SAVE_DELAY_S + 3.0, what="缩放/拖动落盘")

        # 最大化只改标记：这会儿的宽高是整个显示器的框，不能把上次几何冲掉
        w.on_event(_WinEvt(ft.WindowEventType.MAXIMIZE))
        await support.pump_until(lambda: st.window_prefs().get("maximized") is True,
                                 timeout=st.SAVE_DELAY_S + 3.0, what="最大化状态落盘")
        saved = st.window_prefs()
        self.assertEqual((saved.get("width"), saved.get("left")), (1500, 210),
                         f"最大化把上次的几何冲掉了: {saved}")

    async def test_theme_rebuild_keeps_window_state(self):
        w = self.page.window
        w.width, w.height = 1400, 850
        w.on_event(_WinEvt(ft.WindowEventType.MAXIMIZE))
        await support.pump_until(lambda: st.window_prefs().get("maximized") is True,
                                 timeout=st.SAVE_DELAY_S + 3.0)
        await self.app._toggle_theme(None)
        self.assertTrue(callable(w.on_event), "换主题后窗口事件掉了")
        self.assertIs(st.window_prefs().get("maximized"), True, "换主题把窗口记忆洗掉了")


# ── argv 自动载入（拖到程序图标那条通道）──────────────────────────────────
class ArgvLaunchTest(RealFletUiTest):
    async def test_argv_autoloads_first_target(self):
        """目录记忆要跨重启：启动时从 dialogs 段接上，再被实际载入覆盖导入侧。"""
        exp = self.tmp / "exp"
        exp.mkdir()
        st.save_dialog_dirs(str(self.tmp), str(exp))
        old_argv, sys.argv = sys.argv, ["prog", str(self.fixture)]
        self.addCleanup(setattr, sys, "argv", old_argv)
        page2 = self.new_page()
        app2 = await app_main(page2)
        self.assertEqual(app2.input_file, str(self.fixture), "argv 自动载入失效了")
        self.assertTrue(page2.added, "argv 启动路径未建树")
        self.assertEqual(app2.export_dir, str(exp), "重启后导出目录没接上")
        self.assertEqual(app2.import_dir, str(self.fixture))

    async def test_unknown_argv_still_says_something(self):
        """拖了个不存在/不支持的东西进来也要吭一声：不能静默吞掉。"""
        old_argv, sys.argv = sys.argv, ["prog", str(self.tmp / "gone.bin")]
        self.addCleanup(setattr, sys, "argv", old_argv)
        page2 = self.new_page()
        app2 = await app_main(page2)
        self.assertIsNotNone(app2)
        await support.pump_until(lambda: bool(page2.overlay), what="无法打开的横幅挂上")


# ── 载出 / 重载：回到初始状态的回归防线 ──────────────────────────────────
class UnloadReloadTest(RealFletUiTest):
    async def test_unload_returns_to_initial_state(self):
        await self.load()
        app = self.app
        await app._unload_bin(None)
        self.assertIsNone(app.input_file, "载出后 input_file 未清空")
        self.assertEqual(app.file_infos, [])
        self.assertEqual(app.hierarchy, {})
        self.assertIsNone(app.selected_filename)
        self.assertEqual(app.base_imgs, [])
        self.assertEqual(app.base_idx, 0)
        self.assertFalse(app.tree_list.visible, "载出后树还挂着")
        self.assertEqual(app.tree_list.controls, [])
        self.assertTrue(app.empty_hint.visible, "载出后空状态提示没恢复（回不到初始态）")
        self.assertEqual(app.file_chip.content.value, "未载入文件")
        self.assertFalse(app.unload_btn.visible, "没载东西时载出是死按钮")
        self.assertTrue(app.save_btn.disabled)
        self.assertTrue(app.batch_btn.disabled)
        self.assertEqual(app.status_text.value, "就绪")
        self.assertEqual(app.role_thumb_cache, {}, "载出后缩略图控件字典没清")

        # 再载入要能照常建树、载出按钮回来
        await self.load()
        self.assertTrue(app.tree_list.visible, "载出后无法再次载入")
        self.assertFalse(app.empty_hint.visible)
        self.assertTrue(app.unload_btn.visible)

    async def test_unload_is_idempotent(self):
        await self.app._unload_bin(None)
        await self.app._unload_bin(None)   # 第二次载出同样干净、不抛错
        self.assertIsNone(self.app.input_file)
        self.assertTrue(self.app.empty_hint.visible)

    async def test_unload_during_parse_hides_progress(self):
        """解析中途按下载出：进度条不能跟着带进初始态。"""
        app = self.app
        await self.load()            # 没载入东西时载出走早退，验不到复位那条路
        app._parsing_gen = app._state_gen
        app._parse_shown = True
        app.parse_progress.visible = True
        app.parse_progress.value = 0.5
        await app._unload_bin(None)
        self.assertFalse(app.parse_progress.visible)
        self.assertIsNone(app.parse_progress.value)
        self.assertEqual(app.status_text.value, "就绪")
        self.assertEqual(app.status_meta.value, "")


# ── 角色逐条错峰浮入 ──────────────────────────────────────────────────────
class StaggerTest(RealFletUiTest):
    async def test_roles_start_transparent_and_settle(self):
        """载入的确认长在内容自己身上：起步全是透明的外壳，链子把每条推到终点。"""
        app = self.app
        ok = await app._load_bin(str(self.fixture))
        self.assertTrue(ok)
        # _load_bin 返回时错峰链只排上了计时器，第一帧还没推——起点必须可见
        wraps = [c for c in app.tree_list.controls if isinstance(c, ft.Container)]
        self.assertEqual(len(wraps), len(app.hierarchy))
        self.assertTrue(all(w.opacity == 0.0 for w in wraps), "错峰起步没给动画留起点")
        await support.pump_until(
            lambda: all(w.opacity == 1.0 and w.offset == ft.Offset(0, 0) for w in wraps),
            timeout=5.0, what="全部角色浮到位")

    @_need_dropzone
    async def test_drop_interrupt_settles_every_row(self):
        """错峰中途又拖进来：剩下的必须直接落到终点，不能留半透明的行。"""
        await self.load()
        app = self.app
        wraps = [c for c in app.tree_list.controls if isinstance(c, ft.Container)]
        app._stagger_i = 0
        for w in wraps:
            w.opacity = 0.0
        app._drag_entered()
        app._stagger_step()
        self.assertTrue(all(w.opacity == 1.0 for w in wraps),
                        "拖入打断错峰时留下了半透明的角色")
        app._drag_exited()


# ── 缓存：换资源不串、切回命中、重解不发生 ───────────────────────────────
class AbResourceCacheTest(RealFletUiTest):
    def _watch_decodes(self):
        """把首帧解码计数挂到实例上（树里三级缩略图都走这条）。"""
        app = self.app
        decoded = []
        real_read = app._read_first_frame
        app._read_first_frame = lambda info, data=None, header=None: (
            decoded.append(info["filename"]), real_read(info, data, header))[1]
        self.addCleanup(delattr, app, "_read_first_frame")
        return decoded

    async def test_same_names_do_not_collide_and_switchback_hits(self):
        """键里带资源指纹（路径+大小+mtime）：换资源时不清缓存，同名动作
        各取各的，切回上一份资源还能命中。"""
        decoded = self._watch_decodes()
        app = self.app
        await self.load()
        self.expand_all(app)
        await support.pump_until(lambda: role_thumb_src(app, "翠"), what="角色缩略图上屏")
        await self.select(app, "CHR_翠_喜_私服")
        base_a, strip_a = app.base_image.src, strip_srcs(app)
        role_a = role_thumb_src(app, "翠")

        other = support.build_fixture(salt=90)   # 文件名一模一样、画面不同
        self.addCleanup(shutil.rmtree, other, True)
        await self.load(app, other)
        await support.drain_thumb_pool()
        self.assertIsNotNone(role_thumb_src(app, "翠"), "新资源的角色缩略图没出来")
        self.assertNotEqual(role_thumb_src(app, "翠"), role_a,
                            "换资源后角色缩略图还是上一份的（同名串图）")
        await self.select(app, "CHR_翠_喜_私服")
        self.assertNotEqual(app.base_image.src, base_a, "换资源后同一动作的底图还是上一份的")
        self.assertNotEqual(strip_srcs(app), strip_a, "换资源后差分条还是上一份的")

        # 切回 A：缩略图与整帧都该命中缓存，不再解码
        n_back = len(decoded)
        await self.load(app, self.fixture)
        await support.drain_thumb_pool()
        await self.select(app, "CHR_翠_喜_私服")
        self.assertEqual(app.base_image.src, base_a, "切回 A 后底图没命中缓存")
        self.assertEqual(strip_srcs(app), strip_a, "切回 A 后差分条没命中缓存")
        self.assertEqual(len(decoded), n_back, f"切回 A 又解码了 {len(decoded) - n_back} 张")

    async def test_rebuild_and_reexpand_redecode_nothing(self):
        """换主题整树重建、整批收起再展开：解过的不能再解一遍。"""
        decoded = self._watch_decodes()
        app = self.app
        await self.load()
        self.expand_all(app)
        await support.drain_thumb_pool()
        first = len(decoded)
        self.assertGreater(first, 0, "展开后一张缩略图都没解码，这条断言没走到路径")

        await app._toggle_theme(None)
        await support.drain_thumb_pool()
        self.assertEqual(len(decoded), first, f"重建树又解码了 {len(decoded) - first} 张")

        for role in sorted(app.hierarchy):
            state, tog = app._tree_toggles[("role", role)]
            if state["open"]:
                tog(None)
        self.expand_all(app)
        await support.drain_thumb_pool()
        self.assertEqual(len(decoded), first,
                         f"收起再展开又解码了 {len(decoded) - first} 张")


class FrameCacheTest(RealFletUiTest):
    async def test_switching_actions_does_not_redecode_frames(self):
        """来回切动作不重解整帧：计数挂在真正干整包 zlib 的解码上。
        用一份没被前面流程碰过的新资源——帧缓存跨实例共享，拿旧资源测
        会直接命中，计数永远是 0。"""
        import fvpapp.resources as res
        fixt = support.build_fixture(salt=77)
        self.addCleanup(shutil.rmtree, fixt, True)
        await self.load(target=fixt)
        calls = []
        real_list = res.hzc_data_to_pil_list
        patcher = mock.patch.object(
            res, "hzc_data_to_pil_list",
            side_effect=lambda data, header: (calls.append(1), real_list(data, header))[1])
        patcher.start()
        self.addCleanup(patcher.stop)
        app = self.app
        first = self.info(app, "CHR_翠_基_私服")
        second = self.info(app, "CHR_翠_喜_私服")
        await app._select_action(first)
        n_first = len(calls)
        await app._select_action(second)
        n_second = len(calls)
        await app._select_action(first)
        self.assertGreaterEqual(n_first, 2, f"第一次选动作只解了 {n_first} 次，没走到整帧解码")
        self.assertGreater(n_second, n_first, "换动作没有解新动作")
        self.assertEqual(len(calls), n_second,
                         f"切回已看过的动作又整帧解码了 {len(calls) - n_second} 次")


class CrossSessionThumbTest(RealFletUiTest):
    async def test_new_app_hits_disk_without_decoding(self):
        """换一个 App 实例、同一份资源、同一个缓存目录（等价于下次开会话）：
        树里三级缩略图都应直接命中磁盘，首帧解码一次都不该再走。
        内存缓存先清掉——否则验的是内存而不是磁盘。"""
        await self.load()
        self.expand_all(self.app)
        await support.drain_thumb_pool()
        support.reset_cache_singletons()

        app2 = self.new_app()
        decoded2 = []
        real_read = app2._read_first_frame
        app2._read_first_frame = lambda info, data=None, header=None: (
            decoded2.append(info["filename"]), real_read(info, data, header))[1]
        await app2._load_bin(str(self.fixture))
        for role in sorted(app2.hierarchy):
            state, tog = app2._tree_toggles[("role", role)]
            if not state["open"]:
                tog(None)
        for role in sorted(app2.hierarchy):
            for outfit in sorted(app2.hierarchy[role]):
                state, tog = app2._tree_toggles[("outfit", f"{role}/{outfit}")]
                if not state["open"]:
                    tog(None)
        await support.drain_thumb_pool()
        self.assertEqual(decoded2, [],
                         f"跨会话没命中磁盘缓存，又解码了 {len(decoded2)} 张: {decoded2[:5]}")


class IndexCacheTest(RealFletUiTest):
    def _watch_parse(self):
        import fvpapp.resources as res
        calls = []
        real = res.parse_dir_infos
        res.parse_dir_infos = lambda path, progress=None: (
            calls.append(str(path)), real(path, progress=progress))[1]
        self.addCleanup(setattr, res, "parse_dir_infos", real)
        return calls

    async def test_reload_reuses_index_and_invalidates_on_change(self):
        d = self.tmp / "idx"
        d.mkdir()
        support.make_hzc(d / "CHR_翠_基_私服.hzc")
        await self.load(target=d)
        calls = self._watch_parse()

        app2 = self.new_app()
        await app2._load_bin(str(d))
        self.assertEqual(calls, [], "索引已缓存却又重新解析了")
        self.assertEqual(set(app2.hierarchy), set(self.app.hierarchy),
                         "索引缓存回来的层级与首次不一致")
        self.assertEqual(len(app2.file_infos), len(self.app.file_infos))

        # 改一个文件（指纹变）→ 必须重新解析，不能吃旧索引
        support.make_hzc(d / "CHR_翠_喜_私服.hzc")
        calls.clear()
        app3 = self.new_app()
        await app3._load_bin(str(d))
        self.assertEqual(calls, [str(d)], "目录变更后索引没失效")


# ── 大资源载入的进度反馈 ──────────────────────────────────────────────────
class BigFixtureProgressTest(RealFletUiTest):
    async def test_slow_parse_throttles_and_reports_stages(self):
        """小资源在进度条亮起来之前就解析完了，那条路全程安静；只有慢资源才把
        "正在解析"和进度条一起亮出来，且四百次回调只换来个位数帧。"""
        import fvpapp.resources as res
        big = support.build_large_fixture(400)
        self.addCleanup(shutil.rmtree, big, True)
        raw, frames = [], []
        real_scan = res.parse_dir_infos

        def counting_scan(path, progress=None):
            def inner(done, total, stage):
                raw.append((done, total, stage))
                progress(done, total, stage)
            return real_scan(path, progress=inner)

        res.parse_dir_infos = counting_scan
        self.addCleanup(setattr, res, "parse_dir_infos", real_scan)
        real_show = self.app._show_parse_progress

        def spy(gen, done, total, stage):
            frames.append((done, total, stage, self.app.parse_progress.visible))
            real_show(gen, done, total, stage)

        self.app._show_parse_progress = spy
        self.addCleanup(delattr, self.app, "_show_parse_progress")
        with mock.patch.object(res, "BUSY_DELAY", 0):   # 门槛降到 0 才保证走到慢路径
            ok = await self.app._load_bin(str(big))
        self.assertTrue(ok)

        stages = {s for _d, _t, s in raw}
        self.assertIn("扫描目录", stages)
        self.assertIn("读文件头", stages)
        self.assertEqual(raw[-1][0], raw[-1][1], f"最后一帧没报满: {raw[-1]}")
        self.assertNotEqual(raw[-1][0], 0)
        head = [(d, t) for d, t, s in raw if s == "读文件头"]
        self.assertTrue(all(b[0] >= a[0] for a, b in zip(head, head[1:])),
                        "读文件头的计数倒着走了")
        self.assertEqual(len({t for _d, t in head}), 1,
                         "同一阶段里 total 会变，进度条会来回跳")
        self.assertGreaterEqual(len(raw), 400)
        self.assertLess(len(frames), 60,
                        f"引擎报了 {len(raw)} 次、UI 收到 {len(frames)} 帧，节流没生效")
        self.assertTrue(any(f[3] for f in frames), "慢资源从头到尾没亮过进度条")
        self.assertFalse(self.app.parse_progress.visible)
        self.assertIsNone(self.app.parse_progress.value, "解析完了进度条还挂着（残影）")
        self.assertEqual(self.app.status_meta.value,
                         f"1 角色 · {len(self.app.file_infos)} 图像",
                         f"状态栏停在解析文案: {self.app.status_meta.value}")


# ── 批量导出的状态栏控件 ──────────────────────────────────────────────────
class ExportControlsTest(RealFletUiTest):
    """静止时都藏着，导出中「取消」与进度条一起亮，收尾不许留残影。
    真正的导出流程在 tests/test_export_progress.py 里逐项验，这里只验装机与收起。"""

    def test_idle_hides_all_actions(self):
        self.assertFalse(self.app.export_cancel_btn.visible, "静止时「取消」不该露脸")
        self.assertFalse(self.app.status_fail_action.visible,
                         "没有失败项时不该出现「查看失败项」")
        self.assertIsNotNone(self.app.export_cancel_btn.on_click, "「取消」没接上 _cancel_export")
        self.assertIsNotNone(self.app.status_fail_action.on_click, "「查看失败项」没接上清单")

    def test_begin_update_finish_lifecycle(self):
        app = self.app
        gen = app._export_begin(4)
        self.assertTrue(app.parse_progress.visible)
        self.assertTrue(app.export_cancel_btn.visible)
        self.assertEqual(app.status_meta.value, "导出 0/4")
        app._show_export_progress(gen, 2, 4)
        self.assertAlmostEqual(app.parse_progress.value, 0.5)
        self.assertEqual(app.status_meta.value, "导出 2/4")
        app._finish_export(gen, 4, [], False, str(self.tmp))
        self.assertFalse(app.parse_progress.visible)
        self.assertFalse(app.export_cancel_btn.visible, "导出收尾没收起进度条/取消")
        self.assertIn("已导出 4 张", app.status_text.value)
        # 作废后的迟到帧不许再把进度条点亮
        app._show_export_progress(gen, 3, 4)
        self.assertFalse(app.parse_progress.visible, "过期导出帧又把进度条画回去了")


if __name__ == "__main__":
    unittest.main(verbosity=2)
