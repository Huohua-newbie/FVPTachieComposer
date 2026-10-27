"""窗口记忆（尺寸 / 位置 / 最大化）的回归测试。

对应任务①的验收项与两个坑：
- 删掉 `settings.json` → 回默认 1280×760 且不报错（读坏的、类型不对的也一样）
- 只在关闭时存不够 → 尺寸/位置靠事件记录，这里验 `capture_window` 的取值规则
- 坐标要校验 → 出界的位置丢掉，屏幕内的（含副屏负坐标）留下

纯逻辑，不需要 flet：python tests/test_window_memory.py
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

_tmp = Path(tempfile.mkdtemp(prefix="fvp_settings_"))
os.environ["FVP_SETTINGS_PATH"] = str(_tmp / "settings.json")

import fvpapp.settings as st  # noqa: E402

fails = []


def check(name, ok):
    print(("PASS " if ok else "FAIL ") + name)
    if not ok:
        fails.append(name)


class Win:
    """够用的 flet Window 替身：只有被读的那几个属性。"""

    def __init__(self, width=1280, height=760, left=100, top=80, maximized=False):
        self.width, self.height = width, height
        self.left, self.top = left, top
        self.maximized = maximized


def path():
    return st.settings_path()


def wipe():
    for p in (_tmp / "settings.json", _tmp / "settings.json.tmp"):
        try:
            p.unlink()
        except OSError:
            pass


SCREEN = (0.0, 0.0, 1920.0, 1080.0)

# ── 没有设置文件 / 文件坏掉：一律回默认，不许抛 ──────────────────────────
wipe()
plan = st.plan_window(st.window_prefs(), SCREEN)
check("没有设置文件时回默认 1280×760", (plan["width"], plan["height"]) == (1280, 760))
check("没有设置文件时不还原位置", plan["left"] is None and plan["top"] is None)
check("没有设置文件时不是最大化", plan["maximized"] is False)

for bad in (b"", b"{", b"[1,2,3]", b'{"window": 7}', b'{"window": {"width": "big", "left": null}}',
            b"\xff\xfe\x00broken"):
    wipe()
    path().write_bytes(bad)
    try:
        p = st.plan_window(st.window_prefs(), SCREEN)
        ok = (p["width"], p["height"]) == (1280, 760) and p["left"] is None
    except Exception as exc:
        ok, p = False, repr(exc)
    check(f"坏文件 {bad[:18]!r} 只回默认不抛异常", ok)

# ── 存进去、再读出来：尺寸位置最大化都要原样回来 ──────────────────────────
wipe()
prefs = st.capture_window(Win(width=1440, height=900, left=320, top=150), False, {})
st.save_window(prefs)
back = st.plan_window(st.window_prefs(), SCREEN)
check("尺寸能原样还原", (back["width"], back["height"]) == (1440, 900))
check("位置能原样还原", (back["left"], back["top"]) == (320, 150))
check("写出来的 JSON 是 window 段", isinstance(st.load().get("window"), dict))

# 删掉文件 = 回到默认（任务①的自测项）
wipe()
plan = st.plan_window(st.window_prefs(), SCREEN)
check("删掉 settings.json 后回默认 1280×760", (plan["width"], plan["height"]) == (1280, 760))
check("删掉 settings.json 后不残留位置", plan["left"] is None)

# 别的 section 不能被窗口写盘顺带清掉
wipe()
path().write_text(json.dumps({"theme": "dark", "window": {"width": 1300}}), encoding="utf-8")
st.save_window(st.capture_window(Win(), False, st.window_prefs()))
check("写窗口时保留其它设置段", st.load().get("theme") == "dark")

# ── 坐标校验：别让窗口落在屏幕外 ──────────────────────────────────────────
wipe()
st.save_window({"width": 1280, "height": 760, "left": 99999, "top": 400, "maximized": False})
plan = st.plan_window(st.window_prefs(), SCREEN)
check("出界的位置被丢掉", plan["left"] is None and plan["top"] is None)
check("位置出界时尺寸照样还原", (plan["width"], plan["height"]) == (1280, 760))

st.save_window({"width": 1280, "height": 760, "left": -4000, "top": -3000, "maximized": False})
plan = st.plan_window(st.window_prefs(), SCREEN)
check("整个跑到左上角外面的位置被丢掉", plan["left"] is None)

# 副屏在主屏左边：负坐标是合法的，得留下
LEFT_SCREEN = (-2560.0, 0.0, 4480.0, 1080.0)
st.save_window({"width": 1280, "height": 760, "left": -1500, "top": 200, "maximized": False})
plan = st.plan_window(st.window_prefs(), LEFT_SCREEN)
check("副屏上的负坐标保留", (plan["left"], plan["top"]) == (-1500, 200))

# 只露出一角的旧位置（拔掉显示器之后常见）也要判定为出界
st.save_window({"width": 1280, "height": 760, "left": 1900, "top": 200, "maximized": False})
plan = st.plan_window(st.window_prefs(), SCREEN)
check("只露 20px 的位置算出界", plan["left"] is None)

# 分辨率改小：记住的尺寸比现在的桌面还宽，收窄到桌面内
st.save_window({"width": 4000, "height": 3000, "left": 0, "top": 0, "maximized": False})
plan = st.plan_window(st.window_prefs(), SCREEN)
check("尺寸收进当前桌面", plan["width"] <= 1920 and plan["height"] <= 1080)
check("收窄后不低于下限", plan["width"] >= st.MIN_WIDTH and plan["height"] >= st.MIN_HEIGHT)

# 拿不到显示器范围（非 Windows / API 缺失）：还原尺寸，不还原位置
st.save_window({"width": 1440, "height": 900, "left": 300, "top": 200, "maximized": False})
plan = st.plan_window(st.window_prefs(), None)
check("校验不了范围时不冒险还原位置", plan["left"] is None and plan["top"] is None)
check("校验不了范围时尺寸照旧还原", (plan["width"], plan["height"]) == (1440, 900))
try:
    b = st.screen_bounds()
    ok = b is None or (len(b) == 4 and b[2] > 0 and b[3] > 0)
except Exception as exc:
    ok = False
    print("  抛了:", repr(exc))
check("screen_bounds 只返回 None 或合法四元组", ok)

# ── 记录：最大化不能覆盖上次的非最大化几何 ────────────────────────────────
last = {"width": 1440, "height": 900, "left": 320, "top": 150, "maximized": False}
maxed = Win(width=1920, height=1080, left=-8, top=-8, maximized=True)
got = st.capture_window(maxed, True, last)
check("最大化只改标记", got["maximized"] is True)
check("最大化时保留上次的尺寸", (got["width"], got["height"]) == (1440, 900))
check("最大化时保留上次的位置", (got["left"], got["top"]) == (320, 150))

got = st.capture_window(Win(width=1440, height=900, left=500, top=400), False, got)
check("还原后记下新的位置", (got["left"], got["top"], got["maximized"]) == (500, 400, False))

# 拖动过程中读到 0/None 之类的坏值：沿用上一次，不写坏文件
got = st.capture_window(Win(width=0, height=None, left=float("nan"), top=None), False,
                        {"width": 1440, "height": 900, "left": 500, "top": 400})
check("坏值不覆盖上一次记录",
      (got["width"], got["height"], got["left"], got["top"]) == (1440, 900, 500, 400))

# 类型不对的字段（字符串 / bool / NaN）不能进文件
wipe()
path().write_text(json.dumps({"window": {"width": True, "height": "900", "left": float("nan"),
                                         "top": 120, "maximized": "yes"}}), encoding="utf-8")
prefs = st.window_prefs()
check("字符串/bool/NaN 字段被过滤", prefs == {"top": 120.0})
plan = st.plan_window(prefs, SCREEN)
check("过滤后照样回默认尺寸", (plan["width"], plan["height"]) == (1280, 760))

# 写盘不留半截文件：临时文件不能残留
wipe()
st.save_window({"width": 1300, "height": 800})
check("写完后没有残留 .tmp", not (_tmp / "settings.json.tmp").exists())

# 目录建不出来时只记日志，不许抛（用一个"文件当目录"的路径，跨平台都失败）
wipe()
st.save_window({"width": 1300, "height": 800})
(_tmp / "notdir").write_text("x", encoding="utf-8")
os.environ["FVP_SETTINGS_PATH"] = str(_tmp / "notdir" / "settings.json")
try:
    st.save_window({"width": 1300, "height": 800})
    ok = True
except Exception as exc:
    ok = False
    print("  抛了:", repr(exc))
check("目录不可写时 save 不抛异常", ok)
check("目录不可写时 load 回默认", st.plan_window(st.window_prefs(), SCREEN)["width"] == 1280)
os.environ["FVP_SETTINGS_PATH"] = str(_tmp / "settings.json")

if fails:
    print("\nFAILED:", fails)
    sys.exit(f"WINDOW_MEMORY_TEST_FAILED: {fails}")
print("WINDOW_MEMORY_OK")
