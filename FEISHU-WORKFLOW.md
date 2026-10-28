# 飞书协作工作流（可复用模板）

一套「**需求变更先落表，再推代码**」的最小协作流程：飞书 Base 是事实来源，AI Agent 负责
把状态写对，变更自动通知到群。只有本机 `lark-cli` 登录态，仓库里不放任何 token。

> 本文是从 FVPTachieComposer 抽出来的通用版；换项目时按文末「复用清单」改字段名和 ID 即可。

## 1. 它解决什么问题

- 需求口径变了，协作者（或协作者的 Agent）还在照旧目标写代码
- 「谁做完了 / 该谁评审 / 哪条用例挂了」散落在聊天记录里
- 群里通知靠人手动转述，漏一次就断档

做法是三条：

1. **目标变更**走一份 JSON 清单，推送前自动写进 Base 并通知群；失败就拦下推送
2. **日常状态**（进行中 / 待评审 / 已完成、测试结果）由 Agent 直接改表，Base 自动化发通知
3. **结论**只认 Base 和 `README.md`，聊天记录不算数

## 2. 角色与分工

| 角色 | 负责 | 需要 lark-cli 吗 |
|------|------|-----------------|
| 维护者 | 建 Base / 群 / 自动化；写变更清单并推送；定验收口径 | 要（本机登录） |
| 协作者 | 改代码、维护自己任务卡的状态和测试结果 | 不要（提交里没有清单就不触发同步） |
| Agent（OpenCode / Qoder） | 读仓库根 `AGENTS.md`；按任务卡办事；把状态 / 结果 / 变更行写对 | 写表时需要 |

核心约定：**通知由表驱动**。Agent 不要把进度再手动发一遍群消息，否则重复。

## 3. 整体结构

```
飞书 Base
├── 任务表    验收标准 / 状态 / 负责人 / 优先级 / 负责人备注 / 更新日期
├── 测试表    用例 / 前置数据 / 步骤 / 期望 / 结果 / 备注 / 关联卡 / 类型
└── 变更表    功能 / 类型 / 改了什么 / 为什么 / 日期 / 版本 / 关联

feishu/changes/xxx.json ──► scripts/feishu_sync.py ──► 任务表（追加验收标准）
        （入库，跟提交走）         （调本机 lark-cli）      测试表（旧行标阻塞 / 新增行）
                                                             变更表（一条记录）
                                                             群（markdown + @，bot 身份）
```

通知分段（**不要互相补发**）：

| 触发 | 通知方 | 说明 |
|------|--------|------|
| 任务表 `状态/优先级/负责人` 被改 | Base 自动化 | 人改、Agent 改都发 |
| 测试表 `期望/步骤/前置数据/用例` 被改 | Base 自动化 | 只盯口径字段，跑用例改 `结果/备注` 不刷屏 |
| 变更行新增 | `feishu_sync.py` | 带旧目标 / 新目标 / 要跑的用例 |

> 设计要点：自动化和同步脚本**监控的字段必须错开**。`feishu_sync.py` 会写任务表的
> `验收标准`、测试表的 `备注/结果`，所以自动化不要盯这些字段，否则一次推送会发两条消息。

## 4. 一次性搭建

### 4.1 准备飞书侧

1. 建一个 Base，按上表建三张表（字段名可以改，但要和 `config.json` 对上）
2. 建一个协作群，记下 `chat_id`（`oc_` 开头）
3. 本机装好 `lark-cli` 并 `lark-cli auth status` 已登录（`--as user` 能读表、改表；
   `--as bot` 能发群消息）。权限问题按 lark-shared 的说明补 scope

### 4.2 复制到新仓库

```
scripts/feishu_sync.py            # 清单 → Base + 群通知
scripts/install_feishu_hook.py    # 把 .githooks/pre-push 装进 .git/hooks
.githooks/pre-push                # 扫 Feishu-Change trailer，失败拦推送
feishu/config.json                # Base / 表 / 群 / 字段名映射
feishu/change.example.json        # 清单模板
feishu/README.md                  # 项目内的操作说明（改口径即可）
tests/test_feishu_sync.py         # 回归测试（不联网，假 runner）
scripts/feishu_watch.py           # 可选：手动对比 Base 变更（不常驻）
tests/test_feishu_watch.py        # 可选
```

`.gitignore` 加上：

```gitignore
.feishu-sync/
feishu/local.json
```

### 4.3 改 `feishu/config.json`

