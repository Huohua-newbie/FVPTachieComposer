"""缓存子系统的无头回归测试：指纹、键、索引编解码、字节预算 LRU、磁盘自愈。

对应任务②的 6.1~6.5 / 6.8 / 6.9（跨会话与整流程那几条在 test_ui_smoke.py）。
纯逻辑，不需要 flet：python3 tests/test_cache.py
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# 缓存根指到临时目录：别碰用户 %LOCALAPPDATA% 下真缓存，也从空目录起跑
_CACHE_DIR = Path(tempfile.mkdtemp(prefix="fvp_cache_test_"))
os.environ["FVP_CACHE_DIR"] = str(_CACHE_DIR)

import fvpapp.cache as cache  # noqa: E402

fails = []


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        fails.append(name)


def fresh_dir(name):
    path = Path(tempfile.mkdtemp(prefix=f"fvp_cache_{name}_"))
    return path


def write_file(path: Path, data: bytes):
    path.write_bytes(data)
    return path


# ── 指纹与键 ────────────────────────────────────────────────────────────
src = write_file(Path(tempfile.mkdtemp(prefix="fvp_fp_")) / "a.hzc", b"hzc1" + b"\0" * 60)
fp1 = cache.fingerprint(src)
check("文件指纹带 src: 前缀", fp1 is not None and fp1.startswith("src:"), fp1)
check("同一文件指纹稳定", cache.fingerprint(src) == fp1)

st = src.stat()
os.utime(src, ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000_000))
fp2 = cache.fingerprint(src)
check("mtime 变化换指纹", fp2 != fp1)

with open(src, "ab") as f:
    f.write(b"x")
fp3 = cache.fingerprint(src)
check("大小变化换指纹", fp3 != fp2)

check("缺文件指纹为 None", cache.fingerprint(src.with_name("nope.hzc")) is None)
check("空路径指纹为 None", cache.fingerprint(None) is None)

check("条目指纹优先 path", cache.resource_fingerprint({"path": str(src)}, "/no/such.bin") == cache.fingerprint(src))
check("散装条目回落到 input_file", cache.resource_fingerprint({"filename": "a"}, str(src)) == cache.fingerprint(src))
check("无条目时指纹为 None", cache.resource_fingerprint(None, str(src)) is None)

folder = fresh_dir("dirfp")
(folder / "sub").mkdir()
write_file(folder / "sub" / "b.hzc", b"1")
d1 = cache.dir_fingerprint(folder)
check("目录指纹带 dir: 前缀", d1 is not None and d1.startswith("dir:"), d1)
check("目录指纹稳定", cache.dir_fingerprint(folder) == d1)
write_file(folder / "sub" / "b.hzc", b"22")
check("目录内容变化换指纹", cache.dir_fingerprint(folder) != d1)

ik = cache.index_key(fp1)
check("索引键是 40 位十六进制", isinstance(ik, str) and len(ik) == 40 and all(c in "0123456789abcdef" for c in ik), ik)
check("不同资源不同索引键", cache.index_key(fp2) != ik)
check("无指纹无索引键", cache.index_key(None) is None)
check("缩略图键随尺寸变化", cache.thumb_key(fp1, "a", 36) != cache.thumb_key(fp1, "a", 100))
check("缩略图键随资源变化", cache.thumb_key(fp1, "a", 36) != cache.thumb_key(fp2, "a", 36))
check("无指纹无缩略图键", cache.thumb_key(None, "a", 36) is None)
check("无指纹无内存键", cache.image_key(None, "base", "a", 0) is None
      and cache.frames_key(None, "a") is None)
check("内存键带资源指纹", cache.frames_key(fp1, "a") != cache.frames_key(fp2, "a"))

# ── 索引编解码 ──────────────────────────────────────────────────────────
infos = [
    {"filename": "CHR_A_喜_私服", "offset": 44, "size": 128, "type": "hzc",
     "image_type": 2, "width": 64, "height": 32, "offset_x": 3, "offset_y": 5, "frame_count": 4},
    {"filename": "voice", "offset": 200, "size": 10, "type": "ogg",
     "image_type": None, "width": 0, "height": 0, "offset_x": 0, "offset_y": 0, "frame_count": 1},
]
blob = cache.encode_index(infos)
check("索引 JSON 往返一致", cache.decode_index(blob) == infos)
check("坏 JSON 解码返回 None", cache.decode_index(b"{not json") is None)
check("非列表解码返回 None", cache.decode_index(b'{"a":1}') is None)
check("缺必需字段返回 None", cache.decode_index(b'[{"filename":"a"}]') is None)
check("非字典条目返回 None", cache.decode_index(b'[1,2]') is None)

# 6.1 / 6.2：索引落盘后重新读回（等价于下一次会话），坏文件自愈
def _index_ok(raw):
    return cache.decode_index(raw) is not None


dc_index = cache.DiskCache("index-test", 4 * 1024 * 1024, ".json", validate=_index_ok)
key_i = cache.index_key(fp3)
check("索引初读未命中", dc_index.read(key_i) is None)
check("索引写入成功", dc_index.write(key_i, blob))
again = cache.DiskCache("index-test", 4 * 1024 * 1024, ".json", validate=_index_ok)
check("新实例读回索引（跨会话）", cache.decode_index(again.read(key_i)) == infos)
check("读回计入命中", again.stats()["hits"] == 1, again.stats())

bad_path = again.path_for(key_i)
bad_path.write_bytes(b'[{"filename":"a","type":"hzc"}')  # 截断
check("坏索引当未命中", again.read(key_i) is None)
check("坏索引被删除", not bad_path.exists())
check("坏索引不抛异常且能重建", again.write(key_i, blob) and cache.decode_index(again.read(key_i)) == infos)

# ── 内存 LRU：按字节、非条数 ─────────────────────────────────────────────
lru = cache.ByteLRU("unit", max_bytes=1000, max_entry_bytes=600)
check("写三条未超预算", all(lru.put(k, k, 300) for k in ("a", "b", "c")))
check("三百万字节都命中", all(lru.get(k) == k for k in ("a", "b", "c")))
check("读 a 把它提到最近", lru.get("a") == "a")
lru.put("d", "d", 300)                      # 1200 > 1000 → 淘汰最久未用的 b
check("超预算淘汰最早", lru.get("b") is None)
check("最近用过的留下", lru.get("a") == "a" and lru.get("d") == "d")
stats = lru.stats()
check("字节数不超预算", stats["bytes"] <= lru.max_bytes, stats)
check("淘汰计数有涨", stats["evictions"] >= 1, stats)

# 6.4：单条超限不入缓存，也不能把已有条目挤掉
lru.put("keep", "keep", 200)
check("超限单条拒绝写入", lru.put("huge", "huge", 900) is False)
check("超限计数有涨", lru.stats()["oversize"] == 1, lru.stats())
check("已有条目没被挤掉", lru.get("keep") == "keep")
check("超限条目再读仍没有", lru.get("huge") is None)

lru.clear()
check("clear 清空字节与条目", lru.stats()["bytes"] == 0 and lru.stats()["entries"] == 0)

# 6.8：同一键并发只算一次
sf = cache.ByteLRU("single", max_bytes=10_000, max_entry_bytes=10_000)
gate = threading.Event()
calls = []
results = []


def compute():
    calls.append(threading.get_ident())
    gate.wait(2.0)
    return "value"


def worker():
    results.append(sf.get_or_compute("k", compute, len))


t1 = threading.Thread(target=worker, name="sf-1")
t1.start()
time.sleep(0.08)          # 让第一个线程先登记 inflight 再进 compute
t2 = threading.Thread(target=worker, name="sf-2")
t2.start()
time.sleep(0.08)
gate.set()
t1.join(3.0)
t2.join(3.0)
check("并发只计算一次", len(calls) == 1, calls)
check("两个线程拿到同一结果", results == ["value", "value"], results)
check("等待计数有涨", sf.stats()["inflight_waits"] >= 1, sf.stats())
check("算不出结果不缓存", sf.get_or_compute("none", lambda: None, len) is None
      and sf.stats()["entries"] == 1, sf.stats())

# ── 磁盘 LRU：按字节、坏文件自愈 ────────────────────────────────────────
dc = cache.DiskCache("bytes-test", 3000, ".png", magic=b"\x89PNG\r\n\x1a\n")
png = b"\x89PNG\r\n\x1a\n" + b"\0" * 992     # 每份 1000 字节
keys = ["a" * 40, "b" * 40, "c" * 40, "d" * 40]
for k in keys[:3]:
    dc.write(k, png)
# 把 a 的 mtime 推远，保证它是最"新"的
future = time.time() + 3600
os.utime(dc.path_for(keys[0]), (future, future))
dc.write(keys[3], png)
check("磁盘总量不超预算", dc.stats()["bytes"] <= dc.max_bytes, dc.stats())
disk_total = sum(p.stat().st_size for p in dc._dir().iterdir() if not dc._skip(p))
check("目录实际字节不超预算", disk_total <= dc.max_bytes, disk_total)
check("最近用过的 a 还在", dc.read(keys[0]) is not None)
check("真正最老的 b 被淘汰", dc.read(keys[1]) is None)
check("磁盘淘汰计数有涨", dc.stats()["evictions"] >= 1, dc.stats())

bad_png = dc.path_for(keys[2])
bad_png.write_bytes(b"not a png")           # 半截 / 被写坏
check("坏 PNG 当未命中", dc.read(keys[2]) is None)
check("坏 PNG 被删除", not bad_png.exists())
check("坏 PNG 能重建", dc.write(keys[2], png) and dc.read(keys[2]) == png)

# 6.5 的透明文件残留：临时文件在扫描时被清掉
tmp_file = dc._dir() / ".leftover.tmp"
tmp_file.write_bytes(b"x")
dc2 = cache.DiskCache("bytes-test", 3000, ".png", magic=b"\x89PNG\r\n\x1a\n")
dc2.read(keys[2])
check("扫描时清掉临时文件", not tmp_file.exists())

# ── 6.9：不可写目录 / 关缓存都不炸 ──────────────────────────────────────
blocked_file = write_file(fresh_dir("blocked") / "not_a_dir", b"x")
old_dir = os.environ["FVP_CACHE_DIR"]
os.environ["FVP_CACHE_DIR"] = str(blocked_file)   # 让 mkdir 失败
try:
    broken = cache.DiskCache("broken", 1024 * 1024, ".png")
    check("不可写目录：读返回未命中", broken.read("e" * 40) is None)
    check("不可写目录：写返回 False", broken.write("e" * 40, b"x") is False)
    check("不可写目录：stats 仍可用", broken.stats()["bytes"] == 0)
finally:
    os.environ["FVP_CACHE_DIR"] = old_dir

os.environ["FVP_CACHE"] = "off"
try:
    check("FVP_CACHE=off 生效", cache.enabled() is False)
    off_lru = cache.ByteLRU("off", 1000, 1000)
    off_lru.put("k", "v", 1)
    check("关缓存：写不进读不到", off_lru.get("k") is None and off_lru.stats()["entries"] == 0)
    off_disk = cache.DiskCache("off", 1000, ".png")
    check("关缓存：磁盘写返回 False", off_disk.write("f" * 40, b"x") is False)
    check("关缓存：磁盘读返回 None", off_disk.read("f" * 40) is None)
    check("关缓存：get_or_compute 直接算", off_lru.get_or_compute("z", lambda: 7, len) == 7)
finally:
    os.environ.pop("FVP_CACHE", None)

# 缓存根布局：版本目录 + 命名空间，且旧目录清理只在默认根下发生
check("缓存根可覆盖", cache.cache_root() == _CACHE_DIR, str(cache.cache_root()))
check("版本目录存在", (_CACHE_DIR / cache.CACHE_VERSION).is_dir())

shutil.rmtree(_CACHE_DIR, ignore_errors=True)
if fails:
    print(f"CACHE_FAIL {len(fails)}: {fails}")
    sys.exit(1)
print("CACHE_OK")
