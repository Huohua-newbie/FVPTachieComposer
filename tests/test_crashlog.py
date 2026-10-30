"""崩溃与诊断日志落盘的回归测试（stdlib unittest，不碰真用户的日志目录）。

覆盖：文件名/目录规则（版本号 + 启动时刻 + 打包态 + 回退）、启动头信息、
主线程 / 子线程 / asyncio / 析构异常落盘、原钩子仍被调用、旧日志清理、
目录不可写时的降级、`FVP_NO_FILE_LOG` 开关、真实 faulthandler 崩溃（子进程）。

每个用例一份临时目录；`support.CrashlogSandbox` 负责还原 logging handler 与
全部异常钩子，所以用例顺序无关、可单跑：

    python tests/test_crashlog.py -v
    cd tests && python -m unittest test_crashlog.HooksTest -v
"""

from __future__ import annotations

import gc
import logging
import os
import re
import subprocess
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

try:
    from . import support
except ImportError:  # 直接 python tests/test_crashlog.py
    import support

import fvpapp.crashlog as cl  # noqa: E402

LOG_NAME_RE = re.compile(
    r"fvptachie-%s-\d{8}-\d{6}(-\d+)?\.log" % re.escape(cl.app_version()))


class CrashlogTestBase(support.EnvTestCase):
    """临时目录 + 环境沙箱 + crashlog 全局状态沙箱。"""

    def setUp(self) -> None:  # noqa: N802
        super().setUp()
        # support 在导入期设了 FVP_NO_FILE_LOG，避免别的测试留 logs/；
        # 本文件要测落盘，这里摘掉。
        self.sandbox.pop("FVP_NO_FILE_LOG")
        self.sandbox.set(FVP_LOG_LEVEL="CRITICAL")  # 别让用例往测试输出刷日志
        self.addCleanup(support.CrashlogSandbox().start().restore)
        self.logger = logging.getLogger(cl.APP_LOGGER)
        self.root = logging.getLogger()

    # ── 小工具 ────────────────────────────────────────────────
    def file_handlers(self) -> list[logging.Handler]:
        return [h for h in self.root.handlers if getattr(h, "_fvp_file", False)]

    def start_log(self, log_dir: Path | None = None) -> Path:
        path = cl.setup_logging(log_dir if log_dir is not None else self.tmp / "logs")
        self.assertIsNotNone(path, "日志应落盘，实际返回 None")
        return path

    def read_log(self, path: Path) -> str:
        for h in self.root.handlers:
            try:
                h.flush()
            except Exception:
                pass
        return Path(path).read_text(encoding="utf-8", errors="replace")

    def pretend_packaged(self) -> None:
        """模拟 flet 打包态：内嵌解释器不设 sys.frozen，代码在 <exe>\\app\\fvpapp。"""
        pkg = self.tmp / "FVP" / "app" / "fvpapp"
        pkg.mkdir(parents=True)
        old_exe, old_pkg = sys.executable, cl._PKG_DIR
        sys.executable = str(self.tmp / "FVP" / "FVPTachieComposer.exe")
        cl._PKG_DIR = pkg.resolve()
        self.addCleanup(setattr, cl, "_PKG_DIR", old_pkg)
        self.addCleanup(setattr, sys, "executable", old_exe)

    def run_python(self, code: str, *args: str) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env.pop("FVP_NO_FILE_LOG", None)  # 子进程要真的写日志
        env["PYTHONPATH"] = os.pathsep.join(
            [str(support.REPO), env["PYTHONPATH"]] if env.get("PYTHONPATH")
            else [str(support.REPO)])
        return subprocess.run(
            [sys.executable, "-c", code, *args],
            capture_output=True, text=True, encoding="utf-8",
            env=env, cwd=str(support.REPO), timeout=60,
        )


