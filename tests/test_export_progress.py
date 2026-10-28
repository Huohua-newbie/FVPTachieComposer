"""无头回归：批量导出的进度、取消、失败清单与「只重试失败项」。

轻量 stub 顶掉 flet，PIL 用真的（要真的写 PNG），直接驱动 ExportMixin：
python tests/test_export_progress.py
"""
import asyncio
import sys
import tempfile
import threading
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


# ---------- Fake flet（只提供属性，不渲染） ----------
class _Any:
    def __init__(self, *a, **k):
        self._args = a
        self._kwargs = dict(k)
        self.controls = []

    def __call__(self, *a, **k):
        return _Any(*a, **k)

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        if name in self._kwargs:
            return self._kwargs[name]
        return _Any()

    def update(self):
        return None


class FakePage(_Any):
    def __init__(self):
        super().__init__()
        self.overlay = []
        self.services = None
        self.window = _Any()
        self.dialogs = []

    def show_dialog(self, dlg):
        self.dialogs.append(dlg)

    def pop_dialog(self):
        if self.dialogs:
            self.dialogs.pop()
        return None


class _FtModule(types.ModuleType):
    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return _Any()


_flet = _FtModule("flet")
_canvas = _FtModule("flet.canvas")
_flet.canvas = _canvas
sys.modules["flet"] = _flet
sys.modules["flet.canvas"] = _canvas

from PIL import Image  # noqa: E402

import fvpapp.export as export_mod  # noqa: E402
from FVPTachieComposerFlet import ComposerApp  # noqa: E402

# 进度节流在真机上是为了别把事件循环淹了；测试里要看到每一帧，关掉。
export_mod.PROGRESS_PUSH_S = 0.0

fails = []


def check(name, ok):
    print(("PASS " if ok else "FAIL ") + name)
    if not ok:
        fails.append(name)


def new_app(tmp, frames, base_only=False):
    page = FakePage()
    app = ComposerApp(page)
    app.selected_info = {"filename": "CHR_T_基_私服"}
    app.selected_filename = "CHR_T_基_私服"
    app.base_imgs = [Image.new("RGBA", (8, 8), (10, 20, 30, 255))]
    app.part_imgs = [Image.new("RGBA", (8, 8), (i * 7 % 256, 40, 90, 255))
                     for i in range(frames)]
    app.part_info = {"offset_x": 0, "offset_y": 0}
    app.base_only = base_only
    app.export_dir = None

    async def fake_dir(dialog_title=None, initial_directory=None, **kw):
        return str(tmp)

    app.file_picker.get_directory_path = fake_dir
    app._loop = asyncio.get_running_loop()
    return app


def spy_progress(app, sink, on_frame=None):
    real = app._show_export_progress

    def spy(gen, done, total):
        sink.append((gen, done, total))
        real(gen, done, total)
        if on_frame is not None:
            on_frame(done, total)

    app._show_export_progress = spy
    return real


