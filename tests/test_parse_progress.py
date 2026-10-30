"""解析进度上报（任务③「大资源加载进度条」的内核半边）的回归测试。

卡片要求：载入超过 2 秒的资源时显示**阶段与进度**，不出现无响应的假死。
界面拿到的每个进度点都得可信，所以这里盯四件事：
- 传不传 `progress`，解析结果必须逐字段一样（进度不能改变行为）
- `done` 只准往前走，`total` 一个阶段内不许变
- 末次必须正好报满（否则进度条永远差一格）
- 目录扫描在数清总数之前，`total` 报 0 表示"还不知道"，界面据此走不确定态

外加损坏输入的错误路径：截断的头 / 表 / 文件名区、无效相对偏移、越界绝对
偏移、坏 zlib、帧数超出实际数据、Shift-JIS 非法文件名。

纯内核，不需要 flet：
    python tests/test_parse_progress.py -v
    cd tests && python -m unittest test_parse_progress.BinProgressTest -v
"""

from __future__ import annotations

import struct
import unittest

try:
    from . import support
except ImportError:  # 直接 python tests/test_parse_progress.py
    import support

from fvpapp.engine import (
    hzc_data_to_pil_list,
    hzc_decode_first_frame,
    parse_bin_info,
    parse_bin_info_extended,
    parse_dir_infos,
    parse_hzc_header_from_bytes,
)

HEADER_KEYS = {"image_type", "width", "height", "offset_x", "offset_y", "frame_count"}
BIN_KEYS = {"filename", "offset", "size", "type"} | HEADER_KEYS


def collect(fn, *args, **kwargs):
    points = []
    result = fn(*args, progress=lambda d, t, s: points.append((d, t, s)), **kwargs)
    return result, points


def monotone(points):
    """前一个数不许比后一个大：进度只准往前走。"""
    return all(p[0] <= q[0] for p, q in zip(points, points[1:]))


def raw_bin(x: int, y: int, entries, names_blob: bytes = b"", payload: bytes = b"") -> bytes:
    """按 .bin 布局拼字节：头(文件数, 名称区长度) + 表 + 名称区 + 数据区。"""
    out = bytearray(struct.pack("<II", x, y))
    for rel, abs_offset, size in entries:
        out += struct.pack("<III", rel, abs_offset, size)
    out += names_blob + payload
    return bytes(out)


