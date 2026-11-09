# Agent 工作约定

本项目是 FVP 角色立绘查看 / 合成工具（Flet 桌面端），目前由维护者一人开发。
功能说明只认 [`README.md`](README.md)，协作流程见 [`CONTRIBUTING.md`](CONTRIBUTING.md)。

## 开工前必须做（每次会话）

1. 同步 main：`git switch main && git pull`；如果已在任务分支，先 `git rebase origin/main`
2. 读 `README.md` 里和当前任务有关的小节
3. 口径不清楚就直接问维护者，别自己猜；聊天里定下来的结论要回写 `README.md`

## 改完之后

- 用户能感觉到的行为变了：同一个提交里改 `README.md`
- 推送前按 [`TESTING.md`](TESTING.md) 跑无头自检（`python tests/run_all.py`），全绿再推
- 不要提交日志、截图、`build/`、`dist/`、`.venv/`

## 边界

- 不要开后台监视器 / 轮询
- 不要把 token、webhook、app secret 写进仓库或 GitHub Secrets
- 结论只认 `README.md` 和代码；聊天记录不算数