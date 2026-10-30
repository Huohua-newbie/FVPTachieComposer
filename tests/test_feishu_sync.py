"""需求变更同步脚本的回归测试：不联网、不碰真飞书、不写仓库状态。

lark-cli 全部用假 runner 顶掉；每个用例独立 config/state/manifest 临时目录，
失败不会互相污染。

    python tests/test_feishu_sync.py -v
    cd tests && python -m unittest test_feishu_sync.ResumeTest -v
"""

from __future__ import annotations

import copy
import io
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

try:
    from . import support
except ImportError:  # 直接 python tests/test_feishu_sync.py
    import support

support.ensure_scripts_on_path()

import feishu_sync as fs  # noqa: E402

CONFIG = {
    "base_token": "base_test",
    "identity": "user",
    "tables": {"tasks": "tblTASKS", "tests": "tblTESTS", "changes": "tblCHANGES"},
    "notify": {"chat_id": "oc_test", "mentions": ["ou_test"]},
}

MANIFEST = {
    "id": "test-change",
    "summary": "改测试目标",
    "old_goal": "旧目标",
    "new_goal": "新目标",
    "commit": "abc1234",
    "task": {
        "record_id": "recTASK",
        "acceptance_append": "；新验收",
        "note_append": "kitsuneMori｜2026-10-28：补",
    },
    "tests": {
        "block": [{"record_id": "recTEST", "用例": "1.5 旧用例",
                   "note_append": "目标已变更，暂停"}],
        "create": [{"用例": "1.6 新用例", "关联卡": ["窗口尺寸/位置记忆"],
                    "类型": ["无头+手测"], "结果": ["未跑"]}],
    },
    "change": {"功能": "—", "类型": ["修改"],
               "改了什么": "改了目标", "为什么": "旧目标不对"},
}

TODAY = "2026-10-28"


class FakeLark:
    """按 argv 分派的假 lark-cli；只记录调用，不碰网络。"""

    def __init__(self, fail_create: str | None = None):
        self.calls: list[list[str]] = []
        self.updates: list[tuple[str, dict]] = []
        self.creates: list[tuple[str, list[dict]]] = []
        self.messages: list[dict] = []
        self.fail_create = fail_create
        self.records = {
            ("tblTASKS", "recTASK"): {"验收标准": "旧目标", "负责人备注": "旧备注"},
            ("tblTESTS", "recTEST"): {"备注": "旧备注", "结果": ["未跑"]},
        }

    def __call__(self, args):
        self.calls.append(list(args))
        table = args[args.index("--table-id") + 1] if "--table-id" in args else None
        if "+record-get" in args:
            rid = args[args.index("--record-id") + 1]
            rec = self.records[(table, rid)]
            return {"ok": True,
                    "data": {"data": [list(rec.values())], "fields": list(rec.keys())}}
        if "+record-batch-update" in args:
            payload = json.loads(args[args.index("--json") + 1])
            self.updates.append((table, payload["update_records"]))
            return {"ok": True, "data": {"record_id_list": list(payload["update_records"])}}
        if "+record-batch-create" in args:
            if self.fail_create == table:
                raise fs.SyncError("boom")
            payload = json.loads(args[args.index("--json") + 1])
            self.creates.append((table, payload["create_records"]))
            ids = ["recNEWTEST"] if table == "tblTESTS" else ["recNEWCHANGE"]
            return {"ok": True, "data": {"record_id_list": ids}}
        if "+messages-send" in args:
            self.messages.append({
                "chat_id": args[args.index("--chat-id") + 1],
                "text": args[args.index("--markdown") + 1],
                "key": args[args.index("--idempotency-key") + 1],
                "as": args[args.index("--as") + 1],
            })
            return {"ok": True, "data": {"message_id": "om_test"}}
        raise AssertionError(f"未预期的 lark-cli 调用: {list(args)}")

    def updates_for(self, table):
        return [payload for name, payload in self.updates if name == table]

    def creates_for(self, table):
        return [rows for name, rows in self.creates if name == table]