class BinTestBase(support.EnvTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.names = [f"CHR_翠_{'喜基'[i % 2]}_私服{i}" for i in range(12)]
        self.bin_path = self.tmp / "graph.bin"
        self.data_start = support.build_bin(self.bin_path, self.names)

    def write_raw(self, name: str, data: bytes):
        path = self.tmp / name
        path.write_bytes(data)
        return path


class BinProgressTest(BinTestBase):
    def test_progress_does_not_change_result(self):
        plain = parse_bin_info_extended(self.bin_path)
        with_progress, _ = collect(parse_bin_info_extended, self.bin_path)
        self.assertEqual(plain, with_progress)

        self.assertEqual([info["filename"] for info in with_progress], self.names)
        for info in with_progress:
            self.assertEqual(set(info), BIN_KEYS)
            self.assertEqual(info["type"], "hzc")
            self.assertEqual((info["width"], info["height"]), (8, 8))
            self.assertEqual(info["frame_count"], 1)

        payload_size = len(support.make_hzc_bytes())
        expected_offsets = [self.data_start + i * payload_size for i in range(len(self.names))]
        self.assertEqual([info["offset"] for info in with_progress], expected_offsets)
        self.assertEqual({info["size"] for info in with_progress}, {payload_size})

    def test_stage_sequence_and_totals(self):
        infos, points = collect(parse_bin_info_extended, self.bin_path)
        n = len(infos)
        stages = [s for _, _, s in points]
        self.assertEqual(stages[:2], ["读文件表", "读文件名"])
        self.assertLessEqual({"读文件表", "读文件名", "识别类型", "读图像头"}, set(stages))

        self.assertEqual({t for _, t, _ in points}, {2 * n})
        self.assertTrue(monotone(points), [p[0] for p in points[:6]])
        self.assertEqual(points[-1][0], 2 * n)
        self.assertEqual(len(points), 2 * n + 2)
        self.assertEqual(sum(1 for _, _, s in points if s == "识别类型"), n)
        self.assertEqual(sum(1 for _, _, s in points if s == "读图像头"), n)

    def test_parse_bin_info_reports_its_own_progress(self):
        infos, points = collect(parse_bin_info, self.bin_path)
        n = len(infos)
        self.assertEqual({t for _, t, _ in points}, {n})
        self.assertEqual(points[-1][0], n)
        self.assertTrue(monotone(points))

    def test_empty_bin_reports_zero_total(self):
        infos, points = collect(parse_bin_info_extended, self.write_raw("empty.bin", b"\0" * 8))
        self.assertEqual(infos, [])
        self.assertEqual(points[-1][1], 0)


class BinErrorTest(BinTestBase):
    def test_truncated_header_raises(self):
        path = self.write_raw("short-header.bin", b"\x01\x02\x03")
        with self.assertRaisesRegex(ValueError, "文件头不完整"):
            parse_bin_info(path)

    def test_truncated_entry_table_raises(self):
        # 声明 2 条，只给 1 条
        data = raw_bin(2, 0, [(0, 0, 0)])
        path = self.write_raw("short-table.bin", data)
        with self.assertRaisesRegex(ValueError, "文件信息表不完整"):
            parse_bin_info(path)

    def test_truncated_name_area_raises(self):
        # 声明名称区 10 字节，实际只有 2 字节
        data = raw_bin(1, 10, [(0, 0, 0)], b"ab")
        path = self.write_raw("short-names.bin", data)
        with self.assertRaisesRegex(ValueError, "文件名区域长度不符"):
            parse_bin_info(path)

    def test_invalid_relative_name_offset_raises(self):
        data = raw_bin(1, 4, [(4, 0, 0)], b"a\0b\0")
        path = self.write_raw("bad-rel.bin", data)
        with self.assertRaisesRegex(ValueError, "无效的文件名偏移"):
            parse_bin_info(path)

    def test_absolute_offset_past_eof_degrades_to_bin_type(self):
        data = raw_bin(1, 2, [(0, 9999, 10)], b"a\0")
        path = self.write_raw("past-eof.bin", data)

        infos = parse_bin_info(path)
        self.assertEqual(len(infos), 1)
        self.assertEqual(infos[0]["type"], "bin")

        extended = parse_bin_info_extended(path)
        self.assertEqual(extended[0]["type"], "bin")
        self.assertIsNone(extended[0]["image_type"])
        self.assertEqual(extended[0]["frame_count"], 1)

    def test_invalid_shift_jis_name_uses_replacement(self):
        data = raw_bin(1, 2, [(0, 0, 0)], b"\xff\x00")
        path = self.write_raw("bad-name.bin", data)
        infos = parse_bin_info(path)
        self.assertEqual(len(infos), 1)
        self.assertEqual(infos[0]["filename"], "\ufffd")
        self.assertEqual(infos[0]["type"], "bin")


class DirProgressTest(support.EnvTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.folder = self.tmp / "loose"
        self.folder.mkdir()
        for i in range(12):
            (self.folder / f"CHR_翠_喜_私服{i}.hzc").write_bytes(support.make_hzc_bytes())
        (self.folder / "readme.txt").write_bytes(b"not an image")
        (self.folder / "voice.ogg").write_bytes(b"OggS" + b"\0" * 40)

    def test_progress_does_not_change_result(self):
        plain = parse_dir_infos(self.folder)
        with_progress, _ = collect(parse_dir_infos, self.folder)
        self.assertEqual(plain, with_progress)
        self.assertEqual(len(with_progress), 12)

        for info in with_progress:
            self.assertEqual(set(info), BIN_KEYS | {"path"})
            self.assertEqual(info["type"], "hzc")
            self.assertEqual(info["offset"], 0)
            self.assertIn(info["filename"], {f"CHR_翠_喜_私服{i}" for i in range(12)})

    def test_header_stage_counts_all_files(self):
        _, points = collect(parse_dir_infos, self.folder)
        total_files = 14  # 12 张图 + txt + ogg：读文件头阶段要逐个看
        header = [p for p in points if p[2] == "读文件头"]
        self.assertEqual({t for _, t, _ in header}, {total_files})
        self.assertEqual(points[-1], (total_files, total_files, "读文件头"))
        self.assertTrue(monotone(header))
        self.assertNotIn("扫描目录", [s for _, _, s in points])

    def test_hzc_magic_beats_suffix(self):
        weird = self.tmp / "magic"
        weird.mkdir()
        (weird / "noext.dat").write_bytes(support.make_hzc_bytes())
        (weird / "fake.hzc").write_bytes(b"not hzc at all" + b"\0" * 44)
        infos = parse_dir_infos(weird)
        by_name = {info["filename"]: info for info in infos}
        self.assertEqual(set(by_name), {"noext", "fake"})
        self.assertEqual(by_name["noext"]["image_type"], 1)
        self.assertIsNone(by_name["fake"]["image_type"])

    def test_empty_dir_reports_one_point(self):
        empty = self.tmp / "empty"
        empty.mkdir()
        infos, points = collect(parse_dir_infos, empty)
        self.assertEqual(infos, [])
        self.assertEqual(points, [(0, 0, "读文件头")])

    def test_only_subdirs_still_empty(self):
        only_dirs = self.tmp / "onlydirs"
        (only_dirs / "a" / "b").mkdir(parents=True)
        self.assertEqual(parse_dir_infos(only_dirs), [])

    def test_scan_pages_at_every_200_files(self):
        pages = self.tmp / "pages"
        pages.mkdir()
        hzc = support.make_hzc_bytes()
        for i in range(200):
            (pages / f"CHR_翠_喜_私服{i}.hzc").write_bytes(hzc)

        infos, points = collect(parse_dir_infos, pages)
        scan = [p for p in points if p[2] == "扫描目录"]
        self.assertEqual(scan, [(200, 0, "扫描目录")], "扫描阶段只在整批 200 时报一次")
        self.assertEqual(points[-1], (200, 200, "读文件头"))
        self.assertEqual(len(infos), 200)

        # 201 个：仍然只在 200 报一次，末次报满 201
        (pages / "CHR_翠_喜_私服_extra.hzc").write_bytes(hzc)
        infos, points = collect(parse_dir_infos, pages)
        scan = [p for p in points if p[2] == "扫描目录"]
        self.assertEqual(scan, [(200, 0, "扫描目录")])
        self.assertEqual(points[-1], (201, 201, "读文件头"))
        self.assertEqual(len(infos), 201)


class HzcErrorTest(support.EnvTestCase):
    def header(self, raw: bytes) -> dict:
        info = parse_hzc_header_from_bytes(raw[:44])
        self.assertIsNotNone(info)
        return info

    def test_bad_zlib_returns_empty_list_and_none(self):
        raw = bytearray(support.make_hzc_bytes())
        raw[44:] = b"not zlib at all"
        header = self.header(raw)
        self.assertEqual(hzc_data_to_pil_list(bytes(raw), header), [])
        self.assertIsNone(hzc_decode_first_frame(bytes(raw), header))

    def test_header_shorter_than_44_bytes(self):
        raw = support.make_hzc_bytes()
        header = self.header(raw)
        self.assertEqual(hzc_data_to_pil_list(raw[:43], header), [])
        self.assertIsNone(hzc_decode_first_frame(raw[:43], header))
        self.assertIsNone(parse_hzc_header_from_bytes(raw[:43]))

    def test_non_hzc_magic_rejected(self):
        raw = b"NOPE" + b"\0" * 60
        self.assertIsNone(parse_hzc_header_from_bytes(raw))

    def test_frame_count_beyond_payload_yields_partial_frames(self):
        raw = bytearray(support.make_hzc_bytes(image_type=2, frames=1))
        struct.pack_into("<I", raw, 32, 3)  # 头里声称 3 帧，数据只有 1 帧
        header = self.header(raw)
        self.assertEqual(header["frame_count"], 3)
        frames = hzc_data_to_pil_list(bytes(raw), header)
        self.assertEqual(len(frames), 1, "数据不够时只返回能完整解出的帧")
        first = hzc_decode_first_frame(bytes(raw), header)
        self.assertEqual(first.tobytes(), frames[0].tobytes())

    def test_zero_frame_count_returns_no_frames(self):
        raw = bytearray(support.make_hzc_bytes(image_type=2, frames=0))
        header = self.header(raw)
        self.assertEqual(header["frame_count"], 0)
        self.assertEqual(hzc_data_to_pil_list(bytes(raw), header), [])

    def test_first_frame_matches_full_decode_for_each_type(self):
        for image_type in (0, 1, 2):
            with self.subTest(image_type=image_type):
                raw = support.make_hzc_bytes(image_type=image_type, frames=1)
                header = self.header(raw)
                full = hzc_data_to_pil_list(raw, header)
                self.assertEqual(len(full), 1)
                first = hzc_decode_first_frame(raw, header)
                self.assertIsNotNone(first)
                self.assertEqual(first.tobytes(), full[0].tobytes())

    def test_zero_size_header_returns_none_for_first_frame(self):
        raw = bytearray(support.make_hzc_bytes())
        struct.pack_into("<H", raw, 20, 0)  # width = 0
        header = parse_hzc_header_from_bytes(bytes(raw[:44]))
        self.assertEqual(header["width"], 0)
        self.assertIsNone(hzc_decode_first_frame(bytes(raw), header))


if __name__ == "__main__":
    unittest.main(verbosity=2)
