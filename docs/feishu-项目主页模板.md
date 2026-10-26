# 飞书「项目主页」文档模板

> 用法：在飞书云文档里新建一篇文档，把下面 `---` 之后的内容整段粘进去，
> 然后把 `<...>` 换成你们自己的链接和人名。这篇文档只在飞书里维护，不进仓库。

---

# FVP Tachie Composer · 项目主页

## 一句话

给差分立绘做「表情预览 + 合成导出」的 Windows 桌面工具，解压即用，用户不需要装 Python。

## 现在在哪一步

- 当前正式版：**v2.2.3** —— [下载](https://github.com/Huohua-newbie/FVPTachieComposer/releases/latest)
- 这一期在做：<例如「差分预览性能优化」>
- 下一版预计包含：<最多写 3 条，写多了等于没重点>

## 谁做什么

| 人 | 负责 | 卡住了找 TA |
| --- | --- | --- |
| <你的名字> | 合并 PR、发版、UI 与合成逻辑 | 需求取舍、能不能发版 |
| <同学的名字> | <例如 文档 / 某个模块 / 测试> | <对应范围> |

> 规则：**一个人负责一件事**。同一个模块同时两个人改，冲突只是时间问题。

## 链接（全项目只放这一处）

- 代码仓库：https://github.com/Huohua-newbie/FVPTachieComposer
- 任务看板：<多维表格链接>
- 协作规范：仓库里的 `CONTRIBUTING.md`
- 发版流程：仓库里的 `RELEASING.md`
- 下载页：https://github.com/Huohua-newbie/FVPTachieComposer/releases

## 新人环境（20 分钟）

```bash
git clone https://github.com/Huohua-newbie/FVPTachieComposer.git
cd FVPTachieComposer
py -3.13 -m venv .venv
.venv\Scripts\activate            # PowerShell / cmd；Git Bash 用 source .venv/Scripts/activate
pip install -r requirements.txt
py -3.13 FVPTachieComposerFlet.py
```

自检：`python tests/test_ui_smoke.py`、`python tests/test_thumb_threading.py`、`python .github/selfcheck.py`

## 我们的节奏

| 频率 | 做什么 |
| --- | --- |
| 每天 | 看板认领一张卡 → 开分支 → 提 PR → 群里 @人 review |
| 每周一次 | 15 分钟站会（飞书会议 + 妙记），会后待办直接进看板 |
| 攒够再发 | 一批改动做完 → 改版本号 → 打包 → 发 Release（只有维护者能发） |

## 边界（避免返工）

- **代码只在 GitHub**，飞书只放文档、任务、会议结论
- **任务只在多维表格**，群里聊出来的事，散会 5 分钟内进表，否则等于没发生
- 大文件（素材、录屏）放飞书云盘并标版本；构建产物走 Release，不互相发包

## 决策记录

| 日期 | 决定 | 原因 |
| --- | --- | --- |
| <YYYY-MM-DD> | <例如：不做自动更新> | <例如：签名成本高，用户量还小> |

> 有争议的事在这里落一行，过两周就不会再吵一遍。

## 常见问题

- **启动后一片空白 / 弹框没反应**：多半是用了 `flet run -d` 热重载，改成直接跑 `py -3.13 FVPTachieComposerFlet.py`。
- **推代码被拒绝**：不在 `main` 上改，看到 protected branch 就开分支。
- **不知道现在该做什么**：看板里「待认领」那一列，不要自己猜。