class LogPathTest(CrashlogTestBase):
    def test_version_matches_pyproject(self):
        pyproject = (support.REPO / "pyproject.toml").read_text(encoding="utf-8")
        m = re.search(r'^version\s*=\s*"([^"]+)"', pyproject, re.M)
        self.assertIsNotNone(m, "pyproject.toml 里应有 version")
        self.assertEqual(m.group(1), cl.app_version())

    def test_source_run_is_not_packaged(self):
        self.assertFalse(cl.is_packaged())
        self.assertEqual(cl.app_root(), support.REPO)

    def test_default_dir_follows_app_root(self):
        self.sandbox.pop("FVP_LOG_DIR")  # 否则 for_test 的覆盖优先
        with mock.patch.object(cl, "app_root", lambda: self.tmp / "app"):
            self.assertEqual(cl.logs_dir(), self.tmp / "app" / "logs")
            self.assertTrue((self.tmp / "app" / "logs").is_dir(), "应创建日志目录")

    def test_packaged_root_is_exe_dir(self):
        self.sandbox.pop("FVP_LOG_DIR")
        self.pretend_packaged()
        self.assertTrue(cl.is_packaged())
        self.assertEqual(cl.app_root(), self.tmp / "FVP")
        self.sandbox.set(LOCALAPPDATA=self.tmp / "AppData" / "Local")
        self.assertEqual(cl.logs_dir(), self.tmp / "FVP" / "logs")

    def test_unwritable_root_falls_back_to_localappdata(self):
        blocker = self.tmp / "not-a-dir"
        blocker.write_text("", encoding="utf-8")
        self.sandbox.pop("FVP_LOG_DIR")
        local = self.tmp / "AppData" / "Local"
        self.sandbox.set(LOCALAPPDATA=local)
        with mock.patch.object(cl, "app_root", lambda: blocker):
            self.assertEqual(cl.logs_dir(), local / cl.APP_NAME / "logs")

    @unittest.skipIf(os.name == "nt", "Windows 走 %LOCALAPPDATA%，另有分支")
    def test_unwritable_root_falls_back_to_xdg(self):
        blocker = self.tmp / "not-a-dir"
        blocker.write_text("", encoding="utf-8")
        self.sandbox.pop("LOCALAPPDATA", "FVP_LOG_DIR")
        self.sandbox.set(XDG_STATE_HOME=self.tmp / "state")
        with mock.patch.object(cl, "app_root", lambda: blocker):
            self.assertEqual(cl.logs_dir(), self.tmp / "state" / cl.APP_NAME / "logs")

    def test_fvp_log_dir_overrides_everything(self):
        self.sandbox.set(FVP_LOG_DIR=self.tmp / "override")
        with mock.patch.object(cl, "app_root",
                                       lambda: self.tmp / "app"):
            self.assertEqual(cl.logs_dir(), self.tmp / "override")

    def test_fvp_log_dir_expands_home(self):
        self.sandbox.set(FVP_LOG_DIR="~/fvp-logs")
        self.assertEqual(cl.logs_dir(), Path.home() / "fvp-logs")


