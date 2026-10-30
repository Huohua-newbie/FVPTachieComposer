"""窗口记忆（尺寸 / 位置 / 最大化）的回归测试。

每个用例一份独立的 settings.json（`support.EnvTestCase` 提供临时目录与
`FVP_SETTINGS_PATH`），屏幕范围用 monkeypatch 注入，结果与开发机分辨率无关。

    python tests/test_window_memory.py -v
    cd tests && python -m unittest test_window_memory.PlanWindowTest -v
"""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from unittest import mock

try:
    from . import support
except ImportError:  # 直接 python tests/test_window_memory.py
    import support

import fvpapp.settings as st  # noqa: E402

SCREEN = (0.0, 0.0, 1920.0, 1080.0)
LEFT_SCREEN = (-2560.0, 0.0, 4480.0, 1080.0)  # 副屏在主屏左边

_AUTO = object()


class FakeWindow:
    """够用的 flet Window 替身。"""

    def __init__(self, width=1280, height=760, left=100, top=80,
                 maximized=False):
        self.width, self.height = width, height
        self.left, self.top = left, top
        self.maximized = maximized


class BareWindow:
    pass


class SettingsTestBase(support.EnvTestCase):
    def setUp(self) -> None:  # noqa: N802
        super().setUp()
        self.path = st.settings_path()
        self.assertEqual(self.path, self.tmp / "settings.json")

    # ── 小工具 ────────────────────────────────────────────────
    def wipe(self) -> None:
        for p in (self.path, self.path.with_name(self.path.name + ".tmp")):
            try:
                p.unlink()
            except OSError:
                pass

    def write_raw(self, data: bytes | str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, bytes):
            self.path.write_bytes(data)
        else:
            self.path.write_text(data, encoding="utf-8")

    def write_json(self, data) -> None:
        self.write_raw(json.dumps(data, ensure_ascii=False))

    def plan(self, prefs=None, bounds=_AUTO) -> dict:
        """默认走 `plan_window` 自己的 `screen_bounds()`，以便验证接线。"""
        if bounds is _AUTO:
            with mock.patch.object(st, "screen_bounds", lambda: SCREEN):
                return st.plan_window(prefs)
        return st.plan_window(prefs, bounds)


