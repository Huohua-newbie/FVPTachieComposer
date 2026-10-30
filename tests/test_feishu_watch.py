"""飞书 Base 变更监视的回归测试：不联网、不碰真飞书、状态落在用例临时目录。

每个用例一份假的 lark-cli runner 与独立 state，断点/基线互不干扰。

    python tests/test_feishu_watch.py -v
    cd tests && python -m unittest test_feishu_watch.PaginationTest -v
"""

from __future__ import annotations

import json
import unittest

try:
    from . import support
except ImportError:  # 直接 python tests/test_feishu_watch.py
    import support

support.ensure_scripts_on_path()

import feishu_sync as fs  # noqa: E402
import feishu_watch as fw  # noqa: E402

CONFIG = {
    "base_token": "base_test",
    "identity": "user",
    "tables": {"tasks": "tblTASKS", "tests": "tblTESTS", "changes": "tblCHANGES"},
}


class FakeWatch:
    """按 `--limit/--offset` 分页返回记录的假 lark-cli。"""

    def __init__(self):
        self.rev: dict[str, int | None] = {"tblTASKS": 1, "tblTESTS": 1, "tblCHANGES": 1}
        self.fields = {
            "tblTASKS": ["任务", "状态"],
            "tblTESTS": ["用例", "结果", "备注"],
            "tblCHANGES": ["功能", "改了什么"],
        }
        self.rows = {
            "tblTASKS": {"recK1": {"任务": "卡①", "状态": ["进行中"]}},
            "tblTESTS": {"recT1": {"用例": "1.2 最大化往返", "结果": ["未跑"], "备注": ""}},
            "tblCHANGES": {"recC1": {"功能": "—", "改了什么": "旧改动"}},
        }
        self.calls: list[tuple[str, int, int]] = []

    def bump(self, table):
        if self.rev[table] is not None:
            self.rev[table] += 1

    def set(self, table, rid, values):
        self.rows[table][rid].update(values)

    def add(self, table, rid, values):
        self.rows[table][rid] = values
        self.bump(table)

    def remove(self, table, rid):
        del self.rows[table][rid]
        self.bump(table)

    def __call__(self, args):
        table = args[args.index("--table-id") + 1]
        limit = int(args[args.index("--limit") + 1])
        offset = int(args[args.index("--offset") + 1]) if "--offset" in args else 0
        self.calls.append((table, limit, offset))
        fields = self.fields[table]
        ids = list(self.rows[table])[offset:offset + limit]
        data = [[self.rows[table][rid].get(name) for name in fields] for rid in ids]
        return {"ok": True, "data": {
            "fields": list(fields),
            "record_id_list": ids,
            "data": data,
            "has_more": offset + limit < len(self.rows[table]),
            "rev": self.rev[table],
        }}

    def full_fetches(self, table=None):
        return sum(1 for name, limit, _ in self.calls
                   if limit == fw.PAGE_SIZE and (table is None or name == table))

    def offsets(self, table):
        return [offset for name, limit, offset in self.calls
                if name == table and limit == fw.PAGE_SIZE]


class Recorder:
    def __init__(self):
        self.lines: list[str] = []
        self.errors: list[str] = []

    def out(self, text):
        self.lines.append(text)

    def err(self, text):
        self.errors.append(text)


class WatchTestBase(support.EnvTestCase):
    def setUp(self):
        super().setUp()
        self.fake = FakeWatch()
        self.rec = Recorder()
        self.state = self.tmp / "state.json"

    def watcher(self, *, state=None, tables=None, runner=None, config=None):
        return fw.Watcher(config or CONFIG, state_path=state or self.state,
                          runner=runner or self.fake,
                          out=self.rec.out, err=self.rec.err, tables=tables)

    def baseline(self, tables=None):
        watcher = self.watcher(tables=tables)
        watcher.poll_once()
        self.rec.lines.clear()
        self.rec.errors.clear()
        return watcher

    def read_state(self, path=None):
        return json.loads((path or self.state).read_text(encoding="utf-8"))


