"""异常日志落盘的回归测试。

覆盖：日志目录/filename 规则（版本号 + 启动时刻）、启动头信息、
未捕获异常（主线程 / 子线程 / asyncio 任务）落盘、flet 事件回调异常落盘、
旧日志清理、目录不可写时的降级、版本号与 pyproject.toml 一致。

Linux / Windows 都能直接跑：python tests/test_crashlog.py
"""
import asyncio
import logging
import os
import re
import sys
import tempfile
import threading
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import fvpapp.crashlog as cl  # noqa: E402

fails = []


def check(name, ok):
    print(("PASS " if ok else "FAIL ") + name)
    if not ok:
        fails.append(name)


def reset_logging():
    """拆掉当前的文件 handler，让下一次 setup_logging 重新开一个。"""
    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, "_fvp_file", False):
            root.removeHandler(h)
            try:
                h.close()
            except Exception:
                pass
    if cl._fault_stream is not None:
        try:
            cl._fault_stream.close()
        except Exception:
            pass
    cl._log_path = None
    cl._file_handler = None
    cl._fault_stream = None


def read_all(path):
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def main():
    # resolve()：Windows 的 TEMP 常是 8.3 短名（ADMINI~1），而 app_root()
    # 返回长路径，不先展开会因为大小写不同的同一路径判成不相等。
    tmp = Path(tempfile.mkdtemp(prefix="fvp-crashlog-")).resolve()

    # ── 1. 版本号只有一处真源 ────────────────────────────
    pyproject = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'^version\s*=\s*"([^"]+)"', pyproject, re.M)
    check("pyproject.toml 里有 version", m is not None)
    check("fvpapp/version.py 与 pyproject.toml 版本一致",
          m is not None and m.group(1) == cl.app_version())

    # ── 2. 目录规则：日志跟着应用根目录走 ────────────────
    old_local = os.environ.get("LOCALAPPDATA")
    old_dir = os.environ.pop("FVP_LOG_DIR", None)
    real_app_root = cl.app_root
    try:
        os.environ["LOCALAPPDATA"] = str(tmp / "AppData" / "Local")
        check("默认落在应用根目录的 logs 下",
              cl.logs_dir() == cl.app_root() / "logs")
        # flet 打包态：内嵌解释器不设 sys.frozen，代码树在 <exe>\app\fvpapp 下
        real_exe, real_pkg = sys.executable, cl._PKG_DIR
        try:
            pkg = tmp / "FVP" / "app" / "fvpapp"
            pkg.mkdir(parents=True)
            sys.executable = str(tmp / "FVP" / "FVPTachieComposer.exe")
            cl._PKG_DIR = pkg.resolve()
            check("flet 打包态认 exe 所在目录", cl.app_root() == tmp / "FVP")
        finally:
            sys.executable = real_exe
            cl._PKG_DIR = real_pkg
        # 根目录写不进去（装进 Program Files、只读介质）才退回 LOCALAPPDATA
        blocker = tmp / "not-a-dir"
        blocker.write_text("", encoding="utf-8")
        cl.app_root = lambda: blocker
        check("根目录不可写时退回 %LOCALAPPDATA%",
              cl.logs_dir() == tmp / "AppData" / "Local" / "FVPTachieComposer" / "logs")
        os.environ["FVP_LOG_DIR"] = str(tmp / "override")
        check("FVP_LOG_DIR 可覆盖", cl.logs_dir() == tmp / "override")
    finally:
        cl.app_root = real_app_root
        os.environ.pop("FVP_LOG_DIR", None)
        if old_local is None:
            os.environ.pop("LOCALAPPDATA", None)
        else:
            os.environ["LOCALAPPDATA"] = old_local
        if old_dir is not None:
            os.environ["FVP_LOG_DIR"] = old_dir

    # ── 3. 落盘：文件名带版本号与时间戳 ──────────────────
    reset_logging()
    log_dir = tmp / "logs"
    path = cl.setup_logging(log_dir)
    check("日志文件已创建", path is not None and path.is_file())
    name_ok = path is not None and re.fullmatch(
        r"fvptachie-%s-\d{8}-\d{6}(-\d+)?\.log" % re.escape(cl.app_version()),
        path.name,
    )
    check("文件名含版本号与时间戳", bool(name_ok))
    check("setup_logging 幂等", cl.setup_logging(log_dir) == path)

    text = read_all(path)
    check("启动头信息有版本号", cl.app_version() in text)
    check("启动头信息有 Python 版本", sys.version.split()[0] in text)
    check("启动头信息有日志文件自身路径", str(path) in text)

    # ── 4. 我们自己的 debug 也要进文件（排查"被吞的异常"）─
    log = logging.getLogger("fvptachie")
    log.debug("静默调试行")
    check("应用 logger 的 DEBUG 落盘", "静默调试行" in read_all(path))

    # ── 5. flet 事件回调异常走它自己的 logger，也要收进来 ──
    flet_log = logging.getLogger("flet.messaging.session")
    try:
        raise RuntimeError("回调里炸了")
    except RuntimeError:
        flet_log.error("Unhandled error in 'on_%s' handler", "click", exc_info=True)
    text = read_all(path)
    check("flet 回调异常落盘", "Unhandled error in 'on_click' handler" in text)
    check("flet 回调异常带完整栈", "回调里炸了" in text and "Traceback" in text)

    # ── 6. 主线程未捕获异常：落盘 + 原钩子仍然被调用 ──────
    hit = []

    def fake_prev(*args):
        hit.append(args)

    real = sys.excepthook
    sys.excepthook = fake_prev
    cl._hooked = False  # 允许再包一层
    cl.install_hooks()
    try:
        try:
            raise ValueError("主线程炸了")
        except ValueError:
            sys.excepthook(*sys.exc_info())
        text = read_all(path)
        check("主线程未捕获异常落盘", "主线程炸了" in text)
        check("主线程异常留了 CRITICAL 标记", "主线程未捕获异常" in text)
        check("原 excepthook 仍被调用（不改原提示）", len(hit) == 1)
    finally:
        sys.excepthook = real

    # ── 7. 子线程异常（缩略图线程池那条路）──────────────
    def boom():
        raise RuntimeError("线程里炸了")

    t = threading.Thread(target=boom, name="thumb-0")
    t.start()
    t.join()
    text = read_all(path)
    check("子线程异常落盘", "线程里炸了" in text)
    check("子线程异常记了线程名", "thumb-0" in text)

    # ── 8. asyncio 任务异常 ─────────────────────────────
    async def async_main():
        loop = asyncio.get_running_loop()
        cl.install_asyncio_handler(loop)

        async def bad():
            raise RuntimeError("任务里炸了")

        task = asyncio.create_task(bad())
        await asyncio.sleep(0.05)
        # 任务没人 await 时，异常要等 Task 被回收才交给 loop 的异常钩子
        del task
        import gc
        gc.collect()
        await asyncio.sleep(0.05)
    
    asyncio.run(async_main())
    text = read_all(path)
    check("asyncio 任务异常落盘", "任务里炸了" in text)

    # ── 9. 只留最近 N 个 ────────────────────────────────
    prune_dir = tmp / "prune"
    prune_dir.mkdir()
    for i in range(5):
        p = prune_dir / f"fvptachie-2.2.3-2026010{i + 1}-000000.log"
        p.write_text("x", encoding="utf-8")
        os.utime(p, (1_700_000_000 + i, 1_700_000_000 + i))
    cl._prune(prune_dir, 2)
    left = sorted(p.name for p in prune_dir.glob("fvptachie-*.log"))
    check("旧日志被清理，只留最近 2 个",
          left == ["fvptachie-2.2.3-20260104-000000.log",
                   "fvptachie-2.2.3-20260105-000000.log"])

    # ── 10. 目录不可写：降级成只有控制台，不抛 ──────────
    reset_logging()
    blocker = tmp / "not-a-dir"
    blocker.write_text("", encoding="utf-8")
    try:
        got = cl.setup_logging(blocker / "logs")
    except Exception as exc:  # 绝不能往上抛
        got = f"raised {exc!r}"
    check("目录不可写时返回 None 而不是抛异常", got is None)

    # 恢复一个能用的，避免影响后续（同进程内的其它测试）输出
    reset_logging()
    cl.setup_logging(tmp / "after")
    check("降级后还能重新装上", cl.current_log_path() is not None)

    if fails:
        print("\nFAILED:", fails)
        sys.exit(f"CRASHLOG_TEST_FAILED: {fails}")
    print("CRASHLOG_OK")


main()
