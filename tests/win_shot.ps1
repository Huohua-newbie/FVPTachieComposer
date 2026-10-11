# 开发用：在 Windows 侧启动真机预览并截屏，绕过 WSLg 抓不到 Flutter 窗口的限制。
# 用法： powershell -NoProfile -ExecutionPolicy Bypass -File tests\win_shot.ps1 -Out C:\Public\shot.png
param(
    [string]$Out = "C:\Users\Public\fvp_shot.png",
    [string]$Python = "C:\Users\Administrator\AppData\Local\Programs\Python\Python313\python.exe",
    [string]$Title = "FVP Tachie Composer",
    [int]$TimeoutSec = 120
)

$ErrorActionPreference = "Stop"
$proj = Split-Path -Parent $PSScriptRoot
$log = Join-Path $env:TEMP "fvp_shot_out.log"
$err = Join-Path $env:TEMP "fvp_shot_err.log"
Remove-Item $log, $err -ErrorAction SilentlyContinue

$env:FVP_PREVIEW_HOLD = "45"
$p = Start-Process -FilePath $Python `
    -ArgumentList "`"$proj\tests\win_preview.py`"" `
    -WorkingDirectory $proj `
    -RedirectStandardOutput $log -RedirectStandardError $err `
    -PassThru

Add-Type @"
using System;
using System.Text;
using System.Runtime.InteropServices;
public class W {
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumWindowsProc cb, IntPtr l);
  public delegate bool EnumWindowsProc(IntPtr h, IntPtr l);
  [DllImport("user32.dll")] public static extern int GetWindowTextW(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out R r);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  [StructLayout(LayoutKind.Sequential)] public struct R { public int L, T, Rt, B; }
}
"@

$script:hwnd = [IntPtr]::Zero
$cb = [W+EnumWindowsProc] {
    param($h, $l)
    $sb = New-Object System.Text.StringBuilder 256
    [void][W]::GetWindowTextW($h, $sb, 256)
    if ($sb.ToString() -like "*$Title*" -and [W]::IsWindowVisible($h)) {
        $script:hwnd = $h
        return $false
    }
    return $true
}

$deadline = (Get-Date).AddSeconds($TimeoutSec)
while ((Get-Date) -lt $deadline) {
    if ($p.HasExited) { break }
    [void][W]::EnumWindows($cb, [IntPtr]::Zero)
    if ($script:hwnd -ne [IntPtr]::Zero) { break }
    Start-Sleep -Milliseconds 700
}

if ($script:hwnd -eq [IntPtr]::Zero) {
    Write-Output "NO_WINDOW_BY_TITLE exited=$($p.HasExited) — 可见窗口列表："
    $dump = @()
    $cbDump = [W+EnumWindowsProc] {
        param($h, $l)
        $sb = New-Object System.Text.StringBuilder 256
        [void][W]::GetWindowTextW($h, $sb, 256)
        if ([W]::IsWindowVisible($h) -and $sb.Length -gt 0) { $script:dump += "$($sb.ToString())" }
        return $true
    }
    [void][W]::EnumWindows($cbDump, [IntPtr]::Zero)
    $dump | ForEach-Object { Write-Output "  WIN: $_" }
    # 兜底：直接截主屏（应用是前台窗口）
    $script:hwnd = [IntPtr](-1)
}

Add-Type -AssemblyName System.Windows.Forms, System.Drawing

if ($script:hwnd -eq [IntPtr](-1)) {
    $bds = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds
    $x, $y, $w, $hh = $bds.X, $bds.Y, $bds.Width, $bds.Height
} else {
    Start-Sleep -Seconds 8
    [void][W]::SetForegroundWindow($script:hwnd)
    Start-Sleep -Milliseconds 800
    $r = New-Object W+R
    [void][W]::GetWindowRect($script:hwnd, [ref]$r)
    $x, $y, $w, $hh = $r.L, $r.T, ($r.Rt - $r.L), ($r.B - $r.T)
}

$bmp = New-Object System.Drawing.Bitmap($w, $hh)
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.CopyFromScreen($x, $y, 0, 0, (New-Object System.Drawing.Size($w, $hh)))
$bmp.Save($Out, [System.Drawing.Imaging.ImageFormat]::Png)
$g.Dispose(); $bmp.Dispose()

Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
Write-Output "SHOT_SAVED $Out ${w}x${hh}"
if (Test-Path $log) { Get-Content $log -Tail 5 }
if (Test-Path $err) { Write-Output "--- STDERR ---"; Get-Content $err -Tail 25 }
