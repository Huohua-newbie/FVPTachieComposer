"""测试共享设施：环境隔离、假 flet、真 flet 页面、夹具与事件轮询。

本模块只依赖标准库，导入它不会碰 flet / PIL，也不会往仓库里写日志。
应用入口 `FVPTachieComposerFlet` 在导入期就装文件日志，所以这里先把
`FVP_NO_FILE_LOG` 置 1——任何测试文件只要先 `import support`，就不会留下
`logs/` 目录。要测日志落盘的用例（test_crashlog）在自己的沙箱里把它摘掉即可。

假 flet 与真 flet 共享同一批模块级状态（`sys.modules`、应用单例），
**不能在同一个进程里混跑**；`tests/run_all.py` 按文件分进程。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import shutil
import struct
import sys
import tempfile
import threading
import time
import types
import unittest
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"

# 必须在导入应用模块之前生效：否则 FVPTachieComposerFlet 会在仓库根建 logs/。
os.environ["FVP_NO_FILE_LOG"] = "1"

if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def ensure_scripts_on_path() -> None:
    """飞书脚本不在包里，测试工具直接按路径导入。"""
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))


def configure_utf8_console() -> None:
    """Windows 控制台默认不是 UTF-8，断言信息里的中文会直接抛 UnicodeEncodeError。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass


configure_utf8_console()


# ── 环境变量沙箱 ──────────────────────────────────────────────────────────
class EnvSandbox:
    """快照并恢复整个 `os.environ`；每个用例一份，失败也不会污染下一个。"""

    def __init__(self) -> None:
        self._saved: dict[str, str] | None = None

    def start(self) -> "EnvSandbox":
        if self._saved is None:
            self._saved = dict(os.environ)
        return self

    def restore(self) -> None:
        if self._saved is None:
            return
        os.environ.clear()
        os.environ.update(self._saved)
        self._saved = None

    def set(self, **values: Any) -> None:
        for name, value in values.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = str(value)

    def pop(self, *names: str) -> None:
        for name in names:
            os.environ.pop(name, None)

    def for_test(self, tmp: str | Path) -> "EnvSandbox":
        """把设置、缓存、日志都指到本次用例的临时目录。"""
        tmp = Path(tmp)
        self.set(
            FVP_NO_FILE_LOG="1",
            FVP_SETTINGS_PATH=tmp / "settings.json",
            FVP_CACHE_DIR=tmp / "cache",
            FVP_CACHE="1",
            FVP_LOG_DIR=tmp / "logs",
        )
        return self

    def __enter__(self) -> "EnvSandbox":
        return self.start()

    def __exit__(self, *exc: Any) -> bool:
        self.restore()
        return False


def reset_cache_singletons() -> None:
    """清空缓存模块的单例（条目与计数），让每个用例从空缓存、零命中开始。

    预算类用例一律 new 一个 `ByteLRU`/`DiskCache`：模块单例的预算是**导入期**
    读环境变量得来的，运行中改 env 不会重建实例。这里只重置可变状态。
    """
    from fvpapp import cache

    for lru in (cache.frames, cache.images):
        with lru._lock:
            lru._items.clear()
            lru._bytes = 0
            lru._hits = lru._misses = lru._evictions = 0
            lru._oversize = lru._waits = lru._errors = 0
    for disk in (cache.thumbs, cache.index):
        with disk._lock:
            disk._bytes = 0
            disk._swept = False
            disk._broken = False
            disk._hits = disk._misses = disk._writes = 0
            disk._evictions = disk._errors = 0
    cache._legacy_cleaned = False


# ── 假 flet ───────────────────────────────────────────────────────────────
_FAKE_MARKER = "__fvp_fake_flet__"
_FAKE_SKIP = {"_args", "_kwargs", "controls", "update_threads", "updates"}