class SetupLoggingTest(CrashlogTestBase):
    def test_filename_and_banner(self):
        path = self.start_log()
        self.assertTrue(path.is_file())
        self.assertRegex(path.name, LOG_NAME_RE)
        text = self.read_log(path)
        self.assertIn(cl.app_version(), text)
        self.assertIn(sys.version.split()[0], text)
        self.assertIn(str(path), text)
        self.assertIn("启动于", text)

    def test_packaged_banner(self):
        self.pretend_packaged()
        text = self.read_log(self.start_log())
        self.assertIn("打包态    : 是", text)

    def test_idempotent(self):
        path = self.start_log()
        again = cl.setup_logging(self.tmp / "elsewhere")
        self.assertEqual(again, path)
        self.assertEqual(len(self.file_handlers()), 1)
        self.assertEqual(self.read_log(path).count("启动于"), 1)

    def test_same_second_gets_suffix(self):
        d = self.tmp / "same-second"
        first = cl._make_path(d)
        first.write_text("", encoding="utf-8")
        second = cl._make_path(d)
        self.assertNotEqual(first, second)
        self.assertRegex(second.name, r"-\d+\.log$")

    def test_custom_log_dir_is_used(self):
        path = self.start_log(self.tmp / "custom" / "nested")
        self.assertEqual(path.parent, self.tmp / "custom" / "nested")
        self.assertEqual(cl.current_log_path(), path)

    def test_default_dir_uses_fvp_log_dir(self):
        # support 的 for_test 把 FVP_LOG_DIR 指到 tmp/logs
        path = self.start_log(None)
        self.assertEqual(path.parent, self.tmp / "logs")

    def test_app_logger_debug_is_written(self):
        path = self.start_log()
        self.assertEqual(self.root.level, logging.WARNING)
        self.assertEqual(self.logger.level, logging.DEBUG)
        self.logger.debug("静默调试行")
        self.assertIn("静默调试行", self.read_log(path))

    def test_flet_callback_error_written(self):
        path = self.start_log()
        flet_log = logging.getLogger("flet.messaging.session")
        try:
            raise RuntimeError("回调里炸了")
        except RuntimeError:
            flet_log.error("Unhandled error in 'on_%s' handler", "click",
                           exc_info=True)
        text = self.read_log(path)
        self.assertIn("Unhandled error in 'on_click' handler", text)
        self.assertIn("回调里炸了", text)
        self.assertIn("Traceback", text)

    def test_no_file_log_env_disables_writing(self):
        self.sandbox.set(FVP_NO_FILE_LOG="1")
        self.assertIsNone(cl.setup_logging(self.tmp / "logs"))
        self.assertIsNone(cl.current_log_path())
        self.assertEqual(self.file_handlers(), [])
        self.assertFalse((self.tmp / "logs").exists())

    def test_console_level_from_env(self):
        self.sandbox.pop("FVP_LOG_LEVEL", "FVP_DEBUG")
        self.assertEqual(cl._console_level(), logging.WARNING)
        self.sandbox.set(FVP_LOG_LEVEL="debug")
        self.assertEqual(cl._console_level(), logging.DEBUG)
        self.sandbox.set(FVP_LOG_LEVEL="不认识的级别")
        self.assertEqual(cl._console_level(), logging.WARNING)
        self.sandbox.pop("FVP_LOG_LEVEL")
        self.sandbox.set(FVP_DEBUG="1")
        self.assertEqual(cl._console_level(), logging.DEBUG)

    def test_console_handler_level_follows_env(self):
        self.sandbox.set(FVP_LOG_LEVEL="DEBUG")
        self.start_log()
        console = [h for h in self.root.handlers
                   if getattr(h, "_fvp_console", False)]
        self.assertEqual(len(console), 1)
        self.assertEqual(console[0].level, logging.DEBUG)
        # 根 logger 跟随控制台级别；默认 WARNING 时压住 flet 的 debug 刷屏
        self.assertEqual(self.root.level, logging.DEBUG)

    def test_faulthandler_enabled(self):
        import faulthandler
        self.start_log()
        self.assertIsNotNone(cl._fault_stream)
        self.assertTrue(faulthandler.is_enabled())

    def test_exit_marker_logged_once(self):
        path = self.start_log()
        cl.mark_exit_once()
        cl.mark_exit_once()  # atexit 与断连都到也只记一次
        self.assertEqual(self.read_log(path).count("正常退出"), 1)

    def test_atexit_marker_in_subprocess(self):
        code = (
            "import sys\n"
            "from pathlib import Path\n"
            "from fvpapp.crashlog import setup_logging, install_hooks\n"
            "p = setup_logging(Path(sys.argv[1]))\n"
            "install_hooks()\n"
            "print(p)\n"
        )
        log_dir = self.tmp / "sub"
        proc = self.run_python(code, str(log_dir))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        path = Path(proc.stdout.strip())
        self.assertTrue(path.is_file())
        text = self.read_log(path)
        self.assertIn("正常退出", text)
        self.assertIn("启动于", text)


