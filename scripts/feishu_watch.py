#!/usr/bin/env python3
"""盯飞书协作 Base：任务 / 测试 / 变更三个表一改，就逐行报出来。

飞书没有把「Base 记录变更」放进 lark-cli 的事件目录，所以这里用表级
`rev` 做闸门：每轮只拉 1 行读 rev，rev 没动就完全不拉全表。改动发生后
才拉全表做逐条 diff，报新增 / 修改 / 删除 / 列变化。

常用：
  python3 scripts/feishu_watch.py --once      # 看上次以来的变更（会话开始补看）
  python3 scripts/feishu_watch.py             # 常驻；变更逐行写 stdout
  python3 scripts/feishu_watch.py --interval 30

状态在 `.feishu-sync/watch-state.json`（gitignore）。首次运行只建基线，
不会把现有几十行当成「新增」刷屏。

只调用本机 lark-cli 的登录态；不是每条消息都推事件，所以别把它当审计日志。
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any, Callable, Optional

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

import feishu_sync as fs  # noqa: E402

DEFAULT_STATE = REPO / ".feishu-sync" / "watch-state.json"
DEFAULT_INTERVAL = 20
PAGE_SIZE = 200          # lark-cli 单次 record-list 上限
LABEL_FIELDS = {
    "tasks": ("任务", "用例", "功能"),
    "tests": ("用例", "任务", "功能"),
    "changes": ("功能", "改了什么", "任务"),
}
TABLE_LABELS = {"tasks": "任务", "tests": "测试", "changes": "变更"}


def load_state(path: Path) -> dict:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def _norm(value: Any) -> Any:
    if isinstance(value, list):
        return [_norm(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _norm(v) for k, v in value.items()}
    return value


def _same(a: Any, b: Any) -> bool:
    return json.dumps(a, ensure_ascii=False, sort_keys=True) == \
        json.dumps(b, ensure_ascii=False, sort_keys=True)


def _short(value: Any, width: int = 28) -> str:
    if value is None or value == "" or value == []:
        return "空"
    if isinstance(value, list):
        text = "、".join(str(v) for v in value)
    elif isinstance(value, dict):
        text = json.dumps(value, ensure_ascii=False)
    else:
        text = str(value)
    text = " ".join(text.split())
    return text if len(text) <= width else text[:width - 1] + "…"


def record_label(key: str, fields: dict, record_id: str) -> str:
    for name in LABEL_FIELDS.get(key, ()):
        value = fields.get(name)
        if value not in (None, "", []):
            return _short(value, 40)
    for value in fields.values():
        if isinstance(value, str) and value.strip():
            return _short(value, 40)
    return record_id


def diff_snapshot(key: str, old: dict, new: dict) -> list[str]:
    label = TABLE_LABELS.get(key, key)
    lines: list[str] = []

    old_fields = old.get("fields") or []
    new_fields = new.get("fields") or []
    if old_fields != new_fields:
        added = [f for f in new_fields if f not in old_fields]
        removed = [f for f in old_fields if f not in new_fields]
        parts = []
        if added:
            parts.append("新增列 " + "、".join(added))
        if removed:
            parts.append("删除列 " + "、".join(removed))
        if not parts:                      # 只换了顺序
            parts.append("列顺序变了")
        lines.append(f"[结构] {label} · " + "；".join(parts))

    old_rows = old.get("records") or {}
    new_rows = new.get("records") or {}
    # 列被删/新增时逐行对比会刷一屏，交给上面的「结构」行；
    # 这里只比两版都存在的列，避免把整列消失报成每行都改了。
    common_fields = [f for f in new_fields if f in old_fields]

    for rid in [r for r in new_rows if r not in old_rows]:
        row = new_rows[rid]
        lines.append(f"[新增] {label} · {record_label(key, row, rid)}")

    for rid in [r for r in old_rows if r not in new_rows]:
        row = old_rows[rid]
        lines.append(f"[删除] {label} · {record_label(key, row, rid)}（{rid}）")

    for rid in [r for r in new_rows if r in old_rows]:
        before, after = old_rows[rid], new_rows[rid]
        changed = [name for name in common_fields
                   if not _same(before.get(name), after.get(name))]
        if not changed:
            continue
        changed.sort(key=lambda name: (name not in LABEL_FIELDS.get(key, ()), name))
        detail = "；".join(
            f"{name}: {_short(before.get(name))} → {_short(after.get(name))}"
            for name in changed[:3]
        )
        if len(changed) > 3:
            detail += f"；等 {len(changed)} 项"
        lines.append(f"[修改] {label} · {record_label(key, after, rid)} · {detail}")

    return lines


class Watcher:
    def __init__(self, config: dict, *, state_path: Path = DEFAULT_STATE,
                 runner: Optional[Callable[[list[str]], dict]] = None,
                 out=None, err=None,
                 tables: Optional[list[str]] = None):
        self.config = config
        self.state_path = Path(state_path)
        self.state = load_state(self.state_path)
        self.lark = fs.Lark(config, runner)
        self.out = out or (lambda text: print(text, flush=True))
        self.err = err or (lambda text: print(text, file=sys.stderr, flush=True))
        self.tables = tables or ["tasks", "tests", "changes"]
        for key in self.tables:
            if key not in self.lark.tables:
                raise fs.SyncError(f"config.tables 里没有 {key}")

    def _list(self, table: str, limit: int, offset: int = 0) -> dict:
        args = ["base", "+record-list",
                "--base-token", self.lark.base_token,
                "--table-id", self.lark.tables[table],
                "--limit", str(limit)]
        if offset:
            args += ["--offset", str(offset)]
        return self.lark.call(args).get("data") or {}

    def read_rev(self, table: str) -> Optional[int]:
        data = self._list(table, 1)
        rev = data.get("rev")
        return rev if isinstance(rev, int) else None

    def read_snapshot(self, table: str) -> dict:
        fields: list[str] = []
        records: dict[str, dict] = {}
        offset = 0
        while True:
            data = self._list(table, PAGE_SIZE, offset)
            fields = data.get("fields") or fields
            ids = data.get("record_id_list") or []
            rows = data.get("data") or []
            for rid, row in zip(ids, rows):
                records[str(rid)] = dict(zip(fields, (_norm(v) for v in row)))
            if not data.get("has_more") or not ids:
                break
            offset += len(ids)
        return {"fields": list(fields), "records": records}

    def poll_once(self) -> int:
        emitted = 0
        state_tables = self.state.setdefault("tables", {})
        for key in self.tables:
            rev = self.read_rev(key)
            stored = state_tables.get(key) or {}

            if rev is not None and stored.get("rev") == rev and "snapshot" in stored:
                continue

            snapshot = self.read_snapshot(key)
            if "snapshot" not in stored:
                state_tables[key] = {"rev": rev, "snapshot": snapshot}
                self.err(f"[watch] {TABLE_LABELS.get(key, key)} 建基线："
                         f"{len(snapshot['records'])} 行")
                continue

            for line in diff_snapshot(key, stored["snapshot"], snapshot):
                self.out(line)
                emitted += 1
            if rev is not None:
                state_tables[key] = {"rev": rev, "snapshot": snapshot}
            else:
                state_tables[key] = {"snapshot": snapshot}

        save_state(self.state_path, self.state)
        return emitted


def run_once(watcher: Watcher) -> int:
    try:
        watcher.poll_once()
    except fs.SyncError as exc:
        watcher.err(f"[watch] {exc}")
        return 1
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="盯飞书协作 Base 的变更")
    parser.add_argument("--once", action="store_true", help="只跑一轮，打印上次以来的变更")
    parser.add_argument("--interval", type=float, default=None, help="轮询间隔秒，默认 20")
    parser.add_argument("--config", default=str(fs.DEFAULT_CONFIG))
    parser.add_argument("--state", default=str(DEFAULT_STATE))
    parser.add_argument("--tables", default="tasks,tests,changes",
                        help="逗号分隔：tasks,tests,changes")
    args = parser.parse_args(argv)

    config = fs.load_config(Path(args.config))
    tables = [t.strip() for t in args.tables.split(",") if t.strip()]
    interval = args.interval or (config.get("watch") or {}).get("interval_s") or DEFAULT_INTERVAL

    watcher = Watcher(config, state_path=Path(args.state), tables=tables)

    if args.once:
        return run_once(watcher)

    def _stop(_signum, _frame):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _stop)
    try:
        while True:
            run_once(watcher)
            time.sleep(max(1.0, float(interval)))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