class WindowPrefsTest(SettingsTestBase):
    def test_missing_file_returns_empty(self):
        self.assertEqual(st.window_prefs(), {})
        self.assertEqual(st.load(), {})

    def test_corrupt_files_fall_back_to_empty(self):
        for bad in (b"", b"{", b"[1,2,3]", b'{"window": 7}',
                    b"\xff\xfe\x00broken",
                    '{"window": {"width": "big", "left": null}}'):
            with self.subTest(payload=bad[:20]):
                self.wipe()
                self.write_raw(bad)
                self.assertEqual(st.window_prefs(), {})

    def test_top_level_not_object_is_empty(self):
        self.write_json([1, 2, 3])
        self.assertEqual(st.load(), {})

    def test_type_errors_are_filtered(self):
        self.write_json({"window": {
            "width": True,          # bool 不是尺寸
            "height": "900",
            "left": float("nan"),
            "top": 120,
            "maximized": "yes",
        }})
        self.assertEqual(st.window_prefs(), {"top": 120.0})

    def test_infinite_coordinates_are_filtered(self):
        # 尺寸走 _clean_size（>0 即可），无穷大坐标走 _clean_coord 的 |v|<1e6
        self.write_raw('{"window": {"width": Infinity, "height": 1e309, '
                       '"left": Infinity, "top": -Infinity}}')
        prefs = st.window_prefs()
        self.assertNotIn("left", prefs)
        self.assertNotIn("top", prefs)
        self.assertTrue(float("inf") == prefs.get("width"))

    def test_absurd_size_is_clamped_by_bounds(self):
        self.write_raw('{"window": {"width": Infinity, "height": 1e309}}')
        plan = self.plan()
        self.assertEqual((plan["width"], plan["height"]), (1920, 1080))

    def test_positive_sizes_only(self):
        self.write_json({"window": {"width": 0, "height": -10}})
        self.assertEqual(st.window_prefs(), {})

    def test_negative_coordinates_are_kept(self):
        self.write_json({"window": {"left": -1500, "top": -40}})
        self.assertEqual(st.window_prefs(), {"left": -1500.0, "top": -40.0})

    def test_maximized_must_be_bool(self):
        self.write_json({"window": {"maximized": 1}})
        self.assertEqual(st.window_prefs(), {})
        self.write_json({"window": {"maximized": False}})
        self.assertEqual(st.window_prefs(), {"maximized": False})

    def test_round_trip_through_save(self):
        st.save_window({"width": 1440, "height": 900, "left": 320,
                        "top": 150, "maximized": True})
        self.assertEqual(st.window_prefs(), {
            "width": 1440.0, "height": 900.0, "left": 320.0,
            "top": 150.0, "maximized": True,
        })
        self.assertIsInstance(st.load()["window"], dict)

    def test_unknown_keys_ignored(self):
        self.write_json({"window": {"width": 1300, "bogus": "x", "screen": 2}})
        self.assertEqual(st.window_prefs(), {"width": 1300.0})

    def test_save_preserves_other_sections(self):
        self.write_json({"theme": "dark", "window": {"width": 1300}})
        st.save_window({"width": 1400, "height": 800})
        raw = st.load()
        self.assertEqual(raw["theme"], "dark")
        self.assertEqual(raw["window"]["width"], 1400)

    def test_delete_file_resets_to_default(self):
        st.save_window({"width": 1440, "height": 900, "left": 320, "top": 150})
        self.wipe()
        plan = self.plan()
        self.assertEqual((plan["width"], plan["height"]),
                         (st.DEFAULT_WIDTH, st.DEFAULT_HEIGHT))
        self.assertIsNone(plan["left"])

    def test_atomic_write_leaves_no_tmp(self):
        st.save_window({"width": 1300, "height": 800})
        self.assertEqual(list(self.tmp.glob("settings.json*.tmp")), [])

    def test_unwritable_dir_does_not_raise(self):
        blocker = self.tmp / "notdir"
        blocker.write_text("x", encoding="utf-8")
        self.sandbox.set(FVP_SETTINGS_PATH=blocker / "settings.json")
        st.save_window({"width": 1300, "height": 800})  # 只记日志
        self.assertEqual(st.load(), {})
        self.assertEqual(st.window_prefs(), {})
        self.assertEqual(self.plan()["width"], st.DEFAULT_WIDTH)

    @unittest.skipIf(os.name == "nt", "Windows 走 %LOCALAPPDATA%")
    def test_settings_path_xdg_fallback(self):
        self.sandbox.pop("FVP_SETTINGS_PATH")
        self.sandbox.set(XDG_CONFIG_HOME=self.tmp / "cfg")
        self.assertEqual(st.settings_path(),
                         self.tmp / "cfg" / st.APP_NAME / "settings.json")

    def test_settings_path_env_override_expands_home(self):
        env_home = self.tmp / "home"
        env_home.mkdir()
        self.sandbox.set(FVP_SETTINGS_PATH="~/s.json", HOME=env_home,
                         USERPROFILE=env_home)
        self.assertEqual(st.settings_path(), env_home / "s.json")