class HooksTest(CrashlogTestBase):
    def install(self) -> list[tuple]:
        calls: list[tuple] = []
        sys.excepthook = lambda *a: calls.append(a)
        cl.install_hooks()
        return calls

    def test_main_thread_exception_logged_and_chained(self):
        path = self.start_log()
        calls = self.install()
        try:
            raise ValueError("主线程炸了")
        except ValueError:
            sys.excepthook(*sys.exc_info())
        text = self.read_log(path)
        self.assertIn("主线程炸了", text)
        self.assertIn("主线程未捕获异常", text)
        self.assertIn("CRITICAL", text)
        self.assertEqual(len(calls), 1, "原 excepthook 仍应被调用")

    def test_keyboard_interrupt_not_logged(self):
        path = self.start_log()
        calls = self.install()
        try:
            raise KeyboardInterrupt
        except KeyboardInterrupt:
            sys.excepthook(*sys.exc_info())
        text = self.read_log(path)
        self.assertNotIn("KeyboardInterrupt", text)
        self.assertNotIn("主线程未捕获异常", text)
        self.assertEqual(len(calls), 1)

    def test_system_exit_not_logged(self):
        path = self.start_log()
        self.install()
        try:
            raise SystemExit(3)
        except SystemExit:
            sys.excepthook(*sys.exc_info())
        self.assertNotIn("SystemExit", self.read_log(path))

    def test_thread_exception_logged_with_name(self):
        path = self.start_log()
        seen: list = []
        threading.excepthook = lambda args: seen.append(args)
        cl.install_hooks()

        def boom():
            raise RuntimeError("线程里炸了")

        t = threading.Thread(target=boom, name="thumb-0")
        t.start()
        t.join()
        text = self.read_log(path)
        self.assertIn("线程里炸了", text)
        self.assertIn("thumb-0", text)
        self.assertIn("线程未捕获异常", text)
        self.assertEqual(len(seen), 1, "原 threading.excepthook 仍应被调用")

    @unittest.skipUnless(hasattr(sys, "unraisablehook"), "3.8+ 才有 unraisablehook")
    def test_unraisable_exception_logged(self):
        path = self.start_log()
        seen: list = []
        sys.unraisablehook = lambda args: seen.append(args)
        cl.install_hooks()
        exc = RuntimeError("析构里炸了")
        sys.unraisablehook(type("A", (), {
            "exc_type": RuntimeError,
            "exc_value": exc,
            "exc_traceback": None,
            "err_msg": "Exception ignored in __del__",
            "object": None,
        })())
        text = self.read_log(path)
        self.assertIn("析构里炸了", text)
        self.assertIn("无法抛出的异常", text)
        self.assertEqual(len(seen), 1)

    def test_duplicate_install_does_not_double_wrap(self):
        path = self.start_log()
        self.install()
        hook = sys.excepthook
        cl.install_hooks()
        self.assertIs(sys.excepthook, hook)
        try:
            raise ValueError("只该记一次")
        except ValueError:
            sys.excepthook(*sys.exc_info())
        self.assertEqual(self.read_log(path).count("主线程未捕获异常"), 1)

    def test_asyncio_task_exception_logged_and_chained(self):
        import asyncio

        path = self.start_log()
        states: list[dict] = []

        async def main():
            loop = asyncio.get_running_loop()
            loop.set_exception_handler(lambda _l, ctx: states.append(ctx))
            cl.install_asyncio_handler(loop)

            async def bad():
                raise RuntimeError("任务里炸了")

            task = asyncio.create_task(bad())
            await asyncio.sleep(0)   # 让任务跑完并把异常记在 Task 上
            del task                 # 没人 await，Task 回收时才交给 loop
            gc.collect()

        asyncio.run(main())
        self.assertIn("任务里炸了", self.read_log(path))
        self.assertTrue(states, "原 loop 异常处理器仍应被调用")
        self.assertIsInstance(states[0].get("exception"), RuntimeError)

    def test_asyncio_context_without_exception(self):
        path = self.start_log()

        class Loop:
            handler = None

            def get_exception_handler(self):
                return None

            def set_exception_handler(self, handler):
                self.handler = handler

        loop = Loop()
        cl.install_asyncio_handler(loop)
        loop.handler(loop, {"message": "没有异常对象的上下文"})
        self.assertIn("没有异常对象的上下文", self.read_log(path))

    def test_asyncio_previous_handler_raising_does_not_escape(self):
        import asyncio

        path = self.start_log()
        loop = asyncio.new_event_loop()
        self.addCleanup(loop.close)

        def angry(_l, _ctx):
            raise RuntimeError("旧处理器也炸了")

        loop.set_exception_handler(angry)
        cl.install_asyncio_handler(loop)
        loop.call_exception_handler({"message": "新消息"})
        self.assertIn("新消息", self.read_log(path))

    def test_asyncio_handler_none_loop_is_noop(self):
        cl.install_asyncio_handler(None)


