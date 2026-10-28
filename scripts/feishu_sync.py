#!/usr/bin/env python3
"""把一份需求变更清单同步到飞书 Base，并在群里发一条变更通知。

清单长什么样、什么时候跑，见 `feishu/README.md`；最小例子见
`feishu/change.example.json`。脚本只调用本机 `lark-cli` 的登录态，
不读取、不保存任何 token；`feishu/config.json` 里只有 Base/表/群 ID。

常用：
  python scripts/feishu_sync.py --manifest feishu/changes/xxx.json --dry-run
  python scripts/feishu_sync.py --manifest feishu/changes/xxx.json
  python scripts/feishu_sync.py --pre-push          # 由 .githooks/pre-push 调用

幂等：每个清单第一次成功同步后记在 `.feishu-sync/state.json`
（gitignore），重复推送不会再建记录、再发消息；中途失败下次从断点续。
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Optional

REPO = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = REPO / "feishu" / "config.json"
DEFAULT_STATE = REPO / ".feishu-sync" / "state.json"

DEFAULT_FIELDS = {
    "task_acceptance": "验收标准",
    "task_note": "负责人备注",
    "test_result": "结果",
    "test_note": "备注",
    "change_date": "日期",
    "change_link": "关联",
    "change_version": "版本",
}

MANIFEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,60}$")
TRAILER_RE = re.compile(r"^Feishu-Change:[ \t]*(\S+)[ \t]*$", re.MULTILINE)
ZERO_SHA = re.compile(r"^0+$")

Runner = Callable[[list[str]], dict]


class SyncError(RuntimeError):
    """清单不合法、lark-cli 失败或状态写不进去；调用方负责打印/阻断推送。"""


# ── 文件读写 ────────────────────────────────────────────────────────────
def load_json(path: Path) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SyncError(f"找不到文件: {path}")
    except (OSError, ValueError) as exc:
        raise SyncError(f"读不了 {path}: {exc}")


def atomic_write_json(path: Path, data: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                       encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        raise SyncError(f"状态写不进去 {path}: {exc}")


def deep_merge(base: dict, extra: dict) -> dict:
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def load_config(path: Path = DEFAULT_CONFIG) -> dict:
    cfg = load_json(path)
    if not isinstance(cfg, dict):
        raise SyncError("config 顶层必须是对象")
    # 同目录的 local.json（gitignore）覆盖本机差异：chat_id、提及名单等
    local = Path(path).with_name("local.json")
    if local.exists():
        override = load_json(local)
        if not isinstance(override, dict):
            raise SyncError("local.json 顶层必须是对象")
        cfg = deep_merge(cfg, override)
    if not cfg.get("base_token") or not isinstance(cfg.get("tables"), dict):
        raise SyncError("config 缺少 base_token / tables")
    for key in ("tasks", "tests", "changes"):
        if not cfg["tables"].get(key):
            raise SyncError(f"config.tables 缺少 {key}")
    cfg.setdefault("identity", "user")
    return cfg


# ── 清单校验 ────────────────────────────────────────────────────────────
def _want_str(value: Any, name: str, *, empty: bool = False) -> None:
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise SyncError(f"{name} 必须是非空字符串")


def _want_list(value: Any, name: str) -> None:
    if value is None:
        return
    if not isinstance(value, list):
        raise SyncError(f"{name} 必须是数组")


def normalize_manifest(raw: Any) -> dict:
    if not isinstance(raw, dict):
        raise SyncError("清单顶层必须是对象")
    mid = raw.get("id")
    _want_str(mid, "id")
    if not MANIFEST_ID_RE.match(mid):
        raise SyncError("id 只能包含字母、数字、点、下划线、短横线，且不超过 60 字符")
    _want_str(raw.get("summary"), "summary")

    task = raw.get("task")
    if task is not None:
        if not isinstance(task, dict):
            raise SyncError("task 必须是对象")
        _want_str(task.get("record_id"), "task.record_id")
        if task.get("acceptance_set") is not None and task.get("acceptance_append") is not None:
            raise SyncError("task 里 acceptance_set 与 acceptance_append 只能二选一")
        for key in ("acceptance_set", "acceptance_append", "note_append"):
            if task.get(key) is not None:
                _want_str(task[key], f"task.{key}")
        if task.get("fields") is not None and not isinstance(task["fields"], dict):
            raise SyncError("task.fields 必须是对象")

    tests = raw.get("tests")
    if tests is not None:
        if not isinstance(tests, dict):
            raise SyncError("tests 必须是对象")
        _want_list(tests.get("block"), "tests.block")
        _want_list(tests.get("create"), "tests.create")
        seen = set()
        for idx, item in enumerate(tests.get("block") or []):
            if not isinstance(item, dict):
                raise SyncError(f"tests.block[{idx}] 必须是对象")
            _want_str(item.get("record_id"), f"tests.block[{idx}].record_id")
            if item["record_id"] in seen:
                raise SyncError(f"tests.block 里 {item['record_id']} 出现了两次")
            seen.add(item["record_id"])
            if item.get("fields") is not None and not isinstance(item["fields"], dict):
                raise SyncError(f"tests.block[{idx}].fields 必须是对象")
            for key in ("note_append",):
                if item.get(key) is not None:
                    _want_str(item[key], f"tests.block[{idx}].{key}")
        for idx, item in enumerate(tests.get("create") or []):
            if not isinstance(item, dict) or not item:
                raise SyncError(f"tests.create[{idx}] 必须是非空对象")

    change = raw.get("change")
    if change is not None and not isinstance(change, dict):
        raise SyncError("change 必须是对象")

    notify = raw.get("notify")
    if notify is not None and not isinstance(notify, (dict, bool)):
        raise SyncError("notify 必须是对象或 false")
    if not any(k in raw for k in ("task", "tests", "change", "notify")):
        raise SyncError("清单里没有任何要同步的内容（task/tests/change/notify）")
    return raw


# ── 小工具 ──────────────────────────────────────────────────────────────
def append_text(old: Any, extra: str, *, sep: str = "") -> str:
    old_text = old if isinstance(old, str) else ""
    if not extra:
        return old_text
    if not old_text:
        return extra
    if extra in old_text:
        return old_text
    if sep:
        return old_text.rstrip() + sep + extra
    return old_text + extra


def day_iso(today: Optional[str] = None) -> str:
    day = today or dt.date.today().isoformat()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", day):
        raise SyncError(f"today 必须是 YYYY-MM-DD: {day!r}")
    return f"{day}T00:00:00+08:00"


def short_commit() -> str:
    try:
        proc = subprocess.run(["git", "-C", str(REPO), "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True, encoding="utf-8")
        if proc.returncode == 0:
            return proc.stdout.strip()
    except OSError:
        pass
    return ""


def idempotency_key(manifest_id: str, salt: str = "") -> str:
    """同一份清单重跑不发第二条；清单内容改过（salt 变）则允许补发新文案。"""
    raw = manifest_id if not salt else f"{manifest_id}\n{salt}"
    return "fvp-" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


# ── lark-cli 封装 ───────────────────────────────────────────────────────
def run_lark(args: list[str]) -> dict:
    exe = shutil.which("lark-cli")
    if not exe:
        raise SyncError("找不到 lark-cli（先按 lark-shared 的说明登录）")
    proc = subprocess.run([exe, *args], capture_output=True, text=True, encoding="utf-8")
    if proc.returncode != 0:
        detail = (proc.stdout or proc.stderr).strip().replace("\n", " ")[:500]
        raise SyncError(f"lark-cli 失败 (exit {proc.returncode}): {detail}")
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        raise SyncError(f"lark-cli 输出不是 JSON: {proc.stdout[:300]}")
    if not isinstance(data, dict) or not data.get("ok"):
        raise SyncError(f"lark-cli 返回失败: {json.dumps(data, ensure_ascii=False)[:500]}")
    return data


class Lark:
    def __init__(self, config: dict, runner: Optional[Runner] = None):
        self.base_token = config["base_token"]
        self.tables = config["tables"]
        self.identity = config.get("identity") or "user"
        self.fields = {**DEFAULT_FIELDS, **(config.get("fields") or {})}
        self.notify = config.get("notify") or {}
        self._run = runner or run_lark

    def call(self, args: list[str]) -> dict:
        full = [*args, "--as", self.identity]
        if "--json" not in args:
            full += ["--format", "json"]
        return self._run(full)

    def get_record(self, table: str, record_id: str) -> dict:
        resp = self.call(["base", "+record-get",
                          "--base-token", self.base_token,
                          "--table-id", self.tables[table],
                          "--record-id", record_id])
        data = resp.get("data") or {}
        rows = data.get("data") or []
        names = data.get("fields") or []
        if not rows:
            raise SyncError(f"读不到记录 {record_id}（表 {self.tables[table]}）")
        return dict(zip(names, rows[0]))

    def batch_update(self, table: str, updates: dict) -> None:
        self.call(["base", "+record-batch-update",
                   "--base-token", self.base_token,
                   "--table-id", self.tables[table],
                   "--json", json.dumps({"update_records": updates}, ensure_ascii=False)])

    def batch_create(self, table: str, records: list[dict]) -> list[str]:
        resp = self.call(["base", "+record-batch-create",
                          "--base-token", self.base_token,
                          "--table-id", self.tables[table],
                          "--json", json.dumps({"create_records": records}, ensure_ascii=False)])
        return list((resp.get("data") or {}).get("record_id_list") or [])

    def send_markdown(self, chat_id: str, text: str, key: str) -> str:
        resp = self.call(["im", "+messages-send",
                          "--chat-id", chat_id,
                          "--markdown", text,
                          "--idempotency-key", key])
        return str((resp.get("data") or {}).get("message_id") or "")


# ── 变更通知文案 ────────────────────────────────────────────────────────
def _labels(tests: dict, key: str) -> list[str]:
    out = []
    for item in tests.get(key) or []:
        label = item.get("用例") or item.get("record_id") or ""
        if label:
            out.append(str(label))
    return out


def render_message(manifest: dict, commit: str) -> str:
    notify = manifest.get("notify") if isinstance(manifest.get("notify"), dict) else {}
    mentions = notify.get("mentions")
    lines = []
    if mentions:
        lines.append(" ".join(f'<at user_id="{uid}"></at>' for uid in mentions))
        lines.append("")
    lines.append(f"**需求变更 · {manifest['summary']}**")
    lines.append("")
    lines.append(f"**旧目标**：{manifest.get('old_goal') or '（见变更记录）'}")
    lines.append(f"**新目标**：{manifest.get('new_goal') or '（见变更记录）'}")

    affected = []
    task = manifest.get("task")
    if task:
        affected.append(f"任务卡 `{task.get('name') or task['record_id']}` 验收口径已更新")
    tests = manifest.get("tests") or {}
    blocked = _labels(tests, "block")
    if blocked:
        affected.append("暂停/作废：" + "、".join(f"「{x}」" for x in blocked))
    created = _labels(tests, "create")
    if created:
        affected.append("新增用例：" + "、".join(f"「{x}」" for x in created))
    change = manifest.get("change")
    if isinstance(change, dict) and change.get("改了什么"):
        affected.append(f"变更记录：{change['改了什么']}")
    if affected:
        lines.append("")
        lines.append("**受影响**")
        lines.extend(f"- {x}" for x in affected)

    lines.append("")
    ref = " / ".join(x for x in (commit, manifest.get("pr")) if x) or "本次提交"
    lines.append(f"**从 `{ref}` 起以新目标为准**，旧目标下的产出不用继续。")
    return "\n".join(lines)


# ── 同步主体 ────────────────────────────────────────────────────────────
def _plan(out, label: str, payload: Any) -> None:
    out(f"[plan] {label}: {json.dumps(payload, ensure_ascii=False)}")


def _apply_task(lark: Lark, manifest: dict, dry: bool, out) -> bool:
    task = manifest.get("task")
    if not task:
        return False
    rid = task["record_id"]
    cur = lark.get_record("tasks", rid)
    fields = lark.fields
    acc_key, note_key = fields["task_acceptance"], fields["task_note"]
    changes: dict = {}
    for key, value in (task.get("fields") or {}).items():
        if cur.get(key) != value:
            changes[key] = value
    if task.get("acceptance_set") is not None:
        if cur.get(acc_key) != task["acceptance_set"]:
            changes[acc_key] = task["acceptance_set"]
    elif task.get("acceptance_append"):
        merged = append_text(cur.get(acc_key), task["acceptance_append"])
        if merged != cur.get(acc_key):
            changes[acc_key] = merged
    if task.get("note_append"):
        merged = append_text(cur.get(note_key), task["note_append"], sep="\n")
        if merged != cur.get(note_key):
            changes[note_key] = merged
    if not changes:
        out(f"[feishu-sync] 任务卡 {rid} 无需改动")
        return False
    if dry:
        _plan(out, f"更新任务卡 {rid}", changes)
    else:
        lark.batch_update("tasks", {rid: changes})
        out(f"[feishu-sync] 任务卡已更新 {rid}")
    return True


def _apply_blocked_tests(lark: Lark, manifest: dict, dry: bool, out) -> bool:
    items = (manifest.get("tests") or {}).get("block") or []
    if not items:
        return False
    note_key = lark.fields["test_note"]
    result_key = lark.fields["test_result"]
    updates: dict = {}
    for item in items:
        rid = item["record_id"]
        cur = lark.get_record("tests", rid)
        changes = {k: v for k, v in (item.get("fields") or {}).items() if cur.get(k) != v}
        if item.get("note_append"):
            merged = append_text(cur.get(note_key), item["note_append"], sep="\n")
            if merged != cur.get(note_key):
                changes[note_key] = merged
        # 没显式写结果的行默认标成阻塞，避免「备注写了暂停、结果还是未跑」
        if result_key not in changes:
            changes[result_key] = ["阻塞"]
        updates[rid] = changes
    if dry:
        _plan(out, "更新测试行", updates)
    else:
        lark.batch_update("tests", updates)
        out(f"[feishu-sync] 已标记 {len(updates)} 条测试为阻塞/暂停")
    return True


def _create_tests(lark: Lark, manifest: dict, dry: bool, out) -> bool:
    records = (manifest.get("tests") or {}).get("create") or []
    if not records:
        return False
    if dry:
        _plan(out, "新增测试行", records)
    else:
        ids = lark.batch_create("tests", records)
        out(f"[feishu-sync] 已新增 {len(ids)} 条测试行")
    return True


def _create_change(lark: Lark, manifest: dict, commit: str, today: Optional[str],
                   dry: bool, out) -> bool:
    raw = manifest.get("change")
    if not raw:
        return False
    change = dict(raw)
    fields = lark.fields
    change.setdefault(fields["change_date"], day_iso(today))
    change.setdefault(fields["change_version"], "待定")
    link = manifest.get("link") or " / ".join(
        x for x in (commit, manifest.get("pr")) if x)
    if link:
        change.setdefault(fields["change_link"], link)
    if dry:
        _plan(out, "新增变更记录", change)
    else:
        ids = lark.batch_create("changes", [change])
        out(f"[feishu-sync] 已写变更记录 {ids[0] if ids else '(无 id)'}")
    return True


def _notify(lark: Lark, manifest: dict, commit: str, dry: bool, out) -> bool:
    notify = manifest.get("notify")
    if notify is False:
        return False
    notify = notify if isinstance(notify, dict) else {}
    if notify.get("enabled") is False:
        return False
    chat_id = notify.get("chat_id") or lark.notify.get("chat_id")
    if not chat_id:
        raise SyncError("notify 已启用但 config/manifest 里都没有 chat_id")
    if "mentions" not in notify and lark.notify.get("mentions") is not None:
        manifest = {**manifest,
                    "notify": {**notify, "mentions": lark.notify["mentions"]}}
    text = notify.get("text") or render_message(manifest, commit)
    if dry:
        _plan(out, f"群通知 {chat_id}", text)
    else:
        salt = json.dumps(manifest, sort_keys=True, ensure_ascii=False)
        msg_id = lark.send_markdown(chat_id, text, idempotency_key(manifest["id"], salt))
        out(f"[feishu-sync] 已发群通知 {msg_id or '(无 message_id)'}")
    return True


def sync_manifest(manifest_path: Path, *, config_path: Path = DEFAULT_CONFIG,
                  state_path: Path = DEFAULT_STATE, dry_run: bool = False,
                  force: bool = False, notify: bool = True,
                  runner: Optional[Runner] = None, out=print,
                  today: Optional[str] = None) -> dict:
    config = load_config(config_path)
    manifest = normalize_manifest(load_json(manifest_path))
    mid = manifest["id"]
    commit = manifest.get("commit") or short_commit()

    state = load_state(state_path)
    entry = state.get(mid) or {"done": {}}
    done = entry.setdefault("done", {})
    if entry.get("complete") and not force:
        out(f"[feishu-sync] {mid} 已同步过，跳过（--force 可重跑）")
        return entry

    lark = Lark(config, runner=runner)

    def step(name: str, action) -> None:
        if done.get(name) and not force:
            out(f"[feishu-sync] {mid}.{name} 已完成，跳过")
            return
        changed = action()
        if changed:
            done[name] = True
            entry["at"] = dt.datetime.now().isoformat(timespec="seconds")
            state[mid] = entry
            if not dry_run:
                atomic_write_json(state_path, state)

    try:
        step("task", lambda: _apply_task(lark, manifest, dry_run, out))
        step("tests_block", lambda: _apply_blocked_tests(lark, manifest, dry_run, out))
        step("tests_create", lambda: _create_tests(lark, manifest, dry_run, out))
        step("change", lambda: _create_change(lark, manifest, commit, today, dry_run, out))
        if notify:
            step("notify", lambda: _notify(lark, manifest, commit, dry_run, out))
        else:
            out("[feishu-sync] --no-notify：跳过群通知")
    except Exception:
        # 已完成的步骤留在 state 里，下次从断点继续
        if not dry_run:
            state[mid] = entry
            atomic_write_json(state_path, state)
        raise

    entry["complete"] = True
    entry["at"] = dt.datetime.now().isoformat(timespec="seconds")
    state[mid] = entry
    if not dry_run:
        atomic_write_json(state_path, state)
        out(f"[feishu-sync] {mid} 同步完成")
    return entry


# ── 状态 / pre-push ─────────────────────────────────────────────────────
def load_state(path: Path = DEFAULT_STATE) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def pre_push_refs(stdin_text: str) -> list[tuple[str, str]]:
    """pre-push 的标准输入：<local ref> <local sha> <remote ref> <remote sha>。"""
    refs = []
    for line in stdin_text.splitlines():
        parts = line.split()
        if len(parts) != 4:
            continue
        _local_ref, local_sha, _remote_ref, remote_sha = parts
        if not local_sha or ZERO_SHA.match(local_sha):
            continue
        refs.append((local_sha, remote_sha))
    return refs


def resolve_manifest_path(rel: str) -> Path:
    """trailer 里的路径只允许仓库内的 .json，挡住 ../ 和绝对路径。"""
    path = Path(rel)
    if path.is_absolute() or ".." in path.parts or path.suffix.lower() != ".json":
        raise SyncError(f"清单路径不合法（只允许仓库内的 .json）: {rel}")
    full = (REPO / path).resolve()
    if not full.is_relative_to(REPO):
        raise SyncError(f"清单跑出仓库了: {rel}")
    return full


def extract_trailers(message: str) -> list[str]:
    seen, out = set(), []
    for path in TRAILER_RE.findall(message or ""):
        if path not in seen:
            seen.add(path)
            out.append(path)
    return out


def git_log_messages(local_sha: str, remote_sha: str) -> str:
    if remote_sha and not ZERO_SHA.match(remote_sha):
        args = ["git", "-C", str(REPO), "log", "--format=%B", f"{remote_sha}..{local_sha}"]
    else:
        args = ["git", "-C", str(REPO), "log", "--format=%B", local_sha,
                "--not", "--remotes"]
    proc = subprocess.run(args, capture_output=True, text=True, encoding="utf-8")
    if proc.returncode != 0:
        if remote_sha and not ZERO_SHA.match(remote_sha):
            raise SyncError(f"git log 失败: {proc.stderr.strip()[:300]}")
        fallback = ["git", "-C", str(REPO), "log", "--format=%B", "-1", local_sha]
        proc = subprocess.run(fallback, capture_output=True, text=True, encoding="utf-8")
        if proc.returncode != 0:
            raise SyncError(f"git log 失败: {proc.stderr.strip()[:300]}")
    return proc.stdout


def run_pre_push(stdin_text: str, *, config_path: Path = DEFAULT_CONFIG,
                 state_path: Path = DEFAULT_STATE, runner: Optional[Runner] = None,
                 out=print, err=print, today: Optional[str] = None) -> int:
    paths: list[str] = []
    seen = set()
    for local_sha, remote_sha in pre_push_refs(stdin_text):
        for path in extract_trailers(git_log_messages(local_sha, remote_sha)):
            if path not in seen:
                seen.add(path)
                paths.append(path)
    if not paths:
        out("[feishu-sync] 待推送提交里没有 Feishu-Change 清单，跳过")
        return 0

    failures = 0
    for rel in paths:
        try:
            path = resolve_manifest_path(rel)
            if not path.exists():
                raise SyncError(f"清单不存在: {rel}（推送的提交里要带着它）")
            out(f"[feishu-sync] 同步 {rel}")
            sync_manifest(path, config_path=config_path, state_path=state_path,
                          runner=runner, out=out, today=today)
        except SyncError as exc:
            failures += 1
            err(f"[feishu-sync] 同步失败 {rel}: {exc}")
    if failures:
        err(f"[feishu-sync] {failures} 份清单没同步成功，推送已拦下。"
            "修好后重推；确需离线推送可设 FVP_FEISHU_SYNC=off 后手动补同步。")
        return 1
    return 0


# ── CLI ─────────────────────────────────────────────────────────────────
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="把需求变更清单同步到飞书 Base")
    parser.add_argument("--manifest", type=Path, action="append", default=[],
                        help="清单路径，可重复")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--dry-run", action="store_true",
                        help="只读当前记录并打印将要执行的写入，不落库、不写状态")
    parser.add_argument("--force", action="store_true",
                        help="忽略已同步状态重跑（可能重复建记录，慎用）")
    parser.add_argument("--no-notify", action="store_true", help="不发群消息")
    parser.add_argument("--pre-push", action="store_true",
                        help="读 pre-push 的标准输入，同步待推送提交里引用的清单")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.pre_push:
            return run_pre_push(sys.stdin.read(), config_path=args.config,
                                state_path=args.state, today=None)
        if not args.manifest:
            print("没有 --manifest；用 --pre-push 或至少给一份清单", file=sys.stderr)
            return 2
        for path in args.manifest:
            sync_manifest(path, config_path=args.config, state_path=args.state,
                          dry_run=args.dry_run, force=args.force,
                          notify=not args.no_notify)
        return 0
    except SyncError as exc:
        print(f"[feishu-sync] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
