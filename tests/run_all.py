"""全套自检入口：每个测试文件起一个独立子进程跑。

为什么分进程：假 flet（宽容 _Any 模块）与真 flet 共用同一批全局状态
（sys.modules、应用单例、缓存目录环境变量），同进程混跑会互相污染；
分进程顺带保证一个文件崩了不拖累其他文件。真 flet 的 UI 冒烟只在装好
flet 的机器（Windows 打包机）上过——缺 flet 时它是**失败**，不是跳过。

用法：
    python tests/run_all.py            # 逐个跑，末尾汇总，非零退出即有文件未过
    python tests/test_cache.py         # 单个文件仍可独立跑，入口不变
    python -m unittest test_cache.ByteLRUTest -v   # 单类跑法（在 tests/ 目录里）
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
FLET_TESTS = {"test_ui_smoke.py"}  # 需要真 flet + Pillow 的文件


def run_one(path: Path) -> int:
    env = dict(os.environ)
    # UI 冒烟要建真控件：Windows 上给 _custom_client_available 一个明确答案，
    # 省得拖放门控随 cwd 里有没有 build/windows 而漂。
    if path.name in FLET_TESTS and os.name == "nt":
        env.setdefault("FLET_PLATFORM", "windows")
    return subprocess.run([sys.executable, str(path)], cwd=str(REPO), env=env).returncode


def main() -> int:
    failed = []
    for path in sorted(HERE.glob("test_*.py")):
        print(f"\n===== {path.name} " + "=" * (56 - len(path.name)), flush=True)
        code = run_one(path)
        if code:
            failed.append(path.name)
    print("\n" + "=" * 62, flush=True)
    if failed:
        print(f"FAILED: {len(failed)} 个文件未过: {', '.join(failed)}")
        return 1
    print("OK: 全部测试文件通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
