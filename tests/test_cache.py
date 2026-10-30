"""缓存子系统回归测试：指纹、键、索引编解码、字节预算 LRU、磁盘自愈。

stdlib unittest；每个用例独立临时目录 + 环境沙箱（见 tests/support.py），
可整文件跑，也可单跑某个类：

    python tests/test_cache.py -v
    cd tests && python -m unittest test_cache.ByteLRUTest -v

跨会话与整流程的缓存行为在 test_ui_smoke.py；这里只测纯逻辑。
预算类用例一律 new 一个 ByteLRU/DiskCache：模块单例的预算是导入期读的，
运行中改环境变量不会重建实例。
"""

from __future__ import annotations

import os
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

try:
    from . import support
except ImportError:  # 直接 python tests/test_cache.py
    import support

from fvpapp import cache
from fvpapp.version import APP_NAME

PNG = b"\x89PNG\r\n\x1a\n"

INDEX_INFOS = [
    {"filename": "CHR_A_喜_私服", "offset": 44, "size": 128, "type": "hzc",
     "image_type": 2, "width": 64, "height": 32, "offset_x": 3, "offset_y": 5,
     "frame_count": 4},
    {"filename": "voice", "offset": 200, "size": 10, "type": "ogg",
     "image_type": None, "width": 0, "height": 0, "offset_x": 0, "offset_y": 0,
     "frame_count": 1},
]


class CacheTestBase(support.EnvTestCase):
    def write(self, name: str, data: bytes) -> Path:
        path = self.tmp / "src" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def png_file(self, name: str = "a.hzc") -> Path:
        return self.write(name, b"hzc1" + b"\0" * 60)


# ── 指纹与键 ──────────────────────────────────────────────────────────────
class FingerprintKeyTest(CacheTestBase):
    def test_file_fingerprint_stable_and_scoped(self):
        src = self.png_file()
        fp1 = cache.fingerprint(src)
        self.assertTrue(fp1.startswith("src:"), fp1)
        self.assertEqual(cache.fingerprint(src), fp1)

        st = src.stat()
        os.utime(src, ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000_000))
        fp2 = cache.fingerprint(src)
        self.assertNotEqual(fp2, fp1, "mtime 变了指纹却没变")

        src.write_bytes(src.read_bytes() + b"x")
        fp3 = cache.fingerprint(src)
        self.assertNotEqual(fp3, fp2, "大小变了指纹却没变")

    def test_missing_file_and_empty_path_have_no_fingerprint(self):
        self.assertIsNone(cache.fingerprint(self.tmp / "nope.hzc"))
        self.assertIsNone(cache.fingerprint(None))
        self.assertIsNone(cache.fingerprint(""))

    def test_resource_fingerprint_prefers_entry_path(self):
        src = self.png_file()
        expected = cache.fingerprint(src)
        self.assertEqual(cache.resource_fingerprint({"path": str(src)}, "/no/such.bin"), expected)
        self.assertEqual(cache.resource_fingerprint({"filename": "a"}, str(src)), expected)
        self.assertIsNone(cache.resource_fingerprint(None, str(src)))
        self.assertIsNone(cache.resource_fingerprint({}, str(src)))

    def test_dir_fingerprint_tracks_content(self):
        folder = self.tmp / "dirfp"
        (folder / "sub").mkdir(parents=True)
        (folder / "sub" / "b.hzc").write_bytes(b"1")
        d1 = cache.dir_fingerprint(folder)
        self.assertTrue(d1.startswith("dir:"), d1)
        self.assertEqual(cache.dir_fingerprint(folder), d1)

        (folder / "sub" / "b.hzc").write_bytes(b"22")
        self.assertNotEqual(cache.dir_fingerprint(folder), d1)

        empty = self.tmp / "empty"
        empty.mkdir()
        self.assertEqual(cache.dir_fingerprint(empty), cache.dir_fingerprint(empty))

    def test_keys_are_stable_and_scoped(self):
        src = self.png_file()
        fp1 = cache.fingerprint(src)
        fp2 = cache.fingerprint(self.write("b.hzc", b"hzc1" + b"\0" * 60))

        ik = cache.index_key(fp1)
        self.assertEqual(len(ik), 40)
        self.assertEqual(set(ik) | set("0123456789abcdef"), set("0123456789abcdef"))
        self.assertEqual(cache.index_key(fp1), ik)
        self.assertNotEqual(cache.index_key(fp2), ik)
        self.assertIsNone(cache.index_key(None))

        self.assertEqual(cache.thumb_key(fp1, "a", 36), cache.thumb_key(fp1, "a", 36))
        self.assertNotEqual(cache.thumb_key(fp1, "a", 36), cache.thumb_key(fp1, "a", 100))
        self.assertNotEqual(cache.thumb_key(fp1, "a", 36), cache.thumb_key(fp1, "b", 36))
        self.assertNotEqual(cache.thumb_key(fp1, "a", 36), cache.thumb_key(fp2, "a", 36))
        self.assertIsNone(cache.thumb_key(None, "a", 36))

        self.assertEqual(cache.image_key(fp1, "base", "a", 0), cache.image_key(fp1, "base", "a", 0))
        self.assertEqual(cache.frames_key(fp1, "a"), ("frames", fp1, "a"))
        self.assertNotEqual(cache.frames_key(fp1, "a"), cache.frames_key(fp2, "a"))
        self.assertIsNone(cache.image_key(None, "base", "a", 0))
        self.assertIsNone(cache.frames_key(None, "a"))


