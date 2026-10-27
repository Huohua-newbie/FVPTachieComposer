# 协作指南（2 人版）

> 目标：让刚开始用 git 的人 20 分钟内跑通一次完整改动，并且永远不会把 `main` 弄坏。

## 谁做什么

| 角色 | 负责 | 改动怎么进 `main` |
| --- | --- | --- |
| **维护者**（项目作者） | 定方向、合并 PR、发版、仓库权限 | **直接 `git push`**（管理员可绕过保护） |
| **协作者**（同学） | 在分支上改代码 / 文档 | **分支 + PR**，等维护者合并 |

两人并行开发就是两条线：**维护者在 `main` 上直推，协作者在自己的分支上提 PR**。互不阻塞。

## 硬规则

对**协作者**：

1. **不要直接往 `main` 推** —— 已开分支保护，推不上去。所有改动走「分支 + PR」。
2. **PR 必须写清「怎么验证的」** —— 不能只写「改好了」。

对**维护者**：

3. **直推 `main` 前把三个测试跑绿**（`test_ui_smoke` / `test_thumb_threading` / `selfcheck`），一次推送只做一件事。
4. **不要 `git push --force` 到 `main`** —— 协作者的分支是从它长出来的。

其他都可以商量，这几条不商量。

---

## 同学第一次上手（约 20 分钟）

### 1. 装环境

- [Git for Windows](https://git-scm.com/download/win)（一路下一步，装完会有 Git Bash）
- Python **3.13**（命令行里 `py -3.13 -V` 能出版本号就行；项目要求 `>=3.13,<3.14`）

### 2. 把仓库拉下来

```bash
git clone https://github.com/Huohua-newbie/FVPTachieComposer.git
cd FVPTachieComposer
```

### 3. 装依赖 + 跑起来

```bash
py -3.13 -m venv .venv
.venv\Scripts\activate            # PowerShell / cmd 用这行
# Git Bash 用：source .venv/Scripts/activate
pip install -r requirements.txt

py -3.13 FVPTachieComposerFlet.py
```

窗口能起来、能点「打开 BIN」就算环境 OK（没有素材也可以，列表空着是正常的）。

> **不要用 `flet run -d` 热重载**：那个模式下 FilePicker 服务建不起来，所有弹框都会卡住。
> 理由见 [`tests/DEV.md`](tests/DEV.md)。

### 4. 跑一次自检

```bash
python tests/test_ui_smoke.py
python tests/test_thumb_threading.py
python .github/selfcheck.py
```

三个都过 = 环境和工作区都是干净的。

---

## 改一样东西的完整流程

```bash
# ① 先同步，别在旧代码上开工
git switch main
git pull

# ② 开一条分支（名字带任务号，方便对上看板）
git switch -c feat/t12-缩略图间距

# ③ 改代码，然后只提交你改的文件
git status
git add FVPTachieComposerFlet.py
git commit -m "feat: 缩略图间距 4px → 6px"

# ④ 推上去
git push -u origin feat/t12-缩略图间距
```

⑤ 终端会打印一个链接 → 打开 → 点 **Create pull request** → 把模板四项填完 → 群里 @维护者。

维护者用 **Squash and merge** 合并后，本地收尾：

```bash
git switch main
git pull
git branch -d feat/t12-缩略图间距
```

---

## 提交信息怎么写

格式：`类型: 一句话说清改了什么`

| 类型 | 用在 |
| --- | --- |
| `feat` | 新功能 |
| `fix` | 修 bug |
| `docs` | 只改文档 |
| `style` | 只改格式 / 空白，不动逻辑 |
| `perf` | 性能 |
| `chore` | 依赖、构建、杂事 |

中文写清楚即可。别写「更新」「修改」这种看不出内容的。

---

## 新手的第一件事：先改一个标点

别一上来就改功能。先完整走一遍：**改 README 里的一句话 → 开分支 → 提交 → 推 → 提 PR → 合并**。

流程跑通了再碰代码。这样真出问题时，你能区分是「git 不会用」还是「代码改错了」。

---

## 常见错误

| 现象 | 原因 | 怎么办 |
| --- | --- | --- |
| `! [rejected] main -> main (fetch first)` | 本地 `main` 落后了 | `git pull --rebase` 再推 |
| 推不上去，提示 protected branch | 你在 `main` 上改了 | 用下面的「误改 main」办法救 |
| PR 里混进一堆没改过的文件 | 用了 `git add .` | 只 `git add` 具体文件 |
| 冲突（conflict） | 两人改了同一行 | 找改同一处的人一起看，别硬删对方代码 |
| 改了没生效 | 以为热重载在跑 | 关掉进程重开 |

### 在 `main` 上误提交了怎么救

```bash
git switch -c fix/wip          # 把当前这些提交接到新分支上
git switch main
git reset --hard origin/main   # main 回到远端状态（改动已经在 fix/wip 里了）
git switch fix/wip
```

---

## 不要做的事

- 不要提交 `dist/`、`build/`、`.venv/`、截图、日志（`.gitignore` 挡了大部分，但别依赖它）
- 不要 `git push --force`
- 不要在飞书里贴代码片段当最终版 —— 代码只认 GitHub
- 不要自己改版本号或发 Release（维护者负责，见 [`RELEASING.md`](RELEASING.md)）
- 不要把整个项目打包发给对方 —— 走 `git pull`
