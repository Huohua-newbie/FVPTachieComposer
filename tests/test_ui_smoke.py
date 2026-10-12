"""真实 flet 下的 UI 冒烟测试：把整棵控件树建出来并驱动一遍主流程。

与 test_thumb_threading.py 的区别：那个用宽容的假 flet 验证线程契约，
这个用**真** flet 构造控件，任何拼错的属性名/类型都会在这里炸出来，
顺带断言"滚动条必须留在预留槽内"这类布局不变量（重合 bug 的回归防线）。

需要已安装 flet + Pillow：python3 tests/test_ui_smoke.py
"""
import asyncio
import struct
import sys
import tempfile
import zlib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

try:
    import flet as ft  # noqa: F401
    import PIL.Image  # noqa: F401
except Exception as ex:  # 环境缺依赖时跳过，不算失败
    print(f"UI_SMOKE_SKIP (缺依赖: {ex})")
    sys.exit(0)

# 真实控件的 update() 会沿 parent 链找 ft.Page 实例；这里把 page 解析改成
# 指向当前 FakePage，于是 app 里所有 update 调用都能真的走一遍（而不是被
# try/except 静默吞掉），同时不需要起会话。
import flet.controls.base_control as _bc  # noqa: E402

_CURRENT_PAGE = []
_bc.BaseControl.page = property(lambda self: _CURRENT_PAGE[-1] if _CURRENT_PAGE else None)


class FakeWindow:
    def __init__(self):
        self.width = 1280
        self.height = 760
        self.min_width = 1020
        self.min_height = 600
        self.title_bar_hidden = False
        self.icon = None
        self.maximized = False
        self.minimized = False

    async def destroy(self):
        pass


class FakeSession:
    """记录控件方法调用（InteractiveViewer.reset 之类走 session 的指令）。"""

    def __init__(self):
        self.calls = []

    async def invoke_method(self, target, method, args=None, timeout=None):
        self.calls.append((method, args))
        return None


class FakePage:
    """只覆盖 ComposerApp 用到的 page 接口；控件级 update() 由 app 自身 try 兜住。"""

    def __init__(self):
        _CURRENT_PAGE.append(self)
        self.session = FakeSession()
        self.window = FakeWindow()
        self.overlay = []
        self.services = None
        self.dialogs = []
        self.added = []
        self.updates = 0
        self.theme = None
        self.dark_theme = None
        self.theme_mode = None
        self.bgcolor = None
        self.padding = None
        self.spacing = None
        self.title = None
        self.on_keyboard_event = None

    def add(self, *controls):
        self.added.extend(controls)

    def update(self, *a, **k):
        self.updates += 1

    def clean(self):
        self.added.clear()

    def show_dialog(self, dialog):
        self.dialogs.append(dialog)
        return True

    def pop_dialog(self):
        if self.dialogs:
            self.dialogs.pop()
        return None


def make_hzc(path, image_type=1, w=8, h=8, frames=1):
    head = bytearray(44)
    head[0:4] = b"hzc1"
    struct.pack_into("<H", head, 18, image_type)
    struct.pack_into("<H", head, 20, w)
    struct.pack_into("<H", head, 22, h)
    struct.pack_into("<I", head, 32, frames)
    bpp = 3 if image_type == 0 else 4
    need = w * h * bpp * (frames if image_type == 2 else 1)
    path.write_bytes(bytes(head) + zlib.compress(bytes((i * 7) % 256 for i in range(need))))


def build_fixture():
    tmp = Path(tempfile.mkdtemp(prefix="ui_smoke_"))
    for role in ("翠", "真紅"):
        for outfit in ("私服", "制服"):
            for action in ("基", "喜"):
                make_hzc(tmp / f"CHR_{role}_{action}_{outfit}.hzc")
                make_hzc(tmp / f"CHR_{role}_{action}_{outfit}_表情.hzc", image_type=2, frames=3)
    return tmp