# ── 索引编解码 ────────────────────────────────────────────────────────────
class IndexCodecTest(CacheTestBase):
    def test_roundtrip_keeps_unicode_and_extra_fields(self):
        self.assertEqual(cache.decode_index(cache.encode_index(INDEX_INFOS)), INDEX_INFOS)
        extra = [{"filename": "a", "type": "hzc", "extra": 1}]
        self.assertEqual(cache.decode_index(cache.encode_index(extra)), extra)

    def test_malformed_payloads_are_rejected(self):
        bad = {
            "坏 JSON": b"{not json",
            "非列表": b'{"a":1}',
            "缺必需字段": b'[{"filename":"a"}]',
            "非字典条目": b"[1,2]",
            "非 UTF-8": b"\xff\xfe\x00",
            "空内容": b"",
            "空白": b"   ",
            "null": b"null",
        }
        for name, raw in bad.items():
            with self.subTest(name=name):
                self.assertIsNone(cache.decode_index(raw))

    def test_disk_heals_corrupt_file(self):
        src = self.png_file("idx.hzc")
        key = cache.index_key(cache.fingerprint(src))
        validate = lambda raw: cache.decode_index(raw) is not None  # noqa: E731
        blob = cache.encode_index(INDEX_INFOS)

        disk = cache.DiskCache("index-heal", 4 * 1024 * 1024, ".json", validate=validate)
        self.assertIsNone(disk.read(key), "空目录不应命中")
        self.assertTrue(disk.write(key, blob))

        again = cache.DiskCache("index-heal", 4 * 1024 * 1024, ".json", validate=validate)
        self.assertEqual(cache.decode_index(again.read(key)), INDEX_INFOS)
        self.assertEqual(again.stats()["hits"], 1)

        bad = again.path_for(key)
        bad.write_bytes(b'[{"filename":"a",')  # 截断
        self.assertIsNone(again.read(key), "坏索引应回退成未命中")
        self.assertFalse(bad.exists(), "坏索引应被删掉")
        self.assertTrue(again.write(key, blob))
        self.assertEqual(cache.decode_index(again.read(key)), INDEX_INFOS)

    def test_invalid_keys_and_empty_writes(self):
        disk = cache.DiskCache("index-keys", 1024 * 1024, ".json")
        self.assertIsNone(disk.path_for("abc"))
        self.assertIsNone(disk.path_for("Z" * 40))
        self.assertIsNone(disk.path_for(None))
        self.assertIsNone(disk.read("abc"))
        self.assertFalse(disk.write("abc", b"{}"))
        self.assertFalse(disk.write("a" * 40, b""))
        self.assertIsNone(disk.read("a" * 40))
        self.assertEqual(disk.path_for("a" * 40).suffix, ".json")


