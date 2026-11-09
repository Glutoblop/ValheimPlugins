# Valheim Mod Installer launcher (Windows).
# Starts the GUI with pythonw, then looks for or offers to install Python if startup fails.

$ErrorActionPreference = 'Stop'
$here   = Split-Path -Parent $MyInvocation.MyCommand.Path
$script = Join-Path $here 'valheim_mod_installer.pyw'
$title  = 'Valheim Mod Installer'
$pythonVersion = '3.12.10'
$attempted = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)

function Show-Message($text, $icon = 'Information', $buttons = 'OK') {
    Add-Type -AssemblyName System.Windows.Forms | Out-Null
    return [System.Windows.Forms.MessageBox]::Show($text, $title, $buttons, $icon)
}

# Observe early exits without waiting for the installer to close.
function Start-WithPythonW($pyw) {
    if (-not $attempted.Add($pyw)) { return $false }
    try {
        $process = Start-Process -FilePath $pyw -ArgumentList @("`"$script`"", '--check-for-updates') `
            -WorkingDirectory $here -PassThru
    } catch {
        return $false
    }

    try {
        $timer = [Diagnostics.Stopwatch]::StartNew()
        do {
            if ($process.WaitForExit(100)) { return ($process.ExitCode -eq 0) }
            $process.Refresh()
        } while ($process.MainWindowHandle -eq [IntPtr]::Zero -and $timer.ElapsedMilliseconds -lt 10000)
        return $true
    } catch [InvalidOperationException] {
        if ($process.HasExited) { return ($process.ExitCode -eq 0) }
        throw
    } finally {
        $process.Dispose()
    }
}

function Start-WithDiscoveredPython {
    foreach ($cmd in @(@('py', '-3'), @('python'), @('python3'))) {
        $exe = Get-Command $cmd[0] -CommandType Application -ErrorAction SilentlyContinue
        if (-not $exe) { continue }
        # Skip the Microsoft Store "python.exe" stub that just opens the Store.
        if ($exe.Source -like '*\WindowsApps\*') { continue }
        try {
            $extra = @()
            if ($cmd.Count -gt 1) { $extra = $cmd[1..($cmd.Count - 1)] }
            $out = & $exe.Source @extra -c "import sys; print(sys.executable)" 2>$null
        } catch { continue }
        if ($LASTEXITCODE -eq 0 -and $out) {
            $pyw = Join-Path (Split-Path -Parent ($out | Select-Object -Last 1)) 'pythonw.exe'
            if ((Test-Path -LiteralPath $pyw) -and (Start-WithPythonW $pyw)) { return $true }
        }
    }

    $dirs = @("$env:LOCALAPPDATA\Programs\Python")
    foreach ($root in @($env:ProgramFiles, ${env:ProgramFiles(x86)})) {
        if ($root) {
            $dirs += Get-ChildItem -LiteralPath $root -Directory -Filter 'Python*' -ErrorAction SilentlyContinue |
                Select-Object -ExpandProperty FullName
        }
    }
    foreach ($dir in $dirs) {
        $candidates = Get-ChildItem -LiteralPath $dir -File -Filter pythonw.exe -Recurse -Depth 2 `
            -ErrorAction SilentlyContinue | Sort-Object FullName -Descending
        foreach ($candidate in $candidates) {
            if (Start-WithPythonW $candidate.FullName) { return $true }
        }
    }
    return $false
}

function Install-Python {
    $answer = Show-Message ("Python 3 is needed to run the installer and wasn't found on this PC.`n`n" +
        "Install Python $pythonVersion now? (one-time, about 30 MB, installs just for your user account)") 'Question' 'YesNo'
    if ($answer -ne 'Yes') { return $false }

    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if ($winget) {
        & winget install --exact --id Python.Python.3.12 --scope user --silent `
            --accept-package-agreements --accept-source-agreements | Out-Null
        $attempted.Clear()
        if (Start-WithDiscoveredPython) { return $true }
    }

    # Fallback: official installer from python.org, silent per-user install with Tkinter and the py launcher.
    $arch = if ([Environment]::Is64BitOperatingSystem) { '-amd64' } else { '' }
    $url  = "https://www.python.org/ftp/python/$pythonVersion/python-$pythonVersion$arch.exe"
    $tmp  = Join-Path $env:TEMP "python-$pythonVersion-installer.exe"
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -Uri $url -OutFile $tmp -UseBasicParsing
    $p = Start-Process -FilePath $tmp -Wait -PassThru -ArgumentList @(
        '/quiet', 'InstallAllUsers=0', 'PrependPath=1', 'Include_tcltk=1', 'Include_launcher=1', 'Include_test=0') `
        -WindowStyle Hidden
    Remove-Item $tmp -ErrorAction SilentlyContinue
    if ($p.ExitCode -ne 0) { return $false }
    $attempted.Clear()
    return (Start-WithDiscoveredPython)
}

try {
    if (-not (Test-Path -LiteralPath $script)) {
        Show-Message "Can't find valheim_mod_installer.pyw next to this launcher.`nKeep all the installer files together in one folder." 'Error' | Out-Null
        exit 1
    }
    $pythonw = Get-Command pythonw.exe -CommandType Application -ErrorAction SilentlyContinue
    if ($pythonw -and $pythonw.Source -notlike '*\WindowsApps\*' -and (Start-WithPythonW $pythonw.Source)) {
        exit 0
    }
    if (Start-WithDiscoveredPython) { exit 0 }
    if (Install-Python) { exit 0 }
    Show-Message "Couldn't start the installer. Install Python 3 with Tkinter from https://www.python.org/downloads/ and run this again." 'Warning' | Out-Null
    exit 1
} catch {
    Show-Message ("Launcher error:`n`n" + $_.Exception.Message) 'Error' | Out-Null
    exit 1
}
