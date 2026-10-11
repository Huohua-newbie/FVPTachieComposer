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
    app._select_action(info, action_tile)
    assert app.base_imgs, "底图未加载"
    assert app.part_imgs, "差分部件未加载"
    assert app.composed_img is not None, "合成结果为空"
    assert app.thumb_list.visible, "缩略图条未显示"
    # 图像源必须是字符串 data URI：src 给 bytes 只在构造时有效，
    # 之后 `img.src = bytes` 不会被重新编码，客户端当成路径解析 → 预览区空白。
    for name in ("base_image", "result_image"):
        src = getattr(app, name).src
        assert isinstance(src, str) and src.startswith("data:image/"), \
            f"{name}.src 不是 data URI，更新后不会显示: {type(src)}"
    tile_src = app.thumb_list.controls[0].content.src
    assert isinstance(tile_src, str), "差分缩略图 src 必须是字符串"

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