# ── 内存 LRU：按字节、非条数 ─────────────────────────────────────────────
class ByteLRUTest(CacheTestBase):
    def test_evicts_least_recently_used_by_bytes(self):
        lru = cache.ByteLRU("unit", max_bytes=1000, max_entry_bytes=600)
        for key in ("a", "b", "c"):
            self.assertTrue(lru.put(key, key, 300))
        self.assertEqual([lru.get(k) for k in ("a", "b", "c")], ["a", "b", "c"])
        self.assertEqual(lru.get("a"), "a")  # a 提前到最近

        self.assertTrue(lru.put("d", "d", 300))  # 1200 > 1000 → 淘汰 b
        self.assertIsNone(lru.get("b"))
        self.assertEqual(lru.get("a"), "a")
        self.assertEqual(lru.get("d"), "d")
        stats = lru.stats()
        self.assertLessEqual(stats["bytes"], lru.max_bytes)
        self.assertEqual(stats["entries"], 3)
        self.assertEqual(stats["evictions"], 1)

    def test_exact_budget_and_same_key_replacement(self):
        lru = cache.ByteLRU("unit", max_bytes=1000, max_entry_bytes=1000)
        lru.put("x", "x", 1000)
        lru.put("y", "y", 1000)
        self.assertIsNone(lru.get("x"))
        self.assertEqual(lru.get("y"), "y")
        self.assertEqual(lru.stats()["bytes"], 1000)

        lru.put("y", "y2", 400)
        self.assertEqual(lru.get("y"), "y2")
        self.assertEqual(lru.stats()["bytes"], 400)
        self.assertEqual(lru.stats()["entries"], 1)

    def test_oversize_entry_rejected_without_evicting_others(self):
        lru = cache.ByteLRU("unit", max_bytes=1000, max_entry_bytes=600)
        lru.put("keep", "keep", 200)
        self.assertFalse(lru.put("huge", "huge", 900))
        self.assertEqual(lru.stats()["oversize"], 1)
        self.assertEqual(lru.get("keep"), "keep")
        self.assertIsNone(lru.get("huge"))

    def test_none_key_and_none_value_are_not_cached(self):
        lru = cache.ByteLRU("unit", max_bytes=1000, max_entry_bytes=1000)
        self.assertFalse(lru.put(None, "v", 1))
        self.assertIsNone(lru.get(None))

        calls = []
        self.assertEqual(lru.get_or_compute(None, lambda: calls.append(1) or 7, len), 7)
        self.assertEqual(lru.get_or_compute(None, lambda: calls.append(1) or 7, len), 7)
        self.assertEqual(len(calls), 2, "key=None 时不应缓存")
        self.assertEqual(lru.stats()["entries"], 0)

        self.assertIsNone(lru.get_or_compute("none", lambda: None, len))
        self.assertEqual(lru.stats()["entries"], 0, "算不出结果（None）不应被缓存")

    def test_clear_resets_bytes_and_entries(self):
        lru = cache.ByteLRU("unit", max_bytes=1000, max_entry_bytes=1000)
        lru.put("a", "a", 100)
        lru.put("b", "b", 100)
        lru.clear()
        self.assertEqual(lru.stats()["bytes"], 0)
        self.assertEqual(lru.stats()["entries"], 0)
        self.assertIsNone(lru.get("a"))

    def test_stats_count_hits_and_misses(self):
        lru = cache.ByteLRU("unit", max_bytes=1000, max_entry_bytes=1000)
        lru.put("k", "v", 10)
        self.assertEqual(lru.get("k"), "v")
        self.assertIsNone(lru.get("nope"))
        stats = lru.stats()
        self.assertEqual(stats["hits"], 1)
        self.assertEqual(stats["misses"], 1)
        self.assertEqual(stats["entries"], 1)
        self.assertEqual(stats["bytes"], 10)


