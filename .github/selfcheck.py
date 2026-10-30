"""CI 自检：不依赖游戏数据、也不依赖 flet，用合成夹具验证核心逻辑。

覆盖：散装文件夹识别（后缀/魔数/递归/排除）、表情编码变体匹配、
HZC 三种像素格式的首帧/全帧解码一致性、坏数据与帧数不符的降级。
"""

from __future__ import annotations

import struct
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from fvpapp import engine  # noqa: E402
from fvpapp import naming  # noqa: E402


def make_hzc_bytes(image_type=2, w=16, h=8, frames=2, salt=0):
    head = bytearray(44)
    head[0:4] = b"hzc1"
    struct.pack_into("<H", head, 18, image_type)
    struct.pack_into("<H", head, 20, w)
    struct.pack_into("<H", head, 22, h)
    struct.pack_into("<I", head, 32, frames)
    bpp = 3 if image_type == 0 else 4
    need = w * h * bpp * (frames if image_type == 2 else 1)
    payload = bytes(((i * 7) + salt) % 256 for i in range(need))
    return bytes(head) + zlib.compress(payload)


def write_hzc(path, **kw):
    path.write_bytes(make_hzc_bytes(**kw))
    return path


class FolderScanTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="hzc_selfcheck_")
        self.root = Path(self._tmp.name)
        self.sub = self.root / "sub"
        self.sub.mkdir()
        self.base = "CHR_テスト_基_私服"
        write_hzc(self.root / f"{self.base}.hzc")
        write_hzc(self.sub / f"{self.base}_表情.hzc")
        write_hzc(self.root / f"{self.base}_昞忣.hzc")   # GBK 误读 SJIS 的「表情」
        write_hzc(self.root / f"{self.base}L_ru")         # 无后缀，靠魔数识别
        (self.root / "broken.hzc").write_bytes(b"hzc1\x00"[:10])  # 头不足，保留默认头
        (self.root / "sound.ogg").write_bytes(b"OggS" + b"\0" * 50)
        (self.root / "note.txt").write_text("x", encoding="utf-8")
        self.infos = engine.parse_dir_infos(str(self.root))

    def tearDown(self):
        self._tmp.cleanup()

    def test_recognizes_five_hzc_including_suffixless_mojibake_broken(self):
        self.assertEqual(len(self.infos), 5)

    def test_excludes_non_image_files(self):
        names = {i["filename"] for i in self.infos}
        self.assertFalse([n for n in names if n.endswith((".ogg", ".txt"))], names)

    def test_recurses_into_subfolders(self):
        rels = {Path(i["path"]).relative_to(self.root).as_posix() for i in self.infos}
        self.assertIn(f"sub/{self.base}_表情.hzc", rels)

    def test_reads_header_fields(self):
        info = next(i for i in self.infos if i["filename"] == self.base)
        self.assertEqual((info["image_type"], info["width"], info["height"]), (2, 16, 8))
        self.assertEqual(info["frame_count"], 2)

    def test_broken_header_falls_back_to_zeros(self):
        info = next(i for i in self.infos if i["filename"] == "broken")
        self.assertEqual((info["width"], info["height"], info["frame_count"]), (0, 0, 1))
        self.assertIsNone(info["image_type"])

    def test_suffixless_file_with_magic_is_kept(self):
        info = next(i for i in self.infos if i["filename"].endswith("_ru"))
        self.assertEqual(info["width"], 16)

    def test_plain_hzc_suffix_without_magic_is_kept_with_defaults(self):
        path = self.root / "fake.hzc"
        path.write_bytes(b"not an hzc file at all")
        infos = engine.parse_dir_infos(str(self.root))
        info = next(i for i in infos if i["filename"] == "fake")
        self.assertIsNone(info["image_type"])
        self.assertEqual(info["width"], 0)


