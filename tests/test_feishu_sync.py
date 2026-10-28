"""需求变更同步脚本的回归测试（不联网、不碰真飞书）。

用假的 lark-cli runner 验证：
- 追加验收 / 备注不会覆盖旧内容，旧测试行默认标阻塞
- 测试行、变更记录、群消息的字段与文案
- 清单同步成功后幂等；中途失败后从断点续
- dry-run 只读不写；清单校验能挡住拼错的字段
- pre-push 的 ref / Feishu-Change trailer 解析

python tests/test_feishu_sync.py
"""
import json
import sys
import tempfile
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import feishu_sync as fs  # noqa: E402

fails = []


def check(name, ok):
    print(("PASS " if ok else "FAIL ") + name)
    if not ok:
        fails.append(name)


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


class Fake:
    def __init__(self, fail_on=None):
        self.calls = []
        self.fail_on = fail_on
        self.updates = []
        self.creates = []
        self.messages = []
        self.records = {
            ("tblTASKS", "recTASK"): {"验收标准": "旧目标", "负责人备注": "旧备注"},
            ("tblTESTS", "recTEST"): {"备注": "旧备注", "结果": ["未跑"]},
        }

    def __call__(self, args):
        self.calls.append(args)
        table = args[args.index("--table-id") + 1] if "--table-id" in args else None
        if "+record-get" in args:
            rid = args[args.index("--record-id") + 1]
            rec = self.records[(table, rid)]
            return {"ok": True, "data": {"data": [list(rec.values())],
                                         "fields": list(rec.keys())}}
        if "+record-batch-update" in args:
            payload = json.loads(args[args.index("--json") + 1])
            self.updates.append((table, payload["update_records"]))
            return {"ok": True, "data": {"record_id_list": list(payload["update_records"])}}
        if "+record-batch-create" in args:
            if self.fail_on == "changes_create" and table == "tblCHANGES":
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
        raise AssertionError(f"未预期的 lark-cli 调用: {args}")


def write_fixture(tmp: Path, manifest=None):
    cfg = tmp / "config.json"
    cfg.write_text(json.dumps(CONFIG, ensure_ascii=False), encoding="utf-8")
    mf = tmp / "change.json"
    mf.write_text(json.dumps(manifest or MANIFEST, ensure_ascii=False), encoding="utf-8")
    return cfg, mf


def run_sync(tmp: Path, fake, *, dry=False, state=None):
    cfg, mf = write_fixture(tmp)
    state = state or (tmp / "state.json")
    return fs.sync_manifest(mf, config_path=cfg, state_path=state, dry_run=dry,
                            runner=fake, out=lambda *_: None, today="2026-10-28")


# ── 完整同步 ────────────────────────────────────────────────────────────
tmp = Path(tempfile.mkdtemp(prefix="fvp_feishu_sync_"))
fake = Fake()
entry = run_sync(tmp, fake)
check("同步完成后标记 complete", entry.get("complete") is True)

task_updates = [p for t, p in fake.updates if t == "tblTASKS"]
check("任务卡只更新一次", len(task_updates) == 1)
task = task_updates[0].get("recTASK", {})
check("验收标准是追加不是覆盖", task.get("验收标准") == "旧目标；新验收")
check("负责人备注保留旧内容", task.get("负责人备注") == "旧备注\nkitsuneMori｜2026-10-28：补")

test_updates = [p for t, p in fake.updates if t == "tblTESTS"]
check("旧测试行被标阻塞", (test_updates[0].get("recTEST") or {}).get("结果") == ["阻塞"])
check("旧测试行备注追加原因",
      (test_updates[0].get("recTEST") or {}).get("备注") == "旧备注\n目标已变更，暂停")

test_creates = [r for t, r in fake.creates if t == "tblTESTS"]
check("新增测试行只建一次", len(test_creates) == 1 and test_creates[0][0]["用例"] == "1.6 新用例")

change_creates = [r for t, r in fake.creates if t == "tblCHANGES"]
change = change_creates[0][0]
check("变更记录自动补日期", change.get("日期") == "2026-10-28T00:00:00+08:00")
check("变更记录自动补版本", change.get("版本") == "待定")
check("变更记录自动补关联", change.get("关联") == "abc1234")
check("变更记录保留正文", change.get("改了什么") == "改了目标")

check("群消息只发一条", len(fake.messages) == 1)
check("群消息默认用 bot 身份", fake.messages[0]["as"] == "bot")
check("读表仍用 user 身份", all(c[c.index("--as") + 1] == "user" for c in fake.calls
                                  if "+messages-send" not in c))
text = fake.messages[0]["text"]
check("群消息 @ 了配置里的默认人", '<at user_id="ou_test"></at>' in text)
check("群消息带旧目标与新目标", "旧目标" in text and "新目标" in text)
check("群消息带 commit 与新用例", "abc1234" in text and "1.6 新用例" in text)
check("幂等键稳定且有前缀", fake.messages[0]["key"].startswith("fvp-")
      and len(fake.messages[0]["key"]) == 20)
check("同清单幂等键稳定", fs.idempotency_key("x", "a") == fs.idempotency_key("x", "a"))
check("清单改过则换幂等键", fs.idempotency_key("x", "a") != fs.idempotency_key("x", "b"))

# 再跑一次：没有任何 lark-cli 调用
fake2 = Fake()
run_sync(tmp, fake2, state=tmp / "state.json")
check("重复同步直接跳过", fake2.calls == [])

