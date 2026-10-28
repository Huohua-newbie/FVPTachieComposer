# 需求变更同步（飞书）

> 要把这套流程搬到别的项目：看根目录 [`FEISHU-WORKFLOW.md`](../FEISHU-WORKFLOW.md)
> （通用模板 + 换项目清单）。

目标：需求一变，**代码推上去之前**任务卡、测试表、变更表已经改好，群里也已通知。
协作者不用靠猜，也不会照着旧目标继续做。

```
feishu/changes/xxx.json  ──►  scripts/feishu_sync.py  ──►  任务表（验收标准）
        （清单，入库）              （本机 lark-cli）         测试表（阻塞旧行 / 新增行）
                                                              变更表（一条记录）
                                                              协作群（markdown + @）
```

推送时 `.githooks/pre-push` 会扫提交信息里的 `Feishu-Change:`，逐份执行清单；
**任何一份失败都会拦下推送**。清单本身跟代码一起提交，事后能对上「哪次需求变更对应哪次提交」。

## 一次性安装

**会推清单的人**在自己的机器上安装（用的是本机 `lark-cli` 登录态）：

```bash
git pull
py -3.13 scripts/install_feishu_hook.py
```

确认 `lark-cli auth status` 是已登录；脚本不会读取或保存任何 token。
只拉代码、提普通 PR 的协作者不装也能推——提交里没有 `Feishu-Change:` 就不会触发同步。

## 写一份变更清单

复制模板，填成 `feishu/changes/YYYY-MM-DD-短名.json`：

```bash
cp feishu/change.example.json feishu/changes/2026-10-31-dialog-memory.json
```

最小字段：

| 字段 | 作用 |
| --- | --- |
| `id` | 清单唯一名，也是幂等键；同步过一次后再推不会重复建记录 / 发消息 |
| `summary` | 群消息标题 |
| `old_goal` / `new_goal` | 旧目标 → 新目标，协作者只看这两行也知道该停什么 |
| `commit` / `pr` / `link` | 留空会自动取当前短 commit；`link` 优先，其次 `commit / pr` |
| `task.record_id` | 要改的任务卡；`acceptance_append` 追加验收口径，`note_append` 追加负责人备注 |
| `tests.block[]` | 作废/暂停的旧测试行；默认把 `结果` 改成「阻塞」，`note_append` 写原因 |
| `tests.create[]` | 新测试行，字段名与飞书表头一致（`用例`/`关联卡`/`类型`/`结果`/`前置数据`/`步骤`/`期望`/`备注`） |
| `change` | 变更表一条；`日期`/`版本`/`关联` 没填会自动补 |
| `notify` | 默认用 `config.json` 的群；`enabled: false` 可只改表不发消息；`identity` 可覆盖发送身份 |

群消息默认用 **bot** 身份发（`notify.identity`，默认 `bot`）：user 身份发消息需要额外授权
`im:message.send_as_user`，不该让群通知成为推送的前置条件。读表 / 改表仍用 `identity`（默认 `user`）。

`acceptance_append` 是**追加**，不用把旧验收标准抄一遍；`note_append` 同理，旧备注保留。

## 跑

```bash
# 先看会写什么（会读当前记录来算追加，但不写库、不发消息）
py -3.13 scripts/feishu_sync.py --manifest feishu/changes/xxx.json --dry-run

# 真正执行；之后提交时带上 trailer
git commit -m "feat: ..." -m "Feishu-Change: feishu/changes/xxx.json"
```

推送前钩子会自动跑一次；也可以手动补跑：

```bash
py -3.13 scripts/feishu_sync.py --manifest feishu/changes/xxx.json
```

- `--no-notify`：只改表，不发群消息
- `--force`：忽略「已同步」状态重跑（可能重复建记录，慎用）
- 状态在 `.feishu-sync/state.json`（不入库）；中途失败下次从断点继续

## 什么时候必须写清单

**用户能感觉到的目标变了**：验收口径、优先级、行为边界、测试期望。
纯重构 / typo / 依赖升级不用写，也不发群消息。

变更发生后**先写清单并推送**，再继续做新目标 —— 这样协作者在拉代码时就被拦住了。

## 看板变更怎么通知

看板变动由**维护者负责**，Agent 不需要开轮询或后台监视器。通知分三段，**不要互相补发**：

| 触发 | 通知方式 | 覆盖 |
|------|---------|------|
| 任务表 `状态/优先级/负责人` 被改 | Base 自动化「任务状态变更 → 开发组」 | AI 推进状态、人手动改都发 |
| 测试表 `期望/步骤/前置数据/用例` 被改 | Base 自动化「测试口径变更 → 开发组」 | 口径变了才发；跑用例改 `结果/备注` 不打扰 |
| 变更行新增 | `feishu_sync.py` 汇总消息 | 带旧目标 / 新目标 / 要跑的用例 |

自动化在飞书 Base 里配置（`lark-cli base +workflow-list` 可核对），不依赖任何仓库外的服务。
测试表只盯口径字段，是为了避免每次跑用例都刷屏；任务表盯状态，是为了让「谁做完了、
该谁评审」直接可见。所以 Agent 的职责就一条：**把表写对**，不要去群里再发一遍进度。

### feishu_watch.py（可选，手动对比用）

飞书没把「Base 记录变更」放进 `lark-cli event list` 的事件目录（只有 IM、任务、会议等），
脚本用表级 `rev` 做闸门：每轮只拉 1 行，**rev 没动就不拉全表**；动了才拉全表逐条 diff，
报新增 / 修改 / 删除 / 列变化。

```bash
# 只在人类明确要求「对比板子改了什么」时手动跑一次
python3 scripts/feishu_watch.py --once
```

- 不需要常驻：`--interval` 存在，但约定不开后台监视器
- 首次运行只建基线，不会把现有几十行当成「新增」刷屏
- 状态在 `.feishu-sync/watch-state.json`（不入库）；删掉它等于重新建基线
- 默认盯 `tasks / tests / changes` 三张表，`--tables tests,changes` 可缩小
- 这不是审计日志：只保证「下一次能看到」，不保证每一次中间态都留痕

没有 `lark-cli` 的机器看不到看板：Agent 必须明说「未能检查飞书」，让人补清单，
不能假装检查过。

## 配置与安全

- `feishu/config.json`（入库）：Base / 表 / 群 ID、字段名映射
- `feishu/local.json`（gitignore）：本机覆盖，例如临时换群
- `FVP_LARK_CLI`：覆盖 lark-cli 启动命令；Windows 上若 lark-cli 在 WSL，用
  `wsl.exe -e zsh -lc 'exec lark-cli "$@"' _`（详见上文「协作者的 Agent 怎么接」）
- 脚本只调用本机 `lark-cli`，不碰 GitHub Secrets / webhook；群消息用 markdown
- 清单里的 `record_id` 是飞书记录 ID；不确定就用 `lark-cli base +record-list` 查

## 离线应急

钩子同步失败默认拦推送。确需离线推送：

```bash
FVP_FEISHU_SYNC=off git push
```

之后务必手动补跑同一份清单；否则协作者看到的仍是旧目标。
