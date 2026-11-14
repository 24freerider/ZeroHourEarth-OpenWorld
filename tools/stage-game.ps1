# Stage the built engine into ..\game, a self-contained run folder next to (never inside) the Steam install
# and the installed GeneralsX. Safe to run again after every build: only the engine files are replaced.
#
#   powershell -ExecutionPolicy Bypass -File tools\stage-game.ps1
param(
    # Vanguard 0.9.74+ installs to Programs\GeneralsVanguard; older installs (and updates over them) stay in GeneralsX
    [string]$Installed = $(if (Test-Path (Join-Path $env:LOCALAPPDATA 'Programs\GeneralsVanguard\d3d8.dll')) {
        Join-Path $env:LOCALAPPDATA 'Programs\GeneralsVanguard' } else { Join-Path $env:LOCALAPPDATA 'Programs\GeneralsX' }),
    [switch]$SkipPatch          # do not (re)copy the 1.2 GB GeneralsXPatch folder
)
$ErrorActionPreference = 'Stop'
$root  = Split-Path -Parent $PSScriptRoot
$build = Join-Path $root 'engine\build\win64'
$zh    = Join-Path $build 'GeneralsMD\RelWithDebInfo'
$game  = Join-Path $root 'game'

foreach ($f in @("$zh\generalszh.exe", "$zh\zlib1.dll", "$build\_deps\openal_soft-build\RelWithDebInfo\OpenAL32.dll")) {
    if (-not (Test-Path $f)) { throw "missing $f - run tools\build-engine.ps1 first" }
}
if (-not (Test-Path "$Installed\d3d8.dll")) { throw "the installed GeneralsX was not found at $Installed (DXVK d3d8/d3d9 come from it)" }

New-Item -ItemType Directory -Force $game, "$game\userdata" | Out-Null

# engine
Copy-Item "$zh\generalszh.exe", "$zh\zlib1.dll" $game -Force
if (Test-Path "$zh\generalszh.pdb") { Copy-Item "$zh\generalszh.pdb" $game -Force }
Copy-Item "$build\_deps\openal_soft-build\RelWithDebInfo\OpenAL32.dll" $game -Force

# runtime pieces the engine does not build: DXVK pair and its settings, OpenAL settings
foreach ($f in 'd3d8.dll', 'd3d9.dll', 'dxvk.conf', 'alsoft.ini', 'LICENSE.md', 'THIRD_PARTY_NOTICES.txt') {
    if (Test-Path "$Installed\$f") { Copy-Item "$Installed\$f" $game -Force }
}

# data shipped with GeneralsX: loose Data\ and the community patch archives
robocopy "$Installed\Data" "$game\Data" /E /NFL /NDL /NJH /NJS /NP | Out-Null
if ($LASTEXITCODE -ge 8) { throw "copying Data failed ($LASTEXITCODE)" }
if (-not $SkipPatch) {
    robocopy "$Installed\GeneralsXPatch" "$game\GeneralsXPatch" /E /XF *.off /NFL /NDL /NJH /NJS /NP | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "copying GeneralsXPatch failed ($LASTEXITCODE)" }
}

# our own loose data (menus, the world exploration base map, ...) overrides everything above
$overlay = Join-Path $root 'gamedata'
if (Test-Path $overlay) {
    robocopy $overlay $game /E /NFL /NDL /NJH /NJS /NP | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "copying gamedata failed ($LASTEXITCODE)" }
}

# first run: start from the player's existing settings (resolution, volume, key bindings)
$profile = Join-Path ([Environment]::GetFolderPath('MyDocuments')) 'GeneralsX\GeneralsZH'
foreach ($f in 'Options.ini', 'GXUserKeys.ini', 'GXHud.ini', 'SagePatch.ini') {
    if ((Test-Path "$profile\$f") -and -not (Test-Path "$game\userdata\$f")) { Copy-Item "$profile\$f" "$game\userdata" }
}

$exe = Get-Item "$game\generalszh.exe"
Write-Host ("staged {0}  {1:N0} bytes  {2}" -f $exe.FullName, $exe.Length, $exe.LastWriteTime)
exit 0