# ── single-flight ─────────────────────────────────────────────────────────
class SingleFlightTest(CacheTestBase):
    def test_concurrent_compute_runs_once(self):
        lru = cache.ByteLRU("single", max_bytes=10_000, max_entry_bytes=10_000)
        started, gate = threading.Event(), threading.Event()
        calls, results = [], []

        def compute():
            calls.append(threading.get_ident())
            started.set()
            gate.wait(5)
            return "value"

        def worker():
            results.append(lru.get_or_compute("k", compute, len))

        t1 = threading.Thread(target=worker)
        t1.start()
        self.assertTrue(started.wait(2), "第一个线程没有进入 compute")

        t2 = threading.Thread(target=worker)
        t2.start()
        support.wait_until(lambda: lru.stats()["inflight_waits"] >= 1,
                           what="第二个线程登记为等待者")
        gate.set()

        for thread in (t1, t2):
            thread.join(3)
            self.assertFalse(thread.is_alive(), "线程没有结束（可能死锁）")
        self.assertEqual(len(calls), 1, "同一个键只应计算一次")
        self.assertEqual(results, ["value", "value"])

    def test_compute_exception_releases_inflight(self):
        lru = cache.ByteLRU("single", max_bytes=1000, max_entry_bytes=1000)

        def boom():
            raise RuntimeError("boom")

        with self.assertRaises(RuntimeError):
            lru.get_or_compute("k", boom, len)

        waits = lru.stats()["inflight_waits"]
        done = []
        thread = threading.Thread(
            target=lambda: done.append(lru.get_or_compute("k", lambda: "ok", len))
        )
        thread.start()
        thread.join(3)
        self.assertFalse(thread.is_alive(), "inflight 没释放，同键后续计算被卡住")
        self.assertEqual(done, ["ok"])
        self.assertEqual(lru.stats()["inflight_waits"], waits,
                         "inflight 没清掉：后续同键被当成了等待者")

        calls = []
        self.assertEqual(lru.get_or_compute("k", lambda: calls.append(1) or "again", len), "ok")
        self.assertEqual(calls, [], "成功算出的结果应进入缓存")

    def test_owner_failure_lets_waiter_recompute(self):
        lru = cache.ByteLRU("single", max_bytes=1000, max_entry_bytes=1000)
        started, gate = threading.Event(), threading.Event()
        owner_calls, waiter_calls = [], []
        out = {}

        def owner_compute():
            owner_calls.append(1)
            started.set()
            gate.wait(5)
            raise RuntimeError("owner failed")

        def waiter_compute():
            waiter_calls.append(1)
            return "waiter-value"

        def run_owner():
            try:
                lru.get_or_compute("k", owner_compute, len)
            except RuntimeError as exc:
                out["owner"] = type(exc).__name__

        def run_waiter():
            out["waiter"] = lru.get_or_compute("k", waiter_compute, len)

        t1 = threading.Thread(target=run_owner)
        t1.start()
        self.assertTrue(started.wait(2))
        t2 = threading.Thread(target=run_waiter)
        t2.start()
        support.wait_until(lambda: lru.stats()["inflight_waits"] >= 1,
                           what="等待者登记")
        gate.set()
        for thread in (t1, t2):
            thread.join(3)
            self.assertFalse(thread.is_alive(), "线程没有结束（可能死锁）")

        self.assertEqual(out, {"owner": "RuntimeError", "waiter": "waiter-value"})
        self.assertEqual((len(owner_calls), len(waiter_calls)), (1, 1))


