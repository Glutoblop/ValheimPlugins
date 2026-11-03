# Valheim Mod Installer launcher (Windows).
# Finds a Python 3 with Tkinter, installs Python if needed (with permission), then starts the GUI
# with pythonw so no console window stays open.

$ErrorActionPreference = 'Stop'
$here   = Split-Path -Parent $MyInvocation.MyCommand.Path
$script = Join-Path $here 'valheim_mod_installer.pyw'
$title  = 'Valheim Mod Installer'
$pythonVersion = '3.12.10'

Add-Type -AssemblyName System.Windows.Forms | Out-Null

function Show-Message($text, $icon = 'Information', $buttons = 'OK') {
    return [System.Windows.Forms.MessageBox]::Show($text, $title, $buttons, $icon)
}

# Returns the full path of a pythonw.exe whose python can import tkinter, or $null.
function Find-PythonW {
    $candidates = @()
    foreach ($cmd in @(@('py', '-3'), @('python'), @('python3'))) {
        $exe = Get-Command $cmd[0] -ErrorAction SilentlyContinue
        if (-not $exe) { continue }
        # Skip the Microsoft Store "python.exe" stub that just opens the Store.
        if ($exe.Source -like '*\WindowsApps\*') { continue }
        $candidates += , $cmd
    }
    foreach ($dir in @("$env:LOCALAPPDATA\Programs\Python", "$env:ProgramFiles\Python*", "${env:ProgramFiles(x86)}\Python*")) {
        Get-ChildItem -Path $dir -Filter python.exe -Recurse -Depth 2 -ErrorAction SilentlyContinue |
            Sort-Object FullName -Descending | ForEach-Object { $candidates += , @($_.FullName) }
    }
    foreach ($c in $candidates) {
        try {
            $extra = @()
            if ($c.Count -gt 1) { $extra = $c[1..($c.Count - 1)] }
            $out = & $c[0] @extra -c "import sys, tkinter; assert sys.version_info >= (3, 8); print(sys.executable)" 2>$null
            if ($LASTEXITCODE -eq 0 -and $out) {
                $pyw = Join-Path (Split-Path -Parent ($out | Select-Object -Last 1)) 'pythonw.exe'
                if (Test-Path $pyw) { return $pyw }
            }
        } catch { }
    }
    return $null
}

function Install-Python {
    $answer = Show-Message ("Python 3 is needed to run the installer and wasn't found on this PC.`n`n" +
        "Install Python $pythonVersion now? (one-time, about 30 MB, installs just for your user account)") 'Question' 'YesNo'
    if ($answer -ne 'Yes') { return $false }

    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if ($winget) {
        & winget install --exact --id Python.Python.3.12 --scope user --silent `
            --accept-package-agreements --accept-source-agreements | Out-Null
        if (Find-PythonW) { return $true }
    }

    # Fallback: official installer from python.org, silent per-user install with Tkinter and the py launcher.
    $arch = if ([Environment]::Is64BitOperatingSystem) { '-amd64' } else { '' }
    $url  = "https://www.python.org/ftp/python/$pythonVersion/python-$pythonVersion$arch.exe"
    $tmp  = Join-Path $env:TEMP "python-$pythonVersion-installer.exe"
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -Uri $url -OutFile $tmp -UseBasicParsing
    $p = Start-Process -FilePath $tmp -Wait -PassThru -ArgumentList @(
        '/quiet', 'InstallAllUsers=0', 'PrependPath=1', 'Include_tcltk=1', 'Include_launcher=1', 'Include_test=0')
    Remove-Item $tmp -ErrorAction SilentlyContinue
    return ($p.ExitCode -eq 0)
}

try {
    if (-not (Test-Path $script)) {
        Show-Message "Can't find valheim_mod_installer.pyw next to this launcher.`nKeep all the installer files together in one folder." 'Error' | Out-Null
        exit 1
    }
    $pyw = Find-PythonW
    if (-not $pyw) {
        if (-not (Install-Python)) {
            Show-Message "Python wasn't installed. Install Python 3 from https://www.python.org/downloads/ and run this again." 'Warning' | Out-Null
            exit 1
        }
        $pyw = Find-PythonW
        if (-not $pyw) {
            Show-Message "Python was installed but couldn't be located. Please run this launcher again." 'Warning' | Out-Null
            exit 1
        }
    }
    Start-Process -FilePath $pyw -ArgumentList @("`"$script`"") -WorkingDirectory $here
} catch {
    Show-Message ("Launcher error:`n`n" + $_.Exception.Message) 'Error' | Out-Null
    exit 1
}
