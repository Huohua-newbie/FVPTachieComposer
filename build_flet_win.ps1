<#
  flet build windows 管线（替代 PyInstaller 单 exe 的另一条发布链）
  ---------------------------------------------------------------------------
  产物：build\windows\FVPTachieComposer.exe + 数据目录（非单文件），
        desktop_drop 插件被编进自定义 Flutter 客户端 → 支持把文件拖进左侧角色库面板。
  用法：.\build_flet_win.ps1 -Version 2.2.3-beta [-SkipFlutterDoctor]
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

# --build-name 只吃 x.y.z，-beta 只保留在 tag 与包名里
$BuildName = ($Version -split "-")[0]

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