class FakeControl:
    """宽容的控件替身：未知属性返回同实例的稳定子替身，update() 记线程。"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        object.__setattr__(self, "_args", args)
        object.__setattr__(self, "_kwargs", dict(kwargs))
        object.__setattr__(self, "controls", [])
        object.__setattr__(self, "update_threads", [])
        object.__setattr__(self, "updates", 0)

    def __call__(self, *args: Any, **kwargs: Any) -> "FakeControl":
        return FakeControl(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__") or name in _FAKE_SKIP:
            raise AttributeError(name)
        if name in self._kwargs:
            return self._kwargs[name]
        # 真 flet 的 Text("x").value == "x"；替身也照此还原，别让断言读到子替身。
        if name == "value" and self._args and isinstance(self._args[0], str):
            return self._args[0]
        # 同一次属性访问要拿到同一个替身：`self.foo.visible = True` 之后再读
        # `self.foo.visible`，生产代码期望它还在。
        child = FakeControl()
        object.__setattr__(self, name, child)
        return child

    def update(self) -> None:
        self.updates += 1
        self.update_threads.append(threading.get_ident())

    def clean(self) -> None:
        return None


class FakeSession(FakeControl):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[str, Any]] = []

    async def invoke_method(self, target: Any, method: str, args: Any = None,
                            timeout: float | None = None) -> None:
        self.calls.append((method, args))


class FakeWindow(FakeControl):
    def __init__(self) -> None:
        super().__init__()
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

    async def destroy(self) -> None:
        return None


class FakePage(FakeControl):
    """ComposerApp 需要的 page 接口；不接 flet 会话。"""

    def __init__(self) -> None:
        super().__init__()
        self.session = FakeSession()
        self.window = FakeWindow()
        self.overlay: list[Any] = []
        self.services: Any = None
        self.dialogs: list[Any] = []
        self.added: list[Any] = []
        self.theme = None
        self.dark_theme = None
        self.theme_mode = None
        self.bgcolor = None
        self.padding = None
        self.spacing = None
        self.title = None
        self.on_keyboard_event = None
        self.on_disconnect = None

    def add(self, *controls: Any) -> None:
        self.added.extend(controls)

    def update(self, *args: Any, **kwargs: Any) -> None:
        self.updates += 1
        self.update_threads.append(threading.get_ident())

    def clean(self) -> None:
        self.added.clear()

    def show_dialog(self, dialog: Any) -> bool:
        self.dialogs.append(dialog)
        return True

    def pop_dialog(self) -> None:
        if self.dialogs:
            self.dialogs.pop()


class _FakeModule(types.ModuleType):
    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)
        return FakeControl()


def install_fake_flet() -> types.ModuleType:
    """把 `flet` / `flet.canvas` 换成宽容替身；真 flet 已导入就拒绝混跑。"""
    existing = sys.modules.get("flet")
    if existing is not None:
        if getattr(existing, _FAKE_MARKER, False):
            return existing
        raise RuntimeError(
            "真 flet 已在当前进程导入；假 flet 必须在单独进程里跑"
            "（用 tests/run_all.py 按文件分进程）"
        )
    flet = _FakeModule("flet")
    canvas = _FakeModule("flet.canvas")
    setattr(flet, _FAKE_MARKER, True)
    flet.canvas = canvas
    sys.modules["flet"] = flet
    sys.modules["flet.canvas"] = canvas
    return flet


def uninstall_fake_flet() -> None:
    """只拆自己装的替身；应用模块本身不在这里清，换进程才是干净做法。"""
    module = sys.modules.get("flet")
    if module is not None and getattr(module, _FAKE_MARKER, False):
        sys.modules.pop("flet", None)
        sys.modules.pop("flet.canvas", None)


# ── 真 flet 的页面替身 ────────────────────────────────────────────────────
_CURRENT_PAGE: list[Any] = []
_REAL_PAGE_DESCRIPTOR: Any = None


class RealFletPage(FakePage):
    """真 flet 控件树 + 假 Page。

    控件的 `.update()` 会沿 parent 链找 `BaseControl.page`；把它临时指到
    当前测试页，整棵树的操作就能真的执行一遍，而不用起 flet 会话。
    多个页面可以嵌套创建，最上面的那个接收更新；close() 时恢复描述符。
    """

    def __init__(self) -> None:
        super().__init__()
        self._closed = False
        self._patch_page_descriptor()
        _CURRENT_PAGE.append(self)

    @staticmethod
    def _patch_page_descriptor() -> None:
        global _REAL_PAGE_DESCRIPTOR
        if _REAL_PAGE_DESCRIPTOR is not None:
            return
        import flet.controls.base_control as base_control
        _REAL_PAGE_DESCRIPTOR = base_control.BaseControl.page
        base_control.BaseControl.page = property(
            lambda self: _CURRENT_PAGE[-1] if _CURRENT_PAGE else None
        )

    @staticmethod
    def _restore_page_descriptor() -> None:
        global _REAL_PAGE_DESCRIPTOR
        if _REAL_PAGE_DESCRIPTOR is None or _CURRENT_PAGE:
            return
        import flet.controls.base_control as base_control
        base_control.BaseControl.page = _REAL_PAGE_DESCRIPTOR
        _REAL_PAGE_DESCRIPTOR = None

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for index in range(len(_CURRENT_PAGE) - 1, -1, -1):
            if _CURRENT_PAGE[index] is self:
                del _CURRENT_PAGE[index]
                break
        self._restore_page_descriptor()

    def __enter__(self) -> "RealFletPage":
        return self

    def __exit__(self, *exc: Any) -> bool:
        self.close()
        return False


def walk_controls(root: Any):
    """递归走真实 flet 控件树（controls 列表 + 单个 content 子控件）。"""
    import flet as ft

    yield root
    for child in getattr(root, "controls", None) or []:
        yield from walk_controls(child)
    inner = getattr(root, "content", None)
    if isinstance(inner, ft.Control):
        yield from walk_controls(inner)


@dataclass
class ActionRow:
    row: Any
    hl: Any
    tile: Any


def _unwrap_column(node: Any) -> Any:
    """载入时角色外面套了错峰动画的 Container，剥壳取里层 Column。"""
    import flet as ft

    if (isinstance(node, ft.Container)
            and isinstance(getattr(node, "content", None), ft.Column)):
        return node.content
    return node


def _tile_title(ctrl: Any) -> Any:
    title = getattr(ctrl, "title", None)
    return getattr(title, "value", None)


def find_action_row(app: Any, filename: str) -> ActionRow:
    """按文件名在真实控件树里找回动作行，不依赖 `app._action_rows` 私有表。"""
    parts = filename.split("_")
    role = parts[1] if len(parts) >= 3 else filename
    action = parts[2] if len(parts) >= 3 else filename
    outfit = parts[3] if len(parts) >= 4 else "默认"

    for role_node in app.tree_list.controls:
        role_col = _unwrap_column(role_node)
        controls = getattr(role_col, "controls", [])
        if len(controls) < 2 or _tile_title(controls[0]) != role:
            continue
        for outfit_node in controls[1].controls:
            outfit_col = _unwrap_column(outfit_node)
            octls = getattr(outfit_col, "controls", [])
            if len(octls) < 2 or _tile_title(octls[0]) != outfit:
                continue
            for row in octls[1].controls:
                hl = getattr(row, "content", None)
                tile = getattr(hl, "content", None)
                if tile is not None and _tile_title(tile) == action:
                    return ActionRow(row=row, hl=hl, tile=tile)
    raise AssertionError(f"树里找不到动作行: {filename}")


# ── 夹具 ──────────────────────────────────────────────────────────────────
def make_hzc_bytes(image_type: int = 1, w: int = 8, h: int = 8,
                   frames: int = 1, salt: int = 0) -> bytes:
    """最小但合法的 HZC：44 字节头 + zlib 像素。

    纯线性序列周期 256 正好等于一帧字节数，多帧会解出一模一样的画面；
    混进帧号并以 251 为周期，翻帧断言才看得出变化。
    """
    head = bytearray(44)
    head[0:4] = b"hzc1"
    struct.pack_into("<H", head, 18, image_type)
    struct.pack_into("<H", head, 20, w)
    struct.pack_into("<H", head, 22, h)
    struct.pack_into("<I", head, 32, frames)
    bpp = 3 if image_type == 0 else 4
    need = w * h * bpp * (frames if image_type == 2 else 1)
    stride = w * h * bpp
    body = bytes(((i % 251) * 7 + (i // stride) * 53 + salt) % 256 for i in range(need))
    return bytes(head) + zlib.compress(body)


def make_hzc(path: str | Path, image_type: int = 1, w: int = 8, h: int = 8,
             frames: int = 1, salt: int = 0) -> Path:
    """写一个 HZC 文件；文件名也掺进 salt，同名不同资源画面才真的不同。"""
    path = Path(path)
    name_salt = int(hashlib.sha1(path.name.encode("utf-8")).hexdigest()[:8], 16) % 251
    path.write_bytes(make_hzc_bytes(image_type, w, h, frames, salt + name_salt))
    return path


def build_bin(path: str | Path, names: list[str]) -> int:
    """写一个最小 .bin：文件表 + 文件名区 + 每条一个 HZC；返回数据区起点。"""
    path = Path(path)
    payloads = [make_hzc_bytes() for _ in names]
    names_blob = b""
    rel_offsets = []
    for name in names:
        rel_offsets.append(len(names_blob))
        names_blob += name.encode("shift-jis") + b"\x00"
    x, y = len(names), len(names_blob)
    data_start = 8 + 12 * x + y
    out = bytearray(struct.pack("<II", x, y))
    abs_offset = data_start
    for index, payload in enumerate(payloads):
        out += struct.pack("<III", rel_offsets[index], abs_offset, len(payload))
        abs_offset += len(payload)
    out += names_blob
    for payload in payloads:
        out += payload
    path.write_bytes(bytes(out))
    return data_start


def build_fixture(salt: int = 0, include_nodiff: bool = True) -> Path:
    """标准 UI 夹具：两个角色 × 两套服装 × 两个动作，外加多帧底图。"""
    tmp = Path(tempfile.mkdtemp(prefix="fvp_fixture_"))
    for role in ("翠", "真紅"):
        for outfit in ("私服", "制服"):
            for action in ("基", "喜"):
                make_hzc(tmp / f"CHR_{role}_{action}_{outfit}.hzc", salt=salt)
                make_hzc(tmp / f"CHR_{role}_{action}_{outfit}_表情.hzc",
                         image_type=2, frames=3, salt=salt)
    if include_nodiff:
        # 没有 _表情 变体的多帧底图：走「直接导出底图」那条退路
        make_hzc(tmp / "CHR_翠_无_私服.hzc", image_type=2, frames=3, salt=salt)
    return tmp


def build_large_fixture(count: int = 400) -> Path:
    """一个服装下塞很多动作，用来制造能看见进度条的慢载入。"""
    tmp = Path(tempfile.mkdtemp(prefix="fvp_large_fixture_"))
    for index in range(count):
        make_hzc(tmp / f"CHR_翠_动作{index:03d}_私服.hzc")
    return tmp


# ── 事件轮询与线程池收尾 ──────────────────────────────────────────────────
async def pump_until(pred: Callable[[], bool], timeout: float = 3.0,
                     interval: float = 0.01, what: str = "条件") -> bool:
    """轮询谓词直到成立；超时抛 AssertionError（别再用固定 sleep 赌时间）。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        if pred():
            return True
        if loop.time() >= deadline:
            raise AssertionError(f"等待超时（{timeout}s）：{what}")
        await asyncio.sleep(interval)