class ExprNamingTest(unittest.TestCase):
    infos = [
        {"filename": "a", "type": "hzc"},
        {"filename": "a_表情", "type": "hzc"},
        {"filename": "a_昞忣", "type": "hzc"},
        {"filename": "aL_ru", "type": "hzc"},
        {"filename": "b_表情", "type": "ogg"},
    ]

    def test_variants_include_common_misdecodes(self):
        self.assertIn("表情", naming.EXPR_SUFFIXES)
        self.assertIn("昞忣", naming.EXPR_SUFFIXES)

    def test_is_expr(self):
        self.assertTrue(naming._is_expr("x_表情"))
        self.assertTrue(naming._is_expr("x_昞忣"))
        self.assertFalse(naming._is_expr("xL_ru"))
        self.assertFalse(naming._is_expr("x"))

    def test_find_expr_info(self):
        hit = naming._find_expr_info(self.infos, "a")
        self.assertIsNotNone(hit)
        self.assertEqual(hit["filename"], "a_表情")

    def test_find_expr_info_ignores_non_hzc(self):
        self.assertIsNone(naming._find_expr_info(self.infos, "b"))

    def test_no_expr_returns_none(self):
        self.assertIsNone(naming._find_expr_info(self.infos, "aL"))


class DecodeTest(unittest.TestCase):
    def _roundtrip(self, **kw):
        data = make_hzc_bytes(**kw)
        header = engine.parse_hzc_header_from_bytes(data[:44])
        self.assertIsNotNone(header)
        first = engine.hzc_decode_first_frame(data, header)
        full = engine.hzc_data_to_pil_list(data, header)
        return first, full

    def test_multi_frame_first_matches_full(self):
        first, full = self._roundtrip(image_type=2, frames=2)
        self.assertEqual(len(full), 2)
        self.assertIsNotNone(first)
        self.assertEqual(first.tobytes(), full[0].tobytes())

    def test_bgra_single_frame_first_matches_full(self):
        first, full = self._roundtrip(image_type=1)
        self.assertEqual(len(full), 1)
        self.assertEqual(first.tobytes(), full[0].tobytes())

    def test_rgb_single_frame_first_matches_full(self):
        first, full = self._roundtrip(image_type=0)
        self.assertEqual(len(full), 1)
        self.assertIsNotNone(first, "24 位 HZC 的首帧解码被误判为失败")
        self.assertEqual(first.tobytes(), full[0].tobytes())

    def test_corrupt_zlib_degrades(self):
        data = make_hzc_bytes()[:44] + b"definitely not zlib"
        header = engine.parse_hzc_header_from_bytes(data[:44])
        self.assertIsNone(engine.hzc_decode_first_frame(data, header))
        self.assertEqual(engine.hzc_data_to_pil_list(data, header), [])

    def test_truncated_data_degrades(self):
        header = engine.parse_hzc_header_from_bytes(make_hzc_bytes()[:44])
        self.assertIsNone(engine.hzc_decode_first_frame(b"short", header))
        self.assertEqual(engine.hzc_data_to_pil_list(b"short", header), [])

    def test_frame_count_overrun_returns_available_frames(self):
        data = make_hzc_bytes(image_type=2, frames=3)
        header = engine.parse_hzc_header_from_bytes(data[:44])
        header["frame_count"] = 5          # 头里写 5 帧，实际只有 3 帧
        self.assertEqual(len(engine.hzc_data_to_pil_list(data, header)), 3)

    def test_rgb_payload_is_bgr_swapped(self):
        data = make_hzc_bytes(image_type=0, w=2, h=1, frames=1)
        header = engine.parse_hzc_header_from_bytes(data[:44])
        img = engine.hzc_data_to_pil_list(data, header)[0]
        self.assertEqual(img.mode, "RGB")
        # make_hzc_bytes 的线性序列首像素为 (0,7,14)，文件里按 BGR 存 → 解码后变 (14,7,0)
        self.assertEqual(img.getpixel((0, 0)), (14, 7, 0))


if __name__ == "__main__":
    unittest.main(verbosity=2)
