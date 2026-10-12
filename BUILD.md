# 构建说明（权威构建命令）

Release 构建统一走 GitHub Actions（`.github/workflows/release.yml`），
本文件仅记录手动后备流程。

## 环境要求

- Windows 10/11 64-bit + Python 3.13
- 依赖与仓库 `requirements.txt` 完全一致（当前 `flet[desktop]==0.86.5`，`Pillow==12.2.0`），外加 `pyinstaller`

```powershell
pip install -r requirements.txt pyinstaller
```

> 注意：不要混用其它 Pillow 大版本构建正式产物；版本变更先改 `requirements.txt` 并跑 `.github/selfcheck.py` 回归。

## 构建命令（仓库根目录执行）

```powershell
.\build_win.ps1 -Version <版本号，如 2.1.2-beta>
```

脚本内容：`pyinstaller`（`FVPTachieComposer.win.spec`，入库版本）→
三项校验（Shinku.ico 进包 / tcl·numpy·pygments·libmpv 无残留 / exe 图标非默认）→
打包 zip + 生成 `SHA256SUMS.txt`。

说明：

- 体积裁剪逻辑（excludes / DLL 同源去重 / 去 libmpv）全部收敛在
  `FVPTachieComposer.win.spec` 的 `_slim()` 与 `excludes` 中，`build_win.ps1`
  与 CI 共用同一份，保证本地/云端构建一致。
- `--icon` 写 exe 程序图标；`--add-data` 把图标打进包供运行时窗口图标读取（`_app_icon()` 走 `_MEIPASS`）。

## 打包与校验

```powershell
Compress-Archive -Path dist/FVPTachieComposer.exe -DestinationPath dist/FVPTachieComposer-v<ver>-win64.zip -Force
(Get-FileHash dist/FVPTachieComposer-v<ver>-win64.zip -Algorithm SHA256).Hash.ToLower() + "  FVPTachieComposer-v<ver>-win64.zip" | Out-File dist/SHA256SUMS.txt -Encoding ascii
```

## 拖放载入（可选，需换构建方式）

`FVP_DROPZONE=1` 时整个窗口会被包成拖放目标，拖入 `.bin` 或 HZC 文件夹即载入。
该能力由 `flet-dropzone`（内部是 Flutter 的 `desktop_drop` 插件）提供，
**插件必须编译进 Flutter 客户端**，因此：

- `flet run` / 现在的 PyInstaller 产物用的是官方桌面客户端，不认这个控件，
  所以默认关闭（环境变量未设即完全不改布局）。
- 要启用就得改走 `flet build windows`（需 Flutter SDK + VS C++ 桌面负载），
  并在构建环境里 `pip install flet-dropzone`、设 `FVP_DROPZONE=1`。
  这会替换掉本文件描述的 PyInstaller 单 exe 流程，属于发布管线变更，需单独决策。

## 构建后验证（必做）

1. `Select-String build/FVPTachieComposer/PKG-00.toc -Pattern 'Shinku.ico'` 有命中；
2. 用 `[System.Drawing.Icon]::ExtractAssociatedIcon()` 提取 exe 图标，与默认图标比对确认非 Python 默认；
3. 按 `TESTING.md` 跑冒烟清单，通过后才放行发布。
