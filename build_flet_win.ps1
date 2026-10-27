<#
  flet build windows 管线（替代 PyInstaller 单 exe 的另一条发布链）
  ---------------------------------------------------------------------------
  产物：build\windows\FVPTachieComposer.exe + 数据目录（非单文件），
        desktop_drop 插件被编进自定义 Flutter 客户端 → 支持把文件拖进左侧角色库面板。
  用法：.\build_flet_win.ps1 -Version 2.2.3 [-SkipFlutterDoctor]
  备注：PyInstaller 流程仍由 build_win.ps1 + FVPTachieComposer.win.spec 负责，
        两者互不影响（flet 构建只写 build\ 与 dist\ 下各自的子目录）。
#>
param(
    [Parameter(Mandatory = $true)]
    [string]$Version,
    [switch]$SkipFlutterDoctor
)

$ErrorActionPreference = "Stop"
$Repo = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Repo

# 中文 Windows 默认 GBK：flet/rich 输出含 ✅ 等字符会 UnicodeEncodeError，
# 给子进程开 UTF-8 模式（PYTHONUTF8 对 pipe/控制台都生效）
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

# Flutter SDK 不在系统 PATH，只在本会话注入。
# flet 0.86.x 要求特定 minor（如 3.44.8，校验过不了会每次构建都重新走 install_flutter），
# 优先用 flet 自己装到 %USERPROFILE%\flutter\<要求版本>\ 的那份；找不到再退回 D:\soft 的 zip 版
# （版本不符时 flet 会自行补装要求的版本）。
$RequiredFlutter = ""
try {
    $RequiredFlutter = (& py -3.13 -c "import flet.version;print(flet.version.flutter_version)").Trim()
} catch { }
$FletFlutter = if ($RequiredFlutter) {
    Join-Path $env:USERPROFILE "flutter\$RequiredFlutter\bin"
} else { "" }
if ($FletFlutter -and (Test-Path (Join-Path $FletFlutter "flutter.bat"))) {
    $FlutterBin = $FletFlutter
} else {
    $FlutterBin = "D:\soft\flutter_windows_3.47.2-stable\flutter\bin"
    if (-not (Test-Path (Join-Path $FlutterBin "flutter.bat"))) {
        throw "找不到 Flutter SDK: $FletFlutter（也无 $FlutterBin）"
    }
}
$env:PATH = "$FlutterBin;$env:PATH"
Write-Host "   Flutter SDK: $FlutterBin"

# flutter 探测引擎版本（flutter doctor / --version）依赖 git，本机 git 装在
# D:\soft\SDK\Git 且不在系统 PATH——缺了它 doctor 直接 "Unable to determine
# engine version" 构建失败。与 Flutter 同样只在本会话注入。
if (-not (Get-Command git.exe -ErrorAction SilentlyContinue)) {
    $GitBin = "D:\soft\SDK\Git\cmd"
    if (-not (Test-Path (Join-Path $GitBin "git.exe"))) {
        throw "找不到 git.exe（PATH 与 $GitBin 均无），Flutter 引擎版本探测会失败"
    }
    $env:PATH = "$GitBin;$env:PATH"
    Write-Host "   Git: $GitBin"
}

# --build-name 只吃 x.y.z，-beta 只保留在 tag 与包名里
$BuildName = ($Version -split "-")[0]

# ---- flet 客户端补丁 ----------------------------------------------------
# flet 0.86.5 客户端把「桌面 + 位置参数」当成 developer mode（args[0] 当 page
# URL，嵌入 Python 不启动），Windows 拖文件到 exe 图标正踩这条 → 打包版卡死
# 启动屏。client\main.dart 是打好补丁的渲染版（头部 FVP-PATCH 标记说明根因），
# 在渲染产物上覆盖它。flet 只在模板 hash 变化时重渲染 build\flutter，所以
# 平时预铺即生效；干净树首次构建会先渲染未补丁版，由构建后校验兜底重打。
$RenderedMain = Join-Path $Repo "build\flutter\lib\main.dart"
$PatchedMain = Join-Path $Repo "client\main.dart"
if (-not (Test-Path $PatchedMain)) { throw "客户端补丁缺失: $PatchedMain" }
function Test-MainPatched {
    (Test-Path $RenderedMain) -and
    (Select-String -Path $RenderedMain -Pattern "FVP-PATCH" -Quiet)
}
if ((Test-Path $RenderedMain) -and -not (Test-MainPatched)) {
    Copy-Item $PatchedMain $RenderedMain -Force
    Write-Host "   客户端补丁已应用: build\flutter\lib\main.dart"
}