class SyncTestBase(support.EnvTestCase):
    def write_config(self, path: Path | None = None, config: dict | None = None) -> Path:
        path = Path(path or self.tmp / "config.json")
        body = CONFIG if config is None else config
        path.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
        return path

    def write_manifest(self, manifest: dict | None = None,
                       path: Path | None = None) -> Path:
        path = Path(path or self.tmp / "change.json")
        body = MANIFEST if manifest is None else manifest
        path.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
        return path

    def sync(self, fake, *, manifest=None, config=None, state=None, **kwargs):
        cfg = self.write_config(config=config)
        mf = self.write_manifest(manifest)
        return fs.sync_manifest(mf, config_path=cfg,
                                state_path=Path(state or self.tmp / "state.json"),
                                runner=fake, out=self.out.append, today=TODAY, **kwargs)

    def setUp(self):
        super().setUp()
        self.out: list[str] = []
        self.errors: list[str] = []

    @property
    def state_path(self) -> Path:
        return self.tmp / "state.json"


class SyncManifestTest(SyncTestBase):
    def full_sync(self, fake=None):
        fake = fake or FakeLark()
        entry = self.sync(fake)
        return fake, entry

    def test_task_fields_are_appended_not_overwritten(self):
        fake, entry = self.full_sync()
        self.assertIs(entry.get("complete"), True)

        updates = fake.updates_for("tblTASKS")
        self.assertEqual(len(updates), 1, "任务卡只更新一次")
        task = updates[0]["recTASK"]
        self.assertEqual(task["验收标准"], "旧目标；新验收")
        self.assertEqual(task["负责人备注"], "旧备注\nkitsuneMori｜2026-10-28：补")

    def test_blocked_tests_marked_and_note_appended(self):
        fake, _ = self.full_sync()
        updates = fake.updates_for("tblTESTS")
        self.assertEqual(len(updates), 1)
        blocked = updates[0]["recTEST"]
        self.assertEqual(blocked["结果"], ["阻塞"], "没显式改结果的旧行应当默认阻塞")
        self.assertEqual(blocked["备注"], "旧备注\n目标已变更，暂停")

    def test_new_tests_and_change_record_created(self):
        fake, _ = self.full_sync()
        created_tests = fake.creates_for("tblTESTS")
        self.assertEqual(len(created_tests), 1, "新增测试行只建一次")
        self.assertEqual(created_tests[0][0]["用例"], "1.6 新用例")

        change = fake.creates_for("tblCHANGES")[0][0]
        self.assertEqual(change["日期"], f"{TODAY}T00:00:00+08:00")
        self.assertEqual(change["版本"], "待定")
        self.assertEqual(change["关联"], "abc1234")
        self.assertEqual(change["改了什么"], "改了目标")

    def test_group_message_content_and_identity(self):
        fake, _ = self.full_sync()
        self.assertEqual(len(fake.messages), 1, "群消息只发一条")
        message = fake.messages[0]
        self.assertEqual(message["as"], "bot", "群消息默认用 bot 身份")
        self.assertEqual(message["chat_id"], "oc_test")

        reads = [c for c in fake.calls if "+messages-send" not in c]
        self.assertTrue(reads)
        self.assertTrue(all(c[c.index("--as") + 1] == "user" for c in reads),
                        "读表仍用 user 身份")

        text = message["text"]
        self.assertIn('<at user_id="ou_test"></at>', text)
        self.assertIn("旧目标", text)
        self.assertIn("新目标", text)
        self.assertIn("abc1234", text)
        self.assertIn("1.6 新用例", text)
        self.assertTrue(message["key"].startswith("fvp-"))
        self.assertEqual(len(message["key"]), 20)

    def test_second_run_is_idempotent(self):
        self.full_sync()
        fake2 = FakeLark()
        entry = self.sync(fake2, state=self.state_path)
        self.assertIs(entry.get("complete"), True)
        self.assertEqual(fake2.calls, [], "同一清单重跑不应再调 lark-cli")

    def test_dry_run_reads_but_never_writes_or_records_state(self):
        fake = FakeLark()
        self.sync(fake, dry_run=True)
        self.assertTrue(any("+record-get" in c for c in fake.calls))
        self.assertFalse(fake.updates)
        self.assertFalse(fake.creates)
        self.assertFalse(fake.messages)
        self.assertFalse(self.state_path.exists(), "dry-run 不该写状态文件")

    def test_acceptance_set_replaces_instead_of_appending(self):
        manifest = copy.deepcopy(MANIFEST)
        manifest["task"] = {"record_id": "recTASK", "acceptance_set": "全新验收"}
        fake = FakeLark()
        self.sync(fake, manifest=manifest)
        task = fake.updates_for("tblTASKS")[0]["recTASK"]
        self.assertEqual(task["验收标准"], "全新验收")

    def test_manifest_notify_text_overrides_renderer(self):
        manifest = copy.deepcopy(MANIFEST)
        manifest["notify"] = {"text": "自定义通知", "chat_id": "oc_custom"}
        fake = FakeLark()
        self.sync(fake, manifest=manifest)
        self.assertEqual(fake.messages[0]["chat_id"], "oc_custom")
        self.assertEqual(fake.messages[0]["text"], "自定义通知")

    def test_idempotency_key_tracks_manifest_content(self):
        self.assertEqual(fs.idempotency_key("x", "a"), fs.idempotency_key("x", "a"))
        self.assertNotEqual(fs.idempotency_key("x", "a"), fs.idempotency_key("x", "b"))


