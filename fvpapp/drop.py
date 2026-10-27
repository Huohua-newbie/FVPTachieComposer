"""拖放反馈：画在角色库面板上的虚线与光斑，以及松手后的载入。"""

from __future__ import annotations

import logging
import math
import os
import sys
from pathlib import Path

import flet as ft
import flet.canvas as fc

from .dropzone import DROPZONE_ON, _dropzone, pick_drop_path
from .theme import ANIM_VEIL, RADIUS_PANEL, _rgb

log = logging.getLogger("fvptachie")

DROP_TICK = 0.03
DROP_IN_FRAMES = 6
DROP_OUT_FRAMES = 4
DROP_INSET = 0.0
DROP_STREAK = 30.0
DROP_STEP = 12.0
DROP_REJECT_MS = 160


class DropFeedbackMixin:
    """拖放反馈：整扇窗口的落点、提示文案与动画画布。

    只依赖宿主实例上已有的状态与控件（``_drop_canvas`` / ``empty_hint`` /
    ``tree_list`` / ``_hint_*`` / ``_state_gen`` …），因此以 mixin 形式挂在
    ``ComposerApp`` 上，状态归属不变。
    """

    def _drop_wrap(self, content):
        """把整扇窗口包成拖放目标；未启用（官方客户端没有该插件）时原样返回。

        落点铺满全窗：文件一进窗口就该有反应（进场亮面板、松手即清场），
        等它挪到角色库栏才动会让人以为没接住。反馈视觉仍画在角色库面板的画布上（_build_tree_column 的 overlay），落点大、反馈贴着"资源入口"。
        """
        if not DROPZONE_ON:
            return content
        # expand 不可省：Dropzone 是 LayoutControl，没有尺寸时 Dart 侧按 0 高度排版，
        # 整棵树静默消失（白屏、无异常、无日志）。
        return _dropzone.Dropzone(
            expand=True,
            content=content,
            on_dropped=self._on_dropped,
            on_entered=lambda e: self._drag_entered(),
            on_exited=lambda e: self._drag_exited(),
        )

    def _drop_rgba(self, alpha_max=255):
        """当前色调（accent / err_frame）× 淡入进度，拼成 #AARRGGBB。

        Flet 的 hex 是 alpha 在最前，拼反了会被当成另一个色（近乎不透明），
        整块面板变成一坨脏色——这里只有一处拼接，别再手写第二处。
        """
        rgb = _rgb(self.ink["err_frame" if self._drop_tone == "err" else "frame"])
        a = max(0, min(255, int(round(alpha_max * self._drop_a))))
        return "#%02X%02X%02X%02X" % (a, *rgb)

    def _frame_point(self, w, h, m, d):
        """圆角矩形周长上的参数化取点：顺时针，从上边直段的起点开始。

        光斑要贴着框边爬，而 Flet 的 Paint 只有 stroke_dash_pattern、没有
        stroke_dash_offset，没法靠 dash 位移做"行进"，只能自己按弧长算点。
        """
        r = min(RADIUS_PANEL - m, (w - 2 * m) / 2.0, (h - 2 * m) / 2.0)
        lx = max(0.0, w - 2 * m - 2 * r)
        ly = max(0.0, h - 2 * m - 2 * r)
        arc = math.pi * r / 2.0
        d %= 2 * lx + 2 * ly + 8 * arc

        def take(n):
            nonlocal d
            if d <= n:
                return True
            d -= n
            return False

        if take(lx):                                    # 上直边 →
            return (m + r + d, m)
        if take(arc):                                   # 右上圆角
            a = d / arc * math.pi / 2
            return (w - m - r + r * math.sin(a), m + r - r * math.cos(a))
        if take(ly):                                    # 右直边 ↓
            return (w - m, m + r + d)
        if take(arc):                                   # 右下圆角
            a = d / arc * math.pi / 2
            return (w - m - r + r * math.cos(a), h - m - r + r * math.sin(a))
        if take(lx):                                    # 下直边 ←
            return (w - m - r - d, h - m)
        if take(arc):                                   # 左下圆角
            a = d / arc * math.pi / 2
            return (m + r - r * math.sin(a), h - m - r + r * math.cos(a))
        if take(ly):                                    # 左直边 ↑
            return (m, h - m - r - d)
        a = d / arc * math.pi / 2                       # 左上圆角
        return (m + r - r * math.cos(a), m + r - r * math.sin(a))

    def _streak(self, w, h, m, head, color, width):
        """一截沿框边爬的拖尾。按弧长采 4 段折线，过圆角时才不会切角。"""
        pts = [self._frame_point(w, h, m, head - i * DROP_STREAK / 3.0)
               for i in range(4)]
        els = [fc.Path.MoveTo(x=pts[0][0], y=pts[0][1])]
        els += [fc.Path.LineTo(x=p[0], y=p[1]) for p in pts[1:]]
        return fc.Path(
            elements=els,
            paint=ft.Paint(
                style=ft.PaintingStyle.STROKE,
                stroke_width=width,
                stroke_cap=ft.StrokeCap.ROUND,
                color=color,
            ),
        )

    def _drop_shapes(self, w, h):
        """画布的全部内容：底纹 + 辉光边 + 虚线边 + 爬行的光斑（拖尾 + 核心）。

        五个形状都贴面板外框走。整层的淡入淡出不走 Container 的隐式动画
        （0.86 没有 animate_color），而是把进度算进每个 shape 的颜色 alpha，
        所以一切由 Python 逐帧重绘。
        """
        m = DROP_INSET
        if self._drop_a <= 0.0 or w <= m * 2 + 4 or h <= m * 2 + 4:
            return []
        box = dict(x=m, y=m, width=w - 2 * m, height=h - 2 * m,
                   border_radius=ft.BorderRadius.all(RADIUS_PANEL - m))
        return [
            # 底纹 = "我在听"。最深只有 18% 的强调色，不能压过面板里的文字。
            fc.Path(
                elements=[fc.Path.Rect(x=0, y=0, width=w, height=h,
                                       border_radius=ft.BorderRadius.all(RADIUS_PANEL))],
                paint=ft.Paint(style=ft.PaintingStyle.FILL,
                               color=self._drop_rgba(0x2E)),
            ),
            fc.Path(
                elements=[fc.Path.Rect(**box)],
                paint=ft.Paint(style=ft.PaintingStyle.STROKE,
                               stroke_width=6, color=self._drop_rgba(0x33)),
            ),
            # 虚线边 = "落点就是这块面板"。它替代的是面板那 1px 实描边，
            # 所以圆角、位置都必须跟面板本体严丝合缝，否则就又是"贴了一层"。
            fc.Path(
                elements=[fc.Path.Rect(**box)],
                paint=ft.Paint(style=ft.PaintingStyle.STROKE,
                               stroke_width=2, color=self._drop_rgba(),
                               stroke_dash_pattern=[10, 7]),
            ),
            self._streak(w, h, m, self._drop_dist, self._drop_rgba(0x59), 8),
            self._streak(w, h, m, self._drop_dist, self._drop_rgba(), 2.5),
        ]

    def _redraw_drop(self):
        canvas = self._drop_canvas
        if canvas is None:
            return
        w, h = self._drop_size
        canvas.shapes = self._drop_shapes(w, h)
        self._safe_update(canvas)

    def _on_drop_resize(self, e):
        self._drop_size = (float(e.width), float(e.height))
        self._redraw_drop()

    def _tick_drop(self, gen):
        """淡入淡出和光斑爬行共用这一条计时链，只在画布在册时跑。"""
        if gen != self._drop_gen or not self._drop_hint_shown:
            return
        if self._drop_dir > 0:
            self._drop_a = min(1.0, self._drop_a + 1.0 / DROP_IN_FRAMES)
            if self._drop_a >= 1.0:
                self._drop_dir = 0
        elif self._drop_dir < 0:
            self._drop_a = max(0.0, self._drop_a - 1.0 / DROP_OUT_FRAMES)
            if self._drop_a <= 0.0:
                # 走完就收画布：Canvas 会吃掉自己矩形内的点击，留着树就点不动了
                self._drop_dir = 0
                self._close_drop_canvas()
                return
        self._drop_dist += DROP_STEP
        self._redraw_drop()
        self._later(DROP_TICK, self._tick_drop, gen)

    def _drop_open(self):
        """进入拖放态：画布先亮起来，淡入交给 _tick_drop 逐帧推。"""
        if self._drop_canvas is None:
            return
        self._drop_gen += 1              # 在途的淡出链就此作废
        self._drop_hint_shown = True
        self._drop_tone = "accent"
        self._drop_dir = 1
        # 起点不能是 0：alpha=0 时形状整个不画，亮起来的第一帧会是空白板，
        # 白掉一帧的进场看着就是"慢半拍"。从第一帧的量起步。
        self._drop_a = max(self._drop_a, 1.0 / DROP_IN_FRAMES)
        self._drop_canvas.visible = True
        # 亮起来之前没有排版，尺寸靠随后到达的 on_resize（约 10ms）补；
        # 这里先用上一次记下的尺寸画一帧，免得淡入的头两帧是空的。
        self._redraw_drop()
        self._safe_update(self._drop_canvas)
        self._kick_drop()

    def _kick_drop(self):
        """保证有且在跑的计时链——淡入淡出和光斑爬行都吊在这条链上。"""
        self._later(DROP_TICK, self._tick_drop, self._drop_gen)

    def _drop_close(self, immediate=False):
        """退场：淡出后再收。immediate 给"松手快门"用——快门不需要谢幕。"""
        if self._drop_canvas is None:
            return
        if immediate or self._drop_a <= 0.0:
            self._drop_gen += 1
            self._drop_dir = 0
            self._close_drop_canvas()
        else:
            self._drop_dir = -1
            self._kick_drop()

    def _close_drop_canvas(self):
        self._drop_hint_shown = False
        if self._drop_canvas is None:
            return
        self._drop_canvas.shapes = []
        self._drop_canvas.visible = False
        self._safe_update(self._drop_canvas)

    def _hint_stage(self, on):
        """提示文字的舞台调度：拖入期间让它上场（树让位），退场后按"有没有树"归位。

        空态时永远是提示在场；载入过就轮到树。只改 visible 不换 Stack 顺序，
        点击归属不变（提示本来就 expand=True 不吞点击）。
        """
        has_tree = bool(self.tree_list.controls)
        self.tree_list.visible = has_tree and not on
        self.empty_hint.visible = on or not has_tree

    def _drag_entered(self):
        if self._drop_armed:
            return
        self._drop_armed = True
        self._drop_open()
        self._hint_stage(True)       # 已载入时提示被树盖着：先让树让位
        self._set_hint_state("armed")
        self.page.update()

    def _drag_exited(self):
        if not self._drop_armed:
            return
        self._drop_armed = False
        self._drop_close()
        self._set_hint_state("idle")
        self._hint_stage(False)      # 树回来了就归位
        self.page.update()

    def _set_hint_state(self, state):
        """状态文案就地改字：idle / armed / reject / loading 四套说法长在同一个容器里。

        不再新起一层提示——落点上叠东西就又回到"给应用贴取景框"了。
        loading 是松手后的第一帧反馈：旧树已撤、解析还没回来，这段时间
        得有个东西占着场子，不然用户以为没松手成功。
        """
        if self._hint_title is None or state == self._hint_state:
            return
        self._hint_state = state
        ink = self.ink
        if state == "loading":
            self._hint_icon.name = ft.Icons.HOURGLASS_TOP
            self._hint_icon.color = ink["accent"]
            self._hint_title.value = "正在解析…"
            self._hint_title.color = ink["accent"]
            self._hint_sub.value = "松手后马上就好"
        elif state == "armed":
            self._hint_icon.name = ft.Icons.UPLOAD_FILE
            self._hint_icon.color = ink["accent"]
            self._hint_title.value = "松手即载入"
            self._hint_title.color = ink["accent"]
            self._hint_sub.value = "支持 .bin 或整个 HZC 文件夹"
        elif state == "reject":
            self._hint_icon.name = ft.Icons.UPLOAD_FILE
            self._hint_icon.color = ink["err_frame"]
            self._hint_title.value = "拖进来的不是资源"
            self._hint_title.color = ink["err_frame"]
            self._hint_sub.value = "只能放 .bin 或 HZC 文件夹进来"
        else:
            self._hint_icon.name = ft.Icons.FOLDER_OPEN
            self._hint_icon.color = ink["accent"]
            self._hint_title.value = "尚未加载文件"
            self._hint_title.color = ink["text"]
            self._hint_sub.value = self._hint_idle_sub
        self._safe_update(self.empty_hint)

    def _reject_drop(self):
        """拖进来的东西不认：面板边框变红停 160ms，再淡出。

        不弹底部横幅——报错就该发生在落点旁边，用户的视线正停在那儿。
        """
        self._drop_tone = "err"
        self._drop_a = 1.0
        self._hint_stage(True)
        self._set_hint_state("reject")
        self._redraw_drop()
        self.page.update()
        if not self._later(DROP_REJECT_MS / 1000, self._reject_fade):
            self._reject_fade()

    def _reject_fade(self):
        if self._drop_tone != "err":
            return          # 这 160ms 里又有新的进入/拖入，本次拒绝不算数
        self._drop_armed = False
        # 画布没在册（比如拖进来前没触发 enter）就没必要淡出，直接把它清干净
        self._drop_close(immediate=not self._drop_hint_shown)
        self._set_hint_state("idle")
        self._hint_stage(False)
        self.page.update()

    def _safe_update(self, control):
        try:
            control.update()
        except Exception:
            log.debug("忽略异常（_safe_update）", exc_info=True)
            pass

    async def _on_dropped(self, e):
        paths = [getattr(f, "path", None) or getattr(f, "name", None)
                 for f in (getattr(e, "files", None) or [])]
        target = pick_drop_path(paths)
        if not target:
            self._reject_drop()
            return
        # 快门：画布立刻收掉，不谢幕——松手之后的确认交给内容本身（角色逐条长出来）。
        self._drop_armed = False
        self._drop_close(immediate=True)
        # 松手即清场：旧树先撤、"正在解析"当场上场。
        # 否则要等 to_thread 解析完才换内容，中间那 100ms 左右旧树纹丝不动
        # （初始态则是一片没有任何反馈的空窗），用户以为没松手成功。
        self.tree_list.controls.clear()
        self._tree_toggles = {}
        self._action_rows = []
        self._tree_wraps = []
        self._stagger_i = 0
        self._hint_stage(True)
        self._set_hint_state("loading")
        self.page.update()

        gen0 = self._state_gen
        ok = await self._load_bin(target)
        if ok:
            self._set_hint_state("idle")
            return
        if ok is None or self._state_gen != gen0 + 1:
            # 过期（被载出/被另一次载入接管）：界面归接管者管，这里不越权还原。
            return
        # 解析失败：旧数据其实没动过（_load_bin 在写入前就退出了），
        # 把旧树原样长回来；若本来就是空态，_hint_stage(False) 会把提示还回去。
        self._restoring = True
        try:
            self._render_tree()
            self._restore_tree_state()
        finally:
            self._restoring = False
        self._sync_selected_row()
        self._set_hint_state("idle")
        self._hint_stage(False)
        self.page.update()