Write-Host "== flet build windows  (version=$Version, build-name=$BuildName)"
$buildArgs = @(
    "-3.13", "-m", "flet.cli", "build", "windows", ".",
    "--module-name", "FVPTachieComposerFlet",
    "--build-version", $BuildName,
    "--yes"
)
if ($SkipFlutterDoctor) { $buildArgs += "--skip-flutter-doctor" }

& py @buildArgs
if ($LASTEXITCODE -ne 0) { throw "flet build 失败 (exit $LASTEXITCODE)" }

# 模板刚渲染（干净树 / 模板 hash 变了）会把补丁冲掉：重打后重建一次。
# 此时模板 hash 已稳定，第二次构建不会再渲染，直接编译补丁版。
if (-not (Test-Path $RenderedMain)) { throw "渲染产物缺失: $RenderedMain" }
if (-not (Test-MainPatched)) {
    Write-Host "   客户端补丁被模板渲染冲掉，重打补丁后重建" -ForegroundColor Yellow
    Copy-Item $PatchedMain $RenderedMain -Force
    & py @buildArgs
    if ($LASTEXITCODE -ne 0) { throw "flet 重建(带补丁)失败 (exit $LASTEXITCODE)" }
    if (-not (Test-MainPatched)) { throw "补丁再次被冲掉，检查 flet 模板渲染逻辑" }
}
Write-Host "   客户端补丁在编译源中生效: FVP-PATCH"

# ---- 产物校验 ----------------------------------------------------------
$OutDir = Join-Path $Repo "build\windows"
$Exe = Join-Path $OutDir "FVPTachieComposer.exe"
if (-not (Test-Path $Exe)) { throw "构建产物缺失: $Exe" }

# desktop_drop / flet_dropzone 必须真的编进客户端，否则拖入功能是哑的。
# 判据用产物根目录的 desktop_drop_plugin.dll（插件原生代码编进客户端的直接证据）
# —— flutter_assets\packages 只收「带 assets 的包」，desktop_drop 没有 assets 会漏判。
$DropDll = Join-Path $OutDir "desktop_drop_plugin.dll"
if (-not (Test-Path $DropDll)) { throw "客户端里没有 desktop_drop 插件 DLL，拖入不会生效: $DropDll" }
$GenPlugins = Join-Path $Repo "build\flutter\windows\flutter\generated_plugins.cmake"
if ((Test-Path $GenPlugins) -and
    -not (Select-String -Path $GenPlugins -Pattern "desktop_drop" -Quiet)) {
    throw "generated_plugins.cmake 未注册 desktop_drop: $GenPlugins"
}
Write-Host "   拖放插件已编入: desktop_drop_plugin.dll"

# 应用源码（编译后的 .pyc 或原始 .py）必须进了 python-app
$PyApp = Join-Path $Repo "build\python-app"
$Entry = Get-ChildItem -Path $PyApp -Recurse -File -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -like "FVPTachieComposerFlet.*" }
if (-not $Entry) { throw "python-app 里没有入口模块: $PyApp" }
Write-Host "   入口模块已打包: $($Entry[0].Name)"

# 图标（flutter_launcher_icons 从 assets\icon.png 生成）
$RunnerIcon = Join-Path $Repo "build\flutter\windows\runner\resources\app_icon.ico"
if (-not (Test-Path $RunnerIcon)) { throw "应用图标未生成: $RunnerIcon" }
Write-Host "   图标已生成: app_icon.ico"

