"""文件名约定：表情差分的后缀变体与识别。

纯标准库，不依赖 flet / Pillow —— 解析与自检脚本可以单独 import。
"""

from __future__ import annotations


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


EXPR_SUFFIXES = _expr_suffixes()


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