# ── 磁盘 LRU：按字节、坏文件自愈 ─────────────────────────────────────────
class DiskLRUTest(CacheTestBase):
    PNG_1K = PNG + b"\0" * 992

    def test_write_read_and_stats(self):
        disk = cache.DiskCache("disk-basic", 10_000, ".png", magic=PNG)
        key = "a" * 40
        self.assertIsNone(disk.read(key))
        self.assertTrue(disk.write(key, self.PNG_1K))
        self.assertEqual(disk.read(key), self.PNG_1K)

        stats = disk.stats()
        self.assertEqual(stats["writes"], 1)
        self.assertEqual(stats["hits"], 1)
        self.assertEqual(stats["misses"], 1)
        self.assertEqual(stats["entries"], 1)
        self.assertEqual(stats["bytes"], 1000)
        self.assertEqual(disk.path_for(key).suffix, ".png")

    def test_byte_budget_evicts_oldest(self):
        disk = cache.DiskCache("disk-budget", 3000, ".png", magic=PNG)
        keys = ["a" * 40, "b" * 40, "c" * 40, "d" * 40]
        for key in keys[:3]:
            self.assertTrue(disk.write(key, self.PNG_1K))

        # 显式指定 mtime 排出新旧：a 最新、c 居中、b 最老。
        # 不能依赖写入顺序——drvfs 等粗粒度文件系统连续写会得到相同 mtime_ns。
        now_ns = time.time_ns()
        for key, offset_s in ((keys[0], 3600), (keys[2], -1800), (keys[1], -7200)):
            stamp = (now_ns + offset_s * 1_000_000_000) / 1_000_000_000
            os.utime(disk.path_for(key), (stamp, stamp))

        self.assertTrue(disk.write(keys[3], self.PNG_1K))
        stats = disk.stats()
        self.assertLessEqual(stats["bytes"], disk.max_bytes)
        self.assertEqual(stats["evictions"], 2)

        self.assertFalse(disk.path_for(keys[1]).exists(), "最老的 b 应被淘汰")
        self.assertFalse(disk.path_for(keys[2]).exists(), "淘汰到 90% 预算，c 也应被淘汰")
        self.assertEqual(disk.read(keys[0]), self.PNG_1K)
        self.assertEqual(disk.read(keys[3]), self.PNG_1K)

        directory = disk.path_for(keys[0]).parent
        self.assertEqual(sum(p.stat().st_size for p in directory.iterdir()), 2000)

    def test_bad_magic_removed_and_rebuilt(self):
        disk = cache.DiskCache("disk-heal", 10_000, ".png", magic=PNG)
        key = "a" * 40
        bad = disk.path_for(key)
        bad.write_bytes(b"not a png")
        self.assertIsNone(disk.read(key))
        self.assertFalse(bad.exists(), "坏文件应被删掉")
        self.assertTrue(disk.write(key, self.PNG_1K))
        self.assertEqual(disk.read(key), self.PNG_1K)

    def test_sweep_removes_temp_and_hidden_files(self):
        disk = cache.DiskCache("disk-sweep", 10_000, ".png", magic=PNG)
        key = "a" * 40
        directory = disk.path_for(key).parent
        leftover = directory / ".leftover.tmp"
        leftover.write_bytes(b"x")
        hidden = directory / ".hidden"
        hidden.write_bytes(b"y")

        fresh = cache.DiskCache("disk-sweep", 10_000, ".png", magic=PNG)
        self.assertIsNone(fresh.read(key))
        self.assertFalse(leftover.exists())
        self.assertFalse(hidden.exists())

    def test_invalid_keys(self):
        disk = cache.DiskCache("disk-keys", 1000, ".png")
        for key in ("abc", "Z" * 40, "g" * 40, None):
            with self.subTest(key=key):
                self.assertIsNone(disk.path_for(key))
                self.assertIsNone(disk.read(key))
                self.assertFalse(disk.write(key, b"x"))

    def test_clear_removes_files_and_bytes(self):
        disk = cache.DiskCache("disk-clear", 10_000, ".png", magic=PNG)
        for key in ("a" * 40, "b" * 40):
            disk.write(key, self.PNG_1K)
        disk.clear()
        self.assertEqual(disk.stats()["entries"], 0)
        self.assertEqual(disk.stats()["bytes"], 0)
        self.assertIsNone(disk.read("a" * 40))

    def test_write_failure_is_counted_not_raised(self):
        disk = cache.DiskCache("disk-fail", 10_000, ".png", magic=PNG)
        key = "a" * 40
        with mock.patch.object(Path, "write_bytes", side_effect=OSError("disk full")):
            self.assertFalse(disk.write(key, self.PNG_1K))
        self.assertEqual(disk.stats()["errors"], 1)
        self.assertFalse(disk.path_for(key).exists())
        self.assertTrue(disk.write(key, self.PNG_1K))

    def test_unwritable_root_is_silent(self):
        blocked = self.write("not_a_dir", b"x")
        os.environ["FVP_CACHE_DIR"] = str(blocked)  # mkdir 必然失败
        disk = cache.DiskCache("disk-blocked", 1024 * 1024, ".png")
        key = "e" * 40
        self.assertIsNone(disk.read(key))
        self.assertFalse(disk.write(key, b"x"))
        self.assertEqual(disk.stats()["bytes"], 0)
        self.assertIsNone(disk.path_for(key))


# ── 关缓存 / 预算环境变量 ────────────────────────────────────────────────
class DisabledCacheTest(CacheTestBase):
    def test_off_values(self):
        for value, expected in (("0", False), ("off", False), ("FALSE", False),
                                ("no", False), ("disable", False), ("disabled", False),
                                ("1", True), ("on", True), ("yes", True), ("", True)):
            with self.subTest(value=value):
                os.environ["FVP_CACHE"] = value
                self.assertIs(cache.enabled(), expected)

    def test_disabled_cache_is_noop(self):
        os.environ["FVP_CACHE"] = "off"
        lru = cache.ByteLRU("off", 1000, 1000)
        self.assertFalse(lru.put("k", "v", 1))
        self.assertIsNone(lru.get("k"))
        self.assertEqual(lru.stats()["entries"], 0)
        self.assertEqual(lru.get_or_compute("z", lambda: 7, len), 7)

        disk = cache.DiskCache("off", 1000, ".png")
        self.assertFalse(disk.write("f" * 40, b"x"))
        self.assertIsNone(disk.read("f" * 40))
        self.assertEqual(disk.stats()["bytes"], 0)

    def test_bad_budget_env_falls_back(self):
        os.environ["FVP_TEST_MB"] = "abc"
        self.assertEqual(cache._env_mb("FVP_TEST_MB", 3), 3 * cache.MiB)
        os.environ["FVP_TEST_MB"] = " 4 "
        self.assertEqual(cache._env_mb("FVP_TEST_MB", 3), 4 * cache.MiB)
        os.environ["FVP_TEST_MB"] = "-5"
        self.assertEqual(cache._env_mb("FVP_TEST_MB", 3), cache.MiB)
        os.environ.pop("FVP_TEST_MB")
        self.assertEqual(cache._env_mb("FVP_TEST_MB", 3), 3 * cache.MiB)


