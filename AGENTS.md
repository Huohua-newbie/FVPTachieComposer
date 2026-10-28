# Agent 工作约定

本项目是 FVP 角色立绘查看 / 合成工具（Flet 桌面端）。功能说明只认
[`README.md`](README.md)，协作流程见 [`CONTRIBUTING.md`](CONTRIBUTING.md)。

## 开工前必须做（每次会话）

1. 同步 main：`git switch main && git pull`；如果已在任务分支，先 `git rebase origin/main`
2. 读 `README.md` 里和当前任务有关的小节
3. 手上的活如果可能被改过口径，直接读飞书任务卡的「验收标准」——**不要**开轮询或后台
   监视器：看板口径的变动由维护者负责通知，Agent 只按当前的表和任务卡办事

## 维护看板（硬性责任）

看板不是给人看的附属品，是协作的唯一事实来源。群里能收到变更通知，靠的是**你把状态写对**，
不是靠谁记得转述：

| 表 | 什么时候写 | 写什么 |
|----|-----------|--------|
| 任务 | 开工、交评审、完成、搁置 | `状态`（待开始/进行中/待评审/已完成/搁置）、`负责人`、`更新日期` |
| 测试 | 跑完一条用例 | `结果`（通过/失败/未跑）、`备注`；口径变了才改 `期望/步骤/前置数据/用例` |
| 变更 | 用户能感觉到的行为变了 | 一行：`功能/类型/改了什么/为什么/版本/关联` |

- 自动化在飞书 Base 里由维护者配置（`lark-cli base +workflow-list` 可核对）
- 所以**不需要**另发群消息报进度；把表改对，通知自然发生（手动补发反而重复）
- 改动做完必须回写：任务状态推进到「待评审」，测试跑过就把 `结果` 从「未跑」改掉

## 改完之后

- 用户能感觉到的行为变了：同一个提交里改 `README.md`，并按 `CONTRIBUTING.md` 写
  `Feishu-Change` 清单
- 推送前按 [`TESTING.md`](TESTING.md) 跑无头自检，全绿再推
- 不要提交 `.feishu-sync/`、`feishu/local.json`、日志或截图

## 边界

- 飞书只通过本机 `lark-cli` 访问；不要把 webhook、app secret、token 写进仓库或 GitHub Secrets
- 没有 `lark-cli` 时不要假装看过了看板：明确告诉人类「未能检查飞书」，让对方同步或给清单
- 不要开后台监视器 / 轮询：看板变动由维护者负责通知；`feishu_watch.py` 只在人类明确
  要求对比时手动跑一次 `--once`
- 板上的聊天记录不算数；定下来的结论必须回写 `README.md` 或变成清单

## Windows 上调 WSL 里的 lark-cli

`lark-cli` / `node` 在 nvm 里，非登录 shell 找不到，`wsl.exe -e lark-cli` 会 `execvpe` 失败。
跑 `feishu_sync.py` 等脚本前设：

```powershell
$env:FVP_LARK_CLI = 'wsl.exe -e zsh -lc ''exec lark-cli "$@"'' _'
```

（在 WSL 里直接跑 `python3` 则不需要这个变量。）
