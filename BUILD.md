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
pyinstaller --onefile --noconfirm --noconsole `
  --name FVPTachieComposer `
  --icon Shinku.ico `
  --add-data "Shinku.ico;." `
  --distpath dist --workpath build --specpath build `
  FVPTachieComposerFlet.py
```

说明：

- `--icon` 写 exe 程序图标；`--add-data` 把图标打进包供运行时窗口图标读取（`_app_icon()` 走 `_MEIPASS`）。
- 相对路径写法仅在**仓库根目录**执行时有效。经 WSL 调用 Windows Python 时 cwd 解析异常，需改用绝对路径（历史坑，CI 无此问题）。

## 打包与校验

```powershell
Compress-Archive -Path dist/FVPTachieComposer.exe -DestinationPath dist/FVPTachieComposer-v<ver>-win64.zip -Force
(Get-FileHash dist/FVPTachieComposer-v<ver>-win64.zip -Algorithm SHA256).Hash.ToLower() + "  FVPTachieComposer-v<ver>-win64.zip" | Out-File dist/SHA256SUMS.txt -Encoding ascii
```

## 构建后验证（必做）

1. `Select-String build/FVPTachieComposer/PKG-00.toc -Pattern 'Shinku.ico'` 有命中；
2. 用 `[System.Drawing.Icon]::ExtractAssociatedIcon()` 提取 exe 图标，与默认图标比对确认非 Python 默认；
3. 按 `TESTING.md` 跑冒烟清单，通过后才放行发布。