class PruneTest(CrashlogTestBase):
    def make_logs(self, d: Path, n: int, start: int = 0) -> list[Path]:
        d.mkdir(parents=True, exist_ok=True)
        out = []
        for i in range(n):
            p = d / f"fvptachie-2.2.3-202601{i + 1:02d}-000000.log"
            p.write_text("x", encoding="utf-8")
            stamp = 1_700_000_000 + start + i
            os.utime(p, (stamp, stamp))
            out.append(p)
        return out

    def test_keeps_newest_by_mtime(self):
        files = self.make_logs(self.tmp / "prune", 5)
        cl._prune(self.tmp / "prune", 2)
        left = sorted(p.name for p in (self.tmp / "prune").glob("fvptachie-*.log"))
        self.assertEqual(left, [files[3].name, files[4].name])

    def test_keeps_all_when_below_limit(self):
        files = self.make_logs(self.tmp / "few", 3)
        cl._prune(self.tmp / "few", 5)
        self.assertTrue(all(p.exists() for p in files))

    def test_ignores_unrelated_files(self):
        files = self.make_logs(self.tmp / "mixed", 3)
        other = self.tmp / "mixed" / "other.log"
        other.write_text("keep me", encoding="utf-8")
        cl._prune(self.tmp / "mixed", 1)
        self.assertTrue(other.exists())
        self.assertEqual([p for p in files if p.exists()], [files[-1]])

    def test_missing_dir_is_quiet(self):
        cl._prune(self.tmp / "does-not-exist", 2)

    def test_setup_prunes_to_keep_runs(self):
        files = self.make_logs(self.tmp / "many", cl.KEEP_RUNS + 5)
        path = self.start_log(self.tmp / "many")
        remaining = sorted((self.tmp / "many").glob("fvptachie-*.log"))
        # FileHandler(delay=True)：新文件在 _prune 之后才因首条记录落盘，
        # 所以磁盘上会短暂是 KEEP_RUNS + 1 个。
        self.assertEqual(len(remaining), cl.KEEP_RUNS + 1)
        self.assertIn(path, remaining)
        for gone in files[:5]:
            self.assertFalse(gone.exists(), f"{gone.name} 应被清掉")


class DegradeTest(CrashlogTestBase):
    def test_unwritable_dir_returns_none(self):
        blocker = self.tmp / "not-a-dir"
        blocker.write_text("", encoding="utf-8")
        self.assertIsNone(cl.setup_logging(blocker / "logs"))
        self.assertIsNone(cl.current_log_path())
        self.assertEqual(self.file_handlers(), [])

    def test_recovers_after_degrade(self):
        blocker = self.tmp / "not-a-dir"
        blocker.write_text("", encoding="utf-8")
        self.assertIsNone(cl.setup_logging(blocker / "logs"))
        path = self.start_log(self.tmp / "after")
        self.assertTrue(path.is_file())
        self.assertEqual(cl.current_log_path(), path)

    @unittest.skipUnless(hasattr(__import__("faulthandler"), "_sigsegv"),
                         "需要 CPython 的 faulthandler._sigsegv")
    def test_native_crash_leaves_fatal_traceback(self):
        code = (
            "import faulthandler, sys\n"
            "from pathlib import Path\n"
            "from fvpapp.crashlog import setup_logging\n"
            "print(setup_logging(Path(sys.argv[1])))\n"
            "faulthandler._sigsegv()\n"
        )
        log_dir = self.tmp / "crash"
        proc = self.run_python(code, str(log_dir))
        self.assertNotEqual(proc.returncode, 0, "段错误子进程应异常退出")
        logs = list(log_dir.glob("fvptachie-*.log"))
        self.assertEqual(len(logs), 1)
        text = self.read_log(logs[0])
        self.assertIn("Fatal Python error", text)
        self.assertIn("Current thread", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
