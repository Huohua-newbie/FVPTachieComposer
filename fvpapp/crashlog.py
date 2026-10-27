"""崩溃与诊断日志落盘。

用户报错时只要让他把当次的日志文件发过来：

    Windows（打包态）：%LOCALAPPDATA%\\FVPTachieComposer\\logs\\
    开发环境（非 Windows）：~/.local/state/FVPTachieComposer/logs/

文件名 = 版本号 + 启动时刻，例如 `fvptachie-2.2.3-20261027-153012.log`，
一次运行一个文件，天然对得上「用户说的是哪一版、哪一次」。只保留最近
`KEEP_RUNS` 次，不会无限长。

捕获范围（都往同一个文件里写）：

- **logging 的一切**：flet 抓到的事件回调异常走
  `flet.messaging.session` 的 `logger.error(exc_info=True)`，会被一起收进来；
  我们自己 `log.debug` 吞掉的预期外异常也能在文件里看到栈。
- **主线程未捕获异常**：`sys.excepthook`（包装原钩子——PyInstaller 的
  崩溃弹窗、控制台 traceback 都还在，界面表现不变）。
- **后台线程异常**：`threading.excepthook`，缩略图线程池就在这儿。
- **asyncio 任务未捕获异常**：`loop.set_exception_handler`。
- **原生崩溃**：`faulthandler`，段错误 / 栈溢出时把 Python 栈写进同一个文件。

原则：**只留证据，不改用户可见行为**。界面上原本的提示条、错误文案、
flet 的崩溃页一律保持原样；写日志本身失败也绝对不往上抛，最多退化成只有
控制台输出（旧行为）。
"""

from __future__ import annotations

import atexit
import faulthandler
import logging
import os
import platform
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from .version import APP_NAME, __version__

log = logging.getLogger("fvptachie")

# 保留最近多少次运行的日志（一次运行一个文件）
KEEP_RUNS = 20

_CONSOLE_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_FILE_FORMAT = "%(asctime)s %(levelname)-7s [%(threadName)s] %(name)s: %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# 应用自己的 logger 名（各模块统一用这个前缀）
APP_LOGGER = "fvptachie"

_log_path: Optional[Path] = None
_file_handler: Optional[logging.FileHandler] = None
_fault_stream: Optional[Any] = None
_hooked = False
_atexit_done = False


# ── 路径 ──────────────────────────────────────────────────────────────
def logs_dir() -> Path:
    """日志目录。`FVP_LOG_DIR` 可覆盖（测试与排障用）。"""
    override = os.environ.get("FVP_LOG_DIR")
    if override:
        return Path(override).expanduser()
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local) / APP_NAME / "logs"
    if os.name == "nt":  # LOCALAPPDATA 被清掉的极端情况
        return Path.home() / "AppData" / "Local" / APP_NAME / "logs"
    # 开发环境（WSL/Linux/macOS）
    state = os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local" / "state")
    return Path(state) / APP_NAME / "logs"


def app_version() -> str:
    return __version__


def current_log_path() -> Optional[Path]:
    """本次运行的日志文件；没落盘（被禁用或目录不可写）时是 None。"""
    return _log_path


def _new_log_path() -> Path:
    d = logs_dir()
    d.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    stem = f"fvptachie-{__version__}-{stamp}"
    path = d / f"{stem}.log"
    n = 1
    while path.exists():  # 同一秒里连开两次（测试/双击连点）
        path = d / f"{stem}-{n}.log"
        n += 1
    return path


def _prune(d: Path, keep: int) -> None:
    """只留最近 keep 个日志文件；删失败无所谓。"""
    try:
        files = [p for p in d.glob("fvptachie-*.log") if p.is_file()]
    except OSError:
        return
    if len(files) <= keep:
        return
    files.sort(key=lambda p: p.stat().st_mtime)  # 按修改时间，跨版本也排得对
    for old in files[: len(files) - keep]:
        try:
            old.unlink()
        except OSError:
            pass


# ── 装配 ──────────────────────────────────────────────────────────────
def _console_level() -> int:
    name = os.environ.get("FVP_LOG_LEVEL") or (
        "DEBUG" if os.environ.get("FVP_DEBUG") else "WARNING"
    )
    return getattr(logging, name.upper(), logging.WARNING)


def _setup_console(level: int) -> None:
    """控制台保持旧行为：打包态没有控制台（stderr 为 None）就干脆不装。"""
    root = logging.getLogger()
    for h in root.handlers:
        if getattr(h, "_fvp_console", False):
            h.setLevel(level)
            return
    if sys.stderr is None:
        return
    handler = logging.StreamHandler()
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter(_CONSOLE_FORMAT, _DATE_FORMAT))
    handler._fvp_console = True  # type: ignore[attr-defined]
    root.addHandler(handler)


def setup_logging(log_dir: Optional[Path] = None) -> Optional[Path]:
    """装好文件日志并写启动头信息，返回日志路径。

    幂等；任何一步失败都退化成只有控制台输出，绝不抛异常。
    """
    global _log_path, _file_handler

    if _log_path is not None:
        return _log_path
    if os.environ.get("FVP_NO_FILE_LOG"):
        return None

    console_level = _console_level()
    _setup_console(console_level)

    # 根 logger 维持 WARNING：不让 flet 的 debug 刷屏（它每帧都在打）
    root = logging.getLogger()
    root.setLevel(min(console_level, logging.WARNING))
    # 只把自己家的 logger 放开到 DEBUG，好让「被吞的异常」在文件里留全栈。
    # 传播到 root 的 handler 时不再过 root 的 level，所以文件拿得到 debug。
    logging.getLogger(APP_LOGGER).setLevel(logging.DEBUG)

    try:
        path = _new_log_path() if log_dir is None else _make_path(log_dir)
        handler = logging.FileHandler(path, mode="a", encoding="utf-8",
                                     errors="backslashreplace", delay=True)
        handler.setLevel(logging.NOTSET)  # 事件本身该不该记由 logger 决定
        handler.setFormatter(logging.Formatter(_FILE_FORMAT, _DATE_FORMAT))
        handler._fvp_file = True  # type: ignore[attr-defined]
        root.addHandler(handler)
        _log_path, _file_handler = path, handler
    except Exception:
        _log_path, _file_handler = None, None
        log.debug("日志目录不可写，仅保留控制台输出", exc_info=True)
        return None

    _prune(path.parent, KEEP_RUNS)
    _enable_faulthandler(path)
    log.info("%s", _banner(path))
    return path