def wait_until(pred: Callable[[], bool], timeout: float = 3.0,
               interval: float = 0.001, what: str = "条件") -> bool:
    """同步版谓词轮询：给后台线程用例等一个可观察条件，别用固定 sleep 猜。"""
    deadline = time.monotonic() + timeout
    while True:
        if pred():
            return True
        if time.monotonic() >= deadline:
            raise AssertionError(f"等待超时（{timeout}s）：{what}")
        time.sleep(interval)


async def drain_thumb_pool(app: Any = None, timeout: float = 5.0) -> None:
    """作废当前载入代数，并等缩略图线程池与 inflight 全部落地。

    线程池是模块级单例，`call_soon_threadsafe` 的推送可能在用例结束后才回来；
    不排干就会把上一用例的控件更新带进下一用例。
    """
    if app is not None:
        app._state_gen += 1
    try:
        import fvpapp.resources as resources
    except Exception:
        return
    from fvpapp import cache

    loop = asyncio.get_running_loop()
    try:
        barrier = resources._THUMB_POOL.submit(lambda: None)
    except RuntimeError:
        return
    await asyncio.to_thread(barrier.result)

    deadline = loop.time() + timeout
    while True:
        with cache.images._lock:
            busy = bool(cache.images._inflight)
        if not busy:
            break
        if loop.time() >= deadline:
            break
        await asyncio.sleep(0.01)
    # 让 loop 把已经排进来的 call_soon_threadsafe 处理掉
    for _ in range(5):
        await asyncio.sleep(0)


