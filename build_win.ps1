#Requires -Version 5
<#
.SYNOPSIS  FVPTachieComposer Windows 一键构建（含体积裁剪与产物校验）
.EXAMPLE   .\build_win.ps1 -Version 2.1.2-beta
#>
param([Parameter(Mandatory = $true)][string]$Version)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$zip = "FVPTachieComposer-$Version-win64.zip"

Write-Host "== PyInstaller =="
$py = $null
foreach ($c in @('py -3.13', 'python')) {
    try {
        $parts = $c -split ' ', 2
        if ($parts.Count -eq 2) { & $parts[0] $parts[1] -m PyInstaller --version | Out-Null }
        else { & $c -m PyInstaller --version | Out-Null }
        $py = $c; break
    } catch { }
}
if (-not $py) { throw '找不到带 PyInstaller 的 Python（需要 py -3.13 或 python -m PyInstaller 可用）' }
Write-Host "使用解释器: $py"
$parts = $py -split ' ', 2
if ($parts.Count -eq 2) { & $parts[0] $parts[1] -m PyInstaller --noconfirm FVPTachieComposer.win.spec --distpath dist --workpath build }
else { & $py -m PyInstaller --noconfirm FVPTachieComposer.win.spec --distpath dist --workpath build }

Write-Host "== 校验: Shinku.ico 进入运行时资源 =="
Select-String -Path build/FVPTachieComposer/PKG-00.toc -Pattern 'Shinku.ico' | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Shinku.ico missing from PKG datas' }

Write-Host "== 校验: 体积裁剪生效(tcl/numpy/pygments/libmpv 不应残留) =="
$names = Get-Content build/FVPTachieComposer/PKG-00.toc -Raw
foreach ($pat in @('tcl\', 'tkinter', 'numpy', 'pygments', "'rich'", 'libmpv')) {
    if ($names -match [regex]::Escape($pat)) { throw "裁剪失效, 仍含: $pat" }
}

Write-Host "== 校验: exe 图标非默认 =="
Add-Type -AssemblyName System.Drawing
function Get-IconPng($exe) {
    $ms = New-Object IO.MemoryStream
    [System.Drawing.Icon]::ExtractAssociatedIcon($exe).ToBitmap().Save($ms, [System.Drawing.Imaging.ImageFormat]::Png)
    return $ms.ToArray()
}
$a = Get-IconPng "$PSScriptRoot/dist/FVPTachieComposer.exe"
$b = Get-IconPng "$env:SystemRoot/System32/notepad.exe"
if ([BitConverter]::ToString($a) -eq [BitConverter]::ToString($b)) { throw 'exe icon check failed' }

Write-Host "== 打包 + 校验文件 =="
Compress-Archive -Path dist/FVPTachieComposer.exe -DestinationPath "dist/$zip" -Force
$h = (Get-FileHash "dist/$zip" -Algorithm SHA256).Hash.ToLower()
"$h  $zip" | Out-File dist/SHA256SUMS.txt -Encoding ascii

$mb = [math]::Round((Get-Item "dist/$zip").Length / 1MB, 1)
Write-Host "DONE: dist/$zip  ($mb MB)"