```json
{
  "base_token": "<Base URL 里 /base/ 后面那段>",
  "identity": "user",
  "tables": {
    "tasks": "<任务表 table_id>",
    "tests": "<测试表 table_id>",
    "changes": "<变更表 table_id>"
  },
  "fields": {
    "task_acceptance": "验收标准",
    "task_note": "负责人备注",
    "test_result": "结果",
    "test_note": "备注",
    "change_date": "日期",
    "change_link": "关联",
    "change_version": "版本"
  },
  "notify": {
    "chat_id": "oc_xxx",
    "identity": "bot",
    "mentions": ["ou_协作者open_id"]
  }
}
```

- `notify.identity` 默认 `bot`：user 身份发群消息要额外授权 `im:message.send_as_user`，
  不能让群通知成为推送的前置条件
- `feishu/local.json`（gitignore）可覆盖本机配置，例如临时换群

### 4.4 装推送钩子

```bash
python scripts/install_feishu_hook.py
```

只复制文件、不改 `git config`；已有同名钩子会备份成 `pre-push.bak-<时间>`。
没装钩子的人推普通提交不受影响（没有 `Feishu-Change:` 就跳过）。

### 4.5 配 Base 自动化（各项目自己做一次）

两条工作流，都是 `SetRecordTrigger` + `LarkMessageAction`：

| 工作流 | 监控表 | 监控字段 |
|--------|--------|---------|
| 任务状态变更 → 群 | 任务 | `状态` / `优先级` / `负责人` |
| 测试口径变更 → 群 | 测试 | `期望` / `步骤` / `前置数据` / `用例` |

通用 JSON（把 `<TABLE_NAME>`、`<CHAT_ID>`、`<FIELD_ID>` 换成自己的）：

```json
{
  "client_token": "myproj-task-status-20260101",
  "title": "任务状态变更 → 开发组",
  "steps": [
    {
      "id": "trigger_tasks",
      "type": "SetRecordTrigger",
      "title": "任务的状态/优先级/负责人被修改",
      "next": "notify_group",
      "data": {
        "table_name": "<TABLE_NAME>",
        "record_watch_conjunction": "and",
        "record_watch_info": [],
        "field_watch_info": [
          { "field_name": "状态" },
          { "field_name": "优先级" },
          { "field_name": "负责人" }
        ],
        "trigger_control_list": [],
        "condition_list": null
      }
    },
    {
      "id": "notify_group",
      "type": "LarkMessageAction",
      "title": "发到协作群",
      "next": null,
      "data": {
        "receiver": [
          { "value_type": "group", "value": { "id": "<CHAT_ID>", "name": "<群名>" } }
        ],
        "send_to_everyone": false,
        "title": [{ "value_type": "text", "value": "任务状态变更" }],
        "content": [
          { "value_type": "ref", "value": "$.trigger_tasks.<任务名字段ID>" },
          { "value_type": "text", "value": " 现在 " },
          { "value_type": "ref", "value": "$.trigger_tasks.<状态字段ID>" }
        ],
        "btn_list": [
          {
            "text": "查看任务",
            "btn_action": "openLink",
            "link": [{ "value_type": "ref", "value": "$.trigger_tasks.recordLink" }]
          }
        ]
      }
    }
  ]
}
```

```bash
lark-cli base +workflow-create --base-token <base_token> \
  --json "$(cat workflow.json)" --dry-run --as user   # 先看请求体
lark-cli base +workflow-create --base-token <base_token> \
  --json "$(cat workflow.json)" --as user
lark-cli base +workflow-enable --base-token <base_token> \
  --workflow-id <返回的 workflow_id> --as user
```

- 新建的 workflow 默认是 `disabled`，必须再 `+workflow-enable`
- CLI **没有删除命令**，删要在飞书界面里删；不想用就先 `+workflow-disable`
- `content` 里的字段引用用**字段 ID**（`fld...`），不是字段名；`recordLink` 是触发器内置输出
- `trigger_control_list` 的语义（是「忽略这些来源」还是「只处理这些来源」）没有可靠文档，
  **不要依赖它**去过滤 API 写入；用「字段错开」来避免重复通知更稳

### 4.6 写进 `AGENTS.md`

根目录 `AGENTS.md` 写硬约定（OpenCode / Qoder 会自动读）：

```markdown
## 维护看板（硬性责任）

| 表 | 什么时候写 | 写什么 |
|----|-----------|--------|
| 任务 | 开工、交评审、完成、搁置 | 状态、负责人、更新日期 |
| 测试 | 跑完一条用例 | 结果、备注；口径变了才改期望/步骤/前置数据/用例 |
| 变更 | 用户能感觉到的行为变了 | 一行：功能/类型/改了什么/为什么/版本/关联 |

- 不要另发群消息报进度；把表改对，通知自然发生
- 不要开后台监视器 / 轮询；看板变动由维护者负责通知
- 改动做完必须回写：任务状态推进到「待评审」，测试跑过就把结果从「未跑」改掉
```