class ResumeTest(SyncTestBase):
    def test_failed_step_resumes_without_repeating_done_steps(self):
        cfg = self.write_config()
        mf = self.write_manifest()
        with self.assertRaises(fs.SyncError):
            fs.sync_manifest(mf, config_path=cfg, state_path=self.state_path,
                             runner=FakeLark(fail_create="tblCHANGES"),
                             out=self.out.append, today=TODAY)

        state = fs.load_state(self.state_path)
        entry = state["test-change"]
        self.assertFalse(entry.get("complete"), "失败的清单不该标记 complete")
        for step in ("task", "tests_block", "tests_create"):
            self.assertTrue(entry["done"].get(step), f"{step} 应留在断点里")

        resume = FakeLark()
        fs.sync_manifest(mf, config_path=cfg, state_path=self.state_path,
                         runner=resume, out=self.out.append, today=TODAY)
        self.assertFalse(any("+record-get" in c for c in resume.calls),
                         "续跑不该再读任务卡/测试行")
        self.assertEqual(len(resume.creates_for("tblCHANGES")), 1)
        self.assertEqual(len(resume.messages), 1)
        self.assertIs(fs.load_state(self.state_path)["test-change"]["complete"], True)

    def test_force_reruns_a_completed_manifest(self):
        self.sync(FakeLark())
        fake = FakeLark()
        self.sync(fake, force=True)
        self.assertTrue(fake.calls, "--force 应重新走一遍")
        self.assertEqual(len(fake.messages), 1)

    def test_no_notify_skips_message_but_syncs(self):
        fake = FakeLark()
        entry = self.sync(fake, notify=False)
        self.assertFalse(fake.messages)
        self.assertIs(entry.get("complete"), True)
        self.assertTrue(entry["done"].get("change"))

    def test_manifest_notify_false_skips_message(self):
        manifest = copy.deepcopy(MANIFEST)
        manifest["notify"] = False
        fake = FakeLark()
        entry = self.sync(fake, manifest=manifest)
        self.assertFalse(fake.messages)
        self.assertNotIn("notify", entry["done"])
        self.assertIs(entry.get("complete"), True)

    def test_corrupt_state_file_is_ignored(self):
        self.state_path.write_text("{not json", encoding="utf-8")
        fake = FakeLark()
        entry = self.sync(fake)
        self.assertIs(entry.get("complete"), True)
        self.assertTrue(fake.calls)
        self.assertEqual(fs.load_state(self.state_path)["test-change"]["complete"], True)

    def test_unwritable_state_aborts_the_sync(self):
        cfg = self.write_config()
        mf = self.write_manifest()
        blocked = self.tmp / "state-dir"
        blocked.mkdir()
        with self.assertRaises(fs.SyncError):
            fs.sync_manifest(mf, config_path=cfg, state_path=blocked,
                             runner=FakeLark(), out=self.out.append, today=TODAY)


