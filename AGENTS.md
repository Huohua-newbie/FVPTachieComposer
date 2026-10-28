# Agent 工作约定

本项目是 FVP 角色立绘查看 / 合成工具（Flet 桌面端）。功能说明只认
[`README.md`](README.md)，协作流程见 [`CONTRIBUTING.md`](CONTRIBUTING.md)。

## 开工前必须做（每次会话）

1. 同步 main：`git switch main && git pull`；如果已在任务分支，先 `git rebase origin/main`
2. 查飞书看板有没有新变动：

   ```bash
   python3 scripts/feishu_watch.py --once
   ```

   - 没有输出 = 上次检查以来没有新变动
   - 出现 `[修改] / [新增] / [删除] / [结构]` 行 = **先停下当前任务**，读对应任务卡的
     「验收标准」和 `feishu/changes/` 下的清单，再决定是否继续；不要按旧口径写代码
3. 读 `README.md` 里和当前任务有关的小节。

Windows 上如果 `lark-cli` 装在 WSL：

```powershell
$env:FVP_LARK_CLI = 'wsl.exe -e zsh -lc ''exec lark-cli "$@"'' _'
py -3.13 scripts/feishu_watch.py --once
```

（`lark-cli` / `node` 在 nvm 里，非登录 shell 找不到；这段让 WSL 先读 `.zshrc` 再把参数原样转发。
直接在 WSL 里跑 `python3` 就不需要这个变量。）

## 维护看板（硬性责任）

看板不是给人看的附属品，是协作的唯一事实来源。群里能收到变更通知，靠的是**你把状态写对**，
不是靠谁记得转述：

| 表 | 什么时候写 | 写什么 |
|----|-----------|--------|
| 任务 | 开工、交评审、完成、搁置 | `状态`（待开始/进行中/待评审/已完成/搁置）、`负责人`、`更新日期` |
| 测试 | 跑完一条用例 | `结果`（通过/失败/未跑）、`备注`；口径变了才改 `期望/步骤/前置数据/用例` |
| 变更 | 用户能感觉到的行为变了 | 一行：`功能/类型/改了什么/为什么/版本/关联` |

- 这三张表都配了 Base 自动化：任务表的 `状态/优先级/负责人` 被改、测试表的
  `期望/步骤/前置数据/用例` 被改，会自动往开发组发一条消息；`feishu_sync.py` 负责
  变更行 + 一条带上下文的汇总消息
- 所以**不需要**另发群消息报进度；把表改对，通知自然发生（手动补发反而重复）
- 改动做完必须回写：任务状态推进到「待评审」，测试跑过就把 `结果` 从「未跑」改掉

## 长任务 / 多轮修改

在后台跑监视器，看到新行就停下来核对：

```bash
python3 scripts/feishu_watch.py --interval 20
```

（OpenCode / Qoder 用各自的后台任务或终端跑；不要把输出吞掉。）

## 改完之后

- 用户能感觉到的行为变了：同一个提交里改 `README.md`，并按 `CONTRIBUTING.md` 写
  `Feishu-Change` 清单
- 推送前按 [`TESTING.md`](TESTING.md) 跑无头自检，全绿再推
- 不要提交 `.feishu-sync/`、`feishu/local.json`、日志或截图

## 边界

- 飞书只通过本机 `lark-cli` 访问；不要把 webhook、app secret、token 写进仓库或 GitHub Secrets
- 没有 `lark-cli` 时不要假装看过了看板：明确告诉人类「未能检查飞书」，让对方同步或给清单
- `feishu_watch.py` 是 20 秒轮询 + 表 `rev` 闸门，不是逐事件审计；两次轮询之间的中间态可能看不到
- 板上的聊天记录不算数；定下来的结论必须回写 `README.md` 或变成清单
