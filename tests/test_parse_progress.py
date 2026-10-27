"""解析进度上报（任务③「大资源加载进度条」的内核半边）的回归测试。

卡片要求：载入超过 2 秒的资源时显示**阶段与进度**，不出现无响应的假死。
界面拿到的每个进度点都得可信，所以这里盯四件事：
- 传不传 `progress`，解析结果必须逐字段一样（进度不能改变行为）
- `done` 只准往前走，`total` 一个阶段内不许变
- 末次必须正好报满（否则进度条永远差一格）
- 目录扫描在数清总数之前，`total` 报 0 表示"还不知道"，界面据此走不确定态

纯内核，不需要 flet：python tests/test_parse_progress.py
"""
import struct
import sys
import tempfile
import zlib
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from fvpapp.engine import (  # noqa: E402
    parse_bin_info,
    parse_bin_info_extended,
    parse_dir_infos,
)

fails = []


def check(name, ok, extra=""):
    print(("PASS " if ok else "FAIL ") + name + (("  <- " + extra) if (extra and not ok) else ""))
    if not ok:
        fails.append(name)


def make_hzc_bytes(image_type=1, w=8, h=8, frames=1):
    head = bytearray(44)
    head[0:4] = b"hzc1"
    struct.pack_into("<H", head, 18, image_type)
    struct.pack_into("<H", head, 20, w)
    struct.pack_into("<H", head, 22, h)
    struct.pack_into("<I", head, 32, frames)
    bpp = 3 if image_type == 0 else 4
    body = bytes((i % 251) * 7 % 256 for i in range(w * h * bpp))
    return bytes(head) + zlib.compress(body)


def build_bin(path, names):
    """写一个最小但合法的 .bin：文件表 + 文件名区 + 每条一个 HZC。

    条目偏移是绝对偏移，文件名偏移是相对文件名区起点的，两个别搞混。
    """
    payloads = [make_hzc_bytes() for _ in names]
    names_blob = b""
    rel = []
    for n in names:
        rel.append(len(names_blob))
        names_blob += n.encode("shift-jis") + b"\x00"
    x, y = len(names), len(names_blob)
    data_start = 8 + 12 * x + y
    out = bytearray(struct.pack("<II", x, y))
    abs_off = data_start
    for i, p in enumerate(payloads):
        out += struct.pack("<III", rel[i], abs_off, len(p))
        abs_off += len(p)
    out += names_blob
    for p in payloads:
        out += p
    path.write_bytes(bytes(out))
    return data_start


def collect(fn, *a, **kw):
    pts = []
    r = fn(*a, progress=lambda d, t, s: pts.append((d, t, s)), **kw)
    return r, pts


def monotone(pts):
    """前一个数不许比后一个大：进度只准往前走。"""
    return all(p[0] <= q[0] for p, q in zip(pts, pts[1:]))


_tmp = Path(tempfile.mkdtemp(prefix="fvp_progress_"))

# ── .bin ──────────────────────────────────────────────────────
names = [f"CHR_翠_{'喜基'[i % 2]}_私服{i}" for i in range(12)]
bin_path = _tmp / "graph.bin"
build_bin(bin_path, names)

plain = parse_bin_info_extended(bin_path)
infos, pts = collect(parse_bin_info_extended, bin_path)
check("bin：传不传 progress 结果逐字段一样", plain == infos, f"{len(plain)} vs {len(infos)}")
check("bin：条目数对得上", len(infos) == len(names), str(len(infos)))

stages = [s for _, _, s in pts]
check("bin：阶段齐全", {"读文件表", "读文件名", "识别类型", "读图像头"} <= set(stages), str(stages[:4]))
check("bin：先报索引再报逐条", stages[0] == "读文件表" and stages[1] == "读文件名", str(stages[:2]))
n = len(names)
check("bin：total 恒为两段条目数之和", all(t == 2 * n for _, t, _ in pts), str({t for _, t, _ in pts}))
check("bin：done 只往前走", monotone(pts), str([p[0] for p in pts[:6]]))
check("bin：末次正好报满", pts[-1][0] == 2 * n, str(pts[-1]))
check("bin：回调次数 = 2 次索引 + 2 轮逐条", len(pts) == 2 * n + 2, str(len(pts)))
check("bin：识别类型与读图像头各占一半",
      sum(1 for _, _, s in pts if s == "识别类型") == n and sum(1 for _, _, s in pts if s == "读图像头") == n)

