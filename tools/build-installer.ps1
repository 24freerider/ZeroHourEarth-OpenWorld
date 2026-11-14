# Pack Zero Hour Earth - Open World into an installer package: dist\ZeroHourEarth-OpenWorld-<version>.zip
# (unpack, double-click Install.cmd). Run after tools\build-engine.ps1.
#
#   powershell -ExecutionPolicy Bypass -File tools\build-installer.ps1 [-WithPdb] [-NoZip]
param(
    [switch]$WithPdb,     # ship generalszh.pdb (crash dumps with symbols; much bigger)
    [switch]$NoZip        # leave the folder, skip the zip
)
$ErrorActionPreference = 'Stop'
$root  = Split-Path -Parent $PSScriptRoot
$build = Join-Path $root 'engine\build\win64'
$zh    = Join-Path $build 'GeneralsMD\RelWithDebInfo'
foreach ($f in "$zh\generalszh.exe", "$zh\zlib1.dll", "$build\_deps\openal_soft-build\RelWithDebInfo\OpenAL32.dll") {
    if (-not (Test-Path $f)) { throw "missing $f - run tools\build-engine.ps1 first" }
}

$git = (Get-Command git -ErrorAction SilentlyContinue).Source
if (-not $git) { $git = (Get-ChildItem "$env:LOCALAPPDATA\GitHubDesktop\app-*\resources\app\git\cmd\git.exe" | Sort-Object FullName -Descending | Select-Object -First 1).FullName }
$engine = Join-Path $root 'engine'
$commit = (& $git -C $engine rev-parse --short HEAD)
$branch = (& $git -C $engine rev-parse --abbrev-ref HEAD)
$base   = (& $git -C $engine describe --tags --abbrev=0 HEAD)
if (& $git -C $engine status --porcelain --untracked-files=no) { Write-Warning 'engine\ has uncommitted changes: the package will not match a commit' }
$version = '{0}-{1}-{2}' -f (Get-Date -Format 'yyyy.MM.dd'), ($base -replace '^v', 'vanguard'), $commit

$name = "ZeroHourEarth-OpenWorld-$version"
$out  = Join-Path $root "dist\$name"
if (Test-Path $out) { Remove-Item $out -Recurse -Force }
$pl = Join-Path $out 'payload'
New-Item -ItemType Directory -Force "$pl\game", "$pl\worldserver", "$pl\generator" | Out-Null

function Robo($from, $to, [string[]]$extra = @()) {
    robocopy $from $to /E /NFL /NDL /NJH /NJS /NP @extra | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "copying $from failed ($LASTEXITCODE)" }
}

# engine
Copy-Item "$zh\generalszh.exe", "$zh\zlib1.dll", "$build\_deps\openal_soft-build\RelWithDebInfo\OpenAL32.dll" "$pl\game"
if ($WithPdb -and (Test-Path "$zh\generalszh.pdb")) { Copy-Item "$zh\generalszh.pdb" "$pl\game" }
# our loose data: the World Exploration maps (with their map.ini) and whatever else gamedata\ overrides
Robo (Join-Path $root 'gamedata') "$pl\game"
# world server and the generator modules it imports (no caches, no venv, no builds)
Robo (Join-Path $root 'worldserver') "$pl\worldserver" @('/XD', '__pycache__', '.venv', 'cache', '/XF', '*.pyc', 'config.json')
Robo (Join-Path $root 'generator') "$pl\generator" @('/XD', '__pycache__', 'build', 'dist', '.venv', '/XF', '*.pyc', '*.exe', '*.spec')

# installer itself
$inst = Join-Path $PSScriptRoot 'installer'
Copy-Item "$inst\Install.cmd", "$inst\Install.ps1", "$inst\Uninstall.ps1", "$inst\README.txt" $out
Copy-Item "$inst\Play OpenWorld.cmd", "$inst\README.txt" $pl
Set-Content "$pl\VERSION.txt" $version -Encoding Ascii
Set-Content "$pl\BUILT_ON.txt" $base -Encoding Ascii
Set-Content "$out\VERSION.txt" "$version`r`nengine branch $branch, commit $commit, Generals Vanguard $base" -Encoding Ascii

$size = (Get-ChildItem $out -Recurse -File | Measure-Object Length -Sum).Sum / 1MB
Write-Host ("package folder {0}  ({1:N0} MB)" -f $out, $size)
if (-not $NoZip) {
    $zip = "$out.zip"
    if (Test-Path $zip) { Remove-Item $zip -Force }
    Compress-Archive -Path $out -DestinationPath $zip -CompressionLevel Optimal
    Write-Host ("zip {0}  ({1:N0} MB)" -f $zip, ((Get-Item $zip).Length / 1MB))
}
