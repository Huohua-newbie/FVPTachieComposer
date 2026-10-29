"""全应用缓存：内存按字节、磁盘按字节，键里带资源指纹。

四类内容共用同一套原语：

  index    解析结果（磁盘，跨会话）   sha1(资源指纹 + 索引格式版本)
  frames   整帧解码（内存）           (资源指纹, 条目名)
  thumbs   缩略图 PNG（磁盘）         sha1(资源指纹 + 条目名 + 尺寸)
  images   显示用 data URI（内存）    (资源指纹, 用途, 条目名, …)

资源指纹 = 源文件绝对路径 + 大小 + mtime。键里带着它，换资源就不用清缓存：
同名动作不会串到上一份资源上，切回上一份还能命中；源文件改过指纹自动变化，
旧条目由字节预算自然淘汰，不需要额外的登记表。

`ByteLRU.get_or_compute` 做 single-flight：同一个键并发只算一次，其余等结果。
缓存是加速层——目录不可写、磁盘满、条目损坏都只记 DEBUG，主流程照常跑。
`FVP_CACHE=off` 整条关闭；`FVP_CACHE_DIR` / `FVP_CACHE_MEM_MB` /
`FVP_CACHE_DISK_MB` 供测试与排障。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable

from .version import APP_NAME

log = logging.getLogger("fvptachie")

MiB = 1024 * 1024

CACHE_VERSION = "v1"
INDEX_FORMAT = 1
THUMB_FORMAT = 1

_ENV_ENABLED = "FVP_CACHE"
_ENV_DIR = "FVP_CACHE_DIR"
_ENV_MEM = "FVP_CACHE_MEM_MB"
_ENV_DISK = "FVP_CACHE_DISK_MB"

_OFF_VALUES = {"0", "off", "false", "no", "disable", "disabled"}


def _env_mb(name: str, default_mb: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    try:
        value = int(raw)
    except ValueError:
        return default_mb * MiB
    return max(1, value) * MiB


def _env_on() -> bool:
    return (os.environ.get(_ENV_ENABLED) or "").strip().lower() not in _OFF_VALUES


def enabled() -> bool:
    """运行期可关：测试与排障把 FVP_CACHE 置 off，所有读写立刻变成未命中。"""
    return _env_on()


def cache_root() -> Path:
    override = os.environ.get(_ENV_DIR)
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA")
        base = Path(local) if local else Path.home() / "AppData" / "Local"
        return base / APP_NAME / "cache"
    base = Path(os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache"))
    return base / APP_NAME / "cache"


def _norm(path: Any) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


# ── 键 ──────────────────────────────────────────────────────────────────
def fingerprint(path: Any) -> str | None:
    """源文件指纹；stat 不到时返回 None（调用方跳过缓存）。"""
    if not path:
        return None
    try:
        st = os.stat(path)
    except OSError:
        return None
    return f"src:{_norm(path)}|{st.st_size}|{st.st_mtime_ns}"


def resource_fingerprint(info: dict | None, input_file: str | None) -> str | None:
    """条目所属资源的指纹：散装 HZC 按文件算，`.bin` 里的条目共用包指纹。"""
    if not info:
        return None
    return fingerprint(info.get("path") or input_file)


def dir_fingerprint(folder: Any) -> str | None:
    """目录指纹：按「相对路径|大小|mtime」汇总，改任何一个文件都会换键。

    只做 rglob + stat（不打开文件），比逐个读 44 字节文件头便宜得多。
    """
    folder = Path(folder)
    try:
        paths = sorted(p for p in folder.rglob("*") if p.is_file())
    except OSError:
        return None
    digest = hashlib.sha1()
    for path in paths:
        try:
            st = path.stat()
            rel = path.relative_to(folder).as_posix()
        except (OSError, ValueError):
            return None
        digest.update(f"{rel}|{st.st_size}|{st.st_mtime_ns}\n".encode("utf-8"))
    return f"dir:{digest.hexdigest()}"


def _hash(*parts: Any) -> str:
    raw = "\0".join(str(p) for p in parts).encode("utf-8")
    return hashlib.sha1(raw).hexdigest()


def image_key(fp: str | None, *parts: Any) -> tuple | None:
    """内存里显示图像的键；没有资源指纹就不缓存（宁可重算，不能串图）。"""
    return ("image", fp, *parts) if fp else None


def frames_key(fp: str | None, filename: str) -> tuple | None:
    return ("frames", fp, filename) if fp else None


def thumb_key(fp: str | None, filename: str, size: int) -> str | None:
    if not fp:
        return None
    return _hash("thumb", THUMB_FORMAT, fp, filename, size)


def index_key(fp: str | None) -> str | None:
    if not fp:
        return None
    return _hash("index", INDEX_FORMAT, fp)


# ── 尺寸 ────────────────────────────────────────────────────────────────
def image_bytes(img: Any) -> int:
    size = getattr(img, "size", (0, 0))
    try:
        width, height = int(size[0]), int(size[1])
    except (TypeError, ValueError, IndexError):
        return 1
    mode = getattr(img, "mode", "RGBA")
    per_pixel = {"RGB": 3, "RGBA": 4, "L": 1, "LA": 2}.get(mode, 4)
    return max(1, width * height * per_pixel)


def frames_bytes(images: list) -> int:
    return sum(image_bytes(img) for img in images)


# ── 内存：按字节的 LRU ───────────────────────────────────────────────────
class ByteLRU:
    """线程安全的字节预算 LRU；`get_or_compute` 对同一个键做 single-flight。"""

    def __init__(self, name: str, max_bytes: int, max_entry_bytes: int):
        self.name = name
        self.max_bytes = max(1, int(max_bytes))
        self.max_entry_bytes = max(1, int(max_entry_bytes))
        self._lock = threading.RLock()
        self._items: OrderedDict[Any, tuple[Any, int]] = OrderedDict()
        self._bytes = 0
        self._inflight: dict[Any, threading.Event] = {}
        self._hits = 0
        self._misses = 0
        self._evictions = 0
        self._oversize = 0
        self._waits = 0
        self._errors = 0

    def get(self, key: Any) -> Any:
        if key is None or not enabled():
            return None
        with self._lock:
            item = self._items.get(key)
            if item is None:
                self._misses += 1
                return None
            self._items.move_to_end(key)
            self._hits += 1
            return item[0]

    def put(self, key: Any, value: Any, size: int) -> bool:
        if key is None or not enabled():
            return False
        size = max(0, int(size))
        if size > self.max_entry_bytes:
            # 单条比预算还大：存进去会把其它条目全挤掉，不如每次重算
            with self._lock:
                self._oversize += 1
            return False
        with self._lock:
            old = self._items.pop(key, None)
            if old is not None:
                self._bytes -= old[1]
            self._items[key] = (value, size)
            self._bytes += size
            while self._items and self._bytes > self.max_bytes:
                _, (_, dropped) = self._items.popitem(last=False)
                self._bytes -= dropped
                self._evictions += 1
            return True

    def get_or_compute(self, key: Any, compute: Callable[[], Any],
                       size_of: Callable[[Any], int]) -> Any:
        """命中直接返回；未命中时同一个键只让一个线程算，其余等它的结果。"""
        cached = self.get(key)
        if cached is not None or key is None:
            return cached if key is not None else compute()
        if not enabled():
            return compute()

        with self._lock:
            event = self._inflight.get(key)
            owner = event is None
            if owner:
                event = threading.Event()
                self._inflight[key] = event
            else:
                self._waits += 1
        if not owner:
            event.wait()
            cached = self.get(key)
            return cached if cached is not None else compute()

        try:
            value = compute()
        finally:
            with self._lock:
                self._inflight.pop(key, None)
            event.set()
        if value is not None:
            # 算不出结果（None）不缓存：失败不该被当成一次命中，下次重新尝试
            self.put(key, value, size_of(value))
        return value

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self._bytes = 0

    def stats(self) -> dict:
        with self._lock:
            return {
                "name": self.name,
                "entries": len(self._items),
                "bytes": self._bytes,
                "max_bytes": self.max_bytes,
                "hits": self._hits,
                "misses": self._misses,
                "evictions": self._evictions,
                "oversize": self._oversize,
                "inflight_waits": self._waits,
                "errors": self._errors,
            }


# ── 磁盘：分命名空间、按字节淘汰 ─────────────────────────────────────────
_legacy_cleaned = False
_legacy_lock = threading.Lock()


def _cleanup_legacy() -> None:
    """旧版 `...\\FVPTachieComposer\\thumbcache` 整目录作废（纯缓存，可重建）。"""
    global _legacy_cleaned
    if os.environ.get(_ENV_DIR):
        return  # 目录被指到别处时不碰用户原来的位置
    with _legacy_lock:
        if _legacy_cleaned:
            return
        _legacy_cleaned = True
    legacy = cache_root().parent / "thumbcache"
    if not legacy.is_dir():
        return
    try:
        shutil.rmtree(legacy)
        log.info("已清理旧版缩略图缓存目录: %s", legacy)
    except OSError:
        log.debug("旧版缓存目录删不掉，跳过: %s", legacy, exc_info=True)


class DiskCache:
    """命名空间目录 + 原子写 + 按字节 LRU；坏条目当未命中并删掉。"""

    def __init__(self, name: str, max_bytes: int, suffix: str,
                 magic: bytes | None = None, validate: Callable[[bytes], bool] | None = None):
        self.name = name
        self.max_bytes = max(1, int(max_bytes))
        self.suffix = suffix
        self.magic = magic
        self.validate = validate
        self._lock = threading.RLock()
        self._bytes = 0
        self._swept = False
        self._broken = False
        self._hits = 0
        self._misses = 0
        self._writes = 0
        self._evictions = 0
        self._errors = 0

    def _dir(self) -> Path | None:
        if not enabled() or self._broken:
            return None
        _cleanup_legacy()
        directory = cache_root() / CACHE_VERSION / self.name
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError:
            self._broken = True
            log.debug("%s 缓存目录不可用，本次跳过磁盘缓存", self.name, exc_info=True)
            return None
        return directory

    def _skip(self, path: Path) -> bool:
        return path.name.endswith(".tmp") or path.name.startswith(".")

    def _sweep_locked(self, directory: Path) -> None:
        """每个进程第一次使用时扫一遍：清临时文件、算总字节、超了先淘汰。"""
        if self._swept:
            return
        self._swept = True
        try:
            entries = []
            for path in directory.iterdir():
                if self._skip(path):
                    try:
                        path.unlink()
                    except OSError:
                        pass
                    continue
                try:
                    st = path.stat()
                except OSError:
                    continue
                entries.append((st.st_mtime_ns, path, st.st_size))
        except OSError:
            return
        self._bytes = sum(size for _, _, size in entries)
        if self._bytes > self.max_bytes:
            self._evict_locked(directory, entries)

    def _evict_locked(self, directory: Path, entries=None) -> None:
        """超预算就淘汰到 90%：留一段缓冲，避免每写一个文件都重扫目录。"""
        if entries is None:
            try:
                entries = []
                for path in directory.iterdir():
                    if self._skip(path):
                        continue
                    try:
                        st = path.stat()
                    except OSError:
                        continue
                    entries.append((st.st_mtime_ns, path, st.st_size))
            except OSError:
                return
            self._bytes = sum(size for _, _, size in entries)
        target = int(self.max_bytes * 0.9)
        entries.sort(key=lambda item: item[0])
        for _, path, size in entries:
            if self._bytes <= target:
                break
            try:
                path.unlink()
                self._bytes -= size
                self._evictions += 1
            except OSError:
                log.debug("%s 缓存淘汰失败: %s", self.name, path, exc_info=True)

    def path_for(self, key: str) -> Path | None:
        if not self._valid(key):
            return None
        directory = self._dir()
        return None if directory is None else directory / f"{key}{self.suffix}"

    @staticmethod
    def _valid(key: str) -> bool:
        return (isinstance(key, str) and 16 <= len(key) <= 64
                and all(ch in "0123456789abcdef" for ch in key))

    def read(self, key: str) -> bytes | None:
        path = self.path_for(key)
        if path is None:
            return None
        with self._lock:
            directory = path.parent
            self._sweep_locked(directory)
            try:
                data = path.read_bytes()
            except FileNotFoundError:
                self._misses += 1
                return None
            except OSError:
                self._errors += 1
                log.debug("%s 缓存读取失败: %s", self.name, path, exc_info=True)
                return None
            if self.magic is not None and not data.startswith(self.magic):
                # 半截文件 / 被别的程序写坏：删掉当未命中，下一次重建
                self._drop_locked(path)
                return None
            if self.validate is not None and not self.validate(data):
                self._drop_locked(path)
                return None
            try:
                os.utime(path, None)  # mtime 就是 LRU 的"最近使用"
            except OSError:
                pass
            self._hits += 1
            return data

    def write(self, key: str, data: bytes) -> bool:
        if not data:
            return False
        path = self.path_for(key)
        if path is None:
            return False
        with self._lock:
            directory = path.parent
            self._sweep_locked(directory)
            old_size = 0
            try:
                old_size = path.stat().st_size
            except OSError:
                pass
            tmp = directory / f".{key}.{os.getpid()}.{threading.get_ident()}.tmp"
            try:
                tmp.write_bytes(data)
                os.replace(tmp, path)
            except OSError:
                self._errors += 1
                log.debug("%s 缓存写入失败: %s", self.name, path, exc_info=True)
                return False
            finally:
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass
            self._bytes += len(data) - old_size
            self._writes += 1
            if self._bytes > self.max_bytes:
                self._evict_locked(directory)
            return True

    def _drop_locked(self, path: Path) -> None:
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        try:
            path.unlink()
            self._bytes -= size
        except OSError:
            pass

    def clear(self) -> None:
        directory = self._dir()
        if directory is None:
            return
        with self._lock:
            try:
                for path in directory.iterdir():
                    if path.is_file():
                        try:
                            path.unlink()
                        except OSError:
                            pass
            except OSError:
                pass
            self._bytes = 0
            self._swept = True

    def stats(self) -> dict:
        with self._lock:
            entries = 0
            directory = self._dir()
            if directory is not None:
                try:
                    entries = sum(1 for p in directory.iterdir()
                                  if p.is_file() and not self._skip(p))
                except OSError:
                    entries = 0
            return {
                "name": self.name,
                "entries": entries,
                "bytes": self._bytes,
                "max_bytes": self.max_bytes,
                "hits": self._hits,
                "misses": self._misses,
                "writes": self._writes,
                "evictions": self._evictions,
                "errors": self._errors,
            }


# ── 索引编解码 ──────────────────────────────────────────────────────────
_INDEX_REQUIRED = ("filename", "type")


def encode_index(infos: list[dict]) -> bytes:
    return json.dumps(infos, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def decode_index(data: bytes) -> list[dict] | None:
    try:
        infos = json.loads(data)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(infos, list):
        return None
    for info in infos:
        if not isinstance(info, dict):
            return None
        if any(key not in info for key in _INDEX_REQUIRED):
            return None
    return infos


# ── 预算与实例 ──────────────────────────────────────────────────────────
_MEM_TOTAL = _env_mb(_ENV_MEM, 320)
_DISK_TOTAL = _env_mb(_ENV_DISK, 128)

_FRAMES_MAX = int(_MEM_TOTAL * 0.8)
_IMAGES_MAX = max(1, _MEM_TOTAL - _FRAMES_MAX)
_THUMBS_MAX = int(_DISK_TOTAL * 0.75)
_INDEX_MAX = max(1, _DISK_TOTAL - _THUMBS_MAX)

frames = ByteLRU("frames", _FRAMES_MAX, int(_FRAMES_MAX * 0.75))
images = ByteLRU("images", _IMAGES_MAX, min(16 * MiB, max(1, _IMAGES_MAX // 2)))
thumbs = DiskCache("thumb", _THUMBS_MAX, ".png", magic=b"\x89PNG\r\n\x1a\n")
index = DiskCache("index", _INDEX_MAX, ".json",
                  validate=lambda raw: decode_index(raw) is not None)


def stats() -> dict:
    """四层缓存的命中/字节/淘汰计数；测试与排障用，不参与业务判断。"""
    return {
        "frames": frames.stats(),
        "images": images.stats(),
        "thumbs": thumbs.stats(),
        "index": index.stats(),
    }