class ManifestValidationTest(SyncTestBase):
    def assert_rejected(self, manifest):
        with self.assertRaises(fs.SyncError):
            fs.normalize_manifest(copy.deepcopy(manifest))

    def test_rejects_invalid_content(self):
        self.assert_rejected(dict(MANIFEST, id="有中文/空格"))
        self.assert_rejected(dict(MANIFEST, summary="  "))
        self.assert_rejected({"id": "x"})  # 没有任何可同步内容
        self.assert_rejected([1, 2, 3])

    def test_rejects_task_mistakes(self):
        self.assert_rejected(dict(MANIFEST, task={"record_id": "r",
                                                  "acceptance_set": "x",
                                                  "acceptance_append": "y"}))
        self.assert_rejected(dict(MANIFEST, task={"record_id": "", "note_append": "x"}))
        self.assert_rejected(dict(MANIFEST, task={"record_id": "r", "fields": []}))
        self.assert_rejected(dict(MANIFEST, task="recTASK"))

    def test_rejects_test_mistakes(self):
        self.assert_rejected(dict(MANIFEST, tests={"block": [{"record_id": "r"},
                                                             {"record_id": "r"}]}))
        self.assert_rejected(dict(MANIFEST, tests={"block": ["r"]}))
        self.assert_rejected(dict(MANIFEST, tests={"create": [{}]}))
        self.assert_rejected(dict(MANIFEST, tests={"block": "r"}))
        self.assert_rejected(dict(MANIFEST, tests=[]))

    def test_rejects_notify_mistakes(self):
        self.assert_rejected(dict(MANIFEST, notify="oc_test"))

    def test_minimal_manifest_passes(self):
        manifest = {"id": "a-1", "summary": "说明", "notify": False}
        self.assertIs(fs.normalize_manifest(manifest), manifest)

    def test_append_text_is_idempotent_and_separator_aware(self):
        self.assertEqual(fs.append_text("旧；新验收", "；新验收"), "旧；新验收")
        self.assertEqual(fs.append_text("旧", "新", sep="\n"), "旧\n新")
        self.assertEqual(fs.append_text(None, "新"), "新")
        self.assertEqual(fs.append_text("旧", ""), "旧")
        self.assertEqual(fs.append_text(42, "新"), "新")

    def test_day_iso_validates_format(self):
        self.assertEqual(fs.day_iso("2026-10-28"), "2026-10-28T00:00:00+08:00")
        with self.assertRaises(fs.SyncError):
            fs.day_iso("2026/10/28")


class ConfigTest(SyncTestBase):
    def test_local_json_overrides_without_dropping_other_keys(self):
        cfg_path = self.write_config()
        (self.tmp / "local.json").write_text(
            json.dumps({"notify": {"chat_id": "oc_local"}}, ensure_ascii=False),
            encoding="utf-8")
        cfg = fs.load_config(cfg_path)
        self.assertEqual(cfg["notify"]["chat_id"], "oc_local")
        self.assertEqual(cfg["notify"]["mentions"], ["ou_test"], "深层合并要保留未覆盖的键")
        self.assertEqual(cfg["tables"]["tasks"], "tblTASKS")

    def test_local_json_must_be_object(self):
        cfg_path = self.write_config()
        (self.tmp / "local.json").write_text("[]", encoding="utf-8")
        with self.assertRaises(fs.SyncError):
            fs.load_config(cfg_path)

    def test_missing_required_keys_rejected(self):
        for broken in (
            {"tables": CONFIG["tables"]},
            {**CONFIG, "tables": {"tasks": "t"}},
            {**CONFIG, "tables": "tbl"},
            [],
        ):
            path = self.write_config(self.tmp / "broken.json", broken)
            with self.assertRaises(fs.SyncError):
                fs.load_config(path)

    def test_identity_defaults_to_user(self):
        cfg = {k: v for k, v in CONFIG.items() if k != "identity"}
        path = self.write_config(config=cfg)
        self.assertEqual(fs.load_config(path)["identity"], "user")

    def test_missing_config_file(self):
        with self.assertRaises(fs.SyncError):
            fs.load_config(self.tmp / "nope.json")


