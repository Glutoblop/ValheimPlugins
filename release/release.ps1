# Full release process for the Valheim Mod Installer and the sibling plugins.
#
#   release\Release.bat                 (double-click)
#   powershell -File release\release.ps1 [-BuildOnly] [-SkipBuild] [-NoPlugins]
#                                        [-Bump patch|minor|major] [-Changes "text"]
#
# 1. Checks the installer repo is committed and pushed to origin/main.
# 2. Releases every sibling plugin (..\<Plugin>\release\release.ps1): each one bumps its patch
#    version, rebuilds, commits + pushes "Release vX.Y.Z" and publishes its GitHub release.
# 3. Bumps the installer's APP_VERSION (patch by default) in valheim_mod_installer.pyw,
#    commits + pushes "Release vX.Y.Z".
# 4. Runs release\make_release.py --installer-only: builds ValheimModInstaller.zip and publishes it.
#
# -BuildOnly skips bumping, committing and publishing: it just builds every package into release\out.
# -NoPlugins releases only the installer. -Bump/-Changes apply to the installer (plugins always bump patch).

param(
    [switch]$BuildOnly,
    [switch]$SkipBuild,
    [switch]$NoPlugins,
    [ValidateSet('patch', 'minor', 'major')][string]$Bump = 'patch',
    [string]$Changes = ''
)

$ErrorActionPreference = 'Stop'
$installerDir = Split-Path -Parent $PSScriptRoot
$pluginsDir   = Split-Path -Parent $installerDir
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

$plugins = @()
if (-not $NoPlugins) {
    $plugins = @(Get-ChildItem $pluginsDir -Directory |
        Where-Object { (Test-Path (Join-Path $_.FullName 'plugin.json')) -and $_.FullName -ne $installerDir })
    foreach ($p in $plugins) {
        if (-not (Test-Path (Join-Path $p.FullName 'release\release.ps1'))) {
            Fail "$($p.Name) has no release\release.ps1 (copy it from another plugin)."
        }
    }
}

if (-not $BuildOnly) {
    Step 'Checking installer repo is committed and pushed'
    Assert-GitClean $installerDir
}

# Each plugin's own script does its checks, bump, build, commit/push and publish.
foreach ($p in $plugins) {
    Step "Plugin: $($p.Name)"
    $pargs = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', (Join-Path $p.FullName 'release\release.ps1'))
    if ($BuildOnly) { $pargs += '-BuildOnly' }
    if ($SkipBuild) { $pargs += '-SkipBuild' }
    & powershell.exe @pargs
    if ($LASTEXITCODE -ne 0) { Fail "$($p.Name) release failed (see above)." }
}

if (-not $BuildOnly) {
    Step "Bumping installer version ($Bump)"
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

    Step "Committing and pushing installer v$new"
    $msg = "Release v$new"
    if ($Changes) { $msg += "`n`n$Changes" }
    git -C $installerDir add -A
    git -C $installerDir commit -q -m $msg
    if ($LASTEXITCODE -ne 0) { Fail 'git commit failed.' }
    git -C $installerDir push -q origin HEAD
    if ($LASTEXITCODE -ne 0) { Fail 'git push failed (the version bump is committed locally).' }
    Write-Host "  pushed"
}

Step ($(if ($BuildOnly) { 'Building packages' } else { 'Building and publishing installer' }))
$makeArgs = @((Join-Path $PSScriptRoot 'make_release.py'), '--plugins-dir', $pluginsDir)
if ($BuildOnly) { $makeArgs += '--build-only' } else { $makeArgs += '--installer-only' }
if ($NoPlugins) { $makeArgs += '--installer-only' }
& $python @makeArgs
if ($LASTEXITCODE -ne 0) { Fail 'make_release.py failed (see above).' }

Write-Host "`nRelease process finished." -ForegroundColor Green
