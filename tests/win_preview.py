"""开发用真机预览：加载真实 bin、自动展开到第一个动作并停在合成态。

不是测试用例，只用来在 Windows 侧把窗口打开、给外部截屏工具看真实渲染结果。
环境变量：FVP_PREVIEW_HOLD 停留秒数（默认 90）。
"""

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import flet as ft

from FVPTachieComposerFlet import ComposerApp, _is_expr

def _bin_path() -> str:
    """测试用 bin 路径：环境变量优先，其次 tests/preview_bin.txt（本机路径不入库）。"""
    env = os.environ.get("FVP_PREVIEW_BIN")
    if env:
        return env
    local = Path(__file__).resolve().parent / "preview_bin.txt"
    if local.exists():
        return local.read_text(encoding="utf-8").strip()
    raise SystemExit("缺少 bin 路径：设 FVP_PREVIEW_BIN 或写 tests/preview_bin.txt")


async def main(page: ft.Page):
    app = ComposerApp(page)
    await app._load_bin(_bin_path())
    await asyncio.sleep(0.8)

    role_col = app.tree_list.controls[0]
    role_header, role_body = role_col.controls[0], role_col.controls[1]
    role_header.on_click(None)
    outfit_col = role_body.controls[0]
    outfit_header, outfit_body = outfit_col.controls[0], outfit_col.controls[1]
    outfit_header.on_click(None)
    await asyncio.sleep(0.8)

    action_tile = outfit_body.controls[0]
    base = next(i for i in app.file_infos if not _is_expr(i["filename"]))
    app._select_action(base)
    await asyncio.sleep(1.5)
    page.update()
    print(f"PREVIEW_READY roles={len(app.hierarchy)} "
          f"base={len(app.base_imgs)} parts={len(app.part_imgs)}", flush=True)
    await asyncio.sleep(float(os.environ.get("FVP_PREVIEW_HOLD", "90")))


ft.run(main)
