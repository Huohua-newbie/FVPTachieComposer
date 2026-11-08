"""跨服装同图标注（私服里混进制服那类）的纯逻辑回归测试。

`Sakura moyu/graph_bs.bin` 里 `CHR_ハル_喜_私服` 与 `CHR_ハル_喜_制服` 是两份
字节完全相同的制服图，私服节点因此看着像"混进了制服"。工具标出来而不是改图，
所以这里盯的是判定本身：

- 跨服装、字节相同 → 两条都标，互相指向对方
- 同服装内重复 / 同大小但字节不同 → 不标
- 表情差分、非 hzc 条目 → 不参与
- 读盘失败 → 跳过那一条，不炸

纯内核，不需要 flet：
    python tests/test_alias_images.py -v
    cd tests && python -m unittest test_alias_images.AliasImagesTest -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

# 与 support.py 同款：直接 `python tests/xxx.py` 时把仓库根放进 sys.path，
# 否则 `import fvpapp` 找不到。
REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from fvpapp.aliases import find_alias_images
from fvpapp.naming import split_role_outfit


def _info(filename, size, typ="hzc"):
    return {"filename": filename, "size": size, "type": typ}


def _hier(pairs):
    """pairs: [(filename, payload_bytes)] → 按 split_role_outfit 归到服装节点。"""
    hier = {}
    for filename, _ in pairs:
        role, outfit = split_role_outfit(filename)
        hier.setdefault(role, {}).setdefault(outfit, []).append(
            _info(filename, len(payload_of(pairs, filename))))
    return hier


def payload_of(pairs, filename):
    return next(p for n, p in pairs if n == filename)


def make_reader(pairs):
    payloads = dict(pairs)

    def read(info):
        return payloads[info["filename"]]

    return read


class AliasImagesTest(unittest.TestCase):
    def test_cross_outfit_identical_bytes_are_flagged(self):
        pairs = [
            ("CHR_ハル_喜_私服", b"uniform"),
            ("CHR_ハル_喜_制服", b"uniform"),
        ]
        aliases = find_alias_images(_hier(pairs), make_reader(pairs))
        self.assertEqual(aliases["CHR_ハル_喜_私服"], ["CHR_ハル_喜_制服"])
        self.assertEqual(aliases["CHR_ハル_喜_制服"], ["CHR_ハル_喜_私服"])

    def test_same_outfit_duplicate_not_flagged(self):
        pairs = [
            ("CHR_ハル_喜_私服", b"same"),
            ("CHR_ハル_基_私服", b"same"),
        ]
        aliases = find_alias_images(_hier(pairs), make_reader(pairs))
        self.assertEqual(aliases, {})

    def test_same_size_different_bytes_not_flagged(self):
        pairs = [
            ("CHR_ハル_喜_私服", b"abcdef"),
            ("CHR_ハル_喜_制服", b"123456"),
        ]
        aliases = find_alias_images(_hier(pairs), make_reader(pairs))
        self.assertEqual(aliases, {})

    def test_expr_and_non_hzc_excluded(self):
        pairs = [
            ("CHR_ハル_喜_私服", b"uniform"),
            ("CHR_ハル_喜_制服", b"uniform"),
            ("CHR_ハル_喜_私服_表情", b"uniform"),
        ]
        hier = _hier(pairs)
        # 塞一条同大小的非 hzc 条目，验证它不进候选
        hier["ハル"]["私服"].append(_info("CHR_ハル_喜_私服_ogg", len(b"uniform"), "ogg"))
        aliases = find_alias_images(hier, make_reader(pairs))
        self.assertEqual(aliases["CHR_ハル_喜_私服"], ["CHR_ハル_喜_制服"])
        self.assertNotIn("CHR_ハル_喜_私服_表情", aliases)

    def test_read_failure_skips_entry(self):
        pairs = [
            ("CHR_ハル_喜_私服", b"uniform"),
            ("CHR_ハル_喜_制服", b"uniform"),
        ]

        def read(info):
            if info["filename"] == "CHR_ハル_喜_私服":
                raise OSError("读不到")
            return dict(pairs)[info["filename"]]

        aliases = find_alias_images(_hier(pairs), read)
        self.assertEqual(aliases, {})


class SplitRoleOutfitTest(unittest.TestCase):
    def test_four_parts(self):
        self.assertEqual(split_role_outfit("CHR_ハル_喜_私服"), ("ハル", "私服"))

    def test_three_parts_default(self):
        self.assertEqual(split_role_outfit("CHR_ハル_喜"), ("ハル", "默认"))

    def test_ru_suffix(self):
        self.assertEqual(split_role_outfit("CHR_ハル_基_私服_ru"), ("ハル", "私服_ru"))

    def test_non_chr(self):
        self.assertEqual(split_role_outfit("logo"), ("logo", "默认"))


if __name__ == "__main__":
    unittest.main(verbosity=2)