class BaselineTest(WatchTestBase):
    def test_first_poll_builds_baseline_without_reporting_existing_rows(self):
        self.watcher().poll_once()
        self.assertEqual(self.rec.lines, [], "首次运行不该把已有行当新增")
        self.assertEqual(len(self.rec.errors), 3)
        self.assertTrue(all("建基线" in e for e in self.rec.errors))
        self.assertEqual(self.fake.full_fetches(), 3)

        state = self.read_state()
        self.assertEqual(state["tables"]["tests"]["rev"], 1)
        self.assertEqual(state["tables"]["tests"]["snapshot"]["records"]["recT1"]["用例"],
                         "1.2 最大化往返")
        self.assertEqual(list(self.tmp.glob("*.tmp")), [], "状态写盘不该留下 .tmp")

    def test_only_selected_tables_are_polled(self):
        self.watcher(tables=["tasks"]).poll_once()
        self.assertEqual([name for name, _, _ in self.fake.calls], ["tblTASKS"] * 2)
        self.assertEqual(list(self.read_state()["tables"]), ["tasks"])

    def test_unknown_table_is_rejected(self):
        bad = {**CONFIG, "tables": {"tasks": "tblTASKS"}}
        with self.assertRaises(fs.SyncError):
            self.watcher(config=bad)


class DiffTest(WatchTestBase):
    def test_unchanged_rev_reads_one_row_per_table_and_skips_full_fetch(self):
        watcher = self.baseline()
        calls_before = len(self.fake.calls)
        watcher.poll_once()
        self.assertEqual(self.rec.lines, [])
        self.assertEqual(self.fake.full_fetches(), 3, "rev 没动不该拉全表")
        self.assertEqual(len(self.fake.calls) - calls_before, 3, "每张表查一次 rev")

    def test_modified_row_reports_old_and_new_value(self):
        watcher = self.baseline()
        self.fake.set("tblTESTS", "recT1", {"结果": ["通过"]})
        self.fake.bump("tblTESTS")
        watcher.poll_once()
        self.assertEqual(len(self.rec.lines), 1, self.rec.lines)
        line = self.rec.lines[0]
        self.assertIn("[修改] 测试 · 1.2 最大化往返", line)
        self.assertIn("结果: 未跑 → 通过", line)
        self.assertEqual(self.fake.full_fetches(), 4, "只该补拉改动的那张表")

    def test_added_and_removed_rows_each_report_one_line(self):
        watcher = self.baseline()
        self.fake.add("tblTESTS", "recT2", {"用例": "1.8 新用例", "结果": ["未跑"], "备注": ""})
        watcher.poll_once()
        self.assertEqual(self.rec.lines, ["[新增] 测试 · 1.8 新用例"])

        self.rec.lines.clear()
        self.fake.remove("tblTESTS", "recT2")
        watcher.poll_once()
        self.assertEqual(len(self.rec.lines), 1)
        self.assertIn("[删除] 测试 · 1.8 新用例", self.rec.lines[0])

    def test_column_add_and_remove_report_structure_only(self):
        watcher = self.baseline()
        self.fake.fields["tblTESTS"].append("负责人")
        self.fake.bump("tblTESTS")
        watcher.poll_once()
        self.assertEqual(self.rec.lines, ["[结构] 测试 · 新增列 负责人"])

        self.rec.lines.clear()
        self.fake.fields["tblTESTS"].remove("负责人")
        self.fake.bump("tblTESTS")
        watcher.poll_once()
        self.assertEqual(self.rec.lines, ["[结构] 测试 · 删除列 负责人"])

    def test_multi_field_change_stays_one_line_and_caps_detail(self):
        self.fake.fields["tblTASKS"].extend(["优先级", "截止", "负责人"])
        watcher = self.baseline()
        self.fake.set("tblTASKS", "recK1", {"任务": "卡①改名", "状态": ["已完成"],
                                            "优先级": "高", "截止": "明天",
                                            "负责人": "kitsuneMori"})
        self.fake.bump("tblTASKS")
        watcher.poll_once()
        self.assertEqual(len(self.rec.lines), 1, self.rec.lines)
        line = self.rec.lines[0]
        self.assertIn("任务: 卡① → 卡①改名", line)
        self.assertIn("等 5 项", line)

    def test_new_watcher_resumes_from_state_without_repeating(self):
        self.baseline()
        rec2 = Recorder()
        watcher2 = fw.Watcher(CONFIG, state_path=self.state, runner=self.fake,
                              out=rec2.out, err=rec2.err)
        watcher2.poll_once()
        self.assertEqual(rec2.lines, [], "续跑不应重复报基线里的行")

    def test_rev_none_forces_full_snapshot_each_round(self):
        for table in self.fake.rev:
            self.fake.rev[table] = None
        watcher = self.watcher()
        watcher.poll_once()
        self.assertEqual(self.fake.full_fetches(), 3)
        self.assertIsNone(self.read_state()["tables"]["tests"]["rev"])

        self.fake.calls.clear()
        watcher.poll_once()
        self.assertEqual(self.fake.full_fetches(), 3, "没有 rev 只能每轮重新拉")
        self.assertEqual(self.rec.lines, [])
        self.assertNotIn("rev", self.read_state()["tables"]["tests"])

    def test_clean_state_is_written_with_snapshot(self):
        self.baseline()
        self.fake.set("tblTASKS", "recK1", {"任务": "新名字"})
        self.fake.bump("tblTASKS")
        self.watcher().poll_once()
        snapshot = self.read_state()["tables"]["tasks"]["snapshot"]["records"]["recK1"]
        self.assertEqual(snapshot["任务"], "新名字")


