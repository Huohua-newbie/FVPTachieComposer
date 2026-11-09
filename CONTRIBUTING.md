# 协作指南

> 项目目前由维护者一人开发。这份文档留着两件事：改动的硬规则，和以后有人加入时的上手路径。

## 硬规则

1. **推 `main` 前把无头自检跑绿**（`python tests/run_all.py`，清单在 [`TESTING.md`](TESTING.md)，
   加测试只在那儿补一行，这里不数个数），一次推送只做一件事。
2. **不要 `git push --force` 到 `main`**。
3. **用户能感觉到的行为变了，同一个提交里改 `README.md`** —— 它是唯一的功能说明。
4. **口径不清楚就问，别猜**；聊天里定下来的结论必须回写 `README.md`，否则等于没定。

协作者加入后走「分支 + PR」，维护者合并；维护者自己直推 `main`。PR 必须写清「怎么验证的」。

## 文档在哪

| 要什么 | 去哪 |
| --- | --- |
| 功能**现在**怎么工作（改代码前先读） | [`README.md`](README.md) —— **唯一的功能说明**，跟代码一起改 |
| 每个改动**为什么**这么改 | 提交信息正文（写动机与取舍，diff 已经说明了「改了什么」） |
| 怎么验证、人工冒烟清单 | [`TESTING.md`](TESTING.md) |
| 怎么构建、怎么发版 | [`BUILD.md`](BUILD.md) / [`RELEASING.md`](RELEASING.md) |

## 什么算「做完了」

1. 无头自检全绿（清单见 [`TESTING.md`](TESTING.md) 的「无头自检」）
2. 用户能感觉到的行为变了 → `README.md` 对应小节也改了（同一个提交里改）
3. 提交信息说清「为什么」—— 三个月后回头看 `git log` 能读懂这次改动的动机

只改代码不改文档 = 没做完。

---

## 第一次上手（约 20 分钟）

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
python tests/run_all.py   # 或按 TESTING.md「无头自检」逐个跑
```

全绿才继续（清单以 [`TESTING.md`](TESTING.md) 的「无头自检」为准，加测试只在那儿补一行）。
`run_all.py` 每个文件起独立子进程，Windows 上会自动给 UI 冒烟补 `FLET_PLATFORM=windows`；
缺 flet/Pillow 时 `test_ui_smoke.py` 是失败不是跳过，所以全套要在打包机上跑。

---

## 改一样东西的完整流程

```bash
# ① 先同步，别在旧代码上开工
git switch main
git pull

# ② 开一条分支（协作者用；维护者可以直推 main）
git switch -c feat/缩略图间距

# ③ 改代码，然后只提交你改的文件
git status
git add FVPTachieComposerFlet.py
git commit -m "feat: 缩略图间距 4px → 6px"

# ④ 推上去
git push -u origin feat/缩略图间距
```

⑤ 终端会打印一个链接 → 打开 → 点 **Create pull request** → 把模板填完 → 通知维护者。

维护者用 **Squash and merge** 合并后，本地收尾：

```bash
git switch main
git pull
git branch -d feat/缩略图间距
```

---

## 提交信息怎么写

格式：`类型(范围): 一句话说清为什么这么改`

| 类型 | 用在 |
| --- | --- |
| `feat` | 新功能 |
| `fix` | 修 bug |
| `docs` | 只改文档 |
| `style` | 只改格式 / 空白，不动逻辑 |
| `perf` | 性能 |
| `test` | 只加 / 改测试 |
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
- 不要自己改版本号或发 Release（见 [`RELEASING.md`](RELEASING.md)）
- 不要把 token、webhook、app secret 写进仓库或 GitHub Secrets
- 不要把整个项目打包发给对方 —— 走 `git pull`
