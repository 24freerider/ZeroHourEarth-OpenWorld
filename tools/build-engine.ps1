# Configure and build the Zero Hour engine (GeneralsXZH / generalszh.exe) with the win64 preset.
#
#   powershell -ExecutionPolicy Bypass -File tools\build-engine.ps1            # incremental build
#   powershell -ExecutionPolicy Bypass -File tools\build-engine.ps1 -Reconfigure
#
# Needs: Visual Studio 2022 Build Tools (C++ x64), tools\vcpkg (bootstrapped), any git.exe.
param(
    [switch]$Reconfigure,
    [string]$Target = "z_generals"
)
$ErrorActionPreference = 'Stop'
$root   = Split-Path -Parent $PSScriptRoot
$engine = Join-Path $root 'engine'

# --- MSVC environment (vcvars64) imported into this PowerShell process
$vswhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
$vs = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
if (-not $vs) { throw 'Visual Studio Build Tools with the C++ x64 toolset were not found.' }
$vcvars = Join-Path $vs 'VC\Auxiliary\Build\vcvars64.bat'
cmd /c "`"$vcvars`" >nul 2>&1 && set" | ForEach-Object {
    if ($_ -match '^([^=]+)=(.*)$') { Set-Item -Path "env:$($Matches[1])" -Value $Matches[2] }
}

# --- git: PATH first, then the copy GitHub Desktop ships
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    $g = Get-ChildItem "$env:LOCALAPPDATA\GitHubDesktop" -Recurse -Filter git.exe -ErrorAction SilentlyContinue |
         Where-Object { $_.FullName -like '*\cmd\git.exe' } | Sort-Object FullName -Descending | Select-Object -First 1
    if (-not $g) { throw 'git.exe was not found (install Git or GitHub Desktop).' }
    $env:PATH = "$($g.DirectoryName);$env:PATH"
}

$env:VCPKG_ROOT = Join-Path $PSScriptRoot 'vcpkg'
$env:VCPKG_DISABLE_METRICS = '1'
if (-not (Test-Path (Join-Path $env:VCPKG_ROOT 'vcpkg.exe'))) { throw "vcpkg is not bootstrapped at $env:VCPKG_ROOT" }

Set-Location $engine
$configured = Test-Path (Join-Path $engine 'build\win64\build-RelWithDebInfo.ninja')
if ($Reconfigure -or -not $configured) {
    Write-Host '== cmake --preset win64'
    cmake --preset win64
    if ($LASTEXITCODE -ne 0) { throw "configure failed ($LASTEXITCODE)" }
}
Write-Host "== cmake --build --preset win64 --target $Target"
$sw = [Diagnostics.Stopwatch]::StartNew()
cmake --build --preset win64 --target $Target
$code = $LASTEXITCODE
Write-Host ("== build exit {0} after {1:N0} s" -f $code, $sw.Elapsed.TotalSeconds)
exit $code
