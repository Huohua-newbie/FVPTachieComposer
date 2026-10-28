#!/usr/bin/env python3
"""把 `.githooks/pre-push` 装到当前仓库的 `.git/hooks/pre-push`。

不碰 `git config`：只复制文件。已有同名钩子会先备份成
`pre-push.bak-<时间>`，不会直接覆盖。
"""
from __future__ import annotations

import shutil
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SOURCE = REPO / ".githooks" / "pre-push"
DEST = REPO / ".git" / "hooks" / "pre-push"


def main() -> int:
    if not SOURCE.exists():
        print(f"找不到 {SOURCE}", file=sys.stderr)
        return 1
    if not (REPO / ".git").exists():
        print(f"{REPO} 不是 git 仓库根目录", file=sys.stderr)
        return 1
    DEST.parent.mkdir(parents=True, exist_ok=True)
    if DEST.exists() and DEST.read_bytes() != SOURCE.read_bytes():
        backup = DEST.with_name(f"pre-push.bak-{datetime.now():%Y%m%d-%H%M%S}")
        shutil.copy2(DEST, backup)
        print(f"已有 pre-push 已备份：{backup}")
    shutil.copy2(SOURCE, DEST)
    DEST.chmod(0o755)
    print(f"已安装：{DEST}")
    print("之后推送带 Feishu-Change: <清单> 的提交时会先同步飞书，失败则拦截推送。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