class PrePushTest(SyncTestBase):
    def test_refs_skip_deletes_and_malformed_lines(self):
        refs = fs.pre_push_refs(
            "refs/heads/main 1111111 refs/heads/main 2222222\n"
            "refs/heads/new 3333333 refs/heads/new 0000000\n"
            "(delete) 0000000 refs/heads/gone 4444444\n"
            "garbage line\n"
        )
        self.assertEqual(refs, [("1111111", "2222222"), ("3333333", "0000000")])

    def test_trailers_are_deduped_and_keep_order(self):
        message = ("feat: x\n\nFeishu-Change: feishu/changes/a.json\n"
                   "Feishu-Change: b.json\nFeishu-Change: feishu/changes/a.json\n")
        self.assertEqual(fs.extract_trailers(message),
                         ["feishu/changes/a.json", "b.json"])
        self.assertEqual(fs.extract_trailers("feat: x\n"), [])

    def test_manifest_path_guards(self):
        for bad in ("../outside.json", "/etc/passwd.json", "feishu/changes/x.txt"):
            with self.assertRaises(fs.SyncError):
                fs.resolve_manifest_path(bad)
        self.assertEqual(fs.resolve_manifest_path("feishu/change.example.json").name,
                         "change.example.json")

    def prepush(self, fake, *, message="Feishu-Change: changes/a.json\n",
                stdin="refs/heads/main 1 refs/heads/main 2\n"):
        with mock.patch.object(fs, "REPO", self.tmp), \
                mock.patch.object(fs, "git_log_messages", return_value=message):
            return fs.run_pre_push(stdin, config_path=self.write_config(),
                                   state_path=self.state_path, runner=fake,
                                   out=self.out.append, err=self.errors.append,
                                   today=TODAY)

    def test_no_trailers_is_a_noop(self):
        fake = FakeLark()
        self.assertEqual(self.prepush(fake, message="feat: x\n"), 0)
        self.assertEqual(fake.calls, [])

    def test_syncs_manifest_found_in_push(self):
        (self.tmp / "changes").mkdir()
        self.write_manifest(path=self.tmp / "changes" / "a.json")
        fake = FakeLark()
        self.assertEqual(self.prepush(fake), 0)
        self.assertTrue(fake.creates_for("tblCHANGES"))
        self.assertTrue(fs.load_state(self.state_path)["test-change"]["complete"])

    def test_missing_manifest_blocks_push(self):
        self.assertEqual(self.prepush(FakeLark()), 1)
        self.assertTrue(any("清单不存在" in e for e in self.errors), self.errors)

    def test_sync_failure_blocks_push(self):
        (self.tmp / "changes").mkdir()
        self.write_manifest(path=self.tmp / "changes" / "a.json")
        self.assertEqual(self.prepush(FakeLark(fail_create="tblCHANGES")), 1)
        self.assertTrue(any("同步失败" in e for e in self.errors), self.errors)


class LarkCommandTest(SyncTestBase):
    def test_env_override_forms(self):
        for raw, expected in (
            ("wsl.exe -e lark-cli", ["wsl.exe", "-e", "lark-cli"]),
            ('"C:\\Program Files\\lark-cli.exe" --profile x',
             ["C:\\Program Files\\lark-cli.exe", "--profile", "x"]),
            ("wsl.exe -e zsh -lc 'exec lark-cli \"$@\"' _",
             ["wsl.exe", "-e", "zsh", "-lc", 'exec lark-cli "$@"', "_"]),
        ):
            with mock.patch.dict("os.environ", {"FVP_LARK_CLI": raw}):
                self.assertEqual(fs.lark_command(), expected)

    def test_empty_override_rejected(self):
        with mock.patch.dict("os.environ", {"FVP_LARK_CLI": "   "}), \
                mock.patch("shutil.which", return_value=None):
            with self.assertRaises(fs.SyncError):
                fs.lark_command()

    def test_missing_binary_is_explained(self):
        with mock.patch.dict(os.environ, {}, clear=False), \
                mock.patch("shutil.which", return_value=None):
            os.environ.pop("FVP_LARK_CLI", None)
            with self.assertRaises(fs.SyncError) as ctx:
                fs.lark_command()
        self.assertIn("FVP_LARK_CLI", str(ctx.exception))

    def run_lark_with(self, **proc):
        fake_proc = SimpleNamespace(**{"returncode": 0, "stdout": "", "stderr": "",
                                       **proc})
        with mock.patch.object(fs, "lark_command", return_value=["lark"]), \
                mock.patch.object(subprocess, "run", return_value=fake_proc) as run:
            data = fs.run_lark(["base", "+record-list"])
        return data, run

    def test_run_lark_returns_parsed_json(self):
        data, run = self.run_lark_with(stdout='{"ok": true, "data": {}}')
        self.assertEqual(data["ok"], True)
        self.assertEqual(run.call_args[0][0], ["lark", "base", "+record-list"])

    def test_run_lark_reports_exit_code_and_detail(self):
        with self.assertRaises(fs.SyncError) as ctx:
            self.run_lark_with(returncode=7, stdout="", stderr="permission denied")
        self.assertIn("exit 7", str(ctx.exception))
        self.assertIn("permission denied", str(ctx.exception))

    def test_run_lark_rejects_non_json_output(self):
        with self.assertRaises(fs.SyncError) as ctx:
            self.run_lark_with(stdout="not json")
        self.assertIn("不是 JSON", str(ctx.exception))

    def test_run_lark_rejects_ok_false_envelope(self):
        with self.assertRaises(fs.SyncError) as ctx:
            self.run_lark_with(stdout='{"ok": false, "error": "nope"}')
        self.assertIn("返回失败", str(ctx.exception))

    def test_call_appends_identity_and_format(self):
        calls = []

        def runner(args):
            calls.append(list(args))
            return {"ok": True, "data": {}}

        lark = fs.Lark(CONFIG, runner=runner)
        lark.call(["base", "+record-list"])
        self.assertEqual(calls[0][-4:], ["--as", "user", "--format", "json"])

        lark.call(["base", "+record-batch-update", "--json", "{}"])
        self.assertNotIn("--format", calls[1])
        lark.send_markdown("oc_x", "hi", "key", identity="bot")
        self.assertEqual(calls[2][calls[2].index("--as") + 1], "bot")

    def test_get_record_maps_parallel_arrays(self):
        fake = FakeLark()
        lark = fs.Lark(CONFIG, runner=fake)
        self.assertEqual(lark.get_record("tasks", "recTASK"),
                         {"验收标准": "旧目标", "负责人备注": "旧备注"})

    def test_get_record_without_rows_is_an_error(self):
        lark = fs.Lark(CONFIG, runner=lambda args: {"ok": True, "data": {}})
        with self.assertRaises(fs.SyncError):
            lark.get_record("tasks", "recEmpty")