class PaginationTest(WatchTestBase):
    def build(self, count=250):
        self.fake.rows["tblTESTS"] = {
            f"rec{i:04d}": {"用例": f"用例 {i}", "结果": ["未跑"], "备注": ""}
            for i in range(count)
        }
        self.fake.fields["tblTESTS"] = ["用例", "结果", "备注"]

    def test_snapshot_walks_all_pages(self):
        self.build(250)
        self.baseline(tables=["tests"])
        self.assertEqual(self.fake.offsets("tblTESTS"), [0, 200],
                         "250 行应分两页拉，offset 依次 0/200")
        records = self.read_state()["tables"]["tests"]["snapshot"]["records"]
        self.assertEqual(len(records), 250)
        self.assertIn("rec0249", records)

    def test_change_in_last_page_is_detected(self):
        self.build(250)
        watcher = self.baseline(tables=["tests"])
        self.fake.set("tblTESTS", "rec0249", {"结果": ["通过"]})
        self.fake.bump("tblTESTS")
        watcher.poll_once()
        self.assertEqual(len(self.rec.lines), 1, self.rec.lines)
        self.assertIn("用例 249", self.rec.lines[0])
        self.assertIn("结果: 未跑 → 通过", self.rec.lines[0])
        self.assertEqual(self.fake.offsets("tblTESTS")[-2:], [0, 200])


class BadStateTest(WatchTestBase):
    def test_corrupt_state_rebuilds_baseline(self):
        bad = self.tmp / "bad.json"
        bad.write_text("{", encoding="utf-8")
        self.watcher(state=bad).poll_once()
        self.assertEqual(self.rec.lines, [], "坏状态应静默重建基线")
        state = self.read_state(bad)
        self.assertIn("snapshot", state["tables"]["tests"])

    def test_non_dict_state_is_ignored(self):
        bad = self.tmp / "list.json"
        bad.write_text("[1, 2]", encoding="utf-8")
        self.watcher(state=bad).poll_once()
        self.assertIn("snapshot", self.read_state(bad)["tables"]["tests"])

    def test_unwritable_state_propagates_instead_of_silently_succeeding(self):
        blocked = self.tmp / "state-dir"
        blocked.mkdir()
        with self.assertRaises(OSError):
            self.watcher(state=blocked).poll_once()


class LabelsTest(support.EnvTestCase):
    def test_label_prefers_known_fields_in_order(self):
        fields = {"用例": "1.2 最大化往返", "任务": "卡①"}
        self.assertEqual(fw.record_label("tests", fields, "recT1"), "1.2 最大化往返")
        self.assertEqual(fw.record_label("tasks", fields, "recK1"), "卡①")

    def test_label_falls_back_to_first_non_empty_string(self):
        self.assertEqual(fw.record_label("tests", {"其他": "", "说明": "随便"}, "recX"), "随便")

    def test_label_falls_back_to_record_id(self):
        self.assertEqual(fw.record_label("tests", {"结果": ["未跑"]}, "recX"), "recX")

    def test_short_renders_empty_lists_and_truncates(self):
        self.assertEqual(fw._short(None), "空")
        self.assertEqual(fw._short([]), "空")
        self.assertEqual(fw._short(["进行中", "高"]), "进行中、高")
        long_text = "一" * 40
        self.assertTrue(fw._short(long_text, 10).endswith("…"))
        self.assertEqual(len(fw._short(long_text, 10)), 10)


class RunOnceTest(WatchTestBase):
    class Boom:
        def __call__(self, _args):
            raise fs.SyncError("读表失败")

    def test_success_returns_zero(self):
        self.assertEqual(fw.run_once(self.watcher()), 0)
        self.assertEqual(self.rec.errors[0].count("建基线"), 1)

    def test_fetch_failure_returns_one_and_reports(self):
        watcher = self.watcher(runner=self.Boom())
        self.assertEqual(fw.run_once(watcher), 1)
        self.assertTrue(any("读表失败" in e for e in self.rec.errors), self.rec.errors)


if __name__ == "__main__":
    unittest.main(verbosity=2)
