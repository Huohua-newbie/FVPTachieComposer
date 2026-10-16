# 发布节奏与版本号

## 版本号：`MAJOR.MINOR.PATCH`

| 位 | 什么时候 +1 | 例子 |
| --- | --- | --- |
| **PATCH** | 纯 bug 修复、小调整 | `2.2.2` → `2.2.3` |
| **MINOR** | 新增功能，向后兼容 | `2.2.x` → `2.3.0` |
| **MAJOR** | 不兼容的破坏性变更 | `2.x` → `3.0.0` |

- 正式版本号**不带** `-beta`；只有明确还不稳定的预览版才用 Pre-release（并加 `-beta`）。
- 版本号改 `pyproject.toml` 的 `[project].version`，同时同步 `BUILD.md` 示例与 `build_flet_win.ps1` 头部注释。

## 发布节奏

- **日常 commit 不打 tag、不发 Release**：小修复合并进 `main` 即可。
- **攒够再发**：约 1–2 周，或攒够 3–5 个有意义的改动，才切一次版本并发布。
- **紧急线上问题**：可以立刻单独发一个 PATCH hotfix。
- **拿不准稳不稳**：先发 Pre-release；稳定后再 `gh release edit <tag> --prerelease=false --latest=true` 转正式。

一句话：**代码随时合并，Release 按需发布。**

## 发一次 Release

见 `BUILD.md` 的「发布动作」章节：`build + tag` → 建 Draft → `curl --http1.1` 传资产 → 放行并核对。

## 硬规则

- 已公开的 Release **永不** `--clobber` 覆盖同名资产；要改就发新版本号。
- **删 Release 不动 tag**（tag 是历史存档）。
- Release 页按创建时间倒序；补发旧版本要按「先旧后新」重建，否则顺序会乱。
