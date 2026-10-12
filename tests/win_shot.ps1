# 开发用：在 Windows 侧启动真机预览并截屏，绕过 WSLg 抓不到 Flutter 窗口的限制。
# 用法： powershell -NoProfile -ExecutionPolicy Bypass -File tests\win_shot.ps1 -Out C:\Public\shot.png
param(
    [string]$Out = "C:\Users\Public\fvp_shot.png",
    [string]$Python = "C:\Users\Administrator\AppData\Local\Programs\Python\Python313\python.exe",
    [string]$Title = "FVP Tachie Composer",
    [string]$Scenario = "tree",
    [string]$Target = "win_preview.py",
    [int]$TimeoutSec = 120
)

$ErrorActionPreference = "Stop"
$proj = Split-Path -Parent $PSScriptRoot
$log = Join-Path $env:TEMP "fvp_shot_out.log"
$err = Join-Path $env:TEMP "fvp_shot_err.log"
Remove-Item $log, $err -ErrorAction SilentlyContinue

$env:FVP_PREVIEW_HOLD = "45"
$env:FVP_PREVIEW_SCENARIO = $Scenario
$p = Start-Process -FilePath $Python `
    -ArgumentList "`"$proj\tests\$Target`"" `
    -WorkingDirectory $proj `
    -RedirectStandardOutput $log -RedirectStandardError $err `
    -PassThru

Add-Type @"
using System;
using System.Text;
using System.Runtime.InteropServices;
public class W {
  [DllImport("user32.dll", CharSet=System.Runtime.InteropServices.CharSet.Unicode)] public static extern bool EnumWindows(EnumWindowsProc cb, IntPtr l);
  public delegate bool EnumWindowsProc(IntPtr h, IntPtr l);
  [DllImport("user32.dll", CharSet=System.Runtime.InteropServices.CharSet.Unicode)] public static extern int GetWindowTextW(IntPtr h, StringBuilder s, int n);
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
    if ($sb.ToString() -like "*$Title*" -and $sb.ToString() -notlike "*DevTools*" -and [W]::IsWindowVisible($h)) {
        $script:hwnd = $h
        return $false
    }
    return $true
}

$deadline = (Get-Date).AddSeconds($TimeoutSec)
while ((Get-Date) -lt $deadline) {
    if ($p.HasExited) { break }
    [void][W]::EnumWindows($cb, [IntPtr]::Zero)
    # 光有窗口不够：状态是脚本摆的，必须等它自己报 PREVIEW_READY，否则截到的是半成品
    $ready = (Test-Path $log) -and (Select-String -Path $log -Pattern "PREVIEW_READY|DIAG_READY" -Quiet)
    if ($script:hwnd -ne [IntPtr]::Zero -and $ready) { break }
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

# Flutter 桌面端是硬件合成的，CopyFromScreen 有时只会抓到一块白。
# 先走 PrintWindow(PW_RENDERFULLCONTENT)，全白时再退回屏幕抓取。
Add-Type -ReferencedAssemblies System.Drawing -TypeDefinition @"
using System;
using System.Drawing;
using System.Drawing.Imaging;
using System.Runtime.InteropServices;
public class Shot {
  [DllImport("user32.dll")] static extern bool PrintWindow(IntPtr h, IntPtr hdc, int flags);
  public static Bitmap Capture(IntPtr hwnd, int w, int h) {
    Bitmap bmp = new Bitmap(w, h, PixelFormat.Format32bppArgb);
    using (Graphics g = Graphics.FromImage(bmp)) {
      IntPtr hdc = g.GetHdc();
      try { PrintWindow(hwnd, hdc, 2); } finally { g.ReleaseHdc(hdc); }
    }
    return bmp;
  }
  public static bool Blank(Bitmap bmp) {
    Color c0 = bmp.GetPixel(bmp.Width / 2, bmp.Height / 2);
    for (int i = 0; i < 40; i++) {
      Random r = new Random(i);
      Color c = bmp.GetPixel(r.Next(1, bmp.Width - 1), r.Next(1, bmp.Height - 1));
      if (Math.Abs(c.R - c0.R) + Math.Abs(c.G - c0.G) + Math.Abs(c.B - c0.B) > 12) return false;
    }
    return true;
  }
}
"@

if ($script:hwnd -eq [IntPtr](-1)) {
    $bds = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds
    $x, $y, $w, $hh = $bds.X, $bds.Y, $bds.Width, $bds.Height
} else {
    Start-Sleep -Seconds 5
    [void][W]::SetForegroundWindow($script:hwnd)
    Start-Sleep -Milliseconds 800
    $r = New-Object W+R
    [void][W]::GetWindowRect($script:hwnd, [ref]$r)
    $x, $y, $w, $hh = $r.L, $r.T, ($r.Rt - $r.L), ($r.B - $r.T)
}

$bmp = $null
$how = "printwindow"
if ($script:hwnd -ne [IntPtr](-1)) {
    $bmp = [Shot]::Capture($script:hwnd, $w, $hh)
    if ([Shot]::Blank($bmp)) { $bmp.Dispose(); $bmp = $null; $how = "screen" }
}
if (-not $bmp) {
    $how = "screen"
    $bmp = New-Object System.Drawing.Bitmap($w, $hh)
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.CopyFromScreen($x, $y, 0, 0, (New-Object System.Drawing.Size($w, $hh)))
    $g.Dispose()
}
$bmp.Save($Out, [System.Drawing.Imaging.ImageFormat]::Png)
$bmp.Dispose()

Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
Write-Output "SHOT_SAVED $Out ${w}x${hh} via=$how"
if (Test-Path $log) { Get-Content $log -Tail 5 }
if (Test-Path $err) { Write-Output "--- STDERR ---"; Get-Content $err -Tail 25 }
