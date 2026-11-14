# Move the Zero Hour Earth mod onto a new Generals Vanguard (GeneralsX) release.
#
#   powershell -ExecutionPolicy Bypass -File tools\port-to-upstream.ps1 -Tag v0.9.75            # check only
#   powershell -ExecutionPolicy Bypass -File tools\port-to-upstream.ps1 -Tag v0.9.75 -Merge     # make the branch
#
# What it does:
#   1. fetches the tag from origin (github.com/wh1ter0se69/GeneralsVanguard) - nothing is pushed;
#   2. a dry-run merge (git merge-tree, the working tree is not touched) lists the files that conflict;
#   3. with -Merge: creates branch openworld-<ver> from the tag and merges the mod branch into it (no commit),
#      leaving the conflicts for you to resolve. Then: build, test, commit.
# Read port\PORTING.md first: it names the files that conflict every time and how each was resolved before.
param(
    [Parameter(Mandatory = $true)][string]$Tag,      # e.g. v0.9.75
    [string]$From = '',                              # the mod branch to move; default: the newest openworld* branch
    [switch]$Merge
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$engine = Join-Path $root 'engine'

$git = (Get-Command git -ErrorAction SilentlyContinue).Source
if (-not $git) {
    $gd = Get-ChildItem "$env:LOCALAPPDATA\GitHubDesktop\app-*\resources\app\git\cmd\git.exe" -ErrorAction SilentlyContinue |
        Sort-Object FullName -Descending | Select-Object -First 1
    if ($gd) { $git = $gd.FullName } else { throw 'git not found (install Git or GitHub Desktop)' }
}
function G { & $git -C $engine @args; if ($LASTEXITCODE -ne 0) { throw "git $args failed ($LASTEXITCODE)" } }

if (& $git -C $engine status --porcelain) { throw 'engine\ has uncommitted changes - commit or stash them first' }

if (-not $From) {
    $From = (& $git -C $engine for-each-ref --sort=-committerdate --format='%(refname:short)' 'refs/heads/openworld*' | Select-Object -First 1)
}
Write-Host "mod branch: $From"

G fetch -q origin "refs/tags/${Tag}:refs/tags/${Tag}" --no-tags
$ver = $Tag -replace '^v0\.9\.', '0' -replace '[^0-9A-Za-z]', ''
$branch = "openworld-$ver"

$current = (& $git -C $engine describe --tags --abbrev=0 $From)
Write-Host "the mod sits on $current; upstream since then:"
& $git -C $engine log --oneline "$current..$Tag"

Write-Host "`ndry-run merge of $From into $Tag ..."
$out = & $git -C $engine merge-tree --write-tree --name-only --no-messages $Tag $From
$conflicts = @($out | Select-Object -Skip 1 | Where-Object { $_ })
if ($conflicts.Count -eq 0) { Write-Host 'no conflicts' -ForegroundColor Green }
else {
    Write-Host "$($conflicts.Count) file(s) conflict:" -ForegroundColor Yellow
    $conflicts | ForEach-Object { "  $_" }
}

if (-not $Merge) { Write-Host "`n(check only - run again with -Merge to create $branch)"; exit 0 }

G checkout -q -b $branch $Tag
& $git -C $engine -c merge.conflictStyle=diff3 merge --no-ff --no-commit $From
Write-Host "`nbranch $branch created, merge staged. Resolve the conflicts (diff3 markers, see port\PORTING.md), then:"
Write-Host '  powershell -ExecutionPolicy Bypass -File tools\build-engine.ps1 *> logs\build.txt'
Write-Host "  git commit -F <message file>     (message: 'Merge $From into Generals Vanguard $Tag')"