class PlanWindowTest(SettingsTestBase):
    def save(self, **prefs) -> None:
        st.save_window(prefs)

    def test_defaults_without_prefs(self):
        plan = self.plan()
        self.assertEqual((plan["width"], plan["height"]),
                         (st.DEFAULT_WIDTH, st.DEFAULT_HEIGHT))
        self.assertIsNone(plan["left"])
        self.assertIsNone(plan["top"])
        self.assertFalse(plan["maximized"])

    def test_round_trip_restores_all(self):
        self.save(width=1440, height=900, left=320, top=150, maximized=True)
        plan = self.plan()
        self.assertEqual((plan["width"], plan["height"]), (1440, 900))
        self.assertEqual((plan["left"], plan["top"]), (320, 150))
        self.assertIs(plan["maximized"], True)

    def test_plan_window_reads_prefs_from_disk(self):
        self.save(width=1440, height=900)
        plan = self.plan(None)
        self.assertEqual((plan["width"], plan["height"]), (1440, 900))

    def test_oversized_window_clamped_to_desktop(self):
        self.save(width=4000, height=3000, left=0, top=0)
        plan = self.plan()
        self.assertEqual((plan["width"], plan["height"]), (1920, 1080))

    def test_undersized_window_clamped_to_minimum(self):
        self.save(width=100, height=100)
        plan = self.plan()
        self.assertEqual((plan["width"], plan["height"]),
                         (st.MIN_WIDTH, st.MIN_HEIGHT))

    def test_off_screen_position_dropped_but_size_kept(self):
        self.save(width=1280, height=760, left=99999, top=400)
        plan = self.plan()
        self.assertIsNone(plan["left"])
        self.assertIsNone(plan["top"])
        self.assertEqual((plan["width"], plan["height"]), (1280, 760))

    def test_fully_negative_position_dropped(self):
        self.save(width=1280, height=760, left=-4000, top=-3000)
        plan = self.plan()
        self.assertIsNone(plan["left"])

    def test_negative_position_on_left_monitor_kept(self):
        self.save(width=1280, height=760, left=-1500, top=200)
        plan = self.plan(bounds=LEFT_SCREEN)
        self.assertEqual((plan["left"], plan["top"]), (-1500, 200))

    def test_overlap_boundary_exactly_enough_is_kept(self):
        # 横向至少露出 MIN_ONSCREEN_X=64px，纵向至少 MIN_ONSCREEN_Y=48px
        self.save(width=1280, height=760,
                  left=1920 - st.MIN_ONSCREEN_X, top=0)
        self.assertIsNotNone(self.plan()["left"])
        self.save(width=1280, height=760,
                  left=1920 - st.MIN_ONSCREEN_X + 1, top=0)
        self.assertIsNone(self.plan()["left"])

    def test_vertical_overlap_boundary(self):
        self.save(width=1280, height=760,
                  left=0, top=1080 - st.MIN_ONSCREEN_Y)
        self.assertIsNotNone(self.plan()["top"])
        self.save(width=1280, height=760,
                  left=0, top=1080 - st.MIN_ONSCREEN_Y + 1)
        self.assertIsNone(self.plan()["top"])

    def test_left_edge_overlap_boundary(self):
        self.save(width=1280, height=760,
                  left=-1280 + st.MIN_ONSCREEN_X, top=0)
        self.assertIsNotNone(self.plan()["left"])
        self.save(width=1280, height=760,
                  left=-1280 + st.MIN_ONSCREEN_X - 1, top=0)
        self.assertIsNone(self.plan()["left"])

    def test_partial_position_without_top_dropped(self):
        self.save(width=1280, height=760, left=100)
        plan = self.plan()
        self.assertIsNone(plan["left"])
        self.assertIsNone(plan["top"])

    def test_unknown_bounds_keeps_size_drops_position(self):
        self.save(width=1440, height=900, left=300, top=200)
        plan = self.plan(bounds=None)
        self.assertEqual((plan["width"], plan["height"]), (1440, 900))
        self.assertIsNone(plan["left"])
        self.assertIsNone(plan["top"])

    def test_small_screen_still_respects_minimum(self):
        self.save(width=4000, height=3000)
        plan = self.plan(bounds=(0.0, 0.0, 800.0, 500.0))
        self.assertEqual((plan["width"], plan["height"]),
                         (st.MIN_WIDTH, st.MIN_HEIGHT))


