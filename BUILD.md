# 构建说明（权威构建命令）

发布包**只有一条链**：本机跑 `flet build windows`（见下方「拖放载入」一节）。
自 v2.2.2-beta 起不再用 GitHub Actions 在线构建——拖放插件必须编进 Flutter 客户端，
而发布产物就是这条链的产物；PyInstaller 链保留在仓库里只作本地后备，不再出现在发布页。

发布动作（本机执行）：

```powershell
pwsh .\build_flet_win.ps1 -Version <版本号，如 2.2.3>
git tag -a v<版本号> -m "FVP Tachie Composer v<版本号>"; git push origin v<版本号>
```

**先建 Draft，再单独传资产**——`gh release create` 若带上 41 MB 的 zip 一起提交，
上传一旦失败会连 Release 一起回滚（实测报 `HTTP 408`）：

```powershell
gh release create v<版本号> --title "FVP Tachie Composer v<版本号>" `
  --notes-file <面向用户的说明.md> --prerelease --latest=false --draft
```

传资产：**别用 `gh release upload`**。它默认走 HTTP/2，经本机代理
（WSL 下 `http_proxy=172.27.48.1:7890`）传 40 MB 的包会中途静默卡死，最后报
`HTTP 408: Upload body timed out due to inactivity (https://uploads.github.com/…)`；
而几百字节的 `SHA256SUMS.txt` 反而能过，于是经常只传上去一半。
改用 `curl --http1.1` 直传，十几秒完成（实测 14–15 s，`HTTP 201`）：

```bash
TOKEN=$(~/.local/gh/bin/gh auth token)
RID=$(~/.local/gh/bin/gh release view v<版本号> -R Huohua-newbie/FVPTachieComposer --json databaseId --jq .databaseId)
for f in dist/FVPTachieComposer-<版本号>-win64-flet.zip dist/SHA256SUMS.txt; do
  curl --http1.1 -sS -X POST \
    -H "Authorization: Bearer $TOKEN" \
    -H "Accept: application/vnd.github+json" \
    --data-binary @"$f" \
    "https://uploads.github.com/repos/Huohua-newbie/FVPTachieComposer/releases/$RID/assets?name=$(basename "$f")" \
    -o /dev/null -w "$(basename "$f") HTTP=%{http_code} %{size_upload}B %{time_total}s\n"
done
```

资产到位后再放行，并核对：

```powershell
gh release edit v<版本号> --draft=false
gh release view v<版本号> --json isDraft,isPrerelease,tagName,assets --jq '{isDraft,isPrerelease,tagName,assets:[.assets[]|{name,size}]}'
```

核对点：`isDraft=false`、`isPrerelease=true`、zip 与 `SHA256SUMS.txt` 都在、zip 的
`size` 与本地一致。

> `SHA256SUMS.txt` 由构建脚本生成，只留发布页上真有的那几个包。
> 本机 `gh` 在 `~/.local/gh/bin/gh`（未在 PATH 时用全路径）；仓库 remote 是 SSH，
> 不带 `-R Huohua-newbie/FVPTachieComposer` 时 `gh` 会报
> `none of the git remotes … point to a known GitHub host`。
> **顺序：先旧后新**——GitHub Release 页按创建时间倒序排，先建新版会把旧版顶到下面。

### 大资产上传超时（`HTTP 408 Upload body timed out`）

40MB 的 zip 传到一半卡住、GitHub 报 `HTTP 408: Upload body timed out due to inactivity`
时，基本都是**当前网络的上行带宽太小**（有线网上行极低会稳定复现）。别被下载速度误导：
下行可以很快，上行却是瓶颈；`SHA256SUMS.txt` 才几百字节所以总能传上去，容易误判成
「GitHub 抽风」或代理问题。

判据是先测上行基准：

```bash
curl --noproxy '*' --max-time 20 -sS -o /dev/null \
  -w 'HTTP=%{http_code} speed=%{speed_upload}B/s\n' \
  -X POST --data-binary @dist/<你的 zip> https://speed.cloudflare.com/__up
```

速度只有几十 KB/s 就是本地上行的问题（正常应 ≥1MB/s）。此时**换网络重传**即可——
实测手机热点一次成功（41MB / 15s）；不要折腾代理、`--http1.1`、`Expect:` 这些开关。

## 启动屏（开机到 UI 之间那一屏）

`flet build windows` 的产物在 Python 起来之前会先渲染一个启动屏。不配置就吃 Flet
默认值（`theme_mode` 跟随系统、不画转圈、不显示文字）——系统是深色时就是一块纯黑，
紧接着跳成应用的浅色 UI，观感很突兀。

启动屏在 `pyproject.toml` 里配置，选项要放进**以屏名命名的子表**（内置屏名是 `flet`）：

```toml
[tool.flet.boot_screen.flet]
theme_mode = "light"          # 与应用默认（FVPTachieComposerFlet.py 的 is_dark=False）一致
spinner_size = 26
bgcolor_light = "#eff1f4"     # 取值对齐 _palette()，保证与 UI 同底色
spinner_color_light = "#3f6ee0"
text_color_light = "#6b7488"
prepare_message = "正在准备…"
startup_message = "正在载入界面…"
fade_out_duration = 200       # 毫秒，淡出而不是硬切
```

- 内置屏只支持颜色 / 转圈 / 文字，**不支持图片 logo**；`--splash-*` 系列 CLI 开关只对
  iOS / Android / web 生效，Windows 桌面不吃。
- 改完必须重新 `flet build windows` 才会进包，改 `pyproject.toml` 不影响已构建产物。
- 应用默认主题在 `FVPTachieComposerFlet.py` 的 `__init__`（`self.is_dark = False`）。
  若哪天把默认改成深色，这里的 `theme_mode` / `bgcolor_*` 要跟着改，否则又会闪。

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
pwsh .\build_flet_win.ps1 -Version <版本号，如 2.2.3>
```

## 客户端补丁：打包态位置参数（`client\main.dart`，2026-10-23 实录）

现象：把 `.bin` 拖到 `FVPTachieComposer.exe` 图标（或任何带位置参数启动）后，
窗口永远停在启动屏；无参数双击一切正常，dev 态带参数也正常。

根因在 flet 客户端 Dart 侧，不在 Python：`client/lib/main.dart` 把
「桌面平台 + 收到位置参数」一律当成 developer mode——`args[0]` 被当作 page URL
去连 WebSocket，并且不构建 `_ProdApp`，嵌入 Python 从头到尾不启动。
诊断构建实测带参数时入口 `main()` 根本不进。Windows 拖文件到 exe 图标
正是以位置参数拉起进程，所以必踩。

修法是仓库里的 `client\main.dart`（打好补丁的渲染版，头部 `FVP-PATCH` 注释
说明细节）：用编译期烧进 app.so 的 `pythonModuleName` 区分打包态与通用
`flet run` 客户端，打包态位置参数照常走 production，`runPythonApp` 本就把
args 转发给嵌入解释器，Python 侧 `sys.argv` 原样可见——「拖到 exe 图标启动即
载入」因此恢复可用。

`build_flet_win.ps1` 负责铺设与守护：构建前把补丁覆盖到 `build\flutter\lib\main.dart`
（flet 只在模板 hash 变化时重渲染，预铺即生效）；构建后校验 `FVP-PATCH` 标记还在，
干净树首建/模板更新会把补丁冲掉，脚本自动重打并重建一次。
升级 flet 版本时要重新核对补丁的三处条件与新版模板的差异。

## flet 会把仓库根整个拷进 `app\`，打包前按名单剔除

`flet build windows` 的 python-app 阶段不是「只收包」，而是把**整个项目目录**拷成
`build\windows\app\`。于是 `.pytest_cache\`、截图目录、构建日志、`CONTRIBUTING.md`
这些开发期文件全部跟着进 zip；把日志改到应用根目录之后更明显——开发机自己跑出来的
`logs\*.log` 也被原样发给用户（2.3.0 首次实测 `app\logs\` 里躺着 3 个日志）。

`build_flet_win.ps1` 在打 zip 前按 `$StripNames` 剔除，并校验 `fvpapp\`、入口 pyc、
`Shinku.ico` 仍在——运行时需要的只有这三样（`theme._app_icon()` 从 `app\` 根读图标），
剔除名单配错了直接 `throw`，不产出起不来的包。

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

## jni 插件编译失败：CMake 缓存里的 JDK 路径过期（2026-10-23 实录）

现象：改了 Dart 侧代码后首次重建，`dartjni.c : error C1083: 无法打开包括文件 jni.h`；
之前几次只改 Python 的构建全部正常（没触发原生重编，坏缓存一直没暴露）。

根因：`build\flutter\build\windows\x64\CMakeCache.txt` 是老的 `flutter doctor`/构建
生成的，`FIND_PACKAGE_MESSAGE_DETAILS_JNI` 记的是当时的 `JAVA_HOME`（`D:/soft`）。
JDK 后来搬到 `D:\soft\SDK\JDK25`，缓存里的 `D:/soft/include` 成了死路径。

处理：删掉 `build\flutter\build\windows\x64\CMakeCache.txt` 与同目录 `CMakeFiles\`
重新配置即可（纯中间产物，flutter 会自己重建）。若 `flutter doctor` 报
`Unable to determine engine version`，是本会话 PATH 里没有 git——
`build_flet_win.ps1` 已自动注入 `D:\soft\SDK\Git\cmd`。

## 构建后验证（必做）

发布链（`build\windows`）：

1. `build\windows\desktop_drop_plugin.dll` 存在（脚本已断言，缺了拖入功能是哑的）；
2. `build\windows\app\FVPTachieComposerFlet.pyc` 时间戳晚于本次改动；
3. `build\flutter\lib\main.dart` 含 `FVP-PATCH` 标记（脚本已断言，丢了就是带参数启动卡启动屏的老 bug）；
4. 双击 `build\windows\FVPTachieComposer.exe`，按 `TESTING.md` 跑冒烟清单，通过后才放行发布；
5. 把某个 `.bin` 拖到 exe 图标上，应正常进 UI 且该资源自动载入。

后备链（PyInstaller）：

1. `Select-String build/FVPTachieComposer/PKG-00.toc -Pattern 'Shinku.ico'` 有命中；
2. 用 `[System.Drawing.Icon]::ExtractAssociatedIcon()` 提取 exe 图标，与默认图标比对确认非 Python 默认。