# ── dry-run：只读不写 ───────────────────────────────────────────────────
tmp_dry = Path(tempfile.mkdtemp(prefix="fvp_feishu_dry_"))
fake_dry = Fake()
run_sync(tmp_dry, fake_dry, dry=True, state=tmp_dry / "state.json")
check("dry-run 读了当前记录", any("+record-get" in c for c in fake_dry.calls))
check("dry-run 不写任何记录", not fake_dry.updates and not fake_dry.creates)
check("dry-run 不发消息", not fake_dry.messages)
check("dry-run 不写状态", not (tmp_dry / "state.json").exists())

# ── 中途失败：断点续，不重复改已完成步骤 ─────────────────────────────────
tmp_fail = Path(tempfile.mkdtemp(prefix="fvp_feishu_fail_"))
state_path = tmp_fail / "state.json"
cfg_f, mf_f = write_fixture(tmp_fail)
try:
    fs.sync_manifest(mf_f, config_path=cfg_f, state_path=state_path,
                     runner=Fake(fail_on="changes_create"), out=lambda *_: None,
                     today="2026-10-28")
    failed = False
except fs.SyncError:
    failed = True
check("变更表写失败时抛错（推送会被拦）", failed)
state = fs.load_state(state_path)
check("失败的清单没有标记 complete", not state.get("test-change", {}).get("complete"))
check("已完成步骤留在状态里",
      all(state["test-change"]["done"].get(k) for k in ("task", "tests_block", "tests_create")))

fake_resume = Fake()
fs.sync_manifest(mf_f, config_path=cfg_f, state_path=state_path,
                 runner=fake_resume, out=lambda *_: None, today="2026-10-28")
check("续跑不再读任务卡/测试行",
      not any("+record-get" in c for c in fake_resume.calls))
check("续跑只补变更记录与通知",
      len([r for t, r in fake_resume.creates if t == "tblCHANGES"]) == 1
      and len(fake_resume.messages) == 1)
check("续跑后标记 complete", fs.load_state(state_path)["test-change"]["complete"] is True)

# ── 清单校验 ────────────────────────────────────────────────────────────
def expect_bad(name, manifest):
    try:
        fs.normalize_manifest(manifest)
        ok = False
    except fs.SyncError:
        ok = True
    check(name, ok)


bad = dict(MANIFEST, id="有中文/空格")
expect_bad("非法 id 被拒绝", bad)
bad = dict(MANIFEST, task={"record_id": "r", "acceptance_set": "x", "acceptance_append": "y"})
expect_bad("验收 set/append 二选一", bad)
bad = dict(MANIFEST, tests={"block": [{"record_id": "r"}, {"record_id": "r"}]})
expect_bad("同一测试行不能 block 两次", bad)
bad = {k: v for k, v in MANIFEST.items() if k not in ("task", "tests", "change", "notify")}
expect_bad("没有任何同步内容被拒绝", bad)

# 追加是幂等的：原文里已经有这句就不重复加
check("重复追加不叠字", fs.append_text("旧；新验收", "；新验收") == "旧；新验收")

# ── pre-push 解析 ───────────────────────────────────────────────────────
refs = fs.pre_push_refs(
    "refs/heads/main 1111111 refs/heads/main 2222222\n"
    "refs/heads/new 3333333 refs/heads/new 0000000\n"
    "(delete) 0000000 refs/heads/gone 4444444\n"
)
check("pre-push 解析两个要推的 ref", refs == [("1111111", "2222222"), ("3333333", "0000000")])

msg = ("feat: x\n\nFeishu-Change: feishu/changes/a.json\n"
       "Feishu-Change: b.json\nFeishu-Change: feishu/changes/a.json\n")
check("trailer 去重并保序", fs.extract_trailers(msg) == ["feishu/changes/a.json", "b.json"])
check("没有 trailer 时为空", fs.extract_trailers("feat: x\n") == [])

for bad in ("../outside.json", "/etc/passwd.json", "feishu/changes/x.txt"):
    try:
        fs.resolve_manifest_path(bad)
        ok = False
    except fs.SyncError:
        ok = True
    check(f"非法清单路径被拒绝 {bad}", ok)
check("仓库内清单路径可用",
      fs.resolve_manifest_path("feishu/change.example.json").name == "change.example.json")

# lark-cli 启动命令可被环境变量覆盖（Windows 的 Agent 指向 WSL）
import os  # noqa: E402

old_lark = os.environ.get("FVP_LARK_CLI")
try:
    os.environ["FVP_LARK_CLI"] = "wsl.exe -e lark-cli"
    check("FVP_LARK_CLI 支持带参数", fs.lark_command() == ["wsl.exe", "-e", "lark-cli"])
    os.environ["FVP_LARK_CLI"] = '"C:\\Program Files\\lark-cli.exe" --profile x'
    check("FVP_LARK_CLI 支持带空格路径",
          fs.lark_command() == ["C:\\Program Files\\lark-cli.exe", "--profile", "x"])
    os.environ["FVP_LARK_CLI"] = "wsl.exe -e zsh -lc 'exec lark-cli \"$@\"' _"
    check("FVP_LARK_CLI 支持 zsh 转发壳",
          fs.lark_command() == ["wsl.exe", "-e", "zsh", "-lc",
                                'exec lark-cli "$@"', "_"])
finally:
    if old_lark is None:
        os.environ.pop("FVP_LARK_CLI", None)
    else:
        os.environ["FVP_LARK_CLI"] = old_lark

if fails:
    print("\nFAILED:", fails)
    sys.exit(f"FEISHU_SYNC_TEST_FAILED: {fails}")
print("FEISHU_SYNC_OK")
