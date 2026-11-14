# Zero Hour Earth - Open World: uninstaller. Removes the install folder and its shortcuts; Vanguard, the game and
# Documents\GeneralsX are not touched. Saves and settings of the mod live in <install>\game\userdata and go with it,
# unless -KeepUserData.
#
#   powershell -ExecutionPolicy Bypass -File Uninstall.ps1 [-KeepUserData]
param([switch]$KeepUserData)
$ErrorActionPreference = 'Stop'
$dir = $PSScriptRoot
if (-not (Test-Path (Join-Path $dir 'zhe-paths.cmd'))) { Write-Host "$dir does not look like a Zero Hour Earth install"; exit 1 }
if (Get-Process generalszh -ErrorAction SilentlyContinue | Where-Object { $_.Path -and $_.Path.StartsWith($dir, 'OrdinalIgnoreCase') }) {
    Write-Host 'Close the game first.'; exit 1
}
Get-Process python, pythonw -ErrorAction SilentlyContinue | Where-Object { $_.Path -and $_.Path.StartsWith($dir, 'OrdinalIgnoreCase') } | Stop-Process -Force

foreach ($lnk in (Join-Path ([Environment]::GetFolderPath('Desktop')) 'Zero Hour Earth - Open World.lnk'),
                 (Join-Path ([Environment]::GetFolderPath('Programs')) 'Zero Hour Earth')) {
    if (Test-Path $lnk) { Remove-Item $lnk -Recurse -Force }
}
if ($KeepUserData -and (Test-Path "$dir\game\userdata")) {
    $keep = Join-Path ([Environment]::GetFolderPath('MyDocuments')) 'ZeroHourEarth-userdata'
    robocopy "$dir\game\userdata" $keep /E /NFL /NDL /NJH /NJS /NP | Out-Null
    Write-Host "user data kept in $keep"
}
Set-Location $env:TEMP
Remove-Item $dir -Recurse -Force
Write-Host 'Zero Hour Earth - Open World removed.'
