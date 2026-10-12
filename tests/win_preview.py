"""开发用真机预览：在 Windows 侧把窗口开到某个具体状态，供截屏工具抓渲染结果。

不是测试用例——它只负责"把界面摆成某个样子然后停住"。
环境变量：
  FVP_PREVIEW_SCENARIO  要摆的状态，见 SCENARIOS（默认 tree）
  FVP_PREVIEW_HOLD      停住秒数（默认 90）
  FVP_PREVIEW_BIN       测试 bin 路径；未设则读 tests/preview_bin.txt
"""

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import flet as ft

from FVPTachieComposerFlet import ComposerApp, _find_expr_info, _is_expr


def _bin_path() -> str:
    """测试用 bin 路径：环境变量优先，其次 tests/preview_bin.txt（本机路径不入库）。"""
    env = os.environ.get("FVP_PREVIEW_BIN")
    if env:
        return env
    local = Path(__file__).resolve().parent / "preview_bin.txt"
    if local.exists():
        return local.read_text(encoding="utf-8").strip()
    raise SystemExit("缺少 bin 路径：设 FVP_PREVIEW_BIN 或写 tests/preview_bin.txt")


def _role(app, idx=0):
    col = app.tree_list.controls[idx]
    return col.controls[0], col.controls[1]


def _outfit(app, role_body, idx=0):
    col = role_body.controls[idx]
    return col.controls[0], col.controls[1]


def _richest_role_index(app, n=2):
    """挑动作最多的前几个角色：层级和缩进在"内容多"的那棵树上才看得出来。"""
    order = sorted(range(len(app.tree_list.controls)),
                   key=lambda i: -len(app.tree_list.controls[i].controls[1].controls))
    return order[:n]


async def _open_first_action(app, role_idx=0, outfit_idx=0):
    rheader, rbody = _role(app, role_idx)
    rheader.on_click(None)
    oheader, obody = _outfit(app, rbody, outfit_idx)
    oheader.on_click(None)
    await asyncio.sleep(0.6)
    info = next((i for i in app.file_infos if not _is_expr(i["filename"])), None)
    if info:
        app._select_action(info)
    await asyncio.sleep(1.2)
    return rbody, obody


async def scenario_initial(app, page):
    """空状态：只看和纸底、琉璃面板和引导区搭不搭。"""


async def scenario_tree(app, page):
    """展开到动作层并选中一项：三级缩进、导轨、选中高亮都在这里。"""
    idx = _richest_role_index(app)[0]
    await _open_first_action(app, idx)
    # 再开一个服装，让"已展开/未展开"和第三级缩进同框
    rheader, rbody = _role(app, idx)
    if len(rbody.controls) > 1:
        _outfit(app, rbody, 1)[0].on_click(None)


async def scenario_parts(app, page):
    """差分条：切到第 3 帧，检查角标中性色 + 选中描边。"""
    idx = _richest_role_index(app)[0]
    await _open_first_action(app, idx)
    with_parts = next(
        (i for i in app.file_infos
         if not _is_expr(i["filename"]) and _find_expr_info(app.file_infos, i["filename"])),
        None)
    if with_parts:
        app._select_action(with_parts)
        await asyncio.sleep(1.0)
        app._pick_part(min(2, len(app.part_imgs) - 1))
    await asyncio.sleep(0.6)


async def scenario_dark(app, page):
    """深色 + 已展开的树：换主题不能把现场弄丢，也不能把材质感丢掉。"""
    idx = _richest_role_index(app)[0]
    await _open_first_action(app, idx)
    await app._toggle_theme(None)
    await asyncio.sleep(0.8)


SCENARIOS = {"initial": scenario_initial, "tree": scenario_tree,
             "parts": scenario_parts, "dark": scenario_dark}


async def main(page: ft.Page):
    app = ComposerApp(page)
    name = os.environ.get("FVP_PREVIEW_SCENARIO", "tree")
    run = SCENARIOS.get(name, scenario_tree)
    if name != "initial":
        await app._load_bin(_bin_path())
        await asyncio.sleep(0.8)
    await run(app, page)
    await asyncio.sleep(0.8)
    page.update()
    print(f"PREVIEW_READY scenario={name} roles={len(app.hierarchy)} "
          f"base={len(app.base_imgs)} parts={len(app.part_imgs)} "
          f"tree_rows={len(app.tree_list.controls)} "
          f"selected={app.selected_filename}", flush=True)
    await asyncio.sleep(float(os.environ.get("FVP_PREVIEW_HOLD", "90")))


ft.run(main)
