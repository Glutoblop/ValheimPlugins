# Full release process for the Valheim Mod Installer and the sibling plugins.
#
#   release\Release.bat                 (double-click)
#   powershell -File release\release.ps1 [-BuildOnly] [-SkipBuild]
#
# 1. Builds every sibling plugin (..\<Plugin>\*.csproj) with `dotnet build -c Release`.
# 2. Checks every repo (installer + plugins) is committed and pushed to origin/main, because
#    plugin.json/dist DLLs are served from main and releases are tagged from main.
# 3. Runs release\make_release.py: builds the zips into release\out and publishes any version
#    that doesn't have a GitHub release yet (bump APP_VERSION / plugin.json + Plugin.cs first).

param(
    [switch]$BuildOnly,   # build the packages but don't publish anything
    [switch]$SkipBuild    # don't run dotnet build (use the DLLs already in dist\)
)

$ErrorActionPreference = 'Stop'
$installerDir = Split-Path -Parent $PSScriptRoot
$pluginsDir   = Split-Path -Parent $installerDir

function Step($text) { Write-Host "`n== $text" -ForegroundColor Cyan }
function Fail($text) { Write-Host "`nRELEASE STOPPED: $text" -ForegroundColor Red; exit 1 }

function Find-Exe($name, $fallbacks) {
    $c = Get-Command $name -ErrorAction SilentlyContinue
    if ($c) { return $c.Source }
    foreach ($f in $fallbacks) { if (Test-Path $f) { return $f } }
    return $null
}

# Fails if the repo has uncommitted changes or commits that aren't on origin.
function Assert-GitClean($dir) {
    $name = Split-Path -Leaf $dir
    if (-not (Test-Path (Join-Path $dir '.git'))) { Write-Host "  $name (not a git repo, skipped)"; return }
    $dirty = git -C $dir status --porcelain
    if ($dirty) { Fail "$name has uncommitted changes:`n$($dirty -join "`n")`nCommit and push them first." }
    git -C $dir fetch --quiet origin 2>$null
    $ahead = git -C $dir rev-list --count '@{u}..HEAD' 2>$null
    if ($LASTEXITCODE -ne 0) { Fail "$name has no upstream branch. Push it first (git push -u origin main)." }
    if ([int]$ahead -gt 0) { Fail "$name has $ahead unpushed commit(s). Run: git -C `"$dir`" push" }
    Write-Host "  $name OK"
}

$python = Find-Exe 'python' @("$env:LOCALAPPDATA\Programs\Python\Python312\python.exe")
if (-not $python) { $python = Find-Exe 'py' @() }
if (-not $python) { Fail 'Python not found.' }

$plugins = Get-ChildItem $pluginsDir -Directory |
    Where-Object { (Test-Path (Join-Path $_.FullName 'plugin.json')) -and $_.FullName -ne $installerDir }

if (-not $SkipBuild) {
    $dotnet = Find-Exe 'dotnet' @("$env:ProgramFiles\dotnet\dotnet.exe")
    if (-not $dotnet) { Fail '.NET SDK not found (winget install Microsoft.DotNet.SDK.8).' }
    Step 'Building plugins'
    foreach ($p in $plugins) {
        $csproj = Get-ChildItem $p.FullName -Filter *.csproj | Select-Object -First 1
        if (-not $csproj) { Write-Host "  $($p.Name): no .csproj, using existing dist\"; continue }
        # Builds aren't byte-identical, so only rebuild when the source is newer than dist\ -
        # otherwise every run would produce an uncommitted DLL change.
        $dll = Get-ChildItem (Join-Path $p.FullName 'dist') -Filter *.dll -ErrorAction SilentlyContinue | Select-Object -First 1
        $inputs = @(Get-ChildItem (Join-Path $p.FullName 'src') -Recurse -File -ErrorAction SilentlyContinue) + @($csproj)
        $newest = ($inputs | Measure-Object LastWriteTimeUtc -Maximum).Maximum
        if ($dll -and $dll.LastWriteTimeUtc -ge $newest) { Write-Host "  $($p.Name): up to date"; continue }
        Write-Host "  $($p.Name): building"
        & $dotnet build $csproj.FullName -c Release --nologo -v quiet
        if ($LASTEXITCODE -ne 0) { Fail "$($p.Name) failed to build." }
    }
}

if (-not $BuildOnly) {
    Step 'Checking repos are committed and pushed'
    Assert-GitClean $installerDir
    foreach ($p in $plugins) { Assert-GitClean $p.FullName }   # also catches a rebuilt dist DLL that isn't committed
}

Step ($(if ($BuildOnly) { 'Building packages' } else { 'Building and publishing packages' }))
$makeArgs = @((Join-Path $PSScriptRoot 'make_release.py'), '--plugins-dir', $pluginsDir)
if ($BuildOnly) { $makeArgs += '--build-only' }
& $python @makeArgs
if ($LASTEXITCODE -ne 0) { Fail 'make_release.py failed (see above).' }

Write-Host "`nRelease process finished." -ForegroundColor Green
