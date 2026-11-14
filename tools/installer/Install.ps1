# Zero Hour Earth - Open World: installer.
#
# Needs: Command & Conquer Generals Zero Hour (Steam, EA app or a CD install) and Generals Vanguard (GeneralsX)
# installed with its own installer, and Python 3.10 or newer for the world server.
#
# The mod is installed into a folder of its own (by default "Zero Hour Earth - Open World" on the desktop): a full
# copy of the Vanguard install with the mod's engine, maps and world server on top. Vanguard, the game and the
# player's profile are never changed, and updating Vanguard does not touch the mod. Run this again to repair, or to
# take a newer Vanguard's data (only with a mod package built for that Vanguard release).
#
#   Install.cmd                                    (double-click; the same as below with the defaults)
#   powershell -ExecutionPolicy Bypass -File Install.ps1 [-InstallDir <dir>] [-VanguardDir <dir>] [-ZHDir <dir>]
#                                                  [-NoShortcuts] [-SkipPython]
param(
    # A folder of its own on the desktop: a full copy of Vanguard with the mod on top, so a later Vanguard update
    # never breaks the mod, and the mod never changes Vanguard.
    [string]$InstallDir = (Join-Path ([Environment]::GetFolderPath('Desktop')) 'Zero Hour Earth - Open World'),
    [string]$VanguardDir = '',
    [string]$ZHDir = '',
    [switch]$NoShortcuts,
    [switch]$SkipPython
)
$ErrorActionPreference = 'Stop'
$payload = Join-Path $PSScriptRoot 'payload'
function Say($m, $c = 'Gray') { Write-Host $m -ForegroundColor $c }
function Fail($m) { Write-Host "`nERROR: $m" -ForegroundColor Red; exit 1 }

if (-not (Test-Path "$payload\game\generalszh.exe")) { Fail "payload\game\generalszh.exe is missing - run Install.cmd from the unpacked folder." }
$modVersion = (Get-Content "$payload\VERSION.txt" -ErrorAction SilentlyContinue | Select-Object -First 1)
Say "Zero Hour Earth - Open World $modVersion" Cyan

