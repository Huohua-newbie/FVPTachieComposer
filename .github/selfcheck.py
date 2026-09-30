"""CI 自检：不依赖游戏数据，用合成夹具验证核心逻辑。
覆盖：散装文件夹识别（后缀/魔数/递归/排除）、表情编码变体匹配、
首帧解码与全帧解码一致性、_ru 剪影分类。
"""
import os
import struct
import sys
import tempfile
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import FVPTachieComposer as core
import FVPTachieComposerFlet as ui


def make_hzc(path, image_type=2, w=16, h=8, frames=2, bad=False):
    head = bytearray(44)
    head[0:4] = b"hzc1"
    struct.pack_into("<H", head, 18, image_type)
    struct.pack_into("<H", head, 20, w)
    struct.pack_into("<H", head, 22, h)
    struct.pack_into("<I", head, 32, frames)
    if bad:
        path.write_bytes(bytes(head[:10]))
        return
    bpp = 3 if image_type == 0 else 4
    need = w * h * bpp * (frames if image_type == 2 else 1)
    payload = bytes((i * 7) % 256 for i in range(need))
    path.write_bytes(bytes(head) + zlib.compress(payload))


fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


root = Path(tempfile.mkdtemp(prefix="hzc_selfcheck_"))
sub = root / "sub"
sub.mkdir()
base = "CHR_テスト_基_私服"
make_hzc(root / f"{base}.hzc")
make_hzc(sub / f"{base}_表情.hzc")
make_hzc(root / f"{base}_昞忣.hzc")          # GBK 误读 SJIS 的「表情」
make_hzc(root / f"{base}L_ru")                # 无后缀，靠魔数识别
make_hzc(root / "broken.hzc", bad=True)       # 头损坏，保留默认头
(root / "sound.ogg").write_bytes(b"OggS" + b"\0" * 50)
(root / "note.txt").write_text("x")

infos = core.parse_dir_infos(str(root))
names = {i["filename"] for i in infos}
check("识别 5 条 HZC（含无后缀/乱码/坏头）", len(infos) == 5)
check("排除 ogg/txt", not any(n.endswith((".ogg", ".txt")) for n in names))
check("递归命中 sub/", any(i["path"].endswith("表情.hzc") for i in infos))
check("头部解析 width/height", any(i["width"] == 16 and i["height"] == 8 for i in infos))

check("变体集合含 昞忣", "昞忣" in ui.EXPR_SUFFIXES)
moji = next(i for i in infos if i["filename"].endswith("昞忣"))
check("_is_expr 命中 GBK 乱码", ui._is_expr(moji["filename"]))
ru = next(i for i in infos if i["filename"].endswith("_ru"))
check("_ru 不被误判为表情", not ui._is_expr(ru["filename"]))
hit = ui._find_expr_info(infos, base)
check("乱码差分可被底图找到", hit is not None and hit["filename"].endswith("昞忣"))
check("无差分底图返回 None", ui._find_expr_info(infos, base + "L") is None)

# 首帧一致性 + 分类
ok = True
for i in infos:
    if i["width"] == 0:
        continue
    data = Path(i["path"]).read_bytes()
    hdr = {k: i[k] for k in ("image_type", "width", "height", "frame_count")}
    f1 = core.hzc_decode_first_frame(data, hdr)
    full = core.hzc_data_to_pil_list(data, hdr)
    if not f1 or not full or f1.tobytes() != full[0].tobytes():
        ok = False
        print("  首帧不一致:", i["filename"])
check("首帧 == 全帧解码第一帧", ok)

if fails:
    sys.exit(f"SELFCHECK FAILED: {fails}")
print("SELFCHECK OK")