# ── 日志/钩子沙箱 ─────────────────────────────────────────────────────────
class CrashlogSandbox:
    """保存并恢复 crashlog 的全部全局状态（日志 handler、异常钩子、faulthandler）。"""

    def __init__(self) -> None:
        self._saved: dict[str, Any] | None = None

    def start(self) -> "CrashlogSandbox":
        import fvpapp.crashlog as cl

        root = logging.getLogger()
        self._saved = {
            "cl": cl,
            "sys_excepthook": sys.excepthook,
            "threading_excepthook": threading.excepthook,
            "unraisablehook": getattr(sys, "unraisablehook", None),
            "hooked": cl._hooked,
            "log_path": cl._log_path,
            "file_handler": cl._file_handler,
            "fault_stream": cl._fault_stream,
            "exit_logged": cl._exit_logged,
            "atexit_done": cl._atexit_done,
            "root_handlers": list(root.handlers),
            "root_level": root.level,
            "app_level": logging.getLogger(cl.APP_LOGGER).level,
        }
        return self

    def restore(self) -> None:
        if self._saved is None:
            return
        import faulthandler

        cl = self._saved["cl"]
        root = logging.getLogger()
        saved_handlers = self._saved["root_handlers"]
        for handler in list(root.handlers):
            if handler not in saved_handlers:
                root.removeHandler(handler)
                try:
                    handler.close()
                except Exception:
                    pass
        root.handlers[:] = saved_handlers
        root.setLevel(self._saved["root_level"])
        logging.getLogger(cl.APP_LOGGER).setLevel(self._saved["app_level"])

        if cl._file_handler is not self._saved["file_handler"] and cl._file_handler is not None:
            try:
                cl._file_handler.close()
            except Exception:
                pass
        if cl._fault_stream is not self._saved["fault_stream"]:
            try:
                faulthandler.disable()
            except Exception:
                pass
            if cl._fault_stream is not None:
                try:
                    cl._fault_stream.close()
                except Exception:
                    pass
            saved_stream = self._saved["fault_stream"]
            if saved_stream is not None:
                try:
                    faulthandler.enable(file=saved_stream, all_threads=True)
                except Exception:
                    pass

        cl._hooked = self._saved["hooked"]
        cl._log_path = self._saved["log_path"]
        cl._file_handler = self._saved["file_handler"]
        cl._fault_stream = self._saved["fault_stream"]
        cl._exit_logged = self._saved["exit_logged"]
        cl._atexit_done = self._saved["atexit_done"]

        sys.excepthook = self._saved["sys_excepthook"]
        threading.excepthook = self._saved["threading_excepthook"]
        if self._saved["unraisablehook"] is not None:
            sys.unraisablehook = self._saved["unraisablehook"]
        self._saved = None

    def __enter__(self) -> "CrashlogSandbox":
        return self.start()

    def __exit__(self, *exc: Any) -> bool:
        self.restore()
        return False


# ── 基类 ──────────────────────────────────────────────────────────────────
class _EnvMixin:
    """每用例独立 tmpdir + 环境沙箱 + 缓存单例重置。"""

    tmp: Path

    def _setup_env(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="fvp_test_")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.sandbox = EnvSandbox().start()
        self.addCleanup(self.sandbox.restore)
        self.sandbox.for_test(self.tmp)
        reset_cache_singletons()
        self.addCleanup(reset_cache_singletons)


class EnvTestCase(_EnvMixin, unittest.TestCase):
    def setUp(self) -> None:  # noqa: N802 - unittest 命名
        super().setUp()
        self._setup_env()


class AsyncEnvTestCase(_EnvMixin, unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:  # noqa: N802
        super().setUp()
        self._setup_env()
