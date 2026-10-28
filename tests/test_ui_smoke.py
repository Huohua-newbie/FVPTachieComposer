"""真实 flet 下的 UI 冒烟测试：把整棵控件树建出来并驱动一遍主流程。

与 test_thumb_threading.py 的区别：那个用宽容的假 flet 验证线程契约，
这个用**真** flet 构造控件，任何拼错的属性名/类型都会在这里炸出来，
顺带断言"滚动条必须留在预留槽内"这类布局不变量（重合 bug 的回归防线）。

需要已安装 flet + Pillow：python3 tests/test_ui_smoke.py
"""
import asyncio
import hashlib
import os
import struct
import sys
import tempfile
import zlib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# 窗口偏好写进临时文件：测试不该碰 %LOCALAPPDATA% 下真用户的那份 settings.json
os.environ.setdefault(
    "FVP_SETTINGS_PATH",
    str(Path(tempfile.mkdtemp(prefix="ui_smoke_settings_")) / "settings.json"),
)
# 缩略图磁盘缓存同理：测试用独立目录，避免读到上一次运行留下的 50 张缓存
os.environ.setdefault(
    "FVP_THUMB_CACHE",
    str(Path(tempfile.mkdtemp(prefix="ui_smoke_thumbcache_"))),
)

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


class _CanvasSize:
    """假 CanvasResizeEvent：只带宽高，够驱动 _on_drop_resize。"""

    def __init__(self, w, h):
        self.width = w
        self.height = h


class FakeWindow:
    def __init__(self):
        self.width = 1280
        self.height = 760
        self.left = None
        self.top = None
        self.min_width = 1020
        self.min_height = 600
        self.title_bar_hidden = False
        self.icon = None
        self.maximized = False
        self.minimized = False
        self.on_event = None

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


