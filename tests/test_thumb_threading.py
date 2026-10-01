"""无头回归测试：缩略图后台解码完成后，替换+刷新必须发生在 UI（事件循环）线程。

用轻量 stub 替代 flet/PIL/tkinter，驱动真实 ComposerApp 代码：
_load_bin → _render_tree → 线程池解码 → loop 回调推送 → toggle 展开。
Linux 下可直接运行：python3 tests/test_thumb_threading.py
"""
import asyncio
import io
import struct
import sys
import tempfile
import threading
import types
import zlib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

MAIN_IDENT = threading.get_ident()

# ---------- Fake PIL ----------
class FakeImage:
    class Resampling:
        LANCZOS = 1

    def __init__(self, size=(8, 8), mode="RGBA"):
        self.size = tuple(size)
        self.mode = mode

    @classmethod
    def frombytes(cls, mode, size, data):
        need = size[0] * size[1] * (3 if mode == "RGB" else 4)
        assert len(data) >= need, (len(data), need)
        return cls(size, mode)

    @classmethod
    def merge(cls, mode, bands):
        first = list(bands)[0]
        return cls(getattr(first, "size", (8, 8)), mode)

    @staticmethod
    def alpha_composite(a, b):
        return FakeImage(getattr(a, "size", (8, 8)))

    def split(self):
        n = 3 if self.mode == "RGB" else 4
        return tuple(FakeImage(self.size) for _ in range(n))

    def convert(self, mode):
        return FakeImage(self.size, mode)

    def copy(self):
        return FakeImage(self.size, self.mode)

    def crop(self, box):
        w = max(1, box[2] - box[0])
        h = max(1, box[3] - box[1])
        return FakeImage((w, h), self.mode)

    def paste(self, img, box):
        return None

    def resize(self, size, *a, **k):
        return FakeImage(tuple(size), self.mode)

    def save(self, buf, fmt=None):
        buf.write(b"FAKEPNG" + bytes(64))

    def tobytes(self):
        return b"\0" * (self.size[0] * self.size[1] * 4)

    def getpixel(self, xy):
        return (1, 2, 3, 255)


pil_mod = types.ModuleType("PIL")
pil_mod.Image = FakeImage
sys.modules["PIL"] = pil_mod

# ---------- Fake flet ----------
class _Any:
    def __init__(self, *a, **k):
        self._args = a
        self._kwargs = dict(k)
        self.controls = []
        self.update_threads = []

    def __call__(self, *a, **k):
        return _Any(*a, **k)

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return _Any()

    def update(self):
        self.update_threads.append(threading.get_ident())


class FakePage(_Any):
    def __init__(self):
        super().__init__()
        self.overlay = []
        self.services = None
        self.window = _Any()


class _FtModule(types.ModuleType):
    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return _Any()


sys.modules["flet"] = _FtModule("flet")

from FVPTachieComposerFlet import ComposerApp  # noqa: E402


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
    tmp = Path(tempfile.mkdtemp(prefix="thumb_test_"))
    for outfit in ("私服", "制服"):
        for action in ("基", "喜"):
            make_hzc(tmp / f"CHR_R_{action}_{outfit}.hzc")
            make_hzc(tmp / f"CHR_R_{action}_{outfit}_表情.hzc", image_type=2, frames=2)
    return tmp


def find_tiles(app):
    """返回 [(role_header, role_body, role_toggle), ...] 与 outfit/action 结构。"""
    roles = []
    for role_col in app.tree_list.controls:
        header, body = role_col._args[0]
        roles.append((header, body, header._kwargs["on_click"]))
    return roles


def swapped_src(leading):
    """占位容器是否已被替换为带 src 的 Image（读实例属性，而非构造 kwargs）。"""
    w = leading.__dict__.get("content")
    return w is not None and w._kwargs.get("src") is not None


async def main():
    page = FakePage()
    app = ComposerApp(page)
    assert app._loop is None

    tmp = build_fixture()
    await app._load_bin(str(tmp))
    assert app._loop is asyncio.get_running_loop(), "loop 未捕获"

    # 等待后台解码 + loop 回调全部落地
    await asyncio.sleep(2.5)

    roles = find_tiles(app)
    assert len(roles) == 1, f"role 数={len(roles)}"
    header, body, toggle = roles[0]

    # 1) 角色缩略图：无任何点击即应完成替换，且 update 发生在 loop 线程
    thumb = header._kwargs["leading"]
    assert swapped_src(thumb), "角色缩略图未替换（需点击才出现=旧 bug）"
    assert thumb.update_threads, "角色缩略图从未 update"
    assert all(t == MAIN_IDENT for t in thumb.update_threads), \
        f"update 不在 UI 线程: {set(thumb.update_threads)} vs main {MAIN_IDENT}"

    # 2) 展开角色：服装头缩略图当场排队，完成后同样走 loop 线程推送
    toggle(_Any())
    assert body.visible is True
    await asyncio.sleep(2.0)
    outfit_thumb_ok = 0
    for outfit_col in body.controls:
        oheader, _obody = outfit_col._args[0]
        t = oheader._kwargs["leading"]
        if swapped_src(t):
            outfit_thumb_ok += 1
            assert all(x == MAIN_IDENT for x in t.update_threads), "服装缩略图 update 不在 UI 线程"
    assert outfit_thumb_ok == 2, f"服装头缩略图仅 {outfit_thumb_ok}/2 完成"

    # 3) 展开服装：动作缩略图同样机制
    first_outfit = body.controls[0]
    oheader, obody = first_outfit._args[0]
    oheader._kwargs["on_click"](_Any())
    await asyncio.sleep(2.0)
    n_action = 0
    for act in obody.controls:
        aheader = act  # _action_tile 直接返回 ListTile 本体
        t = aheader._kwargs["leading"]
        if swapped_src(t):
            n_action += 1
            assert all(x == MAIN_IDENT for x in t.update_threads), "动作缩略图 update 不在 UI 线程"
    assert n_action == 2, f"动作缩略图仅 {n_action}/2 完成"

    # 4) 点击动作整链路（底图/差分/合成）在 stub 下无异常
    act_header = obody.controls[0]
    # _select_action 需要 ctrl.update：fake 自带
    app._select_action(first_outfit_info(app), act_header)
    assert app.composed_img is not None, "合成结果为空"

    # 5) 成功横幅 1 秒后自动消除（事件循环定时器）；失败横幅常驻
    app._snack("ok-test")
    assert len(page.overlay) == 1, "横幅未挂载"
    await asyncio.sleep(1.3)
    assert len(page.overlay) == 0, "成功横幅未自动消除"
    app._snack("err-test", error=True)
    await asyncio.sleep(1.3)
    assert len(page.overlay) == 1, "失败横幅不应自动消失"

    print("THUMB_THREADING_OK")


def first_outfit_info(app):
    for outfit, infos in sorted(app.hierarchy["R"].items()):
        for i in infos:
            from FVPTachieComposerFlet import _is_expr
            if not _is_expr(i["filename"]):
                return i
    raise AssertionError("no base info")


asyncio.run(main())