## 5. 日常流程

### 5.1 目标变了（验收口径 / 行为边界 / 测试期望）

1. 复制模板：`cp feishu/change.example.json feishu/changes/2026-01-01-short-name.json`
2. 填 `id`（幂等键）、`summary`、`old_goal` / `new_goal`
3. `task.record_id` 填任务卡；`acceptance_append` 只写新增口径（脚本会追加，不用抄旧文）
4. `tests.block[]` 填要暂停的旧用例；`tests.create[]` 填新用例
5. `change` 填变更行（`日期/版本/关联` 留空会自动补）
6. 先预览：`python scripts/feishu_sync.py --manifest <清单> --dry-run`
7. 提交并推送，提交信息带一行：

```
feat: 对话框记住上次目录

Feishu-Change: feishu/changes/2026-01-01-short-name.json
```

推送时钩子自动同步；**任何一份清单失败都会拦下推送**。离线应急：

```bash
FVP_FEISHU_SYNC=off git push   # 之后必须手动补跑同一份清单
```

### 5.2 日常状态（不需要清单）

Agent 直接改表：

- 开工：任务 `状态` → `进行中`
- 交评审：`状态` → `待评审`
- 跑完用例：`结果` → `通过/失败`，`备注` 写清做了什么
- 行为变了：变更表加一行

自动化会把这些变更推到群里，**不要再手动发**。

## 6. 脚本速查

```bash
# 同步一份清单（可重复，幂等）
python scripts/feishu_sync.py --manifest feishu/changes/x.json

# 只预览，不写库不发消息
python scripts/feishu_sync.py --manifest feishu/changes/x.json --dry-run

# 只改表不发群消息 / 忽略已同步状态重跑（可能重复建记录）
python scripts/feishu_sync.py --manifest x.json --no-notify
python scripts/feishu_sync.py --manifest x.json --force

# 可选：手动对比 Base 自上次以来改了什么（不常驻、不开后台）
python scripts/feishu_watch.py --once
```

- 幂等状态在 `.feishu-sync/state.json`（不入库），中途失败下次从断点续
- 脚本只调用本机 `lark-cli`，不读不存 token
- `feishu_watch.py` 用表级 `rev` 做闸门：rev 没动只拉 1 行，动了才拉全表逐行 diff

## 7. Windows：lark-cli 装在 WSL 时

`lark-cli` / `node` 在 nvm 里，非登录 shell 找不到，`wsl.exe -e lark-cli` 会
`execvpe(...) failed`。给脚本一个转发壳：

```powershell
$env:FVP_LARK_CLI = 'wsl.exe -e zsh -lc ''exec lark-cli "$@"'' _'
```

（WSL 的 zsh 先读 `.zshrc` 加载 nvm，再把参数原样转发。在 WSL 里直接跑 `python3`
就不需要这个变量。）

钩子里的 Python 也能覆盖：

```powershell
$env:FVP_PYTHON = "py -3.13"
```

## 8. 边界与坑

- **不要在仓库里放 webhook / app secret / token**；只依赖本机 `lark-cli` 登录态
- 没有 `lark-cli` 的机器：Agent 必须明说「未能检查飞书」，不能假装看过
- 群消息统一用 markdown；改已发消息 = 重发新版 + 删除旧消息
- 清单是**追加**语义（`acceptance_append` / `note_append`），重复跑不会叠字；
  但 `--force` 可能重复建变更行
- 幂等键是清单 `id`：改名等于新清单，会再发一次通知
- `trigger_control_list` 不要猜；用字段错开避免重复通知
- 自动化只解决「变动 → 群」；Agent 端要精确 diff 时，手动跑一次 `--once`

## 9. 换项目要改的地方

- [ ] `feishu/config.json`：Base / 表 / 群 ID、字段名映射
- [ ] `feishu/change.example.json`：测试行字段名、任务字段名
- [ ] `tests/test_feishu_sync.py`：`CONFIG` / `MANIFEST` 里的表 ID、字段名、样例
- [ ] `AGENTS.md`：三张表的写入责任、Windows 调用方式
- [ ] `CONTRIBUTING.md`：清单什么时候必须写、谁维护看板
- [ ] `.gitignore`：`.feishu-sync/`、`feishu/local.json`
- [ ] Base 自动化：监控字段换成新项目的口径字段（仍要和同步脚本写的字段错开）
- [ ] 群成员 open_id（`notify.mentions`）
- [ ] 钩子只装在「会推清单的人」机器上；其他人不装也能正常推代码
