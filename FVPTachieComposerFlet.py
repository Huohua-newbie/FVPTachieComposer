"""FVP Tachie Composer — Flet UI

FVP engine character sprite viewer & composer.
HZC / BIN 解析与合成内核在 `fvpapp/engine.py`。

界面本身按四块拆在 `fvpapp/` 下：
  ui.py（布局与控件树）/ state.py（主题、提示、选中态）/ resources.py（解析与载入）/
  export.py（导出）。这里只剩 `__init__` 与入口。
旧脚本/测试引用的名字仍然从这个模块导出一份。

Dependencies: flet>=0.86  Pillow>=10
Run: python FVPTachieComposerFlet.py
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

# ── 日志 / 崩溃落盘 ───────────────────────────────────────
# 必须在 `import flet` 之前装：flet 导入期或客户端拉起阶段出的错也要留证据。
# 控制台行为不变（默认 WARNING 以上），文件里则是完整的 DEBUG。
# 位置：<应用根目录>\logs\fvptachie-<版本>-<时间>.log（打包态即 exe 旁边）
# 用户报错时让他把这个文件发过来就行，界面上原本的提示一律不改。
from fvpapp.crashlog import _flush as _flush_log  # noqa: F401
from fvpapp.crashlog import install as _install_logging
from fvpapp.crashlog import install_asyncio_handler
from fvpapp.crashlog import mark_exit_once

log = logging.getLogger("fvptachie")
LOG_PATH = _install_logging()

import flet as ft  # noqa: E402  （要装在日志之后）

from fvpapp.drop import DropFeedbackMixin
from fvpapp.engine import (  # noqa: F401  （内核，历史脚本从这里取）
    compose_preview,
    hzc_data_to_pil_list,
    hzc_decode_first_frame,
    parse_bin_info_extended,
    parse_dir_infos,
)
from fvpapp.dropzone import (  # noqa: F401  （测试/脚本从这里取）
    DROPZONE_ON,
    _custom_client_available,
    _dropzone,
    _dropzone_on,
    pick_drop_path,
)
from fvpapp.export import ExportMixin
from fvpapp.media import (  # noqa: F401  （历史脚本按老路径取这些工具）
    EXPR_SUFFIXES,
    _cached_src,
    _cached_thumb,
    _checker_src,
    _find_expr_info,
    _img_bytes,
    _img_src,
    _is_expr,
    _make_thumb_widget,
    _part_thumb_srcs,
    _thumb_bytes,
)
from fvpapp.resources import ResourceMixin
from fvpapp.state import StateMixin
from fvpapp.theme import (  # noqa: F401  （历史脚本按老路径取这些常量）
    ANIM_ROW,
    ANIM_VEIL,
    BUSY_DELAY,
    GUTTER,
    INDENT_ACTION,
    INDENT_OUTFIT,
    PART_HINT_EMPTY,
    PART_HINT_IDLE,
    PART_STRIP_H,
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
from fvpapp.ui import UiMixin


class ComposerApp(UiMixin, StateMixin, ResourceMixin, ExportMixin, DropFeedbackMixin):
    def __init__(self, page: ft.Page):
        self.page = page
        self.is_dark = False
        self.ink = _palette(False)

        self.input_file = None
        self.file_infos = []
        self.hierarchy = {}
        self.selected_info = None
        self.selected_filename = None

        self.base_imgs = []
        self.base_idx = 0
        self.part_info = None
        self.part_imgs = []
        self.part_idx = 0
        self.composed_img = None
        # 没有表情差分可用：结果面板与导出链路都走「直接导出底图」这条退路
        self.base_only = False
        self.thumb_refs = []
        self.role_thumb_cache = {}
        # 图缓存的资源代号：图都按文件名索引，换一份 BIN 后代号 +1，
        # 在途的编码任务回来时对不上号就不写缓存，免得同名动作串图。
        self._cache_token = 0
        # 树的展开状态记在 app 上而不是 tile 闭包里，
        # 这样换主题整树重建后能原样展开回去（否则用户展开的角色/服装会被强制收起）。
        self.open_roles = set()
        self.open_outfits = set()
        self._tree_toggles = {}
        # 角色错峰入场：本次载入的角色外壳（透明度/位移在 _stagger_next 里逐条推）
        self._tree_wraps = []
        self._stagger_i = 0
        self._stagger_gen = -1
        # 重建树期间屏蔽"展开即选中"等级联副作用，见 _outfit_tile.toggle
        self._restoring = False
        # ── 拖放态（画布长在角色库面板上）────────────────────
        # 文件是否正悬在面板上方；文案和错峰入场都看它。
        self._drop_armed = False
        # 画布在册（visible）吗。动画全靠逐帧重绘，见 _drop_open / _tick_drop。
        self._drop_hint_shown = False
        # 画布尺寸：Canvas 不参与排版时不会有尺寸，重新亮起来后要靠它补出第一帧
        self._drop_size = (0.0, 0.0)
        # 整层淡入淡出进度 0→1（算进每个 shape 的颜色 alpha）与它的方向
        self._drop_a = 0.0
        self._drop_dir = 0
        # 配色（accent / err）与光斑弧长
        self._drop_tone = "accent"
        self._drop_dist = 0.0
        # 计时器代数：+1 即可让所有在途的 tick / 错峰链自己作废，
        # 不用去 cancel 具体的 call_later 句柄。
        self._drop_gen = 0
        self._drop_canvas = None
        self._hint_icon = None
        self._hint_title = None
        self._hint_sub = None
        self._hint_idle_sub = ""
        self._hint_state = "idle"
        # 树里所有动作行的选中态控件组；换选时按 filename 统一复原，不记"上一次点了谁"。
        self._action_rows = []
        # 载入代数：载入/载出各 +1。后台解析与缩略图解码都跨"载入中→载出"这段
        # 时间，靠它判定结果是否已经过期——否则载出之后迟到的回调会把状态写回来。
        self._state_gen = 0
        # 载出按钮（_build_tree_column 建）；仅在已载入时可见
        self.unload_btn = None

        # UI 事件循环引用（_load_bin/_toggle_theme 中捕获）：
        # 后台缩略图解码完成后，经由它把“替换+刷新”调度回 UI 线程执行，
        # 避免后台线程直接碰控件导致的更新丢失。
        self._loop = None

        self.file_picker = ft.FilePicker()
        self.clipboard = ft.Clipboard()
        self.page.services = [self.file_picker, self.clipboard]
        # 导入/导出各自记住自己的目录：Windows 原生对话框按进程记忆最后位置，
        # 不分别传 initial_directory 的话，导出一选完，下次打开 bin 就被带去导出目录。
        self.import_dir = None
        self.export_dir = None
        self._snack_seq = 0
        self._snack_overlay = None
        self._snack_toast = None
        # busy 横幅：_parsing_gen 非空表示"这一代解析还在途中"，_busy_seq 是那条卡片
        self._parsing_gen = None
        self._busy_seq = None
        # 状态栏那条进度条亮了吗：慢载入才亮，快路径全程不出现
        self._parse_shown = False
        # 成功提示走状态栏：记住最近一次的目标文件夹与代数
        self._status_folder = None
        self._status_gen = 0
        self.status_action = None
        self._setup_page()
        self._build()



async def main(page: ft.Page):
    try:
        install_asyncio_handler(asyncio.get_running_loop())
    except RuntimeError:
        pass
    try:
        app = ComposerApp(page)
    except Exception as ex:
        # 界面照旧提示（这是用户看得见的那一句），同时把栈留在日志文件里
        log.exception("界面初始化失败")
        try:
            page.add(ft.Text(f"初始化失败: {ex}", color=ft.Colors.ERROR))
            page.update()
        except Exception:
            log.exception("初始化失败的提示都没能显示出来")
        return
    log.info("界面已就绪")
    # 关窗时 atexit 在打包态跑不到（客户端直接结束内嵌进程），断连事件是
    # 唯一还来得及写「正常退出」的时机。
    page.on_disconnect = lambda e: mark_exit_once()
    # 命令行/拖到程序图标打开：自动载入首个存在的 .bin/目录参数。
    # dev 态参数在 argv[1:]（argv[0] 是脚本自身）；打包态 SeriousPython 把
    # 用户参数从 argv[0] 起放（实测 argv=['D:\\...\\graph_bs.bin']，无脚本名）。
    # 脚本路径以 .py 结尾不会被筛选误命中，两种形态直接一起扫。
    # 打包态能走到这里还依赖 client\main.dart 的 FVP-PATCH：flet 0.86.5 原生
    # 客户端会把「桌面 + 位置参数」当成 developer mode，嵌入 Python 根本不启动。
    args = [a for a in sys.argv if not a.endswith(".py")]
    target = next((a for a in args
                   if (p := Path(a)).exists()
                   and (p.is_dir() or p.suffix.lower() == ".bin")),
                  None)
    if target:
        await app._load_bin(target)
    elif args:
        # 拖了个不存在/不支持的东西进来也要吭一声：打包态参数从 argv[0] 起，
        # 拿 len(argv)>1 判会整个静默掉（实测拖已删除的文件就毫无提示）。
        app._snack(f"无法打开: {args[0]}", error=True)
    return app



if __name__ == "__main__":
    try:
        ft.run(main)
    except BaseException:
        # 启动/运行期直接抛到这里（连 flet 的崩溃页都没机会显示）：
        # 日志先落地，再把原异常原样抛回去——PyInstaller 的崩溃弹窗、
        # 控制台 traceback 全都保持原来的行为。
        log.critical("启动或运行失败，程序退出", exc_info=True)
        _flush_log()
        raise
