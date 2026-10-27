"""图像编码与缓存：data URI、LRU 缓存、缩略图工具、表情后缀判定。"""

from __future__ import annotations

import base64
import io
import logging
import os
import sys
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

import flet as ft
from PIL import Image

from .theme import THUMB, _rgb

log = logging.getLogger("fvptachie")

_THUMB_POOL = ThreadPoolExecutor(max_workers=min(8, (os.cpu_count() or 4)), thread_name_prefix="thumb")


def _expr_suffixes() -> tuple[str, ...]:
    """生成“表情”在各编码组合下的后缀变体，兼容 GBK/BIG5 等误读 SJIS 文件名的拆包结果。"""
    cands = {"表情"}
    for enc in ("shift_jis", "utf-8"):
        try:
            raw = "表情".encode(enc)
        except Exception:
            # 编码探测失败是预期分支，不是"被吞的异常"：这里不记日志，
            # 否则每次启动都会在日志文件里留下几条吓人的假 traceback。
            continue
        for dst in ("gbk", "big5", "cp1252"):
            try:
                cands.add(raw.decode(dst))
            except Exception:
                pass
    return tuple(cands)


def _is_expr(filename: str) -> bool:
    """判断文件名是否为表情差分文件（编码无关）。"""
    return any(filename.endswith(s) for s in EXPR_SUFFIXES)


def _find_expr_info(file_infos: list[dict], base_name: str) -> dict | None:
    """查找底图对应的表情差分条目：base_<表情后缀>，后缀按编码变体集合精确匹配。"""
    prefix = base_name + "_"
    for i in file_infos:
        if i.get("type") == "hzc" and i["filename"].startswith(prefix):
            if i["filename"][len(prefix):] in EXPR_SUFFIXES:
                return i
    return None


def _img_bytes(img: Image.Image, fmt: str = "PNG") -> bytes:
    buf = io.BytesIO()
    img.save(buf, fmt)
    return buf.getvalue()


def _img_src(img: Image.Image, fmt: str = "PNG") -> str:
    """给 ft.Image 用的 data URI。

    注意：src 直接塞 bytes 只在「构造时」有效，构造后 `img.src = bytes` 赋值
    不会被重新编码，客户端拿到的是乱码路径 → 图像区一片空白。
    统一走 data URI，构造和更新两条路都成立。
    """
    return "data:image/png;base64," + base64.b64encode(_img_bytes(img, fmt)).decode()


def _checker_src(a: str, b: str, cell: int = 11) -> str:
    """透明立绘的棋盘衬底：一眼分清“这里是透明的”和“这里是白的”。"""
    img = Image.new("RGB", (2, 2))
    img.putpixel((0, 0), _rgb(a))
    img.putpixel((1, 1), _rgb(a))
    img.putpixel((1, 0), _rgb(b))
    img.putpixel((0, 1), _rgb(b))
    img = img.resize((cell * 2, cell * 2), Image.Resampling.NEAREST)
    return _img_src(img)


def _cached_src(cache: OrderedDict, key: tuple, img: Image.Image, build, limit: int) -> str:
    """按 key 取缓存里的 data URI，没有就 build(img) 出来放回去（LRU 上限淘汰）。

    缓存只在 UI 线程与缩略图工作线程之间共享，最多退化成偶尔重复编码，
    不影响正确性。
    """
    src = cache.get(key)
    if src is not None:
        cache.move_to_end(key)
        return src
    src = build(img)
    cache[key] = src
    if len(cache) > limit:
        cache.popitem(last=False)
    return src


def _cached_thumb(key: tuple, img: Image.Image, size: int) -> str:
    return _cached_src(_THUMB_SRC_CACHE, key, img, lambda im: _thumb_bytes(im, size), _THUMB_SRC_CACHE_MAX)


def _part_thumb_srcs(filename: str, imgs: list[Image.Image]) -> list[str]:
    """一次算好一批差分帧的缩略图 data URI（放在线程里跑，别占 UI 事件循环）。"""
    return [_cached_thumb(("part", filename, i), img, THUMB) for i, img in enumerate(imgs)]


def _thumb_bytes(img: Image.Image, size: int = 100) -> str:
    w, h = img.size
    s = min(1.0, size / max(w, h))
    thumb = img.resize((max(1, int(w * s)), max(1, int(h * s))), Image.Resampling.LANCZOS)
    return _img_src(thumb)


def _make_thumb_widget(img: Image.Image, size: int = 36) -> ft.Image:
    data = _thumb_bytes(img, size)
    return ft.Image(src=data, width=size, height=size, fit=ft.BoxFit.CONTAIN)


_SRC_CACHE = OrderedDict()
_SRC_CACHE_MAX = 24
_THUMB_SRC_CACHE = OrderedDict()
_THUMB_SRC_CACHE_MAX = 256

EXPR_SUFFIXES = _expr_suffixes()
