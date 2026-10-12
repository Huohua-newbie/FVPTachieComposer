# 开发用：按进程抓 Flutter 窗口。GDI 的 CopyFromScreen 抓不到硬件合成的 Flutter 表面，
# 所以这里把顶层窗口和它的所有子窗口都试一遍 PrintWindow，挑出真正有内容的那张。
param(
    [int]$Pid_ = 0,
    [string]$Title = "",
    [string]$Out = "C:\Users\Public\fvp_grab.png",
    [switch]$List
)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Windows.Forms, System.Drawing

Add-Type -TypeDefinition @'
using System;
using System.Text;
using System.Runtime.InteropServices;
public class G {
  public delegate bool EnumProc(IntPtr h, IntPtr l);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern bool EnumWindows(EnumProc cb, IntPtr l);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern bool EnumChildWindows(IntPtr p, EnumProc cb, IntPtr l);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetWindowTextW(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out R r);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetClassName(IntPtr h, StringBuilder s, int n);
  [StructLayout(LayoutKind.Sequential)] public struct R { public int L, T, Rt, B; }
}
'@

Add-Type -ReferencedAssemblies System.Drawing -TypeDefinition @'
using System;
using System.Drawing;
using System.Drawing.Imaging;
using System.Runtime.InteropServices;
public class Cap {
  [DllImport("user32.dll")] static extern bool PrintWindow(IntPtr h, IntPtr hdc, int flags);
  public static Bitmap Window(IntPtr hwnd, int w, int h) {
    Bitmap bmp = new Bitmap(w, h, PixelFormat.Format32bppArgb);
    using (Graphics g = Graphics.FromImage(bmp)) {
      IntPtr hdc = g.GetHdc();
      try { PrintWindow(hwnd, hdc, 2); } finally { g.ReleaseHdc(hdc); }
    }
    return bmp;
  }
  public static int Spread(Bitmap b) {
    // 取样点之间的最大色差：全白/全黑都接近 0，有内容才会拉开
    int min = 0xFFFFFF, max = 0;
    Random r = new Random(7);
    for (int i = 0; i < 200; i++) {
      Color c = b.GetPixel(r.Next(1, b.Width - 1), r.Next(1, b.Height - 1));
      int v = (c.R << 16) | (c.G << 8) | c.B;
      if (v < min) min = v;
      if (v > max) max = v;
    }
    return max - min;
  }
}
'@

if ($List) {
    $all = @()
    $lcb = [G+EnumProc] {
        param($h, $l)
        if ([G]::IsWindowVisible($h)) {
            $sb = New-Object System.Text.StringBuilder 256
            [void][G]::GetWindowTextW($h, $sb, 256)
            $pid2 = 0
            [void][G]::GetWindowThreadProcessId($h, [ref]$pid2)
            $r = New-Object G+R
            [void][G]::GetWindowRect($h, [ref]$r)
            if ($sb.Length -gt 0) {
                $script:all += ("{0} | pid={1} | {2}x{3}" -f $sb.ToString(), $pid2, ($r.Rt - $r.L), ($r.B - $r.T))
            }
        }
        return $true
    }
    [void][G]::EnumWindows($lcb, [IntPtr]::Zero)
    $all | ForEach-Object { Write-Output "WIN $_" }
    exit 0
}

$targets = @()
$cb = [G+EnumProc] {
    param($h, $l)
    $pid2 = 0
    [void][G]::GetWindowThreadProcessId($h, [ref]$pid2)
    if (-not [G]::IsWindowVisible($h)) { return $true }
    $sb = New-Object System.Text.StringBuilder 256
    [void][G]::GetWindowTextW($h, $sb, 256)
    $name = $sb.ToString()
    $byTitle = ($Title -ne "" -and $name -like "*$Title*")
    $byPid = ($Pid_ -ne 0 -and [int]$pid2 -eq $Pid_)
    if ($byTitle -or $byPid) {
        $script:targets += [pscustomobject]@{ Hwnd = $h; Title = $name }
    }
    return $true
}
[void][G]::EnumWindows($cb, [IntPtr]::Zero)
if ($targets.Count -eq 0) { Write-Output "NO_TOP_WINDOW pid=$Pid_ title=$Title"; exit 1 }

$children = @()
foreach ($t in $targets) {
    $ccb = [G+EnumProc] {
        param($h, $l)
        if ([G]::IsWindowVisible($h)) {
            $r = New-Object G+R
            [void][G]::GetWindowRect($h, [ref]$r)
            $cn = New-Object System.Text.StringBuilder 256
            [void][G]::GetClassName($h, $cn, 256)
            $script:children += [pscustomobject]@{
                Hwnd = $h; W = ($r.Rt - $r.L); H = ($r.B - $r.T); Class = $cn.ToString()
            }
        }
        return $true
    }
    [void][G]::EnumChildWindows($t.Hwnd, $ccb, [IntPtr]::Zero)
}

$best = $null; $bestSpread = -1
foreach ($c in @($targets | ForEach-Object { @{ Hwnd = $_.Hwnd } }) + $children) {
    $h = $c.Hwnd
    $r = New-Object G+R
    [void][G]::GetWindowRect($h, [ref]$r)
    $w = $r.Rt - $r.L; $hh = $r.B - $r.T
    if ($w -lt 40 -or $hh -lt 40) { continue }
    [void][G]::SetForegroundWindow($h)
    Start-Sleep -Milliseconds 300
    try {
        $bmp = [Cap]::Window($h, $w, $hh)
        $sp = [Cap]::Spread($bmp)
        Write-Output ("CAND hwnd={0} {1}x{2} spread={3}" -f $h, $w, $hh, $sp)
        if ($sp -gt $bestSpread) {
            if ($best) { $best.Dispose() }
            $best = $bmp; $bestSpread = $sp
        } else { $bmp.Dispose() }
    } catch { Write-Output "CAND_FAIL $h $_" }
}

if (-not $best) { Write-Output "NO_CAP"; exit 1 }
$best.Save($Out, [System.Drawing.Imaging.ImageFormat]::Png)
$best.Dispose()
Write-Output "GRAB_SAVED $Out best_spread=$bestSpread children=$($children.Count)"
