"""飞书 Base 变更监视的回归测试（不联网、不碰真飞书）。

用假的 lark-cli runner 验证：
- 首次运行只建基线，不把已有行当新增刷屏
- rev 没动时不拉全表；rev 动了才逐条 diff
- 新增 / 修改 / 删除 / 列变化各报一行，修改带旧值→新值
- 状态落盘后续跑不重复报，坏状态自动重建基线
- 取数失败不崩，--once 返回非零

python tests/test_feishu_watch.py
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
import feishu_watch as fw  # noqa: E402

fails = []


def check(name, ok):
    print(("PASS " if ok else "FAIL ") + name)
    if not ok:
        fails.append(name)


CONFIG = {
    "base_token": "base_test",
    "identity": "user",
    "tables": {"tasks": "tblTASKS", "tests": "tblTESTS", "changes": "tblCHANGES"},
}


class Fake:
    def __init__(self):
        self.rev = {"tblTASKS": 1, "tblTESTS": 1, "tblCHANGES": 1}
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
        self.calls = []

    def bump(self, table):
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
            "fields": list(fields), "record_id_list": ids, "data": data,
            "has_more": offset + limit < len(self.rows[table]),
            "rev": self.rev[table],
        }}

    def full_fetches(self):
        return sum(1 for _t, limit, _o in self.calls if limit == fw.PAGE_SIZE)


class Recorder:
    def __init__(self):
        self.lines = []
        self.errors = []

    def out(self, text):
        self.lines.append(text)

    def err(self, text):
        self.errors.append(text)


def make_watcher(tmp: Path, fake, state="state.json"):
    rec = Recorder()
    watcher = fw.Watcher(CONFIG, state_path=tmp / state, runner=fake,
                         out=rec.out, err=rec.err)
    return watcher, rec


tmp = Path(tempfile.mkdtemp(prefix="fvp_watch_"))
fake = Fake()
watcher, rec = make_watcher(tmp, fake)

watcher.poll_once()
check("首次运行不报新增", rec.lines == [])
check("首次运行给三张表建基线", len(rec.errors) == 3 and
      all("建基线" in e for e in rec.errors))
state = json.loads((tmp / "state.json").read_text(encoding="utf-8"))
check("状态记下 rev", state["tables"]["tests"]["rev"] == 1)
check("状态记下快照", state["tables"]["tests"]["snapshot"]["records"]["recT1"]["用例"]
      == "1.2 最大化往返")
check("基线拉过全表", fake.full_fetches() == 3)
check("没有残留 .tmp", not list(tmp.glob("*.tmp")))

# rev 没动：只读 rev，不拉全表
rec.lines.clear()
calls_before = len(fake.calls)
watcher.poll_once()
check("rev 没动不发事件", rec.lines == [])
check("rev 没动不拉全表", fake.full_fetches() == 3)
check("rev 没动仍逐表查一次 rev", len(fake.calls) - calls_before == 3)

# 修改一行
fake.set("tblTESTS", "recT1", {"结果": ["通过"]})
fake.bump("tblTESTS")
rec.lines.clear()
watcher.poll_once()
check("修改报一行", len(rec.lines) == 1)
line = rec.lines[0] if rec.lines else ""
check("修改标明表与用例", "[修改] 测试 · 1.2 最大化往返" in line)
check("修改带旧值→新值", "结果: 未跑 → 通过" in line)
check("只拉了改动的那张表", fake.full_fetches() == 4)

# 同一份状态换进程续跑：不重复报
watcher2, rec2 = make_watcher(tmp, fake)
watcher2.poll_once()
check("续跑不重复报", rec2.lines == [])

# 新增一行
fake.add("tblTESTS", "recT2", {"用例": "1.8 新用例", "结果": ["未跑"], "备注": ""})
rec.lines.clear()
watcher.poll_once()
check("新增报一行", rec.lines == ["[新增] 测试 · 1.8 新用例"])

# 删除一行
fake.remove("tblTESTS", "recT2")
rec.lines.clear()
watcher.poll_once()
check("删除报一行", len(rec.lines) == 1)
check("删除带用例名", "[删除] 测试 · 1.8 新用例" in rec.lines[0])

# 改表头：只报结构，不把整列变化刷成每行都改了
fake.fields["tblTESTS"].append("负责人")
fake.bump("tblTESTS")
rec.lines.clear()
watcher.poll_once()
check("新增列只报一行结构", rec.lines == ["[结构] 测试 · 新增列 负责人"])

# 删列同理
fake.fields["tblTESTS"].remove("负责人")
fake.bump("tblTESTS")
rec.lines.clear()
watcher.poll_once()
check("删除列只报一行结构", rec.lines == ["[结构] 测试 · 删除列 负责人"])

# 多字段修改只列前三项并标总数
fake.set("tblTASKS", "recK1", {"任务": "卡①改名", "状态": ["已完成"]})
fake.bump("tblTASKS")
rec.lines.clear()
watcher.poll_once()
check("多字段修改只一行", len(rec.lines) == 1)
check("多字段修改按行报", "任务: 卡① → 卡①改名" in rec.lines[0]
      and "状态: 进行中 → 已完成" in rec.lines[0])

# 坏状态文件：重建基线，不崩
(tmp / "bad.json").write_text("{", encoding="utf-8")
watcher3, rec3 = make_watcher(tmp, fake, state="bad.json")
watcher3.poll_once()
check("坏状态不崩", rec3.lines == [])
check("坏状态被重写", "snapshot" in json.loads((tmp / "bad.json").read_text(encoding="utf-8"))
      .get("tables", {}).get("tests", {}))


class Boom:
    def __call__(self, _args):
        raise fs.SyncError("读表失败")


rec4 = Recorder()
bad = fw.Watcher(CONFIG, state_path=tmp / "boom.json", runner=Boom(),
                 out=rec4.out, err=rec4.err)
check("取数失败 --once 返回非零", fw.run_once(bad) == 1)
check("取数失败有错误输出", any("读表失败" in e for e in rec4.errors))

if fails:
    sys.exit(f"FEISHU_WATCH FAILED: {fails}")
print("FEISHU_WATCH_OK")
