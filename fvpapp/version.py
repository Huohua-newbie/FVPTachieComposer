"""版本号与产品名的**唯一**来源。

打包链只在 `pyproject.toml` 里读版本（`flutter --build-name`），但那个文件不会
跟着可执行文件走，运行时读不到。所以这里再写一份常量，由
`tests/test_crashlog.py` 断言两边一致——发版时忘改任何一边，测试会当场失败。

发版要改的地方见 `RELEASING.md`：`pyproject.toml` + 这里。
"""

from __future__ import annotations

APP_NAME = "FVPTachieComposer"

# 与 pyproject.toml 的 [project].version 保持一致（test_crashlog.py 守着）
__version__ = "2.2.3"

# 日志文件名里用它，避免出现 Windows 不允许的字符
__all__ = ["APP_NAME", "__version__"]
