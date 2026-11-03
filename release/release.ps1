# Release process for the Valheim Mod Installer only. Each plugin has its own release script
# (..\<Plugin>\release\Release.bat) and is released separately.
#
#   release\Release.bat                 (double-click)
#   powershell -File release\release.ps1 [-BuildOnly] [-Bump patch|minor|major] [-Changes "text"]
#
# 1. Checks the repo is committed and pushed to origin/main.
# 2. Bumps APP_VERSION (patch by default) in valheim_mod_installer.pyw.
# 3. Commits "Release vX.Y.Z" and pushes it to origin/main.
# 4. Runs release\make_release.py: builds ValheimModInstaller.zip into release\out and publishes it.
#
# -BuildOnly skips steps 1-3 and publishing: it just builds the zip with the current version.

param(
    [switch]$BuildOnly,
    [ValidateSet('patch', 'minor', 'major')][string]$Bump = 'patch',
    [string]$Changes = ''      # extra text for the release commit message
)

$ErrorActionPreference = 'Stop'
$installerDir = Split-Path -Parent $PSScriptRoot
$utf8 = New-Object System.Text.UTF8Encoding($false)

function Step($text) { Write-Host "`n== $text" -ForegroundColor Cyan }
function Fail($text) { Write-Host "`nRELEASE STOPPED: $text" -ForegroundColor Red; exit 1 }

function Find-Exe($name, $fallbacks) {
    $c = Get-Command $name -ErrorAction SilentlyContinue
    if ($c) { return $c.Source }
    foreach ($f in $fallbacks) { if (Test-Path $f) { return $f } }
    return $null
}

# Fails if the repo has uncommitted changes, or commits that aren't on origin (or vice versa).
function Assert-GitClean($dir) {
    $name = Split-Path -Leaf $dir
    if (-not (Test-Path (Join-Path $dir '.git'))) { Fail "$name is not a git repo." }
    $dirty = git -C $dir status --porcelain
    if ($dirty) { Fail "$name has uncommitted changes:`n$($dirty -join "`n")`nCommit and push them first." }
    git -C $dir fetch --quiet origin 2>$null
    $ahead = git -C $dir rev-list --count '@{u}..HEAD' 2>$null
    if ($LASTEXITCODE -ne 0) { Fail "$name has no upstream branch. Push it first (git push -u origin main)." }
    if ([int]$ahead -gt 0) { Fail "$name has $ahead unpushed commit(s). Run: git -C `"$dir`" push" }
    $behind = git -C $dir rev-list --count 'HEAD..@{u}' 2>$null
    if ([int]$behind -gt 0) { Fail "$name is $behind commit(s) behind origin. Run: git -C `"$dir`" pull" }
    Write-Host "  $name OK"
}

$python = Find-Exe 'python' @("$env:LOCALAPPDATA\Programs\Python\Python312\python.exe")
if (-not $python) { $python = Find-Exe 'py' @() }
if (-not $python) { Fail 'Python not found.' }

if (-not $BuildOnly) {
    Step 'Checking repo is committed and pushed'
    Assert-GitClean $installerDir

    Step "Bumping version ($Bump)"
    $pyw = Join-Path $installerDir 'valheim_mod_installer.pyw'
    $src = [System.IO.File]::ReadAllText($pyw)
    $m = [regex]::Match($src, '(?m)^APP_VERSION = "(\d+)\.(\d+)\.(\d+)"')
    if (-not $m.Success) { Fail 'Could not find APP_VERSION = "X.Y.Z" in valheim_mod_installer.pyw.' }
    $old = "$($m.Groups[1].Value).$($m.Groups[2].Value).$($m.Groups[3].Value)"
    [int]$maj = $m.Groups[1].Value; [int]$min = $m.Groups[2].Value; [int]$pat = $m.Groups[3].Value
    switch ($Bump) {
        'major' { $maj++; $min = 0; $pat = 0 }
        'minor' { $min++; $pat = 0 }
        'patch' { $pat++ }
    }
    $new = "$maj.$min.$pat"
    $src = $src.Substring(0, $m.Index) + "APP_VERSION = `"$new`"" + $src.Substring($m.Index + $m.Length)
    [System.IO.File]::WriteAllText($pyw, $src, $utf8)
    Write-Host "  $old -> $new"

    Step "Committing and pushing v$new"
    $msg = "Release v$new"
    if ($Changes) { $msg += "`n`n$Changes" }
    git -C $installerDir add -A
    git -C $installerDir commit -q -m $msg
    if ($LASTEXITCODE -ne 0) { Fail 'git commit failed.' }
    git -C $installerDir push -q origin HEAD
    if ($LASTEXITCODE -ne 0) { Fail 'git push failed (the version bump is committed locally).' }
    Write-Host "  pushed"
}

Step ($(if ($BuildOnly) { 'Building package' } else { 'Building and publishing package' }))
$makeArgs = @((Join-Path $PSScriptRoot 'make_release.py'))
if ($BuildOnly) { $makeArgs += '--build-only' }
& $python @makeArgs
if ($LASTEXITCODE -ne 0) { Fail 'make_release.py failed (see above).' }

Write-Host "`nRelease process finished." -ForegroundColor Green
