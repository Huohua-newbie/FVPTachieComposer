"""对话框目录记忆（跨重启）的回归测试。

每个用例一份独立的 settings.json；目录存在性、类型错误、段落保留、原子写
都在用例内自证，不依赖开发机上的真实目录。

    python tests/test_dialog_dirs.py -v
    cd tests && python -m unittest test_dialog_dirs.DialogDirsTest -v
"""

from __future__ import annotations

import json
import threading
import unittest

try:
    from . import support
except ImportError:  # 直接 python tests/test_dialog_dirs.py
    import support

import fvpapp.settings as st  # noqa: E402


class DialogDirsTest(support.EnvTestCase):
    def setUp(self) -> None:  # noqa: N802
        super().setUp()
        self.path = st.settings_path()
        self.assertEqual(self.path, self.tmp / "settings.json")
        self.import_dir = self.tmp / "import_here"
        self.export_dir = self.tmp / "export_here"
        self.import_dir.mkdir()
        self.export_dir.mkdir()

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

    def raw(self) -> dict:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def save(self, import_dir=None, export_dir=None) -> None:
        st.save_dialog_dirs(str(import_dir) if import_dir is not None else None,
                            str(export_dir) if export_dir is not None else None)

    # ── 读取容错 ──────────────────────────────────────────────
    def test_missing_file_returns_empty(self):
        self.assertEqual(st.dialog_dirs(), {})

    def test_corrupt_files_return_empty(self):
        for bad in (b"", b"{", b"[1,2,3]", b'{"dialogs": 7}',
                    b'{"dialogs": [1]}', b"\xff\xfe\x00broken"):
            with self.subTest(payload=bad[:16]):
                self.wipe()
                self.write_raw(bad)
                self.assertEqual(st.dialog_dirs(), {})

    def test_non_string_fields_are_filtered(self):
        self.write_json({"dialogs": {"import": 123, "export": ["x"],
                                     "other": str(self.import_dir)}})
        self.assertEqual(st.dialog_dirs(), {})

    def test_unknown_dialog_keys_ignored(self):
        self.write_json({"dialogs": {"import": str(self.import_dir),
                                     "last": str(self.export_dir)}})
        self.assertEqual(st.dialog_dirs(), {"import": str(self.import_dir)})

    # ── 存取 ──────────────────────────────────────────────────
    def test_round_trip_keeps_each_direction_separate(self):
        self.save(self.import_dir, self.export_dir)
        self.assertEqual(st.dialog_dirs(), {"import": str(self.import_dir),
                                            "export": str(self.export_dir)})
        self.assertIsInstance(self.raw()["dialogs"], dict)

    def test_only_one_direction(self):
        self.save(self.import_dir, None)
        self.assertEqual(st.dialog_dirs(), {"import": str(self.import_dir)})
        self.assertIsNone(self.raw()["dialogs"]["export"])

    def test_both_none_writes_null_and_reads_empty(self):
        self.save(None, None)
        self.assertEqual(st.dialog_dirs(), {})
        self.assertEqual(self.raw()["dialogs"],
                         {"import": None, "export": None})

    def test_empty_string_is_treated_as_unset(self):
        st.save_dialog_dirs("", "")
        self.assertEqual(st.dialog_dirs(), {})
        self.assertEqual(self.raw()["dialogs"],
                         {"import": None, "export": None})

    def test_tilde_is_expanded_for_validation_but_stored_verbatim(self):
        home = self.tmp / "home"
        (home / "d").mkdir(parents=True)
        self.sandbox.set(HOME=home, USERPROFILE=home)
        st.save_dialog_dirs("~/d", None)
        self.assertEqual(st.dialog_dirs(), {"import": "~/d"})

    # ── 目录存在性校验 ────────────────────────────────────────
    def test_deleted_directory_filtered_but_sibling_kept(self):
        gone = self.tmp / "gone_dir"
        gone.mkdir()
        self.save(gone, self.export_dir)
        gone.rmdir()
        got = st.dialog_dirs()
        self.assertNotIn("import", got)
        self.assertEqual(got.get("export"), str(self.export_dir))

    def test_file_path_filtered(self):
        not_a_dir = self.tmp / "a_file.txt"
        not_a_dir.write_text("x", encoding="utf-8")
        self.save(not_a_dir, self.export_dir)
        self.assertNotIn("import", st.dialog_dirs())

    # ── 写一段不能碰别的段 ────────────────────────────────────
    def test_save_preserves_other_sections(self):
        self.write_json({"window": {"width": 1300}, "theme": "dark",
                         "other": [1, 2]})
        self.save(self.import_dir, self.export_dir)
        raw = self.raw()
        self.assertEqual(raw["window"], {"width": 1300})
        self.assertEqual(raw["theme"], "dark")
        self.assertEqual(raw["other"], [1, 2])

    def test_window_save_preserves_dialogs(self):
        self.save(self.import_dir, self.export_dir)
        st.save_window({"width": 1400, "height": 800})
        self.assertEqual(st.dialog_dirs().get("import"), str(self.import_dir))

    # ── 写盘契约 ──────────────────────────────────────────────
    def test_atomic_write_leaves_no_tmp(self):
        self.save(self.import_dir, self.export_dir)
        self.assertEqual(list(self.tmp.glob("settings.json*.tmp")), [])

    def test_unwritable_dir_does_not_raise(self):
        blocker = self.tmp / "notdir"
        blocker.write_text("x", encoding="utf-8")
        self.sandbox.set(FVP_SETTINGS_PATH=blocker / "settings.json")
        self.save(self.import_dir, self.export_dir)  # 只记日志
        self.assertEqual(st.load(), {})
        self.assertEqual(st.dialog_dirs(), {})

    def test_concurrent_saves_leave_valid_json(self):
        """两个线程抢同一次写盘：os.replace 保证读者只能看到完整文件。

        两侧 payload 等长，所以任何先后顺序读到的都是一份完整、成对的目录。
        """
        a, b = self.tmp / "a", self.tmp / "b"
        a.mkdir()
        b.mkdir()
        barrier = threading.Barrier(2)

        def worker(d):
            barrier.wait()
            for _ in range(10):
                st.save_dialog_dirs(str(d), str(d))

        threads = [threading.Thread(target=worker, args=(d,)) for d in (a, b)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        got = st.dialog_dirs()
        self.assertIn((got.get("import"), got.get("export")),
                      {(str(a), str(a)), (str(b), str(b))})
        self.assertEqual(list(self.tmp.glob("settings.json*.tmp")), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