# parse_bin_info 单独调用（不补头部）也要能自己报进度
_, pts2 = collect(parse_bin_info, bin_path)
check("parse_bin_info 自己也能报：total = 条目数", all(t == n for _, t, _ in pts2), str({t for _, t, _ in pts2}))
check("parse_bin_info 自己也能报：末次报满", pts2[-1][0] == n, str(pts2[-1]))

# ── 目录 ──────────────────────────────────────────────────────
d = _tmp / "loose"
d.mkdir()
for i in range(12):
    (d / f"CHR_翠_喜_私服{i}.hzc").write_bytes(make_hzc_bytes())
(d / "readme.txt").write_bytes(b"not an image")
(d / "voice.ogg").write_bytes(b"OggS" + b"\0" * 40)

plain_dir = parse_dir_infos(d)
infos_d, pts_d = collect(parse_dir_infos, d)
check("目录：传不传 progress 结果逐字段一样", plain_dir == infos_d, f"{len(plain_dir)} vs {len(infos_d)}")
check("目录：非 hzc 文件不进结果", len(infos_d) == 12, str(len(infos_d)))
total_files = 14  # 12 张图 + txt + ogg：读文件头阶段要逐个看，所以总数是 14
check("目录：读文件头按全部文件计数",
      all(t == total_files for _, t, s in pts_d if s == "读文件头"), str({t for _, t, s in pts_d if s == "读文件头"}))
check("目录：末次正好报满", pts_d[-1] == (total_files, total_files, "读文件头"), str(pts_d[-1]))
check("目录：done 只往前走", monotone(pts_d), str([p[0] for p in pts_d]))
check("目录：文件不多时不报扫描阶段", "扫描目录" not in [s for _, _, s in pts_d])

big = _tmp / "big"
big.mkdir()
for i in range(450):
    (big / f"CHR_真紅_喜_制服{i}.hzc").write_bytes(make_hzc_bytes())
_, pts_b = collect(parse_dir_infos, big)
scan = [(dd, tt) for dd, tt, s in pts_b if s == "扫描目录"]
check("大目录：扫描阶段每 200 个报一次", len(scan) == 2, str(len(scan)))
check("大目录：扫描阶段 total 报 0（还不知道总数）", all(tt == 0 for _, tt in scan), str(scan))
check("大目录：扫描计数在往前走", [dd for dd, _ in scan] == [200, 400], str(scan))
check("大目录：扫完转入读文件头且报满",
      pts_b[-1] == (450, 450, "读文件头"), str(pts_b[-1]))

# ── 边界 ──────────────────────────────────────────────────────
empty = _tmp / "empty"
empty.mkdir()
_, pts_e = collect(parse_dir_infos, empty)
check("空目录：不抛且报一个 (0,0) 点", pts_e == [(0, 0, "读文件头")], str(pts_e))
check("空目录：结果为空列表", parse_dir_infos(empty) == [])

only_dirs = _tmp / "onlydirs"
(only_dirs / "a" / "b").mkdir(parents=True)
check("只有子目录：不抛", parse_dir_infos(only_dirs) == [])

bad_bin = _tmp / "bad.bin"
bad_bin.write_bytes(b"\x00\x00\x00\x00\x00\x00\x00\x00")  # x=0, y=0
infos_z, pts_z = collect(parse_bin_info_extended, bad_bin)
check("空 bin：total 报 0 而不是除零崩溃", infos_z == [] and pts_z[-1][1] == 0, str(pts_z[-1]))

if fails:
    print("\nFAILED:", fails)
    sys.exit(f"PARSE_PROGRESS_TEST_FAILED: {fails}")
print("PARSE_PROGRESS_OK")