def make_hzc(path, image_type=1, w=8, h=8, frames=1, salt=0):
    head = bytearray(44)
    head[0:4] = b"hzc1"
    struct.pack_into("<H", head, 18, image_type)
    struct.pack_into("<H", head, 20, w)
    struct.pack_into("<H", head, 22, h)
    struct.pack_into("<I", head, 32, frames)
    bpp = 3 if image_type == 0 else 4
    need = w * h * bpp * (frames if image_type == 2 else 1)
    stride = w * h * bpp
    # 纯线性序列的周期是 256，正好等于一帧的字节数，多帧会解出一堆一模一样的画面。
    # 混进帧号（i // stride）并取 251 为周期，翻帧断言才能真的看出画面变了。
    # salt 让"同名的另一份资源"画面确实不同：换资源串图与否只能这样验。
    # 文件名也掺一点：同一份资源里不同动作是两张不同的图，否则整帧缓存
    # 会（正确地）把同样字节当成同一张，切动作的断言就验不出东西。
    name_salt = int(hashlib.sha1(path.name.encode("utf-8")).hexdigest()[:8], 16) % 251
    body = bytes(((i % 251) * 7 + (i // stride) * 53 + salt + name_salt) % 256
                 for i in range(need))
    path.write_bytes(bytes(head) + zlib.compress(body))


def build_fixture(salt=0):
    tmp = Path(tempfile.mkdtemp(prefix="ui_smoke_"))
    for role in ("翠", "真紅"):
        for outfit in ("私服", "制服"):
            for action in ("基", "喜"):
                make_hzc(tmp / f"CHR_{role}_{action}_{outfit}.hzc", salt=salt)
                make_hzc(tmp / f"CHR_{role}_{action}_{outfit}_表情.hzc",
                         image_type=2, frames=3, salt=salt)
    # 没有 _表情 变体的多帧底图：走「直接导出底图」那条退路
    make_hzc(tmp / "CHR_翠_无_私服.hzc", image_type=2, frames=3, salt=salt)
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

    # 拖放入口：门控必须跟着"客户端是谁"走——官方客户端没有 desktop_drop 插件，
    # 未 build 的运行时塞 Dropzone 会让整棵控件树渲染失败。
    import os as _os

    from FVPTachieComposerFlet import (
        DROPZONE_ON,
        _custom_client_available,
        _dropzone,
        _dropzone_on,
        pick_drop_path,
    )

    assert DROPZONE_ON is (bool(_dropzone) and _custom_client_available()), \
        "DROPZONE_ON 应由 flet_dropzone 可导入 + 自定义客户端可用共同决定"
    _saved_flag = _os.environ.pop("FVP_DROPZONE", None)
    try:
        _os.environ["FVP_DROPZONE"] = "0"
        assert _dropzone_on() is False, "FVP_DROPZONE=0 必须强制关闭"
        if _dropzone is not None:
            _os.environ["FVP_DROPZONE"] = "1"
            assert _dropzone_on() is True, "FVP_DROPZONE=1 应强制开启"
    finally:
        _os.environ.pop("FVP_DROPZONE", None)
        if _saved_flag is not None:
            _os.environ["FVP_DROPZONE"] = _saved_flag

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

    # 交叉轴 STRETCH 需要"有界高度"，而 ListView/滚动列给不出：
    # 一旦写出来，布局当场抛异常，列表里该项之后的节点全部消失（真机踩过）。
    for c in nodes:
        if isinstance(c, ft.Row):
            assert c.vertical_alignment != ft.CrossAxisAlignment.STRETCH, \
                "Row 在滚动容器里不能用 vertical_alignment=STRETCH"
        if isinstance(c, ft.Column):
            assert c.horizontal_alignment != ft.CrossAxisAlignment.STRETCH, \
                "Column 在滚动容器里不能用 horizontal_alignment=STRETCH"

    # 视觉基线：白卡片 + 浅灰页底 + 1px 细描边，禁止材质特效回潮
    # （和纸纹理/琉璃渐变试过，实测与三栏工具界面不搭，已明确放弃）。
    from FVPTachieComposerFlet import RADIUS_PANEL
    assert app._checker, "透明棋盘底缺失"
    panels = [c for c in nodes
              if type(c) is ft.Container
              and getattr(c, "border_radius", None) == RADIUS_PANEL
              and c.bgcolor == app.ink["panel"]
              and c.border is not None and c.border.top.width == 1]
    assert len(panels) >= 3, f"三栏不是实色细描边卡片（只找到 {len(panels)} 个）"
    for c in panels:
        assert c.gradient is None, "面板不该再用渐变"
        assert c.shadow is None, "面板不该再用投影"
    bare_texture = [c for c in nodes
                    if getattr(c, "image", None) is not None
                    and c.image.repeat == ft.ImageRepeat.REPEAT
                    and getattr(c, "border_radius", None) is None]
    assert not bare_texture, "页面底层不该再铺满纹理"

    # 载入入口只留一套（角色库标题栏的两个图标）。空状态再摆一排按钮既是
    # 重复控件，实心 accent 大色块也和白卡片打架。
    from flet.controls.material.button import Button as _Button
    empty_btns = [c for c in _walk(app.empty_hint) if isinstance(c, _Button)]
    assert not empty_btns, f"空状态里还有 {len(empty_btns)} 个重复的载入按钮"
    solid = [c for c in nodes if isinstance(c, _Button)
             and c.style is not None and c.style.bgcolor == app.ink["accent"]]
    assert not solid, f"还有实心 accent 大按钮: {[c.data for c in solid]}"

    # 展开到动作层并选中，跑完 底图 → 差分 → 合成 全链路
    def role_column(i=0):
        """载入那一轮的角色套在动画外壳（错峰入场）里，取下层 tile 先剥壳。"""
        c = app.tree_list.controls[i]
        return c.content if isinstance(c, ft.Container) else c

    role_col = role_column()
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
    assert action_tile.border.left.width == 1, "动作层缺少导轨线"
    hl = action_tile.content
    assert hl.animate is not None, "动作选中态必须有过渡，不能只是闪一下变色"

    await app._select_action(info)
    assert app.base_imgs, "底图未加载"
    assert app.part_imgs, "差分部件未加载"
    assert app.composed_img is not None, "合成结果为空"
    # 结果面板头不重复底图尺寸：同尺寸时只留偏移，否则左右两个面板头会显示同一个 583×989
    assert "×" in app.base_meta.value, app.base_meta.value
    assert "×" not in app.result_meta.value, app.result_meta.value
    assert app.result_meta.value.startswith("偏移"), app.result_meta.value
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
        await app._select_action(info2)
        assert other["hl"].bgcolor == app.ink["sel"], "新选中的行没有亮起"
        assert sel["hl"].bgcolor == "#00000000", "上一行的选中态没被清掉"
        assert sel["hl"].content.title.weight == ft.FontWeight.W_400, "上一行标题仍是加粗"
        await app._select_action(info)  # 换回来，后面的用例继续用这一张
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
    # 注意：不要用 animate_size/animate_position=False 去治"拉伸时闪烁"——
    # Flet 0.86 下给 Container 显式设这两个 False 会让整块内容不渲染（真机截图里
    # 动作行和差分条变成纯灰矩形）。这条只是把坑记在这里。
    assert picked.animate_size is None and picked.animate_position is None, \
        "animate_size/position 必须保持默认，显式 False 会导致不渲染"

    snapshot = app.composed_img
    kept_sel = app.selected_filename
    kept_base_src = app.base_image.src
    await app._toggle_theme(None)
    assert app.is_dark is True
    # 重建树时"单动作服装展开即选中"不能触发，否则底图会被悄悄换成别的
    assert app.selected_filename == kept_sel, "换主题后选中的动作变了"
    assert app.base_image.src == kept_base_src, "换主题后底图被换成了另一张"
    assert role_column().controls[1].visible is True, "换主题把已展开的角色收起了"
    assert role_column().controls[1].controls[0].controls[1].visible is True, \
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
    # 标题栏的文件名胶囊随主题重建，初值必须从 input_file 推导，
    # 否则切一次主题就显示"未载入文件"，看着像整个状态被清空。
    assert app.file_chip.content.value == Path(app.input_file).name, \
        "换主题后标题栏文件名胶囊丢了"
    # 换主题会整棵重建，拖放画布也在其中。状态不跟着归零的话，
    # 下一次 enter 会因为"目标态已经等于当前态"而直接早退——面板再也不亮。
    app._drop_hint_shown = True
    app._drop_armed = True
    await app._toggle_theme(None)
    assert app._drop_hint_shown is False and app._drop_armed is False, \
        "换主题后拖放态残留，下一次拖入不会有反馈"
    assert app._drop_canvas is not None and app._drop_canvas.visible is False, \
        "换主题后画布还亮着——它会吃掉面板里所有的点击"

    # 拖放反馈贴在角色库面板自己身上：画布必须是贴面板四边的定位子节点，
    # 否则画出来的虚线框和面板描边错开一圈，就又是"给应用贴了个取景框"。
    app._on_drop_resize(_CanvasSize(300, 700))
    app._drag_entered()
    canvas = app._drop_canvas
    assert app._drop_armed is True and canvas.visible is True, "拖入后面板没进入拖放态"
    assert (canvas.top, canvas.left, canvas.right, canvas.bottom) == (0, 0, 0, 0), \
        "画布没贴满面板，虚线框会缩在描边里面一圈"
    assert len(canvas.shapes) == 5, "画布不止 底纹/辉光边/虚线边/光斑拖尾/光斑核心 五层"
    wash = canvas.shapes[0]
    assert wash.paint.style == ft.PaintingStyle.FILL, "底纹不是填充层"
    assert wash.paint.color.startswith("#") and len(wash.paint.color) == 9, \
        f"底纹颜色不是 #AARRGGBB: {wash.paint.color}"
    dash = canvas.shapes[2]
    assert dash.paint.stroke_dash_pattern, "落点边框不是虚线"
    assert app._hint_title.value == "松手即载入", "拖入时空状态文案没换"

    # 淡入：进度逐帧推到 1 后停住（静止是默认，动是点缀）
    app._drop_gen += 1                  # 掐掉自发排队的计时链，下面手工逐帧推
    for _ in range(12):                 # DROP_IN_FRAMES=6，多推几帧看过冲和停表
        app._tick_drop(app._drop_gen)
        if app._drop_a >= 1.0:
            break
    assert app._drop_a == 1.0 and app._drop_dir == 0, "淡入没走到终点或没停下"

    # 行进光斑：tick 一次，光斑必须沿面板边挪动（停留态唯一的动效）
    before = (canvas.shapes[4].elements[0].x, canvas.shapes[4].elements[0].y)
    app._tick_drop(app._drop_gen)
    after = (canvas.shapes[4].elements[0].x, canvas.shapes[4].elements[0].y)
    assert after != before, "光斑没跟着 tick 行进"
    assert app._drop_dist > 0

    # 拒绝态：边框变红、落点旁边就地改红字，不再跑到窗口底部弹横幅
    app._reject_drop()
    err = "#FF" + app.ink["err_frame"].lstrip("#").upper()
    assert canvas.shapes[2].paint.color == err, f"拒绝时虚线框没变红: {canvas.shapes[2].paint.color}"
    assert app._hint_title.value == "拖进来的不是资源", "拒绝时空状态文案没改"
    assert app._snack_overlay not in app.page.overlay, "拒绝不该弹底部横幅（视线就在落点上）"
    await asyncio.sleep(0.5)           # 红停 160ms + 淡出 120ms 后才收回
    assert canvas.visible is False, "拒绝后画布没收回，会继续吃掉面板里的点击"
    assert app._hint_title.value == "尚未加载文件", "拒绝退场后文案没还原"

    # 载入着再拖入：提示必须上场——它在 Stack 下层，不给树让位就永远看不见
    app._drag_entered()
    assert app.empty_hint.visible is True and app.tree_list.visible is False, \
        "已载入文件后拖入，提示被树盖住（没让位）"
    assert app._hint_title.value == "松手即载入"
    app._drag_exited()
    assert app.tree_list.visible is True and app.empty_hint.visible is False, \
        "拖放退出后旧树没回来"

    # 松手即清场：解析在后台跑，这期间旧树必须先撤，提示当场接上，
    # 不能让用户对着一动不动的旧树干等（初始态同理，不能空窗）。
    class _DropFile:
        def __init__(self, p):
            self.path, self.name = p, Path(p).name

    class _DropEvt:
        def __init__(self, ps):
            self.files = [_DropFile(p) for p in ps]

    real_load = app._load_bin
    seen = {}

    async def hold(path):
        seen["tree"] = app.tree_list.visible
        seen["controls"] = len(app.tree_list.controls)
        seen["hint"] = (app.empty_hint.visible, app._hint_title.value)
        await asyncio.Event().wait()   # 一直挂着，由测试收场

    app._load_bin = hold
    try:
        task = asyncio.create_task(app._on_dropped(_DropEvt([bin_a])))
        await asyncio.sleep(0.05)
        assert seen.get("controls") == 0 and seen.get("tree") is False, \
            "松手后旧树还挂着——要先清场再解析"
        assert seen.get("hint") == (True, "正在解析…"), \
            "松手后没有当场反馈，这就是那个空窗期"
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    finally:
        app._load_bin = real_load

    # 解析失败：旧树原样回来，载入状态/状态栏都不能被坏文件冲掉
    broken = str(tmp / "broken.bin")
    Path(broken).write_bytes(b"not a bin")
    prev_status = app.status_text.value
    await app._on_dropped(_DropEvt([broken]))
    assert app.input_file == str(tmp), "坏文件把载入状态冲掉了"
    assert app.tree_list.controls and app.tree_list.visible is True, \
        "解析失败后旧树没回来"
    assert app.empty_hint.visible is False, "解析失败后空态提示冒出来了"
    assert app.status_text.value == prev_status, \
        f"状态栏卡在 {app.status_text.value!r}，没回到解析前"
    assert app._hint_title.value == "尚未加载文件", "失败退场后提示文字没复位"

    # 提示横幅：居中下方的小卡片，从底部浮起；外层只定位不吞点击。
    # 成功提示要自己淡出消失，报错才留着手动关。
    app._snack("已载出: demo.bin")
    holder = app._snack_overlay.controls[0]
    assert holder.content is app._snack_toast, "横幅不是单个小卡片（又铺成整宽了）"
    assert holder.alignment is not None and holder.left == 0 and holder.right == 0, \
        "横幅没在窗口底部居中"
    assert getattr(holder, "bgcolor", None) is None, "定位层有底色会吞掉整条横带的点击"
    assert app._snack_toast.opacity == 0.0, "横幅初始态没留动画起点"
    await asyncio.sleep(0.05)
    assert app._snack_toast.opacity == 1.0 and app._snack_toast.offset == ft.Offset(0, 0), \
        "横幅没走到显示最终态"
    await asyncio.sleep(2.4)           # 2s 自动消除 + 淡出
    assert app._snack_overlay not in app.page.overlay, "成功横幅没有自动消失"

    # 过程反馈：代数不符（解析早就完了）不弹；还在途中才弹，而且绝不自退。
    app._parsing_gen = 7
    app._show_parsing_busy(8, "x.bin")
    assert app._busy_seq is None and not app.page.overlay, "快路径也被弹了横幅"
    assert app.parse_progress.visible is False, "没到揭示时刻就把进度条亮出来了"
    app._show_parsing_busy(7, "x.bin")
    assert app._busy_seq == app._snack_seq, "慢路径没弹\"正在解析\""
    # 进度条和这条提示同一时刻上场，此刻还不知道总数 → 不定态
    assert app.parse_progress.visible is True and app.parse_progress.value is None, \
        "\"正在解析\"弹了但进度条没跟上"
    await asyncio.sleep(2.4)
    assert app._snack_overlay in app.page.overlay, "busy 卡片不该自己消失"
    app._dismiss_snack(app._busy_seq)
    await asyncio.sleep(0.3)
    assert app._snack_overlay not in app.page.overlay
    app._end_parse_feedback()
    assert app.parse_progress.visible is False, "解析收尾后进度条没收回去"

    # 方向键：列表项/按钮点过之后会持有焦点，e.control 不为空是常态而不是"在打字"，
    # 拿它当"焦点被占用"判断会把 ←/→ 全吞掉，帧切换直接失效。
    class _Key:
        def __init__(self, key, control=None):
            self.key = key
            self.control = control
            self.ctrl = False

    if len(app.base_imgs) > 1:
        before = app.base_idx
        await app._on_keyboard(_Key("Right", control=object()))
        assert app.base_idx != before, "控件持有焦点时方向键切不了帧"
        await app._on_keyboard(_Key("Left", control=object()))
        assert app.base_idx == before, "往回切没恢复"

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

    # ── 窗口记忆 ─────────────────────────────────────────────
    # 拖动和缩放都不走关闭流程：只存"退出时"会漏掉整个「拖过一遍再关」的情况，
    # 所以尺寸/位置靠窗口事件记录。这里验的就是事件确实挂上了、并且会落盘。
    import fvpapp.settings as st

    # 目录记忆要跨重启：退出时落盘成 dialogs 段（打包态只有 CLOSE 事件来得及，_close 也写）
    app._write_dialog_dirs()
    assert st.dialog_dirs() == {"import": str(tmp), "export": str(tmp / "export")}, \
        f"对话框目录没落盘: {st.dialog_dirs()}"

    class _WinEvt:
        def __init__(self, t):
            self.type = t

    assert callable(page.window.on_event), "窗口事件没挂上，尺寸位置变化不会被记录"
    page.window.width, page.window.height = 1500, 900
    page.window.left, page.window.top = 210, 130
    page.window.on_event(_WinEvt(ft.WindowEventType.RESIZED))
    await asyncio.sleep(st.SAVE_DELAY_S + 0.2)      # 等节流那一档落地
    saved = st.window_prefs()
    assert (saved.get("width"), saved.get("height")) == (1500, 900), f"缩放后没写盘: {saved}"
    assert (saved.get("left"), saved.get("top")) == (210, 130), f"拖动后没写盘: {saved}"

    # 最大化只改标记：这会儿的宽高是整个显示器的框，存下来下次会还原成铺满的浮窗
    page.window.on_event(_WinEvt(ft.WindowEventType.MAXIMIZE))
    await asyncio.sleep(st.SAVE_DELAY_S + 0.2)
    saved = st.window_prefs()
    assert saved.get("maximized") is True, f"最大化状态没记住: {saved}"
    assert (saved.get("width"), saved.get("left")) == (1500, 210), \
        f"最大化把上次的几何冲掉了: {saved}"

    # 换主题整树重建，窗口偏好与事件不能丢
    await app._toggle_theme(None)
    assert callable(page.window.on_event), "换主题后窗口事件掉了"
    assert st.window_prefs().get("maximized") is True, "换主题把窗口记忆洗掉了"
    await app._toggle_theme(None)

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
    await asyncio.sleep(0.3)           # 淡出走完才摘掉，不再瞬间消失
    assert not page.overlay, "横幅未移除"
    app._snack("报错横幅", error=True)
    app._dismiss_snack(-1)
    await asyncio.sleep(0.3)
    assert len(page.overlay) == 1, "错误横幅不应被别的序号消掉"
    app._dismiss_snack(app._snack_seq)
    await asyncio.sleep(0.3)
    assert not page.overlay, "手动关闭后错误横幅未移除"

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

    # argv 自动载入要一直可用（拖到 exe 图标那条通道）。打包态能走到这里
    # 全靠 client\main.dart 的 FVP-PATCH：flet 0.86.5 原生客户端会把位置参数
    # 当成 developer mode 的 page URL，嵌入 Python 根本不启动。
    page2 = FakePage()
    old_argv, sys.argv = sys.argv, ["prog", str(tmp)]
    try:
        app2 = await app_main(page2)
    finally:
        sys.argv = old_argv
    assert app2.input_file == str(tmp), "argv 自动载入失效了"
    assert page2.added, "argv 启动路径未建树"
    assert app2.export_dir == str(tmp / "export"), "重启后导出目录没接上"
    assert app2.import_dir == str(tmp), "重启后导入目录没接上"

    # ── 无表情差分 → 直接导出底图 ──────────────────────────────
    # 只让看不让导出的状态是不完整的：没有 _表情 变体（或载入的不是立绘）时，
    # 结果面板要镜像当前底图帧，保存/批量导出都退到底图本身。
    from FVPTachieComposerFlet import PART_HINT_EMPTY
    nodiff = next(i for i in app.file_infos if i["filename"] == "CHR_翠_无_私服")
    await app._select_action(nodiff)
    assert app.base_only is True, "没有差分时应进入「直接导出底图」模式"
    assert app.part_imgs == [] and app.composed_img is None, "无差分模式不该留下合成结果"
    assert app.part_hint_text.value == PART_HINT_EMPTY, "左栏没说明会导出底图"
    assert app.result_hint.visible is False, "底图模式下结果面板还是占位图"
    assert app.save_btn.disabled is False and app.batch_btn.disabled is False, \
        "无差分模式下导出按钮被禁用（只能看不能导）"
    mirrored = app.result_image.src
    assert isinstance(mirrored, str) and mirrored.startswith("data:image/"), \
        "底图模式右栏没镜像成 data URI"
    assert mirrored == app.base_image.src, "右栏镜像的不是当前底图帧——看到的和导出的对不上"

    # 换帧要跟手：所见即所导
    app._next_frame(None)
    assert app.base_idx == 1
    assert app.result_image.src != mirrored, "翻帧后右栏还停在上一帧"
    assert app.result_image.src == app.base_image.src, "翻帧后右栏没跟着底图走"
    assert "底图（无表情差分）" in app.result_meta.value

    # 保存命名走 _base_ 且用当前底图帧号。
    # 注意要挂在实例上：上面「导入/导出记目录」那一步已经把 save_file 换成了
    # 实例属性，类级别的补丁会被它挡住。
    picked_names = []
    batch_dir = tmp / "batch"
    batch_dir.mkdir(exist_ok=True)

    async def fake_base_save(dialog_title=None, file_name=None,
                             initial_directory=None, **kw):
        picked_names.append(file_name)
        return str(tmp / file_name)

    async def fake_base_dir(dialog_title=None, initial_directory=None, **kw):
        return str(batch_dir)

    app.file_picker.save_file = fake_base_save
    app.file_picker.get_directory_path = fake_base_dir
    try:
        await app._save_current(None)
        assert picked_names == ["CHR_翠_无_私服_base_001.png"], \
            f"底图导出命名不对: {picked_names}"
        assert (tmp / picked_names[0]).exists(), "底图没真正写盘"

        await app._batch_export(None)
        assert sorted(p.name for p in batch_dir.glob("*.png")) == \
            [f"CHR_翠_无_私服_base_{i:03d}.png" for i in range(3)], \
            "无差分批量导出没有铺平底图全部帧"
    finally:
        del app.file_picker.save_file
        del app.file_picker.get_directory_path

    # 换主题后底图模式必须原样回来，按钮不能重新变灰
    kept_base_src = app.base_image.src
    await app._toggle_theme(None)
    assert app.base_only is True, "换主题把「直接导出底图」模式弄丢了"
    assert app.base_idx == 1, "换主题后底图帧号被重置"
    assert app.base_image.src == kept_base_src
    assert app.result_image.src == app.base_image.src, "换主题后右栏没跟住底图"
    assert app.save_btn.disabled is False and app.batch_btn.disabled is False, \
        "换主题后底图模式的导出按钮又灰了"

    # 载出：卸载后必须回到"刚打开时"的空状态，且能再次载入（回初始态的回归防线）
    await app._unload_bin(None)
    assert app.input_file is None, "载出后 input_file 未清空"
    assert app.file_infos == [] and app.hierarchy == {}, "载出后解析状态未清空"
    assert app.selected_info is None and app.selected_filename is None, "载出后选中态未清空"
    assert app.base_imgs == [] and app.part_imgs == [] and app.composed_img is None, \
        "载出后预览状态未清空"
    assert app.base_idx == 0 and app.part_idx == 0, "载出后帧号未复位"
    assert app.tree_list.visible is False and not app.tree_list.controls, "载出后树还挂着"
    assert app.empty_hint.visible is True, "载出后空状态提示没恢复（回不到初始态）"
    assert app.file_chip.content.value == "未载入文件", "载出后标题栏文件名胶囊没复位"
    assert app.unload_btn.visible is False, "载出后载出按钮还在显示"
    assert app.save_btn.disabled is True and app.batch_btn.disabled is True, \
        "载出后导出按钮没被禁用"
    assert app.role_thumb_cache == {}, "载出后缩略图缓存没清"
    assert app.status_text.value == "就绪", f"载出后状态栏没复位: {app.status_text.value}"
    # 再载入要能照常建树、载出按钮回来
    await app._load_bin(str(tmp))
    assert app.input_file == str(tmp) and app.tree_list.visible, "载出后无法再次载入"
    assert app.empty_hint.visible is False and app.unload_btn.visible is True

    # 角色逐条错峰浮入：起步全是透明的外壳，链子把每条推到终点。
    # 载入的确认长在内容自己身上，不再另起一层提示。
    from FVPTachieComposerFlet import STAGGER_MS
    assert len(app._tree_wraps) == len(app.hierarchy), "错峰外壳数量对不上角色数"
    assert all(w.opacity == 0.0 for w in app._tree_wraps), "错峰起步没给动画留起点"
    await asyncio.sleep(len(app._tree_wraps) * STAGGER_MS + 0.3)
    assert all(w.opacity == 1.0 and w.offset == ft.Offset(0, 0)
               for w in app._tree_wraps), "错峰没把每条角色都浮到位"
    # 中途又拖进来：剩下的必须直接落到终点，不能留半透明的行
    app._stagger_i = 0
    for w in app._tree_wraps:
        w.opacity = 0.0
    app._drop_armed = True
    app._stagger_step()
    assert all(w.opacity == 1.0 for w in app._tree_wraps), "拖入打断错峰时留下了半透明的角色"
    app._drop_armed = False

    # ── 缩略图缓存 ─────────────────────────────────────────────
    # 三个缓存的键都只带文件名（角色名、mini_<文件名>_<尺寸>、("part"/"base", 文件名, 帧号)），
    # 而不同 bin / 不同拆包目录里文件名撞车是常态：载入新资源时不清缓存，新资源就直接
    # 拿到上一份的图（串图）。这里用"文件名一模一样、画面不同"的两份资源来验。
    from fvpapp import media as _media

    def _role_thumb_src(role):
        w = app.role_thumb_cache.get(role)
        if w is None:
            return None
        return getattr(w, "src", None) or getattr(getattr(w, "content", None), "src", None)

    def _strip_srcs():
        return [getattr(c.content, "src", None) for c in app.thumb_list.controls]

    decoded = []
    real_read = app._read_first_frame
    app._read_first_frame = lambda info, data=None, header=None: (
        decoded.append(info["filename"]), real_read(info, data, header)
    )[1]
    try:
        target = next(i for i in app.file_infos if i["filename"] == "CHR_翠_喜_私服")
        await app._select_action(target)
        await asyncio.sleep(0.6)                       # 底图与差分条都在后台解
        base_a, strip_a = app.base_image.src, _strip_srcs()
        assert base_a.startswith("data:image/") and strip_a, "底图或差分条没出来"
        role_a = _role_thumb_src("翠")

        # 角色/服装现在才展开：这些层的缩略图此前不该提前解码
        for role in sorted(app.hierarchy):
            app._tree_toggles[("role", role)][1](None)
        for role in sorted(app.hierarchy):
            for outfit in sorted(app.hierarchy[role]):
                app._tree_toggles[("outfit", f"{role}/{outfit}")][1](None)
        await asyncio.sleep(0.6)
        first = len(decoded)
        assert first > 0, "展开后一张缩略图都没解码，这条断言没走到路径"

        # 换主题会整棵重建树并恢复展开态：解过的不能再解一遍
        await app._toggle_theme(None)
        await asyncio.sleep(0.6)
        assert len(decoded) == first, f"重建树又解码了 {len(decoded) - first} 张缩略图"

        # 卡片上写的自测口径：整批收起再展开，第二次不重复解码
        roles = sorted(app.hierarchy)
        for role in roles:
            app._tree_toggles[("role", role)][1](None)
        for role in roles:
            app._tree_toggles[("role", role)][1](None)
        await asyncio.sleep(0.4)
        assert len(decoded) == first, f"收起再展开又解码了 {len(decoded) - first} 张缩略图"

        # 换资源：同名不同画面，角色/服装/动作/底图/差分条都要跟着换
        other = build_fixture(salt=90)
        await app._load_bin(str(other))
        await asyncio.sleep(0.6)
        assert _role_thumb_src("翠") is not None, "新资源的角色缩略图没出来"
        assert _role_thumb_src("翠") != role_a, "换资源后角色缩略图还是上一份的（同名串图）"
        assert _media._SRC_CACHE == {}, "换资源后底图/合成缓存还留着上一份的（按文件名索引）"
        assert _media._THUMB_SRC_CACHE == {}, "换资源后差分条缓存还留着上一份的（按文件名索引）"
        target_b = next(i for i in app.file_infos if i["filename"] == "CHR_翠_喜_私服")
        await app._select_action(target_b)
        await asyncio.sleep(0.6)
        assert app.base_image.src != base_a, "换资源后同一动作的底图还是上一份的"
        assert _strip_srcs() != strip_a, "换资源后差分条还是上一份的"
    finally:
        app._read_first_frame = real_read

    await app._unload_bin(None)  # 幂等收尾：第二次载出同样干净
    assert app.input_file is None and app.empty_hint.visible is True
    assert app.role_thumb_cache == {} and _media._SRC_CACHE == {} and _media._THUMB_SRC_CACHE == {}, \
        "载出后图像缓存没清干净"
    from fvpapp import thumbcache as _tc
    assert _tc.decoded_frames() == 0, "载出后整帧缓存还占着内存"

    # ── 大资源载入的进度反馈 ───────────────────────────────────
    # 小资源在进度条亮起来之前就解析完了，那条路全程安静；只有慢资源才把
    # "正在解析"和进度条一起亮出来，状态栏右侧报阶段，收尾立刻收起。
    import fvpapp.resources as _res

    big = Path(tempfile.mkdtemp(prefix="ui_smoke_big_"))
    for i in range(400):
        make_hzc(big / f"CHR_翠_动作{i:03d}_私服.hzc")

    raw, frames = [], []
    real_scan, real_show = _res.parse_dir_infos, app._show_parse_progress

    def counting_scan(path, progress=None):
        def inner(done, total, stage):
            raw.append((done, total, stage))
            progress(done, total, stage)
        return real_scan(path, progress=inner)

    def spy(gen, done, total, stage):
        frames.append((done, total, stage, app.parse_progress.visible))
        real_show(gen, done, total, stage)

    _res.parse_dir_infos = counting_scan
    app._show_parse_progress = spy
    old_delay = _res.BUSY_DELAY
    _res.BUSY_DELAY = 0            # 400 个文件在小机器上也可能秒完，门槛降到 0 才保证走到慢路径
    try:
        await app._load_bin(str(big))
    finally:
        _res.BUSY_DELAY = old_delay
        _res.parse_dir_infos = real_scan
        del app._show_parse_progress

    stages = {s for _, _, s in raw}
    assert "扫描目录" in stages and "读文件头" in stages, f"阶段不全: {stages}"
    assert raw[-1][0] == raw[-1][1] != 0, f"最后一帧没报满: {raw[-1]}"
    head = [(d, t) for d, t, s in raw if s == "读文件头"]
    assert all(b[0] >= a[0] for a, b in zip(head, head[1:])), "读文件头的计数倒着走了"
    assert len({t for _, t in head}) == 1, "同一阶段里 total 会变，进度条会来回跳"
    # 节流：四百次回调只该换来个位数帧——逐条刷会把事件循环淹了
    assert len(raw) >= 400 and len(frames) < 60, \
        f"引擎报了 {len(raw)} 次、UI 收到 {len(frames)} 帧，节流没生效"
    assert any(f[3] for f in frames), "慢资源从头到尾没亮过进度条"
    assert app.parse_progress.visible is False and app.parse_progress.value is None, \
        "解析完了进度条还挂着（残影）"
    assert app.status_meta.value == f"1 角色 · {len(app.file_infos)} 图像", \
        f"状态栏停在解析文案: {app.status_meta.value}"

    # UI 线程那一半不用起线程也能验：没到揭示时刻不画、代号不符自弃、
    # total=0 是"还不知道总数"（不定态），文案不能写成 0/0。
    app._parsing_gen, app._parse_shown = 21, False
    app._show_parse_progress(21, 5, 400, "读文件头")
    assert app.parse_progress.visible is False, "还没到揭示时刻就把进度条画出去了"
    app._parse_shown = True
    app._show_parse_progress(21, 5, 400, "读文件头")
    assert app.parse_progress.value == 5 / 400, app.parse_progress.value
    assert app.status_meta.value == "读文件头 5/400", app.status_meta.value
    app._show_parse_progress(21, 30, 0, "扫描目录")
    assert app.parse_progress.value is None, "total=0 该是不定态，不该给个具体比例"
    assert app.status_meta.value == "扫描目录 30 个", app.status_meta.value
    app._show_parse_progress(22, 399, 400, "读文件头")      # 上一轮解析的迟到帧
    assert app.status_meta.value == "扫描目录 30 个", "过期代号的帧还在改状态栏"
    app._end_parse_feedback()
    assert (app._parsing_gen, app._parse_shown) == (None, False), "收尾没把解析代号作废"
    assert app.parse_progress.visible is False and app.parse_progress.value is None, \
        "收尾没清干净"

    # ── 跨会话磁盘命中（50 张上限内的树缩略图）────────────────
    # 换一个 App 实例、同一份资源、同一个 FVP_THUMB_CACHE：树里三级缩略图
    # 都应直接命中磁盘，_read_first_frame 一次都不该再走。
    app2 = ComposerApp(FakePage())
    decoded2 = []
    real_read2 = app2._read_first_frame
    app2._read_first_frame = lambda info, data=None, header=None: (
        decoded2.append(info["filename"]), real_read2(info, data, header)
    )[1]
    try:
        await app2._load_bin(str(tmp))
        for role in sorted(app2.hierarchy):
            app2._tree_toggles[("role", role)][1](None)
        for role in sorted(app2.hierarchy):
            for outfit in sorted(app2.hierarchy[role]):
                app2._tree_toggles[("outfit", f"{role}/{outfit}")][1](None)
        await asyncio.sleep(1.0)
        assert decoded2 == [], \
            f"跨会话没命中磁盘缓存，又解码了 {len(decoded2)} 张: {decoded2[:5]}"
    finally:
        app2._read_first_frame = real_read2

    # ── 卡片 2.4：来回切动作不重解整帧 ─────────────────────────
    # 计数挂在真正干整包 zlib 的 hzc_data_to_pil_list 上（不是树里的首帧解码）。
    app3 = ComposerApp(FakePage())
    await app3._load_bin(str(tmp))
    await asyncio.sleep(0.2)
    calls = []
    real_list = _res.hzc_data_to_pil_list

    def counting_list(data, header):
        calls.append(1)
        return real_list(data, header)

    _res.hzc_data_to_pil_list = counting_list
    try:
        first = next(i for i in app3.file_infos if i["filename"] == "CHR_翠_基_私服")
        second = next(i for i in app3.file_infos if i["filename"] == "CHR_翠_喜_私服")
        await app3._select_action(first)
        await asyncio.sleep(0.4)
        n_first = len(calls)
        await app3._select_action(second)
        await asyncio.sleep(0.4)
        n_second = len(calls)
        await app3._select_action(first)
        await asyncio.sleep(0.4)
        assert n_first >= 2, f"第一次选动作只解了 {n_first} 次，没走到整帧解码"
        assert n_second > n_first, "换动作没有解新动作"
        assert len(calls) == n_second, \
            f"切回已看过的动作又整帧解码了 {len(calls) - n_second} 次"
    finally:
        _res.hzc_data_to_pil_list = real_list

    # 解析中途按下载出：进度条不能跟着带进初始态
    app._parsing_gen = app._state_gen
    app.parse_progress.visible = True
    await app._unload_bin(None)
    assert app.parse_progress.visible is False and app.parse_progress.value is None, \
        "载出把亮着的进度条带进了初始态"
    assert app.status_text.value == "就绪" and app.status_meta.value == ""

    # 批量导出的状态栏控件：静止时都藏着，导出中「取消」与进度条一起亮，
    # 收尾三个路口（成功/失败/取消）都不许留残影。真正的导出流程在
    # tests/test_export_progress.py 里逐项验，这里只验装机与收起。
    assert app.export_cancel_btn.visible is False, "静止时「取消」不该露脸"
    assert app.status_fail_action.visible is False, "没有失败项时不该出现「查看失败项」"
    assert app.export_cancel_btn.on_click is not None, "「取消」没接上 _cancel_export"
    assert app.status_fail_action.on_click is not None, "「查看失败项」没接上清单"
    gen = app._export_begin(4)
    assert app.parse_progress.visible is True and app.export_cancel_btn.visible is True
    assert app.status_meta.value == "导出 0/4", app.status_meta.value
    app._show_export_progress(gen, 2, 4)
    assert abs(app.parse_progress.value - 0.5) < 1e-6, app.parse_progress.value
    assert app.status_meta.value == "导出 2/4", app.status_meta.value
    app._finish_export(gen, 4, [], False, str(tmp))
    assert app.parse_progress.visible is False and app.export_cancel_btn.visible is False, \
        "导出收尾没收起进度条/取消"
    assert "已导出 4 张" in app.status_text.value, app.status_text.value
    # 作废后的迟到帧不许再把进度条点亮
    app._show_export_progress(gen, 3, 4)
    assert app.parse_progress.visible is False, "过期导出帧又把进度条画回去了"

    print("UI_SMOKE_OK")


asyncio.run(run())