# ---- 剔除 app\ 里的开发期杂物 ------------------------------------------
# flet 把整个项目目录拷进 <exe>\app\，仓库根的一切都跟着进包：这次改日志
# 落点后尤其明显（app\logs\ 里是开发机自己的日志，还有 .pytest_cache、
# 截图目录、构建日志）。运行时真正需要的只有 fvpapp\*.pyc、入口 pyc 和
# Shinku.ico（theme._app_icon 从 app\ 根读它），其余按名单剔除。
$AppDir = Join-Path $OutDir "app"
$StripNames = @(
    "logs", ".pytest_cache", "__pycache__", "图片", "client", "tests", ".github",
    ".gitignore", "build_flet_win.ps1", "build_win.ps1",
    "CONTRIBUTING.md", "RELEASING.md", "README.md", "BUILD.md", "TESTING.md",
    "pyproject.toml", "requirements.txt"
)
if (Test-Path $AppDir) {
    $stripped = @()
    foreach ($n in $StripNames) {
        $p = Join-Path $AppDir $n
        if (Test-Path $p) {
            Remove-Item -Recurse -Force $p
            $stripped += $n
        }
    }
    Get-ChildItem -Path $AppDir -Filter "*.log" -File -ErrorAction SilentlyContinue | ForEach-Object {
        Remove-Item -Force $_.FullName
        $stripped += $_.Name
    }
    # 开发机跑 python 留下的 __pycache__（cpython-310/313 等）也整个带进包；
    # 运行时用的是 app\fvpapp\*.pyc（flet 自己编译的那份），这些缓存纯属垃圾。
    Get-ChildItem -Path $AppDir -Recurse -Directory -Filter "__pycache__" -Force -ErrorAction SilentlyContinue |
        ForEach-Object {
            Remove-Item -Recurse -Force $_.FullName
            $stripped += "pycache"
        }
    Write-Host "   已剔除 app\ 杂物: $(if ($stripped) { ($stripped | Select-Object -Unique) -join ', ' } else { '无' })"

    # 剔除过头就把包废了，宁可不打包也别发一个起不来的包
    foreach ($need in @("fvpapp", "Shinku.ico")) {
        if (-not (Test-Path (Join-Path $AppDir $need))) {
            throw "运行时需要 $need，但它不在 $AppDir —— 剔除名单配错了"
        }
    }
    if (-not (Get-ChildItem -Path $AppDir -Recurse -File |
              Where-Object { $_.Name -like "FVPTachieComposerFlet.*" })) {
        throw "入口模块不在 app\ 里 —— 剔除名单配错了"
    }
}

# ---- 打包 + 校验和 -----------------------------------------------------
$DistDir = Join-Path $Repo "dist"
New-Item -ItemType Directory -Force -Path $DistDir | Out-Null

# flet 产物是"exe + 数据目录"，与 PyInstaller 单 exe 包名区分开，避免互相覆盖
$ZipName = "FVPTachieComposer-$Version-win64-flet.zip"
$ZipPath = Join-Path $DistDir $ZipName
if (Test-Path $ZipPath) { Remove-Item $ZipPath -Force }
Compress-Archive -Path (Join-Path $OutDir "*") -DestinationPath $ZipPath -CompressionLevel Optimal
if (-not (Test-Path $ZipPath)) { throw "打包失败: $ZipPath" }

$Hash = (Get-FileHash -Algorithm SHA256 -Path $ZipPath).Hash.ToLower()
$SumsPath = Join-Path $DistDir "SHA256SUMS.txt"
$Entry = "$Hash  $ZipName"
$Lines = @()
if (Test-Path $SumsPath) {
    $Lines = @(Get-Content -Path $SumsPath |
        Where-Object { $_ -notmatch [regex]::Escape($ZipName) })
}
$Lines += $Entry
Set-Content -Path $SumsPath -Value ($Lines | Sort-Object) -Encoding ascii

$Size = [math]::Round((Get-Item $ZipPath).Length / 1MB, 1)
Write-Host "== 完成: $ZipPath  ($Size MB)"
Write-Host "   校验和已写入 $SumsPath"