# ── 缓存根布局 ────────────────────────────────────────────────────────────
class _FakeOs:
    """只换 cache 模块眼里的 `os.name`；environ 共用真的，其余全走真 os。

    平台分支必须在任何一台机器上都验得到——裸改全局 os.name 会让 pathlib
    选错 Path 类型，SimpleNamespace 又缺 `os.path`/`os.replace` 这些成员。
    """

    def __init__(self, name):
        self.name = name
        self.environ = os.environ

    def __getattr__(self, item):
        return getattr(os, item)


class CacheRootTest(CacheTestBase):
    def test_override_is_used_and_expands_user(self):
        root = self.tmp / "custom"
        os.environ["FVP_CACHE_DIR"] = str(root)
        self.assertEqual(cache.cache_root(), root)

        os.environ["FVP_CACHE_DIR"] = "~/fvp-cache-test"
        self.assertEqual(cache.cache_root(), Path("~/fvp-cache-test").expanduser())

    def test_default_root_per_platform(self):
        os.environ.pop("FVP_CACHE_DIR", None)

        with mock.patch.object(cache, "os", _FakeOs("nt")):
            os.environ["LOCALAPPDATA"] = str(self.tmp / "local")
            self.assertEqual(cache.cache_root(),
                             self.tmp / "local" / APP_NAME / "cache")
            os.environ.pop("LOCALAPPDATA", None)
            # LOCALAPPDATA 读不到还有 home 兜底，两条都得留在屏幕内
            self.assertEqual(cache.cache_root(),
                             Path.home() / "AppData" / "Local" / APP_NAME / "cache")

        with mock.patch.object(cache, "os", _FakeOs("posix")):
            os.environ["XDG_CACHE_HOME"] = str(self.tmp / "xdg")
            self.assertEqual(cache.cache_root(),
                             self.tmp / "xdg" / APP_NAME / "cache")
            os.environ.pop("XDG_CACHE_HOME", None)
            self.assertEqual(cache.cache_root(),
                             Path.home() / ".cache" / APP_NAME / "cache")

    def test_version_namespace_created_on_demand(self):
        version_dir = cache.cache_root() / cache.CACHE_VERSION
        self.assertFalse(version_dir.exists())
        disk = cache.DiskCache("lazy", 1000, ".png")
        self.assertIsNone(disk.read("a" * 40))
        self.assertTrue(version_dir.is_dir())

    def test_legacy_thumbcache_untouched_when_root_overridden(self):
        legacy = self.tmp / "thumbcache"
        legacy.mkdir()
        (legacy / "old.png").write_bytes(b"x")

        disk = cache.DiskCache("legacy-override", 1000, ".png")
        self.assertIsNone(disk.read("a" * 40))
        self.assertTrue(legacy.exists(), "缓存目录被指到别处时不该动用户原来的位置")

    def test_legacy_thumbcache_removed_under_default_root(self):
        os.environ.pop("FVP_CACHE_DIR", None)
        base = self.tmp / "local" / APP_NAME
        legacy = base / "thumbcache"
        legacy.mkdir(parents=True)
        (legacy / "old.png").write_bytes(b"x")
        # 默认根走哪条分支取决于平台：钉死 nt 分支，清理断言在两台机器上都成立
        os.environ["LOCALAPPDATA"] = str(self.tmp / "local")

        with mock.patch.object(cache, "os", _FakeOs("nt")):
            disk = cache.DiskCache("legacy-default", 1000, ".png")
            self.assertIsNone(disk.read("b" * 40))
        self.assertFalse(legacy.exists(), "默认缓存根下应清理旧版 thumbcache")
        self.assertTrue((base / "cache" / cache.CACHE_VERSION).is_dir())


if __name__ == "__main__":
    unittest.main(verbosity=2)