async def main():
    root = Path(tempfile.mkdtemp(prefix="export_prog_"))

    # ── 1. 成功路径：逐张进度、末帧报满、收尾全部收起 ───────────────────
    out = root / "ok"
    out.mkdir()
    app = new_app(out, 12)
    frames = []
    spy_progress(app, frames)
    await app._batch_export(None)
    await asyncio.sleep(0.05)

    names = sorted(p.name for p in out.glob("*.png"))
    check("12 帧全部写盘", len(names) == 12 and names[0].endswith("_diff_000.png"))
    check("进度单调递增", all(b[1] >= a[1] for a, b in zip(frames, frames[1:])))
    check("末次进度报满", bool(frames) and frames[-1][1] == frames[-1][2] == 12)
    check("成功后状态栏报张数", "已导出 12 张" in app.status_text.value)
    check("成功后进度条收起", app.parse_progress.visible is False
          and app.parse_progress.value is None)
    check("成功后「取消」收起", app.export_cancel_btn.visible is False)
    check("成功时没有失败项按钮", app.status_fail_action.visible is False)

    # 进度条与取消按钮在导出途中确实是亮的：再跑一次，在回调里看
    mid = {}
    out2 = root / "mid"
    out2.mkdir()
    app2 = new_app(out2, 6)

    def look(done, total):
        if done == 1 and not mid:
            mid["bar"] = app2.parse_progress.visible
            mid["cancel"] = app2.export_cancel_btn.visible
            mid["text"] = app2.status_text.value

    spy_progress(app2, [], look)
    await app2._batch_export(None)
    await asyncio.sleep(0.05)
    check("导出途中进度条亮着", mid.get("bar") is True)
    check("导出途中「取消」可见", mid.get("cancel") is True)
    check("导出途中状态栏写「正在批量导出」", mid.get("text") == "正在批量导出…")

    # ── 2. 取消：停下后不再新增文件，已写出的保留 ───────────────────────
    out3 = root / "cancel"
    out3.mkdir()
    app3 = new_app(out3, 40)
    cancelled_at = {}

    def cancel_after_first(done, total):
        if done >= 1 and not cancelled_at:
            cancelled_at["done"] = done
            app3._cancel_export()

    spy_progress(app3, [], cancel_after_first)
    await app3._batch_export(None)
    await asyncio.sleep(0.1)
    written = len([p for p in out3.glob("*.png") if p.is_file()])
    check("取消后写出的少于总数", 0 < written < 40)
    check("取消后状态栏说已取消", "已取消导出" in app3.status_text.value)
    check("取消后报的张数与磁盘一致", f"已写出 {written} 张" in app3.status_text.value)
    check("取消后进度条收起", app3.parse_progress.visible is False)
    check("取消后「取消」按钮收起", app3.export_cancel_btn.visible is False)
    check("取消后取消位已复位", app3._export_cancel is None)
    check("取消保留已写出的文件（没删）", written > 0)

    # ── 3. 失败清单：文件名与原因都留得住 ───────────────────────────────
    out4 = root / "fail"
    out4.mkdir()
    app4 = new_app(out4, 6)
    # 拿一个同名目录占住第 3 张的落点，写盘必然抛 IsADirectoryError
    (out4 / "CHR_T_基_私服_diff_003.png").mkdir()
    await app4._batch_export(None)
    await asyncio.sleep(0.05)
    check("失败只影响那一张，其余照常写盘",
          len([p for p in out4.glob("*.png") if p.is_file()]) == 5)
    check("失败状态栏报总数", "5 张成功" in app4.status_text.value
          and "1 张失败" in app4.status_text.value)
    check("失败清单记了文件名与原因",
          len(app4._export_failed) == 1
          and app4._export_failed[0][2] == "CHR_T_基_私服_diff_003.png"
          and "Error" in app4._export_failed[0][3])
    check("失败后右侧出现「查看失败项」", app4.status_fail_action.visible is True)

    # ── 4. 只重试失败项：成功的不重写，失败项补上 ───────────────────────
    (out4 / "CHR_T_基_私服_diff_003.png").rmdir()
    retried = []
    spy_progress(app4, retried)
    await app4._retry_failed(None)
    await asyncio.sleep(0.05)
    check("重试后失败项补齐", (out4 / "CHR_T_基_私服_diff_003.png").is_file())
    check("重试只跑一张（没有重写其余 5 张）",
          bool(retried) and retried[-1][1] == retried[-1][2] == 1)
    check("重试成功后清单清空", app4._export_failed == []
          and app4.status_fail_action.visible is False)
    check("重试成功后状态栏报张数", "已导出 1 张" in app4.status_text.value)

    # ── 5. 过期代数：作废后的回调不许再改状态栏 ─────────────────────────
    stale_gen = retried[-1][0]
    before_text = app4.status_text.value
    before_meta = app4.status_meta.value
    app4._show_export_progress(stale_gen, 3, 9)
    check("过期进度帧不动状态栏",
          app4.status_text.value == before_text and app4.status_meta.value == before_meta)
    check("过期帧也不会把进度条重新点亮", app4.parse_progress.visible is False)

    # ── 6. 换资源即中止：取消位置起、反馈收起、代数作废 ─────────────────
    app5 = new_app(root / "abort", 8)
    gen_before = app5._export_gen
    app5._export_cancel = threading.Event()
    app5._export_shown = True
    app5.parse_progress.visible = True
    app5.parse_progress.value = 0.4
    app5.export_cancel_btn.visible = True
    app5._abort_export()
    check("中止会置起取消位", app5._export_cancel is None)
    check("中止会作废代数", app5._export_gen != gen_before)
    check("中止收起进度条与取消按钮",
          app5.parse_progress.visible is False and app5.export_cancel_btn.visible is False)
    late_gen = gen_before + 1
    app5._show_export_progress(late_gen, 4, 8)
    check("中止后迟到的进度帧被丢弃", app5.parse_progress.visible is False)

    # ── 7. 无差分模式：命名走 _base_，取消同样生效 ───────────────────────
    out6 = root / "base_only"
    out6.mkdir()
    app6 = new_app(out6, 4, base_only=True)
    # 无差分模式铺平的是底图自己的帧，不是差分帧
    app6.base_imgs = [Image.new("RGBA", (8, 8), (i * 11 % 256, 70, 30, 255))
                      for i in range(4)]
    await app6._batch_export(None)
    await asyncio.sleep(0.05)
    names6 = sorted(p.name for p in out6.glob("*.png") if p.is_file())
    check("无差分模式导出走 _base_ 命名",
          names6 == [f"CHR_T_基_私服_base_{i:03d}.png" for i in range(4)])

    if fails:
        print("\nFAILED:", fails)
        sys.exit(f"EXPORT_PROGRESS_TEST_FAILED: {fails}")
    print("EXPORT_PROGRESS_OK")


asyncio.run(main())
