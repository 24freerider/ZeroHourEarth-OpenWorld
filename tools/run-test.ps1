# Developer helper: start the staged game windowed, wait, save pictures of its window, optionally stop it.
#
#   powershell -ExecutionPolicy Bypass -File tools\run-test.ps1 -Seconds 40 -Shot logs\menu.png
#   powershell -ExecutionPolicy Bypass -File tools\run-test.ps1 -Server -Place "Poltar" -Drive 0 -Seconds 90 -Shot logs\drive.png -ShotAt 40,60,80
param(
    [int]$Seconds = 40,
    [string]$Shot = "",
    [string]$GameArgs = "-win -xres 1600 -yres 900",
    [switch]$Keep,                       # leave the game (and the server) running
    [switch]$Server,                     # start the world server first
    [string]$Place = "",                 # ZHE_AUTOSTART: open World Exploration at this place without a click
    [string]$Drive = "",                 # ZHE_AUTODRIVE: keep driving on this heading in degrees (0 = east)
    [string]$ShotAt = "",                # "15 30 45": seconds after the start at which to save numbered pictures (with -Shot)
    [string]$Spawn = "",                 # ZHE_SPAWN: free (RTS / free camera, the default) or vehicle (-Drive implies it)
    [int]$Window = 0,                    # ZHE_WINDOW: odd window size in chunks (0 = the menu's default, 7)
    [string]$Pan = "",                   # ZHE_AUTOPAN: glide the camera on this heading in degrees (0 = east)
    [string]$PanSpeed = "",              # ZHE_AUTOPANSPEED: the glide's speed in units per second (default 160)
    [string]$PanBack = "",               # ZHE_AUTOPANBACK: turn the glide round after this many seconds
    [string]$ClickAt = "",               # "15:1000,450 18:R800,450": at second 15 a left click (R: right) at client x,y
    [switch]$Overlay,                    # ZHE_OVERLAY: the debug overlay on
    [switch]$FreeCam                     # ZHE_FREECAM: the free camera on from the start
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$game = Join-Path $root 'game'
$env:CNC_GENERALS_ZH_PATH = "C:\Program Files (x86)\Steam\steamapps\common\Command & Conquer Generals - Zero Hour\"
$env:GX_USERDATA_DIR = Join-Path $game 'userdata'
$env:ZHE_WORLD_ROOT = $root
$env:ZHE_AUTOSTART = $Place
$env:ZHE_AUTODRIVE = $Drive
if (-not $Spawn -and $Drive) { $Spawn = 'vehicle' }
$env:ZHE_SPAWN = $Spawn
$env:ZHE_WINDOW = if ($Window -gt 0) { "$Window" } else { "" }
$env:ZHE_AUTOPAN = $Pan
$env:ZHE_AUTOPANSPEED = $PanSpeed
$env:ZHE_AUTOPANBACK = $PanBack
$env:ZHE_OVERLAY = if ($Overlay) { '1' } else { '' }
$env:ZHE_FREECAM = if ($FreeCam) { '1' } else { '' }

Add-Type -AssemblyName System.Drawing
Add-Type @"
using System;
using System.Runtime.InteropServices;
public static class ZheWin {
    [StructLayout(LayoutKind.Sequential)] public struct RECT { public int L, T, R, B; }
    [DllImport("user32.dll")] public static extern bool GetClientRect(IntPtr h, out RECT r);
    [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
    [DllImport("user32.dll")] public static extern bool PrintWindow(IntPtr h, IntPtr hdc, uint flags);
    [StructLayout(LayoutKind.Sequential)] public struct POINT { public int X, Y; }
    [DllImport("user32.dll")] public static extern bool ClientToScreen(IntPtr h, ref POINT p);
    [DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
    [DllImport("user32.dll")] public static extern void mouse_event(uint f, uint dx, uint dy, uint d, UIntPtr e);
}
"@
[void][ZheWin]::SetProcessDPIAware()

function Save-WindowShot([Diagnostics.Process]$p, [string]$path) {
    # PrintWindow with PW_RENDERFULLCONTENT asks the compositor for the window's own picture, so it works while the
    # window is behind others and never copies anything else that happens to be on the screen.
    $p.Refresh()
    $h = $p.MainWindowHandle
    if ($h -eq [IntPtr]::Zero) { Write-Host 'no game window to capture'; return $false }
    $r = New-Object ZheWin+RECT
    [void][ZheWin]::GetClientRect($h, [ref]$r)
    $w = $r.R - $r.L; $hgt = $r.B - $r.T
    if ($w -le 0 -or $hgt -le 0) { Write-Host 'game window has no client area'; return $false }
    $bmp = New-Object System.Drawing.Bitmap $w, $hgt
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $hdc = $g.GetHdc()
    $ok = [ZheWin]::PrintWindow($h, $hdc, 3)      # PW_CLIENTONLY | PW_RENDERFULLCONTENT
    $g.ReleaseHdc($hdc)
    $g.Dispose()
    New-Item -ItemType Directory -Force (Split-Path -Parent $path) | Out-Null
    $bmp.Save($path, [System.Drawing.Imaging.ImageFormat]::Png)
    $bmp.Dispose()
    Write-Host "saved $path (${w}x${hgt})"
    return $ok
}

$srv = $null
if ($Server) {
    $py = Join-Path $root 'worldserver\.venv\Scripts\python.exe'
    $srv = Start-Process -FilePath $py -ArgumentList '-X', 'utf8', "`"$(Join-Path $root 'worldserver\server.py')`"" `
        -WorkingDirectory (Join-Path $root 'worldserver') -PassThru -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $root 'logs\server-out.txt') -RedirectStandardError (Join-Path $root 'logs\server-err.txt')
    Start-Sleep -Seconds 4
}

function Start-Game {
    Start-Process -FilePath (Join-Path $game 'generalszh.exe') -ArgumentList $GameArgs -WorkingDirectory $game -PassThru `
        -RedirectStandardError (Join-Path $root 'logs\run-err.txt') -RedirectStandardOutput (Join-Path $root 'logs\run-out.txt')
}
$p = Start-Game
# The first start of a newly built exe ends by itself within a second or two; the second start runs.
Start-Sleep -Seconds 4
if ($p.HasExited) { Write-Host 'first start ended at once, starting again'; $p = Start-Game }

$started = Get-Date
$deadline = $started.AddSeconds($Seconds)
$pending = [Collections.Generic.List[int]]::new()
foreach ($s in ($ShotAt -split '[^0-9]+' | Where-Object { $_ } | ForEach-Object { [int]$_ } | Sort-Object)) { $pending.Add($s) }
$clicks = [Collections.Generic.List[object]]::new()
foreach ($c in ($ClickAt -split '\s+' | Where-Object { $_ })) {
    if ($c -match '^(\d+):(R?)(\d+),(\d+)$') {
        $clicks.Add([pscustomobject]@{ At = [int]$matches[1]; Right = ($matches[2] -eq 'R'); X = [int]$matches[3]; Y = [int]$matches[4] })
    }
}
while ((Get-Date) -lt $deadline -and -not $p.HasExited) {
    Start-Sleep -Milliseconds 250
    if ($clicks.Count -gt 0 -and ((Get-Date) - $started).TotalSeconds -ge $clicks[0].At) {
        $c = $clicks[0]; $clicks.RemoveAt(0)
        $p.Refresh(); $h = $p.MainWindowHandle
        $pt = New-Object ZheWin+POINT; $pt.X = $c.X; $pt.Y = $c.Y
        [void][ZheWin]::ClientToScreen($h, [ref]$pt)
        [void][ZheWin]::SetForegroundWindow($h); Start-Sleep -Milliseconds 300
        [void][ZheWin]::SetCursorPos($pt.X, $pt.Y); Start-Sleep -Milliseconds 300
        $down, $up = if ($c.Right) { 0x0008, 0x0010 } else { 0x0002, 0x0004 }
        [ZheWin]::mouse_event($down, 0, 0, 0, [UIntPtr]::Zero); Start-Sleep -Milliseconds 80
        [ZheWin]::mouse_event($up, 0, 0, 0, [UIntPtr]::Zero)
        Write-Host "clicked $(if ($c.Right) {'right'} else {'left'}) at $($c.X),$($c.Y) (second $($c.At))"
    }
    if ($Shot -and $pending.Count -gt 0 -and ((Get-Date) - $started).TotalSeconds -ge $pending[0]) {
        $at = $pending[0]; $pending.RemoveAt(0)
        $numbered = ($Shot -replace '\.png$', '') + "-$at.png"
        [void](Save-WindowShot $p (Join-Path $root $numbered))
    }
}
if ($p.HasExited) {
    Write-Host "the game exited early with code $($p.ExitCode)"
} else {
    if ($Shot) { [void](Save-WindowShot $p (Join-Path $root $Shot)) }
    if (-not $Keep) { Stop-Process -Id $p.Id -Force }
}
if ($srv -and -not $Keep -and -not $srv.HasExited) { Stop-Process -Id $srv.Id -Force }
Write-Host "game pid $($p.Id)$(if ($srv) { ", server pid $($srv.Id)" })"
