"""缩略图 / 整帧缓存的回归测试。

对应任务②的验收项：
- 键要绑资源指纹（路径 + 大小 + mtime）、条目名和尺寸，同名不同源不会串图
- 磁盘严格 50 张上限，按最近使用淘汰；坏文件当未命中
- 整帧内存缓存按帧数 50 上限，超过 50 帧的单条不缓存
- 缓存目录不可写时只当未命中，不抛异常

纯逻辑，不需要 flet：python tests/test_thumb_cache.py
"""
import io
import os
import sys
import tempfile
import time
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import fvpapp.thumbcache as tc  # noqa: E402

fails = []


def check(name, ok):
    print(("PASS " if ok else "FAIL ") + name)
    if not ok:
        fails.append(name)


def fresh_dir(tag):
    """每个场景一个全新目录，避开进程内 _pruned/_failed_dirs 的记忆。"""
    path = Path(tempfile.mkdtemp(prefix=f"fvp_tc_{tag}_"))
    os.environ["FVP_THUMB_CACHE"] = str(path)
    return path


def make_source(directory, name="CHR_A_喜_私服.hzc", payload=b"x" * 32):
    path = Path(directory) / name
    path.write_bytes(payload)
    return path


# ── 目录定位：环境变量整条覆盖 ─────────────────────────────────────────────
root = fresh_dir("dir")
check("FVP_THUMB_CACHE 覆盖缓存目录", tc.cache_dir() == root)

saved = os.environ.pop("FVP_THUMB_CACHE")
try:
    default = tc.cache_dir()
    check("默认目录以 thumbcache 结尾", default.name == "thumbcache")
    check("默认目录在应用数据目录下", tc.APP_NAME in str(default))
finally:
    os.environ["FVP_THUMB_CACHE"] = str(root)

# ── 键：同源同尺寸同条目才相同，任何一项变了都要换键 ───────────────────────
src = make_source(root)
info = {"filename": "CHR_A_喜_私服", "path": str(src)}
k = tc.entry_key(info, 40, None)
check("同一份源重复算键稳定", k == tc.entry_key(dict(info), 40, None))
check("键是 40 位十六进制", isinstance(k, str) and len(k) == 40)

check("文件名变了换键",
      k != tc.entry_key({**info, "filename": "CHR_A_怒_私服"}, 40, None))
check("尺寸变了换键", k != tc.entry_key(info, 26, None))

# 同名不同目录（不同资源）不能共用键——这就是"防同名串图"
other_dir = fresh_dir("other")
src2 = make_source(other_dir)
check("不同目录的同名文件键不同",
      k != tc.entry_key({"filename": "CHR_A_喜_私服", "path": str(src2)}, 40, None))
os.environ["FVP_THUMB_CACHE"] = str(root)

st = src.stat()
src.write_bytes(b"y" * 48)          # 内容变了 → 大小 / mtime 跟着变
check("源文件改过之后换键",
      k != tc.entry_key(info, 40, None))
k_after = tc.entry_key(info, 40, None)
os.utime(src, ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000))
check("只改 mtime 也会换键", k_after != tc.entry_key(info, 40, None))

check("源文件不存在时不给键",
      tc.entry_key({"filename": "x", "path": str(root / "missing.hzc")}, 40, None) is None)

# .bin 里的条目没有 path，指纹取 BIN 文件
bin_path = root / "res.bin"
bin_path.write_bytes(b"BIN" * 16)
bin_info = {"filename": "CHR_A_喜_私服"}
kb = tc.entry_key(bin_info, 40, str(bin_path))
check("BIN 条目能算出键", isinstance(kb, str) and len(kb) == 40)
check("BIN 条目文件名变了换键", kb != tc.entry_key({"filename": "CHR_A_怒_私服"}, 40, str(bin_path)))
res2 = root / "res2.bin"
res2.write_bytes(b"BIN" * 16 + b"!")
check("换了 BIN 文件换键", kb != tc.entry_key(bin_info, 40, str(res2)))
res2.write_bytes(b"BIN" * 16 + b"?!")
check("BIN 内容改了换键", kb != tc.entry_key(bin_info, 40, str(res2)))
check("既没 path 也没 input_file 不给键", tc.entry_key({"filename": "x"}, 40, None) is None)
check("BIN 不存在不给键",
      tc.entry_key({"filename": "x"}, 40, str(root / "nope.bin")) is None)

# ── 存取往返 ──────────────────────────────────────────────────────────────
store = fresh_dir("roundtrip")
key = "a" * 40
png = tc._PNG_MAGIC + b"payload"
check("写入成功", tc.save(key, png) is True)
check("读回原样", tc.load(key) == png)
check("未命中返回 None", tc.load("b" * 40) is None)
check("非法键不读不写", tc.load("not-a-key") is None and tc.save("not-a-key", png) is False)
check("空内容不写盘", tc.save("c" * 40, b"") is False)
check("写盘后没有残留 .tmp", not list(store.glob("*.tmp")))