def _walk(ctrl):
    """递归走完一棵真实 flet 控件树（controls 列表 + 单个 content 子控件）。"""
    yield ctrl
    for child in getattr(ctrl, "controls", None) or []:
        yield from _walk(child)
    inner = getattr(ctrl, "content", None)
    if isinstance(inner, ft.Control):
        yield from _walk(inner)


def _right_pad(lv):
    p = lv.padding
    if p is None:
        return 0, 0
    return (getattr(p, "right", 0) or 0), (getattr(p, "bottom", 0) or 0)


async def run():
    from FVPTachieComposerFlet import ComposerApp, main as app_main

    page = FakePage()
    app = ComposerApp(page)
    assert page.added, "页面根控件未添加"

    # 主题切换：两套主题都要能整树重建
    await app._toggle_theme(None)
    await app._toggle_theme(None)

    tmp = build_fixture()
    await app._load_bin(str(tmp))
    assert app.tree_list.visible, "树未显示"

    # 布局不变量：滚动条必须走预留槽，不能压在内容上
    for name, lv in (("tree_list", app.tree_list), ("thumb_list", app.thumb_list)):
        right, bottom = _right_pad(lv)
        assert right >= 8 or bottom >= 8, f"{name} 未为滚动条预留槽位 (right={right}, bottom={bottom})"
    sb = page.theme.scrollbar_theme
    assert sb.track_visibility is False, "滚动条轨道不应占视觉重量"
    assert (sb.thickness or 0) <= 10, f"滚动条过粗: {sb.thickness}"

    # 拖放入口：默认必须关闭——官方 flet 客户端没有 desktop_drop 插件，
    # 未 build 的运行时塞 Dropzone 会让整棵控件树渲染失败。
    from FVPTachieComposerFlet import DROPZONE_ON, pick_drop_path
    assert DROPZONE_ON is False, "测试环境里拖放不该被启用"

    bin_a, bin_b = str(tmp / "graph_a.bin"), str(tmp / "graph_b.bin")
    Path(bin_a).touch()
    assert pick_drop_path([str(tmp), bin_a]) == bin_a, ".bin 应优先于文件夹"
    assert pick_drop_path([bin_a, str(tmp)]) == bin_a
    assert pick_drop_path([str(tmp)]) == str(tmp), "只拖文件夹时应返回该目录"
    assert pick_drop_path([bin_b]) == bin_b, "不存在的 bin 仍要交给 _load_bin 报错"
    assert pick_drop_path(["", None]) is None, "空路径应返回 None"

    # 图像源必须是字符串 data URI：src 给 bytes 只在构造时有效，
    # 之后 `img.src = bytes` 不会被重新编码，客户端当成路径解析 → 预览区空白。

    roots = page.added if isinstance(page.added, list) else [page.added]
    nodes = [c for r in roots for c in _walk(r)]
    bad_img = [c for c in nodes
               if getattr(c, "image", None) is not None
               and not isinstance(c.image, ft.DecorationImage)]
    assert not bad_img, \
        f"Container.image 只认 DecorationImage，塞 ft.Image 会被静默丢掉: {type(bad_img[0].image)}"

    # ListTile.trailing 里的 Row 默认撑满主轴，会把标题/副标题挤成竖排
    for c in nodes:
        if isinstance(c, ft.ListTile) and isinstance(c.trailing, ft.Row):
            assert c.trailing.tight, "ListTile.trailing 的 Row 必须 tight=True"

    # 设计语言落地检查：和纸是页面最底层的纤维纹理，琉璃是压在纸上的渐变玻璃片。
    # 二者都只是"能看见"才有意义——没有这两条，所谓和纸/琉璃就是文案。
    washi = [c for c in nodes
             if getattr(c, "image", None) is not None
             and c.image.repeat == ft.ImageRepeat.REPEAT
             and c.image.src == app._washi]
    assert washi, "页面底层缺少和纸纹理"
    glass = [c for c in nodes if getattr(c, "gradient", None) is not None]
    assert len(glass) >= 3, f"面板没有琉璃质感（渐变面板仅 {len(glass)} 个）"
    for c in glass:
        assert c.border.top.color == app.ink["glass_hi"], "琉璃片上沿缺高光边"

    # 展开到动作层并选中，跑完 底图 → 差分 → 合成 全链路
    role_col = app.tree_list.controls[0]
    role_header, role_body = role_col.controls[0], role_col.controls[1]
    role_header.on_click(None)
    outfit_col = role_body.controls[0]
    outfit_header, outfit_body = outfit_col.controls[0], outfit_col.controls[1]
    outfit_header.on_click(None)
    assert outfit_body.controls, "动作层为空"
    await asyncio.sleep(0.4)  # 等后台缩略图解码回推

    action_tile = outfit_body.controls[0]
    info = next(i for i in app.file_infos
                if i["filename"] == "CHR_翠_基_私服")

    # 层级：第三级必须比第二级更深，且带一条导轨线，否则"服装/动作"看着像同级
    from FVPTachieComposerFlet import INDENT_ACTION, INDENT_OUTFIT
    assert INDENT_ACTION > INDENT_OUTFIT, "动作层缩进必须大于服装层"
    assert action_tile.margin.left == INDENT_ACTION
    assert outfit_header.content_padding.left == INDENT_OUTFIT
    rail, hl = action_tile.content.controls
    assert rail.width == 1, "动作层缺少导轨线"
    assert hl.animate is not None, "动作选中态必须有过渡，不能只是闪一下变色"

    app._select_action(info)
    assert app.base_imgs, "底图未加载"
    assert app.part_imgs, "差分部件未加载"
    assert app.composed_img is not None, "合成结果为空"
    assert app.thumb_list.visible, "缩略图条未显示"
    # 选中态由 filename 决定，不是"点了哪个控件"——所以断言要按文件名找回那一行
    sel = next(e for e in app._action_rows if e["filename"] == info["filename"])
    assert sel["hl"].bgcolor == app.ink["sel"], "选中行没有高亮底色"
    assert sel["hl"].border.left.color == app.ink["accent"], "选中行没有左侧标记条"
    assert sel["hl"].content.title.weight == ft.FontWeight.W_700, "选中行标题没有加粗"

    # 选中态互斥：换选后上一行必须整体复原（底色/标记条/字重），不能两行同时亮
    other = next((e for e in app._action_rows
                  if e["filename"] != info["filename"]), None)
    if other is not None:
        info2 = next(i for i in app.file_infos if i["filename"] == other["filename"])
        app._select_action(info2)
        assert other["hl"].bgcolor == app.ink["sel"], "新选中的行没有亮起"
        assert sel["hl"].bgcolor == "#00000000", "上一行的选中态没被清掉"
        assert sel["hl"].content.title.weight == ft.FontWeight.W_400, "上一行标题仍是加粗"
        app._select_action(info)  # 换回来，后面的用例继续用这一张
    # 图像源必须是字符串 data URI：src 给 bytes 只在构造时有效，
    # 之后 `img.src = bytes` 不会被重新编码，客户端当成路径解析 → 预览区空白。
    for name in ("base_image", "result_image"):
        src = getattr(app, name).src
        assert isinstance(src, str) and src.startswith("data:image/"), \
            f"{name}.src 不是 data URI，更新后不会显示: {type(src)}"
    tile_src = app.thumb_list.controls[0].content.src
    assert isinstance(tile_src, str), "差分缩略图 src 必须是字符串"

    # 换主题必须保留现场：展开的角色/服装、选中的差分、底图与合成预览
    app._pick_part(min(2, len(app.part_imgs) - 1))
    kept_idx = app.part_idx

    # 差分角标只做编号：选中与否都不能变蓝，否则 28 格里有一格在抢画面。
    badges = {t.badge.bgcolor for t in app.thumb_list.controls}
    assert badges == {app.ink["badge_bg"]}, f"角标颜色被选中态带跑了: {badges}"
    picked = app.thumb_list.controls[kept_idx]
    assert picked.border.top.color == app.ink["accent"], "选中的差分没有描边提示"
    assert picked.animate is not None, "差分切换应有过渡"

    snapshot = app.composed_img
    await app._toggle_theme(None)
    assert app.is_dark is True
    assert app.tree_list.controls[0].controls[1].visible is True, "换主题把已展开的角色收起了"
    assert app.tree_list.controls[0].controls[1].controls[0].controls[1].visible is True, \
        "换主题把已展开的服装收起了"
    assert app.part_idx == kept_idx, "换主题后选中的差分帧被重置"
    assert app.composed_img is snapshot, "换主题重新读盘重算了（应复用缓存）"
    assert len(app.thumb_list.controls) == len(app.part_imgs), "换主题后差分条没重建"
    for name in ("base_image", "result_image"):
        src = getattr(app, name).src
        assert isinstance(src, str) and src.startswith("data:image/"), \
            f"换主题后 {name}.src 丢了，预览区会空"
    assert app.save_btn.disabled is False and app.batch_btn.disabled is False, \
        "换主题后导出按钮被禁用"
    await app._toggle_theme(None)
    assert app.is_dark is False and app.composed_img is snapshot

    # 导入 / 导出各自记目录：选完导出目录后，再开 bin 不能被带去导出那边
    seen = {}

    async def fake_pick(**kw):
        seen["import"] = kw.get("initial_directory")
        return None

    async def fake_save(**kw):
        seen["export"] = kw.get("initial_directory")
        return str(tmp / "export" / "one.png")

    app.file_picker.pick_files = fake_pick
    app.file_picker.save_file = fake_save
    (tmp / "export").mkdir(exist_ok=True)
    await app._open_bin(None)
    await app._save_current(None)
    assert seen["import"] == str(tmp), f"导入对话框初始目录不对: {seen['import']}"
    assert seen["export"] is None, f"导出对话框被导入目录污染: {seen['export']}"
    assert app.export_dir == str(tmp / "export"), app.export_dir
    assert app.import_dir == str(tmp), "导出动作改写了导入记忆目录"
    seen.clear()
    await app._open_bin(None)
    assert seen["import"] == str(tmp), "导出之后导入对话框的初始目录被带跑"

    # 帧导航 / 部件切换 / 缩放复位
    if len(app.base_imgs) > 1:
        app._next_frame(None)
        app._prev_frame(None)
    app._pick_part(min(2, len(app.part_imgs) - 1))
    app._reset_zoom(None)
    await asyncio.sleep(0.1)
    assert any(m == "reset" for m, _a in page.session.calls), "复位缩放未真正下发"

    app._snack("冒烟横幅")
    assert len(page.overlay) == 1, "横幅未挂载"
    app._dismiss_snack(app._snack_seq)
    assert not page.overlay, "横幅未移除"
    app._snack("报错横幅", error=True)
    app._dismiss_snack(-1)
    assert len(page.overlay) == 1, "错误横幅不应被别的序号消掉"
    app._dismiss_snack(app._snack_seq)

    app._help(None)
    assert page.dialogs, "帮助对话框未弹出"
    dialog = page.dialogs[0]
    assert dialog.content is not None, "帮助内容为空"
    page.pop_dialog()

    # 键盘事件入口（不抛异常即可，具体分派依赖真实 key）
    if app.page.on_keyboard_event is not None:
        for key in ("Left", "Right", "Escape", "0", "l"):
            ev = ft.KeyboardEvent(name=key, data="", control=None, key=key,
                                  shift=False, ctrl=False, alt=False, meta=False)
            await app._on_keyboard(ev)

    # argv 自动载入路径同样要能建树
    page2 = FakePage()
    old_argv, sys.argv = sys.argv, ["prog", str(tmp)]
    try:
        app2 = await app_main(page2)
    finally:
        sys.argv = old_argv
    assert app2.input_file == str(tmp)
    assert page2.added, "argv 启动路径未建树"

    print("UI_SMOKE_OK")


asyncio.run(run())
