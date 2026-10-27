"""内容寻址的图像缓存：树缩略图落盘，整帧解码结果留在内存。

磁盘缩略图的键是 `sha1(资源指纹 + 条目文件名 + 缩略图尺寸)`：
资源指纹用源文件绝对路径 + 大小 + mtime，所以不同 BIN / 拆包目录里的同名动作
天然不会串图，源文件被改过（mtime / 大小变化）也会自动换键。缩略图按 PNG
存到 `%LOCALAPPDATA%\\FVPTachieComposer\\thumbcache`，最多 50 张，按最近使用
（mtime）淘汰。`FVP_THUMB_CACHE` 可以整条覆盖目录，测试用。

整帧解码结果只在内存里复用，键是源字节的 SHA-1，按帧数做 50 张上限的 LRU：
同一份资源里来回切动作不再把整包 zlib 重解一遍；换资源 / 载出时清空。
单条超过 50 帧的 HZC 不缓存，避免为了"部分命中"返回缺帧的列表。

缓存是加速层：目录不可写、磁盘满、文件损坏都只记 DEBUG，主流程照常跑。
"""

from __future__ import annotations

import base64
import hashlib
import io
import logging
import os
import threading
from collections import OrderedDict
from pathlib import Path

from PIL import Image

from .version import APP_NAME

log = logging.getLogger("fvptachie")

# 任务卡写死 50 张；磁盘缩略图和内存整帧各自按这个上限做 LRU。
MAX_THUMBS = 50
MAX_DECODED_IMAGES = 50

_ENV_DIR = "FVP_THUMB_CACHE"
_KEY_HEX_LEN = 40
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

_DISK_LOCK = threading.RLock()
_DECODED_LOCK = threading.Lock()
_pruned: set[str] = set()
_failed_dirs: set[str] = set()

_decoded: OrderedDict[str, list[Image.Image]] = OrderedDict()
_decoded_frames = 0


def cache_dir() -> Path:
    override = os.environ.get(_ENV_DIR)
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA")
        base = Path(local) if local else Path.home() / "AppData" / "Local"
        return base / APP_NAME / "thumbcache"
    base = Path(os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache"))
    return base / APP_NAME / "thumbcache"


def _norm(path: str) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def entry_key(info: dict, size: int, input_file: str | None) -> str | None:
    """算一张缩略图的磁盘键；源文件 stat 不到时返回 None（本次不读写磁盘）。

    目录资源按条目自己的 path + size + mtime 算指纹，改单个 HZC 也能失效；
    `.bin` 里的条目共用 BIN 文件的指纹。文件名和尺寸也进键，同名不同动作、
    同一动作的不同尺寸不会互相覆盖。
    """
    try:
        source = info.get("path")
        if source:
            st = os.stat(source)
            fingerprint = f"file:{_norm(source)}|{st.st_size}|{st.st_mtime_ns}"
        else:
            if not input_file:
                return None
            st = os.stat(input_file)
            fingerprint = f"bin:{_norm(input_file)}|{st.st_size}|{st.st_mtime_ns}"
    except OSError:
        return None
    raw = f"{fingerprint}\0{info.get('filename', '')}\0{size}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()


def _valid_key(key: str) -> bool:
    return isinstance(key, str) and len(key) == _KEY_HEX_LEN \
        and all(ch in "0123456789abcdef" for ch in key)


def _ensure_dir_locked() -> Path | None:
    directory = cache_dir()
    marker = str(directory)
    if marker in _failed_dirs:
        return None
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        _failed_dirs.add(marker)
        log.debug("缩略图缓存目录不可用，本次跳过磁盘缓存: %s", directory, exc_info=True)
        return None
    if marker not in _pruned:
        _pruned.add(marker)
        _prune_locked(directory)
    return directory


def _prune_locked(directory: Path) -> int:
    """按 mtime 淘汰，严格保留最多 MAX_THUMBS 个 PNG。"""
    try:
        entries = []
        for path in directory.glob("*.png"):
            try:
                entries.append((path.stat().st_mtime_ns, path))
            except OSError:
                continue
    except OSError:
        return 0
    if len(entries) <= MAX_THUMBS:
        return 0
    entries.sort(key=lambda item: item[0])
    removed = 0
    for _, path in entries[: len(entries) - MAX_THUMBS]:
        try:
            path.unlink()
            removed += 1
        except OSError:
            log.debug("缩略图缓存淘汰失败: %s", path, exc_info=True)
    return removed


def load(key: str) -> bytes | None:
    """读一张缩略图 PNG；命中时顺手把 mtime 推到最新，作为 LRU 的"最近使用"。"""
    if not _valid_key(key):
        return None
    with _DISK_LOCK:
        directory = _ensure_dir_locked()
        if directory is None:
            return None
        path = directory / f"{key}.png"
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError:
            log.debug("缩略图缓存读取失败: %s", path, exc_info=True)
            return None
        if not data.startswith(_PNG_MAGIC):
            # 半截文件 / 被别的程序写坏：删掉当未命中，下一次重建
            try:
                path.unlink()
            except OSError:
                pass
            return None
        try:
            os.utime(path, None)
        except OSError:
            pass
        return data


def save(key: str, png: bytes) -> bool:
    """原子写盘；写完立刻按 50 张上限淘汰。失败不抛，调用方继续走内存。"""
    if not _valid_key(key) or not png:
        return False
    with _DISK_LOCK:
        directory = _ensure_dir_locked()
        if directory is None:
            return False
        path = directory / f"{key}.png"
        tmp = directory / f".{key}.{os.getpid()}.{threading.get_ident()}.tmp"
        try:
            tmp.write_bytes(png)
            os.replace(tmp, path)
        except OSError:
            log.debug("缩略图缓存写入失败: %s", path, exc_info=True)
            return False
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
        _prune_locked(directory)
        return True


def thumb_png(img: Image.Image, size: int) -> bytes:
    """把一帧缩到长边不超过 size，编码成 PNG；小图不放大。"""
    width, height = img.size
    longest = max(width, height)
    scale = min(1.0, size / longest) if longest else 1.0
    thumb = img.resize(
        (max(1, int(width * scale)), max(1, int(height * scale))),
        Image.Resampling.LANCZOS,
    )
    buffer = io.BytesIO()
    thumb.save(buffer, "PNG")
    return buffer.getvalue()


def data_uri(png: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


def source_digest(data: bytes) -> str:
    """整帧缓存的键：源字节的 SHA-1，内容相同就复用，跨资源也安全。"""
    return hashlib.sha1(data).hexdigest()


def get_decoded(digest: str) -> list[Image.Image] | None:
    with _DECODED_LOCK:
        images = _decoded.get(digest)
        if images is None:
            return None
        _decoded.move_to_end(digest)
        return images


def put_decoded(digest: str, images: list[Image.Image]) -> bool:
    """按帧数计上限；单条超过 50 帧不缓存，免得命中时拿到缺帧列表。"""
    global _decoded_frames
    if not images or len(images) > MAX_DECODED_IMAGES:
        return False
    with _DECODED_LOCK:
        old = _decoded.pop(digest, None)
        if old is not None:
            _decoded_frames -= len(old)
        while _decoded and _decoded_frames + len(images) > MAX_DECODED_IMAGES:
            _, dropped = _decoded.popitem(last=False)
            _decoded_frames -= len(dropped)
        _decoded[digest] = images
        _decoded_frames += len(images)
        return True


def clear_decoded() -> None:
    global _decoded_frames
    with _DECODED_LOCK:
        _decoded.clear()
        _decoded_frames = 0


def decoded_frames() -> int:
    with _DECODED_LOCK:
        return _decoded_frames