def _make_path(log_dir: Path) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    stem = f"fvptachie-{__version__}-{stamp}"
    path = log_dir / f"{stem}.log"
    n = 1
    while path.exists():
        path = log_dir / f"{stem}-{n}.log"
        n += 1
    return path


def _enable_faulthandler(path: Path) -> None:
    """原生崩溃（段错误 / 栈溢出）也能在同一个文件里留下 Python 栈。"""
    global _fault_stream
    try:
        _fault_stream = open(path, "a", encoding="utf-8",
                             errors="backslashreplace", buffering=1)
        faulthandler.enable(file=_fault_stream, all_threads=True)
    except Exception:
        _fault_stream = None
        log.debug("faulthandler 未能启用", exc_info=True)


def _banner(path: Path) -> str:
    frozen = getattr(sys, "frozen", False)
    lines = [
        "=" * 62,
        f" {APP_NAME} {__version__}  启动于 {datetime.now():%Y-%m-%d %H:%M:%S}",
        "=" * 62,
        f"版本      : {__version__}",
        f"Python    : {sys.version.split()[0]}  {platform.machine()}",
        f"打包态    : {'是' if frozen else '否（源码运行）'}",
        f"平台      : {platform.platform()}",
        f"工作目录  : {os.getcwd()}",
        f"命令行    : {sys.argv}",
        f"日志文件  : {path}",
        "-" * 62,
    ]
    return "\n".join(lines)


# ── 钩子 ──────────────────────────────────────────────────────────────
def install_hooks() -> None:
    """接管未捕获异常，落盘后**继续调用原钩子**（界面/系统提示保持原样）。"""
    global _hooked
    if _hooked:
        return
    _hooked = True

    prev_excepthook = sys.excepthook

    def _excepthook(exc_type, exc, tb):
        if not issubclass(exc_type, (KeyboardInterrupt, SystemExit)):
            _safe_log("主线程未捕获异常", (exc_type, exc, tb))
        try:
            prev_excepthook(exc_type, exc, tb)
        except Exception:
            pass

    sys.excepthook = _excepthook

    prev_thread_hook = getattr(threading, "excepthook", None)

    def _thread_hook(args):
        if not issubclass(args.exc_type, (KeyboardInterrupt, SystemExit)):
            _safe_log(
                f"线程未捕获异常: {getattr(args.thread, 'name', '?')}",
                (args.exc_type, args.exc_value, args.exc_traceback),
            )
        if prev_thread_hook is not None:
            try:
                prev_thread_hook(args)
            except Exception:
                pass

    threading.excepthook = _thread_hook  # type: ignore[assignment]

    prev_unraisable = getattr(sys, "unraisablehook", None)

    def _unraisable(args):
        try:
            log.error("无法抛出的异常（__del__/析构里）: %s", args.err_msg,
                      exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
        except Exception:
            pass
        if prev_unraisable is not None:
            try:
                prev_unraisable(args)
            except Exception:
                pass

    if prev_unraisable is not None:
        sys.unraisablehook = _unraisable  # type: ignore[assignment]

    _atexit_marker()


def install_asyncio_handler(loop: Any) -> None:
    """asyncio 里没人 await 的 task 抛异常时，默认只往 stderr 打一行。"""
    if loop is None:
        return
    try:
        prev = loop.get_exception_handler()

        def _handler(loop_, context):
            exc = context.get("exception")
            msg = context.get("message") or "asyncio 任务未捕获异常"
            try:
                if exc is not None:
                    log.error("%s", msg, exc_info=(type(exc), exc, exc.__traceback__))
                else:
                    log.error("%s: %s", msg, context)
            except Exception:
                pass
            if prev is not None:
                try:
                    prev(loop_, context)
                except Exception:
                    pass

        loop.set_exception_handler(_handler)
    except Exception:
        log.debug("asyncio 异常钩子未装上", exc_info=True)


def _safe_log(message: str, exc_info: Any) -> None:
    try:
        log.critical("%s", message, exc_info=exc_info)
        _flush()
    except Exception:
        pass


def _flush() -> None:
    try:
        if _file_handler is not None:
            _file_handler.flush()
    except Exception:
        pass


def _atexit_marker() -> None:
    """正常退出也留一行，用户发日志时能区分「崩了」和「关了」。"""
    global _atexit_done
    if _atexit_done:
        return
    _atexit_done = True

    def _bye():
        try:
            log.info("正常退出")
            for h in logging.getLogger().handlers:
                try:
                    h.flush()
                except Exception:
                    pass
        except Exception:
            pass
        try:
            faulthandler.disable()
        except Exception:
            pass
        try:
            if _fault_stream is not None:
                _fault_stream.close()
        except Exception:
            pass

    atexit.register(_bye)


def install(log_dir: Optional[Path] = None) -> Optional[Path]:
    """一步到位：装文件日志 + 装钩子。入口模块只调这一个。"""
    path = setup_logging(log_dir)
    install_hooks()
    return path