# --- Generals Vanguard (GeneralsX) ---------------------------------------------------------------------------
function Find-Vanguard {
    $c = @()
    foreach ($hive in 'HKCU', 'HKLM') {
        $k = "${hive}:\Software\Microsoft\Windows\CurrentVersion\Uninstall\{8F3C1D42-6A21-4B7E-9F0C-2E5A7D9B4C11}_is1"
        $p = (Get-ItemProperty $k -ErrorAction SilentlyContinue).InstallLocation
        if ($p) { $c += $p }
    }
    $c += (Join-Path $env:LOCALAPPDATA 'Programs\GeneralsVanguard'), (Join-Path $env:LOCALAPPDATA 'Programs\GeneralsX')
    foreach ($d in $c) { if ($d -and (Test-Path (Join-Path $d 'd3d8.dll')) -and (Test-Path (Join-Path $d 'Data'))) { return $d.TrimEnd('\') } }
    return $null
}
if (-not $VanguardDir) { $VanguardDir = Find-Vanguard }
if (-not $VanguardDir -or -not (Test-Path (Join-Path $VanguardDir 'd3d8.dll'))) {
    Fail "Generals Vanguard (GeneralsX) was not found. Install it first (github.com/wh1ter0se69/GeneralsVanguard/releases), or pass -VanguardDir <folder>."
}
Say "Vanguard:   $VanguardDir"
$built = (Get-Content "$payload\BUILT_ON.txt" -ErrorAction SilentlyContinue | Select-Object -First 1)
if ($built) { Say "            (this mod's engine is built on Vanguard $built; its data comes from the install above)" DarkGray }

# --- Zero Hour ------------------------------------------------------------------------------------------------
function Find-ZH {
    $name = 'Command & Conquer Generals - Zero Hour'
    $c = @()
    if ($env:CNC_GENERALS_ZH_PATH) { $c += $env:CNC_GENERALS_ZH_PATH }
    $steam = (Get-ItemProperty 'HKLM:\SOFTWARE\WOW6432Node\Valve\Steam' -ErrorAction SilentlyContinue).InstallPath
    if (-not $steam) { $steam = (Get-ItemProperty 'HKCU:\Software\Valve\Steam' -ErrorAction SilentlyContinue).SteamPath }
    if ($steam) {
        $c += Join-Path $steam "steamapps\common\$name"
        $vdf = Join-Path $steam 'steamapps\libraryfolders.vdf'
        if (Test-Path $vdf) {
            foreach ($m in [regex]::Matches((Get-Content $vdf -Raw), '"path"\s+"([^"]+)"')) {
                $c += Join-Path ($m.Groups[1].Value -replace '\\\\', '\') "steamapps\common\$name"
            }
        }
    }
    foreach ($k in 'HKLM:\SOFTWARE\WOW6432Node\Electronic Arts\EA Games\Command and Conquer Generals Zero Hour',
                   'HKLM:\SOFTWARE\WOW6432Node\EA Games\Command and Conquer Generals Zero Hour') {
        $p = (Get-ItemProperty $k -ErrorAction SilentlyContinue).InstallPath
        if ($p) { $c += $p }
    }
    $c += "${env:ProgramFiles(x86)}\Steam\steamapps\common\$name"
    foreach ($d in $c) {
        if ($d -and ((Test-Path (Join-Path $d 'INIZH.big')) -or (Test-Path (Join-Path $d 'WindowZH.big')))) { return $d.TrimEnd('\') }
    }
    return $null
}
if (-not $ZHDir) { $ZHDir = Find-ZH }
if (-not $ZHDir) { Fail "Command & Conquer Generals Zero Hour was not found. Pass -ZHDir <the folder with INIZH.big>." }
Say "Zero Hour:  $ZHDir"
Say "Install to: $InstallDir"

# --- Is the game running? -----------------------------------------------------------------------------------
$running = Get-Process generalszh -ErrorAction SilentlyContinue | Where-Object { $_.Path -and $_.Path.StartsWith($InstallDir, 'OrdinalIgnoreCase') }
if ($running) { Fail 'Zero Hour Earth is running from the install folder - close the game first.' }

# --- Files ----------------------------------------------------------------------------------------------------
function Robo($from, $to, [string[]]$extra = @()) {
    robocopy $from $to /E /NFL /NDL /NJH /NJS /NP @extra | Out-Null
    if ($LASTEXITCODE -ge 8) { Fail "copying $from failed (robocopy $LASTEXITCODE)" }
}
$game = Join-Path $InstallDir 'game'
$needGB = 2.5
$drive = (Get-Item (Split-Path -Qualifier ([IO.Path]::GetFullPath($InstallDir))) -ErrorAction SilentlyContinue)
if ($drive -and -not (Test-Path $game)) {
    $free = (Get-PSDrive $drive.Name.TrimEnd(':\') -ErrorAction SilentlyContinue).Free / 1GB
    if ($free -and $free -lt $needGB) { Fail ("{0:N1} GB free, about {1} GB needed" -f $free, $needGB) }
}
New-Item -ItemType Directory -Force $InstallDir, $game, "$game\userdata" | Out-Null

Say "`n[1/5] A copy of Vanguard (about 2 GB; on a repair only changed files are copied) ..." Cyan
# everything but its uninstaller, logs and shader caches; its generalszh.exe is replaced by the mod's in step 2
Robo $VanguardDir $game @('/XF', 'unins*.exe', 'unins*.dat', '*.log', '*.dxvk-cache', '*.dxvk-ffkeys', '*.off', '/XD', 'userdata')

Say '[2/5] The mod: engine, maps, world server ...' Cyan
Robo "$payload\game" $game                      # generalszh.exe, its dlls, Maps\WorldExploration*, overrides
foreach ($d in 'worldserver', 'generator', 'tools') {
    if (Test-Path "$payload\$d") { Robo "$payload\$d" (Join-Path $InstallDir $d) @('/XD', '__pycache__', '.venv', 'cache') }
}
Copy-Item "$payload\Play OpenWorld.cmd", "$payload\README.txt", "$payload\VERSION.txt" $InstallDir -Force -ErrorAction SilentlyContinue
Copy-Item "$PSScriptRoot\Install.ps1", "$PSScriptRoot\Uninstall.ps1" $InstallDir -Force

# where the launcher finds the game (read by Play OpenWorld.cmd)
@"
@rem Written by Install.ps1. Run the installer again to change it.
set "CNC_GENERALS_ZH_PATH=$ZHDir\"
set "ZHE_VANGUARD_DIR=$VanguardDir"
"@ | Set-Content (Join-Path $InstallDir 'zhe-paths.cmd') -Encoding Ascii

# first run: the player's existing settings (resolution, volume, keys)
$profileDir = Join-Path ([Environment]::GetFolderPath('MyDocuments')) 'GeneralsX\GeneralsZH'
foreach ($f in 'Options.ini', 'GXUserKeys.ini', 'GXHud.ini', 'SagePatch.ini') {
    if ((Test-Path "$profileDir\$f") -and -not (Test-Path "$game\userdata\$f")) { Copy-Item "$profileDir\$f" "$game\userdata" }
}

# --- Python for the world server ------------------------------------------------------------------------------
Say '[3/5] Python for the world server ...' Cyan
$venvPy = Join-Path $InstallDir 'worldserver\.venv\Scripts\python.exe'
if ($SkipPython) { Say '      skipped (-SkipPython)' DarkGray }
else {
    # Native tools write warnings to stderr; under 'Stop' Windows PowerShell 5.1 turns those into terminating
    # errors. Here success is judged by $LASTEXITCODE alone.
    $ErrorActionPreference = 'Continue'
    $py = $null
    foreach ($cand in @(@('py', '-3'), @('python'), @('python3'))) {
        $exe = Get-Command $cand[0] -ErrorAction SilentlyContinue
        if (-not $exe -or $exe.Source -like '*WindowsApps*') { continue }   # the Store's stub only opens the Store
        $args0 = @($cand | Select-Object -Skip 1)
        # no quotes inside the code: Windows PowerShell 5.1 strips them when it passes arguments to a native exe
        # (no Select-Object -First here: it stops the pipeline early and the exit code becomes -1)
        $out = @(& $exe.Source @args0 -c 'import sys; print(sys.version_info[0]*100+sys.version_info[1])' 2>$null)
        $code = $LASTEXITCODE
        $v = if ($out.Count) { "$($out[0])".Trim() } else { '' }
        if ($code -eq 0 -and $v -match '^\d+$' -and [int]$v -ge 310) { $py = @($exe.Source) + $args0; break }
    }
    if (-not $py) {
        Say '      Python 3.10+ was not found. Install it (python.org, or: winget install Python.Python.3.12)' Yellow
        Say '      and run this installer again - the game runs, but World Exploration needs the world server.' Yellow
    } else {
        if (-not (Test-Path $venvPy)) {
            $pyExe = $py[0]; $pyArgs = @($py | Select-Object -Skip 1)
            & $pyExe @pyArgs -m venv (Join-Path $InstallDir 'worldserver\.venv')
            if ($LASTEXITCODE -ne 0) { Fail 'creating the Python environment failed' }
        }
        Say '      installing numpy, pillow, requests (from PyPI) ...'
        & $venvPy -m pip install --disable-pip-version-check -q -r (Join-Path $InstallDir 'worldserver\requirements.txt')
        if ($LASTEXITCODE -ne 0) { Fail 'pip install failed (is the internet reachable?)' }
    }
    $ErrorActionPreference = 'Stop'
}

# --- Shortcuts --------------------------------------------------------------------------------------------
Say '[4/5] Shortcuts ...' Cyan
if (-not $NoShortcuts) {
    $sh = New-Object -ComObject WScript.Shell
    $targets = @([Environment]::GetFolderPath('Desktop'), (Join-Path ([Environment]::GetFolderPath('Programs')) 'Zero Hour Earth'))
    foreach ($dir in $targets) {
        New-Item -ItemType Directory -Force $dir | Out-Null
        $lnk = $sh.CreateShortcut((Join-Path $dir 'Zero Hour Earth - Open World.lnk'))
        $lnk.TargetPath = Join-Path $InstallDir 'Play OpenWorld.cmd'
        $lnk.WorkingDirectory = $InstallDir
        $lnk.IconLocation = (Join-Path $game 'generalszh.exe') + ',0'
        $lnk.WindowStyle = 7
        $lnk.Save()
    }
} else { Say '      skipped' DarkGray }

Say '[5/5] Check ...' Cyan
$exe = Get-Item "$game\generalszh.exe"
$maps = @(Get-ChildItem "$game\Maps" -Directory -Filter 'WorldExploration*' -ErrorAction SilentlyContinue).Count
Say ("      engine {0:N0} bytes, {1} World Exploration maps" -f $exe.Length, $maps)
Say "`nDone. Start 'Zero Hour Earth - Open World' (desktop), then MAIN MENU > WORLD EXPLORATION." Green
Say "To update after a new Vanguard release: install the matching Zero Hour Earth package and run Install.cmd." DarkGray