class MainTest(SyncTestBase):
    def invoke_main(self, argv, fake=None, stdin_text=""):
        fake = fake or FakeLark()
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(fs, "run_lark", fake), \
                mock.patch("sys.stdin", io.StringIO(stdin_text)), \
                mock.patch("sys.stdout", stdout), mock.patch("sys.stderr", stderr):
            code = fs.main(argv)
        return code, stdout.getvalue(), stderr.getvalue(), fake

    def test_without_manifest_returns_usage_error(self):
        code, _, err, _ = self.invoke_main([])
        self.assertEqual(code, 2)
        self.assertIn("--manifest", err)

    def test_syncs_given_manifest(self):
        cfg = self.write_config()
        mf = self.write_manifest()
        code, _, err, fake = self.invoke_main(
            ["--manifest", str(mf), "--config", str(cfg),
             "--state", str(self.state_path), "--no-notify"])
        self.assertEqual(code, 0, err)
        self.assertTrue(fake.creates_for("tblCHANGES"))
        self.assertFalse(fake.messages, "--no-notify 不该发消息")
        self.assertIs(fs.load_state(self.state_path)["test-change"]["complete"], True)

    def test_dry_run_does_not_write_state(self):
        cfg = self.write_config()
        mf = self.write_manifest()
        code, _, err, _ = self.invoke_main(
            ["--manifest", str(mf), "--config", str(cfg),
             "--state", str(self.state_path), "--dry-run"])
        self.assertEqual(code, 0, err)
        self.assertFalse(self.state_path.exists())

    def test_sync_error_returns_one(self):
        cfg = self.write_config()
        mf = self.write_manifest({"id": "bad id", "summary": "x", "notify": False})
        code, _, err, fake = self.invoke_main(
            ["--manifest", str(mf), "--config", str(cfg), "--state", str(self.state_path)])
        self.assertEqual(code, 1)
        self.assertIn("feishu-sync", err)
        self.assertFalse(fake.calls)

    def test_pre_push_mode_reads_stdin(self):
        (self.tmp / "changes").mkdir()
        self.write_manifest(path=self.tmp / "changes" / "a.json")
        cfg = self.write_config()
        with mock.patch.object(fs, "REPO", self.tmp), \
                mock.patch.object(fs, "git_log_messages",
                                  return_value="Feishu-Change: changes/a.json\n"):
            code, _, err, fake = self.invoke_main(
                ["--pre-push", "--config", str(cfg), "--state", str(self.state_path)],
                stdin_text="refs/heads/main 1 refs/heads/main 2\n")
        self.assertEqual(code, 0, err)
        self.assertTrue(fake.creates_for("tblCHANGES"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