class CaptureWindowTest(SettingsTestBase):
    def test_plain_window_records_geometry(self):
        got = st.capture_window(FakeWindow(1440, 900, 320, 150), False, {})
        self.assertEqual(got, {"maximized": False, "width": 1440.0,
                               "height": 900.0, "left": 320.0, "top": 150.0})

    def test_maximized_only_flips_flag(self):
        last = {"width": 1440, "height": 900, "left": 320, "top": 150,
                "maximized": False}
        got = st.capture_window(FakeWindow(1920, 1080, -8, -8, True),
                                True, last)
        self.assertTrue(got["maximized"])
        self.assertEqual((got["width"], got["height"]), (1440, 900))
        self.assertEqual((got["left"], got["top"]), (320, 150))

    def test_restored_window_records_new_position(self):
        got = st.capture_window(FakeWindow(1440, 900, 500, 400), False,
                                {"width": 1440, "height": 900,
                                 "left": 320, "top": 150, "maximized": True})
        self.assertEqual((got["left"], got["top"], got["maximized"]),
                         (500.0, 400.0, False))

    def test_bad_values_fall_back_to_previous(self):
        prev = {"width": 1440, "height": 900, "left": 500, "top": 400}
        bad = FakeWindow(width=0, height=None, left=float("nan"), top=None)
        got = st.capture_window(bad, False, prev)
        self.assertEqual((got["width"], got["height"], got["left"], got["top"]),
                         (1440, 900, 500, 400))

    def test_bad_values_without_previous_are_omitted(self):
        got = st.capture_window(FakeWindow(width=0, height=-5, left="x",
                                           top=float("inf")), False, {})
        self.assertEqual(got, {"maximized": False})

    def test_missing_attributes_do_not_raise(self):
        got = st.capture_window(BareWindow(), False,
                                {"width": 1280, "height": 760})
        self.assertEqual((got["width"], got["height"]), (1280, 760))

    def test_bool_is_not_a_size(self):
        got = st.capture_window(FakeWindow(width=True, height=False), False,
                                {"width": 1280, "height": 760})
        self.assertEqual((got["width"], got["height"]), (1280, 760))

    def test_capture_output_is_saveable_and_plannable(self):
        got = st.capture_window(FakeWindow(1366, 768, 10, 20), False, {})
        st.save_window(got)
        plan = self.plan()
        self.assertEqual((plan["width"], plan["height"]), (1366, 768))
        self.assertEqual((plan["left"], plan["top"]), (10, 20))


class ScreenBoundsTest(SettingsTestBase):
    @unittest.skipIf(os.name == "nt", "Windows 上会真的问 Win32")
    def test_non_windows_returns_none(self):
        self.assertIsNone(st.screen_bounds())

    def test_returns_none_or_valid_box(self):
        bounds = st.screen_bounds()
        if bounds is not None:
            self.assertEqual(len(bounds), 4)
            self.assertGreater(bounds[2], 0)
            self.assertGreater(bounds[3], 0)

    def test_on_screen_uses_shrunk_threshold_for_narrow_windows(self):
        # 窗口比阈值(min 64px)还窄时，整窗露出才算在屏幕内
        self.assertTrue(st._on_screen(1920 - 40, 0, 40, 100, SCREEN))
        self.assertFalse(st._on_screen(1920 - 39, 0, 40, 100, SCREEN))
        self.assertFalse(st._on_screen(1920, 0, 40, 100, SCREEN))

    def test_on_screen_negative_origin(self):
        self.assertTrue(st._on_screen(-2500, 100, 1280, 760, LEFT_SCREEN))
        self.assertFalse(st._on_screen(-2560 - 1280, 100, 1280, 760, LEFT_SCREEN))

    def test_on_screen_requires_both_axes(self):
        self.assertFalse(st._on_screen(100, 1080, 1280, 760, SCREEN))
        self.assertFalse(st._on_screen(1920, 100, 1280, 760, SCREEN))


if __name__ == "__main__":
    unittest.main(verbosity=2)
