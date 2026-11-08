"""跨服装同图检测：资源包把同一张底图挂在了两个服装名下。

`Sakura moyu/graph_bs.bin` 里 `CHR_ハル_喜_私服` 与 `CHR_ハル_喜_制服` 是两份
字节完全相同的制服图，于是私服节点看着像"混进了制服"。包里的图改不了，
工具能做的是把这条标出来，省得当成解析串图去查。

纯标准库，读盘由调用方注入 —— 自检脚本可以拿假字节跑。
"""

from __future__ import annotations

import hashlib
import logging
from collections import defaultdict
from typing import Callable

from .naming import _is_expr

log = logging.getLogger("fvptachie")


def find_alias_images(hier: dict, read_bytes: Callable[[dict], bytes]) -> dict[str, list[str]]:
    """返回 {文件名: [同图的其它文件名…]}，只收录跨服装节点的底图。

    先按字节数分桶，只有同一大小出现在两个以上服装节点里才真读盘算摘要：
    整包扫一遍是几百 MB，候选通常只有几十条。表情差分不参与——`_ネコ`/`２`
    这类变体共用一套表情是正常做法，标出来只会淹掉真正的错标。
    """
    by_size = defaultdict(list)
    for role, outfits in hier.items():
        for outfit, infos in outfits.items():
            for info in infos:
                if info.get("type") != "hzc" or _is_expr(info["filename"]):
                    continue
                by_size[info["size"]].append(((role, outfit), info))

    aliases: dict[str, list[str]] = {}
    for entries in by_size.values():
        if len({key for key, _ in entries}) < 2:
            continue
        by_digest = defaultdict(list)
        for key, info in entries:
            try:
                digest = hashlib.sha256(read_bytes(info)).digest()
            except OSError:
                # 标注是附加信息，读不到就少标一条，不能把整次载入带崩
                log.debug("读同图候选失败：%s", info["filename"], exc_info=True)
                continue
            by_digest[digest].append((key, info["filename"]))
        for group in by_digest.values():
            if len({key for key, _ in group}) < 2:
                continue
            names = sorted(name for _, name in group)
            for name in names:
                aliases[name] = [n for n in names if n != name]
    return aliases