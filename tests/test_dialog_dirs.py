"""对话框目录记忆（跨重启）的回归测试。

对应「记住上次的文件夹」：
- 打开 / 保存对话框各记各的，互不污染
- 写进 settings.json 后，重新构造 App 能读回来（跨重启保留）
- 上次的目录被删掉 / 换成文件 / 字段类型不对 → 当作没设过，不抛异常
- 写目录时不能顺带清掉窗口等其它设置段

纯逻辑，不需要 flet：python tests/test_dialog_dirs.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

_tmp = Path(tempfile.mkdtemp(prefix="fvp_dialog_dirs_"))
os.environ["FVP_SETTINGS_PATH"] = str(_tmp / "settings.json")

import fvpapp.settings as st  # noqa: E402

fails = []


def check(name, ok):
    print(("PASS " if ok else "FAIL ") + name)
    if not ok:
        fails.append(name)


def path():
    return st.settings_path()


def wipe():
    for p in (path(), path().with_name(path().name + ".tmp")):
        try:
            p.unlink()
        except OSError:
            pass


def read_raw():
    return json.loads(path().read_text(encoding="utf-8"))


IMPORT_DIR = _tmp / "import_here"
EXPORT_DIR = _tmp / "export_here"
IMPORT_DIR.mkdir()
EXPORT_DIR.mkdir()

# ── 没存过 / 文件坏掉：回空，不抛 ─────────────────────────────────────────
wipe()
check("没有设置文件时不记任何目录", st.dialog_dirs() == {})

for bad in (b"", b"{", b"[1,2,3]", b'{"dialogs": 7}', b'{"dialogs": [1]}',
            b"\xff\xfe\x00broken"):
    wipe()
    path().write_bytes(bad)
    try:
        got = st.dialog_dirs()
        ok = got == {}
    except Exception as exc:
        ok, got = False, repr(exc)
    check(f"坏文件 {bad[:16]!r} 只回空不抛异常", ok)

# ── 存进去、再读出来：两个目录各归各的 ────────────────────────────────────
wipe()
st.save_dialog_dirs(str(IMPORT_DIR), str(EXPORT_DIR))
got = st.dialog_dirs()
check("导入目录能读回来", got.get("import") == str(IMPORT_DIR))
check("导出目录能读回来", got.get("export") == str(EXPORT_DIR))
check("写出来是 dialogs 段", isinstance(read_raw().get("dialogs"), dict))

# 只设过一个：另一边不出现，不会把 None 当路径
wipe()
st.save_dialog_dirs(str(IMPORT_DIR), None)
got = st.dialog_dirs()
check("只设导入时导出不出现", got == {"import": str(IMPORT_DIR)})

# ── 目录没了：当作没设过，让对话框回系统默认位置 ──────────────────────────
wipe()
gone = _tmp / "gone_dir"
gone.mkdir()
st.save_dialog_dirs(str(gone), str(EXPORT_DIR))
gone.rmdir()
got = st.dialog_dirs()
check("被删掉的目录被过滤", "import" not in got)
check("同一段里另一个仍有效", got.get("export") == str(EXPORT_DIR))

not_a_dir = _tmp / "a_file.txt"
not_a_dir.write_text("x", encoding="utf-8")
wipe()
st.save_dialog_dirs(str(not_a_dir), str(EXPORT_DIR))
check("路径是文件时被过滤", "import" not in st.dialog_dirs())

wipe()
path().write_text(json.dumps({"dialogs": {"import": 123, "export": ["x"],
                                         "other": str(IMPORT_DIR)}}), encoding="utf-8")
check("非字符串字段被过滤", st.dialog_dirs() == {})

# ── 写一段不能碰别的段 ────────────────────────────────────────────────────
wipe()
path().write_text(json.dumps({"window": {"width": 1300}, "theme": "dark"}), encoding="utf-8")
st.save_dialog_dirs(str(IMPORT_DIR), str(EXPORT_DIR))
raw = read_raw()
check("写目录时保留 window 段", raw.get("window") == {"width": 1300})
check("写目录时保留 theme 段", raw.get("theme") == "dark")

# 反过来：写窗口不能把 dialogs 冲掉
st.save_window({"width": 1400, "height": 800})
check("写窗口时保留 dialogs 段", st.dialog_dirs().get("import") == str(IMPORT_DIR))

# ── 写盘契约：不留半截文件；目录不可写也不抛 ──────────────────────────────
wipe()
st.save_dialog_dirs(str(IMPORT_DIR), str(EXPORT_DIR))
check("写完后没有残留 .tmp", not path().with_name(path().name + ".tmp").exists())

blocker = _tmp / "notdir"
blocker.write_text("x", encoding="utf-8")
os.environ["FVP_SETTINGS_PATH"] = str(blocker / "settings.json")
try:
    st.save_dialog_dirs(str(IMPORT_DIR), str(EXPORT_DIR))
    ok = True
except Exception as exc:
    ok = False
    print("  抛了:", repr(exc))
check("目录不可写时 save 不抛异常", ok)
check("目录不可写时读回空", st.dialog_dirs() == {})
os.environ["FVP_SETTINGS_PATH"] = str(_tmp / "settings.json")

if fails:
    print("\nFAILED:", fails)
    sys.exit(f"DIALOG_DIRS_TEST_FAILED: {fails}")
print("DIALOG_DIRS_OK")
