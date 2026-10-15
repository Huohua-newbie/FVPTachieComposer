# 构建说明（权威构建命令）

发布包**只有一条链**：本机跑 `flet build windows`（见下方「拖放载入」一节）。
自 v2.2.2-beta 起不再用 GitHub Actions 在线构建——拖放插件必须编进 Flutter 客户端，
而发布产物就是这条链的产物；PyInstaller 链保留在仓库里只作本地后备，不再出现在发布页。

发布动作（本机执行）：

```powershell
pwsh .\build_flet_win.ps1 -Version <版本号，如 2.2.2-beta>
git tag -a v<版本号> -m "FVP Tachie Composer v<版本号>"; git push origin v<版本号>
gh release create v<版本号> dist\FVPTachieComposer-<版本号>-win64-flet.zip dist\SHA256SUMS.txt `
  --title "FVP Tachie Composer v<版本号>" --notes-file <面向用户的说明.md> --latest
```

> `SHA256SUMS.txt` 由构建脚本生成，只留发布页上真有的那几个包。

## 环境要求

- Windows 10/11 64-bit + Python 3.13
- 依赖与仓库 `requirements.txt` 完全一致（当前 `flet[desktop]==0.86.5`，`Pillow==12.2.0`），外加 `pyinstaller`

```powershell
pip install -r requirements.txt pyinstaller
```

> 注意：不要混用其它 Pillow 大版本构建正式产物；版本变更先改 `requirements.txt` 并跑 `.github/selfcheck.py` 回归。

## 后备链：PyInstaller 单 exe（不发布）

官方桌面客户端不认 `Dropzone`，这条链的产物没有「拖进窗口载入」。只在想要
单文件版时本地构建，发布页不放这个包。

```powershell
.\build_win.ps1 -Version <版本号，如 2.1.2-beta>
```

脚本内容：`pyinstaller`（`FVPTachieComposer.win.spec`，入库版本）→
三项校验（Shinku.ico 进包 / tcl·numpy·pygments·libmpv 无残留 / exe 图标非默认）→
打包 zip + 生成 `SHA256SUMS.txt`。

说明：

- 体积裁剪逻辑（excludes / DLL 同源去重 / 去 libmpv）全部收敛在
  `FVPTachieComposer.win.spec` 的 `_slim()` 与 `excludes` 中，`build_win.ps1`
  后备链只读这一份 spec，改体积策略不会漏改第二处。
- `--icon` 写 exe 程序图标；`--add-data` 把图标打进包供运行时窗口图标读取（`_app_icon()` 走 `_MEIPASS`）。

## 打包与校验

```powershell
Compress-Archive -Path dist/FVPTachieComposer.exe -DestinationPath dist/FVPTachieComposer-v<ver>-win64.zip -Force
(Get-FileHash dist/FVPTachieComposer-v<ver>-win64.zip -Algorithm SHA256).Hash.ToLower() + "  FVPTachieComposer-v<ver>-win64.zip" | Out-File dist/SHA256SUMS.txt -Encoding ascii
```

## 发布链：`flet build windows`（拖放载入）

把 `.bin` / HZC 文件夹直接拖进左侧「角色库」面板即载入（只有这一栏是落点）。
该能力由 `flet-dropzone`（内部是 Flutter 的 `desktop_drop` 插件）提供，
**插件必须编译进 Flutter 客户端**，
所以只有 `flet build windows` 的产物有它；`flet run` 与 PyInstaller 产物用官方桌面
客户端，不认这个控件。

开关逻辑见 `FVPTachieComposerFlet._dropzone_on()`：打包产物里 `FLET_PLATFORM` 必被
注入 → 自动启用；`FVP_DROPZONE=0/1` 可强制关/开（给 `FLET_VIEW_PATH` 指向自定义客
户端的开发态兜底）。

构建（需 Flutter SDK + VS C++ 桌面负载；**必须用 PowerShell 7**，见下）：

```powershell
pwsh .\build_flet_win.ps1 -Version <版本号，如 2.2.4-beta>
```

## 跑 `build_flet_win.ps1` 要用 pwsh，不能用 powershell

脚本是 UTF-8 无 BOM 且含中文注释。Windows PowerShell 5.1（`powershell.exe`）按系统
ANSI 代码页（中文机为 GBK）解码 `.ps1`，多字节序列会吃掉引号和换行，报一串
`表达式或语句中包含意外的标记` 的解析错误——脚本根本没执行，产物时间戳不变。
PowerShell 7（`pwsh`）默认按 UTF-8 读，才正常。

## 打包产物白屏排查（2026-10-13 实录）

现象：`build\windows\FVPTachieComposer.exe` 起来后整窗纯白，无报错、无异常。

结论：**`Dropzone` 是 `LayoutControl`，没给任何尺寸时 Dart 侧按 0 高度排版，整棵子树
静默消失**——Python 不抛异常、客户端不报错、日志一个字都没有。修法是
`Dropzone(expand=True, ...)`（见 `_drop_wrap`）。

排查时值得记住的三条：

1. **嵌入式产物有日志出口**，不是"双击就没输出"。启动脚本把 Python 的 stdout/stderr
   tee 到 `%LOCALAPPDATA%\Your Company\FVPTachieComposer\console.log`（路径由
   `FLET_APP_CONSOLE` 给出）；未捕获异常会打进这个文件并触发客户端的错误屏。
   所以"白屏 + 日志为空"= Python 活着，问题在 Dart 侧排版/建树，别去猜导入失败。
2. **不用重编 flutter 就能改代码验证**。客户端用 `importlib.util.find_spec(模块名)`
   定位入口，而 `find_spec` 对同名文件优先取 `.py` 再取 `.pyc`。于是可以：把
   `build\windows\app\<模块>.pyc` 改名挪开 → 放一个同名 `.py` 诊断壳 → 把真实源码复制
   成 `app\RealApp.py` 让壳去 `import RealApp` → 直接 `Start-Process` 跑 exe 读日志。
   验证完记得删掉 `.py` 并还原 `.pyc`。
3. **用现成的开关做对照实验**比读代码快：`FVP_DROPZONE=0` 立即正常，直接把范围锁到
   拖放层；再单独加 `expand=True` 复现正常，根因即确认。

顺带：`Problem getting monitor brightness` 是 `screen_brightness_windows` 插件的噪音，
与白屏无关。

## 构建后验证（必做）

发布链（`build\windows`）：

1. `build\windows\desktop_drop_plugin.dll` 存在（脚本已断言，缺了拖入功能是哑的）；
2. `build\windows\app\FVPTachieComposerFlet.pyc` 时间戳晚于本次改动；
3. 双击 `build\windows\FVPTachieComposer.exe`，按 `TESTING.md` 跑冒烟清单，通过后才放行发布。

后备链（PyInstaller）：

1. `Select-String build/FVPTachieComposer/PKG-00.toc -Pattern 'Shinku.ico'` 有命中；
2. 用 `[System.Drawing.Icon]::ExtractAssociatedIcon()` 提取 exe 图标，与默认图标比对确认非 Python 默认。