# 坏文件（半截 / 被别的程序写坏）当未命中，并顺手删掉
bad = store / f"{'d' * 40}.png"
bad.write_bytes(b"not a png")
check("坏 PNG 当未命中", tc.load("d" * 40) is None)
check("坏 PNG 被清掉", not bad.exists())

# 命中会把 mtime 推到最新（LRU 的"最近使用"）
old = store / f"{'e' * 40}.png"
old.write_bytes(tc._PNG_MAGIC + b"x")
os.utime(old, ns=(1, 1))
tc.load("e" * 40)
check("命中后 mtime 被推进", old.stat().st_mtime_ns > 1)

# ── 磁盘上限：严格 50 张，按最近使用淘汰 ─────────────────────────────────
cap = fresh_dir("cap")
keys = [f"{i:040x}" for i in range(50)]
for i, key in enumerate(keys):
    tc.save(key, tc._PNG_MAGIC + bytes([i]))
    time.sleep(0.002)                   # 拉开 mtime，淘汰顺序可判定
check("写满 50 张不会被提前淘汰", len(list(cap.glob("*.png"))) == 50)

tc.load(keys[0])                        # 把最老的那张用一下
time.sleep(0.002)
new_key = "f" * 40
tc.save(new_key, tc._PNG_MAGIC + b"new")
remaining = {p.stem for p in cap.glob("*.png")}
check("超过 50 张后严格保持 50 张", len(remaining) == 50)
check("最近用过的老图被留下", keys[0] in remaining)
check("真正最老的图被淘汰", keys[1] not in remaining)
check("新写入的图在缓存里", new_key in remaining)

# ── 整帧内存缓存：50 帧上限，超过 50 帧的单条不缓存 ──────────────────────
tc.clear_decoded()
check("清空后帧数为 0", tc.decoded_frames() == 0)

frames = [object() for _ in range(30)]
check("30 帧能进缓存", tc.put_decoded("d1", frames) is True)
check("命中返回同一份列表", tc.get_decoded("d1") is frames)
check("未命中返回 None", tc.get_decoded("nope") is None)

check("再加 30 帧会把最老的整条挤掉", tc.put_decoded("d2", [object() for _ in range(30)]) is True)
check("被挤掉的条目不再命中", tc.get_decoded("d1") is None)
check("总帧数不超过 50", tc.decoded_frames() <= tc.MAX_DECODED_IMAGES)

tc.clear_decoded()
check("超过 50 帧的单条不缓存", tc.put_decoded("big", [object() for _ in range(51)]) is False)
check("空列表不缓存", tc.put_decoded("empty", []) is False)
check("不缓存的条目不占帧数", tc.decoded_frames() == 0)

# 命中会刷新 LRU：d1 用一下之后，d2 才是被淘汰的那条
tc.clear_decoded()
one = [object()]
two = [object()]
tc.put_decoded("d1", one)
tc.put_decoded("d2", two)
tc.get_decoded("d1")                    # d1 变成最近使用
for i in range(48):
    tc.put_decoded(f"x{i}", [object()])
tc.put_decoded("d3", [object()])        # 触发淘汰
check("刷新过的那条留下", tc.get_decoded("d1") is one)
check("没刷新的那条被淘汰", tc.get_decoded("d2") is None)
tc.clear_decoded()

# ── 目录不可写：只当未命中，不抛 ─────────────────────────────────────────
blocked = Path(tempfile.mkdtemp(prefix="fvp_tc_blocked_")) / "file"
blocked.write_text("not a directory", encoding="utf-8")
os.environ["FVP_THUMB_CACHE"] = str(blocked / "thumbcache")
try:
    ok = tc.save("a" * 40, tc._PNG_MAGIC + b"x") is False and tc.load("a" * 40) is None
except Exception as exc:
    ok = False
    print("  抛了:", repr(exc))
check("目录建不出来时不抛、当未命中", ok)

# 缩略图编码本身（真实 PIL）
os.environ["FVP_THUMB_CACHE"] = str(fresh_dir("png"))
from PIL import Image  # noqa: E402

wide = Image.new("RGBA", (200, 100), (255, 0, 0, 255))
out = tc.thumb_png(wide, 40)
check("缩略图是 PNG", out.startswith(tc._PNG_MAGIC))
check("长边缩到 40", Image.open(io.BytesIO(out)).size == (40, 20))
small = Image.new("RGBA", (10, 10), (0, 0, 0, 255))
check("小图不放大", Image.open(io.BytesIO(tc.thumb_png(small, 40))).size == (10, 10))
check("data URI 前缀正确", tc.data_uri(out).startswith("data:image/png;base64,"))

if fails:
    print("\nFAILED:", fails)
    sys.exit(f"THUMB_CACHE_TEST_FAILED: {fails}")
print("THUMB_CACHE_OK")
